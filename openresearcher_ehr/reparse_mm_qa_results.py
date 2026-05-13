"""Re-run parse_answer on an existing mm_qa results.jsonl in place.

Rewrites each row's `predictions`, `parse_mode`, and the ehr.finish tool_call
arguments using the current eval_mm_qa.parse_answer implementation. Produces
a new results.jsonl so the scorer can be re-run without regenerating model
completions.

Usage:
  python reparse_mm_qa_results.py --input <in.jsonl> --output <out.jsonl>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from eval_mm_qa import parse_answer  # noqa: E402


def reparse_row(row: dict) -> dict:
    msgs = row.get("messages") or []
    assistant_msg = None
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            assistant_msg = m
            break
    raw = (assistant_msg or {}).get("content") or ""
    preds, parse_mode = parse_answer(raw)
    row["predictions"] = preds
    row["parse_mode"] = parse_mode
    if assistant_msg is not None:
        assistant_msg["tool_calls"] = [
            {
                "id": "call_qa_finish",
                "type": "function",
                "function": {
                    "name": "ehr.finish",
                    "arguments": json.dumps({"response": preds}, ensure_ascii=False),
                },
            }
        ]
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    before = Counter()
    after = Counter()
    pred_changed = 0
    total = 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.input.open("r", encoding="utf-8") as fin, args.output.open(
        "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1
            before[row.get("parse_mode")] += 1
            old_preds = row.get("predictions")
            row = reparse_row(row)
            after[row.get("parse_mode")] += 1
            if old_preds != row.get("predictions"):
                pred_changed += 1
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[reparse] total rows: {total}")
    print(f"[reparse] predictions changed: {pred_changed}")
    print(f"[reparse] parse_mode before: {dict(before)}")
    print(f"[reparse] parse_mode after:  {dict(after)}")


if __name__ == "__main__":
    main()
