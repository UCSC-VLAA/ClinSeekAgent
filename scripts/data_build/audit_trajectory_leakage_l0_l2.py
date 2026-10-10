#!/usr/bin/env python3
"""Deterministically audit L0-L2 temporal leakage in EHR trajectories.

Levels
------
L0 available
    Confirmed future information survives the original EHRManager cutoff.
L1 accessed
    A recorded tool call accesses a table that contains an L0 item for the
    sample. This is an access upper bound, not proof that a value was returned.
L2 exposed
    The recorded tool observation is reproduced from the original runtime
    view, and replaying the same call against a view with confirmed leakage
    removed changes the observation.

No language model is used.  The script reproduces the original first-time-
column loader, the patient/reference joins, the record tools' deterministic
formatting, and the configured tool-result truncation.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


MISSING_STRINGS = {"", "nan", "nat", "none", "null", "<na>"}
DATE_ONLY_COLUMNS = {"chartdate", "storedate", "dod", "expirationdate"}
SUPPORTED_REPLAY_TOOLS = {
    "ehr.get_records_by_time",
    "ehr.get_event_counts_by_time",
    "ehr.get_latest_records",
    "ehr.get_records_by_keyword",
    "ehr.get_records_by_value",
    "ehr.run_sql_query",
    "ehr.get_unique_values",
}

AVAILABILITY_FIELDS: dict[str, tuple[str, ...]] = {
    "chartevents": ("storetime",),
    "datetimeevents": ("storetime",),
    "labevents": ("storetime",),
    "outputevents": ("storetime",),
    "inputevents": ("storetime",),
    "ingredientevents": ("storetime",),
    "procedureevents": ("storetime",),
    "emar": ("storetime",),
    "microbiologyevents": ("storedate", "storetime"),
    "radiology": ("storetime",),
    "discharge": ("storetime",),
}

OUTCOME_FIELDS: dict[str, dict[str, str]] = {
    "admissions": {
        "dischtime": "dischtime",
        "deathtime": "deathtime",
        "edouttime": "edouttime",
        "discharge_location": "dischtime",
        "hospital_expire_flag": "dischtime",
    },
    "transfers": {"outtime": "outtime"},
    "edstays": {
        "outtime": "outtime",
        "disposition": "outtime",
    },
    "icustays": {
        "outtime": "outtime",
        "los": "outtime",
        "last_careunit": "outtime",
    },
}

CHILD_SPECS: tuple[
    tuple[str, tuple[str, ...], str, tuple[str, ...]], ...
] = (
    (
        "discharge_detail",
        ("note_id",),
        "discharge",
        ("charttime", "storetime"),
    ),
    (
        "radiology_detail",
        ("note_id",),
        "radiology",
        ("charttime", "storetime"),
    ),
    (
        "poe_detail",
        ("poe_id", "poe_seq"),
        "poe",
        ("ordertime",),
    ),
    (
        "emar_detail",
        ("emar_id",),
        "emar",
        ("charttime", "storetime"),
    ),
    (
        "triage",
        ("stay_id",),
        "edstays",
        ("intime",),
    ),
    (
        "drgcodes",
        ("hadm_id",),
        "admissions",
        ("dischtime",),
    ),
)

SYNTHETIC_SPECS: tuple[
    tuple[str, tuple[str, ...], str, tuple[str, ...]], ...
] = (
    (
        "diagnoses_icd",
        ("hadm_id",),
        "admissions",
        ("dischtime",),
    ),
    (
        "diagnosis",
        ("stay_id",),
        "edstays",
        ("outtime",),
    ),
)


@dataclass
class L0State:
    confirmed_counts: collections.Counter[str] = field(
        default_factory=collections.Counter
    )
    high_risk_counts: collections.Counter[str] = field(
        default_factory=collections.Counter
    )
    table_categories: dict[str, set[str]] = field(
        default_factory=lambda: collections.defaultdict(set)
    )

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_counts)

    @property
    def high_risk(self) -> bool:
        return bool(self.high_risk_counts)

    def add_confirmed(
        self, category: str, table: str, detail: str, count: int
    ) -> None:
        if count <= 0:
            return
        self.confirmed_counts[f"{category}:{table}.{detail}"] += int(count)
        self.table_categories[table].add(category)

    def add_high_risk(
        self, category: str, table: str, detail: str, count: int
    ) -> None:
        if count <= 0:
            return
        self.high_risk_counts[f"{category}:{table}.{detail}"] += int(count)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit deterministic L0-L2 leakage in saved EHR trajectories."
    )
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument(
        "--runtime-assets-root",
        type=Path,
        required=True,
        help=(
            "Data root containing database/{candidate,reference}_table.db and "
            "table_description/link_information.json used by the run."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-tool-result-chars", type=int, default=100_000)
    return parser.parse_args()


def present(series: pd.Series) -> pd.Series:
    return series.notna() & ~series.astype(str).str.strip().str.lower().isin(
        MISSING_STRINGS
    )


def parse_time(series: pd.Series, column: str) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    if column.lower() in DATE_ONLY_COLUMNS:
        parsed = parsed.dt.normalize() + pd.Timedelta(
            hours=23, minutes=59, seconds=59, microseconds=999999
        )
    return parsed


def read_sqlite_tables(path: Path) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    with sqlite3.connect(path) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for name in names:
            escaped = name.replace('"', '""')
            result[name] = pd.read_sql_query(
                f'SELECT * FROM "{escaped}"', connection
            )
    return result


def original_runtime_filter(
    source: pd.DataFrame, cutoff: pd.Timestamp
) -> pd.DataFrame:
    """Reproduce EHRManager's first-time-column and first-ten-values logic."""

    timestamp_column = next(
        (
            column
            for column in source.columns
            if "time" in column.lower() or "date" in column.lower()
        ),
        None,
    )
    if timestamp_column is None:
        return source.copy()

    sample_values = source[timestamp_column].dropna().head(10)
    is_date_only = True
    for value in sample_values:
        if pd.isna(value):
            continue
        value_string = str(value).strip()
        if len(value_string) > 10 and " " in value_string:
            is_date_only = False
            break

    parsed = pd.to_datetime(
        source[timestamp_column], errors="coerce", format="mixed"
    )
    if is_date_only:
        parsed = parsed.dt.normalize() + pd.Timedelta(hours=23, minutes=59)
    result = source.loc[parsed < cutoff].copy()
    result[timestamp_column] = parsed.loc[result.index]
    return result


