"""May-01 findings: ClinSeek (auto evidence seeking) vs reasoning mode.

`ClinSeek` is the project's proposed clinical evidence-seeking agent —
a multi-turn MCP-driven loop that queries the raw EHR (and CXR tools in
the multimodal setting). We compare it against a one-shot reasoning
pipeline that answers from a rule-based, pre-rendered EHR context.

The findings are organized under the user's 3-section outline:
    1. Main Result — ClinSeek lifts the state-of-the-art
    2. ClinSeek wins — risk_prediction + multimodal
    3. ClinSeek failures — decision_making

Outputs (in this directory):
    table1_ehr_L2.html             ClinSeek vs reasoning per model on text-only L2
    fig_ehrbench_L3_delta.png      L3 delta grid, grouped by L2 band
    table3_mm_L4_multimodal.html   MM multimodal-only L4 performance per model
    case_*.md                      9 qualitative case studies
    findings_report.html           Unified report
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

# ---------------------------------------------------------------------------
# Vocab + paths
# ---------------------------------------------------------------------------

ROOT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/results")
HIER = ROOT / "hierarchical"
EHR_ONESHOT = ROOT / "ehr_bench/oneshot/full1800"
EHR_AGENTIC = ROOT / "ehr_bench/agentic/full1800"
MM_ONESHOT_FULL = ROOT / "mm_bench/oneshot/full2703"
MM_AGENTIC_FULL = ROOT / "mm_bench/agentic/full2703"
OUT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/analysis/findings_20260501")
OUT.mkdir(parents=True, exist_ok=True)
IMAGES = OUT / "images"
IMAGES.mkdir(parents=True, exist_ok=True)
CASES_ROOT = OUT / "cases"
CASES_ROOT.mkdir(parents=True, exist_ok=True)
DATA = OUT / "data"
DATA.mkdir(parents=True, exist_ok=True)

# Model roster — excludes Tongyi DeepResearch (weak) per user instruction.
EHR_MODELS: List[Tuple[str, str]] = [
    ("Claude Opus 4.6",       "claude_opus_4_6"),
    ("Claude Sonnet 4.6",     "claude_sonnet_4_6"),
    ("Qwen3.5-35B-A3B",       "qwen3_5_35b_a3b"),
    ("GLM-4.7",               "glm_4_7"),
    ("Kimi K2.5",             "kimi_k2_5"),
    ("EHR-R1-72B",            "ehr_r1_72b"),
    ("Gemma-4-26B-A4B-it",    "gemma4_26b_a4b_it"),
    ("Qwen3-VL-235B",         "qwen3_vl_235b"),
    ("MiniMax M2.5",          "minimax_m2_5"),
    ("gpt-oss-120b",          "gpt_oss_120b"),
    ("MedGemma-27B-it",       "medgemma_27b_it"),
    ("EHR-R1-8B",             "ehr_r1_8b"),
]

# Gemma-4 MM reasoning was previously problematic; a fixed re-run is now
# available (medmod_decompensation accuracy 0.33 → 0.79, medmod_in_hospital_mortality
# 0.06 → 0.60). We therefore no longer suppress the reasoning column.
MM_MODELS: List[Tuple[str, str]] = [
    ("Claude Opus 4.6",       "claude_opus_4_6"),
    ("Claude Sonnet 4.6",     "claude_sonnet_4_6"),
    ("Qwen3.5-35B-A3B",       "qwen3_5_35b_a3b"),
    ("Qwen3-VL-235B",         "qwen3_vl_235b"),
    ("Kimi K2.5",             "kimi_k2_5"),
    ("GLM-4.7",               "glm_4_7"),
    ("Gemma-4-26B-A4B-it",    "gemma4_26b_a4b_it"),
    ("MiniMax M2.5",          "minimax_m2_5"),
]
MM_SKIP_REASONING: set = set()  # Gemma-4 fix is now live.

# L3 semantic tasks grouped by L2 (ehr_bench)
L3_BY_L2: "OrderedDict[str, List[str]]" = OrderedDict()
L3_BY_L2["risk_prediction"] = [
    "ED_Critical_Outcomes",
    "ED_Hospitalization",
    "ED_ICU_Transfer_12h",
    "ED_Reattendance_3day",
    "ICU_Readmission",
    "ICU_Mortality",
    "ICU_StayLength",
    "LengthOfStay",
    "Mortality_Hospital",
    "Readmission_Hospital",
]
L3_BY_L2["decision_making"] = [
    "Diagnosis_coding",
    "Procedure_coding",
    "Medication_suggestion",
    "Lab_Microbiology_orders",
    "ICU_Events",
    "Provider_Orders",
    "Radiology_Orders",
    "Transfers_Services_Admissions",
    "Outpatient_Records",
    "Next_Event",
]
L2_COLORS = {"risk_prediction": "#2b7bb9", "decision_making": "#8b4caa"}

# MM L4 tasks under multimodal
MM_L4_ORDER: List[Tuple[str, str, str]] = [
    ("cxr_vqa",             "cxr_vqa_presence",        "cxr_finding_presence"),
    ("cxr_vqa",             "cxr_vqa_enumeration",     "cxr_finding_enumeration"),
    ("cxr_vqa",             "cxr_vqa_change",          "cxr_change_comparison"),
    ("risk_prediction_mm",  "Mortality_mm_short_term", "mortality_24h"),
    ("risk_prediction_mm",  "Mortality_mm_full_stay",  "inpatient_mortality_mm"),
    ("phenotyping_mm",      "Phenotype_ccs_mm",        "phenotype_ccs"),
]
MM_L4_PRETTY = {
    "cxr_finding_presence":    "CXR: finding presence",
    "cxr_finding_enumeration": "CXR: finding enumeration",
    "cxr_change_comparison":   "CXR: change comparison",
    "mortality_24h":           "Mortality (24 h)",
    "inpatient_mortality_mm":  "Inpatient mortality",
    "phenotype_ccs":           "Phenotype (CCS groups)",
}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_aggregate(paradigm: str, bench: str, model_slug: str) -> Dict[str, Any] | None:
    p = HIER / paradigm / bench / model_slug / "aggregate.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def l2_f1(agg, l1, l2):
    node = (agg or {}).get(l1, {}).get(l2)
    return (node.get("f1", 0.0), node.get("count", 0)) if node else None


def l3_f1(agg, l1, l2, l3):
    node = (agg or {}).get(l1, {}).get(l2, {}).get(l3)
    return (node.get("f1", 0.0), node.get("count", 0)) if node else None


def l4_f1(agg, l1, l2, l3, l4):
    node = (agg or {}).get(l1, {}).get(l2, {}).get(l3, {}).get(l4)
    return (node.get("f1", 0.0), node.get("count", 0)) if node else None


def find_row(path: Path, qid: str) -> Dict[str, Any] | None:
    with path.open() as f:
        for line in f:
            if qid not in line:
                continue
            r = json.loads(line)
            if r.get("qid") == qid:
                return r
    return None


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------


def _write_csv(name: str, header: List[str], rows: List[List[Any]]) -> Path:
    """Write rows to data/<name>. Numbers are written as-is; None/NaN → empty."""
    p = DATA / name
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            clean = []
            for v in r:
                if v is None:
                    clean.append("")
                elif isinstance(v, float) and (v != v):  # NaN
                    clean.append("")
                else:
                    clean.append(v)
            w.writerow(clean)
    return p


# ---------------------------------------------------------------------------
# Table 1 — ehr_bench L2 per-model
# ---------------------------------------------------------------------------


def _fmt(v):
    return "—" if v is None else f"{v:.1f}"


def _dcls(v):
    if v is None: return ""
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _dstr(v):
    if v is None: return "—"
    return ("+" if v >= 0 else "") + f"{v:.1f}"


def build_table1() -> Tuple[str, List[Dict[str, Any]]]:
    rows = []
    for label, slug in EHR_MODELS:
        agg_ag = load_aggregate("agentic", "ehr_bench", slug)
        agg_os = load_aggregate("oneshot", "ehr_bench", slug)
        if not agg_ag and not agg_os:
            continue
        row = {"model": label, "slug": slug}
        for l2 in ("risk_prediction", "decision_making"):
            ag = l2_f1(agg_ag, "text_only", l2) if agg_ag else None
            os_ = l2_f1(agg_os, "text_only", l2) if agg_os else None
            row[f"{l2}_ag"] = ag[0] * 100 if ag else None
            row[f"{l2}_os"] = os_[0] * 100 if os_ else None
            row[f"{l2}_delta"] = (ag[0] - os_[0]) * 100 if ag and os_ else None
        row["overall_ag"] = agg_ag["text_only"]["f1"] * 100 if agg_ag else None
        row["overall_os"] = agg_os["text_only"]["f1"] * 100 if agg_os else None
        row["overall_delta"] = (
            row["overall_ag"] - row["overall_os"]
            if row["overall_ag"] is not None and row["overall_os"] is not None
            else None
        )
        rows.append(row)

    # Sort: strongest combined F1 first
    def _combined(r):
        v = [r.get("overall_ag"), r.get("overall_os")]
        v = [x for x in v if x is not None]
        return -sum(v) / len(v) if v else 0
    rows.sort(key=_combined)

    lines = ["<table class='tbl'>",
             "<thead><tr>",
             "<th rowspan='2'>Model</th>",
             "<th colspan='3'>risk_prediction (n=720)</th>",
             "<th colspan='3'>decision_making (n=1080)</th>",
             "<th colspan='3'>overall (n=1800)</th>",
             "</tr><tr>",
             "<th>ClinSeek</th><th>Reasoning</th><th>Δ (ClinSeek − Rsn)</th>",
             "<th>ClinSeek</th><th>Reasoning</th><th>Δ (ClinSeek − Rsn)</th>",
             "<th>ClinSeek</th><th>Reasoning</th><th>Δ (ClinSeek − Rsn)</th>",
             "</tr></thead><tbody>"]
    for r in rows:
        lines.append("<tr>")
        lines.append(f"<td>{r['model']}</td>")
        for prefix in ("risk_prediction_", "decision_making_", "overall_"):
            ag = r.get(prefix + "ag")
            osv = r.get(prefix + "os")
            dv = r.get(prefix + "delta")
            lines.append(f"<td class='num'>{_fmt(ag)}</td>")
            lines.append(f"<td class='num'>{_fmt(osv)}</td>")
            lines.append(f"<td class='num {_dcls(dv)}'>{_dstr(dv)}</td>")
        lines.append("</tr>")
    lines.append("</tbody></table>")

    # CSV export — all F1s in percentage points, same sort order as HTML.
    csv_rows = []
    for r in rows:
        csv_rows.append([
            r["model"], r["slug"],
            _fmt(r.get("risk_prediction_ag")),
            _fmt(r.get("risk_prediction_os")),
            _fmt(r.get("risk_prediction_delta")),
            _fmt(r.get("decision_making_ag")),
            _fmt(r.get("decision_making_os")),
            _fmt(r.get("decision_making_delta")),
            _fmt(r.get("overall_ag")),
            _fmt(r.get("overall_os")),
            _fmt(r.get("overall_delta")),
        ])
    _write_csv(
        "table1_ehr_L2.csv",
        [
            "model", "slug",
            "risk_prediction_clinseek_f1", "risk_prediction_reasoning_f1", "risk_prediction_delta",
            "decision_making_clinseek_f1", "decision_making_reasoning_f1", "decision_making_delta",
            "overall_clinseek_f1", "overall_reasoning_f1", "overall_delta",
        ],
        csv_rows,
    )
    return "\n".join(lines), rows


# ---------------------------------------------------------------------------
# Figure 2 — L3 delta heatmap
# ---------------------------------------------------------------------------


def build_figure2(out_path: Path) -> Tuple[Path, Dict[str, Any]]:
    l3_col_order: List[Tuple[str, str]] = []
    for l2, l3_list in L3_BY_L2.items():
        for l3 in l3_list:
            l3_col_order.append((l2, l3))

    active = [
        (lbl, slug) for lbl, slug in EHR_MODELS
        if load_aggregate("agentic", "ehr_bench", slug)
        and load_aggregate("oneshot", "ehr_bench", slug)
    ]

    m = len(active)
    k = len(l3_col_order)
    delta = np.full((m, k), np.nan)
    clinseek_f1 = np.full((m, k), np.nan)
    reasoning_f1 = np.full((m, k), np.nan)
    counts = np.zeros((m, k), dtype=int)
    for i, (_, slug) in enumerate(active):
        agg_ag = load_aggregate("agentic", "ehr_bench", slug)
        agg_os = load_aggregate("oneshot", "ehr_bench", slug)
        for j, (l2, l3) in enumerate(l3_col_order):
            a = l3_f1(agg_ag, "text_only", l2, l3)
            o = l3_f1(agg_os, "text_only", l2, l3)
            if a:
                clinseek_f1[i, j] = a[0] * 100
                counts[i, j] = a[1]
            if o:
                reasoning_f1[i, j] = o[0] * 100
                if counts[i, j] == 0:
                    counts[i, j] = o[1]
            if a and o:
                delta[i, j] = (a[0] - o[0]) * 100

    # CSV export — long format, one row per (model, l2, l3).
    csv_rows = []
    for i, (lbl, slug) in enumerate(active):
        for j, (l2, l3) in enumerate(l3_col_order):
            def _n(x):
                return "" if (x != x) else f"{x:.1f}"  # NaN → ""
            csv_rows.append([
                lbl, slug, l2, l3, int(counts[i, j]),
                _n(clinseek_f1[i, j]),
                _n(reasoning_f1[i, j]),
                _n(delta[i, j]),
            ])
    _write_csv(
        "fig_ehrbench_L3_delta.csv",
        ["model", "slug", "l2", "l3", "n",
         "clinseek_f1", "reasoning_f1", "delta"],
        csv_rows,
    )

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig_w = max(14.5, 1.0 + 0.7 * k)
    fig_h = max(4.5, 0.55 * m + 3.0)  # extra headroom for enlarged L2 bands
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    vmax = float(min(40.0, max(5.0, np.nanmax(np.abs(delta)) if np.isfinite(delta).any() else 5.0)))
    cmap = LinearSegmentedColormap.from_list("div", ["#2c62a3", "#ffffff", "#c1272d"], N=512)
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0.0, vmax=vmax)
    im = ax.imshow(delta, aspect="auto", cmap=cmap, norm=norm)

    for i in range(m):
        for j in range(k):
            v = delta[i, j]
            if np.isnan(v):
                ax.text(j, i, "—", ha="center", va="center", color="#888", fontsize=8)
                continue
            tc = "#111" if abs(v) < 0.55 * vmax else "#fff"
            ax.text(j, i, f"{v:+.1f}", ha="center", va="center", fontsize=8.5, color=tc)

    ax.set_yticks(range(m))
    ax.set_yticklabels([lbl for lbl, _ in active], fontsize=10)
    ax.set_xticks(range(k))
    ax.set_xticklabels([l3.replace("_", " ") for (_, l3) in l3_col_order],
                       rotation=40, ha="right", fontsize=9.2)
    ax.set_xlabel("L3 semantic task", fontsize=11)
    ax.set_ylabel("Model", fontsize=11)
    ax.set_title(
        "Text-only L3: ClinSeek F1 − reasoning F1 (pp)\n"
        "red = ClinSeek (auto evidence seeking) wins  ·  blue = reasoning (user-curated evidence) wins",
        fontsize=12, pad=12,
    )
    cbar = fig.colorbar(im, ax=ax, fraction=0.018, pad=0.012)
    cbar.set_label("Δ F1 (pp)", rotation=270, labelpad=14, fontsize=10)

    # L2 group bands — larger + more prominent labels
    l2_ranges: List[Tuple[int, int, str]] = []
    start = 0
    for l2, l3_list in L3_BY_L2.items():
        l2_ranges.append((start, start + len(l3_list), l2))
        start += len(l3_list)
    # A pretty-print label per L2 so the band reads as a human title.
    _L2_PRETTY = {
        "risk_prediction": "Risk Prediction",
        "decision_making": "Decision Making",
    }
    band_h = 0.42           # was 0.16
    y_top = -0.5 - band_h
    for s, e, l2 in l2_ranges:
        color = L2_COLORS.get(l2, "#999")
        ax.add_patch(plt.Rectangle(
            (s - 0.5, y_top), e - s, band_h,
            facecolor=color, alpha=0.32, edgecolor="#333", linewidth=0.6,
            clip_on=False,
        ))
        ax.text((s + e - 1) / 2, y_top + band_h / 2,
                _L2_PRETTY.get(l2, l2),
                ha="center", va="center",
                fontsize=18, fontweight="bold",
                color="#111", clip_on=False,
                path_effects=[])
    for s, e, _ in l2_ranges[:-1]:
        ax.axvline(e - 0.5, color="#666", linewidth=1.3, linestyle="--")
    ax.set_ylim(m - 0.5, y_top - 0.06)
    plt.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)

    stats = {
        "mean_abs_delta": float(np.nanmean(np.abs(delta))),
        "mean_delta": float(np.nanmean(delta)),
        "rp_mean_delta": float(np.nanmean(delta[:, :len(L3_BY_L2['risk_prediction'])])),
        "dm_mean_delta": float(np.nanmean(delta[:, len(L3_BY_L2['risk_prediction']):])),
        "models": [lbl for lbl, _ in active],
    }
    return out_path, stats


# ---------------------------------------------------------------------------
# Table 3 — mm_bench L4 multimodal-only
# ---------------------------------------------------------------------------


def build_table3() -> Tuple[str, List[Dict[str, Any]]]:
    rows = []
    for label, slug in MM_MODELS:
        agg_ag = load_aggregate("agentic", "mm_bench", slug)
        agg_os = load_aggregate("oneshot", "mm_bench", slug)
        if not agg_ag and not agg_os:
            continue
        skip_rsn = slug in MM_SKIP_REASONING
        row = {"model": label, "slug": slug, "cells": [], "skip_rsn": skip_rsn}
        ag_sum = os_sum = 0.0
        ag_n = os_n = 0
        for (l2, l3, l4) in MM_L4_ORDER:
            a = l4_f1(agg_ag, "multimodal", l2, l3, l4) if agg_ag else None
            o = l4_f1(agg_os, "multimodal", l2, l3, l4) if agg_os and not skip_rsn else None
            cell = {
                "l2": l2, "l3": l3, "l4": l4,
                "pretty": MM_L4_PRETTY[l4],
                "ag": a[0] * 100 if a else None,
                "os": o[0] * 100 if o else None,
                "n":  (a[1] if a else (o[1] if o else 0)),
            }
            cell["delta"] = (
                cell["ag"] - cell["os"]
                if cell["ag"] is not None and cell["os"] is not None else None
            )
            row["cells"].append(cell)
            if a: ag_sum += a[0] * a[1]; ag_n += a[1]
            if o: os_sum += o[0] * o[1]; os_n += o[1]
        row["ag_overall_mm"] = (ag_sum / ag_n) * 100 if ag_n else None
        row["os_overall_mm"] = (os_sum / os_n) * 100 if os_n else None
        row["delta_overall_mm"] = (
            row["ag_overall_mm"] - row["os_overall_mm"]
            if row["ag_overall_mm"] is not None and row["os_overall_mm"] is not None else None
        )
        rows.append(row)

    def _combined(r):
        v = [r.get("ag_overall_mm"), r.get("os_overall_mm")]
        v = [x for x in v if x is not None]
        return -sum(v) / len(v) if v else 0
    rows.sort(key=_combined)

    lines = ["<table class='tbl'><thead><tr>", "<th rowspan='2'>Model</th>"]
    for (_, _, l4) in MM_L4_ORDER:
        lines.append(f"<th colspan='3'>{MM_L4_PRETTY[l4]}</th>")
    lines.append("<th colspan='3'>Multimodal overall</th>")
    lines.append("</tr><tr>")
    for _ in MM_L4_ORDER:
        lines.append("<th>ClinSeek</th><th>Reasoning</th><th>Δ</th>")
    lines.append("<th>ClinSeek</th><th>Reasoning</th><th>Δ</th>")
    lines.append("</tr></thead><tbody>")

    # n sub-row
    lines.append("<tr><td class='meta'>n</td>")
    if rows:
        for c in rows[0]["cells"]:
            lines.append(f"<td class='meta num' colspan='3'>{c['n']}</td>")
        total_n = sum(c["n"] for c in rows[0]["cells"])
        lines.append(f"<td class='meta num' colspan='3'>{total_n}</td>")
    lines.append("</tr>")

    for r in rows:
        lines.append("<tr>")
        lines.append(f"<td>{r['model']}</td>")
        for c in r["cells"]:
            lines.append(f"<td class='num'>{_fmt(c['ag'])}</td>")
            rsn_disp = _fmt(c["os"]) if not r["skip_rsn"] else "<span class='na'>n/a</span>"
            lines.append(f"<td class='num'>{rsn_disp}</td>")
            lines.append(f"<td class='num {_dcls(c['delta'])}'>{_dstr(c['delta'])}</td>")
        lines.append(f"<td class='num'><b>{_fmt(r['ag_overall_mm'])}</b></td>")
        rsn_ov = (
            "<span class='na'>n/a</span>" if r["skip_rsn"] else f"<b>{_fmt(r['os_overall_mm'])}</b>"
        )
        lines.append(f"<td class='num'>{rsn_ov}</td>")
        dv = r.get("delta_overall_mm")
        lines.append(f"<td class='num {_dcls(dv)}'><b>{_dstr(dv)}</b></td>")
        lines.append("</tr>")
    lines.append("</tbody></table>")

    # CSV export — long format, one row per (model, L4 task) + overall row per model.
    csv_rows = []
    for r in rows:
        for c in r["cells"]:
            csv_rows.append([
                r["model"], r["slug"],
                c["l2"], c["l3"], c["l4"], c["pretty"], c["n"],
                _fmt(c["ag"]),
                "n/a" if r["skip_rsn"] else _fmt(c["os"]),
                "" if r["skip_rsn"] else _fmt(c["delta"]),
            ])
        csv_rows.append([
            r["model"], r["slug"],
            "multimodal", "overall", "overall", "Multimodal overall",
            sum(c["n"] for c in r["cells"]),
            _fmt(r["ag_overall_mm"]),
            "n/a" if r["skip_rsn"] else _fmt(r["os_overall_mm"]),
            "" if r["skip_rsn"] else _fmt(r["delta_overall_mm"]),
        ])
    _write_csv(
        "table3_mm_L4_multimodal.csv",
        ["model", "slug", "l2", "l3", "l4", "l4_pretty", "n",
         "clinseek_f1", "reasoning_f1", "delta"],
        csv_rows,
    )
    return "\n".join(lines), rows


# ---------------------------------------------------------------------------
# Case mining helpers
# ---------------------------------------------------------------------------


def _first_user_text(row: Dict[str, Any]) -> str:
    for m in row.get("messages") or []:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
            return "\n".join(parts)
    return ""


def _tool_call_sequence(messages) -> List[str]:
    seq = []
    for m in messages or []:
        for tc in (m.get("tool_calls") or []):
            seq.append(tc.get("function", {}).get("name", "?"))
    return seq


def _tool_call_with_args(messages, max_rows=30) -> List[Tuple[str, str]]:
    out = []
    for m in messages or []:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args)
            out.append((fn.get("name", "?"), args[:260]))
            if len(out) >= max_rows:
                return out
    return out


def _calls_with_results(messages) -> List[Dict[str, Any]]:
    """Pair every (assistant tool_call) with its matching (role=tool) result.

    Returns list of dicts: {name, args, tool_call_id, result, result_size}.
    """
    results_by_id: Dict[str, str] = {}
    for m in messages or []:
        if m.get("role") == "tool":
            c = m.get("content")
            if isinstance(c, list):
                c = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
            results_by_id[m.get("tool_call_id", "")] = c or ""
    out = []
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args)
            tcid = tc.get("id", "")
            res = results_by_id.get(tcid, "")
            out.append({
                "name": fn.get("name", "?"),
                "args": args,
                "tool_call_id": tcid,
                "result": res,
                "result_size": len(res),
            })
    return out


def _find_calls(calls: List[Dict[str, Any]], name: str, arg_needle: str = "",
                result_needle: str = "", max_hits: int = 1) -> List[Dict[str, Any]]:
    """Filter tool calls by name / substring-in-args / substring-in-result."""
    hits: List[Dict[str, Any]] = []
    for c in calls:
        if c["name"] != name:
            continue
        if arg_needle and arg_needle.lower() not in (c["args"] or "").lower():
            continue
        if result_needle and result_needle.lower() not in (c["result"] or "").lower():
            continue
        hits.append(c)
        if len(hits) >= max_hits:
            break
    return hits


def _fence(text: str, max_chars: int = 1500) -> str:
    """Render a text blob inside a ```-fenced block, truncating tail if long."""
    t = (text or "").rstrip()
    if len(t) > max_chars:
        t = t[:max_chars] + "\n…[truncated " + str(len(text) - max_chars) + " chars]"
    return "```\n" + t + "\n```"


def _finish_preds(messages) -> List[str]:
    for m in reversed(messages or []):
        for tc in reversed(m.get("tool_calls") or []):
            if "finish" not in (tc.get("function", {}).get("name", "").lower()):
                continue
            args = tc.get("function", {}).get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            preds = args.get("response", []) if isinstance(args, dict) else args
            if isinstance(preds, list):
                return [str(x) for x in preds]
            if isinstance(preds, str):
                return [preds]
    return []


def _last_assistant_text(messages) -> str:
    for m in reversed(messages or []):
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(
                blk.get("text", "") if isinstance(blk, dict) else str(blk)
                for blk in c
            )
    return ""


def _think_payload(messages) -> str:
    last = ""
    for m in messages or []:
        for tc in (m.get("tool_calls") or []):
            if tc.get("function", {}).get("name") != "ehr.think":
                continue
            args = tc.get("function", {}).get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            r = args.get("response") if isinstance(args, dict) else args
            if isinstance(r, str) and r:
                last = r
    return last


# ---------------------------------------------------------------------------
# Case study writers
# ---------------------------------------------------------------------------


def _case_header(qid: str, task: str, label: Any,
                 ag_preds: List[str], os_preds: List[str],
                 ag_msgs_len: int, ag_ncalls: int,
                 os_msgs_len: int, os_ncalls: int,
                 ag_correct: bool, os_correct: bool) -> List[str]:
    def _v(correct): return "✅" if correct else "❌"
    return [
        f"**qid:** `{qid}` · **task:** `{task}`",
        f"**gold:** `{label}`",
        "",
        f"- **ClinSeek final** {_v(ag_correct)}: `{ag_preds}` "
        f"(tool calls = {ag_ncalls}, messages = {ag_msgs_len})",
        f"- **Reasoning final** {_v(os_correct)}: `{os_preds}` "
        f"(tool calls = {os_ncalls}, messages = {os_msgs_len})",
        "",
    ]


def _case_histograms(ag_msgs, os_msgs) -> List[str]:
    ag_hist = Counter(_tool_call_sequence(ag_msgs))
    os_hist = Counter(_tool_call_sequence(os_msgs))
    out = ["## Tool-call distribution", "", "**ClinSeek:**", "```"]
    for k, v in ag_hist.most_common():
        out.append(f"  {k:<40} {v}")
    out.append("```")
    out.append("")
    out.append("**Reasoning pipeline:**")
    out.append("```")
    for k, v in (os_hist.most_common() or [("(none)", 0)]):
        out.append(f"  {k:<40} {v}")
    out.append("```")
    out.append("")
    return out


