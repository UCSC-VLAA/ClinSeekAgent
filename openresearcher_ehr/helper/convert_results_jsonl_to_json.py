#!/usr/bin/env python3
"""
Convert a results.jsonl file into a pretty-printed JSON file and attach
ground-truth answers from the diagnoses benchmark.
"""

import argparse
import json
from pathlib import Path


DEFAULT_RESULTS = Path(
    "/home/efs/zlt/deepresearch/openresearcher_ehr/"
    "diagnoses_ccs_500_serper_results_fixed_20260316_2125/results.jsonl"
)
DEFAULT_BENCHMARK = Path(
    "/home/efs/zlt/deepresearch/data/EHRAgentBench/common/diagnoses_ccs_500.json"
)


def build_qid(record: dict) -> str:
    return f"{record['task']}_{record['subject_id']}_{record['hadm_id']}"


def extract_answer_names(labels: list[dict]) -> list[str]:
    names = []
    seen = set()

    for label in labels:
        name = label.get("name")
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        names.append(name)

    return names


def load_benchmark(benchmark_path: Path) -> dict[str, dict]:
    with benchmark_path.open("r", encoding="utf-8") as f:
        benchmark = json.load(f)

    return {build_qid(item): item for item in benchmark}


def load_results(results_path: Path) -> list[dict]:
    results = []
    with results_path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                results.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Failed to parse JSON at line {line_number} of {results_path}"
                ) from exc
    return results


def augment_results(results: list[dict], benchmark_by_qid: dict[str, dict]) -> tuple[list[dict], list[str]]:
    augmented = []
    missing_qids = []

    for result in results:
        qid = result.get("qid")
        benchmark_item = benchmark_by_qid.get(qid)

        enriched = dict(result)
        if benchmark_item is None:
            enriched["answer"] = None
            enriched["answer_labels"] = []
            missing_qids.append(qid or "<missing-qid>")
        else:
            enriched["task"] = benchmark_item.get("task")
            enriched["subject_id"] = benchmark_item.get("subject_id")
            enriched["hadm_id"] = benchmark_item.get("hadm_id")
            enriched["prediction_time"] = benchmark_item.get("prediction_time")
            enriched["answer"] = extract_answer_names(benchmark_item.get("label", []))
            enriched["answer_labels"] = benchmark_item.get("label", [])

        augmented.append(enriched)

    return augmented, missing_qids


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert results.jsonl to pretty JSON and attach benchmark answers."
        )
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=DEFAULT_RESULTS,
        help="Path to results.jsonl",
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=DEFAULT_BENCHMARK,
        help="Path to diagnoses benchmark JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to output JSON file. Defaults to results_with_answers.json next to results.jsonl.",
    )
    args = parser.parse_args()

    results_path = args.results
    benchmark_path = args.benchmark
    output_path = (
        args.output if args.output else results_path.parent / "results_with_answers.json"
    )

    if not results_path.exists():
        raise SystemExit(f"Results file not found: {results_path}")
    if not benchmark_path.exists():
        raise SystemExit(f"Benchmark file not found: {benchmark_path}")

    benchmark_by_qid = load_benchmark(benchmark_path)
    results = load_results(results_path)
    augmented_results, missing_qids = augment_results(results, benchmark_by_qid)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(augmented_results, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Results input: {results_path}")
    print(f"Benchmark input: {benchmark_path}")
    print(f"Output JSON: {output_path}")
    print(f"Records written: {len(augmented_results)}")
    print(f"Records missing answers: {len(missing_qids)}")
    if missing_qids:
        print("Missing qids:")
        for qid in missing_qids[:20]:
            print(f"  - {qid}")
        if len(missing_qids) > 20:
            print(f"  ... and {len(missing_qids) - 20} more")


if __name__ == "__main__":
    main()
