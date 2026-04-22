"""Re-score len>=2 rows from `scored.jsonl` after mapping each predicted
string to the nearest controlled-vocabulary label for its task.

Motivation:
  - The original scorer uses exact string-set overlap. Phenotyping and
    radiology tasks draw gold labels from a closed vocabulary (AHRQ CCS
    phenotypes, CheXpert findings, etc.). Models that answer with paraphrases
    or different capitalization score 0 even when they name the right
    concept.
  - This script normalizes both sides. For each task we build the vocabulary
    from all gold labels observed across the 2,695-row dataset. Each
    predicted string is then replaced by the nearest vocab entry (by
    sentence-embedding cosine similarity), subject to a minimum similarity
    threshold. Below threshold, the prediction stays as-is (so
    out-of-vocabulary guesses can still score via the exact-string path).
  - Output: `rescored.jsonl` alongside the original `scored.jsonl`, plus
    updated `summary_vocab.md` / `summary_vocab.json`. The len=1 judge
    verdicts are preserved as-is (vocabulary normalization is only useful
    for set-overlap metrics).

Only used for offline re-analysis — the agent and scorer are untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# ---------------------------------------------------------------------------
# Vocabulary construction
# ---------------------------------------------------------------------------

def load_gold_dataset(path: Path) -> List[Dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def build_task_vocabularies(
    dataset_rows: List[Dict],
) -> Dict[str, List[str]]:
    """Collect the union of gold name strings per task.

    Gold label format is `[{"name": "...", "value": ...}, ...]`. We keep the
    raw `name` field verbatim — the downstream set match will lowercase-strip
    both sides.
    """
    buckets: Dict[str, set] = defaultdict(set)
    for row in dataset_rows:
        task = row.get("task", "?")
        for item in row.get("label") or []:
            if isinstance(item, dict):
                name = item.get("name")
            else:
                name = item
            if isinstance(name, str) and name.strip():
                buckets[task].add(name.strip())
    return {t: sorted(v) for t, v in buckets.items()}


# ---------------------------------------------------------------------------
# Embedding + nearest-vocab match
# ---------------------------------------------------------------------------

class VocabMatcher:
    """Maps predicted free-text strings to the closest vocabulary entry.

    Embeddings are computed with sentence-transformers (CPU by default).
    Uses a per-task cache so each vocabulary is embedded only once.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", threshold: float = 0.55):
        from sentence_transformers import SentenceTransformer
        import numpy as np
        self.st = SentenceTransformer
        self.np = np
        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")
        self.threshold = threshold
        self._vocab_cache: Dict[str, Tuple[List[str], "np.ndarray"]] = {}

    def _embed(self, texts: List[str]):
        # L2-normalize so dot product = cosine similarity
        embs = self.model.encode(
            texts,
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embs

    def set_vocab(self, task: str, vocab: List[str]) -> None:
        if not vocab:
            return
        embs = self._embed(vocab)
        self._vocab_cache[task] = (vocab, embs)

    def map_string(self, pred: str, task: str) -> Tuple[str, float]:
        """Return (mapped_string, similarity). If no vocab or below threshold,
        returns (pred, 0.0) so the original string stays in play.
        """
        if task not in self._vocab_cache:
            return pred, 0.0
        vocab, embs = self._vocab_cache[task]
        pred_emb = self._embed([pred])[0]
        sims = embs @ pred_emb
        idx = int(self.np.argmax(sims))
        sim = float(sims[idx])
        if sim < self.threshold:
            return pred, sim
        return vocab[idx], sim

    def map_many(self, preds: List[str], task: str) -> List[Tuple[str, float]]:
        if not preds:
            return []
        if task not in self._vocab_cache:
            return [(p, 0.0) for p in preds]
        vocab, embs = self._vocab_cache[task]
        pred_embs = self._embed(preds)
        sims = pred_embs @ embs.T
        out = []
        for i, p in enumerate(preds):
            j = int(self.np.argmax(sims[i]))
            s = float(sims[i][j])
            out.append((vocab[j] if s >= self.threshold else p, s))
        return out


# ---------------------------------------------------------------------------
# Set-metric recomputation
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def set_metrics(pred: List[str], gold: List[str]) -> Dict[str, float]:
    p = {_normalize(x) for x in pred if _normalize(x)}
    g = {_normalize(x) for x in gold if _normalize(x)}
    tp = len(p & g)
    prec = tp / len(p) if p else 0.0
    rec = tp / len(g) if g else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
    union = p | g
    acc = tp / len(union) if union else 0.0
    subset = 1.0 if p == g else 0.0
    return {
        "precision": prec,
        "recall": rec,
        "f1": f1,
        "accuracy": acc,
        "subset_match": subset,
        "tp": tp,
        "n_pred": len(p),
        "n_gold": len(g),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_scored_file(
    scored_path: Path,
    matcher: VocabMatcher,
    vocabs: Dict[str, List[str]],
    out_path: Path,
) -> Dict:
    """Walk a `scored.jsonl`, recompute set metrics after vocabulary
    normalization, and fold len=1 judge verdicts into the same F1 counters
    so a single metric covers both tracks.

    Unification rule: for a len=1 row, we treat the judge's verdict as a
    1-element set match — correct → tp=1 n_pred=1 n_gold=1 (P=R=F1=1);
    incorrect/incomplete → tp=0 n_pred=max(1,|pred|) n_gold=1 (P=R=F1=0).
    This reduces to accuracy-when-|pred|=1 while sharing the aggregator
    with len>=2 rows.

    Returns aggregate stats for Markdown rendering.
    """
    # Unified per-task F1 buckets (len=1 judge + len>=2 set metrics).
    per_task_unified: Dict[str, List[Dict]] = defaultdict(list)
    # Kept for backward-compat reporting.
    per_task_len2: Dict[str, List[Dict]] = defaultdict(list)
    per_task_len1: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "correct": 0})
    total_len1 = {"count": 0, "correct": 0}
    mapped_rows = []

    with scored_path.open() as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            task = r.get("task", "?")
            route = r.get("route")

            if route == "set_f1" and (r.get("gold_names") or []):
                pred_strs = r.get("prediction_strs") or []
                mapped = matcher.map_many(pred_strs, task)
                mapped_preds = [m for m, _ in mapped]
                sims = [s for _, s in mapped]
                m = set_metrics(mapped_preds, r["gold_names"])
                new_row = dict(r)
                new_row["prediction_strs_mapped"] = mapped_preds
                new_row["mapping_similarity"] = sims
                for k in ("precision", "recall", "f1", "accuracy",
                          "subset_match", "tp", "n_pred", "n_gold"):
                    new_row[k] = m[k]
                mapped_rows.append(new_row)
                per_task_len2[task].append(m)
                per_task_unified[task].append(m)
            else:
                mapped_rows.append(r)
                if route == "llm_judge":
                    per_task_len1[task]["count"] += 1
                    is_correct = bool(r.get("correct"))
                    if is_correct:
                        per_task_len1[task]["correct"] += 1
                    total_len1["count"] += 1
                    if is_correct:
                        total_len1["correct"] += 1
                    # Fold the judge verdict into the unified F1 aggregator.
                    unified = {
                        "precision": 1.0 if is_correct else 0.0,
                        "recall":    1.0 if is_correct else 0.0,
                        "f1":        1.0 if is_correct else 0.0,
                        "accuracy":  1.0 if is_correct else 0.0,
                        "subset_match": 1.0 if is_correct else 0.0,
                        "tp": 1 if is_correct else 0,
                        "n_pred": 1,
                        "n_gold": 1,
                    }
                    per_task_unified[task].append(unified)
                elif route == "incomplete":
                    # An incomplete trajectory gets 0 on every metric,
                    # regardless of label length, so the aggregator still
                    # counts the sample in the denominator.
                    unified = {
                        "precision": 0.0, "recall": 0.0, "f1": 0.0,
                        "accuracy": 0.0, "subset_match": 0.0,
                        "tp": 0, "n_pred": 0,
                        "n_gold": len(r.get("gold_names") or []),
                    }
                    per_task_unified[task].append(unified)

    # Write rescored.jsonl
    with out_path.open("w") as fout:
        for r in mapped_rows:
            fout.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Aggregate
    def _avg(vals):
        return sum(vals) / len(vals) if vals else 0.0

    per_task_agg = {}
    totals = {"n": 0, "precision": [], "recall": [], "f1": [],
              "accuracy": [], "subset_match": []}
    for task, lst in per_task_len2.items():
        per_task_agg[task] = {
            "count": len(lst),
            "precision": _avg([x["precision"] for x in lst]),
            "recall": _avg([x["recall"] for x in lst]),
            "f1": _avg([x["f1"] for x in lst]),
            "accuracy": _avg([x["accuracy"] for x in lst]),
            "subset_match": _avg([x["subset_match"] for x in lst]),
        }
        for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
            totals[k].extend(x[k] for x in lst)
        totals["n"] += len(lst)

    # Unified per-task + overall aggregation.
    per_task_unified_agg = {}
    unified_totals = {"n": 0, "precision": [], "recall": [], "f1": [],
                      "accuracy": [], "subset_match": []}
    for task, lst in per_task_unified.items():
        per_task_unified_agg[task] = {
            "count": len(lst),
            "precision": _avg([x["precision"] for x in lst]),
            "recall": _avg([x["recall"] for x in lst]),
            "f1": _avg([x["f1"] for x in lst]),
            "accuracy": _avg([x["accuracy"] for x in lst]),
            "subset_match": _avg([x["subset_match"] for x in lst]),
        }
        for k in ("precision", "recall", "f1", "accuracy", "subset_match"):
            unified_totals[k].extend(x[k] for x in lst)
        unified_totals["n"] += len(lst)

    overall = {
        "len2plus_count": totals["n"],
        "len2plus_precision": _avg(totals["precision"]),
        "len2plus_recall": _avg(totals["recall"]),
        "len2plus_f1": _avg(totals["f1"]),
        "len2plus_accuracy": _avg(totals["accuracy"]),
        "len2plus_subset_match": _avg(totals["subset_match"]),
        "len1_count": total_len1["count"],
        "len1_accuracy": (total_len1["correct"] / total_len1["count"]
                           if total_len1["count"] else 0.0),
        "per_task_len2plus_vocab": per_task_agg,
        "per_task_len1": {t: {"count": d["count"],
                              "accuracy": (d["correct"] / d["count"]
                                            if d["count"] else 0.0)}
                          for t, d in per_task_len1.items()},
        # Unified F1 — folds len=1 judge verdicts into the F1 aggregator so
        # a single number covers both tracks per task.
        "unified_count": unified_totals["n"],
        "unified_precision": _avg(unified_totals["precision"]),
        "unified_recall": _avg(unified_totals["recall"]),
        "unified_f1": _avg(unified_totals["f1"]),
        "unified_accuracy": _avg(unified_totals["accuracy"]),
        "per_task_unified": per_task_unified_agg,
        "vocab_threshold": matcher.threshold,
        "vocab_model": matcher.model_name,
    }
    return overall


