#!/bin/bash
# Convert the SFT Megatron distributed checkpoint to HF safetensors so FSDP+vLLM
# can load it during GRPO. One-time ~80 GB write.
#
# Defaults point at the canonical Qwen3.5-35B-A3B SFT run (global_step_675) and
# write to /fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf.
# Override via env: CKPT_DIR, OUT_DIR, MODEL_PATH, GPU_ID.

set -euo pipefail

PROJ="/fsx-shared/juncheng/EHR"
VENV="${VENV:-$PROJ/venvs/qwen3_5_sft}"
PYTHON="${PYTHON:-$VENV/bin/python}"
GPU_ID="${GPU_ID:-0}"

CKPT_DIR="${CKPT_DIR:-$PROJ/verl/checkpoints/deepmed-sft/deepmed-sft-qwen3_5-35b-a3b-megatron-52k-tp2-pp1-ep8/global_step_675}"
OUT_DIR="${OUT_DIR:-$PROJ/checkpoints/qwen3_5_35b_a3b_sft_hf}"
MODEL_PATH="${MODEL_PATH:-$PROJ/models/Qwen3.5-35B-A3B}"

if [ ! -d "$CKPT_DIR" ]; then
    echo "ERROR: checkpoint dir not found: $CKPT_DIR"
    exit 1
fi
if [ ! -d "$MODEL_PATH" ]; then
    echo "ERROR: original model dir not found (for tokenizer/config): $MODEL_PATH"
    exit 1
fi

echo "Converting:"
echo "  ckpt:   $CKPT_DIR"
echo "  out:    $OUT_DIR"
echo "  tokenizer source: $MODEL_PATH"
echo "  GPU:    $GPU_ID"
mkdir -p "$(dirname "$OUT_DIR")"

CUDA_VISIBLE_DEVICES=$GPU_ID "$PYTHON" "$PROJ/convert_dist_ckpt_to_hf.py" \
    --ckpt_dir "$CKPT_DIR" \
    --output_dir "$OUT_DIR" \
    --model_path "$MODEL_PATH"

echo ""
echo "Post-conversion sanity check:"
"$PYTHON" - <<PY
from transformers import AutoConfig
cfg = AutoConfig.from_pretrained("$OUT_DIR", trust_remote_code=True)
print("architectures:", getattr(cfg, 'architectures', None))
print("model_type:   ", getattr(cfg, 'model_type', None))
print("num_layers:   ", getattr(cfg, 'num_hidden_layers', None))
print("vocab_size:   ", getattr(cfg, 'vocab_size', None))
PY

echo "Done: $OUT_DIR"
