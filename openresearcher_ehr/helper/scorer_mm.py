"""Score a multimodal-pipeline results.jsonl against ground-truth labels.

End-to-end scorer for the multimodal EHR benchmark. Handles:

  1. Routing by gold label shape:
       - len(gold) == 1  →  LLM judge (Claude Sonnet 4.6 on Bedrock).
                            Picks one of six templates based on gold content
                            + task: yesno, count, date_time, id, label_name,
                            generic_string.
       - len(gold) >= 2  →  Rule-based set F1 / precision / recall / subset
                            accuracy over the normalized `name` strings.

  2. (Optional, default ON for len>=2 rows) Vocabulary normalization:
       For tasks that draw gold labels from a closed vocabulary (CheXpert
       findings, HCUP phenotypes, …), each predicted string is mapped to
       the nearest gold-vocab entry via sentence-transformer cosine
       similarity before the set-overlap metric runs. Below
       `--vocab-threshold`, the raw prediction is kept.

  3. Unified F1: the final summary reports a single per-task F1 that folds
       len=1 judge verdicts (correct → F1=1, else F1=0) into the same
       aggregator as len>=2 set metrics.

Outputs:
  <output_dir>/scored.jsonl     One record per sample with fields:
      qid, task, scope, len_gold, judge_subtype (if len=1),
      prediction, gold_names, correct (bool for len=1),
      precision, recall, f1, accuracy, subset_match,
      prediction_strs_mapped + mapping_similarity (len>=2, if vocab on).
  <output_dir>/summary.json     Aggregate stats per task + overall +
                                per_task_unified (len1+len2plus folded).
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
# Look in this dir + parent so the scorer can live at
# `openresearcher_ehr/scorer_mm.py` or `openresearcher_ehr/helper/scorer_mm.py`.
for _candidate in (SCRIPT_DIR, SCRIPT_DIR.parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

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
    """Raw normalization — strip + lowercase only.

    Used by the raw set-F1 path. Historically this was the only normalizer;
    kept identical so raw metrics stay byte-comparable with older runs.
    """
    if s is None:
        return ""
    return str(s).strip().lower()


def _normalize_name_collapse(s: Any) -> str:
    """Same as `_normalize_name` but also collapses internal whitespace.

    Used after vocabulary mapping so e.g. "sodium chloride 0.9%  flush"
    (double space in gold) matches the embedder-mapped entry.
    """
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


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

# ---------------------------------------------------------------------------
# Vocabulary normalization (optional, for len>=2 set-F1 rows)
# ---------------------------------------------------------------------------

class VocabMatcher:
    """Map predicted free-text strings to the closest gold-vocab entry.

    Vocabulary is the set of gold `name` strings observed per task. Each
    predicted string is replaced by the nearest vocab entry by sentence-
    embedding cosine similarity, subject to `threshold`; below threshold,
    the raw prediction is kept so out-of-vocabulary guesses still score
    via the exact-string path.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2",
                 threshold: float = 0.55, device: str = "cpu"):
        from sentence_transformers import SentenceTransformer
        import numpy as np
        self._np = np
        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device=device)
        self.threshold = threshold
        self._cache: Dict[str, Tuple[List[str], Any]] = {}  # task -> (vocab, embs)

    def _embed(self, texts: List[str]):
        return self.model.encode(
            texts,
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,   # L2 → dot == cosine
            convert_to_numpy=True,
        )

    def set_vocab(self, task: str, vocab: List[str]) -> None:
        if not vocab:
            return
        self._cache[task] = (list(vocab), self._embed(list(vocab)))

    def has_vocab(self, task: str) -> bool:
        return task in self._cache

    def map_many(self, preds: List[str], task: str) -> List[Tuple[str, float]]:
        if not preds:
            return []
        if task not in self._cache:
            return [(p, 0.0) for p in preds]
        vocab, embs = self._cache[task]
        pred_embs = self._embed(preds)
        sims = pred_embs @ embs.T
        out: List[Tuple[str, float]] = []
        for i, p in enumerate(preds):
            j = int(self._np.argmax(sims[i]))
            s = float(sims[i][j])
            out.append((vocab[j] if s >= self.threshold else p, s))
        return out


