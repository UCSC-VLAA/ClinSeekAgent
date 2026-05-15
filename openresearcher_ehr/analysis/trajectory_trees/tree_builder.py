"""Recursive tree builder: skeleton + Bedrock Opus → subproblem tree."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .bedrock_client import BedrockOpusJSON
from .prompts import (
    DECOMPOSE_SYSTEM,
    LEAF_SYSTEM,
    SEGMENT_SYSTEM,
    build_decompose_user_prompt,
    build_leaf_user_prompt,
    build_segment_user_prompt,
    validate_decompose_response,
    validate_leaf_response,
    validate_segment_response,
)
from .skeleton import (
    build_skeleton,
    render_skeleton_for_prompt,
)

# Termination thresholds for recursion
MAX_DEPTH = 5
MIN_EVENTS_FOR_DECOMPOSE = 3  # below this → treat as atomic
MAX_ACTIONS_FOR_ATOMIC = 2

# Batch size for leaf-summary calls
LEAF_BATCH = 10


@dataclass
class BuildConfig:
    max_depth: int = MAX_DEPTH
    min_events_for_decompose: int = MIN_EVENTS_FOR_DECOMPOSE
    max_actions_for_atomic: int = MAX_ACTIONS_FOR_ATOMIC
    leaf_batch: int = LEAF_BATCH
    summarize_leaves: bool = True
    # Per-stage token budgets
    segment_max_tokens: int = 4096
    decompose_max_tokens: int = 2048
    leaf_max_tokens: int = 2048


# ── Helpers ───────────────────────────────────────────────────────────────


def _count_actions(events: List[Dict[str, Any]], event_ids: List[int]) -> int:
    return sum(1 for e in event_ids if events[e]["type"] == "action")


def _is_trivially_atomic(events: List[Dict[str, Any]], event_ids: List[int], cfg: BuildConfig) -> bool:
    if len(event_ids) < cfg.min_events_for_decompose:
        return True
    if _count_actions(events, event_ids) <= cfg.max_actions_for_atomic:
        return True
    return False


def _msg_indices_for(events: List[Dict[str, Any]], event_ids: List[int]) -> List[int]:
    out = []
    seen = set()
    for eid in event_ids:
        idx = events[eid]["idx"]
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def _make_node_id_gen():
    counter = itertools.count(1)
    return lambda: f"n_{next(counter):04d}"


# ── Recursive decomposition ───────────────────────────────────────────────


def _snap_segments_to_action_groups(
    segments: List[Dict[str, Any]], skeleton: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Snap segment boundaries so no action is split from its observations.

    An assistant turn may fire N parallel tool calls whose observations arrive
    in separate events; the segmentation model shouldn't split inside such a
    group. We find the smallest group containing each action's msg_idx and
    merge overlapping groups across segment boundaries.
    """
    events = skeleton["events"]
    # Map call_id → (action_eid, [observation_eids])
    groups_by_aeid: Dict[int, List[int]] = {}
    cid_to_aeid: Dict[str, int] = {}
    for i, e in enumerate(events):
        if e["type"] == "action":
            groups_by_aeid[i] = [i]
            cid_to_aeid[e.get("call_id", "")] = i
    for i, e in enumerate(events):
        if e["type"] == "observation":
            cid = e.get("call_id", "")
            aeid = cid_to_aeid.get(cid)
            if aeid is not None:
                groups_by_aeid[aeid].append(i)
    # union-find over atomic groups: snap by iterating through segments and
    # pushing any event whose group crosses a boundary into the earlier segment.
    if not segments:
        return segments
    # assignment[event_id] = segment_index; start from current assignment
    assignment: List[int] = [-1] * len(events)
    for si, seg in enumerate(segments):
        for eid in seg["event_ids"]:
            assignment[eid] = si
    # For each group, force every member to match the earliest segment index
    # any member is currently in.
    for aeid, eids in groups_by_aeid.items():
        indices = [assignment[x] for x in eids if assignment[x] >= 0]
        if not indices:
            continue
        target = min(indices)
        for x in eids:
            assignment[x] = target
    # Rebuild segments from the new assignment, keeping title/rationale from
    # the first event's original owner.
    rebuilt: Dict[int, Dict[str, Any]] = {}
    for eid, si in enumerate(assignment):
        if si < 0:
            continue
        rebuilt.setdefault(si, {
            "id": segments[si]["id"],
            "title": segments[si]["title"],
            "rationale": segments[si].get("rationale", ""),
            "event_ids": [],
            "atomic": segments[si].get("atomic", False),
        })
        rebuilt[si]["event_ids"].append(eid)
    out = [rebuilt[k] for k in sorted(rebuilt.keys())]
    for s in out:
        s["event_ids"].sort()
    return out


