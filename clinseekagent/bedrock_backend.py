"""
AWS Bedrock Claude generator for agent inference
Uses boto3 bedrock-runtime client with Anthropic Messages API
"""
from typing import List, Optional, AsyncIterator, Dict, Any
import boto3
from botocore.config import Config as BotoConfig
import json
import re
from concurrent.futures import ThreadPoolExecutor
import asyncio
from botocore.exceptions import BotoCoreError, ClientError


def _sanitize_tool_name(name: str) -> str:
    """Sanitize a tool name for Bedrock Converse API: [a-zA-Z0-9_-]+, max 64 chars."""
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return name[:64]

# Pre-import transformers to avoid issues in multiprocessing
try:
    from transformers import AutoTokenizer
    _TRANSFORMERS_AVAILABLE = True
except Exception as e:
    print(f"Warning: transformers not available: {e}")
    _TRANSFORMERS_AVAILABLE = False
    AutoTokenizer = None


API_RETRY_DELAY_SECONDS = 5
API_MAX_RETRIES = 2

# Claude models on which the fixed `budget_tokens` thinking form was removed:
# they take adaptive thinking, with depth set by `output_config.effort`.
# Claude 4.6 and older still accept a fixed budget, which is what the paper
# setting uses (extended thinking with a 4,096-token budget).
ADAPTIVE_ONLY_THINKING_MARKERS = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable-5",
    "claude-mythos-5",
)

# Claude models that reject temperature / top_p / top_k with a 400.
SAMPLING_REMOVED_MARKERS = ADAPTIVE_ONLY_THINKING_MARKERS


def requires_adaptive_thinking(model_id: str) -> bool:
    normalized = (model_id or "").lower()
    return any(marker in normalized for marker in ADAPTIVE_ONLY_THINKING_MARKERS)


def rejects_sampling_params(model_id: str) -> bool:
    normalized = (model_id or "").lower()
    return any(marker in normalized for marker in SAMPLING_REMOVED_MARKERS)


