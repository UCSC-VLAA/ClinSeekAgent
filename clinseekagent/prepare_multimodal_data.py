#!/usr/bin/env python3
"""Filter the multimodal benchmark manifests into an easy-to-load JSONL.

Given a source manifest (e.g.
`data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3/common/ready/test.json`),
emit only rows that match the requested tasks / scope and optionally keep just
a small sample for smoke tests.

Usage:
    python prepare_multimodal_data.py \
      --src .../EHRXQAAgentBench_v3/common/ready/test.json \
      --tasks ehrxqa_image,ehrxqa_image_table \
      --scope patient_scope \
      --out ./data_mm/ehrxqa_image_test.jsonl \
      --sample_size 3
"""
import argparse
import json
import os
import random
from pathlib import Path
from typing import List


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=str, required=True,
                        help="Source manifest JSON (list of records).")
    parser.add_argument("--tasks", type=str, required=True,
                        help="Comma-separated task names to keep (e.g. ehrxqa_image,medmod_radiology).")
    parser.add_argument("--scope", type=str, default="patient_scope",
                        choices=["patient_scope", "cohort_scope", "any"])
    parser.add_argument("--require_images", action="store_true",
                        help="Keep only rows that have a non-empty image_paths list.")
    parser.add_argument("--require_subject_id", action="store_true",
                        help="Keep only rows where subject_id is non-null.")
    parser.add_argument("--sample_size", type=int, default=0,
                        help="If >0, randomly keep this many rows (shuffled with --seed).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, required=True, help="Output JSONL path.")
    parser.add_argument("--out_format", type=str, default="jsonl", choices=["jsonl", "json"])
    args = parser.parse_args()

    tasks = {t.strip() for t in args.tasks.split(",") if t.strip()}
    if not tasks:
        raise SystemExit("--tasks must list at least one task")

    print(f"Loading {args.src} ...")
    with open(args.src, "r") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"Expected top-level JSON list, got {type(data).__name__}")
    print(f"Loaded {len(data)} records")

    filtered: List[dict] = []
    for row in data:
        if row.get("task") not in tasks:
            continue
        if args.scope != "any" and row.get("scope") != args.scope:
            continue
        if args.require_images and not row.get("image_paths"):
            continue
        if args.require_subject_id and row.get("subject_id") in (None, "", 0):
            continue
        filtered.append(row)
    print(f"After filter: {len(filtered)} records "
          f"(tasks={sorted(tasks)}, scope={args.scope})")

    if args.sample_size and args.sample_size < len(filtered):
        rng = random.Random(args.seed)
        rng.shuffle(filtered)
        filtered = filtered[: args.sample_size]
        print(f"Sampled {len(filtered)} rows with seed {args.seed}")

    os.makedirs(Path(args.out).parent, exist_ok=True)
    if args.out_format == "jsonl":
        with open(args.out, "w") as f:
            for row in filtered:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        with open(args.out, "w") as f:
            json.dump(filtered, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(filtered)} rows to {args.out} ({args.out_format})")


if __name__ == "__main__":
    main()
