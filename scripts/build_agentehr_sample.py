#!/usr/bin/env python3
"""Build the fixed AgentEHR-Bench evaluation sample used in the paper.

100 questions per task for five tasks (diagnoses_ccs, procedures_ccs,
labevents, microbiologyevents, transfers) from the MIMIC-IV `common` test split
of AgentEHR-Bench, 500 in total. The draw is deterministic: the seed is fixed
and each task draws from its own RNG stream, so adding or dropping a task never
shifts which rows the other tasks get. Cases whose patient DB is missing are
filtered out *before* sampling, so every drawn row is runnable.

The qids of the paper sample ship with the repository
(clinseekagent/resources/agentehr_common_sample500.json); the rows themselves
are rebuilt from your local AgentEHR-Bench download
(scripts/download_agentehr_bench.sh) and checked against that list.

    python scripts/build_agentehr_sample.py \\
        --root /path/to/AgentEHR-Bench/MIMICIVAgentBench --out /path/to/agentehr_sample500
    python scripts/build_agentehr_sample.py --root ... --out ... --verify

Each `<out>/<task>_100.json` can be passed to scripts/run_agentehr_bench.sh as
CASES_OVERRIDE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path

DEFAULT_TASKS = ["diagnoses_ccs", "procedures_ccs", "labevents",
                 "microbiologyevents", "transfers"]
REFERENCE = (Path(__file__).resolve().parents[1] / "clinseekagent" / "resources"
             / "agentehr_common_sample500.json")


def qid(rec: dict) -> str:
    """Same rule as clinseekagent/scoring/score_text.py:build_qid."""
    return (f"{rec['task']}_{rec['subject_id']}_{rec['hadm_id']}"
            if rec.get("hadm_id") is not None
            else f"{rec['task']}_{rec['subject_id']}")


def draw(root: Path, task: str, split: str, n: int, seed: int):
    matches = sorted(root.glob(f"{split}/{task}_*.json"))
    if not matches:
        sys.exit(f"ERROR: no '{task}' file under {root}/{split}")
    src = matches[0]
    records = json.loads(src.read_text())
    usable = [r for r in records
              if (root / f"database/patient_{r['subject_id']}.db").exists()]
    if len(usable) < n:
        sys.exit(f"ERROR: {task} has only {len(usable)} runnable cases, need {n}")
    # Per-task stream keyed by name: the draw for one task never depends on
    # which other tasks are in the list.
    picked = random.Random(f"{seed}:{task}").sample(usable, n)
    return src, records, usable, picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path,
                    default=Path(os.environ["AGENTEHR_DATA_ROOT"]) / "MIMICIVAgentBench"
                    if os.environ.get("AGENTEHR_DATA_ROOT") else None,
                    help="AgentEHR-Bench MIMICIVAgentBench directory "
                         "(default: $AGENTEHR_DATA_ROOT/MIMICIVAgentBench)")
    ap.add_argument("--out", type=Path, required=True,
                    help="output directory for <task>_<n>.json and manifest.json")
    ap.add_argument("--tasks", nargs="+", default=DEFAULT_TASKS)
    ap.add_argument("--split", default="common")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20260830)
    ap.add_argument("--reference", type=Path, default=REFERENCE,
                    help="qid list the draw must reproduce (the paper sample); "
                         "pass an empty string to skip the check")
    ap.add_argument("--verify", action="store_true",
                    help="recompute and compare against the sample already in "
                         "--out; write nothing, exit non-zero on any drift")
    args = ap.parse_args()
    if args.root is None:
        ap.error("--root is required (or set AGENTEHR_DATA_ROOT)")

    manifest = {"seed": args.seed, "n_per_task": args.n,
                "source_split": args.split, "tasks": {}}
    files: dict[str, list] = {}
    for task in args.tasks:
        src, records, usable, picked = draw(
            args.root, task, args.split, args.n, args.seed)
        files[task] = picked
        manifest["tasks"][task] = {
            "source_file": src.name,
            "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest()[:16],
            "total": len(records), "usable": len(usable),
            "sampled": len(picked), "qids": sorted(qid(r) for r in picked),
        }
        print(f"  {task:20s} total {len(records):>4}  runnable {len(usable):>4}"
              f"  -> sampled {len(picked)}")
    manifest["n_total"] = sum(v["sampled"] for v in manifest["tasks"].values())

    drift = []
    if args.reference and str(args.reference) and Path(args.reference).exists():
        ref = json.loads(Path(args.reference).read_text())
        for task in args.tasks:
            want = ref.get("tasks", {}).get(task, {}).get("qids")
            if want is not None and want != manifest["tasks"][task]["qids"]:
                drift.append(f"{task}: qids differ from {Path(args.reference).name} "
                             "(different AgentEHR-Bench revision or a partial download?)")
    if args.verify:
        for task, picked in files.items():
            f = args.out / f"{task}_{args.n}.json"
            if not f.exists():
                drift.append(f"{task}: {f.name} missing")
                continue
            if sorted(qid(r) for r in json.loads(f.read_text())) != \
                    manifest["tasks"][task]["qids"]:
                drift.append(f"{task}: qids differ from the written sample")
    if drift:
        print("\nDRIFT:")
        for d in drift:
            print(f"  {d}")
        return 1
    if args.verify:
        print(f"\nverified: {manifest['n_total']} rows reproduce exactly")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for task, picked in files.items():
        (args.out / f"{task}_{args.n}.json").write_text(
            json.dumps(picked, ensure_ascii=False, indent=1))
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"\nwrote {manifest['n_total']} rows to {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
