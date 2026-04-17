#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv_mcp/bin/python}"

GPU_ID=${1:-0}
PORT=${2:-5103}
DATA_PATH="${DATA_PATH:-${REPO_ROOT}/data/EHRAgentBench}"
HOST="${HOST:-127.0.0.1}"

cd "${REPO_ROOT}"

CUDA_VISIBLE_DEVICES=${GPU_ID} "${PYTHON_BIN}" "./src/run_mcp_server.py" \
    --mode "http" \
    --host $HOST \
    --port $PORT \
    --disable-knowledge-tools \
    --data_path "$DATA_PATH"
