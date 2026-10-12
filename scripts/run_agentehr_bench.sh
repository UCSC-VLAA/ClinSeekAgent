#!/usr/bin/env bash
# End-to-end AgentEHR-Bench evaluation with ClinSeekAgent.
#
# Starts the EHR MCP server against the right benchmark tree (unless one is
# already listening), slices the requested cases out of the split, runs the
# agent loop with the paper settings (16 EHR tools, up to 200 rounds, strict
# temporal policy, reasoning on, temperature 0 where accepted, 8,192 output
# tokens), and stops what it started.
#
# Paper evaluation (ClinSeek-35B-A3B and baselines): 100 questions per task for
# diagnoses_ccs, procedures_ccs, labevents, microbiologyevents and transfers
# from the MIMIC-IV `common` split -- build the fixed sample with
# scripts/build_agentehr_sample.py and pass each task file as CASES_OVERRIDE:
#
#   python scripts/build_agentehr_sample.py --root $AGENTEHR_DATA_ROOT/MIMICIVAgentBench \
#       --out data/agentehr_sample500
#   for t in diagnoses_ccs procedures_ccs labevents microbiologyevents transfers; do
#     SUBSET=mimic4-common TASK=$t CASES_OVERRIDE=data/agentehr_sample500/${t}_100.json \
#     BACKEND=vllm VLLM_MODEL=ClinSeek-35B-A3B OUTPUT_DIR=outputs/agentehr/clinseek/$t \
#       bash scripts/run_agentehr_bench.sh
#   done
#
# Knobs (all optional):
#   SUBSET     mimic3-test | mimic4-common | mimic4-rare | mimic4-sample | mimic4-train
#   TASK       diagnoses_ccs | procedures_ccs | labevents | prescriptions |
#              microbiologyevents | transfers
#   LIMIT      number of cases (0 = the whole split); OFFSET skips cases first
#   CASES_OVERRIDE  JSON/JSONL file with the exact rows to run
#   BACKEND    vllm (default) | vertex | bedrock
#   VLLM_MODEL, VLLM_BASE_URL, VLLM_API_KEY      OpenAI-compatible endpoint
#   VERTEX_MODEL_ID, VERTEX_REGION, VERTEX_PROJECT
#   BEDROCK_MODEL_ID, BEDROCK_REGION
#   ENABLE_BROWSER=1 adds the optional browser tools (not part of the paper setting)
#   AGENTEHR_DATA_ROOT  where scripts/download_agentehr_bench.sh put the data
#   OUTPUT_DIR, MAX_ROUNDS, MAX_CONCURRENCY, RUNS_PER_QUESTION, MAX_TOKENS,
#   TEMPERATURE, ENABLE_THINKING, THINKING_BUDGET, REASONING_EFFORT
#
# Extra run_text.py flags pass straight through ("$@").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Command-line values must outrank .env, but sourcing .env overwrites them --
# stash them first and put them back afterwards.
_stash=(VERTEX_MODEL_ID VERTEX_REGION BACKEND VLLM_MODEL VLLM_BASE_URL VLLM_API_KEY
        BEDROCK_MODEL_ID BEDROCK_REGION AGENTEHR_DATA_ROOT)
declare -A _cli=()
for _v in "${_stash[@]}"; do _cli[${_v}]="${!_v:-}"; done
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a; . "${REPO_ROOT}/.env"; set +a
fi
for _v in "${_stash[@]}"; do
  [[ -n "${_cli[${_v}]}" ]] && printf -v "${_v}" '%s' "${_cli[${_v}]}"
done

SUBSET="${SUBSET:-mimic4-common}"
TASK="${TASK:-diagnoses_ccs}"
LIMIT="${LIMIT:-1}"
OFFSET="${OFFSET:-0}"

