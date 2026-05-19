#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DATA_PATH="${DATA_PATH:?Set DATA_PATH to a JSON or JSONL benchmark manifest}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/outputs/mm_eval}"
BACKEND="${BACKEND:-bedrock}"
if [[ "${BACKEND}" == "vllm" ]]; then
  MODEL_NAME_OR_PATH="${MODEL_NAME_OR_PATH:-${VLLM_MODEL:-auto}}"
else
  MODEL_NAME_OR_PATH="${MODEL_NAME_OR_PATH:-${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}}"
fi
BEDROCK_REGION="${BEDROCK_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:4000/v1}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
EHR_MCP_URL="${EHR_MCP_URL:-http://127.0.0.1:5003/mcp}"
IMAGE_MCP_URL="${IMAGE_MCP_URL:-http://127.0.0.1:5203/mcp}"
BENCH_ROOT="${BENCH_ROOT:-${CLINSEEK_DATA_ROOT:-${REPO_ROOT}/data}/multimodal}"
IMAGE_MAX_EDGE="${IMAGE_MAX_EDGE:-1568}"
MAX_ROUNDS="${MAX_ROUNDS:-200}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-6}"
RUNS_PER_QUESTION="${RUNS_PER_QUESTION:-1}"
MAX_TOOL_RESULT_CHARS="${MAX_TOOL_RESULT_CHARS:-100000}"
PYTHON_BIN="${PYTHON_BIN:-python}"
ENABLE_IMAGE="${ENABLE_IMAGE:-1}"

export PYTHONPATH="${REPO_ROOT}/clinseekagent:${REPO_ROOT}/src:${PYTHONPATH:-}"

COMMON_ARGS=(
  --data_path "${DATA_PATH}"
  --output_dir "${OUTPUT_DIR}"
  --backend "${BACKEND}"
  --model_name_or_path "${MODEL_NAME_OR_PATH}"
  --enable_ehr
  --ehr_mcp_url "${EHR_MCP_URL}"
  --bench_root "${BENCH_ROOT}"
  --image_max_edge "${IMAGE_MAX_EDGE}"
  --max_rounds "${MAX_ROUNDS}"
  --max_concurrency "${MAX_CONCURRENCY}"
  --runs_per_question "${RUNS_PER_QUESTION}"
  --max_tool_result_chars "${MAX_TOOL_RESULT_CHARS}"
)

if [[ "${ENABLE_IMAGE}" == "1" ]]; then
  COMMON_ARGS+=(--enable_image --image_mcp_url "${IMAGE_MCP_URL}")
fi

if [[ "${BACKEND}" == "bedrock" ]]; then
  COMMON_ARGS+=(--bedrock_region "${BEDROCK_REGION}")
else
  COMMON_ARGS+=(--api_base_url "${VLLM_BASE_URL}" --api_key "${VLLM_API_KEY}")
fi

exec "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_multimodal.py" "${COMMON_ARGS[@]}" "$@"
