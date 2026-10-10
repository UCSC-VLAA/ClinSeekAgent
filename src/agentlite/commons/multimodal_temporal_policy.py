"""Strict prediction-time views for the two multimodal benchmark schemas.

The source SQLite files are retained for reproducibility.  This module builds
the only view that may be exposed to a model.  It applies table-aware temporal
rules and question-specific CXR scope instead of relying on the legacy
"first column containing time/date" heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

import pandas as pd


POLICY_VERSION = "multimodal-strict-temporal-scope-v1"
EHRXQA_TABLES = {
    "admissions",
    "chartevents",
    "cost",
    "diagnoses_icd",
    "icustays",
    "inputevents",
    "labevents",
    "microbiologyevents",
    "outputevents",
    "patients",
    "prescriptions",
    "procedures_icd",
    "tb_cxr",
    "transfers",
}


class MultimodalTemporalPolicyError(RuntimeError):
    """Raised when a safe runtime view cannot be constructed."""


@dataclass
class TableReport:
    input_rows: int
    output_rows: int
    dropped_rows: int = 0
    redacted_values: int = 0
    reason: str = ""


@dataclass
class MultimodalRuntimeView:
    tables: dict[str, pd.DataFrame]
    cutoff: pd.Timestamp
    reports: dict[str, TableReport]
    schema: str
    policy_version: str = POLICY_VERSION

    def report_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "schema": self.schema,
            "cutoff": str(self.cutoff),
            "tables": {
                name: {
                    "input_rows": report.input_rows,
                    "output_rows": report.output_rows,
                    "dropped_rows": report.dropped_rows,
                    "redacted_values": report.redacted_values,
                    "reason": report.reason,
                }
                for name, report in sorted(self.reports.items())
            },
        }


def detect_multimodal_schema(
    source_tables: Mapping[str, pd.DataFrame],
) -> Optional[str]:
    names = set(source_tables)
    if {"events", "stays", "tb_cxr"}.issubset(names):
        return "medmod"
    if {"admissions", "patients", "tb_cxr"}.issubset(names):
        return "ehrxqa"
    if names and names.issubset(EHRXQA_TABLES) and any(
        "row_id" in frame.columns for frame in source_tables.values()
    ):
        # EHRXQA cohort questions are packaged as pseudo-subject databases
        # containing only the cohort-relevant table or table pair.
        return "ehrxqa"
    return None


def _copy(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.copy().reset_index(drop=True)


def _parse_cutoff(timestamp: str) -> pd.Timestamp:
    cutoff = pd.to_datetime(timestamp, errors="coerce")
    if pd.isna(cutoff):
        raise MultimodalTemporalPolicyError(
            f"Invalid prediction timestamp: {timestamp!r}"
        )
    return cutoff


def _filter_at_or_before(
    frame: pd.DataFrame,
    column: str,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, int]:
    if column not in frame.columns:
        raise MultimodalTemporalPolicyError(
            f"Required temporal column {column!r} is missing"
        )
    parsed = pd.to_datetime(frame[column], errors="coerce")
    date_only = frame[column].astype(str).str.fullmatch(r"\d{4}-\d{2}-\d{2}")
    parsed = parsed.where(
        ~date_only,
        parsed.dt.normalize()
        + pd.Timedelta(hours=23, minutes=59, seconds=59),
    )
    keep = parsed.notna() & (parsed <= cutoff)
    result = frame.loc[keep].copy()
    result[column] = parsed.loc[keep]
    return result.reset_index(drop=True), int((~keep).sum())


def _redact_after(
    frame: pd.DataFrame,
    column: str,
    cutoff: pd.Timestamp,
) -> int:
    if column not in frame.columns:
        return 0
    parsed = pd.to_datetime(frame[column], errors="coerce")
    date_only = frame[column].astype(str).str.fullmatch(r"\d{4}-\d{2}-\d{2}")
    parsed = parsed.where(
        ~date_only,
        parsed.dt.normalize()
        + pd.Timedelta(hours=23, minutes=59, seconds=59),
    )
    redact = parsed.isna() | (parsed > cutoff)
    count = int(frame.loc[redact, column].notna().sum())
    # Assign the whole column at once. Partial `.loc` assignment of Timestamps
    # into a string-dtype column raises TypeError on pandas >= 3.0, where such
    # columns load as the new `str` dtype rather than object.
    frame[column] = parsed.where(~redact, other=pd.NaT)
    return count


def _normalize_ids(values: Optional[Iterable[Any]]) -> set[str]:
    result: set[str] = set()
    for value in values or []:
        try:
            result.add(str(int(float(value))))
        except (TypeError, ValueError):
            result.add(str(value))
    return result


def _linked_cxr(
    frame: pd.DataFrame,
    cutoff: pd.Timestamp,
    linked_study_ids: Optional[Iterable[Any]],
) -> tuple[pd.DataFrame, int]:
    visible, dropped = _filter_at_or_before(
        frame, "studydatetime", cutoff
    )
    allowed = _normalize_ids(linked_study_ids)
    if "study_id" not in visible.columns:
        raise MultimodalTemporalPolicyError(
            "tb_cxr is missing required study_id"
        )
    normalized = visible["study_id"].map(
        lambda value: str(int(float(value)))
        if pd.notna(value)
        else ""
    )
    keep = normalized.isin(allowed) if allowed else pd.Series(
        False, index=visible.index
    )
    scope_dropped = int((~keep).sum())
    return visible.loc[keep].reset_index(drop=True), dropped + scope_dropped


def _has_longitudinal_ehr(modalities: Optional[Iterable[Any]]) -> bool:
    normalized = {str(value).strip().lower() for value in modalities or []}
    return bool(normalized & {"ehr", "ehr_table", "clinical_ehr"})


def _record(
    reports: dict[str, TableReport],
    name: str,
    source: pd.DataFrame,
    output: pd.DataFrame,
    *,
    dropped: int = 0,
    redacted: int = 0,
    reason: str = "",
) -> None:
    reports[name] = TableReport(
        input_rows=len(source),
        output_rows=len(output),
        dropped_rows=dropped,
        redacted_values=redacted,
        reason=reason,
    )


def _build_medmod(
    source_tables: Mapping[str, pd.DataFrame],
    cutoff: pd.Timestamp,
    linked_study_ids: Optional[Iterable[Any]],
    modalities: Optional[Iterable[Any]],
) -> MultimodalRuntimeView:
    tables: dict[str, pd.DataFrame] = {}
    reports: dict[str, TableReport] = {}
    allow_ehr = _has_longitudinal_ehr(modalities)

    for name, source in source_tables.items():
        if name == "tb_cxr":
            output, dropped = _linked_cxr(
                source, cutoff, linked_study_ids
            )
            reason = "prediction-time and linked-study allowlist"
        elif not allow_ehr:
            output = source.iloc[0:0].copy()
            if name == "stays":
                outcome_columns = [
                    column
                    for column in output.columns
                    if column.lower()
                    in {
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
                ]
                output = output.drop(columns=outcome_columns)
            dropped = len(source)
            reason = "image/CXR-only task: longitudinal EHR suppressed"
        elif name == "events":
            output, dropped = _filter_at_or_before(
                source, "charttime", cutoff
            )
            reason = "charttime <= prediction_time"
        elif name == "stays":
            output, dropped = _filter_at_or_before(
                source, "intime", cutoff
            )
            leaky = [
                column
                for column in output.columns
                if column.lower()
                in {
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
            ]
            redacted = int(output[leaky].notna().sum().sum()) if leaky else 0
            output = output.drop(columns=leaky)
            _record(
                reports,
                name,
                source,
                output,
                dropped=dropped,
                redacted=redacted,
                reason="intime <= prediction_time; outcome columns removed",
            )
            tables[name] = _copy(output)
            continue
        else:
            output = source.iloc[0:0].copy()
            dropped = len(source)
            reason = "unknown MedMod table rejected fail-closed"
        tables[name] = _copy(output)
        _record(
            reports,
            name,
            source,
            output,
            dropped=dropped,
            reason=reason,
        )

    return MultimodalRuntimeView(tables, cutoff, reports, "medmod")


def _discharged_hadm_ids(
    admissions: Optional[pd.DataFrame],
    cutoff: pd.Timestamp,
) -> set[str]:
    if admissions is None or not {"hadm_id", "dischtime"}.issubset(
        admissions.columns
    ):
        return set()
    parsed = pd.to_datetime(admissions["dischtime"], errors="coerce")
    mask = parsed.notna() & (parsed <= cutoff)
    return _normalize_ids(admissions.loc[mask, "hadm_id"].tolist())


def _build_ehrxqa(
    source_tables: Mapping[str, pd.DataFrame],
    cutoff: pd.Timestamp,
    linked_study_ids: Optional[Iterable[Any]],
    modalities: Optional[Iterable[Any]],
) -> MultimodalRuntimeView:
    tables: dict[str, pd.DataFrame] = {}
    reports: dict[str, TableReport] = {}
    allow_ehr = _has_longitudinal_ehr(modalities)
    discharged = _discharged_hadm_ids(
        source_tables.get("admissions"), cutoff
    )

    for name, source in source_tables.items():
        redacted = 0
        if name == "tb_cxr":
            output, dropped = _linked_cxr(
                source, cutoff, linked_study_ids
            )
            reason = "prediction-time and linked-study allowlist"
        elif not allow_ehr:
            output = source.iloc[0:0].copy()
            dropped = len(source)
            reason = "image/CXR-only task: longitudinal EHR suppressed"
        elif name == "admissions":
            output, dropped = _filter_at_or_before(
                source, "admittime", cutoff
            )
            discharge_known = (
                pd.to_datetime(output["dischtime"], errors="coerce")
                <= cutoff
                if "dischtime" in output.columns
                else pd.Series(False, index=output.index)
            )
            for column in [
                "dischtime",
                "deathtime",
                "discharge_location",
                "hospital_expire_flag",
            ]:
                if column in output.columns:
                    mask = ~discharge_known
                    redacted += int(output.loc[mask, column].notna().sum())
                    output.loc[mask, column] = None
            reason = "admittime <= cutoff; discharge outcomes availability-gated"
        elif name in {"diagnoses_icd", "procedures_icd"}:
            if (
                source_tables.get("admissions") is None
                and "charttime" in source.columns
            ):
                output, dropped = _filter_at_or_before(
                    source, "charttime", cutoff
                )
                reason = (
                    "cohort pseudo-subject post-hoc table: "
                    "charttime <= prediction_time"
                )
            elif "hadm_id" not in source.columns:
                output = source.iloc[0:0].copy()
                dropped = len(source)
                reason = "post-hoc table missing hadm_id; rejected fail-closed"
            else:
                ids = source["hadm_id"].map(
                    lambda value: str(int(float(value)))
                    if pd.notna(value)
                    else ""
                )
                keep = ids.isin(discharged)
                output = source.loc[keep].copy().reset_index(drop=True)
                dropped = int((~keep).sum())
                reason = (
                    "post-hoc coded table visible only after parent discharge"
                )
        elif name == "prescriptions":
            output, dropped = _filter_at_or_before(
                source, "starttime", cutoff
            )
            redacted += _redact_after(output, "stoptime", cutoff)
            reason = "starttime <= cutoff; future stoptime redacted"
        elif name in {"transfers", "icustays"}:
            output, dropped = _filter_at_or_before(
                source, "intime", cutoff
            )
            redacted += _redact_after(output, "outtime", cutoff)
            if "last_careunit" in output.columns:
                outtime = pd.to_datetime(
                    output.get("outtime"), errors="coerce"
                )
                mask = outtime.isna() | (outtime > cutoff)
                redacted += int(
                    output.loc[mask, "last_careunit"].notna().sum()
                )
                output.loc[mask, "last_careunit"] = None
            reason = "intime <= cutoff; future outtime/outcome redacted"
        elif name in {"labevents", "chartevents"}:
            output, dropped = _filter_at_or_before(
                source, "charttime", cutoff
            )
            reason = "charttime <= prediction_time"
        elif name == "inputevents":
            output, dropped = _filter_at_or_before(
                source, "starttime", cutoff
            )
            redacted += _redact_after(output, "endtime", cutoff)
            reason = "starttime <= cutoff; future endtime redacted"
        elif name == "outputevents":
            output, dropped = _filter_at_or_before(
                source, "charttime", cutoff
            )
            reason = "charttime <= prediction_time"
        elif name == "microbiologyevents":
            time_column = (
                "charttime"
                if "charttime" in source.columns
                else "chartdate"
            )
            output, dropped = _filter_at_or_before(
                source, time_column, cutoff
            )
            reason = f"{time_column} <= prediction_time"
        elif name == "cost":
            output, dropped = _filter_at_or_before(
                source, "chargetime", cutoff
            )
            reason = "chargetime <= prediction_time"
        elif name == "patients":
            output = _copy(source)
            dropped = 0
            if "dod" in output.columns:
                parsed = pd.to_datetime(output["dod"], errors="coerce")
                date_only = output["dod"].astype(str).str.fullmatch(
                    r"\d{4}-\d{2}-\d{2}"
                )
                available = parsed.where(
                    ~date_only,
                    parsed.dt.normalize()
                    + pd.Timedelta(hours=23, minutes=59, seconds=59),
                )
                mask = available.isna() | (available > cutoff)
                redacted += int(output.loc[mask, "dod"].notna().sum())
                output.loc[mask, "dod"] = None
            reason = "static demographics; death date availability-gated"
        else:
            output = source.iloc[0:0].copy()
            dropped = len(source)
            reason = "unknown EHRXQA table rejected fail-closed"

        tables[name] = _copy(output)
        _record(
            reports,
            name,
            source,
            output,
            dropped=dropped,
            redacted=redacted,
            reason=reason,
        )

    return MultimodalRuntimeView(tables, cutoff, reports, "ehrxqa")


def build_multimodal_runtime_view(
    source_tables: Mapping[str, pd.DataFrame],
    timestamp: str,
    *,
    linked_study_ids: Optional[Iterable[Any]] = None,
    modalities: Optional[Iterable[Any]] = None,
) -> MultimodalRuntimeView:
    schema = detect_multimodal_schema(source_tables)
    cutoff = _parse_cutoff(timestamp)
    if schema == "medmod":
        return _build_medmod(
            source_tables, cutoff, linked_study_ids, modalities
        )
    if schema == "ehrxqa":
        return _build_ehrxqa(
            source_tables, cutoff, linked_study_ids, modalities
        )
    raise MultimodalTemporalPolicyError(
        "Patient database does not match EHRXQA or MedMod schema"
    )