BACKEND="${BACKEND:-vllm}"
ENABLE_BROWSER="${ENABLE_BROWSER:-0}"
VLLM_MODEL="${VLLM_MODEL:-auto}"
VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:4000/v1}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
VERTEX_MODEL_ID="${VERTEX_MODEL_ID:-claude-opus-4-6}"
VERTEX_REGION="${VERTEX_REGION:-${CLOUD_ML_REGION:-us-east5}}"
VERTEX_PROJECT="${VERTEX_PROJECT:-${ANTHROPIC_VERTEX_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}"
BEDROCK_REGION="${BEDROCK_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

AGENTEHR_DATA_ROOT="${AGENTEHR_DATA_ROOT:-${REPO_ROOT}/data/AgentEHR-Bench}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/outputs/agentehr_${SUBSET}_${TASK}}"
MAX_ROUNDS="${MAX_ROUNDS:-200}"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-4}"
RUNS_PER_QUESTION="${RUNS_PER_QUESTION:-1}"
MAX_TOOL_RESULT_CHARS="${MAX_TOOL_RESULT_CHARS:-100000}"
MAX_TOKENS="${MAX_TOKENS:-8192}"
TEMPERATURE="${TEMPERATURE:-0.0}"
ENABLE_THINKING="${ENABLE_THINKING:-1}"
THINKING_BUDGET="${THINKING_BUDGET:-4096}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"

EHR_MCP_HOST="${EHR_MCP_HOST:-127.0.0.1}"
EHR_MCP_PORT="${EHR_MCP_PORT:-5003}"
EHR_MCP_URL="http://${EHR_MCP_HOST}:${EHR_MCP_PORT}/mcp"

PYTHON_BIN="${PYTHON_BIN:-python}"
MCP_PYTHON_BIN="${MCP_PYTHON_BIN:-${PYTHON_BIN}}"

# -- Resolve the benchmark tree ---------------------------------------------------
case "${SUBSET}" in
  mimic3-test)   BENCH_DIR="MIMICIIIAgentBench"; SPLIT="test" ;;
  mimic4-common) BENCH_DIR="MIMICIVAgentBench";  SPLIT="common" ;;
  mimic4-rare)   BENCH_DIR="MIMICIVAgentBench";  SPLIT="rare" ;;
  mimic4-sample) BENCH_DIR="MIMICIVAgentBench";  SPLIT="sample" ;;
  mimic4-train)  BENCH_DIR="MIMICIVAgentBench";  SPLIT="train" ;;
  *) echo "ERROR: unknown SUBSET '${SUBSET}'" >&2; exit 1 ;;
esac

EHR_DATA_PATH="${AGENTEHR_DATA_ROOT}/${BENCH_DIR}"
if [[ ! -d "${EHR_DATA_PATH}/database" ]]; then
  echo "ERROR: ${EHR_DATA_PATH}/database not found." >&2
  echo "       Run: SUBSET=${BENCH_DIR} bash scripts/download_agentehr_bench.sh" >&2
  exit 1
fi
case "${BACKEND}" in
  vertex)
    if [[ -z "${VERTEX_PROJECT}" ]]; then
      echo "ERROR: no GCP project. Set ANTHROPIC_VERTEX_PROJECT_ID in .env." >&2
      exit 1
    fi
    MODEL_LABEL="${VERTEX_MODEL_ID} @ ${VERTEX_REGION} (project=${VERTEX_PROJECT})"
    BACKEND_ARGS=(--backend vertex
                  --vertex_model_id "${VERTEX_MODEL_ID}"
                  --vertex_project "${VERTEX_PROJECT}"
                  --vertex_region "${VERTEX_REGION}")
    ;;
  bedrock)
    MODEL_LABEL="${BEDROCK_MODEL_ID} @ ${BEDROCK_REGION}"
    BACKEND_ARGS=(--backend bedrock
                  --model_name_or_path "${BEDROCK_MODEL_ID}"
                  --bedrock_region "${BEDROCK_REGION}")
    ;;
  vllm)
    MODEL_LABEL="${VLLM_MODEL} @ ${VLLM_BASE_URL}"
    BACKEND_ARGS=(--backend vllm
                  --model_name_or_path "${VLLM_MODEL}"
                  --api_base_url "${VLLM_BASE_URL}"
                  --api_key "${VLLM_API_KEY}")
    ;;
  *) echo "ERROR: unknown BACKEND '${BACKEND}' (want vllm|vertex|bedrock)" >&2; exit 1 ;;
