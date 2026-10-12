"""
ClinSeekAgent text evaluation driver.

Runs the agentic evidence-seeking loop (or the single-pass retrieval baseline)
over a benchmark manifest. The model reaches the patient record only through
the EHR MCP tools (16 agent-exposed tools); browser tools are an optional,
non-default extra (``--enable_browser``). Backends: AWS Bedrock, Google Vertex
AI (Claude), and any OpenAI-compatible endpoint (``--backend vllm``).
"""
import os
import json
import asyncio
import datetime
import argparse
import re
from typing import List, Dict, Any, Optional, Tuple
import traceback

from browser_tool import BrowserTool, LocalServiceBrowserBackend, SerperServiceBrowserBackend
from ehr_tool_pool import EHRToolPool
from prompts_text import (
    DEVELOPER_CONTENT_CLAUDE,
    DEVELOPER_CONTENT_CLAUDE_EHR_ONLY,
    SFT_MODEL_PROMPT,
    COMBINED_TOOL_CONTENT_FULL,
    EHR_ONLY_TOOL_CONTENT_FULL,
)
import dotenv

# Verbose flag
VERBOSE = False
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"
DEFAULT_BEDROCK_REGION = "us-east-1"
DEFAULT_VLLM_BASE_URL = "http://127.0.0.1:4000"
DEFAULT_VLLM_API_KEY = "EMPTY"
# Ceiling on parallel queries; raise it via the environment when a run owns a
# dedicated EHR MCP server. The default of 20 protects a server shared by
# several runs from exhausting its connection pool.
MAX_PARALLEL_QUERIES = int(os.environ.get("CLINSEEK_MAX_PARALLEL_QUERIES", "20"))
DEFAULT_MAX_TOOL_RESULT_CHARS = 100000
# Paper setting: 8,192 output tokens per model call.
DEFAULT_MAX_TOKENS = 8192

BEDROCK_MODEL_ALIASES = {
    "anthropic.claude-opus-4-6-v1": "us.anthropic.claude-opus-4-6-v1",
}

def vprint(*args, **kwargs):
    """Verbose print: only prints when VERBOSE is True."""
    if VERBOSE:
        print(*args, **kwargs)


def mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:6]}...{secret[-4:]}"


def configure_bedrock_auth(api_key: str | None) -> str | None:
    token = (
        api_key
        or os.getenv("BEDROCK_API_KEY")
        or os.getenv("AWS_BEARER_TOKEN_BEDROCK")
    )
    if not token:
        return None

    os.environ["BEDROCK_API_KEY"] = token
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token
    return token


def resolve_bedrock_model_id(model_id: str) -> str:
    return BEDROCK_MODEL_ALIASES.get(model_id, model_id)


def mask_optional_secret(secret: str | None) -> str:
    if not secret:
        return "(empty)"
    return mask_secret(secret)


