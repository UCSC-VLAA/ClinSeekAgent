"""Reorganize EHR-Bench + MM-Bench scored outputs under a 4-layer hierarchy.

Layers (top → bottom):
    L1  modality          text_only | multimodal
    L2  overall_task      ehr_bench: risk_prediction | decision_making
                          mm_bench text_only:  ehr_query
                          mm_bench multimodal: risk_prediction_mm | cxr_vqa | phenotyping_mm
    L3  semantic_task     collapse temporal/format variants (ICU_Mortality_*day → ICU_Mortality, ...)
    L4  fine_task         renamed original task (with __ehr_text / __cxr / __ehr_cxr suffix)

Inputs:
  * ehr_bench one-shot + agentic scored results under results/ehr_bench/.../full1800/<model>/
      - raw:    results.jsonl      (one row per qid, label in `label`, finish args in messages[-2].tool_calls[0])
      - scores: scores.json        (per_task / per_task_type from helper/evaluate_results.py)

  * mm_bench scored outputs under results/mm_bench/.../scored/<model>/
      - scored.jsonl               (per-row LLM-judge / set-metric, one entry per qid)
      - summary.json / summary.md  (aggregate)

  * mm_bench task classifier output
      analysis/mm_bench_classify/classification_*.jsonl  (qid → {task, is_multimodal, ...})

Outputs (written to --out-root):
    results/<paradigm>/<model>/
        L1.<modality>/L2.<overall>/L3.<semantic>/L4.<fine>/rows.jsonl      per-row rows with F1, P, R
        L1.<modality>/L2.<overall>/L3.<semantic>/L4.<fine>/summary.json    count / macro-F1 / P / R
        aggregate_by_L1.json
        aggregate_by_L2.json
        aggregate_by_L3.json
        aggregate_by_L4.json
        summary.md                                                         global table

Assumes the one-shot agentic / oneshot run dirs already exist with
helper/evaluate_results.py style per_task outputs. For mm_bench it uses
the scorer_mm scored.jsonl per-row entries.

Usage:
    python reorganize_results.py --out-root results/hierarchical
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


# -----------------------------------------------------------------------------
# Repo paths
# -----------------------------------------------------------------------------

REPO = Path("/fsx-shared/juncheng/EHR")
RESULTS_ROOT = REPO / "openresearcher_ehr/results"
EHR_ONESHOT = RESULTS_ROOT / "ehr_bench/oneshot/full1800"
EHR_AGENTIC = RESULTS_ROOT / "ehr_bench/agentic/full1800"
MM_ONESHOT_SCORED = RESULTS_ROOT / "mm_bench/oneshot/scored"
MM_AGENTIC_SCORED = RESULTS_ROOT / "mm_bench/agentic/scored"
MM_CLASSIFY_DIR = REPO / "openresearcher_ehr/analysis/mm_bench_classify"


# -----------------------------------------------------------------------------
# Taxonomy
# -----------------------------------------------------------------------------

# Fine-task rename map (L4 name) for the mm_bench classifier output.
# L4 names do NOT carry a modality suffix — modality is carried at L1.
MM_FINE_RENAME = {
    "ehrxqa_patient_lookup":      "ehrxqa_patient_lookup",
    "ehrxqa_cohort_query":        "ehrxqa_cohort_query",
    "cxr_finding_presence":       "cxr_finding_presence",
    "cxr_finding_enumeration":    "cxr_finding_enumeration",
    "cxr_change_comparison":      "cxr_change_comparison",
    "mortality_24h":              "mortality_24h",
    "Inpatient_Mortality":        "inpatient_mortality_mm",  # distinguish from ehr_bench's Inpatient_Mortality
    "phenotype_group_assignment": "phenotype_ccs",
}

# Edge-case fine labels to drop (from classifier output)
MM_IGNORE_FINE = {"prescriptions", "other"}

# Semantic-merge table (L3) — covers every ehr_bench task + renamed mm task.
L3_MAP: Dict[str, str] = {
    # ICU mortality across horizons
    "ICU_Mortality_1day":  "ICU_Mortality",
    "ICU_Mortality_2day":  "ICU_Mortality",
    "ICU_Mortality_3day":  "ICU_Mortality",
    "ICU_Mortality_7day":  "ICU_Mortality",
    "ICU_Mortality_14day": "ICU_Mortality",
    # ICU stay length
    "ICU_Stay_7day":  "ICU_StayLength",
    "ICU_Stay_14day": "ICU_StayLength",
    # Hospital length-of-stay
    "LengthOfStay_3day": "LengthOfStay",
    "LengthOfStay_7day": "LengthOfStay",
    # Hospital readmission
    "Readmission_30day": "Readmission_Hospital",
    "Readmission_60day": "Readmission_Hospital",
    # ED/Inpatient mortality
    "ED_Inpatient_Mortality": "Mortality_Hospital",
    "Inpatient_Mortality":    "Mortality_Hospital",
    # Single-variant risk tasks
    "ED_Critical_Outcomes":   "ED_Critical_Outcomes",
    "ED_Hospitalization":     "ED_Hospitalization",
    "ED_ICU_Tranfer_12hour":  "ED_ICU_Transfer_12h",
    "ED_Reattendance_3day":   "ED_Reattendance_3day",
    "ICU_Readmission":        "ICU_Readmission",

    # Decision-making semantic groups
    "diagnoses_ccs":     "Diagnosis_coding",
    "diagnoses_icd":     "Diagnosis_coding",
    "diagnosis":         "Diagnosis_coding",
    "diagnosis_ccs":     "Diagnosis_coding",
    "procedures_ccs":    "Procedure_coding",
    "procedures_icd":    "Procedure_coding",
    "procedureevents":   "Procedure_coding",
    "prescriptions":     "Medication_suggestion",
    "prescriptions_atc": "Medication_suggestion",
    "medrecon":          "Medication_suggestion",
    "medrecon_atc":      "Medication_suggestion",
    "emar":              "Medication_suggestion",
    "pyxis":             "Medication_suggestion",
    "labevents":         "Lab_Microbiology_orders",
    "microbiologyevents":"Lab_Microbiology_orders",
    "chartevents":       "ICU_Events",
    "datetimeevents":    "ICU_Events",
    "inputevents":       "ICU_Events",
    "outputevents":      "ICU_Events",
    "ingredientevents":  "ICU_Events",
    "poe":               "Provider_Orders",
    "radiology":         "Radiology_Orders",
    "admissions":        "Transfers_Services_Admissions",
    "transfers":         "Transfers_Services_Admissions",
    "services":          "Transfers_Services_Admissions",
    "omr":               "Outpatient_Records",
    "next_event":        "Next_Event",

    # mm_bench fine (post-rename) tasks
    "ehrxqa_patient_lookup":   "ehrxqa_patient_query",
    "ehrxqa_cohort_query":     "ehrxqa_cohort_query",
    "cxr_finding_presence":    "cxr_vqa_presence",
    "cxr_finding_enumeration": "cxr_vqa_enumeration",
    "cxr_change_comparison":   "cxr_vqa_change",
    "mortality_24h":           "Mortality_mm_short_term",
    "inpatient_mortality_mm":  "Mortality_mm_full_stay",
    "phenotype_ccs":           "Phenotype_ccs_mm",
}

# Overall / L2 assignments by fine-task (post-rename)
L2_MAP: Dict[str, str] = {}

# ehr_bench risk_prediction
for t in [
    "ED_Critical_Outcomes","ED_Hospitalization","ED_ICU_Tranfer_12hour",
    "ED_Inpatient_Mortality","ED_Reattendance_3day",
    "ICU_Mortality_1day","ICU_Mortality_2day","ICU_Mortality_3day",
    "ICU_Mortality_7day","ICU_Mortality_14day","ICU_Readmission",
    "ICU_Stay_7day","ICU_Stay_14day","Inpatient_Mortality",
    "LengthOfStay_3day","LengthOfStay_7day",
    "Readmission_30day","Readmission_60day",
]:
    L2_MAP[t] = "risk_prediction"

# ehr_bench decision_making
for t in [
    "admissions","chartevents","datetimeevents","diagnoses_ccs","diagnoses_icd",
    "diagnosis","diagnosis_ccs","emar","ingredientevents","inputevents",
    "labevents","medrecon","medrecon_atc","microbiologyevents","next_event",
    "omr","outputevents","poe","prescriptions","prescriptions_atc",
    "procedureevents","procedures_ccs","procedures_icd","pyxis","radiology",
    "services","transfers",
]:
    L2_MAP[t] = "decision_making"

# mm_bench text_only → ehr_query
L2_MAP["ehrxqa_patient_lookup"] = "ehr_query"
L2_MAP["ehrxqa_cohort_query"]   = "ehr_query"
# mm_bench multimodal → cxr_vqa / risk_prediction_mm / phenotyping_mm
L2_MAP["cxr_finding_presence"]    = "cxr_vqa"
L2_MAP["cxr_finding_enumeration"] = "cxr_vqa"
L2_MAP["cxr_change_comparison"]   = "cxr_vqa"
L2_MAP["mortality_24h"]           = "risk_prediction_mm"
L2_MAP["inpatient_mortality_mm"]  = "risk_prediction_mm"
L2_MAP["phenotype_ccs"]           = "phenotyping_mm"

# L1 modality: ehr_bench tasks are always text_only; mm_bench fine tasks are
# text_only iff they are the EHRXQA-table variants, multimodal otherwise.
_MM_MULTIMODAL_FINE = {
    "cxr_finding_presence",
    "cxr_finding_enumeration",
    "cxr_change_comparison",
    "mortality_24h",
    "inpatient_mortality_mm",
    "phenotype_ccs",
}
_MM_TEXT_ONLY_FINE = {
    "ehrxqa_patient_lookup",
    "ehrxqa_cohort_query",
}


def l1_of(fine: str) -> str:
    if fine in _MM_MULTIMODAL_FINE:
        return "multimodal"
    if fine in _MM_TEXT_ONLY_FINE:
        return "text_only"
    # ehr_bench tasks
    return "text_only"


# -----------------------------------------------------------------------------
# Helpers to load ehr_bench per-row data
# -----------------------------------------------------------------------------


def _parse_json_or_py(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return raw
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        try:
            import ast
            return ast.literal_eval(raw)
        except Exception:
            return raw


def _label_to_strings(label: Any) -> List[str]:
    if label is None:
        return []
    if isinstance(label, str):
        return [label.strip()]
    if isinstance(label, list):
        out: List[str] = []
        for x in label:
            if isinstance(x, dict):
                v = x.get("name") or x.get("value") or x.get("text") or x.get("answer")
                if v is None:
                    v = json.dumps(x)
                out.append(str(v).strip())
            else:
                out.append(str(x).strip())
        return [s for s in out if s]
    return [str(label).strip()]


def _extract_finish_predictions(messages: List[Dict[str, Any]]) -> List[str]:
    for msg in reversed(messages or []):
        for tc in reversed(msg.get("tool_calls") or []):
            fn = (tc.get("function") or {}).get("name", "").lower()
            if "finish" not in fn:
                continue
            args = _parse_json_or_py((tc.get("function") or {}).get("arguments", {}))
            if isinstance(args, dict):
                preds = args.get("response", [])
            else:
                preds = args
            if isinstance(preds, list):
                return [str(p).strip() for p in preds if str(p).strip()]
            if isinstance(preds, str) and preds.strip():
                return [preds.strip()]
    return []


def _norm_set(xs: Iterable[str]) -> set:
    return {x.strip().lower() for x in xs if x and x.strip()}


def _f1_prec_rec(pred: List[str], gold: List[str]) -> Tuple[float, float, float]:
    p = _norm_set(pred)
    g = _norm_set(gold)
    if not p and not g:
        return 0.0, 0.0, 0.0
    if not p or not g:
        return 0.0, 0.0, 0.0
    tp = len(p & g)
    prec = tp / len(p)
    rec = tp / len(g)
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return f1, prec, rec


# -----------------------------------------------------------------------------
# EHR-Bench loader
# -----------------------------------------------------------------------------


def load_ehr_bench_rows(run_dir: Path) -> List[Dict[str, Any]]:
    """Return one normalized dict per qid for an ehr_bench full1800 run dir."""
    src = run_dir / "results.jsonl"
    if not src.exists():
        return []
    out = []
    with src.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            qid = r.get("qid") or ""
            task = r.get("task") or ""
            task_type = r.get("task_type") or ""
            gold = _label_to_strings(r.get("label"))
            pred = _extract_finish_predictions(r.get("messages") or [])
            f1, prec, rec = _f1_prec_rec(pred, gold)
            out.append({
                "qid": qid,
                "bench": "ehr_bench",
                "fine_task": task,            # original 45-task name
                "ehr_bench_group": task_type, # risk_prediction | decision_making
                "gold": gold,
                "pred": pred,
                "f1": f1,
                "precision": prec,
                "recall": rec,
                "is_multimodal": False,
            })
    return out


# -----------------------------------------------------------------------------
# MM-Bench loader
# -----------------------------------------------------------------------------


def load_mm_classifier(latest_only: bool = True) -> Dict[str, Dict[str, Any]]:
    """Load the latest mm_bench classifier output, keyed by qid."""
    files = sorted(MM_CLASSIFY_DIR.glob("classification_*.jsonl"))
    if not files:
        raise SystemExit("No mm_bench classifier file found under analysis/mm_bench_classify/")
    target = files[-1] if latest_only else files[0]
    by_qid: Dict[str, Dict[str, Any]] = {}
    with target.open() as f:
        for line in f:
            r = json.loads(line)
            cls = r.get("classification") or r.get("classification_fallback") or {}
            by_qid[r["qid"]] = {
                "raw_task":  cls.get("task") or "other",
                "is_multimodal": bool(cls.get("is_multimodal")),
                "multimodal_kind": cls.get("multimodal_kind") or "none",
                "coarse_task": r.get("coarse_task"),
            }
    print(f"[mm classifier] loaded {len(by_qid)} rows from {target.name}", flush=True)
    return by_qid


def load_mm_bench_rows(run_dir: Path, classifier: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """run_dir points to results/mm_bench/.../scored/<model>/ with scored.jsonl.

    Each scored row already has f1 / precision / recall (either via LLM judge
    or rule-based set metrics). We just wrap them into our common format and
    attach the fine task from the classifier.
    """
    src = run_dir / "scored.jsonl"
    if not src.exists():
        src = run_dir / "rescored.jsonl"
    if not src.exists():
        return []
    out = []
    dropped = 0
    with src.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            qid = r.get("qid") or ""
            cls = classifier.get(qid)
            if not cls:
                dropped += 1
                continue
            raw_fine = cls["raw_task"]
            if raw_fine in MM_IGNORE_FINE:
                dropped += 1
                continue
            fine = MM_FINE_RENAME.get(raw_fine, raw_fine)
            if fine not in L2_MAP:
                # Unknown fine task — skip
                dropped += 1
                continue
            gold = [str(g) for g in (r.get("gold_names") or [])]
            pred = [str(p) for p in (r.get("prediction_strs") or r.get("prediction_flat") or [])]
            f1 = float(r.get("f1") or 0.0)
            prec = float(r.get("precision") or 0.0)
            rec = float(r.get("recall") or 0.0)
            out.append({
                "qid": qid,
                "bench": "mm_bench",
                "fine_task": fine,
                "gold": gold,
                "pred": pred,
                "f1": f1,
                "precision": prec,
                "recall": rec,
                "is_multimodal": cls["is_multimodal"],
                "multimodal_kind": cls["multimodal_kind"],
                "coarse_task": cls["coarse_task"],
            })
    if dropped:
        print(f"[mm] {run_dir.name}: dropped {dropped} rows "
              f"(missing classifier or ignored fine task)", flush=True)
    return out


# -----------------------------------------------------------------------------
# Hierarchy aggregation
# -----------------------------------------------------------------------------


def layer_labels(fine: str, bench: str) -> Tuple[str, str, str, str]:
    l1 = l1_of(fine)
    l2 = L2_MAP[fine]
    l3 = L3_MAP.get(fine, fine)
    l4 = fine
    return l1, l2, l3, l4


def aggregate_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Bucket rows by (L1,L2,L3,L4) and compute micro-average F1/P/R."""
    grouped: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        key = layer_labels(r["fine_task"], r["bench"])
        grouped[key].append(r)
    # Build nested aggregate
    out: Dict[str, Any] = {}
    for (l1, l2, l3, l4), group in grouped.items():
        node = out.setdefault(l1, {}).setdefault(l2, {}).setdefault(l3, {}).setdefault(l4, {
            "count": 0,
            "f1_sum": 0.0, "prec_sum": 0.0, "rec_sum": 0.0,
        })
        for r in group:
            node["count"] += 1
            node["f1_sum"] += r["f1"]
            node["prec_sum"] += r["precision"]
            node["rec_sum"] += r["recall"]
    # Roll-up means at every layer
    def _finalize(d: Dict[str, Any]) -> Tuple[int, float, float, float]:
        if "count" in d and "f1_sum" in d and set(d.keys()) <= {"count", "f1_sum", "prec_sum", "rec_sum"}:
            n = d["count"] or 1
            d["f1"] = d["f1_sum"] / n
            d["precision"] = d["prec_sum"] / n
            d["recall"] = d["rec_sum"] / n
            return d["count"], d["f1_sum"], d["prec_sum"], d["rec_sum"]
        n = fs = ps = rs = 0
        for k, v in d.items():
            if k in ("count", "f1", "precision", "recall", "f1_sum", "prec_sum", "rec_sum"):
                continue
            cn, cf, cp, cr = _finalize(v)
            n += cn; fs += cf; ps += cp; rs += cr
        d["count"] = n
        d["f1_sum"] = fs
        d["prec_sum"] = ps
        d["rec_sum"] = rs
        d["f1"] = fs / n if n else 0.0
        d["precision"] = ps / n if n else 0.0
        d["recall"] = rs / n if n else 0.0
        return n, fs, ps, rs
    _finalize(out)
    return out


