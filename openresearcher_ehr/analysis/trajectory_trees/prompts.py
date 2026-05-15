"""Claude prompts + tolerant JSON parsing for the three LLM stages.

Stage 1 — SEGMENT:   segment the whole trajectory into top-level subproblems.
Stage 2 — DECOMPOSE: recursively break a subproblem into child subproblems.
Stage 3 — LEAF_SUM:  batch-summarize what each tool call returned.

All prompts demand strict JSON and are parsed through `extract_json_block`,
which tolerates ```json fences and leading chit-chat that sometimes slips
through even at temperature 0.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# ── JSON parsing ──────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_block(text: str) -> Any:
    """Extract the first JSON object/array from an LLM response.

    Order of attempts:
      1. ```json ... ``` fenced block
      2. whole text as JSON
      3. slice from first `{`/`[` to the matching last `}`/`]`

    Raises ValueError if nothing parseable is found.
    """
    if not text:
        raise ValueError("empty response")

    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Fallback: take the broadest brace/bracket slice.
    for opener, closer in (("{", "}"), ("[", "]")):
        i = stripped.find(opener)
        j = stripped.rfind(closer)
        if i != -1 and j != -1 and j > i:
            candidate = stripped[i : j + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    raise ValueError(f"no JSON block parseable in: {text[:400]!r}")


# ── Stage 1: segment ──────────────────────────────────────────────────────

SEGMENT_SYSTEM = """You are an expert at reading clinical AI-agent trajectories.

You will be shown:
  • a ROOT TASK the agent was solving (from AgentEHR-Bench)
  • a linear SKELETON of events the agent produced, with IDs like E000, E001, ...
    Events are tagged [reason:*], [action], [obs:*], or [finish].

Your job: partition the event sequence into TOP-LEVEL SUBPROBLEMS that the
agent had to solve in order to answer the root task. Each subproblem should
be a *semantically coherent unit of work* — e.g. "gather patient demographics
and admission history", "map aortic stenosis to CCS categories", "verify
uncertain CCS candidates via web search".

Rules:
  • Every event ID in the skeleton must belong to exactly one segment.
  • Segments must be contiguous in event-ID order and non-overlapping.
  • Use 2–8 top-level segments.
  • Keep each [action] together with the [obs] events for its tool call;
    do NOT split a parallel-call group across segments.
  • A segment is `atomic` if it would not meaningfully decompose further
    (e.g., a single lookup followed by one observation). Otherwise set
    `atomic=false` and the caller will recurse.

Output JSON strictly matching:

{
  "segments": [
    {
      "id": "s1",
      "title": "<short imperative noun phrase>",
      "rationale": "<one-sentence why this is one coherent subproblem>",
      "event_ids": [0, 1, 2, ...],   // integers, contiguous
      "atomic": false
    },
    ...
  ]
}

Output ONLY the JSON object, no preamble."""


def build_segment_user_prompt(root_task: str, skeleton_text: str) -> str:
    return f"""ROOT TASK:
{root_task}

SKELETON ({skeleton_text.count(chr(10)) + 1} events):
{skeleton_text}

Now partition the events into top-level subproblems. Return JSON only."""


# ── Stage 2: decompose ────────────────────────────────────────────────────

DECOMPOSE_SYSTEM = """You are decomposing one subproblem of a clinical AI-agent
trajectory into smaller child subproblems.

You will be shown:
  • the ROOT TASK
  • the PARENT PATH — titles of ancestor subproblems from root down to the
    subproblem you are decomposing
  • the events that belong to the current subproblem, with IDs like E042,
    E043, ...

Your job: partition THIS subproblem's events into 2–6 child subproblems.
Each child must cover a contiguous range of event IDs and be more specific
than the parent. Keep each [action] together with the [obs] events that
belong to its tool call — parallel calls must not be split across children.
If the parent is already atomic work (e.g. one tool call plus one
observation, or a single lookup), return a single child with `atomic=true`
covering all events.

Output strict JSON:

{
  "children": [
    {
      "id": "<id>",
      "title": "<short imperative noun phrase>",
      "rationale": "<one sentence>",
      "event_ids": [N, N+1, ...],
      "atomic": false
    },
    ...
  ]
}

Output ONLY the JSON object."""


def build_decompose_user_prompt(
    root_task: str,
    parent_path: List[str],
    segment_title: str,
    segment_rationale: str,
    segment_skeleton_text: str,
) -> str:
    path_str = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(parent_path))
    return f"""ROOT TASK:
{root_task}

PARENT PATH:
{path_str}

CURRENT SUBPROBLEM:
  title: {segment_title}
  rationale: {segment_rationale}

EVENTS IN THIS SUBPROBLEM:
{segment_skeleton_text}

Decompose this subproblem. Return JSON only."""


# ── Stage 3: leaf-summary (batched) ───────────────────────────────────────

LEAF_SYSTEM = """You are summarizing tool calls made by a clinical AI agent.

For each tool call you will see:
  • tool name and arguments
  • the observation it returned (head+tail if long)

Write ONE short sentence per call that says WHAT THE CALL LEARNED — i.e.
the information content, not the mechanics. Examples of good summaries:

  • "Retrieved 763 lab events over the admission; sodium ranged 128–137 mEq/L."
  • "Confirmed that `drgcodes` has no timestamp column (expected: lookup failed)."
  • "Semantic search mapped 'aortic stenosis' to CCS 'Heart valve disorders' at similarity 0.52."

Avoid quoting raw table rows. Avoid restating the tool name.

