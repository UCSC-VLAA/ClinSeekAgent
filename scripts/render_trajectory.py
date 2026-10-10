#!/usr/bin/env python3
"""Render a results.jsonl row into a readable trajectory.

The eval driver writes one JSON object per run, with the full `messages` list
inside. That is the trajectory, but it is unreadable as raw JSON. This turns it
into Markdown: one section per round, with the tool calls, the (truncated) tool
results, and the final ehr.finish answer next to the ground-truth label.

    python scripts/render_trajectory.py outputs/<run>/results.jsonl -o traj.md

Note the output contains patient records. MIMIC is credentialed data — keep it
local, do not upload it.
"""
import argparse
import json
from pathlib import Path


def fence(text: str, limit: int, lang: str = "") -> str:
    text = text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more chars]"
    return f"```{lang}\n{text}\n```"


def render(row: dict, limit: int) -> str:
    out = []
    label = row.get("label")
    gt = [l.get("name", l) if isinstance(l, dict) else l for l in label] if isinstance(label, list) else label

    out.append(f"# Trajectory — {row.get('qid')}\n")
    out.append("| | |")
    out.append("|---|---|")
    for k in ("task", "subject_id", "hadm_id", "prediction_time", "status",
              "completed", "stop_reason", "session_id"):
        if k in row:
            out.append(f"| {k} | `{row[k]}` |")
    out.append("")

    if gt:
        out.append("**Ground truth**\n")
        for g in gt:
            out.append(f"- {g}")
        out.append("")

    messages = row.get("messages") or []

    # Rounds are delimited by assistant turns; tool messages belong to the
    # assistant turn immediately before them.
    out.append("---\n\n## Prompt\n")
    for m in messages[:2]:
        out.append(f"**{m['role']}**\n")
        out.append(fence(m.get("content") or "", limit))
        out.append("")

    round_num = 0
    i = 2
    final_answer = None
    while i < len(messages):
        m = messages[i]
        if m.get("role") != "assistant":
            i += 1
            continue
        round_num += 1
        out.append(f"---\n\n## Round {round_num}\n")

        if m.get("reasoning_content"):
            out.append("**reasoning**\n")
            out.append(fence(m["reasoning_content"], limit))
            out.append("")
        if (m.get("content") or "").strip():
            out.append("**assistant**\n")
            out.append(fence(m["content"], limit))
            out.append("")

        calls = m.get("tool_calls") or []
        # The tool results follow in order, one per call.
        results = []
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            results.append(messages[j])
            j += 1

        for k, tc in enumerate(calls):
            name = tc["function"]["name"]
            args = tc["function"]["arguments"]
            try:
                args = json.dumps(json.loads(args), ensure_ascii=False, indent=2)
            except Exception:
                pass
            out.append(f"### 🔧 `{name}`\n")
            out.append(fence(args, limit, "json"))
            if k < len(results):
                res = results[k].get("content") or ""
                out.append(f"\n<sub>result ({len(res)} chars)</sub>\n")
                out.append(fence(res, limit))
            out.append("")
            if name.endswith("finish"):
                final_answer = args

        i = j if j > i else i + 1

    if final_answer:
        out.append("---\n\n## Final answer (ehr.finish)\n")
        out.append(fence(final_answer, 8000, "json"))

    out.append(f"\n---\n\n{round_num} rounds, {len(messages)} messages.")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", help="path to results.jsonl")
    ap.add_argument("-o", "--output", help="output .md (default: alongside input)")
    ap.add_argument("--index", type=int, default=0, help="which row to render")
    ap.add_argument("--limit", type=int, default=2500,
                    help="max chars per rendered block")
    args = ap.parse_args()

    rows = [json.loads(line) for line in Path(args.results).read_text().splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"{args.results} is empty")
    row = rows[args.index]

    out = Path(args.output) if args.output else Path(args.results).with_suffix(".trajectory.md")
    out.write_text(render(row, args.limit))
    print(f"wrote {out}  ({out.stat().st_size:,} bytes, {len(rows)} row(s) in input)")


if __name__ == "__main__":
    main()
