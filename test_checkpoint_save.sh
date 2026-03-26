#!/usr/bin/env bash
# Test checkpoint save memory behavior at early step
# This helps reproduce the OOM bug faster without waiting for step 50
#
# Usage: bash test_checkpoint_save.sh
#
# This script runs a SHORT training with checkpoint save at step 2
# to verify distributed checkpointing works and memory doesn't spike

set -xeuo pipefail

export CUDA_DEVICE_MAX_CONNECTIONS=1
export PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
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
# Training (MODIFIED FOR TESTING)
# ============================================================
TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-32}
MICRO_BATCH_SIZE=${MICRO_BATCH_SIZE:-1}
MAX_LENGTH=${MAX_LENGTH:-65536}
LR=${LR:-2e-5}
MIN_LR=${MIN_LR:-2e-6}
DTYPE=${DTYPE:-bfloat16}
TOTAL_EPOCHS=${TOTAL_EPOCHS:-1}  # Only 1 epoch for testing

BACKEND=megatron
RESUME_MODE=${RESUME_MODE:-disable}

project_name=deepmed-sft-test
exp_name=test-checkpoint-memory-tp${TP_SIZE}-pp${PP_SIZE}-ep${EP_SIZE}
ckpts_home=/fsx-shared/juncheng/EHR/verl/checkpoints/${project_name}/${exp_name}
mkdir -p "${ckpts_home}"

echo "================================================"
echo "CHECKPOINT SAVE TEST"
echo "================================================"
echo "This will run training for ~5-10 steps"
echo "Checkpoint will be saved at step 2"
echo "Monitor memory spike during save phase"
echo ""
echo "Checkpoint directory: ${ckpts_home}"
echo "================================================"
echo ""

# ============================================================
# Engine config (Same as production, with distributed checkpointing)
# ============================================================
ENGINE_CONFIG="\
    engine=${BACKEND} \
    optim=${BACKEND} \
    optim.lr=${LR} \
    optim.min_lr=${MIN_LR} \
    optim.lr_warmup_steps=2 \
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
    +engine.override_transformer_config.recompute_num_layers=1 \
    +engine.override_transformer_config.moe_aux_loss_coeff=0.01 \
    +engine.override_transformer_config.moe_z_loss_coeff=0.001 \
    +engine.override_transformer_config.moe_permute_fusion=False"

# ============================================================
# Enhanced monitoring for checkpoint test
# ============================================================
MONITOR_LOG="/fsx-shared/juncheng/EHR/checkpoint_test_monitor.log"
TRAINING_LOG="/fsx-shared/juncheng/EHR/checkpoint_test.log"
MEMORY_CSV="/fsx-shared/juncheng/EHR/checkpoint_test_memory.csv"

# Clear old logs
rm -f "${MONITOR_LOG}" "${TRAINING_LOG}" "${MEMORY_CSV}"

# OOM guard: kills training at 96% cgroup memory to prevent pod crash
bash /fsx-shared/juncheng/EHR/oom_guard.sh 96 5 &
OOMGUARD_PID=$!

# Start monitors (2s interval for checkpoint phase)
bash /fsx-shared/juncheng/EHR/monitor_training_enhanced.sh 2 "${TRAINING_LOG}" "${MONITOR_LOG}" &
MONITOR_PID=$!

bash /fsx-shared/juncheng/EHR/log_memory_per_step.sh "${TRAINING_LOG}" "${MEMORY_CSV}" &
MEMLOG_PID=$!

trap "kill ${OOMGUARD_PID} ${MONITOR_PID} ${MEMLOG_PID} 2>/dev/null" EXIT

echo "Monitors started. Logs:"
echo "  Training: ${TRAINING_LOG}"
echo "  System monitor: ${MONITOR_LOG}"
echo "  Per-step memory: ${MEMORY_CSV}"
echo ""

# ============================================================
# Launch (with checkpoint at step 2)
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
    data.val_max_samples=10 \
    model.path=${MODEL_PATH} \
    model.use_remove_padding=False \
    model.trust_remote_code=True \
    ${ENGINE_CONFIG} \
    checkpoint.save_contents='["model","extra"]' \
    trainer.test_freq=100 \
    trainer.save_freq=2 \
    trainer.logger="['console']" \
    trainer.project_name="${project_name}" \
    trainer.experiment_name="${exp_name}" \
    trainer.total_epochs=${TOTAL_EPOCHS} \
    trainer.default_local_dir="${ckpts_home}" \
    trainer.resume_mode=${RESUME_MODE} \
    trainer.seed=42 \
    trainer.nnodes=${NNODES} \
    "$@" 2>&1 | tee "${TRAINING_LOG}"

# Wait for monitors to finish logging
sleep 5
kill ${MONITOR_PID} ${MEMLOG_PID} 2>/dev/null || true

echo ""
echo "================================================"
echo "TEST COMPLETED"
echo "================================================"

# Analyze results
echo ""
echo "Memory at step 2 (checkpoint save):"
grep ",2," "${MEMORY_CSV}" || echo "Step 2 not found in memory log"

echo ""
echo "Memory spike during checkpoint save:"
grep -B2 -A5 "PHASE: checkpoint_save" "${MONITOR_LOG}" | head -15 || echo "No checkpoint save phase detected"

echo ""
echo "Peak memory during test:"
grep "CGROUP_MEM:" "${MONITOR_LOG}" | awk -F'[: /]' '{print $2, $3}' | sort -k1 -rn | head -1

echo ""
echo "Check for OOM warnings:"
grep -E "CRITICAL|WARNING.*Memory" "${MONITOR_LOG}" | tail -5 || echo "No warnings"

echo ""
echo "Checkpoint saved to:"
ls -lh "${ckpts_home}/" 2>/dev/null || echo "No checkpoint found (possible failure)"

echo ""
echo "================================================"
echo "ANALYSIS"
echo "================================================"
echo "Compare memory BEFORE step 2 vs DURING checkpoint save:"
echo "  - If spike > 300 GB: Non-distributed checkpoint issue"
echo "  - If spike < 100 GB: Distributed checkpoint working"
echo ""
echo "To view full logs:"
echo "  cat ${TRAINING_LOG}"
echo "  cat ${MONITOR_LOG}"
echo "  cat ${MEMORY_CSV}"
echo "================================================"
