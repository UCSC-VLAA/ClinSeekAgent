#!/usr/bin/env bash
# SFT of Qwen3.5-35B-A3B into ClinSeek-35B-A3B (paper recipe, Appendix "Training
# Settings"): full-parameter SFT with verl + Megatron + mbridge on 2x H200,
# TP=2 EP=2 PP=1 CP=1, global batch 32 / micro batch 1, 52K max length,
# 3 epochs (the paper reports the checkpoint after epoch 2), Adam (0.9, 0.999)
# with optimizer states offloaded to CPU, LR 2e-5 cosine to 2e-6 with 10 warmup
# steps, weight decay 0.1, gradient clipping 1.0, MoE aux loss 0.01 and z-loss
# 0.001, bf16, full activation recomputation, seed 42.
#
#   TRAIN_FILES=data/clinseek_sft/train.parquet VAL_FILES=data/clinseek_sft/val.parquet \
#   MODEL_PATH=/path/to/Qwen3.5-35B-A3B bash scripts/train_sft.sh
#
# The parquet files come from verl/examples/sft/clinseek/prepare_clinseek_data.py
# (see docs/sft_training.md). Fitting 52K-token sequences on 2 GPUs needs the
# long-context patch in verl/examples/sft/clinseek/patches/ (git apply it once).
# Host RAM: the CPU-resident Adam states of a 35B model need roughly 1 TB for 2
# ranks. Extra hydra overrides pass through ("$@").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VERL_ROOT="${VERL_ROOT:-${REPO_ROOT}/verl}"
TRAIN_FILES="${TRAIN_FILES:?Set TRAIN_FILES to train.parquet}"
VAL_FILES="${VAL_FILES:?Set VAL_FILES to val.parquet}"
MODEL_PATH="${MODEL_PATH:?Set MODEL_PATH to the base model path}"

NUM_GPUS="${NUM_GPUS:-2}"
MASTER_ADDR="${MASTER_ADDR:-localhost}"
MASTER_PORT="${MASTER_PORT:-29500}"
NNODES="${NNODES:-1}"
NODE_RANK="${NODE_RANK:-0}"

TP_SIZE="${TP_SIZE:-2}"
PP_SIZE="${PP_SIZE:-1}"
VPP_SIZE="${VPP_SIZE:-null}"
CP_SIZE="${CP_SIZE:-1}"
EP_SIZE="${EP_SIZE:-2}"
ETP_SIZE="${ETP_SIZE:-1}"

TRAIN_BATCH_SIZE="${TRAIN_BATCH_SIZE:-32}"
MICRO_BATCH_SIZE="${MICRO_BATCH_SIZE:-1}"
MAX_LENGTH="${MAX_LENGTH:-52000}"
LR="${LR:-2e-5}"
MIN_LR="${MIN_LR:-2e-6}"
DTYPE="${DTYPE:-bfloat16}"
TOTAL_EPOCHS="${TOTAL_EPOCHS:-3}"
# Per-layer full recompute keeps 52K-token sequences within 2x H200 memory.
RECOMPUTE_NUM_LAYERS="${RECOMPUTE_NUM_LAYERS:-1}"
PROJECT_NAME="${PROJECT_NAME:-clinseek-sft}"
EXP_NAME="${EXP_NAME:-clinseek-35b-a3b-sft}"
CKPT_DIR="${CKPT_DIR:-checkpoints/${PROJECT_NAME}/${EXP_NAME}}"
RESUME_MODE="${RESUME_MODE:-disable}"
LOGGER="${LOGGER:-[\"console\"]}"   # e.g. LOGGER='["console","wandb"]'

export PYTHONPATH="${VERL_ROOT}:${PYTHONPATH:-}"
export CUDA_DEVICE_MAX_CONNECTIONS="${CUDA_DEVICE_MAX_CONNECTIONS:-1}"
# max_split_size_mb fragments the cache at 52K tokens; GC-threshold reclaim
# keeps room for Triton / NCCL allocations instead.
export PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF:-expandable_segments:True,garbage_collection_threshold:0.8}"
export TORCH_COMPILE_THREADS="${TORCH_COMPILE_THREADS:-8}"
# torchrun defaults to one OpenMP thread per rank, which makes the CPU Adam
# step several times slower.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-14}"
# Memory-saving switches read by verl/examples/sft/clinseek/patches/
# verl_megatron_long_context.patch (no effect on an unpatched verl tree): keep
# the 248K-vocab logits in bf16, chunk the vocab-parallel cross entropy over
# the sequence under activation checkpointing, empty the CUDA cache before
# every micro-batch, and cap the per-process CUDA memory fraction.
export VERL_MEGATRON_KEEP_BF16_LOGITS="${VERL_MEGATRON_KEEP_BF16_LOGITS:-1}"
export VERL_LOGPROB_CHUNK_SIZE="${VERL_LOGPROB_CHUNK_SIZE:-4096}"
export VERL_EMPTY_CACHE_PER_MICROBATCH="${VERL_EMPTY_CACHE_PER_MICROBATCH:-1}"
export VERL_CUDA_MEMORY_FRACTION="${VERL_CUDA_MEMORY_FRACTION:-0.93}"

