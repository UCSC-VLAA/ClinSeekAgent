#!/usr/bin/env bash

# set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

export MODEL="/home/efs/zlt/deepresearch/models/OpenSeeker-v1-30B-SFT"
export MODEL_ARCH="Qwen3MoeForCausalLM"
export MODEL_TYPE="qwen3_moe"
export CUDA_DEVICES=${1:-0,1,2,3,4,5,6,7}
export PORT=${2:-4000}
export MAX_MODEL_LEN=262144
export GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}
export TRUST_REMOTE_CODE=${TRUST_REMOTE_CODE:-1}
export DTYPE=${DTYPE:-bfloat16}
export TOOL_CALL_PARSER=${TOOL_CALL_PARSER:-}
export TOOL_PARSER_PLUGIN=${TOOL_PARSER_PLUGIN:-}
export CHAT_TEMPLATE=${CHAT_TEMPLATE:-}
export REASONING_PARSER=${REASONING_PARSER:-qwen3}
export LANGUAGE_MODEL_ONLY=1

IFS=',' read -r -a DEVICE_ARRAY <<< "${CUDA_DEVICES}"
TP_SIZE=${#DEVICE_ARRAY[@]}
HF_OVERRIDES=${HF_OVERRIDES:-}

SERVE_MODEL="${MODEL}"

if [[ "${MODEL,,}" == *"openseeker"* ]]; then
    if [[ -z "${CHAT_TEMPLATE}" ]]; then
        CHAT_TEMPLATE="${REPO_ROOT}/openresearcher_ehr/openseeker_vllm/chat_template.jinja"
    fi
    if [[ -z "${TOOL_PARSER_PLUGIN}" ]]; then
        TOOL_PARSER_PLUGIN="${REPO_ROOT}/openresearcher_ehr/openseeker_vllm/tool_parser.py"
    fi
    if [[ -z "${TOOL_CALL_PARSER}" || "${TOOL_CALL_PARSER}" == "qwen3_xml" ]]; then
        TOOL_CALL_PARSER="openseeker"
    fi
elif [[ -z "${TOOL_CALL_PARSER}" ]]; then
    TOOL_CALL_PARSER="qwen3_xml"
fi

if [[ -n "${CHAT_TEMPLATE}" && ! -f "${CHAT_TEMPLATE}" ]]; then
    echo "chat template not found: ${CHAT_TEMPLATE}" >&2
    exit 1
fi

if [[ -n "${TOOL_PARSER_PLUGIN}" && ! -f "${TOOL_PARSER_PLUGIN}" ]]; then
    echo "tool parser plugin not found: ${TOOL_PARSER_PLUGIN}" >&2
    exit 1
fi

echo "run vllm on cuda: ${CUDA_DEVICES}, tp_size: ${TP_SIZE}, port: ${PORT}"
echo "model: ${MODEL}, architecture: ${MODEL_ARCH}, model_type: ${MODEL_TYPE}"
echo "serve_model: ${SERVE_MODEL}"
echo "dtype: ${DTYPE}, max_model_len: ${MAX_MODEL_LEN}, trust_remote_code: ${TRUST_REMOTE_CODE}"
echo "language_model_only: ${LANGUAGE_MODEL_ONLY}"
echo "tool_call_parser: ${TOOL_CALL_PARSER}"
if [[ -n "${TOOL_PARSER_PLUGIN}" ]]; then
    echo "tool_parser_plugin: ${TOOL_PARSER_PLUGIN}"
fi
if [[ -n "${CHAT_TEMPLATE}" ]]; then
    echo "chat_template: ${CHAT_TEMPLATE}"
fi
if [[ -n "${REASONING_PARSER}" ]]; then
    echo "reasoning_parser: ${REASONING_PARSER}"
fi
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
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
)

if [[ -n "${REASONING_PARSER}" ]]; then
    ARGS+=(--reasoning-parser "${REASONING_PARSER}")
fi

if [[ -n "${TOOL_PARSER_PLUGIN}" ]]; then
    ARGS+=(--tool-parser-plugin "${TOOL_PARSER_PLUGIN}")
fi

if [[ -n "${CHAT_TEMPLATE}" ]]; then
    ARGS+=(--chat-template "${CHAT_TEMPLATE}")
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