def render_markdown(summary: Dict, orig_summary: Optional[Dict], label: str) -> str:
    lines = [
        f"# Vocabulary-normalized re-scoring — {label}",
        "",
        f"- vocab embedding model: `{summary['vocab_model']}`",
        f"- cosine threshold: **{summary['vocab_threshold']:.2f}** "
        "(below threshold, the raw prediction is kept)",
        f"- len=1 rows unchanged (judge verdicts preserved). n="
        f"{summary['len1_count']}.",
        f"- len≥2 rows re-scored after vocabulary mapping. n="
        f"{summary['len2plus_count']}.",
        "",
        "## Headline (len≥2 set metrics)",
        "",
        "| metric | before | after | Δ |",
        "|---|---:|---:|---:|",
    ]
    keys = [("precision", "precision"), ("recall", "recall"),
            ("f1", "f1"), ("accuracy", "jaccard accuracy"),
            ("subset_match", "subset match")]
    if orig_summary is not None:
        for mk, label_ in keys:
            before = orig_summary.get(f"len2plus_{mk}", float("nan"))
            after = summary[f"len2plus_{mk}"]
            delta = after - before
            lines.append(f"| {label_} | {before:.4f} | {after:.4f} | "
                          f"{'+' if delta >= 0 else ''}{delta*100:.2f} pp |")
    else:
        for mk, label_ in keys:
            lines.append(f"| {label_} | — | {summary[f'len2plus_{mk}']:.4f} | — |")

    lines.extend(["", "## Per-task len≥2 after normalization", "",
                  "| task | n | precision | recall | F1 | jaccard | subset |",
                  "|---|---:|---:|---:|---:|---:|---:|"])
    for task in sorted(summary.get("per_task_len2plus_vocab", {})):
        d = summary["per_task_len2plus_vocab"][task]
        lines.append(
            f"| {task} | {d['count']} | {d['precision']:.4f} | {d['recall']:.4f} "
            f"| {d['f1']:.4f} | {d['accuracy']:.4f} | {d['subset_match']:.4f} |"
        )
    # Unified per-task F1 (folds len=1 judge verdicts into the F1 aggregator).
    lines.extend(["",
                  "## Per-task unified F1 (len=1 judge + len≥2 set metrics)",
                  "",
                  "Unification: a correct len=1 judge verdict counts as F1=1 "
                  "(precision=recall=1); incorrect/incomplete counts as F1=0. "
                  "Reduces to accuracy when |gold|=1, shares the aggregator with "
                  "len≥2 rows.",
                  "",
                  "| task | n | precision | recall | F1 | jaccard |",
                  "|---|---:|---:|---:|---:|---:|"])
    for task in sorted(summary.get("per_task_unified", {})):
        d = summary["per_task_unified"][task]
        lines.append(
            f"| {task} | {d['count']} | {d['precision']:.4f} | {d['recall']:.4f} "
            f"| {d['f1']:.4f} | {d['accuracy']:.4f} |"
        )
    lines.extend(["",
                  "### Overall unified F1",
                  "",
                  f"- n = {summary.get('unified_count', 0)}",
                  f"- F1 = **{summary.get('unified_f1', 0.0):.4f}**",
                  f"- precision = {summary.get('unified_precision', 0.0):.4f}",
                  f"- recall = {summary.get('unified_recall', 0.0):.4f}",
                  f"- jaccard accuracy = {summary.get('unified_accuracy', 0.0):.4f}",
                  ])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--scored", action="append", required=True,
        help="Path to a scored.jsonl (repeat for multiple models). Each "
             "lands in its own output subdir.",
    )
    p.add_argument(
        "--dataset", type=str,
        default="/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/"
                "combined_test_set_nonempty.jsonl",
        help="Gold-label dataset to build per-task vocabularies from.",
    )
    p.add_argument("--output-root", type=str, required=True)
    p.add_argument("--threshold", type=float, default=0.55)
    p.add_argument("--embedding-model", type=str, default="all-MiniLM-L6-v2")
    p.add_argument(
        "--skip-tasks", nargs="*",
        default=["ehrxqa_table"],
        help="Tasks to leave unchanged (no vocabulary mapping). Default "
             "skips `ehrxqa_table` because its 'vocabulary' is cohort "
             "IDs / numbers / dates that can't meaningfully be normalized.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_root = Path(args.output_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"[vocab] loading dataset: {args.dataset}", flush=True)
    dataset_rows = load_gold_dataset(Path(args.dataset))
    vocabs = build_task_vocabularies(dataset_rows)
    print(f"[vocab] per-task vocab sizes:", flush=True)
    for t, v in sorted(vocabs.items()):
        print(f"  {t:32} {len(v):>4} labels")
    print(flush=True)

    print(f"[vocab] loading embedder: {args.embedding_model} (cpu)", flush=True)
    matcher = VocabMatcher(args.embedding_model, threshold=args.threshold)
    skipped = set(args.skip_tasks or [])
    for t, v in vocabs.items():
        if t in skipped:
            print(f"[vocab] skipping {t!r} (per --skip-tasks)")
            continue
        matcher.set_vocab(t, v)
    print(flush=True)

    # Process each scored file
    for raw in args.scored:
        scored_path = Path(raw).resolve()
        if not scored_path.exists():
            print(f"[WARN] {scored_path} does not exist, skipping")
            continue
        label = scored_path.parent.name or scored_path.stem
        sub = out_root / label
        sub.mkdir(parents=True, exist_ok=True)
        out_path = sub / "rescored.jsonl"
        print(f"[vocab] rescoring {scored_path} -> {out_path}", flush=True)

        # Try to load original summary for before/after delta
        orig_summary_path = scored_path.parent / "summary.json"
        orig_summary = None
        if orig_summary_path.exists():
            try:
                orig_summary = json.load(orig_summary_path.open())
            except Exception:
                orig_summary = None

        summary = process_scored_file(scored_path, matcher, vocabs, out_path)
        (sub / "summary_vocab.json").write_text(json.dumps(summary, indent=2))
        (sub / "summary_vocab.md").write_text(
            render_markdown(summary, orig_summary, label)
        )
        print(f"  wrote: {out_path}")
        print(f"  wrote: {sub / 'summary_vocab.json'}")
        print(f"  wrote: {sub / 'summary_vocab.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
