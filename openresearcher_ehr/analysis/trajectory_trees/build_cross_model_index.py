"""Build the cross-model browser:

  1. Render every (model, qid) tree as a standalone page (re-uses
     visualize_tree._render_page).
  2. Generate one entry-point index.html with a two-step picker:
        a. choose qid (grouped by task and tier, with per-model F1 chips)
        b. choose model
     The selected page is shown in an iframe so the user can navigate
     freely without losing context.

Usage:
    python -m analysis.trajectory_trees.build_cross_model_index \\
        --selection analysis/trajectory_trees/cross_model/selected_qids.json \\
        --trees-dir analysis/trajectory_trees/cross_model \\
        --out-dir   analysis/trajectory_trees/cross_model/html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_OPENRES = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _OPENRES not in sys.path:
    sys.path.insert(0, _OPENRES)

from analysis.trajectory_trees.visualize_tree import _render_page, _safe_filename  # noqa: E402

MODELS = ["claude_opus_4_6", "claude_sonnet_4_6", "kimi_k2_5", "qwen3_235b"]
MODEL_LABELS = {
    "claude_opus_4_6": "Opus 4.6",
    "claude_sonnet_4_6": "Sonnet 4.6",
    "kimi_k2_5": "Kimi K2.5",
    "qwen3_235b": "Qwen3 235B",
}

TASKS = [
    "diagnoses_ccs",
    "labevents",
    "microbiologyevents",
    "prescriptions",
    "procedures_ccs",
    "transfers",
]

_INDEX_CSS = r"""
:root {
  --border: #d1d5db;
  --muted: #6b7280;
  --text: #111827;
  --bg: #fafafa;
  --panel: #ffffff;
  --accent: #1d4ed8;
  --tier-a: #2563eb;
  --tier-b: #b91c1c;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 13px;
}
.layout { display: flex; height: 100%; }
.picker {
  width: 360px; min-width: 320px; flex-shrink: 0;
  border-right: 1px solid var(--border); background: var(--panel);
  display: flex; flex-direction: column;
}
.picker-header {
  padding: 12px 14px; border-bottom: 1px solid var(--border);
}
.picker-header h1 {
  margin: 0; font-size: 14px; font-weight: 600;
}
.picker-header .subtitle {
  font-size: 11px; color: var(--muted); margin-top: 4px;
}
.picker-body { flex: 1; overflow-y: auto; padding: 10px 0; }
.picker-section { margin-bottom: 6px; }
.picker-section > h2 {
  margin: 6px 14px; font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em;
}
.tier-label {
  display: inline-block; font-size: 10px; padding: 1px 6px;
  border-radius: 3px; margin-left: 6px; color: white; font-weight: 600;
  text-transform: uppercase; letter-spacing: .04em;
}
.tier-label.a { background: var(--tier-a); }
.tier-label.b { background: var(--tier-b); }
.qid-row {
  padding: 6px 14px; cursor: pointer; border-left: 3px solid transparent;
}
.qid-row:hover { background: #f3f4f6; }
.qid-row.selected {
  background: #eff6ff; border-left-color: var(--accent);
}
.qid-row .qid {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; word-break: break-all;
}
.qid-row .scores {
  margin-top: 3px; display: flex; gap: 6px; flex-wrap: wrap;
}
.score-chip {
  font-size: 10px; padding: 1px 6px; border-radius: 9px;
  background: #e5e7eb; color: #1f2937; font-family: ui-monospace, monospace;
  white-space: nowrap;
}
.score-chip .label { color: var(--muted); margin-right: 3px; }
.score-chip.high { background: #d1fae5; color: #065f46; }
.score-chip.mid  { background: #fef3c7; color: #92400e; }
.score-chip.low  { background: #fee2e2; color: #991b1b; }

.right { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.model-bar {
  padding: 8px 14px; border-bottom: 1px solid var(--border);
  background: var(--panel); display: flex; gap: 8px; align-items: center;
  flex-wrap: wrap;
}
.model-bar .label { font-size: 11px; color: var(--muted); margin-right: 4px; }
.model-bar button {
  font: inherit; font-size: 12px; padding: 4px 10px;
  border: 1px solid var(--border); background: white; border-radius: 4px;
  cursor: pointer;
}
.model-bar button.active {
  background: var(--accent); color: white; border-color: var(--accent);
}
.model-bar button:disabled {
  background: #f3f4f6; color: #9ca3af; cursor: not-allowed;
}
.model-bar .open-link {
  margin-left: auto; font-size: 11px; color: var(--accent);
  text-decoration: none;
}
.model-bar .open-link:hover { text-decoration: underline; }

iframe {
  flex: 1; width: 100%; border: none; background: var(--bg);
}
.placeholder {
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-style: italic; padding: 40px;
  text-align: center;
}
"""

_INDEX_JS = r"""
(function() {
  const data = window.__SELECTION__;
  const allTrees = window.__TREES__;
  const MODELS = window.__MODELS__;

  let selectedQid = null;
  let selectedModel = null;

  const modelBar = document.getElementById("model-bar");
  const frame = document.getElementById("frame");
  const openLink = document.getElementById("open-link");

  function showPlaceholder() {
    frame.style.display = "none";
    document.getElementById("placeholder").style.display = "flex";
    openLink.style.display = "none";
  }
  function showFrame(src) {
    document.getElementById("placeholder").style.display = "none";
    frame.style.display = "block";
    frame.src = src;
    openLink.style.display = "inline";
    openLink.href = src;
  }
  function updateModelBar() {
    modelBar.querySelectorAll("button").forEach(b => {
      const m = b.dataset.model;
      const has = !!((allTrees[selectedQid] || {})[m]);
      b.disabled = !has;
      b.classList.toggle("active", m === selectedModel && has);
    });
  }
  function pickModel(m) {
    selectedModel = m;
    updateModelBar();
    const fn = (allTrees[selectedQid] || {})[m];
    if (!fn) { showPlaceholder(); return; }
    showFrame(fn);
  }
  function pickQid(qid, rowEl) {
    selectedQid = qid;
    document.querySelectorAll(".qid-row").forEach(x =>
      x.classList.toggle("selected", x === rowEl));
    const avail = MODELS.filter(m => (allTrees[selectedQid] || {})[m]);
    if (avail.length === 0) { selectedModel = null; updateModelBar(); showPlaceholder(); return; }
    const f = (data.scores[selectedQid] || {});
    // Stable order: keep selectedModel if still available, else pick highest-F1
    if (!selectedModel || !avail.includes(selectedModel)) {
      avail.sort((a, b) => (f[b] ?? 0) - (f[a] ?? 0));
      selectedModel = avail[0];
    }
    pickModel(selectedModel);
  }

  document.querySelectorAll(".qid-row").forEach(r => {
    r.addEventListener("click", () => pickQid(r.dataset.qid, r));
  });
  modelBar.querySelectorAll("button").forEach(b => {
    b.addEventListener("click", () => {
      if (!b.disabled) pickModel(b.dataset.model);
    });
  });

  showPlaceholder();
  // Auto-select first qid for convenience
  const first = document.querySelector(".qid-row");
  if (first) pickQid(first.dataset.qid, first);
})();
"""


def _f1_chip_class(f1: float) -> str:
    if f1 >= 0.5:
        return "high"
    if f1 >= 0.2:
        return "mid"
    return "low"


def _render_qid_row(qid: str, scores: Dict[str, float]) -> str:
    chips = []
    for m in MODELS:
        v = scores.get(m, 0.0) or 0.0
        chips.append(
            f'<span class="score-chip {_f1_chip_class(v)}">'
            f'<span class="label">{html.escape(MODEL_LABELS[m])}</span>'
            f'{v:.2f}</span>'
        )
    return (
        f'<div class="qid-row" data-qid="{html.escape(qid)}">'
        f'  <div class="qid">{html.escape(qid)}</div>'
        f'  <div class="scores">{"".join(chips)}</div>'
        f'</div>'
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", required=True)
    ap.add_argument("--trees-dir", required=True,
                    help="dir with <model>.trees.jsonl files")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sel = json.loads(Path(args.selection).read_text())
    trees_dir = Path(args.trees_dir)

    # Build per-qid scores from selection (we already computed F1 there)
    scores: Dict[str, Dict[str, float]] = {}
    qid_to_task_tier: Dict[str, Any] = {}
    for task, tiers in sel["selection"].items():
        for tier_key in ("tier_a", "tier_b"):
            for r in tiers.get(tier_key, []):
                qid = r["qid"]
                scores[qid] = r["f1"]
                qid_to_task_tier[qid] = (task, tier_key)

    # Render every (model, qid) tree to its own HTML file
    qid_to_files: Dict[str, Dict[str, str]] = {qid: {} for qid in scores}
    counts: Dict[str, int] = {m: 0 for m in MODELS}
    for model in MODELS:
        path = trees_dir / f"{model}.trees.jsonl"
        if not path.exists():
            print(f"[index] WARN: missing {path}; skipping {model}")
            continue
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "tree" not in d:
                    continue
                qid = d.get("qid")
                if qid not in scores:
                    continue
                fname = f"{model}__{_safe_filename(qid)}"
                # Inject the model's own label so the page header shows the
                # right model even if the source jsonl row was missing it.
                d["model"] = model
                (out_dir / fname).write_text(_render_page(d), encoding="utf-8")
                qid_to_files[qid][model] = fname
                counts[model] += 1
    for m, c in counts.items():
        print(f"[index] rendered {c} pages for {m}")

    # Build entry index.html
    sections = []
    for task in TASKS:
        tiers = sel["selection"].get(task) or {}
        rows_a, rows_b = [], []
        for r in tiers.get("tier_a", []):
            rows_a.append(_render_qid_row(r["qid"], r["f1"]))
        for r in tiers.get("tier_b", []):
            rows_b.append(_render_qid_row(r["qid"], r["f1"]))
        sections.append(
            f'<div class="picker-section">'
            f'<h2>{html.escape(task)} <span class="tier-label a">tier A · perf-ordered</span></h2>'
            f'{"".join(rows_a)}'
            f'<h2 style="margin-top:8px">'
            f'<span style="color:transparent">{html.escape(task)}</span>'
            f' <span class="tier-label b">tier B · all bad</span></h2>'
            f'{"".join(rows_b)}'
            f'</div>'
        )

    model_buttons = "".join(
        f'<button data-model="{html.escape(m)}">'
        f'{html.escape(MODEL_LABELS[m])}</button>'
        for m in MODELS
    )

    n_qids = len(scores)
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AgentEHR cross-model trajectory trees</title>
<style>{_INDEX_CSS}</style>
</head>
<body>
<div class="layout">
  <aside class="picker">
    <div class="picker-header">
      <h1>AgentEHR · cross-model tree browser</h1>
      <div class="subtitle">{n_qids} qids · 6 tasks · 4 models · click a qid then a model</div>
    </div>
    <div class="picker-body">
      {''.join(sections)}
    </div>
  </aside>
  <main class="right">
    <div class="model-bar">
      <span class="label">Model:</span>
      <span id="model-bar">{model_buttons}</span>
      <a id="open-link" class="open-link" target="_blank" rel="noopener">open in new tab ↗</a>
    </div>
    <div id="placeholder" class="placeholder">
      Select a qid on the left, then choose a model.
    </div>
    <iframe id="frame" style="display:none"></iframe>
  </main>
</div>
<script>
window.__MODELS__ = {json.dumps(MODELS)};
window.__SELECTION__ = {{"scores": {json.dumps(scores, default=str)}}};
window.__TREES__ = {json.dumps(qid_to_files, default=str)};
</script>
<script>{_INDEX_JS}</script>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html_doc, encoding="utf-8")
    print(f"[index] wrote {out_dir/'index.html'}  ({sum(counts.values())} tree pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
