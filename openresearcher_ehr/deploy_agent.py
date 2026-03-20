"""
Deploy agent with both Browser and EHR tools integration.
Supports AWS Bedrock Claude for reasoning.
"""
import os
import json
import asyncio
import datetime
import argparse
from typing import List, Dict, Any
import traceback

from browser import BrowserTool, LocalServiceBrowserBackend, SerperServiceBrowserBackend
from ehr_pool import EHRToolPool
from data_utils import DEVELOPER_CONTENT_CLAUDE, COMBINED_TOOL_CONTENT_FULL
import dotenv

# Verbose flag
VERBOSE = False
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MAX_PARALLEL_QUERIES = 10

def vprint(*args, **kwargs):
    """Verbose print: only prints when VERBOSE is True."""
    if VERBOSE:
        print(*args, **kwargs)


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


def resolve_question(item: Dict[str, Any]) -> str:
    if 'question' in item or 'query' in item:
        return item.get('question', item.get('query', ''))

    from data_utils import generate_question_from_task
    return generate_question_from_task(item)


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


async def run_one_native(
    question: str,
    qid: Any,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool = None,
    max_rounds: int = 200,
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

    # System prompt
    system_prompt = DEVELOPER_CONTENT_CLAUDE + f"\n\nToday's date: {datetime.datetime.now().strftime('%Y-%m-%d')}"
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
                temperature=1.0,
                max_tokens=8192
            )

            # Extract message from response
            message = response["choices"][0]["message"]
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            content_preview = content[:300] if len(content) > 300 else content
            print(f"[qid={qid}] Round {round_num} MODEL RESPONSE: content_len={len(content)}, tool_calls={len(tool_calls)}")
            print(f"[qid={qid}] Round {round_num} CONTENT PREVIEW: {content_preview!r}", flush=True)

            # Add assistant message
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls if tool_calls else None
            })

            # Execute tool calls if present
            if tool_calls:
                for tc_idx, tool_call in enumerate(tool_calls):
                    tool_id = tool_call["id"]
                    function_name = tool_call["function"]["name"]
                    function_args_raw = tool_call["function"]["arguments"]

                    try:
                        # Parse arguments
                        if isinstance(function_args_raw, dict):
                            function_args = function_args_raw
                        else:
                            function_args = json.loads(function_args_raw)

                        print(f"[qid={qid}] Round {round_num} TOOL_CALL[{tc_idx}]: {function_name}({json.dumps(function_args, ensure_ascii=False)[:200]})", flush=True)

                        # Route to appropriate tool pool
                        if function_name.startswith("ehr.") or function_name.startswith("ehr_"):
                            # EHR tool execution (handle both ehr. and ehr_ formats)
                            if ehr_pool:
                                # Normalize: remove both ehr. and ehr_ prefixes
                                actual_function_name = function_name.replace("ehr_", "").replace("ehr.", "")
                                result = await ehr_pool.call_tool(qid, actual_function_name, function_args)
                            else:
                                result = "Error: EHR tools not available. Start with --enable_ehr flag."

                        elif function_name.startswith("browser."):
                            # Browser tool execution
                            actual_function_name = function_name.split(".", 1)[1]
                            result = await browser_pool.call_tool(qid, actual_function_name, function_args)
                            if not result:
                                result = f"{function_name} completed"
                        else:
                            result = f"Unknown tool namespace: {function_name}"

                        # Add tool response
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": result
                        })

                        result_preview = result[:200] if len(result) > 200 else result
                        print(f"[qid={qid}] Round {round_num} TOOL_RESULT[{tc_idx}]: len={len(result)}, preview={result_preview!r}", flush=True)

                    except Exception as e:
                        error_msg = f"Error executing {function_name}: {str(e)}"
                        print(f"[qid={qid}] Round {round_num} TOOL_ERROR[{tc_idx}]: {error_msg}", flush=True)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": error_msg
                        })

                # Continue to next round
                continue

            # Check for answer termination
            content_lower = content.lower()
            if '<answer>' in content_lower and '</answer>' in content_lower:
                print(f"[qid={qid}] ✅ Round {round_num}: Found <answer> tag - DONE", flush=True)
                break

            if "exact answer:" in content_lower and "confidence:" in content_lower:
                print(f"[qid={qid}] ✅ Round {round_num}: Found 'Exact Answer:' + 'Confidence:' - DONE", flush=True)
                break

            if "final answer:" in content_lower or "answer:" in content_lower:
                print(f"[qid={qid}] ✅ Round {round_num}: Found 'Final Answer:' or 'Answer:' - DONE", flush=True)
                break

            if content.strip():
                print(f"[qid={qid}] ✅ Round {round_num}: Assistant returned final text without more tool calls", flush=True)
                break

            # No tool calls and no answer detected
            print(f"[qid={qid}] Round {round_num}: No tool calls, no answer detected — ending conversation", flush=True)
            break

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
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool = None,
    max_rounds: int = 200
):
    """Run a single query and return the result."""
    try:
        messages = await run_one_native(
            question=question,
            qid=session_id,
            generator=generator,
            browser_pool=browser_pool,
            ehr_pool=ehr_pool,
            max_rounds=max_rounds
        )

        return {
            "qid": qid,
            "question": question,
            "messages": messages,
            "status": "success"
        }

    except Exception as e:
        print(f"[qid={qid}] ERROR: {e}")
        traceback.print_exc()
        return {
            "qid": qid,
            "question": question,
            "messages": [],
            "status": "error",
            "error": str(e)
        }


