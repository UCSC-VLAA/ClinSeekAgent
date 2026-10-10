#!/usr/bin/env bash
set -euo pipefail

# Paper SFT recipe for ClinSeek-35B-A3B (Qwen3.5-35B-A3B, verl + Megatron +
# mbridge, 2x H200, TP=2 EP=2 PP=1 CP=1, global batch 32, 52K max length,
# 3 epochs, Adam (0.9, 0.999) with CPU optimizer offload, LR 2e-5 cosine to
# 2e-6, MoE aux loss 0.01 / z-loss 0.001, full recompute, seed 42).
#
# The recipe lives in the repository's scripts/train_sft.sh; this wrapper
# keeps the historical entry point. Expected data:
#   TRAIN_FILES=/path/to/train.parquet
#   VAL_FILES=/path/to/val.parquet
#   MODEL_PATH=/path/to/Qwen3.5-35B-A3B
# produced by prepare_clinseek_data.py (see docs/sft_training.md). Apply
# patches/verl_megatron_long_context.patch to the vendored verl tree first.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERL_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
REPO_ROOT="$(cd "${VERL_ROOT}/.." && pwd)"

export VERL_ROOT
exec bash "${REPO_ROOT}/scripts/train_sft.sh" "$@"
