#!/usr/bin/env bash
# Qwen3.5-35B-A3B MoE SFT with Megatron backend + mbridge on the
# native-tool-call 52K DeepMed_trajectory dataset.
#
# Data:
#   /fsx-shared/juncheng/EHR/data/deepmed_trajectory_qwen35_52k/
#     - Qwen3.5 tokenizer, native <tool_call>/<tool_response> rendering
#     - max_token_length=52000 → 7204 train / 147 val (18.3% dropped)
#
# Hardware / parallelism (8× H200, 140 GB each, 1× 1.8 TB host):
#   TP=2  PP=1  CP=1  EP=8  ETP=1
#   bshd format (Qwen3.5's Gated Delta Net linear attn has no THD/packed support)
#
# Memory optimizations (carried over from run_qwen3_5_35b_a3b_sft_megatron.sh):
#   - full CPU offload: param + grad + optimizer
#   - TORCH_COMPILE_THREADS=8 caps forked-process RSS (~280 GB extra vs ~1120 GB)
#   - PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
#   - oom_guard.sh trips at 96% cgroup memory to prevent pod crash
#   - enhanced system monitor + per-step memory CSV for leak detection
#
# Usage:
#   bash run_qwen3_5_35b_a3b_sft_megatron_52k.sh [hydra overrides ...]
#
# Overridable via env:
#   NUM_GPUS, MASTER_ADDR, MASTER_PORT, NNODES, NODE_RANK
#   TRAIN_FILES, VAL_FILES, MODEL_PATH
#   TP_SIZE, PP_SIZE, EP_SIZE, ETP_SIZE, CP_SIZE, VPP_SIZE
#   TRAIN_BATCH_SIZE, MICRO_BATCH_SIZE, MAX_LENGTH
#   LR, MIN_LR, DTYPE, TOTAL_EPOCHS
#   RESUME_MODE, OOM_THRESHOLD, ckpts_home

set -xeuo pipefail

export CUDA_DEVICE_MAX_CONNECTIONS=1
export PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
export TORCH_COMPILE_THREADS=${TORCH_COMPILE_THREADS:-8}

# ============================================================
# Distributed
# ============================================================
NUM_GPUS=${NUM_GPUS:-8}
MASTER_ADDR=${MASTER_ADDR:-localhost}
MASTER_PORT=${MASTER_PORT:-29500}
NNODES=${NNODES:-1}
NODE_RANK=${NODE_RANK:-0}

# ============================================================
# Data — native-tool-call 52K dataset
# ============================================================
TRAIN_FILES=${TRAIN_FILES:-/fsx-shared/juncheng/EHR/data/deepmed_trajectory_qwen35_52k/train.parquet}
VAL_FILES=${VAL_FILES:-/fsx-shared/juncheng/EHR/data/deepmed_trajectory_qwen35_52k/val.parquet}

# ============================================================
# Model
# ============================================================
MODEL_PATH=${MODEL_PATH:-/fsx-shared/juncheng/EHR/models/Qwen3.5-35B-A3B}

# ============================================================
# Parallelism
# ============================================================
TP_SIZE=${TP_SIZE:-2}
PP_SIZE=${PP_SIZE:-1}
VPP_SIZE=${VPP_SIZE:-null}
CP_SIZE=${CP_SIZE:-1}
EP_SIZE=${EP_SIZE:-8}
ETP_SIZE=${ETP_SIZE:-1}

# ============================================================
# Training
# ============================================================
TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-32}
MICRO_BATCH_SIZE=${MICRO_BATCH_SIZE:-1}
# 52K matches the data filter — no need to reserve extra headroom.
MAX_LENGTH=${MAX_LENGTH:-52000}
LR=${LR:-2e-5}
MIN_LR=${MIN_LR:-2e-6}
DTYPE=${DTYPE:-bfloat16}
TOTAL_EPOCHS=${TOTAL_EPOCHS:-3}

BACKEND=megatron
RESUME_MODE=${RESUME_MODE:-disable}

project_name=deepmed-sft
exp_name=deepmed-sft-qwen3_5-35b-a3b-megatron-52k-tp${TP_SIZE}-pp${PP_SIZE}-ep${EP_SIZE}
ckpts_home=${ckpts_home:-/fsx-shared/juncheng/EHR/verl/checkpoints/${project_name}/${exp_name}}
mkdir -p "${ckpts_home}"

