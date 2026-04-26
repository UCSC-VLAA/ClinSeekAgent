#!/usr/bin/env bash
# ============================================================================
# Multimodal EHR tool-calling pipeline — unified launcher
# ============================================================================
# Purpose:
#   End-to-end driver for Claude Opus 4.6 (Bedrock) with:
#     - EHR MCP tools (patient DB queries)
#     - 6 Meissa image tools (CXR classifier, grounding, segmentation, ...)
#     - Browser tools (optional; disabled unless SERPER_API_KEY is set)
#     - Native multimodal input (images + reports inlined as Anthropic blocks)
#
# The text-only pipeline in deploy_agent.py / run.sh is NEVER touched.
#
# What this script does:
#   1. (optional) Start two EHR MCP servers — one per benchmark root:
#          - EHRXQA   on :5103, pointing at EHRXQAAgentBench_v3
#          - MedMod   on :5104, pointing at MedModAgentBench_v3
#   2. (optional) Start the medical-image MCP server on :5203.
#      Requires torchxrayvision + transformers + pydicom + matplotlib +
#      MAIRA-2 / PSPNet model weights. If --no-image is passed, tools stay
#      routable but return "not available"; Claude still sees the image via
#      the base64 content block we attach.
#   3. Invoke deploy_agent_mm.py with BOTH benchmark roots so image paths
#      resolve regardless of which row they came from.
#
# Typical invocations:
#
#   # Default: 2,703-row prepared test set.
#   bash run_mm_pipeline.sh
#
#   # The full EHRXQA + MedMod test (all 2,220 + 392,811 = 395,031 rows)
#   bash run_mm_pipeline.sh --mode full
#
#   # Re-use already-running MCP servers (don't start or kill them)
#   bash run_mm_pipeline.sh --no-start-mcp
#
#   # Skip the image MCP (routing stays — tools just error out)
#   bash run_mm_pipeline.sh --no-image
#
#   # Limit samples for a smoke test
#   bash run_mm_pipeline.sh --mode prepared --limit 20
#
# Environment overrides (all optional; defaults shown):
#   BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1
#   BEDROCK_REGION=us-east-1
#   BEDROCK_API_KEY   (or AWS_BEARER_TOKEN_BEDROCK / default AWS chain)
#   SERPER_API_KEY    (optional, enables browser.search backend)
#   MAX_ROUNDS=200
#   MAX_CONCURRENCY=6
#   RUNS_PER_QUESTION=1
#   MAX_TOOL_RESULT_CHARS=100000
#   IMAGE_MAX_EDGE=1568
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Defaults --------------------------------------------------------------
MODE="prepared"                 # prepared | full | full-ehrxqa | full-medmod
START_MCP=1                     # whether to start EHR MCPs
START_IMAGE=1                   # whether to start the image MCP
ENABLE_IMAGE=1                  # whether to route image.* calls at all
LIMIT=0                         # 0 = no limit; N = truncate to first N rows

BACKEND=${BACKEND:-bedrock}     # bedrock | vllm
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}
BEDROCK_REGION=${BEDROCK_REGION:-us-east-1}

# vLLM backend (only used when BACKEND=vllm)
VLLM_API_BASE_URL=${VLLM_API_BASE_URL:-http://127.0.0.1:4000/v1}
VLLM_API_KEY=${VLLM_API_KEY:-EMPTY}
VLLM_MODEL=${VLLM_MODEL:-}      # empty = auto-resolve from /v1/models

MAX_ROUNDS=${MAX_ROUNDS:-200}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-6}
RUNS_PER_QUESTION=${RUNS_PER_QUESTION:-1}
MAX_TOOL_RESULT_CHARS=${MAX_TOOL_RESULT_CHARS:-100000}
IMAGE_MAX_EDGE=${IMAGE_MAX_EDGE:-1568}
ENABLE_THINKING=${ENABLE_THINKING:-0}

# Where the datasets live (must have been extracted; see docs/05_*.md)
REPO_ROOT=${REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}
DATA_BASE=${DATA_BASE:-${REPO_ROOT}/data}
BENCH_ROOT_EHRXQA="${DATA_BASE}/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3"
BENCH_ROOT_MEDMOD="${DATA_BASE}/EHR_multimodal_bench/extracted/MedModAgentBench_v3"
# Combined path passed to deploy_agent_mm.py (colon-separated list).
BENCH_ROOT="${BENCH_ROOT_EHRXQA}:${BENCH_ROOT_MEDMOD}"

