#!/bin/bash
# Multi-turn SFT training for Qwen3.5-27B on DeepMed_trajectory dataset
# Usage: run_qwen3_5_27b_sft.sh <nproc_per_node> <save_path> [other_configs...]

set -x

if [ "$#" -lt 2 ]; then
    echo "Usage: run_qwen3_5_27b_sft.sh <nproc_per_node> <save_path> [other_configs...]"
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
    data.train_batch_size=128 \
    data.max_length=4096 \
    data.truncation=left \
    data.use_dynamic_bsz=True \
    data.max_token_len_per_gpu=8192 \
    data.messages_key=messages \
    data.ignore_input_ids_mismatch=True \
    data.train_max_samples=-1 \
    data.val_max_samples=50 \
    model=hf_model \
    model.path=Qwen/Qwen3.5-27B \
    model.trust_remote_code=True \
    model.use_remove_padding=True \
    engine=automodel \
    engine.distributed_strategy=fsdp2 \
    engine.tp_size=1 \
    engine.pp_size=1 \
    engine.cp_size=1 \
    engine.ep_size=1 \
    engine.backend_config.attn=sdpa \
    engine.backend_config.enable_fsdp_optimizations=True \
    engine.activation_checkpointing=True \
    engine.model_dtype=bf16 \
    engine.attn_implementation=sdpa \
    optim=automodel \
    optim.optimizer=AdamW \
    optim.lr=2e-5 \
    optim.lr_warmup_steps_ratio=0.05 \
    optim.weight_decay=0.01 \
    optim.betas='[0.9,0.95]' \
    optim.clip_grad=1.0 \
    optim.lr_scheduler_type=cosine \
    trainer.default_local_dir=$save_path \
    trainer.project_name=deepmed-sft \
    trainer.experiment_name=deepmed-sft-qwen3.5-27b \
    trainer.total_epochs=3 \
    trainer.save_freq=500 \
    trainer.test_freq=100 \
    trainer.logger=wandb \
    trainer.seed=42 \
    trainer.nnodes=1 $@
