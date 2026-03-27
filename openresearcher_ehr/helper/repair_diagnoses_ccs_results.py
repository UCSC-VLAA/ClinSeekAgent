#!/usr/bin/env python3
"""
Conservatively repair incomplete diagnoses_ccs results by appending a synthetic
ehr.finish call when the final assistant text mentions official CCS candidates
that were already surfaced earlier from diagnoses_ccs_candidates tool outputs.
"""

import argparse
import copy
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path


DEFAULT_RESULTS = (
    "/home/efs/zlt/deepresearch/openresearcher_ehr/"
    "diagnoses_ccs_500_results_qwen3_5_35b_a3b_deepmed_sft_epoch2_vllm_fixed_20260326T230132Z/"
    "results.jsonl"
)


def load_deploy_agent_module():
    module_path = (
        "/home/efs/zlt/deepresearch/openresearcher_ehr/deploy_agent.py"
    )
    spec = importlib.util.spec_from_file_location("deploy_agent", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def extract_candidate_names_from_tool_output(text: str) -> list[str]:
    names: list[str] = []
    single_column_mode = False

    for raw_line in text.splitlines():
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

        # Matches rows like:
        #   1740           9               Cancer of breast
        #   1740           9               Cancer of breast   98
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

    deduped: list[str] = []
    seen = set()
    for name in names:
        key = normalize_text(name)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)

    return deduped


def collect_official_candidate_names(messages: list[dict]) -> list[str]:
    tool_call_name_by_id: dict[str, str] = {}
    tool_call_args_by_id: dict[str, str] = {}
    official_names: list[str] = []
    seen = set()

    for message in messages:
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                tool_call_name_by_id[tool_call.get("id")] = (
                    tool_call.get("function", {}).get("name", "")
                )
                tool_call_args_by_id[tool_call.get("id")] = (
                    tool_call.get("function", {}).get("arguments", "")
                )
            continue

        if message.get("role") != "tool":
            continue

        tool_call_id = message.get("tool_call_id")
        tool_name = tool_call_name_by_id.get(tool_call_id, "")
        tool_args = tool_call_args_by_id.get(tool_call_id, "")
        if not isinstance(tool_args, str):
            tool_args = json.dumps(tool_args, ensure_ascii=False)

        if "diagnoses_ccs_candidates" not in tool_args:
            continue

        if "get_candidates" not in tool_name and "run_sql_query" not in tool_name:
            continue

        for candidate in extract_candidate_names_from_tool_output(
            message.get("content", "")
        ):
            key = normalize_text(candidate)
            if key in seen:
                continue
            seen.add(key)
            official_names.append(candidate)

    return official_names


def find_repair_predictions(
    messages: list[dict],
    official_candidates: list[str],
) -> tuple[int | None, list[str]]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue

        text = normalize_text(
            (message.get("content") or "") + "\n" + (message.get("reasoning_content") or "")
        )
        if not text:
            continue

        matched = [
            candidate
            for candidate in official_candidates
            if normalize_text(candidate) in text
        ]
        matched = list(dict.fromkeys(matched))
        if matched:
            return index, matched

    return None, []


