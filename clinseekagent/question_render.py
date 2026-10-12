#!/usr/bin/env python3
"""Re-render the text `question` prompt from the temporally filtered pool.

Why this module exists
----------------------
`question` in the EHR-Bench source manifest is

    <task_instruction> ... </task_instruction>
    <patient_info> Current Time: <prediction_time> </patient_info>
    <patient_timeline>
    <the `input` field verbatim -- EHR-R1's render of the curated input>
    </patient_timeline>
    <candidate_answers> ... </candidate_answers>

The `input` block is EHR-R1's own serialisation of the curated input, produced
from the unfiltered record.  Copying `question` through unchanged would hand
every consumer the evidence that the temporal policy removed from the CSVs
(rows that only became available after the prediction time).

So `question` is rebuilt here.  The template (instruction, candidate list,
`<patient_timeline>` wrapper) is preserved byte-for-byte; only the timeline body
is replaced, by a render of the tables the corpus actually publishes.  After
this, `question` and `evidence/<qid>/recent/*.csv` describe the same evidence by
construction.

What the render is, and is not
------------------------------
It is NOT a reimplementation of EHR-R1's renderer.  EHR-R1 projects and renames
columns (`Item_Name`, `Ref_Range_Lower`, ...) through joins not available here,
and its per-row identity is not recoverable from its own output -- a block
prints N rows with no per-row timestamp, so a post-cutoff row inside a
surviving block cannot be deleted from the text.  That is why the text is
re-rendered rather than filtered.

What this render is: EHR-R1's *section grammar* (`## <Label> [<timestamp>]`,
one block per run of consecutive same-table events, markdown table body) filled
with the filtered pool's own columns and values.  Every column it prints is a
column already published in the corresponding CSV, so the render can add no
information the evidence tree does not already carry.

Stated time
-----------
`restated_times()` computes the latest event visible in the filtered pool and
`latest + 1s` (EHR-Bench's own "one second after the latest observed event"
convention).  The corpus builder records the latest visible event but keeps the
benchmark's original `prediction_time` as the stated `Current Time`, so
`rebuild_question` is called with identical old/new times and the template's
time strings are left unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

from ehr_r1_selection import CHILD_OF, SECTION_TO_TABLE, anchor_series

RENDER_VERSION = "clean-pool-question-render-v1"

#: table -> the section label EHR-R1 printed for it.  Inverted from the parse
#: map so the two can never drift apart.
TABLE_TO_SECTION: dict[str, str] = {v: k for k, v in SECTION_TO_TABLE.items()}

#: EHR-R1 folds detail rows into their parent block and never labels them.  The
#: filtered pool publishes them as their own tables, so they need a label; it is
#: derived from the parent's so a reader can see the relationship.
for _child, (_parent, _keys) in CHILD_OF.items():
    TABLE_TO_SECTION.setdefault(_child, f"{TABLE_TO_SECTION[_parent]} Detail")


def section_label(table: str) -> str:
    """Section label for a table; unmapped tables keep a readable fallback."""
    if table in TABLE_TO_SECTION:
        return TABLE_TO_SECTION[table]
    return " ".join(p.capitalize() for p in str(table).split("_"))


def column_label(col: str) -> str:
    """`ref_range_lower` -> `Ref_Range_Lower`, EHR-R1's cosmetic convention."""
    return "_".join(p.capitalize() for p in str(col).split("_"))


def _blank(v: Any) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() in {"", "nan", "NaN", "NaT", "None", "<NA>"}


def _cell(v: Any) -> str:
    if _blank(v):
        return ""
    s = str(v)
    # A literal pipe would break the markdown table this row is printed into.
    return s.replace("|", "/").replace("\r\n", " ").replace("\n", " ").strip()


