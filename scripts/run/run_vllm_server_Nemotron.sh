#!/usr/bin/env bash

set -euo pipefail

MODEL="/home/efs/zlt/deepresearch/models/OpenResearcher-30B-A3B"
MODEL_ARCH="NemotronHForCausalLM"
MODEL_TYPE="nemotron_h"
TRUST_REMOTE_CODE=1
DTYPE="bfloat16"
TOOL_CALL_PARSER="qwen3_xml"
REASONING_PARSER="nemotron_v3"
LANGUAGE_MODEL_ONLY=1
export SERVE_MODEL_NAME=${SERVE_MODEL_NAME:-"OpenResearcher-30B-A3B"}
export CUDA_DEVICES=${1:-0,1,2,3,4,5,6,7}
export PORT=${2:-4000}
export MAX_MODEL_LEN=${MAX_MODEL_LEN:-262144}
export GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}

IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}
HF_OVERRIDES=${HF_OVERRIDES:-}

if [[ ! -d "${MODEL}" ]]; then
    echo "model directory not found: ${MODEL}" >&2
    exit 1
fi

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, model_type: ${MODEL_TYPE}"
echo "served_model_name: ${SERVE_MODEL_NAME}"
echo "dtype: ${DTYPE}, max_model_len: ${MAX_MODEL_LEN}, trust_remote_code: ${TRUST_REMOTE_CODE}"
echo "language_model_only: ${LANGUAGE_MODEL_ONLY}"
if [[ -n "${HF_OVERRIDES}" ]]; then
    echo "hf_overrides: ${HF_OVERRIDES}"
fi

export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1

ARGS=(
    "${MODEL}"
    --port "${PORT}"
    --dtype "${DTYPE}"
    --tensor-parallel-size "${TP_SIZE}"
    --served-model-name "${SERVE_MODEL_NAME}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
)

if [[ -n "${TOOL_CALL_PARSER}" ]]; then
    ARGS+=(--enable-auto-tool-choice --tool-call-parser "${TOOL_CALL_PARSER}")
fi

if [[ -n "${REASONING_PARSER}" ]]; then
    ARGS+=(--reasoning-parser "${REASONING_PARSER}")
fi

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
