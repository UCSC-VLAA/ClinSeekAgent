#!/usr/bin/env python3
"""Convert EHR-Bench JSONL files into deploy_agent-compatible JSON arrays.

The output format is intentionally close to
`data/EHRAgentBench/common/subset_500/merged_subsets_500.json` while keeping
the original self-contained timeline text. Each converted record includes:

- `qid`: stable unique identifier
- `prediction_time`: latest event timestamp in `input` plus one second
- `task`: original task name from `task_info.task`
- `label`: original ground-truth label
- `question`: prompt text that embeds the full patient timeline

The original fields are preserved so downstream evaluation can still access the
source record and metadata.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


TIMESTAMP_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            if not isinstance(record, dict):
                raise ValueError(
                    f"{path}:{line_number} is {type(record).__name__}, expected JSON object"
                )
            records.append(record)
    return records


def extract_event_timestamps(text: str) -> list[datetime]:
    return [datetime.strptime(match, TS_FORMAT) for match in TIMESTAMP_RE.findall(text or "")]


def compute_prediction_time(text: str) -> tuple[str | None, str | None]:
    timestamps = extract_event_timestamps(text)
    if not timestamps:
        return None, None
    latest = max(timestamps)
    prediction_time = latest + timedelta(seconds=1)
    return latest.strftime(TS_FORMAT), prediction_time.strftime(TS_FORMAT)


def resolve_label(record: dict[str, Any]) -> Any:
    task_info = record.get("task_info") or {}
    if "label" in task_info:
        return task_info["label"]
    if "output" in record:
        return record["output"]
    return None


def render_candidates(candidates: Any) -> str | None:
    if not candidates:
        return None
    if not isinstance(candidates, list):
        return json.dumps(candidates, ensure_ascii=False)
    return json.dumps(candidates, ensure_ascii=False)


def answer_format_instruction(label: Any, candidates: Any) -> str:
    if isinstance(label, list):
        base = (
            "Return only the final answer text. "
            "If multiple answers are needed, put one answer per line with no bullets or numbering."
        )
    else:
        base = "Return only the final answer text as a single line."

    if candidates:
        return f"{base} If candidate answers are provided, choose only from that candidate list."
    return base


def build_question(
    *,
    instruction: str,
    input_text: str,
    prediction_time: str | None,
    latest_event_time: str | None,
    label: Any,
    candidates: Any,
) -> str:
    question_parts = [
        "<task_instruction>",
        instruction.strip()
        or "Complete the clinical prediction task using the patient timeline below.",
        "All patient events needed for this task are already included below.",
    ]

    if latest_event_time and prediction_time:
        question_parts.append(
            f"The current time is {prediction_time}, which is exactly one second after the latest observed event at {latest_event_time}."
        )
    elif prediction_time:
        question_parts.append(f"The current time is {prediction_time}.")
    else:
        question_parts.append("No explicit event timestamp was available in the source record.")

    question_parts.extend(
        [
            "Use only the provided patient timeline.",
            "Do not assume any unseen future events beyond the current time.",
            "No EHR database lookup is needed for this record because the full timeline is embedded in the prompt.",
            answer_format_instruction(label, candidates),
            "</task_instruction>",
            "",
            "<patient_info>",
            f"Current Time: {prediction_time or 'UNKNOWN'}",
            "</patient_info>",
            "",
            "<patient_timeline>",
            (input_text or "").strip(),
            "</patient_timeline>",
        ]
    )

    rendered_candidates = render_candidates(candidates)
    if rendered_candidates is not None:
        question_parts.extend(
            [
                "",
                "<candidate_answers>",
                rendered_candidates,
                "</candidate_answers>",
            ]
        )

    return "\n".join(question_parts).strip()


def normalize_task_info(
    task_info: dict[str, Any],
    *,
    source_idx: Any,
    source_file: str,
    latest_event_time: str | None,
    prediction_time: str | None,
) -> dict[str, Any]:
    normalized = dict(task_info)
    normalized["source_idx"] = source_idx
    normalized["source_file"] = source_file
    normalized["latest_event_time"] = latest_event_time
    normalized["prediction_time"] = prediction_time
    return normalized


def convert_record(record: dict[str, Any], source_path: Path) -> dict[str, Any]:
    source_idx = record.get("idx")
    latest_event_time, prediction_time = compute_prediction_time(record.get("input", ""))
    label = resolve_label(record)
    task_info = record.get("task_info") or {}
    task = task_info.get("task") or source_path.stem
    qid = f"{source_path.stem}_{source_idx}"

    return {
        "qid": qid,
        "subject_id": None,
        "hadm_id": None,
        "prediction_time": prediction_time,
        "latest_event_time": latest_event_time,
        "task": task,
        "task_type": task_info.get("task_type"),
        "label": label,
        "question": build_question(
            instruction=record.get("instruction", ""),
            input_text=record.get("input", ""),
            prediction_time=prediction_time,
            latest_event_time=latest_event_time,
            label=label,
            candidates=record.get("candidates"),
        ),
        "source_idx": source_idx,
        "source_file": source_path.name,
        "instruction": record.get("instruction"),
        "input": record.get("input"),
        "output": record.get("output"),
        "candidates": record.get("candidates"),
        "task_info": normalize_task_info(
            task_info,
            source_idx=source_idx,
            source_file=source_path.name,
            latest_event_time=latest_event_time,
            prediction_time=prediction_time,
        ),
    }


def convert_file(source_path: Path, output_path: Path) -> tuple[int, int]:
    source_records = load_jsonl(source_path)
    converted_records = [convert_record(record, source_path) for record in source_records]
    missing_prediction_time = sum(
        1 for record in converted_records if record.get("prediction_time") is None
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(converted_records, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return len(converted_records), missing_prediction_time


def default_output_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}_subset_format.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert EHR-Bench JSONL files into subset-style JSON files."
    )
    parser.add_argument(
        "sources",
        nargs="*",
        type=Path,
        default=[
            Path("/home/efs/zlt/deepresearch/data/EHR-Bench/ehr_bench_decision_making.jsonl"),
            Path("/home/efs/zlt/deepresearch/data/EHR-Bench/ehr_bench_risk_prediction.jsonl"),
        ],
        help="Source EHR-Bench JSONL files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional output directory. Defaults to writing next to each source file.",
    )
    return parser.parse_args()


def iter_output_pairs(sources: Iterable[Path], output_dir: Path | None) -> Iterable[tuple[Path, Path]]:
    for source_path in sources:
        if output_dir is None:
            output_path = default_output_path(source_path)
        else:
            output_path = output_dir / f"{source_path.stem}_subset_format.json"
        yield source_path, output_path


def main() -> None:
    args = parse_args()

    for source_path, output_path in iter_output_pairs(args.sources, args.output_dir):
        total, missing_prediction_time = convert_file(source_path, output_path)
        print(
            f"Converted {source_path} -> {output_path} | records={total} | missing_prediction_time={missing_prediction_time}"
        )


if __name__ == "__main__":
    main()
