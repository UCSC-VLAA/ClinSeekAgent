#!/usr/bin/env python3
"""Bind diagnosis-code benchmark rows to target encounter IDs.

The strict runtime must distinguish a task's answer encounter from ordinary
historical encounters. A fixed post-discharge delay is not sufficient: once
the delay expires, the answer codes become visible again. This one-time
manifest materializer adds server-trusted ``target_hadm_id`` or
``target_stay_id`` fields without adding them to the model prompt.

Inpatient targets are verified against the manifest label and the ICD/CCS
reference table. The existing ``hadm_id`` is preferred when it is a verified
candidate; otherwise exactly one label-complete admission is required. ED
targets do not need the gold label for selection: they are the unique stay
ending at ``prediction_time - 1 second`` or, otherwise, the unique stay active
at prediction time. Gold labels are used only as a post-selection assertion.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


INPATIENT_TASKS = frozenset({"diagnoses_icd", "diagnoses_ccs"})
ED_TASKS = frozenset({"diagnosis", "diagnosis_ccs"})


def normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def normalized_labels(row: dict[str, Any]) -> set[str]:
    labels = (row.get("task_info") or {}).get("label")
    if not isinstance(labels, list) or not labels:
        raise ValueError(f"{row.get('qid')}: non-empty task_info.label required")
    return {str(value).strip() for value in labels}


def load_reference_map(
    connection: sqlite3.Connection,
    table: str,
) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        f'SELECT icd_code, icd_version, long_title FROM "{table}"'
    )
    return {
        (str(code).strip(), str(version).strip()): str(title).strip()
        for code, version, title in rows
    }


def code_labels_by_encounter(
    connection: sqlite3.Connection,
    *,
    table: str,
    encounter_key: str,
    label_map: dict[tuple[str, str], str] | None,
) -> dict[str, set[str]]:
    columns = f'{encounter_key}, icd_code, icd_version'
    if table == "diagnosis" and label_map is None:
        columns += ", icd_title"
    grouped: dict[str, set[str]] = defaultdict(set)
    for record in connection.execute(f'SELECT {columns} FROM "{table}"'):
        encounter_id = normalize_id(record[0])
        if encounter_id is None:
            continue
        if label_map is None:
            label = str(record[3]).strip()
        else:
            label = label_map.get(
                (str(record[1]).strip(), str(record[2]).strip())
            )
        if label:
            grouped[encounter_id].add(label)
    return dict(grouped)


def resolve_inpatient_target(
    row: dict[str, Any],
    connection: sqlite3.Connection,
    icd_map: dict[tuple[str, str], str],
    ccs_map: dict[tuple[str, str], str],
) -> str:
    gold = normalized_labels(row)
    label_map = icd_map if row["task"] == "diagnoses_icd" else ccs_map
    by_hadm = code_labels_by_encounter(
        connection,
        table="diagnoses_icd",
        encounter_key="hadm_id",
        label_map=label_map,
    )
    candidates = sorted(
        hadm_id for hadm_id, labels in by_hadm.items() if gold <= labels
    )
    manifest_hadm_id = normalize_id(row.get("hadm_id"))
    if manifest_hadm_id in candidates:
        return manifest_hadm_id
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        f"{row.get('qid')}: target_hadm_id is ambiguous; "
        f"verified candidates={candidates}, manifest hadm_id={manifest_hadm_id!r}"
    )


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def resolve_ed_target(
    row: dict[str, Any],
    connection: sqlite3.Connection,
    ccs_map: dict[tuple[str, str], str],
) -> str:
    cutoff = parse_time(row.get("prediction_time"))
    if cutoff is None:
        raise ValueError(f"{row.get('qid')}: valid prediction_time required")

    stays = []
    for stay_id, intime, outtime in connection.execute(
        "SELECT stay_id, intime, outtime FROM edstays"
    ):
        normalized_stay_id = normalize_id(stay_id)
        if normalized_stay_id is None:
            continue
        stays.append(
            (normalized_stay_id, parse_time(intime), parse_time(outtime))
        )

    expected_end = cutoff - timedelta(seconds=1)
    candidates = [stay for stay in stays if stay[2] == expected_end]
    resolution = "outtime=prediction_time-1s"
    if len(candidates) != 1:
        candidates = [
            stay
            for stay in stays
            if stay[1] is not None
            and stay[2] is not None
            and stay[1] < cutoff <= stay[2]
        ]
        resolution = "unique active ED stay"
    if len(candidates) != 1:
        raise ValueError(
            f"{row.get('qid')}: {resolution} produced "
            f"{len(candidates)} target_stay_id candidates"
        )

    target_stay_id = candidates[0][0]
    label_map = None if row["task"] == "diagnosis" else ccs_map
    by_stay = code_labels_by_encounter(
        connection,
        table="diagnosis",
        encounter_key="stay_id",
        label_map=label_map,
    )
    gold = normalized_labels(row)
    if not gold <= by_stay.get(target_stay_id, set()):
        raise ValueError(
            f"{row.get('qid')}: time-resolved stay {target_stay_id} does not "
            "contain all target labels"
        )
    return target_stay_id


def atomic_write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--reference-db", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Output manifest; defaults to replacing --manifest atomically.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Manifest must contain a JSON list")

    with sqlite3.connect(args.reference_db) as reference:
        icd_map = load_reference_map(reference, "d_icd_diagnoses")
        ccs_map = load_reference_map(reference, "d_ccs_diagnoses")

    counts = defaultdict(int)
    for row in rows:
        task = str(row.get("task") or "").strip()
        if task not in INPATIENT_TASKS | ED_TASKS:
            continue
        patient_db = (
            args.data_root
            / "database"
            / f"patient_{normalize_id(row.get('subject_id'))}.db"
        )
        if not patient_db.is_file():
            raise FileNotFoundError(patient_db)
        with sqlite3.connect(patient_db) as patient:
            if task in INPATIENT_TASKS:
                row["target_hadm_id"] = resolve_inpatient_target(
                    row,
                    patient,
                    icd_map,
                    ccs_map,
                )
                row.pop("target_stay_id", None)
                counts["target_hadm_id"] += 1
            else:
                row["target_stay_id"] = resolve_ed_target(
                    row,
                    patient,
                    ccs_map,
                )
                row.pop("target_hadm_id", None)
                counts["target_stay_id"] += 1

    output = args.output or args.manifest
    atomic_write_json(output, rows)
    print(
        json.dumps(
            {
                "manifest": str(output),
                "rows": len(rows),
                "target_hadm_id": counts["target_hadm_id"],
                "target_stay_id": counts["target_stay_id"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
