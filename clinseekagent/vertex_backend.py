"""
Google Vertex AI Claude generator for agent inference
Uses the official Anthropic SDK's AsyncAnthropicVertex client

Both Bedrock and Vertex serve Claude through the Anthropic Messages API, so
message/tool conversion, thinking-block replay and the OpenAI-shaped response
adapter are inherited from BedrockAsyncGenerator. Only the transport, the
credentials and a handful of per-model parameter rules differ, and those are
the only things overridden here.
"""
from typing import Any, Optional
import asyncio
import os
import re

from anthropic import AsyncAnthropicVertex
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from bedrock_backend import (
    API_MAX_RETRIES,
    API_RETRY_DELAY_SECONDS,
    BedrockAsyncGenerator,
)


# Candidates tried in order when no model is pinned. Vertex only serves a
# subset of the Claude lineup per project/region, and an unavailable ID 404s at
# call time rather than at startup — so the first reachable candidate wins and
# is cached for the rest of the process.
VERTEX_MODEL_CANDIDATES = (
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)
DEFAULT_VERTEX_REGION = "us-east5"

# Sentinels meaning "pick one for me" rather than naming a model.
_AUTO_MODEL_TOKENS = {"", "auto", "default", "none"}

# Which models take adaptive-only thinking and which reject sampling params is
# shared with the Bedrock generator (bedrock_backend.requires_adaptive_thinking /
# rejects_sampling_params): both serve Claude through the same Messages API.

# Bedrock-style inference-profile prefixes that mean nothing on Vertex.
_BEDROCK_PREFIX_RE = re.compile(r"^(?:global|us|eu|apac)\.")
_BEDROCK_SUFFIX_RE = re.compile(r"-v\d+(?::\d+)?$")
_DATED_SNAPSHOT_RE = re.compile(r"^(?P<base>.+)-(?P<date>\d{8})$")


def is_auto_model(model_id: Optional[str]) -> bool:
    """True when the caller wants the backend to choose a model."""
    return (model_id or "").strip().lower() in _AUTO_MODEL_TOKENS


def resolve_vertex_model_id(model_id: str) -> str:
    """Normalize a model ID to the form Vertex expects.

    Vertex uses the bare first-party ID (``claude-opus-4-6``) with an ``@``
    separator for dated snapshots (``claude-opus-4-5@20251101``). Callers often
    arrive holding a Bedrock inference-profile ID because that is this repo's
    default, so translate the common shapes instead of 404-ing on them.
    """
    resolved = (model_id or "").strip()
    resolved = _BEDROCK_PREFIX_RE.sub("", resolved)
    if resolved.startswith("anthropic."):
        resolved = resolved[len("anthropic."):]
    resolved = _BEDROCK_SUFFIX_RE.sub("", resolved)

    if "@" not in resolved:
        dated = _DATED_SNAPSHOT_RE.match(resolved)
        if dated:
            resolved = f"{dated.group('base')}@{dated.group('date')}"

    if resolved != model_id:
        print(f"[Vertex] Normalized model ID {model_id!r} -> {resolved!r}")
    return resolved


def resolve_vertex_project_id(project_id: Optional[str]) -> str:
    resolved = (
        project_id
        or os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
    )
    if not resolved:
        raise ValueError(
            "Vertex AI requires a GCP project. Pass --vertex_project or set "
            "ANTHROPIC_VERTEX_PROJECT_ID (or GOOGLE_CLOUD_PROJECT)."
        )
    return resolved


def resolve_vertex_region(region: Optional[str]) -> str:
    return (
        region
        or os.getenv("ANTHROPIC_VERTEX_REGION")
        or os.getenv("CLOUD_ML_REGION")
        or DEFAULT_VERTEX_REGION
    )


