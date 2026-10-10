"""Curated Input / Full-Record Input driver for ClinSeek-Bench-Text (no tools).

Sends one rendered patient-record prompt per row to the model, asks for the
final answer inside an `<answer>...</answer>` block, and wraps the parsed
answer into a synthetic `ehr.finish` tool call so `scoring/score_text.py`
scores the resulting `results.jsonl` exactly like an agentic run.

Paper setting (`--corpus-root`): rows and records come from a corpus built by
`scripts/clean_data/build_evidence_corpus.py`, i.e. the policy-filtered record
under the same strict temporal policy the agent sees.

    --view recent   Curated Input: the source benchmark's evidence package,
                    re-rendered from the policy-filtered record.
    --view full     Full-Record Input: the patient's whole pre-cutoff record,
                    rendered the same way and truncated oldest-first to fit
                    the prompt window (`--max-context-window`, default 100k).

`--data` instead sends each row's own `question` field verbatim. The public
manifest does not carry `question`; restore it with
`scripts/data_build/restore_ehr_bench_inputs.py`. That prompt is the upstream
EHR-Bench serialization, which predates the temporal policy, so it is NOT the
paper's Curated Input setting.

Backends: bedrock (Claude via `--model` friendly name from
`bedrock_region_availability.json`), vertex (Claude via Vertex model id), and
vllm (any OpenAI-compatible endpoint: `--api-base-url`, `--api-key`).

    python clinseekagent/run_text_curated.py \
        --backend vertex --model claude-opus-4-6 \
        --corpus-root /path/to/text_corpus --view recent \
        --output-dir outputs/text_curated_recent
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
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_limits import (  # noqa: E402
    DEFAULT_MAX_CONTEXT_WINDOW, history_budget, resolve_context_window)


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


def build_user_content(row: Dict[str, Any],
                       question: Optional[str] = None) -> str:
    """Prompt for `row`, optionally overriding its rendered `question`.

    The Full Record mode renders a wider timeline into `question` at run time
    (see `clinseekagent/record_context.py`); everything else about the prompt,
    including this trailer, stays identical so the two record widths are the
    only difference between the arms.
    """
    return (row["question"] if question is None else question) + _ANSWER_TRAILER


# ----------------------------------------------------------------------------
# Response parsing  (same shape as run_text._salvage_plain_text_answer)
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
        # Unterminated: take tail after the last <answer...>
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
    s = s.lstrip("-*\u2022").strip()
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
    # out of max_tokens inside the reasoning, so no answer was produced.
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
                # Unterminated: drop from the open tag to end.
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
# Invokers live in curated_invokers.py (shared with the multimodal runner).
# ----------------------------------------------------------------------------

_REGION_AVAILABILITY_PATH = Path(__file__).with_name(
    "bedrock_region_availability.json"
)


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
    render: Optional[Callable[[Dict[str, Any]], Tuple[str, Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    qid = _qid_of(row)
    record_meta: Optional[Dict[str, Any]] = None
    if render is None:
        if not row.get("question"):
            raise ValueError(
                f"row {qid!r} has no `question`. The public ClinSeek-Bench-Text "
                "manifest omits it: run scripts/data_build/"
                "restore_ehr_bench_inputs.py first, or use --corpus-root "
                "(the paper's Curated Input setting).")
        prompt_text = build_user_content(row)
    else:
        question, record_meta = render(row)
        prompt_text = build_user_content(row, question)
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
    if record_meta is not None:
        out["record_context"] = record_meta
    return out


# ----------------------------------------------------------------------------
# Shard runner
# ----------------------------------------------------------------------------


def load_previous(out_root: Path, region_names: List[str]) -> Dict[str, str]:
    """`qid -> results line` for everything already written under `out_root`.

    Reads the merged file and every shard file, later occurrences winning, so
    a rerun is idempotent no matter which of them a previous attempt got as
    far as writing. Only rows that actually completed are kept -- an errored
    row should be retried, that is the point of rerunning.
    """
    seen: Dict[str, str] = {}
    candidates = [out_root / "results.jsonl"]
    candidates += [out_root / r / "results.jsonl" for r in region_names]
    for path in candidates:
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
    invoker,
    output_path: Path,
    concurrency: int,
    run_index: int,
    render=None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    # Append, never truncate: rows already finished by an earlier attempt are
    # filtered out before the shard is built, and a run that dies partway must
    # not throw away what it paid for.
    with output_path.open("a") as f, ThreadPoolExecutor(
        max_workers=concurrency
    ) as pool:
        futures = {
            pool.submit(process_row, row, region, model_id, invoker, run_index,
                        render): row
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
    # fan-out only helps amortize multi-turn tool loops; for single
    # invoke_model calls it mostly just exposes us to whichever region has
    # a wedged/slow shard. Preference order: us-east-1, then first OK.
    for preferred in ("us-east-1", "us-west-2", "us-east-2"):
        if preferred in region_map:
            return [(preferred, region_map[preferred])]
    first = next(iter(region_map.items()))
    return [first]


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
    ap.add_argument("--data", default=None, help="Benchmark JSON file")
    ap.add_argument(
        "--corpus-root", default=None,
        help="Corpus root built by scripts/clean_data/build_evidence_corpus.py "
             "(paper setting). Alternative to --data: rows come from "
             "all_data.jsonl and the record is rendered at the width given by "
             "--view.")
    ap.add_argument(
        "--view", choices=["recent", "full"], default="recent",
        help="Which record width to send. `recent` (Curated Input) is the "
             "corpus' curated selection and ships pre-rendered; `full` "
             "(Full-Record Input) is the whole pre-cutoff history, "
             "re-rendered here and truncated oldest-first to fit.")
    ap.add_argument(
        "--context-window", type=int, default=None,
        help="Override the model's input context window (tokens). Without "
             "it the window comes from clinseekagent/context_limits.py, "
             "which raises on an unknown model rather than guessing.")
    ap.add_argument(
        "--max-context-window", type=int, default=DEFAULT_MAX_CONTEXT_WINDOW,
        help="Hard ceiling on the prompt window, in tokens (default "
             "%(default)s). Clamps whatever the registry or --context-window "
             "resolves to. The model's real ceiling and the one we are "
             "willing to pay for are different numbers; this is the second.")
    ap.add_argument(
        "--context-safety-frac", type=float, default=0.05,
        help="Fraction of the window held back to absorb token-estimate "
             "error (the char/token ratio is accurate to ~5%%).")
    ap.add_argument(
        "--thinking-budget", type=int, default=4096,
        help="Claude extended-thinking budget (models that accept a fixed "
             "budget); also reserved when computing the prompt budget.")
    ap.add_argument(
        "--enable-thinking", dest="enable_thinking", action="store_true",
        help="Enable reasoning (default; paper setting).")
    ap.add_argument(
        "--disable-thinking", dest="enable_thinking", action="store_false",
        help="Disable reasoning.")
    ap.set_defaults(enable_thinking=True)
    ap.add_argument(
        "--reasoning-effort", default="high",
        help="Effort for adaptive-only Claude models (output_config.effort) "
             "and OpenAI reasoning models (reasoning_effort).")
    ap.add_argument(
        "--backend",
        choices=["bedrock", "vertex", "vllm"],
        default="bedrock",
        help="Inference backend; vllm = any OpenAI-compatible endpoint",
    )
    ap.add_argument(
        "--model",
        default="Claude Opus 4.6",
        help="Friendly name from bedrock_region_availability.json (bedrock), "
             "Vertex model id such as claude-opus-4-6 (vertex), or served "
             "model name (vllm)",
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
        default=8192,
        help="Output tokens per call (paper: 8192). Reasoning-heavy models "
        "may need more headroom for the full reasoning + answer.",
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument(
        "--read-timeout",
        type=int,
        default=600,
        help="HTTP read timeout (seconds). Long reasoning outputs over a "
        "100k-token prompt can take several minutes.",
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
        "--qids", nargs="*", default=None,
        help="Run only these qids. For targeted reruns, and for smoke-testing "
             "a specific case (e.g. one with a large history) without having "
             "to reach it by --limit.")
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
        "default because one-shot inference rarely benefits; a wedged "
        "region just blocks the merge. Set this only for very large "
        "benchmarks where per-region throughput is the bottleneck.",
    )
    # Vertex-specific
    ap.add_argument("--vertex-project", default=None,
                    help="GCP project (defaults to ANTHROPIC_VERTEX_PROJECT_ID)")
    ap.add_argument("--vertex-region", default=None,
                    help="Vertex region (defaults to VERTEX_REGION, else us-east5)")
    # OpenAI-compatible (--backend vllm)
    ap.add_argument("--api-base-url", default="http://127.0.0.1:4000/v1",
                    help="OpenAI-compatible base URL (--backend vllm)")
    ap.add_argument("--api-key", default=None,
                    help="API key (--backend vllm; defaults to $VLLM_API_KEY)")
    ap.add_argument("--min-request-interval", type=float, default=0.0,
                    help="Minimum seconds between request starts "
                         "(--backend vllm, rate-limited endpoints)")
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
            text, meta = rc.render_question(
                row, args.corpus_root, args.view, budget)
            meta["context_window"] = window
            return text, meta

        print(f"corpus  {args.corpus_root}  view={args.view}  rows={len(rows)}")
        print(f"window  {window:,} tok -> prompt budget {budget:,} tok "
              f"({render_cfg['context_window_source']})", flush=True)
    else:
        raw = Path(args.data).read_text(encoding="utf-8").strip()
        rows = (json.loads(raw) if raw.startswith("[") else
                [json.loads(line) for line in raw.splitlines() if line.strip()])
        if args.limit > 0:
            rows = rows[: args.limit]
        missing_q = [r.get("qid") for r in rows if not r.get("question")]
        if missing_q:
            raise SystemExit(
                f"{len(missing_q)} row(s) have no `question` (e.g. "
                f"{missing_q[:3]}). The public manifest omits it: restore it "
                "with scripts/data_build/restore_ehr_bench_inputs.py, or use "
                "--corpus-root for the paper's Curated Input setting.")

    if args.qids:
        wanted = set(args.qids)
        rows = [r for r in rows if str(r.get("qid") or r.get("id")) in wanted]
        missing = wanted - {str(r.get("qid") or r.get("id")) for r in rows}
        if missing:
            raise SystemExit(f"--qids not found in the data: {sorted(missing)}")

    # Build invoker + fan-out plan
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
        plan: List[Tuple[str, str]] = _plan_bedrock(args)
        targets = plan
    elif args.backend == "vertex":
        project = (args.vertex_project or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
                   or os.environ.get("GOOGLE_CLOUD_PROJECT"))
        if not project:
            raise SystemExit(
                "vertex backend needs a project: pass --vertex-project or set "
                "ANTHROPIC_VERTEX_PROJECT_ID"
            )
        region = (args.vertex_region or os.environ.get("VERTEX_REGION")
                  or os.environ.get("CLOUD_ML_REGION") or "us-east5")
        invoker = VertexInvoker(
            project_id=project,
            region=region,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            max_retries=args.max_retries,
            read_timeout=args.read_timeout,
            **reasoning,
        )
        # --model is a Vertex model id here, not a friendly name.
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
        # One "region" = the endpoint; kept so the shard plumbing is shared.
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
    run_config = {
        "backend": args.backend, "model": args.model,
        "max_tokens": args.max_tokens, "temperature": args.temperature,
        "thinking_budget": args.thinking_budget,
        "enable_thinking": args.enable_thinking,
        "reasoning_effort": args.reasoning_effort,
        "run_index": args.run_index, "concurrency": args.concurrency,
        "data": args.data, "rows": len(rows), **render_cfg,
    }
    (out_root / "run_config.json").write_text(json.dumps(run_config, indent=2))

    # Resume: skip rows an earlier attempt already completed. A Full Record
    # row costs up to 170k input tokens, so re-spending a finished arm because
    # the process died at row 1700 is a real bill, not a nuisance.
    previous = load_previous(out_root, [r for r, _ in targets])
    if previous:
        before = len(rows)
        rows = [r for r in rows if _qid_of(r) not in previous]
        print(f"resume: {len(previous)} row(s) already complete, "
              f"{len(rows)}/{before} to run", flush=True)

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
                "render": render,
            },
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    # Merge. Deduplicated by qid with the newest write winning, so a resumed
    # run produces one row per question rather than stacking attempts.
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

    # Token usage, in the same shape as the agent driver's usage.json.
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
