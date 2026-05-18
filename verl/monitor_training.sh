#!/usr/bin/env bash
set -euo pipefail

# Monitor training progress by checking the latest Weights & Biases output.log.
# Usage:
#   WANDB_DIR=/path/to/wandb bash verl/monitor_training.sh

WANDB_DIR="${WANDB_DIR:-./wandb}"
LATEST_RUN=$(ls -td "${WANDB_DIR}"/run-* 2>/dev/null | head -1)

if [[ -z "${LATEST_RUN}" ]]; then
    echo "No wandb runs found under ${WANDB_DIR}"
    exit 1
fi

OUTPUT_LOG="${LATEST_RUN}/files/output.log"
echo "=== Monitoring: ${LATEST_RUN} ==="
echo ""

if [[ -f "${OUTPUT_LOG}" ]]; then
    echo "--- Latest training steps ---"
    grep "^step:" "${OUTPUT_LOG}" | tail -10 || true
    echo ""
    echo "--- Progress bar ---"
    grep "Epoch" "${OUTPUT_LOG}" | tail -1 || true
    echo ""
    LAST_STEP=$(grep "^step:" "${OUTPUT_LOG}" | tail -1 | sed 's/step:\([0-9]*\).*/\1/' || true)
    echo "Last completed step: ${LAST_STEP:-N/A}"
else
    echo "No output.log yet (still initializing...)"
fi

echo ""
echo "--- GPU Memory ---"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader 2>/dev/null || true
echo ""
echo "--- Process check ---"
COUNT=$(ps aux | grep sft_trainer | grep -v grep | wc -l | tr -d ' ')
echo "${COUNT} sft_trainer processes running"
