#!/usr/bin/env python3
"""Restore the upstream EHR-Bench `input` and the curated `question` prompt.

The public ClinSeek-Bench-Text manifest (`ClinSeek-Bench_text.json`) does not
redistribute the serialized patient timeline of the source benchmark. Every row
maps 1:1 to a row of the public EHR-Bench release
(https://huggingface.co/datasets/BlueZeros/EHR-Bench) through
`source_file` + `source_idx`, so the two fields can be restored locally:

* `input`    -- the upstream serialized timeline, copied verbatim.
* `question` -- the Curated Input prompt of the source benchmark: the upstream
                task instruction, the stated current time, the upstream
                timeline, and the candidate list, in the fixed template below.

Only the Curated Input / Full-Record corpus builder (`scripts/clean_data/`)
and the legacy `--data` path of `run_text_curated.py` need these fields. The
agentic runner (`run_text.py`) builds its prompt from `instruction` and the
row metadata and never reads them.

    huggingface-cli download BlueZeros/EHR-Bench --repo-type dataset \
        --local-dir /path/to/EHR-Bench
    python scripts/data_build/restore_ehr_bench_inputs.py \
        --manifest /path/to/ClinSeek-Bench/ClinSeek-Bench_text.json \
        --ehr-bench-dir /path/to/EHR-Bench \
        --output /path/to/ClinSeek-Bench_text_with_inputs.json

Rows whose upstream label disagrees with the manifest label are reported and
make the script exit non-zero, so a wrong upstream revision cannot go unnoticed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

QUESTION_TEMPLATE = (
    "<task_instruction>\n"
    "{instruction}\n"
    "All patient events needed for this task are already included below.\n"
    "The current time is {prediction_time}, which is exactly one second after "
    "the latest observed event at {latest_event_time}.\n"
    "Use only the provided patient timeline.\n"
    "Do not assume any unseen future events beyond the current time.\n"
    "No EHR database lookup is needed for this record because the full "
    "timeline is embedded in the prompt.\n"
    "Return only the final answer text{answer_shape} If candidate answers are "
    "provided, choose only from that candidate list.\n"
    "</task_instruction>\n"
    "\n"
    "<patient_info>\n"
    "Current Time: {prediction_time}\n"
    "</patient_info>\n"
    "\n"
    "<patient_timeline>\n"
    "{input}\n"
    "</patient_timeline>\n"
    "\n"
    "<candidate_answers>\n"
    "{candidates}\n"
    "</candidate_answers>"
)


def load_manifest(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def index_upstream(ehr_bench_dir: Path, wanted: dict[str, set[int]]) -> dict[tuple[str, int], dict]:
    """`(source_file, idx) -> upstream row`, reading only the rows we need."""
    rows: dict[tuple[str, int], dict] = {}
    for source_file, indices in wanted.items():
        path = ehr_bench_dir / source_file
        if not path.exists():
            sys.exit(f"ERROR: {path} not found; download BlueZeros/EHR-Bench "
                     f"into --ehr-bench-dir first")
        with path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh):
                if not line.strip():
                    continue
                row = json.loads(line)
                idx = int(row.get("idx", line_no))
                if idx in indices:
                    rows[(source_file, idx)] = row
    return rows


SINGLE_ANSWER_METRICS = {"aucroc", "acc"}


def answer_shape(row: dict[str, Any], upstream: dict[str, Any]) -> str:
    """Single-answer tasks (risk prediction, next event) ask for one line."""
    info = row.get("task_info") or upstream.get("task_info") or {}
    if str(info.get("metric") or "").lower() in SINGLE_ANSWER_METRICS:
        return " as a single line."
    return (". If multiple answers are needed, put one answer per line with no "
            "bullets or numbering.")


def build_question(row: dict[str, Any], upstream: dict[str, Any]) -> str:
    candidates = row.get("candidates")
    if candidates is None:
        candidates = upstream.get("candidates") or []
    return QUESTION_TEMPLATE.format(
        instruction=str(upstream["instruction"]).strip(),
        prediction_time=row["prediction_time"],
        latest_event_time=row["latest_event_time"],
        answer_shape=answer_shape(row, upstream),
        input=str(upstream["input"]).strip(),
        candidates=json.dumps(candidates, ensure_ascii=False),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, type=Path,
                    help="ClinSeek-Bench_text.json (JSON list or JSONL)")
    ap.add_argument("--ehr-bench-dir", required=True, type=Path,
                    help="directory holding ehr_bench_risk_prediction.jsonl and "
                         "ehr_bench_decision_making.jsonl from BlueZeros/EHR-Bench")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--fields", nargs="+", default=["input", "question"],
                    choices=["input", "question"],
                    help="which fields to restore (default: both)")
    ap.add_argument("--verify-existing", action="store_true",
                    help="if the manifest already carries input/question, compare "
                         "the restored values against them instead of trusting them")
    args = ap.parse_args()

    rows = load_manifest(args.manifest)
    wanted: dict[str, set[int]] = {}
    for row in rows:
        if row.get("source_file") is None or row.get("source_idx") is None:
            sys.exit(f"ERROR: row {row.get('qid')!r} has no source_file/source_idx")
        wanted.setdefault(str(row["source_file"]), set()).add(int(row["source_idx"]))
    upstream = index_upstream(args.ehr_bench_dir, wanted)

    restored, missing, label_mismatch, verify_mismatch = [], [], [], []
    for row in rows:
        key = (str(row["source_file"]), int(row["source_idx"]))
        src = upstream.get(key)
        if src is None:
            missing.append(row.get("qid"))
            continue
        up_label = (src.get("task_info") or {}).get("label", src.get("output"))
        if row.get("label") is not None and str(up_label) != str(row.get("label")):
            label_mismatch.append(row.get("qid"))
        out = dict(row)
        new_values = {"input": src["input"], "question": build_question(row, src)}
        for field in args.fields:
            if args.verify_existing and field in row and row[field] != new_values[field]:
                verify_mismatch.append((row.get("qid"), field))
            out[field] = new_values[field]
        restored.append(out)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        if args.output.suffix == ".jsonl":
            for row in restored:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            json.dump(restored, fh, ensure_ascii=False, indent=2)

    print(f"rows={len(rows)} restored={len(restored)} missing_upstream={len(missing)} "
          f"label_mismatch={len(label_mismatch)}"
          + (f" verify_mismatch={len(verify_mismatch)}" if args.verify_existing else ""))
    for name, items in (("missing upstream rows", missing),
                        ("label mismatches", label_mismatch),
                        ("restored values that differ from the manifest", verify_mismatch)):
        if items:
            print(f"  {name}: {items[:10]}{' ...' if len(items) > 10 else ''}")
    return 1 if (missing or label_mismatch or verify_mismatch) else 0


if __name__ == "__main__":
    raise SystemExit(main())
