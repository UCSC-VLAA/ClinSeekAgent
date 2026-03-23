#!/bin/bash

# 切换到 AgentEHR 项目根目录（脚本在 scripts/run/ 下）
# cd "$(dirname "$0")/../.." || exit 1

GPU_ID=${1:-0}
PORT=${2:-5103}
DATA_PATH="/home/efs/zlt/deepresearch/data/EHRAgentBench"
HOST=127.0.0.1

CUDA_VISIBLE_DEVICES=${GPU_ID} /home/efs/zlt/miniconda3/bin/python3.13 src/run_mcp_server.py \
    --mode "http" \
    --host $HOST \
    --port $PORT \
    --data_path "$DATA_PATH"
