#!/usr/bin/env python3
import argparse
import ast
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_RESULTS = (
    "/home/efs/zlt/deepresearch/openresearcher_ehr/diagnoses_ccs_500_results_qwen3_5_35b_a3b_deepmed_sft_epoch2_vllm_fixed_20260326T230132Z/results.jsonl"
)
DEFAULT_BENCHMARK = (
    "/home/efs/zlt/deepresearch/data/EHRAgentBench/train/diagnoses_ccs_500.json"
)


def build_qid(record):
    task = record["task"]
    subject_id = record["subject_id"]
    hadm_id = record["hadm_id"]
    return f"{task}_{subject_id}_{hadm_id}"


def calculate_true_best_at_k(scores_list, k, metric="f1_score"):
    values = sorted(item[metric] for item in scores_list)
    n = len(values)

    if n == 0:
        return 0.0

    if k > n:
        k = n

    expected_max = 0.0
    total_combinations = math.comb(n, k)

    for i in range(k, n + 1):
        weight = math.comb(i - 1, k - 1) / total_combinations
        expected_max += weight * values[i - 1]

    return expected_max


def parse_json_or_python(value):
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        raise TypeError(f"Unsupported arguments type: {type(value)}")

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return ast.literal_eval(value)


def extract_finish_predictions(result):
    messages = result.get("messages", [])

    for message in reversed(messages):
        tool_calls = message.get("tool_calls") or []
        for tool_call in reversed(tool_calls):
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            if "finish" not in tool_name.lower():
                continue

            try:
                arguments = parse_json_or_python(function.get("arguments", {}))
            except Exception:
                return []

            if isinstance(arguments, dict):
                predictions = arguments.get("response", [])
            else:
                predictions = arguments

            if isinstance(predictions, list):
                return predictions
            return []

    return []


def f1_score(predictions, standard_answer):
    prediction_set = set()
    for item in predictions:
        if isinstance(item, str):
            prediction_set.add(item)

    ground_truth = set()
    for answer in standard_answer:
        name = answer.get("name")
        if isinstance(name, str):
            ground_truth.add(name)

    if not prediction_set:
        return {
            "f1_score": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "exact_match": 0.0,
        }

    predictions_lower = {pred.lower() for pred in prediction_set}
    ground_truth_lower = {gt.lower() for gt in ground_truth}

    intersection = predictions_lower.intersection(ground_truth_lower)
    precision = len(intersection) / len(predictions_lower)
    recall = len(intersection) / len(ground_truth_lower) if ground_truth_lower else 0.0
    f1_value = (
        2 * (precision * recall) / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "f1_score": f1_value,
        "precision": precision,
        "recall": recall,
        "exact_match": 1.0 if predictions_lower == ground_truth_lower else 0.0,
    }


def load_benchmark(benchmark_path):
    with open(benchmark_path, "r", encoding="utf-8") as handle:
        benchmark = json.load(handle)

    benchmark_by_qid = {}
    for item in benchmark:
        qid = build_qid(item)
        benchmark_by_qid[qid] = item

    return benchmark_by_qid


