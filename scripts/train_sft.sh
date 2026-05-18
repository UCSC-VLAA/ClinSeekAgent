#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VERL_ROOT="${VERL_ROOT:-${REPO_ROOT}/verl}"
TRAIN_FILES="${TRAIN_FILES:?Set TRAIN_FILES to train.parquet}"
VAL_FILES="${VAL_FILES:?Set VAL_FILES to val.parquet}"
MODEL_PATH="${MODEL_PATH:?Set MODEL_PATH to the base model path}"

NUM_GPUS="${NUM_GPUS:-8}"
MASTER_ADDR="${MASTER_ADDR:-localhost}"
MASTER_PORT="${MASTER_PORT:-29500}"
NNODES="${NNODES:-1}"
NODE_RANK="${NODE_RANK:-0}"

TP_SIZE="${TP_SIZE:-2}"
PP_SIZE="${PP_SIZE:-1}"
VPP_SIZE="${VPP_SIZE:-null}"
CP_SIZE="${CP_SIZE:-1}"
EP_SIZE="${EP_SIZE:-8}"
ETP_SIZE="${ETP_SIZE:-1}"

TRAIN_BATCH_SIZE="${TRAIN_BATCH_SIZE:-32}"
MICRO_BATCH_SIZE="${MICRO_BATCH_SIZE:-1}"
MAX_LENGTH="${MAX_LENGTH:-52000}"
LR="${LR:-2e-5}"
MIN_LR="${MIN_LR:-2e-6}"
DTYPE="${DTYPE:-bfloat16}"
TOTAL_EPOCHS="${TOTAL_EPOCHS:-3}"
PROJECT_NAME="${PROJECT_NAME:-clinseek-sft}"
EXP_NAME="${EXP_NAME:-clinseek-35b-a3b-sft}"
CKPT_DIR="${CKPT_DIR:-checkpoints/${PROJECT_NAME}/${EXP_NAME}}"
RESUME_MODE="${RESUME_MODE:-disable}"

export PYTHONPATH="${VERL_ROOT}:${PYTHONPATH:-}"
export CUDA_DEVICE_MAX_CONNECTIONS="${CUDA_DEVICE_MAX_CONNECTIONS:-1}"
export PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF:-expandable_segments:True,max_split_size_mb:128}"
export TORCH_COMPILE_THREADS="${TORCH_COMPILE_THREADS:-8}"

mkdir -p "${CKPT_DIR}"

ENGINE_CONFIG=(
  engine=megatron
  optim=megatron
  optim.lr="${LR}"
  optim.min_lr="${MIN_LR}"
  optim.lr_warmup_steps=10
  optim.weight_decay=0.1
  "optim.betas=[0.9,0.95]"
  optim.clip_grad=1.0
  optim.lr_decay_style=cosine
  +optim.override_optimizer_config.optimizer_offload_fraction=1
  +optim.override_optimizer_config.optimizer_cpu_offload=True
  engine.tensor_model_parallel_size="${TP_SIZE}"
  engine.pipeline_model_parallel_size="${PP_SIZE}"
  engine.virtual_pipeline_model_parallel_size="${VPP_SIZE}"
  engine.context_parallel_size="${CP_SIZE}"
  engine.expert_model_parallel_size="${EP_SIZE}"
  engine.expert_tensor_parallel_size="${ETP_SIZE}"
  engine.param_offload=True
  engine.grad_offload=True
  engine.optimizer_offload=True
  engine.use_mbridge=True
  engine.vanilla_mbridge=True
  engine.dtype="${DTYPE}"
  engine.use_remove_padding=False
  engine.override_transformer_config.attention_backend=auto
  +engine.override_transformer_config.recompute_method=uniform
  +engine.override_transformer_config.recompute_granularity=full
  +engine.override_transformer_config.recompute_num_layers=4
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
  trainer.save_freq=70 \
  trainer.logger='["console","wandb"]' \
  trainer.project_name="${PROJECT_NAME}" \
  trainer.experiment_name="${EXP_NAME}" \
  trainer.total_epochs="${TOTAL_EPOCHS}" \
  trainer.default_local_dir="${CKPT_DIR}" \
  trainer.resume_mode="${RESUME_MODE}" \
  trainer.seed=42 \
  trainer.nnodes="${NNODES}" \
  "$@"
