"""Build train/val parquet files for EHR multi-turn GRPO.

Reads `data/MIMICIVAgentBench/common/*_500.json` and produces
`data/ehr_rl_qwen35/{train,val}.parquet` with the schema verl's
RLHFDataset expects:

  data_source : str
  prompt      : list[dict]   # chat messages (system + user)
  ability     : str
  reward_model: {"style":"rule", "ground_truth": <labels>}
  extra_info  : {"tools_kwargs": {<tool_name>: {"create_kwargs": {...}}},
                  "interaction_kwargs": {"name":"ehr_eval", "ground_truth":..., "task_type":...},
                  "need_tools_kwargs": True}

Usage:
  python verl_rl_ehr/preprocess/build_ehr_rl_parquet.py \
      --in-dir /fsx-shared/juncheng/EHR/data/MIMICIVAgentBench/common \
      --out    /fsx-shared/juncheng/EHR/data/ehr_rl_qwen35 \
      --val-frac 0.1                      # stratified by task_type
      [--limit-per-task 10]               # for smoke tests
      [--tasks diagnoses_ccs procedures_ccs labevents prescriptions microbiologyevents]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


_PROJECT_ROOT = Path("/fsx-shared/juncheng/EHR")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from openresearcher_ehr.data_utils import (  # noqa: E402
    DEVELOPER_CONTENT_CLAUDE,
    TASK_PROMPT_TEMPLATES,
)


# Appended to the baseline system prompt. The SFT model rarely calls the
# tool-formatted ehr.finish (~5% rate in the v2 20-step run), so we steer
# toward a plain-text <answer>…</answer> block at the end of the trajectory.
# This is closer to Qwen's native generation format and avoids tool-call
# JSON parsing. ehr.finish remains available as a tool and is still picked
# up by the reward function for rollouts that do use it.
_ANSWER_NUDGE = (
    "**Final answer format — required.** When you have gathered enough "
    "evidence, submit your final answer by calling the `ehr.finish` tool. "
    "Pass your answers as a JSON array of strings under the `response` "
    "parameter — for example:\n"
    "`<tool_call><function=ehr.finish><parameter=response>[\"Diabetes\", "
    "\"Hypertension\"]</parameter></function></tool_call>`\n"
    "A rollout that ends without a successful `ehr.finish` call receives a "
    "format penalty. Submit earlier if you are uncertain rather than "
    "exhausting your turn budget. Do NOT produce a natural-language final "
    "answer in free text — `ehr.finish` is the only scored submission path.\n\n"
    "**Terminology alignment — required before submission.** Answers are "
    "scored against canonical candidate-table names (e.g. `diagnoses_ccs_candidates`, "
    "`labevents_candidates`, `prescriptions_atc_candidates`, "
    "`microbiologyevents_candidates`, `procedures_ccs_candidates`, "
    "`transfers_candidates`). Before you commit to `ehr.finish`, look up each "
    "candidate name you plan to include via `ehr.get_candidates_by_semantic_similarity` "
    "or `ehr.get_candidates_by_keyword`, and use the EXACT name the tool "
    "returns. Do not add qualifier suffixes (e.g. \", Whole Blood\", "
    "\", Serum\") that the canonical name does not carry — the scorer will "
    "not recognize them as a match."
)
_RL_SYSTEM_PROMPT = DEVELOPER_CONTENT_CLAUDE.strip() + "\n\n" + _ANSWER_NUDGE


# Subset of EHR tool names that accept per-sample context. Must match the
# `class_name: verl_rl_ehr.tools.ehr_common_tool.EHRMCPTool` entries in the
# tool config yaml — browser/finish tools do not need create_kwargs.
EHR_TOOL_NAMES = (
    "ehr.load_ehr",
    "ehr.get_table_names",
    "ehr.get_column_names",
    "ehr.get_records_by_time",
    "ehr.run_sql_query",
    "ehr.get_candidates_by_semantic_similarity",
    "ehr.get_candidates_by_keyword",
    "ehr.finish",
)


def _extract_labels(task_type: str, label_entries: list[dict[str, Any]]) -> list[str]:
    """Flatten the per-task label list to a list[str] of canonical names."""
    out: list[str] = []
    if not isinstance(label_entries, list):
        return out
    # Special-case prescriptions: prefer atc_name when present, else name.
    has_atc = any(isinstance(e, dict) and e.get("atc_name") for e in label_entries)
    for e in label_entries:
        if isinstance(e, str):
            if e.strip():
                out.append(e.strip())
            continue
        if not isinstance(e, dict):
            continue
        if has_atc and e.get("atc_name"):
            out.append(str(e["atc_name"]).strip())
        elif e.get("name"):
            out.append(str(e["name"]).strip())
    # Dedupe (order-preserving) and drop empties
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _build_prompt(task_type: str, subject_id: Any, prediction_time: str) -> list[dict[str, str]]:
    template = TASK_PROMPT_TEMPLATES.get(task_type, TASK_PROMPT_TEMPLATES["diagnoses_ccs"])
    user_msg = template.format(subject_id=str(subject_id), current_time=prediction_time)
    return [
        {"role": "system", "content": _RL_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def _build_tools_kwargs(subject_id: Any, prediction_time: str, task_type: str) -> dict[str, Any]:
    ctx = {"subject_id": str(subject_id), "prediction_time": prediction_time, "task_type": task_type}
    return {name: {"create_kwargs": dict(ctx)} for name in EHR_TOOL_NAMES}


def _record(entry: dict[str, Any], task_type_fallback: str) -> dict[str, Any]:
    subject_id = entry.get("subject_id") or entry.get("subject")
    prediction_time = entry.get("prediction_time") or entry.get("current_time") or ""
    task_type = entry.get("task") or task_type_fallback
    labels = _extract_labels(task_type, entry.get("label", []))

    prompt = _build_prompt(task_type, subject_id, prediction_time)
    tools_kwargs = _build_tools_kwargs(subject_id, prediction_time, task_type)
    interaction_kwargs = {
        "name": "ehr_eval",
        "ground_truth": labels,
        "task_type": task_type,
    }

    return {
        "data_source": f"ehr_bench/{task_type}",
        "prompt": prompt,
        "ability": "ehr-multiturn",
        "reward_model": {"style": "rule", "ground_truth": labels},
        "extra_info": {
            "tools_kwargs": tools_kwargs,
            "interaction_kwargs": interaction_kwargs,
            "need_tools_kwargs": True,
            "subject_id": str(subject_id),
            "prediction_time": prediction_time,
            "task_type": task_type,
        },
    }


def load_task(task_path: Path, limit: int | None) -> list[dict[str, Any]]:
    with open(task_path) as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        raise ValueError(f"{task_path}: expected a JSON list")
    task_type = task_path.stem.replace("_500", "")
    rows = []
    for e in entries:
        rec = _record(e, task_type_fallback=task_type)
        # Drop samples with empty ground truth — those can't be scored.
        if not rec["reward_model"]["ground_truth"]:
            continue
        rows.append(rec)
    if limit is not None:
        rows = rows[:limit]
    return rows


def stratified_split(rows: list[dict[str, Any]], val_frac: float, seed: int
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_task[r["extra_info"]["task_type"]].append(r)
    train: list[dict[str, Any]] = []
    val: list[dict[str, Any]] = []
    for task, items in sorted(by_task.items()):
        rng.shuffle(items)
        n_val = max(1, int(round(len(items) * val_frac))) if len(items) > 1 else 0
        val.extend(items[:n_val])
        train.extend(items[n_val:])
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-dir", default="/fsx-shared/juncheng/EHR/data/MIMICIVAgentBench/common")
    ap.add_argument("--out", default="/fsx-shared/juncheng/EHR/data/ehr_rl_qwen35")
    ap.add_argument("--tasks", nargs="*", default=None,
                    help="Which *_500.json files to include (stems). Default = all.")
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--limit-per-task", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    task_files: list[Path]
    if args.tasks:
        task_files = [in_dir / f"{t}_500.json" for t in args.tasks]
    else:
        task_files = sorted(in_dir.glob("*_500.json"))

    all_rows: list[dict[str, Any]] = []
    per_task_counts = {}
    for tf in task_files:
        if not tf.exists():
            print(f"[warn] missing {tf}; skipping", file=sys.stderr)
            continue
        rows = load_task(tf, args.limit_per_task)
        per_task_counts[tf.stem] = len(rows)
        all_rows.extend(rows)
        print(f"  {tf.name:30s} -> {len(rows):5d} rows")

    if not all_rows:
        print("No rows produced. Exiting.", file=sys.stderr)
        return 2

    train_rows, val_rows = stratified_split(all_rows, args.val_frac, args.seed)
    train_df = pd.DataFrame(train_rows)
    val_df = pd.DataFrame(val_rows)
    train_path = out / "train.parquet"
    val_path = out / "val.parquet"
    train_df.to_parquet(train_path, index=False)
    val_df.to_parquet(val_path, index=False)

    print(f"Wrote {train_path} ({len(train_df)} rows)")
    print(f"Wrote {val_path}   ({len(val_df)} rows)")
    print("per-task counts:", per_task_counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
