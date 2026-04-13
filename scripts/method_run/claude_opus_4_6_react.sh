#!/bin/bash

# --- 配置区：Claude Opus 4.6 via AWS Bedrock（无需 VLLM 服务）---
MODEL="claude-opus-4-6-v1"

MCP_SERVER=(
    "http://127.0.0.1:5002/mcp"
)

SUBSET="common"
NUM_RUNS=1
METHOD="mcp"

EXP_NAME=${METHOD}_rollout${NUM_RUNS}
OUTPUT="../results/${SUBSET}/${MODEL}"
EHR_PATH="../data/AgentEHR-Bench/MIMICIVAgentBench"
DATA_BASE="../data/AgentEHR-Bench/MIMICIVAgentBench/${SUBSET}"
TASKS=(
    "diagnoses_ccs_500"
)

CONDA_ENV_NAME="base"
# --- 配置区结束 ---

# 切换到项目根目录（脚本位于 scripts/method_run/）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../../src" || exit 1

echo "🎯 正在启动 **${#TASKS[@]}** 个任务 (Claude Bedrock)..."
echo "---"

for i in "${!TASKS[@]}"; do
    task="${TASKS[$i]}"
    sindex=0

    MCP_INDEX=$(( i % ${#MCP_SERVER[@]} ))
    MCP_URL="${MCP_SERVER[$MCP_INDEX]}"

    echo "▶️ 任务: **$task** | 模型: $MODEL (Bedrock) | MCP: $MCP_URL"

    python test.py \
        --data_path "$DATA_BASE/${task}.json" \
        --output_path "$OUTPUT" \
        --model_name_or_path "$MODEL" \
        --mcp_url "$MCP_URL" \
        --ehr_path "$EHR_PATH" \
        --temperature 0.7 \
        --top_p 0.8 \
        --presence_penalty 1.0 \
        --agent_type ${METHOD} \
        --exp_name ${EXP_NAME} \
        --task "$task" \
        --max_exec_steps 100 \
        --start_index $sindex \
        --score_strategy avg \
        --num_runs ${NUM_RUNS} \
        --resume True \
        --enable_thinking False

    echo "   ✅ 任务 $task 完成"
done

echo "---"
echo "🎉 所有任务已完成（Claude Bedrock）。"
