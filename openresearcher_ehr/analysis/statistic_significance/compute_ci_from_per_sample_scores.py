"""Compute 95% confidence intervals for the three paper tables:

    table1_text_ehr_results.csv   — per-model × {risk_prediction, decision_making, overall}
    table2_multimodal_results.csv — per-model × {method} × 6 MM tasks + overall
    table3_agentehr_results.csv   — per-model × 5 AgentEHR subtasks + avg

Per-sample scores are pulled from the result tree:
  - EHR-Bench text-only (table 1): hierarchical/{agentic,oneshot}/ehr_bench/<slug>/
                                   rows.jsonl under L2 buckets
  - MM-Bench (table 2):            hierarchical/{agentic,oneshot}/mm_bench/<slug>/
                                   rows.jsonl under L4 buckets
  - AgentEHR-Bench subset600 (table 3):
        Bedrock hosts: results/agent_ehr_bench/agentic/subset600/<slug>/results.jsonl
                       → recompute per-sample F1 via helper/evaluate_results.py
        External      hf Letian2003/fh37931 @ data/external_results/fh37931/upload/<slug>/
                       → task_scores.jsonl (aggregated.avg.f1_score) OR
                         per_question.jsonl (runs[0].f1 / avg_f1)

For every cell (mean, δ, etc.) we derive a 95% CI from the per-sample F1
scores using a t-interval (same formula as the original one-file script).

Outputs (next to this script):
    table1_text_ehr_results_ci.csv
    table2_multimodal_results_ci.csv
    table3_agentehr_results_ci.csv
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
from scipy import stats

# --- reuse the canonical scorer used to produce the aggregated numbers -----
REPO = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr")
sys.path.insert(0, str(REPO))
from helper.evaluate_results import (  # noqa: E402
    extract_finish_predictions_with_source,
    f1_score,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
RESULTS = REPO / "results"
HIER = RESULTS / "hierarchical"
AGENT_EHR_BEDROCK = RESULTS / "agent_ehr_bench/agentic/subset600"
AGENT_EHR_EXTERNAL = Path("/fsx-shared/juncheng/EHR/data/external_results/fh37931/upload")

OUT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Core CI helper (unchanged from the original script)
# ---------------------------------------------------------------------------
def compute_ci(scores: Iterable[float]) -> Dict[str, float]:
    """Mean + t-based 95% CI. `scores` should be fractional F1 ∈ [0, 1]."""
    a = np.asarray(list(scores), dtype=float)
    a = a[~np.isnan(a)]
    n = int(a.size)
    if n < 2:
        # Can't form a CI with <2 samples; degenerate outputs.
        if n == 1:
            m = float(a[0])
            return {"N": 1, "mean": m, "std": 0.0, "se": 0.0,
                    "ci95_lower": m, "ci95_upper": m, "ci95_radius": 0.0}
        return {"N": 0, "mean": float("nan"), "std": float("nan"),
                "se": float("nan"), "ci95_lower": float("nan"),
                "ci95_upper": float("nan"), "ci95_radius": float("nan")}
    mean = float(a.mean())
    std = float(a.std(ddof=1))
    se = std / np.sqrt(n)
    radius = float(stats.t.ppf(0.975, df=n - 1) * se)
    return {
        "N": n,
        "mean": mean,
        "std": std,
        "se": se,
        "ci95_lower": mean - radius,
        "ci95_upper": mean + radius,
        "ci95_radius": radius,
    }


def compute_ci_diff(a: Iterable[float], b: Iterable[float]) -> Dict[str, float]:
    """Paired t-interval on (a_i − b_i) for matched qids.

    `a` and `b` must already be aligned element-by-element. Returns the same
    keys as compute_ci; the mean is the mean paired difference.
    """
    aa = np.asarray(list(a), dtype=float)
    bb = np.asarray(list(b), dtype=float)
    mask = ~(np.isnan(aa) | np.isnan(bb))
    aa, bb = aa[mask], bb[mask]
    return compute_ci(aa - bb)


# ---------------------------------------------------------------------------
# Slug tables
# ---------------------------------------------------------------------------
TABLE1_SLUGS = {
    # display label → repo slug
    "Claude Opus 4.6":        "claude_opus_4_6",
    "Claude Sonnet 4.6":      "claude_sonnet_4_6",
    "GLM-4.7":                "glm_4_7",
    "Qwen3.5-35B-A3B":        "qwen3_5_35b_a3b",
    "Gemma-4-26B-A4B-it":     "gemma4_26b_a4b_it",
    "MiniMax M2.5":           "minimax_m2_5",
    "Kimi K2.5":              "kimi_k2_5",
    "Qwen3-VL-235B":          "qwen3_vl_235b",
    "gpt-oss-120b":           "gpt_oss_120b",
}

TABLE2_SLUGS = {
    "Claude Opus 4.6":        "claude_opus_4_6",
    "Claude Sonnet 4.6":      "claude_sonnet_4_6",
    "Qwen3.5-35B-A3B":        "qwen3_5_35b_a3b",
    "Kimi K2.5":              "kimi_k2_5",
    "Qwen3-VL-235B":          "qwen3_vl_235b",
    "Gemma-4-26B-A4B-it":     "gemma4_26b_a4b_it",
}

# Table 3 — AgentEHR-Bench subset600. Values of form
#   ("bedrock", <slug>)  → results/agent_ehr_bench/agentic/subset600/<slug>/results.jsonl
#   ("ext_task_scores", <dir>) → external `task_scores.jsonl` (aggregated.avg.f1_score)
#   ("ext_per_question", <dir>) → external `per_question.jsonl` (avg_f1)
TABLE3_SOURCES: Dict[str, Tuple[str, str]] = {
    "Claude Opus 4.6":                 ("bedrock", "claude_opus_4_6"),
    "Claude Sonnet 4.6":               ("bedrock", "claude_sonnet_4_6"),
    "Kimi K2.5":                       ("bedrock", "kimi_k2_5"),
    "MiniMax-M2.5":                    ("bedrock", "minimax_m2_5"),
    "GLM-4.7":                         ("bedrock", "glm_4_7"),
    "Qwen3-235B-A22B":                 ("bedrock", "qwen3_235b"),
    "gpt-oss-120b":                    ("bedrock", "gpt_oss_120b"),
    "Tongyi DeepResearch 30B-A3B":     ("ext_task_scores", "subsets_500_tongyi_deepresearch_30b_a3b"),
    "Gemma-4-26B-A4B-it":              ("ext_per_question", "subset_500_gemma_4_26b_a4b_it_R1"),
    "OpenSeeker-30B":                  ("ext_task_scores", "subset_500_openseeker_v1_30b_sft"),
    "Qwen3.5-35B-A3B (base)":          ("ext_task_scores", "subsets_600_qwen3_5_35b_a3b"),
    "ClinSeek-35B-A3B (ours, SFT)":    ("ext_per_question", "subset_500_qwen3_5_35b_a3b_deepmed_6task_sft_epoch2_nothinking"),
}

# Table 3 column → task name (prescriptions intentionally dropped)
TABLE3_TASKS: "list[tuple[str, str]]" = [
    ("diagnoses",    "diagnoses_ccs"),
    ("labs",         "labevents"),
    ("microbiology", "microbiologyevents"),
    ("procedures",   "procedures_ccs"),
    ("transfers",    "transfers"),
]

# L4 task order for table 2
TABLE2_L4_COLUMNS = [
    ("cxr_presence",        "cxr_finding_presence"),
    ("cxr_enumeration",     "cxr_finding_enumeration"),
    ("cxr_change",          "cxr_change_comparison"),
    ("mortality_24h",       "mortality_24h"),
    ("inpatient_mortality", "inpatient_mortality_mm"),
    ("phenotype_ccs",       "phenotype_ccs"),
]


# ---------------------------------------------------------------------------
# Per-sample loaders from the hierarchical tree
# ---------------------------------------------------------------------------
def iter_rows(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def hier_rows_under(hier_root: Path) -> Dict[str, float]:
    """Return {qid: f1} for every rows.jsonl under hier_root. Ignores duplicates."""
    out: Dict[str, float] = {}
    for rj in hier_root.rglob("rows.jsonl"):
        for r in iter_rows(rj):
            q = r.get("qid")
            f1 = r.get("f1")
            if q is None or f1 is None:
                continue
            out[q] = float(f1)
    return out


def ehr_l2_scores(slug: str, paradigm: str, l2: str) -> Dict[str, float]:
    """Per-sample F1 for a single L2 under ehr_bench text_only."""
    root = HIER / paradigm / "ehr_bench" / slug / "L1.text_only" / f"L2.{l2}"
    return hier_rows_under(root)


def ehr_text_scores(slug: str, paradigm: str) -> Dict[str, float]:
    """Per-sample F1 across all text_only rows."""
    root = HIER / paradigm / "ehr_bench" / slug / "L1.text_only"
    return hier_rows_under(root)


def mm_l4_scores(slug: str, paradigm: str, l4: str) -> Dict[str, float]:
    """Per-sample F1 for a single L4 task under mm_bench multimodal."""
    # rows.jsonl live at .../L4.<l4>/rows.jsonl
    root = HIER / paradigm / "mm_bench" / slug / "L1.multimodal"
    out: Dict[str, float] = {}
    for rj in root.rglob("rows.jsonl"):
        if rj.parent.name != f"L4.{l4}":
            continue
        for r in iter_rows(rj):
            q = r.get("qid")
            f1 = r.get("f1")
            if q is not None and f1 is not None:
                out[q] = float(f1)
    return out


def mm_overall_scores(slug: str, paradigm: str) -> Dict[str, float]:
    """Per-sample F1 across all L4 MM rows (same cell the 'overall' column aggregates)."""
    # Only include the 6 L4 tasks in TABLE2_L4_COLUMNS to match the table's
    # published 'overall' denominator (cxr_vqa + risk_prediction_mm + phenotyping_mm).
    out: Dict[str, float] = {}
    for _, l4 in TABLE2_L4_COLUMNS:
        out.update(mm_l4_scores(slug, paradigm, l4))
    return out


# ---------------------------------------------------------------------------
# AgentEHR-Bench per-sample F1 for Table 3
# ---------------------------------------------------------------------------
def _agent_ehr_bedrock_rows(slug: str) -> Dict[str, List[float]]:
    """Recompute per-sample F1 from results.jsonl for Bedrock subset600 runs."""
    path = AGENT_EHR_BEDROCK / slug / "results.jsonl"
    if not path.exists():
        return {}
    by_task: Dict[str, List[float]] = defaultdict(list)
    for r in iter_rows(path):
        task = r.get("task")
        if not task:
            continue
        preds, _ = extract_finish_predictions_with_source(r, allow_text=False)
        gold = r.get("label")
        if isinstance(gold, list):
            gold_names = [x.get("name") if isinstance(x, dict) else str(x) for x in gold]
        elif isinstance(gold, str):
            gold_names = [gold]
        else:
            gold_names = []
        # labevents / labs task uses alphanumeric-stripped normalization in
        # evaluate_results.py; we just call f1_score directly here.
        score = f1_score(preds or [], gold_names)
        f1 = score["f1"] if isinstance(score, dict) else score
        by_task[task].append(float(f1))
    return by_task


def _agent_ehr_external_task_scores(subdir: str) -> Dict[str, List[float]]:
    """Use task_scores.jsonl from the external fh37931 mirror.

    Extract aggregated.avg.f1_score per row and bucket by task.
    """
    path = AGENT_EHR_EXTERNAL / subdir / "task_scores.jsonl"
    if not path.exists():
        return {}
    by_task: Dict[str, List[float]] = defaultdict(list)
    for r in iter_rows(path):
        task = r.get("task")
        if not task:
            continue
        agg = (r.get("aggregated") or {}).get("avg") or {}
        f1 = agg.get("f1_score")
        if f1 is None:
            continue
        by_task[task].append(float(f1))
    return by_task


def _agent_ehr_external_per_question(subdir: str) -> Dict[str, List[float]]:
    """Use per_question.jsonl (top-level avg_f1) for external subset runs.

    Rows without `avg_f1` (e.g., malformed or uncompleted) are skipped.
    """
    path = AGENT_EHR_EXTERNAL / subdir / "per_question.jsonl"
    if not path.exists():
        return {}
    by_task: Dict[str, List[float]] = defaultdict(list)
    for r in iter_rows(path):
        task = r.get("task")
        if not task:
            continue
        f1 = r.get("avg_f1")
        if f1 is None:
            runs = r.get("runs") or []
            if runs:
                f1 = runs[0].get("f1")
        if f1 is None:
            continue
        by_task[task].append(float(f1))
    return by_task


def load_table3_scores(model_label: str) -> Dict[str, List[float]]:
    kind, key = TABLE3_SOURCES[model_label]
    if kind == "bedrock":
        return _agent_ehr_bedrock_rows(key)
    if kind == "ext_task_scores":
        return _agent_ehr_external_task_scores(key)
    if kind == "ext_per_question":
        return _agent_ehr_external_per_question(key)
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def _fmt_pct(d: Dict[str, float]) -> Tuple[str, str, str, str, str, str]:
    """Produce (mean_pp, ci_radius_pp, lower_pp, upper_pp, n, ci_string)."""
    if d["N"] == 0:
        return ("", "", "", "", "0", "")
    mean = d["mean"] * 100
    rad = d["ci95_radius"] * 100
    lo = d["ci95_lower"] * 100
    hi = d["ci95_upper"] * 100
    return (
        f"{mean:.1f}",
        f"{rad:.2f}",
        f"{lo:.1f}",
        f"{hi:.1f}",
        str(d["N"]),
        f"{mean:.1f} ± {rad:.2f}" if d["N"] >= 2 else f"{mean:.1f}",
    )


def write_csv(path: Path, header: List[str], rows: List[List[Any]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# Table 1 — text-only EHR-Bench
# ---------------------------------------------------------------------------
def build_table1_ci() -> Path:
    """Per (model × {risk_prediction, decision_making, overall}):
    ClinSeek / Curated-input means + 95% CIs and the paired Δ with its CI.
    """
    header = [
        "model", "slug", "task_group", "n",
        "clinseek_mean_pp", "clinseek_ci95_radius_pp", "clinseek_ci95_lower_pp", "clinseek_ci95_upper_pp", "clinseek_display",
        "curated_input_mean_pp", "curated_input_ci95_radius_pp", "curated_input_ci95_lower_pp", "curated_input_ci95_upper_pp", "curated_input_display",
        "delta_mean_pp", "delta_ci95_radius_pp", "delta_ci95_lower_pp", "delta_ci95_upper_pp", "delta_display",
    ]
    rows: List[List[Any]] = []
    for label, slug in TABLE1_SLUGS.items():
        for bucket, loader in [
            ("Risk Prediction", lambda s, p: ehr_l2_scores(s, p, "risk_prediction")),
            ("Decision Making", lambda s, p: ehr_l2_scores(s, p, "decision_making")),
            ("Overall",         lambda s, p: ehr_text_scores(s, p)),
        ]:
            ag = loader(slug, "agentic")
            os_ = loader(slug, "oneshot")
            ci_ag = compute_ci(ag.values())
            ci_os = compute_ci(os_.values())

            common = sorted(set(ag) & set(os_))
            paired_ag = [ag[q] for q in common]
            paired_os = [os_[q] for q in common]
            ci_delta = compute_ci_diff(paired_ag, paired_os)

            ag_fmt = _fmt_pct(ci_ag)
            os_fmt = _fmt_pct(ci_os)
            dl_fmt = _fmt_pct(ci_delta)

            n = ci_ag["N"] if ci_ag["N"] else ci_os["N"]
            rows.append([
                label, slug, bucket, n,
                *ag_fmt[:4], ag_fmt[5],
                *os_fmt[:4], os_fmt[5],
                *dl_fmt[:4], dl_fmt[5],
            ])

    out = OUT_DIR / "table1_text_ehr_results_ci.csv"
    write_csv(out, header, rows)
    return out


# ---------------------------------------------------------------------------
# Table 2 — multimodal
# ---------------------------------------------------------------------------
def build_table2_ci() -> Path:
    header = [
        "model", "slug", "method",
        *sum(([f"{c}_n", f"{c}_mean_pp", f"{c}_ci95_radius_pp",
               f"{c}_ci95_lower_pp", f"{c}_ci95_upper_pp", f"{c}_display"]
              for c, _ in TABLE2_L4_COLUMNS + [("overall", "overall")]), []),
    ]
    rows: List[List[Any]] = []
    for label, slug in TABLE2_SLUGS.items():
        for method, paradigm in (("ClinSeek", "agentic"), ("Curated Input", "oneshot")):
            row: List[Any] = [label, slug, method]
            for _col, l4 in TABLE2_L4_COLUMNS:
                d = compute_ci(mm_l4_scores(slug, paradigm, l4).values())
                m, r, lo, hi, n, disp = _fmt_pct(d)
                row += [n, m, r, lo, hi, disp]
            # overall
            d_ov = compute_ci(mm_overall_scores(slug, paradigm).values())
            m, r, lo, hi, n, disp = _fmt_pct(d_ov)
            row += [n, m, r, lo, hi, disp]
            rows.append(row)

    out = OUT_DIR / "table2_multimodal_results_ci.csv"
    write_csv(out, header, rows)
    return out


# ---------------------------------------------------------------------------
# Table 3 — AgentEHR-Bench subset
# ---------------------------------------------------------------------------
def build_table3_ci() -> Path:
    cols = [c for c, _ in TABLE3_TASKS]
    header = ["model"]
    for c in cols:
        header += [f"{c}_n", f"{c}_mean_pp", f"{c}_ci95_radius_pp",
                   f"{c}_ci95_lower_pp", f"{c}_ci95_upper_pp", f"{c}_display"]
    # avg across per-task means (with CI aggregated across all 5 tasks' per-sample scores
    # — matches the per-task-mean-of-5 used in the paper, but expressed as a pooled
    # per-sample CI, which is the statistically meaningful analogue).
    header += ["avg_n", "avg_mean_pp", "avg_ci95_radius_pp",
               "avg_ci95_lower_pp", "avg_ci95_upper_pp", "avg_display"]
    header += ["notes"]

    rows: List[List[Any]] = []
    for model_label, (kind, key) in TABLE3_SOURCES.items():
        by_task = load_table3_scores(model_label)
        row: List[Any] = [model_label]
        pooled: List[float] = []
        for _col, t_key in TABLE3_TASKS:
            scores = by_task.get(t_key, [])
            pooled.extend(scores)
            d = compute_ci(scores)
            m, r, lo, hi, n, disp = _fmt_pct(d)
            row += [n, m, r, lo, hi, disp]
        d_avg = compute_ci(pooled)
        m, r, lo, hi, n, disp = _fmt_pct(d_avg)
        row += [n, m, r, lo, hi, disp]
        note = {
            "bedrock":          "per-sample F1 recomputed from results.jsonl via helper/evaluate_results.py",
            "ext_task_scores":  f"per-sample F1 from external {key}/task_scores.jsonl",
            "ext_per_question": f"per-sample F1 from external {key}/per_question.jsonl (avg_f1)",
        }[kind]
        row += [note]
        rows.append(row)

    out = OUT_DIR / "table3_agentehr_results_ci.csv"
    write_csv(out, header, rows)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    print("[1/3] Table 1 (text-only EHR)…", flush=True)
    p1 = build_table1_ci()
    print("    →", p1)
    print("[2/3] Table 2 (multimodal)…", flush=True)
    p2 = build_table2_ci()
    print("    →", p2)
    print("[3/3] Table 3 (AgentEHR-Bench)…", flush=True)
    p3 = build_table3_ci()
    print("    →", p3)


if __name__ == "__main__":
    main()