# MCP server endpoints (override env to run multiple pipelines on same host)
EHR_EHRXQA_PORT=${EHR_EHRXQA_PORT:-5103}
EHR_MEDMOD_PORT=${EHR_MEDMOD_PORT:-5104}
IMAGE_PORT=${IMAGE_PORT:-5203}
EHR_MCP_URL_EHRXQA=${EHR_MCP_URL_EHRXQA:-http://127.0.0.1:${EHR_EHRXQA_PORT}/mcp}
EHR_MCP_URL_MEDMOD=${EHR_MCP_URL_MEDMOD:-http://127.0.0.1:${EHR_MEDMOD_PORT}/mcp}
IMAGE_MCP_URL=${IMAGE_MCP_URL:-http://127.0.0.1:${IMAGE_PORT}/mcp}

# Dataset paths
PREPARED_TEST_SET="${DATA_BASE}/EHR_multimodal_bench_tests/combined_test_set.jsonl"
FULL_EHRXQA_TEST="${BENCH_ROOT_EHRXQA}/common/ready/test.json"
FULL_MEDMOD_TEST="${BENCH_ROOT_MEDMOD}/common/ready/test.json"

# Python / virtualenv — override per-role before running if needed.
#   PYBIN       : agent driver + scorer (CPU-only, talks to MCPs via HTTP)
#   EHR_PYBIN   : EHR MCP server (needs sentence-transformers for BioLORD)
#   IMAGE_PYBIN : image MCP server (pinned transformers==4.46.x for MAIRA-2)
PYBIN=${PYBIN:-${REPO_ROOT}/venvs/deploy_agent/bin/python}
EHR_PYBIN=${EHR_PYBIN:-${REPO_ROOT}/venvs/mcp_ehr/bin/python}
IMAGE_PYBIN=${IMAGE_PYBIN:-${REPO_ROOT}/venvs/mcp_image/bin/python}

# ---- CLI parsing -----------------------------------------------------------
usage() {
    sed -n '1,/^# =\{10,\}$/!{/^$/q; p;}' "$0" | sed -n '1,/^$/p'
    cat <<EOF
Usage: $0 [--mode prepared|full|full-ehrxqa|full-medmod] [--limit N]
         [--no-start-mcp] [--no-image]
         [--output-dir DIR] [--data-path PATH]
EOF
}

OUTPUT_DIR=""
DATA_PATH_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)            MODE="$2"; shift 2 ;;
        --limit)           LIMIT="$2"; shift 2 ;;
        --no-start-mcp)    START_MCP=0; shift ;;
        --no-image)        ENABLE_IMAGE=0; START_IMAGE=0; shift ;;
        --no-start-image)  START_IMAGE=0; shift ;;
        --output-dir)      OUTPUT_DIR="$2"; shift 2 ;;
        --data-path)       DATA_PATH_OVERRIDE="$2"; shift 2 ;;
        -h|--help)         usage; exit 0 ;;
        *)                 echo "Unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

# ---- Choose input file based on mode ---------------------------------------
case "$MODE" in
    prepared)      DATA_PATH="$PREPARED_TEST_SET" ;;
    full)          DATA_PATH="${DATA_PATH_OVERRIDE:-$PREPARED_TEST_SET}"
                   # "full" by default runs both test sets back-to-back; see below.
                   ;;
    full-ehrxqa)   DATA_PATH="$FULL_EHRXQA_TEST" ;;
    full-medmod)   DATA_PATH="$FULL_MEDMOD_TEST" ;;
    *)             echo "Unknown --mode: $MODE" >&2; usage; exit 2 ;;
esac
[[ -n "$DATA_PATH_OVERRIDE" ]] && DATA_PATH="$DATA_PATH_OVERRIDE"

if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="./results/mm_${MODE}_$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "$OUTPUT_DIR"

# ---- Truncated-copy helper for --limit -------------------------------------
if [[ "$LIMIT" -gt 0 && -n "$DATA_PATH" && -f "$DATA_PATH" ]]; then
    LIMITED="$OUTPUT_DIR/_input_limit${LIMIT}.jsonl"
    "$PYBIN" - "$DATA_PATH" "$LIMITED" "$LIMIT" <<'PY'
import json, sys
src, dst, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(src) as f:
    if src.endswith(".jsonl"):
        rows = [next(f) for _ in range(n)]
    else:
        rows = [json.dumps(r, ensure_ascii=False) + "\n" for r in json.load(f)[:n]]