class BedrockAsyncGenerator:
    """
    Async generator using AWS Bedrock with Claude models
    Uses native Anthropic Messages API with tool calling support
    """

    # Tag on this class's log lines. Subclasses that reuse these methods over a
    # different transport (see vertex_backend) override it so the output names
    # the provider actually in use.
    _LOG_PREFIX = "[Bedrock]"

    def __init__(
        self,
        model_id: str,
        region_name: str = "us-east-1",
        aws_access_key_id: str = None,
        aws_secret_access_key: str = None,
        max_tokens_default: int = 8192,
        max_workers: int = 10,
        enable_thinking: Optional[bool] = None,
        thinking_budget_tokens: int = 4096,
        reasoning_effort: Optional[str] = None,
    ):
        """
        Args:
            model_id: Bedrock model ID (e.g., "us.anthropic.claude-opus-4-6-v1")
            region_name: AWS region
            aws_access_key_id: AWS access key (optional, will use boto3 defaults)
            aws_secret_access_key: AWS secret key (optional, will use boto3 defaults)
            max_tokens_default: Default max tokens for generation
            max_workers: Number of threads for async execution
            enable_thinking: Turn Claude extended thinking on.
            thinking_budget_tokens: Fixed thinking budget for models that
                accept one (Claude 4.6 and older).
            reasoning_effort: `output_config.effort` for models that only take
                adaptive thinking (Claude Opus 4.7+, Opus 5, Sonnet 5, ...).
        """
        self.model_id = model_id
        self.region_name = region_name
        self.max_tokens_default = max_tokens_default
        self.enable_thinking = enable_thinking
        self.thinking_budget_tokens = thinking_budget_tokens
        self.reasoning_effort = reasoning_effort
        self._warned_dropped_temperature = False

        # Off by default; see VLLMOpenAIAsyncGenerator.capture_raw_response.
        # Here the untouched payload is the Anthropic response dict, whose
        # content blocks carry thinking text and signatures.
        self.capture_raw_response = False

        self.token_usage = self._new_token_usage()

        # Initialize boto3 client with bounded timeouts so a silent TLS hang
        # surfaces as an exception instead of blocking the async loop forever.
        # A per-request hard ceiling is layered on top via asyncio.wait_for
        # (see _invoke_with_timeout); this config is the lower bound.
        boto_config = BotoConfig(
            read_timeout=90,
            connect_timeout=10,
            retries={"max_attempts": 0},  # we handle retries ourselves
        )
        if aws_access_key_id and aws_secret_access_key:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=region_name,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                config=boto_config,
            )
        else:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=region_name,
                config=boto_config,
            )

        # Thread pool for async execution of sync boto3 calls
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        # Tokenizer for compatibility (not used in native API path)
        self.tokenizer = None

        print(f"{self._LOG_PREFIX} Initialized with model: {model_id}, region: {region_name}")

    @staticmethod
    def _clone_jsonable(value: Any) -> Any:
        return json.loads(json.dumps(value))

    def _supports_adaptive_thinking(self) -> bool:
        """True when the model only accepts adaptive thinking."""
        return requires_adaptive_thinking(self.model_id)

    def _rejects_sampling_params(self) -> bool:
        return rejects_sampling_params(self.model_id)

    def _warn_dropped_temperature(self, value: float, reason: str) -> None:
        if self._warned_dropped_temperature:
            return
        self._warned_dropped_temperature = True
        print(
            f"{self._LOG_PREFIX} Warning: temperature={value} is not sent to "
            f"{self.model_id} ({reason})."
        )

    def _build_thinking_config(self, max_tokens: int) -> Optional[dict]:
        if not self.enable_thinking:
            return None

        if self._supports_adaptive_thinking():
            return {"type": "adaptive"}

        budget_tokens = min(self.thinking_budget_tokens, max_tokens - 1)
        if budget_tokens < 1024:
            raise ValueError(
                "Bedrock thinking requires max_tokens to exceed 1024 so a valid "
                "thinking budget can be allocated."
            )
        return {
            "type": "enabled",
            "budget_tokens": budget_tokens,
        }

    @staticmethod
    def _extract_reasoning_content(content_blocks: List[dict]) -> str:
        reasoning_parts = []
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    reasoning_parts.append(thinking.strip())
            elif block_type == "redacted_thinking":
                reasoning_parts.append("[redacted_thinking]")
        return "\n\n".join(reasoning_parts).strip()

    @staticmethod
    def _is_retriable_api_error(exc: Exception) -> bool:
        if isinstance(exc, ClientError):
            error = (getattr(exc, "response", None) or {}).get("Error", {})
            error_code = (error.get("Code") or "").strip()
            if error_code in {
                "InternalServerException",
                "ModelNotReadyException",
                "RequestTimeoutException",
                "ServiceUnavailableException",
                "ThrottlingException",
                "TooManyRequestsException",
            }:
                return True

        if isinstance(exc, BotoCoreError):
            return True

        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "busy",
                "rate exceeded",
                "service unavailable",
                "temporarily unavailable",
                "throttl",
                "timeout",
                "too many requests",
            )
        )

    # Hard ceiling (seconds) for a single invoke_model call, regardless of what
    # boto3 / the TLS layer does. Prevents hung shards from blocking the
    # async loop. Slightly larger than BotoConfig.read_timeout so the socket
    # layer errors first when it can.
    _ASYNCIO_INVOKE_TIMEOUT_SECONDS = 120

    async def _invoke_model_with_retry(self, request: dict) -> dict:
        loop = asyncio.get_event_loop()
        total_attempts = API_MAX_RETRIES + 1

        for attempt in range(1, total_attempts + 1):
            try:
                response = await asyncio.wait_for(
                    loop.run_in_executor(
                        self.executor,
                        lambda: self.client.invoke_model(
                            modelId=self.model_id,
                            body=json.dumps(request)
                        )
                    ),
                    timeout=self._ASYNCIO_INVOKE_TIMEOUT_SECONDS,
                )
                return json.loads(response["body"].read())
            except asyncio.TimeoutError:
                msg = (
                    f"asyncio wait_for timeout after "
                    f"{self._ASYNCIO_INVOKE_TIMEOUT_SECONDS}s"
                )
                if attempt >= total_attempts:
                    raise TimeoutError(msg)
                print(
                    f"{self._LOG_PREFIX} {msg} | "
                    f"retry {attempt}/{API_MAX_RETRIES} in "
                    f"{API_RETRY_DELAY_SECONDS}s"
                )
                await asyncio.sleep(API_RETRY_DELAY_SECONDS)
            except Exception as exc:
                if attempt >= total_attempts or not self._is_retriable_api_error(exc):
                    raise

                retry_index = attempt
                print(
                    f"{self._LOG_PREFIX} Retriable API error: {exc} | "
                    f"retry {retry_index}/{API_MAX_RETRIES} in "
                    f"{API_RETRY_DELAY_SECONDS}s"
                )
                await asyncio.sleep(API_RETRY_DELAY_SECONDS)

    async def _init_tokenizer(self):
        """Initialize tokenizer for compatibility (uses GPT-2 as approximation)"""
        if self.tokenizer is not None:
            return

        if not _TRANSFORMERS_AVAILABLE or AutoTokenizer is None:
            print(f"{self._LOG_PREFIX} Warning: transformers not available, tokenizer not loaded")
            return

        # Use GPT-2 tokenizer as approximation
        # This is only for interface compatibility, not used in actual API calls
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                "gpt2",
                trust_remote_code=True
            )
            print(f"{self._LOG_PREFIX} Loaded compatibility tokenizer")
        except Exception as e:
            print(f"{self._LOG_PREFIX} Warning: Could not load tokenizer: {e}")

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
                raw_bedrock_blocks = msg.get("bedrock_content_blocks")
                assistant_content = []

                if isinstance(raw_bedrock_blocks, list) and raw_bedrock_blocks:
                    assistant_content = self._clone_jsonable(raw_bedrock_blocks)

                if not assistant_content:
                    # Fallback path for messages that were not produced by Bedrock.
                    # reasoning_content is intentionally omitted here: the Claude
                    # API requires thinking blocks to carry their original
                    # signature, which we don't have in the fallback path.

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
                                except Exception:
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
        reasoning_content = self._extract_reasoning_content(content_blocks)

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

        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if content_blocks:
            message["bedrock_content_blocks"] = self._clone_jsonable(content_blocks)
        if tool_calls:
            message["tool_calls"] = tool_calls
        if self.capture_raw_response:
            message["raw_response"] = self._clone_jsonable(bedrock_response)

        usage = bedrock_response.get("usage", {}) or {}
        self._accumulate_usage(usage)

        return {
            "choices": [{
                "message": message,
                "finish_reason": finish_reason
            }],
            "usage": usage
        }

    @staticmethod
    def _new_token_usage() -> dict:
        """Running token total across every call a generator makes.

        An agent run costs one billable request per round, so a single
        response's usage says nothing — only the sum says what a trajectory
        cost. Subclasses that skip this __init__ must set it themselves.
        """
        return {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def _accumulate_usage(self, usage: dict) -> None:
        """Fold one response's usage into the running total."""
        self.token_usage["requests"] += 1
        for key in ("input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                self.token_usage[key] += value

    def usage_summary(self) -> str:
        u = self.token_usage
        parts = [
            f"{u['requests']} requests",
            f"in={u['input_tokens']:,}",
            f"out={u['output_tokens']:,}",
        ]
        if u["cache_read_input_tokens"]:
            parts.append(f"cache_read={u['cache_read_input_tokens']:,}")
        if u["cache_creation_input_tokens"]:
            parts.append(f"cache_write={u['cache_creation_input_tokens']:,}")
        return f"{self._LOG_PREFIX} token usage: " + ", ".join(parts)

    def _is_anthropic_model(self) -> bool:
        """Check if the current model is an Anthropic model."""
        return "anthropic" in self.model_id.lower()

    # ── Main entry point ─────────────────────────────────────────────────

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
        Create a chat completion with optional tool calling.

        Uses Anthropic invoke_model API for Anthropic models,
        and OpenAI-compatible invoke_model for other providers (e.g. Kimi).

        Returns:
            Response dict in OpenAI format
        """
        await self._init_tokenizer()

        if self._is_anthropic_model():
            return await self._chat_completion_anthropic(
                messages, tools, tool_choice, temperature, max_tokens, use_reasoning_content
            )
        else:
            return await self._chat_completion_openai(
                messages, tools, tool_choice, temperature, max_tokens
            )

    async def _chat_completion_anthropic(
        self,
        messages: List[dict],
        tools: Optional[List[dict]],
        tool_choice: str,
        temperature: float,
        max_tokens: int,
        use_reasoning_content: bool = True,
    ) -> dict:
        """Chat completion via Anthropic invoke_model API."""
        system_prompt, anthropic_messages = self._convert_messages_to_anthropic(messages)
        request_max_tokens = max_tokens or self.max_tokens_default
        thinking_config = self._build_thinking_config(request_max_tokens)

        request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": request_max_tokens,
            "messages": anthropic_messages
        }
        if thinking_config:
            # Claude does not accept a sampling temperature together with
            # extended thinking, so a requested temperature is dropped rather
            # than failing the run.
            if temperature != 1.0:
                self._warn_dropped_temperature(
                    temperature, "not settable with extended thinking")
            request["thinking"] = thinking_config
            if thinking_config.get("type") == "adaptive" and self.reasoning_effort:
                request["output_config"] = {"effort": self.reasoning_effort}
        elif self._rejects_sampling_params():
            if temperature != 1.0:
                self._warn_dropped_temperature(
                    temperature, "the model does not accept sampling parameters")
        else:
            request["temperature"] = temperature

        if system_prompt:
            request["system"] = system_prompt

        if tools:
            anthropic_tools = self._convert_tools_to_anthropic(tools)
            request["tools"] = anthropic_tools

            if tool_choice == "auto":
                request["tool_choice"] = {"type": "auto"}
            elif tool_choice == "none":
                pass
            elif thinking_config:
                # Claude rejects forced tool use (`any` / a named tool) while
                # extended thinking is on. Callers that force a tool (the
                # single-pass retrieval and forced-final-answer rounds) also
                # restrict the tool list and instruct the model, so `auto` keeps
                # the same contract without disabling thinking.
                request["tool_choice"] = {"type": "auto"}
            elif tool_choice == "any":
                request["tool_choice"] = {"type": "any"}
            else:
                # Specific tool
                request["tool_choice"] = {
                    "type": "tool",
                    "name": tool_choice.replace(".", "_"),
                }

        print(
            f"{self._LOG_PREFIX} Request: model={self.model_id}, messages={len(anthropic_messages)}, "
            f"tools={len(tools) if tools else 0}, "
            f"thinking={thinking_config['type'] if thinking_config else 'disabled'}"
        )

        try:
            model_response = await self._invoke_model_with_retry(request)

            print(f"{self._LOG_PREFIX} Response received: stop_reason={model_response.get('stop_reason')}")

            # Convert to OpenAI format
            converted = self._convert_response_to_openai(model_response)
            if not use_reasoning_content:
                converted["choices"][0]["message"].pop("reasoning_content", None)
            return converted

        except Exception as e:
            print(f"{self._LOG_PREFIX} Error: {e}")
            raise

    def _prepare_openai_messages(self, messages: List[dict]) -> List[dict]:
        """
        Clean messages for OpenAI-compatible models.
        Strips internal fields (reasoning_content), ensures tool_calls
        is absent (not None) when there are no tool calls, and
        ensures tool call arguments are always valid JSON strings.
        When a user message ships Anthropic-style content blocks (list of
        {type: text|image|...}), we flatten to a single text string because
        the OSS Bedrock models accept only string `content`. Image blocks are
        replaced with a short `[image ...]` marker so the turn stays coherent.
        """
        def _flatten(content):
            if not isinstance(content, list):
                return content
            parts = []
            for b in content:
                if not isinstance(b, dict):
                    parts.append(str(b))
                    continue
                btype = b.get("type")
                if btype == "text":
                    parts.append(b.get("text", ""))
                elif btype == "image":
                    src = b.get("source") or {}
                    media = src.get("media_type") or src.get("type") or "image"
                    parts.append(f"[image attached ({media}) — not inlined for this model]")
                else:
                    parts.append(b.get("text") or json.dumps(b)[:200])
            return "\n".join(p for p in parts if p)

        cleaned = []
        for msg in messages:
            m = {"role": msg["role"]}
            if msg.get("content") is not None:
                m["content"] = _flatten(msg["content"])
            if msg.get("tool_calls"):
                safe_tcs = []
                for tc in msg["tool_calls"]:
                    tc_copy = dict(tc)
                    func = dict(tc_copy.get("function", {}))
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            func["arguments"] = "{}"
                    tc_copy["function"] = func
                    safe_tcs.append(tc_copy)
                m["tool_calls"] = safe_tcs
            if msg.get("tool_call_id"):
                m["tool_call_id"] = msg["tool_call_id"]
            cleaned.append(m)
        return cleaned

    @staticmethod
    def _truncate_tool_results(messages: List[dict], max_body_bytes: int = 10_000_000) -> List[dict]:
        """
        If serialized messages exceed max_body_bytes, progressively truncate
        the oldest tool result contents to bring the payload under limit.
        """
        body = json.dumps(messages)
        if len(body.encode()) <= max_body_bytes:
            return messages

        messages = [dict(m) for m in messages]
        for m in messages:
            if m.get("role") == "tool" and m.get("content"):
                content = m["content"]
                if len(content) > 500:
                    m["content"] = content[:500] + "\n... [truncated]"
                    body = json.dumps(messages)
                    if len(body.encode()) <= max_body_bytes:
                        return messages
        return messages

    async def _chat_completion_openai(
        self,
        messages: List[dict],
        tools: Optional[List[dict]],
        tool_choice: str,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """Chat completion for OpenAI-compatible Bedrock models.

        Covers Kimi-K2.5, MiniMax-M2.5, Qwen3-VL, GLM-4.7, GPT-OSS, etc.
        The request body follows OpenAI chat-completions shape; responses
        come back with choices[].message having {content, tool_calls} —
        `content` is `null` on tool-only turns (OpenAI convention). We
        normalize that to an empty string and coerce tool_calls to a list
        so downstream callers don't need model-specific guards.
        """
        openai_messages = self._prepare_openai_messages(messages)
        openai_messages = self._truncate_tool_results(openai_messages)

        request = {
            "max_tokens": max_tokens or self.max_tokens_default,
            "temperature": temperature,
            "messages": openai_messages,
        }
        if tools:
            request["tools"] = tools
            # Forward non-default tool_choice; Bedrock's OpenAI shim accepts
            # "auto" / "none" / {"type":"function", "function":{"name":...}}.
            if tool_choice and tool_choice != "auto":
                request["tool_choice"] = tool_choice

        print(
            f"[Bedrock/OpenAI] Request: model={self.model_id}, "
            f"messages={len(openai_messages)}, "
            f"tools={len(tools) if tools else 0}"
        )

        loop = asyncio.get_event_loop()
        max_retries = 3
        request_body = json.dumps(request)

        for attempt in range(1, max_retries + 1):
            try:
                response = await asyncio.wait_for(
                    loop.run_in_executor(
                        self.executor,
                        lambda: self.client.invoke_model(
                            modelId=self.model_id,
                            body=request_body
                        )
                    ),
                    timeout=self._ASYNCIO_INVOKE_TIMEOUT_SECONDS,
                )

                model_response = json.loads(response["body"].read())
                self._normalize_openai_response(model_response)

                choice = (model_response.get("choices") or [{}])[0]
                finish_reason = choice.get("finish_reason", "stop")
                message = choice.get("message") or {}
                tool_calls = message.get("tool_calls") or []

                print(
                    f"[Bedrock/OpenAI] Response received: "
                    f"finish_reason={finish_reason}, tool_calls={len(tool_calls)}"
                )

                return model_response

            except asyncio.TimeoutError:
                msg = (
                    f"asyncio wait_for timeout after "
                    f"{self._ASYNCIO_INVOKE_TIMEOUT_SECONDS}s"
                )
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"[Bedrock/OpenAI] Retry {attempt}/{max_retries} after {wait}s: {msg}")
                    await asyncio.sleep(wait)
                    continue
                print(f"[Bedrock/OpenAI] Error (attempt {attempt}/{max_retries}): {msg}")
                raise TimeoutError(msg)
            except Exception as e:
                is_retryable = any(kw in str(e).lower() for kw in ["timeout", "throttl", "too many requests", "service unavailable", "internal server error", "internalserver", "unexpected error"])
                if is_retryable and attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"[Bedrock/OpenAI] Retry {attempt}/{max_retries} after {wait}s: {e}")
                    await asyncio.sleep(wait)
                    continue
                print(f"[Bedrock/OpenAI] Error (attempt {attempt}/{max_retries}): {e}")
                raise

    @staticmethod
    def _normalize_openai_response(resp: dict) -> None:
        """In-place guard: make every assistant message safe to log/consume.

        OpenAI-contract models emit `content: null` on tool-only turns
        (and sometimes `tool_calls: null` on plain-text turns). Downstream
        code assumes both are non-None when the key exists, so coerce them
        here once instead of sprinkling `or ""` guards at every call site.
        Also preserves any CoT strings the model inlines in `content`
        (e.g. MiniMax's `<reasoning>…</reasoning>` blocks) verbatim.
        """
        for choice in resp.get("choices") or []:
            msg = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(msg, dict):
                continue
            if msg.get("content") is None:
                msg["content"] = ""
            if msg.get("tool_calls") is None:
                msg["tool_calls"] = []

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