def load_results(results_path):
    grouped_results = defaultdict(list)

    with open(results_path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue

            result = json.loads(line)
            qid = result.get("qid")
            if not qid:
                raise ValueError(f"Missing qid at line {line_number}")

            grouped_results[qid].append(result)

    return grouped_results


def summarize_scores(completed_task_scores_by_qid, total_result_runs, total_task_count):
    metric_names = ["f1_score", "precision", "recall", "exact_match"]
    summary = {
        "sample_num": total_result_runs,
        "task_num": total_task_count,
        "completed_task_num": len(completed_task_scores_by_qid),
        "score_denominator": "completed_task_num",
        "score": {"avg": {}, "max": {}},
    }

    for metric in metric_names:
        summary["score"]["avg"][metric] = 0.0
        summary["score"]["max"][metric] = 0.0

    if not completed_task_scores_by_qid:
        return summary

    max_runs = max(len(task_scores) for task_scores in completed_task_scores_by_qid.values())

    for k in range(1, max_runs + 1):
        summary["score"][f"best@{k}"] = {metric: 0.0 for metric in metric_names}

    for task_scores in completed_task_scores_by_qid.values():
        for metric in metric_names:
            values = [score[metric] for score in task_scores]
            summary["score"]["avg"][metric] += sum(values) / len(values)
            summary["score"]["max"][metric] += max(values)

            for k in range(1, max_runs + 1):
                summary["score"][f"best@{k}"][metric] += calculate_true_best_at_k(
                    task_scores, k, metric
                )

    task_count = len(completed_task_scores_by_qid)
    for score_type, metrics in summary["score"].items():
        for metric, value in metrics.items():
            metrics[metric] = value / task_count if task_count else 0.0

    return summary


def evaluate(results_path, benchmark_path):
    benchmark_by_qid = load_benchmark(benchmark_path)
    results_by_qid = load_results(results_path)

    total_result_runs = sum(len(items) for items in results_by_qid.values())
    all_runs = [run for runs in results_by_qid.values() for run in runs]
    missing_qids = sorted(set(benchmark_by_qid) - set(results_by_qid))
    extra_qids = sorted(set(results_by_qid) - set(benchmark_by_qid))
    incomplete_runs = [run for run in all_runs if run.get("completed") is not True]
    non_success_runs = [run for run in all_runs if run.get("status") != "success"]
    incomplete_run_stop_reasons = Counter(
        run.get("stop_reason", "unknown") for run in incomplete_runs
    )
    non_success_run_statuses = Counter(
        run.get("status", "unknown") for run in non_success_runs
    )

    completed_task_scores_by_qid = {}
    task_details = []

    for qid, benchmark_item in benchmark_by_qid.items():
        runs = results_by_qid.get(qid, [])
        ground_truth = benchmark_item["label"]
        ground_truth_names = sorted(
            {
                answer["name"]
                for answer in ground_truth
                if isinstance(answer.get("name"), str)
            }
        )

        run_details = []
        task_scores = []

        if not runs:
            zero_score = {
                "f1_score": 0.0,
                "precision": 0.0,
                "recall": 0.0,
                "exact_match": 0.0,
            }
            task_scores = [zero_score]
        else:
            for run_index, run in enumerate(runs, start=1):
                predictions = extract_finish_predictions(run)
                score = f1_score(predictions, ground_truth)
                task_scores.append(score)
                run_details.append(
                    {
                        "run_index": run_index,
                        "completed": run.get("completed", False),
                        "status": run.get("status", "unknown"),
                        "stop_reason": run.get("stop_reason", "unknown"),
                        "prediction_count": len(
                            {item for item in predictions if isinstance(item, str)}
                        ),
                        "predictions": predictions,
                        "score": score,
                    }
                )
            completed_task_scores_by_qid[qid] = task_scores

        task_details.append(
            {
                "qid": qid,
                "subject_id": benchmark_item["subject_id"],
                "hadm_id": benchmark_item["hadm_id"],
                "prediction_time": benchmark_item["prediction_time"],
                "ground_truth": ground_truth_names,
                "missing_result": not runs,
                "num_runs": len(runs),
                "runs": run_details,
                "aggregated": {
                    "avg": {
                        metric: sum(score[metric] for score in task_scores) / len(task_scores)
                        for metric in task_scores[0]
                    },
                    "max": {
                        metric: max(score[metric] for score in task_scores)
                        for metric in task_scores[0]
                    },
                },
            }
        )

    summary = summarize_scores(
        completed_task_scores_by_qid,
        total_result_runs,
        len(benchmark_by_qid),
    )
    summary["info"] = {
        "results_file": str(results_path),
        "benchmark_file": str(benchmark_path),
        "matched_task_count": len(benchmark_by_qid) - len(missing_qids),
        "completed_task_count": len(completed_task_scores_by_qid),
        "missing_task_count": len(missing_qids),
        "task_coverage": (
            len(completed_task_scores_by_qid) / len(benchmark_by_qid)
            if benchmark_by_qid
            else 0.0
        ),
        "extra_result_count": len(extra_qids),
        "missing_qids": missing_qids,
        "extra_qids": extra_qids,
        "incomplete_run_count": len(incomplete_runs),
        "incomplete_run_rate": (
            len(incomplete_runs) / total_result_runs if total_result_runs else 0.0
        ),
        "incomplete_run_stop_reasons": dict(incomplete_run_stop_reasons),
        "non_success_run_count": len(non_success_runs),
        "non_success_run_rate": (
            len(non_success_runs) / total_result_runs if total_result_runs else 0.0
        ),
        "non_success_run_statuses": dict(non_success_run_statuses),
        "avg_runs_per_task": (
            total_result_runs / len(benchmark_by_qid) if benchmark_by_qid else 0.0
        ),
        "avg_runs_per_completed_task": (
            total_result_runs / len(completed_task_scores_by_qid)
            if completed_task_scores_by_qid
            else 0.0
        ),
        "max_runs_per_task": max(len(items) for items in results_by_qid.values())
        if results_by_qid
        else 0,
    }

    return summary, task_details


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Score diagnoses_ccs results against EHRAgentBench."
    )
    parser.add_argument("--results", default=DEFAULT_RESULTS, help="Path to results.jsonl")
    parser.add_argument(
        "--benchmark", default=DEFAULT_BENCHMARK, help="Path to benchmark json"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to summary json. Defaults to scores.json next to results file.",
    )
    parser.add_argument(
        "--details-output",
        default=None,
        help="Path to per-task details jsonl. Defaults to task_scores.jsonl next to results file.",
    )
    args = parser.parse_args()

    results_path = Path(args.results)
    benchmark_path = Path(args.benchmark)
    output_path = Path(args.output) if args.output else results_path.with_name("scores.json")
    details_output_path = (
        Path(args.details_output)
        if args.details_output
        else results_path.with_name("task_scores.jsonl")
    )

    summary, task_details = evaluate(results_path, benchmark_path)

    write_json(output_path, summary)
    write_jsonl(details_output_path, task_details)

    avg_score = summary["score"]["avg"]
    print(f"Results file: {results_path}")
    print(f"Benchmark file: {benchmark_path}")
    print(f"Summary written to: {output_path}")
    print(f"Per-task details written to: {details_output_path}")
    print(f"Benchmark tasks: {summary['task_num']}")
    print(f"Completed tasks: {summary['completed_task_num']}")
    print(f"Result runs: {summary['sample_num']}")
    print(f"Missing tasks: {summary['info']['missing_task_count']}")
    print(f"Incomplete runs (completed != True): {summary['info']['incomplete_run_count']}")
    print(f"Non-success runs (status != success): {summary['info']['non_success_run_count']}")
    print(f"Task coverage: {summary['info']['task_coverage']:.6f}")
    print(f"Score denominator: {summary['score_denominator']}")
    print(f"Extra result qids: {summary['info']['extra_result_count']}")
    print(f"avg precision: {avg_score['precision']:.6f}")
    print(f"avg recall: {avg_score['recall']:.6f}")
    print(f"avg f1_score: {avg_score['f1_score']:.6f}")
    print(f"avg exact_match: {avg_score['exact_match']:.6f}")


if __name__ == "__main__":
    main()