with open(dst, "w") as g:
    g.writelines(rows)
print(f"wrote {n} rows to {dst}")
PY
    DATA_PATH="$LIMITED"
fi

# ---- Credentials -----------------------------------------------------------
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$BEDROCK_REGION}"
export AWS_BEARER_TOKEN_BEDROCK="${AWS_BEARER_TOKEN_BEDROCK:-${BEDROCK_API_KEY:-}}"
if [[ -z "${SERPER_API_KEY:-}" ]]; then
    # Leave browser in "local" mode (it'll error on search attempts, which is
    # fine — Claude can still answer from images / EHR).
    :
else
    export SERPER_API_KEY
fi

# ---- MCP servers: start or verify -----------------------------------------
MCP_PIDS=()
trap 'for p in "${MCP_PIDS[@]}"; do kill "$p" 2>/dev/null || true; done' EXIT

start_ehr_mcp() {
    local port="$1" root="$2" tag="$3" gpu="$4"
    local log="$OUTPUT_DIR/mcp_${tag}.log"
    CUDA_VISIBLE_DEVICES="$gpu" \
    "$EHR_PYBIN" "${REPO_ROOT}/src/run_mcp_server.py" \
        --mode http --host 127.0.0.1 --port "$port" \
        --data_path "$root" \
        --disable-knowledge-tools \
        > "$log" 2>&1 &
    MCP_PIDS+=("$!")
    echo "  started EHR MCP ($tag) on :$port, gpu=$gpu, pid=$!, log=$log"
}

start_image_mcp() {
    local log="$OUTPUT_DIR/mcp_image.log"
    # Per-tool GPU pinning — spread the 4 GPU-backed tools across 4 GPUs by
    # default. Override any `IMAGE_TOOL_DEVICE_*` env var to relocate a tool.
    # The CPU-only tools (image_visualizer, dicom_processor) don't touch CUDA.
    BENCH_ROOT="$BENCH_ROOT" \
    HF_TOKEN="${HF_TOKEN:-$(cat ~/.cache/huggingface/token 2>/dev/null || true)}" \
    HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-1}" \
    CUDA_VISIBLE_DEVICES="${IMAGE_CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}" \
    IMAGE_TOOL_DEVICE_CLASSIFIER="${IMAGE_TOOL_DEVICE_CLASSIFIER:-cuda:2}" \
    IMAGE_TOOL_DEVICE_REPORT_GENERATOR="${IMAGE_TOOL_DEVICE_REPORT_GENERATOR:-cuda:3}" \
    IMAGE_TOOL_DEVICE_GROUNDING="${IMAGE_TOOL_DEVICE_GROUNDING:-cuda:4}" \
    IMAGE_TOOL_DEVICE_SEGMENTATION="${IMAGE_TOOL_DEVICE_SEGMENTATION:-cuda:5}" \
    "$IMAGE_PYBIN" "${REPO_ROOT}/src/mcp_image/run_image_mcp_server.py" \
        --mode http --host 127.0.0.1 --port "$IMAGE_PORT" \
        > "$log" 2>&1 &
    MCP_PIDS+=("$!")
    echo "  started image MCP on :$IMAGE_PORT, pid=$!, log=$log"
    echo "    tool→GPU: classifier=cuda:2, report_generator=cuda:3, grounding=cuda:4, segmentation=cuda:5"
}

wait_for_port() {
    local port="$1" name="$2" timeout="${3:-120}"
    # Prefer a real TCP connect via python — `ss` fails silently in restricted
    # shells (permission to read /proc/net/tcp denied).
    for ((i=0; i<timeout; i++)); do
        if "$PYBIN" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', $port)); sys.exit(0)
except Exception: sys.exit(1)" 2>/dev/null; then
            echo "  $name listening on :$port"
            return 0
        fi
        sleep 1
    done
    echo "  WARN: $name did not come up on :$port within ${timeout}s" >&2
    return 1
}

if [[ "$START_MCP" == "1" ]]; then
    echo ">>> Starting EHR MCP servers"
    start_ehr_mcp "$EHR_EHRXQA_PORT" "$BENCH_ROOT_EHRXQA" ehrxqa "${EHR_EHRXQA_GPU:-0}"
    start_ehr_mcp "$EHR_MEDMOD_PORT" "$BENCH_ROOT_MEDMOD" medmod "${EHR_MEDMOD_GPU:-1}"
    wait_for_port "$EHR_EHRXQA_PORT" "EHRXQA MCP" || true
    wait_for_port "$EHR_MEDMOD_PORT" "MedMod MCP" || true
