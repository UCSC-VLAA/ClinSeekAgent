#!/bin/bash

# 切换到 AgentEHR 项目根目录（脚本在 scripts/run/ 下）
# cd "$(dirname "$0")/../.." || exit 1

GPU_ID=${1:-0}
DATA_PATH="../data/EHRAgentBench"
HOST=127.0.0.1
PORT=5002

CUDA_VISIBLE_DEVICES=${GPU_ID} python run_mcp_server.py \
    --mode "http" \
    --host $HOST \
    --port $PORT \
    --data_path "$DATA_PATH"