Output strict JSON:

{
  "summaries": [
    {"cid": "<call_id>", "summary": "<one sentence>"},
    ...
  ]
}

Output ONLY the JSON."""


def build_leaf_user_prompt(items: List[Dict[str, Any]]) -> str:
    """Render items with short sequential tags (L01, L02, ...) that the model
    echoes back as `cid`. Long toolu_* ids were getting swapped by the model
    because adjacent IDs look almost identical; the caller remaps tags back
    to real call_ids.
    """
    lines = []
    for it in items:
        lines.append(
            f"— cid={it['cid']}\n"
            f"  tool: {it['tool']}\n"
            f"  args: {json.dumps(it.get('args') or {}, default=str)[:500]}\n"
            f"  observation ({it.get('obs_shape','missing')}):\n"
            f"  {(it.get('obs_body') or '<no observation>')[:900]}"
        )
    return (
        "Summarize each call. Echo back the exact `cid` tag you were given "
        "(e.g. 'L01'); do NOT invent or reorder cids.\n\n"
        + "\n\n".join(lines)
        + "\n\nReturn JSON only."
    )


# ── Validators ────────────────────────────────────────────────────────────


def validate_segment_response(resp: Any, total_events: int) -> List[Dict[str, Any]]:
    """Coerce & validate segmentation output.

    Raises ValueError on unfixable structural problems so the caller can
    retry or fall back to a single-segment skeleton.
    """
    if not isinstance(resp, dict):
        raise ValueError(f"segmentation: top-level not dict: {type(resp).__name__}")
    segs = resp.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError("segmentation: missing/empty 'segments' list")

    # Normalize each segment, collect coverage
    covered: List[int] = []
    cleaned: List[Dict[str, Any]] = []
    for i, s in enumerate(segs):
        if not isinstance(s, dict):
            raise ValueError(f"segmentation: segment[{i}] not dict")
        eids = s.get("event_ids") or []
        if not isinstance(eids, list):
            raise ValueError(f"segmentation: segment[{i}].event_ids not list")
        eids = sorted(int(x) for x in eids)
        if not eids:
            raise ValueError(f"segmentation: segment[{i}] empty event_ids")
        title = str(s.get("title") or f"Subproblem {i+1}").strip() or f"Subproblem {i+1}"
        rationale = str(s.get("rationale") or "").strip()
        atomic = bool(s.get("atomic"))
        cleaned.append({
            "id": str(s.get("id") or f"s{i+1}"),
            "title": title[:200],
            "rationale": rationale[:500],
            "event_ids": eids,
            "atomic": atomic,
        })
        covered.extend(eids)

    covered_sorted = sorted(set(covered))
    expected = list(range(total_events))
    if covered_sorted != expected:
        missing = sorted(set(expected) - set(covered_sorted))
        extra = sorted(set(covered_sorted) - set(expected))
        raise ValueError(
            f"segmentation coverage mismatch: missing={missing[:10]} "
            f"extra={extra[:10]} expected_total={total_events} got_total={len(covered_sorted)}"
        )
    if len(covered_sorted) != len(covered):
        raise ValueError("segmentation: event_ids overlap between segments")

    # Ensure order-consistency: segments should be in ascending event-id order
    cleaned.sort(key=lambda s: s["event_ids"][0])
    return cleaned


def validate_decompose_response(
    resp: Any, allowed_events: List[int]
) -> List[Dict[str, Any]]:
    """Validate decompose response against the allowed event-id set."""
    if not isinstance(resp, dict):
        raise ValueError(f"decompose: top-level not dict: {type(resp).__name__}")
    kids = resp.get("children")
    if not isinstance(kids, list) or not kids:
        raise ValueError("decompose: missing/empty 'children' list")

    allowed = set(allowed_events)
    covered: List[int] = []
    cleaned = []
    for i, c in enumerate(kids):
        if not isinstance(c, dict):
            raise ValueError(f"decompose: child[{i}] not dict")
        eids = sorted(int(x) for x in (c.get("event_ids") or []))
        if not eids:
            raise ValueError(f"decompose: child[{i}] empty event_ids")
        bad = [e for e in eids if e not in allowed]
        if bad:
            raise ValueError(f"decompose: child[{i}] has out-of-scope events {bad[:5]}")
        cleaned.append({
            "id": str(c.get("id") or f"c{i+1}"),
            "title": str(c.get("title") or f"Substep {i+1}")[:200],
            "rationale": str(c.get("rationale") or "")[:500],
            "event_ids": eids,
            "atomic": bool(c.get("atomic")),
        })
        covered.extend(eids)

    if sorted(set(covered)) != sorted(allowed):
        raise ValueError("decompose: children don't cover exactly the parent's events")
    if len(covered) != len(set(covered)):
        raise ValueError("decompose: children overlap")
    cleaned.sort(key=lambda c: c["event_ids"][0])
    return cleaned


def validate_leaf_response(resp: Any, expected_cids: List[str]) -> Dict[str, str]:
    """Return a {cid: summary} mapping; missing cids get empty string."""
    if not isinstance(resp, dict):
        raise ValueError(f"leaf: top-level not dict: {type(resp).__name__}")
    items = resp.get("summaries")
    if not isinstance(items, list):
        raise ValueError("leaf: missing 'summaries' list")
    out: Dict[str, str] = {c: "" for c in expected_cids}
    for it in items:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("cid") or "")
        summ = str(it.get("summary") or "").strip()
        if cid in out and summ:
            out[cid] = summ[:400]
    return out
