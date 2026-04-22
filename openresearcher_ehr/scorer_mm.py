"""Score a multimodal-pipeline results.jsonl against ground-truth labels.

Routing by gold label shape:
  - len(gold) == 1  →  LLM judge (Claude Sonnet 4.6 on Bedrock). Picks one of
                       six templates based on gold content + task: yesno,
                       count, date_time, id, label_name, generic_string.
  - len(gold) >= 2  →  Rule-based set F1 / precision / recall / subset
                       accuracy over the normalized `name` strings.

Outputs:
  <output_dir>/scored.jsonl     One record per sample with fields:
      qid, task, scope, len_gold, judge_subtype (if len=1),
      prediction, gold_names, correct (bool for len=1),
      precision, recall, f1, subset_match (for len>=2), status.
  <output_dir>/summary.json     Aggregate stats per task + overall.
  <output_dir>/summary.md       Human-readable companion.

The scorer is driven by Bedrock creds (same auth as deploy_agent_mm.py):
  - AWS_BEARER_TOKEN_BEDROCK or BEDROCK_API_KEY (preferred), or
  - standard boto3 credential chain.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import bedrock_generator as _bgen
# Runtime shim: bedrock_generator._chat_completion_anthropic references a
# `use_reasoning_content` variable that is a parameter of chat_completion but
# was never forwarded to its module scope. Inject so NameErrors don't kill
# the judge path. Same fix as deploy_agent_mm.py.
_bgen.__dict__.setdefault("use_reasoning_content", True)

from bedrock_generator import BedrockAsyncGenerator  # noqa: E402
from deploy_agent import configure_bedrock_auth, resolve_bedrock_model_id  # noqa: E402


# ---------------------------------------------------------------------------
# Prediction extraction
# ---------------------------------------------------------------------------

FINISH_NAMES = {"ehr.finish", "finish"}


def extract_prediction(row: Dict[str, Any]) -> Tuple[Optional[Any], str]:
    """Return the `response` arg from the last `ehr.finish` call.

    Returns (prediction, note). `note` is a short reason string when we
    couldn't extract cleanly ("no_finish_call", "bad_json_args", ...).
    """
    msgs = row.get("messages") or []
    last_args_raw: Optional[str] = None
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or (tc.get("function") or {}).get("name")
            if name in FINISH_NAMES:
                last_args_raw = (
                    tc.get("arguments")
                    or (tc.get("function") or {}).get("arguments")
                )
                break
        if last_args_raw is not None:
            break

    if last_args_raw is None:
        return None, "no_finish_call"

    if isinstance(last_args_raw, dict):
        args = last_args_raw
    else:
        try:
            args = json.loads(last_args_raw)
        except Exception:
            return last_args_raw, "bad_json_args"

    if isinstance(args, dict) and "response" in args:
        return args["response"], "ok"
    return args, "no_response_key"


def _normalize_name(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip().lower()


def pred_to_strings(pred: Any) -> List[str]:
    """Flatten whatever the model returned into a list of strings for set ops."""
    if pred is None:
        return []
    if isinstance(pred, list):
        out: List[str] = []
        for x in pred:
            if isinstance(x, dict):
                val = x.get("name") or x.get("value") or x.get("text") or x.get("answer")
                if val is None:
                    val = json.dumps(x, ensure_ascii=False)
            else:
                val = x
            out.append(str(val))
        return out
    if isinstance(pred, str):
        # Model occasionally returns a single string instead of a list.
        return [pred]
    return [str(pred)]


def gold_to_strings(gold: Any) -> List[str]:
    if not isinstance(gold, list):
        return [] if gold is None else [str(gold)]
    out: List[str] = []
    for x in gold:
        if isinstance(x, dict):
            name = x.get("name")
            if name is None:
                name = x.get("value")
            out.append("" if name is None else str(name))
        else:
            out.append(str(x))
    return out


# ---------------------------------------------------------------------------
# Judge-subtype classifier
# ---------------------------------------------------------------------------

YESNO_SET = {"yes", "no", "true", "false", "0", "1"}
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")
ID_RE = re.compile(r"^\d{6,10}$")
POLAR_PREFIXES = (
    "is ", "are ", "does ", "do ", "did ", "was ", "were ", "has ", "have ",
    "can ", "could ", "should ", "will ", "would ",
)
COUNT_KEYWORDS = (
    "how many", "number of", "count of", "count the", "total number",
    "how often", "what is the count",
)


def classify_subtype(gold_name: str, question: str, task: str) -> str:
    name_l = gold_name.strip().lower()
    q_l = question.strip().lower() if question else ""

    if name_l in YESNO_SET:
        return "yesno"
    if DATE_RE.match(gold_name.strip()):
        return "date_time"
    if task == "ehrxqa_table" and ID_RE.match(gold_name.strip()):
        return "id"
    if INT_RE.match(gold_name.strip()) or FLOAT_RE.match(gold_name.strip()):
        if any(k in q_l for k in COUNT_KEYWORDS):
            return "count"
        return "count"
    # Heuristic: polar questions with free-text gold → still yes/no vibe
    if any(q_l.startswith(p) for p in POLAR_PREFIXES) and len(name_l.split()) <= 3:
        return "label_name"
    return "label_name" if task in {"medmod_radiology", "medmod_phenotyping",
                                     "ehrxqa_image"} else "generic_string"


# ---------------------------------------------------------------------------
# Judge prompts
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are an expert medical judge evaluating whether a clinical agent's "
    "final answer matches a ground-truth label. Respond ONLY with a compact "
    "JSON object: {\"match\": true|false, \"reason\": \"<= 25 words\"}. "
    "Never output anything else — no markdown, no prose, no code fences."
)


def _base_context(row: Dict[str, Any]) -> str:
    q = (row.get("question") or "").strip()
    # The agent's prompt template wraps the question in tags; strip the biggest
    # block to save tokens while keeping the actionable text.
    if q.startswith("<task_instruction>"):
        q = q.split("</task_instruction>", 1)[-1].strip()
    # Don't ship the entire EHR dump — limit length.
    if len(q) > 1200:
        q = q[:1200] + "...(truncated)"
    return q


def build_judge_prompt(
    subtype: str,
    row: Dict[str, Any],
    gold_name: str,
    prediction_str: str,
) -> str:
    q = _base_context(row)
    common_header = (
        f"Question: {q}\n"
        f"Ground truth: {gold_name}\n"
        f"Agent answer: {prediction_str}\n\n"
    )

    if subtype == "yesno":
        return common_header + (
            "The ground truth encodes a yes/no answer (1/yes/true, 0/no/false).\n"
            "Decide if the agent answer expresses the SAME polarity as the\n"
            "ground truth. Phrases like 'No, ...' start negative; 'Yes, ...'\n"
            "start positive. If the agent hedges but the dominant polarity is\n"
            "clear, use that. Return only the JSON verdict."
        )

    if subtype == "count":
        return common_header + (
            "The ground truth is a number (a count, measurement, or rank).\n"
            "Decide if the agent answer reports the SAME numeric value. Numbers\n"
            "expressed as words (e.g. 'two') match digits (e.g. '2'). Small\n"
            "typographic differences (units, thousands separators, trailing\n"
            "zeros) are OK if the value is the same. Return only JSON."
        )

    if subtype == "date_time":
        return common_header + (
            "The ground truth is a date (YYYY-MM-DD) or datetime.\n"
            "Decide if the agent's date matches the ground truth exactly.\n"
            "If the ground truth includes a time, require matching time as\n"
            "well (minute precision). Different timezones DO NOT match.\n"
            "Return only JSON."
        )

    if subtype == "id":
        return common_header + (
            "The ground truth is a numeric identifier (MIMIC subject/hadm/stay\n"
            "id). Decide if the agent's answer is EXACTLY the same id. Any\n"
            "other id, or a descriptive phrase, is a mismatch. Return only JSON."
        )

    if subtype == "label_name":
        return common_header + (
            "The ground truth is a clinical entity (radiographic finding,\n"
            "phenotype, medication, device, etc.). Decide if the agent's\n"
            "answer names the SAME entity. Accept synonyms, abbreviations, and\n"
            "minor wording variation. Do NOT accept partial or adjacent\n"
            "concepts (e.g. 'pneumonia' != 'atelectasis'). Return only JSON."
        )

    # generic_string fallback
    return common_header + (
        "Decide if the agent answer is semantically equivalent to the ground\n"
        "truth string. Minor wording differences are fine; adding unrelated\n"
        "content is not. Return only JSON."
    )


# ---------------------------------------------------------------------------
# Judge wrapper
# ---------------------------------------------------------------------------

_JUDGE_JSON_RE = re.compile(r"\{[^{}]*\"match\"\s*:\s*(?:true|false)[^{}]*\}", re.DOTALL)


async def judge_once(
    gen: BedrockAsyncGenerator,
    prompt: str,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Call Sonnet and parse a {match, reason} JSON. Returns {} on total failure."""
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    last_raw = ""
    for attempt in range(max_retries):
        try:
            resp = await gen.chat_completion(
                messages=messages,
                tools=None,
                tool_choice="none",
                temperature=0.0,
                max_tokens=120,
            )
        except Exception as exc:
            last_raw = f"exception: {exc}"
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        # chat_completion returns an OpenAI-like dict
        content = ""
        try:
            content = resp["choices"][0]["message"].get("content") or ""
        except Exception:
            content = json.dumps(resp)[:400]
        last_raw = content
        # Direct parse first; fall back to regex over loose output.
        try:
            obj = json.loads(content.strip())
            if isinstance(obj, dict) and "match" in obj:
                return {
                    "match": bool(obj["match"]),
                    "reason": str(obj.get("reason", ""))[:200],
                    "raw": content[:500],
                }
        except Exception:
            pass
        m = _JUDGE_JSON_RE.search(content)
        if m:
            try:
                obj = json.loads(m.group(0))
                if "match" in obj:
                    return {
                        "match": bool(obj["match"]),
                        "reason": str(obj.get("reason", ""))[:200],
                        "raw": content[:500],
                    }
            except Exception:
                pass
        await asyncio.sleep(0.5 * (attempt + 1))

    return {"match": False, "reason": "judge_unparsed", "raw": last_raw[:500]}


