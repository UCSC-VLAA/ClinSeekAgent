#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON_BIN="${SCRIPT_DIR}/../../../miniconda3/bin/python3.13"

GPU_ID=${1:-0}
PORT=${2:-5103}
DATA_PATH="${REPO_ROOT}/data/AgentEHR-Bench/MIMICIVAgentBench"
HOST=127.0.0.1

cd "${REPO_ROOT}"

CUDA_VISIBLE_DEVICES=${GPU_ID} "${PYTHON_BIN}" "./src/run_mcp_server.py" \
    --mode "http" \
    --host $HOST \
    --port $PORT \
    --disable-knowledge-tools \
    --data_path "$DATA_PATH"
