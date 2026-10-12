#!/usr/bin/env bash
# ClinSeekAgent on ClinSeek-Bench-MM (agentic loop or single-pass retrieval).
#
# Defaults reproduce the paper setting: 16 EHR + 6 image tools (22 total, no
# browser), up to 200 rounds, up to 4 attached CXR images (longest edge 1,568
# px), 8,192 output tokens per call, tool results truncated to 100,000
# characters, reasoning on, temperature 0 where accepted.
#
#   DATA_PATH=/path/to/ClinSeek-Bench_multimodal.jsonl \
#   BENCH_ROOT=/path/to/mm_bench/ehrxqa:/path/to/mm_bench/medmod \
#   bash scripts/run_mm_eval.sh
#
# BENCH_ROOT is a colon-separated list of roots against which the manifest's
# image/report paths are resolved (the leading `*OriginalLinked_v1/` package
# segment of a released path is stripped when needed).
#
# Requires, before launching:
#   - one EHR MCP server per source benchmark (EHR_MCP_URL_EHRXQA /
#     EHR_MCP_URL_MEDMOD). The two benchmarks have different schemas and
#     overlapping subject_ids, so a single server would serve the wrong
#     patient. Start them with scripts/run_ehr_mcp.sh.
#   - the image MCP server on IMAGE_MCP_URL, unless ENABLE_IMAGE=0
#     (EHR-only tool-space ablation).
# The manifest must carry the trusted temporal/scope metadata (subject_id,
# prediction_time, scope, modalities, study_ids); the public
# ClinSeek-Bench_multimodal.jsonl does. Anything else is rejected by
# multimodal_manifest_policy.validate_agentic_manifest.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DATA_PATH="${DATA_PATH:?Set DATA_PATH to the ClinSeek-Bench-MM manifest (JSONL)}"
BENCH_ROOT="${BENCH_ROOT:?Set BENCH_ROOT to the rebuilt roots, e.g. mm_bench/ehrxqa:mm_bench/medmod}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/outputs/mm_eval}"
BACKEND="${BACKEND:-bedrock}"
if [[ "${BACKEND}" == "vllm" ]]; then
  MODEL_NAME_OR_PATH="${MODEL_NAME_OR_PATH:-${VLLM_MODEL:-auto}}"
elif [[ "${BACKEND}" == "vertex" ]]; then
  MODEL_NAME_OR_PATH="${MODEL_NAME_OR_PATH:-${VERTEX_MODEL_ID:-auto}}"
else
  MODEL_NAME_OR_PATH="${MODEL_NAME_OR_PATH:-${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}}"
fi
BEDROCK_REGION="${BEDROCK_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
VERTEX_REGION="${VERTEX_REGION:-${CLOUD_ML_REGION:-us-east5}}"
VERTEX_PROJECT="${VERTEX_PROJECT:-${ANTHROPIC_VERTEX_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}}"
VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:4000/v1}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
EHR_MCP_URL="${EHR_MCP_URL:-http://127.0.0.1:5003/mcp}"
# One server per benchmark: rows are routed by source_benchmark at runtime.
EHR_MCP_URL_EHRXQA="${EHR_MCP_URL_EHRXQA:-http://127.0.0.1:5004/mcp}"
EHR_MCP_URL_MEDMOD="${EHR_MCP_URL_MEDMOD:-http://127.0.0.1:5005/mcp}"
IMAGE_MCP_URL="${IMAGE_MCP_URL:-http://127.0.0.1:5203/mcp}"
IMAGE_MAX_EDGE="${IMAGE_MAX_EDGE:-1568}"
RETRIEVAL_MODE="${RETRIEVAL_MODE:-agentic}"
MAX_ROUNDS="${MAX_ROUNDS:-200}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-6}"
RUNS_PER_QUESTION="${RUNS_PER_QUESTION:-1}"
MAX_TOOL_RESULT_CHARS="${MAX_TOOL_RESULT_CHARS:-100000}"
TEMPERATURE="${TEMPERATURE:-0.0}"
ENABLE_THINKING="${ENABLE_THINKING:-1}"
THINKING_BUDGET="${THINKING_BUDGET:-4096}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"
ENABLE_IMAGE="${ENABLE_IMAGE:-1}"
ENABLE_BROWSER="${ENABLE_BROWSER:-0}"
PYTHON_BIN="${PYTHON_BIN:-python}"

export PYTHONPATH="${REPO_ROOT}/clinseekagent:${REPO_ROOT}/src:${PYTHONPATH:-}"

COMMON_ARGS=(
  --data_path "${DATA_PATH}"
  --output_dir "${OUTPUT_DIR}"
  --backend "${BACKEND}"
  --model_name_or_path "${MODEL_NAME_OR_PATH}"
  --enable_ehr
  --ehr_mcp_url "${EHR_MCP_URL}"
  --ehr_mcp_url_ehrxqa "${EHR_MCP_URL_EHRXQA}"
  --ehr_mcp_url_medmod "${EHR_MCP_URL_MEDMOD}"
  --bench_root "${BENCH_ROOT}"
  --image_max_edge "${IMAGE_MAX_EDGE}"
  --retrieval_mode "${RETRIEVAL_MODE}"
  --max_rounds "${MAX_ROUNDS}"
  --max_concurrency "${MAX_CONCURRENCY}"
  --runs_per_question "${RUNS_PER_QUESTION}"
  --max_tool_result_chars "${MAX_TOOL_RESULT_CHARS}"
  --temperature "${TEMPERATURE}"
  --thinking_budget_tokens "${THINKING_BUDGET}"
  --reasoning_effort "${REASONING_EFFORT}"
)
if [[ "${ENABLE_THINKING}" == "1" ]]; then
  COMMON_ARGS+=(--enable_thinking)
else
  COMMON_ARGS+=(--disable_thinking)
fi
if [[ "${ENABLE_IMAGE}" == "1" ]]; then
  COMMON_ARGS+=(--enable_image --image_mcp_url "${IMAGE_MCP_URL}")
fi
if [[ "${ENABLE_BROWSER}" == "1" ]]; then
  COMMON_ARGS+=(--enable_browser)
fi

if [[ "${BACKEND}" == "bedrock" ]]; then
  COMMON_ARGS+=(--bedrock_region "${BEDROCK_REGION}")
elif [[ "${BACKEND}" == "vertex" ]]; then
  COMMON_ARGS+=(--vertex_region "${VERTEX_REGION}")
  if [[ -n "${VERTEX_PROJECT}" ]]; then
    COMMON_ARGS+=(--vertex_project "${VERTEX_PROJECT}")
  fi
else
  COMMON_ARGS+=(--api_base_url "${VLLM_BASE_URL}" --api_key "${VLLM_API_KEY}")
fi

exec "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_multimodal.py" "${COMMON_ARGS[@]}" "$@"
