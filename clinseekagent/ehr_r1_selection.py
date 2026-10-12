#!/usr/bin/env python3
"""Recover EHR-R1's curated-input selection and re-apply it to a filtered pool.

Why this module exists
----------------------
The text *Curated Input* (``recent``) view must keep the curated-input
distribution of the source benchmark: the evidence EHR-R1 originally selected
for each question, minus whatever the temporal policy removes.  Using a newly
designed rule instead (for example "the most recent 20 rows per table") would
change the input distribution, so a score difference could come from the new
sampling rather than from the removal of post-cutoff information.  The Recent
view is therefore

    evidence EHR-R1 actually selected  INTERSECT  temporally filtered pool

How the selection is recovered
------------------------------
The upstream renderer lives in MAGIC-AI4Med/EHR-R1, not here.  What is
available is its output: the ``input`` field of every EHR-Bench row holds
EHR-R1's text serialisation of the curated input, one
``## <Section> [<timestamp>]`` block per selected evidence item.

The renderer aggregates rows into one block, so per-row identity is not
recoverable from the text.  The unit of selection is a block, keyed by its
table and its printed timestamp.

What a block covers was determined empirically against the per-patient
databases.  Two models were compared, counting the rows each model picks up
versus the rows EHR-R1 actually printed inside the block:

===============  ======================  =========================
model            rows recovered          blocks reproduced exactly
===============  ======================  =========================
point   ``t``    69.9% of printed rows   -
interval         **99.4%**               **98.3%**
===============  ======================  =========================

The renderer merges a *run* of consecutive same-table events into a single
block labelled with the first event's timestamp.  For example, a
``## Chart Events [02:19:00]`` block can hold rows charted at 02:19, 02:21 and
02:22 when the next chartevents block starts at 02:23.  So block ``i`` of a
table covers

    [ t_i , t_{i+1} )        (the last block runs to the cutoff)

Both models are implemented.  ``parse_selection`` returns the point form;
``parse_selection_intervals`` returns the interval form, and ``select_recent``
accepts either.  The interval form is the faithful one and is what the corpus
builder uses; point selection drops roughly 30% of the rows EHR-R1 showed the
model.

Anchor columns
--------------
For each table, exactly one pool column carries the timestamp the renderer
printed in the section header (``TABLE_ANCHOR``).  These were established
empirically by scoring every time-like column of every table against the
parsed section timestamps; the winners are unambiguous (100% for
poe/prescriptions/transfers/..., 91-97% for the storetime-gated tables, where
the shortfall is exactly the rows the temporal policy removes).

Two tables need more than a column name:

* ``diagnosis`` -- the strict text policy drops the ED-diagnosis charttime and
  emits ``available_time = edstays.outtime + 24h``.  The renderer printed the
  pre-offset time, so the anchor is ``available_time - 24h``.
* ``patients`` / ``diagnoses_icd`` -- rendered with a literal ``[None]``
  timestamp.  They are declared static: every row gets a null anchor, so the
  parsed ``None`` key matches the whole (already filtered) table.  Null anchors
  are handled uniformly, so a microbiology row with a missing charttime matches
  a ``[None]`` microbiology section by the same code path.

Child tables (``poe_detail``, ``radiology_detail``, ``emar_detail``,
``discharge_detail``) are never rendered as their own sections; they follow
their parent and are kept only for parent rows that survived selection.
Tables that EHR-R1 never rendered at all (``drgcodes``, ``hcpcsevents``) are not
part of the curated input and are dropped, which is recorded in the report.

This module only reads; ``scripts/clean_data/build_evidence_corpus.py`` uses it
to build the Recent view.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, NamedTuple, Optional

import pandas as pd

SELECTION_VERSION = "ehr-r1-selection-v1"

#: ``## <Section> [<timestamp>]`` on a line of its own.  Deliberately strict:
#: discharge-summary free text contains markdown-ish ``## Hepatic
#: encephalopathy: ...`` lines that must not be read as section headers.
SECTION_RE = re.compile(r"^## (.+?) \[(.*?)\]\s*$")

NULL_TS_TOKENS = {"None", "", "nan", "NaN", "NaT", "null"}

#: Section label as printed by EHR-R1 -> table name in the filtered pool.
#: ``Labotary`` is misspelled upstream; we match the source, not the spelling.
SECTION_TO_TABLE: dict[str, str] = {
    "Patient Demographics": "patients",
    "EDstays": "edstays",
    "Triage": "triage",
    "Chart Events": "chartevents",
    "Provider Order Entry": "poe",
    "Pharmacy": "pharmacy",
    "Labotary Test Events": "labevents",
    "Prescriptions": "prescriptions",
    "Input Events": "inputevents",
    "Ingredient Events": "ingredientevents",
    "Vitalsign": "vitalsign",
    "Output Events": "outputevents",
    "Datetime Events": "datetimeevents",
    "Transfers": "transfers",
    "Radiology Examinations": "radiology",
    "Pyxis": "pyxis",
    "Microbiology Test Events": "microbiologyevents",
    "Procedure Events": "procedureevents",
    "Admissions": "admissions",
    "Discharge": "discharge",
    "Services": "services",
    "Medrecon": "medrecon",
    "ICUstays": "icustays",
    "Online Medical Record": "omr",
    "Electronic Medicine Administration Record": "emar",
    # Observed in the data, absent from the starting map:
    "ED Diagnoses on International Classification of Diseases": "diagnosis",
    "Diagnoses on International Classification of Diseases": "diagnoses_icd",
    "Procedures on International Classification of Diseases": "procedures_icd",
}

#: table -> (anchor column, offset in seconds applied to the CLEAN column to get
#: back the RENDERED timestamp).  ``None`` marks a statically rendered table.
TABLE_ANCHOR: dict[str, Optional[tuple[str, int]]] = {
    "patients": None,
    "diagnoses_icd": None,
    "admissions": ("admittime", 0),
    "chartevents": ("charttime", 0),
    "datetimeevents": ("charttime", 0),
    "diagnosis": ("available_time", -86400),
    "discharge": ("charttime", 0),
    "edstays": ("intime", 0),
    "emar": ("charttime", 0),
    "icustays": ("intime", 0),
    "ingredientevents": ("starttime", 0),
    "inputevents": ("starttime", 0),
    "labevents": ("charttime", 0),
    "medrecon": ("charttime", 0),
    "microbiologyevents": ("charttime", 0),
    "omr": ("chartdate", 0),
    "outputevents": ("charttime", 0),
    "pharmacy": ("entertime", 0),
    "poe": ("ordertime", 0),
    "prescriptions": ("starttime", 0),
    "procedureevents": ("starttime", 0),
    "procedures_icd": ("chartdate", 0),
    "pyxis": ("charttime", 0),
    "radiology": ("charttime", 0),
    "services": ("transfertime", 0),
    "transfers": ("intime", 0),
    "triage": ("charttime", 0),
    "vitalsign": ("charttime", 0),
}

#: child table -> (parent table, join keys).  Mirrors CHILD_POLICIES in
#: agentlite.commons.temporal_policy; duplicated rather than imported so this
#: module stays a pure reader with no runtime dependency on the policy package.
CHILD_OF: dict[str, tuple[str, tuple[str, ...]]] = {
    "poe_detail": ("poe", ("poe_id",)),
    "radiology_detail": ("radiology", ("note_id",)),
    "discharge_detail": ("discharge", ("note_id",)),
    "emar_detail": ("emar", ("emar_id",)),
}

#: Present in the filtered pool, never rendered by EHR-R1, so never part of the
#: curated input.  Dropped from Recent, counted in the report.
NEVER_RENDERED = ("drgcodes", "hcpcsevents", "cost")


def _parse_ts(raw: str) -> Optional[pd.Timestamp]:
    if raw is None or str(raw).strip() in NULL_TS_TOKENS:
        return None
    ts = pd.to_datetime(str(raw).strip(), errors="coerce")
    return None if pd.isna(ts) else ts


def iter_sections(input_text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(section_label, raw_timestamp_string)`` in document order."""
    for line in str(input_text or "").split("\n"):
        m = SECTION_RE.match(line)
        if m:
            yield m.group(1), m.group(2)


