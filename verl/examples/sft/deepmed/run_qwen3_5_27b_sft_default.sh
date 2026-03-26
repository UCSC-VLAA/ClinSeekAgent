#!/bin/bash
# Multi-turn SFT training for Qwen3.5-27B on DeepMed_trajectory dataset
# Usage: run_qwen3_5_27b_sft_default.sh <nproc_per_node> <save_path> [other_configs...]

set -x

if [ "$#" -lt 2 ]; then
    echo "Usage: run_qwen3_5_27b_sft_default.sh <nproc_per_node> <save_path> [other_configs...]"
    exit 1
fi

nproc_per_node=$1
save_path=$2

# Shift arguments so $@ refers to the rest
shift 2

torchrun --standalone --nnodes=1 --nproc_per_node=$nproc_per_node \
     -m verl.trainer.sft_trainer \
    data.train_files=$HOME/data/deepmed_trajectory/train.parquet \
    data.val_files=$HOME/data/deepmed_trajectory/val.parquet \
    data.train_batch_size=32 \
    data.micro_batch_size_per_gpu=1 \
    data.max_length=32768 \
    data.truncation=left \
    data.use_dynamic_bsz=False \
    data.messages_key=messages \
    data.ignore_input_ids_mismatch=True \
    data.train_max_samples=-1 \
    data.val_max_samples=50 \
    model.path=Qwen/Qwen3.5-27B \
    model.trust_remote_code=True \
    model.use_remove_padding=False \
    engine.use_torch_compile=False \
    optim.lr=2e-5 \
    optim.lr_scheduler_type=cosine \
    optim.lr_warmup_steps_ratio=0.05 \
    trainer.default_local_dir=$save_path \
    trainer.project_name=deepmed-sft \
    trainer.experiment_name=deepmed-sft-qwen3.5-27b-v2 \
    trainer.total_epochs=3 \
    trainer.save_freq=50 \
    trainer.test_freq=25 \
    trainer.logger=wandb \
    trainer.seed=42 \
    trainer.nnodes=1 $@
