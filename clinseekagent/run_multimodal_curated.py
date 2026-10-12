"""Curated Input / Full-Record Input driver for ClinSeek-Bench-MM (no tools).

Mirror of `run_text_curated.py` for the multimodal benchmark: sends the row's
rendered prompt (`input_text`: task, question, and an `<ehr_context>` block)
plus up to four attached CXR images (longest edge 1,568 px) and the linked
radiology reports (up to 4,000 characters each) in a single call, asks for the
final answer inside an `<answer>...</answer>` block, and wraps the response
into a synthetic `ehr.finish` tool call so `scoring/score_multimodal.py`
scores the resulting `results.jsonl` unchanged.

Paper setting (`--corpus-root`): the `<ehr_context>` block is re-rendered from
the policy-filtered record of a corpus built by
`scripts/clean_data/build_evidence_corpus.py` (`--view recent` = Curated
Input, `--view full` = Full-Record Input, truncated oldest-first to fit).

`--data` sends each row's own `input_text`. The public manifest does not carry
it; render it with `scripts/data_build/materialize_multimodal_strict_manifest.py`.

    python clinseekagent/run_multimodal_curated.py \\
        --backend vertex --model claude-opus-4-6 \\
        --corpus-root /path/to/mm_corpus --view recent \\
        --bench-root /path/to/ClinSeek-MM-Bench/data/mm_bench \\
        --output-dir outputs/mm_curated_recent
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_limits import (  # noqa: E402
    DEFAULT_MAX_CONTEXT_WINDOW, history_budget, resolve_context_window)

from multimodal_manifest_policy import validate_curated_manifest


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
# Image/report attachment  (ported from run_multimodal.py)
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
# Must match run_multimodal.py's cap. These two numbers feed the same
# comparison: if the curated arm inlines five times more of each report than
# the agentic arm does, a difference between them is partly a difference in
# how much radiology text the model was handed.
_MAX_REPORT_CHARS = 4000


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
    input_text: Optional[str] = None,
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
    if input_text is not None:
        use_input_text, base_text = True, input_text
    else:
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

    # Reports. Inlined for every row, including rows that carry `input_text`.
    # The earlier guard assumed `input_text` already had them; it does not --
    # 0 of 989 corpus prompts contain a report body, while 469 rows link one.
    # Skipping them here handed the curated arms strictly less evidence than
    # the agentic arms on those rows, which is not an ablation, just a gap.
    resolved_reports: List[str] = []
    report_texts: List[str] = []
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

    # The `<run_context>` preamble is emitted for every row, `input_text` or
    # not, because the agentic runner always emits it. Whether the model is
    # told how many images are attached should not depend on which arm it is
    # in.
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
# Answer extraction  (copied verbatim from run_text_curated.py)
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
# Invokers live in curated_invokers.py (shared with the text runner).
# ----------------------------------------------------------------------------

_REGION_AVAILABILITY_PATH = Path(__file__).with_name(
    "bedrock_region_availability.json"
)


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
    render: Optional[Callable[[Dict[str, Any]], Tuple[str, Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    qid = _qid_of(row)
    record_meta: Optional[Dict[str, Any]] = None
    rendered: Optional[str] = None
    if render is not None:
        rendered, record_meta = render(row)
    user_text, image_b64s, _report_texts, summary = build_mm_inputs(
        row, bench_roots, image_max_edge, input_text=rendered
    )
    text, status = invoker.invoke(region, model_id, user_text, images=image_b64s)

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
            **({"record_context": record_meta} if record_meta else {}),
        }
    )
    return out


# ----------------------------------------------------------------------------
# Shard runner
# ----------------------------------------------------------------------------


def load_previous(out_root: Path, region_names: List[str]) -> Dict[str, str]:
    """`qid -> results line` for everything already written under `out_root`.

    Reads the merged file and every shard file, later occurrences winning, so
    a rerun is idempotent regardless of how far a previous attempt got. Only
    completed rows are kept: an errored row should be retried.
    """
    seen: Dict[str, str] = {}
    for path in ([out_root / "results.jsonl"]
                 + [out_root / r / "results.jsonl" for r in region_names]):
        if not path.exists():
            continue
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                qid = row.get("qid")
                if qid and row.get("completed"):
                    seen[str(qid)] = line
    return seen


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
    render=None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    # Append, never truncate: completed rows are filtered out before the shard
    # is built, so a run that dies partway keeps what it paid for.
    with output_path.open("a") as f, ThreadPoolExecutor(max_workers=concurrency) as pool:
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
                render,
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
    catalog = json.loads(_REGION_AVAILABILITY_PATH.read_text())
    if args.model not in catalog:
        # A raw Bedrock model id (e.g. us.anthropic.claude-opus-4-6-v1) rather
        # than a catalog friendly name: run it in one region.
        region = ((args.regions or [None])[0]
                  or os.environ.get("BEDROCK_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
        return [(region, args.model)]
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


def merge_prior_usage(out_root, usage: dict) -> dict:
    """Fold an existing usage.json into `usage`, so a resumed run reports the
    whole arm's spend rather than the last process's slice.

    Without this the file is overwritten each time, and a run that resumed
    several times would report only the last slice of its token usage.
    """
    import json as _json
    from pathlib import Path as _Path
    prior_path = _Path(out_root) / "usage.json"
    merged = dict(usage)
    if prior_path.exists():
        try:
            prior = _json.loads(prior_path.read_text())
        except (ValueError, OSError):
            return merged
        for key, value in prior.items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
        # Record that this total spans more than one process, so a reader can
        # tell an accumulated figure from a single-run one.
        merged["runs_accumulated"] = int(prior.get("runs_accumulated", 1)) + 1
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=None, help="Benchmark JSON or JSONL")
    ap.add_argument(
        "--corpus-root", default=None,
        help="Corpus root built by scripts/clean_data/build_evidence_corpus.py "
             "(paper setting). Alternative to --data: rows come from "
             "all_data.jsonl and the <ehr_context> block is re-rendered at the "
             "width given by --view.")
    ap.add_argument(
        "--view", choices=["recent", "full"], default="recent",
        help="Which record width to send: `recent` (Curated Input) or `full` "
             "(Full-Record Input). Both are re-rendered here from the corpus "
             "CSVs.")
    ap.add_argument("--context-window", type=int, default=None,
                    help="Override the model's input context window (tokens).")
    ap.add_argument(
        "--max-context-window", type=int, default=DEFAULT_MAX_CONTEXT_WINDOW,
        help="Hard ceiling on the prompt window, in tokens (default "
             "%(default)s). Clamps whatever the registry or --context-window "
             "resolves to. The model's real ceiling and the one we are "
             "willing to pay for are different numbers; this is the second.")
    ap.add_argument("--context-safety-frac", type=float, default=0.05)
    ap.add_argument("--thinking-budget", type=int, default=4096,
                    help="Claude extended-thinking budget (models that accept "
                         "a fixed budget); also reserved in the prompt budget.")
    ap.add_argument("--enable-thinking", dest="enable_thinking",
                    action="store_true", help="Enable reasoning (default).")
    ap.add_argument("--disable-thinking", dest="enable_thinking",
                    action="store_false", help="Disable reasoning.")
    ap.set_defaults(enable_thinking=True)
    ap.add_argument("--reasoning-effort", default="high",
                    help="Effort for adaptive-only Claude models and OpenAI "
                         "reasoning models.")
    ap.add_argument(
        "--backend", choices=["bedrock", "vertex", "vllm"],
        default="bedrock",
        help="Inference backend; vllm = any OpenAI-compatible endpoint "
             "serving a vision-capable model",
    )
    ap.add_argument("--model", default="Claude Opus 4.6",
                    help="Bedrock friendly name, Vertex model id, or served "
                         "model name (vllm)")
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
        default=8192,
        help="Output tokens per call (paper: 8192).",
    )
    ap.add_argument("--read-timeout", type=int, default=600,
                    help="HTTP read timeout (seconds).")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--run-index", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--qids", nargs="*", default=None,
        help="Run only these qids. For targeted reruns, and for smoke-testing "
             "a specific case (e.g. one with a large history) without having "
             "to reach it by --limit.")
    ap.add_argument(
        "--regions", nargs="*", default=None,
        help="Explicit Bedrock region list (overrides default and --multi-region).",
    )
    ap.add_argument(
        "--multi-region",
        action="store_true",
        help="Fan out across every OK region for the model.",
    )
    ap.add_argument("--vertex-project", default=None,
                    help="GCP project (defaults to ANTHROPIC_VERTEX_PROJECT_ID)")
    ap.add_argument("--vertex-region", default=None,
                    help="Vertex region (defaults to VERTEX_REGION, else us-east5)")
    ap.add_argument("--api-base-url", default="http://127.0.0.1:4000/v1",
                    help="OpenAI-compatible base URL (--backend vllm)")
    ap.add_argument("--api-key", default=None,
                    help="API key (--backend vllm; defaults to $VLLM_API_KEY)")
    ap.add_argument("--min-request-interval", type=float, default=0.0,
                    help="Minimum seconds between request starts (--backend vllm)")
    args = ap.parse_args()

    if bool(args.data) == bool(args.corpus_root):
        raise SystemExit("pass exactly one of --data or --corpus-root")

    render = None
    render_cfg: Dict[str, Any] = {}
    if args.corpus_root:
        import record_context as rc

        rows = rc.load_corpus_rows(args.corpus_root, limit=args.limit)
        window = resolve_context_window(
            args.model, args.context_window, cap=args.max_context_window)
        budget = history_budget(
            window, max_tokens=args.max_tokens,
            thinking_budget=args.thinking_budget,
            safety_frac=args.context_safety_frac)
        render_cfg = {
            "corpus_root": str(args.corpus_root), "view": args.view,
            "context_window": window, "prompt_budget_tokens": budget,
            "max_context_window": args.max_context_window,
            "context_window_source": ("--context-window"
                                      if args.context_window else "registry"),
            "render_version": rc.RENDER_VERSION,
        }

        def render(row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            text, meta = rc.render_input_text(
                row, args.corpus_root, args.view, budget)
            meta["context_window"] = window
            return text, meta

        print(f"corpus  {args.corpus_root}  view={args.view}  rows={len(rows)}")
        print(f"window  {window:,} tok -> prompt budget {budget:,} tok "
              f"({render_cfg['context_window_source']})", flush=True)
    else:
        rows = _load_rows(Path(args.data))
        if args.limit > 0:
            rows = rows[: args.limit]
        no_context = [
            r.get("qid") for r in rows
            if str(r.get("source_benchmark") or "").lower() in {"ehrxqa", "medmod"}
            and not r.get("input_text")
        ]
        if no_context:
            raise SystemExit(
                f"{len(no_context)} row(s) have no `input_text` (e.g. "
                f"{no_context[:3]}); sending `question` alone would silently "
                "drop the EHR context. The public manifest omits it: render it "
                "with scripts/data_build/materialize_multimodal_strict_manifest.py, "
                "or use --corpus-root for the paper's Curated Input setting.")

    if args.qids:
        wanted = set(args.qids)
        rows = [r for r in rows if str(r.get("qid") or r.get("id")) in wanted]
        missing = wanted - {str(r.get("qid") or r.get("id")) for r in rows}
        if missing:
            raise SystemExit(f"--qids not found in the data: {sorted(missing)}")
    validate_curated_manifest(rows)
    bench_roots = _split_bench_roots(args.bench_root)
    if not bench_roots:
        print("WARNING: --bench-root is empty; absolute paths only will resolve",
              file=sys.stderr)

    from curated_invokers import BedrockInvoker, OpenAICompatInvoker, VertexInvoker

    reasoning = dict(enable_thinking=args.enable_thinking,
                     thinking_budget=args.thinking_budget,
                     reasoning_effort=args.reasoning_effort)
    if args.backend == "bedrock":
        invoker = BedrockInvoker(
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
            read_timeout=args.read_timeout,
            **reasoning,
        )
        targets = _plan_bedrock(args)
    elif args.backend == "vertex":
        project = (args.vertex_project
                   or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
                   or os.environ.get("GOOGLE_CLOUD_PROJECT"))
        if not project:
            raise SystemExit(
                "vertex backend needs a project: pass --vertex-project or set "
                "ANTHROPIC_VERTEX_PROJECT_ID")
        region = (args.vertex_region or os.environ.get("VERTEX_REGION")
                  or os.environ.get("CLOUD_ML_REGION") or "us-east5")
        invoker = VertexInvoker(
            project_id=project, region=region,
            max_tokens=args.max_tokens, temperature=args.temperature,
            max_retries=args.max_retries, read_timeout=args.read_timeout,
            **reasoning)
        targets = [(region, args.model)]
    else:
        invoker = OpenAICompatInvoker(
            base_url=args.api_base_url,
            api_key=args.api_key or os.environ.get("VLLM_API_KEY"),
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
            read_timeout=args.read_timeout,
            enable_thinking=args.enable_thinking,
            reasoning_effort=args.reasoning_effort,
            min_request_interval=args.min_request_interval,
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
    (out_root / "run_config.json").write_text(json.dumps({
        "backend": args.backend, "model": args.model,
        "max_tokens": args.max_tokens, "temperature": args.temperature,
        "thinking_budget": args.thinking_budget,
        "enable_thinking": args.enable_thinking,
        "reasoning_effort": args.reasoning_effort,
        "run_index": args.run_index, "concurrency": args.concurrency,
        "data": args.data, "rows": len(rows),
        "bench_root": [str(p) for p in bench_roots],
        "image_max_edge": args.image_max_edge,
        "max_report_chars": _MAX_REPORT_CHARS,
        **render_cfg,
    }, indent=2))

    previous = load_previous(out_root, [r for r, _ in targets])
    if previous:
        before = len(rows)
        rows = [r for r in rows if _qid_of(r) not in previous]
        print(f"resume: {len(previous)} row(s) already complete, "
              f"{len(rows)}/{before} to run", flush=True)

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
                "render": render,
            },
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    # Deduplicated by qid, newest write winning, so a resumed run produces one
    # row per question rather than stacking attempts.
    merged = out_root / "results.jsonl"
    by_qid: Dict[str, str] = dict(previous)
    for (region, _) in targets:
        shard_path = out_root / region / "results.jsonl"
        if not shard_path.exists():
            continue
        with shard_path.open() as sf:
            for line in sf:
                line = line.strip()
                if not line:
                    continue
                try:
                    qid = json.loads(line).get("qid")
                except json.JSONDecodeError:
                    continue
                if qid:
                    by_qid[str(qid)] = line
    with merged.open("w") as mf:
        for line in by_qid.values():
            mf.write(line + "\n")
    n = len(by_qid)
    print(f"\nMerged {n} rows -> {merged}", flush=True)

    usage = getattr(invoker, "token_usage", None)
    if usage:
        u = merge_prior_usage(out_root, dict(usage))
        print(f"token usage: {u['requests']} requests, "
              f"in={u['input_tokens']:,}, out={u['output_tokens']:,}", flush=True)
        (out_root / "usage.json").write_text(json.dumps(
            {"model": targets[0][1], "backend": args.backend, **u}, indent=2))
        print(f"Token usage written to {out_root / 'usage.json'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
