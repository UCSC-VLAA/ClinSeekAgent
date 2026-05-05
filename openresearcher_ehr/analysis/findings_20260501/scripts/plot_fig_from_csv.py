"""Regenerate images/fig_ehrbench_L3_delta.png from data/fig_ehrbench_L3_delta.csv.

Reads only the CSV — no dependency on the live results/ tree. Row order in
the CSV determines the row and column order in the heatmap (first-seen
wins). L2 bands are drawn from the `l2` column of the CSV.

    python plot_fig_from_csv.py
"""
from __future__ import annotations

import csv
from collections import OrderedDict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CSV = ROOT / "data" / "fig_ehrbench_L3_delta.csv"
OUT = ROOT / "images" / "fig_ehrbench_L3_delta.png"

L2_COLORS = {"risk_prediction": "#2b7bb9", "decision_making": "#8b4caa"}
L2_PRETTY = {"risk_prediction": "Risk Prediction", "decision_making": "Decision Making"}


def _parse_float(s: str) -> float:
    s = (s or "").strip()
    if s == "" or s.lower() == "nan":
        return float("nan")
    return float(s)


def main() -> None:
    rows = list(csv.DictReader(CSV.open()))
    # Preserve first-seen order for models and columns.
    models: "OrderedDict[str, str]" = OrderedDict()  # label → slug (unused)
    col_order: "OrderedDict[tuple, None]" = OrderedDict()  # (l2, l3) → None
    for r in rows:
        models.setdefault(r["model"], r["slug"])
        col_order.setdefault((r["l2"], r["l3"]), None)

    model_labels = list(models.keys())
    cols = list(col_order.keys())
    m, k = len(model_labels), len(cols)

    delta = np.full((m, k), np.nan)
    mi = {lbl: i for i, lbl in enumerate(model_labels)}
    ci = {col: j for j, col in enumerate(cols)}
    for r in rows:
        delta[mi[r["model"]], ci[(r["l2"], r["l3"])]] = _parse_float(r["delta"])

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig_w = max(14.5, 1.0 + 0.7 * k)
    fig_h = max(4.5, 0.55 * m + 3.0)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    vmax = float(min(40.0, max(5.0, np.nanmax(np.abs(delta)) if np.isfinite(delta).any() else 5.0)))
    cmap = LinearSegmentedColormap.from_list("div", ["#2c62a3", "#ffffff", "#c1272d"], N=512)
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0.0, vmax=vmax)
    im = ax.imshow(delta, aspect="auto", cmap=cmap, norm=norm)

    for i in range(m):
        for j in range(k):
            v = delta[i, j]
            if not np.isfinite(v):
                ax.text(j, i, "—", ha="center", va="center", color="#888", fontsize=8)
                continue
            tc = "#111" if abs(v) < 0.55 * vmax else "#fff"
            ax.text(j, i, f"{v:+.1f}", ha="center", va="center", fontsize=8.5, color=tc)

    ax.set_yticks(range(m))
    ax.set_yticklabels(model_labels, fontsize=10)
    ax.set_xticks(range(k))
    ax.set_xticklabels([l3.replace("_", " ") for (_, l3) in cols],
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

    # L2 bands from the CSV itself (contiguous runs in column order).
    ranges = []
    start = 0
    cur = cols[0][0]
    for j, (l2, _) in enumerate(cols):
        if l2 != cur:
            ranges.append((start, j, cur))
            start = j
            cur = l2
    ranges.append((start, k, cur))

    band_h = 0.42
    y_top = -0.5 - band_h
    for s, e, l2 in ranges:
        color = L2_COLORS.get(l2, "#999")
        ax.add_patch(plt.Rectangle(
            (s - 0.5, y_top), e - s, band_h,
            facecolor=color, alpha=0.32, edgecolor="#333", linewidth=0.6,
            clip_on=False,
        ))
        ax.text((s + e - 1) / 2, y_top + band_h / 2,
                L2_PRETTY.get(l2, l2),
                ha="center", va="center",
                fontsize=18, fontweight="bold",
                color="#111", clip_on=False)
    for s, e, _ in ranges[:-1]:
        ax.axvline(e - 0.5, color="#666", linewidth=1.3, linestyle="--")
    ax.set_ylim(m - 0.5, y_top - 0.06)
    plt.tight_layout()
    fig.savefig(OUT, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
