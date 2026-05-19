"""
Deploy agent with both Browser and EHR tools integration.
Supports AWS Bedrock Claude for reasoning.
"""
import os
import json
import asyncio
import datetime
import argparse
import re
import uuid
from typing import List, Dict, Any
import traceback

from browser_tool import BrowserTool, LocalServiceBrowserBackend, SerperServiceBrowserBackend
from ehr_tool_pool import EHRToolPool
from prompts_text import DEVELOPER_CONTENT_CLAUDE, SFT_MODEL_PROMPT, COMBINED_TOOL_CONTENT_FULL
import dotenv

# Verbose flag
VERBOSE = False
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"
DEFAULT_BEDROCK_REGION = "us-east-1"
DEFAULT_VLLM_BASE_URL = "http://127.0.0.1:4000"
DEFAULT_VLLM_API_KEY = "EMPTY"
MAX_PARALLEL_QUERIES = 12
DEFAULT_MAX_TOOL_RESULT_CHARS = 100000
DEFAULT_MAX_TOKENS = 32768

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


def _extract_embedded_response_json(text: str) -> List[str] | None:
    """Look for a JSON object like {"response": [...]} embedded in free text.

    OSS models (gpt-oss-120b, GLM, Qwen) often emit the ehr.finish payload
    inline in plain content when the harness tool-call parser misfires —
    e.g. `<final_output to=functions.ehr.finish <|message|>{"response":["yes"]}`
    or just `{"response":["Sputum Color"]}` after a reasoning block. Scan
    through every balanced `{ ... }` substring, json.loads it, and return
    the first `response` list we find. Return None if nothing usable.
    """
    if not text or "response" not in text:
        return None

    def _unpack(obj: Any) -> List[str] | None:
        if not isinstance(obj, dict) or "response" not in obj:
            return None
        resp = obj["response"]
        if isinstance(resp, list):
            items = [str(x).strip() for x in resp if str(x).strip()]
            return items or None
        if isinstance(resp, str) and resp.strip():
            return [resp.strip()]
        return None

    # Walk the string, matching balanced braces. We scan left-to-right,
    # stopping at the first parseable object that has a "response" field.
    depth = 0
    start = -1
    last_start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
                last_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    blob = text[start : i + 1]
                    try:
                        obj = json.loads(blob)
                    except Exception:
                        start = -1
                        continue
                    result = _unpack(obj)
                    if result is not None:
                        return result
                    start = -1

    # Fallback for unbalanced tail like `<final_answer>{ "response": ["no"]`
    # (no closing brace). Try to complete the substring from the last open
    # brace to end-of-string by appending closers and re-parsing.
    if last_start >= 0 and depth > 0:
        tail = text[last_start:].rstrip()
        for suffix in ("}", "]}", "]]}", "\"]}"):
            try:
                obj = json.loads(tail + suffix)
            except Exception:
                continue
            result = _unpack(obj)
            if result is not None:
                return result
    return None


def _salvage_plain_text_answer(content: str) -> List[str]:
    """Turn a plain-text final answer into a list the ehr.finish tool expects.

    Strategy (first match wins):
      1. Parse any embedded `{"response": [...]}` JSON object.
      2. Fall back to splitting on newlines and stripping bullets.
      3. Final fallback: return the whole stripped string as a single item.
    """
    text = (content or "").strip()
    if not text:
        return []

    # 1. embedded JSON envelope (most common OSS failure)
    resp = _extract_embedded_response_json(text)
    if resp:
        return resp

    # 2. bullet / newline split — mimic MM driver salvage
    items: List[str] = []
    for raw in text.splitlines():
        cleaned = raw.strip().lstrip("-*•").strip()
        if cleaned:
            items.append(cleaned)
    if items:
        return items

    return [text]


def _synthesize_finish_tool_call(
    messages: List[Dict[str, Any]],
    content: str,
    qid: str,
    round_num: int,
) -> bool:
    """Rewrite the last assistant turn to include a synthetic ehr.finish call.

    Returns True if a synthesis happened. The caller should still break out
    of the action loop after this — we're marking the end of the run, not
    continuing it.
    """
    items = _salvage_plain_text_answer(content)
    if not items:
        return False

    synth_call = {
        "id": f"synthetic_finish_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": "ehr.finish",
            "arguments": json.dumps({"response": items}),
        },
    }
    messages[-1]["tool_calls"] = [synth_call]
    messages.append(
        {
            "role": "tool",
            "content": "Finish (synthesized from plain-text answer)",
            "tool_call_id": synth_call["id"],
        }
    )
    preview = json.dumps(items)[:160]
    print(
        f"[qid={qid}] DONE Round {round_num}: synthesized ehr.finish "
        f"from plain-text answer ({len(items)} item(s)): {preview}",
        flush=True,
    )
    return True


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


def resolve_question(item: Dict[str, Any], data_path: str = "") -> str:
    if "ehr_bench" in os.path.basename(data_path):
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
    def __init__(self, search_url, browser_backend='local'):
        self.search_url = search_url
        self.browser_backend = browser_backend
        self.sessions: Dict[Any, BrowserTool] = {}

    def init_session(self, qid: Any) -> dict:
        if self.browser_backend == 'serper':
            backend = SerperServiceBrowserBackend()
        else:
            backend = LocalServiceBrowserBackend(base_url=self.search_url)
        tool = BrowserTool(backend=backend)
        self.sessions[qid] = tool
        return tool.tool_config

    async def call_tool(self, qid: Any, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Call browser tool and return text result."""
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


def _iter_assistant_completion_texts(message: Dict[str, Any]):
    content = (message.get("content") or "").strip()
    if content:
        yield "content", content

    reasoning_content = (message.get("reasoning_content") or "").strip()
    if reasoning_content and reasoning_content != content:
        yield "reasoning_content", reasoning_content


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

    for text_source, text in _iter_assistant_completion_texts(last_assistant_message):
        content_lower = text.lower()
        if "exact answer:" in content_lower and "confidence:" in content_lower:
            return True, f"exact_answer_text_{text_source}"

    return False, "no_final_answer"


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
) -> List[dict]:
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

    # Use the bracketed tool-call prompt for ClinSeek SFT models.
    model_name_lower = (model_name or "").lower()
    if "clinseek" in model_name_lower:
        system_prompt = SFT_MODEL_PROMPT
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

    # Parse tools (ALL 23 tools: 3 browser + 20 EHR)
    tools = json.loads(COMBINED_TOOL_CONTENT_FULL)
    candidate_table_tool_calls = 0
    browser_tool_calls = 0
    max_browser_tool_calls = 40

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
            messages.append(assistant_message)

            if not tool_calls:
                print(
                    f"[qid={qid}] Round {round_num}: No tool calls detected; "
                    "stopping without a reminder",
                    flush=True,
                )
                # Salvage: OSS models (gpt-oss, GLM, Qwen, Kimi) often emit
                # the final answer as plain text when their native tool-call
                # format confuses Bedrock's shim. Synthesize an ehr.finish so
                # downstream scoring can extract the prediction instead of
                # marking the row incomplete. Some models (notably gpt-oss)
                # put the answer in reasoning_content, so try both sources
                # and pick whichever carries an embedded JSON envelope.
                combined = "\n\n".join(
                    p for p in (content, reasoning_content) if p
                ).strip()
                _synthesize_finish_tool_call(messages, combined, qid, round_num)
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
        return messages

    finally:
        browser_pool.cleanup(qid)
        if ehr_pool:
            await ehr_pool.cleanup(qid)


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
):
    """Run a single query and return the result."""
    try:
        messages = await run_one_native(
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
        )

        completed, stop_reason = summarize_conversation_completion(messages)

        return {
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
) -> Dict[str, Any]:
    qid = resolve_qid(item)
    session_id = f"{qid}__q_{question_index}__run_{run_index}"

    try:
        question = resolve_question(item, data_path=data_path)

        async with semaphore:
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
    parser.add_argument("--backend", type=str, choices=["bedrock", "vllm"], default="bedrock",
                        help="LLM backend to use")
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
    parser.add_argument("--api_base_url", type=str, default=DEFAULT_VLLM_BASE_URL,
                        help="OpenAI-compatible API base URL for vLLM")
    parser.add_argument("--api_key", type=str, default=DEFAULT_VLLM_API_KEY,
                        help="API key for the OpenAI-compatible API")
    parser.add_argument("--enable_thinking", dest="enable_thinking", action="store_true",
                        help="Enable model reasoning/thinking mode when supported")
    parser.add_argument("--disable_thinking", dest="enable_thinking", action="store_false",
                        help="Disable model reasoning/thinking mode when supported")

    # Browser configuration
    parser.add_argument("--search_url", type=str, default="http://localhost:8001",
                        help="Search backend URL")
    parser.add_argument("--browser_backend", type=str, default="local", choices=["local", "serper"],
                        help="Browser backend type")

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
    parser.add_argument("--temperature", type=float, default=1.0,
                        help="Sampling temperature for model calls")
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
        raise ValueError("--use_bedrock conflicts with --backend vllm")

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
            max_tokens_default=8192,
            enable_thinking=args.enable_thinking,
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
    elif selected_backend == "vllm":
        from vllm_backend import VLLMOpenAIAsyncGenerator

        generator = VLLMOpenAIAsyncGenerator(
            model_name=args.model_name_or_path,
            base_url=args.api_base_url,
            api_key=args.api_key,
            max_tokens_default=8192,
            enable_thinking=args.enable_thinking,
        )
        print(
            "Using vLLM OpenAI-compatible API: "
            f"{generator.base_url} | model={generator.model_name or 'auto'} | "
            f"api_key={mask_optional_secret(args.api_key)} | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    else:
        raise NotImplementedError(f"Unsupported backend: {selected_backend}")

    # Initialize browser pool
    browser_pool = BrowserPool(args.search_url, browser_backend=args.browser_backend)

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

    if hasattr(generator, '_init_tokenizer'):
        await generator._init_tokenizer()

    # Process queries — with resume support
    output_file = os.path.join(args.output_dir, "results.jsonl")

    # Load existing results for resume — skip ALL previously attempted qids
    completed_qids = set()
    existing_lines = []
    if os.path.exists(output_file):
        with open(output_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                    completed_qids.add(result["qid"])
                    existing_lines.append(line)
                except json.JSONDecodeError:
                    existing_lines.append(line)

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

    # Cleanup
    if ehr_pool:
        await ehr_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
