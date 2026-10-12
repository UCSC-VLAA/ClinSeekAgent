#!/usr/bin/env python3
"""Audit multimodal benchmark DBs against each sample's cutoff contract.

The audit separates four distinct questions:

1. Does the full physical patient DB contain post-cutoff data?  This is an
   expected storage hazard, not by itself model-visible leakage.
2. Does the legacy first-time-column runtime loader leave a confirmed
   post-cutoff field visible?
3. Does the manifest directly attach a post-cutoff CXR?
4. Does runtime behavior violate the benchmark's linked-study-only policy?

It supports both JSON and JSONL manifests.  It is intentionally read-only.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable


MISSING = {"", "none", "null", "nan", "nat", "<na>"}
TIME_NAME = re.compile(r"(?:time|date)", re.IGNORECASE)
STUDY_IN_PATH = re.compile(r"/s(\d+)(?:/|$)")

OUTCOME_AVAILABILITY = {
    "admissions": {
        "dischtime": "dischtime",
        "deathtime": "deathtime",
        "edouttime": "edouttime",
        "discharge_location": "dischtime",
        "hospital_expire_flag": "dischtime",
    },
    "transfers": {"outtime": "outtime"},
    "edstays": {"outtime": "outtime", "disposition": "outtime"},
    "icustays": {
        "outtime": "outtime",
        "los": "outtime",
        "last_careunit": "outtime",
    },
    "patients": {"dod": "dod"},
}

RISK_TASKS = {
    "medmod_decompensation",
    "medmod_in_hospital_mortality",
}


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        first = handle.read(1)
        handle.seek(0)
        if first == "[":
            data = json.load(handle)
            if not isinstance(data, list):
                raise ValueError("JSON manifest must contain a top-level list")
            return data
        return [json.loads(line) for line in handle if line.strip()]


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1]
        for row in conn.execute(f"PRAGMA table_info({qident(table)})")
    ]


def scalar(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> int:
    return int(conn.execute(sql, tuple(args)).fetchone()[0])


def present_sql(column: str) -> str:
    qc = qident(column)
    missing = ", ".join(f"'{value}'" for value in sorted(MISSING))
    return (
        f"{qc} IS NOT NULL AND "
        f"lower(trim(CAST({qc} AS TEXT))) NOT IN ({missing})"
    )


def datetime_sql(column: str) -> str:
    return f"datetime({qident(column)})"


def date_or_datetime_available_sql(column: str) -> str:
    """Treat date-only values as becoming available at end of that day."""
    qc = qident(column)
    return (
        "CASE WHEN length(trim(CAST("
        f"{qc} AS TEXT))) <= 10 "
        f"THEN datetime({qc}, '+1 day', '-1 second') "
        f"ELSE datetime({qc}) END"
    )


def linked_study_ids(row: dict[str, Any]) -> list[int]:
    explicit = row.get("study_ids")
    if isinstance(explicit, list):
        result = []
        for value in explicit:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                pass
        if result:
            return sorted(set(result))
    result = []
    for value in (row.get("image_paths") or []) + (row.get("report_paths") or []):
        match = STUDY_IN_PATH.search(str(value).replace("\\", "/"))
        if match:
            result.append(int(match.group(1)))
    return sorted(set(result))


def audit_one(row: dict[str, Any], db_root: Path) -> dict[str, Any]:
    qid = str(row.get("qid") or row.get("query_id") or "")
    source = str(row.get("source_benchmark") or "")
    task = str(row.get("task") or "")
    subject_id = row.get("subject_id")
    cutoff = str(
        row.get("prediction_time")
        or (row.get("snapshot_spec") or {}).get("cutoff_time")
        or ""
    )
    db_hint = row.get("db_path_hint")
    db_path = (
        db_root / str(db_hint)
        if db_hint
        else db_root / "database" / f"patient_{subject_id}.db"
    )
    result: dict[str, Any] = {
        "qid": qid,
        "source_benchmark": source,
        "task": task,
        "subject_id": subject_id,
        "hadm_id": row.get("hadm_id"),
        "stay_id": row.get("stay_id"),
        "cutoff": cutoff,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "linked_study_ids": linked_study_ids(row),
        "physical_future_by_table_column": {},
        "runtime_secondary_future_by_table_column": {},
        "runtime_future_outcome_by_table_column": {},
        "runtime_unbounded_tables": {},
        "linked_study_time_relation": {},
        "visible_unlinked_cxr_rows": 0,
        "current_stay_last_careunit": None,
        "availability_unverifiable": [],
        "policy_findings": [],
    }
    if not db_path.exists() or not cutoff:
        if not db_path.exists():
            result["policy_findings"].append("MISSING_PATIENT_DB")
        if not cutoff:
            result["policy_findings"].append("MISSING_CUTOFF")
        return result

    with sqlite3.connect(db_path) as conn:
        schemas = {table: columns(conn, table) for table in table_names(conn)}
        for table, cols in schemas.items():
            time_columns = [column for column in cols if TIME_NAME.search(column)]
            if not time_columns:
                rows = scalar(conn, f"SELECT COUNT(*) FROM {qident(table)}")
                if rows:
                    result["runtime_unbounded_tables"][table] = rows
                continue

            first_time = time_columns[0]
            for column in time_columns:
                count = scalar(
                    conn,
                    f"SELECT COUNT(*) FROM {qident(table)} "
                    f"WHERE {datetime_sql(column)} >= datetime(?)",
                    (cutoff,),
                )
                if count:
                    result["physical_future_by_table_column"][
                        f"{table}.{column}"
                    ] = count
                if column != first_time:
                    runtime_count = scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {qident(table)} "
                        f"WHERE {datetime_sql(first_time)} < datetime(?) "
                        f"AND {datetime_sql(column)} >= datetime(?)",
                        (cutoff, cutoff),
                    )
                    if runtime_count:
                        result["runtime_secondary_future_by_table_column"][
                            f"{table}.{column}"
                        ] = runtime_count

            for field, available_at in OUTCOME_AVAILABILITY.get(table, {}).items():
                if field not in cols or available_at not in cols:
                    continue
                count = scalar(
                    conn,
                    f"SELECT COUNT(*) FROM {qident(table)} "
                    f"WHERE {datetime_sql(first_time)} < datetime(?) "
                    f"AND {present_sql(field)} "
                    f"AND {datetime_sql(available_at)} >= datetime(?)",
                    (cutoff, cutoff),
                )
                if count:
                    result["runtime_future_outcome_by_table_column"][
                        f"{table}.{field}"
                    ] = count

        # The legacy loader does not recognize MIMIC's abbreviated ``dod``
        # (date of death) as a timestamp, so a future death date survives in
        # the otherwise unbounded patients table.
        if "patients" in schemas and "dod" in schemas["patients"]:
            count = scalar(
                conn,
                f"SELECT COUNT(*) FROM {qident('patients')} "
                f"WHERE {present_sql('dod')} "
                f"AND {date_or_datetime_available_sql('dod')} >= datetime(?)",
                (cutoff,),
            )
            if count:
                result["physical_future_by_table_column"]["patients.dod"] = count
                result["runtime_future_outcome_by_table_column"]["patients.dod"] = count

        # In the released EHRXQA DB, diagnosis rows are assigned a synthetic
        # admission-time charttime even though the coded diagnoses are only
        # final after discharge.  A first-time-column filter therefore keeps
        # current-encounter codes whose parent discharge is still future.
        if (
            "diagnoses_icd" in schemas
            and "admissions" in schemas
            and {"hadm_id", "charttime"}.issubset(schemas["diagnoses_icd"])
            and {"hadm_id", "dischtime"}.issubset(schemas["admissions"])
        ):
            count = scalar(
                conn,
                f"SELECT COUNT(*) FROM {qident('diagnoses_icd')} d "
                f"JOIN {qident('admissions')} a "
                "ON CAST(d.hadm_id AS TEXT)=CAST(a.hadm_id AS TEXT) "
                "WHERE datetime(d.charttime) < datetime(?) "
                "AND datetime(a.dischtime) >= datetime(?)",
                (cutoff, cutoff),
            )
            if count:
                result["runtime_future_outcome_by_table_column"][
                    "diagnoses_icd.synthetic_admission_time"
                ] = count

        if "events" in schemas and "charttime" in schemas["events"]:
            # MedMod's flattened event table has no store/availability time.
            result["availability_unverifiable"].append(
                "events:charttime_without_storetime"
            )
        if "tb_cxr" in schemas and "studydatetime" in schemas["tb_cxr"]:
            result["availability_unverifiable"].append(
                "tb_cxr:studydatetime_without_report_storetime"
            )
            linked = result["linked_study_ids"]
            for study_id in linked:
                found = conn.execute(
                    f"SELECT studydatetime FROM {qident('tb_cxr')} "
                    "WHERE CAST(study_id AS TEXT)=? LIMIT 1",
                    (str(study_id),),
                ).fetchone()
                if found is None:
                    result["linked_study_time_relation"][str(study_id)] = "missing"
                    continue
                relation = conn.execute(
                    "SELECT CASE "
                    "WHEN datetime(?) > datetime(?) THEN 'after' "
                    "WHEN datetime(?) = datetime(?) THEN 'equal' "
                    "ELSE 'before' END",
                    (found[0], cutoff, found[0], cutoff),
                ).fetchone()[0]
                result["linked_study_time_relation"][str(study_id)] = relation

            if linked:
                placeholders = ",".join("?" for _ in linked)
                result["visible_unlinked_cxr_rows"] = scalar(
                    conn,
                    f"SELECT COUNT(*) FROM {qident('tb_cxr')} "
                    "WHERE datetime(studydatetime) < datetime(?) "
                    f"AND CAST(study_id AS INTEGER) NOT IN ({placeholders})",
                    (cutoff, *linked),
                )

        stay_id = row.get("stay_id")
        if (
            stay_id not in (None, "")
            and "stays" in schemas
            and {"stay_id", "intime", "last_careunit"}.issubset(schemas["stays"])
        ):
            found = conn.execute(
                f"SELECT last_careunit FROM {qident('stays')} "
                "WHERE CAST(stay_id AS TEXT)=? "
                "AND datetime(intime) < datetime(?) LIMIT 1",
                (str(stay_id), cutoff),
            ).fetchone()
            if found and str(found[0]).strip().lower() not in MISSING:
                result["current_stay_last_careunit"] = found[0]
    conn.close()

    if result["physical_future_by_table_column"]:
        result["policy_findings"].append("FULL_DB_CONTAINS_POST_CUTOFF_DATA")
    if (
        result["runtime_secondary_future_by_table_column"]
        or result["runtime_future_outcome_by_table_column"]
    ):
        result["policy_findings"].append(
            "CONFIRMED_LEGACY_RUNTIME_TEMPORAL_EXPOSURE"
        )
    relations = set(result["linked_study_time_relation"].values())
    if "after" in relations:
        result["policy_findings"].append("DIRECT_CXR_AFTER_CUTOFF")
    if "equal" in relations:
        result["policy_findings"].append(
            "CUTOFF_INCLUSIVE_MANIFEST_VS_STRICT_LESS_THAN_LOADER"
        )
    if result["visible_unlinked_cxr_rows"]:
        result["policy_findings"].append("LINKED_STUDIES_ONLY_POLICY_VIOLATION")
    if task in RISK_TASKS and result["current_stay_last_careunit"]:
        result["policy_findings"].append(
            "CURRENT_STAY_FINAL_SUMMARY_FIELD_HIGH_RISK"
        )
    if task == "medmod_radiology":
        result["policy_findings"].append(
            "IMAGE_ONLY_TASK_HAS_PATIENT_DB_AVAILABLE"
        )
    if result["availability_unverifiable"]:
        result["policy_findings"].append("AVAILABILITY_TIME_UNVERIFIABLE")
    return result


def increment(counter: collections.Counter[str], key: str, amount: int = 1):
    counter[key] += amount


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--db-root",
        action="append",
        required=True,
        metavar="SOURCE=ROOT",
        help="Repeat for each source, e.g. medmod=/path/MedModAgentBench_v3",
    )
    parser.add_argument("--source", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    roots = {}
    for spec in args.db_root:
        source, separator, value = spec.partition("=")
        if not separator:
            raise SystemExit(f"Invalid --db-root: {spec}")
        roots[source] = Path(value)

    rows = load_rows(args.manifest)
    if args.source:
        rows = [
            row
            for row in rows
            if str(row.get("source_benchmark") or "") == args.source
        ]

    results = []
    finding_counts = collections.Counter()
    task_counts = collections.Counter()
    physical_sample_counts = collections.Counter()
    physical_row_counts = collections.Counter()
    runtime_secondary_sample_counts = collections.Counter()
    runtime_secondary_row_counts = collections.Counter()
    runtime_outcome_sample_counts = collections.Counter()
    runtime_outcome_row_counts = collections.Counter()
    linked_relation_counts = collections.Counter()
    visible_unlinked_cxr_rows_total = 0
    db_missing = 0
    for row in rows:
        source = str(row.get("source_benchmark") or "")
        root = roots.get(source)
        if root is None:
            raise SystemExit(f"No --db-root supplied for source {source!r}")
        audited = audit_one(row, root)
        results.append(audited)
        task_counts[audited["task"]] += 1
        if not audited["db_exists"]:
            db_missing += 1
        for key, count in audited["physical_future_by_table_column"].items():
            physical_sample_counts[key] += 1
            physical_row_counts[key] += count
        for key, count in audited[
            "runtime_secondary_future_by_table_column"
        ].items():
            runtime_secondary_sample_counts[key] += 1
            runtime_secondary_row_counts[key] += count
        for key, count in audited[
            "runtime_future_outcome_by_table_column"
        ].items():
            runtime_outcome_sample_counts[key] += 1
            runtime_outcome_row_counts[key] += count
        for relation in audited["linked_study_time_relation"].values():
            linked_relation_counts[relation] += 1
        visible_unlinked_cxr_rows_total += audited["visible_unlinked_cxr_rows"]
        for finding in audited["policy_findings"]:
            finding_counts[finding] += 1

    by_task: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for result in results:
        by_task[result["task"]]["samples"] += 1
        for finding in result["policy_findings"]:
            by_task[result["task"]][finding] += 1

    summary = {
        "manifest": str(args.manifest),
        "samples": len(results),
        "db_missing": db_missing,
        "task_counts": dict(task_counts),
        "finding_counts": dict(finding_counts),
        "physical_future": {
            "samples_by_table_column": dict(physical_sample_counts),
            "rows_by_table_column": dict(physical_row_counts),
        },
        "legacy_runtime_confirmed": {
            "secondary_time_samples_by_table_column": dict(
                runtime_secondary_sample_counts
            ),
            "secondary_time_rows_by_table_column": dict(
                runtime_secondary_row_counts
            ),
            "outcome_samples_by_table_column": dict(
                runtime_outcome_sample_counts
            ),
            "outcome_rows_by_table_column": dict(runtime_outcome_row_counts),
        },
        "linked_study_time_relations": dict(linked_relation_counts),
        "visible_unlinked_cxr_rows_total": visible_unlinked_cxr_rows_total,
        "by_task": {task: dict(counter) for task, counter in by_task.items()},
        "interpretation": {
            "FULL_DB_CONTAINS_POST_CUTOFF_DATA": (
                "Storage hazard only; not model-visible leakage unless runtime "
                "filtering or direct attachment exposes it."
            ),
            "CONFIRMED_LEGACY_RUNTIME_TEMPORAL_EXPOSURE": (
                "Confirmed DB/runtime leakage under the original first-time-column loader."
            ),
            "DIRECT_CXR_AFTER_CUTOFF": (
                "Confirmed manifest/source leakage: an attached study is after cutoff."
            ),
            "LINKED_STUDIES_ONLY_POLICY_VIOLATION": (
                "Runtime DB view contains historical CXR rows outside the linked study set."
            ),
            "AVAILABILITY_TIME_UNVERIFIABLE": (
                "The compact DB dropped store/finalization times, so late-entered "
                "records cannot be proven safe from this DB alone."
            ),
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "per_sample.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
