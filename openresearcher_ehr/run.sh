#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export SERPER_API_KEY=61877f4a59d2968ae439a7d13d49dc2990bc0a1b
export BEDROCK_API_KEY="${BEDROCK_API_KEY:-ABSKQmVkcm9ja0FQSUtleS1xc3k2LWF0LTA2MDc5NTkxNzg0NjpuOUFPMWQwRk9mdTdTeG9icFpzeUtxMWkya0toaEQ5UGYxNGxmTDdoQXNydFA0R095TDBLdk5hWDZIUT0=}"
export AWS_BEARER_TOKEN_BEDROCK="${AWS_BEARER_TOKEN_BEDROCK:-$BEDROCK_API_KEY}"

EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-10}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-5}
BEDROCK_REGION=${BEDROCK_REGION:-us-east-1}
# Bedrock requires the US inference profile ID for Claude Sonnet 4.6.
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6}

python "$SCRIPT_DIR/deploy_agent.py" \
    --data_path /home/efs/zlt/deepresearch/data/EHRAgentBench/common/diagnoses_ccs_500.json \
    --output_dir ./diagnoses_ccs_500_results \
    --use_bedrock \
    --bedrock_model_id "$BEDROCK_MODEL_ID" \
    --bedrock_region "$BEDROCK_REGION" \
    --bedrock_api_key "$BEDROCK_API_KEY" \
    --enable_ehr \
    --ehr_mcp_url "$EHR_MCP_URL" \
    --runs_per_question "$RUNS_PER_QUESTION" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --verbose
