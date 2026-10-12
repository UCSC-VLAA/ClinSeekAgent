#!/usr/bin/env bash
# The four inference modes of the paper on one benchmark and one model.
#
#   mode            runner                      tools               rounds  record in the prompt
#   agentic         run_text.py / run_multimodal.py   EHR (+image for MM)  <=200   none (retrieved by tools)
#   single_shot     same, --retrieval_mode single_shot same              1 + answer  none (one retrieval turn)
#   recent_record   run_*_curated.py --view recent     none                1       Curated Input
#   full_record     run_*_curated.py --view full       none                1       Full-Record Input (100k window)
#
# agentic = ClinSeekAgent, single_shot = Single-Pass Retrieval. The agentic
# modes read the benchmark manifest and reach the record through live MCP
# servers; the record modes read a corpus built by
# scripts/clean_data/build_evidence_corpus.py. All four share one temporal
# policy and the same reasoning / output-token settings.
#
#   BENCH=text MODES="agentic recent_record" BACKEND=vertex MODEL=claude-opus-4-6 \
#     TEXT_DATA=/path/ClinSeek-Bench_text.json TEXT_CORPUS=/path/text_corpus \
#     bash scripts/run_four_modes.sh
#
#   BENCH=mm MODES="agentic single_shot recent_record full_record" \
#     BACKEND=bedrock MODEL=us.anthropic.claude-opus-4-6-v1 \
#     MM_DATA=/path/ClinSeek-Bench_multimodal.jsonl MM_CORPUS=/path/mm_corpus \
#     BENCH_ROOT=/path/mm_bench/ehrxqa:/path/mm_bench/medmod bash scripts/run_four_modes.sh
#
# Knobs (env): BENCH (text|mm), MODES, BACKEND (bedrock|vertex|vllm), MODEL,
#   MODEL_TAG, OUTPUT_BASE, TEXT_DATA, MM_DATA, TEXT_CORPUS, MM_CORPUS,
#   BENCH_ROOT, AGENTIC_TOOLS (mm: ehrimg = 22 tools [paper], ehr = 16),
#   EHR_MCP_URL, EHR_MCP_URL_EHRXQA, EHR_MCP_URL_MEDMOD, IMAGE_MCP_URL,
#   VLLM_BASE_URL, VLLM_API_KEY, VERTEX_PROJECT, VERTEX_REGION, BEDROCK_REGION,
#   CONCURRENCY, MAX_TOKENS, THINKING_BUDGET, REASONING_EFFORT, TEMPERATURE,
#   MAX_CONTEXT_WINDOW, LIMIT (record modes only; for an agentic smoke test
#   point TEXT_DATA / MM_DATA at a small subset file).
#
# A finished arm (usage.json present) is skipped; a partial arm resumes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BENCH="${BENCH:-text}"
MODES="${MODES:-${MODE:-agentic}}"
BACKEND="${BACKEND:-bedrock}"
MODEL="${MODEL:?Set MODEL to the model id for BACKEND (Bedrock model id, Vertex model id, or served model name)}"
MODEL_TAG="${MODEL_TAG:-$(printf '%s' "${MODEL}" | tr -c 'A-Za-z0-9._-' '_')}"
OUTPUT_BASE="${OUTPUT_BASE:-${REPO_ROOT}/outputs/four_modes}"
PYTHON_BIN="${PYTHON_BIN:-python}"

# Paper inference settings.
MAX_CONTEXT_WINDOW="${MAX_CONTEXT_WINDOW:-100000}"
MAX_TOKENS="${MAX_TOKENS:-8192}"
THINKING_BUDGET="${THINKING_BUDGET:-4096}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"
TEMPERATURE="${TEMPERATURE:-0.0}"
MAX_ROUNDS="${MAX_ROUNDS:-200}"
MAX_TOOL_RESULT_CHARS="${MAX_TOOL_RESULT_CHARS:-100000}"
CONCURRENCY="${CONCURRENCY:-6}"
LIMIT="${LIMIT:-0}"

