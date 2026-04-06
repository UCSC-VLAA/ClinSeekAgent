#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export SERPER_API_KEY="${SERPER_API_KEY:-61877f4a59d2968ae439a7d13d49dc2990bc0a1b}"

DATA_PATH=${DATA_PATH:-/home/efs/zlt/deepresearch/data/EHRAgentBench/train/diagnoses_ccs_500.json}
EHR_MCP_URL=${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-5}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-1}
MAX_ROUNDS=${MAX_ROUNDS:-200}

VLLM_BASE_URL=${VLLM_BASE_URL:-http://127.0.0.1:4000}
VLLM_MODEL_NAME=${VLLM_MODEL_NAME:-auto}
VLLM_API_KEY=${VLLM_API_KEY:-EMPTY}
ENABLE_THINKING=${ENABLE_THINKING:-1}
TEMPERATURE=${TEMPERATURE:-0.0}

if [[ "${VLLM_MODEL_NAME}" == "auto" ]]; then
    VLLM_MODEL_NAME="$(python - "$VLLM_BASE_URL" <<'PY'
import json
import sys
import urllib.request

base_url = sys.argv[1].rstrip("/")
if not base_url.endswith("/v1"):
    base_url = f"{base_url}/v1"

with urllib.request.urlopen(f"{base_url}/models", timeout=10) as response:
    payload = json.load(response)

models = payload.get("data") or []
if not models:
    raise SystemExit("No served models reported by vLLM")

model_id = models[0].get("id")
if not model_id:
    raise SystemExit("Unable to resolve served model ID from vLLM")

print(model_id)
PY
)"
fi

MODEL_SLUG="$(python - "$VLLM_MODEL_NAME" <<'PY'
import pathlib
import re
import sys

model_name = sys.argv[1].rstrip("/")
slug_source = pathlib.Path(model_name).name or model_name
slug = re.sub(r"[^A-Za-z0-9]+", "_", slug_source).strip("_").lower()
print(slug or "vllm_model")
PY
)"

OUTPUT_DIR=${OUTPUT_DIR:-./diagnoses_500_${MODEL_SLUG}}

THINKING_FLAG=()
if [[ "${ENABLE_THINKING}" == "1" ]]; then
    THINKING_FLAG+=(--enable_thinking)
else
    THINKING_FLAG+=(--disable_thinking)
fi

echo "Using vLLM base URL: ${VLLM_BASE_URL}"
echo "Resolved served model: ${VLLM_MODEL_NAME}"
echo "Output directory: ${OUTPUT_DIR}"
echo "EHR MCP URL: ${EHR_MCP_URL}"

python "$SCRIPT_DIR/deploy_agent.py" \
    --backend vllm \
    --model_name_or_path "$VLLM_MODEL_NAME" \
    --api_base_url "$VLLM_BASE_URL" \
    --api_key "$VLLM_API_KEY" \
    --data_path "$DATA_PATH" \
    --output_dir "$OUTPUT_DIR" \
    --enable_ehr \
    --ehr_mcp_url "$EHR_MCP_URL" \
    --temperature "$TEMPERATURE" \
    --runs_per_question "$RUNS_PER_QUESTION" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --max_rounds "$MAX_ROUNDS" \
    "${THINKING_FLAG[@]}" \
    --verbose
