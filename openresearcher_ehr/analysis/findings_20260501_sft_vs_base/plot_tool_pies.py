"""Render side-by-side pie charts of tool-call distribution for the baseline
Qwen3.5-35B-A3B and the distilled ClinSeek-35B-A3B.

Single shared legend below the two pies so tool names are listed only once.
Writes `tool_distribution_pies.png` next to this script.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

OUT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/analysis/findings_20260501_sft_vs_base")
STATS = json.loads((OUT / "tool_stats_aligned.json").read_text())

MIN_SHARE = 0.02  # tools below 2% are bucketed into "other" *per-side*


def _labels_and_values(per_tool: dict[str, int]) -> tuple[list[str], list[int]]:
    total = sum(per_tool.values())
    items = sorted(per_tool.items(), key=lambda kv: -kv[1])
    keep: list[tuple[str, int]] = []
    other = 0
    for name, cnt in items:
        if cnt / total >= MIN_SHARE:
            keep.append((name, cnt))
        else:
            other += cnt
    if other > 0:
        keep.append(("(other)", other))
    return [n for n, _ in keep], [v for _, v in keep]


# Build the union of named slices across both pies, preserving order by total usage.
base_labels, base_values = _labels_and_values(STATS["base"]["per_tool_total"])
sft_labels,  sft_values  = _labels_and_values(STATS["sft"]["per_tool_total"])

union_counts: dict[str, int] = {}
for l, v in zip(base_labels, base_values):
    union_counts[l] = union_counts.get(l, 0) + v
for l, v in zip(sft_labels, sft_values):
    union_counts[l] = union_counts.get(l, 0) + v
# put "(other)" last regardless of count
union_order = [l for l in sorted(union_counts, key=lambda k: -union_counts[k]) if l != "(other)"]
if "(other)" in union_counts:
    union_order.append("(other)")

cmap = plt.get_cmap("tab20")
palette = {name: cmap(i % cmap.N) for i, name in enumerate(union_order)}

fig = plt.figure(figsize=(22, 9))
gs = fig.add_gridspec(1, 3, width_ratios=[5, 5, 2.6], wspace=0.0)
ax_base = fig.add_subplot(gs[0, 0])
ax_sft  = fig.add_subplot(gs[0, 1])
ax_legend = fig.add_subplot(gs[0, 2])
ax_legend.axis("off")

for ax, side, title in [
    (ax_base, "base", "Baseline Qwen3.5-35B-A3B"),
    (ax_sft,  "sft",  "ClinSeek-35B-A3B (ours, SFT)"),
]:
    labels, values = _labels_and_values(STATS[side]["per_tool_total"])
    total = sum(values)
    colors = [palette[l] for l in labels]
    wedges, _ = ax.pie(
        values,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops=dict(linewidth=1.0, edgecolor="white"),
    )
    for w, l, v in zip(wedges, labels, values):
        pct = 100 * v / total
        if pct < 4:
            continue
        ang = (w.theta1 + w.theta2) / 2.0
        x = 0.68 * np.cos(np.deg2rad(ang))
        y = 0.68 * np.sin(np.deg2rad(ang))
        ax.text(x, y, f"{pct:.1f}%", ha="center", va="center", fontsize=19,
                color="white", fontweight="bold")
    ax.set_title(f"{title}\n{total:,} tool calls across 500 test qids",
                 fontsize=22, pad=6)

# Shared legend: one handle per tool in union_order.
handles = [plt.Rectangle((0, 0), 1, 1, facecolor=palette[l], edgecolor="white")
           for l in union_order]

ax_legend.legend(
    handles, list(union_order),
    loc="center left", ncol=1, frameon=False, fontsize=15,
    title="Tool",
    title_fontsize=17,
    handlelength=1.5, handleheight=1.5,
    labelspacing=0.7,
)

out_path = OUT / "tool_distribution_pies.png"
fig.savefig(out_path, dpi=160, bbox_inches="tight")
print(f"wrote {out_path}")