def build_original_view(
    source_tables: dict[str, pd.DataFrame], cutoff: pd.Timestamp
) -> dict[str, pd.DataFrame]:
    result = {
        table: original_runtime_filter(frame, cutoff)
        for table, frame in source_tables.items()
    }
    if "pharmacy" in result:
        result["pharmacy"] = result["pharmacy"].iloc[0:0].copy()
    return result


def time_by_key(
    frame: pd.DataFrame | None,
    keys: tuple[str, ...],
    time_columns: tuple[str, ...],
) -> dict[tuple[str, ...], pd.Timestamp]:
    if frame is None or not all(key in frame.columns for key in keys):
        return {}
    parsed_columns = [
        parse_time(frame[column], column)
        for column in time_columns
        if column in frame.columns
    ]
    if not parsed_columns:
        return {}
    available = pd.concat(parsed_columns, axis=1).max(axis=1)
    result: dict[tuple[str, ...], pd.Timestamp] = {}
    for position, row in enumerate(frame.itertuples(index=False, name=None)):
        timestamp = available.iloc[position]
        if pd.isna(timestamp):
            continue
        key = tuple(
            str(row[frame.columns.get_loc(column)]).strip() for column in keys
        )
        previous = result.get(key)
        if previous is None or timestamp > previous:
            result[key] = timestamp
    return result


def future_parent_mask(
    child: pd.DataFrame,
    keys: tuple[str, ...],
    parent_times: dict[tuple[str, ...], pd.Timestamp],
    cutoff: pd.Timestamp,
) -> pd.Series:
    if child.empty or not all(key in child.columns for key in keys):
        return pd.Series(False, index=child.index)
    flags = []
    for values in child[list(keys)].itertuples(index=False, name=None):
        key = tuple(str(value).strip() for value in values)
        parent_time = parent_times.get(key)
        flags.append(parent_time is not None and parent_time >= cutoff)
    return pd.Series(flags, index=child.index)


