"""One-shot reasoning-model evaluation driver (no tool calling).

Sends the benchmark row's fully-rendered `question` directly to the model
once, asks it to return strict JSON `{"response": [...]}`, then wraps the
response into a synthetic `ehr.finish` tool call so
`helper/evaluate_results.py` can score the resulting `results.jsonl`
unchanged.

Backends:
    bedrock   — AWS Bedrock (Anthropic + OpenAI-shape). Supported today.
    vllm      — local OpenAI-compatible endpoint. Stubbed; raises
                NotImplementedError. Wiring point is `_invoke_once`.

Usage:
    python deploy_reasoning_model.py \
        --backend bedrock \
        --model "Claude Opus 4.6" \
        --data /fsx-shared/juncheng/EHR/data/EHR-Bench/ehr_bench_sampled_40_per_task.json \
        --output-dir ./results/one_shot_opus46_1800 \
        --concurrency 6

For Bedrock, `--model` accepts any friendly name from
`bedrock_model_region_availability.json`; rows are round-robin partitioned
across every OK region for that model. When vllm is implemented,
`--api-base-url` and `--model-id` will point at the local server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Tuple


# ----------------------------------------------------------------------------
# Prompt assembly
# ----------------------------------------------------------------------------
#
# The benchmark's `question` field is a fully rendered prompt containing
# the task_instruction, patient_timeline, and (for decision_making tasks)
# a <candidate_answers> list. Its embedded directive says "Return only
# the final answer text..." but reasoning-heavy models often ignore that
# and wrap the answer in prose, markdown bolding, or long justifications.
# To make extraction deterministic we append a short trailer asking the
# model to also emit an <answer>...</answer> block. The extractor looks
# for that block first and falls back to the plain-text salvage when it's
# missing.
#
# Keeping the trailer minimal preserves the original benchmark contract
# while giving us a reliable anchor to parse.

_ANSWER_TRAILER = (
    "\n\n---\n"
    "After any reasoning, emit your final answer inside an <answer>...</answer> "
    "XML block as the very last thing you write. Put one answer per line inside "
    "the block, with no bullets, numbering, or markdown. If the task expects a "
    "single polar answer, put just that one line. If <candidate_answers> were "
    "provided, each line must match a candidate verbatim."
)


def build_user_content(row: Dict[str, Any]) -> str:
    return row["question"] + _ANSWER_TRAILER


# ----------------------------------------------------------------------------
# Response parsing  (same shape as deploy_agent._salvage_plain_text_answer)
# ----------------------------------------------------------------------------


def _extract_embedded_response_json(text: str) -> List[str] | None:
    if not text or "response" not in text:
        return None

    def _unpack(obj: Any) -> List[str] | None:
        if not isinstance(obj, dict) or "response" not in obj:
            return None
        resp = obj["response"]
        if isinstance(resp, list):
            items = [str(x).strip() for x in resp if str(x).strip()]
            return items or None
        if isinstance(resp, str) and resp.strip():
            return [resp.strip()]
        return None

    depth = 0
    start = -1
    last_start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
                last_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except Exception:
                        start = -1
                        continue
                    result = _unpack(obj)
                    if result is not None:
                        return result
                    start = -1

    if last_start >= 0 and depth > 0:
        tail = text[last_start:].rstrip()
        for suffix in ("}", "]}", "]]}", '"]}'):
            try:
                obj = json.loads(tail + suffix)
            except Exception:
                continue
            result = _unpack(obj)
            if result is not None:
                return result
    return None


def _extract_answer_block(text: str) -> List[str] | None:
    """Parse the model's <answer>...</answer> block if present.

    We prefer the *last* block (so chain-of-thought that quotes the tag name
    earlier doesn't hijack extraction). Returns None if the block is missing
    or empty. Lines are split, trimmed, bullets stripped, and markdown
    bold / italic markers removed so '**EW EMER.**' normalizes to 'EW EMER.'
    """
    if not text or "<answer" not in text.lower():
        return None
    matches = list(
        re.finditer(r"<answer[^>]*>\s*(.*?)\s*</answer\s*>", text, re.DOTALL | re.IGNORECASE)
    )
    if not matches:
        # Unterminated — take tail after the last <answer...>
        m = re.search(r"<answer[^>]*>\s*(.*)$", text, re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        blob = m.group(1)
    else:
        blob = matches[-1].group(1)
    items: List[str] = []
    for raw in blob.splitlines():
        cleaned = _clean_answer_line(raw)
        if cleaned:
            items.append(cleaned)
    return items or None


def _clean_answer_line(raw: str) -> str:
    """Strip bullets, numbering, and markdown bold/italic from a line."""
    s = raw.strip()
    # Numbering: "1.", "1)", "(1)"
    s = re.sub(r"^\(?\d+[.)]\s*", "", s)
    # Bullets
    s = s.lstrip("-*•").strip()
    # Markdown bold/italic: **x**, *x*, __x__, _x_. The non-greedy forms
    # handle the common case; as a final pass, strip any leading/trailing
    # unmatched markers too (e.g. "**EW EMER.**" where the regex may not
    # match depending on the surrounding text).
    for _ in range(3):  # handle nested
        new = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        new = re.sub(r"__(.+?)__", r"\1", new)
        new = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", new)
        new = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", new)
        if new == s:
            break
        s = new
    # Strip unmatched leading/trailing markdown markers (e.g. "**EW EMER.**").
    while s.startswith("**") or s.startswith("__"):
        s = s[2:]
    while s.endswith("**") or s.endswith("__"):
        s = s[:-2]
    s = s.strip("*_").strip()
    # Trailing punctuation often attached to bolding: **X.** -> X. (keep .)
    # but strip surrounding quotes/backticks.
    s = s.strip("`\"'“”‘’").strip()
    return s


def _strip_reasoning_blocks(text: str) -> str:
    """Drop <reasoning>...</reasoning>, <think>...</think>, <analysis>... blocks.

    OSS reasoning models (gpt-oss, MiniMax, Qwen, Kimi) often wrap their CoT
    in these tags and emit the actual answer only after a closing tag. For
    one-shot scoring we want just the post-reasoning answer.
    """
    if not text:
        return ""
    # Greedy strip of any paired block. Also handles a dangling open tag
    # at end-of-string by dropping from the open tag onward (the model ran
    # out of max_tokens inside the reasoning — no answer was produced).
    for open_tag, close_tag in (
        ("<reasoning>", "</reasoning>"),
        ("<think>", "</think>"),
        ("<analysis>", "</analysis>"),
        ("<thought>", "</thought>"),
    ):
        while True:
            i = text.find(open_tag)
            if i < 0:
                break
            j = text.find(close_tag, i + len(open_tag))
            if j < 0:
                # Unterminated — drop from the open tag to end.
                text = text[:i]
                break
            text = text[:i] + text[j + len(close_tag):]
    return text.strip()


_ANSWER_CUE_PATTERNS = (
    "final answer:",
    "answer:",
    "the answer is",
    "therefore, the answer",
    "therefore the answer",
    "my answer:",
    "final prediction:",
    "prediction:",
)


def _pick_answer_block(text: str) -> str:
    """Reduce reasoning-heavy output to just the final answer block.

    The benchmark instruction is 'Return only the final answer text. If
    multiple answers are needed, put one answer per line with no bullets
    or numbering.' Many models (Opus especially) ignore this and prefix a
    long justification before emitting the actual answer.

    Heuristics (in priority order):
      1. If any line matches `(final )?answer:` / `prediction:` / similar,
         take the tail starting at that line and drop the label prefix.
      2. If the text ends with a block of consecutive short lines
         (<=80 chars each) separated from earlier content by a blank line,
         return that trailing block. Bullets are stripped.
      3. Otherwise return the last non-empty line.
    """
    body = text.strip()
    if not body:
        return ""
    lines = body.splitlines()

    # --- (1) Answer-cue split --------------------------------------------
    lowered = body.lower()
    cue_pos = -1
    for cue in _ANSWER_CUE_PATTERNS:
        idx = lowered.rfind(cue)
        if idx > cue_pos:
            cue_pos = idx
    if cue_pos >= 0:
        tail = body[cue_pos:]
        # Drop the cue label itself; keep everything after the colon/newline.
        nl = tail.find("\n")
        head = tail[: nl if nl >= 0 else len(tail)]
        after_colon = head.split(":", 1)
        if len(after_colon) == 2 and after_colon[1].strip():
            remainder = after_colon[1].strip()
            if nl >= 0:
                remainder = remainder + "\n" + tail[nl + 1 :]
            return remainder.strip()
        if nl >= 0:
            return tail[nl + 1 :].strip()

    # --- (2) Trailing short-line block -----------------------------------
    # Walk lines from the bottom, collecting consecutive short lines up to
    # the first blank line or the first long (reasoning-style) line.
    tail: List[str] = []
    for raw in reversed(lines):
        stripped = _clean_answer_line(raw)
        if not stripped:
            if tail:
                break  # hit the blank separator
            continue
        if len(stripped) > 120 and tail:
            break  # reasoning paragraph above the answer block
        tail.append(stripped)
    if tail:
        return "\n".join(reversed(tail))
    # --- (3) Fallback: last non-empty line --------------------------------
    for raw in reversed(lines):
        s = _clean_answer_line(raw)
        if s:
            return s
    return body


def salvage_plain_text(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    # 1. Preferred: explicit <answer>...</answer> block we asked the model for.
    resp = _extract_answer_block(text)
    if resp:
        return resp
    # 2. Embedded {"response": [...]} JSON (common OSS format).
    resp = _extract_embedded_response_json(text)
    if resp:
        return resp
    # 3. Strip reasoning-tag blocks and re-try embedded JSON / answer-block.
    stripped = _strip_reasoning_blocks(text)
    if stripped and stripped != text:
        resp = _extract_answer_block(stripped)
        if resp:
            return resp
        resp = _extract_embedded_response_json(stripped)
        if resp:
            return resp
    # 4. Heuristic reduction to the final-answer block.
    body = _pick_answer_block(stripped or text)
    if not body:
        return []
    items = []
    for raw in body.splitlines():
        cleaned = _clean_answer_line(raw)
        if cleaned:
            items.append(cleaned)
    return items or [body]


# ----------------------------------------------------------------------------
# Bedrock invoker
# ----------------------------------------------------------------------------

_REGION_AVAILABILITY_PATH = Path(
    "/fsx-shared/juncheng/EHR/openresearcher_ehr/bedrock_model_region_availability.json"
)

_RETRYABLE_KEYWORDS = (
    "timeout",
    "throttl",
    "too many requests",
    "service unavailable",
    "internal server error",
    "internalserver",
    "unexpected error",
    "busy",
)


class BedrockInvoker:
    """Process-global Bedrock client cache + retry loop.

    Importing boto3 is deferred to instantiation so that running with
    `--backend vllm` does not require boto3 to be installed.
    """

    def __init__(
        self,
        max_tokens: int,
        temperature: float,
        max_retries: int,
        read_timeout: int = 120,
    ):
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError

        self._boto3 = boto3
        self._BotoCoreError = BotoCoreError
        self._ClientError = ClientError
        self._boto_config = BotoConfig(
            read_timeout=read_timeout,
            connect_timeout=10,
            retries={"max_attempts": 0},
        )
        self._clients: Dict[str, Any] = {}
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries

    # --- client -----------------------------------------------------------
    def _client(self, region: str):
        c = self._clients.get(region)
        if c is None:
            c = self._boto3.client(
                "bedrock-runtime", region_name=region, config=self._boto_config
            )
            self._clients[region] = c
        return c

    # --- retry predicate --------------------------------------------------
    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, self._ClientError):
            code = (exc.response or {}).get("Error", {}).get("Code", "") or ""
            if code in {
                "InternalServerException",
                "ModelNotReadyException",
                "RequestTimeoutException",
                "ServiceUnavailableException",
                "ThrottlingException",
                "TooManyRequestsException",
            }:
                return True
        if isinstance(exc, self._BotoCoreError):
            return True
        return any(kw in str(exc).lower() for kw in _RETRYABLE_KEYWORDS)

    # --- dispatchers ------------------------------------------------------
    @staticmethod
    def _is_anthropic(model_id: str) -> bool:
        return "anthropic" in model_id.lower()

    def _invoke_anthropic(self, client, model_id: str, prompt_text: str) -> str:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt_text}],
                }
            ],
        }
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        return "\n".join(
            blk.get("text", "")
            for blk in (out.get("content") or [])
            if blk.get("type") == "text"
        ).strip()

    def _invoke_openai(self, client, model_id: str, prompt_text: str) -> str:
        body = {
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {"role": "user", "content": prompt_text},
            ],
        }
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        try:
            return (out["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            return json.dumps(out)

    # --- main entry -------------------------------------------------------
    def invoke(
        self, region: str, model_id: str, prompt_text: str
    ) -> Tuple[str, str]:
        client = self._client(region)
        use_anthropic = self._is_anthropic(model_id)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if use_anthropic:
                    return self._invoke_anthropic(client, model_id, prompt_text), "ok"
                return self._invoke_openai(client, model_id, prompt_text), "ok"
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                time.sleep(min(2**attempt, 10))
        return f"[ERROR] {type(last_err).__name__}: {last_err}", "error"


# ----------------------------------------------------------------------------
# vLLM invoker  (stub — wire in when we have a local server to test against)
# ----------------------------------------------------------------------------


class VLLMInvoker:
    """Placeholder for a future vLLM (OpenAI-compatible) one-shot invoker."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "vLLM backend is not implemented yet. Wire this up to talk to "
            "an OpenAI-compatible /v1/chat/completions endpoint when ready."
        )

    def invoke(self, region: str, model_id: str, prompt_text: str) -> Tuple[str, str]:
        raise NotImplementedError