def parse_selection(input_text: str) -> dict[str, set]:
    """EHR-R1's curated input -> ``{table: {timestamp | None, ...}}``.

    Unmappable section labels are skipped here; use :func:`parse_selection_report`
    when you need to see them.  A ``None`` member means the renderer printed a
    null timestamp for that item.
    """
    sel: dict[str, set] = {}
    for label, raw in iter_sections(input_text):
        table = SECTION_TO_TABLE.get(label)
        if table is None:
            continue
        sel.setdefault(table, set()).add(_parse_ts(raw))
    return sel


class Block(NamedTuple):
    """One rendered evidence block."""
    table: str
    start: Optional[pd.Timestamp]   # None for a ``[None]`` header
    end: Optional[pd.Timestamp]     # start of the next block of the same table
    rendered_rows: int              # rows EHR-R1 printed inside the block
    order: int                      # position in the document


def _block_rows(lines: list[str]) -> int:
    """How many source rows a rendered block body represents.

    A block is either a markdown table (one data line per row) or a ``- Key:
    value`` field list (exactly one row).
    """
    sep = [i for i, l in enumerate(lines) if l.startswith("| ---")]
    if sep:
        return sum(1 for l in lines[sep[0] + 1:] if l.startswith("|"))
    return 1 if any(l.startswith("- ") for l in lines) else 0


