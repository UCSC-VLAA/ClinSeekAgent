"""ClinSeek vs peers: x = total model parameters (log scale), y = AgentEHR 5-task F1.

Clean version — no logos, no legend clutter, no "optimal region" bubble.
Each point has just <Model name><br/><F1>. Our model is bold.
Proprietary models (Claude Opus/Sonnet 4.6) sit in a narrow "N/A" column
on the right of the log axis.

Writes: clinseek_vs_peers.png
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse

OUT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/analysis/findings_20260501_sft_vs_base")

# (label, F1, total_params_B_or_None, is_ours, is_baseline, is_teacher)
MODELS = [
    ("Claude Opus 4.6",            36.0, None, False, False, True),
    ("ClinSeek-35B-A3B (ours)",    34.0, 35,   True,  False, False),
    ("Claude Sonnet 4.6",          32.7, None, False, False, True),
    ("Kimi K2.5",                  29.9, 1000, False, False, False),
    ("MiniMax M2.5",               27.7, 230,  False, False, False),
    ("GLM-4.7",                    27.6, 355,  False, False, False),
    ("Qwen3.5-35B-A3B (baseline)", 22.1, 35,   False, True,  False),
    ("Qwen3-235B-A22B",            20.5, 235,  False, False, False),
    ("gpt-oss-120B",               15.8, 120,  False, False, False),
    ("Gemma-4-26B-A4B",            15.2, 26,   False, False, False),
]

COLOR_OURS     = "#4b5b7a"  
COLOR_TEACHER  = "#1f3b8c"
COLOR_BASELINE = "#4b5b7a"
COLOR_PEER     = "#4b5b7a"


def color_for(m):
    _, _, _, ours, base, tch = m
    if ours:  return COLOR_OURS
    if tch:   return COLOR_TEACHER
    if base:  return COLOR_BASELINE
    return COLOR_PEER


fig = plt.figure(figsize=(6.4, 5.0))
gs = fig.add_gridspec(1, 2, width_ratios=[9, 2.2], wspace=0.02)
ax  = fig.add_subplot(gs[0, 0])
axN = fig.add_subplot(gs[0, 1], sharey=ax)

fig.subplots_adjust(top=0.88, bottom=0.16, left=0.09, right=0.985)

# main log axis
ax.set_xscale("log")
ax.set_xlim(18, 1800)
ax.set_ylim(12, 40)
ax.set_xticks([26, 35, 120, 235, 355, 1000])
ax.set_xticklabels(["26B", "35B", "120B", "235B", "355B", "1T"], fontsize=8.5)
ax.set_xlabel("Model size (Parameters)", fontsize=9.5, fontweight="bold")
ax.set_ylabel("Avg F1 on AgentEHR-Bench", fontsize=9.5, fontweight="bold")
ax.tick_params(axis="y", labelsize=8.5)
ax.grid(True, which="major", color="#ececec", linestyle="-", linewidth=0.8, zorder=1)
ax.set_axisbelow(True)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.spines["left"].set_color("#c2c2c2")
ax.spines["bottom"].set_color("#c2c2c2")

# N/A column (proprietary)
axN.set_xlim(0, 1)
axN.set_xticks([0.5])
axN.set_xticklabels(["N/A"], fontsize=8.5)
axN.tick_params(axis="y", left=False, labelleft=False)
axN.grid(True, axis="y", which="major", color="#ececec", linestyle="-", linewidth=0.8, zorder=1)
for s in ("top", "right"):
    axN.spines[s].set_visible(False)
axN.spines["left"].set_linestyle((0, (6, 6)))
axN.spines["left"].set_color("#bcbcbc")
axN.spines["bottom"].set_color("#c2c2c2")
axN.set_axisbelow(True)
axN.annotate("proprietary", xy=(0.5, -0.07), xycoords="axes fraction",
             ha="center", va="top", fontsize=7.5, color="#777", fontstyle="italic")


def plot_one(ax_, x, m, dx=0.0, dy=1.6, ha="left"):
    label, f1, *_ = m
    col = color_for(m)
    is_ours = m[3]
    ax_.scatter([x], [f1], s=70 if is_ours else 32, c=col, zorder=5,
                edgecolor="white", linewidth=0.9, alpha=0.0)
    # text label to the right (or left) of the point
    weight = "bold" if is_ours else "normal"
    fs_name = 9 if is_ours else 8
    fs_score = 9 if is_ours else 8
    ax_.annotate(f"{label}",
                 xy=(x, f1), xytext=(dx, dy), textcoords="offset points",
                 ha=ha, va="bottom",
                 fontsize=fs_name, fontweight=weight, color=col, zorder=6)
    ax_.annotate(f"{f1:.1f}",
                 xy=(x, f1), xytext=(dx, -2.0), textcoords="offset points",
                 ha=ha, va="top",
                 fontsize=fs_score, fontweight=weight, color=col, zorder=6)


# Custom dx/dy so labels don't overlap, specified per-model
# dx/dy are in points (offset from the dot)
placement = {
    "ClinSeek-35B-A3B (ours)":       (7,  3, "left"),
    "Qwen3.5-35B-A3B (baseline)":    (7,  3, "left"),
    "Kimi K2.5":                     (-7, 3, "right"),
    "MiniMax M2.5":                  (-7, 3, "right"),
    "GLM-4.7":                       (7,  3, "left"),
    "Qwen3-235B-A22B":               (7,  3, "left"),
    "gpt-oss-120B":                  (7,  3, "left"),
    "Gemma-4-26B-A4B":               (7,  3, "left"),
}

for m in MODELS:
    label, f1, p, ours, base, tch = m
    if p is None:
        continue
    dx, dy, ha = placement.get(label, (9, 4, "left"))
    plot_one(ax, p, m, dx=dx, dy=dy, ha=ha)

# Proprietary column
na_models = [m for m in MODELS if m[2] is None]
for m in na_models:
    plot_one(axN, 0.5, m, dx=7, dy=3, ha="left")

# "Optimal Performance / Params Ratio" — upper-left quarter-ellipse framing ClinSeek.
# Draw a full ellipse centered at the top-left corner of the axes in axes-fraction
# coordinates; set the clip path to the axes box so only the lower-right quadrant
# (which is the upper-left quadrant of the plot area) is visible.
opt = Ellipse(
    xy=(0.0, 1.0),
    width=1.35, height=0.66,
    transform=ax.transAxes,
    facecolor="#d6f3e2", edgecolor="none",
    linewidth=0, alpha=0.60, zorder=2,
)
# Force the clip to the axes patch (the rectangular plotting area).
opt.set_clip_path(ax.patch)
ax.add_patch(opt)
ax.text(
    0.02, 0.96, "Optimal Performance / Params Ratio",
    transform=ax.transAxes, fontsize=9, fontweight="bold",
    color="#2f7a4a", ha="left", va="top", zorder=7,
)

out_path = OUT / "clinseek_vs_peers.png"
fig.savefig(out_path, dpi=400, bbox_inches="tight")
print(f"wrote {out_path}")