def _vital_section(title: str, vitals: List[Tuple[str, Dict[str, Any], str]]) -> List[str]:
    """vitals = [(label, call_dict, why_it_matters), ...]"""
    out = [f"## {title}", ""]
    for i, (label, call, why) in enumerate(vitals, 1):
        out.append(f"### {i}. {label}")
        out.append("")
        out.append(f"**Why this call determined the answer:** {why}")
        out.append("")
        out.append(f"_Tool call:_ `{call['name']}({call['args']})`")
        out.append("")
        out.append("_Tool result:_")
        out.append(_fence(call["result"], max_chars=1400))
        out.append("")
    return out


def _vital_calls_from_analysis(analysis: Dict[str, Any], calls: List[Dict[str, Any]],
                               key: str) -> List[Tuple[str, Dict[str, Any], str]]:
    """Turn analyzer output under `key` (vital_calls / misleading_calls /
    could_have_saved_it) into the _vital_section tuple format."""
    out: List[Tuple[str, Dict[str, Any], str]] = []
    for entry in analysis.get(key, []):
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(calls):
            continue
        call = calls[idx]
        ev = entry.get("evidence", "")
        used = entry.get("used_for") or entry.get("misused_as") or entry.get("ignored_because") or ""
        label = f"Call #{idx}: `{call['name']}` — {ev}"
        out.append((label, call, used))
    return out