# ----------------------------------------------------------------------------
# Row processing  (backend-agnostic)
# ----------------------------------------------------------------------------


def _qid_of(row: Dict[str, Any]) -> str:
    if row.get("qid"):
        return str(row["qid"])
    subj = row.get("subject_id")
    task = row.get("task")
    if task and subj is not None:
        return f"{task}_{subj}"
    return f"unknown_{uuid.uuid4().hex[:8]}"


def process_row(
    row: Dict[str, Any],
    region: str,
    model_id: str,
    invoker,
    run_index: int,
) -> Dict[str, Any]:
    qid = _qid_of(row)
    prompt_text = build_user_content(row)
    text, status = invoker.invoke(region, model_id, prompt_text)

    preds = salvage_plain_text(text) if status == "ok" else []
    synth_id = f"one_shot_{uuid.uuid4().hex[:8]}"
    messages = [
        {"role": "user", "content": prompt_text},
        {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": synth_id,
                    "type": "function",
                    "function": {
                        "name": "ehr.finish",
                        "arguments": json.dumps({"response": preds}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": synth_id,
            "content": "Finish (one-shot)",
        },
    ]

    out = dict(row)
    out.update(
        {
            "qid": qid,
            "session_id": f"{qid}__q_1__run_{run_index}",
            "run_index": run_index,
            "runs_per_question": 1,
            "messages": messages,
            "completed": status == "ok",
            "status": status,
            "stop_reason": "finish_tool_call" if status == "ok" else "exception",
            "error": None if status == "ok" else text,
            "backend_region": region,
            "backend_model_id": model_id,
        }
    )
    return out


# ----------------------------------------------------------------------------
# Shard runner
# ----------------------------------------------------------------------------


def run_shard(
    shard_rows: List[Dict[str, Any]],
    region: str,
    model_id: str,
    invoker,
    output_path: Path,
    concurrency: int,
    run_index: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with output_path.open("w") as f, ThreadPoolExecutor(
        max_workers=concurrency
    ) as pool:
        futures = {
            pool.submit(process_row, row, region, model_id, invoker, run_index): row
            for row in shard_rows
        }
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = futures[fut]
                qid = _qid_of(row)
                res = {
                    **row,
                    "qid": qid,
                    "messages": [],
                    "completed": False,
                    "status": "error",
                    "stop_reason": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                    "backend_region": region,
                    "backend_model_id": model_id,
                }
            f.write(json.dumps(res) + "\n")
            f.flush()
            done += 1
            if done % 25 == 0 or done == len(shard_rows):
                print(
                    f"[{region}] {done}/{len(shard_rows)} "
                    f"status={res.get('status','?')} qid={res.get('qid')}",
                    flush=True,
                )


# ----------------------------------------------------------------------------
# Region planning + entrypoint
# ----------------------------------------------------------------------------


def _load_region_map(model_name: str) -> Dict[str, str]:
    j = json.loads(_REGION_AVAILABILITY_PATH.read_text())
    if model_name not in j:
        raise ValueError(
            f"Model {model_name!r} not in region catalog. Known: "
            f"{sorted(j.keys())}"
        )
    return {e["region"]: e["model_id"] for e in j[model_name] if e["status"] == "OK"}


def _plan_bedrock(args) -> List[Tuple[str, str]]:
    region_map = _load_region_map(args.model)
    if args.regions:
        # Explicit region list (possibly multi) from the user.
        region_map = {r: region_map[r] for r in args.regions if r in region_map}
        if not region_map:
            raise SystemExit(
                f"none of --regions {args.regions} are OK for {args.model}"
            )
        return list(region_map.items())
    if args.multi_region:
        # Keep every OK region (the old default). Useful when the benchmark
        # is large and you want multi-region throughput.
        return list(region_map.items())
    # Default: pick exactly one region. One-shot inference is fast and
    # fan-out only helps amortize multi-turn tool loops — for single
    # invoke_model calls it mostly just exposes us to whichever region has
    # a wedged/slow shard. Preference order: us-east-1, then first OK.
    for preferred in ("us-east-1", "us-west-2", "us-east-2"):
        if preferred in region_map:
            return [(preferred, region_map[preferred])]
    first = next(iter(region_map.items()))
    return [first]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, help="Benchmark JSON file")
    ap.add_argument(
        "--backend",
        choices=["bedrock", "vllm"],
        default="bedrock",
        help="Inference backend (vllm not yet implemented)",
    )
    ap.add_argument(
        "--model",
        default="Claude Opus 4.6",
        help="Friendly name (bedrock) / model id (vllm)",
    )
    ap.add_argument("--output-dir", required=True)
    ap.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Threads per region. Higher values are safe for one-shot "
        "invoke_model because each request is independent.",
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=16384,
        help="Upper bound on output tokens. Reasoning-heavy OSS models "
        "(gpt-oss, MiniMax) need headroom for the full CoT + answer.",
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument(
        "--read-timeout",
        type=int,
        default=120,
        help="Bedrock HTTP read timeout (seconds). Bump for models that "
        "stall on long reasoning outputs (e.g. MiniMax).",
    )
    ap.add_argument("--run-index", type=int, default=1)
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process first N rows (smoke testing)",
    )
    # Bedrock-specific
    ap.add_argument(
        "--regions",
        nargs="*",
        default=None,
        help="Explicit Bedrock region list (overrides both default "
        "single-region pick and --multi-region)",
    )
    ap.add_argument(
        "--multi-region",
        action="store_true",
        help="Fan out across every OK region for the model. Off by "
        "default because one-shot inference rarely benefits — a wedged "
        "region just blocks the merge. Set this only for very large "
        "benchmarks where per-region throughput is the bottleneck.",
    )
    # vLLM-specific (placeholders)
    ap.add_argument("--api-base-url", default=None)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    rows: List[Dict[str, Any]] = json.loads(Path(args.data).read_text())
    if args.limit > 0:
        rows = rows[: args.limit]

    # Build invoker + fan-out plan
    if args.backend == "bedrock":
        invoker = BedrockInvoker(
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
            read_timeout=args.read_timeout,
        )
        plan: List[Tuple[str, str]] = _plan_bedrock(args)
        targets = plan
    else:
        invoker = VLLMInvoker(
            api_base_url=args.api_base_url,
            api_key=args.api_key,
            model_id=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
        )
        # vLLM: one "region" = the local server
        targets = [("local", args.model)]

    print(
        f"[{args.backend}] one-shot on {len(rows)} rows, model={args.model!r}, "
        f"targets={[r for r, _ in targets]}",
        flush=True,
    )

    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "region_map.json").write_text(
        json.dumps(dict(targets), indent=2)
    )

    # Round-robin partition rows across targets
    shards: Dict[str, List[Dict[str, Any]]] = {r: [] for r, _ in targets}
    for i, row in enumerate(rows):
        shards[targets[i % len(targets)][0]].append(row)
    for (region, model_id) in targets:
        print(
            f"  {region:<18} {model_id:<50} {len(shards[region])} rows",
            flush=True,
        )

    # Run shards in parallel threads
    threads: List[Thread] = []
    for (region, model_id) in targets:
        out_path = out_root / region / "results.jsonl"
        t = Thread(
            target=run_shard,
            kwargs={
                "shard_rows": shards[region],
                "region": region,
                "model_id": model_id,
                "invoker": invoker,
                "output_path": out_path,
                "concurrency": args.concurrency,
                "run_index": args.run_index,
            },
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    # Merge
    merged = out_root / "results.jsonl"
    n = 0
    with merged.open("w") as mf:
        for (region, _) in targets:
            shard_path = out_root / region / "results.jsonl"
            if shard_path.exists():
                with shard_path.open() as sf:
                    for line in sf:
                        mf.write(line)
                        n += 1
    print(f"\nMerged {n} rows -> {merged}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
