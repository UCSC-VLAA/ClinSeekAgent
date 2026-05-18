#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_MCP_HOST="${IMAGE_MCP_HOST:-127.0.0.1}"
IMAGE_MCP_PORT="${IMAGE_MCP_PORT:-5203}"
BENCH_ROOT="${BENCH_ROOT:-${CLINSEEK_DATA_ROOT:-${REPO_ROOT}/data}/multimodal}"
IMAGE_ARTIFACT_DIR="${IMAGE_ARTIFACT_DIR:-${REPO_ROOT}/tmp/mm_artifacts}"
PYTHON_BIN="${PYTHON_BIN:-python}"

export PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}"

exec "${PYTHON_BIN}" "${REPO_ROOT}/src/mcp_image/run_image_mcp_server.py" \
  --mode http \
  --host "${IMAGE_MCP_HOST}" \
  --port "${IMAGE_MCP_PORT}" \
  --bench-root "${BENCH_ROOT}" \
  --artifact-dir "${IMAGE_ARTIFACT_DIR}" \
  "$@"
