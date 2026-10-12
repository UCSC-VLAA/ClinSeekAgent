#!/usr/bin/env python3
"""Independent, label-blind availability filter applied after the runtime policy.

This module deliberately does not import ``agentlite`` or either shipped
temporal-policy module.  It accepts the tables that survived the runtime policy
and can only remove rows or blank values.  Decisions use timestamps and raw
provenance only; labels, outcomes, and prediction accuracy are never inputs.

For every check, a row/value has exactly one of three dispositions:

* availability established at or before the cutoff: keep;
* availability established after the cutoff: drop/blank;
* availability cannot be established: keep and count as unresolved.

Raw provenance used (MM only)
-----------------------------
* MedMod ``events``: the raw MIMIC-IV ``storetime`` of the matching
  chartevents / labevents / outputevents row (when the value became readable).
* EHRXQA radiology reports: the raw MIMIC-IV-Note ``radiology.storetime`` of the
  note the packaged MIMIC-CXR report was written as, shifted into the
  benchmark's de-identified time frame by the patient's exact time offset (see
  ``_prepare_report_provenance``).

Both need a raw MIMIC-IV root (``hosp/``, ``icu/``, ``note/``) passed as
``raw_mimic_root``.  Text uses only the per-patient SQLite databases.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


FILTER_VERSION = "independent-availability-v1"

MISSING = {"", "nan", "nat", "none", "null", "<na>"}
LONGITUDINAL_MODALITIES = {"ehr", "ehr_table", "clinical_ehr"}

# These are event/creation anchors, not a copy or import of the runtime policy.
# Alternatives coalesce from left to right.  A missing/unparseable anchor is
# unresolved and therefore kept, never guessed away.
TEXT_EVENT_COLUMNS: dict[str, tuple[str, ...] | None] = {
    "patients": None,
    "admissions": ("admittime",),
    "transfers": ("intime",),
    "edstays": ("intime",),
    "icustays": ("intime",),
    "services": ("transfertime",),
    "prescriptions": ("starttime",),
    "poe": ("ordertime",),
    "medrecon": ("charttime",),
    "pyxis": ("charttime",),
    "vitalsign": ("charttime",),
    "omr": ("chartdate",),
    "chartevents": ("charttime",),
    "datetimeevents": ("charttime",),
    "labevents": ("charttime",),
    "outputevents": ("charttime",),
    "inputevents": ("starttime",),
    "ingredientevents": ("starttime",),
    "procedureevents": ("starttime",),
    "emar": ("charttime",),
    "microbiologyevents": ("charttime", "chartdate"),
    "radiology": ("charttime",),
    "discharge": ("charttime",),
    "pharmacy": ("entertime",),
    "diagnoses_icd": ("available_time",),
    "diagnosis": ("available_time",),
    "procedures_icd": ("available_time",),
    "hcpcsevents": ("available_time",),
    "drgcodes": ("available_time",),
    "triage": ("charttime",),
    # Detail rows are availability-cascaded from a retained parent below.
    "discharge_detail": None,
    "radiology_detail": None,
    "poe_detail": None,
    "emar_detail": None,
}

MM_EVENT_COLUMNS: dict[str, tuple[str, ...] | None] = {
    "patients": None,
    "admissions": ("admittime",),
    "transfers": ("intime",),
    "icustays": ("intime",),
    "inputevents": ("starttime",),
    "labevents": ("charttime",),
    "chartevents": ("charttime",),
    "outputevents": ("charttime",),
    "microbiologyevents": ("charttime", "chartdate"),
    "prescriptions": ("starttime",),
    "diagnoses_icd": ("charttime",),
    "procedures_icd": ("charttime",),
    "cost": ("chargetime",),
    "tb_cxr": ("studydatetime",),
    "events": ("charttime",),
    "stays": ("intime",),
}

# Rows whose content only becomes known at the named timestamp.  When the
# timestamp is later than the clamped cutoff, the row remains but these values
# are blanked.  This is uniform across samples and never consults the task.
FIELD_GUARDS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "admissions": (
        ("dischtime", ("dischtime",)),
        ("deathtime", ("deathtime",)),
        ("edouttime", ("edouttime",)),
        ("discharge_location", ("dischtime",)),
        ("hospital_expire_flag", ("dischtime",)),
    ),
    "transfers": (("outtime", ("outtime",)),),
    "edstays": (
        ("outtime", ("outtime",)),
        ("disposition", ("outtime",)),
    ),
    "icustays": (
        ("outtime", ("outtime",)),
        ("los", ("outtime",)),
        ("last_careunit", ("outtime",)),
    ),
    "prescriptions": (("stoptime", ("stoptime",)),),
    "emar": (("scheduletime", ("scheduletime",)),),
}

COMPLETION_GUARDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "inputevents": (
        ("endtime", "storetime"),
        ("endtime", "storetime", "amount", "amountuom", "rate", "rateuom",
         "totalamount", "totalamountuom", "isopenbag", "continueinnextdept",
         "statusdescription"),
    ),
    "ingredientevents": (
        ("endtime", "storetime"),
        ("endtime", "storetime", "amount", "amountuom", "rate", "rateuom",
         "statusdescription"),
    ),
    "procedureevents": (
        ("endtime", "storetime"),
        ("endtime", "storetime", "value", "valueuom", "isopenbag",
         "continueinnextdept", "statusdescription"),
    ),
}

CHILD_PARENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "discharge_detail": ("discharge", ("note_id",)),
    "radiology_detail": ("radiology", ("note_id",)),
    "poe_detail": ("poe", ("poe_id", "poe_seq")),
    "emar_detail": ("emar", ("emar_id", "emar_seq")),
}



def _line_scanner(pattern: str, path: str):
    """Yield lines of `path` matching `pattern`, using the fastest scanner found.

    ripgrep is optional; grep is near-universal. The pure-Python arm is the
    last resort and is only reached if neither binary is present. All three
    yield the same lines for the anchored patterns used here.
    """
    import shutil
    for exe, argv in (("rg", ["rg", pattern, path]),
                      ("grep", ["grep", "-E", pattern, path])):
        if shutil.which(exe):
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace")
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    yield line
            finally:
                proc.stdout.close()
                proc.wait()
            return
    rx = re.compile(pattern)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if rx.search(line):
                yield line


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _present(value: Any) -> bool:
    return str(value).strip().lower() not in MISSING


def _norm_id(value: Any) -> str:
    if not _present(value):
        return ""
    text = str(value).strip()
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError, OverflowError):
        pass
    return text


def _norm_scalar(value: Any) -> str:
    """Compare values, not their formatting.

    The packaged tables come back through pandas, which renders an integral
    reading as `73.0`; the raw CSV holds `73`. A bare string compare would
    never match and the row would be filed as undecidable (and kept) although
    its raw storetime proves it late. Collapse integral floats before comparing.
    """
    if not _present(value):
        return ""
    text = str(value).strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    if number != number:  # NaN
        return ""
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _norm_time(value: Any) -> str:
    if not _present(value):
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return str(value).strip()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _series_state(series: pd.Series, cutoff: pd.Timestamp) -> pd.Series:
    """Return ``before``, ``after``, or ``unknown`` for each raw value.

    Date-only values on the cutoff date are unresolved unless the cutoff is at
    the very end of that date.  Treating midnight as the availability time
    would silently guess in favor of availability.
    """
    raw = series.astype("string")
    present = ~raw.str.strip().str.lower().isin(MISSING)
    parsed = pd.to_datetime(raw, errors="coerce")
    date_only = raw.str.strip().str.fullmatch(r"\d{4}-\d{2}-\d{2}", na=False)
    result = pd.Series("unknown", index=series.index, dtype="object")

    precise = present & parsed.notna() & ~date_only
    result.loc[precise & (parsed <= cutoff)] = "before"
    result.loc[precise & (parsed > cutoff)] = "after"

    dates = present & parsed.notna() & date_only
    earlier = dates & (parsed.dt.date < cutoff.date())
    later = dates & (parsed.dt.date > cutoff.date())
    same_proven = dates & (parsed.dt.date == cutoff.date()) & (
        cutoff.time().hour == 23
        and cutoff.time().minute == 59
        and cutoff.time().second == 59
    )
    result.loc[earlier | same_proven] = "before"
    result.loc[later] = "after"
    return result


def _coalesced_state(
    frame: pd.DataFrame,
    columns: Iterable[str],
    cutoff: pd.Timestamp,
) -> pd.Series:
    result = pd.Series("unknown", index=frame.index, dtype="object")
    undecided = pd.Series(True, index=frame.index)
    for column in columns:
        if column not in frame.columns:
            continue
        raw = frame[column]
        choose = undecided & raw.map(_present)
        if choose.any():
            state = _series_state(raw, cutoff)
            result.loc[choose] = state.loc[choose]
            undecided.loc[choose] = False
    return result


def _mm_event_key(values: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _norm_time(values.get("charttime")),
        _norm_id(values.get("itemid")),
        _norm_scalar(values.get("value")),
        _norm_scalar(values.get("valuenum")),
    )


def _report_tokens(text: Any) -> frozenset:
    """Word tokens of a radiology report body (text after `FINAL REPORT`).

    Packaged MIMIC-CXR report files and MIMIC-IV-Note radiology notes carry the
    same report with small formatting differences, so they are compared as
    lower-cased token sets rather than byte strings.
    """
    text = str(text or "").replace("\r", "\n")
    match = re.search(r"FINAL REPORT", text, flags=re.IGNORECASE)
    if match:
        text = text[match.end():]
    return frozenset(re.findall(r"\w+", text.lower()))


def _strip_package_prefix(path: str) -> str:
    """`EHRXQAOriginalLinked_v1/mimic-cxr/...` -> `mimic-cxr/...`.

    Manifest attachment paths carry an upstream package prefix as their first
    segment; evidence-table cells (`tb_cxr.image_path`, `tb_cxr.report_path`)
    and the source roots do not.
    """
    parts = str(path).split("/", 1)
    if len(parts) == 2 and parts[0].endswith("OriginalLinked_v1"):
        return parts[1]
    return str(path)


@lru_cache(maxsize=1 << 20)
def _to_datetime_cached(value: Any) -> Any:
    """`pd.to_datetime(value, errors="coerce")`, memoised.

    Raw event timestamps repeat heavily (many items per charttime), so this
    makes the raw storetime scan several times faster without changing a
    single parsed value.
    """
    return pd.to_datetime(value, errors="coerce")


def rewrite_linked_report_count(text: Any, count: int) -> Any:
    """Keep MM prompt metadata consistent with filtered report attachments."""
    if not isinstance(text, str):
        return text
    return re.sub(
        r"(?m)^(Linked Report Count:\s*)\d+\s*$",
        lambda match: f"{match.group(1)}{count}",
        text,
    )


@dataclass
class FilterResult:
    tables: dict[str, pd.DataFrame]
    cutoff: pd.Timestamp
    report_paths: list[str]
    report: dict[str, Any]
    # Internal coordinates used by the builder to report Recent/Full impact.
    dropped_indices: dict[str, dict[Any, set[str]]]
    blanked_indices: dict[str, list[tuple[Any, str, str]]]


class AvailabilityFilter:
    """Independent post-loader filter with raw-provenance lookups for MM.

    Text needs only ``data_root`` (the EHR-Bench root holding
    ``database/patient_<subject_id>.db``).  MM needs ``source_roots``
    (``{"ehrxqa": <root>, "medmod": <root>}``, each holding ``database/`` and the
    packaged MIMIC-CXR assets), ``raw_mimic_root`` and ``cache_root``.
    """

    def __init__(
        self,
        *,
        bench: str,
        rows: list[dict[str, Any]],
        data_root: Path | None = None,
        source_roots: Mapping[str, Path] | None = None,
        cache_root: Path | None = None,
        raw_mimic_root: Path | None = None,
    ) -> None:
        if bench not in {"text", "mm"}:
            raise ValueError(f"unsupported benchmark {bench!r}")
        self.bench = bench
        self.rows = rows
        self.data_root = data_root
        self.source_roots = {str(k).lower(): Path(v)
                             for k, v in (source_roots or {}).items()}
        self.cache_root = cache_root
        self.raw_mimic_root = raw_mimic_root
        self._mm_ce_dir: Path | None = None
        self._mm_ce_lru: OrderedDict[str, dict[tuple[str, str, str, str], list[pd.Timestamp | None]]] = OrderedDict()
        self._report_provenance: dict[str, dict[str, Any]] = {}
        self.preparation_report: dict[str, Any] = {
            "filter_version": FILTER_VERSION,
            "label_independent": True,
        }
        if bench == "mm":
            if cache_root is None or raw_mimic_root is None:
                raise ValueError("MM filtering needs cache_root and raw_mimic_root")
            missing = [rel for rel in (*self.STORETIME_SOURCES,
                                       *self.REPORT_SOURCES)
                       if not (self.raw_mimic_root / rel).is_file()]
            if missing:
                raise FileNotFoundError(
                    f"raw MIMIC-IV files missing under {self.raw_mimic_root}: "
                    f"{missing}")
            self.cache_root.mkdir(parents=True, exist_ok=True)
            self._prepare_mm_chartevents()
            self._prepare_report_provenance()

    def _mm_subject_rows(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            modalities = {str(x).strip().lower()
                          for x in (row.get("modalities") or [])}
            if modalities & LONGITUDINAL_MODALITIES:
                result[_norm_id(row.get("subject_id"))].append(row)
        return result

    # Every raw table that records when a value became readable (`storetime`).
    # MedMod `events` rows are matched against all three.
    STORETIME_SOURCES = (
        "icu/chartevents.csv",
        "hosp/labevents.csv",
        "icu/outputevents.csv",
    )
    # Raw sources for radiology-report availability (EHRXQA).
    REPORT_SOURCES = (
        "note/radiology.csv",
        "hosp/admissions.csv",
    )

    @staticmethod
    def _header_index(path: Path) -> dict[str, int]:
        """Column name -> position, so one scanner can read three schemas."""
        import csv as _csv
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            header = next(_csv.reader([fh.readline()]))
        return {name.strip().lower(): i for i, name in enumerate(header)}

    def _prepare_mm_chartevents(self) -> None:
        """Cache, per MedMod subject, the raw (charttime, storetime, item, value)
        of every chart/lab/output event charted no later than the subject's
        latest prediction time in this run.

        The cache directory is keyed by the raw file identity and the subject
        set, so it is rebuilt automatically when either changes. A row can only
        ever be matched against raw events charted at or before its own cutoff,
        so the cached content needed by a row does not depend on which other
        rows are in the run.
        """
        subject_rows = self._mm_subject_rows()
        raw = self.raw_mimic_root / "icu/chartevents.csv"
        if not subject_rows or not raw.exists():
            self.preparation_report["mm_chartevents"] = {
                "status": "not_prepared",
                "reason": "no longitudinal rows" if not subject_rows
                else f"missing raw file: {raw}",
                "subjects": len(subject_rows),
            }
            return

        identity = json.dumps({
            "version": FILTER_VERSION,
            "raw": str(raw.resolve()),
            "size": raw.stat().st_size,
            "mtime_ns": raw.stat().st_mtime_ns,
            "subjects": sorted(subject_rows),
        }, sort_keys=True).encode()
        key = hashlib.sha256(identity).hexdigest()[:20]
        cache_dir = self.cache_root / f"medmod_chartevents_{key}"
        done = cache_dir / "_DONE.json"
        self._mm_ce_dir = cache_dir
        if done.exists():
            cached = json.loads(done.read_text(encoding="utf-8"))
            self.preparation_report["mm_chartevents"] = {
                **cached, "status": "cache_reused", "cache": str(cache_dir),
            }
            return

        cache_dir.mkdir(parents=True, exist_ok=True)
        maximum_cutoff = {
            subject: max(pd.to_datetime(r["prediction_time"])
                         for r in members)
            for subject, members in subject_rows.items()
        }
        # Anchor the pattern on the subject_id column, wherever it is.
        _ids = "(" + "|".join(re.escape(x) for x in subject_rows) + ")"

        def _pattern_for(index: int) -> str:
            if index == 0:
                return "^" + _ids + ","
            return "^([^,]*,){" + str(index) + "}" + _ids + ","

        handles: dict[str, Any] = {}
        writers: dict[str, csv.writer] = {}
        scanned = written = unparseable = 0
        per_source: dict[str, dict[str, int]] = {}
        try:
            for rel in self.STORETIME_SOURCES:
                source = self.raw_mimic_root / rel
                if not source.exists():
                    per_source[rel] = {"status": "missing"}
                    continue
                cols = self._header_index(source)
                need = ("subject_id", "charttime", "storetime", "itemid")
                if any(c not in cols for c in need):
                    per_source[rel] = {"status": "schema_mismatch",
                                       "columns": sorted(cols)[:12]}
                    continue
                i_sub, i_chart = cols["subject_id"], cols["charttime"]
                pattern = _pattern_for(i_sub)
                i_store, i_item = cols["storetime"], cols["itemid"]
                i_val = cols.get("value")
                i_num = cols.get("valuenum")
                src_scanned = src_written = 0
                for line in _line_scanner(pattern, str(source)):
                    scanned += 1
                    src_scanned += 1
                    try:
                        values = next(csv.reader([line]))
                    except (csv.Error, StopIteration):
                        unparseable += 1
                        continue
                    if len(values) <= max(i_sub, i_chart, i_store, i_item):
                        unparseable += 1
                        continue
                    subject = _norm_id(values[i_sub])
                    chart = _to_datetime_cached(values[i_chart])
                    if subject not in maximum_cutoff or pd.isna(chart):
                        unparseable += 1
                        continue
                    if chart > maximum_cutoff[subject]:
                        continue
                    if subject not in handles:
                        handle = (cache_dir / f"{subject}.csv").open(
                            "w", encoding="utf-8", newline="")
                        handles[subject] = handle
                        writers[subject] = csv.writer(handle)
                        writers[subject].writerow(
                            ["charttime", "storetime", "itemid", "value",
                             "valuenum"])
                    writers[subject].writerow([
                        values[i_chart], values[i_store], values[i_item],
                        values[i_val] if i_val is not None
                        and i_val < len(values) else "",
                        values[i_num] if i_num is not None
                        and i_num < len(values) else "",
                    ])
                    written += 1
                    src_written += 1
                per_source[rel] = {
                    "status": ("scanned" if src_written
                               else "scanned_but_cached_nothing"),
                    "subject_id_column_index": i_sub,
                    "rows_matched": src_scanned,
                    "rows_cached": src_written,
                }
        finally:
            for handle in handles.values():
                handle.close()
        summary = {
            "status": "cache_built",
            "cache": str(cache_dir),
            "raw_file": str(raw),
            "raw_size": raw.stat().st_size,
            "subjects": len(subject_rows),
            "subject_rows_scanned": scanned,
            "per_source": per_source,
            "provenance_rows_cached": written,
            "unparseable_rows": unparseable,
        }
        done.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        self.preparation_report["mm_chartevents"] = summary

    def _load_mm_chartevents(
        self, subject_id: Any,
    ) -> dict[tuple[str, str, str, str], list[pd.Timestamp | None]]:
        subject = _norm_id(subject_id)
        cached = self._mm_ce_lru.pop(subject, None)
        if cached is not None:
            self._mm_ce_lru[subject] = cached
            return cached
        result: dict[tuple[str, str, str, str], list[pd.Timestamp | None]] = defaultdict(list)
        path = self._mm_ce_dir / f"{subject}.csv" if self._mm_ce_dir else None
        if path and path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    key = _mm_event_key(row)
                    stamp = _to_datetime_cached(row.get("storetime"))
                    result[key].append(None if pd.isna(stamp) else stamp)
        self._mm_ce_lru[subject] = result
        while len(self._mm_ce_lru) > 8:
            self._mm_ce_lru.popitem(last=False)
        return result

    def _source_root(self, source: str) -> Path | None:
        return self.source_roots.get(str(source or "").lower())

    def _resolve_asset(self, source: str, raw_path: str) -> Path | None:
        """Manifest attachment path -> local file.

        Manifest paths carry the upstream package prefix
        (`EHRXQAOriginalLinked_v1/...`); it is stripped and the remainder is
        resolved under the source's root.
        """
        path = Path(raw_path)
        if path.is_absolute():
            return path
        root = self._source_root(source)
        if root is None:
            return None
        return root / _strip_package_prefix(raw_path)

    def _prepare_report_provenance(self) -> None:
        """When did each linked radiology report become readable?

        The EHRXQA databases are de-identified by a patient-wide time offset,
        and, for chest X-ray studies only, `tb_cxr.studydatetime` is
        additionally rotated among each patient's studies. The radiology report
        itself never enters that preprocessing; it is attached later by
        unchanged `study_id`. So neither the delivered study time nor
        "study time + typical report lag" is the report's availability.

        Instead each packaged MIMIC-CXR report is linked to the raw
        MIMIC-IV-Note radiology note it was written as -- the same-patient note
        with the unique maximum word-token Jaccard similarity of the report
        body (text after `FINAL REPORT`, lower-cased) -- and its availability is

            raw_note.storetime + patient_offset

        where `patient_offset` is the one exact shift between the raw MIMIC-IV
        admissions and the patient's benchmark database (every admit/discharge
        timestamp pair must agree). A report whose link or offset cannot be
        established is left unresolved and kept.
        """
        wanted: dict[str, tuple[str, str, Path | None]] = {}
        for row in self.rows:
            source = str(row.get("source_benchmark", "")).lower()
            subject = _norm_id(row.get("subject_id"))
            for raw_path in row.get("report_paths") or []:
                wanted.setdefault(str(raw_path), (
                    source, subject, self._resolve_asset(source, str(raw_path))))
        if not wanted:
            self.preparation_report["radiology_reports"] = {
                "status": "not_prepared", "reason": "no reports"}
            return

        subjects = {subject for _, subject, _ in wanted.values()}
        notes: dict[str, list[tuple[str, Any, Any, frozenset]]] = defaultdict(list)
        scanned = 0
        for chunk in pd.read_csv(
            self.raw_mimic_root / "note/radiology.csv",
            usecols=["note_id", "subject_id", "charttime", "storetime", "text"],
            dtype=str,
            chunksize=100_000,
        ):
            scanned += len(chunk)
            chunk = chunk[chunk["subject_id"].map(_norm_id).isin(subjects)]
            for note in chunk.itertuples(index=False):
                notes[_norm_id(note.subject_id)].append((
                    str(note.note_id), note.charttime, note.storetime,
                    _report_tokens(note.text)))

        offsets, offset_witnesses = self._patient_offsets(subjects, wanted)

        counts = Counter()
        for raw_path, (source, subject, asset) in sorted(wanted.items()):
            entry: dict[str, Any] = {"subject_id": subject,
                                     "available_time": None}
            if asset is None or not asset.is_file():
                entry["unresolved"] = "missing_packaged_report"
            elif subject not in offsets:
                entry["unresolved"] = "no_unique_patient_offset"
            else:
                tokens = _report_tokens(
                    asset.read_text(encoding="utf-8", errors="replace"))
                scored = sorted(
                    ((len(tokens & cand[3]) / max(1, len(tokens | cand[3])),
                      cand) for cand in notes.get(subject, [])),
                    key=lambda item: -item[0])
                best = scored[0] if scored else None
                runner_up = scored[1][0] if len(scored) > 1 else 0.0
                if best is None or best[0] <= 0:
                    entry["unresolved"] = "no_raw_note"
                elif best[0] <= runner_up:
                    entry["unresolved"] = "ambiguous_raw_note"
                else:
                    note_id, charttime, storetime, _ = best[1]
                    store = pd.to_datetime(storetime, errors="coerce")
                    if pd.isna(store):
                        entry["unresolved"] = "unparseable_raw_storetime"
                    else:
                        shift = pd.Timedelta(seconds=offsets[subject])
                        entry.update({
                            "raw_note_id": note_id,
                            "token_jaccard": round(best[0], 6),
                            "next_best_token_jaccard": round(runner_up, 6),
                            "raw_charttime": charttime,
                            "raw_storetime": storetime,
                            "patient_offset_seconds": offsets[subject],
                            "offset_witnesses": offset_witnesses[subject],
                            "available_time": store + shift,
                        })
            counts["resolved" if entry["available_time"] is not None
                   else entry["unresolved"]] += 1
            self._report_provenance[raw_path] = entry
        self.preparation_report["radiology_reports"] = {
            "status": "prepared",
            "raw_rows_scanned": scanned,
            "unique_reports": len(wanted),
            "subjects": len(subjects),
            "subjects_with_offset": len(offsets),
            "outcomes": dict(counts),
        }

    def _patient_offsets(
        self,
        subjects: set[str],
        wanted: Mapping[str, tuple[str, str, Path | None]],
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Exact per-patient shift (seconds) from raw to benchmark time."""
        source_of = {subject: source for source, subject, _ in wanted.values()}
        raw = pd.read_csv(
            self.raw_mimic_root / "hosp/admissions.csv", dtype=str,
            usecols=["subject_id", "hadm_id", "admittime", "dischtime"])
        raw = raw[raw["subject_id"].map(_norm_id).isin(subjects)]
        raw_by_hadm = {
            (_norm_id(r.subject_id), _norm_id(r.hadm_id)): (r.admittime, r.dischtime)
            for r in raw.itertuples(index=False)}
        offsets: dict[str, int] = {}
        witnesses: dict[str, int] = {}
        for subject in sorted(subjects):
            db_path = self._patient_db({"subject_id": subject,
                                        "source_benchmark": source_of[subject]})
            if db_path is None or not db_path.exists():
                continue
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                bench = self._read_raw_table(connection, "admissions")
            finally:
                connection.close()
            if not {"hadm_id", "admittime", "dischtime"}.issubset(bench.columns):
                continue
            values: set[int] = set()
            n = 0
            for item in bench.itertuples(index=False):
                pair = raw_by_hadm.get((subject, _norm_id(item.hadm_id)))
                if pair is None:
                    continue
                for bench_time, raw_time in ((item.admittime, pair[0]),
                                             (item.dischtime, pair[1])):
                    b = pd.to_datetime(bench_time, errors="coerce")
                    r = pd.to_datetime(raw_time, errors="coerce")
                    if not pd.isna(b) and not pd.isna(r):
                        values.add(int((b - r).total_seconds()))
                        n += 1
            if len(values) == 1:
                offsets[subject] = next(iter(values))
                witnesses[subject] = n
        return offsets, witnesses

    def _patient_db(self, row: Mapping[str, Any]) -> Path | None:
        subject = _norm_id(row.get("subject_id"))
        if self.bench == "text":
            return (self.data_root / "database" / f"patient_{subject}.db") \
                if self.data_root else None
        root = self._source_root(row.get("source_benchmark"))
        if root is None:
            return None
        return root / "database" / f"patient_{subject}.db"

    @staticmethod
    def _read_raw_table(connection: sqlite3.Connection, table: str) -> pd.DataFrame:
        names = {x[0] for x in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:
            return pd.DataFrame()
        escaped = table.replace('"', '""')
        return pd.read_sql_query(f'SELECT * FROM "{escaped}"', connection)

    @staticmethod
    def _row_key(row: Mapping[str, Any], columns: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_norm_id(row.get(c)) for c in columns)

    def _filter_prescriptions(
        self,
        frame: pd.DataFrame,
        cutoff: pd.Timestamp,
        connection: sqlite3.Connection | None,
    ) -> tuple[pd.Series, dict[Any, set[str]], Counter]:
        drop = pd.Series(False, index=frame.index)
        reasons: dict[Any, set[str]] = defaultdict(set)
        counts = Counter()
        if connection is None or frame.empty:
            counts["unresolved_no_raw_database"] += len(frame)
            return drop, reasons, counts

        poe = self._read_raw_table(connection, "poe")
        pharmacy = self._read_raw_table(connection, "pharmacy")
        poe_lookup: dict[tuple[str, str], list[pd.Timestamp]] = defaultdict(list)
        pharmacy_lookup: dict[str, list[pd.Timestamp]] = defaultdict(list)
        if {"poe_id", "poe_seq", "ordertime"}.issubset(poe.columns):
            for item in poe.itertuples(index=False):
                stamp = pd.to_datetime(getattr(item, "ordertime"), errors="coerce")
                if not pd.isna(stamp):
                    poe_lookup[(_norm_id(getattr(item, "poe_id")),
                                _norm_id(getattr(item, "poe_seq")))].append(stamp)
        if {"pharmacy_id", "entertime"}.issubset(pharmacy.columns):
            for item in pharmacy.itertuples(index=False):
                stamp = pd.to_datetime(getattr(item, "entertime"), errors="coerce")
                if not pd.isna(stamp):
                    pharmacy_lookup[_norm_id(getattr(item, "pharmacy_id"))].append(stamp)

        for index, item in frame.iterrows():
            applicable = 0
            unresolved = False
            # The earliest record-creation anchor wins, and anchor semantics
            # are per field. `poe.ordertime` and `pharmacy.entertime` are both record-creation
            # times, so either one proving the record existed by the cutoff is
            # enough to keep it.
            #
            # This does NOT generalise into min() over a row's timestamps.
            # `labevents.charttime` is when the specimen was drawn and
            # `storetime` is when the result became readable; taking the earlier
            # of those would keep a result nobody could read at the cutoff.
            # Availability comes from what a field MEANS, never from which
            # value happens to be smallest.
            early_anchors = 0
            late_anchors = 0
            poe_id = _norm_id(item.get("poe_id"))
            poe_seq = _norm_id(item.get("poe_seq"))
            pharmacy_id = _norm_id(item.get("pharmacy_id"))
            if poe_id:
                applicable += 1
                stamps = poe_lookup.get((poe_id, poe_seq), [])
                if not stamps:
                    # Some source releases encode poe_seq inconsistently; the
                    # id-only fallback is unresolved if it is non-unique.
                    candidates = [values for (pid, _), values in poe_lookup.items()
                                  if pid == poe_id]
                    if len(candidates) == 1:
                        stamps = candidates[0]
                if not stamps:
                    unresolved = True
                    counts["unresolved_poe_link"] += 1
                elif min(stamps) > cutoff:
                    late_anchors += 1
                    reasons[index].add("prescription_poe_after_cutoff")
                    counts["poe_after_cutoff"] += 1
                else:
                    early_anchors += 1
                    counts["poe_available"] += 1
            if pharmacy_id:
                applicable += 1
                stamps = pharmacy_lookup.get(pharmacy_id, [])
                if not stamps:
                    unresolved = True
                    counts["unresolved_pharmacy_link"] += 1
                elif min(stamps) > cutoff:
                    late_anchors += 1
                    reasons[index].add("prescription_pharmacy_after_cutoff")
                    counts["pharmacy_after_cutoff"] += 1
                else:
                    early_anchors += 1
                    counts["pharmacy_available"] += 1
            if applicable == 0:
                unresolved = True
                counts["unresolved_no_order_link"] += 1
            if late_anchors and not early_anchors:
                drop.loc[index] = True
            elif late_anchors and early_anchors:
                # One order was placed before the cutoff, so the row was
                # readable; keep it and record the mixed evidence.
                counts["kept_mixed_anchor_evidence"] += 1
                reasons.pop(index, None)
            if unresolved and not drop.loc[index]:
                counts["unresolved_rows_kept"] += 1
            if len(reasons.get(index, ())) == 2:
                counts["both_after_cutoff"] += 1
        counts["rows_dropped"] = int(drop.sum())
        return drop, reasons, counts

    def _encounter_starts(
        self, connection: sqlite3.Connection | None,
    ) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
        hadm: dict[str, list[Any]] = defaultdict(list)
        stay: dict[str, list[Any]] = defaultdict(list)
        if connection is None:
            return hadm, stay
        admissions = self._read_raw_table(connection, "admissions")
        if "hadm_id" in admissions.columns:
            for _, item in admissions.iterrows():
                key = _norm_id(item.get("hadm_id"))
                if key:
                    hadm[key].extend([item.get("admittime"), item.get("edregtime")])
        ed = self._read_raw_table(connection, "edstays")
        if {"stay_id", "intime"}.issubset(ed.columns):
            for _, item in ed.iterrows():
                stay[_norm_id(item.get("stay_id"))].append(item.get("intime"))
        icu = self._read_raw_table(connection, "icustays")
        if {"stay_id", "intime"}.issubset(icu.columns):
            for _, item in icu.iterrows():
                stay[_norm_id(item.get("stay_id"))].append(item.get("intime"))
        packaged = self._read_raw_table(connection, "stays")
        if not packaged.empty:
            for _, item in packaged.iterrows():
                sid = _norm_id(item.get("stay_id"))
                hid = _norm_id(item.get("hadm_id"))
                if sid:
                    stay[sid].append(item.get("intime"))
                if hid:
                    hadm[hid].extend([item.get("admittime"), item.get("intime")])
        return hadm, stay

    @staticmethod
    def _encounter_state(values: list[Any] | None, cutoff: pd.Timestamp) -> str:
        if not values:
            return "unknown"
        parsed = [pd.to_datetime(v, errors="coerce") for v in values
                  if _present(v)]
        parsed = [v for v in parsed if not pd.isna(v)]
        if not parsed:
            return "unknown"
        if any(value <= cutoff for value in parsed):
            return "before"
        return "after"



    # Columns the MedMod branch drops by name. Availability of each is the
    # stay's own `outtime`: once the stay has ended, they were readable.
    MEDMOD_OUTCOME_COLUMNS = (
        "last_careunit", "outtime", "los", "dischtime", "deathtime",
        "mortality_inunit", "mortality", "mortality_inhospital",
    )

    def _restore_medmod_outcomes(self, tables, row, cutoff, connection):
        """Put back MedMod stay outcomes that had already ended by the cutoff.

        Returns a counter. Restores a value only when the raw stay's `outtime`
        is at or before the cutoff -- the same per-row test the EHRXQA branch
        of the shipped policy already applies to `last_careunit`.
        """
        from collections import Counter
        counts = Counter()
        stays = tables.get("stays")
        if stays is None or not len(stays) or connection is None:
            return counts
        if "stay_id" not in stays.columns:
            return counts
        try:
            cur = connection.execute("SELECT * FROM stays")
            cols = [d[0] for d in cur.description]
            raw = {_norm_id(dict(zip(cols, r)).get("stay_id")): dict(zip(cols, r))
                   for r in cur.fetchall()}
        except Exception:
            return counts
        if not raw:
            return counts

        for column in self.MEDMOD_OUTCOME_COLUMNS:
            if column in stays.columns:
                continue          # the policy left it in place; nothing to do
            values = []
            restored = withheld = unresolved = 0
            for sid in stays["stay_id"]:
                src = raw.get(_norm_id(sid)) or {}
                out = pd.to_datetime(src.get("outtime"), errors="coerce")
                val = src.get(column)
                if val is None or (isinstance(val, float) and val != val):
                    values.append(None)
                elif pd.isna(out):
                    values.append(None)
                    unresolved += 1
                elif out <= cutoff:
                    values.append(val)
                    restored += 1
                else:
                    values.append(None)
                    withheld += 1
            if restored:
                stays[column] = values
                counts[f"restored:{column}"] += restored
            counts[f"withheld_still_open:{column}"] += withheld
            counts[f"unresolved_no_outtime:{column}"] += unresolved
        tables["stays"] = stays
        return counts

    def _raw_dischtime(self, connection) -> dict[str, "pd.Timestamp"]:
        """True dischtime per hadm_id from the patient database.

        The runtime view blanks this column, so availability of anything
        determined at discharge cannot be established from the view alone.
        Raw provenance can still establish it, and establishing it is what
        turns an unresolved keep into a defensible drop.
        """
        out: dict[str, Any] = {}
        if connection is None:
            return out
        try:
            cur = connection.execute("SELECT hadm_id, dischtime FROM admissions")
        except Exception:
            return out
        for hadm, dis in cur.fetchall():
            ts = pd.to_datetime(dis, errors="coerce")
            if not pd.isna(ts):
                out[_norm_id(hadm)] = ts
        return out

    def _blank_future_identifiers(
        self,
        tables: dict[str, pd.DataFrame],
        cutoff: pd.Timestamp,
        connection: sqlite3.Connection | None,
    ) -> tuple[Counter, dict[str, list[tuple[Any, str, str]]]]:
        counts = Counter()
        coordinates: dict[str, list[tuple[Any, str, str]]] = defaultdict(list)
        hadm_starts, stay_starts = self._encounter_starts(connection)
        for table, frame in tables.items():
            for column, lookup, kind in (
                ("hadm_id", hadm_starts, "hadm"),
                ("stay_id", stay_starts, "stay"),
            ):
                if column not in frame.columns:
                    continue
                for index, value in frame[column].items():
                    key = _norm_id(value)
                    if not key:
                        continue
                    state = self._encounter_state(lookup.get(key), cutoff)
                    if state == "after":
                        frame.at[index, column] = None
                        counts[f"{kind}_ids_blanked"] += 1
                        coordinates[table].append(
                            (index, column, f"future_{kind}_encounter"))
                    elif state == "unknown":
                        counts[f"unresolved_{kind}_ids_kept"] += 1
        counts["rows_with_ids_blanked"] = len({
            (table, index) for table, values in coordinates.items()
            for index, _, _ in values
        })
        return counts, coordinates

    def _cascade_children(
        self,
        tables: dict[str, pd.DataFrame],
        dropped: dict[str, dict[Any, set[str]]],
    ) -> None:
        for child, (parent, keys) in CHILD_PARENTS.items():
            cdf, pdf = tables.get(child), tables.get(parent)
            if cdf is None or cdf.empty:
                continue
            if pdf is None or any(key not in cdf.columns or key not in pdf.columns
                                  for key in keys):
                # No proof either way: retain and let unresolved accounting
                # report the missing parent linkage.
                continue
            available = {
                tuple(_norm_id(item.get(key)) for key in keys)
                for _, item in pdf.iterrows()
            }
            mask = cdf.apply(
                lambda item: tuple(_norm_id(item.get(key)) for key in keys)
                in available,
                axis=1,
            )
            for index in cdf.index[~mask]:
                dropped[child].setdefault(index, set()).add(
                    "parent_unavailable_after_cutoff")
            tables[child] = cdf.loc[mask].copy()

    def _apply_mm_event_provenance(
        self,
        frame: pd.DataFrame,
        row: Mapping[str, Any],
        cutoff: pd.Timestamp,
    ) -> tuple[pd.Series, dict[Any, set[str]], Counter]:
        drop = pd.Series(False, index=frame.index)
        reasons: dict[Any, set[str]] = defaultdict(set)
        counts = Counter()
        raw = self._load_mm_chartevents(row.get("subject_id"))
        grouped: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
        for index, item in frame.iterrows():
            grouped[_mm_event_key(item)].append(index)
        for key, indices in grouped.items():
            stores = raw.get(key, [])
            if not stores:
                counts["unresolved_rows_kept"] += len(indices)
                continue
            available = sum(value is not None and value <= cutoff for value in stores)
            late = sum(value is not None and value > cutoff for value in stores)
            unknown = sum(value is None for value in stores)
            if late and not available and not unknown:
                n_drop = min(len(indices), late)
            elif len(indices) > available + unknown:
                # At least this many indistinguishable occurrences must be
                # late by multiset cardinality; the remainder are unresolved.
                n_drop = min(late, len(indices) - available - unknown)
            else:
                n_drop = 0
            for index in indices[:n_drop]:
                drop.loc[index] = True
                reasons[index].add("mm_chartevent_storetime_after_cutoff")
            counts["rows_dropped_storetime"] += n_drop
            resolved_kept = min(len(indices) - n_drop, available)
            counts["rows_proven_available"] += resolved_kept
            counts["unresolved_rows_kept"] += len(indices) - n_drop - resolved_kept
        return drop, reasons, counts

    def _filter_reports(
        self,
        row: Mapping[str, Any],
        tables: dict[str, pd.DataFrame],
        cutoff: pd.Timestamp,
    ) -> tuple[list[str], dict[str, Any], dict[str, list[tuple[Any, str, str]]]]:
        """Drop report attachments that became readable after the cutoff.

        A report is late when its availability (see
        `_prepare_report_provenance`) is strictly after the cutoff. Late reports
        are removed from the record's `report_paths` and, member by member,
        from pipe-joined `tb_cxr.report_path` cells; every other cell value is
        left untouched. Unresolved reports are kept and counted.
        """
        paths = [str(x) for x in (row.get("report_paths") or [])]
        if self.bench != "mm" or not paths:
            return paths, {
                "attachments_in": len(paths), "kept_available": 0,
                "dropped_late": 0, "unresolved_kept": 0,
            }, defaultdict(list)

        kept: list[str] = []
        details: list[dict[str, Any]] = []
        blanked: dict[str, list[tuple[Any, str, str]]] = defaultdict(list)
        counts = Counter(attachments_in=len(paths))
        late_members: set[str] = set()
        for path in paths:
            provenance = self._report_provenance.get(path) or {}
            available = provenance.get("available_time")
            detail: dict[str, Any] = {"path": path,
                                      "classification": "unresolved"}
            if available is None:
                kept.append(path)
                counts["unresolved_kept"] += 1
                detail["reason"] = provenance.get("unresolved", "not_prepared")
            else:
                detail.update({
                    "available_time": str(available),
                    "raw_note_id": provenance.get("raw_note_id"),
                    "raw_storetime": provenance.get("raw_storetime"),
                    "patient_offset_seconds": provenance.get(
                        "patient_offset_seconds"),
                })
                if available > cutoff:
                    counts["dropped_late"] += 1
                    detail["classification"] = "after_cutoff"
                    late_members.add(_strip_package_prefix(path))
                else:
                    kept.append(path)
                    counts["kept_available"] += 1
                    detail["classification"] = "available_by_cutoff"
            details.append(detail)

        cxr = tables.get("tb_cxr")
        if late_members and cxr is not None and "report_path" in cxr.columns:
            for index, value in cxr["report_path"].items():
                if not _present(value):
                    continue
                members = [m.strip() for m in str(value).split("|") if m.strip()]
                survivors = [m for m in members if m not in late_members]
                if len(survivors) != len(members):
                    cxr.at[index, "report_path"] = (
                        "|".join(survivors) if survivors else None)
                    blanked["tb_cxr"].append(
                        (index, "report_path", "late_radiology_report"))
        return kept, {**dict(counts), "details": details}, blanked

    def apply(
        self,
        tables: Mapping[str, pd.DataFrame],
        *,
        row: Mapping[str, Any],
        policy_cutoff: Any,
    ) -> FilterResult:
        manifest_cutoff = pd.to_datetime(row.get("prediction_time"), errors="coerce")
        runtime_cutoff = pd.to_datetime(policy_cutoff, errors="coerce")
        if pd.isna(manifest_cutoff):
            raise ValueError(f"unparseable manifest prediction_time: {row.get('prediction_time')!r}")
        cutoff = manifest_cutoff if pd.isna(runtime_cutoff) else min(
            manifest_cutoff, runtime_cutoff)
        clean = {name: frame.copy() for name, frame in tables.items()
                 if frame is not None}
        dropped: dict[str, dict[Any, set[str]]] = defaultdict(dict)
        blanked: dict[str, list[tuple[Any, str, str]]] = defaultdict(list)
        table_reports: dict[str, dict[str, Any]] = {}
        event_rules = TEXT_EVENT_COLUMNS if self.bench == "text" else MM_EVENT_COLUMNS

        db_path = self._patient_db(row)
        connection = None
        if db_path and db_path.exists():
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for table, frame in list(clean.items()):
                report = Counter(input_rows=len(frame))
                row_drop = pd.Series(False, index=frame.index)
                columns = event_rules.get(table, ...)
                if columns is ...:
                    report["unresolved_rows_kept"] += len(frame)
                    report["unresolved_unknown_table"] += len(frame)
                elif columns is not None and len(frame):
                    state = _coalesced_state(frame, columns, cutoff)
                    after = state == "after"
                    unknown = state == "unknown"
                    row_drop |= after
                    report["event_after_cutoff"] += int(after.sum())
                    report["unresolved_rows_kept"] += int(unknown.sum())
                    for index in frame.index[after]:
                        dropped[table].setdefault(index, set()).add(
                            "clamped_event_time_after_cutoff")

                # Every table that exposes a store clock must also pass it.
                store_columns = tuple(c for c in ("storetime", "storedate")
                                      if c in frame.columns)
                if store_columns and len(frame):
                    store_state = _coalesced_state(frame, store_columns, cutoff)
                    after = store_state == "after"
                    unknown = store_state == "unknown"
                    row_drop |= after
                    report["store_after_cutoff"] += int(after.sum())
                    report["unresolved_store_rows_kept"] += int(
                        (unknown & ~row_drop).sum())
                    for index in frame.index[after]:
                        dropped[table].setdefault(index, set()).add(
                            "storetime_after_cutoff")

                if table == "prescriptions" and self.bench == "text":
                    rx_drop, rx_reasons, rx_counts = self._filter_prescriptions(
                        frame, cutoff, connection)
                    row_drop |= rx_drop
                    report.update({f"prescription_{k}": v
                                   for k, v in rx_counts.items()})
                    for index, reasons in rx_reasons.items():
                        dropped[table].setdefault(index, set()).update(reasons)

                if (table == "events" and self.bench == "mm"
                        and str(row.get("source_benchmark", "")).lower() == "medmod"):
                    mm_drop, mm_reasons, mm_counts = self._apply_mm_event_provenance(
                        frame, row, cutoff)
                    row_drop |= mm_drop
                    report.update({f"raw_chartevents_{k}": v
                                   for k, v in mm_counts.items()})
                    for index, reasons in mm_reasons.items():
                        dropped[table].setdefault(index, set()).update(reasons)

                clean[table] = frame.loc[~row_drop].copy()
                report["rows_dropped"] = int(row_drop.sum())
                report["output_rows"] = len(clean[table])
                table_reports[table] = dict(report)

            # Cascades occur before identifier blanking so a blanked foreign
            # key never causes a legitimate child row to disappear.
            self._cascade_children(clean, dropped)

            field_counts = Counter()
            for table, guards in FIELD_GUARDS.items():
                frame = clean.get(table)
                if frame is None or frame.empty:
                    continue
                for field, sources in guards:
                    if field not in frame.columns:
                        continue
                    state = _coalesced_state(frame, sources, cutoff)
                    for index in frame.index[state == "after"]:
                        if _present(frame.at[index, field]):
                            frame.at[index, field] = None
                            field_counts[f"{table}.{field}"] += 1
                            blanked[table].append(
                                (index, field, "field_available_after_cutoff"))
                    field_counts[f"unresolved:{table}.{field}"] += int(
                        ((state == "unknown") & frame[field].map(_present)).sum())

            if self.bench == "mm":
                for _k, _v in self._restore_medmod_outcomes(
                        clean, row, cutoff, connection).items():
                    field_counts[f"medmod_outcome_{_k}"] += _v

            # Resolve, from the patient database, what the runtime view can no
            # longer prove (its dischtime column is blanked).
            _raw_dis = self._raw_dischtime(connection)
            if _raw_dis:
                # `edouttime` is deliberately absent: ED departure is
                # determined at ED exit, not at hospital discharge, so keying
                # it off dischtime would blank values that were readable at
                # prediction time. It guards itself via FIELD_GUARDS.
                _disch_fields = {
                    "admissions": ("dischtime", "deathtime", "discharge_location",
                                   "hospital_expire_flag"),
                }
                for _t, _fields in _disch_fields.items():
                    _fr = clean.get(_t)
                    if _fr is None or _fr.empty or "hadm_id" not in _fr.columns:
                        continue
                    for _idx in _fr.index:
                        _d = _raw_dis.get(_norm_id(_fr.at[_idx, "hadm_id"]))
                        if _d is None or _d <= cutoff:
                            continue
                        for _f in _fields:
                            if _f in _fr.columns and _present(_fr.at[_idx, _f]):
                                _fr.at[_idx, _f] = None
                                field_counts[f"raw_dischtime:{_t}.{_f}"] += 1
                                blanked[_t].append(
                                    (_idx, _f, "determined_at_discharge_after_cutoff"))

                # A discharge summary cannot be read before the discharge that
                # produced it, whatever its (midnight-dated) charttime says.
                for _t in ("discharge", "discharge_detail"):
                    _fr = clean.get(_t)
                    if _fr is None or _fr.empty or "hadm_id" not in _fr.columns:
                        continue
                    _drop = [
                        _idx for _idx in _fr.index
                        if (_d := _raw_dis.get(_norm_id(_fr.at[_idx, "hadm_id"])))
                        is not None and _d > cutoff
                    ]
                    if _drop:
                        field_counts[f"raw_dischtime_rows_dropped:{_t}"] += len(_drop)
                        for _idx in _drop:
                            dropped[_t].setdefault(_idx, set()).add(
                                "discharge_summary_available_after_cutoff")
                        clean[_t] = _fr.drop(index=_drop)

                # Discharge cascade: a detail row whose parent note is gone is
                # content-free and timestamp-free, so no sweep can see it -- but
                # its mere existence says the index admission produced a
                # discharge summary, i.e. that it ended.
                _notes = clean.get("discharge")
                _det = clean.get("discharge_detail")
                if (_det is not None and len(_det) and "note_id" in _det.columns):
                    _live = set()
                    if _notes is not None and "note_id" in _notes.columns:
                        _live = {_norm_scalar(v) for v in _notes["note_id"]}
                    _orphan = [_i for _i in _det.index
                               if _norm_scalar(_det.at[_i, "note_id"]) not in _live]
                    if _orphan:
                        field_counts["orphan_discharge_detail_dropped"] += len(_orphan)
                        for _i in _orphan:
                            dropped["discharge_detail"].setdefault(_i, set()).add(
                                "parent_discharge_note_after_cutoff")
                        clean["discharge_detail"] = _det.drop(index=_orphan)

            for table, (sources, fields) in COMPLETION_GUARDS.items():
                frame = clean.get(table)
                if frame is None or frame.empty:
                    continue
                states = [_coalesced_state(frame, (source,), cutoff)
                          for source in sources if source in frame.columns]
                if not states:
                    continue
                completion_after = pd.concat(states, axis=1).eq("after").any(axis=1)
                completion_unknown = pd.concat(states, axis=1).eq("unknown").all(axis=1)
                for index in frame.index[completion_after]:
                    for field in fields:
                        if field in frame.columns and _present(frame.at[index, field]):
                            frame.at[index, field] = None
                            field_counts[f"{table}.{field}"] += 1
                            blanked[table].append(
                                (index, field, "completion_after_cutoff"))
                field_counts[f"unresolved_completion:{table}"] += int(
                    completion_unknown.sum())

            id_counts, id_coordinates = self._blank_future_identifiers(
                clean, cutoff, connection)
            for table, values in id_coordinates.items():
                blanked[table].extend(values)

            report_paths, report_availability, report_blanked = self._filter_reports(
                row, clean, cutoff)
            for table, values in report_blanked.items():
                blanked[table].extend(values)
        finally:
            if connection is not None:
                connection.close()

        # Cascade row counts were applied after the first per-table report.
        for table, frame in clean.items():
            table_reports.setdefault(table, {})["final_output_rows"] = len(frame)
            cascaded = sum(
                "parent_unavailable_after_cutoff" in reasons
                for reasons in dropped.get(table, {}).values())
            if cascaded:
                table_reports[table]["parent_rows_dropped"] = cascaded

        all_reasons = Counter()
        for table_reasons in dropped.values():
            for reasons in table_reasons.values():
                all_reasons.update(reasons)
        unresolved_rows = sum(
            values.get("unresolved_rows_kept", 0)
            + values.get("unresolved_store_rows_kept", 0)
            + values.get("prescription_unresolved_rows_kept", 0)
            + values.get("raw_chartevents_unresolved_rows_kept", 0)
            for values in table_reports.values()
        )
        report = {
            "filter_version": FILTER_VERSION,
            "label_independent": True,
            "policy_cutoff": None if pd.isna(runtime_cutoff) else str(runtime_cutoff),
            "manifest_prediction_time": str(manifest_cutoff),
            "effective_cutoff": str(cutoff),
            "cutoff_clamped": bool(not pd.isna(runtime_cutoff)
                                   and runtime_cutoff > manifest_cutoff),
            "tables": table_reports,
            "rows_removed_by_reason": dict(all_reasons),
            "rows_removed_total": sum(len(x) for x in dropped.values()),
            "field_values_blanked": dict(field_counts),
            "encounter_identifiers": dict(id_counts),
            "report_availability": report_availability,
            "unresolved_row_checks_kept": int(unresolved_rows),
            "unresolved_report_attachments_kept": int(
                report_availability.get("unresolved_kept", 0)),
            "unresolved_identifier_values_kept": int(
                id_counts.get("unresolved_hadm_ids_kept", 0)
                + id_counts.get("unresolved_stay_ids_kept", 0)),
        }
        return FilterResult(
            clean, cutoff, report_paths, report, dropped, blanked)