esac

RUN_ARGS=(--max_tokens "${MAX_TOKENS}" --temperature "${TEMPERATURE}"
          --thinking_budget_tokens "${THINKING_BUDGET}"
          --reasoning_effort "${REASONING_EFFORT}")
if [[ "${ENABLE_THINKING}" == "1" ]]; then
  RUN_ARGS+=(--enable_thinking)
else
  RUN_ARGS+=(--disable_thinking)
fi
[[ "${ENABLE_BROWSER}" == "1" ]] && RUN_ARGS+=(--enable_browser)

# -- Slice the split into a JSONL the driver can read ------------------------------
# Split files carry their case count in the name (diagnoses_ccs_600.json), and
# MIMIC-III spells one task 'prescriptions3', so resolve by prefix rather than
# hardcoding. Cases whose patient DB is missing are skipped, which is what a
# partial download looks like.
mkdir -p "${OUTPUT_DIR}"
CASES_FILE="${OUTPUT_DIR}/cases.jsonl"

# CASES_OVERRIDE pins the exact rows to run (e.g. the fixed paper sample).
# Copied in rather than read in place so the run keeps the case list it
# actually ran, which is what scoring is pointed at.
if [[ -n "${CASES_OVERRIDE:-}" ]]; then
  [[ -f "${CASES_OVERRIDE}" ]] || {
    echo "ERROR: CASES_OVERRIDE not found: ${CASES_OVERRIDE}" >&2; exit 1; }
  "${PYTHON_BIN}" - "${CASES_OVERRIDE}" "${CASES_FILE}" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read().strip()
recs = json.loads(text) if text[:1] == "[" else [
    json.loads(l) for l in text.splitlines() if l.strip()]
