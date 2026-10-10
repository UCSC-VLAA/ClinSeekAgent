#!/usr/bin/env python3
"""
Prepare ClinSeekAgent trajectory data for VERL SFT training.

Reads trajectory JSONL (ClinSeekAgent `results.jsonl` rows, from local files
via --local_path or from a Hugging Face dataset via --repo_id), preserves the
OpenAI-native tool-calling schema so the tokenizer's chat template (e.g.
Qwen3.5) renders native <tool_call> / <tool_response> blocks, then splits into
train/val and saves as parquet.

Paper recipe (ClinSeek-35B-A3B): teacher trajectories that end with an answer
(--success_only), with the teacher's `ehr.think` calls folded into the
student's native thinking block (--merge_think --strip_think_protocol), capped
at 52K tokens, 2% random validation split (seed 42):

    python verl/examples/sft/clinseek/prepare_clinseek_data.py \
        --local_path /path/to/teacher_results.jsonl \
        --success_only --merge_think --strip_think_protocol \
        --model_name /path/to/Qwen3.5-35B-A3B --max_token_length 52000 \
        --output_dir data/clinseek_sft_52k

--merge_think: each assistant turn that only calls `ehr.think` is removed
together with its tool response, and its text becomes the `reasoning_content`
of the next assistant turn (consecutive think turns are joined with blank
lines); in a turn that mixes `ehr.think` with other calls only the think call
is removed. The chat template renders `reasoning_content` as the native
<think>...</think> block, so the student reasons in its thinking channel
instead of calling a tool. The provider's own reasoning is dropped unless
--keep_provider_reasoning. --strip_think_protocol removes the "REASONING TRACE
PROTOCOL" section (the instruction to call `ehr.think` before every action)
from the system prompt, so the training prompt matches deployment.

Output parquet rows have shape:
    {"messages": [ {role, content, [tool_calls], [tool_call_id],
                    [reasoning_content]}, ... ], "qid": str, "source": str}

Assistant turns keep structured tool_calls:
    tool_calls = [{"id": str, "type": "function",
                   "function": {"name": str, "arguments": dict}}]

Tool turns keep tool_call_id so the template can match responses to calls.

The script filters samples whose rendered length exceeds --max_token_length.
"""

import argparse
import collections
import json
import os
import re
from concurrent.futures import ProcessPoolExecutor

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
from transformers import AutoProcessor, AutoTokenizer

# Worker-side globals populated by the pool initializer so each worker
# loads the tokenizer exactly once.
_WORKER_TOKENIZER = None


def _worker_init(model_name: str):
    global _WORKER_TOKENIZER
    try:
        _WORKER_TOKENIZER = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    except Exception:
        _WORKER_TOKENIZER = AutoProcessor.from_pretrained(model_name, trust_remote_code=True)


def _worker_tokenize(messages):
    """Render + tokenize one sample; return token length (or a sentinel on error)."""
    try:
        text = _WORKER_TOKENIZER.apply_chat_template(messages, tokenize=False)
        tokens = _WORKER_TOKENIZER(text, add_special_tokens=False)["input_ids"]
        return len(tokens)
    except Exception:
        return 10 ** 9


def _coerce_arguments(arguments):
    """Qwen3.5's chat template iterates arguments via |items, so it must be a
    dict. The source data stores arguments as JSON strings; parse
    them; fall back to wrapping raw text in {"_raw": ...} if parsing fails.
    """
    if isinstance(arguments, dict):
        return arguments
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw": arguments}
        return parsed if isinstance(parsed, dict) else {"_raw": arguments}
    return {"_raw": str(arguments)}


def _normalize_message(m):
    """Return a clean OpenAI-style message dict, preserving tool_calls and
    tool_call_id so downstream chat templates can render native tool syntax."""
    if isinstance(m, str):
        m = json.loads(m)

    role = m["role"]
    content = m.get("content") or ""
    out = {"role": role, "content": content}

    if role == "assistant":
        if m.get("reasoning_content"):
            out["reasoning_content"] = m["reasoning_content"]
        raw_calls = m.get("tool_calls")
        if raw_calls:
            if isinstance(raw_calls, str):
                raw_calls = json.loads(raw_calls)
            normalized = []
            for call in raw_calls:
                fn = call.get("function", {}) or {}
                normalized.append({
                    "id": call.get("id", ""),
                    "type": call.get("type", "function"),
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": _coerce_arguments(fn.get("arguments", {})),
                    },
                })
            out["tool_calls"] = normalized

    elif role == "tool":
        tcid = m.get("tool_call_id")
        if tcid:
            out["tool_call_id"] = tcid

    return out


