"""
Convert EHR-Ins-Reasoning matched records to the exact EHR-Bench JSON schema
(same layout as data/EHR-Bench/ehr_bench_sampled_40_per_task.json).

Per-field rules:
  - instruction: strip the prefix
        "Given the sequence of events that have occurred in a hospital, "
    and capitalize "please" -> "Please" (only at the start).
  - question: rebuilt with the EHR-Bench `<task_instruction> ... <patient_timeline>`
    template (decision_making / risk_prediction variants).
  - prediction_time / latest_event_time: derived from the max timestamp found
    in `input` (latest_event_time = that timestamp; prediction_time = +1s).
  - task / task_type / label / target / etc.: taken from task_info.
  - task_info: reordered to match EHR-Bench keys; drops Ins-Reasoning extras
    (`idx`, `reasoning`).
  - qid: "ehr_ins_reasoning_<task_type>_<source_idx>" (matches EHR-Bench
    naming pattern `ehr_bench_<task_type>_<source_idx>`).

Usage:
    python ehrins_to_ehrbench.py \
        --src  ../data/EHR-Ins-Reasoning/ehr_ins_reasoning_sample300_per_task_matched.jsonl \
        --dst  ../data/EHR-Ins-Reasoning/ehr_bench_from_ehrins_sample5.json \
        --n 5
"""
import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path


TS_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
INSTR_PREFIX = "Given the sequence of events that have occurred in a hospital, "

DECISION_TAIL = (
    "Return only the final answer text. If multiple answers are needed, "
    "put one answer per line with no bullets or numbering. "
    "If candidate answers are provided, choose only from that candidate list."
)
RISK_TAIL = (
    "Return only the final answer text as a single line. "
    "If candidate answers are provided, choose only from that candidate list."
)


def transform_instruction(instr: str) -> str:
    """Strip the 'Given the sequence ... hospital, ' prefix and uppercase 'please'."""
    if not instr:
        return instr
    stripped = instr.strip()
    if stripped.startswith(INSTR_PREFIX):
        stripped = stripped[len(INSTR_PREFIX):]
    elif stripped.lower().startswith(INSTR_PREFIX.lower()):
        stripped = stripped[len(INSTR_PREFIX):]
    # Only capitalize if it starts with lowercase "please"
    if stripped.startswith("please "):
        stripped = "Please " + stripped[len("please "):]
    return stripped


def build_question(instr: str, prediction_time: str, latest_event_time: str,
                   task_type: str, patient_input: str) -> str:
    tail = RISK_TAIL if task_type == "risk_prediction" else DECISION_TAIL
    original_instr_line = INSTR_PREFIX + instr[0].lower() + instr[1:] if instr else ""
    return (
        "<task_instruction>\n"
        f"{original_instr_line}\n"
        "All patient events needed for this task are already included below.\n"
        f"The current time is {prediction_time}, which is exactly one second after "
        f"the latest observed event at {latest_event_time}.\n"
        "Use only the provided patient timeline.\n"
        "Do not assume any unseen future events beyond the current time.\n"
        "No EHR database lookup is needed for this record because the full "
        "timeline is embedded in the prompt.\n"
        f"{tail}\n"
        "</task_instruction>\n"
        "\n"
        "<patient_info>\n"
        f"Current Time: {prediction_time}\n"
        "</patient_info>\n"
        "\n"
        "<patient_timeline>\n"
        f"{patient_input}\n"
        "</patient_timeline>"
    )


def latest_timestamp(text: str) -> str | None:
    stamps = TS_PATTERN.findall(text or "")
    return max(stamps) if stamps else None


def plus_one_second(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=1)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def convert_record(entry: dict) -> dict | None:
    if entry.get("subject_id") is None:
        return None

    task_info = entry.get("task_info", {}) or {}
    task_type = task_info.get("task_type") or "decision_making"
    task = task_info.get("task") or ""
    source_idx = task_info.get("idx")
    if source_idx is None:
        source_idx = entry.get("idx")

    latest = latest_timestamp(entry.get("input", ""))
    if latest is None:
        return None
    prediction_time = plus_one_second(latest)

    raw_instruction = entry.get("instruction", "") or ""
    instruction = transform_instruction(raw_instruction)

    # Use the ORIGINAL full instruction line (with "Given the sequence ...") in
    # the question template, matching EHR-Bench convention.
    question = build_question(
        instr=instruction,
        prediction_time=prediction_time,
        latest_event_time=latest,
        task_type=task_type,
        patient_input=entry.get("input", ""),
    )

    label = task_info.get("label")
    if label is None:
        output = entry.get("output")
        label = [output] if isinstance(output, str) else (output or [])

    target = task_info.get("target")
    if target is None:
        target = json.dumps(label if isinstance(label, list) else [label], ensure_ascii=False)

    return {
        "qid": f"ehr_ins_reasoning_{task_type}_{source_idx}",
        "subject_id": entry["subject_id"],
        "hadm_id": entry.get("hadm_id"),
        "prediction_time": prediction_time,
        "latest_event_time": latest,
        "task": task,
        "task_type": task_type,
        "label": label,
        "question": question,
        "source_idx": source_idx,
        "source_file": "ehr_ins_reasoning.jsonl",
        "instruction": instruction,
        "input": entry.get("input", ""),
        "output": entry.get("output", ""),
        "candidates": entry.get("candidates", []),
        "task_info": {
            "target_key": task_info.get("target_key"),
            "event": task_info.get("event"),
            "metric": task_info.get("metric"),
            "task_type": task_type,
            "task": task,
            "target": target,
            "label": label,
            "source_idx": source_idx,
            "source_file": "ehr_ins_reasoning.jsonl",
            "latest_event_time": latest,
            "prediction_time": prediction_time,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--n", type=int, default=0, help="Take only first N convertible records (0=all)")
    ap.add_argument("--jsonl", action="store_true", help="Write JSONL instead of JSON array")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    out_records = []
    n_total = 0
    n_skipped = 0
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_total += 1
            rec = convert_record(json.loads(line))
            if rec is None:
                n_skipped += 1
                continue
            out_records.append(rec)
            if args.n and len(out_records) >= args.n:
                break

    if args.jsonl:
        with open(dst, "w") as f:
            for rec in out_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    else:
        with open(dst, "w") as f:
            json.dump(out_records, f, ensure_ascii=False, indent=2)

    print(f"Source:      {src}")
    print(f"Destination: {dst}")
    print(f"Read:        {n_total}")
    print(f"Skipped:     {n_skipped} (no subject_id / no timestamps)")
    print(f"Converted:   {len(out_records)}")


if __name__ == "__main__":
    main()
