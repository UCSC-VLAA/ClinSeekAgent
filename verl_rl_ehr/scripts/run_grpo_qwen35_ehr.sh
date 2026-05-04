#!/bin/bash
# Full 8-GPU GRPO training for Qwen3.5-35B-A3B on EHR-Bench.
#
# Preflight:
#   - MCP EHR server must be running on $EHR_MCP_URL (default 127.0.0.1:5103).
#     Launch separately:
#       bash scripts/run/run_mcp_server.sh 0 5103
#   - HF-converted SFT checkpoint at
#     /fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf (see
#     scripts/convert_sft_ckpt_to_hf.sh).
#   - Preprocessed parquet at /fsx-shared/juncheng/EHR/data/ehr_rl_qwen35/.
#
# Override via env:
#   CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
#   N_GPUS=8 TP_SIZE=4
#   EXPERIMENT_NAME=ehr_grpo_qwen35_v0
#   EHR_MCP_URL=http://127.0.0.1:5103/mcp
#   BROWSER_SEARCH_MODE=stub            # or http (+ SEARCH_SERVICE_URL)

set -euo pipefail
ulimit -n 65535

PROJ="/fsx-shared/juncheng/EHR"
# Default to the RL-dedicated venv (transformers 5.4 + vllm 0.12 stack that
# matches verl agent_loop). The SFT venv at venvs/qwen3_5_sft is intentionally
# separate so SFT/eval workflows aren't affected by RL-specific bumps.
VENV="${VENV:-$PROJ/venvs/qwen3_5_rl}"
PYTHON="${PYTHON:-$VENV/bin/python}"

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
N_GPUS="${N_GPUS:-8}"
TP_SIZE="${TP_SIZE:-4}"

EHR_MCP_URL="${EHR_MCP_URL:-http://127.0.0.1:5103/mcp}"
SEARCH_SERVICE_URL="${SEARCH_SERVICE_URL:-http://127.0.0.1:8090}"
BROWSER_SEARCH_MODE="${BROWSER_SEARCH_MODE:-stub}"
REWARD_DEBUG_LOG="${REWARD_DEBUG_LOG:-/tmp/reward_debug_ehr.jsonl}"

# Context summarizer (Claude Haiku via Bedrock). Off by default — only used
# when the agent hits the context_reset_threshold and CONTEXT_SUMMARIZER_* is
# configured. The summarizer produces bullet-point findings that replace the
# static reset message. Falls back to static on Bedrock failure.
CONTEXT_SUMMARIZER_ENABLED="${CONTEXT_SUMMARIZER_ENABLED:-false}"
# Credentials (choose one path):
#   CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID + _SECRET_ACCESS_KEY
#   CONTEXT_SUMMARIZER_BEDROCK_KEY=base64(key_id:secret_key)  (single combined)
# If neither is set, boto3 default chain (instance profile etc.) is used.

EXPERIMENT_NAME="${EXPERIMENT_NAME:-ehr_grpo_qwen35_v0}"
TRAIN_DATA="${TRAIN_DATA:-$PROJ/data/ehr_rl_qwen35/train.parquet}"
VAL_DATA="${VAL_DATA:-$PROJ/data/ehr_rl_qwen35/val.parquet}"
MODEL_PATH="${MODEL_PATH:-$PROJ/checkpoints/qwen3_5_35b_a3b_sft_hf}"

# ── Pre-flight ───────────────────────────────────────────────────────────
export PATH="$VENV/bin:$PATH"
export PYTHONPATH="$PROJ:$PROJ/verl:${PYTHONPATH:-}"
export CUDA_VISIBLE_DEVICES
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export RAY_memory_monitor_refresh_ms=0
export PYTORCH_ALLOC_CONF=expandable_segments:True
export VLLM_USE_V1="${VLLM_USE_V1:-1}"
export EHR_MCP_URL SEARCH_SERVICE_URL BROWSER_SEARCH_MODE REWARD_DEBUG_LOG
export CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID CONTEXT_SUMMARIZER_AWS_SECRET_ACCESS_KEY \
       CONTEXT_SUMMARIZER_BEDROCK_KEY 2>/dev/null || true

# SGLang: known-stable setting from upstream OpenResearcher. The default
# message-queue broadcaster deadlocks on FSDP-actor/sglang weight-sync
# resume_memory_occupation calls when TP>1, producing 3× 60s HTTP timeouts.
export SGLANG_USE_MESSAGE_QUEUE_BROADCASTER=false