def sanitize_confirmed_leakage(
    original_view: dict[str, pd.DataFrame],
    source_tables: dict[str, pd.DataFrame],
    cutoff: pd.Timestamp,
) -> tuple[dict[str, pd.DataFrame], L0State]:
    """Remove only confirmed leakage; keep separately reported high-risk data."""

    safe = {table: frame.copy() for table, frame in original_view.items()}
    state = L0State()
    removal_masks = {
        table: pd.Series(False, index=frame.index)
        for table, frame in original_view.items()
    }

    for table, fields in OUTCOME_FIELDS.items():
        frame = original_view.get(table)
        if frame is None or frame.empty:
            continue
        for field_name, available_at in fields.items():
            if field_name not in frame or available_at not in frame:
                continue
            availability = parse_time(frame[available_at], available_at)
            mask = (
                present(frame[field_name])
                & availability.notna()
                & (availability >= cutoff)
            )
            count = int(mask.sum())
            state.add_confirmed(
                "future_outcome", table, field_name, count
            )
            if count:
                safe[table].loc[mask, field_name] = float("nan")

    patients = original_view.get("patients")
    if patients is not None and "dod" in patients:
        dod = parse_time(patients["dod"], "dod")
        mask = present(patients["dod"]) & dod.notna() & (dod >= cutoff)
        count = int(mask.sum())
        state.add_confirmed("future_outcome", "patients", "dod", count)
        if count:
            safe["patients"].loc[mask, "dod"] = float("nan")

    admissions = original_view.get("admissions")
    if admissions is not None and "text" in admissions:
        discharge_times = time_by_key(
            source_tables.get("discharge"),
            ("hadm_id",),
            ("charttime", "storetime"),
        )
        flags: list[bool] = []
        for row in admissions.itertuples(index=False):
            text_value = getattr(row, "text", None)
            if str(text_value).strip().lower() in MISSING_STRINGS:
                flags.append(False)
                continue
            key = (str(getattr(row, "hadm_id", "")).strip(),)
            note_time = discharge_times.get(key)
            discharge_time = pd.to_datetime(
                getattr(row, "dischtime", None), errors="coerce"
            )
            flags.append(
                (note_time is not None and note_time >= cutoff)
                or (
                    pd.notna(discharge_time)
                    and discharge_time >= cutoff
                )
            )
        mask = pd.Series(flags, index=admissions.index)
        count = int(mask.sum())
        state.add_confirmed("future_outcome", "admissions", "text", count)
        if count:
            safe["admissions"].loc[mask, "text"] = float("nan")

    for table, availability_columns in AVAILABILITY_FIELDS.items():
        frame = original_view.get(table)
        if frame is None or frame.empty:
            continue
        union = pd.Series(False, index=frame.index)
        for column in availability_columns:
            if column not in frame:
                continue
            parsed = parse_time(frame[column], column)
            mask = present(frame[column]) & parsed.notna() & (parsed >= cutoff)
            state.add_confirmed(
                "late_availability", table, column, int(mask.sum())
            )
            union |= mask
        removal_masks[table] |= union

    micro = original_view.get("microbiologyevents")
    if micro is not None and "charttime" in micro:
        charttime = parse_time(micro["charttime"], "charttime")
        mask = charttime.notna() & (charttime >= cutoff)
        state.add_confirmed(
            "future_event_time",
            "microbiologyevents",
            "charttime",
            int(mask.sum()),
        )
        removal_masks["microbiologyevents"] |= mask

    for child_name, keys, parent_name, parent_time_columns in CHILD_SPECS:
        child = original_view.get(child_name)
        if child is None or child.empty:
            continue
        parent_times = time_by_key(
            source_tables.get(parent_name), keys, parent_time_columns
        )
        mask = future_parent_mask(child, keys, parent_times, cutoff)
        state.add_confirmed(
            "future_parent", child_name, parent_name, int(mask.sum())
        )
        removal_masks[child_name] |= mask

    for table, keys, parent_name, parent_time_columns in SYNTHETIC_SPECS:
        frame = original_view.get(table)
        if frame is None or frame.empty:
            continue
        parent_times = time_by_key(
            source_tables.get(parent_name), keys, parent_time_columns
        )
        mask = future_parent_mask(frame, keys, parent_times, cutoff)
        state.add_confirmed(
            "synthetic_time", table, parent_name, int(mask.sum())
        )
        removal_masks[table] |= mask

    for table, mask in removal_masks.items():
        if mask.any():
            safe[table] = safe[table].loc[~mask].copy()

    prescriptions = original_view.get("prescriptions")
    if prescriptions is not None and "stoptime" in prescriptions:
        stoptime = parse_time(prescriptions["stoptime"], "stoptime")
        mask = (
            present(prescriptions["stoptime"])
            & stoptime.notna()
            & (stoptime >= cutoff)
        )
        state.add_high_risk(
            "future_plan_or_final",
            "prescriptions",
            "stoptime",
            int(mask.sum()),
        )

    emar = original_view.get("emar")
    if emar is not None and "scheduletime" in emar:
        schedule = parse_time(emar["scheduletime"], "scheduletime")
        mask = (
            present(emar["scheduletime"])
            & schedule.notna()
            & (schedule >= cutoff)
        )
        state.add_high_risk(
            "future_plan_or_final",
            "emar",
            "scheduletime",
            int(mask.sum()),
        )

    for table in ("inputevents", "ingredientevents", "procedureevents"):
        frame = original_view.get(table)
        if frame is None or "endtime" not in frame:
            continue
        endtime = parse_time(frame["endtime"], "endtime")
        mask = endtime.notna() & (endtime >= cutoff)
        state.add_high_risk(
            "ongoing_interval", table, "endtime", int(mask.sum())
        )

    admission_end = time_by_key(
        source_tables.get("admissions"), ("hadm_id",), ("dischtime",)
    )
    for table in ("procedures_icd", "hcpcsevents"):
        frame = original_view.get(table)
        if frame is None or frame.empty:
            continue
        mask = future_parent_mask(
            frame, ("hadm_id",), admission_end, cutoff
        )
        state.add_high_risk(
            "current_encounter_admin_code",
            table,
            "dischtime",
            int(mask.sum()),
        )

    return safe, state


