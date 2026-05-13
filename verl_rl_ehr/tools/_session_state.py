"""Shared per-trajectory state + budget tracking for browser tools.

Trimmed from OpenResearcher researcher_v2 — only the per-trajectory tool-call
counter and a minimal search-result registry so browser.open can resolve
integer IDs from a prior browser.search.
"""
from collections import defaultdict
from typing import Any


SOFT_WARNING_AFTER = 200  # tool calls at which we start appending a warning
MAX_TOOL_CALLS = 300       # tool calls at which we refuse further tool calls

BUDGET_WARNING_MSG = (
    "\n\n[SYSTEM] RESEARCH BUDGET WARNING: You have used most of your allowed tool calls. "
    "Start wrapping up NOW and submit your final answer via ehr.finish."
)
BUDGET_EXHAUSTED_MSG = (
    "[SYSTEM] BUDGET EXHAUSTED — NO MORE TOOL CALLS ALLOWED.\n"
    "You MUST call ehr.finish with your best answer NOW."
)


_sessions: dict[str, dict[str, Any]] = defaultdict(
    lambda: {"search_results": {}, "all_results": {}, "total_tool_calls": 0}
)


def get_session(instance_id: str) -> dict[str, Any]:
    return _sessions[instance_id]


def increment_tool_calls(instance_id: str) -> int:
    s = _sessions[instance_id]
    s["total_tool_calls"] += 1
    return s["total_tool_calls"]


def is_budget_exhausted(instance_id: str) -> bool:
    return _sessions[instance_id]["total_tool_calls"] >= MAX_TOOL_CALLS


def should_warn(instance_id: str) -> bool:
    n = _sessions[instance_id]["total_tool_calls"]
    return SOFT_WARNING_AFTER <= n < MAX_TOOL_CALLS


def clear_session(instance_id: str) -> None:
    _sessions.pop(instance_id, None)