def merge_think_calls(messages, think_tool="ehr.think", keep_provider_reasoning=False, stats=None):
    """Turn `think -> (tool response) -> action` rounds into a single action turn whose
    reasoning_content carries the think text, so the chat template renders it as a native
    <think> block instead of a tool call. Consecutive think turns are concatenated. Works on
    already-normalized messages (tool_call arguments are dicts)."""
    out, pending, skip_ids = [], [], set()
    for m in messages:
        if m["role"] == "tool" and m.get("tool_call_id") in skip_ids:
            skip_ids.discard(m["tool_call_id"])
            continue
        if m["role"] == "assistant":
            calls = m.get("tool_calls") or []
            names = {c["function"]["name"] for c in calls}
            if calls and names == {think_tool}:
                for c in calls:
                    args = c["function"]["arguments"]
                    text = args.get("response") if isinstance(args, dict) else None
                    if text is None:
                        text = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                    pending.append(text.strip())
                    if c.get("id"):
                        skip_ids.add(c["id"])
                if m.get("content"):
                    pending.append(m["content"].strip())
                if stats is not None:
                    stats["think_turns_folded"] += 1
                continue
            if think_tool in names:  # mixed turn: strip the think call, keep the rest
                kept = []
                for c in calls:
                    if c["function"]["name"] == think_tool:
                        args = c["function"]["arguments"]
                        pending.append((args.get("response") if isinstance(args, dict) else str(args)) or "")
                        if c.get("id"):
                            skip_ids.add(c["id"])
                    else:
                        kept.append(c)
                m = dict(m)
                m["tool_calls"] = kept
                if stats is not None:
                    stats["mixed_turns_split"] += 1
            if pending:
                parts = [p for p in pending if p]
                if keep_provider_reasoning and m.get("reasoning_content"):
                    parts.append(m["reasoning_content"].strip())
                m = dict(m)
                m["reasoning_content"] = "\n\n".join(parts)
                pending = []
            elif not keep_provider_reasoning:
                m = dict(m)
                m.pop("reasoning_content", None)
        out.append(m)
    if pending and stats is not None:
        stats["dangling_think_dropped"] += 1
    return out


_PROTOCOL_RE = re.compile(r"\n*-{3,}\s*\nREASONING TRACE PROTOCOL.*", re.S)


def strip_think_prompt(system_content, think_tool="ehr.think", drop_tool_line=False, stats=None):
    new = _PROTOCOL_RE.sub("", system_content)
    if new != system_content and stats is not None:
        stats["protocol_blocks_stripped"] += 1
    if drop_tool_line:
        new2 = re.sub(r"^- " + re.escape(think_tool) + r":[^\n]*\n", "", new, flags=re.M)
        if new2 != new and stats is not None:
            stats["think_tool_lines_dropped"] += 1
        new = new2
    return new.rstrip() + "\n"


