"""One-shot reasoning-model evaluation driver for **multimodal** EHR-Bench.

Mirror of `deploy_reasoning_model.py` for the MM pipeline: sends the
benchmark row's pre-rendered `question` + attached CXR image(s) + inlined
radiology reports to the model in a single invoke, asks for the final
answer inside an `<answer>...</answer>` block, and wraps the response
into a synthetic `ehr.finish` tool call so `helper/scorer_mm.py` can
score the resulting `results.jsonl` unchanged.

Backends:
    bedrock   - AWS Bedrock (Anthropic content-blocks; OpenAI-shape with
                a text-only flatten for OSS vision-limited models).
    vllm      - stubbed; raises NotImplementedError.

Usage:
    python deploy_reasoning_model_mm.py \\
        --backend bedrock \\
        --model "Claude Opus 4.6" \\
        --data data/ClinSeek-Bench/inputs/mm_bench.jsonl \\
        --bench-root data/ClinSeek-Bench/data/mm_bench \\
        --output-dir ./results/oneshot_mm_opus46 \\
        --concurrency 8
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------------------------------------------------------
# Prompt trailer (same contract as text-only one-shot)
# ----------------------------------------------------------------------------

_ANSWER_TRAILER = (
    "\n\n---\n"
    "After any reasoning, emit your final answer inside an <answer>...</answer> "
    "XML block as the very last thing you write. Put one answer per line inside "
    "the block, with no bullets, numbering, or markdown. If the task expects a "
    "single polar answer, put just that one line. If <candidate_answers> were "
    "provided, each line must match a candidate verbatim."
)


# ----------------------------------------------------------------------------
# Image/report attachment  (ported from deploy_agent_mm.py)
# ----------------------------------------------------------------------------

_VALID_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_MEDIA_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_IMAGES_PER_SAMPLE = 4
_MAX_REPORT_CHARS = 20000


def _split_bench_roots(raw: Optional[str]) -> List[Path]:
    if not raw:
        return []
    parts: List[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        is_windows_drive = (
            len(chunk) >= 3
            and chunk[1] == ":"
            and chunk[0].isalpha()
            and chunk[2] in ("\\", "/")
        )
        if is_windows_drive:
            parts.append(chunk)
        else:
            parts.extend(p for p in chunk.split(":") if p.strip())
    return [Path(p).expanduser() for p in parts]


def resolve_asset_path(path: str, bench_roots: List[Path]) -> Optional[Path]:
    """Resolve `path` (usually relative) against any of the bench roots.

    Accepts absolute paths verbatim; otherwise joins to each root in order
    and returns the first that exists. Also handles the case where the
    model-ready JSONL encodes paths with a leading `MedModOriginalLinked_v1/`
    or `EHRXQAOriginalLinked_v1/` prefix. We strip that first segment
    before retrying against each bench root (since the v3 root layouts
    start at `mimic-cxr/...`).
    """
    if not path:
        return None
    p = Path(path)
    if p.is_absolute() and p.exists():
        return p
    candidates = [path]
    # Strip leading "*OriginalLinked_v1/" / "*AgentBench_v3/" segments.
    first, sep, rest = path.partition("/")
    if sep and (
        first.endswith("OriginalLinked_v1")
        or first.endswith("AgentBench_v3")
        or first.endswith("AgentBench_v3.zip")
    ):
        candidates.append(rest)
    for cand in candidates:
        for root in bench_roots:
            resolved = (root / cand).resolve()
            if resolved.exists():
                return resolved
    return None


def _load_image_b64(path: Path, max_edge: int) -> Optional[Tuple[str, str]]:
    """Return (base64_data, media_type) for a resized JPEG encoding."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for multimodal runs: pip install Pillow"
        ) from exc
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if max_edge and longest > max_edge:
                scale = max_edge / float(longest)
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        print(f"[mm] failed to load image {path}: {exc}", flush=True)
        return None
    return data, "image/jpeg"


