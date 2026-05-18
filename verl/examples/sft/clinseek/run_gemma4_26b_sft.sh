#!/usr/bin/env bash
set -euo pipefail

# Gemma-4-26B-A4B-it SFT with FSDP on ClinSeek trajectory data.
#
# Usage:
#   TRAIN_FILES=/path/train.parquet VAL_FILES=/path/val.parquet \
#   MODEL_PATH=/path/gemma-4-26B-A4B-it \
#   bash verl/examples/sft/clinseek/run_gemma4_26b_sft.sh 8 checkpoints/gemma4

if [[ "$#" -lt 2 ]]; then
    echo "Usage: run_gemma4_26b_sft.sh <nproc_per_node> <save_path> [hydra_overrides...]"
    exit 1
fi

nproc_per_node="$1"
save_path="$2"
shift 2

export PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF:-expandable_segments:True}"

TRAIN_FILES="${TRAIN_FILES:-${HOME}/data/clinseek_trajectory/train.parquet}"
VAL_FILES="${VAL_FILES:-${HOME}/data/clinseek_trajectory/val.parquet}"
MODEL_PATH="${MODEL_PATH:-google/gemma-4-26b-it}"
PROJECT_NAME="${PROJECT_NAME:-clinseek-sft}"
EXP_NAME="${EXP_NAME:-clinseek-gemma4-26b-a4b-it}"
LOGGER="${LOGGER:-console}"

torchrun --standalone --nnodes=1 --nproc_per_node="${nproc_per_node}" \
    -m verl.trainer.sft_trainer \
    data.train_files="${TRAIN_FILES}" \
    data.val_files="${VAL_FILES}" \
    data.train_batch_size=8 \
    data.micro_batch_size_per_gpu=1 \
    data.max_length=60000 \
    data.truncation=left \
    data.pad_mode=no_padding \
    data.use_dynamic_bsz=False \
    data.messages_key=messages \
    data.ignore_input_ids_mismatch=True \
    data.train_max_samples=-1 \
    data.val_max_samples=50 \
    model.path="${MODEL_PATH}" \
    model.trust_remote_code=False \
    model.use_remove_padding=False \
    model.enable_gradient_checkpointing=True \
    model.lora_rank=32 \
    model.lora_alpha=64 \
    'model.target_modules="^.*language_model\..*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$"' \
    +model.override_config.attn_implementation=flash_attention_2 \
    engine.use_torch_compile=False \
    engine.strategy=fsdp \
    engine.param_offload=False \
    engine.optimizer_offload=False \
    optim.lr=1e-4 \
    optim.lr_scheduler_type=cosine \
    optim.lr_warmup_steps_ratio=0.05 \
    optim.weight_decay=0.01 \
    optim.clip_grad=1.0 \
    trainer.default_local_dir="${save_path}" \
    trainer.project_name="${PROJECT_NAME}" \
    trainer.experiment_name="${EXP_NAME}" \
    trainer.total_epochs=3 \
    trainer.save_freq=70 \
    trainer.test_freq=25 \
    trainer.logger="${LOGGER}" \
    trainer.seed=42 \
    trainer.nnodes=1 \
    "$@"