def _segment(
    client: BedrockOpusJSON,
    root_task: str,
    skeleton: Dict[str, Any],
    cfg: BuildConfig,
) -> List[Dict[str, Any]]:
    events = skeleton["events"]
    skel_text = render_skeleton_for_prompt(skeleton)
    user = build_segment_user_prompt(root_task, skel_text)
    parsed, _ = client.call_json(SEGMENT_SYSTEM, user, max_tokens=cfg.segment_max_tokens)
    try:
        segs = validate_segment_response(parsed, total_events=len(events))
    except ValueError as e:
        print(f"[tree] segment validation failed: {e}; falling back to single segment")
        return [{
            "id": "s1",
            "title": "Entire trajectory",
            "rationale": "segmentation fallback",
            "event_ids": list(range(len(events))),
            "atomic": False,
        }]
    return _snap_segments_to_action_groups(segs, skeleton)


def _decompose(
    client: BedrockOpusJSON,
    root_task: str,
    parent_path: List[str],
    segment: Dict[str, Any],
    skeleton: Dict[str, Any],
    cfg: BuildConfig,
) -> List[Dict[str, Any]]:
    events = skeleton["events"]
    skel_text = render_skeleton_for_prompt(skeleton, event_ids=segment["event_ids"])
    user = build_decompose_user_prompt(
        root_task=root_task,
        parent_path=parent_path,
        segment_title=segment["title"],
        segment_rationale=segment.get("rationale", ""),
        segment_skeleton_text=skel_text,
    )
    parsed, _ = client.call_json(DECOMPOSE_SYSTEM, user, max_tokens=cfg.decompose_max_tokens)
    try:
        kids = validate_decompose_response(parsed, allowed_events=segment["event_ids"])
    except ValueError as e:
        print(f"[tree] decompose validation failed: {e}; marking subproblem atomic")
        return [{
            "id": "c1",
            "title": segment["title"],
            "rationale": "decompose fallback",
            "event_ids": segment["event_ids"],
            "atomic": True,
        }]
    # Same snap as segmentation: keep each action+observations group together.
    return _snap_segments_to_action_groups(kids, skeleton)


def _build_subtree(
    client: BedrockOpusJSON,
    root_task: str,
    parent_path: List[str],
    segment: Dict[str, Any],
    skeleton: Dict[str, Any],
    cfg: BuildConfig,
    depth: int,
    new_id,
) -> Dict[str, Any]:
    events = skeleton["events"]
    event_ids = segment["event_ids"]
    node: Dict[str, Any] = {
        "id": new_id(),
        "level": depth,
        "type": "subproblem",
        "title": segment["title"],
        "rationale": segment.get("rationale", ""),
        "event_indices": event_ids,
        "msg_indices": _msg_indices_for(events, event_ids),
        "children": [],
    }

    # Termination: atomic ⇒ children are the tool calls themselves
    terminate = (
        segment.get("atomic", False)
        or depth >= cfg.max_depth
        or _is_trivially_atomic(events, event_ids, cfg)
    )
    if terminate:
        node["children"] = _build_leaf_children(events, event_ids, depth + 1, new_id)
        return node

    # Recurse: ask the model to split further
    children_segs = _decompose(client, root_task, parent_path + [segment["title"]],
                               segment, skeleton, cfg)
    # Guard: if the model returned one child covering everything with atomic=false,
    # we'd loop forever. Force atomic in that case.
    if (
        len(children_segs) == 1
        and children_segs[0]["event_ids"] == event_ids
        and not children_segs[0].get("atomic", False)
    ):
        children_segs[0]["atomic"] = True

    for cseg in children_segs:
        node["children"].append(
            _build_subtree(
                client,
                root_task,
                parent_path + [segment["title"]],
                cseg,
                skeleton,
                cfg,
                depth + 1,
                new_id,
            )
        )
    return node