def build_task_vocabularies(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Collect the union of gold `name` strings per task from a results or
    gold-label JSONL. Accepts the raw results.jsonl (each row has `label`)
    or a separate prepared-gold JSONL with the same shape.
    """
    buckets: Dict[str, set] = defaultdict(set)
    for row in rows:
        task = row.get("task", "?")
        for item in row.get("label") or []:
            name = item.get("name") if isinstance(item, dict) else item
            if isinstance(name, str) and name.strip():
                buckets[task].add(name.strip())
    return {t: sorted(v) for t, v in buckets.items()}


def set_f1(pred: List[str], gold: List[str],
           collapse_whitespace: bool = False) -> Dict[str, float]:
    norm = _normalize_name_collapse if collapse_whitespace else _normalize_name
    pred_set = {norm(x) for x in pred if norm(x)}
    gold_set = {norm(x) for x in gold if norm(x)}
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
    vocab_matcher: Optional[VocabMatcher] = None,
    vocab_skip_tasks: Optional[Iterable[str]] = None,
    vocab_gold_path: Optional[Path] = None,
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

    # ---- Build per-task vocabularies (if vocab mapping enabled) -----------
    # Vocab source defaults to the results file itself (rows carry `label`).
    # If a separate gold JSONL is provided, use it instead — useful when the
    # results file was subsampled or contains multiple runs per qid.
    vocab_skip = set(vocab_skip_tasks or ())
    if vocab_matcher is not None:
        if vocab_gold_path is not None and vocab_gold_path.exists():
            with vocab_gold_path.open() as f:
                gold_rows = []
                for line in f:
                    line = line.strip()
                    if line:
                        gold_rows.append(json.loads(line))
        else:
            gold_rows = rows
        vocabs = build_task_vocabularies(gold_rows)
        print("[vocab] per-task vocabulary sizes:", flush=True)
        for t in sorted(vocabs):
            size = len(vocabs[t])
            tag = " (skipped)" if t in vocab_skip else ""
            print(f"  {t:32s} {size:>4} labels{tag}", flush=True)
        for t, v in vocabs.items():
            if t in vocab_skip:
                continue
            vocab_matcher.set_vocab(t, v)

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
            task = row.get("task") or "?"
            # Always record the raw-string metrics under *_raw.
            raw_metrics = set_f1(pred_strs, gold_names)
            for k, v in raw_metrics.items():
                base[f"{k}_raw"] = v
            if vocab_matcher is not None:
                if vocab_matcher.has_vocab(task):
                    mapped = vocab_matcher.map_many(pred_strs, task)
                    mapped_preds = [m for m, _ in mapped]
                    sims = [s for _, s in mapped]
                    base["prediction_strs_mapped"] = mapped_preds
                    base["mapping_similarity"] = sims
                    base["vocab_mapped"] = True
                else:
                    # Task excluded from vocab mapping — keep the raw
                    # prediction strings but still use the lenient
                    # (whitespace-collapsing) normalizer for the primary
                    # metrics, so mapped-track numbers are comparable.
                    mapped_preds = pred_strs
                    base["vocab_mapped"] = False
                # Primary metrics come from the mapped path. Uses whitespace-
                # collapsing normalization (matches the old rescorer) so gold
                # labels like "a  b" and preds like "a b" agree.
                base.update(set_f1(mapped_preds, gold_names,
                                   collapse_whitespace=True))
            else:
                # Vocab disabled globally — primary == raw.
                base.update(raw_metrics)
                base["vocab_mapped"] = False
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
    if vocab_matcher is not None:
        summary["vocab_model"] = vocab_matcher.model_name
        summary["vocab_threshold"] = vocab_matcher.threshold
        summary["vocab_skip_tasks"] = sorted(vocab_skip)
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
        "precision_raw": [], "recall_raw": [], "f1_raw": [],
        "accuracy_raw": [], "subset_match_raw": [],
    })
    overall = {
        "total": len(scored),
        "incomplete": 0,
        "len1": {"count": 0, "correct": 0},
        "len2plus": {"count": 0, "precision": [], "recall": [], "f1": [],
                      "accuracy": [], "subset_match": [],
                      "precision_raw": [], "recall_raw": [], "f1_raw": [],
                      "accuracy_raw": [], "subset_match_raw": []},
    }

    for r in scored:
        task = r.get("task") or "?"
        route = r.get("route")
        if route == "incomplete":
            overall["incomplete"] += 1
            if r.get("len_gold", 0) >= 2:
                per_task_multi[task]["count"] += 1
                per_task_multi[task]["incomplete"] += 1
                for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
                    per_task_multi[task][k].append(0.0)
                    per_task_multi[task][f"{k}_raw"].append(0.0)
                overall["len2plus"]["count"] += 1
                for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
                    overall["len2plus"][k].append(0.0)
                    overall["len2plus"][f"{k}_raw"].append(0.0)
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
                # Raw (pre-vocab-mapping) metrics — fall back to primary
                # when the row didn't go through vocab mapping.
                raw = float(r.get(f"{k}_raw", r.get(k, 0.0)))
                per_task_multi[task][f"{k}_raw"].append(raw)
                overall["len2plus"][f"{k}_raw"].append(raw)
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
            # Raw (pre-vocab-mapping) averages, parallel to the mapped ones.
            "precision_raw": _avg(d["precision_raw"]),
            "recall_raw": _avg(d["recall_raw"]),
            "f1_raw": _avg(d["f1_raw"]),
            "accuracy_raw": _avg(d["accuracy_raw"]),
            "subset_match_raw": _avg(d["subset_match_raw"]),
        }

    overall["len1"]["accuracy"] = (
        overall["len1"]["correct"] / overall["len1"]["count"]
        if overall["len1"]["count"] else 0.0
    )
    for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
        overall["len2plus"][k] = _avg(overall["len2plus"][k])
        overall["len2plus"][f"{k}_raw"] = _avg(overall["len2plus"][f"{k}_raw"])

    # ------------------------------------------------------------------
    # Unified F1: per-task and overall. Folds len=1 judge verdicts into
    # the same aggregator as len>=2 set metrics — a correct len=1
    # verdict counts as F1=1 (precision=recall=1); incorrect/incomplete
    # counts as F1=0.
    # ------------------------------------------------------------------
    _METRIC_KEYS = ("precision", "recall", "f1", "accuracy", "subset_match")
    _METRIC_KEYS_ALL = _METRIC_KEYS + tuple(f"{k}_raw" for k in _METRIC_KEYS)

    per_task_unified: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: {k: [] for k in _METRIC_KEYS_ALL}
    )
    unified_totals: Dict[str, List[float]] = {k: [] for k in _METRIC_KEYS_ALL}

    for r in scored:
        task = r.get("task") or "?"
        route = r.get("route")
        if route == "set_f1":
            vals = {k: float(r.get(k, 0.0)) for k in _METRIC_KEYS}
            vals.update({f"{k}_raw": float(r.get(f"{k}_raw", r.get(k, 0.0)))
                         for k in _METRIC_KEYS})
        elif route == "llm_judge":
            v = 1.0 if r.get("correct") else 0.0
            vals = {k: v for k in _METRIC_KEYS}
            # Judge verdicts have no vocab-mapping distinction — raw == mapped.
            vals.update({f"{k}_raw": v for k in _METRIC_KEYS})
        elif route == "incomplete":
            vals = {k: 0.0 for k in _METRIC_KEYS_ALL}
        else:
            continue
        for k, x in vals.items():
            per_task_unified[task][k].append(x)
            unified_totals[k].append(x)

    per_task_unified_final = {
        task: {
            "count": len(d["f1"]),
            **{k: _avg(d[k]) for k in _METRIC_KEYS_ALL},
        }
        for task, d in per_task_unified.items()
    }

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
        # Raw (pre-vocab-mapping) overall len2plus metrics.
        "len2plus_f1_raw": overall["len2plus"]["f1_raw"],
        "len2plus_precision_raw": overall["len2plus"]["precision_raw"],
        "len2plus_recall_raw": overall["len2plus"]["recall_raw"],
        "len2plus_accuracy_raw": overall["len2plus"]["accuracy_raw"],
        "len2plus_subset_match_raw": overall["len2plus"]["subset_match_raw"],
        "per_task_len1": per_task_single_final,
        "per_task_len2plus": per_task_multi_final,
        # Unified (judge + set metrics folded together).
        "unified_count": len(unified_totals["f1"]),
        "unified_precision": _avg(unified_totals["precision"]),
        "unified_recall": _avg(unified_totals["recall"]),
        "unified_f1": _avg(unified_totals["f1"]),
        "unified_accuracy": _avg(unified_totals["accuracy"]),
        "unified_subset_match": _avg(unified_totals["subset_match"]),
        # Raw unified (no vocab mapping on the set-F1 leg).
        "unified_precision_raw": _avg(unified_totals["precision_raw"]),
        "unified_recall_raw": _avg(unified_totals["recall_raw"]),
        "unified_f1_raw": _avg(unified_totals["f1_raw"]),
        "unified_accuracy_raw": _avg(unified_totals["accuracy_raw"]),
        "unified_subset_match_raw": _avg(unified_totals["subset_match_raw"]),
        "per_task_unified": per_task_unified_final,
    }


def render_markdown(summary: Dict[str, Any], results_path: Path) -> str:
    lines: List[str] = []
    has_vocab = "vocab_model" in summary
    lines.append(f"# Scoring summary — `{results_path.name}`\n")
    lines.append(f"- total rows: **{summary['total']}**")
    lines.append(f"- incomplete (no finish): **{summary['incomplete']}**")
    if has_vocab:
        lines.append(
            f"- vocab normalization: model=`{summary['vocab_model']}`, "
            f"threshold={summary['vocab_threshold']:.2f}, "
            f"skipped tasks={summary.get('vocab_skip_tasks') or '—'}"
        )
    lines.append("")
    lines.append(f"- len=1 (judge) accuracy: **{summary['len1_accuracy']:.4f}** "
                 f"(n={summary['len1_count']})")
    if has_vocab:
        lines.append(
            f"- len>=2 F1 (mapped): **{summary['len2plus_f1']:.4f}** "
            f"| raw: {summary['len2plus_f1_raw']:.4f} "
            f"| ΔF1: {(summary['len2plus_f1']-summary['len2plus_f1_raw'])*100:+.2f} pp "
            f"(n={summary['len2plus_count']})"
        )
        lines.append(
            f"- **unified F1 (mapped): {summary['unified_f1']:.4f}** "
            f"| raw: {summary['unified_f1_raw']:.4f} "
            f"(n={summary['unified_count']})\n"
        )
    else:
        lines.append(f"- len>=2 F1: **{summary['len2plus_f1']:.4f}** "
                     f"| P: {summary['len2plus_precision']:.4f} "
                     f"| R: {summary['len2plus_recall']:.4f} "
                     f"| subset_match: {summary['len2plus_subset_match']:.4f} "
                     f"(n={summary['len2plus_count']})")
        lines.append(f"- **unified F1: {summary['unified_f1']:.4f}** "
                     f"(n={summary['unified_count']})\n")

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
    if has_vocab:
        lines.append(
            "| task | n | F1 (raw → mapped) | P (raw → mapped) | R (raw → mapped) "
            "| jaccard (raw → mapped) | subset (raw → mapped) | incomplete |"
        )
        lines.append("|---|---:|---|---|---|---|---|---:|")
        for task in sorted(summary["per_task_len2plus"]):
            d = summary["per_task_len2plus"][task]
            def _pair(a: float, b: float) -> str:
                return f"{a:.4f} → {b:.4f}"
            lines.append(
                f"| {task} | {d['count']} "
                f"| {_pair(d['f1_raw'], d['f1'])} "
                f"| {_pair(d['precision_raw'], d['precision'])} "
                f"| {_pair(d['recall_raw'], d['recall'])} "
                f"| {_pair(d['accuracy_raw'], d['accuracy'])} "
                f"| {_pair(d['subset_match_raw'], d['subset_match'])} "
                f"| {d['incomplete']} |"
            )
    else:
        lines.append(
            "| task | n | precision | recall | F1 | jaccard-acc | subset_match | incomplete |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for task in sorted(summary["per_task_len2plus"]):
            d = summary["per_task_len2plus"][task]
            lines.append(
                f"| {task} | {d['count']} | {d['precision']:.4f} | {d['recall']:.4f} "
                f"| {d['f1']:.4f} | {d['accuracy']:.4f} | {d['subset_match']:.4f} "
                f"| {d['incomplete']} |"
            )

    lines.append("\n## Per-task unified F1 (len=1 judge + len>=2 set metrics)\n")
    lines.append(
        "Unification: a correct len=1 judge verdict counts as F1=1 "
        "(precision=recall=1); incorrect/incomplete counts as F1=0. "
        "Reduces to accuracy when |gold|=1, shares the aggregator with "
        "len>=2 rows.\n"
    )
    if has_vocab:
        lines.append("| task | n | F1 (raw → mapped) | P (raw → mapped) "
                     "| R (raw → mapped) | jaccard (raw → mapped) |")
        lines.append("|---|---:|---|---|---|---|")
        for task in sorted(summary.get("per_task_unified", {})):
            d = summary["per_task_unified"][task]
            def _pair(a: float, b: float) -> str:
                return f"{a:.4f} → {b:.4f}"
            lines.append(
                f"| {task} | {d['count']} "
                f"| {_pair(d['f1_raw'], d['f1'])} "
                f"| {_pair(d['precision_raw'], d['precision'])} "
                f"| {_pair(d['recall_raw'], d['recall'])} "
                f"| {_pair(d['accuracy_raw'], d['accuracy'])} |"
            )
    else:
        lines.append("| task | n | precision | recall | F1 | jaccard |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for task in sorted(summary.get("per_task_unified", {})):
            d = summary["per_task_unified"][task]
            lines.append(
                f"| {task} | {d['count']} | {d['precision']:.4f} "
                f"| {d['recall']:.4f} | {d['f1']:.4f} | {d['accuracy']:.4f} |"
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

    # --- vocabulary normalization (len>=2 rows only) ----------------------
    p.add_argument(
        "--vocab", dest="vocab", action="store_true", default=True,
        help="Enable vocabulary normalization for len>=2 rows (default on).",
    )
    p.add_argument(
        "--no-vocab", dest="vocab", action="store_false",
        help="Disable vocabulary normalization — only raw string set-F1.",
    )
    p.add_argument(
        "--vocab-model", type=str, default="all-MiniLM-L6-v2",
        help="sentence-transformers model id for vocab matching (CPU).",
    )
    p.add_argument(
        "--vocab-threshold", type=float, default=0.55,
        help="Min cosine similarity to replace a prediction with the "
             "nearest vocab entry. Below threshold → keep raw.",
    )
    p.add_argument(
        "--vocab-device", type=str, default="cpu",
        help="Torch device for the embedder (`cpu`, `cuda`, `cuda:0`, ...).",
    )
    p.add_argument(
        "--vocab-skip-tasks", nargs="*",
        default=["ehrxqa_table"],
        help="Tasks excluded from vocab mapping (defaults to ehrxqa_table: "
             "its gold values are cohort ids / numbers / dates that cannot "
             "meaningfully be normalized).",
    )
    p.add_argument(
        "--vocab-gold", type=str, default=None,
        help="Optional separate JSONL whose `label` field provides the "
             "per-task vocabulary. Defaults to reading gold from --results.",
    )
    return p.parse_args()


async def _main() -> None:
    args = parse_args()
    out_root = Path(args.output_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("BEDROCK_REGION", args.region)
    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)

    vocab_matcher: Optional[VocabMatcher] = None
    if args.vocab:
        print(f"[vocab] loading embedder {args.vocab_model} on {args.vocab_device}, "
              f"threshold={args.vocab_threshold}", flush=True)
        vocab_matcher = VocabMatcher(
            model_name=args.vocab_model,
            threshold=args.vocab_threshold,
            device=args.vocab_device,
        )

    vocab_gold = Path(args.vocab_gold).resolve() if args.vocab_gold else None

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
            vocab_matcher=vocab_matcher,
            vocab_skip_tasks=args.vocab_skip_tasks,
            vocab_gold_path=vocab_gold,
        )


if __name__ == "__main__":
    asyncio.run(_main())
