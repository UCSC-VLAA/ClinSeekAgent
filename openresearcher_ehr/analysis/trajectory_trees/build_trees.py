"""CLI: read AgentEHR results.jsonl, build trajectory trees with Claude Opus 4.6.

Default behaviour: pilot mode — 1 model × 6 tasks × 5 samples = 30 trees,
written to `pilot_30/<model>.trees.jsonl` with a sibling `stats.json`.

Usage examples:

    # Pilot (default): claude_sonnet_4_6, 5 samples per task
    python -m analysis.trajectory_trees.build_trees

    # Full model, all 600
    python -m analysis.trajectory_trees.build_trees \
        --model claude_sonnet_4_6 --per-task all \
        --out-dir analysis/trajectory_trees/full/

    # Different model, smaller sample
    python -m analysis.trajectory_trees.build_trees \
        --model gpt_oss_120b --per-task 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_OPENRES_DIR = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _OPENRES_DIR not in sys.path:
    sys.path.insert(0, _OPENRES_DIR)
_ANALYSIS_PARENT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ANALYSIS_PARENT not in sys.path:
    sys.path.insert(0, _ANALYSIS_PARENT)

from analysis.trajectory_trees.bedrock_client import BedrockOpusJSON, DEFAULT_OPUS_4_6, DEFAULT_REGION  # noqa: E402
from analysis.trajectory_trees.tree_builder import BuildConfig, build_tree  # noqa: E402


DEFAULT_RESULTS_ROOT = (
    "results/agent_ehr_bench/agentic/subset600"
)
TASKS = [
    "diagnoses_ccs",
    "labevents",
    "microbiologyevents",
    "prescriptions",
    "procedures_ccs",
    "transfers",
]


def _iter_records(path: str) -> Iterable[Dict[str, Any]]:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[build_trees] bad JSON line in {path}: {e}")


def _select_samples(records: Iterable[Dict[str, Any]], per_task: str) -> List[Dict[str, Any]]:
    """Select samples: either all, or first N per task."""
    if per_task == "all":
        return [r for r in records if r.get("task") in TASKS]
    n = int(per_task)
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        t = r.get("task")
        if t in TASKS and len(buckets[t]) < n:
            buckets[t].append(r)
        if all(len(buckets[x]) >= n for x in TASKS):
            break
    out: List[Dict[str, Any]] = []
    for t in TASKS:
        out.extend(buckets.get(t, []))
    return out


def _existing_qids(out_path: Path) -> set:
    if not out_path.exists():
        return set()
    qids = set()
    with out_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            qid = d.get("qid")
            if qid:
                qids.add(qid)
    return qids


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="claude_sonnet_4_6",
                   help="trajectory source model directory name under subset600/")
    p.add_argument("--results-root", default=DEFAULT_RESULTS_ROOT)
    p.add_argument("--per-task", default="5",
                   help="samples per task, or 'all'")
    p.add_argument("--out-dir", default="analysis/trajectory_trees/pilot_30")
    p.add_argument("--bedrock-model-id", default=DEFAULT_OPUS_4_6)
    p.add_argument("--bedrock-region", default=DEFAULT_REGION)
    p.add_argument("--force", action="store_true",
                   help="overwrite existing output file (default: resume)")
    p.add_argument("--no-leaf-summary", action="store_true",
                   help="skip Stage 3 leaf summaries (cheaper, less detail)")
    p.add_argument("--max-depth", type=int, default=BuildConfig.max_depth)
    p.add_argument("--start-index", type=int, default=0,
                   help="skip the first N selected samples (debug)")
    p.add_argument("--limit", type=int, default=None,
                   help="only process this many samples total (debug)")
    p.add_argument("--qids-from-json", default=None,
                   help="path to selected_qids.json (from select_qids.py); "
                        "selects exactly the listed qids regardless of --per-task")
    args = p.parse_args(argv)

    src = Path(args.results_root) / args.model / "results.jsonl"
    if not src.exists():
        print(f"[build_trees] no such file: {src}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.model}.trees.jsonl"
    stats_path = out_dir / f"{args.model}.stats.json"

    if args.force and out_path.exists():
        out_path.unlink()

    done_qids = _existing_qids(out_path)
    print(f"[build_trees] output: {out_path}  (resuming over {len(done_qids)} existing)")

    if args.qids_from_json:
        with open(args.qids_from_json) as f:
            qids_doc = json.load(f)
        wanted = set()
        for task, tiers in qids_doc.get("selection", {}).items():
            for tier in ("tier_a", "tier_b"):
                for r in tiers.get(tier, []):
                    wanted.add(r["qid"])
        selected = [r for r in _iter_records(str(src)) if r.get("qid") in wanted]
        print(f"[build_trees] qid-list mode: {len(selected)}/{len(wanted)} qids found")
    else:
        selected = _select_samples(_iter_records(str(src)), args.per_task)
    if args.start_index:
        selected = selected[args.start_index :]
    if args.limit is not None:
        selected = selected[: args.limit]
    print(f"[build_trees] selected {len(selected)} trajectories "
          f"({args.model}, per_task={args.per_task})")

    cfg = BuildConfig(
        max_depth=args.max_depth,
        summarize_leaves=not args.no_leaf_summary,
    )

    client = BedrockOpusJSON(model_id=args.bedrock_model_id, region_name=args.bedrock_region)

    all_stats = {
        "model": args.model,
        "source": str(src),
        "per_task": args.per_task,
        "bedrock_model_id": args.bedrock_model_id,
        "n_selected": len(selected),
        "n_done": 0,
        "n_skipped": 0,
        "n_errors": 0,
        "trees": [],
    }
    t_start = time.time()
    try:
        with out_path.open("a") as f_out:
            for idx, rec in enumerate(selected):
                qid = rec.get("qid")
                if qid in done_qids:
                    continue
                if not rec.get("messages") or rec.get("status") not in ("success", "partial"):
                    f_out.write(json.dumps({
                        "qid": qid, "task": rec.get("task"),
                        "skipped": rec.get("status", "no_messages"),
                    }) + "\n")
                    f_out.flush()
                    all_stats["n_skipped"] += 1
                    continue

                t0 = time.time()
                try:
                    result = build_tree(rec["messages"], client, cfg)
                    out_row = {
                        "qid": qid,
                        "task": rec.get("task"),
                        "model": args.model,
                        "subject_id": rec.get("subject_id"),
                        "hadm_id": rec.get("hadm_id"),
                        "prediction_time": rec.get("prediction_time"),
                        "label": rec.get("label"),
                        "status": rec.get("status"),
                        "stop_reason": rec.get("stop_reason"),
                        "tree": result["root"],
                        "skeleton_stats": result["skeleton_stats"],
                        "tree_stats": result["tree_stats"],
                    }
                    f_out.write(json.dumps(out_row, default=str) + "\n")
                    f_out.flush()
                    all_stats["n_done"] += 1
                    all_stats["trees"].append({
                        "qid": qid,
                        "task": rec.get("task"),
                        **result["skeleton_stats"],
                        **result["tree_stats"],
                        "elapsed_s": round(time.time() - t0, 2),
                    })
                    print(
                        f"[{idx+1}/{len(selected)}] qid={qid} task={rec.get('task')} "
                        f"n_events={result['skeleton_stats']['n_events']} "
                        f"nodes={result['tree_stats']['n_nodes']} "
                        f"depth={result['tree_stats']['max_depth']} "
                        f"time={time.time()-t0:.1f}s"
                    )
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    all_stats["n_errors"] += 1
                    f_out.write(json.dumps({
                        "qid": qid, "task": rec.get("task"),
                        "error": repr(e),
                    }) + "\n")
                    f_out.flush()
    finally:
        all_stats["wall_s"] = round(time.time() - t_start, 1)
        all_stats["bedrock_stats"] = dict(client.stats)
        with stats_path.open("w") as f:
            json.dump(all_stats, f, indent=2)
        client.close()
        print(f"[build_trees] done. stats → {stats_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