def build_messages(item, merge_think=False, think_tool="ehr.think", keep_provider_reasoning=False, stats=None,
                   strip_protocol=False, drop_tool_line=False):
    messages = [_normalize_message(m) for m in item["messages"]]
    if merge_think:
        messages = merge_think_calls(messages, think_tool, keep_provider_reasoning, stats)
        if (strip_protocol or drop_tool_line) and messages and messages[0]["role"] == "system":
            messages[0] = dict(messages[0])
            messages[0]["content"] = strip_think_prompt(messages[0]["content"], think_tool, drop_tool_line, stats)
    elif not keep_provider_reasoning:
        for m in messages:
            m.pop("reasoning_content", None)
    return messages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_token_length", type=int, default=65536,
                        help="Max token length; samples longer than this are filtered out")
    parser.add_argument("--model_name", type=str, default="Qwen/Qwen3.5-35B-A3B",
                        help="Model name or local path for tokenizer / chat template")
    parser.add_argument("--output_dir", type=str,
                        default="data/clinseek_sft",
                        help="Output directory for train/val parquet files")
    parser.add_argument("--local_path", type=str, default=None, nargs="+",
                        help="Use one or more local JSONL files instead of downloading from --repo_id")
    parser.add_argument("--split_by_qid", action="store_true",
                        help="Assign train/val by qid (all trajectories of a question land in the same split); "
                             "use when several rollouts of the same prompt are present")
    parser.add_argument("--success_only", action="store_true",
                        help="Keep only rows with status == 'success' (drops errored rollouts)")
    parser.add_argument("--merge_think", action="store_true",
                        help="Fold each `ehr.think` tool call into the next assistant turn's reasoning_content "
                             "(rendered as <think>...</think> by the Qwen3.5 template) and drop the think "
                             "tool call together with its 'Thinking Finish' tool response")
    parser.add_argument("--think_tool_name", type=str, default="ehr.think")
    parser.add_argument("--strip_think_protocol", action="store_true",
                        help="With --merge_think, delete the 'REASONING TRACE PROTOCOL' section (the mandatory "
                             "ehr.think instruction) from the system prompt so the prompt matches deployment")
    parser.add_argument("--drop_think_tool_line", action="store_true",
                        help="Also remove the '- ehr.think: ...' entry from the system prompt's tool list")
    parser.add_argument("--keep_provider_reasoning", action="store_true",
                        help="With --merge_think, append the provider's own reasoning_content (when present) "
                             "after the folded think text instead of discarding it")
    parser.add_argument("--repo_id", type=str, default=None,
                        help="Hugging Face dataset repo containing the trajectory JSONL")
    parser.add_argument("--filename", type=str, default="clinseek_trajectories.jsonl",
                        help="JSONL file inside --repo_id")
    parser.add_argument("--val_ratio", type=float, default=0.02)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num_workers", type=int, default=min(16, (os.cpu_count() or 4)),
                        help="Parallel workers for tokenization")
    args = parser.parse_args()

    # Read the raw JSONL directly. `datasets.load_dataset` fails on this repo
    # because the `label` column has inconsistent struct schemas across rows;
    # we only need `messages`, so sidestep the arrow cast entirely.
    if args.local_path:
        jsonl_paths = list(args.local_path)
        print(f"Using local trajectory JSONL(s): {jsonl_paths}")
    else:
        assert args.repo_id, "--repo_id is required when --local_path is not given"
        print("Downloading ClinSeek trajectory JSONL from Hugging Face...")
        jsonl_path = hf_hub_download(
            repo_id=args.repo_id,
            filename=args.filename,
            repo_type="dataset",
        )
        print(f"  {jsonl_path}")
        jsonl_paths = [jsonl_path]

    print("Normalizing messages (preserving structured tool_calls / tool_call_id)...")
    rows = []
    merge_stats = collections.Counter()
    for jsonl_path in jsonl_paths:
        with open(jsonl_path) as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                if args.success_only and item.get("status", "success") != "success":
                    continue
                rows.append({"messages": build_messages(item, args.merge_think, args.think_tool_name,
                                                        args.keep_provider_reasoning, merge_stats,
                                                        args.strip_think_protocol, args.drop_think_tool_line),
                             "qid": item.get("qid", ""), "source": os.path.basename(jsonl_path)})
    print(f"Total samples: {len(rows)}")
    if args.merge_think:
        print(f"merge_think stats: {dict(merge_stats)}")
    df = pd.DataFrame(rows)
    print(f"Total samples before filtering: {len(df)}")

    # NOTE: prefer AutoTokenizer. AutoProcessor (e.g. Qwen3_5Processor) is
    # multimodal and crashes on plain text with "cannot identify image file".
    # We only need text tokenization; each worker reloads the tokenizer once.
    print(f"\nTokenizing with {args.num_workers} workers "
          f"(tokenizer: {args.model_name}, "
          f"filter threshold: {args.max_token_length})...")

    messages_list = df["messages"].tolist()
    token_lengths = [None] * len(messages_list)
    with ProcessPoolExecutor(
        max_workers=args.num_workers,
        initializer=_worker_init,
        initargs=(args.model_name,),
    ) as pool:
        it = pool.map(_worker_tokenize, messages_list, chunksize=8)
        for i, tlen in enumerate(it):
            token_lengths[i] = tlen
            if (i + 1) % 500 == 0:
                print(f"  Tokenized {i + 1}/{len(messages_list)}", flush=True)

    n_template_fail = sum(1 for t in token_lengths if t >= 10 ** 9)
    keep = [i for i, t in enumerate(token_lengths) if t <= args.max_token_length]

    # Length distribution report, only over samples that tokenized cleanly.
    import numpy as np
    valid = np.array([t for t in token_lengths if t < 10 ** 9])
    if len(valid):
        pcts = [50, 75, 90, 95, 97, 99, 99.5, 100]
        perc_vals = np.percentile(valid, pcts)
        print("\n====== Token-length distribution "
              f"({len(valid)} clean-tokenized samples) ======")
        print(f"  min   : {int(valid.min())}")
        print(f"  mean  : {int(valid.mean())}")
        print(f"  median: {int(np.median(valid))}")
        print(f"  max   : {int(valid.max())}")
        print("  percentiles:")
        for p, v in zip(pcts, perc_vals):
            print(f"    p{p:<5}: {int(v):>7}")

        caps = [8000, 12000, 16000, 24000, 32000, 40000, 48000,
                56000, 60000, 64000, 72000, 80000, 96000, 128000]
        print("  survivors by max-token cap:")
        for c in caps:
            n_ok = int((valid <= c).sum())
            print(f"    cap={c:<6}: keep {n_ok}/{len(valid)} "
                  f"({100 * n_ok / len(valid):.1f}%)")
        print("=" * 64 + "\n")

    before = len(df)
    df = df.loc[keep].reset_index(drop=True)
    print(f"Filtered out {before - len(df)} samples "
          f"({100 * (before - len(df)) / before:.1f}%; "
          f"{n_template_fail} were template failures)")
    print(f"Remaining samples: {len(df)}")

    print(f"\nSplitting dataset into train/val ({1 - args.val_ratio:.0%}/{args.val_ratio:.0%})...")
    df = df.sample(frac=1, random_state=args.seed).reset_index(drop=True)
    val_size = max(1, int(len(df) * args.val_ratio))
    if args.split_by_qid and "qid" in df.columns:
        import numpy as _np
        rng = _np.random.RandomState(args.seed)
        qids = df["qid"].unique().tolist()
        rng.shuffle(qids)
        val_qids, count = set(), 0
        for q in qids:
            if count >= val_size:
                break
            val_qids.add(q)
            count += int((df["qid"] == q).sum())
        is_val = df["qid"].isin(val_qids)
        val_df = df[is_val].reset_index(drop=True)
        train_df = df[~is_val].reset_index(drop=True)
        print(f"Split by qid: {len(val_qids)} val questions -> {len(val_df)} val rows; "
              f"train/val qid overlap = {len(set(train_df['qid']) & val_qids)}")
    else:
        val_df = df.iloc[:val_size].reset_index(drop=True)
        train_df = df.iloc[val_size:].reset_index(drop=True)
    if "source" in df.columns:
        print("rows per source file:", df["source"].value_counts().to_dict())
    print(f"Train samples: {len(train_df)}")
    print(f"Val samples: {len(val_df)}")

    os.makedirs(args.output_dir, exist_ok=True)
    train_path = os.path.join(args.output_dir, "train.parquet")
    val_path = os.path.join(args.output_dir, "val.parquet")

    # Messages across rows have heterogeneous keys (some have tool_calls, some
    # have tool_call_id, most have neither) AND each tool_call's arguments
    # dict has different keys per call. Pyarrow's auto-inferred schema chokes
    # on both. Build an explicit schema:
    #   - every message struct carries the full union of fields (null-filled)
    #   - `arguments` is stored as a JSON STRING per tool_call. Uniform schema,
    #     no map-vs-tuple round-trip issues. verl's MultiTurnSFTDataset is
    #     patched to `json.loads` this back into a dict before rendering.
    tool_call_struct = pa.struct([
        ("id", pa.string()),
        ("type", pa.string()),
        ("function", pa.struct([
            ("name", pa.string()),
            ("arguments", pa.string()),
        ])),
    ])
    message_struct = pa.struct([
        ("role", pa.string()),
        ("content", pa.string()),
        ("tool_calls", pa.list_(tool_call_struct)),
        ("tool_call_id", pa.string()),
        ("reasoning_content", pa.string()),
    ])
    schema = pa.schema([("messages", pa.list_(message_struct))])

    def _serialize_messages(messages):
        out = []
        for m in messages:
            entry = {
                "role": m["role"],
                "content": m.get("content") or "",
                "tool_calls": None,
                "tool_call_id": m.get("tool_call_id"),
                "reasoning_content": m.get("reasoning_content") or None,
            }
            if m.get("tool_calls"):
                entry["tool_calls"] = [{
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": json.dumps(
                            tc["function"].get("arguments") or {},
                            ensure_ascii=False,
                        ),
                    },
                } for tc in m["tool_calls"]]
            out.append(entry)
        return out

    def _write(df_part, path):
        records = [_serialize_messages(ms) for ms in df_part["messages"].tolist()]
        table = pa.Table.from_pydict({"messages": records}, schema=schema)
        pq.write_table(table, path)

    print(f"\nSaving train set to: {train_path}")
    _write(train_df, train_path)
    print(f"Saving val set to: {val_path}")
    _write(val_df, val_path)

    print("\nDataset preparation complete!")
    print(f"Train: {train_path}")
    print(f"Val:   {val_path}")


if __name__ == "__main__":
    main()
