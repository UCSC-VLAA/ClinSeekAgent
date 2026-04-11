#!/usr/bin/env bash

# set -euo pipefail

export MODEL="/home/efs/zlt/deepresearch/models/Meissa-4B"
export MODEL_ARCH="Qwen3VLForConditionalGeneration"
export MODEL_TYPE="qwen3_vl"
export SERVED_MODEL_NAME=${SERVED_MODEL_NAME:-Meissa-4B}
export CUDA_DEVICES=${1:-0}
export PORT=${2:-4000}
export MAX_MODEL_LEN=${MAX_MODEL_LEN:-8192}
export GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.85}
export TRUST_REMOTE_CODE=${TRUST_REMOTE_CODE:-1}
export DTYPE=${DTYPE:-bfloat16}
# Meissa-4B uses Hermes-format tool calls and should stay multimodal by default.
export TOOL_CALL_PARSER=${TOOL_CALL_PARSER:-hermes}
export REASONING_PARSER=${REASONING_PARSER:-}
export LANGUAGE_MODEL_ONLY=${LANGUAGE_MODEL_ONLY:-0}

IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}
HF_OVERRIDES=${HF_OVERRIDES:-}

SERVE_MODEL="${MODEL}"

if [[ ! -d "${MODEL}" ]]; then
    echo "model directory not found: ${MODEL}" >&2
    exit 1
fi

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, model_type: ${MODEL_TYPE}"
echo "served_model_name: ${SERVED_MODEL_NAME}"
echo "dtype: ${DTYPE}, max_model_len: ${MAX_MODEL_LEN}, trust_remote_code: ${TRUST_REMOTE_CODE}"
echo "tool_call_parser: ${TOOL_CALL_PARSER}, reasoning_parser: ${REASONING_PARSER:-<disabled>}"
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
    --served-model-name "${SERVED_MODEL_NAME}"
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