fi
if [[ "$START_IMAGE" == "1" ]]; then
    echo ">>> Starting image MCP server"
    start_image_mcp
    # First-run MAIRA-2 download can take several minutes (14 GB across 6 shards),
    # so give the image MCP a long wait window. Subsequent runs hit the HF cache
    # and come up in <30s.
    wait_for_port "$IMAGE_PORT" "Image MCP" "${IMAGE_WAIT_TIMEOUT:-1200}" || true
fi

# ---- Thinking flag ---------------------------------------------------------
THINKING_FLAG=(--disable_thinking)
[[ "$ENABLE_THINKING" == "1" ]] && THINKING_FLAG=(--enable_thinking)

IMAGE_FLAG=()
[[ "$ENABLE_IMAGE" == "1" ]] && IMAGE_FLAG=(--enable_image --image_mcp_url "$IMAGE_MCP_URL")

# ---- Run -------------------------------------------------------------------
BACKEND_FLAGS=(--backend "$BACKEND")
if [[ "$BACKEND" == "bedrock" ]]; then
    BACKEND_FLAGS+=(
        --bedrock_model_id "$BEDROCK_MODEL_ID"
        --bedrock_region "$BEDROCK_REGION"
    )
    BACKEND_SUMMARY="bedrock:$BEDROCK_MODEL_ID @ $BEDROCK_REGION"
elif [[ "$BACKEND" == "vllm" ]]; then
    BACKEND_FLAGS+=(
        --api_base_url "$VLLM_API_BASE_URL"
        --api_key     "$VLLM_API_KEY"
    )
    [[ -n "$VLLM_MODEL" ]] && BACKEND_FLAGS+=(--model_name_or_path "$VLLM_MODEL")
    BACKEND_SUMMARY="vllm:${VLLM_MODEL:-auto} @ $VLLM_API_BASE_URL"
else
    echo "Unsupported BACKEND=$BACKEND (must be 'bedrock' or 'vllm')" >&2
    exit 2
fi

run_agent() {
    local data="$1" out="$2"
    mkdir -p "$out"
    echo ""
    echo ">>> Running deploy_agent_mm.py"
    echo "    data:          $data"
    echo "    output:        $out"
    echo "    backend:       $BACKEND_SUMMARY"
    echo "    max_rounds:    $MAX_ROUNDS"
    echo "    concurrency:   $MAX_CONCURRENCY"
    echo "    runs/question: $RUNS_PER_QUESTION"
    echo "    image_enabled: $ENABLE_IMAGE"
    echo "    bench_root:    $BENCH_ROOT"
    "$PYBIN" "$SCRIPT_DIR/deploy_agent_mm.py" \
        --data_path "$data" \
        --output_dir "$out" \
        "${BACKEND_FLAGS[@]}" \
        --enable_ehr \
        --ehr_mcp_url "$EHR_MCP_URL_EHRXQA" \
        --ehr_mcp_url_ehrxqa "$EHR_MCP_URL_EHRXQA" \
        --ehr_mcp_url_medmod "$EHR_MCP_URL_MEDMOD" \
        "${IMAGE_FLAG[@]}" \
        --bench_root "$BENCH_ROOT" \
        --image_max_edge "$IMAGE_MAX_EDGE" \
        --max_rounds "$MAX_ROUNDS" \
        --max_concurrency "$MAX_CONCURRENCY" \
        --runs_per_question "$RUNS_PER_QUESTION" \
        --max_tool_result_chars "$MAX_TOOL_RESULT_CHARS" \
        "${THINKING_FLAG[@]}" \
        --verbose \
        2>&1 | tee "$out/run.log"
}

case "$MODE" in
    prepared | full-ehrxqa | full-medmod)
        run_agent "$DATA_PATH" "$OUTPUT_DIR"
        ;;
    full)
        # Two back-to-back runs, each benchmark's test set in full.
        run_agent "$FULL_EHRXQA_TEST"  "$OUTPUT_DIR/ehrxqa"
        run_agent "$FULL_MEDMOD_TEST"  "$OUTPUT_DIR/medmod"
        ;;
esac

echo ""
echo ">>> Done. Results in: $OUTPUT_DIR"
