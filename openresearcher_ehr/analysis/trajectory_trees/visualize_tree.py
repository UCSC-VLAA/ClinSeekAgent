"""Render each tree in a trees.jsonl to a standalone interactive HTML page.

The page shows the tree as a *true graph*: left-to-right tidy-tree layout
with SVG edges between parent/child nodes. Each node shows only a concise
label. Click a node → details panel on the right. Double-click → collapse
or expand the subtree rooted at that node. Mouse-wheel zooms, drag pans.

Everything is self-contained (no external JS/CSS). No dependencies.

Usage:
    python -m analysis.trajectory_trees.visualize_tree \\
        analysis/trajectory_trees/pilot_30/claude_opus_4_6.trees.jsonl \\
        --out-dir analysis/trajectory_trees/pilot_30/html_opus

    # Single qid
    python -m analysis.trajectory_trees.visualize_tree \\
        analysis/trajectory_trees/pilot_30/claude_sonnet_4_6.trees.jsonl \\
        --qid diagnoses_ccs_13998457_22470798 --out-dir /tmp/one_tree
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse the official scorer so visualization F1 matches benchmark numbers.
_HERE = os.path.dirname(os.path.abspath(__file__))
_OPENRES = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_OPENRES, "helper"))
from evaluate_results import (  # noqa: E402
    f1_score as _f1_score,
    extract_finish_predictions_with_source as _extract_finish_from_rollout,
)

# ── Page CSS/JS (used verbatim, embedded in every page) ───────────────────

_CSS = r"""
:root {
  --border: #d1d5db;
  --muted: #6b7280;
  --text: #111827;
  --bg: #fafafa;
  --panel: #ffffff;
  --link: #9ca3af;
  --link-dim: #e5e7eb;
  --root: #1e40af;
  --sub:  #3b82f6;
  --tool: #10b981;
  --reason: #f59e0b;
  --finish: #ec4899;
  --selected: #111827;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text); font-size: 13px;
}
.header {
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  background: var(--panel);
}
.header h1 { margin: 0; font-size: 15px; font-weight: 600; }
.header .meta { margin-top: 2px; color: var(--muted); font-size: 11px; }
.header .meta span { margin-right: 14px; }
.outcome {
  margin-top: 6px; border: 1px solid var(--border); border-radius: 3px;
  background: var(--bg);
}
.outcome > summary {
  list-style: none; cursor: pointer; padding: 3px 8px;
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: 11px;
}
.outcome > summary::-webkit-details-marker { display: none; }
.outcome > summary::before {
  content: "▸"; color: var(--muted); width: 9px; font-size: 10px;
  transition: transform .12s; display: inline-block;
}
.outcome[open] > summary::before { transform: rotate(90deg); }
.outcome .pill {
  display: inline-block; font-size: 10px; padding: 1px 6px;
  border-radius: 8px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.pill.f1-high { background: #d1fae5; color: #065f46; }
.pill.f1-mid  { background: #fef3c7; color: #92400e; }
.pill.f1-low  { background: #fee2e2; color: #991b1b; }
.pill.metric  { background: #e5e7eb; color: #374151; }
.outcome-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
  padding: 2px 8px 8px 8px;
}
.outcome-grid .col {
  border: 1px solid var(--border); border-radius: 3px; background: var(--panel);
  padding: 4px 8px; min-height: 0; max-height: 140px; overflow: auto;
}
.outcome-grid h3 {
  margin: 0 0 3px 0; font-size: 10px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em;
}
.outcome-grid ul { margin: 0; padding-left: 16px; }
.outcome-grid li {
  font-size: 11.5px; padding: 0; line-height: 1.35; word-break: break-word;
}
.outcome-grid li.hit     { color: #065f46; }
.outcome-grid li.missing { color: #991b1b; }
.outcome-grid li.extra   { color: #92400e; }
.outcome-grid li .marker { display: inline-block; width: 14px; }
.outcome-grid li.hit .marker     { color: #10b981; }
.outcome-grid li.missing .marker { color: #ef4444; }
.outcome-grid li.extra .marker   { color: #f59e0b; }
.outcome-grid .empty { color: var(--muted); font-style: italic; font-size: 12px; }
.app {
  display: flex; flex: 1; min-height: 0;
}
.page {
  display: flex; flex-direction: column; height: 100%;
}
.canvas-wrap {
  flex: 1; position: relative; overflow: hidden; background: var(--bg);
}
#canvas { width: 100%; height: 100%; cursor: grab; user-select: none; }
#canvas.dragging { cursor: grabbing; }

.controls {
  position: absolute; top: 8px; left: 8px; z-index: 10;
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  padding: 4px; display: flex; gap: 2px; box-shadow: 0 1px 2px rgba(0,0,0,.06);
}
.controls button {
  font: inherit; font-size: 11px; padding: 3px 8px; cursor: pointer;
  background: white; border: 1px solid var(--border); border-radius: 3px;
}
.controls button:hover { background: #f3f4f6; }
.legend {
  position: absolute; bottom: 8px; left: 8px; z-index: 10;
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  padding: 6px 10px; font-size: 11px; color: var(--muted);
  display: flex; gap: 12px; box-shadow: 0 1px 2px rgba(0,0,0,.06);
}
.legend .swatch {
  display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  vertical-align: middle; margin-right: 4px;
}

/* Nodes */
.node rect {
  stroke: var(--border); stroke-width: 1;
  fill: var(--panel); cursor: pointer;
}
.node text {
  pointer-events: none; font-size: 11px; fill: var(--text);
}
.node .bar {
  stroke: none;
}
.node.type-root   .bar { fill: var(--root);   }
.node.type-subproblem .bar { fill: var(--sub); }
.node.type-tool_call  .bar { fill: var(--tool); }
.node.type-reason .bar { fill: var(--reason); }
.node.type-finish .bar { fill: var(--finish); }

.node.has-hidden rect { stroke-dasharray: 3 3; }
.node.selected rect { stroke: var(--selected); stroke-width: 2; }

.node .collapsed-badge {
  fill: var(--text); font-size: 10px; font-weight: 600;
}

.link {
  fill: none; stroke: var(--link); stroke-width: 1;
}
.link.dim { stroke: var(--link-dim); }

/* Sidebar */
.sidebar {
  width: 420px; min-width: 320px; border-left: 1px solid var(--border);
  background: var(--panel); display: flex; flex-direction: column;
  flex-shrink: 0;
}
.sidebar .tab {
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  font-weight: 600; font-size: 12px; display: flex; justify-content: space-between;
  align-items: center;
}
.sidebar .body {
  padding: 10px 14px; overflow-y: auto; flex: 1; line-height: 1.5;
}
.sidebar .placeholder { color: var(--muted); font-style: italic; }
.sidebar h2 {
  font-size: 13px; margin: 12px 0 4px 0; color: #374151;
}
.sidebar h2:first-child { margin-top: 0; }
.sidebar .kv { margin: 4px 0; font-size: 12px; }
.sidebar .label {
  color: var(--muted); font-size: 10px; text-transform: uppercase;
  letter-spacing: .04em; display: block; margin-top: 8px;
}
.sidebar pre {
  margin: 4px 0; padding: 8px 10px; background: #f3f4f6; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; white-space: pre-wrap; word-break: break-word;
  max-height: 40vh; overflow: auto;
}
.sidebar .badge {
  display: inline-block; font-size: 10px; padding: 1px 6px;
  border-radius: 10px; color: white; margin-right: 4px;
  text-transform: uppercase; letter-spacing: .04em;
}
.badge-root   { background: var(--root); }
.badge-subproblem { background: var(--sub); }
.badge-tool_call  { background: var(--tool); }
.badge-reason { background: var(--reason); }
.badge-finish { background: var(--finish); }
"""

_JS = r"""
(function() {
  const DATA = window.__TREE__;
  const NODES = [];
  const EDGES = [];

  // Assign stable numeric ids and collapsed flags
  let nid = 0;
  function walk(n, parent) {
    n.__nid = nid++;
    n.__parent = parent ? parent.__nid : null;
    n.__collapsed = false;
    (n.children || []).forEach(c => walk(c, n));
  }
  walk(DATA, null);

  // ── Layout (simple Reingold-Tilford: leaves get consecutive y slots,
  //          internal nodes sit at the midpoint of their children's y) ───
  const DX = 60;   // vertical step per leaf slot
  const DY = 270;  // horizontal step per depth level
  const NODE_W = 240, NODE_H = 54;

  function layout(root) {
    let leafSlot = 0;
    function visit(n, depth) {
      n.__depth = depth;
      const kids = (n.__collapsed ? [] : (n.children || []));
      if (kids.length === 0) {
        n.__y = leafSlot * DX;
        leafSlot++;
      } else {
        kids.forEach(c => visit(c, depth + 1));
        const ys = kids.map(c => c.__y);
        n.__y = (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2;
      }
      n.__x = depth * DY;
    }
    visit(root, 0);
  }

  function flatten(root) {
    NODES.length = 0; EDGES.length = 0;
    function rec(n) {
      NODES.push(n);
      const kids = (n.__collapsed ? [] : (n.children || []));
      kids.forEach(c => { EDGES.push([n, c]); rec(c); });
    }
    rec(root);
  }

  // ── Labels ────────────────────────────────────────────────────────────
  function shortTitle(n) {
    const t = n.type;
    if (t === "root") return "ROOT";
    if (t === "subproblem") return (n.title || "").trim();
    if (t === "tool_call")  return (n.tool || "tool");
    if (t === "reason")     return "reason";
    if (t === "finish")     return "FINISH";
    return t;
  }
  function subLabel(n) {
    const t = n.type;
    if (t === "root") return (n.title || "").trim();
    if (t === "subproblem") return (n.rationale || "").trim();
    if (t === "tool_call")  return (n.observation_summary || "").trim();
    if (t === "reason")     return (n.rationale || "").trim();
    if (t === "finish") {
      const a = n.answer;
      if (a == null) return "";
      const s = typeof a === "string" ? a : JSON.stringify(a);
      return s;
    }
    return "";
  }
  function truncate(s, n) {
    s = (s || "").replace(/\s+/g, " ");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function countHiddenDescendants(n) {
    if (!n.__collapsed) return 0;
    let c = 0;
    function rec(x) {
      (x.children || []).forEach(k => { c++; rec(k); });
    }
    rec(n);
    return c;
  }

  // ── Render ────────────────────────────────────────────────────────────
  const svg = document.getElementById("canvas");
  const viewport = document.getElementById("viewport");
  let selectedNid = null;

  function render() {
    layout(DATA);
    flatten(DATA);
    viewport.innerHTML = "";

    // Compute bounding box
    const xs = NODES.map(n => n.__x);
    const ys = NODES.map(n => n.__y);
    const xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    const ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    const W = (xmax - xmin) + NODE_W + 40;
    const H = (ymax - ymin) + NODE_H + 40;
    // Re-base so everything starts at (20, 20)
    const ox = 20 - xmin, oy = 20 - ymin;

    // Edges
    for (const [p, c] of EDGES) {
      const x1 = p.__x + ox + NODE_W;
      const y1 = p.__y + oy + NODE_H / 2;
      const x2 = c.__x + ox;
      const y2 = c.__y + oy + NODE_H / 2;
      const mx = (x1 + x2) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "link");
      path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
      viewport.appendChild(path);
    }

    // Nodes
    for (const n of NODES) {
      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("class",
        `node type-${n.type}` +
        (n.__collapsed ? " has-hidden" : "") +
        (n.__nid === selectedNid ? " selected" : ""));
      g.setAttribute("transform",
        `translate(${n.__x + ox}, ${n.__y + oy})`);
      g.setAttribute("data-nid", n.__nid);

      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("width", NODE_W);
      rect.setAttribute("height", NODE_H);
      rect.setAttribute("rx", 4);
      g.appendChild(rect);

      // Left colored bar
      const bar = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      bar.setAttribute("class", "bar");
      bar.setAttribute("width", 5);
      bar.setAttribute("height", NODE_H);
      bar.setAttribute("rx", 2);
      g.appendChild(bar);

      // Main label (row 1)
      const t1 = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t1.setAttribute("x", 12);
      t1.setAttribute("y", 16);
      t1.setAttribute("font-weight", "600");
      t1.textContent = truncate(shortTitle(n), 32);
      g.appendChild(t1);

      // Sub label (row 2) — full width, no overlap
      const t2 = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t2.setAttribute("x", 12);
      t2.setAttribute("y", 32);
      t2.setAttribute("fill", "#6b7280");
      t2.textContent = truncate(subLabel(n), 38);
      g.appendChild(t2);

      // Footer row (row 3): event range left-aligned, hidden-count badge right
      if (n.event_indices && n.event_indices.length) {
        const ev = n.event_indices;
        const er = document.createElementNS("http://www.w3.org/2000/svg", "text");
        er.setAttribute("x", 12);
        er.setAttribute("y", 47);
        er.setAttribute("fill", "#9ca3af");
        er.setAttribute("font-size", "10");
        er.textContent = ev.length === 1
          ? `E${String(ev[0]).padStart(3,"0")}`
          : `E${String(ev[0]).padStart(3,"0")}–E${String(ev[ev.length-1]).padStart(3,"0")}`;
        g.appendChild(er);
      }
      if (n.__collapsed) {
        const hc = countHiddenDescendants(n);
        const tb = document.createElementNS("http://www.w3.org/2000/svg", "text");
        tb.setAttribute("class", "collapsed-badge");
        tb.setAttribute("x", NODE_W - 10);
        tb.setAttribute("y", 47);
        tb.setAttribute("text-anchor", "end");
        tb.setAttribute("font-size", "10");
        tb.textContent = `+${hc} hidden`;
        g.appendChild(tb);
      }

      g.addEventListener("click", ev => {
        ev.stopPropagation();
        if (ev.shiftKey || ev.detail === 2) {
          // Shift+click or double-click: toggle collapse (only if has children)
          if (n.children && n.children.length) {
            n.__collapsed = !n.__collapsed;
            render();
          }
          return;
        }
        selectedNid = n.__nid;
        renderSidebar(n);
        render();
      });
      viewport.appendChild(g);
    }

    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.__bbox = { W, H };
  }

  // ── Sidebar ───────────────────────────────────────────────────────────
  const sidebarBody = document.getElementById("sidebar-body");
  const sidebarTab = document.getElementById("sidebar-tab-title");

  function h(tag, attrs, children) {
    const e = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === "class") e.className = attrs[k];
      else if (k === "html") e.innerHTML = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(c => {
      if (typeof c === "string") e.appendChild(document.createTextNode(c));
      else if (c) e.appendChild(c);
    });
    return e;
  }
  function pretty(v) {
    if (v == null) return "";
    if (typeof v === "string") return v;
    try { return JSON.stringify(v, null, 2); }
    catch (e) { return String(v); }
  }
  function renderSidebar(n) {
    sidebarTab.innerHTML = "";
    sidebarTab.appendChild(h("span", {}, [
      h("span", {class: `badge badge-${n.type}`}, [n.type.toUpperCase()]),
      " ",
      (n.id || "")
    ]));
    const body = sidebarBody;
    body.innerHTML = "";

    // Title
    body.appendChild(h("h2", {}, [shortTitle(n) || n.type]));
    if (n.type === "subproblem" && n.title) {
      body.appendChild(h("div", {class: "kv"}, [n.title]));
    }
    if (n.type === "root" && n.title) {
      body.appendChild(h("div", {class: "kv"}, [n.title]));
    }

    // Rationale
    if (n.rationale) {
      body.appendChild(h("span", {class: "label"}, ["rationale"]));
      body.appendChild(h("div", {class: "kv"}, [n.rationale]));
    }

    // Event / msg references
    const meta = [];
    if (n.level !== undefined) meta.push(`level=${n.level}`);
    if (n.event_indices && n.event_indices.length)
      meta.push(`events=${n.event_indices.join(",")}`);
    if (n.msg_indices && n.msg_indices.length)
      meta.push(`messages=${n.msg_indices.join(",")}`);
    if ((n.children || []).length)
      meta.push(`children=${n.children.length}`);
    if (meta.length) {
      body.appendChild(h("span", {class: "label"}, ["metadata"]));
      body.appendChild(h("div", {class: "kv"}, [meta.join(" · ")]));
    }

    if (n.type === "root") {
      if (n.root_task) {
        body.appendChild(h("span", {class: "label"}, ["root task"]));
        body.appendChild(h("pre", {}, [n.root_task]));
      }
    }
    if (n.type === "tool_call") {
      body.appendChild(h("span", {class: "label"}, ["tool"]));
      body.appendChild(h("div", {class: "kv"}, [n.tool || ""]));
      body.appendChild(h("span", {class: "label"}, ["arguments"]));
      body.appendChild(h("pre", {}, [pretty(n.args)]));
      if (n.call_id) {
        body.appendChild(h("span", {class: "label"}, ["call id"]));
        body.appendChild(h("div", {class: "kv"}, [n.call_id]));
      }
      body.appendChild(h("span", {class: "label"}, ["observation shape"]));
      body.appendChild(h("div", {class: "kv"}, [n.observation_shape || "—"]));
      body.appendChild(h("span", {class: "label"}, ["observation summary"]));
      body.appendChild(h("div", {class: "kv"},
        [n.observation_summary || "(no summary)"]));
    }
    if (n.type === "reason") {
      body.appendChild(h("span", {class: "label"}, ["reasoning text"]));
      body.appendChild(h("pre", {}, [n.rationale || ""]));
    }
    if (n.type === "finish") {
      body.appendChild(h("span", {class: "label"}, ["final answer"]));
      body.appendChild(h("pre", {}, [pretty(n.answer)]));
    }
  }
  function resetSidebar() {
    sidebarTab.textContent = "Details";
    sidebarBody.innerHTML =
      '<div class="placeholder">Click a node to see details. ' +
      'Shift+click or double-click a node to collapse/expand its subtree.</div>';
  }
  resetSidebar();

  // ── Zoom & pan ────────────────────────────────────────────────────────
  let tx = 0, ty = 0, scale = 1;
  function applyTransform() {
    viewport.setAttribute("transform",
      `translate(${tx}, ${ty}) scale(${scale})`);
  }
  function fit() {
    const bbox = svg.__bbox;
    if (!bbox) return;
    const cw = svg.clientWidth, ch = svg.clientHeight;
    const sx = cw / bbox.W, sy = ch / bbox.H;
    scale = Math.min(sx, sy, 1.0);
    tx = (cw - bbox.W * scale) / 2;
    ty = (ch - bbox.H * scale) / 2;
    applyTransform();
  }
  svg.addEventListener("wheel", ev => {
    ev.preventDefault();
    const r = svg.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const wx = (mx - tx) / scale, wy = (my - ty) / scale;
    const k = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    scale = Math.max(0.15, Math.min(3.0, scale * k));
    tx = mx - wx * scale;
    ty = my - wy * scale;
    applyTransform();
  }, { passive: false });

  let dragging = false, dragStart = null;
  svg.addEventListener("mousedown", ev => {
    if (ev.target.closest(".node")) return;
    dragging = true; svg.classList.add("dragging");
    dragStart = { x: ev.clientX - tx, y: ev.clientY - ty };
  });
  document.addEventListener("mousemove", ev => {
    if (!dragging) return;
    tx = ev.clientX - dragStart.x;
    ty = ev.clientY - dragStart.y;
    applyTransform();
  });
  document.addEventListener("mouseup", () => {
    dragging = false; svg.classList.remove("dragging");
  });

  // Controls
  document.getElementById("ctl-fit").addEventListener("click", fit);
  document.getElementById("ctl-zoom-in").addEventListener("click", () => {
    scale = Math.min(3.0, scale * 1.25); applyTransform();
  });
  document.getElementById("ctl-zoom-out").addEventListener("click", () => {
    scale = Math.max(0.15, scale / 1.25); applyTransform();
  });
  document.getElementById("ctl-expand-all").addEventListener("click", () => {
    function rec(n) { n.__collapsed = false; (n.children||[]).forEach(rec); }
    rec(DATA); render(); fit();
  });
  document.getElementById("ctl-collapse-deep").addEventListener("click", () => {
    // Collapse at depth 2 (keep root + level-1 subproblems expanded)
    function rec(n, d) {
      n.__collapsed = (d >= 2 && (n.children||[]).length > 0);
      (n.children||[]).forEach(c => rec(c, d + 1));
    }
    rec(DATA, 0); render(); fit();
  });

  render();
  // Initial fit after DOM has laid out
  requestAnimationFrame(fit);
})();
"""


# ── Python-side rendering ─────────────────────────────────────────────────


def _safe_filename(qid: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in qid) + ".html"


def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def _gold_names(label: Any) -> List[str]:
    """Mirror the f1_score gold-extraction logic for *display*.

    f1_score lowercases for matching; we keep original case for display.
    """
    if isinstance(label, str):
        return [label]
    if not isinstance(label, list):
        return []
    has_atc = any(isinstance(a, dict) and a.get("atc_name") for a in label)
    out: List[str] = []
    for a in label:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            if has_atc:
                v = a.get("atc_name")
                if isinstance(v, str):
                    out.append(v)
            else:
                v = a.get("name")
                if isinstance(v, str):
                    out.append(v)
    return out


def _looks_like_finish_tool(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    return n == "finish" or n.endswith("_finish") or n.endswith(".finish")


def _coerce_answer(ans: Any) -> List[str]:
    if isinstance(ans, list):
        return [a for a in ans if isinstance(a, str)]
    if isinstance(ans, str):
        return [ans]
    return []


def _extract_prediction(tree: Dict[str, Any]) -> List[str]:
    """Recover the model's final answer from the tree.

    Preferred source: a node of `type=finish` (skeleton recognized the
    finish call). Fallback: a `tool_call` whose tool name resembles
    `finish` (e.g. Kimi's bare `finish`) — this happens when the skeleton
    pre-dated the cross-model normalization and is kept as defensive.
    """
    stack = [tree]
    fallback: List[str] = []
    while stack:
        n = stack.pop()
        if n.get("type") == "finish":
            return _coerce_answer(n.get("answer"))
        if n.get("type") == "tool_call" and _looks_like_finish_tool(n.get("tool", "")):
            args = n.get("args") or {}
            if isinstance(args, dict):
                fallback = _coerce_answer(args.get("response"))
        stack.extend(n.get("children") or [])
    return fallback


def _diff_predictions(preds: List[str], golds: List[str]
                      ) -> Tuple[List[str], List[str], List[str]]:
    """Return (hits, missing, extra), preserving original casing for display."""
    p_lower = {p.lower(): p for p in preds}
    g_lower = {g.lower(): g for g in golds}
    hit_keys = set(p_lower) & set(g_lower)
    hits = [g_lower[k] for k in hit_keys]
    missing = [g for k, g in g_lower.items() if k not in hit_keys]
    extra = [p for k, p in p_lower.items() if k not in hit_keys]
    return sorted(hits), sorted(missing), sorted(extra)


def _f1_pill_class(f1: float) -> str:
    if f1 >= 0.5: return "f1-high"
    if f1 >= 0.2: return "f1-mid"
    return "f1-low"


def _render_outcome_block(label: Any, prediction: List[str]) -> str:
    golds = _gold_names(label)
    sc = _f1_score(prediction, label)
    f1, prec, rec = sc.get("f1", 0.0), sc.get("prec", 0.0), sc.get("rec", 0.0)
    hits, missing, extra = _diff_predictions(prediction, golds)

    hit_set_lower = {h.lower() for h in hits}
    extra_set_lower = {e.lower() for e in extra}

    def _ul_items(items: List[Tuple[str, str, str]]) -> str:
        # items: list of (text, css_class, marker)
        if not items:
            return '<div class="empty">(none)</div>'
        return "<ul>" + "".join(
            f'<li class="{cls}"><span class="marker">{marker}</span>{_esc(text)}</li>'
            for text, cls, marker in items
        ) + "</ul>"

    # Gold column — tick-or-cross next to every gold item
    gold_items = []
    for g in sorted(golds, key=str.lower):
        if g.lower() in hit_set_lower:
            gold_items.append((g, "hit", "✓"))
        else:
            gold_items.append((g, "missing", "✗"))
    # Prediction column — tick-or-plus next to every prediction
    pred_items = []
    for p in sorted(prediction, key=str.lower):
        if p.lower() in extra_set_lower:
            pred_items.append((p, "extra", "+"))
        else:
            pred_items.append((p, "hit", "✓"))

    pill = _f1_pill_class(f1)
    summary_chips = (
        f'<span class="pill {pill}">F1 {f1:.2f}</span>'
        f'<span class="pill metric">P {prec:.2f}</span>'
        f'<span class="pill metric">R {rec:.2f}</span>'
        f'<span class="pill metric">{len(hits)} hit · {len(missing)} miss · {len(extra)} extra</span>'
    )
    return f"""<details class="outcome">
  <summary><strong>Outcome</strong>{summary_chips}</summary>
  <div class="outcome-grid">
    <div class="col">
      <h3>Gold label · {len(golds)} item(s)</h3>
      {_ul_items(gold_items)}
    </div>
    <div class="col">
      <h3>Prediction · {len(prediction)} item(s)</h3>
      {_ul_items(pred_items)}
    </div>
  </div>
</details>"""


def _render_page(entry: Dict[str, Any]) -> str:
    qid = entry.get("qid") or ""
    task = entry.get("task") or ""
    model = entry.get("model") or ""
    status = entry.get("status") or ""
    sk = entry.get("skeleton_stats") or {}
    ts = entry.get("tree_stats") or {}
    tree = entry["tree"]
    tree_json = json.dumps(tree, ensure_ascii=False, default=str)

    label = entry.get("label")
    prediction = _extract_prediction(tree)
    outcome_block = _render_outcome_block(label, prediction)
    sc = _f1_score(prediction, label)
    f1_chip = (
        f'<span class="pill {_f1_pill_class(sc["f1"])}">F1 {sc["f1"]:.2f}</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(qid)} — {_esc(task)} — {_esc(model)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
<div class="header">
  <h1>{_esc(qid)}</h1>
  <div class="meta">
    <span><strong>task:</strong> {_esc(task)}</span>
    <span><strong>model:</strong> {_esc(model)}</span>
    <span><strong>status:</strong> {_esc(status)}</span>
    <span><strong>events:</strong> {_esc(sk.get('n_events','?'))}</span>
    <span><strong>nodes:</strong> {_esc(ts.get('n_nodes','?'))}</span>
    <span><strong>depth:</strong> {_esc(ts.get('max_depth','?'))}</span>
    <span>{f1_chip}</span>
  </div>
  {outcome_block}
</div>
<div class="app">
  <div class="canvas-wrap">
    <div class="controls">
      <button id="ctl-fit" title="Fit to window">Fit</button>
      <button id="ctl-zoom-in">+</button>
      <button id="ctl-zoom-out">−</button>
      <button id="ctl-expand-all" title="Expand every node">Expand all</button>
      <button id="ctl-collapse-deep" title="Collapse everything below depth 2">
        Collapse deep
      </button>
    </div>
    <div class="legend">
      <span><span class="swatch" style="background:var(--root)"></span>root</span>
      <span><span class="swatch" style="background:var(--sub)"></span>subproblem</span>
      <span><span class="swatch" style="background:var(--tool)"></span>tool call</span>
      <span><span class="swatch" style="background:var(--reason)"></span>reason</span>
      <span><span class="swatch" style="background:var(--finish)"></span>finish</span>
      <span style="color:#9ca3af">· drag to pan · wheel to zoom · click node for details · shift+click or double-click to collapse</span>
    </div>
    <svg id="canvas"><g id="viewport"></g></svg>
  </div>
  <div class="sidebar">
    <div class="tab" id="sidebar-tab">
      <span id="sidebar-tab-title">Details</span>
    </div>
    <div class="body" id="sidebar-body"></div>
  </div>
</div>
<script>window.__TREE__ = {tree_json};</script>
<script>{_JS}</script>
</div>
</body>
</html>
"""


def _render_index(entries: List[Dict[str, Any]], title: str) -> str:
    by_task: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_task[e.get("task", "?")].append(e)
    sections = []
    for task in sorted(by_task):
        rows = []
        for e in by_task[task]:
            ts = e.get("tree_stats") or {}
            sk = e.get("skeleton_stats") or {}
            rows.append(
                f'<li><a href="{_esc(e["_filename"])}">{_esc(e.get("qid"))}</a>'
                f' <span style="color:#9ca3af;font-size:11px">'
                f'events={_esc(sk.get("n_events","?"))}, '
                f'nodes={_esc(ts.get("n_nodes","?"))}, '
                f'depth={_esc(ts.get("max_depth","?"))}</span></li>'
            )
        sections.append(
            f'<h2 style="font-size:14px;margin:16px 0 4px 0;color:#334155">'
            f'{_esc(task)} <span style="color:#9ca3af;font-weight:normal">'
            f'({len(rows)})</span></h2><ul style="margin:4px 0 0 20px;padding:0">'
            f'{"".join(rows)}</ul>'
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
  "Helvetica Neue", Arial, sans-serif; font-size: 13px; padding: 16px;
  background: #fafafa; color: #111827; }}
h1 {{ margin: 0 0 4px 0; font-size: 16px; }}
.meta {{ color: #6b7280; font-size: 11px; margin-bottom: 16px; }}
a {{ text-decoration: none; color: #1d4ed8; }}
a:hover {{ text-decoration: underline; }}
ul li {{ list-style: disc; margin: 2px 0; }}
</style></head>
<body>
<h1>{_esc(title)}</h1>
<div class="meta">{len(entries)} trees</div>
{''.join(sections)}
</body></html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path", help="path to a *.trees.jsonl")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--qid", default=None, help="render only this qid")
    p.add_argument("--title", default=None)
    args = p.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries: List[Dict[str, Any]] = []
    with open(args.path) as f:
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
            if args.qid and d.get("qid") != args.qid:
                continue
            entries.append(d)

    if not entries:
        print(f"[visualize_tree] no trees in {args.path}", file=sys.stderr)
        return 1

    for e in entries:
        fname = _safe_filename(e["qid"])
        e["_filename"] = fname
        (out_dir / fname).write_text(_render_page(e), encoding="utf-8")
    if not args.qid or len(entries) > 1:
        title = args.title or f"{Path(args.path).stem} — {len(entries)} trees"
        (out_dir / "index.html").write_text(_render_index(entries, title),
                                            encoding="utf-8")
    print(f"[visualize_tree] wrote {len(entries)} page(s) → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