def repair_record(record: dict) -> tuple[dict, bool, str]:
    if record.get("status") != "incomplete" or record.get("completed") is True:
        return record, False, "not_incomplete"

    messages = record.get("messages") or []
    if not messages:
        return record, False, "no_messages"

    official_candidates = collect_official_candidate_names(messages)
    if not official_candidates:
        return record, False, "no_official_candidates_found"

    matched_index, matched_predictions = find_repair_predictions(
        messages,
        official_candidates,
    )
    if not matched_predictions:
        return record, False, "no_candidate_mentions_in_assistant_text"

    repaired = copy.deepcopy(record)
    synthetic_tool_call_id = f"repair_synthetic_finish_{record.get('qid', 'unknown')}"
    repaired["messages"].append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": synthetic_tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "ehr.finish",
                        "arguments": json.dumps(
                            {"response": matched_predictions},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
            "repair_synthetic": True,
        }
    )
    repaired["messages"].append(
        {
            "role": "tool",
            "tool_call_id": synthetic_tool_call_id,
            "content": "Finish (repaired from parseable historical output)",
            "repair_synthetic": True,
        }
    )

    repaired["completed"] = True
    repaired["status"] = "success"
    repaired["stop_reason"] = "repair_finish_tool_call"
    repaired["repair_info"] = {
        "repaired": True,
        "repair_strategy": (
            "synthetic_finish_from_official_candidate_mentions_in_assistant_text"
        ),
        "original_completed": record.get("completed"),
        "original_status": record.get("status"),
        "original_stop_reason": record.get("stop_reason"),
        "matched_assistant_message_index": matched_index,
        "matched_predictions": matched_predictions,
        "official_candidate_pool_size": len(official_candidates),
    }

    return repaired, True, "repaired"


def summarize_records(records: list[dict]) -> dict:
    status_counts = Counter(record.get("status", "unknown") for record in records)
    completed_counts = Counter(record.get("completed", False) for record in records)
    stop_reason_counts = Counter(
        record.get("stop_reason", "unknown") for record in records
    )
    repaired_count = sum(
        1 for record in records if record.get("repair_info", {}).get("repaired")
    )
    return {
        "status_counts": dict(status_counts),
        "completed_counts": dict(completed_counts),
        "stop_reason_counts": dict(stop_reason_counts),
        "repaired_count": repaired_count,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Conservatively repair parseable incomplete diagnoses_ccs results."
    )
    parser.add_argument(
        "--results",
        default=DEFAULT_RESULTS,
        help="Path to results.jsonl",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to repaired results.jsonl. Defaults to results.repaired.jsonl next to input.",
    )
    parser.add_argument(
        "--summary-output",
        default=None,
        help="Path to repair summary json. Defaults to results.repaired.summary.json next to input.",
    )
    args = parser.parse_args()

    results_path = Path(args.results)
    if not results_path.exists():
        raise SystemExit(f"Results file not found: {results_path}")

    output_path = (
        Path(args.output)
        if args.output
        else results_path.with_name("results.repaired.jsonl")
    )
    summary_output_path = (
        Path(args.summary_output)
        if args.summary_output
        else results_path.with_name("results.repaired.summary.json")
    )

    original_records: list[dict] = []
    with results_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                original_records.append(json.loads(line))

    repaired_records: list[dict] = []
    repair_reason_counts = Counter()
    repaired_qids: list[str] = []

    for record in original_records:
        repaired_record, changed, reason = repair_record(record)
        repaired_records.append(repaired_record)
        repair_reason_counts[reason] += 1
        if changed:
            repaired_qids.append(record.get("qid"))

    before_summary = summarize_records(original_records)
    after_summary = summarize_records(repaired_records)

    with output_path.open("w", encoding="utf-8") as handle:
        for record in repaired_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary_payload = {
        "input_results": str(results_path),
        "output_results": str(output_path),
        "total_records": len(original_records),
        "before": before_summary,
        "after": after_summary,
        "repair_reason_counts": dict(repair_reason_counts),
        "repaired_qids": repaired_qids,
    }

    with summary_output_path.open("w", encoding="utf-8") as handle:
        json.dump(summary_payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"Input results: {results_path}")
    print(f"Output results: {output_path}")
    print(f"Summary output: {summary_output_path}")
    print(f"Total records: {len(original_records)}")
    print(f"Repaired records: {after_summary['repaired_count']}")
    print(f"Before status counts: {before_summary['status_counts']}")
    print(f"After status counts: {after_summary['status_counts']}")
    print(f"Before completed counts: {before_summary['completed_counts']}")
    print(f"After completed counts: {after_summary['completed_counts']}")
    print(f"Repair reason counts: {dict(repair_reason_counts)}")


if __name__ == "__main__":
    main()