def load_link_information(path: Path) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                links.append(json.loads(line))
    return links


def normalize_ndc(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "ndc" in result:
        result["ndc"] = (
            result["ndc"]
            .astype(str)
            .str.split(".", n=1)
            .str[0]
            .str.zfill(11)
        )
    return result


def join_reference_tables(
    tables: dict[str, pd.DataFrame],
    reference_tables: dict[str, pd.DataFrame],
    links: Iterable[dict[str, Any]],
) -> dict[str, pd.DataFrame]:
    result = {name: frame.copy() for name, frame in tables.items()}
    if "prescriptions" in result:
        result["prescriptions"] = normalize_ndc(result["prescriptions"])
    for link in links:
        table_name = link["table_name"]
        reference_name = link["link_table"]
        link_columns = link["link_column"]
        if table_name not in result or reference_name not in reference_tables:
            continue
        try:
            result[table_name] = pd.merge(
                result[table_name],
                reference_tables[reference_name],
                on=link_columns,
                how="left",
            )
        except (KeyError, ValueError):
            # Match EHRManager's practical behavior for unavailable joins by
            # leaving the patient table untouched.
            continue
    # run_mcp_server stores EHRManager.get_ehr_data_json(), which casts every
    # patient cell to string before record tools reconstruct a DataFrame.
    return {name: frame.astype(str) for name, frame in result.items()}


def normalize_datetime(value: Any) -> pd.Timestamp | None:
    try:
        timestamp = pd.to_datetime(value)
        if not timestamp.minute:
            timestamp = timestamp.replace(minute=0)
        if not timestamp.second:
            timestamp = timestamp.replace(second=0)
        return timestamp
    except (ValueError, TypeError):
        return None


def find_timestamp_column(frame: pd.DataFrame) -> str | None:
    return next(
        (
            column
            for column in frame.columns
            if "time" in column.lower() or "date" in column.lower()
        ),
        None,
    )


def truncate_result(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= max_chars else text[:max_chars]


def replay_get_records_by_time(
    frame: pd.DataFrame | None,
    table_name: str,
    start_time: str,
    end_time: str,
) -> str:
    if frame is None:
        return f"Error: Table '{table_name}' not found."
    timestamp_column = find_timestamp_column(frame)
    if timestamp_column is None:
        return (
            f"Error: No timestamp column found in table '{table_name}'. "
            "Cannot filter by time."
        )
    frame = frame.copy()
    try:
        frame[timestamp_column] = pd.to_datetime(
            frame[timestamp_column], format="mixed"
        )
        start_timestamp = normalize_datetime(start_time)
        end_timestamp = normalize_datetime(end_time)
        frame[timestamp_column] = frame[timestamp_column].apply(
            normalize_datetime
        )
        filtered = frame[
            (frame[timestamp_column] >= start_timestamp)
            & (frame[timestamp_column] <= end_timestamp)
        ]
        if filtered.empty:
            return (
                f"No records found in table '{table_name}' between "
                f"{start_time} and {end_time}."
            )
        return filtered.to_string(index=False)
    except Exception as error:
        return f"An error occurred while filtering records: {error}"


def replay_get_latest_records(
    frame: pd.DataFrame | None, table_name: str
) -> str:
    if frame is None:
        return f"Error: Table '{table_name}' not found."
    timestamp_column = find_timestamp_column(frame)
    if timestamp_column is None:
        return (
            f"Error: No timestamp column found in table '{table_name}'. "
            "Cannot find the latest records."
        )
    frame = frame.copy()
    try:
        frame[timestamp_column] = pd.to_datetime(
            frame[timestamp_column], format="mixed"
        )
        latest_timestamp = frame[timestamp_column].max()
        if pd.isna(latest_timestamp):
            return (
                f"No valid timestamps found in table '{table_name}' to "
                "determine the latest records."
            )
        latest = frame[frame[timestamp_column] == latest_timestamp]
        if latest.empty:
            return (
                f"No records found in table '{table_name}' for the latest "
                f"timestamp: {latest_timestamp}."
            )
        return latest.to_string(index=False)
    except Exception as error:
        return f"An error occurred while retrieving the latest records: {error}"


def replay_get_records_by_keyword(
    frame: pd.DataFrame | None, table_name: str, keyword: str
) -> str:
    if frame is None:
        return f"Error: Table '{table_name}' not found."
    text_columns = [
        column
        for column in frame.columns
        if frame[column].dtype in ["object", "string"]
    ]
    if not text_columns:
        return (
            f"Error: No text-based columns found in table '{table_name}' "
            "to search."
        )
    mask = pd.Series(False, index=frame.index)
    for column in text_columns:
        mask |= (
            frame[column]
            .str.lower()
            .str.contains(str(keyword).lower(), na=False)
        )
    matching = frame[mask]
    if matching.empty:
        return (
            f"No records found in table '{table_name}' containing the "
            f"keyword '{keyword}'."
        )
    return matching.to_string(index=False)


def replay_get_records_by_value(
    frame: pd.DataFrame | None,
    table_name: str,
    column_name: str,
    value: str,
) -> str:
    if frame is None:
        return f"Error: Table '{table_name}' not found."
    if column_name not in frame.columns:
        return (
            f"Error: Column '{column_name}' not found in table "
            f"'{table_name}'."
        )
    frame = frame.copy()
    try:
        frame[column_name] = (
            frame[column_name].astype(str).str.strip().fillna("")
        )
        matching = frame[
            frame[column_name] == str(value).strip()
        ]
        if matching.empty:
            return (
                f"No records found in table '{table_name}' where "
                f"'{column_name}' equals '{value}'."
            )
        return matching.to_string(index=False)
    except Exception as error:
        return f"An error occurred while searching for records: {error}"


def replay_event_counts(
    tables: dict[str, pd.DataFrame], start_time: str, end_time: str
) -> str:
    start_timestamp = normalize_datetime(start_time)
    end_timestamp = normalize_datetime(end_time)
    if start_timestamp is None or end_timestamp is None:
        return (
            "Error: Start time or end time format is invalid. Please use a "
            "format like 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."
        )
    lines = [f"Event counts between {start_time} and {end_time}:"]
    for table_name in tables:
        frame = tables[table_name].copy()
        timestamp_column = find_timestamp_column(frame)
        if timestamp_column is None:
            lines.append(
                f"- {table_name}: Could not find a timestamp column."
            )
            continue
        try:
            frame[timestamp_column] = frame[timestamp_column].apply(
                normalize_datetime
            )
            filtered = frame[
                (frame[timestamp_column] >= start_timestamp)
                & (frame[timestamp_column] <= end_timestamp)
            ].dropna(subset=[timestamp_column])
            lines.append(f"- {table_name}: {len(filtered)} events")
        except Exception:
            lines.append(
                f"- {table_name}: An error occurred while processing this table."
            )
    return "\n".join(lines)


def replay_unique_values(
    frame: pd.DataFrame | None, table_name: str, column_name: str
) -> str:
    if frame is None:
        return f"Error: Table '{table_name}' not found in EHR table list."
    if column_name not in frame.columns:
        return (
            f"Error: Column '{column_name}' not found in table '{table_name}'."
        )
    try:
        values = frame[column_name].dropna().unique()
        if len(values) > 50:
            return (
                f"Error: The column '{column_name}' has {len(values)} unique "
                "values, which is too many to display. This tool is intended "
                "for categorical columns with fewer than 50 options. Here are "
                "the first 10 examples: "
                + str(values[:10].tolist())
            )
        try:
            sorted_values = sorted(values)
        except Exception:
            sorted_values = values
        return str(list(sorted_values))
    except Exception as error:
        return f"An error occurred while retrieving unique values: {error}"


def referenced_tables(
    tool_name: str,
    arguments: dict[str, Any],
    patient_tables: Iterable[str],
) -> set[str]:
    table_names = set(patient_tables)
    if tool_name == "ehr.get_event_counts_by_time":
        return table_names
    direct = arguments.get("table_name")
    if direct in table_names:
        return {str(direct)}
    if tool_name != "ehr.run_sql_query":
        return set()
    sql = str(arguments.get("sql_query", "")).lower()
    return {
        table
        for table in table_names
        if re.search(
            rf"(?<![a-z0-9_]){re.escape(table.lower())}(?![a-z0-9_])",
            sql,
        )
    }


def replay_sql(
    sql_query: str,
    patient_tables: dict[str, pd.DataFrame],
    candidate_tables: dict[str, pd.DataFrame],
) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        loaded = 0
        for table_name, frame in {
            **patient_tables,
            **candidate_tables,
        }.items():
            if table_name.lower() not in sql_query.lower():
                continue
            sql_frame = frame.copy()
            for column in sql_frame.columns:
                if "time" in column.lower() or "date" in column.lower():
                    sql_frame[column] = sql_frame[column].astype(str)
            sql_frame.to_sql(
                table_name,
                connection,
                index=False,
                if_exists="replace",
            )
            loaded += 1
        if not loaded:
            available = list(patient_tables) + list(candidate_tables)
            return (
                "Error: The SQL query does not seem to reference any "
                f"available tables. Available tables: {available}"
            )
        result = pd.read_sql_query(sql_query, connection)
        if result.empty:
            return "Query executed successfully but returned no results."
        return result.to_string(index=False)
    except Exception as error:
        return f"SQL Execution Error: {error}"
    finally:
        connection.close()


def replay_tool(
    tool_name: str,
    arguments: dict[str, Any],
    patient_tables: dict[str, pd.DataFrame],
    candidate_tables: dict[str, pd.DataFrame],
    max_chars: int,
) -> str | None:
    table_name = str(arguments.get("table_name", ""))
    frame = patient_tables.get(table_name)
    if tool_name == "ehr.get_records_by_time":
        result = replay_get_records_by_time(
            frame,
            table_name,
            str(arguments.get("start_time", "")),
            str(arguments.get("end_time", "")),
        )
    elif tool_name == "ehr.get_latest_records":
        result = replay_get_latest_records(frame, table_name)
    elif tool_name == "ehr.get_records_by_keyword":
        result = replay_get_records_by_keyword(
            frame, table_name, str(arguments.get("keyword", ""))
        )
    elif tool_name == "ehr.get_records_by_value":
        result = replay_get_records_by_value(
            frame,
            table_name,
            str(arguments.get("column_name", "")),
            str(arguments.get("value", "")),
        )
    elif tool_name == "ehr.get_event_counts_by_time":
        result = replay_event_counts(
            patient_tables,
            str(arguments.get("start_time", "")),
            str(arguments.get("end_time", "")),
        )
    elif tool_name == "ehr.get_unique_values":
        result = replay_unique_values(
            frame,
            table_name,
            str(arguments.get("column_name", "")),
        )
    elif tool_name == "ehr.run_sql_query":
        result = replay_sql(
            str(arguments.get("sql_query", "")),
            patient_tables,
            candidate_tables,
        )
    else:
        return None
    return truncate_result(result, max_chars)


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def normalize_observation(text: str) -> str:
    return "\n".join(line.rstrip() for line in str(text).strip().splitlines())


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_call_observations(
    messages: list[dict[str, Any]],
) -> Iterable[
    tuple[int, int, str, str, dict[str, Any], str]
]:
    pending: dict[
        str, tuple[int, str, dict[str, Any]]
    ] = {}
    for message_index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            call_id = str(call.get("id", ""))
            function = call.get("function") or {}
            pending[call_id] = (
                message_index,
                str(function.get("name", "")),
                parse_arguments(function.get("arguments")),
            )
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id", ""))
        call_info = pending.get(call_id)
        if call_info is None:
            continue
        call_message_index, tool_name, arguments = call_info
        yield (
            call_message_index,
            message_index,
            call_id,
            tool_name,
            arguments,
            str(message.get("content", "")),
        )


def load_results(path: Path, limit: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            rows.append(json.loads(line))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def load_runtime_assets(
    root: Path,
) -> tuple[
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
    list[dict[str, Any]],
]:
    database_root = root / "database"
    reference = read_sqlite_tables(database_root / "reference_table.db")
    candidate = read_sqlite_tables(database_root / "candidate_table.db")
    if "d_atc_prescriptions" in reference:
        reference["d_atc_prescriptions"] = normalize_ndc(
            reference["d_atc_prescriptions"]
        )
    if "prescriptions_atc_candidates" in candidate:
        candidate["prescriptions_atc_candidates"] = normalize_ndc(
            candidate["prescriptions_atc_candidates"]
        )
    links = load_link_information(
        root / "table_description" / "link_information.json"
    )
    return reference, candidate, links


def json_safe_counter(counter: collections.Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counter.items())}


def write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def main() -> None:
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive.")
    if args.max_tool_result_chars < 1:
        raise ValueError("--max-tool-result-chars must be positive.")

    results_path = args.results.resolve()
    database_root = args.database_root.resolve()
    runtime_assets_root = args.runtime_assets_root.resolve()
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    results = load_results(results_path, args.limit)
    by_subject: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for result in results:
        by_subject[str(result["subject_id"])].append(result)

    reference_tables, candidate_tables, links = load_runtime_assets(
        runtime_assets_root
    )

    per_qid: list[dict[str, Any]] = []
    aggregate = {
        "samples": len(results),
        "subjects": len(by_subject),
        "trajectory_status": collections.Counter(),
        "l0_confirmed": 0,
        "l0_high_risk": 0,
        "l1_accessed": 0,
        "l2_exposed": 0,
        "l1_calls": 0,
        "l2_calls": 0,
        "supported_observations": 0,
        "unsafe_replay_exact": 0,
        "unsafe_replay_normalized": 0,
        "unsafe_replay_mismatch": 0,
        "l0_confirmed_by_key": collections.Counter(),
        "l0_high_risk_by_key": collections.Counter(),
        "l1_by_table": collections.Counter(),
        "l2_by_table": collections.Counter(),
        "l2_by_tool": collections.Counter(),
        "l2_by_category": collections.Counter(),
        "by_task": collections.defaultdict(collections.Counter),
    }

    for subject_position, (subject_id, subject_results) in enumerate(
        by_subject.items(), start=1
    ):
        source_db = database_root / f"patient_{subject_id}.db"
        if not source_db.is_file():
            raise FileNotFoundError(source_db)
        source_tables = read_sqlite_tables(source_db)

        for result in subject_results:
            qid = str(result["qid"])
            cutoff = pd.Timestamp(result["prediction_time"])
            original_raw = build_original_view(source_tables, cutoff)
            safe_raw, l0 = sanitize_confirmed_leakage(
                original_raw, source_tables, cutoff
            )
            original_tables = join_reference_tables(
                original_raw, reference_tables, links
            )
            safe_tables = join_reference_tables(
                safe_raw, reference_tables, links
            )

            l1_calls: list[dict[str, Any]] = []
            l2_calls: list[dict[str, Any]] = []
            replay_counts = collections.Counter()
            l1_tables: set[str] = set()
            l2_tables: set[str] = set()

            for (
                call_message_index,
                observation_message_index,
                call_id,
                tool_name,
                arguments,
                actual_observation,
            ) in iter_call_observations(result.get("messages") or []):
                if tool_name not in SUPPORTED_REPLAY_TOOLS:
                    continue
                aggregate["supported_observations"] += 1
                referenced = referenced_tables(
                    tool_name, arguments, original_tables
                )
                changed_tables = referenced & set(l0.table_categories)
                if changed_tables:
                    l1_tables |= changed_tables
                    aggregate["l1_calls"] += 1
                    for table in changed_tables:
                        aggregate["l1_by_table"][table] += 1
                    l1_calls.append(
                        {
                            "call_id": call_id,
                            "call_message_index": call_message_index,
                            "observation_message_index": (
                                observation_message_index
                            ),
                            "tool": tool_name,
                            "tables": sorted(changed_tables),
                        }
                    )

                unsafe_replay = replay_tool(
                    tool_name,
                    arguments,
                    original_tables,
                    candidate_tables,
                    args.max_tool_result_chars,
                )
                safe_replay = replay_tool(
                    tool_name,
                    arguments,
                    safe_tables,
                    candidate_tables,
                    args.max_tool_result_chars,
                )
                if unsafe_replay is None or safe_replay is None:
                    continue

                actual_normalized = normalize_observation(actual_observation)
                unsafe_normalized = normalize_observation(unsafe_replay)
                safe_normalized = normalize_observation(safe_replay)
                if actual_observation == unsafe_replay:
                    validation = "exact"
                elif actual_normalized == unsafe_normalized:
                    validation = "normalized"
                else:
                    validation = "mismatch"
                replay_counts[validation] += 1
                aggregate[f"unsafe_replay_{validation}"] += 1

                exposed = (
                    validation in {"exact", "normalized"}
                    and unsafe_normalized != safe_normalized
                )
                if not exposed:
                    continue

                aggregate["l2_calls"] += 1
                l2_tables |= changed_tables
                categories = sorted(
                    {
                        category
                        for table in changed_tables
                        for category in l0.table_categories[table]
                    }
                )
                for table in changed_tables:
                    aggregate["l2_by_table"][table] += 1
                for category in categories:
                    aggregate["l2_by_category"][category] += 1
                aggregate["l2_by_tool"][tool_name] += 1
                l2_calls.append(
                    {
                        "call_id": call_id,
                        "call_message_index": call_message_index,
                        "observation_message_index": (
                            observation_message_index
                        ),
                        "tool": tool_name,
                        "tables": sorted(changed_tables),
                        "candidate_categories": categories,
                        "unsafe_observation_sha256": hash_text(
                            unsafe_normalized
                        ),
                        "safe_observation_sha256": hash_text(
                            safe_normalized
                        ),
                        "unsafe_chars": len(actual_observation),
                        "safe_chars": len(safe_replay),
                        "replay_validation": validation,
                    }
                )

            status = str(result.get("status", "unknown"))
            task = str(result.get("task", "unknown"))
            aggregate["trajectory_status"][status] += 1
            task_counts = aggregate["by_task"][task]
            task_counts["samples"] += 1
            if l0.confirmed:
                aggregate["l0_confirmed"] += 1
                task_counts["l0_confirmed"] += 1
            if l0.high_risk:
                aggregate["l0_high_risk"] += 1
                task_counts["l0_high_risk"] += 1
            if l1_calls:
                aggregate["l1_accessed"] += 1
                task_counts["l1_accessed"] += 1
            if l2_calls:
                aggregate["l2_exposed"] += 1
                task_counts["l2_exposed"] += 1
            aggregate["l0_confirmed_by_key"].update(
                l0.confirmed_counts
            )
            aggregate["l0_high_risk_by_key"].update(
                l0.high_risk_counts
            )

            per_qid.append(
                {
                    "qid": qid,
                    "subject_id": subject_id,
                    "prediction_time": str(result["prediction_time"]),
                    "task": task,
                    "task_type": result.get("task_type"),
                    "trajectory_status": status,
                    "l0": {
                        "confirmed": l0.confirmed,
                        "confirmed_counts": json_safe_counter(
                            l0.confirmed_counts
                        ),
                        "high_risk": l0.high_risk,
                        "high_risk_counts": json_safe_counter(
                            l0.high_risk_counts
                        ),
                        "affected_tables": sorted(
                            l0.table_categories
                        ),
                    },
                    "l1": {
                        "accessed": bool(l1_calls),
                        "tables": sorted(l1_tables),
                        "calls": l1_calls,
                    },
                    "l2": {
                        "exposed": bool(l2_calls),
                        "tables": sorted(l2_tables),
                        "calls": l2_calls,
                    },
                    "replay_validation": {
                        key: int(value)
                        for key, value in sorted(replay_counts.items())
                    },
                }
            )

        if subject_position % 50 == 0:
            print(
                f"processed subjects {subject_position}/{len(by_subject)}",
                flush=True,
            )

    summary = {
        "results": str(results_path),
        "results_sha256": hash_file(results_path),
        "database_root": str(database_root),
        "runtime_assets_root": str(runtime_assets_root),
        "samples": aggregate["samples"],
        "subjects": aggregate["subjects"],
        "trajectory_status": json_safe_counter(
            aggregate["trajectory_status"]
        ),
        "levels": {
            "l0_confirmed": aggregate["l0_confirmed"],
            "l0_high_risk": aggregate["l0_high_risk"],
            "l1_accessed": aggregate["l1_accessed"],
            "l2_exposed": aggregate["l2_exposed"],
        },
        "calls": {
            "supported_observations": aggregate[
                "supported_observations"
            ],
            "l1_calls": aggregate["l1_calls"],
            "l2_calls": aggregate["l2_calls"],
        },
        "replay_validation": {
            "exact": aggregate["unsafe_replay_exact"],
            "normalized": aggregate["unsafe_replay_normalized"],
            "mismatch": aggregate["unsafe_replay_mismatch"],
        },
        "l0_confirmed_by_key": json_safe_counter(
            aggregate["l0_confirmed_by_key"]
        ),
        "l0_high_risk_by_key": json_safe_counter(
            aggregate["l0_high_risk_by_key"]
        ),
        "l1_by_table": json_safe_counter(aggregate["l1_by_table"]),
        "l2_by_table": json_safe_counter(aggregate["l2_by_table"]),
        "l2_by_tool": json_safe_counter(aggregate["l2_by_tool"]),
        "l2_by_category": json_safe_counter(
            aggregate["l2_by_category"]
        ),
        "by_task": {
            task: json_safe_counter(counts)
            for task, counts in sorted(aggregate["by_task"].items())
        },
        "method": {
            "l0": "Confirmed-only temporal and parent-availability rules.",
            "l1": "Recorded calls referencing a table with sample-specific L0 data.",
            "l2": (
                "Recorded observation validated by unsafe replay and changed "
                "by confirmed-safe replay."
            ),
            "max_tool_result_chars": args.max_tool_result_chars,
        },
    }

    write_jsonl_atomic(output_root / "per_qid_l0_l2.jsonl", per_qid)
    write_json_atomic(output_root / "summary.json", summary)
    print(
        "completed "
        f"samples={summary['samples']} "
        f"L0={summary['levels']['l0_confirmed']} "
        f"L1={summary['levels']['l1_accessed']} "
        f"L2={summary['levels']['l2_exposed']} "
        f"output={output_root}",
        flush=True,
    )


if __name__ == "__main__":
    main()
