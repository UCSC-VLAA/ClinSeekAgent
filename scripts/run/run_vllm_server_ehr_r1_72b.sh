#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MODEL="${REPO_ROOT}/models/EHR-R1-72B"
MODEL_ARCH="Qwen2ForCausalLM"
MODEL_TYPE="qwen2"
CUDA_DEVICES=${1:-0,1,2,3,4,5,6,7}
PORT=${2:-4000}
MAX_MODEL_LEN=32768
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}
TRUST_REMOTE_CODE=0
DTYPE=bfloat16
IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, model_type: ${MODEL_TYPE}"
echo "dtype: ${DTYPE}, max_model_len: ${MAX_MODEL_LEN}"

ARGS=(
    "${MODEL}"
    --port "${PORT}"
    --dtype "${DTYPE}"
    --tensor-parallel-size "${TP_SIZE}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
)

CUDA_VISIBLE_DEVICES=${CUDA_DEVICES} vllm serve "${ARGS[@]}"
