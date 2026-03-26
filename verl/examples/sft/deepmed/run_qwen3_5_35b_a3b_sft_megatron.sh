#!/usr/bin/env bash
# Qwen3.5-35B-A3B MoE SFT with Megatron backend + mbridge on DeepMed_trajectory
#
# Requirements:
#   - 8 GPUs (140GB each, e.g. 1x8 H200)
#   - megatron-core==0.16.0, flash-linear-attention, mbridge
#
# Qwen3.5 architecture notes:
#   Uses Gated Delta Net (GDN) linear attention - no THD/packed sequence support.
#   Must use bshd format: use_remove_padding=False, use_dynamic_bsz=False
#
# Parallelism config (8 GPUs / 1 node):
#   TP=2 PP=1 CP=1 EP=8 ETP=1
#   (256 experts / EP=8 = 32 experts per GPU)
#
# Memory notes (Bug 15 fix, updated for 1.8TB pod):
#   Pod cgroup limit = 1.8TB. Full CPU offloading (param+grad+optimizer) with
#   torch.compile forked processes caused host OOM (~1.6TB total RSS) on 1TB pod.
#   With 1.8TB: keep full offloading, but limit TORCH_COMPILE_THREADS=4
#   to cap forked process memory (~280GB extra instead of ~1120GB with 32 threads).
#   Estimated peak: ~448GB (workers) + 30GB (shm) + 420GB (offload) + 280GB (compile) = ~1178GB
#   Headroom: ~622GB for safety margin.

set -xeuo pipefail

export CUDA_DEVICE_MAX_CONNECTIONS=1
# max_split_size_mb reduces CUDA memory fragmentation (GPU2 had 35GB reserved-but-unusable)
export PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
# Limit torch.compile threads to avoid forking too many processes (host OOM fix)
# Default is ~32 threads which forks ~32 processes inheriting 35-56GB RSS each.
# With 8 threads on 1.8TB pod: ~280GB extra host memory (balanced speed/memory).
export TORCH_COMPILE_THREADS=8

# ============================================================
# Distributed
# ============================================================
NUM_GPUS=${NUM_GPUS:-8}
MASTER_ADDR=${MASTER_ADDR:-localhost}
MASTER_PORT=${MASTER_PORT:-29500}
NNODES=${NNODES:-1}
NODE_RANK=${NODE_RANK:-0}

# ============================================================
# Data
# ============================================================
TRAIN_FILES=${TRAIN_FILES:-/fsx-shared/juncheng/EHR/data/deepmed_trajectory/train.parquet}
VAL_FILES=${VAL_FILES:-/fsx-shared/juncheng/EHR/data/deepmed_trajectory/val.parquet}

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
MAX_LENGTH=${MAX_LENGTH:-57344}
LR=${LR:-2e-5}
MIN_LR=${MIN_LR:-2e-6}
DTYPE=${DTYPE:-bfloat16}
TOTAL_EPOCHS=${TOTAL_EPOCHS:-3}

BACKEND=megatron
RESUME_MODE=${RESUME_MODE:-disable}

project_name=deepmed-sft
exp_name=deepmed-sft-qwen3_5-35b-a3b-megatron-tp${TP_SIZE}-pp${PP_SIZE}-ep${EP_SIZE}
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
# System monitor (background, logs to fsx-shared for persistence)
# Enhanced version with checkpoint save detection + per-step memory logger
# ============================================================
MONITOR_LOG="/fsx-shared/juncheng/EHR/training_system_monitor.log"
TRAINING_LOG="/fsx-shared/juncheng/EHR/training_v3.log"
MEMORY_CSV="/fsx-shared/juncheng/EHR/memory_per_step.csv"

# OOM guard: kills training at 96% cgroup memory to prevent pod crash
OOM_THRESHOLD=${OOM_THRESHOLD:-96}
bash /fsx-shared/juncheng/EHR/oom_guard.sh "${OOM_THRESHOLD}" 5 &
OOMGUARD_PID=$!

# Enhanced system monitor (adaptive interval based on training phase)
bash /fsx-shared/juncheng/EHR/monitor_training_enhanced.sh 10 "${TRAINING_LOG}" "${MONITOR_LOG}" &
MONITOR_PID=$!

# Per-step memory logger (for memory leak detection)
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
    "$@"