with open(dst, "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"cases  : {len(recs)} from {src}")
PY
else
EHR_DATA_PATH="${EHR_DATA_PATH}" SPLIT="${SPLIT}" TASK="${TASK}" \
LIMIT="${LIMIT}" OFFSET="${OFFSET}" CASES_FILE="${CASES_FILE}" \
"${PYTHON_BIN}" - <<'PY'
import glob
import json
import os
import sys

root = os.environ["EHR_DATA_PATH"]
split, task = os.environ["SPLIT"], os.environ["TASK"]
limit, offset = int(os.environ["LIMIT"]), int(os.environ["OFFSET"])

matches = sorted(glob.glob(f"{root}/{split}/{task}_*.json")
                 or glob.glob(f"{root}/{split}/{task}3_*.json"))
if not matches:
    available = sorted(os.path.basename(p) for p in
                       glob.glob(f"{root}/{split}/*.json"))
    sys.exit(f"ERROR: no '{task}' file in {root}/{split}. Available: {available}")
if len(matches) > 1:
    print(f"note: {len(matches)} files match '{task}'; using "
          f"{os.path.basename(matches[0])}")

records = json.load(open(matches[0]))
records = records[offset:]
kept, skipped = [], 0
for rec in records:
    if not os.path.exists(f"{root}/database/patient_{rec['subject_id']}.db"):
        skipped += 1
        continue
    kept.append(rec)
    if limit and len(kept) >= limit:
        break

if not kept:
    sys.exit(f"ERROR: no usable cases (skipped {skipped} with a missing "
             f"patient DB -- is the download complete?)")
with open(os.environ["CASES_FILE"], "w", encoding="utf-8") as f:
    for rec in kept:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(f"source : {os.path.basename(matches[0])} ({len(records) + offset} cases)")
print(f"using  : {len(kept)} case(s)"
      + (f", skipped {skipped} with no patient DB" if skipped else ""))
PY
fi

# -- EHR MCP server ------------------------------------------------------------------
MCP_PID=""
cleanup() {
  if [[ -n "${MCP_PID}" ]] && kill -0 "${MCP_PID}" 2>/dev/null; then
    echo "stopping EHR MCP server (pid ${MCP_PID})"
    kill "${MCP_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if curl -sf -o /dev/null --max-time 2 "http://${EHR_MCP_HOST}:${EHR_MCP_PORT}/mcp" \
   || nc -z "${EHR_MCP_HOST}" "${EHR_MCP_PORT}" 2>/dev/null; then
  # A running server cannot be asked which tree it loaded, and pointing a
  # MIMIC-IV run at a MIMIC-III server fails as "patient DB not found" deep in
  # the loop rather than up front. Say so instead of assuming.
  echo "WARNING: something is already listening on ${EHR_MCP_URL}; reusing it."
  echo "         Confirm it serves ${EHR_DATA_PATH}, or set EHR_MCP_PORT to"
  echo "         start a separate one."
else
  MCP_LOG="${OUTPUT_DIR}/ehr_mcp.log"
  echo "starting EHR MCP server on ${EHR_MCP_URL} (log: ${MCP_LOG})"
  EHR_DATA_PATH="${EHR_DATA_PATH}" EHR_MCP_HOST="${EHR_MCP_HOST}" \
  EHR_MCP_PORT="${EHR_MCP_PORT}" PYTHON_BIN="${MCP_PYTHON_BIN}" \
    bash "${SCRIPT_DIR}/run_ehr_mcp.sh" > "${MCP_LOG}" 2>&1 &
  MCP_PID=$!

  # The server loads the reference and candidate tables before it binds, which
  # takes a few seconds on the MIMIC-IV tree.
  for _ in $(seq 1 60); do
    grep -q "Application startup complete" "${MCP_LOG}" 2>/dev/null && break
    kill -0 "${MCP_PID}" 2>/dev/null || { echo "ERROR: server died:" >&2
                                          tail -20 "${MCP_LOG}" >&2; exit 1; }
    sleep 1
  done
  grep -q "Application startup complete" "${MCP_LOG}" 2>/dev/null || {
    echo "ERROR: server did not come up in 60s:" >&2; tail -20 "${MCP_LOG}" >&2
    exit 1
  }
fi

# -- Run -------------------------------------------------------------------------------
export PYTHONPATH="${REPO_ROOT}/clinseekagent:${REPO_ROOT}/src:${PYTHONPATH:-}"

echo
echo "subset : ${SUBSET} / ${TASK}"
echo "data   : ${EHR_DATA_PATH}"
echo "model  : ${MODEL_LABEL}"
echo "tools  : EHR MCP$([[ "${ENABLE_BROWSER}" == "1" ]] && echo ' + browser')"
echo "out    : ${OUTPUT_DIR}"
echo

"${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/run_text.py" \
  "${BACKEND_ARGS[@]}" \
  "${RUN_ARGS[@]}" \
  --data_path "${CASES_FILE}" \
  --output_dir "${OUTPUT_DIR}" \
  --enable_ehr \
  --ehr_mcp_url "${EHR_MCP_URL}" \
  --max_rounds "${MAX_ROUNDS}" \
  --max_concurrency "${MAX_CONCURRENCY}" \
  --runs_per_question "${RUNS_PER_QUESTION}" \
  --max_tool_result_chars "${MAX_TOOL_RESULT_CHARS}" \
  "$@"

echo
echo "results : ${OUTPUT_DIR}/results.jsonl"
echo "score   : ${PYTHON_BIN} clinseekagent/scoring/score_text.py --results ${OUTPUT_DIR}/results.jsonl --benchmark ${CASES_FILE} --output-dir ${OUTPUT_DIR}/scores"
echo "render  : ${PYTHON_BIN} scripts/render_trajectory.py ${OUTPUT_DIR}/results.jsonl"