def parse_blocks(input_text: str) -> dict[str, list[Block]]:
    """EHR-R1's curated input -> ``{table: [Block, ...]}`` in document order.

    ``end`` is the start of the next block of the *same* table, or None for that
    table's last block.  ``[None]``-timestamped blocks carry no interval: they
    are matched by null anchor instead and never bound another block.
    """
    lines = str(input_text or "").split("\n")
    idx = [i for i, l in enumerate(lines) if SECTION_RE.match(l)]
    per: dict[str, list[Block]] = {}
    for n, i in enumerate(idx):
        m = SECTION_RE.match(lines[i])
        table = SECTION_TO_TABLE.get(m.group(1))
        if table is None:
            continue
        j = idx[n + 1] if n + 1 < len(idx) else len(lines)
        per.setdefault(table, []).append(
            Block(table, _parse_ts(m.group(2)), None,
                  _block_rows(lines[i + 1:j]), n)
        )
    out: dict[str, list[Block]] = {}
    for table, blocks in per.items():
        timed = sorted([b for b in blocks if b.start is not None],
                       key=lambda b: (b.start, b.order))
        null = [b for b in blocks if b.start is None]
        closed = [b._replace(end=timed[k + 1].start if k + 1 < len(timed) else None)
                  for k, b in enumerate(timed)]
        out[table] = closed + null
    return out


def parse_selection_intervals(input_text: str) -> dict[str, list[Block]]:
    """Alias of :func:`parse_blocks`, named for the selection it expresses."""
    return parse_blocks(input_text)


def parse_selection_report(input_text: str) -> dict[str, Any]:
    """:func:`parse_selection` plus everything a validator needs to audit it."""
    sel: dict[str, set] = {}
    n_sections = 0
    per_section: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    null_ts: dict[str, int] = {}
    unparseable: dict[str, int] = {}
    for label, raw in iter_sections(input_text):
        n_sections += 1
        per_section[label] = per_section.get(label, 0) + 1
        table = SECTION_TO_TABLE.get(label)
        if table is None:
            unmapped[label] = unmapped.get(label, 0) + 1
            continue
        ts = _parse_ts(raw)
        if ts is None:
            if str(raw).strip() in NULL_TS_TOKENS:
                null_ts[label] = null_ts.get(label, 0) + 1
            else:
                unparseable[label] = unparseable.get(label, 0) + 1
        sel.setdefault(table, set()).add(ts)
    return {
        "selection": sel,
        "sections": n_sections,
        "per_section": per_section,
        "unmapped": unmapped,
        "null_timestamp": null_ts,
        "unparseable_timestamp": unparseable,
        "selected_keys": sum(len(v) for v in sel.values()),
    }


def anchor_series(df: pd.DataFrame, table: str) -> pd.Series:
    """Per-row rendered timestamp for ``table``; all-NaT for static tables."""
    spec = TABLE_ANCHOR.get(table, ...)
    if spec is None or spec is ...:
        # Static table, or a table with no declared anchor: null anchor for
        # every row, so only a ``[None]`` selection key can match it.
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    col, offset = spec
    if col not in df.columns:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    parsed = pd.to_datetime(df[col], errors="coerce")
    if offset:
        parsed = parsed + pd.Timedelta(seconds=offset)
    return parsed


