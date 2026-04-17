#!/usr/bin/env bash
# Gemma-4-26B-A4B-it SFT with FSDP on DeepMed_trajectory dataset
#
# Requirements:
#   - 8 GPUs (H200 141GB recommended)
#   - gemma4 venv activated (transformers>=5.5.4)
#
# Gemma-4-26B-A4B-it architecture:
#   MoE: 128 experts, 8 active per token (~4B active params)
#   Attention: hybrid sliding-window (1024) + global, pattern 5:1
#   Position IDs: standard 1D (not MRoPE), uses proportional RoPE on global layers
#   Config: nested Gemma4Config with text_config, vision_config, audio_config
#   For text-only SFT, vision/audio towers are unused (inputs=None skips them)
#
# Usage:
#   bash run_gemma4_26b_sft.sh <nproc_per_node> <save_path> [other_configs...]
#
# Example:
#   bash run_gemma4_26b_sft.sh 8 /fsx-shared/juncheng/EHR/verl/checkpoints/deepmed-sft/gemma4-26b

set -xeuo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: run_gemma4_26b_sft.sh <nproc_per_node> <save_path> [other_configs...]"
    exit 1
fi

nproc_per_node=$1
save_path=$2
shift 2

# Reduce CUDA memory fragmentation for MoE models with variable expert activation
export PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128

TRAIN_FILES=${TRAIN_FILES:-/fsx-shared/juncheng/EHR/data/deepmed_trajectory/train.parquet}
VAL_FILES=${VAL_FILES:-/fsx-shared/juncheng/EHR/data/deepmed_trajectory/val.parquet}
MODEL_PATH=${MODEL_PATH:-/fsx-shared/juncheng/EHR/models/gemma-4-26B-A4B-it}

torchrun --standalone --nnodes=1 --nproc_per_node=$nproc_per_node \
    -m verl.trainer.sft_trainer \
    data.train_files="${TRAIN_FILES}" \
    data.val_files="${VAL_FILES}" \
    data.train_batch_size=32 \
    data.micro_batch_size_per_gpu=1 \
    data.max_length=8192 \
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
    +model.override_config.attn_implementation=sdpa \
    engine.use_torch_compile=False \
    optim.lr=2e-5 \
    optim.lr_scheduler_type=cosine \
    optim.lr_warmup_steps_ratio=0.05 \
    optim.weight_decay=0.01 \
    optim.clip_grad=1.0 \
    trainer.default_local_dir="${save_path}" \
    trainer.project_name=deepmed-sft \
    trainer.experiment_name=deepmed-sft-gemma4-26b-a4b-it \
    trainer.total_epochs=3 \
    trainer.save_freq=70 \
    trainer.test_freq=25 \
    trainer.logger=wandb \
    trainer.seed=42 \
    trainer.nnodes=1 \
    "$@"
