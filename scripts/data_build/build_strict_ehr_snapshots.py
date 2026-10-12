#!/usr/bin/env python3
"""Build leak-resistant AgentEHR-Bench SQLite snapshots.

The released AgentEHR patient databases contain each patient's complete
longitudinal record.  That is convenient for retrieval, but unsafe when the
runtime loader filters only on the first timestamp-like column in each table:
post-cutoff fields can ride along on an otherwise pre-cutoff row, untimestamped
detail tables are loaded in full, and ``charttime`` may precede the time at
which a record became readable (``storetime``).

This script converts the full patient databases into one *physical snapshot*
per subject at the prediction cutoff found in a benchmark manifest.  Future
information is absent or NULL in the output, so safety does not depend on the
runtime reimplementing the same policy correctly.

Strictness choices
------------------
* Every supported table has an explicit policy.  Unknown tables fail closed by
  default (``--unknown-table-policy error``).
* Rows with missing/unparseable required event or availability timestamps are
  dropped.
* ``storetime``/``storedate`` bounds actual record availability wherever
  present.
* Future outcome/end-time fields on surviving rows are redacted.
* Discharge text copied onto ``admissions.text`` is removed completely.
* Untimestamped detail tables are retained only when their parent event
  survives the cutoff.
* Tables without defensible availability semantics (billing/coding tables and
  ``pharmacy``) are omitted.
* A final audit rejects any remaining parseable timestamp at or after cutoff,
  any unparseable non-empty timestamp, unsafe admissions fields, or orphaned
  detail rows.

Compatibility
-------------
The current AgentEHR runtime opens ``database/patient_<subject_id>.db``.
Accordingly, the default output uses the same filename and requires each
subject to have exactly one unique prediction cutoff in the input manifest.
If a manifest contains multiple cutoffs for one subject, the build stops:
serving those samples safely requires cutoff-specific database identifiers or
a fixed runtime loader.

Example
-------
Optional defense in depth for AgentEHR-Bench: the EHR MCP server already
applies the strict temporal policy at load time; physical snapshots additionally
remove future data from the files themselves. Run with the EHR MCP environment
(pandas is required):

    python scripts/data_build/build_strict_ehr_snapshots.py \
        --source-root /path/to/AgentEHR-Bench/MIMICIVAgentBench \
        --manifest /path/to/cases.json \
        --output-root /path/to/MIMICIVAgentBench-strict

For a quick validation build add `--limit 2`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


POLICY_VERSION = "strict-ehr-snapshot-v1"
MISSING_STRINGS = {"", "nan", "nat", "none", "null", "<na>"}

# Date-only values are conservatively placed at the final instant of the day.
# This prevents a date such as 2174-05-02 from becoming visible at midnight
# during a prediction made earlier on that same calendar date.
DATE_ONLY_COLUMNS = {
    "chartdate",
    "storedate",
    "dod",
    "expirationdate",
}

# These identifiers/answers belong to the benchmark rather than the EHR source
# and must never be introduced into a patient snapshot.
FORBIDDEN_EHR_COLUMNS = {
    "label",
    "ground_truth",
    "prediction",
    "answer",
}


@dataclass(frozen=True)
class FieldGuard:
    """Make ``field`` visible only when one of ``available_at`` is pre-cutoff."""

    field: str
    available_at: tuple[str, ...]


@dataclass(frozen=True)
class TablePolicy:
    """Explicit temporal policy for a timestamped or static patient table."""

    event_at: tuple[str, ...] = ()
    available_at: tuple[str, ...] = ()
    guards: tuple[FieldGuard, ...] = ()
    drop_columns: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ChildPolicy:
    """Filter an untimestamped child table through a safe parent snapshot."""

    parent: str
    keys: tuple[str, ...]
    note: str = ""


@dataclass
class TableReport:
    input_rows: int
    output_rows: int = 0
    dropped_rows: int = 0
    redacted_values: int = 0
    unparseable_time_values_redacted: int = 0
    future_time_values_redacted: int = 0
    dropped_columns: list[str] = field(default_factory=list)
    policy: str = ""


# Explicitly supported tables.  ``event_at`` selects the clinical event time;
# ``available_at`` selects the time the record became readable.  Alternative
# columns are coalesced left-to-right per row.
TABLE_POLICIES: dict[str, TablePolicy] = {
    "patients": TablePolicy(
        guards=(FieldGuard("dod", ("dod",)),),
        note="Static demographics; date of death is cutoff-guarded.",
    ),
    "admissions": TablePolicy(
        event_at=("admittime",),
        guards=(
            FieldGuard("dischtime", ("dischtime",)),
            FieldGuard("deathtime", ("deathtime",)),
            FieldGuard("edouttime", ("edouttime",)),
            FieldGuard("discharge_location", ("dischtime",)),
            FieldGuard("hospital_expire_flag", ("dischtime",)),
        ),
        drop_columns=("text",),
        note="Admission is visible at admittime; discharge outcomes are guarded.",
    ),
    "transfers": TablePolicy(
        event_at=("intime",),
        guards=(FieldGuard("outtime", ("outtime",)),),
    ),
    "edstays": TablePolicy(
        event_at=("intime",),
        guards=(
            FieldGuard("outtime", ("outtime",)),
            FieldGuard("disposition", ("outtime",)),
        ),
    ),
    "icustays": TablePolicy(
        event_at=("intime",),
        guards=(
            FieldGuard("outtime", ("outtime",)),
            FieldGuard("los", ("outtime",)),
        ),
    ),
    "services": TablePolicy(event_at=("transfertime",)),
    "prescriptions": TablePolicy(
        event_at=("starttime",),
        guards=(FieldGuard("stoptime", ("stoptime",)),),
    ),
    "poe": TablePolicy(
        event_at=("ordertime",),
        # These columns describe later order disposition in a final-state row.
        drop_columns=(
            "order_status",
            "discontinue_of_poe_id",
            "discontinued_by_poe_id",
        ),
    ),
    "medrecon": TablePolicy(event_at=("charttime",)),
    "pyxis": TablePolicy(event_at=("charttime",)),
    "vitalsign": TablePolicy(event_at=("charttime",)),
    "omr": TablePolicy(event_at=("chartdate",)),
    "chartevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "datetimeevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "labevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "outputevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "inputevents": TablePolicy(
        # The row contains final quantities/status.  Keeping an infusion that
        # merely started before cutoff can expose how it ended, so strict mode
        # admits only completed intervals.
        event_at=("endtime",),
        available_at=("storetime",),
        note="Visible only after both the interval end and store time.",
    ),
    "ingredientevents": TablePolicy(
        event_at=("endtime",),
        available_at=("storetime",),
        note="Visible only after both the interval end and store time.",
    ),
    "procedureevents": TablePolicy(
        event_at=("endtime",),
        available_at=("storetime",),
        note="Visible only after both the procedure end and store time.",
    ),
    "emar": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
        guards=(FieldGuard("scheduletime", ("scheduletime",)),),
    ),
    "microbiologyevents": TablePolicy(
        event_at=("charttime", "chartdate"),
        available_at=("storetime", "storedate"),
    ),
    "radiology": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "discharge": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
}


# These final-state/billing tables do not carry a defensible record-availability
# timestamp.  The two diagnosis tables also contain timestamps synthesized by
# patient_event2db.py from dischtime/outtime minus one minute.  Fail closed.
DROP_TABLES: dict[str, str] = {
    "diagnoses_icd": "Synthetic charttime based on dischtime; coding availability unknown.",
    "diagnosis": "Synthetic charttime based on ED outtime; coding availability unknown.",
    "procedures_icd": "Administrative code availability is not represented.",
    "hcpcsevents": "Billing/code availability is not represented.",
    "drgcodes": "Post-discharge billing codes have no timestamp.",
    "pharmacy": "Mutable final-state medication table; current runtime also clears it.",
    "cost": "Post-event administrative table with no availability timestamp.",
    "triage": (
        "No record timestamp; ED arrival time does not prove when triage was "
        "completed."
    ),
}


CHILD_POLICIES: dict[str, ChildPolicy] = {
    "discharge_detail": ChildPolicy(
        parent="discharge",
        keys=("note_id",),
        note="Visible only with a pre-cutoff stored discharge note.",
    ),
    "radiology_detail": ChildPolicy(
        parent="radiology",
        keys=("note_id",),
        note="Visible only with a pre-cutoff stored radiology note.",
    ),
    "poe_detail": ChildPolicy(
        parent="poe",
        keys=("poe_id", "poe_seq"),
        note="Visible only with a pre-cutoff parent order.",
    ),
    "emar_detail": ChildPolicy(
        parent="emar",
        keys=("emar_id",),
        note="Visible only with a pre-cutoff stored medication administration.",
    ),
}


# Input schemas are allowlisted as well as table names.  A newly added source
# column has unknown temporal semantics and must not silently flow into a
# supposedly strict snapshot.
EXPECTED_SOURCE_COLUMNS: dict[str, set[str]] = {
    "patients": set(
        "subject_id gender anchor_age anchor_year anchor_year_group dod".split()
    ),
    "admissions": set(
        "subject_id hadm_id admittime dischtime deathtime admission_type "
        "admit_provider_id admission_location discharge_location insurance "
        "language marital_status race edregtime edouttime "
        "hospital_expire_flag text".split()
    ),
    "transfers": set(
        "subject_id hadm_id transfer_id eventtype careunit intime outtime".split()
    ),
    "edstays": set(
        "subject_id hadm_id stay_id intime outtime gender race "
        "arrival_transport disposition".split()
    ),
    "icustays": set(
        "subject_id hadm_id stay_id first_careunit last_careunit intime "
        "outtime los".split()
    ),
    "services": set(
        "subject_id hadm_id transfertime prev_service curr_service".split()
    ),
    "prescriptions": set(
        "subject_id hadm_id pharmacy_id poe_id poe_seq order_provider_id "
        "starttime stoptime drug_type drug formulary_drug_cd gsn ndc "
        "prod_strength form_rx dose_val_rx dose_unit_rx form_val_disp "
        "form_unit_disp doses_per_24_hrs route".split()
    ),
    "poe": set(
        "poe_id poe_seq subject_id hadm_id ordertime order_type order_subtype "
        "transaction_type discontinue_of_poe_id discontinued_by_poe_id "
        "order_provider_id order_status".split()
    ),
    "medrecon": set(
        "subject_id stay_id charttime name gsn ndc etc_rn etccode "
        "etcdescription".split()
    ),
    "pyxis": set("subject_id stay_id charttime med_rn name gsn_rn gsn".split()),
    "vitalsign": set(
        "subject_id stay_id charttime temperature heartrate resprate o2sat "
        "sbp dbp rhythm pain".split()
    ),
    "omr": set(
        "subject_id chartdate seq_num result_name result_value".split()
    ),
    "chartevents": set(
        "subject_id hadm_id stay_id caregiver_id charttime storetime itemid "
        "value valuenum valueuom warning".split()
    ),
    "datetimeevents": set(
        "subject_id hadm_id stay_id caregiver_id charttime storetime itemid "
        "value valueuom warning".split()
    ),
    "labevents": set(
        "labevent_id subject_id hadm_id specimen_id itemid order_provider_id "
        "charttime storetime value valuenum valueuom ref_range_lower "
        "ref_range_upper flag priority comments".split()
    ),
    "outputevents": set(
        "subject_id hadm_id stay_id caregiver_id charttime storetime itemid "
        "value valueuom".split()
    ),
    "inputevents": set(
        "subject_id hadm_id stay_id caregiver_id starttime endtime storetime "
        "itemid amount amountuom rate rateuom orderid linkorderid "
        "ordercategoryname secondaryordercategoryname "
        "ordercomponenttypedescription ordercategorydescription "
        "patientweight totalamount totalamountuom isopenbag continueinnextdept "
        "statusdescription originalamount originalrate".split()
    ),
    "ingredientevents": set(
        "subject_id hadm_id stay_id caregiver_id starttime endtime storetime "
        "itemid amount amountuom rate rateuom orderid linkorderid "
        "statusdescription originalamount originalrate".split()
    ),
    "procedureevents": set(
        "subject_id hadm_id stay_id caregiver_id starttime endtime storetime "
        "itemid value valueuom location locationcategory orderid linkorderid "
        "ordercategoryname ordercategorydescription patientweight isopenbag "
        "continueinnextdept statusdescription originalamount originalrate".split()
    ),
    "emar": set(
        "subject_id hadm_id emar_id emar_seq poe_id pharmacy_id "
        "enter_provider_id charttime medication event_txt scheduletime "
        "storetime".split()
    ),
    "microbiologyevents": set(
        "microevent_id subject_id hadm_id micro_specimen_id order_provider_id "
        "chartdate charttime spec_itemid spec_type_desc test_seq storedate "
        "storetime test_itemid test_name org_itemid org_name isolate_num "
        "quantity ab_itemid ab_name dilution_text dilution_comparison "
        "dilution_value interpretation comments".split()
    ),
    "radiology": set(
        "note_id subject_id hadm_id note_type note_seq charttime storetime text".split()
    ),
    "discharge": set(
        "note_id subject_id hadm_id note_type note_seq charttime storetime text".split()
    ),
    "discharge_detail": set(
        "note_id subject_id field_name field_value field_ordinal".split()
    ),
    "radiology_detail": set(
        "note_id subject_id field_name field_value field_ordinal".split()
    ),
    "poe_detail": set(
        "poe_id poe_seq subject_id field_name field_value".split()
    ),
    "emar_detail": set(
        "subject_id emar_id emar_seq parent_field_ordinal administration_type "
        "pharmacy_id barcode_type reason_for_no_barcode "
        "complete_dose_not_given dose_due dose_due_unit dose_given "
        "dose_given_unit will_remainder_of_dose_be_given product_amount_given "
        "product_unit product_code product_description "
        "product_description_other prior_infusion_rate infusion_rate "
        "infusion_rate_adjustment infusion_rate_adjustment_amount "
        "infusion_rate_unit route infusion_complete completion_interval "
        "new_iv_bag_hung continued_infusion_in_other_location restart_interval "
        "side site non_formulary_visual_verification".split()
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build cutoff-specific, leak-resistant AgentEHR SQLite snapshots."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="AgentEHR data root containing database/patient_<subject_id>.db.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="JSON benchmark manifest with subject_id and prediction_time.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Destination data root. Safe DBs are written under database/.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Build only the first N unique subjects (smoke testing).",
    )
    parser.add_argument(
        "--unknown-table-policy",
        choices=("error", "drop"),
        default="error",
        help="Fail or omit tables not covered by an explicit policy.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip snapshot DBs that already exist. Without this flag, they are errors.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Atomically replace existing snapshot DBs.",
    )
    parser.add_argument(
        "--copy-runtime-assets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Copy candidate/reference DBs and table_description into output root.",
    )
    parser.add_argument(
        "--report-name",
        default="strict_snapshot_report.json",
        help="Build report filename under output-root.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError(f"Manifest must contain a JSON array: {path}")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Manifest row {index} is not an object.")
        for key in ("subject_id", "prediction_time"):
            if key not in row:
                raise ValueError(f"Manifest row {index} is missing {key!r}.")
        try:
            cutoff = pd.Timestamp(row["prediction_time"])
        except Exception as exc:
            raise ValueError(
                f"Manifest row {index} has invalid prediction_time "
                f"{row['prediction_time']!r}."
            ) from exc
        if pd.isna(cutoff):
            raise ValueError(
                f"Manifest row {index} has invalid prediction_time "
                f"{row['prediction_time']!r}."
            )
    return rows


def unique_subject_cutoffs(
    rows: Iterable[dict[str, Any]],
) -> list[tuple[str, pd.Timestamp]]:
    """Return manifest-order unique subjects and reject ambiguous cutoffs."""

    by_subject: dict[str, pd.Timestamp] = {}
    ordered: list[tuple[str, pd.Timestamp]] = []
    for row in rows:
        subject_id = str(row["subject_id"]).strip()
        cutoff = pd.Timestamp(row["prediction_time"])
        previous = by_subject.get(subject_id)
        if previous is not None and previous != cutoff:
            raise ValueError(
                f"Subject {subject_id} has multiple prediction cutoffs "
                f"({previous} and {cutoff}). A single patient_<subject_id>.db "
                "cannot safely represent both. Split the manifest by cutoff or "
                "update the runtime to address cutoff-specific snapshot IDs."
            )
        if previous is None:
            by_subject[subject_id] = cutoff
            ordered.append((subject_id, cutoff))
    return ordered


def selected_manifest_rows(
    rows: list[dict[str, Any]],
    snapshots: list[tuple[str, pd.Timestamp]],
) -> list[dict[str, Any]]:
    """Keep only manifest rows represented by the selected physical snapshots."""

    selected = {
        (subject_id, pd.Timestamp(cutoff)) for subject_id, cutoff in snapshots
    }
    return [
        row
        for row in rows
        if (str(row["subject_id"]).strip(), pd.Timestamp(row["prediction_time"]))
        in selected
    ]


def clean_missing_values(df: pd.DataFrame) -> pd.DataFrame:
    """Convert pandas/CSV sentinel strings to real SQL NULL values."""

    out = df.copy()
    for column in out.columns:
        series = out[column]
        missing = series.isna()
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            missing |= series.astype(str).str.strip().str.lower().isin(MISSING_STRINGS)
        out.loc[missing, column] = None
    return out


def parse_time_column(df: pd.DataFrame, column: str) -> pd.Series:
    """Parse one timestamp column using conservative date-only semantics."""

    if column not in df.columns:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    values = df[column]
    parsed = pd.to_datetime(values, errors="coerce", format="mixed")
    if column.lower() in DATE_ONLY_COLUMNS:
        parsed = parsed.dt.normalize() + pd.Timedelta(
            hours=23, minutes=59, seconds=59, microseconds=999999
        )
    return parsed


def coalesce_times(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Use the first parseable timestamp in ``columns`` for each row."""

    result = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    for column in columns:
        parsed = parse_time_column(df, column)
        result = result.fillna(parsed)
    return result