# ---------------------------------------------------------------------------
# Set-F1 scoring
# ---------------------------------------------------------------------------

def set_f1(pred: List[str], gold: List[str]) -> Dict[str, float]:
    pred_set = {_normalize_name(x) for x in pred if _normalize_name(x)}
    gold_set = {_normalize_name(x) for x in gold if _normalize_name(x)}
    tp = len(pred_set & gold_set)
    precision = tp / len(pred_set) if pred_set else 0.0
    recall = tp / len(gold_set) if gold_set else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    # "Accuracy" here = element-wise accuracy on the union (Jaccard-style)
    union = pred_set | gold_set
    acc = (tp / len(union)) if union else 0.0
    subset_match = 1.0 if pred_set == gold_set else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": acc,
        "subset_match": subset_match,
        "tp": tp,
        "n_pred": len(pred_set),
        "n_gold": len(gold_set),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_results(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


async def _worker(
    sem: asyncio.Semaphore,
    gen: BedrockAsyncGenerator,
    region_tag: str,
    idx: int,
    total: int,
    row: Dict[str, Any],
    subtype: str,
    gold_name: str,
    prediction_str: str,
    progress_every: int,
) -> Dict[str, Any]:
    async with sem:
        prompt = build_judge_prompt(subtype, row, gold_name, prediction_str)
        t0 = time.monotonic()
        verdict = await judge_once(gen, prompt)
        elapsed = time.monotonic() - t0
        if idx % progress_every == 0 or idx == total - 1:
            print(
                f"  [judge] {idx + 1}/{total} (region={region_tag}, "
                f"subtype={subtype}, {elapsed:.1f}s): match={verdict.get('match')}",
                flush=True,
            )
        return verdict


_REGION_PREFIX_MAP = {
    # Anthropic cross-region inference profile prefixes
    "us-east-1": "us", "us-east-2": "us", "us-west-2": "us",
    "eu-central-1": "eu", "eu-west-1": "eu", "eu-west-3": "eu",
    "ap-northeast-1": "ap", "ap-southeast-2": "ap",
}


def _anthropic_id_for_region(model_id: str, region: str) -> str:
    """Swap `us.` / `eu.` / `global.` prefix of an Anthropic model id so it
    resolves to the inference profile available in `region`.

    Non-Anthropic ids pass through unchanged.
    """
    target = _REGION_PREFIX_MAP.get(region, "us")
    for old in ("us.", "eu.", "ap.", "global."):
        if model_id.startswith(old + "anthropic."):
            return f"{target}.{model_id.split('.', 1)[1]}"
    return model_id


async def score_file(
    results_path: Path,
    out_dir: Path,
    bedrock_model_id: str,
    bedrock_region: str,
    bedrock_regions: Optional[List[str]],
    concurrency: int,
    max_rows: Optional[int],
    resume: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    scored_path = out_dir / "scored.jsonl"
    summary_path = out_dir / "summary.json"
    summary_md = out_dir / "summary.md"

    already: Dict[str, Dict[str, Any]] = {}
    if resume and scored_path.exists():
        with scored_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                key = (r.get("qid"), r.get("run_index"))
                already[key] = r
        print(f"[resume] {len(already)} scored rows already in {scored_path}", flush=True)

    rows: List[Dict[str, Any]] = list(load_results(results_path))
    if max_rows:
        rows = rows[:max_rows]
    print(f"[scorer] {len(rows)} rows from {results_path}", flush=True)

    # ---- First pass: extract prediction + route ---------------------------
    judge_tasks: List[Tuple[int, Dict[str, Any], str, str, str]] = []  # idx, row, subtype, gold_name, pred_str
    prepared: List[Dict[str, Any]] = []

    skipped_empty = 0
    for idx, row in enumerate(rows):
        key = (row.get("qid"), row.get("run_index"))
        if key in already:
            prepared.append(already[key])
            continue
        gold = row.get("label") or []
        gold_names = [g for g in gold_to_strings(gold) if g.strip()]
        if not gold_names:
            # Empty-label rows were supposed to be filtered upstream; skip
            # defensively with a logged marker rather than scoring them.
            skipped_empty += 1
            continue
        prediction, note = extract_prediction(row)
        pred_strs = pred_to_strings(prediction)
        pred_flat = " | ".join(s.strip() for s in pred_strs if s) if pred_strs else ""

        base = {
            "qid": row.get("qid"),
            "run_index": row.get("run_index"),
            "task": row.get("task"),
            "scope": row.get("scope"),
            "source_benchmark": row.get("source_benchmark"),
            "len_gold": len(gold_names),
            "gold_names": gold_names,
            "prediction_raw": prediction,
            "prediction_strs": pred_strs,
            "prediction_flat": pred_flat,
            "note": note,
            "status": row.get("status"),
        }

        if note != "ok" or prediction is None:
            base.update({
                "route": "incomplete",
                "correct": False,
                "precision": 0.0,
                "recall": 0.0,
                "f1": 0.0,
                "accuracy": 0.0,
                "subset_match": 0.0,
            })
            prepared.append(base)
            continue

        if len(gold_names) >= 2:
            base["route"] = "set_f1"
            base.update(set_f1(pred_strs, gold_names))
            prepared.append(base)
            continue

        # len == 1 → judge
        gold_name = gold_names[0] if gold_names else ""
        subtype = classify_subtype(gold_name, row.get("question") or "", row.get("task") or "")
        base["route"] = "llm_judge"
        base["judge_subtype"] = subtype
        base["_gold_name"] = gold_name  # transient, removed later
        prepared.append(base)
        judge_tasks.append((idx, row, subtype, gold_name, pred_flat or "(empty response)"))

    print(
        f"[scorer] routing: "
        f"incomplete={sum(1 for r in prepared if r.get('route') == 'incomplete')}, "
        f"set_f1={sum(1 for r in prepared if r.get('route') == 'set_f1')}, "
        f"llm_judge={len(judge_tasks)}, "
        f"skipped_empty_gold={skipped_empty}",
        flush=True,
    )

    # ---- Second pass: run judge in parallel -------------------------------
    if judge_tasks:
        configure_bedrock_auth(None)  # propagate env
        # Build a pool of generators — one per region. Each gets its own
        # semaphore so the caller's `--concurrency` is the per-region cap;
        # global concurrency = concurrency × len(regions).
        regions = bedrock_regions if bedrock_regions else [bedrock_region]
        pool = []
        for region in regions:
            # Anthropic requires a region-prefixed id (us. / eu. / global.).
            mid = resolve_bedrock_model_id(bedrock_model_id)
            # If the caller passed a `global.` alias, it still works anywhere;
            # otherwise for multi-region we switch the `us.` / `eu.` prefix.
            region_mid = _anthropic_id_for_region(mid, region)
            gen = BedrockAsyncGenerator(
                model_id=region_mid,
                region_name=region,
                max_tokens_default=200,
                enable_thinking=False,
            )
            pool.append((region, gen, asyncio.Semaphore(concurrency)))

        progress_every = max(10, len(judge_tasks) // 20)

        coros = []
        for i, (idx, row, subtype, gold_name, pred_str) in enumerate(judge_tasks):
            region, gen, sem = pool[i % len(pool)]
            coros.append(
                _worker(
                    sem,
                    gen,
                    region,
                    i,
                    len(judge_tasks),
                    row,
                    subtype,
                    gold_name,
                    pred_str,
                    progress_every,
                )
            )
        print(
            f"[scorer] judge pool: {len(pool)} regions "
            f"({[r for r, _, _ in pool]}), per-region concurrency={concurrency}, "
            f"global concurrency={concurrency * len(pool)}",
            flush=True,
        )
        verdicts = await asyncio.gather(*coros)

        # Splice verdicts back into the corresponding prepared[] entries.
        # judge_tasks[j] -> idx_in_rows -> prepared index matches rows index
        # prepared list is parallel to rows after the first pass.
        for (idx, _, subtype, gold_name, _), verdict in zip(judge_tasks, verdicts):
            target = next(
                (r for r in prepared
                 if r.get("qid") == rows[idx].get("qid")
                 and r.get("run_index") == rows[idx].get("run_index")
                 and r.get("route") == "llm_judge"),
                None,
            )
            if target is None:
                continue
            target["judge_match"] = bool(verdict.get("match"))
            target["judge_reason"] = verdict.get("reason", "")
            target["judge_raw"] = verdict.get("raw", "")
            target["correct"] = target["judge_match"]
            # Fill set-style fields for uniform aggregation.
            target["precision"] = 1.0 if target["correct"] else 0.0
            target["recall"] = 1.0 if target["correct"] else 0.0
            target["f1"] = 1.0 if target["correct"] else 0.0
            target["accuracy"] = 1.0 if target["correct"] else 0.0
            target["subset_match"] = 1.0 if target["correct"] else 0.0

    # Strip transient helpers.
    for r in prepared:
        r.pop("_gold_name", None)

    # ---- Write scored + aggregates ---------------------------------------
    with scored_path.open("w") as f:
        for r in prepared:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[scorer] wrote {scored_path}", flush=True)

    summary = aggregate(prepared)
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)
    summary_md.write_text(render_markdown(summary, results_path))
    print(f"[scorer] wrote {summary_path}")
    print(f"[scorer] wrote {summary_md}")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(scored: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _avg(values: List[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    per_task_single: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0, "correct": 0, "incomplete": 0,
        "by_subtype": Counter(),
        "by_subtype_correct": Counter(),
    })
    per_task_multi: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0, "incomplete": 0,
        "precision": [], "recall": [], "f1": [], "accuracy": [], "subset_match": [],
    })
    overall = {
        "total": len(scored),
        "incomplete": 0,
        "len1": {"count": 0, "correct": 0},
        "len2plus": {"count": 0, "precision": [], "recall": [], "f1": [],
                      "accuracy": [], "subset_match": []},
    }

    for r in scored:
        task = r.get("task") or "?"
        route = r.get("route")
        if route == "incomplete":
            overall["incomplete"] += 1
            if r.get("len_gold", 0) >= 2:
                per_task_multi[task]["count"] += 1
                per_task_multi[task]["incomplete"] += 1
                per_task_multi[task]["precision"].append(0.0)
                per_task_multi[task]["recall"].append(0.0)
                per_task_multi[task]["f1"].append(0.0)
                per_task_multi[task]["accuracy"].append(0.0)
                per_task_multi[task]["subset_match"].append(0.0)
                overall["len2plus"]["count"] += 1
                for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
                    overall["len2plus"][k].append(0.0)
            else:
                per_task_single[task]["count"] += 1
                per_task_single[task]["incomplete"] += 1
                overall["len1"]["count"] += 1
            continue

        if route == "llm_judge":
            per_task_single[task]["count"] += 1
            if r.get("correct"):
                per_task_single[task]["correct"] += 1
                overall["len1"]["correct"] += 1
            subtype = r.get("judge_subtype") or "?"
            per_task_single[task]["by_subtype"][subtype] += 1
            if r.get("correct"):
                per_task_single[task]["by_subtype_correct"][subtype] += 1
            overall["len1"]["count"] += 1
        elif route == "set_f1":
            per_task_multi[task]["count"] += 1
            for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
                per_task_multi[task][k].append(float(r.get(k, 0.0)))
                overall["len2plus"][k].append(float(r.get(k, 0.0)))
            overall["len2plus"]["count"] += 1

    # Finalize.
    per_task_single_final = {}
    for task, d in per_task_single.items():
        n = d["count"]
        per_task_single_final[task] = {
            "count": n,
            "accuracy": d["correct"] / n if n else 0.0,
            "correct": d["correct"],
            "incomplete": d["incomplete"],
            "by_subtype": dict(d["by_subtype"]),
            "by_subtype_accuracy": {
                k: (d["by_subtype_correct"][k] / d["by_subtype"][k]) if d["by_subtype"][k] else 0.0
                for k in d["by_subtype"]
            },
        }

    per_task_multi_final = {}
    for task, d in per_task_multi.items():
        n = d["count"]
        per_task_multi_final[task] = {
            "count": n,
            "incomplete": d["incomplete"],
            "precision": _avg(d["precision"]),
            "recall": _avg(d["recall"]),
            "f1": _avg(d["f1"]),
            "accuracy": _avg(d["accuracy"]),
            "subset_match": _avg(d["subset_match"]),
        }

    overall["len1"]["accuracy"] = (
        overall["len1"]["correct"] / overall["len1"]["count"]
        if overall["len1"]["count"] else 0.0
    )
    for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
        overall["len2plus"][k] = _avg(overall["len2plus"][k])

    return {
        "total": overall["total"],
        "incomplete": overall["incomplete"],
        "len1_accuracy": overall["len1"]["accuracy"],
        "len1_count": overall["len1"]["count"],
        "len2plus_f1": overall["len2plus"]["f1"],
        "len2plus_precision": overall["len2plus"]["precision"],
        "len2plus_recall": overall["len2plus"]["recall"],
        "len2plus_accuracy": overall["len2plus"]["accuracy"],
        "len2plus_subset_match": overall["len2plus"]["subset_match"],
        "len2plus_count": overall["len2plus"]["count"],
        "per_task_len1": per_task_single_final,
        "per_task_len2plus": per_task_multi_final,
    }


def render_markdown(summary: Dict[str, Any], results_path: Path) -> str:
    lines: List[str] = []
    lines.append(f"# Scoring summary — `{results_path.name}`\n")
    lines.append(f"- total rows: **{summary['total']}**")
    lines.append(f"- incomplete (no finish): **{summary['incomplete']}**\n")
    lines.append(f"- len=1 (judge) accuracy: **{summary['len1_accuracy']:.4f}** "
                 f"(n={summary['len1_count']})")
    lines.append(f"- len>=2 F1: **{summary['len2plus_f1']:.4f}** "
                 f"| P: {summary['len2plus_precision']:.4f} "
                 f"| R: {summary['len2plus_recall']:.4f} "
                 f"| subset_match: {summary['len2plus_subset_match']:.4f} "
                 f"(n={summary['len2plus_count']})\n")

    lines.append("## Per-task (len=1, judge accuracy)\n")
    lines.append("| task | n | accuracy | incomplete | subtype breakdown |")
    lines.append("|---|---:|---:|---:|---|")
    for task in sorted(summary["per_task_len1"]):
        d = summary["per_task_len1"][task]
        sub = ", ".join(
            f"{k}={d['by_subtype'][k]}({d['by_subtype_accuracy'][k]:.2f})"
            for k in sorted(d["by_subtype"])
        )
        lines.append(
            f"| {task} | {d['count']} | {d['accuracy']:.4f} "
            f"| {d['incomplete']} | {sub or '—'} |"
        )

    lines.append("\n## Per-task (len>=2, set metrics)\n")
    lines.append("| task | n | precision | recall | F1 | jaccard-acc | subset_match | incomplete |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for task in sorted(summary["per_task_len2plus"]):
        d = summary["per_task_len2plus"][task]
        lines.append(
            f"| {task} | {d['count']} | {d['precision']:.4f} | {d['recall']:.4f} "
            f"| {d['f1']:.4f} | {d['accuracy']:.4f} | {d['subset_match']:.4f} "
            f"| {d['incomplete']} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score multimodal results.jsonl")
    p.add_argument(
        "--results", type=str, action="append", required=True,
        help="Path to a results.jsonl. Repeat for multiple runs; each lands in "
             "its own <output_root>/<dirname> subdirectory.",
    )
    p.add_argument("--output-root", type=str, required=True)
    p.add_argument("--judge-model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--region", type=str, default=os.environ.get("BEDROCK_REGION", "ca-west-1"),
                   help="Single-region fallback if --regions is not provided.")
    p.add_argument(
        "--regions", type=str, nargs="*", default=None,
        help="Round-robin pool of regions for the judge. If set, overrides "
             "--region. For Anthropic models we swap the region prefix "
             "(us./eu.) of the model id automatically per region.",
    )
    p.add_argument("--concurrency", type=int, default=10,
                   help="Per-region concurrency. Global concurrency is "
                        "concurrency × len(regions).")
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--no-resume", action="store_true")
    return p.parse_args()


async def _main() -> None:
    args = parse_args()
    out_root = Path(args.output_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("BEDROCK_REGION", args.region)
    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)

    for raw in args.results:
        p = Path(raw).resolve()
        if not p.exists():
            print(f"[WARN] {p} does not exist, skipping", file=sys.stderr)
            continue
        sub = out_root / p.parent.name
        print(f"\n>>> Scoring {p}\n    → {sub}")
        await score_file(
            results_path=p,
            out_dir=sub,
            bedrock_model_id=args.judge_model,
            bedrock_region=args.region,
            bedrock_regions=args.regions,
            concurrency=args.concurrency,
            max_rows=args.max_rows or None,
            resume=not args.no_resume,
        )


if __name__ == "__main__":
    asyncio.run(_main())
