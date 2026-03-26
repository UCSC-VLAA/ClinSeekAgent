#!/usr/bin/env python3
"""
Prepare DeepMed_trajectory dataset for verl SFT training.
Loads from HuggingFace, splits into train/val, and saves as parquet.
Filters out samples exceeding max_token_length.
"""

import os
import json
import argparse
import pandas as pd
from datasets import load_dataset
from transformers import AutoProcessor

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_token_length", type=int, default=65536,
                        help="Max token length; samples longer than this are filtered out")
    parser.add_argument("--model_name", type=str, default="Qwen/Qwen3.5-35B-A3B",
                        help="Model name for tokenizer")
    args = parser.parse_args()

    print("Loading DeepMed_trajectory dataset from HuggingFace...")
    ds = load_dataset('Letian2003/DeepMed_trajectory', split='train')

    print(f"Total samples: {len(ds)}")
    print(f"Dataset features: {ds.features}")

    # Convert to list of dicts with only role and content
    # Tool calls are embedded as text in assistant content for SFT compatibility
    print("\nParsing messages and converting to pandas DataFrame...")
    rows = []
    for item in ds:
        msgs = item['messages']
        cleaned = []
        for m in msgs:
            if isinstance(m, str):
                m = json.loads(m)
            role = m['role']
            content = m.get('content', '') or ''
            # For assistant messages with tool_calls, append tool call info to content
            tc = m.get('tool_calls')
            if tc and tc is not None:
                if isinstance(tc, str):
                    tc = json.loads(tc)
                calls_text = []
                for call in tc:
                    func = call.get('function', {})
                    calls_text.append(f"[Tool Call: {func.get('name', '')}({func.get('arguments', '')})]")
                if calls_text:
                    content = (content + '\n' + '\n'.join(calls_text)).strip()
            # Map tool role to user (for chat template compatibility)
            if role == 'tool':
                role = 'user'
                tool_id = m.get('tool_call_id', '')
                if tool_id:
                    content = f"[Tool Response]\n{content}"
            cleaned.append({'role': role, 'content': content})
        # Merge consecutive same-role messages
        merged = [cleaned[0]]
        for msg in cleaned[1:]:
            if msg['role'] == merged[-1]['role']:
                merged[-1]['content'] += '\n' + msg['content']
            else:
                merged.append(msg)
        rows.append({'messages': merged})

    df = pd.DataFrame(rows)
    print(f"Type check: {type(df['messages'].iloc[0][0])}")  # should be dict
    print(f"Total samples before filtering: {len(df)}")

    # Filter by token length
    print(f"\nFiltering samples with token length > {args.max_token_length}...")
    processor = AutoProcessor.from_pretrained(args.model_name, trust_remote_code=True)
    token_lengths = []
    for i, row in df.iterrows():
        try:
            tokens = processor.apply_chat_template(row['messages'], tokenize=True)
        except Exception:
            text = "\n".join([m['content'] for m in row['messages']])
            tokens = processor.tokenizer.encode(text)
        token_lengths.append(len(tokens))
        if (i + 1) % 500 == 0:
            print(f"  Tokenized {i + 1}/{len(df)}")
    df['token_length'] = token_lengths

    before_count = len(df)
    df = df[df['token_length'] <= args.max_token_length].reset_index(drop=True)
    filtered_count = before_count - len(df)
    print(f"Filtered out {filtered_count} samples ({100*filtered_count/before_count:.1f}%)")
    print(f"Remaining samples: {len(df)}")
    df = df.drop(columns=['token_length'])

    # Split into train (98%) and validation (2%)
    print("\nSplitting dataset into train/val (98/2)...")
    val_size = int(len(df) * 0.02)
    # Shuffle with seed for reproducibility
    df_shuffled = df.sample(frac=1, random_state=42).reset_index(drop=True)
    train_df = df_shuffled[val_size:]
    val_df = df_shuffled[:val_size]

    print(f"Train samples: {len(train_df)}")
    print(f"Val samples: {len(val_df)}")

    # Create output directory
    output_dir = os.path.expanduser('~/data/deepmed_trajectory')
    os.makedirs(output_dir, exist_ok=True)

    # Save to parquet using pandas (preserves nested dict structure)
    train_path = os.path.join(output_dir, 'train.parquet')
    val_path = os.path.join(output_dir, 'val.parquet')

    print(f"\nSaving train set to: {train_path}")
    train_df.to_parquet(train_path)

    print(f"Saving val set to: {val_path}")
    val_df.to_parquet(val_path)

    print("\n✓ Dataset preparation complete!")
    print(f"Train: {train_path}")
    print(f"Val: {val_path}")

if __name__ == "__main__":
    main()
