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

def vprint(*args, **kwargs):
    """Verbose print: only prints when VERBOSE is True."""
    if VERBOSE:
        print(*args, **kwargs)


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
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: EHRToolPool = None,
    max_rounds: int = 200
):
    """Run a single query and return the result."""
    try:
        messages = await run_one_native(
            question=question,
            qid=qid,
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


async def main():
    """Main entry point."""
    # Load environment variables
    dotenv.load_dotenv("../.env")
    
    parser = argparse.ArgumentParser(description="OpenResearcher with EHR integration")

    # Model configuration
    parser.add_argument("--model_name_or_path", type=str, default="us.anthropic.claude-sonnet-4-5-v1:0",
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
    parser.add_argument("--ehr_mcp_url", type=str, default="http://127.0.0.1:5002/mcp",
                        help="EHR MCP server URL")

    # Data configuration
    parser.add_argument("--data_path", type=str, required=True,
                        help="Path to JSONL file with questions")
    parser.add_argument("--output_dir", type=str, default="./results",
                        help="Output directory for results")

    # Execution configuration
    parser.add_argument("--max_rounds", type=int, default=200,
                        help="Maximum conversation rounds")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose output")

    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

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
    data = []
    with open(args.data_path, 'r', encoding='utf-8') as f:
        for line in f:
            data.append(json.loads(line))

    print(f"Loaded {len(data)} queries from {args.data_path}")

    # Process queries
    output_file = os.path.join(args.output_dir, "results.jsonl")
    with open(output_file, 'w', encoding='utf-8') as out_f:
        for item in data:
            qid = item.get('qid', item.get('query_id', 'unknown'))

            # Generate question from task data if not present
            if 'question' in item or 'query' in item:
                question = item.get('question', item.get('query', ''))
            else:
                # Generate question from task data using original prompt template
                from data_utils import generate_question_from_task
                question = generate_question_from_task(item)

            print(f"\n{'='*80}")
            print(f"Processing qid={qid}: {question[:100]}...")
            print(f"{'='*80}")

            result = await run_one_query(
                question=question,
                qid=qid,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                max_rounds=args.max_rounds
            )

            # Write result
            out_f.write(json.dumps(result, ensure_ascii=False) + '\n')
            out_f.flush()

            print(f"[qid={qid}] Result written to {output_file}")

    print(f"\n✅ All queries processed. Results in {output_file}")

    # Cleanup
    if ehr_pool:
        await ehr_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