def _load_report_text(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[mm] failed to read report {path}: {exc}", flush=True)
        return None
    text = text.strip()
    if len(text) > _MAX_REPORT_CHARS:
        text = text[:_MAX_REPORT_CHARS] + "\n[report truncated]"
    return text


def build_mm_inputs(
    row: Dict[str, Any],
    bench_roots: List[Path],
    image_max_edge: int,
) -> Tuple[str, List[Tuple[str, str]], List[str], Dict[str, Any]]:
    """Return (user_text, image_b64s, report_texts, summary).

    Prompt assembly:
      - If the row carries a pre-built `input_text` (e.g. the model-ready
        combined_test_set released on HF, which inlines pre-sliced EHR
        tables as `<ehr_context>`), use it directly and only append the
        `<answer>` trailer.
      - Otherwise fall back to `question` and stitch a `<run_context>`
        preamble describing what's attached.
    Inline reports are appended only when they aren't already in
    `input_text` (avoids duplicate content).
    """
    use_input_text = bool(row.get("input_text"))
    base_text = row.get("input_text") if use_input_text else (
        row.get("question") or row.get("query") or ""
    )

    # Images
    resolved_images: List[str] = []
    dropped_images: List[str] = []
    image_b64s: List[Tuple[str, str]] = []
    for raw in (row.get("image_paths") or [])[:_MAX_IMAGES_PER_SAMPLE]:
        resolved = resolve_asset_path(raw, bench_roots)
        if resolved is None or resolved.suffix.lower() not in _VALID_IMAGE_SUFFIXES:
            dropped_images.append(raw)
            continue
        loaded = _load_image_b64(resolved, image_max_edge)
        if loaded is None:
            dropped_images.append(str(resolved))
            continue
        image_b64s.append(loaded)
        resolved_images.append(str(resolved))

    # Reports: skip if input_text already has them inlined.
    resolved_reports: List[str] = []
    report_texts: List[str] = []
    if not use_input_text:
        for raw in row.get("report_paths") or []:
            resolved = resolve_asset_path(raw, bench_roots)
            if resolved is None:
                continue
            text = _load_report_text(resolved)
            if not text:
                continue
            report_texts.append(
                f"\n<linked_radiology_report path=\"{resolved}\">\n{text}\n</linked_radiology_report>\n"
            )
            resolved_reports.append(str(resolved))

    subject_id = row.get("subject_id")
    scope = row.get("scope")
    is_cohort_scope = scope == "cohort_scope"

    if use_input_text:
        # The model-ready text already contains all context; just add the
        # answer-extraction trailer.
        user_text = base_text + _ANSWER_TRAILER
    else:
        preamble_lines = [
            "<run_context>",
            f"- images_attached: {len(resolved_images)}",
            f"- reports_inlined: {len(resolved_reports)}",
            f"- patient_ehr_available: {'yes' if (subject_id and not is_cohort_scope) else 'no'}",
            "</run_context>",
        ]
        if resolved_images:
            preamble_lines.insert(
                -1, "- image_paths (for reference):\n    " + "\n    ".join(resolved_images)
            )
        preamble = "\n".join(preamble_lines)
        user_text = preamble + "\n\n" + base_text + "".join(report_texts) + _ANSWER_TRAILER

    summary = {
        "num_images": len(resolved_images),
        "num_reports": len(resolved_reports),
        "resolved_image_paths": resolved_images,
        "resolved_report_paths": resolved_reports,
        "dropped_image_paths": dropped_images,
        "has_subject_id": bool(subject_id),
        "is_cohort_scope": is_cohort_scope,
        "used_input_text": use_input_text,
    }
    return user_text, image_b64s, report_texts, summary


# ----------------------------------------------------------------------------
# Answer extraction  (copied verbatim from deploy_reasoning_model.py)
# ----------------------------------------------------------------------------


def _extract_answer_block(text: str) -> Optional[List[str]]:
    if not text or "<answer" not in text.lower():
        return None
    matches = list(
        re.finditer(r"<answer[^>]*>\s*(.*?)\s*</answer\s*>", text, re.DOTALL | re.IGNORECASE)
    )
    if not matches:
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
    s = raw.strip()
    s = re.sub(r"^\(?\d+[.)]\s*", "", s)
    s = s.lstrip("-*\u2022").strip()
    for _ in range(3):
        new = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        new = re.sub(r"__(.+?)__", r"\1", new)
        new = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", new)
        new = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", new)
        if new == s:
            break
        s = new
    while s.startswith("**") or s.startswith("__"):
        s = s[2:]
    while s.endswith("**") or s.endswith("__"):
        s = s[:-2]
    s = s.strip("*_").strip()
    s = s.strip("`\"'“”‘’").strip()
    return s


def _strip_reasoning_blocks(text: str) -> str:
    if not text:
        return ""
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
                text = text[:i]
                break
            text = text[:i] + text[j + len(close_tag) :]
    return text.strip()


def _extract_embedded_response_json(text: str) -> Optional[List[str]]:
    if not text or "response" not in text:
        return None

    def _unpack(obj: Any) -> Optional[List[str]]:
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
        for suffix in ("}", "]}", "]]}", "\"]}"):
            try:
                obj = json.loads(tail + suffix)
            except Exception:
                continue
            result = _unpack(obj)
            if result is not None:
                return result
    return None


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
    body = text.strip()
    if not body:
        return ""
    lines = body.splitlines()
    lowered = body.lower()
    cue_pos = -1
    for cue in _ANSWER_CUE_PATTERNS:
        idx = lowered.rfind(cue)
        if idx > cue_pos:
            cue_pos = idx
    if cue_pos >= 0:
        tail = body[cue_pos:]
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
    tail_lines: List[str] = []
    for raw in reversed(lines):
        stripped = _clean_answer_line(raw)
        if not stripped:
            if tail_lines:
                break
            continue
        if len(stripped) > 120 and tail_lines:
            break
        tail_lines.append(stripped)
    if tail_lines:
        return "\n".join(reversed(tail_lines))
    for raw in reversed(lines):
        s = _clean_answer_line(raw)
        if s:
            return s
    return body


def salvage_plain_text(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    resp = _extract_answer_block(text)
    if resp:
        return resp
    resp = _extract_embedded_response_json(text)
    if resp:
        return resp
    stripped = _strip_reasoning_blocks(text)
    if stripped and stripped != text:
        resp = _extract_answer_block(stripped)
        if resp:
            return resp
        resp = _extract_embedded_response_json(stripped)
        if resp:
            return resp
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
# Bedrock invoker  (Anthropic content-blocks + OpenAI-shape text flatten)
# ----------------------------------------------------------------------------

_REGION_AVAILABILITY_PATH = Path(__file__).with_name(
    "bedrock_model_region_availability.json"
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
    def __init__(self, max_tokens: int, temperature: float, max_retries: int):
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError

        self._boto3 = boto3
        self._BotoCoreError = BotoCoreError
        self._ClientError = ClientError
        self._boto_config = BotoConfig(
            read_timeout=180,
            connect_timeout=10,
            retries={"max_attempts": 0},
        )
        self._clients: Dict[str, Any] = {}
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries

    def _client(self, region: str):
        c = self._clients.get(region)
        if c is None:
            c = self._boto3.client(
                "bedrock-runtime", region_name=region, config=self._boto_config
            )
            self._clients[region] = c
        return c

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

    @staticmethod
    def _is_anthropic(model_id: str) -> bool:
        return "anthropic" in model_id.lower()

    @staticmethod
    def _supports_images(model_id: str) -> bool:
        """Best-guess per Bedrock catalog. Anthropic + Qwen-VL accept image
        content blocks; all other OSS shapes lose on image input, so we
        flatten to a text marker before sending."""
        m = model_id.lower()
        if "anthropic" in m:
            return True
        if "qwen3-vl" in m or "qwen.qwen3-vl" in m:
            return True
        return False

    def _invoke_anthropic(
        self, client, model_id: str, user_text: str, image_b64s: List[Tuple[str, str]]
    ) -> str:
        content: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
        for data, media_type in image_b64s:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": content}],
        }
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        return "\n".join(
            blk.get("text", "")
            for blk in (out.get("content") or [])
            if blk.get("type") == "text"
        ).strip()

    def _invoke_openai(
        self, client, model_id: str, user_text: str, image_b64s: List[Tuple[str, str]]
    ) -> str:
        supports_images = self._supports_images(model_id)
        if supports_images and image_b64s:
            # OpenAI-shape multimodal: content is a list of parts.
            parts: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
            for data, media_type in image_b64s:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{data}"},
                    }
                )
            content: Any = parts
        else:
            # Flatten to text with a marker line per image.
            if image_b64s:
                markers = "\n".join(
                    f"[image attached ({mt}) - not inlined for this model]"
                    for _, mt in image_b64s
                )
                content = user_text + "\n\n" + markers
            else:
                content = user_text
        body = {
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": content}],
        }
        resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        try:
            msg_content = out["choices"][0]["message"]["content"]
            if isinstance(msg_content, list):
                # Some models return list-of-parts in responses too.
                return "\n".join(
                    p.get("text", "") for p in msg_content if isinstance(p, dict)
                ).strip()
            return (msg_content or "").strip()
        except Exception:
            return json.dumps(out)

    def invoke(
        self,
        region: str,
        model_id: str,
        user_text: str,
        image_b64s: List[Tuple[str, str]],
    ) -> Tuple[str, str]:
        client = self._client(region)
        use_anthropic = self._is_anthropic(model_id)
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                if use_anthropic:
                    return self._invoke_anthropic(client, model_id, user_text, image_b64s), "ok"
                return self._invoke_openai(client, model_id, user_text, image_b64s), "ok"
            except Exception as exc:
                last_err = exc
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                time.sleep(min(2**attempt, 10))
        return f"[ERROR] {type(last_err).__name__}: {last_err}", "error"