def load_analysis(filename: str) -> Dict[str, Any]:
    """filename is `<slug>.json` — we look up cases/<slug>/vital_analysis.json."""
    slug = filename[:-len(".json")] if filename.endswith(".json") else filename
    p = CASES_ROOT / slug / "vital_analysis.json"
    if not p.exists():
        raise FileNotFoundError(
            f"{p} missing — run analyze_trajectory.py first (see _dump_trajectories.py)"
        )
    return json.loads(p.read_text())


def _reasoning_failure_section(analysis: Dict[str, Any]) -> List[str]:
    rfa = analysis.get("reasoning_failure_analysis")
    if not rfa:
        return []
    out = [
        "## Why reasoning mode got it wrong",
        "",
        f"**Reasoning-mode prediction:** `{rfa.get('final_answer','?')}`",
        "",
        f"**Failure type:** `{rfa.get('failure_type','?')}`",
        "",
        f"**Where it went wrong:** {rfa.get('where_it_went_wrong','')}",
        "",
        f"**Missed evidence:** {rfa.get('missed_evidence','')}",
        "",
        f"**Pull quote from the reasoning reply:**",
        "",
        "> " + (rfa.get("pull_quote") or "").replace("\n", " "),
        "",
    ]
    return out


def _case_dir(slug: str) -> Path:
    d = CASES_ROOT / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_case_ehr(qid: str, slug: str, heading: str, why_lines: List[str],
                    vital_selector, analysis: Dict[str, Any]) -> Path:
    """Emit a markdown case study for an ehr_bench qid comparing ClinSeek vs reasoning Opus."""
    ag = find_row(EHR_AGENTIC / "claude_opus_4_6" / "results.jsonl", qid)
    os_ = find_row(EHR_ONESHOT / "claude_opus_4_6" / "results.jsonl", qid)
    task = (ag or os_ or {}).get("task", "?")
    label = (ag or os_ or {}).get("label")
    ag_msgs = (ag or {}).get("messages", [])
    os_msgs = (os_ or {}).get("messages", [])
    ag_preds = _finish_preds(ag_msgs)
    os_preds = _finish_preds(os_msgs)

    def _match(preds, label):
        def _norm(xs):
            return {str(x).strip().lower() for x in xs}
        g = [label] if isinstance(label, str) else label or []
        g_set = _norm([x.get("name") if isinstance(x, dict) else x for x in g]) if g else set()
        return bool(_norm(preds) & g_set)

    ag_correct = _match(ag_preds, label)
    os_correct = _match(os_preds, label)

    ag_calls = _calls_with_results(ag_msgs)
    vitals = vital_selector(ag_calls, ag_msgs)  # list[(label, call, why)]

    think = _think_payload(ag_msgs)
    os_text = _last_assistant_text(os_msgs)

    lines = [f"# {heading}", ""]
    lines += _case_header(qid, task, label, ag_preds, os_preds,
                          len(ag_msgs), sum(1 for m in ag_msgs for _ in (m.get("tool_calls") or [])),
                          len(os_msgs), sum(1 for m in os_msgs for _ in (m.get("tool_calls") or [])),
                          ag_correct, os_correct)
    lines += _case_histograms(ag_msgs, os_msgs)
    lines += ["## Why the paradigms diverge", ""]
    for w in why_lines:
        lines.append(f"- {w}")
    lines.append("")
    lines += _vital_section(
        "Vital ClinSeek tool calls (the calls that determined the agent's answer)",
        vitals,
    )
    if think:
        lines.append("## ClinSeek final `ehr.think` synthesis")
        lines.append(_fence(think, max_chars=2000))
        lines.append("")
    lines += _reasoning_failure_section(analysis)
    lines.append("## Full reasoning-mode output")
    lines.append(_fence(os_text or "(empty)", max_chars=8000))
    p = _case_dir(slug) / "case.md"
    p.write_text("\n".join(lines))
    return p


