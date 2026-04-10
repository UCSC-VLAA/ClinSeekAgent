"""
OpenAI-compatible async generator for vLLM-backed local models.
"""
from __future__ import annotations

import asyncio
import copy
import json
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
    Returns the same minimal OpenAI-style dict shape used by deploy_agent.py.
    """

    def __init__(
        self,
        model_name: Optional[str],
        base_url: str = "http://127.0.0.1:4000",
        api_key: str = "EMPTY",
        max_tokens_default: int = 8192,
        max_workers: int = 10,
        enable_thinking: Optional[bool] = None,
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = api_key
        self.model_name = model_name
        self.max_tokens_default = max_tokens_default
        self.enable_thinking = enable_thinking
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        print(
            f"[vLLM] Initialized with base_url={self.base_url}, "
            f"model={self.model_name or 'auto'}"
        )

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        return normalized

    async def _init_tokenizer(self):
        """Compatibility no-op; deploy_agent checks for this method."""
        return None

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
    def _stringify_message_field(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @classmethod
    def _normalize_assistant_tool_calls(
        cls,
        tool_calls: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized_tool_calls: List[Dict[str, Any]] = []

        for index, tool_call in enumerate(tool_calls, start=1):
            function = tool_call.get("function", {})
            function_name = cls._normalize_tool_name(function.get("name", ""))
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
                if assistant_content:
                    turn["content"] = assistant_content
                elif normalized_tool_calls:
                    turn["content"] = None
                else:
                    turn["content"] = ""

                if normalized_tool_calls:
                    turn["tool_calls"] = normalized_tool_calls
            else:
                turn["content"] = self._stringify_message_field(
                    original_turn.get("content")
                )
                if role == "tool":
                    turn["tool_call_id"] = original_turn.get("tool_call_id")

            prepared.append(turn)

        return prepared

    def _build_extra_body(self) -> Optional[Dict[str, Any]]:
        if self.enable_thinking is None:
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
    def _convert_response_to_openai(response: Any) -> Dict[str, Any]:
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

        if tool_calls:
            output_message["tool_calls"] = tool_calls
        else:
            fallback_content, fallback_tool_calls = (
                VLLMOpenAIAsyncGenerator._extract_xml_tool_calls(content)
            )
            if fallback_tool_calls:
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

        usage = getattr(response, "usage", None)
        usage_dict = {}
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }

        return {
            "choices": [{
                "message": output_message,
                "finish_reason": choice.finish_reason,
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
        prepared_messages = self._prepare_messages(messages)
        extra_body = self._build_extra_body()

        print(
            f"[vLLM] Request: model={model_name}, messages={len(prepared_messages)}, "
            f"tools={len(tools) if tools else 0}"
        )

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self.executor,
            lambda: self._make_client().chat.completions.create(
                model=model_name,
                messages=prepared_messages,
                tools=tools,
                tool_choice=tool_choice if tools else None,
                max_tokens=max_tokens or self.max_tokens_default,
                temperature=temperature,
                extra_body=extra_body,
                stream=False,
            )
        )

        converted = self._convert_response_to_openai(response)
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