async def process_query_item(
    item: Dict[str, Any],
    index: int,
    total: int,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool,
    max_rounds: int,
    semaphore: asyncio.Semaphore,
    out_f: Any,
    output_file: str,
    write_lock: asyncio.Lock,
    pending_results: Dict[int, Dict[str, Any]],
    write_state: Dict[str, int],
) -> Dict[str, Any]:
    qid = resolve_qid(item)
    session_id = f"{qid}__run_{index}"

    try:
        question = resolve_question(item)

        async with semaphore:
            print(f"\n{'='*80}")
            print(f"Processing {index}/{total} qid={qid}: {question[:100]}...")
            print(f"{'='*80}")

            result = await run_one_query(
                question=question,
                qid=qid,
                session_id=session_id,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                max_rounds=max_rounds
            )
    except Exception as e:
        print(f"[qid={qid}] ERROR before completion: {e}")
        traceback.print_exc()
        result = {
            "qid": qid,
            "question": item.get('question', item.get('query', '')),
            "messages": [],
            "status": "error",
            "error": str(e)
        }

    async with write_lock:
        pending_results[index] = result
        while write_state["next_index"] in pending_results:
            next_index = write_state["next_index"]
            next_result = pending_results.pop(next_index)
            out_f.write(json.dumps(next_result, ensure_ascii=False) + '\n')
            out_f.flush()
            print(f"[qid={next_result['qid']}] Result written to {output_file}")
            write_state["next_index"] += 1

    return result


async def main():
    """Main entry point."""
    # Load environment variables
    dotenv.load_dotenv("../.env")
    
    parser = argparse.ArgumentParser(description="OpenResearcher with EHR integration")

    # Model configuration
    parser.add_argument("--model_name_or_path", type=str, default=DEFAULT_BEDROCK_MODEL_ID,
                        help="Model name or Bedrock model ID")
    parser.add_argument("--use_bedrock", action="store_true", default=True,
                        help="Use AWS Bedrock (default: True)")
    parser.add_argument("--bedrock_model_id", type=str, default=None,
                        help="Bedrock model ID (overrides model_name_or_path)")
    parser.add_argument("--bedrock_region", type=str, default="us-west-2",
                        help="AWS Bedrock region")

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
    parser.add_argument("--max_concurrency", type=int, default=MAX_PARALLEL_QUERIES,
                        help=f"Maximum parallel queries (capped at {MAX_PARALLEL_QUERIES})")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose output")

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

    concurrency = min(args.max_concurrency, MAX_PARALLEL_QUERIES)
    if concurrency != args.max_concurrency:
        print(
            f"--max_concurrency={args.max_concurrency} exceeds limit; "
            f"capping to {MAX_PARALLEL_QUERIES}"
        )

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Initialize generator
    if args.use_bedrock:
        # Import Bedrock generator (local copy to avoid vllm dependency)
        from bedrock_generator import BedrockAsyncGenerator

        generator = BedrockAsyncGenerator(
            model_id=args.bedrock_model_id or args.model_name_or_path,
            region_name=args.bedrock_region,
            max_tokens_default=8192
        )
        print(f"Using AWS Bedrock: {generator.model_id}")
    else:
        raise NotImplementedError("Only Bedrock is supported in this version")

    # Initialize browser pool
    browser_pool = BrowserPool(args.search_url, browser_backend=args.browser_backend)

    # Initialize EHR pool if enabled
    ehr_pool = None
    if args.enable_ehr:
        ehr_pool = EHRToolPool(mcp_url=args.ehr_mcp_url)
        print(f"EHR tools enabled: {args.ehr_mcp_url}")

    # Load data
    data = load_query_data(args.data_path)

    print(f"Loaded {len(data)} queries from {args.data_path}")
    print(f"Running with max concurrency: {concurrency}")

    if hasattr(generator, '_init_tokenizer'):
        await generator._init_tokenizer()

    # Process queries
    output_file = os.path.join(args.output_dir, "results.jsonl")
    with open(output_file, 'w', encoding='utf-8') as out_f:
        semaphore = asyncio.Semaphore(concurrency)
        write_lock = asyncio.Lock()
        pending_results: Dict[int, Dict[str, Any]] = {}
        write_state = {"next_index": 1}

        tasks = [
            asyncio.create_task(
                process_query_item(
                    item=item,
                    index=index,
                    total=len(data),
                    generator=generator,
                    browser_pool=browser_pool,
                    ehr_pool=ehr_pool,
                    max_rounds=args.max_rounds,
                    semaphore=semaphore,
                    out_f=out_f,
                    output_file=output_file,
                    write_lock=write_lock,
                    pending_results=pending_results,
                    write_state=write_state,
                )
            )
            for index, item in enumerate(data, start=1)
        ]

        if tasks:
            await asyncio.gather(*tasks)

    print(f"\n✅ All queries processed. Results in {output_file}")

    # Cleanup
    if ehr_pool:
        await ehr_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
