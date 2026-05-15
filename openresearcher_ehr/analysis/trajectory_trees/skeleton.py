"""Stage 0: turn a raw AgentEHR trajectory into a compact Event list.

An `Event` is one of:

    {type: "reason",      idx, src: "assistant"|"ehr_think", text}
    {type: "action",      idx, call_id, tool, args}
    {type: "observation", idx, call_id, shape, body}   # body is head+tail truncated
    {type: "finish",      idx, answer}

`idx` is the index into the original `messages` list so downstream nodes can
ground back to the raw trajectory. `action` and its matching `observation`
share the same `call_id` (the OpenAI-style `tool_call_id`).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Different model harnesses register the same logical tool under different
# names: Sonnet exposes `ehr_finish`, Opus/Qwen3 use the dotted `ehr.finish`,
# Kimi strips the prefix entirely (`finish`). To stay model-agnostic, we
# match by *suffix* rather than by exact string. Same for `think`.
THINK_TOOL_SUFFIXES = ("think",)
FINISH_TOOL_SUFFIXES = ("finish",)


def _is_special_tool(normalized: str, suffixes: tuple) -> bool:
    """Match `finish`, `ehr_finish`, `<anything>_finish`, etc."""
    return any(normalized == s or normalized.endswith("_" + s) for s in suffixes)

# Observation compression budget. Long tool outputs are kept as head+tail so
# the model still sees row structure but the skeleton doesn't explode.
_OBS_HEAD = 800
_OBS_TAIL = 400
_OBS_TABLE_ROWS = 3

_REASONING_RE = re.compile(r"<reasoning>(.*?)</reasoning>", re.DOTALL | re.IGNORECASE)


def _coerce_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        parts = []
        for b in x:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or json.dumps(b)[:200])
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(x)


def _parse_tool_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"_raw": obj}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": str(raw)[:500]}


def _strip_reasoning_tag(text: str) -> Tuple[str, str]:
    """Split an assistant.content string into (reasoning, visible_text).

    gpt-oss-120b style wraps its chain-of-thought in <reasoning>...</reasoning>.
    """
    m = _REASONING_RE.search(text or "")
    if not m:
        return "", text or ""
    reasoning = m.group(1).strip()
    visible = (text[: m.start()] + text[m.end() :]).strip()
    return reasoning, visible


def _compress_observation(body: str) -> Tuple[str, str]:
    """Return (shape, compressed_body).

    - For table-like bodies (lots of `|` separators or newline-joined columns),
      keep the header + first N rows.
    - Otherwise head+tail truncate long strings.
    """
    body = body or ""
    n = len(body)
    # Heuristic: tabular if contains many pipe-separated runs *and* has newlines
    pipe_count = body.count(" | ")
    newline_count = body.count("\n")
    is_table = pipe_count >= 3 and newline_count >= 2

    if n <= _OBS_HEAD + _OBS_TAIL + 50:
        return f"text,len={n}", body

    if is_table:
        lines = body.splitlines()
        header = lines[0] if lines else ""
        rows = lines[1 : 1 + _OBS_TABLE_ROWS]
        omitted = max(0, len(lines) - (1 + _OBS_TABLE_ROWS))
        compressed = header + "\n" + "\n".join(rows)
        if omitted:
            compressed += f"\n... [{omitted} rows omitted]"
        return f"table,nrows={len(lines) - 1},len={n}", compressed

    head = body[:_OBS_HEAD]
    tail = body[-_OBS_TAIL:]
    return f"text,len={n}", f"{head}\n... [{n - _OBS_HEAD - _OBS_TAIL} chars omitted]\n{tail}"


def extract_root_task(messages: List[Dict[str, Any]]) -> str:
    """The first user message is the root task.

    We strip the XML-ish `<task_instruction>` / `<patient_info>` tags so the
    prompt to Claude is compact; the raw question is kept intact in the output
    tree metadata by the caller.
    """
    for m in messages:
        if m.get("role") == "user":
            return _coerce_str(m.get("content")).strip()
    return ""


def build_skeleton(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn raw OpenAI-style messages into a compact event skeleton.

    Returns a dict with:
      events:       list of events (see module docstring)
      root_task:    first user message (verbatim, tags included)
      system_hint:  short summary of available tool categories (from system msg)
    """
    events: List[Dict[str, Any]] = []
    root_task = ""
    system_hint = ""

    # Buffer pending tool results so action+observation share a call_id,
    # even though trajectory emits them in separate messages.
    # (We don't actually need a buffer; we can just scan and match by id.)
    id_to_action_eid: Dict[str, int] = {}

    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            system_hint = _coerce_str(m.get("content"))[:600]
            continue
        if role == "user":
            if not root_task:
                root_task = _coerce_str(m.get("content"))
            continue

        if role == "assistant":
            raw_text = _coerce_str(m.get("content"))
            _, visible = _strip_reasoning_tag(raw_text)
            reasoning_inline, _ = _strip_reasoning_tag(raw_text)
            # Assistant prose / reasoning becomes a reason event (merge the two
            # flavors — <reasoning> tag or free content — into one event).
            prose = (reasoning_inline + ("\n" + visible if visible else "")).strip()
            if prose:
                events.append({
                    "type": "reason",
                    "idx": idx,
                    "src": "assistant",
                    "text": prose,
                })

            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                # Normalize dotted names to underscore for cross-model parity
                normalized = name.replace(".", "_")
                call_id = tc.get("id") or f"call_{idx}_{len(events)}"
                args = _parse_tool_args(fn.get("arguments"))

                if _is_special_tool(normalized, THINK_TOOL_SUFFIXES):
                    # `think` (under any name) carries the model's internal
                    # plan as its `response` argument; surface it as a reason
                    # event rather than an external tool.
                    txt = _coerce_str(args.get("response") or args.get("thought") or args)
                    if txt.strip():
                        events.append({
                            "type": "reason",
                            "idx": idx,
                            "src": "ehr_think",
                            "text": txt,
                            "call_id": call_id,  # so the matching obs is skipped
                        })
                    id_to_action_eid[call_id] = -1
                    continue

                if _is_special_tool(normalized, FINISH_TOOL_SUFFIXES):
                    answer = args.get("response") if isinstance(args, dict) else args
                    events.append({
                        "type": "finish",
                        "idx": idx,
                        "call_id": call_id,
                        "answer": answer,
                    })
                    id_to_action_eid[call_id] = len(events) - 1
                    continue

                events.append({
                    "type": "action",
                    "idx": idx,
                    "call_id": call_id,
                    "tool": normalized,
                    "args": args,
                })
                id_to_action_eid[call_id] = len(events) - 1
            continue

        if role == "tool":
            call_id = m.get("tool_call_id") or ""
            # Skip observations that belong to ehr_think (we already absorbed
            # the think text) or to a finish call (the terminal "Finish" ack).
            action_eid = id_to_action_eid.get(call_id)
            if action_eid is not None and action_eid < 0:
                continue
            if action_eid is not None and events[action_eid]["type"] == "finish":
                continue

            raw = _coerce_str(m.get("content"))
            shape, body = _compress_observation(raw)
            events.append({
                "type": "observation",
                "idx": idx,
                "call_id": call_id,
                "shape": shape,
                "body": body,
            })
            continue

    return {
        "root_task": root_task,
        "system_hint": system_hint,
        "events": events,
    }


