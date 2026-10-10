#!/usr/bin/env bash
# ClinSeekAgent on ClinSeek-Bench-Text (agentic loop or single-pass retrieval).
#
# Defaults reproduce the paper setting: 16 EHR tools (no browser), up to 200
# rounds, 8,192 output tokens per call, tool results truncated to 100,000
# characters, reasoning on (Claude: 4,096-token thinking budget, or effort=high
# on adaptive-only models), temperature 0 where the model accepts it.
#
#   DATA_PATH=/path/to/ClinSeek-Bench_text.json bash scripts/run_text_eval.sh
#   BACKEND=vertex MODEL_NAME_OR_PATH=claude-opus-4-6 DATA_PATH=... bash scripts/run_text_eval.sh
#   BACKEND=vllm MODEL_NAME_OR_PATH=ClinSeek-35B-A3B DATA_PATH=... bash scripts/run_text_eval.sh
#   RETRIEVAL_MODE=single_shot DATA_PATH=... bash scripts/run_text_eval.sh   # Single-Pass Retrieval
#
# Requires an EHR MCP server on EHR_MCP_URL (scripts/run_ehr_mcp.sh).
# Extra run_text.py flags pass straight through ("$@"), e.g. --enable_browser.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DATA_PATH="${DATA_PATH:?Set DATA_PATH to the ClinSeek-Bench-Text manifest (JSON or JSONL)}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/outputs/text_eval}"
BACKEND="${BACKEND:-bedrock}"
if [[ "${BACKEND}" == "vllm" ]]; then
  MODEL_NAME_OR_PATH="${MODEL_NAME_OR_PATH:-${VLLM_MODEL:-auto}}"
elif [[ "${BACKEND}" == "vertex" ]]; then
  # "auto" => the backend picks the first Claude model the project serves
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
RETRIEVAL_MODE="${RETRIEVAL_MODE:-agentic}"
MAX_ROUNDS="${MAX_ROUNDS:-200}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-6}"
RUNS_PER_QUESTION="${RUNS_PER_QUESTION:-1}"
MAX_TOOL_RESULT_CHARS="${MAX_TOOL_RESULT_CHARS:-100000}"
MAX_TOKENS="${MAX_TOKENS:-8192}"
TEMPERATURE="${TEMPERATURE:-0.0}"
ENABLE_THINKING="${ENABLE_THINKING:-1}"
THINKING_BUDGET="${THINKING_BUDGET:-4096}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"
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
  --retrieval_mode "${RETRIEVAL_MODE}"
  --max_rounds "${MAX_ROUNDS}"
  --max_concurrency "${MAX_CONCURRENCY}"
  --runs_per_question "${RUNS_PER_QUESTION}"
  --max_tool_result_chars "${MAX_TOOL_RESULT_CHARS}"
  --max_tokens "${MAX_TOKENS}"
  --temperature "${TEMPERATURE}"
  --thinking_budget_tokens "${THINKING_BUDGET}"
  --reasoning_effort "${REASONING_EFFORT}"
)
if [[ "${ENABLE_THINKING}" == "1" ]]; then
  COMMON_ARGS+=(--enable_thinking)
else
  COMMON_ARGS+=(--disable_thinking)
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

exec "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_text.py" "${COMMON_ARGS[@]}" "$@"