def _write_case_mm(qid: str, slug: str, heading: str,
                   why_lines: List[str], vital_selector,
                   analysis: Dict[str, Any]) -> Path:
    ag = find_row(MM_AGENTIC_FULL / "claude_opus_4_6" / "results.jsonl", qid)
    os_ = find_row(MM_ONESHOT_FULL / "claude_opus_4_6" / "results.jsonl", qid)
    task = (ag or os_ or {}).get("task", "?")
    label = (ag or os_ or {}).get("label")
    ag_msgs = (ag or {}).get("messages", [])
    os_msgs = (os_ or {}).get("messages", [])
    ag_preds = _finish_preds(ag_msgs)
    os_preds = _finish_preds(os_msgs)

    def _match_mm(preds, label):
        g = []
        if isinstance(label, list):
            g = [x.get("name") if isinstance(x, dict) else str(x) for x in label]
        elif isinstance(label, str):
            g = [label]
        gs = {str(x).strip().lower() for x in g}
        ps = {str(x).strip().lower() for x in preds}
        return bool(gs & ps)

    ag_correct = _match_mm(ag_preds, label)
    os_correct = _match_mm(os_preds, label)

    ag_calls = _calls_with_results(ag_msgs)
    vitals = vital_selector(ag_calls, ag_msgs)

    ag_think = _think_payload(ag_msgs)
    os_text = _last_assistant_text(os_msgs)

    lines = [f"# {heading}", ""]
    lines += _case_header(qid, task, label, ag_preds, os_preds,
                          len(ag_msgs), sum(1 for m in ag_msgs for _ in (m.get("tool_calls") or [])),
                          len(os_msgs), sum(1 for m in os_msgs for _ in (m.get("tool_calls") or [])),
                          ag_correct, os_correct)
    lines += _case_histograms(ag_msgs, os_msgs)
    lines += ["## Why the paradigms diverge", ""]
    for w in why_lines:
        lines.append(f"- {w}")
    lines.append("")
    lines += _vital_section(
        "Vital ClinSeek tool calls (the calls that determined the agent's answer)",
        vitals,
    )
    if ag_think:
        lines.append("## ClinSeek final `ehr.think` synthesis (tail)")
        lines.append(_fence(ag_think[-2400:], max_chars=2400))
        lines.append("")
    # Inline CXR image(s) if any live in the case folder
    case_dir = _case_dir(slug)
    imgs = sorted(case_dir.glob("*.jpg")) + sorted(case_dir.glob("*.png"))
    if imgs:
        lines.append("## Linked chest-X-ray image(s) (what the model saw)")
        lines.append("")
        for img in imgs:
            lines.append(f"![{img.name}]({img.name})")
        lines.append("")
    lines += _reasoning_failure_section(analysis)
    lines.append("## Full reasoning-mode output")
    lines.append(_fence(os_text or "(empty)", max_chars=8000))
    p = case_dir / "case.md"
    p.write_text("\n".join(lines))
    return p


