#!/usr/bin/env python3
import argparse
import ast
import json
import math
import re
from collections import Counter, defaultdict, deque
from pathlib import Path


DEFAULT_RESULTS = (
    "./openresearcher_ehr/results/train_trajectory_3k_4ep_20260415T002208Z/results.jsonl"
)
DEFAULT_BENCHMARK = (
    "./data/AgentEHR-Bench/MIMICIVAgentBench/train/mix_training_3k.json"
)


def build_qid(record):
    task = record.get("task")
    subject_id = record.get("subject_id")
    hadm_id = record.get("hadm_id")

    if task is None or subject_id is None:
        raise ValueError(f"Unable to build qid from record: {record}")

    if hadm_id is not None:
        return f"{task}_{subject_id}_{hadm_id}"
    return f"{task}_{subject_id}"


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


def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def dedupe_string_predictions(values):
    deduped = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = re.sub(r"\s+", " ", value).strip().strip('"').strip("'")
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def extract_candidate_names_from_tool_output(text):
    names = []
    single_column_mode = False

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            single_column_mode = False
            continue

        if stripped.startswith("--- Results for keyword "):
            single_column_mode = False
            continue

        if stripped.startswith("Error:") or stripped.startswith("No records found"):
            single_column_mode = False
            continue

        if lower == "candidate":
            single_column_mode = True
            continue

        if "icd_code" in lower and "candidate" in lower:
            single_column_mode = False
            continue

        if single_column_mode:
            if not any(
                token in lower
                for token in ("similarity_score", "columns:", "table:", "description:")
            ):
                names.append(stripped)
            continue

        match = re.match(
            r"^\s*(\S+)\s+(\d{1,2})\s+(.+?)\s+(\d+(?:\.\d+)?)\s*$",
            line,
        )
        if match:
            names.append(match.group(3).strip())
            continue

        match = re.match(r"^\s*(\S+)\s+(\d{1,2})\s+(.+?)\s*$", line)
        if match:
            names.append(match.group(3).strip())
            continue

    return dedupe_string_predictions(names)