mkdir -p "${CKPT_DIR}"

ENGINE_CONFIG=(
  engine=megatron
  optim=megatron
  optim.lr="${LR}"
  optim.min_lr="${MIN_LR}"
  optim.lr_warmup_steps=10
  optim.weight_decay=0.1
  # Adam betas of the paper run (Megatron's defaults, 0.9 / 0.999).
  "optim.betas=[0.9,0.999]"
  optim.clip_grad=1.0
  optim.lr_decay_style=cosine
  # Optimizer states (fp32) live on the CPU; gradients and the main-grad
  # buffer stay in bf16 on the GPU.
  +optim.override_optimizer_config.optimizer_offload_fraction=1
  +optim.override_optimizer_config.optimizer_cpu_offload=True
  +optim.override_optimizer_config.use_precision_aware_optimizer=True
  +optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d=True
  +optim.override_optimizer_config.main_grads_dtype=bf16
  +engine.override_ddp_config.grad_reduce_in_fp32=False
  engine.tensor_model_parallel_size="${TP_SIZE}"
  engine.pipeline_model_parallel_size="${PP_SIZE}"
  engine.virtual_pipeline_model_parallel_size="${VPP_SIZE}"
  engine.context_parallel_size="${CP_SIZE}"
  engine.expert_model_parallel_size="${EP_SIZE}"
  engine.expert_tensor_parallel_size="${ETP_SIZE}"
  # verl's own param/grad/optimizer offload is redundant with Megatron's CPU
  # optimizer offload for SFT and costs >100 GB extra host RAM.
  engine.param_offload=False
  engine.grad_offload=False
  engine.optimizer_offload=False
  engine.use_mbridge=True
  engine.vanilla_mbridge=True
  engine.dtype="${DTYPE}"
  engine.use_remove_padding=False
  engine.override_transformer_config.attention_backend=auto
  +engine.override_transformer_config.recompute_method=uniform
  +engine.override_transformer_config.recompute_granularity=full
  +engine.override_transformer_config.recompute_num_layers="${RECOMPUTE_NUM_LAYERS}"
  +engine.override_transformer_config.moe_aux_loss_coeff=0.01
  +engine.override_transformer_config.moe_z_loss_coeff=0.001
  +engine.override_transformer_config.moe_permute_fusion=False
)

exec torchrun \
  --nproc_per_node="${NUM_GPUS}" \
  --nnodes="${NNODES}" \
  --node_rank="${NODE_RANK}" \
  --master_addr="${MASTER_ADDR}" \
  --master_port="${MASTER_PORT}" \
  -m verl.trainer.sft_trainer \
  data.train_files="${TRAIN_FILES}" \
  data.val_files="${VAL_FILES}" \
  data.train_batch_size="${TRAIN_BATCH_SIZE}" \
  data.micro_batch_size_per_gpu="${MICRO_BATCH_SIZE}" \
  data.max_length="${MAX_LENGTH}" \
  data.pad_mode=no_padding \
  data.truncation=left \
  data.use_dynamic_bsz=False \
  data.max_token_len_per_gpu="${MAX_LENGTH}" \
  data.messages_key=messages \
  data.ignore_input_ids_mismatch=True \
  data.val_max_samples=50 \
  model.path="${MODEL_PATH}" \
  model.use_remove_padding=False \
  model.trust_remote_code=True \
  "${ENGINE_CONFIG[@]}" \
  checkpoint.save_contents='["model","extra"]' \
  trainer.test_freq=25 \
  trainer.save_freq=after_each_epoch \
  trainer.logger="${LOGGER}" \
  trainer.project_name="${PROJECT_NAME}" \
  trainer.experiment_name="${EXP_NAME}" \
  trainer.total_epochs="${TOTAL_EPOCHS}" \
  trainer.default_local_dir="${CKPT_DIR}" \
  trainer.resume_mode="${RESUME_MODE}" \
  trainer.seed=42 \
  trainer.nnodes="${NNODES}" \
  "$@"
