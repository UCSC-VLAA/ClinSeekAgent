#!/usr/bin/env python3
"""Convert Megatron distributed checkpoint to HuggingFace safetensors format.

Handles Qwen3.5 MoE models whose top-level config is a VL config (Qwen3_5MoeForConditionalGeneration)
by extracting text_config and presenting it as Qwen3MoeForCausalLM to the merger.

Usage:
    python convert_dist_ckpt_to_hf.py \
        --ckpt_dir /path/to/global_step_N \
        --output_dir /path/to/hf_output \
        [--model_path /path/to/original/model]  # optional, for tokenizer
"""

import argparse
import json
import os
import shutil
import tempfile

from transformers import AutoConfig


def create_compatible_config(ckpt_dir: str, model_path: str = None) -> str:
    """Create a temp dir with config.json compatible with verl model_merger.

    For Qwen3.5 MoE VL models, extracts text_config and sets architecture
    to Qwen3MoeForCausalLM so the merger can handle it.

    Returns path to temp config dir.
    """
    hf_dir = os.path.join(ckpt_dir, "huggingface")
    config = AutoConfig.from_pretrained(hf_dir, trust_remote_code=True)

    # Check if this is a VL model with nested text_config
    if hasattr(config, "text_config") and not hasattr(config, "num_hidden_layers"):
        print(f"Detected VL config ({config.architectures}), extracting text_config...")
        text_config = config.text_config
        text_dict = text_config.to_dict()

        # Set architecture to Qwen3MoeForCausalLM (supported by merger)
        text_dict["architectures"] = ["Qwen3MoeForCausalLM"]
        text_dict["model_type"] = "qwen3_moe"

        # Qwen3.5's text_config nests rope state under `rope_parameters`
        # (transformers 5.x). verl's Megatron merger (Qwen3MoeConfig path)
        # expects the legacy flat `rope_theta` / `rope_scaling` attributes.
        # Promote them so the merger can construct the mcore model.
        rope_params = text_dict.pop("rope_parameters", None)
        if isinstance(rope_params, dict):
            if "rope_theta" not in text_dict and "rope_theta" in rope_params:
                text_dict["rope_theta"] = rope_params["rope_theta"]
            # partial_rotary_factor may also live inside rope_parameters.
            if (
                "partial_rotary_factor" not in text_dict
                and "partial_rotary_factor" in rope_params
            ):
                text_dict["partial_rotary_factor"] = rope_params["partial_rotary_factor"]
            # rope_scaling is the optional side channel for long-context
            # extensions (yarn/linear). Only forward it if present.
            if "rope_scaling" not in text_dict and rope_params.get("rope_scaling"):
                text_dict["rope_scaling"] = rope_params["rope_scaling"]
        # Final safety net — Qwen3Moe's flat default is 10,000 but Qwen3.5
        # uses 10,000,000; if we still don't have it, fail loudly.
        if "rope_theta" not in text_dict:
            raise KeyError(
                "text_config is missing rope_theta; cannot flatten from "
                "rope_parameters. Inspect the VL config manually."
            )

        # Create temp dir with modified config
        tmp_dir = tempfile.mkdtemp(prefix="hf_config_")
        with open(os.path.join(tmp_dir, "config.json"), "w") as f:
            json.dump(text_dict, f, indent=2)

        # Copy tokenizer files from checkpoint or original model
        source_dir = hf_dir
        if model_path and os.path.isdir(model_path):
            source_dir = model_path

        for fname in os.listdir(source_dir):
            if fname != "config.json":
                src = os.path.join(source_dir, fname)
                dst = os.path.join(tmp_dir, fname)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)

        print(f"Created compatible config in {tmp_dir}")
        return tmp_dir
    else:
        print("Config already compatible, using as-is")
        return hf_dir


def main():
    parser = argparse.ArgumentParser(description="Convert dist checkpoint to HF format")
    parser.add_argument("--ckpt_dir", required=True, help="Path to global_step_N directory")
    parser.add_argument("--output_dir", required=True, help="Output directory for HF model")
    parser.add_argument("--model_path", default=None, help="Original model path (for tokenizer)")
    args = parser.parse_args()

    # Step 1: Create compatible config
    tmp_config_dir = create_compatible_config(args.ckpt_dir, args.model_path)

    # Step 2: Swap the huggingface dir temporarily for the merger
    hf_dir = os.path.join(args.ckpt_dir, "huggingface")
    hf_backup = os.path.join(args.ckpt_dir, "huggingface_backup")

    try:
        os.rename(hf_dir, hf_backup)
        # Symlink or copy tmp config dir as huggingface/
        if tmp_config_dir != hf_dir:
            os.symlink(tmp_config_dir, hf_dir)

        # Step 3: Run the merger
        import subprocess
        import sys

        cmd = [
            sys.executable, "-m", "verl.model_merger", "merge",
            "--backend", "megatron",
            "--trust-remote-code",
            "--use_cpu_initialization",
            "--local_dir", args.ckpt_dir,
            "--target_dir", args.output_dir,
        ]
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, env={**os.environ, "CUDA_VISIBLE_DEVICES": "0"})

        if result.returncode != 0:
            print(f"Merger failed with return code {result.returncode}")
            return result.returncode

    finally:
        # Step 4: Restore original huggingface dir
        if os.path.islink(hf_dir):
            os.unlink(hf_dir)
        elif os.path.isdir(hf_dir) and hf_dir != hf_backup:
            shutil.rmtree(hf_dir)
        if os.path.exists(hf_backup):
            os.rename(hf_backup, hf_dir)

        # Cleanup temp dir
        if tmp_config_dir != hf_dir and os.path.exists(tmp_config_dir):
            shutil.rmtree(tmp_config_dir)

    # Step 5: Copy original config + tokenizer to output (preserve Qwen3.5 identity)
    print("Copying original config and tokenizer to output...")
    source = args.model_path if args.model_path else hf_dir
    for fname in os.listdir(source):
        src = os.path.join(source, fname)
        dst = os.path.join(args.output_dir, fname)
        if os.path.isfile(src) and not fname.endswith(".safetensors"):
            # Don't overwrite safetensors, but copy config/tokenizer
            if not os.path.exists(dst) or fname in ("config.json", "tokenizer.json", "tokenizer_config.json"):
                shutil.copy2(src, dst)
                print(f"  Copied {fname}")

    print(f"\nConversion complete! HF model saved to: {args.output_dir}")
    return 0


if __name__ == "__main__":
    exit(main())
