"""Single-call model invokers for the Curated Input / Full-Record runners.

`run_text_curated.py` and `run_multimodal_curated.py` send one prompt per row
(optionally with attached CXR images) and parse the reply; there is no tool
loop. Every invoker here exposes the same synchronous contract, so the two
runners' thread-pool / shard plumbing is backend-agnostic:

    invoker.invoke(region, model_id, prompt_text, images=None) -> (text, status)

where `images` is a list of `(base64_data, media_type)` pairs and `status` is
"ok" or "error" (on error, `text` carries the exception).

Reasoning settings follow the agentic backends so the four inference modes run
the same model configuration (paper: extended thinking with a 4,096-token
budget for Claude models that accept a fixed budget, `effort=high` for
adaptive-only Claude models and OpenAI reasoning models, temperature 0 where
accepted, 8,192 output tokens):

* Claude (Bedrock / Vertex): `bedrock_backend.requires_adaptive_thinking` and
  `rejects_sampling_params` decide between a fixed budget and adaptive thinking
  with `output_config.effort`, and whether a temperature may be sent.
* OpenAI-compatible endpoints (`--backend vllm`): vLLM chat templates take the
  thinking switch through `chat_template_kwargs`; OpenAI reasoning models take
  `reasoning_effort` and `max_completion_tokens`.
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bedrock_backend import rejects_sampling_params, requires_adaptive_thinking
from vllm_backend import VLLMOpenAIAsyncGenerator

Images = Optional[Sequence[Tuple[str, str]]]

RETRYABLE_KEYWORDS = (
    "timeout",
    "timed out",
    "throttl",
    "too many requests",
    "rate limit",
    "service unavailable",
    "internal server error",
    "internalserver",
    "unexpected error",
    "overloaded",
    "busy",
    "connection",
)


def claude_request_fields(
    model_id: str,
    max_tokens: int,
    temperature: float,
    enable_thinking: bool,
    thinking_budget: int,
    reasoning_effort: Optional[str],
) -> Dict[str, Any]:
    """`max_tokens` / `thinking` / `output_config` / `temperature` for Claude."""
    fields: Dict[str, Any] = {"max_tokens": max_tokens}
    if enable_thinking:
        if requires_adaptive_thinking(model_id):
            fields["thinking"] = {"type": "adaptive"}
            if reasoning_effort:
                fields["output_config"] = {"effort": reasoning_effort}
        else:
            budget = min(int(thinking_budget), max_tokens - 1)
            if budget < 1024:
                raise ValueError(
                    "Claude extended thinking needs max_tokens > 1024 so a "
                    "thinking budget of at least 1024 tokens fits.")
            fields["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Claude does not accept a sampling temperature with extended thinking.
    elif not rejects_sampling_params(model_id):
        fields["temperature"] = temperature
    return fields


def claude_content(prompt_text: str, images: Images) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for data, media_type in images or ():
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": data}})
    return content


def openai_content(prompt_text: str, images: Images) -> Any:
    """Plain string, or OpenAI-style multipart content with data-URL images."""
    if not images:
        return prompt_text
    parts: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for data, media_type in images:
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{media_type};base64,{data}"}})
    return parts


class _UsageCounter:
    """Thread-safe running token totals, written to the run's usage.json."""

    def __init__(self) -> None:
        self.token_usage = {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
        self._usage_lock = threading.Lock()

    def _add_usage(self, mapping: Dict[str, Any]) -> None:
        with self._usage_lock:
            self.token_usage["requests"] += 1
            for key in ("input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens"):
                value = mapping.get(key)
                if isinstance(value, int):
                    self.token_usage[key] += value

    @staticmethod
    def _backoff(attempt: int) -> None:
        # Jitter so workers that failed together do not retry together.
        time.sleep(min(2 ** attempt, 30) * (0.5 + random.random()))


class BedrockInvoker(_UsageCounter):
    """AWS Bedrock: Anthropic Messages API for Claude, OpenAI shape otherwise."""

    def __init__(
        self,
        max_tokens: int,
        temperature: float,
        max_retries: int,
        read_timeout: int = 180,
        enable_thinking: bool = True,
        thinking_budget: int = 4096,
        reasoning_effort: Optional[str] = "high",
    ):
        super().__init__()
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError

        self._boto3 = boto3
        self._BotoCoreError = BotoCoreError
        self._ClientError = ClientError
        self._boto_config = BotoConfig(
            read_timeout=read_timeout, connect_timeout=10,
            retries={"max_attempts": 0})
        self._clients: Dict[str, Any] = {}
        self._clients_lock = threading.Lock()
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.enable_thinking = enable_thinking
        self.thinking_budget = thinking_budget
        self.reasoning_effort = reasoning_effort

    def _client(self, region: str):
        with self._clients_lock:
            client = self._clients.get(region)
            if client is None:
                client = self._boto3.client(
                    "bedrock-runtime", region_name=region,
                    config=self._boto_config)
                self._clients[region] = client
            return client

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, self._ClientError):
            code = (exc.response or {}).get("Error", {}).get("Code", "") or ""
            if code in {
                "InternalServerException", "ModelNotReadyException",
                "RequestTimeoutException", "ServiceUnavailableException",
                "ThrottlingException", "TooManyRequestsException",
            }:
                return True
        if isinstance(exc, self._BotoCoreError):
            return True
        return any(kw in str(exc).lower() for kw in RETRYABLE_KEYWORDS)

    @staticmethod
    def _is_anthropic(model_id: str) -> bool:
        return "anthropic" in model_id.lower()

    @staticmethod
    def _supports_images(model_id: str) -> bool:
        """Anthropic and Qwen-VL accept image blocks on Bedrock; other
        OpenAI-shape models get a text marker per image instead."""
        name = model_id.lower()
        return "anthropic" in name or "qwen3-vl" in name

    def _invoke_anthropic(self, client, model_id: str, prompt_text: str,
                          images: Images) -> str:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            **claude_request_fields(
                model_id, self.max_tokens, self.temperature,
                self.enable_thinking, self.thinking_budget,
                self.reasoning_effort),
            "messages": [{"role": "user",
                          "content": claude_content(prompt_text, images)}],
        }
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        self._add_usage(out.get("usage") or {})
        return "\n".join(
            blk.get("text", "") for blk in (out.get("content") or [])
            if blk.get("type") == "text").strip()

    def _invoke_openai(self, client, model_id: str, prompt_text: str,
                       images: Images) -> str:
        if images and not self._supports_images(model_id):
            markers = "\n".join(
                f"[image attached ({mt}) - not inlined for this model]"
                for _, mt in images)
            content: Any = prompt_text + "\n\n" + markers
        else:
            content = openai_content(prompt_text, images)
        body = {
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": content}],
        }
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        usage = out.get("usage") or {}
        self._add_usage({"input_tokens": usage.get("prompt_tokens"),
                         "output_tokens": usage.get("completion_tokens")})
        try:
            msg_content = out["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            return json.dumps(out)
        if isinstance(msg_content, list):
            return "\n".join(p.get("text", "") for p in msg_content
                             if isinstance(p, dict)).strip()
        return (msg_content or "").strip()

    def invoke(self, region: str, model_id: str, prompt_text: str,
               images: Images = None) -> Tuple[str, str]:
        client = self._client(region)
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                if self._is_anthropic(model_id):
                    return self._invoke_anthropic(
                        client, model_id, prompt_text, images), "ok"
                return self._invoke_openai(
                    client, model_id, prompt_text, images), "ok"
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                self._backoff(attempt)
        return f"[ERROR] {type(last_err).__name__}: {last_err}", "error"


class VertexInvoker(_UsageCounter):
    """Claude on Google Vertex AI (Application Default Credentials).

    `region` is fixed at construction; the per-call value is a label kept so
    the shard plumbing matches the Bedrock path.
    """

    def __init__(
        self,
        project_id: str,
        region: str,
        max_tokens: int,
        temperature: float,
        max_retries: int,
        read_timeout: int = 180,
        enable_thinking: bool = True,
        thinking_budget: int = 4096,
        reasoning_effort: Optional[str] = "high",
    ):
        super().__init__()
        from anthropic import AnthropicVertex

        self.client = AnthropicVertex(
            project_id=project_id, region=region,
            timeout=float(read_timeout), max_retries=0)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.enable_thinking = enable_thinking
        self.thinking_budget = thinking_budget
        self.reasoning_effort = reasoning_effort

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if getattr(exc, "status_code", None) in (408, 409, 429, 500, 502, 503, 504, 529):
            return True
        return any(kw in str(exc).lower() for kw in RETRYABLE_KEYWORDS)

    def invoke(self, region: str, model_id: str, prompt_text: str,
               images: Images = None) -> Tuple[str, str]:
        payload = {
            **claude_request_fields(
                model_id, self.max_tokens, self.temperature,
                self.enable_thinking, self.thinking_budget,
                self.reasoning_effort),
            "messages": [{"role": "user",
                          "content": claude_content(prompt_text, images)}],
        }
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                msg = self.client.messages.create(model=model_id, **payload)
                usage = getattr(msg, "usage", None)
                if usage is not None:
                    self._add_usage({
                        key: getattr(usage, key, None) for key in (
                            "input_tokens", "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens")})
                return "\n".join(
                    b.text for b in msg.content
                    if getattr(b, "type", "") == "text").strip(), "ok"
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                self._backoff(attempt)
        return f"[ERROR] {type(last_err).__name__}: {last_err}", "error"


class OpenAICompatInvoker(_UsageCounter):
    """Any OpenAI-compatible chat-completions endpoint (vLLM, OpenAI, proxies).

    Request shaping matches `vllm_backend.VLLMOpenAIAsyncGenerator`, the agentic
    `--backend vllm` path, so the record and agentic modes send the same
    reasoning settings to the same model.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        max_tokens: int,
        temperature: float,
        max_retries: int,
        read_timeout: int = 1800,
        enable_thinking: Optional[bool] = True,
        reasoning_effort: Optional[str] = "high",
        min_request_interval: float = 0.0,
    ):
        super().__init__()
        from openai import OpenAI

        self.base_url = VLLMOpenAIAsyncGenerator._normalize_base_url(base_url)
        self.client = OpenAI(base_url=self.base_url, api_key=api_key or "EMPTY",
                             timeout=float(read_timeout), max_retries=0)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.enable_thinking = enable_thinking
        self.reasoning_effort = reasoning_effort
        self.min_request_interval = max(0.0, float(min_request_interval))
        self._gate_lock = threading.Lock()
        self._next_allowed_at = 0.0

    def _wait_for_gate(self) -> None:
        """Space request starts across threads (rate-limited endpoints)."""
        if self.min_request_interval <= 0:
            return
        with self._gate_lock:
            now = time.monotonic()
            start = max(now, self._next_allowed_at)
            self._next_allowed_at = start + self.min_request_interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _request_kwargs(self, model_id: str) -> Dict[str, Any]:
        is_openai = VLLMOpenAIAsyncGenerator._is_openai_reasoning_model(model_id)
        kwargs: Dict[str, Any] = {}
        if is_openai:
            kwargs["max_completion_tokens"] = self.max_tokens
            if self.enable_thinking and self.reasoning_effort:
                kwargs["reasoning_effort"] = self.reasoning_effort
        else:
            kwargs["max_tokens"] = self.max_tokens
            if not any(tag in model_id
                       for tag in VLLMOpenAIAsyncGenerator._SAMPLING_REMOVED):
                kwargs["temperature"] = self.temperature
            if self.enable_thinking is not None:
                kwargs["extra_body"] = {"chat_template_kwargs": {
                    "enable_thinking": bool(self.enable_thinking)}}
        return kwargs

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if getattr(exc, "status_code", None) in (408, 409, 429, 500, 502, 503, 504, 529):
            return True
        return any(kw in str(exc).lower() for kw in RETRYABLE_KEYWORDS)

    def invoke(self, region: str, model_id: str, prompt_text: str,
               images: Images = None) -> Tuple[str, str]:
        kwargs = {
            "model": model_id,
            "messages": [{"role": "user",
                          "content": openai_content(prompt_text, images)}],
            **self._request_kwargs(model_id),
        }
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                self._wait_for_gate()
                resp = self.client.chat.completions.create(**kwargs)
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    self._add_usage({
                        "input_tokens": getattr(usage, "prompt_tokens", None),
                        "output_tokens": getattr(usage, "completion_tokens", None)})
                message = resp.choices[0].message
                text = (getattr(message, "content", "") or "").strip()
                if not text:
                    # A reasoning-only reply (output budget spent in the
                    # thinking channel). Keep it so the row is auditable and
                    # scripts/force_final_answer.py can recover an answer.
                    text = (getattr(message, "reasoning_content", None)
                            or getattr(message, "reasoning", None) or "").strip()
                return text, "ok"
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                self._backoff(attempt)
        return f"[ERROR] {type(last_err).__name__}: {last_err}", "error"