class VLLMInvoker:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "vLLM backend is not implemented yet for the MM driver."
        )

    def invoke(self, *a, **kw):
        raise NotImplementedError


# ----------------------------------------------------------------------------
# Row processing
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
    bench_roots: List[Path],
    image_max_edge: int,
    invoker,
    run_index: int,
) -> Dict[str, Any]:
    qid = _qid_of(row)
    user_text, image_b64s, _report_texts, summary = build_mm_inputs(
        row, bench_roots, image_max_edge
    )
    text, status = invoker.invoke(region, model_id, user_text, image_b64s)

    preds = salvage_plain_text(text) if status == "ok" else []
    synth_id = f"one_shot_{uuid.uuid4().hex[:8]}"
    messages = [
        {"role": "user", "content": user_text},
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
            "content": "Finish (one-shot MM)",
        },
    ]

    out = dict(row)
    # scorer_mm reads `label`; model_ready files put gold under `ground_truth`.
    if out.get("label") is None and out.get("ground_truth") is not None:
        out["label"] = out["ground_truth"]
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
            "multimodal_summary": summary,
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
    bench_roots: List[Path],
    image_max_edge: int,
    invoker,
    output_path: Path,
    concurrency: int,
    run_index: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with output_path.open("w") as f, ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                process_row,
                row,
                region,
                model_id,
                bench_roots,
                image_max_edge,
                invoker,
                run_index,
            ): row
            for row in shard_rows
        }
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as exc:
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
            if done % 50 == 0 or done == len(shard_rows):
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
            f"Model {model_name!r} not in region catalog. Known: {sorted(j.keys())}"
        )
    return {e["region"]: e["model_id"] for e in j[model_name] if e["status"] == "OK"}


