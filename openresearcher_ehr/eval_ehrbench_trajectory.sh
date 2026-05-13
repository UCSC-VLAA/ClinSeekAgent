#!/usr/bin/env bash
# EHR-Bench trajectory evaluation using AWS Bedrock (Claude Opus 4.7 by default).
# To swap the backend model, override BEDROCK_MODEL_ID / BEDROCK_REGION.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -n "${OUTPUT_DIR:-}" && "${OUTPUT_DIR}" != /* ]] && OUTPUT_DIR="$(pwd)/${OUTPUT_DIR}"
cd "$SCRIPT_DIR"

export SERPER_API_KEY="${SERPER_API_KEY:-61877f4a59d2968ae439a7d13d49dc2990bc0a1b}"

# Bedrock credentials — keep the run.sh fallback so existing environments keep working.
export BEDROCK_API_KEY="${BEDROCK_API_KEY:-ABSKQmVkcm9ja0FQSUtleS1xc3k2LWF0LTA2MDc5NTkxNzg0NjpuOUFPMWQwRk9mdTdTeG9icFpzeUtxMWkya0toaEQ5UGYxNGxmTDdoQXNydFA0R095TDBLdk5hWDZIUT0=}"
export AWS_BEARER_TOKEN_BEDROCK="${AWS_BEARER_TOKEN_BEDROCK:-${BEDROCK_API_KEY}}"

DATA_PATH=${DATA_PATH:-../data/EHR-Bench/ehr_bench_sampled_40_per_task.json}

EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-15}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-5}
MAX_ROUNDS=${MAX_ROUNDS:-200}
MAX_TOOL_RESULT_CHARS=${MAX_TOOL_RESULT_CHARS:-100000}
MAX_TOKENS=${MAX_TOKENS:-32768}
TEMPERATURE=${TEMPERATURE:-1.0}
ENABLE_THINKING=${ENABLE_THINKING:-1}

BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-7}
BEDROCK_REGION=${BEDROCK_REGION:-us-east-1}

# Build a filesystem-safe slug from the Bedrock model id.
MODEL_SLUG="$(python3 - "$BEDROCK_MODEL_ID" <<'PY'
import re
import sys

model_id = sys.argv[1].rstrip("/")
slug_source = model_id
for prefix in ("global.anthropic.", "us.anthropic.", "eu.anthropic.", "anthropic."):
    if slug_source.startswith(prefix):
        slug_source = slug_source[len(prefix):]
        break
slug = re.sub(r"[^A-Za-z0-9]+", "_", slug_source).strip("_").lower()
print(slug or "bedrock_model")
PY
)"

OUTPUT_DIR=${OUTPUT_DIR:-./results/ehrbench_1800_${MODEL_SLUG}}
mkdir -p "$OUTPUT_DIR"

LOG_TIMESTAMP=${RUN_TEST_SUBSET_LOG_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_FILE=${RUN_TEST_SUBSET_LOG_FILE:-"${OUTPUT_DIR}/run_test_subset_${LOG_TIMESTAMP}.log"}

if [[ "${RUN_TEST_SUBSET_LOGGING_INITIALIZED:-0}" != "1" ]]; then
    export RUN_TEST_SUBSET_LOGGING_INITIALIZED=1
    export RUN_TEST_SUBSET_LOG_FILE="$LOG_FILE"
    exec > >(tee -a "$RUN_TEST_SUBSET_LOG_FILE") 2>&1
fi

THINKING_FLAG=()
if [[ "${ENABLE_THINKING}" == "1" ]]; then
    THINKING_FLAG+=(--enable_thinking)
else
    THINKING_FLAG+=(--disable_thinking)
fi

BEDROCK_KEY_FLAG=()
if [[ -n "${BEDROCK_API_KEY}" ]]; then
    BEDROCK_KEY_FLAG+=(--bedrock_api_key "$BEDROCK_API_KEY")
fi

echo "Backend: AWS Bedrock"
echo "Model: ${BEDROCK_MODEL_ID} @ ${BEDROCK_REGION}"
echo "Output directory: ${OUTPUT_DIR}"
echo "Log file: ${RUN_TEST_SUBSET_LOG_FILE}"
echo "EHR MCP URL: ${EHR_MCP_URL}"
echo "Data path: ${DATA_PATH}"
echo "Max concurrency: ${MAX_CONCURRENCY}"
echo "Runs per question: ${RUNS_PER_QUESTION}"
echo "Max rounds: ${MAX_ROUNDS}"
echo "Tool result char limit: ${MAX_TOOL_RESULT_CHARS}"
echo "Max tokens per call: ${MAX_TOKENS}"
echo "Temperature: ${TEMPERATURE}"
echo "Thinking: ${ENABLE_THINKING}"

PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/venv/gemma/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Error: python not found at $PYTHON_BIN" >&2
    exit 1
fi

"$PYTHON_BIN" "$SCRIPT_DIR/deploy_agent.py" \
    --backend bedrock \
    --bedrock_model_id "$BEDROCK_MODEL_ID" \
    --bedrock_region "$BEDROCK_REGION" \
    "${BEDROCK_KEY_FLAG[@]}" \
    --data_path "$DATA_PATH" \
    --output_dir "$OUTPUT_DIR" \
    --enable_ehr \
    --ehr_mcp_url "$EHR_MCP_URL" \
    --temperature "$TEMPERATURE" \
    --runs_per_question "$RUNS_PER_QUESTION" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --max_rounds "$MAX_ROUNDS" \
    --max_tool_result_chars "$MAX_TOOL_RESULT_CHARS" \
    --max_tokens "$MAX_TOKENS" \
    "${THINKING_FLAG[@]}" \
    --verbose