def _is_interval(selection: dict) -> bool:
    for v in selection.values():
        return isinstance(v, (list, tuple))
    return False


def _match_point(anchor: pd.Series, keys: set) -> tuple[pd.Series, set]:
    wanted = {k for k in keys if k is not None}
    mask = anchor.isin(list(wanted)) if wanted else pd.Series(False, index=anchor.index)
    if None in keys:
        mask = mask | anchor.isna()
    present = set(anchor.dropna().unique())
    matched = {k for k in wanted if k in present}
    if None in keys and bool(anchor.isna().any()):
        matched.add(None)
    return mask, matched


def _ts_key(ts) -> Optional[str]:
    """Report form of a block/selection key: a string, or JSON ``null``.

    A block EHR-R1 rendered with a literal ``[None]`` header has no timestamp.
    Emitting the *string* ``None`` for it would make such entries
    indistinguishable from a real timestamp in ``removed_timestamps``, so a
    reader counting removed evidence could not tell a null-anchored block from
    one the temporal policy deleted.  ``None`` serialises to ``null`` and is
    unambiguous.
    """
    return None if ts is None else str(ts)


def _sorted_keys(keys) -> list[Optional[str]]:
    """Sort report keys with ``null`` first; ``None`` is not comparable to str."""
    return sorted(keys, key=lambda k: (k is not None, k or ""))


def _match_intervals(
    anchor: pd.Series,
    blocks: list[Block],
    cutoff: Optional[pd.Timestamp],
) -> tuple[pd.Series, list[dict[str, Any]]]:
    mask = pd.Series(False, index=anchor.index)
    detail: list[dict[str, Any]] = []
    for b in blocks:
        if b.start is None:
            hit = anchor.isna()
        else:
            hit = anchor >= b.start
            if b.end is not None:
                hit = hit & (anchor < b.end)
            elif cutoff is not None:
                hit = hit & (anchor <= cutoff)
        n = int(hit.sum())
        mask = mask | hit
        detail.append({
            "start": _ts_key(b.start),
            "end": _ts_key(b.end),
            "rendered_rows": b.rendered_rows,
            "rows_kept": n,
        })
    return mask, detail