EHR_MCP_URL="${EHR_MCP_URL:-http://127.0.0.1:5003/mcp}"
EHR_MCP_URL_EHRXQA="${EHR_MCP_URL_EHRXQA:-http://127.0.0.1:5004/mcp}"
EHR_MCP_URL_MEDMOD="${EHR_MCP_URL_MEDMOD:-http://127.0.0.1:5005/mcp}"
IMAGE_MCP_URL="${IMAGE_MCP_URL:-http://127.0.0.1:5203/mcp}"
VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:4000/v1}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
BEDROCK_REGION="${BEDROCK_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
VERTEX_REGION="${VERTEX_REGION:-${CLOUD_ML_REGION:-us-east5}}"
VERTEX_PROJECT="${VERTEX_PROJECT:-${ANTHROPIC_VERTEX_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}}"

case "${BENCH}" in
  text) CORPUS="${TEXT_CORPUS:-}"; DATA="${TEXT_DATA:-}" ;;
  mm)   CORPUS="${MM_CORPUS:-}";   DATA="${MM_DATA:-}" ;;
  *) echo "ERROR: BENCH must be 'text' or 'mm', got '${BENCH}'" >&2; exit 2 ;;
esac
AGENTIC_TOOLS="${AGENTIC_TOOLS:-$([[ "${BENCH}" == "mm" ]] && echo ehrimg || echo ehr)}"
case "${AGENTIC_TOOLS}" in
  ehr|ehrimg) ;;
  *) echo "ERROR: AGENTIC_TOOLS must be 'ehr' or 'ehrimg'" >&2; exit 2 ;;
esac
case "${BACKEND}" in
  bedrock|vertex|vllm) ;;
  *) echo "ERROR: BACKEND must be bedrock, vertex or vllm" >&2; exit 2 ;;
esac

NEEDS_CORPUS=0; NEEDS_AGENTIC=0
for _m in ${MODES}; do
  case "${_m}" in
    recent_record|full_record) NEEDS_CORPUS=1 ;;
    agentic|single_shot)       NEEDS_AGENTIC=1 ;;
    *) echo "ERROR: unknown mode '${_m}'" >&2; exit 2 ;;
  esac
done
if [[ "${NEEDS_CORPUS}" == "1" && ! -d "${CORPUS}" ]]; then
  echo "ERROR: record modes need a corpus root (TEXT_CORPUS / MM_CORPUS), got '${CORPUS}'." >&2
  exit 1
fi
if [[ "${NEEDS_AGENTIC}" == "1" ]]; then
  if [[ ! -f "${DATA}" ]]; then
    echo "ERROR: agentic modes need the benchmark manifest (TEXT_DATA / MM_DATA), got '${DATA}'." >&2
    exit 1
  fi
  # A dead MCP server does not fail fast -- every tool call returns an error
  # string and the run completes with uniformly empty evidence. Check first.
  _urls="${EHR_MCP_URL}"
  [[ "${BENCH}" == "mm" ]] && _urls="${EHR_MCP_URL_EHRXQA} ${EHR_MCP_URL_MEDMOD}"
  [[ "${BENCH}" == "mm" && "${AGENTIC_TOOLS}" == "ehrimg" ]] && _urls="${_urls} ${IMAGE_MCP_URL}"
  for _u in ${_urls}; do
    _hostport="${_u#http://}"; _hostport="${_hostport%%/*}"
    if ! (exec 3<>"/dev/tcp/${_hostport%%:*}/${_hostport##*:}") 2>/dev/null; then
      echo "ERROR: no MCP server listening on ${_u}" >&2; exit 1
    fi
  done
fi
if [[ "${BENCH}" == "mm" && -z "${BENCH_ROOT:-}" ]]; then
  echo "ERROR: BENCH_ROOT must list the rebuilt multimodal roots (mm_bench/ehrxqa:mm_bench/medmod)." >&2; exit 1
fi
if [[ "${BACKEND}" == "vertex" && -z "${VERTEX_PROJECT}" ]]; then
  echo "ERROR: BACKEND=vertex needs ANTHROPIC_VERTEX_PROJECT_ID (or VERTEX_PROJECT)." >&2; exit 1
fi

export PYTHONPATH="${REPO_ROOT}/clinseekagent:${REPO_ROOT}/src:${PYTHONPATH:-}"

arm_dir() { echo "${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_$1"; }