def _anchor_for(table: str, df: pd.DataFrame,
                parents: dict[str, tuple[pd.DataFrame, pd.Series]]) -> pd.Series:
    """Per-row rendered timestamp, including for child-detail tables.

    A child row is placed at its parent row's timestamp, so `poe_detail` sorts
    next to the `poe` order it belongs to instead of collapsing into an
    undated block at the top of the timeline.
    """
    if table not in CHILD_OF:
        return anchor_series(df, table)
    parent, keys = CHILD_OF[table]
    got = parents.get(parent)
    if got is None or not all(k in df.columns for k in keys):
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    pdf, panchor = got
    if not all(k in pdf.columns for k in keys):
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    lookup: dict[tuple, Any] = {}
    for key, ts in zip(map(tuple, pdf[list(keys)].astype(str).values), panchor):
        lookup.setdefault(key, ts)
    mapped = [lookup.get(k, pd.NaT)
              for k in map(tuple, df[list(keys)].astype(str).values)]
    return pd.Series(pd.to_datetime(pd.Series(mapped, index=df.index),
                                    errors="coerce"), index=df.index)


def _anchor_column(table: str) -> Optional[str]:
    """The column whose value the section header prints, if any."""
    from ehr_r1_selection import TABLE_ANCHOR
    spec = TABLE_ANCHOR.get(table)
    return spec[0] if spec else None


def _render_block(table: str, df: pd.DataFrame, rows: list,
                  start: Optional[pd.Timestamp]) -> list[str]:
    """One `## <Label> [<ts>]` block over `rows` (positional indices)."""
    sub = df.iloc[rows]
    drop = {c for c in sub.columns if c == _anchor_column(table)}
    # An all-blank column carries nothing; EHR-R1 omits those too.
    keep = [c for c in sub.columns
            if c not in drop and not all(_blank(v) for v in sub[c])]
    header = (f"## {section_label(table)} "
              f"[{'None' if start is None or pd.isna(start) else start}]")
    if not keep:
        return [header, ""]
    if len(sub) == 1:
        body = [f"- {column_label(c)}: {_cell(sub.iloc[0][c])}"
                for c in keep if not _blank(sub.iloc[0][c])]
        return [header] + body + [""]
    body = ["| " + " | ".join(column_label(c) for c in keep) + " |",
            "| " + " | ".join("---" for _ in keep) + " |"]
    for _, r in sub.iterrows():
        body.append("| " + " | ".join(_cell(r[c]) for c in keep) + " |")
    return [header] + body + [""]


