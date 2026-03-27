#!/usr/bin/env bash

set -euo pipefail

MODEL=${MODEL:-"/home/efs/zlt/deepresearch/models/Tongyi-DeepResearch-30B-A3B"}
MODEL_ARCH=${MODEL_ARCH:-"Qwen3MoeForCausalLM"}
CUDA_DEVICES=${1:-0,1,2,3,4,5,6,7}
PORT=${2:-4000}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-131072}
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}
TRUST_REMOTE_CODE=${TRUST_REMOTE_CODE:-0}

IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, max_model_len: ${MAX_MODEL_LEN}, trust_remote_code: ${TRUST_REMOTE_CODE}"

export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1

ARGS=(
    "${MODEL}"
    --port "${PORT}"
    --dtype auto
    --tensor-parallel-size "${TP_SIZE}"
    --hf-overrides "{\"architectures\":[\"${MODEL_ARCH}\"]}"
    --enable-auto-tool-choice
    --tool-call-parser qwen3_xml
    --reasoning-parser qwen3
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
)

if [[ "${TRUST_REMOTE_CODE}" == "1" ]]; then
    ARGS+=(--trust-remote-code)
fi

CUDA_VISIBLE_DEVICES=${CUDA_DEVICES} vllm serve "${ARGS[@]}"
    # --speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":2}'
