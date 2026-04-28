#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -n "${OUTPUT_DIR:-}" && "${OUTPUT_DIR}" != /* ]] && OUTPUT_DIR="$(pwd)/${OUTPUT_DIR}"
cd "$SCRIPT_DIR"

DATA_PATH=${DATA_PATH:-../data/EHR-Bench/ehr_bench_sampled_40_per_task.json}

MAX_CONCURRENCY=${MAX_CONCURRENCY:-8}
MAX_TOKENS=${MAX_TOKENS:-32768}
MAX_RETRIES=${MAX_RETRIES:-2}
RETRY_SLEEP=${RETRY_SLEEP:-2.0}
LIMIT=${LIMIT:-}
START_INDEX=${START_INDEX:-0}

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

DATA_SLUG="$(python - "$DATA_PATH" <<'PY'
import pathlib
import re
import sys

path = sys.argv[1]
stem = pathlib.Path(path).stem or "ehrbench"
slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").lower()
print(slug or "ehrbench")
PY
)"

OUTPUT_DIR=${OUTPUT_DIR:-./results/qa_${DATA_SLUG}_${MODEL_SLUG}}
mkdir -p "$OUTPUT_DIR"

LOG_TIMESTAMP=${EVAL_QA_LOG_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_FILE=${EVAL_QA_LOG_FILE:-"${OUTPUT_DIR}/eval_qa_${LOG_TIMESTAMP}.log"}

if [[ "${EVAL_QA_LOGGING_INITIALIZED:-0}" != "1" ]]; then
    export EVAL_QA_LOGGING_INITIALIZED=1
    export EVAL_QA_LOG_FILE="$LOG_FILE"
    exec > >(tee -a "$EVAL_QA_LOG_FILE") 2>&1
fi


THINKING_FLAG=()
if [[ "${ENABLE_THINKING}" == "1" ]]; then
    THINKING_FLAG+=(--enable_thinking)
else
    THINKING_FLAG+=(--disable_thinking)
fi

LIMIT_FLAG=()
if [[ -n "${LIMIT}" ]]; then
    LIMIT_FLAG+=(--limit "${LIMIT}")
fi

echo "Mode: single-turn QA (no MCP, no agent)"
echo "Using vLLM base URL: ${VLLM_BASE_URL}"
echo "Resolved served model: ${VLLM_MODEL_NAME}"
echo "Data path: ${DATA_PATH}"
echo "Output directory: ${OUTPUT_DIR}"
echo "Log file: ${EVAL_QA_LOG_FILE}"
echo "Max concurrency: ${MAX_CONCURRENCY}"
echo "Max tokens per call: ${MAX_TOKENS}"
echo "Temperature: ${TEMPERATURE}"
echo "Enable thinking: ${ENABLE_THINKING}"
echo "Start index: ${START_INDEX}"
[[ -n "${LIMIT}" ]] && echo "Limit: ${LIMIT}"

python "$SCRIPT_DIR/eval_ehrbench_qa.py" \
    --data_path "$DATA_PATH" \
    --output_path "$OUTPUT_DIR/results.jsonl" \
    --vllm_base_url "$VLLM_BASE_URL" \
    --vllm_api_key "$VLLM_API_KEY" \
    --model_name "$VLLM_MODEL_NAME" \
    --temperature "$TEMPERATURE" \
    --max_tokens "$MAX_TOKENS" \
    --max_concurrency "$MAX_CONCURRENCY" \
    --max_retries "$MAX_RETRIES" \
    --retry_sleep "$RETRY_SLEEP" \
    --start_index "$START_INDEX" \
    "${LIMIT_FLAG[@]}" \
    "${THINKING_FLAG[@]}"