def _plan_bedrock(args) -> List[Tuple[str, str]]:
    region_map = _load_region_map(args.model)
    if args.regions:
        region_map = {r: region_map[r] for r in args.regions if r in region_map}
        if not region_map:
            raise SystemExit(
                f"none of --regions {args.regions} are OK for {args.model}"
            )
        return list(region_map.items())
    if args.multi_region:
        return list(region_map.items())
    for preferred in ("us-east-1", "us-west-2", "us-east-2"):
        if preferred in region_map:
            return [(preferred, region_map[preferred])]
    return [next(iter(region_map.items()))]


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    """Accept either JSON list or JSONL."""
    raw = path.read_text()
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    return [json.loads(l) for l in raw.splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, help="Benchmark JSON or JSONL")
    ap.add_argument(
        "--backend", choices=["bedrock", "vllm"], default="bedrock"
    )
    ap.add_argument("--model", default="Claude Opus 4.6")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument(
        "--bench-root",
        required=True,
        help="Colon- or comma-separated list of bench roots to resolve "
        "relative image_paths / report_paths against.",
    )
    ap.add_argument("--image-max-edge", type=int, default=1568)
    ap.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Threads per region.",
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=16384,
        help="Upper bound on output tokens.",
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--run-index", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--regions", nargs="*", default=None,
        help="Explicit Bedrock region list (overrides default and --multi-region).",
    )
    ap.add_argument(
        "--multi-region",
        action="store_true",
        help="Fan out across every OK region for the model.",
    )
    ap.add_argument("--api-base-url", default=None)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    rows = _load_rows(Path(args.data))
    if args.limit > 0:
        rows = rows[: args.limit]
    bench_roots = _split_bench_roots(args.bench_root)
    if not bench_roots:
        print("WARNING: --bench-root is empty; absolute paths only will resolve",
              file=sys.stderr)

    if args.backend == "bedrock":
        invoker = BedrockInvoker(
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
        )
        targets = _plan_bedrock(args)
    else:
        invoker = VLLMInvoker(
            api_base_url=args.api_base_url,
            api_key=args.api_key,
            model_id=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
        )
        targets = [("local", args.model)]

    print(
        f"[{args.backend}] MM one-shot on {len(rows)} rows, model={args.model!r}, "
        f"targets={[r for r, _ in targets]}, "
        f"bench_roots={[str(p) for p in bench_roots]}",
        flush=True,
    )

    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "region_map.json").write_text(json.dumps(dict(targets), indent=2))

    shards: Dict[str, List[Dict[str, Any]]] = {r: [] for r, _ in targets}
    for i, row in enumerate(rows):
        shards[targets[i % len(targets)][0]].append(row)
    for (region, model_id) in targets:
        print(
            f"  {region:<18} {model_id:<60} {len(shards[region])} rows",
            flush=True,
        )

    threads: List[Thread] = []
    for (region, model_id) in targets:
        out_path = out_root / region / "results.jsonl"
        t = Thread(
            target=run_shard,
            kwargs={
                "shard_rows": shards[region],
                "region": region,
                "model_id": model_id,
                "bench_roots": bench_roots,
                "image_max_edge": args.image_max_edge,
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
