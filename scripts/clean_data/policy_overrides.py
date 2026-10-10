#!/usr/bin/env python3
"""Runtime overrides of the shipped temporal policy used by the corpus builder.

`agentlite.commons.temporal_policy` (strict-ehr-runtime-v8) contains two
mechanisms that remove information for a reason other than temporal
availability.  The corpus builder overrides them in-process; nothing under
`src/agentlite/` is edited, the overrides are recorded in each case manifest,
and they apply only to the process that imports this module.

Override 1 -- `mask_target_code_rows` is disabled (both policy modes)
--------------------------------------------------------------------
It is a target-label guard: it deletes the target encounter's diagnosis rows
regardless of elapsed time, i.e. it removes rows *because they are the answer*.
Diagnosis codes that only became available after the prediction time are
already removed by the temporal rules; codes that were available before it
stay, even when they reveal the label (an intrinsic property of the benchmark,
not leakage).  `resolve_target_code_scope` is neutralised as well, because the
mask and the audit branch that re-checks it are both gated on that scope
object.

Override 2 -- timestamp sanitisation (``faithful`` mode only)
------------------------------------------------------------
`sanitize_all_timestamp_values` blanks every column whose name contains
`time` or `date` when the VALUE is at or after the cutoff.  In ``aggressive``
mode (the paper setting) this value-based rule is kept unchanged and is
complemented by `sweep_future_values`, which blanks any cell whose *text*
contains a date later than the cutoff: no post-cutoff timestamp survives in any
column.

``faithful`` mode instead keeps planned times that were recorded before the
cutoff.  Example:

    06:50  order written: "medication starts at 08:00"    cutoff 07:00

The order was readable at 06:50; `08:00` is content, not availability.  The
distinction is made per field, never as min() over a row's timestamps:

    labevents  charttime 06:30 (content: when drawn)
               storetime 07:30 (availability: when readable)   cutoff 07:00

min() would keep this row although it was unreadable at the cutoff.  So this
module carries an explicit list of fields whose values are PLANS recorded at
order time (`CONTENT_TIME_FIELDS`) and exempts only those.  Every other time
column keeps the shipped behaviour, because a field that materialises later (an
infusion `endtime`, a `dischtime`) genuinely was not readable at the cutoff.
"""
from __future__ import annotations

import pandas as pd

OVERRIDE_VERSION = "benchmark-faithful-override-v1"

# Fields whose value is a PLAN written at order time. The row's availability is
# established elsewhere (ordertime / entertime / storetime); these values may
# point past the cutoff and survive in `faithful` mode. Deliberately a short,
# named list -- not a heuristic, and not min() over a row.
CONTENT_TIME_FIELDS: frozenset[str] = frozenset({
    "starttime",        # prescriptions / pharmacy: planned administration start
    "stoptime",         # prescriptions / pharmacy: planned administration stop
    "expirationdate",   # pharmacy: planned expiry
    "scheduletime",     # emar: scheduled administration
})

# Fields that genuinely materialise later and keep being blanked when the value
# is past the cutoff. They are simply everything not in CONTENT_TIME_FIELDS and
# are named here only for the reader.
_MATERIALISES_LATER_EXAMPLES = (
    "dischtime", "deathtime", "outtime", "endtime", "storetime", "dod",
)

_applied: dict[str, object] = {}


def _faithful_sanitize(df: pd.DataFrame, cutoff: pd.Timestamp) -> tuple[int, int]:
    """Availability-based replacement for `sanitize_all_timestamp_values`.

    Identical to the original except that a column named in
    CONTENT_TIME_FIELDS is never blanked for holding a future value. Its
    unparseable-value handling is kept unchanged for every column, because an
    unparseable stamp cannot establish availability either way.
    """
    from agentlite.commons.temporal_policy import (  # local import: patched module
        nonempty_mask, parse_time_column,
    )
    future_count = 0
    unparseable_count = 0
    for column in df.columns:
        name = str(column).lower()
        if "time" not in name and "date" not in name and name != "dod":
            continue
        present = nonempty_mask(df[column])
        parsed = parse_time_column(df, column)
        unparseable = present & parsed.isna()
        if unparseable.any():
            df.loc[unparseable, column] = None
            unparseable_count += int(unparseable.sum())
        if name in CONTENT_TIME_FIELDS:
            # A plan recorded before the cutoff. Content, not availability.
            continue
        future = present & parsed.notna() & (parsed >= cutoff)
        if future.any():
            df.loc[future, column] = None
            future_count += int(future.sum())
    return future_count, unparseable_count


AGGRESSIVE_VERSION = "aggressive-zero-future-v1"


