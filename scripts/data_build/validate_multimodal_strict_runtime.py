#!/usr/bin/env python3
"""Validate the strict multimodal runtime view for every ClinSeek-Bench-MM row.

Loads each row through `EHRManager.load_ehr_for_sample` with its trusted
metadata (cutoff, linked study ids, scope, task, modalities) and checks the
`multimodal-strict-temporal-scope-v1` contract on what the agent can see:

* no time column holds a value after the prediction cutoff;
* `tb_cxr` contains only the studies linked by the row;
* image-only rows expose no longitudinal EHR tables;
* MedMod `stays` exposes none of the outcome columns (outtime, los, mortality,
  ...).

    python scripts/data_build/validate_multimodal_strict_runtime.py \
        --manifest /path/to/ClinSeek-Bench_multimodal.jsonl \
        --ehrxqa-root /path/to/ClinSeek-MM-Bench/data/mm_bench/ehrxqa \
        --medmod-root /path/to/ClinSeek-MM-Bench/data/mm_bench/medmod \
        --output outputs/mm_strict_runtime_gate.json

Exits non-zero on any violation.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentlite.commons.EHRManager import EHRManager  # noqa: E402


TIME_COLUMNS = {
    "events": ["charttime"],
    "stays": ["intime", "admittime"],
    "tb_cxr": ["studydatetime"],
    "admissions": ["admittime", "dischtime", "deathtime"],
    "cost": ["chargetime"],
    "labevents": ["charttime"],
    "chartevents": ["charttime"],
    "prescriptions": ["starttime", "stoptime"],
    "transfers": ["intime", "outtime"],
    "icustays": ["intime", "outtime"],
    "inputevents": ["starttime", "endtime"],
    "outputevents": ["charttime"],
    "microbiologyevents": ["charttime", "chartdate"],
}
FORBIDDEN_MEDMOD = {
    "last_careunit",
    "outtime",
    "los",
    "dischtime",
    "deathtime",
    "dod",
    "mortality_inunit",
    "mortality",
    "mortality_inhospital",
}


def normalize_id(value):
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, required=True,
                    help="ClinSeek-Bench_multimodal.jsonl (JSON list or JSONL)")
    ap.add_argument("--ehrxqa-root", type=Path, required=True)
    ap.add_argument("--medmod-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True,
                    help="where to write the JSON gate summary")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    text = args.manifest.read_text(encoding="utf-8").strip()
    all_rows = (json.loads(text) if text.startswith("[") else
                [json.loads(line) for line in text.splitlines() if line.strip()])
    summary = {}
    all_violations = []
    for benchmark, root in (("ehrxqa", args.ehrxqa_root), ("medmod", args.medmod_root)):
        rows = [row for row in all_rows
                if str(row.get("source_benchmark") or "").lower() == benchmark]
        manager = EHRManager(str(root))
        counts = Counter(samples=len(rows))
        task_counts = Counter()
        table_dropped = Counter()
        table_redacted = Counter()
        for index, row in enumerate(rows, 1):
            manager.load_ehr_for_sample(
                str(row["subject_id"]),
                row["prediction_time"],
                linked_study_ids=row.get("study_ids"),
                scope=row.get("scope"),
                task=row.get("task"),
                modalities=row.get("modalities"),
            )
            task_counts[row["task"]] += 1
            if manager.temporal_policy_version != (
                "multimodal-strict-temporal-scope-v1"
            ):
                all_violations.append(
                    {
                        "qid": row["qid"],
                        "type": "wrong_policy",
                        "actual": manager.temporal_policy_version,
                    }
                )
                if len(all_violations) <= 20:
                    print(json.dumps(all_violations[-1]), flush=True)
            report = manager.last_temporal_report
            sample_redacted = 0
            sample_scope_dropped = 0
            for table_name, table_report in report["tables"].items():
                table_dropped[table_name] += table_report.get("dropped_rows", 0)
                table_redacted[table_name] += table_report.get(
                    "redacted_values", 0
                )
                sample_redacted += table_report.get("redacted_values", 0)
                if table_name == "tb_cxr":
                    sample_scope_dropped += table_report.get("dropped_rows", 0)
            counts["samples_with_redaction"] += int(sample_redacted > 0)
            counts["samples_with_cxr_scope_drop"] += int(
                sample_scope_dropped > 0
            )

            cutoff = pd.Timestamp(row["prediction_time"])
            for table_name, frame in manager.ehr_data.items():
                for column in TIME_COLUMNS.get(table_name, []):
                    if column not in frame.columns:
                        continue
                    parsed = pd.to_datetime(frame[column], errors="coerce")
                    future = parsed.notna() & (parsed > cutoff)
                    if future.any():
                        all_violations.append(
                            {
                                "qid": row["qid"],
                                "type": "future_value",
                                "table": table_name,
                                "column": column,
                                "count": int(future.sum()),
                            }
                        )
                        if len(all_violations) <= 20:
                            print(
                                json.dumps(all_violations[-1]),
                                flush=True,
                            )

            cxr = manager.ehr_data.get("tb_cxr")
            if cxr is not None and "study_id" in cxr.columns:
                actual = {normalize_id(value) for value in cxr["study_id"]}
                allowed = {
                    normalize_id(value) for value in row.get("study_ids") or []
                }
                if not actual.issubset(allowed):
                    all_violations.append(
                        {
                            "qid": row["qid"],
                            "type": "unlinked_cxr",
                            "actual_minus_allowed": sorted(actual - allowed),
                        }
                    )
                    if len(all_violations) <= 20:
                        print(json.dumps(all_violations[-1]), flush=True)

            modalities = {
                str(value).strip().lower()
                for value in row.get("modalities") or []
            }
            has_ehr = bool(
                modalities & {"ehr", "ehr_table", "clinical_ehr"}
            )
            if not has_ehr:
                nonempty_longitudinal = [
                    name
                    for name, frame in manager.ehr_data.items()
                    if name != "tb_cxr" and not frame.empty
                ]
                counts["image_only_samples"] += 1
                if nonempty_longitudinal:
                    all_violations.append(
                        {
                            "qid": row["qid"],
                            "type": "image_only_longitudinal_ehr",
                            "tables": nonempty_longitudinal,
                        }
                    )

            if benchmark == "medmod":
                stays = manager.ehr_data.get("stays")
                if stays is not None:
                    forbidden = sorted(
                        {
                            column.lower()
                            for column in stays.columns
                        }
                        & FORBIDDEN_MEDMOD
                    )
                    if forbidden:
                        all_violations.append(
                            {
                                "qid": row["qid"],
                                "type": "medmod_outcome_columns",
                                "columns": forbidden,
                            }
                        )

            if index % 100 == 0 or index == len(rows):
                print(
                    json.dumps(
                        {
                            "benchmark": benchmark,
                            "progress": index,
                            "total": len(rows),
                            "violations": len(all_violations),
                            "violation_types": dict(
                                Counter(
                                    violation["type"]
                                    for violation in all_violations
                                )
                            ),
                        }
                    ),
                    flush=True,
                )

        benchmark_violations = [
            violation
            for violation in all_violations
            if violation["qid"].startswith(benchmark)
            or (
                benchmark == "medmod"
                and violation["qid"].startswith("medmod")
            )
        ]
        summary[benchmark] = {
            **dict(counts),
            "task_counts": dict(sorted(task_counts.items())),
            "table_dropped_rows": dict(sorted(table_dropped.items())),
            "table_redacted_values": dict(sorted(table_redacted.items())),
            "violations": len(benchmark_violations),
        }

    payload = {
        "policy_version": "multimodal-strict-temporal-scope-v1",
        "benchmarks": summary,
        "total_samples": sum(value["samples"] for value in summary.values()),
        "total_violations": len(all_violations),
        "violation_examples": all_violations[:100],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 1 if all_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
