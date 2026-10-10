#!/usr/bin/env bash
# Score the arms written by scripts/run_four_modes.sh and report F1 per task
# category, with the paired difference of every mode against the Curated Input
# arm (paired-bootstrap 95% CI, 4,000 resamples).
#
#   BENCH=text MODEL_TAG=claude-opus-4-6 TEXT_DATA=/path/ClinSeek-Bench_text.json \
#     bash scripts/score_four_modes.sh
#   BENCH=mm MODEL_TAG=... JUDGE_BACKEND=vertex bash scripts/score_four_modes.sh
#
# Text scoring is rule-based. MM routes single-label rows through an LLM judge
# (Claude Sonnet 4.6 in the paper) on JUDGE_BACKEND (bedrock | vertex | vllm);
# multi-label rows are scored by set F1 after sentence-embedding vocabulary
# mapping.
#
# Every arm is checked with scripts/check_results_complete.py first; set
# FORCE_SCORE=1 to score anyway.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BENCH="${BENCH:-text}"
MODEL_TAG="${MODEL_TAG:?Set MODEL_TAG to the tag used by run_four_modes.sh}"
OUTPUT_BASE="${OUTPUT_BASE:-${REPO_ROOT}/outputs/four_modes}"
MODES="${MODES:-recent_record full_record single_shot agentic}"
PYTHON_BIN="${PYTHON_BIN:-python}"
JUDGE_BACKEND="${JUDGE_BACKEND:-bedrock}"
JUDGE_CONCURRENCY="${JUDGE_CONCURRENCY:-10}"
case "${BENCH}" in
  text) EXPECT="${EXPECT:-1800}"
        TEXT_DATA="${TEXT_DATA:?Set TEXT_DATA to the ClinSeek-Bench-Text manifest (gold labels)}" ;;
  mm)   EXPECT="${EXPECT:-989}" ;;
  *) echo "ERROR: BENCH must be 'text' or 'mm'" >&2; exit 2 ;;
esac

export PYTHONPATH="${REPO_ROOT}/clinseekagent:${REPO_ROOT}/src:${PYTHONPATH:-}"

arms=()
for m in ${MODES}; do
  d="${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_${m}"
  [[ -f "${d}/results.jsonl" ]] && arms+=("${m}")
done
if [[ ${#arms[@]} -eq 0 ]]; then
  echo "no arms found under ${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_*" >&2; exit 1
fi

# --- gate -------------------------------------------------------------------
gate_fail=0
for m in "${arms[@]}"; do
  "${PYTHON_BIN}" "${REPO_ROOT}/scripts/check_results_complete.py" \
    "${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_${m}/results.jsonl" --expect "${EXPECT}" \
    || gate_fail=1
done
if [[ "${gate_fail}" == "1" && "${FORCE_SCORE:-0}" != "1" ]]; then
  echo "ERROR: refusing to score incomplete arms (FORCE_SCORE=1 to override)." >&2
  exit 1
fi

# --- score --------------------------------------------------------------------
if [[ "${BENCH}" == "text" ]]; then
  for m in "${arms[@]}"; do
    d="${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_${m}"
    echo "-- text ${m} --"
    "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/scoring/score_text.py" \
      --results "${d}/results.jsonl" --benchmark "${TEXT_DATA}" \
      --output-dir "${d}/scores" | tail -20
  done
else
  mm_args=()
  for m in "${arms[@]}"; do
    mm_args+=(--results "${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_${m}/results.jsonl")
  done
  # One invocation for every arm, so the judge pool is shared. Each arm lands
  # in <output-root>/<arm dir name>/.
  "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/scoring/score_multimodal.py" \
    "${mm_args[@]}" --output-root "${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_scores" \
    --backend "${JUDGE_BACKEND}" --concurrency "${JUDGE_CONCURRENCY}" \
    ${JUDGE_MODEL:+--judge-model "${JUDGE_MODEL}"}
fi

# --- per-category report --------------------------------------------------------
report_args=()
for m in "${arms[@]}"; do
  if [[ "${BENCH}" == "text" ]]; then
    report_args+=(--run "${m}=${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_${m}/scores")
  else
    report_args+=(--run "${m}=${OUTPUT_BASE}/${BENCH}_${MODEL_TAG}_scores/${BENCH}_${MODEL_TAG}_${m}")
  fi
done
base="recent_record"
for m in "${arms[@]}"; do
  [[ "${m}" == "${base}" ]] && continue
  if printf '%s\n' "${arms[@]}" | grep -qx "${base}"; then
    echo
    echo "== ${m} vs ${base} =="
    "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/scoring/report_by_category.py" \
      --bench "${BENCH}" "${report_args[@]}" --compare "${base}" "${m}" --markdown
  fi
done
if ! printf '%s\n' "${arms[@]}" | grep -qx "${base}"; then
  "${PYTHON_BIN}" "${REPO_ROOT}/clinseekagent/scoring/report_by_category.py" \
    --bench "${BENCH}" "${report_args[@]}" --markdown
fi