def select_recent(
    clean_tables: dict[str, pd.DataFrame],
    selection: dict[str, Any],
    cutoff: Any = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Intersect the filtered pool with EHR-R1's selection.

    ``clean_tables`` must be the temporally filtered pool (the Full view).  ``selection``
    is either the point form from :func:`parse_selection` (``{table: {ts, ...}}``)
    or the interval form from :func:`parse_selection_intervals`
    (``{table: [Block, ...]}``); the form is detected from the value type.

    Returns the new Recent Record and a report whose per-table entry records how
    many evidence blocks EHR-R1 selected, how many still have at least one
    surviving row, which ones have none -- those are the evidence items the
    temporal policy removed -- how many rows EHR-R1 rendered, and how many rows
    were emitted.
    """
    interval = _is_interval(selection)
    cut = pd.to_datetime(cutoff, errors="coerce") if cutoff is not None else None
    if cut is not None and pd.isna(cut):
        cut = None
    out: dict[str, pd.DataFrame] = {}
    tables_report: dict[str, Any] = {}

    for table, sel in sorted(selection.items()):
        blocks = list(sel) if interval else None
        n_keys = len(blocks) if interval else len(sel)
        rendered = sum(b.rendered_rows for b in blocks) if interval else None
        df = clean_tables.get(table)
        if df is None or len(df) == 0:
            tables_report[table] = {
                "selected_keys": n_keys, "matched_keys": 0, "removed_keys": n_keys,
                "removed_timestamps": _sorted_keys(
                    _ts_key(b.start) for b in blocks
                ) if interval else _sorted_keys(_ts_key(k) for k in sel),
                "rows_emitted": 0, "clean_rows_available": 0,
                "rendered_rows": rendered, "rows_removed": rendered,
                "anchor": _anchor_name(table),
                "table_absent_from_clean_pool": True,
            }
            continue
        anchor = anchor_series(df, table)
        if interval:
            mask, detail = _match_intervals(anchor, blocks, cut)
            matched = [d for d in detail if d["rows_kept"]]
            removed = [d for d in detail if not d["rows_kept"]]
            n_matched = len(matched)
            removed_ts = _sorted_keys(d["start"] for d in removed)
        else:
            mask, matched_keys = _match_point(anchor, sel)
            n_matched = len(matched_keys)
            removed_ts = _sorted_keys(_ts_key(k) for k in (sel - matched_keys))
            detail = None
        kept = df.loc[mask]
        if len(kept):
            out[table] = kept
        tables_report[table] = {
            "selected_keys": n_keys,
            "matched_keys": n_matched,
            "removed_keys": n_keys - n_matched,
            "removed_timestamps": removed_ts,
            "rows_emitted": int(len(kept)),
            "clean_rows_available": int(len(df)),
            "rendered_rows": rendered,
            "rows_removed": (max(0, rendered - int(len(kept)))
                             if rendered is not None else None),
            "rows_over_selected": (max(0, int(len(kept)) - rendered)
                                   if rendered is not None else None),
            "anchor": _anchor_name(table),
            "table_absent_from_clean_pool": False,
        }

    # Child tables follow their parent: keep detail rows whose parent row
    # survived selection.  They are not independently selectable evidence.
    child_report: dict[str, Any] = {}
    for child, (parent, keys) in CHILD_OF.items():
        df = clean_tables.get(child)
        if df is None or len(df) == 0:
            continue
        kept_parent = out.get(parent)
        if kept_parent is None or not all(k in df.columns for k in keys) \
                or not all(k in kept_parent.columns for k in keys):
            child_report[child] = {"rows_emitted": 0, "parent": parent,
                                   "reason": "parent not selected"}
            continue
        allowed = set(map(tuple, kept_parent[list(keys)].astype(str).values))
        mask = df[list(keys)].astype(str).apply(tuple, axis=1).isin(allowed)
        kept = df.loc[mask]
        if len(kept):
            out[child] = kept
        child_report[child] = {"rows_emitted": int(len(kept)),
                               "parent": parent, "reason": "followed parent"}

    dropped = {
        name: int(len(df))
        for name, df in clean_tables.items()
        if df is not None and len(df)
        and name not in out and name not in selection and name not in CHILD_OF
    }

    report = {
        "selection_version": SELECTION_VERSION,
        "mode": "interval" if interval else "point",
        "tables": tables_report,
        "children": child_report,
        "clean_tables_not_in_selection": dropped,
        "selected_keys": sum(v["selected_keys"] for v in tables_report.values()),
        "matched_keys": sum(v["matched_keys"] for v in tables_report.values()),
        "removed_keys": sum(v["removed_keys"] for v in tables_report.values()),
        "rendered_rows": sum(v["rendered_rows"] or 0 for v in tables_report.values()),
        "rows_removed": sum(v["rows_removed"] or 0 for v in tables_report.values()),
        # Three counts, deliberately not one.  Every per-table `rows_emitted`
        # and `rendered_rows` above is parent-only.  Child rows are not
        # selected by EHR-R1 at all -- they are carried because their parent
        # row was (`children.*.reason == "followed parent"`) -- so they must not
        # enter a selection-retention ratio.
        #
        # Retention = rows_emitted / rendered_rows, both parent-only.  The
        # physical row count of the recent view is
        # `rows_emitted_including_children`.
        "rows_emitted": sum(
            int(len(df)) for name, df in out.items() if name not in CHILD_OF
        ),
        "child_rows_emitted": sum(
            int(len(df)) for name, df in out.items() if name in CHILD_OF
        ),
        "rows_emitted_including_children": sum(
            int(len(df)) for df in out.values()
        ),
    }
    return out, report


def _anchor_name(table: str) -> str:
    spec = TABLE_ANCHOR.get(table, ...)
    if spec is None:
        return "static (no rendered timestamp)"
    if spec is ...:
        return "unknown table"
    col, offset = spec
    return col if not offset else f"{col} {offset:+d}s"