# -----------------------------------------------------------------------------
# Emit per-model hierarchy
# -----------------------------------------------------------------------------


def write_markdown(agg: Dict[str, Any], path: Path, title: str) -> None:
    lines: List[str] = [f"# {title}\n"]

    # Global counts
    lines.append(f"- total rows: {agg.get('count', 0) if 'count' in agg else sum(v.get('count',0) for v in agg.values())}")

    # Flat dump at each layer for readability
    def _walk(node: Dict[str, Any], path_so_far: List[str], depth: int, max_depth: int):
        if depth > max_depth:
            return
        for key, sub in node.items():
            if key in ("count", "f1", "precision", "recall", "f1_sum", "prec_sum", "rec_sum"):
                continue
            cnt = sub.get("count", 0)
            f1 = sub.get("f1", 0.0) * 100
            p = sub.get("precision", 0.0) * 100
            r = sub.get("recall", 0.0) * 100
            indent = "  " * depth
            lines.append(f"{indent}- **{key}** (n={cnt})  F1 {f1:5.1f}  P {p:5.1f}  R {r:5.1f}")
            _walk(sub, path_so_far + [key], depth + 1, max_depth)

    lines.append("\n## Full tree\n")
    _walk(agg, [], 0, 4)

    # L2 and L3 summary tables
    lines.append("\n## L1 × L2 summary\n")
    lines.append("| L1 modality | L2 overall | n | F1 | P | R |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for l1, sub1 in agg.items():
        if not isinstance(sub1, dict):
            continue
        for l2, sub2 in sub1.items():
            if not isinstance(sub2, dict) or l2 in ("count","f1","precision","recall","f1_sum","prec_sum","rec_sum"):
                continue
            lines.append(f"| {l1} | {l2} | {sub2.get('count',0)} | "
                         f"{sub2.get('f1',0)*100:.1f} | "
                         f"{sub2.get('precision',0)*100:.1f} | "
                         f"{sub2.get('recall',0)*100:.1f} |")

    lines.append("\n## L3 semantic-task summary\n")
    lines.append("| L1 | L2 | L3 semantic | n | F1 | P | R |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for l1, sub1 in agg.items():
        if not isinstance(sub1, dict): continue
        for l2, sub2 in sub1.items():
            if not isinstance(sub2, dict) or l2 in ("count","f1","precision","recall","f1_sum","prec_sum","rec_sum"): continue
            for l3, sub3 in sub2.items():
                if not isinstance(sub3, dict) or l3 in ("count","f1","precision","recall","f1_sum","prec_sum","rec_sum"): continue
                lines.append(f"| {l1} | {l2} | {l3} | {sub3.get('count',0)} | "
                             f"{sub3.get('f1',0)*100:.1f} | "
                             f"{sub3.get('precision',0)*100:.1f} | "
                             f"{sub3.get('recall',0)*100:.1f} |")
    path.write_text("\n".join(lines))


def dump_model_tree(
    model: str,
    paradigm: str,
    bench_source: str,
    rows: List[Dict[str, Any]],
    out_root: Path,
) -> Dict[str, Any]:
    agg = aggregate_rows(rows)
    base = out_root / paradigm / bench_source / model
    base.mkdir(parents=True, exist_ok=True)
    # Per-row dumps under L1/L2/L3/L4/
    for r in rows:
        l1, l2, l3, l4 = layer_labels(r["fine_task"], r["bench"])
        cell = base / f"L1.{l1}" / f"L2.{l2}" / f"L3.{l3}" / f"L4.{l4}"
        cell.mkdir(parents=True, exist_ok=True)
        with (cell / "rows.jsonl").open("a") as f:
            f.write(json.dumps({
                "qid": r["qid"], "f1": r["f1"], "precision": r["precision"],
                "recall": r["recall"], "gold": r["gold"], "pred": r["pred"],
                "bench": r["bench"],
            }) + "\n")
    # Per-cell summary.json
    def _walk_and_emit(node: Dict[str, Any], path: Path, layer: int):
        for k, v in node.items():
            if not isinstance(v, dict) or k in ("count","f1","precision","recall","f1_sum","prec_sum","rec_sum"):
                continue
            cell_path = path / f"L{layer+1}.{k}"
            if (cell_path).exists():
                (cell_path / "summary.json").write_text(json.dumps({
                    "count": v.get("count",0),
                    "f1":     v.get("f1",0.0),
                    "precision": v.get("precision",0.0),
                    "recall": v.get("recall",0.0),
                }, indent=2))
                _walk_and_emit(v, cell_path, layer + 1)
    _walk_and_emit(agg, base, 0)
    # Aggregate files at the model root
    (base / "aggregate.json").write_text(json.dumps(agg, indent=2))
    write_markdown(agg, base / "summary.md", f"{paradigm} / {bench_source} / {model}")
    return agg


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root",
                    default=str(RESULTS_ROOT / "hierarchical"),
                    help="Where to emit the reorganized tree.")
    ap.add_argument("--models", nargs="*", default=None,
                    help="Optional subset of model slugs (intersection of what's present).")
    args = ap.parse_args()
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    classifier = load_mm_classifier()

    def _model_dirs(root: Path) -> List[str]:
        if not root.exists():
            return []
        return sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and not p.name.endswith("_retried")
        )

    model_slugs_ehr_oneshot = _model_dirs(EHR_ONESHOT)
    model_slugs_ehr_agentic = _model_dirs(EHR_AGENTIC)
    model_slugs_mm_oneshot = sorted(p.name for p in MM_ONESHOT_SCORED.iterdir() if p.is_dir() and not p.name.startswith("smoke")) if MM_ONESHOT_SCORED.exists() else []
    model_slugs_mm_agentic = sorted(p.name for p in MM_AGENTIC_SCORED.iterdir() if p.is_dir() and not p.name.startswith("smoke")) if MM_AGENTIC_SCORED.exists() else []

    def _filter(slugs: List[str]) -> List[str]:
        if args.models:
            return [s for s in slugs if s in args.models]
        return slugs

    print(f"ehr_bench oneshot models: {_filter(model_slugs_ehr_oneshot)}")
    print(f"ehr_bench agentic models: {_filter(model_slugs_ehr_agentic)}")
    print(f"mm_bench  oneshot models: {_filter(model_slugs_mm_oneshot)}")
    print(f"mm_bench  agentic models: {_filter(model_slugs_mm_agentic)}")

    overall: Dict[Tuple[str, str, str], Dict[str, Any]] = {}  # (paradigm, bench, model) -> agg

    for model in _filter(model_slugs_ehr_oneshot):
        rows = load_ehr_bench_rows(EHR_ONESHOT / model)
        print(f"  ehr oneshot / {model}: {len(rows)} rows")
        if rows:
            overall[("oneshot","ehr_bench",model)] = dump_model_tree(model, "oneshot", "ehr_bench", rows, out_root)

    for model in _filter(model_slugs_ehr_agentic):
        rows = load_ehr_bench_rows(EHR_AGENTIC / model)
        print(f"  ehr agentic / {model}: {len(rows)} rows")
        if rows:
            overall[("agentic","ehr_bench",model)] = dump_model_tree(model, "agentic", "ehr_bench", rows, out_root)

    for model in _filter(model_slugs_mm_oneshot):
        rows = load_mm_bench_rows(MM_ONESHOT_SCORED / model, classifier)
        print(f"  mm oneshot / {model}: {len(rows)} rows")
        if rows:
            overall[("oneshot","mm_bench",model)] = dump_model_tree(model, "oneshot", "mm_bench", rows, out_root)

    for model in _filter(model_slugs_mm_agentic):
        rows = load_mm_bench_rows(MM_AGENTIC_SCORED / model, classifier)
        print(f"  mm agentic / {model}: {len(rows)} rows")
        if rows:
            overall[("agentic","mm_bench",model)] = dump_model_tree(model, "agentic", "mm_bench", rows, out_root)

    # Top-level summary: (paradigm, bench, model) × L1×L2 scoreboard
    lines = ["# Hierarchical result board\n"]
    for (paradigm, bench, model), agg in sorted(overall.items()):
        lines.append(f"\n## {paradigm} / {bench} / {model}\n")
        lines.append(f"- total rows: {agg.get('count',0)}")
        for l1, sub1 in agg.items():
            if not isinstance(sub1, dict) or l1 in ("count","f1","precision","recall","f1_sum","prec_sum","rec_sum"):
                continue
            lines.append(f"  - **{l1}** (n={sub1.get('count',0)})  F1 {sub1.get('f1',0)*100:5.1f}")
            for l2, sub2 in sub1.items():
                if not isinstance(sub2, dict) or l2 in ("count","f1","precision","recall","f1_sum","prec_sum","rec_sum"):
                    continue
                lines.append(f"    - L2 {l2}: n={sub2.get('count',0)} F1 {sub2.get('f1',0)*100:.1f}")
    (out_root / "README.md").write_text("\n".join(lines))

    print(f"\nHierarchy written to: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
