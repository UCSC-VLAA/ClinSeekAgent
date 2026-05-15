"""Pretty-print a tree (or a whole .trees.jsonl) for eyeball inspection.

Usage:
    python -m analysis.trajectory_trees.inspect_tree path/to/*.trees.jsonl
    python -m analysis.trajectory_trees.inspect_tree file.jsonl --qid labevents_15571908
    python -m analysis.trajectory_trees.inspect_tree file.jsonl --sample-per-task 1
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

INDENT = "  "


def _wrap(text: str, width: int = 100) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _format_node(node: Dict[str, Any], depth: int) -> List[str]:
    prefix = INDENT * depth
    nid = node.get("id", "?")
    ntype = node.get("type", "?")
    title = node.get("title", "")
    lines = [f"{prefix}[{ntype} {nid} L{node.get('level','?')}] {_wrap(title)}"]
    if node.get("type") == "root":
        rt = node.get("root_task", "")
        if rt:
            lines.append(f"{prefix}{INDENT}root_task: {_wrap(rt, 140)}")
    rationale = node.get("rationale")
    if rationale:
        lines.append(f"{prefix}{INDENT}↳ {_wrap(rationale, 140)}")
    if ntype == "tool_call":
        args = json.dumps(node.get("args") or {}, default=str)[:120]
        lines.append(f"{prefix}{INDENT}call: {node.get('tool','?')}({args})")
        obs_shape = node.get("observation_shape")
        if obs_shape:
            lines.append(f"{prefix}{INDENT}obs_shape: {obs_shape}")
        summ = node.get("observation_summary")
        if summ:
            lines.append(f"{prefix}{INDENT}obs: {_wrap(summ, 140)}")
    if ntype == "finish":
        ans = node.get("answer")
        lines.append(f"{prefix}{INDENT}answer: {_wrap(json.dumps(ans, default=str), 140)}")
    for ch in node.get("children") or []:
        lines.extend(_format_node(ch, depth + 1))
    return lines


def format_tree(entry: Dict[str, Any]) -> str:
    head = (
        f"═══ qid={entry.get('qid')}  task={entry.get('task')}  "
        f"model={entry.get('model')}  status={entry.get('status')}  "
        f"events={entry.get('skeleton_stats',{}).get('n_events')}  "
        f"nodes={entry.get('tree_stats',{}).get('n_nodes')}  "
        f"depth={entry.get('tree_stats',{}).get('max_depth')} ═══"
    )
    if "tree" not in entry:
        return head + "\n  (no tree — " + json.dumps({k: v for k, v in entry.items() if k != "label"}) + ")"
    return head + "\n" + "\n".join(_format_node(entry["tree"], 0))


def _load(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--qid", default=None)
    p.add_argument("--sample-per-task", type=int, default=0,
                   help="if >0, print N samples per task and stop")
    p.add_argument("--max", type=int, default=0,
                   help="if >0, print up to N trees total")
    args = p.parse_args(argv)

    rows = _load(args.path)
    if args.qid:
        rows = [r for r in rows if r.get("qid") == args.qid]
    elif args.sample_per_task > 0:
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            if "tree" not in r:
                continue
            buckets[r.get("task", "?")].append(r)
        picked = []
        for t, lst in buckets.items():
            picked.extend(lst[: args.sample_per_task])
        rows = picked
    if args.max:
        rows = rows[: args.max]

    for r in rows:
        print(format_tree(r))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
