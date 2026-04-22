#!/usr/bin/env bash
# Multimodal pipeline launcher. Mirrors run.sh but targets deploy_agent_mm.py.
# The text-only run.sh / deploy_agent.py are untouched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Credentials (keep parity with run.sh defaults)
export SERPER_API_KEY="${SERPER_API_KEY:-61877f4a59d2968ae439a7d13d49dc2990bc0a1b}"
export BEDROCK_API_KEY="${BEDROCK_API_KEY:-ABSKQmVkcm9ja0FQSUtleS1xc3k2LWF0LTA2MDc5NTkxNzg0NjpuOUFPMWQwRk9mdTdTeG9icFpzeUtxMWkya0toaEQ5UGYxNGxmTDdoQXNydFA0R095TDBLdk5hWDZIUT0=}"
export AWS_BEARER_TOKEN_BEDROCK="${AWS_BEARER_TOKEN_BEDROCK:-$BEDROCK_API_KEY}"

# MCP + data defaults
EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
IMAGE_MCP_URL=${IMAGE_MCP_URL:-http://127.0.0.1:5203/mcp}
BENCH_ROOT=${BENCH_ROOT:-/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3}
IMAGE_MAX_EDGE=${IMAGE_MAX_EDGE:-1568}

# Execution parameters
MAX_CONCURRENCY=${MAX_CONCURRENCY:-6}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-1}
MAX_ROUNDS=${MAX_ROUNDS:-60}
MAX_TOOL_RESULT_CHARS=${MAX_TOOL_RESULT_CHARS:-100000}
ENABLE_THINKING=${ENABLE_THINKING:-0}
BEDROCK_REGION=${BEDROCK_REGION:-us-east-1}
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}

DATA_PATH=${DATA_PATH:-./data_mm/ehrxqa_image_test.jsonl}
OUTPUT_DIR=${OUTPUT_DIR:-./results/mm_ehrxqa_image}
mkdir -p "$OUTPUT_DIR"

LOG_TIMESTAMP=${RUN_LOG_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_FILE=${RUN_LOG_FILE:-"${OUTPUT_DIR}/run_${LOG_TIMESTAMP}.log"}

if [[ "${RUN_LOGGING_INITIALIZED:-0}" != "1" ]]; then
    export RUN_LOGGING_INITIALIZED=1
    export RUN_LOG_FILE="$LOG_FILE"
    exec > >(tee -a "$RUN_LOG_FILE") 2>&1
fi

THINKING_FLAG=()
if [[ "${ENABLE_THINKING}" == "1" ]]; then
    THINKING_FLAG+=(--enable_thinking)
else
    THINKING_FLAG+=(--disable_thinking)
fi

echo "=== MULTIMODAL RUN ==="
echo "Output directory: ${OUTPUT_DIR}"
echo "Log file: ${RUN_LOG_FILE}"
echo "EHR MCP URL: ${EHR_MCP_URL}"
echo "Image MCP URL: ${IMAGE_MCP_URL}"
echo "Bench root: ${BENCH_ROOT}"
echo "Model: ${BEDROCK_MODEL_ID} @ ${BEDROCK_REGION}"
echo "Image max edge: ${IMAGE_MAX_EDGE}"
echo "Thinking: ${ENABLE_THINKING}"
echo "Tool result char limit: ${MAX_TOOL_RESULT_CHARS}"

python ./deploy_agent_mm.py \
    --data_path "$DATA_PATH" \
    --output_dir "$OUTPUT_DIR" \
    --use_bedrock \
    --bedrock_model_id "$BEDROCK_MODEL_ID" \
    --bedrock_region "$BEDROCK_REGION" \
    --bedrock_api_key "$BEDROCK_API_KEY" \
    --enable_ehr \
    --ehr_mcp_url "$EHR_MCP_URL" \
    --enable_image \
    --image_mcp_url "$IMAGE_MCP_URL" \
    --bench_root "$BENCH_ROOT" \
    --image_max_edge "$IMAGE_MAX_EDGE" \
    --runs_per_question "$RUNS_PER_QUESTION" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --max_rounds "$MAX_ROUNDS" \
    --max_tool_result_chars "$MAX_TOOL_RESULT_CHARS" \
    "${THINKING_FLAG[@]}" \
    --verbose
