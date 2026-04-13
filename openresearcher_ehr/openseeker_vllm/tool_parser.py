import json
import re
from collections.abc import Sequence
from typing import Any

from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest
from vllm.entrypoints.openai.engine.protocol import (
    DeltaMessage,
    ExtractedToolCallInformation,
    FunctionCall,
    ToolCall,
)
from vllm.logger import init_logger
from vllm.tokenizers import TokenizerLike
from vllm.tool_parsers import ToolParser, ToolParserManager

logger = init_logger(__name__)

_TOOL_CALLS_BLOCK_RE = re.compile(
    r"<tool_calls_begin>\s*(.*?)\s*</tool_calls_end>",
    re.DOTALL,
)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _normalize_tool_name(function_name: str) -> str:
    name = (function_name or "").strip()
    if not name:
        return name

    if name.startswith("browser."):
        return name
    if name.startswith("browser_"):
        suffix = name[len("browser_") :].strip("_")
        return f"browser.{suffix}" if suffix else "browser.search"
    if name in {"search", "open", "find"}:
        return f"browser.{name}"

    if name.startswith("ehr.") or name.startswith("ehr_"):
        return name
    if "." not in name:
        return f"ehr_{name}"
    return name


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


def _load_tool_payload(payload_text: str) -> Any:
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError:
        return json.loads(_try_fix_incomplete_json(payload_text))


class OpenSeekerToolParser(ToolParser):
    def __init__(self, tokenizer: TokenizerLike):
        super().__init__(tokenizer)
        logger.info("Loaded OpenSeeker tool parser")

    def extract_tool_calls(
        self,
        model_output: str,
        request: ChatCompletionRequest,
    ) -> ExtractedToolCallInformation:
        text = model_output or ""
        blocks = [m.group(1) for m in _TOOL_CALLS_BLOCK_RE.finditer(text)]
        if not blocks:
            return ExtractedToolCallInformation(
                tools_called=False,
                tool_calls=[],
                content=text,
            )

        tool_calls: list[ToolCall] = []

        for chunk in blocks:
            for match in _TOOL_CALL_RE.finditer(chunk):
                inner = (match.group(1) or "").strip()
                if not inner:
                    continue

                try:
                    payload = _load_tool_payload(inner)
                except Exception:
                    continue

                payload_items = payload if isinstance(payload, list) else [payload]
                for item in payload_items:
                    if not isinstance(item, dict):
                        continue

                    function_name = item.get("tool_name") or item.get("name")
                    if not isinstance(function_name, str) or not function_name.strip():
                        continue

                    arguments = item.get("tool_args")
                    if arguments is None:
                        arguments = item.get("arguments")

                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except Exception:
                            arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}

                    tool_calls.append(
                        ToolCall(
                            function=FunctionCall(
                                name=_normalize_tool_name(function_name),
                                arguments=json.dumps(arguments, ensure_ascii=False),
                            )
                        )
                    )

        if not tool_calls:
            return ExtractedToolCallInformation(
                tools_called=False,
                tool_calls=[],
                content=text,
            )

        cleaned = _TOOL_CALLS_BLOCK_RE.sub("", text)
        cleaned = _TOOL_CALL_RE.sub("", cleaned).strip()
        return ExtractedToolCallInformation(
            tools_called=True,
            tool_calls=tool_calls,
            content=cleaned,
        )

    def extract_tool_calls_streaming(
        self,
        previous_text: str,
        current_text: str,
        delta_text: str,
        previous_token_ids: Sequence[int],
        current_token_ids: Sequence[int],
        delta_token_ids: Sequence[int],
        request: ChatCompletionRequest,
    ) -> DeltaMessage | None:
        if not delta_text:
            return None
        return DeltaMessage(content=delta_text)


ToolParserManager.register_module(
    name=["openseeker", "openseeker_xml"],
    module=OpenSeekerToolParser,
)