def _build_leaf_children(
    events: List[Dict[str, Any]],
    event_ids: List[int],
    depth: int,
    new_id,
) -> List[Dict[str, Any]]:
    """Turn each `action` event in the range into a leaf node.

    Observations are kept alongside their action on the same leaf. Reason
    events become rationale strings on a dedicated reason-type leaf (rare but
    keeps the information).
    """
    # Pair observations to actions by call_id. Multi-action assistant turns
    # fire parallel tool calls, and the observations come back in arbitrary
    # order — stack-matching (leaves[-1]) gets it wrong.
    leaves: List[Dict[str, Any]] = []
    cid_to_leaf: Dict[str, Dict[str, Any]] = {}
    pending_reasons: List[str] = []

    def _flush_reasons_as_node():
        nonlocal pending_reasons
        if pending_reasons:
            leaves.append({
                "id": new_id(),
                "level": depth,
                "type": "reason",
                "title": "reasoning note",
                "rationale": "\n".join(pending_reasons)[:2000],
                "event_indices": [],
                "msg_indices": [],
                "children": [],
            })
            pending_reasons = []

    for eid in event_ids:
        ev = events[eid]
        if ev["type"] == "action":
            _flush_reasons_as_node()
            cid = ev.get("call_id", "")
            leaf = {
                "id": new_id(),
                "level": depth,
                "type": "tool_call",
                "title": f"{ev['tool']}",
                "tool": ev["tool"],
                "args": ev.get("args", {}),
                "call_id": cid,
                "observation_summary": None,
                "observation_shape": None,
                "observation_body": None,
                "event_indices": [eid],
                "msg_indices": [ev["idx"]],
                "children": [],
            }
            leaves.append(leaf)
            if cid:
                cid_to_leaf[cid] = leaf
        elif ev["type"] == "observation":
            cid = ev.get("call_id", "")
            leaf = cid_to_leaf.get(cid)
            if leaf is not None and not leaf["observation_shape"]:
                leaf["observation_shape"] = ev.get("shape")
                leaf["observation_body"] = ev.get("body")
                leaf["event_indices"].append(eid)
                leaf["msg_indices"].append(ev["idx"])
            # else: orphan observation (action was outside this range, or
            # never issued by the model) — skipped silently.
        elif ev["type"] == "reason":
            pending_reasons.append(ev.get("text", ""))
        elif ev["type"] == "finish":
            _flush_reasons_as_node()
            ans = ev.get("answer")
            leaves.append({
                "id": new_id(),
                "level": depth,
                "type": "finish",
                "title": "finish",
                "answer": ans,
                "event_indices": [eid],
                "msg_indices": [ev["idx"]],
                "children": [],
            })
    _flush_reasons_as_node()
    return leaves


# ── Leaf summarization (batched) ──────────────────────────────────────────


