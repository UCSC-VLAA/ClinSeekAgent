"""Pick 6 qids per task for the cross-model tree-comparison study.

Selection criteria:
  Tier A (3 qids per task) — performance-ordered:
    F1(opus) >= F1(sonnet) >= F1(kimi) >= F1(qwen3),
    with strict-inequality at *some* boundary (so the four models disagree),
    but allow ties at opus≈sonnet and kimi≈qwen3 (≤ 0.05 gap).
    Pick the qids whose ordering is *most consistent* with the desired rank.

  Tier B (3 qids per task) — all four models score F1 ≤ 0.30.

Output JSON: {task: {tier_a: [qid,...], tier_b: [qid,...]}}.

Run:
    python -m analysis.trajectory_trees.select_qids \\
        --out analysis/trajectory_trees/cross_model/selected_qids.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_OPENRES = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _OPENRES not in sys.path:
    sys.path.insert(0, _OPENRES)

# Reuse the existing F1 scorer
sys.path.insert(0, os.path.join(_OPENRES, "helper"))
from evaluate_results import (  # noqa: E402
    extract_finish_predictions_with_source,
    f1_score,
)

MODELS = ["claude_opus_4_6", "claude_sonnet_4_6", "kimi_k2_5", "qwen3_235b"]
DEFAULT_ROOT = "results/agent_ehr_bench/agentic/subset600"
TASKS = [
    "diagnoses_ccs",
    "labevents",
    "microbiologyevents",
    "prescriptions",
    "procedures_ccs",
    "transfers",
]
NEAR_TIE = 0.05    # Tolerance for opus≈sonnet and kimi≈qwen3
LOW_F1 = 0.30      # All-bad threshold for tier B


def _per_qid_f1(model: str, root: Path) -> Dict[str, Dict[str, Any]]:
    """Return {qid: {task, f1, label}} for one model."""
    out: Dict[str, Dict[str, Any]] = {}
    p = root / model / "results.jsonl"
    with p.open() as f:
        for line in f:
            d = json.loads(line)
            preds, _ = extract_finish_predictions_with_source(d, allow_text=False)
            sc = f1_score(preds, d.get("label"))
            out[d["qid"]] = {
                "task": d["task"],
                "f1": sc["f1"],
                "label": d.get("label"),
                "subject_id": d.get("subject_id"),
                "hadm_id": d.get("hadm_id"),
            }
    return out


def _tier_a_score(opus: float, sonnet: float, kimi: float, qwen: float) -> float:
    """Higher = better candidate for tier A (perf-ordered).

    We want opus ≳ sonnet >> kimi ≳ qwen3:
      • reward gap between (opus,sonnet) avg and (kimi,qwen3) avg
      • penalize opus-sonnet inversion or kimi-qwen3 inversion beyond NEAR_TIE
      • penalize qids where everyone is identical (0/0 or 1/1)
    """
    top_avg = (opus + sonnet) / 2.0
    bot_avg = (kimi + qwen) / 2.0
    gap = top_avg - bot_avg
    if gap <= 0.05:
        return -1.0  # not perf-ordered enough
    pen = 0.0
    if sonnet - opus > NEAR_TIE:           # sonnet beats opus by more than tie
        pen += (sonnet - opus) * 2
    if qwen - kimi > NEAR_TIE:
        pen += (qwen - kimi) * 2
    # Mild penalty for cases where opus is also low (we want opus to be GOOD)
    if opus < 0.3:
        pen += 0.5 * (0.3 - opus)
    return gap - pen


def _select_for_task(qids: List[str], scores_by_model: Dict[str, Dict[str, Dict]],
                     task: str) -> Dict[str, List[Dict[str, Any]]]:
    rows = []
    for qid in qids:
        f = {m: scores_by_model[m][qid]["f1"] for m in MODELS}
        rows.append({
            "qid": qid,
            "f1": f,
            "tier_a_score": _tier_a_score(
                f["claude_opus_4_6"], f["claude_sonnet_4_6"],
                f["kimi_k2_5"], f["qwen3_235b"],
            ),
            "all_bad": all(v <= LOW_F1 for v in f.values()),
            "max_f1": max(f.values()),
        })
    # Tier A: highest tier_a_score
    a_pool = [r for r in rows if r["tier_a_score"] > 0]
    a_pool.sort(key=lambda r: -r["tier_a_score"])
    tier_a = a_pool[:3]
    # Tier B: lowest max_f1 (most universally hard); break ties by lowest sum
    used = {x["qid"] for x in tier_a}
    b_pool = [r for r in rows if r["all_bad"] and r["qid"] not in used]
    b_pool.sort(key=lambda r: (r["max_f1"], sum(r["f1"].values())))
    if len(b_pool) < 3:
        # Relax: take qids with the lowest max_f1 even if max_f1 > 0.30
        relaxed = [r for r in rows if r["qid"] not in used
                   and r not in b_pool]
        relaxed.sort(key=lambda r: (r["max_f1"], sum(r["f1"].values())))
        b_pool.extend(relaxed[: 3 - len(b_pool)])
    tier_b = b_pool[:3]
    return {"tier_a": tier_a, "tier_b": tier_b}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    root = Path(args.root)
    print(f"[select] computing per-qid F1 for {len(MODELS)} models …")
    scores = {m: _per_qid_f1(m, root) for m in MODELS}

    by_task_qids: Dict[str, List[str]] = defaultdict(list)
    for qid, info in scores[MODELS[0]].items():
        if all(qid in scores[m] for m in MODELS):
            by_task_qids[info["task"]].append(qid)

    selection: Dict[str, Any] = {}
    for task in TASKS:
        sel = _select_for_task(by_task_qids[task], scores, task)
        selection[task] = sel

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({
            "models": MODELS,
            "near_tie_tolerance": NEAR_TIE,
            "low_f1_threshold": LOW_F1,
            "selection": selection,
        }, f, indent=2, default=str)
    # Print a digest
    for task in TASKS:
        s = selection[task]
        print(f"\n=== {task} ===")
        print("  tier_a (perf-ordered):")
        for r in s["tier_a"]:
            print(f"    {r['qid']:40s}  "
                  f"opus={r['f1']['claude_opus_4_6']:.2f}  "
                  f"sonnet={r['f1']['claude_sonnet_4_6']:.2f}  "
                  f"kimi={r['f1']['kimi_k2_5']:.2f}  "
                  f"qwen={r['f1']['qwen3_235b']:.2f}  "
                  f"score={r['tier_a_score']:.2f}")
        print("  tier_b (all bad):")
        for r in s["tier_b"]:
            print(f"    {r['qid']:40s}  "
                  f"opus={r['f1']['claude_opus_4_6']:.2f}  "
                  f"sonnet={r['f1']['claude_sonnet_4_6']:.2f}  "
                  f"kimi={r['f1']['kimi_k2_5']:.2f}  "
                  f"qwen={r['f1']['qwen3_235b']:.2f}")
    print(f"\n[select] wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
