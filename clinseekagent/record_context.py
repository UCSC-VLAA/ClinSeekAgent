"""Render a benchmark row's patient record at a chosen width, at run time.

A corpus built by `scripts/clean_data/build_evidence_corpus.py` publishes, per
question, two row sets of the *same* policy-filtered pool:

    evidence/<qid>/recent/<table>.csv   the source benchmark's curated selection
    evidence/<qid>/full/<table>.csv     everything visible before the cutoff

and one rendered prompt per row (`question` for text, `input_text` for MM).

How the prompts are built
-------------------------
By replacing exactly one block of the row's prompt:

    question   = <task_instruction> <patient_info> <patient_timeline> <candidates>
                                                    ^^^^^^^^^^^^^^^^^ swapped
    input_text = ... <ehr_context> ... </ehr_context>              ^^ swapped

using the corpus renderers (`question_render`, `mm_context_render`), so
everything outside the record -- instruction, the stated `Current Time`, the
candidate list, the linked-study ids -- stays byte-identical between the
Curated Input (`recent`) and Full-Record Input (`full`) arms. The two prompts
then differ in exactly one thing: which rows of the same pool they show.

Text `recent` returns the corpus `question` as is: the builder already rendered
it from `recent/` (re-rendering reproduces it byte-for-byte). MM re-renders
both views from the CSVs.

Truncation
----------
`full/` frequently does not fit (median ~52k tokens, long tail in the
millions). Rows are dropped oldest-first down to a clean timestamp boundary,
leaving a contiguous window `[window_start, cutoff]` -- the curated window
widened until the budget runs out. The cut is found by binary search over
distinct timestamps against the *actually rendered* prompt, not an estimate of
it, because the section grammar costs far more per row than the CSV it comes
from.

Every row records what happened to it under `record_context`
(`history_truncated`, `window_start`, `rows_kept`, `rows_dropped`, ...), so
cases that fit and cases that were cut can be reported as separate strata.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

import mm_context_render  # noqa: E402  (clinseekagent/ is on sys.path)
import question_render  # noqa: E402

from context_limits import approx_tokens  # noqa: E402

VIEWS = ("recent", "full")

RENDER_VERSION = "record-context-v1"

#: A nested copy of the row's query fields (including a second copy of the
#: prompt) that the corpus keeps for provenance. Dropped on load so it is not
#: copied into every results.jsonl row.
_DROP_FIELDS = ("query",)


# --------------------------------------------------------------------------
# loading


def load_corpus_rows(corpus_root: str | Path, limit: int = 0) -> list[dict]:
    """Rows of a corpus root (`all_data.jsonl`), without the nested `query`.

    The per-view `all_data_*_view.jsonl` files are not needed: the evidence
    directory of each view is derived from the row itself.
    """
    root = Path(corpus_root)
    path = root / "all_data.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; is {root} a corpus root?")
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for key in _DROP_FIELDS:
                row.pop(key, None)
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def evidence_dir(row: dict, corpus_root: str | Path, view: str) -> Path:
    """Directory of `view`'s CSVs for `row`, from the row's own pointer."""
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    rel = row.get(f"{view}_evidence_list")
    if not rel:
        qid = row.get("qid") or row.get("id")
        rel = f"evidence/{qid}/{view}"
    return Path(corpus_root) / rel


def read_tables(view_dir: Path) -> dict[str, pd.DataFrame]:
    """Every `<table>.csv` under `view_dir`, as strings.

    `dtype=str` and `keep_default_na=False` keep the values exactly as the
    corpus published them: letting pandas infer would turn ids into floats and
    empty cells into the string "nan", both of which would show up in the
    prompt.
    """
    if not view_dir.exists():
        return {}
    return {
        path.stem: pd.read_csv(path, dtype=str, keep_default_na=False)
        for path in sorted(view_dir.glob("*.csv"))
    }


# --------------------------------------------------------------------------
# truncation


def _plan_timeline(tables: dict[str, pd.DataFrame]) -> tuple[dict, list, dict[str, list[int]]]:
    """`(anchors, distinct timestamps newest-first, undated positions)`.

    Timestamps come from `question_render.compute_anchors`, the same function
    the renderer uses to place a row on the timeline, so a row is selected on
    the timeline it will actually be printed on. Computed once and threaded
    through the search: on the 7.9M-token tail cases re-deriving it per probe
    dominates everything else.
    """
    anchors = question_render.compute_anchors(tables)
    stamps: set = set()
    undated: dict[str, list[int]] = {}
    for name, series in anchors.items():
        for pos, ts in enumerate(series):
            if pd.isna(ts):
                undated.setdefault(name, []).append(pos)
            else:
                stamps.add(ts)
    return anchors, sorted(stamps, reverse=True), undated


def _slice_from(
    tables: dict[str, pd.DataFrame],
    anchors: dict,
    undated: dict[str, list[int]],
    keep_at_or_after,
) -> dict[str, pd.DataFrame]:
    """Undated rows, plus dated rows at or after `keep_at_or_after`.

    `None` drops every dated row. Cutting on a timestamp rather than a row
    count keeps the kept set a clean time boundary, and keeps a child-detail
    row together with the parent row it inherits its timestamp from.
    """
    out: dict[str, pd.DataFrame] = {}
    for name, df in tables.items():
        series = anchors.get(name)
        if series is None:
            out[name] = df
            continue
        positions = list(undated.get(name, ()))
        if keep_at_or_after is not None:
            positions += [p for p, ts in enumerate(series)
                          if not pd.isna(ts) and ts >= keep_at_or_after]
        out[name] = df.iloc[sorted(positions)] if positions else df.iloc[0:0]
    return out


def fit_tables_to_budget(
    tables: dict[str, pd.DataFrame],
    budget_tokens: int,
    measure,
) -> tuple[dict[str, pd.DataFrame], dict]:
    """Widest recency window of `tables` whose rendered cost fits the budget.

    `measure(tables) -> int` returns the token cost of the *whole prompt* the
    caller would send for those tables, so the search optimises the real thing
    rather than a proxy for it -- the section grammar costs several times what
    the CSV it is rendered from does, so budgeting against the CSV would
    overshoot badly.

    Cost is monotone in the window, which is what makes a binary search over
    distinct timestamps exact rather than approximate.
    """
    total_rows = sum(len(df) for df in tables.values())
    full_cost = measure(tables)
    if full_cost <= budget_tokens:
        return tables, {
            "history_truncated": False,
            "window_start": None,
            "rows_kept": total_rows,
            "rows_dropped": 0,
            "untruncated_tokens": full_cost,
            "prompt_tokens_est": full_cost,
            "probes": 1,
        }

    anchors, stamps, undated = _plan_timeline(tables)

    # k = how many of the newest distinct timestamps to keep. k=0 is undated
    # rows only, the floor; if even that overflows there is nothing further to
    # drop and prompt_tokens_est reports the overshoot rather than hiding it.
    def window(k: int) -> dict[str, pd.DataFrame]:
        return _slice_from(tables, anchors, undated,
                           stamps[k - 1] if k else None)

    lo, hi, probes = 0, len(stamps), 1
    best = window(0)
    best_cost = measure(best)
    probes += 1
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = window(mid)
        cost = measure(cand)
        probes += 1
        if cost <= budget_tokens:
            best, best_cost = cand, cost
            lo = mid + 1
        else:
            hi = mid - 1

    kept_rows = sum(len(df) for df in best.values())
    kept_stamps = [ts for series in question_render.compute_anchors(best).values()
                   for ts in series if not pd.isna(ts)]
    return best, {
        "history_truncated": True,
        "window_start": min(kept_stamps).isoformat() if kept_stamps else None,
        "rows_kept": kept_rows,
        "rows_dropped": total_rows - kept_rows,
        "untruncated_tokens": full_cost,
        "prompt_tokens_est": best_cost,
        "probes": probes,
    }


# --------------------------------------------------------------------------
# text


def render_question(
    row: dict,
    corpus_root: str | Path,
    view: str,
    prompt_budget_tokens: int | None = None,
) -> tuple[str, dict]:
    """The text prompt for `row` at `view`, truncated to the budget.

    `view="recent"` returns the corpus `question` untouched: it already *is*
    the Recent render (its `<patient_timeline>` block equals a render of
    `recent/`, verified byte-for-byte), so re-rendering it would add drift risk
    for no gain.
    """
    question = row.get("question") or ""
    if not question:
        raise ValueError(
            f"row {row.get('qid')!r} has no `question`; is this a corpus built "
            "by scripts/clean_data/build_evidence_corpus.py?")
    if view == "recent":
        return question, {
            "view": "recent",
            "render_version": RENDER_VERSION,
            "history_truncated": False,
            "window_start": None,
            "rows_dropped": 0,
            "prompt_tokens_est": approx_tokens(question),
            "budget_tokens": prompt_budget_tokens,
        }
    if view != "full":
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    if not question:
        raise ValueError(f"row {row.get('qid')!r} has no `question` to rebuild")

    tables = read_tables(evidence_dir(row, corpus_root, view))

    def build(tabs: dict[str, pd.DataFrame]) -> str:
        text, status = question_render.rebuild_question(
            question, "", question_render.render_timeline(tabs))
        if text is None:
            raise ValueError(
                f"row {row.get('qid')!r}: cannot rebuild question ({status})")
        return text

    if prompt_budget_tokens is None:
        text = build(tables)
        meta = {
            "history_truncated": False, "window_start": None,
            "rows_kept": sum(len(d) for d in tables.values()), "rows_dropped": 0,
            "untruncated_tokens": approx_tokens(text),
            "prompt_tokens_est": approx_tokens(text),
        }
    else:
        kept, meta = fit_tables_to_budget(
            tables, prompt_budget_tokens, lambda t: approx_tokens(build(t)))
        text = build(kept)

    meta.update({"view": "full", "render_version": RENDER_VERSION,
                 "budget_tokens": prompt_budget_tokens})
    return text, meta


# --------------------------------------------------------------------------
# multimodal


def _csv_rows(path: Path) -> tuple[str, list[str]]:
    """`(header line, data lines)` of a view CSV, text untouched."""
    if not path.exists():
        return "", []
    text = path.read_text(encoding="utf-8").rstrip("\n")
    if not text:
        return "", []
    lines = text.split("\n")
    return lines[0], lines[1:]


def _time_index(header: str) -> int | None:
    """Index of the first column that looks temporal, or None."""
    cols = header.split(",")
    for i, col in enumerate(cols):
        low = col.strip().strip('"').lower()
        if "time" in low or "date" in low:
            return i
    return None


def render_input_text(
    row: dict,
    corpus_root: str | Path,
    view: str,
    prompt_budget_tokens: int | None = None,
) -> tuple[str, dict]:
    """The MM prompt for `row` at `view`, truncated to the budget.

    Unlike the text side, the corpus row carries a single `input_text`
    template, so both views are re-rendered from their CSVs here.

    Row selection works on raw CSV lines rather than through pandas so the
    published byte formatting of every cell survives into the prompt.
    """
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    base = row.get("input_text") or ""
    if not base:
        raise ValueError(f"row {row.get('qid')!r} has no `input_text` to rebuild")

    view_dir = evidence_dir(row, corpus_root, view)
    full_dir = evidence_dir(row, corpus_root, "full")
    # "Rows visible before cutoff" is a statement about the temporal gate, so
    # it stays the full view's count whichever view is being printed.
    visible = {p.stem: len(_csv_rows(p)[1]) for p in sorted(full_dir.glob("*.csv"))}

    tables = {name: (h, lines) for name, (h, lines)
              in ((p.stem, _csv_rows(p)) for p in sorted(view_dir.glob("*.csv")))}

    def bodies(sel: dict[str, tuple[str, list[str]]]) -> dict[str, tuple[str, int]]:
        return {n: (("\n".join([h] + ls) if ls else ""), len(ls))
                for n, (h, ls) in sel.items()}

    def build(sel: dict[str, tuple[str, list[str]]]) -> str:
        text, status = mm_context_render.rebuild_input_text(
            base, view_dir, visible, csv_bodies=bodies(sel))
        if text is None:
            raise ValueError(
                f"row {row.get('qid')!r}: cannot rebuild input_text ({status})")
        return text

    meta: dict[str, Any]
    text = build(tables)
    cost = approx_tokens(text)
    total_rows = sum(len(ls) for _, ls in tables.values())
    if prompt_budget_tokens is None or cost <= prompt_budget_tokens:
        meta = {"history_truncated": False, "window_start": None,
                "rows_kept": total_rows, "rows_dropped": 0,
                "untruncated_tokens": cost, "prompt_tokens_est": cost}
    else:
        stamps: set[str] = set()
        for header, lines in tables.values():
            idx = _time_index(header)
            if idx is None:
                continue
            for line in lines:
                parts = line.split(",")
                if idx < len(parts) and parts[idx].strip():
                    stamps.add(parts[idx].strip())
        # ISO-ish timestamps sort correctly as strings.
        order = sorted(stamps, reverse=True)

        def keep_from(cut: str | None) -> dict[str, tuple[str, list[str]]]:
            out = {}
            for name, (header, lines) in tables.items():
                idx = _time_index(header)
                if idx is None or cut is None:
                    out[name] = (header, lines if cut is None else [])
                    continue
                out[name] = (header, [
                    l for l in lines
                    if (lambda p: idx < len(p) and p[idx].strip() >= cut)(l.split(","))
                ])
            return out

        lo, hi = 0, len(order)
        best, best_cost = keep_from(None), approx_tokens(build(keep_from(None)))
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = keep_from(order[mid - 1] if mid else None)
            c = approx_tokens(build(cand))
            if c <= prompt_budget_tokens:
                best, best_cost = cand, c
                lo = mid + 1
            else:
                hi = mid - 1
        kept_rows = sum(len(ls) for _, ls in best.values())
        kept_stamps = [p[_time_index(h)].strip()
                       for h, ls in best.values() if _time_index(h) is not None
                       for p in (l.split(",") for l in ls)
                       if _time_index(h) < len(p) and p[_time_index(h)].strip()]
        text = build(best)
        meta = {"history_truncated": True,
                "window_start": min(kept_stamps) if kept_stamps else None,
                "rows_kept": kept_rows, "rows_dropped": total_rows - kept_rows,
                "untruncated_tokens": cost, "prompt_tokens_est": best_cost}

    meta.update({"view": view, "render_version": RENDER_VERSION,
                 "budget_tokens": prompt_budget_tokens})
    return text, meta
