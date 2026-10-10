#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH="${MODEL_PATH:-${CLINSEEK_MODEL_DIR:?Set MODEL_PATH or CLINSEEK_MODEL_DIR}}"
HOST="${VLLM_HOST:-0.0.0.0}"
PORT="${VLLM_PORT:-4000}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-$(basename "${MODEL_PATH}")}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
REQUESTED_MAX_MODEL_LEN="${MAX_MODEL_LEN:-}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
VLLM_BIN="${VLLM_BIN:-vllm}"

# FlashInfer launches the external `ninja` command at runtime.  When VLLM_BIN
# points into a dedicated virtualenv, make that environment's helper binaries
# visible even if the caller did not activate it first.
VLLM_BIN_PATH="$(command -v "${VLLM_BIN}")"
export PATH="$(dirname "${VLLM_BIN_PATH}"):${PATH}"
# These are launcher variables, not vLLM runtime environment settings.  Avoid
# vLLM's unknown-VLLM-variable warning after their values have been captured.
unset VLLM_BIN VLLM_HOST

EXTRA_ARGS=()
MODEL_PATH_LOWER="$(printf '%s' "${MODEL_PATH}" | tr '[:upper:]' '[:lower:]')"
if [[ "${MODEL_PATH_LOWER}" == *"clinseek-35b-a3b"* ||
      "${MODEL_PATH_LOWER}" == *"qwen3.5-35b-a3b"* ]]; then
  # ClinSeek and its Qwen3.5-35B-A3B base both use Qwen3.5's native
  # structured tool-call template and the same text-only serving geometry.
  # 2.4 GB/GPU is the largest KV allocation that also passed sampling warmup
  # on 24 GB RTX A5000 cards with TP=4.
  KV_CACHE_MEMORY="${KV_CACHE_MEMORY:-2400000000}"
  # AgentEHR trajectories can exceed 122k prompt tokens before the finish
  # call. The 2.4 GB/GPU cache holds about 227k tokens, so 196608 preserves
  # the configured 8k completion budget while retaining TP=4 on A5000 cards.
  # This remains below the model's native 262144-token limit.
  [[ -z "${REQUESTED_MAX_MODEL_LEN}" ]] && MAX_MODEL_LEN=196608
  EXTRA_ARGS+=(--reasoning-parser qwen3
               --enable-auto-tool-choice
               --tool-call-parser qwen3_coder
               --language-model-only
               --disable-custom-all-reduce)
fi

if [[ -n "${KV_CACHE_MEMORY:-}" ]]; then
  EXTRA_ARGS+=(--kv-cache-memory "${KV_CACHE_MEMORY}")
else
  EXTRA_ARGS+=(--gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}")
fi

exec "${VLLM_BIN_PATH}" serve "${MODEL_PATH}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --served-model-name "${SERVED_MODEL_NAME}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  "${EXTRA_ARGS[@]}" \
  "$@"
