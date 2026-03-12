#!/bin/bash

# --- 配置区（与原脚本保持一致）---
MODEL="qwen3_30b_moe"
VLLM_SERVER=( 
    "http://127.0.0.1:4000"

)
MCP_SERVER=(
    "http://127.0.0.1:5000/mcp"

) # s3

SUBSET="common"
NUM_RUNS=1
METHOD="mcp"

# EXP_NAME=${METHOD}_rollout${NUM_RUNS}_think_new
EXP_NAME=${METHOD}_rollout${NUM_RUNS}
OUTPUT="../results/${SUBSET}/${MODEL}"
EHR_PATH="../data/EHRAgentBench"
DATA_BASE="../data/EHRAgentBench/${SUBSET}"
TASKS=(
    "diagnoses_ccs_500" 
)
# TASKS=("diagnoses_ccs")

# --- 新增和推断的配置 ---
CONDA_ENV_NAME="base" 
# 根据您的上下文推断出的项目根目录，test_mcp.py 应该在此目录下
PROJECT_ROOT="./"
# --- 配置区结束 ---

# 获取当前的 tmux 窗格 ID
current_pane_id=$(tmux display-message -p '#{pane_id}')

echo "🎯 正在从当前窗格 ($current_pane_id) 分割并启动 **${#TASKS[@]}** 个并行任务..."
echo "---"

# 循环遍历任务列表
for i in "${!TASKS[@]}"; do
    task="${TASKS[$i]}"
    sindex=0 
    
    # 循环分配 VLLM 和 MCP 服务器
    VLLM_INDEX=$(( i % ${#VLLM_SERVER[@]} ))
    VLLM_URL="${VLLM_SERVER[$VLLM_INDEX]}"
    
    MCP_INDEX=$(( i % ${#MCP_SERVER[@]} ))
    MCP_URL="${MCP_SERVER[$MCP_INDEX]}"
    
    echo "▶️ 任务: **$task** | VLLM: $VLLM_URL | MCP: $MCP_URL"
    
    # 构建单行 Python 命令字符串
    # 注意：所有参数值都用反斜杠转义的双引号括起来，以确保它们能正确传递给 tmux send-keys
    PYTHON_CMD_SINGLE_LINE="python test.py \
        --data_path \"$DATA_BASE/${task}.json\" \
        --output_path \"$OUTPUT\" \
        --model_name_or_path \"$MODEL\" \
        --vllm_server_url \"$VLLM_URL\" \
        --mcp_url \"$MCP_URL\" \
        --ehr_path \"$EHR_PATH\" \
        --temperature 0.7 \
        --top_p 0.8 \
        --presence_penalty 1.0 \
        --agent_type ${METHOD} \
        --exp_name ${EXP_NAME} \
        --task \"$task\" \
        --max_exec_steps 100 \
        --start_index $sindex \
        --score_strategy avg \
        --num_runs ${NUM_RUNS} \
        --resume True \
        --enable_thinking False"

    # 构建完整的 tmux 发送命令：激活环境 -> 切换目录 -> 执行Python脚本
    # 注意：使用 '&&' 确保前一个命令成功后才执行下一个
    TMUX_SEND_CMD="conda activate $CONDA_ENV_NAME && cd $PROJECT_ROOT && $PYTHON_CMD_SINGLE_LINE"
    
    # 1. 分割当前窗格（采用垂直分割 -v，使用 PROJECT_ROOT 作为新窗格的起始目录）
    new_pane_id=$(tmux split-window -t "$current_pane_id" -h -c "$PROJECT_ROOT" -P -F '#{pane_id}')
    
    # 2. 发送命令到新窗格
    tmux send-keys -t "$new_pane_id" "$TMUX_SEND_CMD" C-m
    
    echo "   ✅ Pane ID: $new_pane_id"
done

echo "---"
echo "🎉 所有任务已在各自的 tmux 窗格中启动并运行。您可以切换到每个窗格查看输出。"

# 保持焦点在原始窗格
tmux select-pane -t "$current_pane_id"