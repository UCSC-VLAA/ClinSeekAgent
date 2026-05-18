#!/usr/bin/env bash
set -euo pipefail

# Default Qwen3.5-27B SFT recipe for ClinSeek trajectory data.
# Usage:
#   bash run_qwen3_5_27b_sft_default.sh <nproc_per_node> <save_path> [hydra_overrides...]

if [[ "$#" -lt 2 ]]; then
    echo "Usage: run_qwen3_5_27b_sft_default.sh <nproc_per_node> <save_path> [hydra_overrides...]"
    exit 1
fi

nproc_per_node="$1"
save_path="$2"
shift 2

TRAIN_FILES="${TRAIN_FILES:-${HOME}/data/clinseek_trajectory/train.parquet}"
VAL_FILES="${VAL_FILES:-${HOME}/data/clinseek_trajectory/val.parquet}"
MODEL_PATH="${MODEL_PATH:-Qwen/Qwen3.5-27B}"
PROJECT_NAME="${PROJECT_NAME:-clinseek-sft}"
EXP_NAME="${EXP_NAME:-clinseek-qwen3.5-27b-default}"
LOGGER="${LOGGER:-console}"

torchrun --standalone --nnodes=1 --nproc_per_node="${nproc_per_node}" \
    -m verl.trainer.sft_trainer \
    data.train_files="${TRAIN_FILES}" \
    data.val_files="${VAL_FILES}" \
    data.train_batch_size=32 \
    data.micro_batch_size_per_gpu=1 \
    data.max_length=32768 \
    data.truncation=left \
    data.use_dynamic_bsz=False \
    data.messages_key=messages \
    data.ignore_input_ids_mismatch=True \
    data.train_max_samples=-1 \
    data.val_max_samples=50 \
    model.path="${MODEL_PATH}" \
    model.trust_remote_code=True \
    model.use_remove_padding=False \
    engine.use_torch_compile=False \
    optim.lr=2e-5 \
    optim.lr_scheduler_type=cosine \
    optim.lr_warmup_steps_ratio=0.05 \
    trainer.default_local_dir="${save_path}" \
    trainer.project_name="${PROJECT_NAME}" \
    trainer.experiment_name="${EXP_NAME}" \
    trainer.total_epochs=3 \
    trainer.save_freq=50 \
    trainer.test_freq=25 \
    trainer.logger="${LOGGER}" \
    trainer.seed=42 \
    trainer.nnodes=1 \
    "$@"
