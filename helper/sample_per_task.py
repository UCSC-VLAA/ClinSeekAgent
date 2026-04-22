#!/usr/bin/env python3
"""
Stratified random sampling for EHR-Bench: draws N questions per `task`.

Default: sample 40 per task from `data/EHR-Bench/ehr_bench_merged_filtered.json`
into `data/EHR-Bench/ehr_bench_sampled_40_per_task.json`, using seed 42.

Usage (run from repo root):
    python helper/sample_per_task.py
    python helper/sample_per_task.py --per-task 40 --seed 42
    python helper/sample_per_task.py \
        --input  data/EHR-Bench/ehr_bench_merged_filtered.json \
        --output data/EHR-Bench/ehr_bench_sampled_40_per_task.json \
        --per-task 40 --seed 42
"""

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def main():
    bench_dir = Path("data/EHR-Bench")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=bench_dir / "ehr_bench_merged_filtered.json")
    parser.add_argument("--output", type=Path, default=bench_dir / "ehr_bench_sampled_40_per_task.json")
    parser.add_argument("--per-task", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise SystemExit(f"Expected a JSON list at {args.input}, got {type(data).__name__}")

    buckets = defaultdict(list)
    for item in data:
        task = item.get("task", "unknown")
        buckets[task].append(item)

    rng = random.Random(args.seed)
    sampled = []
    summary = []
    for task in sorted(buckets):
        bucket = buckets[task]
        n_take = min(args.per_task, len(bucket))
        picks = rng.sample(bucket, n_take)
        sampled.extend(picks)
        summary.append((task, len(bucket), n_take))

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(sampled, f, ensure_ascii=False, indent=2)

    type_counts = Counter(item.get("task_type", "unknown") for item in sampled)

    print(f"Input:      {args.input}")
    print(f"Output:     {args.output}")
    print(f"Seed:       {args.seed}")
    print(f"Per task:   {args.per_task}")
    print(f"Tasks:      {len(summary)}")
    print(f"Total rows: {len(sampled)}")
    print()
    print(f"{'task':<28s} {'available':>9s} {'sampled':>7s}")
    print("-" * 48)
    for task, avail, taken in summary:
        marker = "" if taken == args.per_task else "  (short)"
        print(f"{task:<28s} {avail:>9d} {taken:>7d}{marker}")
    print()
    print("By task_type:", dict(type_counts))


if __name__ == "__main__":
    main()
