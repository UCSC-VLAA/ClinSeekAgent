#!/usr/bin/env python3
"""
Prepare ClinSeek trajectory data for VERL SFT training.

Loads from HuggingFace, preserves OpenAI-native tool-calling schema so the
tokenizer's chat template (e.g. Qwen3.5) can render native <tool_call> /
<tool_response> blocks, then splits into train/val and saves as parquet.

Output parquet rows have shape:
    {"messages": [ {role, content, [tool_calls], [tool_call_id]}, ... ]}

Assistant turns keep structured tool_calls:
    tool_calls = [{"id": str, "type": "function",
                   "function": {"name": str, "arguments": dict}}]

Tool turns keep tool_call_id so the template can match responses to calls.

The script filters samples whose rendered length exceeds --max_token_length.
"""

import argparse
import json
import os
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


def build_messages(item):
    return [_normalize_message(m) for m in item["messages"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_token_length", type=int, default=65536,
                        help="Max token length; samples longer than this are filtered out")
    parser.add_argument("--model_name", type=str, default="Qwen/Qwen3.5-35B-A3B",
                        help="Model name or local path for tokenizer / chat template")
    parser.add_argument("--output_dir", type=str,
                        default=os.path.expanduser('~/data/clinseek_trajectory'),
                        help="Output directory for train/val parquet files")
    parser.add_argument("--repo_id", type=str, required=True,
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
    print("Downloading ClinSeek trajectory JSONL from Hugging Face...")
    jsonl_path = hf_hub_download(
        repo_id=args.repo_id,
        filename=args.filename,
        repo_type="dataset",
    )
    print(f"  {jsonl_path}")

    print("Normalizing messages (preserving structured tool_calls / tool_call_id)...")
    rows = []
    with open(jsonl_path) as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            rows.append({"messages": build_messages(item)})
    print(f"Total samples: {len(rows)}")
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
    val_df = df.iloc[:val_size].reset_index(drop=True)
    train_df = df.iloc[val_size:].reset_index(drop=True)
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
