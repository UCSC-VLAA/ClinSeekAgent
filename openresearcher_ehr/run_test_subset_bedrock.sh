#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export SERPER_API_KEY="${SERPER_API_KEY:-61877f4a59d2968ae439a7d13d49dc2990bc0a1b}"

DATA_PATH=${DATA_PATH:-/fsx-shared/juncheng/DeepMed-eval/data/EHRAgentBench/common/subset_100/merged_subsets_600.json}
EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-5}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-1}
MAX_ROUNDS=${MAX_ROUNDS:-200}
MAX_TOKENS=${MAX_TOKENS:-32768}
TEMPERATURE=${TEMPERATURE:-1.0}

BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-global.anthropic.claude-sonnet-4-6}
BEDROCK_REGION=${BEDROCK_REGION:-ca-west-1}
BEDROCK_API_KEY=${BEDROCK_API_KEY:-}

# Build model slug for output directory
MODEL_SLUG="$(python3 - "$BEDROCK_MODEL_ID" <<'PY'
import re
import sys

model_id = sys.argv[1].rstrip("/")
# Strip "us.anthropic." prefix for shorter slug
slug_source = model_id
for prefix in ("global.anthropic.", "us.anthropic.", "anthropic."):
    if slug_source.startswith(prefix):
        slug_source = slug_source[len(prefix):]
        break
slug = re.sub(r"[^A-Za-z0-9]+", "_", slug_source).strip("_").lower()
print(slug or "bedrock_model")
PY
)"

OUTPUT_DIR=${OUTPUT_DIR:-./subsets_600_${MODEL_SLUG}}

echo "Using AWS Bedrock: ${BEDROCK_MODEL_ID} @ ${BEDROCK_REGION}"
echo "Output directory: ${OUTPUT_DIR}"
echo "EHR MCP URL: ${EHR_MCP_URL}"
echo "Data path: ${DATA_PATH}"
echo "Max concurrency: ${MAX_CONCURRENCY}"
echo "Temperature: ${TEMPERATURE}"
echo "Max tokens per call: ${MAX_TOKENS}"

BEDROCK_KEY_FLAG=()
if [[ -n "${BEDROCK_API_KEY}" ]]; then
    BEDROCK_KEY_FLAG+=(--bedrock_api_key "$BEDROCK_API_KEY")
fi

"$SCRIPT_DIR/.venv312/bin/python" "$SCRIPT_DIR/deploy_agent.py" \
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
    --max_tokens "$MAX_TOKENS" \
    --verbose
