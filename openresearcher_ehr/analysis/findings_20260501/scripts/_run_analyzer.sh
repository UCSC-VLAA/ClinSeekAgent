#!/usr/bin/env bash
# Run analyze_trajectory.py over all 9 cases in parallel. Reads
# cases/<slug>/{trajectory.md, reasoning.md} and writes
# cases/<slug>/{vital_analysis.json, analyzer.log}.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT_DIR="$(cd "$DIR/.." && pwd)"
CASES="$REPORT_DIR/cases"
PY=/fsx-shared/juncheng/EHR/venvs/bedrock_agent/bin/python

declare -a SPECS=(
  "case1_lengthofstay:won_the_answer"
  "case2_mm_phenotyping:won_the_answer"
  "case3_pyxis:lost_the_answer"
  "case4_inpatient_mortality:won_the_answer"
  "case5_ed_hospitalization:won_the_answer"
  "case6_mm_decompensation:won_the_answer"
  "case7_mm_mortality:won_the_answer"
  "case8_labevents:lost_the_answer"
  "case9_next_event:lost_the_answer"
)

PIDS=()
for spec in "${SPECS[@]}"; do
  slug="${spec%:*}"
  mode="${spec#*:}"
  cdir="$CASES/$slug"
  mkdir -p "$cdir"
  traj="$cdir/trajectory.md"
  rsn="$cdir/reasoning.md"
  out="$cdir/vital_analysis.json"
  # Always pass --reasoning even in loss mode; analyzer ignores it for loss mode.
  "$PY" "$DIR/analyze_trajectory.py" \
      --trajectory "$traj" \
      --reasoning "$rsn" \
      --mode "$mode" \
      --out "$out" > "$cdir/analyzer.log" 2>&1 &
  PIDS+=($!)
  echo "launched $slug ($mode) PID=$!  → $cdir"
done

wait
echo "all analyses complete"