run_record_mode() {   # $1 = mode, $2 = view
  local out; out="$(arm_dir "$1")"
  local -a args=(
    --corpus-root "${CORPUS}" --view "$2"
    --backend "${BACKEND}" --model "${MODEL}"
    --output-dir "${out}"
    --max-context-window "${MAX_CONTEXT_WINDOW}"
    --thinking-budget "${THINKING_BUDGET}"
    --reasoning-effort "${REASONING_EFFORT}"
    --max-tokens "${MAX_TOKENS}" --temperature "${TEMPERATURE}"
    --concurrency "${CONCURRENCY}"
  )
  [[ "${LIMIT}" != "0" ]] && args+=(--limit "${LIMIT}")
  case "${BACKEND}" in
    bedrock) args+=(--regions "${BEDROCK_REGION}") ;;
    vertex)  args+=(--vertex-project "${VERTEX_PROJECT}" --vertex-region "${VERTEX_REGION}") ;;
    vllm)    args+=(--api-base-url "${VLLM_BASE_URL}" --api-key "${VLLM_API_KEY}") ;;
  esac
  if [[ "${BENCH}" == "mm" ]]; then
    "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_multimodal_curated.py" \
      "${args[@]}" --bench-root "${BENCH_ROOT}"
  else
    "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_text_curated.py" "${args[@]}"
  fi
}

run_agentic_mode() {   # $1 = mode, $2 = --retrieval_mode value
  local out; out="$(arm_dir "$1")"
  local -a args=(
    --backend "${BACKEND}" --model_name_or_path "${MODEL}"
    --output_dir "${out}"
    --retrieval_mode "$2"
    --enable_ehr
    --temperature "${TEMPERATURE}"
    --max_rounds "${MAX_ROUNDS}"
    --max_concurrency "${CONCURRENCY}"
    --runs_per_question 1
    --max_tool_result_chars "${MAX_TOOL_RESULT_CHARS}"
    --enable_thinking
    --thinking_budget_tokens "${THINKING_BUDGET}"
    --reasoning_effort "${REASONING_EFFORT}"
  )
  case "${BACKEND}" in
    bedrock) args+=(--bedrock_region "${BEDROCK_REGION}") ;;
    vertex)  args+=(--vertex_model_id "${MODEL}" --vertex_project "${VERTEX_PROJECT}"
                    --vertex_region "${VERTEX_REGION}") ;;
    vllm)    args+=(--api_base_url "${VLLM_BASE_URL}" --api_key "${VLLM_API_KEY}") ;;
  esac
  if [[ "${BENCH}" == "mm" ]]; then
    [[ "${AGENTIC_TOOLS}" == "ehrimg" ]] && args+=(--enable_image --image_mcp_url "${IMAGE_MCP_URL}")
    "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_multimodal.py" "${args[@]}" \
      --data_path "${DATA}" \
      --ehr_mcp_url "${EHR_MCP_URL}" \
      --ehr_mcp_url_ehrxqa "${EHR_MCP_URL_EHRXQA}" \
      --ehr_mcp_url_medmod "${EHR_MCP_URL_MEDMOD}" \
      --bench_root "${BENCH_ROOT}"
  else
    "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_text.py" "${args[@]}" \
      --data_path "${DATA}" \
      --ehr_mcp_url "${EHR_MCP_URL}" \
      --max_tokens "${MAX_TOKENS}"
  fi
}

for mode in ${MODES}; do
  out="$(arm_dir "${mode}")"
  echo "================================================================"
  echo "bench=${BENCH} mode=${mode} backend=${BACKEND} model=${MODEL}"
  echo "out=${out}"
  # usage.json is written only after every row is merged, so its presence is
  # the completion marker. Partial arms resume from results.jsonl instead.
  if [[ -f "${out}/usage.json" ]]; then
    echo "  already complete; skipping."
    continue
  fi
  start=$(date +%s)
  case "${mode}" in
    recent_record) run_record_mode recent_record recent ;;
    full_record)   run_record_mode full_record  full   ;;
    agentic)       run_agentic_mode agentic agentic ;;
    single_shot)   run_agentic_mode single_shot single_shot ;;
  esac
  echo "  done in $(( ($(date +%s) - start) / 60 )) min, $(wc -l < "${out}/results.jsonl") rows"
done
