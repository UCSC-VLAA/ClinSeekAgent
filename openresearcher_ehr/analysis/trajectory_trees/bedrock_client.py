"""Thin wrapper around openresearcher_ehr.bedrock_generator for tree builder.

Gives us a single blocking `call_json(system, user, max_tokens)` helper that:
  • runs the async chat_completion on an internal event loop
  • retries with exponential backoff on transient Bedrock errors
  • extracts the first JSON block from the response
  • accumulates token-usage stats
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Make the parent package importable no matter where we're launched from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_OPENRES_DIR = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _OPENRES_DIR not in sys.path:
    sys.path.insert(0, _OPENRES_DIR)

from bedrock_generator import BedrockAsyncGenerator  # noqa: E402

from .prompts import extract_json_block  # noqa: E402


DEFAULT_OPUS_4_6 = "us.anthropic.claude-opus-4-6-v1"
DEFAULT_REGION = "us-west-2"


class BedrockOpusJSON:
    """Blocking JSON-out client against Claude Opus 4.6 on Bedrock.

    This class owns one BedrockAsyncGenerator and one background event loop
    (Bedrock-generator is async-first). `call_json` is thread-safe and
    blocking so the rest of the pipeline can stay sync.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_OPUS_4_6,
        region_name: str = DEFAULT_REGION,
        max_tokens_default: int = 4096,
        max_workers: int = 8,
        temperature: float = 0.0,
    ):
        self.model_id = model_id
        self.region_name = region_name
        self.temperature = temperature
        self._gen = BedrockAsyncGenerator(
            model_id=model_id,
            region_name=region_name,
            max_tokens_default=max_tokens_default,
            max_workers=max_workers,
        )
        # Dedicated background loop so we can call async code from sync threads.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        # Stats
        self._stats_lock = threading.Lock()
        self.stats = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "retries": 0,
            "errors": 0,
            "elapsed_s": 0.0,
        }

    def close(self) -> None:
        try:
            self._gen.shutdown()
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _run(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def _accumulate(self, resp: Dict[str, Any], t_elapsed: float) -> None:
        usage = resp.get("usage") or {}
        in_tok = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0
        out_tok = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0
        with self._stats_lock:
            self.stats["calls"] += 1
            self.stats["input_tokens"] += int(in_tok)
            self.stats["output_tokens"] += int(out_tok)
            self.stats["elapsed_s"] += t_elapsed

    def call_raw(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> Tuple[str, Dict[str, Any]]:
        """Call the model, return (text, full_response)."""
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_err: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            t0 = time.time()
            try:
                resp = self._run(
                    self._gen.chat_completion(
                        messages=msgs,
                        tools=None,
                        tool_choice="none",
                        temperature=self.temperature,
                        max_tokens=max_tokens,
                    )
                )
                elapsed = time.time() - t0
                self._accumulate(resp, elapsed)
                msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
                text = msg.get("content") or ""
                if not text and msg.get("reasoning_content"):
                    # Adaptive-thinking-only responses are a no-op for us
                    text = msg["reasoning_content"]
                if not text.strip():
                    raise ValueError("empty model response")
                return text, resp
            except Exception as e:  # noqa: BLE001
                last_err = e
                with self._stats_lock:
                    self.stats["retries"] += 1
                backoff = min(30, 2 ** attempt)
                print(f"[bedrock_client] attempt {attempt+1} failed: {e!r}; sleeping {backoff}s")
                time.sleep(backoff)
        with self._stats_lock:
            self.stats["errors"] += 1
        raise RuntimeError(f"bedrock call failed after {max_retries+1} attempts: {last_err!r}")

    def call_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> Tuple[Any, str]:
        """Call the model and parse the first JSON block. Returns (parsed, raw_text)."""
        text, _ = self.call_raw(system, user, max_tokens=max_tokens, max_retries=max_retries)
        try:
            return extract_json_block(text), text
        except ValueError as e:
            # One targeted re-ask asking for JSON-only
            print(f"[bedrock_client] JSON parse failed ({e}); re-asking once")
            text2, _ = self.call_raw(
                system,
                user
                + "\n\nIMPORTANT: Return ONLY a single valid JSON object. No prose, no code fences.",
                max_tokens=max_tokens,
                max_retries=max_retries,
            )
            return extract_json_block(text2), text2