class VertexAsyncGenerator(BedrockAsyncGenerator):
    """Async generator using Claude on Google Vertex AI.

    Interface-compatible with BedrockAsyncGenerator: ``_init_tokenizer()``,
    ``chat_completion()`` and assistant messages carrying
    ``bedrock_content_blocks`` (kept under that key so the drivers' thinking-block
    replay path stays backend-agnostic).
    """

    _LOG_PREFIX = "[Vertex]"

    # Hard ceiling (seconds) for a single messages.create call.
    _ASYNCIO_INVOKE_TIMEOUT_SECONDS = 120

    def __init__(
        self,
        model_id: Optional[str] = None,
        project_id: Optional[str] = None,
        region: Optional[str] = None,
        max_tokens_default: int = 8192,
        enable_thinking: Optional[bool] = None,
        thinking_budget_tokens: int = 4096,
        reasoning_effort: Optional[str] = None,
        access_token: Optional[str] = None,
    ):
        """
        Args:
            model_id: Vertex model ID (e.g. "claude-opus-4-6"). Bedrock-style IDs
                are normalized; see resolve_vertex_model_id. Pass None or "auto"
                to let the first reachable entry of VERTEX_MODEL_CANDIDATES win
                at call time — which project/region serves which Claude varies,
                and an unavailable ID only fails when actually invoked.
            project_id: GCP project. Falls back to ANTHROPIC_VERTEX_PROJECT_ID /
                GOOGLE_CLOUD_PROJECT.
            region: Vertex region. Defaults to us-east5, where Anthropic models
                are served; not every region carries them.
            access_token: Optional OAuth token. Omit to use Application Default
                Credentials (`gcloud auth application-default login`).
        """
        # Deliberately does not call super().__init__(): the parent builds a
        # boto3 bedrock-runtime client we have no use for.
        self._auto_model = is_auto_model(model_id)
        if self._auto_model:
            self._candidates = list(VERTEX_MODEL_CANDIDATES)
            self.model_id = self._candidates[0]
        else:
            self._candidates = []
            self.model_id = resolve_vertex_model_id(model_id)
        self.project_id = resolve_vertex_project_id(project_id)
        self.region = resolve_vertex_region(region)
        self.max_tokens_default = max_tokens_default
        self.enable_thinking = enable_thinking
        self.thinking_budget_tokens = thinking_budget_tokens
        self.reasoning_effort = reasoning_effort
        self.capture_raw_response = False
        self.executor = None  # parent-compat: only touched by shutdown()
        self.tokenizer = None
        self.token_usage = self._new_token_usage()
        self._warned_dropped_temperature = False

        self.client = AsyncAnthropicVertex(
            project_id=self.project_id,
            region=self.region,
            access_token=access_token,
            timeout=90.0,
            max_retries=0,  # we handle retries ourselves
        )

        model_desc = (
            f"auto ({' -> '.join(self._candidates)})"
            if self._auto_model
            else self.model_id
        )
        print(
            f"[Vertex] Initialized with model: {model_desc}, "
            f"project: {self.project_id}, region: {self.region}"
        )

    # ── Model capability rules ───────────────────────────────────────────

    def _is_anthropic_model(self) -> bool:
        """Vertex is only used for Claude here; Bedrock's OSS path never applies."""
        return True

    def _normalize_request_for_vertex(self, request: dict) -> dict:
        """Strip fields the Vertex Messages endpoint does not accept."""
        payload = dict(request)
        # The SDK sets the Vertex anthropic_version itself; the Bedrock literal
        # inherited from the parent request builder would be rejected.
        payload.pop("anthropic_version", None)

        if "temperature" in payload and self._rejects_sampling_params():
            self._warn_dropped_temperature(
                payload.pop("temperature"),
                "the model does not accept sampling parameters")
        return payload

    # ── Request entry point ──────────────────────────────────────────────

    async def chat_completion(
        self,
        messages: list,
        tools: Optional[list] = None,
        tool_choice: str = "auto",
        temperature: float = 1.0,
        max_tokens: Optional[int] = None,
        use_reasoning_content: bool = True,
    ) -> dict:
        # Neutralize temperature before the inherited builder sees it: on 4.7+
        # the parameter is rejected by the API anyway, so the parent's
        # "thinking is incompatible with non-default temperature" guard would
        # abort a run over a value we are about to drop regardless.
        if temperature != 1.0 and self._rejects_sampling_params():
            self._warn_dropped_temperature(
                temperature, "the model does not accept sampling parameters")
            temperature = 1.0
        return await super().chat_completion(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            use_reasoning_content=use_reasoning_content,
        )

    # ── Transport ────────────────────────────────────────────────────────

    @staticmethod
    def _is_retriable_api_error(exc: Exception) -> bool:
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", 0) or 0
            return status == 408 or status == 409 or status == 429 or status >= 500
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "overloaded",
                "rate limit",
                "resource exhausted",
                "service unavailable",
                "temporarily unavailable",
                "timeout",
                "too many requests",
                "unavailable",
            )
        )

    @staticmethod
    def _is_model_unavailable(exc: Exception) -> bool:
        """404 — this project/region does not serve the requested model."""
        if isinstance(exc, APIStatusError):
            return getattr(exc, "status_code", 0) == 404
        return False

    def _advance_candidate(self) -> bool:
        """Drop the current auto-selected model and adopt the next candidate.

        Returns False when the model was pinned explicitly or the list is
        exhausted, in which case the caller should surface the original error.
        """
        if not self._auto_model or self.model_id not in self._candidates:
            return False
        self._candidates.remove(self.model_id)
        if not self._candidates:
            return False
        previous, self.model_id = self.model_id, self._candidates[0]
        print(
            f"[Vertex] {previous} unavailable in {self.region}; "
            f"falling back to {self.model_id}"
        )
        return True

    async def _invoke_model_with_retry(self, request: dict) -> dict:
        total_attempts = API_MAX_RETRIES + 1
        attempt = 0

        while True:
            attempt += 1
            # Rebuilt each pass: a candidate switch can change which sampling
            # params are legal for the model now in use.
            payload = self._normalize_request_for_vertex(request)
            try:
                message = await asyncio.wait_for(
                    self.client.messages.create(model=self.model_id, **payload),
                    timeout=self._ASYNCIO_INVOKE_TIMEOUT_SECONDS,
                )
                # model_dump() yields the same {content: [...], stop_reason, usage}
                # shape the inherited response adapter expects. exclude_none
                # keeps unset optional block fields (citations, cache_control)
                # out of the blocks we replay back on the next turn.
                return message.model_dump(exclude_none=True)
            except asyncio.TimeoutError:
                msg = (
                    f"asyncio wait_for timeout after "
                    f"{self._ASYNCIO_INVOKE_TIMEOUT_SECONDS}s"
                )
                if attempt >= total_attempts:
                    raise TimeoutError(msg)
                print(
                    f"[Vertex] {msg} | "
                    f"retry {attempt}/{API_MAX_RETRIES} in "
                    f"{API_RETRY_DELAY_SECONDS}s"
                )
                await asyncio.sleep(API_RETRY_DELAY_SECONDS)
            except Exception as exc:
                # A 404 means the model isn't served here at all — retrying the
                # same ID is pointless, but the next candidate may work, and it
                # deserves its own full retry budget.
                if self._is_model_unavailable(exc) and self._advance_candidate():
                    attempt = 0
                    continue
                if attempt >= total_attempts or not self._is_retriable_api_error(exc):
                    raise
                print(
                    f"[Vertex] Retriable API error: {exc} | "
                    f"retry {attempt}/{API_MAX_RETRIES} in "
                    f"{API_RETRY_DELAY_SECONDS}s"
                )
                await asyncio.sleep(API_RETRY_DELAY_SECONDS)

    async def _chat_completion_openai(self, *args: Any, **kwargs: Any) -> dict:
        raise NotImplementedError(
            "Vertex AI is used here only for Claude models; there is no "
            "OpenAI-compatible path. Use --backend vllm for non-Claude models."
        )

    def shutdown(self) -> None:
        client = getattr(self, "client", None)
        if client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop and loop.is_running():
                loop.create_task(client.close())
            else:
                asyncio.run(client.close())
        except Exception:
            pass