def collect_official_candidate_names(messages):
    pending_tool_calls = deque()
    official_names = []
    seen = set()

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            pending_tool_calls.extend(message.get("tool_calls") or [])
            continue

        if role != "tool":
            continue

        associated_tool_call = pending_tool_calls.popleft() if pending_tool_calls else None
        tool_name = message.get("name", "")
        tool_args = ""
        if associated_tool_call:
            function = associated_tool_call.get("function", {})
            tool_name = tool_name or function.get("name", "")
            tool_args = function.get("arguments", "")

        if not isinstance(tool_args, str):
            tool_args = json.dumps(tool_args, ensure_ascii=False)

        if "_candidates" not in tool_args:
            continue

        if "get_candidates" not in tool_name and "run_sql_query" not in tool_name:
            continue

        for candidate in extract_candidate_names_from_tool_output(message.get("content", "")):
            key = normalize_text(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            official_names.append(candidate)

    return official_names


def extract_explicit_string_list_from_text(text):
    snippets = []

    answer_blocks = list(
        re.finditer(r"<answer>\s*(.*?)\s*</answer>", text or "", flags=re.IGNORECASE | re.DOTALL)
    )
    for match in reversed(answer_blocks):
        snippets.append(match.group(1).strip())

    list_literals = list(re.finditer(r"\[[\s\S]{1,2000}?\]", text or "", flags=re.DOTALL))
    for match in reversed(list_literals):
        snippet = match.group(0)
        if '"' not in snippet and "'" not in snippet:
            continue
        snippets.append(snippet)

    for snippet in snippets:
        try:
            parsed = parse_json_or_python(snippet)
        except Exception:
            continue

        if isinstance(parsed, dict):
            for key in ("response", "answer", "answers", "predictions"):
                if isinstance(parsed.get(key), list):
                    parsed = parsed[key]
                    break

        if isinstance(parsed, list):
            cleaned = dedupe_string_predictions(parsed)
            if cleaned:
                return cleaned

    return []


def extract_bullet_list_from_text(text):
    if not text:
        return []

    cue_re = re.compile(
        r"(final answer|answer should|plausible diagnoses include|list of strings|list like)",
        flags=re.IGNORECASE,
    )
    bullet_re = re.compile(r"^[-*]\s+(.+?)\s*$")

    lines = text.splitlines()
    collecting = False
    bullets = []
    best = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            if collecting and bullets:
                best = bullets
                break
            continue

        if cue_re.search(stripped):
            collecting = True
            bullets = []
            continue

        match = bullet_re.match(stripped)
        if collecting and match:
            bullets.append(match.group(1).strip())
            continue

        if collecting and bullets:
            best = bullets
            break

    if not best and bullets:
        best = bullets

    return dedupe_string_predictions(best)


def align_predictions_to_official_candidates(predictions, official_candidates):
    if not predictions:
        return []

    official_by_key = {
        normalize_text(candidate): candidate
        for candidate in official_candidates
        if normalize_text(candidate)
    }
    aligned = []
    seen = set()

    for prediction in predictions:
        pred_key = normalize_text(prediction)
        if not pred_key:
            continue

        canonical = official_by_key.get(pred_key)
        if canonical is None:
            for official in official_candidates:
                official_key = normalize_text(official)
                if not official_key:
                    continue
                if official_key in pred_key or pred_key in official_key:
                    canonical = official
                    break

        final_value = canonical or prediction
        final_key = normalize_text(final_value)
        if final_key in seen:
            continue
        seen.add(final_key)
        aligned.append(final_value)

    return aligned


def extract_official_candidate_mentions(text, official_candidates):
    normalized = normalize_text(text)
    if not normalized or not official_candidates:
        return []

    matches = []
    for candidate in official_candidates:
        candidate_key = normalize_text(candidate)
        if not candidate_key:
            continue
        position = normalized.find(candidate_key)
        if position == -1:
            continue
        matches.append((position, candidate))

    matches.sort(key=lambda item: item[0])
    return dedupe_string_predictions([candidate for _, candidate in matches])


def extract_fallback_predictions_from_last_assistant(messages):
    last_assistant_message = None
    for message in reversed(messages):
        if message.get("role") == "assistant":
            last_assistant_message = message
            break

    if last_assistant_message is None:
        return [], None

    text = "\n".join(
        part
        for part in (
            (last_assistant_message.get("content") or "").strip(),
            (last_assistant_message.get("reasoning_content") or "").strip(),
        )
        if part
    ).strip()
    if not text:
        return [], None

    explicit_predictions = extract_explicit_string_list_from_text(text)
    official_candidates = collect_official_candidate_names(messages)
    if explicit_predictions:
        aligned = align_predictions_to_official_candidates(
            explicit_predictions,
            official_candidates,
        )
        return aligned or explicit_predictions, "fallback_explicit_string_list"

    bullet_predictions = extract_bullet_list_from_text(text)
    if bullet_predictions:
        aligned = align_predictions_to_official_candidates(
            bullet_predictions,
            official_candidates,
        )
        if aligned:
            return aligned, "fallback_bullet_list"

    answer_like = any(
        cue in normalize_text(text)
        for cue in (
            "ehr.finish",
            "final answer",
            "thus, the answer",
            "list of strings",
            "plausible diagnoses",
            "use ehr.finish",
        )
    )
    if not answer_like:
        return [], None

    mentioned_candidates = extract_official_candidate_mentions(text, official_candidates)
    if mentioned_candidates:
        return mentioned_candidates, "fallback_official_candidate_mentions"

    return [], None


def extract_finish_predictions_with_source(
    result,
    *,
    allow_text_answer_extraction_without_finish=False,
):
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
                return [], "finish_parse_error"

            if isinstance(arguments, dict):
                predictions = arguments.get("response", [])
            else:
                predictions = arguments

            if isinstance(predictions, list):
                return predictions, "finish_tool_call"
            return [], "finish_tool_call_non_list"

    if not allow_text_answer_extraction_without_finish:
        return [], "no_prediction"

    predictions, source = extract_fallback_predictions_from_last_assistant(messages)
    return predictions, source or "no_prediction"


def extract_finish_predictions(
    result,
    *,
    allow_text_answer_extraction_without_finish=False,
):
    predictions, _ = extract_finish_predictions_with_source(
        result,
        allow_text_answer_extraction_without_finish=(
            allow_text_answer_extraction_without_finish
        ),
    )
    return predictions


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
        atc_name = answer.get("atc_name")
        if isinstance(atc_name, str):
            ground_truth.add(atc_name)

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


def resolve_task_name(runs, fallback_task="unknown"):
    for run in runs:
        task_name = run.get("task")
        if isinstance(task_name, str) and task_name:
            return task_name

    if isinstance(fallback_task, str) and fallback_task:
        return fallback_task
    return "unknown"


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


def evaluate(
    results_path,
    benchmark_path,
    *,
    allow_text_answer_extraction_without_finish=False,
):
    benchmark_by_qid = load_benchmark(benchmark_path)
    results_by_qid = load_results(results_path)

    total_result_runs = sum(len(items) for items in results_by_qid.values())
    all_runs = [run for runs in results_by_qid.values() for run in runs]
    result_runs_by_task = defaultdict(list)
    for run in all_runs:
        result_runs_by_task[resolve_task_name([run])].append(run)

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
    completed_task_scores_by_task = defaultdict(dict)
    benchmark_qids_by_task = defaultdict(list)
    task_details = []
    prediction_source_counts = Counter()

    for qid, benchmark_item in benchmark_by_qid.items():
        runs = results_by_qid.get(qid, [])
        task_name = resolve_task_name(runs, benchmark_item.get("task"))
        benchmark_qids_by_task[task_name].append(qid)
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
                predictions, prediction_source = extract_finish_predictions_with_source(
                    run,
                    allow_text_answer_extraction_without_finish=(
                        allow_text_answer_extraction_without_finish
                    ),
                )
                prediction_source_counts[prediction_source] += 1
                score = f1_score(predictions, ground_truth)
                task_scores.append(score)
                run_details.append(
                    {
                        "run_index": run_index,
                        "completed": run.get("completed", False),
                        "status": run.get("status", "unknown"),
                        "stop_reason": run.get("stop_reason", "unknown"),
                        "prediction_source": prediction_source,
                        "prediction_count": len(
                            {item for item in predictions if isinstance(item, str)}
                        ),
                        "predictions": predictions,
                        "score": score,
                    }
                )
            completed_task_scores_by_qid[qid] = task_scores
            completed_task_scores_by_task[task_name][qid] = task_scores

        task_details.append(
            {
                "qid": qid,
                "task": task_name,
                "subject_id": benchmark_item.get("subject_id"),
                "hadm_id": benchmark_item.get("hadm_id"),
                "prediction_time": benchmark_item.get("prediction_time"),
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

    extra_qids_by_task = defaultdict(list)
    for qid in extra_qids:
        extra_qids_by_task[resolve_task_name(results_by_qid.get(qid, []))].append(qid)

    summary = summarize_scores(
        completed_task_scores_by_qid,
        total_result_runs,
        len(benchmark_by_qid),
    )
    summary["info"] = {
        "results_file": str(results_path),
        "benchmark_file": str(benchmark_path),
        "allow_text_answer_extraction_without_finish": (
            allow_text_answer_extraction_without_finish
        ),
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
        "prediction_source_counts": dict(prediction_source_counts),
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
    summary["by_task"] = {}

    task_names = sorted(
        set(benchmark_qids_by_task)
        | set(result_runs_by_task)
        | set(extra_qids_by_task)
    )

    for task_name in task_names:
        task_result_runs = result_runs_by_task.get(task_name, [])
        task_completed_scores_by_qid = completed_task_scores_by_task.get(task_name, {})
        task_benchmark_qids = benchmark_qids_by_task.get(task_name, [])
        task_missing_qids = [
            qid for qid in task_benchmark_qids if qid not in task_completed_scores_by_qid
        ]
        task_incomplete_runs = [
            run for run in task_result_runs if run.get("completed") is not True
        ]
        task_non_success_runs = [
            run for run in task_result_runs if run.get("status") != "success"
        ]

        task_summary = summarize_scores(
            task_completed_scores_by_qid,
            len(task_result_runs),
            len(task_benchmark_qids),
        )
        task_summary["info"] = {
            "matched_task_count": len(task_benchmark_qids) - len(task_missing_qids),
            "completed_task_count": len(task_completed_scores_by_qid),
            "missing_task_count": len(task_missing_qids),
            "task_coverage": (
                len(task_completed_scores_by_qid) / len(task_benchmark_qids)
                if task_benchmark_qids
                else 0.0
            ),
            "extra_result_count": len(extra_qids_by_task.get(task_name, [])),
            "missing_qids": task_missing_qids,
            "extra_qids": extra_qids_by_task.get(task_name, []),
            "incomplete_run_count": len(task_incomplete_runs),
            "incomplete_run_rate": (
                len(task_incomplete_runs) / len(task_result_runs)
                if task_result_runs
                else 0.0
            ),
            "incomplete_run_stop_reasons": dict(
                Counter(run.get("stop_reason", "unknown") for run in task_incomplete_runs)
            ),
            "non_success_run_count": len(task_non_success_runs),
            "non_success_run_rate": (
                len(task_non_success_runs) / len(task_result_runs)
                if task_result_runs
                else 0.0
            ),
            "non_success_run_statuses": dict(
                Counter(run.get("status", "unknown") for run in task_non_success_runs)
            ),
            "avg_runs_per_task": (
                len(task_result_runs) / len(task_benchmark_qids)
                if task_benchmark_qids
                else 0.0
            ),
            "avg_runs_per_completed_task": (
                len(task_result_runs) / len(task_completed_scores_by_qid)
                if task_completed_scores_by_qid
                else 0.0
            ),
            "max_runs_per_task": max(len(items) for qid, items in results_by_qid.items() if qid in task_completed_scores_by_qid)
            if task_completed_scores_by_qid
            else 0,
        }
        summary["by_task"][task_name] = task_summary

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
        description="Score EHRAgentBench results and emit per-task summaries."
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
    parser.add_argument(
        "--extract-text-answer-without-finish",
        action="store_true",
        help=(
            "When ehr.finish is missing, extract predictions from the last assistant "
            "text response. Disabled by default."
        ),
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

    summary, task_details = evaluate(
        results_path,
        benchmark_path,
        allow_text_answer_extraction_without_finish=(
            args.extract_text_answer_without_finish
        ),
    )

    write_json(output_path, summary)
    write_jsonl(details_output_path, task_details)

    avg_score = summary["score"]["avg"]
    # print(f"Results file: {results_path}")
    # print(f"Benchmark file: {benchmark_path}")
    # print(f"Summary written to: {output_path}")
    # print(f"Per-task details written to: {details_output_path}")
    # print(f"Benchmark tasks: {summary['task_num']}")
    # print(f"Completed tasks: {summary['completed_task_num']}")
    # print(f"Result runs: {summary['sample_num']}")
    # print(f"Missing tasks: {summary['info']['missing_task_count']}")
    # print(f"Incomplete runs (completed != True): {summary['info']['incomplete_run_count']}")
    # print(f"Non-success runs (status != success): {summary['info']['non_success_run_count']}")
    # print(f"Task coverage: {summary['info']['task_coverage']:.6f}")
    # print(f"Score denominator: {summary['score_denominator']}")
    # print(f"Extra result qids: {summary['info']['extra_result_count']}")
    # print(f"avg precision: {avg_score['precision']:.6f}")
    # print(f"avg recall: {avg_score['recall']:.6f}")
    # print(f"avg f1_score: {avg_score['f1_score']:.6f}")
    # print(f"avg exact_match: {avg_score['exact_match']:.6f}")

    for task_name, task_summary in summary.get("by_task", {}).items():
        task_avg_score = task_summary["score"]["avg"]
        print(f"[{task_name}] # Total / # Completed: {task_summary['task_num']} /  {task_summary['completed_task_num']}")
        print(f"[{task_name}] precision / recall / f1: {task_avg_score['precision']:.4f} / {task_avg_score['recall']:.4f} / {task_avg_score['f1_score']:.4f}")


if __name__ == "__main__":
    main()