def compute_anchors(tables: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """Per-row rendered timestamp for every non-empty table, by table name.

    Parents are resolved first so that child-detail tables can look their
    parent row's timestamp up; a child inherits the timestamp of the row it
    belongs to rather than collapsing into the undated block.

    Split out of `render_timeline` so that a caller which needs to *select*
    rows by recency -- the Full Record truncation in
    `clinseekagent/record_context.py` -- places every row on exactly the same
    timeline the renderer will print it on. Two implementations of "when did
    this row happen" would let the kept window and the rendered window drift
    apart silently.
    """
    anchors: dict[str, pd.Series] = {}
    parents: dict[str, tuple[pd.DataFrame, pd.Series]] = {}
    for name in sorted(tables):
        df = tables[name]
        if df is None or len(df) == 0 or name in CHILD_OF:
            continue
        anchors[name] = _anchor_for(name, df, parents)
        parents[name] = (df, anchors[name])
    for name in sorted(tables):
        df = tables[name]
        if df is None or len(df) == 0 or name not in CHILD_OF:
            continue
        anchors[name] = _anchor_for(name, df, parents)
    return anchors


def render_timeline(tables: dict[str, pd.DataFrame]) -> str:
    """Render a view of the filtered pool in EHR-R1's section grammar.

    Undated rows (static tables such as `patients`, and rows whose anchor is
    null) come first under a `[None]` header, exactly as EHR-R1 prints them.
    Dated rows follow in timestamp order, grouped into one block per run of
    consecutive same-table events.
    """
    anchors = compute_anchors(tables)

    lines: list[str] = []

    # 1 - undated rows, one block per table
    for name in sorted(anchors):
        df, a = tables[name], anchors[name]
        rows = [i for i, ts in enumerate(a) if pd.isna(ts)]
        if rows:
            lines += _render_block(name, df, rows, None)

    # 2 - dated rows in time order, runs of the same table merged into a block
    events: list[tuple[pd.Timestamp, str, int]] = []
    for name in sorted(anchors):
        for i, ts in enumerate(anchors[name]):
            if not pd.isna(ts):
                events.append((ts, name, i))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    run_table: Optional[str] = None
    run_start: Optional[pd.Timestamp] = None
    run_rows: list[int] = []
    for ts, name, i in events:
        if name != run_table:
            if run_table is not None:
                lines += _render_block(run_table, tables[run_table],
                                       run_rows, run_start)
            run_table, run_start, run_rows = name, ts, []
        run_rows.append(i)
    if run_table is not None:
        lines += _render_block(run_table, tables[run_table], run_rows, run_start)

    return "\n".join(lines).rstrip("\n")


def latest_visible_event(tables: dict[str, pd.DataFrame]) -> Optional[pd.Timestamp]:
    """The latest parseable timestamp anywhere in the filtered pool.

    Every value here has already passed the temporal filters, so the maximum is
    the point at which the published record genuinely stops.
    """
    best: Optional[pd.Timestamp] = None
    for df in tables.values():
        if df is None or len(df) == 0:
            continue
        for c in df.columns:
            lc = str(c).lower()
            if "time" not in lc and "date" not in lc:
                continue
            m = pd.to_datetime(df[c], errors="coerce").max()
            if pd.isna(m):
                continue
            if best is None or m > best:
                best = m
    return best


def restated_times(
    tables: dict[str, pd.DataFrame],
    fallback: Any,
) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """`(stated_current_time, latest_visible_event)` for a case.

    The stated current time is `latest + 1s`, unconditionally and identically
    for every case, so the distance between the stated time and the end of the
    record is a constant and can carry no label information.  A pool with no
    parseable timestamp at all keeps `fallback` (the policy cutoff).
    """
    last = latest_visible_event(tables)
    if last is None:
        fb = pd.to_datetime(fallback, errors="coerce")
        return (None if pd.isna(fb) else fb), None
    return last + pd.Timedelta(seconds=1), last


_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def rebuild_question(
    question: str,
    original_input: str,
    timeline: str,
    *,
    old_prediction_time: Any = None,
    old_latest_event_time: Any = None,
    new_prediction_time: Any = None,
    new_latest_event_time: Any = None,
) -> tuple[Optional[str], str]:
    """Swap the timeline inside `question` and restate the times around it.

    Returns `(new_question, status)`.  `status` is recorded per case in the
    corpus manifest so a silent failure to locate the timeline is counted
    rather than papered over; `new_question` is None when the rebuild could not
    be performed, and the caller decides what to do about it.
    """
    q = str(question or "")
    inp = str(original_input or "")
    if not q:
        return None, "no_question"

    if inp and inp in q:
        i = q.index(inp)
        head, tail = q[:i], q[i + len(inp):]
    else:
        m = re.search(r"(<patient_timeline>\s*)(.*?)(\s*</patient_timeline>)",
                      q, re.DOTALL)
        if not m:
            return None, "timeline_block_not_found"
        head, tail = q[:m.end(1)], q[m.start(3):]

    def restate(s: str) -> str:
        for old, new in ((old_latest_event_time, new_latest_event_time),
                         (old_prediction_time, new_prediction_time)):
            if old in (None, "") or new in (None, ""):
                continue
            s = s.replace(str(old), str(new))
        return s

    head, tail = restate(head), restate(tail)
    out = head + timeline + tail
    leftover = [t for t in _TS_RE.findall(head + tail)
                if new_prediction_time and t > str(new_prediction_time)]
    return out, ("rebuilt" if not leftover
                 else f"rebuilt_with_{len(leftover)}_late_template_timestamps")
