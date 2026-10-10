"""
OpenAI-compatible async generator.

Used for vLLM-served local models (ClinSeek-35B-A3B, Qwen3.5) and for any other
OpenAI-compatible chat-completions endpoint passed through ``--api_base_url``.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from openai import OpenAI


TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)
XML_FUNCTION_CALL_RE = re.compile(
    r"<function=([^\n>]+)>\s*(.*?)\s*</function>",
    re.DOTALL,
)
XML_PARAMETER_RE = re.compile(
    r"<parameter=([^\n>]+)>\s*(.*?)\s*</parameter>",
    re.DOTALL,
)
BRACKET_TOOL_CALL_PREFIX = "[Tool Call:"


class VLLMOpenAIAsyncGenerator:
    """
    Async wrapper around vLLM's OpenAI-compatible chat completions API.
    Returns the same minimal OpenAI-style dict shape used by run_text.py.
    """

    def __init__(
        self,
        model_name: Optional[str],
        base_url: str = "http://127.0.0.1:4000",
        api_key: str = "EMPTY",
        max_tokens_default: int = 8192,
        max_workers: int = 10,
        enable_thinking: Optional[bool] = None,
        strip_images: bool = False,
        request_max_attempts: int = 1,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 60.0,
        client_max_retries: int = 2,
        min_request_interval: float = 0.0,
        reasoning_effort: Optional[str] = None,
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = api_key
        self.model_name = model_name
        self.max_tokens_default = max_tokens_default
        self.enable_thinking = enable_thinking
        self.strip_images = strip_images
        self.request_max_attempts = request_max_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.client_max_retries = client_max_retries
        self.min_request_interval = min_request_interval
        # Only sent to OpenAI reasoning models (see _reasoning_kwargs); vLLM
        # chat templates take the thinking switch through chat_template_kwargs.
        self.reasoning_effort = reasoning_effort
        self._api_gate_lock = asyncio.Lock()
        self._next_api_attempt_at = 0.0
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        # Off by default: when set, every converted response carries the
        # provider's untouched payload under `raw_response`, so the caller can
        # persist thinking blocks / reasoning items / signatures that the
        # OpenAI-shaped message drops. Analysis-only — it makes trajectories
        # several times larger. Drivers flip it from a CLI flag.
        self.capture_raw_response = False

        # Running token total across every call, for cost accounting. One
        # billable request per round, so only the run-level sum is meaningful.
        self.token_usage = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        print(
            f"[{self._LOG_PREFIX}] Initialized with base_url={self.base_url}, "
            f"model={self.model_name or 'auto'}"
        )

    # Tag on log lines.
    _LOG_PREFIX = "vLLM"

    # Model-id substrings for which sampling params (temperature/top_p/top_k)
    # were removed -- these Claude tiers reject `temperature` with a 400 when
    # reached through an OpenAI-compatible proxy. Matched as substrings so
    # provider prefixes still hit.
    _SAMPLING_REMOVED = (
        "opus-4-7", "opus-4-8", "opus-5", "fable-5", "mythos-5", "sonnet-5",
        "haiku-5",
    )

    @staticmethod
    def _is_openai_reasoning_model(model_name: str) -> bool:
        """OpenAI reasoning models (GPT-5.x, o-series) behind the OpenAI API.

        They take `reasoning_effort` and `max_completion_tokens` and reject
        `max_tokens` and a non-default `temperature`.
        """
        name = (model_name or "").lower().rsplit("/", 1)[-1]
        return name.startswith(("gpt-5", "o1", "o3", "o4")) or "openai/" in (
            model_name or ""
        ).lower()

    def _sampling_kwargs(self, model_name: str, temperature: float) -> dict:
        """{'temperature': ...} unless the model rejects sampling params."""
        if any(tag in (model_name or "") for tag in self._SAMPLING_REMOVED):
            return {}
        if self._is_openai_reasoning_model(model_name):
            return {}
        return {"temperature": temperature}

    def _reasoning_kwargs(self, model_name: str) -> dict:
        """`reasoning_effort` for OpenAI reasoning models when thinking is on."""
        if (
            self.enable_thinking
            and self.reasoning_effort
            and self._is_openai_reasoning_model(model_name)
        ):
            return {"reasoning_effort": self.reasoning_effort}
        return {}

    def _max_token_kwargs(
        self,
        model_name: str,
        max_tokens: Optional[int],
    ) -> dict:
        """Output-token parameter in the spelling the endpoint accepts."""
        if self._is_openai_reasoning_model(model_name):
            return {"max_completion_tokens": max_tokens or self.max_tokens_default}
        return {"max_tokens": max_tokens or self.max_tokens_default}

    def _accumulate_usage(self, usage_dict: dict) -> None:
        """Fold one OpenAI-shaped usage dict into the running total."""
        self.token_usage["requests"] += 1
        pt = usage_dict.get("prompt_tokens")
        ct = usage_dict.get("completion_tokens")
        if isinstance(pt, int):
            self.token_usage["input_tokens"] += pt
        if isinstance(ct, int):
            self.token_usage["output_tokens"] += ct
        # OpenAI-compatible servers may report cache tokens nested; best-effort.
        details = usage_dict.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if isinstance(cached, int):
            self.token_usage["cache_read_input_tokens"] += cached

    def _accumulate_cost_headers(self, headers) -> None:
        """Sum per-call cost headers, if the endpoint reports them.

        LiteLLM-style proxies return the charged cost of each call in
        `x-litellm-response-cost*` headers. vLLM does not set them, in which
        case the totals stay 0 and cost falls back to token-based estimation.
        """
        try:
            get = headers.get
        except AttributeError:
            return
        for header, key in (
            ("x-litellm-response-cost", "charged_cost_usd"),
            ("x-litellm-response-cost-original", "list_cost_usd"),
        ):
            val = get(header)
            if val is not None:
                try:
                    self.token_usage[key] = self.token_usage.get(key, 0.0) + float(val)
                except (TypeError, ValueError):
                    pass

    def usage_summary(self) -> str:
        u = self.token_usage
        s = (f"[{self._LOG_PREFIX}] token usage: {u['requests']} requests, "
             f"in={u['input_tokens']:,}, out={u['output_tokens']:,}")
        if u.get("charged_cost_usd"):
            s += f", charged=${u['charged_cost_usd']:.4f}"
        return s

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        return normalized

    async def _init_tokenizer(self):
        """Compatibility no-op; run_text checks for this method."""
        return None

    @staticmethod
    def _is_clinseek_sft_model(model_name: Optional[str]) -> bool:
        """Return whether the model uses ClinSeek's legacy bracket protocol.

        The public ClinSeek-35B-A3B release has an OpenAI-compatible native
        tool-calling chat template.  Older ClinSeek checkpoints used the
        plain-text ``[Tool Call: ...]`` convention, so keep that compatibility
        path without accidentally disabling API tools for the released model.
        """
        model_name_lower = (model_name or "").lower()
        if "clinseek-35b-a3b" in model_name_lower:
            return False
        return "clinseek" in model_name_lower

    @staticmethod
    def _normalize_tool_name(function_name: str) -> str:
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

        if name.startswith("ehr.") or name.startswith("ehr_"):
            return name
        if "." not in name:
            return f"ehr_{name}"
        return name

    @staticmethod
    def _api_safe_tool_name(name: str) -> str:
        """Tool name acceptable to strict OpenAI-compatible schemas.

        Strict endpoints require function names to match ^[a-zA-Z0-9_-]+$, so
        the dotted namespaces (ehr.load_ehr, image.dicom_processor) are
        rejected -- including in replayed assistant tool_calls. Send
        underscored names; the model returns underscored, and
        run_text.normalize_tool_call_name restores the dot before dispatch
        (same contract Bedrock uses). Legacy bracket-protocol ClinSeek models
        never see tools (api_tools=None), so this does not affect them.
        """
        return (name or "").replace(".", "_")

    @classmethod
    def _sanitize_tools_for_api(cls, tools):
        """Copy the tool list with API-safe (undotted) function names."""
        if not tools:
            return tools
        out = []
        for tool in tools:
            fn = (tool or {}).get("function") or {}
            name = fn.get("name", "")
            if "." in name:
                tool = copy.deepcopy(tool)
                tool["function"]["name"] = cls._api_safe_tool_name(name)
            out.append(tool)
        return out

    @classmethod
    def _sanitize_tool_choice_for_api(cls, tool_choice):
        """Translate the driver's common tool-choice vocabulary to OpenAI.

        Bedrock accepts ``"any"`` and a bare tool name. OpenAI-compatible
        endpoints instead use ``"required"`` and a structured object for a
        specific function, whose name must also use the API-safe underscore
        spelling.
        """
        if tool_choice == "any":
            return "required"
        if tool_choice is None or (
            isinstance(tool_choice, str)
            and tool_choice in {"auto", "required", "none"}
        ):
            return tool_choice
        if isinstance(tool_choice, str):
            return {
                "type": "function",
                "function": {
                    "name": cls._api_safe_tool_name(tool_choice),
                },
            }
        if isinstance(tool_choice, dict):
            sanitized = copy.deepcopy(tool_choice)
            function = sanitized.get("function")
            if isinstance(function, dict) and isinstance(
                function.get("name"), str
            ):
                function["name"] = cls._api_safe_tool_name(function["name"])
            return sanitized
        return tool_choice

    @staticmethod
    def _stringify_message_field(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _anthropic_to_openai_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Anthropic-style content blocks to OpenAI chat vision blocks.

        Input shapes handled:
          {"type": "text", "text": "..."}                 -> kept as-is
          {"type": "image", "source": {"type": "base64",
               "media_type": "image/jpeg", "data": "<b64>"}}
              -> {"type": "image_url",
                  "image_url": {"url": "data:image/jpeg;base64,<b64>"}}
          {"type": "image_url", ...}                      -> kept as-is
        Unknown shapes are stringified and wrapped as a text block.
        """
        converted: List[Dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                converted.append({"type": "text", "text": str(block)})
                continue
            btype = block.get("type")
            if btype == "text":
                converted.append({"type": "text", "text": block.get("text", "")})
            elif btype == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64":
                    media = source.get("media_type") or "image/jpeg"
                    data = source.get("data") or ""
                    converted.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media};base64,{data}",
                        },
                    })
                elif source.get("type") == "url":
                    converted.append({
                        "type": "image_url",
                        "image_url": {"url": source.get("url", "")},
                    })
                else:
                    converted.append({"type": "text", "text": json.dumps(block, ensure_ascii=False)})
            elif btype == "image_url":
                converted.append(block)
            else:
                converted.append({"type": "text", "text": json.dumps(block, ensure_ascii=False)})
        return converted

    @classmethod
    def _normalize_user_content(cls, value: Any, *, strip_images: bool = False) -> Any:
        """Turn an Anthropic-style content value into an OpenAI-compatible one.

        - str / None / dict-like → stringified (preserves old behavior).
        - list of blocks → converted to OpenAI vision blocks (text + image_url).
          If the list has only text blocks, it is flattened to a plain string.

        When *strip_images* is True, image blocks are dropped and replaced with
        a short text note so that text-only models can still function (the
        agent can use image MCP tools to analyse images instead).
        """
        if not isinstance(value, list):
            return cls._stringify_message_field(value)
        if strip_images:
            text_parts: List[str] = []
            n_images = 0
            for block in value:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype in ("image", "image_url"):
                    n_images += 1
                else:
                    text_parts.append(json.dumps(block, ensure_ascii=False))
            if n_images:
                text_parts.append(
                    f"[{n_images} image(s) attached — use the image MCP tools "
                    f"to analyse them]"
                )
            return "\n".join(text_parts)
        converted = cls._anthropic_to_openai_blocks(value)
        if all(b.get("type") == "text" for b in converted):
            return "\n".join(b.get("text", "") for b in converted)
        return converted

    @classmethod
    def _normalize_assistant_tool_calls(
        cls,
        tool_calls: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized_tool_calls: List[Dict[str, Any]] = []

        for index, tool_call in enumerate(tool_calls, start=1):
            function = tool_call.get("function", {})
            # Undotted for the API schema; run_text restores the dot on the way
            # back in. Without this, replaying a prior ehr.load_ehr tool_call
            # 400s on strict OpenAI-compatible endpoints.
            function_name = cls._api_safe_tool_name(
                cls._normalize_tool_name(function.get("name", ""))
            )
            if not function_name:
                continue

            arguments = function.get("arguments")
            if arguments is None:
                arguments = "{}"
            elif not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)

            normalized_tool_calls.append({
                "id": tool_call.get("id") or f"call_{index}",
                "type": tool_call.get("type") or "function",
                "function": {
                    "name": function_name,
                    "arguments": arguments,
                },
            })

        return normalized_tool_calls

    def _prepare_messages(self, messages: List[dict]) -> List[dict]:
        prepared: List[Dict[str, Any]] = []

        for original_turn in copy.deepcopy(messages):
            role = original_turn.get("role")
            turn: Dict[str, Any] = {"role": role}

            if role == "assistant":
                normalized_tool_calls = self._normalize_assistant_tool_calls(
                    original_turn.get("tool_calls") or []
                )
                assistant_content = self._stringify_message_field(
                    original_turn.get("content")
                )
                assistant_reasoning = (
                    original_turn.get("reasoning")
                    or original_turn.get("reasoning_content")
                )
                if assistant_content:
                    turn["content"] = assistant_content
                elif normalized_tool_calls:
                    turn["content"] = None
                else:
                    turn["content"] = ""

                if normalized_tool_calls:
                    turn["tool_calls"] = normalized_tool_calls
                # NB: the model's own reasoning is deliberately NOT replayed. It
                # is not needed to continue the turn, and strict OpenAI-compatible
                # schemas reject an extra `reasoning` field on input messages
                # ("Extra inputs are not permitted"). vLLM tolerates its absence.
                _ = assistant_reasoning
            else:
                if role == "tool":
                    # Tool results are always text.
                    turn["content"] = self._stringify_message_field(
                        original_turn.get("content")
                    )
                    turn["tool_call_id"] = original_turn.get("tool_call_id")
                else:
                    # User / system: preserve multimodal list content.
                    turn["content"] = self._normalize_user_content(
                        original_turn.get("content"),
                        strip_images=self.strip_images,
                    )

            prepared.append(turn)

        return prepared

    def _prepare_messages_for_clinseek_sft(
        self, messages: List[dict]
    ) -> List[Dict[str, Any]]:
        """Render messages for ClinSeek SFT models.

        Assistant tool_calls are rendered as [Tool Call: name(args)] inline text.
        Tool results are rendered as plain text in user turns.
        No structured tool_calls field is used.
        """
        prepared: List[Dict[str, Any]] = []

        i = 0
        while i < len(messages):
            original_turn = messages[i]
            role = original_turn.get("role")

            if role == "assistant":
                parts: List[str] = []
                content = self._stringify_message_field(original_turn.get("content"))
                if content:
                    parts.append(content)

                tool_calls = original_turn.get("tool_calls") or []
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    arguments = func.get("arguments", "{}")
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    parts.append(f"[Tool Call: {name}({arguments})]")

                turn: Dict[str, Any] = {"role": "assistant", "content": "\n".join(parts)}
                reasoning = (
                    original_turn.get("reasoning")
                    or original_turn.get("reasoning_content")
                )
                if reasoning:
                    turn["reasoning"] = self._stringify_message_field(reasoning)
                prepared.append(turn)
                i += 1

            elif role == "tool":
                tool_results: List[str] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    result_content = self._stringify_message_field(
                        messages[i].get("content")
                    )
                    tool_results.append(result_content)
                    i += 1
                prepared.append({
                    "role": "user",
                    "content": "\n\n".join(tool_results),
                })

            else:
                turn = {
                    "role": role,
                    "content": self._stringify_message_field(
                        original_turn.get("content")
                    ),
                }
                prepared.append(turn)
                i += 1

        return prepared

    def _build_extra_body(self) -> Optional[Dict[str, Any]]:
        if self.enable_thinking is None:
            return None
        if self._is_openai_reasoning_model(self.model_name or ""):
            # The OpenAI API rejects unknown body fields; its thinking switch
            # is `reasoning_effort` (see _reasoning_kwargs).
            return None
        return {
            "chat_template_kwargs": {
                "enable_thinking": self.enable_thinking,
            }
        }

    def _make_client(self) -> OpenAI:
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.client_max_retries,
        )

    @staticmethod
    def _is_retryable_api_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 429, 500, 502, 503, 504, 529}:
            return True
        name = type(exc).__name__.lower()
        return any(
            marker in name
            for marker in (
                "timeout",
                "connection",
                "ratelimit",
                "internalserver",
            )
        )

    def _api_retry_delay(self, exc: Exception, failed_attempt: int) -> float:
        delay = min(
            self.retry_max_delay,
            self.retry_base_delay * (2 ** max(failed_attempt - 1, 0)),
        )
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            retry_after = headers.get("retry-after")
            if retry_after is not None:
                try:
                    delay = max(delay, float(retry_after))
                except (TypeError, ValueError):
                    pass
        jitter_cap = min(5.0, max(0.0, delay * 0.2))
        return delay + random.uniform(0.0, jitter_cap)

    async def _wait_for_api_gate(self) -> None:
        """Space request starts and honor a shared provider cooldown."""
        async with self._api_gate_lock:
            loop = asyncio.get_running_loop()
            while True:
                delay = self._next_api_attempt_at - loop.time()
                if delay <= 0:
                    break
                # Re-check frequently because another in-flight request can
                # extend the shared gate after this waiter starts sleeping.
                await asyncio.sleep(min(delay, 1.0))
            self._next_api_attempt_at = (
                loop.time() + max(0.0, self.min_request_interval)
            )

    def _extend_api_gate(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        self._next_api_attempt_at = max(
            self._next_api_attempt_at,
            loop.time() + max(0.0, delay),
        )

    async def _resolve_model_name(self) -> str:
        if self.model_name and self.model_name != "auto":
            return self.model_name

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self.executor,
            lambda: self._make_client().models.list(),
        )

        data = getattr(response, "data", None) or []
        if not data:
            raise RuntimeError(
                f"No served models reported by vLLM at {self.base_url}"
            )

        first_model = data[0]
        resolved = getattr(first_model, "id", None)
        if not resolved and isinstance(first_model, dict):
            resolved = first_model.get("id")
        if not resolved:
            raise RuntimeError(
                f"Unable to resolve served model ID from vLLM at {self.base_url}"
            )

        self.model_name = resolved
        print(f"[vLLM] Auto-resolved served model: {self.model_name}")
        return self.model_name

    @staticmethod
    def _extract_reasoning_content(message: Any) -> Optional[str]:
        reasoning = getattr(message, "reasoning", None)
        if reasoning:
            return reasoning

        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content:
            return reasoning_content

        model_extra = getattr(message, "model_extra", None) or {}
        for key in ("reasoning_content", "reasoning"):
            reasoning_content = model_extra.get(key)
            if reasoning_content:
                return reasoning_content

        return None

    @staticmethod
    def _try_fix_incomplete_json(json_str: str) -> str:
        if not json_str or not str(json_str).strip():
            return json_str

        value = str(json_str).strip()
        value = re.sub(r'"\s+"', '", "', value)
        value = re.sub(r'"\s*\[', '", [', value)
        value = re.sub(r'\]\s*"', '], "', value)
        value = re.sub(r'\}\s*\{', '}, {', value)
        value = re.sub(r',\s*([\]}])', r"\1", value)
        value = value.rstrip()

        missing_brackets = value.count("[") - value.count("]")
        missing_braces = value.count("{") - value.count("}")
        if missing_brackets > 0:
            value += "]" * missing_brackets
        if missing_braces > 0:
            value += "}" * missing_braces
        return value

    @staticmethod
    def _coerce_xml_argument(raw_value: str) -> Any:
        value = raw_value.strip()
        if not value:
            return ""

        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered == "null":
            return None

        should_try_json = (
            value.startswith("{")
            or value.startswith("[")
            or (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {'"', "'"}
            )
        )
        if should_try_json:
            try:
                return json.loads(value)
            except Exception:
                pass

        return value

    @staticmethod
    def _coerce_tool_call_arguments(raw_value: str) -> Any:
        value = raw_value.strip()
        if not value:
            return {}

        try:
            return json.loads(value)
        except Exception:
            return {"_raw": value}

    @classmethod
    def _parse_inline_function_call(
        cls,
        content: str,
        function_start: int,
    ) -> Optional[tuple[int, int, str, Any]]:
        if not content.startswith("<function=", function_start):
            return None

        segment_start = function_start
        prefix_start = function_start
        while prefix_start > 0 and content[prefix_start - 1].isspace():
            prefix_start -= 1

        tool_call_start = prefix_start - len("<tool_call>")
        if (
            tool_call_start >= 0
            and content[tool_call_start:prefix_start] == "<tool_call>"
        ):
            segment_start = tool_call_start

        name_start = function_start + len("<function=")
        open_paren: Optional[int] = None
        idx = name_start
        while idx < len(content):
            char = content[idx]
            if char == ">":
                # Proper XML function tags are handled by the existing XML parsers.
                return None
            if char == "(":
                open_paren = idx
                break
            if char in {"\r", "\n"}:
                return None
            idx += 1

        if open_paren is None:
            return None

        function_name = content[name_start:open_paren].strip()
        if not function_name:
            return None

        idx = open_paren + 1
        depth = 1
        in_string: Optional[str] = None
        escaped = False

        while idx < len(content):
            char = content[idx]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == in_string:
                    in_string = None
            else:
                if char in {'"', "'"}:
                    in_string = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
            idx += 1

        if depth != 0 or idx >= len(content):
            return None

        args_raw = content[open_paren + 1:idx]
        arguments = cls._coerce_tool_call_arguments(args_raw)

        end_idx = idx + 1
        while end_idx < len(content) and content[end_idx] in {" ", "\t", "]", "}"}:
            end_idx += 1

        while True:
            next_idx = end_idx
            while next_idx < len(content) and content[next_idx].isspace():
                next_idx += 1

            matched = False
            for closing_tag in ("</parameter>", "</function>", "</tool_call>"):
                if content.startswith(closing_tag, next_idx):
                    end_idx = next_idx + len(closing_tag)
                    matched = True
                    break

            if not matched:
                break

        return segment_start, end_idx, function_name, arguments

    @classmethod
    def _extract_inline_function_calls(
        cls,
        content: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        if "<function=" not in content:
            return content, []

        tool_calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        cursor = 0
        search_start = 0

        while True:
            function_start = content.find("<function=", search_start)
            if function_start == -1:
                break

            parsed = cls._parse_inline_function_call(content, function_start)
            if parsed is None:
                search_start = function_start + len("<function=")
                continue

            segment_start, end_idx, function_name, arguments = parsed

            if segment_start > cursor:
                text_parts.append(content[cursor:segment_start])

            tool_calls.append({
                "id": f"call_inline_{len(tool_calls) + 1}",
                "type": "function",
                "function": {
                    "name": cls._normalize_tool_name(function_name),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })
            cursor = end_idx
            search_start = end_idx

        if not tool_calls:
            return content, []

        if cursor < len(content):
            text_parts.append(content[cursor:])

        cleaned_content = "".join(text_parts).strip()
        return cleaned_content, tool_calls

    @classmethod
    def _extract_bracket_tool_calls(
        cls,
        content: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        if BRACKET_TOOL_CALL_PREFIX not in content:
            return content, []

        tool_calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        cursor = 0

        while True:
            start = content.find(BRACKET_TOOL_CALL_PREFIX, cursor)
            if start == -1:
                break

            if start > cursor:
                text_parts.append(content[cursor:start])

            name_start = start + len(BRACKET_TOOL_CALL_PREFIX)
            while name_start < len(content) and content[name_start].isspace():
                name_start += 1

            open_paren = content.find("(", name_start)
            if open_paren == -1:
                text_parts.append(content[start:])
                cursor = len(content)
                break

            function_name = content[name_start:open_paren].strip()
            if not function_name:
                text_parts.append(content[start:open_paren + 1])
                cursor = open_paren + 1
                continue

            idx = open_paren + 1
            depth = 1
            in_string: Optional[str] = None
            escaped = False

            while idx < len(content):
                char = content[idx]
                if in_string is not None:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == in_string:
                        in_string = None
                else:
                    if char in {'"', "'"}:
                        in_string = char
                    elif char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth == 0:
                            break
                idx += 1

            if depth != 0 or idx >= len(content):
                text_parts.append(content[start:])
                cursor = len(content)
                break

            args_raw = content[open_paren + 1:idx]
            closing_bracket = idx + 1
            while closing_bracket < len(content) and content[closing_bracket] in {" ", "\t"}:
                closing_bracket += 1

            end_idx: Optional[int] = None
            if closing_bracket < len(content) and content[closing_bracket] == "]":
                end_idx = closing_bracket + 1
            elif content.startswith("</tool_call>", closing_bracket):
                end_idx = closing_bracket + len("</tool_call>")
            elif closing_bracket >= len(content):
                end_idx = len(content)
            elif content[closing_bracket] in {"\r", "\n"}:
                end_idx = closing_bracket
            elif content.startswith(BRACKET_TOOL_CALL_PREFIX, closing_bracket):
                end_idx = closing_bracket

            if end_idx is None:
                text_parts.append(content[start:idx + 1])
                cursor = idx + 1
                continue

            arguments = cls._coerce_tool_call_arguments(args_raw)
            tool_calls.append({
                "id": f"call_bracket_{len(tool_calls) + 1}",
                "type": "function",
                "function": {
                    "name": cls._normalize_tool_name(function_name),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })
            cursor = end_idx

        if not tool_calls:
            return content, []

        if cursor < len(content):
            text_parts.append(content[cursor:])

        cleaned_content = "".join(text_parts).strip()
        return cleaned_content, tool_calls

    @classmethod
    def _extract_xml_tool_calls(
        cls,
        content: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        if "<tool_call>" not in content or "</tool_call>" not in content:
            return content, []

        tool_calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        cursor = 0

        for match in TOOL_CALL_BLOCK_RE.finditer(content):
            start, end = match.span()
            raw_block = match.group(1).strip()

            function_name: Optional[str] = None
            arguments: Dict[str, Any] | None = None

            xml_match = XML_FUNCTION_CALL_RE.fullmatch(raw_block)
            if xml_match:
                function_name = xml_match.group(1).strip()
                function_body = xml_match.group(2)
                arguments = {}
                for param_match in XML_PARAMETER_RE.finditer(function_body):
                    param_name = param_match.group(1).strip()
                    param_value = cls._coerce_xml_argument(param_match.group(2))
                    arguments[param_name] = param_value
            else:
                try:
                    payload = json.loads(raw_block)
                except json.JSONDecodeError:
                    payload = None

                if isinstance(payload, dict):
                    maybe_name = payload.get("name")
                    if isinstance(maybe_name, str) and maybe_name.strip():
                        function_name = maybe_name.strip()
                        raw_arguments = payload.get("arguments")
                        if isinstance(raw_arguments, dict):
                            arguments = raw_arguments
                        elif raw_arguments is None:
                            arguments = {}
                        else:
                            arguments = {"_raw": raw_arguments}

            if function_name is None or arguments is None:
                continue

            if start > cursor:
                text_parts.append(content[cursor:start])

            tool_calls.append({
                "id": f"call_xml_{len(tool_calls) + 1}",
                "type": "function",
                "function": {
                    "name": cls._normalize_tool_name(function_name),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })
            cursor = end

        if not tool_calls:
            return content, []

        if cursor < len(content):
            text_parts.append(content[cursor:])

        cleaned_content = "".join(text_parts).strip()
        return cleaned_content, tool_calls

    @classmethod
    def _extract_naked_xml_function_calls(
        cls,
        content: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        if "<function=" not in content or "</function>" not in content:
            return content, []

        tool_calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        cursor = 0

        for match in XML_FUNCTION_CALL_RE.finditer(content):
            start, end = match.span()
            function_name = match.group(1).strip()
            function_body = match.group(2)
            arguments: Dict[str, Any] = {}

            for param_match in XML_PARAMETER_RE.finditer(function_body):
                param_name = param_match.group(1).strip()
                param_value = cls._coerce_xml_argument(param_match.group(2))
                arguments[param_name] = param_value

            if start > cursor:
                text_parts.append(content[cursor:start])

            tool_calls.append({
                "id": f"call_xml_{len(tool_calls) + 1}",
                "type": "function",
                "function": {
                    "name": cls._normalize_tool_name(function_name),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })
            cursor = end

        if not tool_calls:
            return content, []

        if cursor < len(content):
            text_parts.append(content[cursor:])

        cleaned_content = "".join(text_parts).strip()
        return cleaned_content, tool_calls

    @staticmethod
    def _looks_like_tool_name(function_name: str) -> bool:
        name = (function_name or "").strip()
        if not name:
            return False
        if "." in name:
            return True
        if name in {"search", "open", "find"}:
            return True
        if name.startswith("browser_") or name.startswith("ehr_"):
            return True
        return False

    @classmethod
    def _extract_json_tool_calls(
        cls,
        content: str,
    ) -> tuple[str, List[Dict[str, Any]]]:
        if '"name"' not in content or '"arguments"' not in content or "{" not in content:
            return content, []

        decoder = json.JSONDecoder()
        tool_calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        cursor = 0
        search_start = 0

        while True:
            object_start = content.find("{", search_start)
            if object_start == -1:
                break

            try:
                payload, object_end = decoder.raw_decode(content, object_start)
            except json.JSONDecodeError:
                search_start = object_start + 1
                continue

            if not isinstance(payload, dict):
                search_start = object_start + 1
                continue

            function_name = payload.get("name")
            if not isinstance(function_name, str) or not cls._looks_like_tool_name(function_name):
                search_start = object_start + 1
                continue

            raw_arguments = payload.get("arguments")
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            elif raw_arguments is None:
                arguments = {}
            else:
                arguments = {"_raw": raw_arguments}

            next_idx = object_end
            while next_idx < len(content) and content[next_idx].isspace():
                next_idx += 1

            if not content.startswith("</tool_call>", next_idx):
                search_start = object_start + 1
                continue

            segment_end = next_idx + len("</tool_call>")
            while True:
                trailing_idx = segment_end
                while trailing_idx < len(content) and content[trailing_idx].isspace():
                    trailing_idx += 1

                matched = False
                for tag in ("</think>", "<think>", "<tool_call>"):
                    if content.startswith(tag, trailing_idx):
                        segment_end = trailing_idx + len(tag)
                        matched = True
                        break

                if not matched:
                    break

            if object_start > cursor:
                text_parts.append(content[cursor:object_start])

            tool_calls.append({
                "id": f"call_json_{len(tool_calls) + 1}",
                "type": "function",
                "function": {
                    "name": cls._normalize_tool_name(function_name),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })

            cursor = segment_end
            search_start = segment_end

        if not tool_calls:
            return content, []

        if cursor < len(content):
            text_parts.append(content[cursor:])

        cleaned_content = "".join(text_parts).strip()
        return cleaned_content, tool_calls

    @staticmethod
    def _dump_raw_response(response: Any) -> Any:
        """The provider payload as plain JSON-able data, best effort."""
        for dump in (
            lambda: response.model_dump(mode="json"),
            lambda: response.model_dump(),
            lambda: response.to_dict(),
        ):
            try:
                return dump()
            except Exception:
                continue
        return {"repr": repr(response)}

    @staticmethod
    def _convert_response_to_openai(
        response: Any,
        model_name: Optional[str] = None,
        capture_raw: bool = False,
    ) -> Dict[str, Any]:
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        output_message: Dict[str, Any] = {
            "role": "assistant",
            "content": content,
        }

        reasoning_content = VLLMOpenAIAsyncGenerator._extract_reasoning_content(message)
        if reasoning_content:
            output_message["reasoning_content"] = reasoning_content

        tool_calls = []
        for tool_call in getattr(message, "tool_calls", None) or []:
            arguments = tool_call.function.arguments
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)

            tool_calls.append({
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": VLLMOpenAIAsyncGenerator._normalize_tool_name(
                        tool_call.function.name
                    ),
                    "arguments": arguments,
                },
            })

        is_clinseek_sft = VLLMOpenAIAsyncGenerator._is_clinseek_sft_model(model_name)

        if tool_calls:
            output_message["tool_calls"] = tool_calls
        elif is_clinseek_sft:
            if content:
                fallback_content, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_bracket_tool_calls(content)
                )
                if fallback_tool_calls:
                    output_message["content"] = fallback_content
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and reasoning_content:
                cleaned_reasoning, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_bracket_tool_calls(
                        reasoning_content
                    )
                )
                if fallback_tool_calls:
                    if cleaned_reasoning != reasoning_content:
                        output_message["reasoning_content"] = cleaned_reasoning
                    output_message["tool_calls"] = fallback_tool_calls
        else:
            fallback_content, fallback_tool_calls = (
                VLLMOpenAIAsyncGenerator._extract_xml_tool_calls(content)
            )
            if "tool_calls" not in output_message and fallback_tool_calls:
                output_message["content"] = fallback_content
                output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and content:
                fallback_content, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_naked_xml_function_calls(
                        content
                    )
                )
                if fallback_tool_calls:
                    output_message["content"] = fallback_content
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and content:
                fallback_content, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_inline_function_calls(
                        content
                    )
                )
                if fallback_tool_calls:
                    output_message["content"] = fallback_content
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and content:
                fallback_content, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_json_tool_calls(content)
                )
                if fallback_tool_calls:
                    output_message["content"] = fallback_content
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and reasoning_content:
                cleaned_reasoning, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_xml_tool_calls(
                        reasoning_content
                    )
                )
                if cleaned_reasoning != reasoning_content:
                    output_message["reasoning_content"] = cleaned_reasoning
                if fallback_tool_calls:
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and reasoning_content:
                cleaned_reasoning, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_naked_xml_function_calls(
                        reasoning_content
                    )
                )
                if cleaned_reasoning != reasoning_content:
                    output_message["reasoning_content"] = cleaned_reasoning
                if fallback_tool_calls:
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and reasoning_content:
                cleaned_reasoning, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_inline_function_calls(
                        reasoning_content
                    )
                )
                if cleaned_reasoning != reasoning_content:
                    output_message["reasoning_content"] = cleaned_reasoning
                if fallback_tool_calls:
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and reasoning_content:
                cleaned_reasoning, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_json_tool_calls(
                        reasoning_content
                    )
                )
                if cleaned_reasoning != reasoning_content:
                    output_message["reasoning_content"] = cleaned_reasoning
                if fallback_tool_calls:
                    output_message["tool_calls"] = fallback_tool_calls
            if "tool_calls" not in output_message and content:
                fallback_content, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_bracket_tool_calls(content)
                )
                if fallback_tool_calls:
                    output_message["content"] = fallback_content
                    output_message["tool_calls"] = fallback_tool_calls

            if "tool_calls" not in output_message and reasoning_content:
                cleaned_reasoning, fallback_tool_calls = (
                    VLLMOpenAIAsyncGenerator._extract_bracket_tool_calls(
                        reasoning_content
                    )
                )
                if cleaned_reasoning != reasoning_content:
                    output_message["reasoning_content"] = cleaned_reasoning
                if fallback_tool_calls:
                    output_message["tool_calls"] = fallback_tool_calls

        finish_reason = choice.finish_reason
        if output_message.get("tool_calls") and finish_reason != "tool_calls":
            finish_reason = "tool_calls"

        usage = getattr(response, "usage", None)
        usage_dict = {}
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", None) if details else None
            if cached is not None:
                usage_dict["prompt_tokens_details"] = {"cached_tokens": cached}

        if capture_raw:
            output_message["raw_response"] = (
                VLLMOpenAIAsyncGenerator._dump_raw_response(response)
            )

        return {
            "choices": [{
                "message": output_message,
                "finish_reason": finish_reason,
            }],
            "usage": usage_dict,
        }

    async def chat_completion(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: float = 1.0,
        max_tokens: Optional[int] = None,
        use_reasoning_content: bool = True,
    ) -> Dict[str, Any]:
        await self._init_tokenizer()
        model_name = await self._resolve_model_name()

        is_clinseek_sft = self._is_clinseek_sft_model(model_name)

        if is_clinseek_sft:
            prepared_messages = self._prepare_messages_for_clinseek_sft(messages)
        else:
            prepared_messages = self._prepare_messages(messages)
        extra_body = self._build_extra_body()

        api_tools = None if is_clinseek_sft else self._sanitize_tools_for_api(tools)
        api_tool_choice = (
            self._sanitize_tool_choice_for_api(tool_choice)
            if api_tools
            else None
        )

        print(
            f"[vLLM] Request: model={model_name}, messages={len(prepared_messages)}, "
            f"tools={len(api_tools) if api_tools else 0}"
        )

        loop = asyncio.get_event_loop()
        # with_raw_response so per-call cost headers can be read when an
        # OpenAI-compatible proxy reports them (see _accumulate_cost_headers).
        raw = None
        for attempt in range(1, self.request_max_attempts + 1):
            await self._wait_for_api_gate()
            try:
                raw = await loop.run_in_executor(
                    self.executor,
                    lambda: self._make_client().chat.completions.with_raw_response.create(
                        model=model_name,
                        messages=prepared_messages,
                        tools=api_tools,
                        tool_choice=api_tool_choice,
                        extra_body=extra_body,
                        stream=False,
                        **self._max_token_kwargs(model_name, max_tokens),
                        **self._reasoning_kwargs(model_name),
                        **self._sampling_kwargs(model_name, temperature),
                    )
                )
                break
            except Exception as exc:
                if (
                    attempt >= self.request_max_attempts
                    or not self._is_retryable_api_error(exc)
                ):
                    raise
                delay = self._api_retry_delay(exc, attempt)
                self._extend_api_gate(delay)
                status = getattr(exc, "status_code", type(exc).__name__)
                print(
                    f"[{self._LOG_PREFIX}] transient API error {status}; "
                    f"retry {attempt + 1}/{self.request_max_attempts} "
                    f"in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        if raw is None:
            raise RuntimeError("API request retry loop ended without a response")
        self._accumulate_cost_headers(raw.headers)
        response = raw.parse()
        converted = self._convert_response_to_openai(
            response,
            model_name=model_name,
            capture_raw=self.capture_raw_response,
        )
        self._accumulate_usage(converted.get("usage") or {})
        if not use_reasoning_content:
            converted["choices"][0]["message"].pop("reasoning_content", None)

        print(
            "[vLLM] Response received: "
            f"finish_reason={converted['choices'][0]['finish_reason']}"
        )
        return converted

    def shutdown(self) -> None:
        try:
            self.executor.shutdown(wait=False)
        except Exception:
            pass

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass
