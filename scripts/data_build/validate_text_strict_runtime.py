#!/usr/bin/env python3
"""Validate the strict text-Agent runtime over an EHR-Bench manifest."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentlite.commons.EHRManager import EHRManager  # noqa: E402
from agentlite.commons.temporal_policy import POLICY_VERSION  # noqa: E402


ADMINISTRATIVE_TABLES = {
    "diagnoses_icd",
    "diagnosis",
    "procedures_icd",
    "hcpcsevents",
    "drgcodes",
}
POE_DETAIL_FIELDS = {
    "Admit to",
    "Admit category",
    "Tubes & Drains type",
    "Code status",
    "Transfer to",
    "Level of Urgency",
    "Indication",
}
EMAR_DETAIL_FORBIDDEN = {
    "reason_for_no_barcode",
    "complete_dose_not_given",
    "dose_given",
    "dose_given_unit",
    "will_remainder_of_dose_be_given",
    "product_amount_given",
    "prior_infusion_rate",
    "infusion_rate",
    "infusion_rate_adjustment",
    "infusion_rate_adjustment_amount",
    "infusion_rate_unit",
    "infusion_complete",
    "completion_interval",
    "new_iv_bag_hung",
    "continued_infusion_in_other_location",
    "restart_interval",
}
PHARMACY_FORBIDDEN = {
    "stoptime",
    "status",
    "expirationdate",
    "dispensation",
    "fill_quantity",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="EHR-Bench patient database root served by the EHR MCP server",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="ClinSeek-Bench_text.json with target_hadm_id / target_stay_id "
             "(see materialize_ehr_bench_target_encounters.py)",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-every", type=int, default=200)
    return parser.parse_args()


def violation(
    failures: list[dict[str, Any]],
    row: dict[str, Any],
    kind: str,
    detail: Any,
) -> None:
    failures.append(
        {
            "qid": row.get("qid"),
            "subject_id": row.get("subject_id"),
            "prediction_time": row.get("prediction_time"),
            "type": kind,
            "detail": detail,
        }
    )


def main() -> int:
    args = parse_args()
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Manifest must contain a JSON list")

    warnings.filterwarnings("ignore")
    manager = EHRManager(
        str(args.data_root),
        temporal_policy_mode="strict_mimic_iv",
    )
    counts = Counter(samples=len(rows))
    table_counts: dict[str, Counter] = defaultdict(Counter)
    failures: list[dict[str, Any]] = []

    for index, row in enumerate(rows, 1):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                manager.load_ehr_for_sample(
                    str(row["subject_id"]),
                    row["prediction_time"],
                    task=row.get("task"),
                    hadm_id=row.get("hadm_id"),
                    target_hadm_id=row.get("target_hadm_id"),
                    target_stay_id=row.get("target_stay_id"),
                )
        except Exception as exc:
            counts["build_failures"] += 1
            violation(failures, row, "build_failure", str(exc))
            continue

        if manager.temporal_policy_version != POLICY_VERSION:
            violation(
                failures,
                row,
                "wrong_policy_version",
                manager.temporal_policy_version,
            )

        report = manager.last_temporal_report or {}
        for table_name, table_report in report.get("tables", {}).items():
            current = table_counts[table_name]
            input_rows = int(table_report.get("input_rows", 0))
            output_rows = int(table_report.get("output_rows", 0))
            current["source_samples"] += int(input_rows > 0)
            current["visible_samples"] += int(output_rows > 0)
            for metric in (
                "input_rows",
                "output_rows",
                "dropped_rows",
                "redacted_values",
            ):
                current[metric] += int(table_report.get(metric, 0))

        tables = manager.ehr_data
        patients = tables.get("patients")
        if patients is not None and "dod" in patients.columns:
            violation(failures, row, "patients_dod_visible", True)
        admissions = tables.get("admissions")
        if admissions is not None and "text" in admissions.columns:
            violation(failures, row, "admissions_text_visible", True)
        if "cost" in tables:
            violation(failures, row, "cost_visible", True)

        for table_name in ADMINISTRATIVE_TABLES:
            frame = tables.get(table_name)
            if frame is None or frame.empty:
                continue
            if "available_time" not in frame.columns:
                violation(
                    failures,
                    row,
                    "administrative_availability_missing",
                    table_name,
                )
            if (
                table_name in {"diagnoses_icd", "diagnosis"}
                and "charttime" in frame.columns
            ):
                violation(
                    failures,
                    row,
                    "synthetic_charttime_visible",
                    table_name,
                )

        poe_detail = tables.get("poe_detail")
        if (
            poe_detail is not None
            and not poe_detail.empty
            and "field_name" in poe_detail.columns
        ):
            unexpected = sorted(
                set(poe_detail["field_name"].dropna()) - POE_DETAIL_FIELDS
            )
            if unexpected:
                violation(
                    failures,
                    row,
                    "poe_detail_nonwhitelisted_field",
                    unexpected,
                )

        emar_detail = tables.get("emar_detail")
        if emar_detail is not None:
            unexpected = sorted(
                set(emar_detail.columns) & EMAR_DETAIL_FORBIDDEN
            )
            if unexpected:
                violation(
                    failures,
                    row,
                    "emar_detail_mutable_field",
                    unexpected,
                )

        pharmacy = tables.get("pharmacy")
        if pharmacy is not None:
            unexpected = sorted(
                set(pharmacy.columns) & PHARMACY_FORBIDDEN
            )
            if unexpected:
                violation(
                    failures,
                    row,
                    "pharmacy_mutable_field",
                    unexpected,
                )

        if args.progress_every and (
            index % args.progress_every == 0 or index == len(rows)
        ):
            print(
                json.dumps(
                    {
                        "progress": index,
                        "total": len(rows),
                        "failures": len(failures),
                    }
                ),
                flush=True,
            )

    payload = {
        "policy_version": POLICY_VERSION,
        "data_root": str(args.data_root),
        "manifest": str(args.manifest),
        **dict(counts),
        "violations": len(failures),
        "violation_examples": failures[:100],
        "tables": {
            table_name: dict(values)
            for table_name, values in sorted(table_counts.items())
        },
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
