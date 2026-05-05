"""Ask Claude Opus 4.6 (Bedrock) to identify the vital tool calls in an
agentic trajectory.

Input:
  --trajectory path/to/trajectory_caseX.md     (dumped by _dump_trajectories.py)
  --gold      "list of gold label strings"     (also parsed from the trajectory
                                                 header, so usually omitted)
  --mode      won_the_answer | lost_the_answer  (default: won_the_answer)
  --out       path/to/analysis.json             (required)

Output JSON:
{
    "case_id": "<qid>",
    "task": "...",
    "gold": ...,
    "final_answer": ...,
    "mode": "won_the_answer" | "lost_the_answer",
    "vital_calls": [{"index": N, "name": "...", "evidence": "...", "used_for": "..."}],
    "could_have_saved_it": [...],       # only populated when mode = lost_the_answer
    "misleading_calls": [...],          # only populated when mode = lost_the_answer
    "root_cause_or_summary": "..."
}

The script calls Claude Opus 4.6 on Bedrock (us-east-1) with a single prompt
that embeds the trajectory verbatim (capped at ~180k chars to stay under the
200k token window) and asks for strict JSON output inside an <analysis>
block.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import boto3
from botocore.config import Config as BotoConfig


MODEL_ID = "us.anthropic.claude-opus-4-6-v1"
REGION = "us-east-1"
MAX_TOKENS = 8192
MAX_TRAJ_CHARS = 170_000


SYSTEM_WIN = """You are a clinical-AI researcher analyzing an agentic LLM trajectory.

The agent below ran a multi-step tool loop and produced a final answer that
matched (or partially matched) the gold label. You will also be shown the
REASONING-MODE transcript — the same model answering the same question in one
shot from a hand-curated EHR context — which got the answer wrong.

Your job is to:
  (1) Pick the vital agentic tool calls that produced evidence the final answer
      depended on.
  (2) Diagnose why the reasoning-mode model got the answer wrong — what
      specific evidence was missing from the curated prompt, and which sentence
      in the reasoning-mode reply reveals the slip (pull_quote).

Rules:
  * "Vital" means the call's *result* contained specific evidence that is
    cited (verbatim or paraphrased) in the model's final ehr.think synthesis
    or whose absence would have caused a different answer.
  * Scouting calls (ehr.load_ehr, ehr.get_table_names, raw SELECT * that the
    agent ignored) are NOT vital even if they preceded the discovery.
  * Prefer 3–8 vital calls. Never more than 10.
  * Evidence paraphrase: <= 25 words per call; "used_for" <= 30 words.
  * failure_type must be one of:
      "missing_evidence_in_prompt"   — the curated prompt didn't contain the
                                       key table/field the agentic path used.
      "under_weighted_signal"        — the signal was technically present in
                                       the prompt but the model missed/weighed
                                       it incorrectly.
      "wrong_clinical_inference"     — model made a reasonable-sounding
                                       clinical conclusion from limited data.
      "format_or_length_error"       — output shape (too few/many items, wrong
                                       verbiage) caused a correct intuition to
                                       score zero.
  * "pull_quote" must be a <= 40-word verbatim snippet from the reasoning-mode
    reply (use ... for elisions).

Output strictly ONE <analysis>...</analysis> block containing one JSON object.
No text outside the block.
""".strip()


SYSTEM_LOSS = """You are a clinical-AI researcher analyzing an agentic LLM trajectory
that produced a WRONG answer.

The model below ran a multi-step tool loop and produced an answer that
DISAGREES with the gold label. Your job is to identify:
  1. The MISLEADING calls whose results pushed the agent toward the wrong
     inference (e.g. tangential clinical guidelines, distant historical data,
     irrelevant microbiology).
  2. The calls that COULD HAVE SAVED the agent — calls whose results contained
     the evidence that matches the gold answer, but the agent under-weighted
     or ignored them.

Rules:
  * Prefer 3–6 misleading calls and 1–3 could-have-saved calls.
  * "evidence" <= 25 words; "misused_as" / "ignored_because" <= 30 words.
  * root_cause_or_summary: <= 60 words explaining in plain language why the
    agent chose the wrong answer.

Output strictly ONE <analysis>...</analysis> block containing one JSON object.
No text outside the block.
""".strip()


USER_TEMPLATE = """Task: {task}
Gold label: {gold}
Agentic final answer: {final_answer}

=== AGENTIC TRAJECTORY (every tool call in order, with args + truncated results) ===
{trajectory}
=== END AGENTIC TRAJECTORY ===

{reasoning_block}

Emit strict JSON with these keys (nothing outside the <analysis> block):