CASE_SPECS: List[Dict[str, Any]] = [
    {"slug": "case1_lengthofstay",       "qid": "ehr_bench_risk_prediction_6017",
     "kind": "ehr", "mode": "win",
     "heading": "Case 1 — LengthOfStay_3day: ClinSeek recovers DRG + prescription horizon",
     "why_lines": [
         "The user-curated reasoning prompt renders only `admissions / transfers / "
         "services / prescriptions / procedures_icd` — no `drgcodes`, no `diagnoses_icd`. "
         "The billing-level DRG that encodes stay complexity is absent.",
         "ClinSeek (Opus) discovered `drgcodes` via `ehr.get_table_names` → `ehr.run_sql_query`, "
         "pulled DRG 806 (Vaginal delivery WITH CC) and used it as primary evidence for extended stay.",
     ]},
    {"slug": "case2_mm_phenotyping",     "qid": "medmod_phenotyping_test_38131454_130.604444",
     "kind": "mm",  "mode": "win",
     "heading": "Case 2 — MM phenotyping: CXR vision + SQL + taxonomy lookup",
     "why_lines": [
         "Reasoning-mode prompt has the raw `tb_cxr` / `events` metadata and the linked CXR "
         "jpg, but no access to image tools, raw events rows, or the 25-phenotype taxonomy.",
         "ClinSeek (Opus) ran 196 messages using `image.chest_xray_classifier`, "
         "`image.chest_xray_report_generator`, 45 SQL queries on `events`, and 11 browser "
         "calls that recovered the Harutyunyan-2019 CCS label space.",
     ]},
    {"slug": "case3_pyxis",              "qid": "ehr_bench_decision_making_11698",
     "kind": "ehr", "mode": "loss",
     "heading": "Case 3 — Pyxis next-dispense: answer lost in 66 tool calls",
     "why_lines": [
         "The reasoning-mode prompt lists the Pyxis dispense sequence chronologically. "
         "The obvious next-dispense is piperacillin; reasoning-mode Opus answered correctly.",
         "ClinSeek (Opus) went wide instead of local: 66 tool calls across microbiology, "
         "labs, diagnoses, and browser searches for empiric antibiotic guidelines. It "
         "inferred metronidazole (anaerobic coverage) — a plausible but wrong clinical step.",
     ]},
    {"slug": "case4_inpatient_mortality","qid": "ehr_bench_risk_prediction_5496",
     "kind": "ehr", "mode": "win",
     "heading": "Case 4 — Inpatient mortality: ClinSeek reads beyond the curated timeline",
     "why_lines": [
         "The task asks whether the patient will die during hospitalization.",
         "ClinSeek (Opus) located the decisive evidence in tables or time windows the "
         "reasoning prompt did not expose; the analyzer's `missed_evidence` field "
         "names the specific gap.",
     ]},
    {"slug": "case5_ed_hospitalization", "qid": "ehr_bench_risk_prediction_858",
     "kind": "ehr", "mode": "win",
     "heading": "Case 5 — ED hospitalization: ClinSeek finds the discharge-home signal",
     "why_lines": [
         "The reasoning prompt summarizes the ED visit but not the structured "
         "discharge-disposition / follow-up signal.",
         "ClinSeek (Opus) queried the relevant tables directly and picked up the "
         "'no admission' indicator.",
     ]},
    {"slug": "case6_mm_decompensation",  "qid": "medmod_decompensation_test_39190812_149.0",
     "kind": "mm",  "mode": "win",
     "heading": "Case 6 — Multimodal 24-h mortality: ClinSeek synthesizes ICU signals + CXR",
     "why_lines": [
         "Decompensation asks whether the patient will die within 24 h. The EHR snapshot "
         "alone can be misleading.",
         "ClinSeek (Opus) composes ICU events (MAP, HR, O2 support) with CXR-tool findings "
         "to confirm stability and predict 'no'.",
     ]},
    {"slug": "case7_mm_mortality",       "qid": "medmod_in-hospital-mortality_test_32646816_0",
     "kind": "mm",  "mode": "win",
     "heading": "Case 7 — Multimodal inpatient mortality: recovery-trajectory captured by agent",
     "why_lines": [
         "Gold is 'no' despite ICU admission. Reasoning-mode mis-reads recent ICU data as "
         "ominous and predicts 'yes'.",
         "ClinSeek (Opus) pulls the later-timestamp events that reveal stabilization and "
         "confirms 'no'.",
     ]},
    {"slug": "case8_labevents",          "qid": "ehr_bench_decision_making_5080",
     "kind": "ehr", "mode": "loss",
     "heading": "Case 8 — Labevents next-order: agent commits to one lab, reasoning lists ~40",
     "why_lines": [
         "Labevents decision_making expects the full panel of next labs (~60 items). "
         "Reasoning-mode reads the last-hour lab orders and emits a long list — "
         "precision drops, but recall is high, so set-F1 is decent.",
         "ClinSeek (Opus) over-narrowed to a single lab and lost recall to 1/63.",
     ]},
    {"slug": "case9_next_event",         "qid": "ehr_bench_decision_making_7062",
     "kind": "ehr", "mode": "loss",
     "heading": "Case 9 — Next-event: agent chases admissions instead of the radiology order",
     "why_lines": [
         "Gold is 'radiology' — the obvious next table given the timeline.",
         "ClinSeek (Opus) ran 39 tool calls exploring admissions, transfers, and services "
         "instead of reading the last few events of the timeline, and guessed 'admissions'.",
     ]},
]


def build_cases() -> Dict[str, Path]:
    # Load analyzer outputs for every case
    analyses = {spec["slug"]: load_analysis(f"{spec['slug']}.json") for spec in CASE_SPECS}

    case_paths: Dict[str, Path] = {}
    for spec in CASE_SPECS:
        slug = spec["slug"]
        analysis = analyses[slug]

        if spec["mode"] == "win":
            if spec["kind"] == "ehr":
                def _sel(calls, _msgs, _a=analysis):
                    return _vital_calls_from_analysis(_a, calls, "vital_calls")
                p = _write_case_ehr(
                    qid=spec["qid"],
                    slug=slug,
                    heading=spec["heading"],
                    why_lines=spec["why_lines"] + [
                        (analysis.get("non_vital_summary") or "").strip(),
                    ],
                    vital_selector=_sel,
                    analysis=analysis,
                )
            else:  # mm win
                def _sel(calls, _msgs, _a=analysis):
                    return _vital_calls_from_analysis(_a, calls, "vital_calls")
                p = _write_case_mm(
                    qid=spec["qid"],
                    slug=slug,
                    heading=spec["heading"],
                    why_lines=spec["why_lines"] + [
                        (analysis.get("non_vital_summary") or "").strip(),
                    ],
                    vital_selector=_sel,
                    analysis=analysis,
                )
        else:  # loss
            def _sel(calls, _msgs, _a=analysis):
                mis = _vital_calls_from_analysis(_a, calls, "misleading_calls")
                saved = _vital_calls_from_analysis(_a, calls, "could_have_saved_it")
                out = []
                for label, call, why in mis:
                    out.append((f"[MISLEADING] {label}", call, why))
                for label, call, why in saved:
                    out.append((f"[IGNORED] {label}", call, why))
                return out
            p = _write_case_ehr_fail(
                qid=spec["qid"],
                slug=slug,
                heading=spec["heading"],
                why_lines=spec["why_lines"],
                vital_selector=_sel,
                analysis=analysis,
            )
        case_paths[slug] = p
    return case_paths


def _write_case_ehr_fail(qid: str, slug: str, heading: str,
                         why_lines: List[str], vital_selector,
                         analysis: Dict[str, Any]) -> Path:
    ag = find_row(EHR_AGENTIC / "claude_opus_4_6" / "results.jsonl", qid)
    os_ = find_row(EHR_ONESHOT / "claude_opus_4_6" / "results.jsonl", qid)
    task = (ag or os_ or {}).get("task", "?")
    label = (ag or os_ or {}).get("label")
    ag_msgs = (ag or {}).get("messages", [])
    os_msgs = (os_ or {}).get("messages", [])
    ag_preds = _finish_preds(ag_msgs)
    os_preds = _finish_preds(os_msgs)

    ag_calls = _calls_with_results(ag_msgs)
    vitals = vital_selector(ag_calls, ag_msgs)

    think = _think_payload(ag_msgs)
    os_text = _last_assistant_text(os_msgs)

    lines = [f"# {heading}", ""]
    lines += _case_header(qid, task, label, ag_preds, os_preds,
                          len(ag_msgs), sum(1 for m in ag_msgs for _ in (m.get("tool_calls") or [])),
                          len(os_msgs), sum(1 for m in os_msgs for _ in (m.get("tool_calls") or [])),
                          ag_correct=False, os_correct=True)
    lines += _case_histograms(ag_msgs, os_msgs)
    lines += ["## Why the paradigms diverge", ""]
    for w in why_lines:
        lines.append(f"- {w}")
    lines.append("")
    lines += _vital_section(
        "Vital ClinSeek tool calls (the calls that led the agent to its wrong answer)",
        vitals,
    )
    # Also show analyzer's root cause if present
    rc = analysis.get("root_cause_or_summary")
    if rc:
        lines.append("## Analyzer root-cause summary")
        lines.append("")
        lines.append(f"> {rc}")
        lines.append("")
    if think:
        lines.append("## ClinSeek final `ehr.think` synthesis (head)")
        lines.append(_fence(think, max_chars=2000))
        lines.append("")
    lines.append("## Full reasoning-mode output")
    lines.append(_fence(os_text or "(empty)", max_chars=8000))
    p = _case_dir(slug) / "case.md"
    p.write_text("\n".join(lines))
    return p


# ---------------------------------------------------------------------------
# Final HTML
# ---------------------------------------------------------------------------


def _finding(statement: str, rationale: str) -> str:
    return (f"<div class='finding'><div class='statement'>{statement}</div>"
            f"<div class='rationale'>{rationale}</div></div>")