def latest_times(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Return the latest parseable availability timestamp per row."""

    parsed = [
        parse_time_column(df, column).rename(column)
        for column in columns
        if column in df.columns
    ]
    if not parsed:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    return pd.concat(parsed, axis=1).max(axis=1)


def nonempty_mask(series: pd.Series) -> pd.Series:
    return series.notna() & ~series.astype(str).str.strip().str.lower().isin(
        MISSING_STRINGS
    )


def redact_field(
    df: pd.DataFrame,
    guard: FieldGuard,
    cutoff: pd.Timestamp,
) -> int:
    """NULL a field unless its field-specific availability is pre-cutoff."""

    if guard.field not in df.columns:
        return 0
    available = coalesce_times(df, guard.available_at)
    unsafe = nonempty_mask(df[guard.field]) & (
        available.isna() | (available >= cutoff)
    )
    count = int(unsafe.sum())
    if count:
        df.loc[unsafe, guard.field] = None
    return count


def sanitize_all_timestamp_values(
    df: pd.DataFrame,
    cutoff: pd.Timestamp,
) -> tuple[int, int]:
    """Remove any remaining future or unparseable timestamp-like value."""

    future_count = 0
    unparseable_count = 0
    for column in df.columns:
        name = column.lower()
        if "time" not in name and "date" not in name and name != "dod":
            continue
        present = nonempty_mask(df[column])
        parsed = parse_time_column(df, column)
        future = present & parsed.notna() & (parsed >= cutoff)
        unparseable = present & parsed.isna()
        if future.any():
            df.loc[future, column] = None
            future_count += int(future.sum())
        if unparseable.any():
            df.loc[unparseable, column] = None
            unparseable_count += int(unparseable.sum())
    return future_count, unparseable_count


def apply_table_policy(
    table_name: str,
    source: pd.DataFrame,
    policy: TablePolicy,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, TableReport]:
    df = clean_missing_values(source)
    report = TableReport(
        input_rows=len(df),
        policy=policy.note or "Explicit table policy.",
    )

    if policy.event_at:
        event_time = coalesce_times(df, policy.event_at)
        keep = event_time.notna() & (event_time < cutoff)
        df = df.loc[keep].copy()

    if policy.available_at and not df.empty:
        available_time = latest_times(df, policy.available_at)
        keep = available_time.notna() & (available_time < cutoff)
        df = df.loc[keep].copy()

    for guard in policy.guards:
        report.redacted_values += redact_field(df, guard, cutoff)

    for column in policy.drop_columns:
        if column in df.columns:
            report.redacted_values += int(nonempty_mask(df[column]).sum())
            df = df.drop(columns=[column])
            report.dropped_columns.append(column)

    future, unparseable = sanitize_all_timestamp_values(df, cutoff)
    report.future_time_values_redacted = future
    report.unparseable_time_values_redacted = unparseable
    report.redacted_values += future + unparseable
    report.output_rows = len(df)
    report.dropped_rows = report.input_rows - report.output_rows
    return df.reset_index(drop=True), report


def filter_child_table(
    table_name: str,
    source: pd.DataFrame,
    policy: ChildPolicy,
    safe_tables: dict[str, pd.DataFrame],
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, TableReport]:
    child = clean_missing_values(source)
    report = TableReport(
        input_rows=len(child),
        policy=policy.note or f"Parent-filtered through {policy.parent}.",
    )
    parent = safe_tables.get(policy.parent)
    if parent is None:
        safe = child.iloc[0:0].copy()
    elif not all(key in child.columns and key in parent.columns for key in policy.keys):
        safe = child.iloc[0:0].copy()
    else:
        parent_keys = parent[list(policy.keys)].dropna().drop_duplicates()
        parent_keys = parent_keys.assign(__strict_parent_visible=1)
        merged = child.merge(parent_keys, on=list(policy.keys), how="left")
        safe = merged[merged["__strict_parent_visible"] == 1].drop(
            columns=["__strict_parent_visible"]
        )
        safe = safe[list(child.columns)]

    future, unparseable = sanitize_all_timestamp_values(safe, cutoff)
    report.future_time_values_redacted = future
    report.unparseable_time_values_redacted = unparseable
    report.redacted_values = future + unparseable
    report.output_rows = len(safe)
    report.dropped_rows = report.input_rows - report.output_rows
    return safe.reset_index(drop=True), report


def read_patient_tables(db_path: Path) -> dict[str, pd.DataFrame]:
    tables: dict[str, pd.DataFrame] = {}
    with sqlite3.connect(db_path) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table_name in names:
            escaped = table_name.replace('"', '""')
            tables[table_name] = pd.read_sql_query(
                f'SELECT * FROM "{escaped}"', conn
            )
    return tables


def read_patient_schema(db_path: Path) -> dict[str, set[str]]:
    """Read table/column names without loading any patient rows."""

    schema: dict[str, set[str]] = {}
    with sqlite3.connect(db_path) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table_name in names:
            escaped = table_name.replace('"', '""')
            schema[table_name] = {
                row[1]
                for row in conn.execute(f'PRAGMA table_info("{escaped}")')
            }
    return schema


def validate_source_schema(
    schema: dict[str, set[str]],
    unknown_table_policy: str,
    context: str = "source database",
) -> None:
    """Fail closed on unknown tables/columns or missing temporal keys."""

    known = set(TABLE_POLICIES) | set(DROP_TABLES) | set(CHILD_POLICIES)
    unknown = sorted(set(schema) - known)
    if unknown and unknown_table_policy == "error":
        raise ValueError(
            f"{context}: no strict temporal policy exists for table(s): "
            + ", ".join(unknown)
        )

    for table_name, columns in schema.items():
        expected = EXPECTED_SOURCE_COLUMNS.get(table_name)
        if expected is None:
            # Fully dropped/unknown tables cannot expose any of their columns.
            continue
        unexpected_columns = sorted(columns - expected)
        if unexpected_columns:
            raise ValueError(
                f"{context}, {table_name}: column(s) have no strict temporal "
                "policy: "
                + ", ".join(unexpected_columns)
            )
        policy = TABLE_POLICIES.get(table_name)
        if policy is not None:
            if policy.event_at and not set(policy.event_at) & columns:
                raise ValueError(
                    f"{context}, {table_name}: none of the required event "
                    f"timestamp columns are present: {policy.event_at}"
                )
            if policy.available_at and not set(policy.available_at) & columns:
                raise ValueError(
                    f"{context}, {table_name}: none of the required "
                    "availability timestamp columns are present: "
                    f"{policy.available_at}"
                )
        child_policy = CHILD_POLICIES.get(table_name)
        if child_policy is not None:
            missing_keys = sorted(set(child_policy.keys) - columns)
            if missing_keys:
                raise ValueError(
                    f"{context}, {table_name}: missing parent-link column(s): "
                    + ", ".join(missing_keys)
                )


def build_safe_tables(
    source_tables: dict[str, pd.DataFrame],
    cutoff: pd.Timestamp,
    unknown_table_policy: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, TableReport]]:
    safe_tables: dict[str, pd.DataFrame] = {}
    reports: dict[str, TableReport] = {}

    validate_source_schema(
        {
            table_name: set(source.columns)
            for table_name, source in source_tables.items()
        },
        unknown_table_policy,
    )

    # Direct/static tables must be built before dependent detail tables.
    for table_name, source in source_tables.items():
        if table_name in DROP_TABLES:
            reports[table_name] = TableReport(
                input_rows=len(source),
                output_rows=0,
                dropped_rows=len(source),
                policy="Dropped: " + DROP_TABLES[table_name],
            )
            continue
        if table_name in CHILD_POLICIES:
            continue
        policy = TABLE_POLICIES.get(table_name)
        if policy is None:
            reports[table_name] = TableReport(
                input_rows=len(source),
                output_rows=0,
                dropped_rows=len(source),
                policy="Dropped: unknown table under fail-closed policy.",
            )
            continue
        safe, report = apply_table_policy(table_name, source, policy, cutoff)
        safe_tables[table_name] = safe
        reports[table_name] = report

    for table_name, policy in CHILD_POLICIES.items():
        source = source_tables.get(table_name)
        if source is None:
            continue
        safe, report = filter_child_table(
            table_name, source, policy, safe_tables, cutoff
        )
        safe_tables[table_name] = safe
        reports[table_name] = report

    return safe_tables, reports


def key_tuples(df: pd.DataFrame, keys: tuple[str, ...]) -> set[tuple[str, ...]]:
    if not all(key in df.columns for key in keys):
        return set()
    values = df[list(keys)].dropna()
    return {
        tuple(str(value).strip() for value in row)
        for row in values.itertuples(index=False, name=None)
    }


def audit_safe_tables(
    safe_tables: dict[str, pd.DataFrame],
    cutoff: pd.Timestamp,
) -> list[str]:
    violations: list[str] = []

    allowed = set(TABLE_POLICIES) | set(CHILD_POLICIES)
    unknown = sorted(set(safe_tables) - allowed - set(DROP_TABLES))
    if unknown:
        violations.append(
            "Tables without an allowed strict output policy are present: "
            + ", ".join(unknown)
        )

    for dropped in DROP_TABLES:
        if dropped in safe_tables:
            violations.append(f"Dropped table unexpectedly present: {dropped}")

    for table_name, df in safe_tables.items():
        expected = EXPECTED_SOURCE_COLUMNS.get(table_name)
        if expected is None:
            violations.append(
                f"{table_name}: no strict output column allowlist exists"
            )
        else:
            unexpected_columns = sorted(set(df.columns) - expected)
            if unexpected_columns:
                violations.append(
                    f"{table_name}: output column(s) have no strict policy: "
                    + ", ".join(unexpected_columns)
                )
        policy = TABLE_POLICIES.get(table_name)
        if policy is not None:
            forbidden_output = sorted(set(policy.drop_columns) & set(df.columns))
            if forbidden_output:
                violations.append(
                    f"{table_name}: columns required to be dropped are present: "
                    + ", ".join(forbidden_output)
                )
        forbidden = sorted(FORBIDDEN_EHR_COLUMNS & set(df.columns))
        if forbidden:
            violations.append(
                f"{table_name}: forbidden benchmark columns present: {forbidden}"
            )
        for column in df.columns:
            name = column.lower()
            if "time" not in name and "date" not in name and name != "dod":
                continue
            present = nonempty_mask(df[column])
            parsed = parse_time_column(df, column)
            unparseable = present & parsed.isna()
            future = present & parsed.notna() & (parsed >= cutoff)
            if unparseable.any():
                violations.append(
                    f"{table_name}.{column}: {int(unparseable.sum())} "
                    "unparseable timestamp value(s)"
                )
            if future.any():
                violations.append(
                    f"{table_name}.{column}: {int(future.sum())} "
                    "value(s) at/after cutoff"
                )

    admissions = safe_tables.get("admissions")
    if admissions is not None:
        if "text" in admissions.columns:
            violations.append("admissions.text must not exist in a strict snapshot")
        if "dischtime" in admissions.columns:
            discharged = parse_time_column(admissions, "dischtime").notna()
            for field_name in ("discharge_location", "hospital_expire_flag"):
                if field_name in admissions.columns:
                    orphaned = nonempty_mask(admissions[field_name]) & ~discharged
                    if orphaned.any():
                        violations.append(
                            f"admissions.{field_name}: {int(orphaned.sum())} "
                            "value(s) visible without a safe dischtime"
                        )

    for child_name, policy in CHILD_POLICIES.items():
        child = safe_tables.get(child_name)
        if child is None or child.empty:
            continue
        parent = safe_tables.get(policy.parent)
        if parent is None:
            violations.append(
                f"{child_name}: rows exist but parent {policy.parent} is absent"
            )
            continue
        missing_child_keys = sorted(set(policy.keys) - set(child.columns))
        missing_parent_keys = sorted(set(policy.keys) - set(parent.columns))
        if missing_child_keys or missing_parent_keys:
            violations.append(
                f"{child_name}: missing link keys; child={missing_child_keys}, "
                f"parent={missing_parent_keys}"
            )
            continue
        parent_keys = key_tuples(parent, policy.keys)
        child_keys = key_tuples(child, policy.keys)
        orphaned = child_keys - parent_keys
        if orphaned:
            violations.append(
                f"{child_name}: {len(orphaned)} parent key(s) are not visible"
            )

    return violations


def write_snapshot_atomic(
    output_db: Path,
    safe_tables: dict[str, pd.DataFrame],
    overwrite: bool,
) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    if output_db.exists() and not overwrite:
        raise FileExistsError(
            f"Snapshot already exists: {output_db}. Use --resume or --overwrite."
        )

    handle = tempfile.NamedTemporaryFile(
        prefix=f".{output_db.name}.",
        suffix=".tmp",
        dir=output_db.parent,
        delete=False,
    )
    temp_path = Path(handle.name)
    handle.close()
    try:
        with sqlite3.connect(temp_path) as conn:
            for table_name in sorted(safe_tables):
                safe_tables[table_name].to_sql(
                    table_name,
                    conn,
                    index=False,
                    if_exists="replace",
                )
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(
                    f"SQLite integrity check failed for temporary snapshot: {result}"
                )
        os.replace(temp_path, output_db)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def copy_runtime_assets(source_root: Path, output_root: Path) -> None:
    source_database = source_root / "database"
    output_database = output_root / "database"
    output_database.mkdir(parents=True, exist_ok=True)
    for name in ("candidate_table.db", "reference_table.db"):
        source = source_database / name
        if source.exists():
            shutil.copy2(source, output_database / name)

    source_descriptions = source_root / "table_description"
    if source_descriptions.exists():
        shutil.copytree(
            source_descriptions,
            output_root / "table_description",
            dirs_exist_ok=True,
        )


def manifest_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    """Write JSON metadata without exposing a partially written file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def serializable_report(report: TableReport) -> dict[str, Any]:
    return {
        "input_rows": report.input_rows,
        "output_rows": report.output_rows,
        "dropped_rows": report.dropped_rows,
        "redacted_values": report.redacted_values,
        "unparseable_time_values_redacted": (
            report.unparseable_time_values_redacted
        ),
        "future_time_values_redacted": report.future_time_values_redacted,
        "dropped_columns": report.dropped_columns,
        "policy": report.policy,
    }


def main() -> None:
    args = parse_args()
    source_root = args.source_root.resolve()
    manifest_path = args.manifest.resolve()
    output_root = args.output_root.resolve()

    if source_root == output_root:
        raise ValueError("--output-root must differ from --source-root.")
    if args.resume and args.overwrite:
        raise ValueError("--resume and --overwrite are mutually exclusive.")
    report_relative = Path(args.report_name)
    if (
        report_relative.is_absolute()
        or report_relative.name != args.report_name
        or args.report_name in {"", ".", "..", "strict_manifest.json"}
    ):
        raise ValueError(
            "--report-name must be a plain filename other than "
            "strict_manifest.json."
        )
    if not (source_root / "database").is_dir():
        raise FileNotFoundError(
            f"Source database directory not found: {source_root / 'database'}"
        )
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest_rows = load_manifest(manifest_path)
    snapshots = unique_subject_cutoffs(manifest_rows)
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be a positive integer.")
        snapshots = snapshots[: args.limit]
    output_manifest_rows = selected_manifest_rows(manifest_rows, snapshots)

    print(f"Preflight: validating {len(snapshots)} source database schema(s)")
    for subject_id, _ in snapshots:
        source_db = source_root / "database" / f"patient_{subject_id}.db"
        if not source_db.is_file():
            raise FileNotFoundError(f"Patient database not found: {source_db}")
        validate_source_schema(
            read_patient_schema(source_db),
            args.unknown_table_policy,
            context=f"subject {subject_id}",
        )

    output_root.mkdir(parents=True, exist_ok=True)
    if args.copy_runtime_assets:
        copy_runtime_assets(source_root, output_root)

    build_report: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "source_root": str(source_root),
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha256(manifest_path),
        "output_root": str(output_root),
        "unknown_table_policy": args.unknown_table_policy,
        "requested_snapshots": len(snapshots),
        "built_snapshots": 0,
        "skipped_snapshots": 0,
        "snapshots": [],
    }

    for position, (subject_id, cutoff) in enumerate(snapshots, start=1):
        source_db = source_root / "database" / f"patient_{subject_id}.db"
        output_db = output_root / "database" / f"patient_{subject_id}.db"
        if not source_db.is_file():
            raise FileNotFoundError(f"Patient database not found: {source_db}")
        if output_db.exists() and args.resume:
            print(
                f"[{position}/{len(snapshots)}] audit existing subject "
                f"{subject_id}"
            )
            existing_tables = read_patient_tables(output_db)
            violations = audit_safe_tables(existing_tables, cutoff)
            if violations:
                joined = "\n  - ".join(violations)
                raise RuntimeError(
                    f"Strict audit failed for existing subject {subject_id}:\n"
                    f"  - {joined}"
                )
            build_report["skipped_snapshots"] += 1
            build_report["snapshots"].append(
                {
                    "subject_id": subject_id,
                    "prediction_time": str(cutoff),
                    "database": str(output_db.relative_to(output_root)),
                    "status": "existing_snapshot_audited",
                    "tables_written": len(existing_tables),
                    "audit_violations": [],
                }
            )
            continue

        print(
            f"[{position}/{len(snapshots)}] build subject={subject_id} "
            f"cutoff={cutoff}"
        )
        source_tables = read_patient_tables(source_db)
        safe_tables, table_reports = build_safe_tables(
            source_tables,
            cutoff,
            args.unknown_table_policy,
        )
        violations = audit_safe_tables(safe_tables, cutoff)
        if violations:
            joined = "\n  - ".join(violations)
            raise RuntimeError(
                f"Strict audit failed for subject {subject_id}:\n  - {joined}"
            )

        write_snapshot_atomic(output_db, safe_tables, args.overwrite)
        build_report["built_snapshots"] += 1
        build_report["snapshots"].append(
            {
                "subject_id": subject_id,
                "prediction_time": str(cutoff),
                "database": str(output_db.relative_to(output_root)),
                "status": "built",
                "tables_written": len(safe_tables),
                "audit_violations": [],
                "tables": {
                    name: serializable_report(report)
                    for name, report in sorted(table_reports.items())
                },
            }
        )

    strict_manifest = output_root / "strict_manifest.json"
    write_json_atomic(strict_manifest, output_manifest_rows)

    report_path = output_root / args.report_name
    write_json_atomic(report_path, build_report)

    print(
        f"Completed: built={build_report['built_snapshots']} "
        f"skipped={build_report['skipped_snapshots']} "
        f"report={report_path}"
    )


if __name__ == "__main__":
    main()