def event_digest(ev: Dict[str, Any], max_chars: int = 500) -> str:
    """One-line digest of an event for prompts."""
    t = ev["type"]
    if t == "reason":
        src = ev.get("src", "")
        text = (ev.get("text") or "").replace("\n", " ")
        return f"[reason:{src}] {text[:max_chars]}"
    if t == "action":
        return f"[action] {ev['tool']}({json.dumps(ev.get('args') or {}, default=str)[:max_chars]})"
    if t == "observation":
        body = (ev.get("body") or "").replace("\n", " ")
        return f"[obs:{ev.get('shape','')}] {body[:max_chars]}"
    if t == "finish":
        ans = ev.get("answer")
        ans_s = json.dumps(ans, default=str) if not isinstance(ans, str) else ans
        return f"[finish] {ans_s[:max_chars]}"
    return f"[{t}]"


def render_skeleton_for_prompt(
    skeleton: Dict[str, Any],
    event_ids: Optional[List[int]] = None,
    max_chars_per_event: int = 600,
) -> str:
    """Render events to a compact string block for an LLM prompt.

    If `event_ids` is given, only those event indices are rendered; otherwise
    all events in the skeleton are rendered.
    """
    events = skeleton["events"]
    if event_ids is None:
        event_ids = list(range(len(events)))
    lines = []
    for eid in event_ids:
        ev = events[eid]
        lines.append(f"E{eid:03d} {event_digest(ev, max_chars_per_event)}")
    return "\n".join(lines)