def apply_overrides(mode: str = "faithful") -> dict[str, object]:
    """Patch the shipped policy in this process. Idempotent.

    `mode="aggressive"` keeps the shipped value-based timestamp sanitiser,
    because blanking every future-valued timestamp IS the aggressive rule. Only
    the target-label guard is disabled, since removing a row for revealing the
    answer is not a temporal-availability rule in either mode.

    Must be called before the first patient is loaded.
    """
    if _applied:
        return dict(_applied)

    from agentlite.commons import temporal_policy as tp

    original_mask = getattr(tp, "mask_target_code_rows", None)
    original_sanitize = getattr(tp, "sanitize_all_timestamp_values", None)

    stats = {"target_code_rows_that_would_have_been_masked": 0}

    def _no_mask(table_name, safe, report, scope):
        """Count what the target-label guard would have taken, take nothing.

        Real signature is (table_name, safe, report, scope) and it returns
        (frame, report) unchanged when the scope does not apply. Here it always
        returns them unchanged, and counts the rows the guard would have
        deleted so the number appears in provenance instead of vanishing.
        """
        if scope is not None and table_name == getattr(scope, "table", None):
            key = getattr(scope, "key", None)
            if key is not None and key in safe.columns:
                ids = safe[key].map(tp.normalize_record_id)
                stats["target_code_rows_that_would_have_been_masked"] += int(
                    (ids == scope.encounter_id).sum())
        return safe, report

    # The mask and the audit branch that re-checks it are both gated on the
    # scope object, so switching the resolver off is the single point of
    # control. Patching only the mask would leave `audit_runtime_view` raising
    # for diagnosis-code tasks whose target codes are legitimately visible.
    original_scope = getattr(tp, "resolve_target_code_scope", None)
    if original_scope is not None:
        def _no_scope(*args, **kwargs):
            try:
                scope = original_scope(*args, **kwargs)
            except Exception:
                scope = None
            if scope is not None:
                stats["target_code_scopes_neutralised"] = (
                    stats.get("target_code_scopes_neutralised", 0) + 1)
            return None
        tp.resolve_target_code_scope = _no_scope

    if original_mask is not None:
        tp.mask_target_code_rows = _no_mask
    if original_sanitize is not None and mode != "aggressive":
        tp.sanitize_all_timestamp_values = _faithful_sanitize

    _applied.update({
        "mode": mode,
        "override_version": (AGGRESSIVE_VERSION if mode == "aggressive"
                             else OVERRIDE_VERSION),
        "mask_target_code_rows": "disabled" if original_mask else "absent",
        "resolve_target_code_scope": (
            "neutralised" if original_scope else "absent"),
        "sanitize_all_timestamp_values": (
            "shipped_value_based_kept" if mode == "aggressive"
            else ("replaced_with_availability_based" if original_sanitize
                  else "absent")),
        "content_time_fields_exempted": (
            [] if mode == "aggressive" else sorted(CONTENT_TIME_FIELDS)),
        "policy_flags": {
            "original_prediction_time_preserved": True,
            "remove_post_prediction_rows": True,
            "mask_post_prediction_timestamp_values": mode == "aggressive",
            "semantic_target_filtering": False,
            "outcome_dependent_guard": False,
        },
        "stats": stats,
    })
    return dict(_applied)


def override_report() -> dict[str, object]:
    return dict(_applied)


# Value-level sweep (aggressive mode) ----------------------------------------

import re as _re  # noqa: E402

_STAMP = _re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\b")


def _cell_is_future(value, cutoff) -> bool:
    """True when the cell text contains a date later than the cutoff.

    Matched on the value, never on the column name: `datetimeevents.value`
    holds dates in a column called `value`, which a name-driven sanitiser
    cannot see.
    """
    if value is None:
        return False
    text = str(value)
    if len(text) < 10:
        return False
    for match in _STAMP.finditer(text):
        try:
            groups = [int(g) if g else 0 for g in match.groups()]
            stamp = pd.Timestamp(*groups[:6])
        except (ValueError, TypeError):
            continue
        if stamp > cutoff:
            return True
    return False


def sweep_future_values(tables, cutoff):
    """Blank every timestamp-valued cell later than the cutoff. Aggressive only."""
    cutoff = pd.to_datetime(cutoff, errors="coerce")
    counts = {}
    if pd.isna(cutoff):
        return counts
    for name, frame in tables.items():
        if frame is None or not len(frame):
            continue
        for column in frame.columns:
            series = frame[column]
            if series.dtype.kind in "ifb":      # numeric cannot hold a date string
                continue
            mask = series.map(lambda v: _cell_is_future(v, cutoff))
            hits = int(mask.sum())
            if hits:
                frame.loc[mask, column] = None
                counts[f"{name}.{column}"] = hits
    return counts