{schema}
""".strip()


WIN_SCHEMA = """
{
  "case_id": "<copied from trajectory qid>",
  "task": "<task name>",
  "gold": <gold label, string or list>,
  "final_answer": <model final answer>,
  "mode": "won_the_answer",
  "vital_calls": [
    {"index": <int>, "name": "<tool name>", "evidence": "<=25 words",
     "used_for": "<=30 words"}
  ],
  "non_vital_summary": "<=40 words summarizing what the other calls did",
  "reasoning_failure_analysis": {
    "final_answer": "<reasoning-mode prediction>",
    "failure_type": "<one of the four failure_type values>",
    "where_it_went_wrong": "<=60 word explanation of the reasoning model's slip",
    "missed_evidence": "<=40 words naming the specific evidence the agentic path used that the reasoning prompt lacked or that reasoning mode overlooked",
    "pull_quote": "<<=40-word verbatim snippet from the reasoning reply>"
  }
}
""".strip()


LOSS_SCHEMA = """
{
  "case_id": "<copied from trajectory qid>",
  "task": "<task name>",
  "gold": <gold label>,
  "final_answer": <model final answer>,
  "mode": "lost_the_answer",
  "misleading_calls": [
    {"index": <int>, "name": "<tool name>", "evidence": "<=25 words",
     "misused_as": "<=30 words on how agent mis-used the result"}
  ],
  "could_have_saved_it": [
    {"index": <int>, "name": "<tool name>", "evidence": "<=25 words",
     "ignored_because": "<=30 words on why agent ignored/under-weighted this"}
  ],
  "root_cause_or_summary": "<=60 word plain-language diagnosis"
}
""".strip()


def _parse_traj_header(trajectory: str) -> Dict[str, Any]:
    header: Dict[str, Any] = {}
    m = re.search(r"qid\s+`([^`]+)`", trajectory)
    if m:
        header["qid"] = m.group(1)
    m = re.search(r"#\s*Agentic trajectory\s+—\s+([^\n—]+?)\s+—", trajectory)
    if m:
        header["task"] = m.group(1).strip()
    m = re.search(r"\*\*gold label:\*\*\s+`([^`]+)`", trajectory)
    if m:
        header["gold_raw"] = m.group(1)
    m = re.search(
        r"## Final answer \(`ehr\.finish` arguments\)\s*\n```\s*\n(.+?)\n```",
        trajectory, re.DOTALL,
    )
    if m:
        header["final_answer_raw"] = m.group(1).strip()
    return header


def _build_messages(trajectory: str, mode: str,
                    reasoning: str = "") -> List[Dict[str, Any]]:
    hdr = _parse_traj_header(trajectory)
    task = hdr.get("task", "unknown")
    gold = hdr.get("gold_raw", "?")
    final = hdr.get("final_answer_raw", "?")
    schema = WIN_SCHEMA if mode == "won_the_answer" else LOSS_SCHEMA
    traj_excerpt = trajectory
    if len(traj_excerpt) > MAX_TRAJ_CHARS:
        traj_excerpt = traj_excerpt[:MAX_TRAJ_CHARS] + \
            f"\n\n…[trajectory truncated {len(trajectory) - MAX_TRAJ_CHARS} chars]"
    reasoning_block = ""
    if reasoning:
        r = reasoning
        if len(r) > 20000:
            r = r[:20000] + f"\n…[reasoning truncated {len(reasoning)-20000} chars]"
        reasoning_block = (
            "=== REASONING-MODE RUN (user-curated evidence, single turn) ===\n"
            f"{r}\n"
            "=== END REASONING-MODE RUN ===\n"
        )
    user = USER_TEMPLATE.format(
        task=task, gold=gold, final_answer=final,
        trajectory=traj_excerpt, reasoning_block=reasoning_block, schema=schema,
    )
    return [{"role": "user",
             "content": [{"type": "text", "text": user}]}]


def invoke_bedrock(system: str, messages: List[Dict[str, Any]],
                   region: str = REGION, model_id: str = MODEL_ID) -> str:
    client = boto3.client(
        "bedrock-runtime", region_name=region,
        config=BotoConfig(read_timeout=600, connect_timeout=10,
                          retries={"max_attempts": 0}),
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "system": system,
        "messages": messages,
    }
    last_err = None
    for attempt in range(4):
        try:
            resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
            out = json.loads(resp["body"].read())
            parts = [blk.get("text", "") for blk in (out.get("content") or [])
                     if blk.get("type") == "text"]
            return "\n".join(parts).strip()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Bedrock invoke failed: {last_err}")


def parse_analysis_block(text: str) -> Dict[str, Any]:
    m = re.search(r"<analysis[^>]*>\s*(.*?)\s*</analysis\s*>", text,
                  re.DOTALL | re.IGNORECASE)
    if m:
        blob = m.group(1).strip()
    else:
        # Best-effort: pick first balanced-brace JSON object.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Could not find <analysis> block nor JSON object")
        blob = text[start:end + 1]
    return json.loads(blob)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trajectory", required=True)
    ap.add_argument("--reasoning", default=None,
                    help="Optional path to reasoning-mode markdown dump; "
                    "required for won_the_answer to populate "
                    "reasoning_failure_analysis.")
    ap.add_argument("--mode", choices=["won_the_answer", "lost_the_answer"],
                    default="won_the_answer")
    ap.add_argument("--out", required=True)
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--model-id", default=MODEL_ID)
    args = ap.parse_args()

    trajectory = Path(args.trajectory).read_text()
    reasoning = Path(args.reasoning).read_text() if args.reasoning else ""
    messages = _build_messages(trajectory, args.mode, reasoning=reasoning)
    system = SYSTEM_WIN if args.mode == "won_the_answer" else SYSTEM_LOSS
    raw = invoke_bedrock(system, messages, region=args.region, model_id=args.model_id)
    try:
        analysis = parse_analysis_block(raw)
    except Exception as exc:
        Path(args.out + ".raw").write_text(raw)
        raise SystemExit(
            f"Failed to parse analysis block. Raw response saved to {args.out}.raw: {exc}"
        )
    Path(args.out).write_text(json.dumps(analysis, indent=2))
    print(f"[{args.mode}] wrote analysis → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
