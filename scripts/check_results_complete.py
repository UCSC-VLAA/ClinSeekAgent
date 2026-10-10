#!/usr/bin/env python3
"""Check that a results.jsonl is complete and safe to score.

A model turn without ``ehr.finish`` (`status=incomplete`) or a request that
exceeded the model's context window is a model outcome: such rows stay in the
file and score F1 = 0. The file is refused when expected qids are missing or
duplicated, when rows failed before reaching the model (`status=error`, e.g.
rate limits or a dead MCP session -- rerun the arm; the runners retry exactly
those rows on resume), or when too many tool results are infrastructure errors
(a stale MCP server answers every call with an error string).

    python scripts/check_results_complete.py outputs/<arm>/results.jsonl --expect 1800
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

# Tool results that mean the call never reached a working server, as opposed to
# a server that answered "no such table for this patient". The second kind is a
# normal negative result and must not gate scoring; the first kind means the
# model answered without the evidence it asked for.
#
# `Unknown tool` is included because a routing error (an image tool sent to the
# EHR server) still lets rows finish with status=success; only the tool results
# reveal it.
_INFRA_TOOL_ERROR = re.compile(
    r'^(MCP Error'
    r'|Tool Error: .*Unknown tool'
    r'|No data in SSE'
    r'|Invalid MCP response'
    r'|Error: Failed to initialize'
    r'|Failed to initialize MCP session'
    r'|Error executing .*: .*(Connection|Timeout|timed out))',
    re.I | re.S)

# A request can legitimately exhaust the fixed context window after the model
# chooses to retrieve very large records.  This is a model/trajectory outcome,
# not an outage: preserve the row and score it as an empty prediction instead
# of retrying it under a different effective trajectory.
_MODEL_RESULT_ERROR = re.compile(
    r"maximum context length is \d+ tokens.*prompt contains at least \d+ input tokens",
    re.I | re.S)


def audit(path: Path, expect: int = 0) -> dict:
    rows = 0
    ok = 0
    empty = 0
    empty_but_ok = 0
    incomplete = 0
    model_failures = 0
    dupes: collections.Counter = collections.Counter()
    errors: collections.Counter = collections.Counter()
    tool_calls = 0
    tool_infra_errors = 0
    rows_all_tools_failed = 0
    infra_kinds: collections.Counter = collections.Counter()
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rows += 1
            dupes[r.get("qid")] += 1
            if r.get("status") in ("ok", "success") and r.get("completed"):
                ok += 1
            elif r.get("status") == "incomplete":
                # The model was reached and did not produce a finish call.
                # That is a datapoint, not an outage: report it, keep it.
                incomplete += 1
            else:
                msg = str(r.get("error") or r.get("stop_reason") or r.get("status"))
                if _MODEL_RESULT_ERROR.search(msg):
                    model_failures += 1
                else:
                    errors["429" if "429" in msg else msg.split(":")[0][:40]] += 1
            n_tool = n_bad = 0
            for m in r.get("messages", []):
                if m.get("role") != "tool":
                    continue
                n_tool += 1
                body = str(m.get("content") or "")
                if _INFRA_TOOL_ERROR.match(body):
                    n_bad += 1
                    infra_kinds[body[:60]] += 1
            tool_calls += n_tool
            tool_infra_errors += n_bad
            # A row whose every tool call failed answered with no evidence at
            # all, whatever its status says.
            if n_tool and n_bad == n_tool:
                rows_all_tools_failed += 1

            calls = [tc for m in r.get("messages", []) if m.get("tool_calls")
                     for tc in m["tool_calls"] if "finish" in tc["function"]["name"]]
            try:
                preds = json.loads(calls[-1]["function"]["arguments"]).get("response")
            except Exception:
                preds = None
            if not preds:
                if r.get("status") in ("ok", "success") and r.get("completed"):
                    empty_but_ok += 1      # the model itself declined to answer
                else:
                    empty += 1             # empty because the call never landed
    return {
        "path": str(path), "rows": rows, "ok": ok,
        "failed": rows - ok - incomplete - model_failures,
        "model_failures": model_failures, "incomplete": incomplete,
        "empty_answers": empty, "no_answer_model": empty_but_ok, "expect": expect,
        "tool_calls": tool_calls, "tool_infra_errors": tool_infra_errors,
        "rows_all_tools_failed": rows_all_tools_failed,
        "infra_kinds": dict(infra_kinds.most_common(3)),
        "duplicate_qids": sum(1 for n in dupes.values() if n > 1),
        "missing": max(0, expect - len({q for q in dupes})) if expect else 0,
        "error_kinds": dict(errors),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--expect", type=int, default=0,
                    help="expected row count; 0 to skip the completeness check")
    ap.add_argument("--max-tool-error-frac", type=float, default=0.05,
                    help="tolerated fraction of tool results that are "
                         "infrastructure failures (default 5%%). Healthy arms "
                         "sit at 0-1%%; the arm that silently lost every image "
                         "tool sat at 44%%.")
    args = ap.parse_args()

    bad = False
    for p in args.results:
        a = audit(Path(p), args.expect)
        status = "OK"
        # `failed` gates: those rows never reached the model (429s, dead MCP
        # sessions) and carry an empty answer that would read as a result.
        # `incomplete` does NOT gate: the model was reached and did not
        # answer, which is a real datapoint.
        tool_frac = a["tool_infra_errors"] / max(1, a["tool_calls"])
        if (a["failed"] or a["missing"] or a["duplicate_qids"]
                or tool_frac > args.max_tool_error_frac
                or a["rows_all_tools_failed"] > 0.02 * max(1, a["rows"])):
            status = "NOT SCOREABLE"
            bad = True
        frac = a["no_answer_model"] / max(1, a["rows"])
        print(f"[{status}] {Path(p).parent.name}")
        print(f"    rows={a['rows']} ok={a['ok']} failed={a['failed']} "
              f"model_failed={a['model_failures']} "
              f"incomplete={a['incomplete']} dupes={a['duplicate_qids']} "
              f"missing={a['missing']}")
        print(f"    no_answer_from_model={a['no_answer_model']} ({frac:.1%}) "
              f"<- scored as-is")
        if a["tool_calls"]:
            mark = ("  <-- tool infrastructure failure"
                    if tool_frac > args.max_tool_error_frac else "")
            print(f"    tool_calls={a['tool_calls']} infra_errors="
                  f"{a['tool_infra_errors']} ({tool_frac:.1%}) "
                  f"rows_with_no_working_tool={a['rows_all_tools_failed']}{mark}")
        if a["error_kinds"]:
            print(f"    errors: {a['error_kinds']}")
        if a["infra_kinds"] and tool_frac > args.max_tool_error_frac:
            for k, v in a["infra_kinds"].items():
                print(f"      {v}x {k}")
    if bad:
        print("\nRefusing to certify: qids are missing/duplicated, rows failed "
              "before reaching the model, or tool results are dominated by "
              "infrastructure errors. Rerun the arm (resume re-runs only the "
              "failed rows). Incomplete rows are valid and score 0.",
              file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
