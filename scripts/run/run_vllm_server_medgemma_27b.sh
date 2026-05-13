#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MODEL="${REPO_ROOT}/models/medgemma-27b-it"
MODEL_ARCH="Gemma3ForConditionalGeneration"
MODEL_TYPE="gemma3"
CUDA_DEVICES=${1:-0,1,2,3,4,5,6,7}
PORT=${2:-4000}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-131072}
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}
DTYPE=${DTYPE:-bfloat16}
CHAT_TEMPLATE="${MODEL}/chat_template.jinja"

VLLM_BIN="${REPO_ROOT}/venv/gemma/bin/vllm"

IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}

if [[ ! -d "${MODEL}" ]]; then
    echo "model directory not found: ${MODEL}" >&2
    exit 1
fi

if [[ ! -f "${VLLM_BIN}" ]]; then
    echo "vllm binary not found: ${VLLM_BIN}" >&2
    exit 1
fi

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, model_type: ${MODEL_TYPE}"
echo "dtype: ${DTYPE}, max_model_len: ${MAX_MODEL_LEN}"
echo "chat_template: ${CHAT_TEMPLATE}"

ARGS=(
    "${MODEL}"
    --port "${PORT}"
    --dtype "${DTYPE}"
    --tensor-parallel-size "${TP_SIZE}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
    --chat-template "${CHAT_TEMPLATE}"
    --language-model-only
)

CUDA_VISIBLE_DEVICES=${CUDA_DEVICES} "${VLLM_BIN}" serve "${ARGS[@]}"
