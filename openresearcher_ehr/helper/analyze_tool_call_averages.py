#!/usr/bin/env python3
"""
Compute average tool-call counts per query from a results.jsonl file.

The analysis is printed to stdout and also written to a JSON file.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def normalize_tool_name(name: str) -> str:
    """Normalize tool names so both `ehr_load_ehr` and `ehr.load_ehr` aggregate together."""
    name = (name or "unknown").strip()
    match = re.match(r"^[A-Za-z0-9_.]+", name)
    if match:
        name = match.group(0)

    if name.startswith("ehr_"):
        return "ehr." + name[len("ehr_") :]
    if name.startswith("browser_"):
        return "browser." + name[len("browser_") :]
    return name


def tool_category(tool_name: str) -> str:
    """Group tools by top-level namespace."""
    if tool_name.startswith("ehr."):
        return "ehr"
    if tool_name.startswith("browser."):
        return "browser"
    if "." in tool_name:
        return tool_name.split(".", 1)[0]
    return "other"


def resolve_task_name(record: dict) -> str:
    task_name = record.get("task")
    if isinstance(task_name, str) and task_name:
        return task_name

    qid = record.get("qid")
    if isinstance(qid, str) and qid:
        parts = qid.split("_")
        if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
            return "_".join(parts[:-2]) or "unknown"
        if len(parts) >= 2 and parts[-1].isdigit():
            return "_".join(parts[:-1]) or "unknown"

    return "unknown"


def load_results(results_path: Path, success_only: bool) -> list[dict]:
    queries = []
    with results_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            if success_only and record.get("status") != "success":
                continue
            queries.append(record)
    return queries


def analyze_queries(queries: list[dict]) -> dict:
    total_queries = len(queries)
    total_tool_calls = 0
    exact_tool_totals = Counter()
    category_totals = Counter()
    exact_tool_query_hits = Counter()
    category_query_hits = Counter()

    for record in queries:
        query_tool_counts = Counter()
        query_category_counts = Counter()

        for message in record.get("messages", []):
            if message.get("role") != "assistant":
                continue

            for tool_call in message.get("tool_calls") or []:
                raw_name = tool_call.get("function", {}).get("name", "unknown")
                name = normalize_tool_name(raw_name)
                category = tool_category(name)

                total_tool_calls += 1
                exact_tool_totals[name] += 1
                category_totals[category] += 1
                query_tool_counts[name] += 1
                query_category_counts[category] += 1

        for name in query_tool_counts:
            exact_tool_query_hits[name] += 1
        for category in query_category_counts:
            category_query_hits[category] += 1

    return {
        "total_queries": total_queries,
        "total_tool_calls": total_tool_calls,
        "exact_tool_totals": exact_tool_totals,
        "category_totals": category_totals,
        "exact_tool_query_hits": exact_tool_query_hits,
        "category_query_hits": category_query_hits,
    }


def summarize_stats(stats: dict) -> dict:
    total_queries = stats["total_queries"]
    total_tool_calls = stats["total_tool_calls"]
    browser_tool_calls = stats["category_totals"].get("browser", 0)
    browser_tool_call_ratio = (
        browser_tool_calls / total_tool_calls if total_tool_calls else 0.0
    )

    return {
        "total_queries": total_queries,
        "total_tool_calls": total_tool_calls,
        "avg_total_tool_calls_per_query": (
            total_tool_calls / total_queries if total_queries else 0.0
        ),
        "browser_tool_calls": browser_tool_calls,
        "browser_tool_call_ratio": browser_tool_call_ratio,
        "browser_tool_call_percentage": browser_tool_call_ratio * 100.0,
    }


def print_section(title: str):
    print("\n" + title)
    print("-" * len(title))


def print_average_table(
    totals: Counter,
    query_hits: Counter,
    total_queries: int,
):
    for name, total in totals.most_common():
        avg_per_query = total / total_queries if total_queries else 0.0
        avg_when_used = total / query_hits[name] if query_hits[name] else 0.0
        print(
            f"{name:40s} total={total:6d}  avg/query={avg_per_query:8.3f}  "
            f"avg/used_query={avg_when_used:8.3f}  used_queries={query_hits[name]:6d}"
        )


def make_average_records(
    totals: Counter,
    query_hits: Counter,
    total_queries: int,
) -> list[dict]:
    records = []
    for name, total in totals.most_common():
        used_queries = query_hits[name]
        records.append(
            {
                "name": name,
                "total_calls": total,
                "avg_per_query": total / total_queries if total_queries else 0.0,
                "avg_when_used": total / used_queries if used_queries else 0.0,
                "used_queries": used_queries,
            }
        )
    return records


def write_json(output_path: Path, payload: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def print_stats_block(title: str, stats: dict) -> None:
    total_queries = stats["total_queries"]
    total_tool_calls = stats["total_tool_calls"]
    browser_tool_calls = stats["category_totals"].get("browser", 0)
    browser_tool_call_ratio = (
        browser_tool_calls / total_tool_calls if total_tool_calls else 0.0
    )

    print_section(title)
    print(f"Queries analyzed: {total_queries}")
    print(
        f"Average total tool calls per query: "
        f"{total_tool_calls / total_queries if total_queries else 0.0:.3f}"
    )
    print(
        f"Browser tool call share: {browser_tool_calls}/{total_tool_calls} "
        f"({browser_tool_call_ratio * 100.0:.2f}%)"
    )

    # print_section(f"{title} | Average Calls By Category")
    # print_average_table(
    #     stats["category_totals"],
    #     stats["category_query_hits"],
    #     total_queries,
    # )

    # print_section(f"{title} | Average Calls By Exact Tool")
    # print_average_table(
    #     stats["exact_tool_totals"],
    #     stats["exact_tool_query_hits"],
    #     total_queries,
    # )


def main():
    parser = argparse.ArgumentParser(
        description="Analyze average tool-call counts from a results.jsonl file."
    )
    parser.add_argument(
        "--results_file",
        help="Path to results.jsonl",
        default="/home/efs/zlt/deepresearch/openresearcher_ehr/subsets_500_tongyi_deepresearch_30b_a3b_20260410T012610Z/results.jsonl",
    )
    parser.add_argument(
        "--output_file",
        help="Path to write JSON analysis output (default: results dir/tool.json).",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Include non-success records as well (default: success only).",
    )
    args = parser.parse_args()

    results_path = Path(args.results_file)
    if not results_path.exists():
        raise SystemExit(f"Results file not found: {results_path}")
    output_path = (
        Path(args.output_file)
        if args.output_file
        else results_path.parent / "tool.json"
    )

    queries = load_results(results_path, success_only=not args.all_statuses)
    stats = analyze_queries(queries)
    status_filter = "all_statuses" if args.all_statuses else "success_only"

    queries_by_task = {}
    for record in queries:
        task_name = resolve_task_name(record)
        queries_by_task.setdefault(task_name, []).append(record)

    by_task_stats = {}
    by_task_payload = {}
    for task_name in sorted(queries_by_task):
        task_stats = analyze_queries(queries_by_task[task_name])
        by_task_stats[task_name] = task_stats
        by_task_payload[task_name] = {
            "summary": summarize_stats(task_stats),
            "by_category": make_average_records(
                task_stats["category_totals"],
                task_stats["category_query_hits"],
                task_stats["total_queries"],
            ),
            "by_exact_tool": make_average_records(
                task_stats["exact_tool_totals"],
                task_stats["exact_tool_query_hits"],
                task_stats["total_queries"],
            ),
        }

    output_payload = {
        "summary": {
            "results_file": str(results_path),
            "status_filter": status_filter,
            **summarize_stats(stats),
        },
        "by_category": make_average_records(
            stats["category_totals"],
            stats["category_query_hits"],
            stats["total_queries"],
        ),
        "by_exact_tool": make_average_records(
            stats["exact_tool_totals"],
            stats["exact_tool_query_hits"],
            stats["total_queries"],
        ),
        "by_task": by_task_payload,
    }
    write_json(output_path, output_payload)

    print(f"Results file: {results_path}")
    print(f"Output file: {output_path}")
    print(
        f"Filter: {'all statuses' if args.all_statuses else 'success only'}"
    )
    print_stats_block("Overall", stats)

    for task_name in sorted(by_task_stats):
        print_stats_block(f"Task: {task_name}", by_task_stats[task_name])


if __name__ == "__main__":
    main()