def _all_tool_call_leaves(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.get("type") == "tool_call":
            out.append(n)
        stack.extend(n.get("children", []))
    return out


def _summarize_leaves(
    client: BedrockOpusJSON,
    tree_root: Dict[str, Any],
    cfg: BuildConfig,
) -> None:
    leaves = _all_tool_call_leaves(tree_root)
    if not leaves:
        return
    # Batch; use short synthetic cids (L01, L02, ...) for the prompt because
    # long toolu_* call_ids look near-identical to the model and get swapped.
    for i in range(0, len(leaves), cfg.leaf_batch):
        chunk = leaves[i : i + cfg.leaf_batch]
        tag_to_leaf: Dict[str, Dict[str, Any]] = {}
        items = []
        for j, leaf in enumerate(chunk):
            tag = f"L{j+1:02d}"
            tag_to_leaf[tag] = leaf
            items.append({
                "cid": tag,
                "tool": leaf["tool"],
                "args": leaf.get("args"),
                "obs_shape": leaf.get("observation_shape"),
                "obs_body": leaf.get("observation_body"),
            })
        user = build_leaf_user_prompt(items)
        try:
            parsed, _ = client.call_json(LEAF_SYSTEM, user, max_tokens=cfg.leaf_max_tokens)
            summaries = validate_leaf_response(parsed, expected_cids=[it["cid"] for it in items])
        except Exception as e:  # noqa: BLE001
            print(f"[tree] leaf summary batch failed: {e}; leaving blank")
            summaries = {}
        for tag, leaf in tag_to_leaf.items():
            leaf["observation_summary"] = summaries.get(tag, "") or None


# ── Entry point ───────────────────────────────────────────────────────────


def build_tree(
    messages: List[Dict[str, Any]],
    client: BedrockOpusJSON,
    cfg: Optional[BuildConfig] = None,
) -> Dict[str, Any]:
    """Build a subproblem tree from raw trajectory messages."""
    cfg = cfg or BuildConfig()
    skeleton = build_skeleton(messages)
    events = skeleton["events"]
    root_task = skeleton["root_task"]

    if not events:
        return {
            "root": {
                "id": "n_0001",
                "level": 0,
                "type": "root",
                "title": "empty trajectory",
                "event_indices": [],
                "msg_indices": [],
                "children": [],
            },
            "skeleton_stats": {"n_events": 0},
        }

    new_id = _make_node_id_gen()

    # Stage 1: top-level segments
    segments = _segment(client, root_task, skeleton, cfg)

    # Build tree
    root_node = {
        "id": new_id(),
        "level": 0,
        "type": "root",
        "title": (root_task[:200] + ("…" if len(root_task) > 200 else "")) or "root task",
        "root_task": root_task,
        "event_indices": list(range(len(events))),
        "msg_indices": _msg_indices_for(events, list(range(len(events)))),
        "children": [],
    }

    for seg in segments:
        child = _build_subtree(
            client,
            root_task,
            parent_path=[root_node["title"]],
            segment=seg,
            skeleton=skeleton,
            cfg=cfg,
            depth=1,
            new_id=new_id,
        )
        root_node["children"].append(child)

    # Stage 3: batched leaf summaries
    if cfg.summarize_leaves:
        _summarize_leaves(client, root_node, cfg)

    # Drop bulky observation_body fields from leaves after we've summarized
    # them — keep only the shape and the LLM summary to keep the output small.
    for leaf in _all_tool_call_leaves(root_node):
        leaf["observation_body"] = None

    # Derive simple stats
    depths, node_count = [], [0]
    def _walk(n, d):
        node_count[0] += 1
        depths.append(d)
        for c in n.get("children", []):
            _walk(c, d + 1)
    _walk(root_node, 0)

    return {
        "root": root_node,
        "skeleton_stats": {
            "n_events": len(events),
            "n_reason": sum(1 for e in events if e["type"] == "reason"),
            "n_action": sum(1 for e in events if e["type"] == "action"),
            "n_observation": sum(1 for e in events if e["type"] == "observation"),
            "n_finish": sum(1 for e in events if e["type"] == "finish"),
        },
        "tree_stats": {
            "n_nodes": node_count[0],
            "max_depth": max(depths) if depths else 0,
            "avg_depth": sum(depths) / len(depths) if depths else 0.0,
        },
    }