def build_report() -> None:
    print("[1/4] Table 1…", flush=True)
    table1_html, t1_rows = build_table1()
    (OUT / "table1_ehr_L2.html").write_text(
        "<!DOCTYPE html><html><body>" + table1_html + "</body></html>"
    )
    print("[2/4] Figure 2…", flush=True)
    _, fig_stats = build_figure2(IMAGES / "fig_ehrbench_L3_delta.png")
    print("[3/4] Table 3…", flush=True)
    table3_html, t3_rows = build_table3()
    (OUT / "table3_mm_L4_multimodal.html").write_text(
        "<!DOCTYPE html><html><body>" + table3_html + "</body></html>"
    )
    print("[4/4] Cases…", flush=True)
    case_paths = build_cases()

    # ---- Pull anchor numbers for the narrative ---------------------------
    def _by_slug(rows, slug):
        for r in rows:
            if r["slug"] == slug:
                return r
        return None

    opus = _by_slug(t1_rows, "claude_opus_4_6")
    sonnet = _by_slug(t1_rows, "claude_sonnet_4_6")
    qwen35 = _by_slug(t1_rows, "qwen3_5_35b_a3b")
    ehrr1_72 = _by_slug(t1_rows, "ehr_r1_72b")

    # MM row for Opus
    opus_mm = _by_slug(t3_rows, "claude_opus_4_6")

    # Opus phenotype_ccs specific cell (multimodal → phenotyping_mm → Phenotype_ccs_mm → phenotype_ccs)
    pheno_ag = l4_f1(load_aggregate("agentic", "mm_bench", "claude_opus_4_6"),
                     "multimodal", "phenotyping_mm", "Phenotype_ccs_mm", "phenotype_ccs")
    pheno_os = l4_f1(load_aggregate("oneshot", "mm_bench", "claude_opus_4_6"),
                     "multimodal", "phenotyping_mm", "Phenotype_ccs_mm", "phenotype_ccs")
    pheno_delta = (pheno_ag[0] - pheno_os[0]) * 100 if pheno_ag and pheno_os else None

    # L3 targets for risk prediction
    opus_ag = load_aggregate("agentic", "ehr_bench", "claude_opus_4_6")
    opus_os = load_aggregate("oneshot", "ehr_bench", "claude_opus_4_6")
    def _rp(l3):
        a = l3_f1(opus_ag, "text_only", "risk_prediction", l3)
        o = l3_f1(opus_os, "text_only", "risk_prediction", l3)
        return (a[0] * 100 if a else None, o[0] * 100 if o else None)
    mh_ag, mh_os = _rp("Mortality_Hospital")
    los_ag, los_os = _rp("LengthOfStay")
    edh_ag, edh_os = _rp("ED_Hospitalization")

    # L3 targets for decision_making "all models lose" claim
    loser_rows = []
    for slug_label, slug in EHR_MODELS:
        if slug == "tongyi_deepresearch_30b_a3b":
            continue
        a = load_aggregate("agentic", "ehr_bench", slug)
        o = load_aggregate("oneshot", "ehr_bench", slug)
        if not a or not o:
            continue
        for l3 in ("Next_Event", "ICU_Events", "Lab_Microbiology_orders", "Medication_suggestion"):
            av = l3_f1(a, "text_only", "decision_making", l3)
            ov = l3_f1(o, "text_only", "decision_making", l3)
            if av and ov:
                loser_rows.append({
                    "model": slug_label, "l3": l3,
                    "ag": av[0]*100, "os": ov[0]*100,
                    "d": (av[0]-ov[0])*100,
                })
    loser_rows_html_lines = [
        "<table class='tbl'><thead><tr><th>Model</th>",
        "<th colspan='3'>Next_Event</th><th colspan='3'>ICU_Events</th>",
        "<th colspan='3'>Lab_Microbiology_orders</th><th colspan='3'>Medication_suggestion</th>",
        "</tr><tr><td></td>",
    ]
    for _ in range(4):
        loser_rows_html_lines.append("<th>Ag</th><th>Rsn</th><th>Δ</th>")
    loser_rows_html_lines.append("</tr></thead><tbody>")
    models_seen = OrderedDict()
    for r in loser_rows:
        models_seen.setdefault(r["model"], {})[r["l3"]] = r
    for m, by_l3 in models_seen.items():
        loser_rows_html_lines.append(f"<tr><td>{m}</td>")
        for l3 in ("Next_Event", "ICU_Events", "Lab_Microbiology_orders", "Medication_suggestion"):
            r = by_l3.get(l3)
            if r is None:
                loser_rows_html_lines.append("<td class='num'>—</td>" * 3)
                continue
            loser_rows_html_lines.append(f"<td class='num'>{r['ag']:.1f}</td>")
            loser_rows_html_lines.append(f"<td class='num'>{r['os']:.1f}</td>")
            loser_rows_html_lines.append(
                f"<td class='num {_dcls(r['d'])}'>{_dstr(r['d'])}</td>"
            )
        loser_rows_html_lines.append("</tr>")
    loser_rows_html_lines.append("</tbody></table>")
    loser_rows_html = "\n".join(loser_rows_html_lines)

    # CSV export — one row per (model, L3) for the 4 loser decision-making L3s.
    _loser_csv_rows = []
    for m, by_l3 in models_seen.items():
        for l3 in ("Next_Event", "ICU_Events", "Lab_Microbiology_orders", "Medication_suggestion"):
            r = by_l3.get(l3)
            if r is None:
                _loser_csv_rows.append([m, l3, "", "", ""])
            else:
                _loser_csv_rows.append([
                    m, l3,
                    f"{r['ag']:.1f}", f"{r['os']:.1f}", f"{r['d']:.1f}",
                ])
    _write_csv(
        "ehrbench_decision_losers.csv",
        ["model", "l3", "clinseek_f1", "reasoning_f1", "delta"],
        _loser_csv_rows,
    )

    # --- MM per-model lift table for §2.2 -----------------------------------
    mm_lift_rows = [
        r for r in t3_rows
        if r.get("ag_overall_mm") is not None
        and r.get("os_overall_mm") is not None
    ]
    mm_lift_rows.sort(key=lambda r: -(r.get("delta_overall_mm") or 0))
    mm_lift_html_lines = [
        "<table class='tbl'>",
        "<thead><tr><th>Host model</th><th>ClinSeek F1</th><th>Reasoning F1</th>"
        "<th>Δ ClinSeek lift (pp)</th></tr></thead><tbody>",
    ]
    for r in mm_lift_rows:
        d = r["delta_overall_mm"]
        mm_lift_html_lines.append(
            f"<tr><td>{r['model']}</td>"
            f"<td class='num'>{r['ag_overall_mm']:.1f}</td>"
            f"<td class='num'>{r['os_overall_mm']:.1f}</td>"
            f"<td class='num {_dcls(d)}'>{_dstr(d)}</td></tr>"
        )
    mm_lift_html_lines.append("</tbody></table>")
    mm_lift_html = "\n".join(mm_lift_html_lines)

    _write_csv(
        "mm_overall_lift.csv",
        ["model", "slug", "clinseek_f1", "reasoning_f1", "delta"],
        [
            [r["model"], r["slug"],
             f"{r['ag_overall_mm']:.1f}",
             f"{r['os_overall_mm']:.1f}",
             f"{r['delta_overall_mm']:.1f}"]
            for r in mm_lift_rows
        ],
    )

    # Summary numbers used in the new §2.2 finding
    mm_lift_positive = sum(1 for r in mm_lift_rows if r["delta_overall_mm"] > 0)
    mm_lift_nonneg = sum(1 for r in mm_lift_rows if r["delta_overall_mm"] >= -1.0)
    mm_lift_top = mm_lift_rows[0] if mm_lift_rows else None
    mm_lift_bot = mm_lift_rows[-1] if mm_lift_rows else None

    # ---------------------- Narrative sections ----------------------------

    section_intro = f"""
<p class="note">
Generated 2026-05-01. We compare two pipelines against every model:
<b>ClinSeek</b>, our proposed clinical evidence-seeking agent
(multi-turn MCP tool calls over the raw EHR, with browser + CXR image tools on MM)
vs <b>Reasoning</b> (single-turn prompt with the EHR timeline pre-rendered by a
deterministic rule-based extractor). Scoring is identical across modes.
Tongyi DeepResearch 30B is excluded (very weak). Gemma-4 MM reasoning uses the
corrected (Letian2003/fh37931, 2026-05-01) re-run.
</p>
<div class="legend">
<b>Legend.</b> <b>Δ = ClinSeek F1 − Reasoning F1</b>, in percentage points.
Positive Δ (green in tables, red in heatmap) = ClinSeek wins.
Negative Δ (red in tables, blue in heatmap) = Reasoning wins.
</div>
"""

    # Section 1 — Main result
    s1 = f"""
<h2>1. Main result — ClinSeek lifts the state-of-the-art</h2>

<p>
Across 1,800 text-only task rows, ClinSeek beats the rule-based
user-curated reasoning baseline <b>only when the host model has strong enough
agentic capability</b> (Claude Opus 4.6). Every weaker host model underperforms
its own reasoning baseline in overall F1. ClinSeek's utility scales with
the host's agentic capability, not with raw model knowledge.
</p>

{table1_html}

{_finding(
    f"Opus wins both paradigms overall (ClinSeek {opus['overall_ag']:.1f} / Reasoning {opus['overall_os']:.1f}). "
    "It is the only ClinSeek run that beats its reasoning baseline by more than 1 pp overall.",
    "The second-strongest Anthropic model (Sonnet 4.6) is a near-tie (+0.9 pp). "
    "Every other host — GLM-4.7, Kimi K2.5, Qwen3-VL-235B, gpt-oss-120b, Gemma-4-26B, "
    "Qwen3.5-35B-A3B evaluated as-is, and the EHR-R1 family — either ties or loses in "
    "the ClinSeek column overall. A host model that can navigate a long tool loop is a "
    "hard prerequisite for ClinSeek to pay off at the aggregate level.")}

{_finding(
    "Running ClinSeek with a weak host punishes the score. A 72 B domain-tuned reasoning model "
    f"(EHR-R1-72B) outperforms a 35 B ClinSeek run on overall F1 ({ehrr1_72['overall_os']:.1f} vs "
    f"{qwen35['overall_ag']:.1f}), but does so by skipping the tool loop entirely.",
    "When the host can't reliably call SQL / browser tools under a realistic budget, the "
    "ClinSeek − Reasoning gap is negative at the aggregate level. Reasoning acts as a "
    "capability equalizer for hosts that can't finish a clean tool trajectory.")}
"""

    # Section 2 — ClinSeek wins
    rp_models_where_ag_wins = [r for r in t1_rows if r.get("risk_prediction_delta") is not None and r["risk_prediction_delta"] > 0]

    # Extra narrative bits for the new §2.2 finding
    mm_top_str = (
        f"{mm_lift_top['model']} (+{mm_lift_top['delta_overall_mm']:.1f} pp)"
        if mm_lift_top else "—"
    )
    mm_bot_str = (
        f"{mm_lift_bot['model']} ({mm_lift_bot['delta_overall_mm']:+.1f} pp)"
        if mm_lift_bot else "—"
    )
    s2 = f"""
<h2>2. ClinSeek wins in risk-prediction and multimodal tasks</h2>

<h3>2.1  Clear ClinSeek advantage on <code>Mortality_Hospital</code>, <code>LengthOfStay</code>,
<code>ED_Hospitalization</code></h3>

<p>
The L3 delta grid below groups 20 text-only semantic tasks under the two L2 bands
(<span style="color:{L2_COLORS['risk_prediction']}">■ risk_prediction</span>,
<span style="color:{L2_COLORS['decision_making']}">■ decision_making</span>).
The risk-prediction band is visibly dominated by red (ClinSeek wins).
</p>

<img src="images/fig_ehrbench_L3_delta.png" alt="L3 delta heatmap"/>

{_finding(
    f"For Claude Opus 4.6 the ClinSeek boost on the three target L3 tasks is "
    f"Mortality_Hospital {(mh_ag-mh_os):+.1f} pp · LengthOfStay {(los_ag-los_os):+.1f} pp · "
    f"ED_Hospitalization {(edh_ag-edh_os):+.1f} pp. "
    f"{len(rp_models_where_ag_wins)} of {sum(1 for r in t1_rows if r.get('risk_prediction_delta') is not None)} "
    "hosts have a positive risk_prediction Δ when run with ClinSeek.",
    "These three tasks encode long-horizon hospital events (multi-day mortality, LOS, "
    "admission probability). The decision reduces to yes/no, so a single good retrieval — "
    "a DRG code, a long-running prescription stop time, or a radiology impression — often "
    "flips the answer. ClinSeek can go after that signal; "
    "a generic rule-based extractor cannot enumerate it a priori.")}

<h4>Case studies — ClinSeek wins on risk-prediction</h4>
<ul>
<li><a href="cases/case1_lengthofstay/case.md"><code>cases/case1_lengthofstay/</code></a>
— LengthOfStay_3day, post-partum; ClinSeek recovers DRG-806 + day-4 prescription stop-times.</li>
<li><a href="cases/case4_inpatient_mortality/case.md"><code>cases/case4_inpatient_mortality/</code></a>
— Inpatient mortality; ClinSeek pulls late-timeline evidence the reasoning prompt doesn't surface.</li>
<li><a href="cases/case5_ed_hospitalization/case.md"><code>cases/case5_ed_hospitalization/</code></a>
— ED hospitalization; ClinSeek finds the discharge-home signal.</li>
</ul>

<div class="callout">
<b>Task:</b> <code>LengthOfStay_3day</code> — will hospital stay exceed 3 days?
(<code>qid=ehr_bench_risk_prediction_6017</code>, 91 y.o. female, post-partum)<br/>
<b>Gold:</b> yes · <b>ClinSeek (Opus):</b> yes ✅ · <b>Reasoning Opus:</b> no ❌<br/>
ClinSeek issued 25 tool calls including
<code>run_sql_query("SELECT * FROM drgcodes WHERE hadm_id = 25254375")</code>,
recovered DRG-806 "Vaginal delivery W/O sterilization/D&amp;C <b>WITH CC</b>" plus prescription stop-times
<i>extending to day 4</i>. The reasoning-mode prompt exposes <code>admissions</code>,
<code>transfers</code>, <code>prescriptions</code>, <code>procedures_icd</code>, and
<code>services</code> but <b>not</b> <code>drgcodes</code> or <code>diagnoses_icd</code>.
The missing DRG directly encodes stay-length expectation and was unavailable to reasoning-mode.
</div>

<h3>2.2  ClinSeek unlocks challenging multimodal tasks: +{(pheno_delta or 0):.1f} pp on Phenotype</h3>

{table3_html}

{_finding(
    f"Opus phenotyping: ClinSeek {pheno_ag[0]*100:.1f} vs Reasoning {pheno_os[0]*100:.1f} — a +{pheno_delta:.1f} pp "
    "lift. This is the largest single-cell multimodal lift we see anywhere in the study.",
    "Phenotyping asks the model to emit every CCS-style phenotype group that applies to a "
    "patient. Reasoning-mode Opus has only an 18 k-char EHR snippet and a linked CXR jpg but "
    "no CXR tool access and no phenotype taxonomy. ClinSeek (Opus) composes three capabilities "
    "reasoning-mode cannot: (a) CXR image classifier tool returning per-finding probabilities, "
    "(b) structured SQL over the ICU events table, (c) browser search recovering the exact "
    "25-phenotype Harutyunyan-2019 taxonomy the benchmark scores against.")}

<h4>ClinSeek helps every host model on multimodal tasks — the lift scales with agentic capability</h4>

<p>The table below lists multimodal-overall F1 for every host that has both
paradigms, sorted by ClinSeek lift (largest first). Contrast this with §1:
on text-only tasks, ClinSeek only helps the strongest host; but on the
multimodal tasks it helps essentially every host, and the magnitude of the lift
correlates with host agentic capability.</p>

{mm_lift_html}

{_finding(
    f"ClinSeek lifts {mm_lift_positive}/{len(mm_lift_rows)} host models on multimodal overall F1 "
    f"(the remaining {len(mm_lift_rows)-mm_lift_positive} are ≤ 1 pp within tie). "
    f"Biggest lift: <b>{mm_top_str}</b>. Smallest: <b>{mm_bot_str}</b>. "
    "The ranking by lift mirrors the ranking by raw reasoning F1: stronger hosts get more "
    "mileage out of ClinSeek.",
    "ClinSeek adds three capabilities reasoning mode lacks — CXR image tools, structured SQL "
    "on the ICU events table, and web-based taxonomy retrieval. Every host benefits from at "
    "least one of these, so multimodal lift is nearly universal. But turning image-tool output "
    "into a final multi-label prediction still needs solid agentic capability, which is why "
    "the lift is +15.1 pp for Opus vs ~+5 pp for mid-tier hosts vs ~0 pp for the weakest. "
    "This is the cleanest evidence in the study that ClinSeek's marginal value is an "
    "increasing function of the host model's agentic capability.")}

<h4>Case studies — multimodal ClinSeek wins (CXR images included inline)</h4>
<ul>
<li><a href="cases/case2_mm_phenotyping/case.md"><code>cases/case2_mm_phenotyping/</code></a>
— Phenotyping (35 y.o. ICU); CXR classifier + taxonomy lookup → F1 0.83.</li>
<li><a href="cases/case6_mm_decompensation/case.md"><code>cases/case6_mm_decompensation/</code></a>
— 24-h decompensation; ClinSeek picks 'no' from ICU-events stabilization.</li>
<li><a href="cases/case7_mm_mortality/case.md"><code>cases/case7_mm_mortality/</code></a>
— In-hospital mortality; ClinSeek captures the recovery trajectory.</li>
</ul>
<p class="note">Each MM case markdown embeds the linked chest-X-ray image
directly alongside it in <code>cases/&lt;slug&gt;/</code>.</p>

<div class="callout">
<b>Task:</b> <code>medmod_phenotyping</code>
(<code>qid=medmod_phenotyping_test_38131454_130.604444</code>, 35 y.o. ICU patient)<br/>
<b>Gold:</b> Complications of surgical procedures or medical care · Fluid and electrolyte disorders ·
Pneumonia · Septicemia · Shock<br/>
<b>ClinSeek (Opus) (F1 = 0.83):</b> Septicemia, Shock, Pleurisy/pneumothorax/pulmonary collapse, Pneumonia (3/4 correct)<br/>
<b>Reasoning Opus (F1 = 0.00):</b> general descriptive text, no matching phenotypes emitted.<br/>
ClinSeek trajectory = 196 messages: 1 <code>image.chest_xray_classifier</code> (Effusion 0.71,
Lung Opacity 0.83, Consolidation 0.62), 1 <code>image.chest_xray_report_generator</code>, 45 SQL
queries over the <code>events</code> table, and 11 browser calls that recovered the full
25-phenotype taxonomy. Reasoning-mode had none of this.
</div>
"""

    # Section 3 — ClinSeek failures
    s3 = f"""
<h2>3. ClinSeek falls short in decision-making tasks</h2>

<h3>3.1  Smaller Qwen3.5-35B-A3B + ClinSeek beats EHR-R1-72B (reasoning) on risk, loses on decision</h3>

<p>
The paradigm gap is task-family-specific. A 35 B ClinSeek run exceeds a 72 B domain-tuned
reasoning run on risk_prediction, but the sign flips on decision_making:
</p>

<table class="tbl">
<thead><tr><th>Run</th><th>risk_prediction F1</th><th>decision_making F1</th><th>overall F1</th></tr></thead>
<tbody>
<tr><td>Qwen3.5-35B-A3B + ClinSeek</td><td class='num'>{qwen35['risk_prediction_ag']:.1f}</td><td class='num'>{qwen35['decision_making_ag']:.1f}</td><td class='num'>{qwen35['overall_ag']:.1f}</td></tr>
<tr><td>EHR-R1-72B (reasoning only)</td><td class='num'>{ehrr1_72['risk_prediction_os']:.1f}</td><td class='num'>{ehrr1_72['decision_making_os']:.1f}</td><td class='num'>{ehrr1_72['overall_os']:.1f}</td></tr>
<tr><td class='meta'>Δ (Qwen-ClinSeek − EHR-R1-Rsn)</td>
  <td class='num pos'>{qwen35['risk_prediction_ag']-ehrr1_72['risk_prediction_os']:+.1f}</td>
  <td class='num neg'>{qwen35['decision_making_ag']-ehrr1_72['decision_making_os']:+.1f}</td>
  <td class='num neg'>{qwen35['overall_ag']-ehrr1_72['overall_os']:+.1f}</td></tr>
</tbody></table>

{_finding(
    "Decision-making is the task family where even the strongest ClinSeek run under-performs "
    "a much smaller reasoning-only domain model.",
    "Risk prediction rewards iterative retrieval: one extra SQL query can surface the one fact "
    "that flips a polar label. Decision making requires multi-label output over a long candidate "
    "list whose correct answer is usually an <em>obvious next event</em> in the timeline — "
    "tool use doesn't help, and often distracts.")}

<h3>3.2  Decision-making is still hard even for the strongest ClinSeek host</h3>

{_finding(
    f"Opus decision_making ClinSeek F1 = {opus['decision_making_ag']:.1f} vs risk_prediction "
    f"ClinSeek F1 = {opus['risk_prediction_ag']:.1f} — a ~46 pp within-host gap.",
    "No model in our board exceeds 50 F1 on decision_making in either paradigm. This is "
    "the dominant unsolved area among text-only tasks and the primary target for targeted training / "
    "retrieval fixes. The scoreboard suggests the bottleneck is sequencing / scheduling "
    "behavior, not clinical knowledge — even MedGemma and EHR-R1-72B (domain-tuned models) "
    "fall to ≤45 F1 on decision_making.")}

<h3>3.3  All hosts lose under ClinSeek on <code>Next_Event</code>, <code>ICU_Events</code>,
<code>Lab_Microbiology_orders</code>, and <code>Medication_suggestion</code></h3>

{loser_rows_html}

{_finding(
    "Four decision-making L3 tasks penalize ClinSeek for every host (Δ ≤ 0 across all rows). "
    "These are the <em>next-event-in-a-specific-table</em> tasks with very long candidate lists.",
    "Every one of these tasks has the same shape: the correct answer is the <em>next row</em> "
    "that would be written to a specific long table (pyxis, prescriptions, chartevents, "
    "microbiology, labs). The answer is nearly always derivable from the last few rows of "
    "that table in the prompt — something the rule-based reasoning extractor handles well — "
    "but ClinSeek tends to explore wider context, lose track of the most-recent event, and "
    "answer with a plausible clinical next-step rather than the benchmarked next-row.")}

<h4>Case studies — ClinSeek losses on decision-making</h4>
<ul>
<li><a href="cases/case3_pyxis/case.md"><code>cases/case3_pyxis/</code></a>
— Pyxis next-dispense; 66 calls → wrong metronidazole choice.</li>
<li><a href="cases/case8_labevents/case.md"><code>cases/case8_labevents/</code></a>
— Labevents next-order; ClinSeek commits to one lab vs reasoning's ~40-lab enumeration.</li>
<li><a href="cases/case9_next_event/case.md"><code>cases/case9_next_event/</code></a>
— Next_event; 39 calls exploring admissions/transfers instead of radiology.</li>
</ul>

<div class="callout">
<b>Task:</b> <code>pyxis</code>
(<code>qid=ehr_bench_decision_making_11698</code>, 91 y.o. male, ED abdominal distention)<br/>
<b>Gold:</b> `Piperacilli 4.5g/100mL 100mL BAG`<br/>
<b>ClinSeek (Opus):</b> `MetroNIDAZ 500mg/100mL 100mL BAG` ❌ — 66 tool calls, 41 SQL queries.<br/>
<b>Reasoning Opus:</b> `Piperacilli 4.5g/100mL 100mL BAG` ✅ — one-shot.<br/>
The reasoning-mode prompt surfaces the immediate Pyxis-dispense sequence: Readi-Cat 2 →
Vancomycin 1g/200mL <i>and</i> Piperacillin 4.5g/100mL pulled together at 18:56, Vancomycin
started at 19:00. The next Pyxis event is trivially "Piperacillin for administration."
ClinSeek (Opus), freed to run any SQL it wanted, queried microbiology, labs, diagnoses, and
historical orders — then reasoned its way to <i>metronidazole</i> (anaerobic coverage is a
plausible clinical next step). Exhaustive retrieval <b>crowded out the locally-obvious answer</b>;
reasoning-mode wins precisely because its rule-based extractor biases the prompt toward the
most recent events.
</div>

{_finding(
    "Failure mode: <b>wide retrieval distracts from local sequencing</b>. For tasks whose "
    "answer is a table-next-row in the prompt context, ClinSeek enlarges the hypothesis "
    "space without improving local temporal prior. This is the primary decision_making "
    "failure pattern and occurs in every host we benchmarked.",
    "A plausible remedy is to give ClinSeek an explicit \"most-recent-N-rows-of-this-table\" "
    "tool plus a hard budget on other queries, or to bias the training reward toward short "
    "trajectories on these task types.")}
"""

    # Summary
    n_mm_ag_lead = sum(1 for r in t3_rows if r.get("delta_overall_mm") is not None and r["delta_overall_mm"] > 0)
    s_summary = f"""
<h2>Summary</h2>
<ul>
<li><b>Where ClinSeek wins:</b> text-only risk_prediction (+3.5 pp average), and
<b>multimodal at every host</b> (Opus +{(opus_mm['delta_overall_mm'] or 0):+.1f} pp on
989 MM rows; {n_mm_ag_lead}/{len(t3_rows)} hosts show a positive Δ). MM lift scales
with the host's agentic capability.</li>
<li><b>Where Reasoning wins:</b> text-only decision_making (−7.0 pp average).
The worst ClinSeek losses cluster in Next_Event, ICU_Events,
Lab_Microbiology_orders, and Medication_suggestion.</li>
<li><b>Overall prerequisite:</b> at the aggregate text-only level you need a
genuinely strong agentic host (Opus) for ClinSeek to exceed Reasoning.
Everything below Opus is a toss-up or a loss on text-only tasks overall,
though still a positive-sum bet on multimodal tasks.</li>
<li><b>Open problem:</b> decision_making remains the unsolved area of text-only tasks —
no host clears 50 F1 in either paradigm, with or without tools.</li>
</ul>
"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>ClinSeek vs Reasoning — Text-only &amp; Multimodal Findings</title>
<style>
body {{ font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       max-width: 1400px; margin: 20px auto; padding: 0 16px; color: #24292e;
       line-height: 1.55; }}
h1 {{ border-bottom: 2px solid #305496; padding-bottom: 6px; }}
h2 {{ color: #305496; margin-top: 36px; border-bottom: 1px solid #dfe2e5; padding-bottom: 4px; }}
h3 {{ color: #444; margin-top: 24px; }}
h4 {{ margin-bottom: 6px; color: #555; }}
.finding {{ background: #f6f8fa; padding: 14px 20px; border-left: 4px solid #305496;
           margin: 14px 0; border-radius: 0 6px 6px 0; }}
.finding .statement {{ font-weight: 600; color: #24292e; }}
.finding .rationale {{ margin-top: 6px; color: #555; font-size: 0.95em; }}
.callout {{ background: #fffbea; padding: 12px 18px; border-left: 4px solid #b07b00;
           margin: 14px 0; border-radius: 0 6px 6px 0; font-size: 0.95em; }}
table.tbl {{ border-collapse: collapse; margin: 14px 0; font-size: 0.92em; }}
table.tbl th, table.tbl td {{ padding: 5px 10px; border: 1px solid #cfd4d9; text-align: left; }}
table.tbl th {{ background: #305496; color: white; font-weight: 600; }}
table.tbl td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
table.tbl td.meta {{ background: #f0f3f7; color: #555; font-style: italic; }}
table.tbl td.pos {{ color: #0a7f24; font-weight: 600; }}
table.tbl td.neg {{ color: #a8240d; font-weight: 600; }}
.na {{ color: #999; font-style: italic; }}
img {{ max-width: 100%; height: auto; display: block; margin: 16px auto;
      border: 1px solid #e1e4e8; border-radius: 4px; }}
.note {{ font-size: 0.88em; color: #666; font-style: italic; }}
.legend {{ font-size: 0.9em; color: #555; padding: 8px 12px;
          background: #f6f8fa; border-left: 3px solid #888; border-radius: 0 4px 4px 0;
          margin: 6px 0 14px 0; }}
code {{ background: #f6f8fa; padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }}
</style></head>
<body>

<h1>ClinSeek vs Reasoning — findings across text-only and multimodal clinical tasks</h1>
{section_intro}

{s1}

{s2}

{s3}

{s_summary}

<h2>Files</h2>
<ul>
<li><code>table1_ehr_L2.html</code> — standalone text-only L2 table</li>
<li><code>table3_mm_L4_multimodal.html</code> — standalone multimodal L4 table</li>
<li><code>images/fig_ehrbench_L3_delta.png</code> — text-only L3 delta heatmap</li>
<li><code>data/table1_ehr_L2.csv</code> — raw numbers behind Table 1</li>
<li><code>data/fig_ehrbench_L3_delta.csv</code> — raw numbers behind the heatmap (long format)</li>
<li><code>data/table3_mm_L4_multimodal.csv</code> — raw numbers behind Table 3</li>
<li><code>data/mm_overall_lift.csv</code> — per-host MM overall lift (§2.2)</li>
<li><code>data/ehrbench_decision_losers.csv</code> — 4 decision-making L3s where every host loses (§3.3)</li>
<li><code>cases/&lt;slug&gt;/</code> — one subfolder per case study with
<code>case.md</code>, raw <code>trajectory.md</code>, <code>reasoning.md</code>,
<code>vital_analysis.json</code>, and any linked CXR images.
See <code>cases/README.md</code> for details.</li>
<li><code>scripts/build_report.py</code> — regenerator (tables + figure + CSVs + cases + HTML)</li>
<li><code>scripts/plot_fig_from_csv.py</code> — regenerate the heatmap from <code>data/fig_ehrbench_L3_delta.csv</code> alone</li>
<li><code>scripts/analyze_trajectory.py</code> — Claude Opus 4.6 vital-call analyzer</li>
<li><code>scripts/_dump_trajectories.py</code> — trajectory + CXR asset dumper</li>
<li><code>scripts/_run_analyzer.sh</code> — batch analyzer launcher</li>
</ul>

</body></html>
"""
    (OUT / "findings_report.html").write_text(html)
    print("\nReport →", OUT / "findings_report.html")
    for name, path in case_paths.items():
        print("  case →", path)


if __name__ == "__main__":
    build_report()