# ============================================================
# Engine config (Megatron + mbridge for Qwen3.5 MoE)
# ============================================================
ENGINE_CONFIG="\
    engine=${BACKEND} \
    optim=${BACKEND} \
    optim.lr=${LR} \
    optim.min_lr=${MIN_LR} \
    optim.lr_warmup_steps=10 \
    optim.weight_decay=0.1 \
    optim.betas='[0.9,0.95]' \
    optim.clip_grad=1.0 \
    optim.lr_warmup_init=0 \
    optim.lr_decay_style=cosine \
    +optim.override_optimizer_config.optimizer_offload_fraction=1 \
    +optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d=True \
    +optim.override_optimizer_config.use_precision_aware_optimizer=True \
    +optim.override_optimizer_config.optimizer_cpu_offload=True \
    engine.tensor_model_parallel_size=${TP_SIZE} \
    engine.pipeline_model_parallel_size=${PP_SIZE} \
    engine.virtual_pipeline_model_parallel_size=${VPP_SIZE} \
    engine.context_parallel_size=${CP_SIZE} \
    engine.expert_model_parallel_size=${EP_SIZE} \
    engine.expert_tensor_parallel_size=${ETP_SIZE} \
    engine.param_offload=True \
    engine.grad_offload=True \
    engine.optimizer_offload=True \
    engine.use_mbridge=True \
    engine.vanilla_mbridge=True \
    engine.use_dist_checkpointing=False \
    engine.dtype=${DTYPE} \
    engine.use_remove_padding=False \
    engine.override_transformer_config.attention_backend=auto \
    +engine.override_transformer_config.recompute_method=uniform \
    +engine.override_transformer_config.recompute_granularity=full \
    +engine.override_transformer_config.recompute_num_layers=4 \
    +engine.override_transformer_config.moe_aux_loss_coeff=0.01 \
    +engine.override_transformer_config.moe_z_loss_coeff=0.001 \
    +engine.override_transformer_config.moe_permute_fusion=False"

# ============================================================
# System monitors (background, persist to fsx-shared)
# ============================================================
STAMP=$(date -u '+%Y%m%d-%H%M%S')
MONITOR_LOG=${MONITOR_LOG:-/fsx-shared/juncheng/EHR/training_system_monitor_52k.log}
TRAINING_LOG=${TRAINING_LOG:-/fsx-shared/juncheng/EHR/training_52k_${STAMP}.log}
MEMORY_CSV=${MEMORY_CSV:-/fsx-shared/juncheng/EHR/memory_per_step_52k.csv}
echo "training log: ${TRAINING_LOG}"
echo "system monitor log: ${MONITOR_LOG}"
echo "per-step memory csv: ${MEMORY_CSV}"

OOM_THRESHOLD=${OOM_THRESHOLD:-96}
bash /fsx-shared/juncheng/EHR/oom_guard.sh "${OOM_THRESHOLD}" 5 &
OOMGUARD_PID=$!

bash /fsx-shared/juncheng/EHR/monitor_training_enhanced.sh 10 "${TRAINING_LOG}" "${MONITOR_LOG}" &
MONITOR_PID=$!

bash /fsx-shared/juncheng/EHR/log_memory_per_step.sh "${TRAINING_LOG}" "${MEMORY_CSV}" &
MEMLOG_PID=$!

trap "kill ${OOMGUARD_PID} ${MONITOR_PID} ${MEMLOG_PID} 2>/dev/null" EXIT

# ============================================================
# Launch
# ============================================================
torchrun \
    --nproc_per_node=${NUM_GPUS} \
    --nnodes=${NNODES} \
    --node_rank=${NODE_RANK} \
    --master_addr=${MASTER_ADDR} \
    --master_port=${MASTER_PORT} \
    -m verl.trainer.sft_trainer \
    data.train_files="${TRAIN_FILES}" \
    data.val_files="${VAL_FILES}" \
    data.train_batch_size=${TRAIN_BATCH_SIZE} \
    data.micro_batch_size_per_gpu=${MICRO_BATCH_SIZE} \
    data.max_length=${MAX_LENGTH} \
    data.pad_mode=no_padding \
    data.truncation=left \
    data.use_dynamic_bsz=False \
    data.max_token_len_per_gpu=${MAX_LENGTH} \
    data.messages_key=messages \
    data.ignore_input_ids_mismatch=True \
    data.val_max_samples=50 \
    model.path=${MODEL_PATH} \
    model.use_remove_padding=False \
    model.trust_remote_code=True \
    ${ENGINE_CONFIG} \
    checkpoint.save_contents='["model","extra"]' \
    trainer.test_freq=25 \
    trainer.save_freq=70 \
    trainer.logger="['console','wandb']" \
    trainer.project_name="${project_name}" \
    trainer.experiment_name="${exp_name}" \
    trainer.total_epochs=${TOTAL_EPOCHS} \
    trainer.default_local_dir="${ckpts_home}" \
    trainer.resume_mode=${RESUME_MODE} \
    trainer.seed=42 \
    trainer.nnodes=${NNODES} \
    "$@" 2>&1 | tee -a "${TRAINING_LOG}"