# Thread both SGLang + AWS Bedrock env vars into Ray worker processes so
# AgentLoopWorker, SGLangHttpServer, and ContextSummarizer all see them.
# RAY_RUNTIME_ENV only propagates vars it explicitly lists.
export RAY_RUNTIME_ENV="${RAY_RUNTIME_ENV:-$(python - <<'PY'
import json, os
env_vars = {
    "SGLANG_USE_MESSAGE_QUEUE_BROADCASTER": "false",
    "EHR_MCP_URL":        os.environ.get("EHR_MCP_URL", ""),
    "SEARCH_SERVICE_URL": os.environ.get("SEARCH_SERVICE_URL", ""),
    "BROWSER_SEARCH_MODE": os.environ.get("BROWSER_SEARCH_MODE", "stub"),
    "REWARD_DEBUG_LOG":   os.environ.get("REWARD_DEBUG_LOG", ""),
    "AWS_BEARER_TOKEN_BEDROCK": os.environ.get("AWS_BEARER_TOKEN_BEDROCK", ""),
    "AWS_REGION":         os.environ.get("AWS_REGION", "us-east-1"),
    "WANDB_API_KEY":      os.environ.get("WANDB_API_KEY", ""),
    "CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID":     os.environ.get("CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID", ""),
    "CONTEXT_SUMMARIZER_AWS_SECRET_ACCESS_KEY": os.environ.get("CONTEXT_SUMMARIZER_AWS_SECRET_ACCESS_KEY", ""),
    "CONTEXT_SUMMARIZER_BEDROCK_KEY":           os.environ.get("CONTEXT_SUMMARIZER_BEDROCK_KEY", ""),
}
print(json.dumps({"env_vars": {k: v for k, v in env_vars.items() if v}}))
PY
)}"

"$PYTHON" "$PROJ/verl_rl_ehr/patches/verify_verl_patches.py"

MCP_PING_BODY='{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"preflight","version":"1"}}}'
MCP_STATUS=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
    -X POST "$EHR_MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$MCP_PING_BODY" || echo "000")
if [ "$MCP_STATUS" != "200" ]; then
    echo "ERROR: EHR MCP server not reachable at $EHR_MCP_URL (status=$MCP_STATUS)"
    echo "       Launch with: bash $PROJ/scripts/run/run_mcp_server.sh 0 5103"
    exit 1
fi
if [ ! -d "$MODEL_PATH" ]; then
    echo "ERROR: model weights not found at $MODEL_PATH"
    echo "       Run scripts/convert_sft_ckpt_to_hf.sh first."
    exit 1
fi
if [ ! -f "$TRAIN_DATA" ]; then
    echo "ERROR: train parquet not found at $TRAIN_DATA"
    echo "       Run verl_rl_ehr/preprocess/build_ehr_rl_parquet.py."
    exit 1
fi

mkdir -p "$PROJ/logs"
TS=$(date +%Y%m%d_%H%M%S)
LOG="$PROJ/logs/grpo_${EXPERIMENT_NAME}_${TS}.log"

echo "=========================================="
echo "EHR GRPO training"
echo "  venv:           $VENV"
echo "  GPUs:           $CUDA_VISIBLE_DEVICES  (N=$N_GPUS, TP=$TP_SIZE)"
echo "  model:          $MODEL_PATH"
echo "  train/val:      $TRAIN_DATA | $VAL_DATA"
echo "  MCP:            $EHR_MCP_URL"
echo "  search_mode:    $BROWSER_SEARCH_MODE"
echo "  reward_debug:   $REWARD_DEBUG_LOG"
echo "  log:            $LOG"
echo "  experiment:     $EXPERIMENT_NAME"
echo "=========================================="

"$PYTHON" -m verl.trainer.main_ppo \
    --config-path="$PROJ/verl_rl_ehr/config" \
    --config-name=ehr_multiturn_grpo_qwen35 \
    data.train_files="$TRAIN_DATA" \
    data.val_files="$VAL_DATA" \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.rollout.tensor_model_parallel_size=$TP_SIZE \
    actor_rollout_ref.rollout.multi_turn.context_reset_summarizer_enabled=$CONTEXT_SUMMARIZER_ENABLED \
    trainer.n_gpus_per_node=$N_GPUS \
    trainer.experiment_name="$EXPERIMENT_NAME" \
    "$@" \
    2>&1 | tee "$LOG"