def normalize_browser_tool_args(tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce numeric browser arguments that Bedrock may emit as strings."""
    if not isinstance(tool_args, dict):
        return tool_args

    normalized = dict(tool_args)
    int_fields_by_tool = {
        "open": ("id", "cursor", "loc", "num_lines"),
        "find": ("cursor",),
        "search": ("topn", "top_n"),
    }

    for field in int_fields_by_tool.get(tool_name.lower(), ()):
        value = normalized.get(field)
        if not isinstance(value, str):
            continue

        cleaned = value.strip()
        if field == "id" and len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            cleaned = cleaned[1:-1].strip()

        if cleaned.lstrip("-").isdigit():
            normalized[field] = int(cleaned)
        else:
            normalized[field] = cleaned

    return normalized


def normalize_tool_call_name(function_name: str) -> str:
    name = (function_name or "").strip()
    if not name:
        return name

    if name.startswith("browser."):
        return name
    if name.startswith("browser_"):
        suffix = name[len("browser_"):].strip("_")
        return f"browser.{suffix}" if suffix else "browser.search"
    if name in {"search", "open", "find"}:
        return f"browser.{name}"

    if name.startswith("ehr."):
        return name
    if name.startswith("ehr_"):
        suffix = name[len("ehr_"):].strip("_")
        return f"ehr.{suffix}" if suffix else "ehr."
    if "." not in name:
        return f"ehr.{name}"
    return name


def normalize_tool_calls(tool_calls: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    normalized_tool_calls: List[Dict[str, Any]] = []

    for tool_call in tool_calls or []:
        normalized_tool_call = dict(tool_call)
        function = dict(tool_call.get("function") or {})
        function["name"] = normalize_tool_call_name(function.get("name", ""))
        normalized_tool_call["function"] = function
        normalized_tool_calls.append(normalized_tool_call)

    return normalized_tool_calls


def tool_call_batch_signature(
    tool_calls: List[Dict[str, Any]] | None,
) -> Tuple[Tuple[str, str], ...]:
    """Return an ID/whitespace-independent signature for one tool-call turn.

    Tool-call IDs change on every generation and JSON object key order is not
    semantically meaningful, so neither participates in loop detection. Call
    order and duplicate calls do participate: a turn is considered identical
    only when its complete ordered batch of tool names and arguments matches.
    """
    signature = []
    for tool_call in tool_calls or []:
        function = tool_call.get("function") or {}
        name = normalize_tool_call_name(str(function.get("name") or ""))
        arguments = function.get("arguments", "")
        if isinstance(arguments, str):
            raw_arguments = arguments.strip()
            try:
                arguments = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError):
                canonical_arguments = f"raw:{raw_arguments}"
            else:
                canonical_arguments = json.dumps(
                    arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        else:
            canonical_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        signature.append((name, canonical_arguments))
    return tuple(signature)


def truncate_tool_result(result: Any, max_chars: int) -> str:
    """Cap tool results before sending them back to the model."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (dict, list)):
        text = json.dumps(result, ensure_ascii=False)
    else:
        text = str(result)

    if max_chars <= 0 or len(text) <= max_chars:
        return text

    notice = f""
    keep_chars = max_chars - len(notice)
    if keep_chars <= 0:
        return text[:max_chars]
    return text[:keep_chars] + notice


def _validate_records(records: Any, data_path: str, source_format: str) -> List[Dict[str, Any]]:
    if isinstance(records, dict):
        records = [records]

    if not isinstance(records, list):
        raise ValueError(
            f"{data_path} parsed as {source_format}, but the top-level value is {type(records).__name__}; "
            "expected an object or a list of objects"
        )

    for idx, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(
                f"{data_path} parsed as {source_format}, but item {idx} is {type(record).__name__}; "
                "expected every record to be a JSON object"
            )

    return records


def _load_json_records(raw_text: str, data_path: str) -> List[Dict[str, Any]]:
    if not raw_text.strip():
        return []
    return _validate_records(json.loads(raw_text), data_path, "JSON")


def _load_jsonl_records(raw_text: str, data_path: str) -> List[Dict[str, Any]]:
    records = []
    for line_number, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        record = json.loads(stripped)
        if not isinstance(record, dict):
            raise ValueError(
                f"{data_path} parsed as JSONL, but line {line_number} is {type(record).__name__}; "
                "expected each line to be a JSON object"
            )
        records.append(record)

    return records


def load_query_data(data_path: str) -> List[Dict[str, Any]]:
    with open(data_path, 'r', encoding='utf-8') as f:
        raw_text = f.read()

    extension = os.path.splitext(data_path)[1].lower()
    loaders = {
        ".json": [("JSON", _load_json_records), ("JSONL", _load_jsonl_records)],
        ".jsonl": [("JSONL", _load_jsonl_records), ("JSON", _load_json_records)],
    }.get(extension, [("JSON", _load_json_records), ("JSONL", _load_jsonl_records)])

    errors = []
    for format_name, loader in loaders:
        try:
            return loader(raw_text, data_path)
        except Exception as exc:
            errors.append(f"{format_name}: {exc}")

    raise ValueError(
        f"Unsupported input format for {data_path}. Expected a JSON object/list or JSONL records. "
        + " | ".join(errors)
    )


def resolve_qid(item: Dict[str, Any]) -> str:
    qid = item.get('qid') or item.get('query_id')
    if qid:
        return str(qid)

    subject_id = item.get("subject_id")
    hadm_id = item.get("hadm_id")
    task = item.get("task")
    if subject_id is not None and task:
        if hadm_id is not None:
            return f"{task}_{subject_id}_{hadm_id}"
        return f"{task}_{subject_id}"
    return "unknown"


def is_ehr_bench_record(item: Dict[str, Any], data_path: str = "") -> bool:
    """Identify EHR-Bench records without depending only on the file name.

    Evaluation subsets are often renamed when a run is resumed.  A renamed
    subset must retain the same agentic prompt construction as its source
    benchmark instead of falling back to an embedded single-turn question.
    """
    if "ehr_bench" in os.path.basename(data_path).lower():
        return True

    source_file = item.get("source_file")
    if isinstance(source_file, str) and "ehr_bench" in source_file.lower():
        return True

    qid = item.get("qid") or item.get("query_id")
    if isinstance(qid, str) and qid.lower().startswith("ehr_bench_"):
        return True

    task_info = item.get("task_info")
    if isinstance(task_info, dict):
        source_file = task_info.get("source_file")
        if isinstance(source_file, str) and "ehr_bench" in source_file.lower():
            return True

    return False


def resolve_question(item: Dict[str, Any], data_path: str = "") -> str:
    if is_ehr_bench_record(item, data_path):
        from prompts_text import generate_ehr_bench_prompt
        return generate_ehr_bench_prompt(item)

    if 'question' in item or 'query' in item:
        return item.get('question', item.get('query', ''))

    from prompts_text import generate_question_from_task
    return generate_question_from_task(item)


def attach_source_fields(result: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(result)
    for key, value in item.items():
        if key not in enriched:
            enriched[key] = value
    return enriched


class BrowserPool:
    """Browser tool pool manager."""
    def __init__(self, search_url, browser_backend='local', enabled: bool = True):
        self.search_url = search_url
        self.browser_backend = browser_backend
        # EHR-only ablation: no sessions are created and browser.* tools are
        # withheld from the tool list, so the model can never reach call_tool.
        self.enabled = enabled
        self.sessions: Dict[Any, BrowserTool] = {}

    def init_session(self, qid: Any) -> dict:
        if not self.enabled:
            return {}
        if self.browser_backend == 'serper':
            backend = SerperServiceBrowserBackend()
        else:
            backend = LocalServiceBrowserBackend(base_url=self.search_url)
        tool = BrowserTool(backend=backend)
        self.sessions[qid] = tool
        return tool.tool_config

    async def call_tool(self, qid: Any, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Call browser tool and return text result."""
        if not self.enabled:
            return (
                "Error: browser tools are disabled in this run. "
                "Answer using the EHR tools only."
            )
        tool = self.sessions[qid]

        # Map tool names to browser recipients
        recipient_map = {
            'search': 'browser.search',
            'find': 'browser.find',
            'open': 'browser.open'
        }

        recipient = recipient_map.get(tool_name.lower())
        if not recipient:
            return f"Unknown browser tool: {tool_name}"

        tool_args = normalize_browser_tool_args(tool_name, tool_args)

        # Create Harmony message with JSON args
        from openai_harmony import TextContent, Message, Role
        args_json = json.dumps(tool_args, ensure_ascii=False)
        tool_msg = Message.from_role_and_content(Role.ASSISTANT, TextContent(text=args_json))
        tool_msg.recipient = recipient

        # Execute tool and collect results
        results = []
        async for msg in tool.process(tool_msg):
            results.append(msg)

        # Extract text from Harmony messages
        text_parts = []
        for msg in results:
            if hasattr(msg, 'content') and isinstance(msg.content, list):
                for item in msg.content:
                    if hasattr(item, 'text'):
                        text_parts.append(item.text)
                    elif isinstance(item, dict) and 'text' in item:
                        text_parts.append(item['text'])
        return '\n'.join(text_parts) if text_parts else ""

    def cleanup(self, qid: Any):
        if qid in self.sessions:
            del self.sessions[qid]


def _message_has_finish_tool_call(message: Dict[str, Any]) -> bool:
    for tool_call in message.get("tool_calls") or []:
        function_name = (
            tool_call.get("function", {}).get("name", "").strip().lower()
        )
        if "finish" in function_name:
            return True
    return False


def summarize_conversation_completion(
    messages: List[Dict[str, Any]],
) -> tuple[bool, str]:
    last_assistant_message = None
    for message in reversed(messages):
        if message.get("role") == "assistant":
            last_assistant_message = message
            break

    if last_assistant_message is None:
        return False, "no_final_answer"

    if _message_has_finish_tool_call(last_assistant_message):
        return True, "finish_tool_call"

    return False, "no_final_answer"


def uses_legacy_clinseek_tool_format(model_name: str) -> bool:
    """Select the bracketed tool protocol only for older ClinSeek models."""
    model_name_lower = (model_name or "").lower()
    return (
        "clinseek" in model_name_lower
        and "clinseek-35b-a3b" not in model_name_lower
    )


async def run_one_native(
    question: str,
    qid: Any,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool = None,
    max_rounds: int = 200,
    temperature: float = 1.0,
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model_name: str = "",
    max_consecutive_identical_tool_rounds: int = 0,
) -> Tuple[List[dict], Optional[str]]:
    """
    Native API tool calling for Bedrock Claude with dual tool support.
    Supports both browser tools and EHR tools.
    """
    # Initialize browser session
    tool_config = browser_pool.init_session(qid)

    # Initialize EHR session if available
    if ehr_pool:
        await ehr_pool.init_session(qid)

    # Initialize tokenizer if needed
    if hasattr(generator, '_init_tokenizer'):
        await generator._init_tokenizer()

    # Use the bracketed tool-call prompt only for legacy ClinSeek SFT models.
    # The public ClinSeek-35B-A3B release supports native structured tools.
    browser_enabled = getattr(browser_pool, "enabled", True)
    if uses_legacy_clinseek_tool_format(model_name):
        system_prompt = SFT_MODEL_PROMPT
    elif not browser_enabled:
        system_prompt = DEVELOPER_CONTENT_CLAUDE_EHR_ONLY
    else:
        system_prompt = DEVELOPER_CONTENT_CLAUDE
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": question,
        }
    ]

    # Tool list: the 16 EHR tools, plus the 3 browser tools when the optional
    # browser extra is enabled.
    tools = json.loads(
        COMBINED_TOOL_CONTENT_FULL if browser_enabled
        else EHR_ONLY_TOOL_CONTENT_FULL
    )
    candidate_table_tool_calls = 0
    browser_tool_calls = 0
    max_browser_tool_calls = 40
    previous_tool_signature = None
    identical_tool_round_streak = 0
    loop_stop_reason = None

    round_num = 0

    try:
        while round_num < max_rounds:
            round_num += 1

            print(f"\n[qid={qid}] {'='*50}")
            print(f"[qid={qid}] Round {round_num}/{max_rounds} | msgs_so_far={len(messages)}")
            print(f"[qid={qid}] {'='*50}", flush=True)

            # Call chat completion with tools
            response = await generator.chat_completion(
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens
            )

            # Extract message from response
            message = response["choices"][0]["message"]
            content = message.get("content", "")
            reasoning_content = (message.get("reasoning_content") or "").strip()
            tool_calls = message.get("tool_calls", [])
            parse_error = (message.get("parse_error") or "").strip()

            preview_text = content or reasoning_content
            preview_text = preview_text[:2000] if len(preview_text) > 2000 else preview_text
            preview_label = "CONTENT" if content else "REASONING"
            print(
                f"[qid={qid}] Round {round_num} MODEL RESPONSE: "
                f"content_len={len(content)}, reasoning_len={len(reasoning_content)}, "
                f"tool_calls={len(tool_calls)}"
            )
            print(
                f"[qid={qid}] Round {round_num} {preview_label} PREVIEW: {preview_text!r}",
                flush=True,
            )
            if reasoning_content and content:
                reasoning_preview = (
                    reasoning_content[:2000]
                    if len(reasoning_content) > 2000
                    else reasoning_content
                )
                print(
                    f"[qid={qid}] Round {round_num} REASONING PREVIEW: "
                    f"{reasoning_preview!r}",
                    flush=True,
                )
            if parse_error and not tool_calls:
                print(
                    f"[qid={qid}] Round {round_num} PARSE ERROR: {parse_error}",
                    flush=True,
                )

            normalized_tool_calls = normalize_tool_calls(tool_calls)

            if normalized_tool_calls:
                tool_signature = tool_call_batch_signature(normalized_tool_calls)
                if tool_signature == previous_tool_signature:
                    identical_tool_round_streak += 1
                else:
                    previous_tool_signature = tool_signature
                    identical_tool_round_streak = 1
            else:
                previous_tool_signature = None
                identical_tool_round_streak = 0

            # Add assistant message
            assistant_message = {
                "role": "assistant",
                "content": content,
                "tool_calls": normalized_tool_calls if normalized_tool_calls else None
            }
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            if message.get("bedrock_content_blocks"):
                assistant_message["bedrock_content_blocks"] = message["bedrock_content_blocks"]
            if message.get("raw_response") is not None:
                assistant_message["raw_response"] = message["raw_response"]
            messages.append(assistant_message)

            if not tool_calls:
                print(
                    f"[qid={qid}] Round {round_num}: No tool calls detected; "
                    "stopping without a reminder",
                    flush=True,
                )
                break

            finish_tool_called = False
            for tc_idx, (raw_tool_call, tool_call) in enumerate(
                zip(tool_calls, normalized_tool_calls)
            ):
                tool_id = tool_call["id"]
                raw_function_name = raw_tool_call["function"]["name"]
                function_name = tool_call["function"]["name"]
                function_args_raw = tool_call["function"]["arguments"]

                try:
                    # Parse arguments
                    if isinstance(function_args_raw, dict):
                        function_args = function_args_raw
                    else:
                        function_args = json.loads(function_args_raw)
                    # if function_name != raw_function_name:
                    #     print(
                    #         f"[qid={qid}] Round {round_num} TOOL_NAME_NORMALIZE[{tc_idx}]: "
                    #         f"{raw_function_name!r} -> {function_name!r}",
                    #         flush=True,
                    #     )

                    print(f"[qid={qid}] Round {round_num} TOOL_CALL[{tc_idx}]: {function_name}({json.dumps(function_args, ensure_ascii=False)[:200]})", flush=True)

                    # Route to appropriate tool pool
                    if function_name.startswith("ehr."):
                        # EHR tool execution
                        if ehr_pool:
                            actual_function_name = function_name.split(".", 1)[1]
                            result = await ehr_pool.call_tool(qid, actual_function_name, function_args)
                        else:
                            result = "Error: EHR tools not available. Start with --enable_ehr flag."

                    elif function_name.startswith("browser."):
                        # Browser tool execution
                        browser_tool_calls += 1
                        actual_function_name = function_name.split(".", 1)[1]
                        result = await browser_pool.call_tool(qid, actual_function_name, function_args)
                        if not result:
                            result = f"{function_name} completed"
                    else:
                        result = f"Unknown tool namespace: {function_name}"

                    original_result_len = len(result) if isinstance(result, str) else None
                    result = truncate_tool_result(result, max_tool_result_chars)

                    # Add tool response
                    tool_message = {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tool_id,
                    }
                    messages.append(tool_message)

                    result_preview = result[:200] if len(result) > 200 else result
                    if original_result_len is not None and original_result_len > len(result):
                        print(
                            f"[qid={qid}] Round {round_num} TOOL_RESULT_TRUNCATED[{tc_idx}]: "
                            f"{original_result_len} -> {len(result)} chars",
                            flush=True,
                        )
                    print(f"[qid={qid}] Round {round_num} TOOL_RESULT[{tc_idx}]: len={len(result)}, preview={result_preview!r}", flush=True)

                    actual_finish_name = (
                        function_name.split(".", 1)[1]
                        if function_name.startswith("ehr.")
                        else function_name
                    )
                    if actual_finish_name == "finish":
                        finish_tool_called = True

                except Exception as e:
                    error_msg = f"Error executing {function_name}: {str(e)}"
                    error_msg = truncate_tool_result(error_msg, max_tool_result_chars)
                    print(f"[qid={qid}] Round {round_num} TOOL_ERROR[{tc_idx}]: {error_msg}", flush=True)
                    error_message = {
                        "role": "tool",
                        "content": error_msg,
                        "tool_call_id": tool_id,
                    }
                    messages.append(error_message)

            if finish_tool_called:
                print(f"[qid={qid}] DONE Round {round_num}: ehr.finish called", flush=True)
                break

            if (
                max_consecutive_identical_tool_rounds > 0
                and identical_tool_round_streak
                >= max_consecutive_identical_tool_rounds
            ):
                loop_stop_reason = "consecutive_identical_tool_rounds"
                print(
                    f"[qid={qid}] CIRCUIT BREAKER Round {round_num}: "
                    f"the same ordered tool-call batch repeated for "
                    f"{identical_tool_round_streak} consecutive rounds "
                    f"(limit={max_consecutive_identical_tool_rounds}) - stopping",
                    flush=True,
                )
                break

            if browser_tool_calls >= max_browser_tool_calls:
                print(
                    f"[qid={qid}] ⛔ Round {round_num}: browser tool call limit reached "
                    f"({browser_tool_calls}/{max_browser_tool_calls}) - stopping",
                    flush=True,
                )
                break

            # Continue to next round
            continue

        print(f"[qid={qid}] Finished after {round_num} rounds, total messages={len(messages)}", flush=True)
        return messages, loop_stop_reason

    finally:
        browser_pool.cleanup(qid)
        if ehr_pool:
            await ehr_pool.cleanup(qid)


# ─────────────────────────────────────────────────────────────────────────────
# Single-pass retrieval (``--retrieval_mode single_shot``)
#
# The non-iterative tool baseline: same model, same tools, same full-history
# access, but ONE query plan instead of an iterative loop. Isolates iterative
# planning (observe -> revise -> re-query) from mere tool access.
#
# Three phases:
#   0. schema orientation, pre-executed and free — deterministic, patient-
#      independent metadata, not evidence, so it does not count against the
#      single shot. Without it the model burns its only turn on load_ehr +
#      get_table_names and retrieves nothing (a strawman).
#   1. THE single retrieval turn — every query the model wants, in one turn.
#   2. answer — tools restricted to ehr.finish, so "single-shot" is enforced
#      mechanically, not by a prompt the model can ignore.
# ─────────────────────────────────────────────────────────────────────────────

# Deterministic patient metadata, executed for the model in phase 0.
_ORIENTATION_TOOLS = {"load_ehr", "get_table_names", "get_column_names",
                      "get_table_description"}

_SINGLE_SHOT_DIRECTIVE = """

---
IMPORTANT — SINGLE RETRIEVAL OPPORTUNITY.
The patient's database has already been loaded and its schema is shown above
under <schema_orientation>. You now get exactly ONE turn to retrieve evidence:
issue every query you need in this single turn as a batch of tool calls. There
is no second retrieval round — after this turn you will be asked only to submit
your final answer. Plan the whole retrieval now.
"""


def _extract_subject_and_time(question: str):
    """Pull subject_id and prediction time out of the rendered prompt.

    Both prompt templates (generate_question_from_task and EHR_Bench_Prompt)
    carry `Current Time: <ts>` and `Patient Subject ID: <id>`, so this works
    for either benchmark without threading the raw row through the call chain.
    """
    sid = re.search(r"Patient Subject ID:\s*(\S+)", question)
    ts = re.search(r"Current Time:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:]+)",
                   question)
    return (sid.group(1) if sid else None,
            ts.group(1).strip() if ts else None)


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


async def _run_orientation(ehr_pool, qid, subject_id, timestamp,
                           max_tool_result_chars):
    """Execute the free schema-orientation tools; return a context block."""
    blocks = []

    async def call(name, args):
        result = await ehr_pool.call_tool(qid, name, args)
        return truncate_tool_result(result, max_tool_result_chars)

    load_log = await call("load_ehr", {"subject_id": subject_id,
                                       "timestamp": timestamp})
    blocks.append(f"### load_ehr\n{load_log}")

    table_list = await call("get_table_names", {"subject_id": subject_id})
    blocks.append(f"### get_table_names\n{table_list}")

    # A column listing per table so the model can write precise queries in its
    # one turn. Table names are parsed leniently from the tool's text output.
    tables = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", table_list)
    seen, ordered = set(), []
    for t in tables:
        if t not in seen and t not in {"EHR", "Tables", "Candidate",
                                       "Available", "and"}:
            seen.add(t)
            ordered.append(t)
    for table in ordered:
        cols = await call("get_column_names", {"subject_id": subject_id,
                                               "table_name": table})
        if "Error" not in cols[:40]:
            blocks.append(f"### get_column_names({table})\n{cols}")

    return "<schema_orientation>\n" + "\n\n".join(blocks) + "\n</schema_orientation>"


async def run_one_single_shot(
    question: str,
    qid: Any,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool = None,
    evidence_budget_tokens: Optional[int] = None,
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 1.0,
    model_name: str = "",
) -> Tuple[List[dict], Dict[str, Any]]:
    """Single-shot tool-use baseline. Returns (messages, stats)."""
    browser_pool.init_session(qid)
    if ehr_pool:
        await ehr_pool.init_session(qid)
    if hasattr(generator, "_init_tokenizer"):
        await generator._init_tokenizer()

    stats: Dict[str, Any] = {
        "retrieval_mode": "single_shot",
        "n_retrieval_calls": 0,
        "tool_result_tokens": 0,
        "evidence_budget_tokens": evidence_budget_tokens,
        "budget_exceeded": False,
        "orientation_ok": False,
    }

    subject_id, timestamp = _extract_subject_and_time(question)

    if uses_legacy_clinseek_tool_format(model_name):
        system_prompt = SFT_MODEL_PROMPT
    elif not getattr(browser_pool, "enabled", True):
        system_prompt = DEVELOPER_CONTENT_CLAUDE_EHR_ONLY
    else:
        system_prompt = DEVELOPER_CONTENT_CLAUDE

    try:
        return await _single_shot_body(
            question, qid, generator, browser_pool, ehr_pool,
            subject_id, timestamp, system_prompt, stats,
            evidence_budget_tokens, max_tool_result_chars, max_tokens,
            temperature), stats
    finally:
        browser_pool.cleanup(qid)
        if ehr_pool:
            await ehr_pool.cleanup(qid)


async def _single_shot_body(
    question, qid, generator, browser_pool, ehr_pool,
    subject_id, timestamp, system_prompt, stats,
    evidence_budget_tokens, max_tool_result_chars, max_tokens, temperature,
) -> List[dict]:
    # ── Phase 0: orientation (free) ──────────────────────────────────────────
    orientation = ""
    if ehr_pool and subject_id and timestamp:
        try:
            orientation = await _run_orientation(
                ehr_pool, qid, subject_id, timestamp, max_tool_result_chars)
            stats["orientation_ok"] = True
        except Exception as exc:  # noqa: BLE001
            print(f"[qid={qid}] single-shot orientation failed: {exc}", flush=True)
    else:
        print(f"[qid={qid}] single-shot: no subject_id/timestamp parsed "
              f"(sid={subject_id!r}, ts={timestamp!r}); skipping orientation",
              flush=True)

    user_content = question
    if orientation:
        user_content = f"{question}\n\n{orientation}{_SINGLE_SHOT_DIRECTIVE}"

    messages: List[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    all_tools = json.loads(
        COMBINED_TOOL_CONTENT_FULL if getattr(browser_pool, "enabled", True)
        else EHR_ONLY_TOOL_CONTENT_FULL
    )
    # Phase 1 excludes finish (so the turn cannot end in an answer) and the
    # orientation tools (already executed for free in phase 0 — leaving them in
    # lets the model waste its one shot re-loading the DB, which is exactly the
    # strawman phase 0 exists to prevent). What remains is real retrieval:
    # get_records_by_*, run_sql_query, the candidate lookups, browser, think.
    def _base(name: str) -> str:
        return name.split(".", 1)[1] if "." in name else name
    retrieval_tools = [
        t for t in all_tools
        if t["function"]["name"] != "ehr.finish"
        and _base(t["function"]["name"]) not in _ORIENTATION_TOOLS
    ]
    finish_tool = [t for t in all_tools
                   if t["function"]["name"] == "ehr.finish"]

    # ── Phase 1: the single retrieval turn ───────────────────────────────────
    print(f"[qid={qid}] single-shot PHASE 1: retrieval", flush=True)
    r1 = await generator.chat_completion(
        messages=messages, tools=retrieval_tools, tool_choice="any",
        temperature=temperature, max_tokens=max_tokens)
    m1 = r1["choices"][0]["message"]
    tool_calls = m1.get("tool_calls") or []
    normalized = normalize_tool_calls(tool_calls)
    stats["n_retrieval_calls"] = len(tool_calls)
    print(f"[qid={qid}] single-shot: model issued {len(tool_calls)} tool call(s) "
          f"in one turn", flush=True)

    assistant_turn = {
        "role": "assistant",
        "content": m1.get("content", ""),
        "tool_calls": normalized if normalized else None,
    }
    if m1.get("reasoning_content"):
        assistant_turn["reasoning_content"] = m1["reasoning_content"]
    if m1.get("bedrock_content_blocks"):
        assistant_turn["bedrock_content_blocks"] = m1["bedrock_content_blocks"]
    if m1.get("raw_response") is not None:
        assistant_turn["raw_response"] = m1["raw_response"]
    messages.append(assistant_turn)

    # Execute every call in order, capping at the evidence budget. The API needs
    # a tool_result for every tool_use id, so over-budget calls get a placeholder
    # rather than being skipped.
    for tc, ntc in zip(tool_calls, normalized):
        tool_id = ntc["id"]
        fname = ntc["function"]["name"]
        try:
            args = ntc["function"]["arguments"]
            args = args if isinstance(args, dict) else json.loads(args)
        except Exception:
            args = {}

        over_budget = (evidence_budget_tokens is not None
                       and stats["tool_result_tokens"] >= evidence_budget_tokens)
        if over_budget:
            stats["budget_exceeded"] = True
            result = "[omitted: evidence budget exhausted]"
        else:
            try:
                if fname.startswith("ehr.") and ehr_pool:
                    result = await ehr_pool.call_tool(
                        qid, fname.split(".", 1)[1], args)
                elif fname.startswith("browser."):
                    result = await browser_pool.call_tool(
                        qid, fname.split(".", 1)[1], args) or f"{fname} completed"
                else:
                    result = f"Unknown tool namespace: {fname}"
            except Exception as exc:  # noqa: BLE001
                result = f"Error executing {fname}: {exc}"
            result = truncate_tool_result(result, max_tool_result_chars)
            stats["tool_result_tokens"] += _estimate_tokens(result)

        messages.append({"role": "tool", "tool_call_id": tool_id,
                         "content": result})
        print(f"[qid={qid}] single-shot TOOL[{fname}] "
              f"result_len={len(result)}"
              + ("  [OVER BUDGET]" if over_budget else ""), flush=True)

    # ── Phase 2: answer, finish only ─────────────────────────────────────────
    print(f"[qid={qid}] single-shot PHASE 2: answer "
          f"(tool_result_tokens={stats['tool_result_tokens']})", flush=True)
    r2 = await generator.chat_completion(
        messages=messages, tools=finish_tool, tool_choice="ehr.finish",
        temperature=temperature, max_tokens=max_tokens)
    m2 = r2["choices"][0]["message"]
    tc2 = m2.get("tool_calls") or []
    normalized2 = normalize_tool_calls(tc2)

    answer_turn = {
        "role": "assistant",
        "content": m2.get("content", ""),
        "tool_calls": normalized2 if normalized2 else None,
    }
    if m2.get("reasoning_content"):
        answer_turn["reasoning_content"] = m2["reasoning_content"]
    if m2.get("bedrock_content_blocks"):
        answer_turn["bedrock_content_blocks"] = m2["bedrock_content_blocks"]
    if m2.get("raw_response") is not None:
        answer_turn["raw_response"] = m2["raw_response"]
    messages.append(answer_turn)

    if normalized2:
        for ntc in normalized2:
            messages.append({
                "role": "tool", "tool_call_id": ntc["id"],
                "content": "Finish (single-shot)",
            })
    return messages


async def run_one_query(
    question: str,
    qid: Any,
    session_id: Any,
    run_index: int,
    runs_per_question: int,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool = None,
    max_rounds: int = 200,
    temperature: float = 1.0,
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model_name: str = "",
    retrieval_mode: str = "agentic",
    evidence_budget_tokens: Optional[int] = None,
    max_consecutive_identical_tool_rounds: int = 0,
):
    """Run a single query and return the result."""
    try:
        single_shot_stats = None
        loop_stop_reason = None
        if retrieval_mode == "single_shot":
            messages, single_shot_stats = await run_one_single_shot(
                question=question,
                qid=session_id,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                evidence_budget_tokens=evidence_budget_tokens,
                max_tool_result_chars=max_tool_result_chars,
                max_tokens=max_tokens,
                temperature=temperature,
                model_name=model_name,
            )
        else:
            messages, loop_stop_reason = await run_one_native(
                question=question,
                qid=session_id,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tool_result_chars=max_tool_result_chars,
                max_tokens=max_tokens,
                model_name=model_name,
                max_consecutive_identical_tool_rounds=(
                    max_consecutive_identical_tool_rounds
                ),
            )

        completed, stop_reason = summarize_conversation_completion(messages)
        if not completed and loop_stop_reason:
            stop_reason = loop_stop_reason

        result = {
            "qid": qid,
            "run_index": run_index,
            "runs_per_question": runs_per_question,
            "session_id": session_id,
            "question": question,
            "messages": messages,
            "completed": completed,
            "status": "success" if completed else "incomplete",
            "stop_reason": stop_reason,
        }
        if single_shot_stats is not None:
            result["single_shot"] = single_shot_stats
        return result

    except Exception as e:
        print(f"[qid={qid}] ERROR: {e}")
        traceback.print_exc()
        return {
            "qid": qid,
            "run_index": run_index,
            "runs_per_question": runs_per_question,
            "session_id": session_id,
            "question": question,
            "messages": [],
            "completed": False,
            "status": "error",
            "stop_reason": "exception",
            "error": str(e)
        }


async def process_query_item(
    item: Dict[str, Any],
    task_index: int,
    total_task_runs: int,
    question_index: int,
    total_questions: int,
    run_index: int,
    runs_per_question: int,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool,
    max_rounds: int,
    temperature: float,
    max_tool_result_chars: int,
    max_tokens: int,
    semaphore: asyncio.Semaphore,
    out_f: Any,
    output_file: str,
    write_lock: asyncio.Lock,
    model_name: str = "",
    data_path: str = "",
    retrieval_mode: str = "agentic",
    evidence_budget_tokens: Optional[int] = None,
    max_consecutive_identical_tool_rounds: int = 0,
) -> Dict[str, Any]:
    qid = resolve_qid(item)
    session_id = f"{qid}__q_{question_index}__run_{run_index}"

    try:
        question = resolve_question(item, data_path=data_path)

        async with semaphore:
            if ehr_pool:
                # Bind immutable benchmark metadata before the model can issue
                # load_ehr or any patient-scoped query.
                ehr_pool.configure_session(session_id, item)
            print(f"\n{'='*80}")
            print(
                f"Processing task {task_index}/{total_task_runs} | "
                f"question {question_index}/{total_questions} | "
                f"run {run_index}/{runs_per_question} | "
                f"qid={qid}: {question[:100]}..."
            )
            print(f"{'='*80}")

            result = await run_one_query(
                question=question,
                qid=qid,
                session_id=session_id,
                run_index=run_index,
                runs_per_question=runs_per_question,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tool_result_chars=max_tool_result_chars,
                max_tokens=max_tokens,
                model_name=model_name,
                retrieval_mode=retrieval_mode,
                evidence_budget_tokens=evidence_budget_tokens,
                max_consecutive_identical_tool_rounds=(
                    max_consecutive_identical_tool_rounds
                ),
            )
            result = attach_source_fields(result, item)
    except Exception as e:
        print(f"[qid={qid}] ERROR before completion: {e}")
        traceback.print_exc()
        result = {
            "qid": qid,
            "run_index": run_index,
            "runs_per_question": runs_per_question,
            "session_id": session_id,
            "question": item.get('question', item.get('query', '')),
            "messages": [],
            "completed": False,
            "status": "error",
            "stop_reason": "exception_before_completion",
            "error": str(e)
        }
        result = attach_source_fields(result, item)

    async with write_lock:
        out_f.write(json.dumps(result, ensure_ascii=False) + '\n')
        out_f.flush()
        print(
            f"[qid={result['qid']}] "
            f"task {task_index}/{total_task_runs} | "
            f"run {result.get('run_index', '?')}/{result.get('runs_per_question', '?')} "
            f"written to {output_file}"
        )

    return result


async def main():
    """Main entry point."""
    # Load environment variables
    dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    
    parser = argparse.ArgumentParser(description="ClinSeekAgent text evaluation driver")

    # Model configuration
    parser.add_argument("--backend", type=str, choices=["bedrock", "vertex", "vllm"], default="bedrock",
                        help="LLM backend: AWS Bedrock, Google Vertex AI (Claude), or "
                             "an OpenAI-compatible endpoint such as a local vLLM server")
    parser.add_argument("--model_name_or_path", type=str, default=DEFAULT_BEDROCK_MODEL_ID,
                        help="Model name, served model ID, or Bedrock model ID")
    parser.add_argument("--use_bedrock", action="store_true", default=False,
                        help="Deprecated alias for --backend bedrock")
    parser.add_argument("--bedrock_model_id", type=str, default=None,
                        help="Bedrock model ID (overrides model_name_or_path)")
    parser.add_argument("--bedrock_region", type=str, default=DEFAULT_BEDROCK_REGION,
                        help="AWS Bedrock region")
    parser.add_argument("--bedrock_api_key", type=str, default=None,
                        help="Bedrock bearer token / API key")
    parser.add_argument("--vertex_model_id", type=str, default=None,
                        help="Vertex AI model ID (overrides model_name_or_path)")
    parser.add_argument("--vertex_project", type=str, default=None,
                        help="GCP project for Vertex AI (defaults to "
                             "ANTHROPIC_VERTEX_PROJECT_ID / GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--vertex_region", type=str, default=None,
                        help="Vertex AI region, e.g. global / us-east5 "
                             "(defaults to CLOUD_ML_REGION or 'global')")
    parser.add_argument("--api_base_url", type=str, default=DEFAULT_VLLM_BASE_URL,
                        help="OpenAI-compatible API base URL (--backend vllm)")
    parser.add_argument("--api_key", type=str, default=DEFAULT_VLLM_API_KEY,
                        help="API key for the OpenAI-compatible API")
    parser.add_argument("--enable_thinking", dest="enable_thinking", action="store_true",
                        help="Enable model reasoning/thinking mode when supported")
    parser.add_argument("--disable_thinking", dest="enable_thinking", action="store_false",
                        help="Disable model reasoning/thinking mode when supported")
    parser.add_argument("--thinking_budget_tokens", type=int, default=4096,
                        help="Claude extended-thinking budget for models that "
                             "accept a fixed budget (Claude 4.6 and older)")
    parser.add_argument("--reasoning_effort", type=str, default="high",
                        help="Reasoning effort when thinking is enabled: "
                             "`output_config.effort` for adaptive-only Claude "
                             "models (Opus 4.7+, Opus 5, Sonnet 5) and "
                             "`reasoning_effort` for OpenAI reasoning models "
                             "behind --backend vllm")
    parser.add_argument("--reasoning_summary", type=str, default="none",
                        choices=["none", "auto", "concise", "detailed"],
                        help="Ask OpenAI-family models for a readable summary "
                             "of their reasoning (their raw chain of thought is "
                             "only ever returned encrypted). Default 'none' "
                             "keeps requests identical to earlier runs")
    parser.add_argument("--save_raw_responses", action="store_true",
                        help="Persist each round's untouched provider payload "
                             "(thinking blocks, reasoning items, signatures) "
                             "under the assistant turn's 'raw_response'. "
                             "Analysis only: it multiplies trajectory size")
    parser.add_argument("--api_max_attempts", type=int, default=3,
                        help="--backend vllm: attempts per request for transient "
                             "408/409/429/5xx/connection errors")
    parser.add_argument("--api_retry_base_seconds", type=float, default=5.0,
                        help="--backend vllm: initial retry delay")
    parser.add_argument("--api_retry_max_seconds", type=float, default=60.0,
                        help="--backend vllm: maximum retry delay")
    parser.add_argument("--api_min_request_interval_seconds", type=float,
                        default=0.0,
                        help="--backend vllm: minimum spacing between request "
                             "starts (rate-limited endpoints)")

    # Optional browser extra (not part of the paper's tool space)
    parser.add_argument("--enable_browser", action="store_true",
                        help="Add the 3 browser.* web-search tools and the "
                             "browser-aware system prompt. Off by default: the "
                             "paper's tool space is EHR-only for text tasks")
    parser.add_argument("--disable_browser", action="store_true",
                        help="Deprecated no-op kept for old launch commands; "
                             "browser tools are off unless --enable_browser")
    parser.add_argument("--search_url", type=str, default="http://localhost:8001",
                        help="Search backend URL (--enable_browser only)")
    parser.add_argument("--browser_backend", type=str, default="local", choices=["local", "serper"],
                        help="Browser backend type (--enable_browser only)")

    # EHR configuration
    parser.add_argument("--enable_ehr", action="store_true",
                        help="Enable EHR tools integration")
    parser.add_argument("--ehr_mcp_url", type=str, default="http://127.0.0.1:5003/mcp",
                        help="EHR MCP server URL")

    # Data configuration
    parser.add_argument("--data_path", type=str, required=True,
                        help="Path to questions file in JSON or JSONL format")
    parser.add_argument("--output_dir", type=str, default="./results",
                        help="Output directory for results")

    # Execution configuration
    parser.add_argument("--max_rounds", type=int, default=200,
                        help="Maximum conversation rounds")
    parser.add_argument(
        "--max_consecutive_identical_tool_rounds",
        type=int,
        default=0,
        help=(
            "Stop an agentic query after the exact same ordered tool-call "
            "batch (tool names plus canonicalized arguments) appears for N "
            "consecutive rounds. 0 disables this circuit breaker."
        ),
    )
    parser.add_argument(
        "--resume_keep_context_limit_errors",
        action="store_true",
        help=(
            "Treat existing model context-window errors as completed results "
            "during resume; other error rows remain retryable."
        ),
    )
    parser.add_argument("--retrieval_mode", choices=["agentic", "single_shot"],
                        default="agentic",
                        help="agentic: the iterative tool loop (default). "
                             "single_shot: Single-Pass Retrieval -- free schema "
                             "orientation, one retrieval turn, then finish-only. "
                             "Isolates iterative planning from tool access.")
    parser.add_argument("--evidence_budget_tokens", type=int, default=None,
                        help="single_shot only: cap on total tool-result tokens "
                             "the one retrieval turn may consume (approx len/4). "
                             "Unset = unbounded (paper setting).")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="Sampling temperature (paper: 0 where accepted; "
                             "dropped for Claude with extended thinking and for "
                             "models that reject sampling parameters)")
    parser.add_argument("--runs_per_question", type=int, default=1,
                        help="Number of independent runs to execute for each question")
    parser.add_argument("--max_concurrency", type=int, default=MAX_PARALLEL_QUERIES,
                        help=f"Maximum parallel queries (capped at {MAX_PARALLEL_QUERIES})")
    parser.add_argument("--max_tool_result_chars", type=int, default=DEFAULT_MAX_TOOL_RESULT_CHARS,
                        help="Maximum number of characters kept from each tool result")
    parser.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="Maximum number of tokens generated per model call")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose output")

    parser.set_defaults(enable_thinking=None)
    args = parser.parse_args()
    browser_backend_explicit = "--browser_backend" in os.sys.argv

    global VERBOSE
    VERBOSE = args.verbose

    if args.enable_browser and args.disable_browser:
        raise ValueError("--enable_browser conflicts with --disable_browser")
    if not args.enable_browser:
        # No browser backend is ever constructed, so a Serper key is neither
        # needed nor meaningful.
        args.browser_backend = "disabled"
        print("Tool space: EHR tools only (paper setting)")
    else:
        if (
            not browser_backend_explicit
            and args.browser_backend == "local"
            and os.getenv("SERPER_API_KEY")
        ):
            args.browser_backend = "serper"
            print("SERPER_API_KEY detected; using Serper browser backend")

        if args.browser_backend == "serper" and not os.getenv("SERPER_API_KEY"):
            raise ValueError("SERPER_API_KEY is required when using --browser_backend serper")

    if args.max_concurrency < 1:
        raise ValueError("--max_concurrency must be at least 1")

    if args.runs_per_question < 1:
        raise ValueError("--runs_per_question must be at least 1")

    if args.max_tool_result_chars < 1:
        raise ValueError("--max_tool_result_chars must be at least 1")

    if args.max_tokens < 1:
        raise ValueError("--max_tokens must be at least 1")

    if args.thinking_budget_tokens < 1024:
        raise ValueError("--thinking_budget_tokens must be at least 1024")

    if args.api_max_attempts < 1:
        raise ValueError("--api_max_attempts must be at least 1")

    if args.api_retry_base_seconds <= 0 or args.api_retry_max_seconds <= 0:
        raise ValueError("API retry delays must be positive")

    if args.api_min_request_interval_seconds < 0:
        raise ValueError("--api_min_request_interval_seconds cannot be negative")

    concurrency = min(args.max_concurrency, MAX_PARALLEL_QUERIES)
    if concurrency != args.max_concurrency:
        print(
            f"--max_concurrency={args.max_concurrency} exceeds limit; "
            f"capping to {MAX_PARALLEL_QUERIES}"
        )

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    selected_backend = "bedrock" if args.use_bedrock else args.backend
    if args.use_bedrock and args.backend != "bedrock":
        raise ValueError(f"--use_bedrock conflicts with --backend {args.backend}")

    # Initialize generator
    if selected_backend == "bedrock":
        # Import Bedrock generator (local copy to avoid vllm dependency)
        from bedrock_backend import BedrockAsyncGenerator

        bedrock_api_key = configure_bedrock_auth(args.bedrock_api_key)
        resolved_model_id = resolve_bedrock_model_id(
            args.bedrock_model_id or args.model_name_or_path
        )

        generator = BedrockAsyncGenerator(
            model_id=resolved_model_id,
            region_name=args.bedrock_region,
            max_tokens_default=args.max_tokens,
            enable_thinking=args.enable_thinking,
            thinking_budget_tokens=args.thinking_budget_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        if bedrock_api_key:
            print(f"Using Bedrock bearer token auth: {mask_secret(bedrock_api_key)}")
        else:
            print("Using default AWS credential chain for Bedrock auth")
        print(
            "Using AWS Bedrock: "
            f"{generator.model_id} @ {args.bedrock_region} | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    elif selected_backend == "vertex":
        from vertex_backend import VertexAsyncGenerator

        # An untouched --model_name_or_path still holds the Bedrock default, so
        # treat it as "unset" here and let the backend auto-pick.
        vertex_model = args.vertex_model_id or args.model_name_or_path
        if vertex_model == DEFAULT_BEDROCK_MODEL_ID:
            vertex_model = None

        generator = VertexAsyncGenerator(
            model_id=vertex_model,
            project_id=args.vertex_project,
            region=args.vertex_region,
            max_tokens_default=args.max_tokens,
            enable_thinking=args.enable_thinking,
            thinking_budget_tokens=args.thinking_budget_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        print(
            "Using Google Vertex AI: "
            f"{generator.model_id} @ {generator.region} "
            f"(project={generator.project_id}) | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    elif selected_backend == "vllm":
        from vllm_backend import VLLMOpenAIAsyncGenerator

        generator = VLLMOpenAIAsyncGenerator(
            model_name=args.model_name_or_path,
            base_url=args.api_base_url,
            api_key=args.api_key,
            max_tokens_default=args.max_tokens,
            max_workers=concurrency,
            enable_thinking=args.enable_thinking,
            reasoning_effort=args.reasoning_effort,
            request_max_attempts=args.api_max_attempts,
            retry_base_delay=args.api_retry_base_seconds,
            retry_max_delay=args.api_retry_max_seconds,
            min_request_interval=args.api_min_request_interval_seconds,
        )
        print(
            "Using vLLM OpenAI-compatible API: "
            f"{generator.base_url} | model={generator.model_name or 'auto'} | "
            f"api_key={mask_optional_secret(args.api_key)} | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    else:
        raise NotImplementedError(f"Unsupported backend: {selected_backend}")

    if args.save_raw_responses:
        if not hasattr(generator, "capture_raw_response"):
            raise SystemExit(
                f"--save_raw_responses is not supported by the "
                f"{selected_backend} backend")
        generator.capture_raw_response = True
        print("Raw provider responses will be stored on each assistant turn "
              "(--save_raw_responses)")

    # Initialize browser pool
    browser_pool = BrowserPool(
        args.search_url,
        browser_backend=args.browser_backend,
        enabled=args.enable_browser,
    )

    # Initialize EHR pool if enabled
    ehr_pool = None
    if args.enable_ehr:
        ehr_pool = EHRToolPool(mcp_url=args.ehr_mcp_url)
        print(f"EHR tools enabled: {args.ehr_mcp_url}")

    # Load data
    data = load_query_data(args.data_path)
    total_task_runs = len(data) * args.runs_per_question

    print(f"Loaded {len(data)} queries from {args.data_path}")
    print(
        f"Running {args.runs_per_question} run(s) per query "
        f"({total_task_runs} total runs)"
    )
    print(
        "Execution order: run 1 over all queries, then run 2, "
        "until runs_per_question is exhausted"
    )
    print(f"Running with max concurrency: {concurrency}")
    print(f"Tool result char limit: {args.max_tool_result_chars}")

    effective_request = {
        "tool_choice": "auto",
        "stream": False,
        "api_worker_threads": getattr(
            getattr(generator, "executor", None),
            "_max_workers",
            None,
        ),
    }
    model_for_request = getattr(
        generator,
        "model_name",
        getattr(generator, "model_id", args.model_name_or_path),
    )
    if hasattr(generator, "_build_extra_body"):
        effective_request["extra_body"] = generator._build_extra_body()
    if hasattr(generator, "_reasoning_kwargs"):
        effective_request["reasoning_kwargs"] = generator._reasoning_kwargs(
            model_for_request
        )
    if hasattr(generator, "_sampling_kwargs"):
        effective_request["sampling_kwargs"] = generator._sampling_kwargs(
            model_for_request,
            args.temperature,
        )
    if hasattr(generator, "_max_token_kwargs"):
        effective_request["max_token_kwargs"] = generator._max_token_kwargs(
            model_for_request,
            args.max_tokens,
        )

    run_config_file = os.path.join(args.output_dir, "run_config.json")
    with open(run_config_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "backend": selected_backend,
                "model": args.model_name_or_path,
                "data_path": os.path.abspath(args.data_path),
                "output_dir": os.path.abspath(args.output_dir),
                "enable_thinking": args.enable_thinking,
                "thinking_budget_tokens": args.thinking_budget_tokens,
                "reasoning_effort": args.reasoning_effort,
                "reasoning_summary": args.reasoning_summary,
                "save_raw_responses": bool(args.save_raw_responses),
                "api_max_attempts": args.api_max_attempts,
                "api_retry_base_seconds": args.api_retry_base_seconds,
                "api_retry_max_seconds": args.api_retry_max_seconds,
                "api_min_request_interval_seconds":
                    args.api_min_request_interval_seconds,
                "temperature_cli": args.temperature,
                "max_tokens": args.max_tokens,
                "max_rounds": args.max_rounds,
                "max_consecutive_identical_tool_rounds": (
                    args.max_consecutive_identical_tool_rounds
                ),
                "resume_keep_context_limit_errors": bool(
                    args.resume_keep_context_limit_errors
                ),
                "max_concurrency": concurrency,
                "runs_per_question": args.runs_per_question,
                "max_tool_result_chars": args.max_tool_result_chars,
                "retrieval_mode": args.retrieval_mode,
                "browser_backend": args.browser_backend,
                "browser_enabled": bool(args.enable_browser),
                "ehr_enabled": bool(args.enable_ehr),
                "ehr_mcp_url": args.ehr_mcp_url if args.enable_ehr else None,
                "effective_model_request": effective_request,
            },
            f,
            indent=2,
        )
    print(f"Run config written to {run_config_file}")

    if hasattr(generator, '_init_tokenizer'):
        await generator._init_tokenizer()

    # Process queries — with resume support
    output_file = os.path.join(args.output_dir, "results.jsonl")

    # Load existing results for resume.
    #
    # A row is skipped if the model was actually reached, whether or not it
    # produced a final answer: `incomplete` (no finish call) is a datapoint and
    # must not be retried into a nicer number. A row is RE-RUN when the request
    # never landed -- `status == "error"`, i.e. 429s and dead MCP sessions --
    # because that is an outage, not a result.
    completed_qids = set()
    existing_lines = []
    retryable = 0
    if os.path.exists(output_file):
        with open(output_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    existing_lines.append(line)
                    continue
                error_text = str(result.get("error") or "").lower()
                keep_context_limit_error = (
                    args.resume_keep_context_limit_errors
                    and result.get("status") == "error"
                    and "maximum context length" in error_text
                )
                if result.get("status") == "error" and not keep_context_limit_error:
                    retryable += 1
                    continue          # drop the row; it will be re-run
                completed_qids.add(result["qid"])
                existing_lines.append(line)
    if retryable:
        print(f"Resuming: {retryable} row(s) failed with an error and will be "
              f"re-run; {len(completed_qids)} kept")

    if completed_qids:
        print(f"Resuming: {len(completed_qids)} already in results, skipping them")

    # Rewrite existing results + append new ones
    with open(output_file, 'w', encoding='utf-8') as out_f:
        # Write back all existing results (completed + incomplete)
        for line in existing_lines:
            out_f.write(line + "\n")
        out_f.flush()

        semaphore = asyncio.Semaphore(concurrency)
        write_lock = asyncio.Lock()
        task_index = 1
        skipped = 0

        for run_index in range(1, args.runs_per_question + 1):
            print(
                f"\n{'#' * 80}\n"
                f"Starting run batch {run_index}/{args.runs_per_question} "
                f"for {len(data)} query(ies)\n"
                f"{'#' * 80}"
            )
            batch_tasks = []

            for question_index, item in enumerate(data, start=1):
                qid = resolve_qid(item)
                if qid in completed_qids:
                    skipped += 1
                    task_index += 1
                    continue
                batch_tasks.append(
                    asyncio.create_task(
                        process_query_item(
                            item=item,
                            task_index=task_index,
                            total_task_runs=total_task_runs,
                            question_index=question_index,
                            total_questions=len(data),
                            run_index=run_index,
                            runs_per_question=args.runs_per_question,
                            generator=generator,
                            browser_pool=browser_pool,
                            ehr_pool=ehr_pool,
                            max_rounds=args.max_rounds,
                            temperature=args.temperature,
                            max_tool_result_chars=args.max_tool_result_chars,
                            max_tokens=args.max_tokens,
                            semaphore=semaphore,
                            out_f=out_f,
                            output_file=output_file,
                            write_lock=write_lock,
                            model_name=args.model_name_or_path,
                            data_path=args.data_path,
                            retrieval_mode=args.retrieval_mode,
                            evidence_budget_tokens=args.evidence_budget_tokens,
                            max_consecutive_identical_tool_rounds=(
                                args.max_consecutive_identical_tool_rounds
                            ),
                        )
                    )
                )
                task_index += 1

            if batch_tasks:
                print(
                    f"Queued {len(batch_tasks)} task(s) for run batch {run_index}"
                    + (f"; skipped {skipped} already-completed so far" if skipped else "")
                )
                await asyncio.gather(*batch_tasks)

    print(f"\nAll queries processed. Results in {output_file}")

    # One billable request per round per query, so only the run-level total is
    # meaningful. Written next to the results so a run's cost stays traceable.
    if hasattr(generator, "token_usage"):
        print(generator.usage_summary())
        usage_file = os.path.join(args.output_dir, "usage.json")
        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(
                {"model": getattr(generator, "model_id", args.model_name_or_path),
                 "backend": selected_backend,
                 **generator.token_usage},
                f,
                indent=2,
            )
        print(f"Token usage written to {usage_file}")

    # Cleanup
    if ehr_pool:
        await ehr_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
