#!/bin/bash
# 1-step GRPO smoke test.
#
# Exercises: tool_agent_loop, EHR MCP round-trip, browser stub path, reward
# compute_score, and importantly the context_reset mechanism (threshold set
# tiny so it fires on even a short rollout).
#
# Acceptance criteria (check /tmp/reward_debug_ehr.jsonl after this exits):
#   - at least one row with `has_format_signal=true`
#   - at least one row with `num_context_resets >= 1`  (if threshold triggers)
#   - trainer exits 0
#
# Override via env:
#   CUDA_VISIBLE_DEVICES, EHR_MCP_URL, SMOKE_TRAIN_DATA (defaults to /tmp/ehr_rl_smoke/train.parquet)

set -euo pipefail

PROJ="/fsx-shared/juncheng/EHR"
SMOKE_DIR="${SMOKE_DIR:-/tmp/ehr_rl_smoke}"
MODEL_PATH="${MODEL_PATH:-$PROJ/checkpoints/qwen3_5_35b_a3b_sft_hf}"

# Regenerate smoke data if not present.
if [ ! -f "$SMOKE_DIR/train.parquet" ]; then
    echo "Smoke data missing — regenerating at $SMOKE_DIR"
    "$PROJ/venvs/qwen3_5_sft/bin/python" \
        "$PROJ/verl_rl_ehr/preprocess/build_ehr_rl_parquet.py" \
        --limit-per-task 4 --val-frac 0.25 --out "$SMOKE_DIR"
fi

export BROWSER_SEARCH_MODE="${BROWSER_SEARCH_MODE:-stub}"
export REWARD_DEBUG_LOG="${REWARD_DEBUG_LOG:-/tmp/reward_debug_ehr_smoke.jsonl}"
: > "$REWARD_DEBUG_LOG"

SMOKE_TRAIN_DATA="${SMOKE_TRAIN_DATA:-$SMOKE_DIR/train.parquet}"
SMOKE_VAL_DATA="${SMOKE_VAL_DATA:-$SMOKE_DIR/val.parquet}"

EXPERIMENT_NAME="ehr_grpo_qwen35_smoke" \
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3}" \
N_GPUS="${N_GPUS:-4}" \
TP_SIZE="${TP_SIZE:-2}" \
TRAIN_DATA="$SMOKE_TRAIN_DATA" \
VAL_DATA="$SMOKE_VAL_DATA" \
MODEL_PATH="$MODEL_PATH" \
bash "$PROJ/verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh" \
    data.train_batch_size=2 \
    actor_rollout_ref.actor.ppo_mini_batch_size=2 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.rollout.n=2 \
    actor_rollout_ref.rollout.agent.num_workers=2 \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=4 \
    actor_rollout_ref.rollout.multi_turn.max_user_turns=4 \
    actor_rollout_ref.rollout.multi_turn.context_reset_threshold=2000 \
    actor_rollout_ref.rollout.max_model_len=4096 \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.35 \
    actor_rollout_ref.rollout.free_cache_engine=false \
    actor_rollout_ref.actor.fsdp_config.param_offload=true \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=true \
    actor_rollout_ref.ref.fsdp_config.param_offload=true \
    data.max_prompt_length=3072 \
    data.max_response_length=3072 \
    trainer.total_training_steps=1 \
    trainer.val_before_train=false \
    trainer.save_freq=-1 \
    trainer.test_freq=-1 \
    "$@"

echo ""
echo "Smoke exited cleanly. Reward trace:  $REWARD_DEBUG_LOG"
echo "Last few rows:"
tail -n 5 "$REWARD_DEBUG_LOG" || true
