"""
AWS Bedrock Claude generator for agent inference
Uses boto3 bedrock-runtime client with Anthropic Messages API
"""
from typing import List, Optional, AsyncIterator, Dict, Any
import boto3
import json
from concurrent.futures import ThreadPoolExecutor
import asyncio

# Pre-import transformers to avoid issues in multiprocessing
try:
    from transformers import AutoTokenizer
    _TRANSFORMERS_AVAILABLE = True
except Exception as e:
    print(f"Warning: transformers not available: {e}")
    _TRANSFORMERS_AVAILABLE = False
    AutoTokenizer = None


class BedrockAsyncGenerator:
    """
    Async generator using AWS Bedrock with Claude models
    Uses native Anthropic Messages API with tool calling support
    """

    def __init__(
        self,
        model_id: str,
        region_name: str = "us-east-1",
        aws_access_key_id: str = None,
        aws_secret_access_key: str = None,
        max_tokens_default: int = 8192,
        max_workers: int = 10
    ):
        """
        Args:
            model_id: Bedrock model ID (e.g., "global.anthropic.claude-sonnet-4-6")
            region_name: AWS region
            aws_access_key_id: AWS access key (optional, will use boto3 defaults)
            aws_secret_access_key: AWS secret key (optional, will use boto3 defaults)
            max_tokens_default: Default max tokens for generation
            max_workers: Number of threads for async execution
        """
        self.model_id = model_id
        self.region_name = region_name
        self.max_tokens_default = max_tokens_default

        # Initialize boto3 client
        # If credentials not provided, boto3 will use environment variables or IAM role
        if aws_access_key_id and aws_secret_access_key:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=region_name,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key
            )
        else:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=region_name
            )

        # Thread pool for async execution of sync boto3 calls
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        # Tokenizer for compatibility (not used in native API path)
        self.tokenizer = None

        print(f"[Bedrock] Initialized with model: {model_id}, region: {region_name}")

    async def _init_tokenizer(self):
        """Initialize tokenizer for compatibility (uses GPT-2 as approximation)"""
        if self.tokenizer is not None:
            return

        if not _TRANSFORMERS_AVAILABLE or AutoTokenizer is None:
            print("[Bedrock] Warning: transformers not available, tokenizer not loaded")
            return

        # Use GPT-2 tokenizer as approximation
        # This is only for interface compatibility, not used in actual API calls
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                "gpt2",
                trust_remote_code=True
            )
            print(f"[Bedrock] Loaded compatibility tokenizer")
        except Exception as e:
            print(f"[Bedrock] Warning: Could not load tokenizer: {e}")

    async def generate(
        self,
        prompt_tokens: List[int],
        stop_tokens: Optional[List[int]] = None,
        stop_strings: Optional[List[str]] = None,
        temperature: float = 1.0,
        max_tokens: int = 0,
        return_logprobs: bool = False
    ) -> AsyncIterator[int]:
        """
        Generate tokens using Bedrock API (for compatibility)

        Note: This method is for interface compatibility.
        The actual agent should use chat_completion() with native tool calling.
        """
        raise NotImplementedError(
            "Token-based generation not supported for Bedrock. "
            "Use chat_completion() with native tool calling instead."
        )

    def _convert_messages_to_anthropic(
        self,
        messages: List[dict]
    ) -> tuple:
        """
        Convert OpenAI-style messages to Anthropic format

        Returns:
            (system_prompt, anthropic_messages)
        """
        system_prompt = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")

            if role == "system":
                # Anthropic uses separate system parameter
                system_prompt = content if isinstance(content, str) else ""
                continue

            elif role == "user":
                # User message
                anthropic_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": content}] if isinstance(content, str) else content
                })

            elif role == "assistant":
                # Assistant message (may have tool_calls)
                assistant_content = []

                # Add reasoning/thinking content if present
                reasoning = msg.get("reasoning_content")
                if reasoning:
                    assistant_content.append({
                        "type": "text",
                        "text": f"<think>{reasoning}</think>"
                    })

                # Add regular content
                if content:
                    assistant_content.append({
                        "type": "text",
                        "text": content
                    })

                # Add tool calls if present
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    for tc in tool_calls:
                        function_name = tc["function"]["name"]
                        function_args = tc["function"]["arguments"]

                        # Parse arguments if string
                        if isinstance(function_args, str):
                            try:
                                function_args = json.loads(function_args)
                            except:
                                pass

                        # Bedrock requires names matching ^[a-zA-Z0-9_-]{1,128}$
                        bedrock_name = function_name.replace(".", "_")

                        assistant_content.append({
                            "type": "tool_use",
                            "id": tc.get("id", "1"),
                            "name": bedrock_name,
                            "input": function_args
                        })

                if assistant_content:
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": assistant_content
                    })

            elif role == "tool":
                # Tool result message
                tool_call_id = msg.get("tool_call_id", "1")
                tool_content = msg.get("content", "")

                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": tool_content
                        }
                    ]
                })

        # Merge consecutive same-role messages (Anthropic requires alternating roles)
        merged = []
        for msg in anthropic_messages:
            if merged and merged[-1]["role"] == msg["role"]:
                # Merge content lists
                merged[-1]["content"].extend(msg["content"])
            else:
                merged.append(msg)

        return system_prompt, merged

    def _convert_tools_to_anthropic(
        self,
        tools: List[dict]
    ) -> List[dict]:
        """
        Convert OpenAI-style tools to Anthropic format

        OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "browser.search",
                "description": "...",
                "parameters": {...}
            }
        }

        Anthropic format:
        {
            "name": "browser.search",
            "description": "...",
            "input_schema": {...}
        }
        """
        anthropic_tools = []

        for tool in tools:
            if tool.get("type") == "function":
                func = tool["function"]
                # Bedrock requires tool names to match ^[a-zA-Z0-9_-]{1,128}$
                # Replace dots with underscores (e.g. "browser.search" -> "browser_search")
                name = func["name"].replace(".", "_")
                anthropic_tools.append({
                    "name": name,
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {})
                })

        return anthropic_tools

    def _convert_response_to_openai(
        self,
        bedrock_response: dict
    ) -> dict:
        """
        Convert Anthropic response to OpenAI format

        Anthropic response has:
        {
            "content": [
                {"type": "text", "text": "..."},
                {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            ],
            "stop_reason": "tool_use" | "end_turn"
        }

        OpenAI format:
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "...",
                    "tool_calls": [...]
                },
                "finish_reason": "tool_calls" | "stop"
            }]
        }
        """
        content_blocks = bedrock_response.get("content", [])

        text_content = ""
        tool_calls = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_content += block.get("text", "")

            elif block.get("type") == "tool_use":
                # Convert underscored names back to dotted format
                # (e.g. "browser_search" -> "browser.search")
                raw_name = block.get("name", "")
                if raw_name.startswith("browser_"):
                    raw_name = "browser." + raw_name[len("browser_"):]
                tool_calls.append({
                    "id": block.get("id", "1"),
                    "type": "function",
                    "function": {
                        "name": raw_name,
                        "arguments": json.dumps(block.get("input", {}))
                    }
                })

        # Determine finish reason
        stop_reason = bedrock_response.get("stop_reason", "end_turn")
        finish_reason = "tool_calls" if tool_calls else "stop"

        # Build OpenAI-compatible response
        message = {
            "role": "assistant",
            "content": text_content
        }

        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "choices": [{
                "message": message,
                "finish_reason": finish_reason
            }],
            "usage": bedrock_response.get("usage", {})
        }

    async def chat_completion(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: float = 1.0,
        max_tokens: int = None,
        use_reasoning_content: bool = True,
    ) -> dict:
        """
        Create a chat completion with optional tool calling using Bedrock Anthropic API

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: List of tool definitions in OpenAI format
            tool_choice: "auto", "none", or specific tool (Anthropic supports "auto", "any", or specific tool)
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            use_reasoning_content: If True, use 'reasoning_content' field for assistant messages

        Returns:
            Response dict in OpenAI format
        """
        await self._init_tokenizer()

        # Convert messages to Anthropic format
        system_prompt, anthropic_messages = self._convert_messages_to_anthropic(messages)

        # Build request
        request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens or self.max_tokens_default,
            "temperature": temperature,
            "messages": anthropic_messages
        }

        # Add system prompt if present
        if system_prompt:
            request["system"] = system_prompt

        # Add tools if provided
        if tools:
            anthropic_tools = self._convert_tools_to_anthropic(tools)
            request["tools"] = anthropic_tools

            # Convert tool_choice
            if tool_choice == "auto":
                request["tool_choice"] = {"type": "auto"}
            elif tool_choice == "none":
                # Anthropic doesn't have explicit "none", just omit tool_choice
                pass
            else:
                # Specific tool
                request["tool_choice"] = {"type": "tool", "name": tool_choice}

        print(f"[Bedrock] Request: model={self.model_id}, messages={len(anthropic_messages)}, tools={len(tools) if tools else 0}")

        # Make async request using thread pool
        loop = asyncio.get_event_loop()

        try:
            response = await loop.run_in_executor(
                self.executor,
                lambda: self.client.invoke_model(
                    modelId=self.model_id,
                    body=json.dumps(request)
                )
            )

            # Parse response
            model_response = json.loads(response["body"].read())

            print(f"[Bedrock] Response received: stop_reason={model_response.get('stop_reason')}")

            # Convert to OpenAI format
            return self._convert_response_to_openai(model_response)

        except Exception as e:
            print(f"[Bedrock] Error: {e}")
            raise

    def shutdown(self) -> None:
        """Close the thread pool executor"""
        try:
            self.executor.shutdown(wait=False)
        except Exception:
            pass

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass
