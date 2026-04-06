#!/usr/bin/env bash

# set -euo pipefail

export MODEL=${MODEL:-"/home/efs/zlt/deepresearch/models/OpenSeeker-v1-30B-SFT"}
export MODEL_ARCH=${MODEL_ARCH:-"Qwen3MoeForCausalLM"}
export MODEL_TYPE=${MODEL_TYPE:-"qwen3_moe"}
export CUDA_DEVICES=${1:-0,1,2,3,4,5,6,7}
export PORT=${2:-4000}
export MAX_MODEL_LEN=${MAX_MODEL_LEN:-262144}
export GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}
export TRUST_REMOTE_CODE=${TRUST_REMOTE_CODE:-1}
export DTYPE=${DTYPE:-bfloat16}
export TOOL_CALL_PARSER=${TOOL_CALL_PARSER:-qwen3_xml}
export REASONING_PARSER=${REASONING_PARSER:-qwen3}
export LANGUAGE_MODEL_ONLY=${LANGUAGE_MODEL_ONLY:-1}

IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}
HF_OVERRIDES=${HF_OVERRIDES:-}

SERVE_MODEL="${MODEL}"

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, model_type: ${MODEL_TYPE}"
echo "serve_model: ${SERVE_MODEL}"
echo "dtype: ${DTYPE}, max_model_len: ${MAX_MODEL_LEN}, trust_remote_code: ${TRUST_REMOTE_CODE}"
echo "language_model_only: ${LANGUAGE_MODEL_ONLY}"
if [[ -n "${HF_OVERRIDES}" ]]; then
    echo "hf_overrides: ${HF_OVERRIDES}"
fi

export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1

ARGS=(
    "${SERVE_MODEL}"
    --port "${PORT}"
    --dtype "${DTYPE}"
    --tensor-parallel-size "${TP_SIZE}"
    --enable-auto-tool-choice
    --tool-call-parser "${TOOL_CALL_PARSER}"
    --reasoning-parser "${REASONING_PARSER}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
)

if [[ -n "${HF_OVERRIDES}" ]]; then
    ARGS+=(--hf-overrides "${HF_OVERRIDES}")
fi

if [[ "${TRUST_REMOTE_CODE}" == "1" ]]; then
    ARGS+=(--trust-remote-code)
fi

if [[ "${LANGUAGE_MODEL_ONLY}" == "1" ]]; then
    ARGS+=(--language-model-only)
fi

CUDA_VISIBLE_DEVICES=${CUDA_DEVICES} vllm serve "${ARGS[@]}"
    # --speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":2}'
