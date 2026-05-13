#!/usr/bin/env bash
# Single-turn QA evaluation on the Multimodal EHR Benchmark.
# No MCP servers needed — input_text already contains pre-rendered EHR context.
# Images are loaded from disk and sent as base64 to vLLM's multimodal API.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -n "${OUTPUT_DIR:-}" && "${OUTPUT_DIR}" != /* ]] && OUTPUT_DIR="$(pwd)/${OUTPUT_DIR}"
cd "$SCRIPT_DIR"

# ---- Defaults --------------------------------------------------------------
DATA_PATH=${DATA_PATH:-../data/EHR_multimodal_bench/EHR_multimodal_bench_tests/model_ready_combined_test_set.jsonl}
BENCH_ROOT=${BENCH_ROOT:-../data/EHR_multimodal_bench/extracted}
IMAGE_MAX_EDGE=${IMAGE_MAX_EDGE:-1568}

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

# ---- Auto-detect served model -----------------------------------------------
if [[ "${VLLM_MODEL_NAME}" == "auto" ]]; then
    VLLM_MODEL_NAME="$(python - "$VLLM_BASE_URL" <<'PY'
import json, sys, urllib.request
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

# ---- Slugs for output dir ---------------------------------------------------
MODEL_SLUG="$(python - "$VLLM_MODEL_NAME" <<'PY'
import pathlib, re, sys
model_name = sys.argv[1].rstrip("/")
slug_source = pathlib.Path(model_name).name or model_name
slug = re.sub(r"[^A-Za-z0-9]+", "_", slug_source).strip("_").lower()
print(slug or "vllm_model")
PY
)"

DATA_SLUG="$(python - "$DATA_PATH" <<'PY'
import pathlib, re, sys
path = sys.argv[1]
stem = pathlib.Path(path).stem or "mm_bench"
slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").lower()
print(slug or "mm_bench")
PY
)"

OUTPUT_DIR=${OUTPUT_DIR:-./results/mm_qa_${DATA_SLUG}_${MODEL_SLUG}}
mkdir -p "$OUTPUT_DIR"

# ---- Logging ----------------------------------------------------------------
LOG_TIMESTAMP=${EVAL_MM_QA_LOG_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_FILE=${EVAL_MM_QA_LOG_FILE:-"${OUTPUT_DIR}/eval_mm_qa_${LOG_TIMESTAMP}.log"}

if [[ "${EVAL_MM_QA_LOGGING_INITIALIZED:-0}" != "1" ]]; then
    export EVAL_MM_QA_LOGGING_INITIALIZED=1
    export EVAL_MM_QA_LOG_FILE="$LOG_FILE"
    exec > >(tee -a "$EVAL_MM_QA_LOG_FILE") 2>&1
fi

# ---- Flags ------------------------------------------------------------------
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

# ---- Summary ----------------------------------------------------------------
echo "Mode: single-turn multimodal QA (no MCP, no agent)"
echo "Using vLLM base URL: ${VLLM_BASE_URL}"
echo "Resolved served model: ${VLLM_MODEL_NAME}"
echo "Data path: ${DATA_PATH}"
echo "Bench root: ${BENCH_ROOT}"
echo "Output directory: ${OUTPUT_DIR}"
echo "Log file: ${LOG_FILE}"
echo "Max concurrency: ${MAX_CONCURRENCY}"
echo "Max tokens per call: ${MAX_TOKENS}"
echo "Temperature: ${TEMPERATURE}"
echo "Enable thinking: ${ENABLE_THINKING}"
echo "Image max edge: ${IMAGE_MAX_EDGE}"
echo "Start index: ${START_INDEX}"
[[ -n "${LIMIT}" ]] && echo "Limit: ${LIMIT}"

# ---- Run --------------------------------------------------------------------
python "$SCRIPT_DIR/eval_mm_qa.py" \
    --data_path "$DATA_PATH" \
    --output_path "$OUTPUT_DIR/results.jsonl" \
    --bench_root "$BENCH_ROOT" \
    --image_max_edge "$IMAGE_MAX_EDGE" \
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
