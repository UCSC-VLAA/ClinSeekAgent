#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

EHR_DATA_PATH="${EHR_DATA_PATH:-${CLINSEEK_DATA_ROOT:-${REPO_ROOT}/data}/ehr_bench}"
EHR_MCP_HOST="${EHR_MCP_HOST:-127.0.0.1}"
EHR_MCP_PORT="${EHR_MCP_PORT:-5003}"
PYTHON_BIN="${PYTHON_BIN:-python}"

export PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}"

exec "${PYTHON_BIN}" "${REPO_ROOT}/src/run_mcp_server.py" \
  --mode http \
  --host "${EHR_MCP_HOST}" \
  --port "${EHR_MCP_PORT}" \
  --data_path "${EHR_DATA_PATH}" \
  "$@"
