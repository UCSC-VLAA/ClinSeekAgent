#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${OUTPUT_DIR:-}" && "${OUTPUT_DIR}" != /* ]] && OUTPUT_DIR="$(pwd)/${OUTPUT_DIR}"
cd "$SCRIPT_DIR"

export SERPER_API_KEY=61877f4a59d2968ae439a7d13d49dc2990bc0a1b
export BEDROCK_API_KEY="${BEDROCK_API_KEY:-ABSKQmVkcm9ja0FQSUtleS1xc3k2LWF0LTA2MDc5NTkxNzg0NjpuOUFPMWQwRk9mdTdTeG9icFpzeUtxMWkya0toaEQ5UGYxNGxmTDdoQXNydFA0R095TDBLdk5hWDZIUT0=}"
export AWS_BEARER_TOKEN_BEDROCK="${BEDROCK_API_KEY}"

EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-15}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-2}
MAX_ROUNDS=${MAX_ROUNDS:-200}
MAX_TOOL_RESULT_CHARS=${MAX_TOOL_RESULT_CHARS:-100000}
MAX_TOKENS=${MAX_TOKENS:-32768}
ENABLE_THINKING=${ENABLE_THINKING:-1}
BEDROCK_REGION=${BEDROCK_REGION:-us-east-1}
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}
DATA_PATH=${DATA_PATH:-../data/AgentEHR-Bench/MIMICIVAgentBench/train/mix_training_3k.json}
OUTPUT_DIR=${OUTPUT_DIR:-./results/train_trajectory_3k_2ep_thinking}
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

echo "Output directory: ${OUTPUT_DIR}"
echo "Log file: ${RUN_LOG_FILE}"
echo "EHR MCP URL: ${EHR_MCP_URL}"
echo "Model: ${BEDROCK_MODEL_ID} @ ${BEDROCK_REGION}"
echo "Thinking: ${ENABLE_THINKING}"
echo "Tool result char limit: ${MAX_TOOL_RESULT_CHARS}"
echo "Max tokens per call: ${MAX_TOKENS}"

python ./deploy_agent.py \
    --data_path "$DATA_PATH" \
    --output_dir "$OUTPUT_DIR" \
    --use_bedrock \
    --bedrock_model_id "$BEDROCK_MODEL_ID" \
    --bedrock_region "$BEDROCK_REGION" \
    --bedrock_api_key "$BEDROCK_API_KEY" \
    --enable_ehr \
    --ehr_mcp_url "$EHR_MCP_URL" \
    --runs_per_question "$RUNS_PER_QUESTION" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --max_rounds "$MAX_ROUNDS" \
    --max_tool_result_chars "$MAX_TOOL_RESULT_CHARS" \
    --max_tokens "$MAX_TOKENS" \
    "${THINKING_FLAG[@]}" \
    --verbose
