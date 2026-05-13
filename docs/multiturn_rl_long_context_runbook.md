# Long-context multi-turn GRPO runbook (56K response, Ulysses SP=8)

**Status:** v5y is the first working long-context config (2026-04-30).
12 clean steps, score +0.41 → +0.47, F1 0.205 → 0.232.
Full marathon of failed attempts + root causes is in
[`debug_logs/09_long_context_rl_oom_marathon.md`](debug_logs/09_long_context_rl_oom_marathon.md).

This is the how-to-run document. If you only need to reproduce the working
setup, jump straight to **Launch**.

---

## 1. Environment

Working dir: `/fsx-shared/juncheng/EHR/`
Venv: `/fsx-shared/juncheng/EHR/venvs/qwen3_5_rl/`
(Python 3.10, pinned stack: torch 2.9.0 · transformers 5.5.3 · sglang 0.5.9 ·
flash-attn 2.8.3 · verl editable install).

Model: `/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf` —
HF-formatted SFT Qwen3.5-35B-A3B checkpoint.

Preprocessed data: `/fsx-shared/juncheng/EHR/data/ehr_rl_qwen35/{train,val_half}.parquet`
(2922 train rows, 163 val rows).

8× H200 (143 GB each) on a single node. TP=4 (sglang rollout) + SP=8
(actor+ref FSDP) share the same 8 GPUs.

---

## 2. Side services (must be up before launch)

### MCP EHR server on :5103
```bash
bash scripts/run/run_mcp_server.sh 0 5103 &
sleep 10
```
Sanity check: `curl -s -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' http://127.0.0.1:5103/mcp | head -c 80`

### Search service on :8090 (Serper-backed)
```bash
set -a; source .env; set +a    # loads SERPER_API_KEY
nohup /fsx-shared/juncheng/EHR/venvs/qwen3_5_rl/bin/python \
      -m verl_rl_ehr.services.search_service --port 8090 \
      > logs/search_service_$(date +%Y%m%d_%H%M%S).log 2>&1 &
disown
```
Code: `verl_rl_ehr/services/search_service.py` (FastAPI wrapper around
Serper `/search` and `/scrape` APIs, with internal page-cache keyed by
`cursor`). Sanity check: `curl -s http://127.0.0.1:8090/healthz` →
`{"ok":true,"serper_key_set":true}`.

Stays up for the duration of training. Not covered by the verl preflight
so set up a watchdog if you care about 12h+ runs.

### `.env` must contain
```
WANDB_API_KEY=<your key>
AWS_BEARER_TOKEN_BEDROCK=<Haiku key>   # for summarizer-based context reset
SERPER_API_KEY=<your key>              # for browser.search
```

---

## 3. Patches that must be present in verl/

Run `./venvs/qwen3_5_rl/bin/python verl_rl_ehr/patches/verify_verl_patches.py`
before every launch. It must print `verl patches OK`. If any sentinel is
missing the script exits 2 and prints the missing file:sentinel pair.

Short list of what's being checked (see the verifier script for the
authoritative list):

- `tool_agent_loop.py`: context-reset state, `_maybe_reset_context`,
  `_do_context_reset`, `_turn_limit_rescue`, `_maybe_force_answer`,
  `force_finish_tool_enabled`, `ContextSummarizer` import.
- `workers/config/rollout.py`: `context_reset_enabled`,
  `context_reset_threshold`, `context_reset_mode`, `context_reset_max_count`,
  `context_reset_keep_last_rounds`, `force_finish_tool_enabled`,
  `force_answer_token_threshold`, `context_reset_summarizer_enabled`.
- `tools/schemas.py`: `ConfigDict(extra="allow")` (needed for tool
  schemas with `default` / `anyOf` fields).
- `trainer/ppo/ray_trainer.py`: `rollout_dump_freq` gate.
- **`models/transformers/monkey_patch.py`: `model_type == "qwen3_5_moe"`**
  — this is the fix that unblocked Ulysses SP>1 for Qwen3.5-MoE. Without
  it, SP runs crash downstream in compute_log_prob with shape mismatches.
- Also runs sglang patches via `apply_sglang_patches.sh` (Qwen3.5-MoE model
  registration into sglang's rollout engine).

---

## 4. GPU-memory strategy (what saves us, and where)

RL training has two distinct GPU memory pressure points that need different
tools. Mixing them up (as I did repeatedly in the debug session) wastes
days of iteration. Here's the clean breakdown:

### 4.1 Rollout phase (sglang generation)
- **Memory driver:** per-sequence KV cache. Size ∝ `len(generation_prompt)` × layers.
- **Levers:**
  - `context_reset_threshold=14000`: when the generation prompt crosses
    this, rebuild it as `[system, original_user, <summary note from Haiku>]`.
    Caps per-sequence KV cache at ~15K even in arbitrarily long trajectories.
  - `context_reset_mode="summarizer"`: Haiku summarizes dropped rounds
    into a concise findings note. Keeps rollouts submitting (~100%
    has_answer) by preserving high-level context.
    Alternative: `"sliding_window"` drops middle rounds verbatim; cheaper
    but submission rate drops to ~34%. Don't use unless you're explicitly
    testing.
  - `gpu_memory_utilization=0.30`: sglang gets 30% of VRAM (~43 GB/H200)
    for its weights + KV cache. With resets firing, this fits ~128
    concurrent rollouts.
  - `max_num_seqs=128`, `tensor_model_parallel_size=4`: 128 concurrent
    rollouts across 4 sglang ranks.
- **Unlimited retries:** `context_reset_max_count=10000` effectively
  removes the cap; rollouts can rescue indefinitely until the training-
  tensor hard wall (`max_response_length=56000`) fires.

### 4.2 Policy update (FSDP actor backward)
- **Memory driver:** activation memory during `loss.backward()`. Scales
  with `total_tokens_per_rank × hidden_size × layers`.
- **Levers:**
  - **Ulysses SP=8**: shards the sequence dimension across all 8 ranks at
    the attention layer via all-to-all. Per-rank activation memory ≈ `1/8`
    of the full sequence. **Requires the verl monkey-patch fix for
    qwen3_5_moe** — see §3.
  - `ppo_micro_batch_size_per_gpu=1`: one rollout per rank per backward
    call. Already the minimum.
  - `log_prob_micro_batch_size_per_gpu=2`: same cap but for the pre-update
    `compute_log_prob` forward pass. Was 4 in the yaml default; reducing
    to 2 cuts lm_head output size in half (was 107 GiB at mbsz=4).
  - `fsdp_config.param_offload=true`, `optimizer_offload=true`: weights
    and optimizer state offloaded to CPU between steps. Frees ~50 GB per
    GPU during forward/backward. (Cost: CPU-GPU swap each step adds
    ~30s of wall-clock.)
  - `enable_gradient_checkpointing=true`: activations re-computed during
    backward instead of stored. ~3-4× saving. Already on by default.
- **Force-answer soft cap** (`force_answer_token_threshold=52000`): when
  `response_mask` crosses this length, inject a `<tool_call>ehr.finish`
  prefix so the remaining 4K tokens go toward the answer instead of
  further exploration. Bounds total training-tensor size predictably at
  ~56K per rollout.

### 4.3 What does NOT save memory (despite claims)
- **Dynamic batching (`use_dynamic_bsz=true`)** — verl asserts
  `max_token_len >= max_seq_len`, so it cannot split a single long rollout.
  Useless when rollouts are near max length.
- **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** — breaks sglang's
  `torch-memory-saver`. Do not set in this venv.
- **Ulysses without the qwen3_5_moe monkey-patch branch** — no-op for our
  model. SP=4 and SP=8 both OOM identically at 107 GiB without the patch.

### 4.4 Peak memory seen in v5y (proof it fits)
- Rollout phase: ~60 GB per GPU.
- compute_log_prob: ~70-80 GB.
- update_actor backward: ~126-132 GB (peak observed).
- H200 cap: 143.8 GB per GPU. Headroom ~12 GB.

---

## 5. Launch

Assumes the side services (§2) are running and patches (§3) verified.

```bash
cd /fsx-shared/juncheng/EHR

set -a; source .env; set +a
export CONTEXT_SUMMARIZER_ENABLED=true
export EXPERIMENT_NAME=ehr_grpo_qwen35_v5y_rerun   # or whatever you want
export REWARD_DEBUG_LOG=/tmp/reward_debug_ehr_${EXPERIMENT_NAME}.jsonl
: > "$REWARD_DEBUG_LOG"

ROLLOUT_DIR=/fsx-shared/juncheng/EHR/analysis/rollouts/${EXPERIMENT_NAME}
mkdir -p "$ROLLOUT_DIR"

export MODEL_PATH=/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf
export VAL_DATA=/fsx-shared/juncheng/EHR/data/ehr_rl_qwen35/val_half.parquet
export BROWSER_SEARCH_MODE=http
export SEARCH_SERVICE_URL=http://127.0.0.1:8090
unset PYTORCH_CUDA_ALLOC_CONF   # IMPORTANT — breaks sglang

source venvs/qwen3_5_rl/bin/activate
python verl_rl_ehr/patches/verify_verl_patches.py   # must print "verl patches OK"

LOG_OUTER=logs/grpo_${EXPERIMENT_NAME}_outer_$(date +%Y%m%d_%H%M%S).log
nohup bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh \
  trainer.total_training_steps=50 \
  trainer.save_freq=10 \
  trainer.test_freq=10 \
  trainer.val_before_train=false \
  trainer.rollout_data_dir="$ROLLOUT_DIR" \
  +trainer.rollout_dump_freq=1 \
  +data.apply_chat_template_kwargs.enable_thinking=false \
  data.max_response_length=56000 \
  actor_rollout_ref.rollout.multi_turn.context_reset_mode=summarizer \
  actor_rollout_ref.rollout.multi_turn.force_finish_tool_enabled=true \
  actor_rollout_ref.rollout.multi_turn.force_answer_token_threshold=52000 \
  actor_rollout_ref.rollout.multi_turn.context_reset_max_count=10000 \
  actor_rollout_ref.actor.ulysses_sequence_parallel_size=8 \
  actor_rollout_ref.ref.ulysses_sequence_parallel_size=8 \
  actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
  actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2 \
  actor_rollout_ref.rollout.gpu_memory_utilization=0.30 \
  actor_rollout_ref.rollout.free_cache_engine=false \
  actor_rollout_ref.actor.fsdp_config.param_offload=true \
  actor_rollout_ref.actor.fsdp_config.optimizer_offload=true \
  actor_rollout_ref.ref.fsdp_config.param_offload=true \
  >"$LOG_OUTER" 2>&1 < /dev/null &
PID=$!; disown $PID
echo "PID=$PID LOG=$LOG_OUTER"
```

**Expected timing** (v5y numbers):
- Boot (weights load + sglang capture + wandb init): ~8-10 min.
- First step rollout (128 rollouts × up to 56K tokens each): ~20-25 min.
- First step `compute_log_prob` + `update_actor`: ~6-8 min.
- **Per step**: ~27-32 min wall-clock.
- 50 steps: ~23-26 hours.

**Monitoring during boot:**
- Ulysses patch confirmation:
  `grep "Monkey patch Qwen3_5MoeTextModel.forward for Ulysses SP" <log>`
  should print 8 lines.
- Summarizer confirmation:
  `grep "\[RESET\].*mode=summarizer.*used=haiku" <log> | head -2`
  should print once rollouts start.

**Failure checks** (run periodically):
```bash
LOG=<log file>
grep -cE "OutOfMemoryError|CUDA out of memory|ActorDiedError|Error executing job|AssertionError" "$LOG"
# should stay at 0
```

---

## 6. Observability

### Reward trace
Per-rollout reward components appended to
`/tmp/reward_debug_ehr_<EXPERIMENT_NAME>.jsonl`. One JSON row per rollout
with fields: `score`, `reward/correctness`, `reward/efficiency`,
`reward/format_penalty`, `f1`, `has_answer`, `n_ehr_tool_calls`,
`n_browser_tool_calls`, `num_turns`, `force_answer_injected`,
`format_penalty_applied`, `answer_extracted`, `ground_truth`, etc.

Python one-liner to summarize per-step:
```python
import json
rows = [json.loads(l) for l in open('/tmp/reward_debug_ehr_<NAME>.jsonl')]
BATCH = 128  # train_batch_size × rollouts_per_prompt / 8 DP ranks → whatever the full batch is
for i in range(0, len(rows), BATCH):
    sub = rows[i:i+BATCH]
    if len(sub) < BATCH: break
    print(f'step {i//BATCH+1}: score={sum(r["score"] for r in sub)/len(sub):+.4f} '
          f'corr={sum(r["reward/correctness"] for r in sub)/len(sub):+.4f} '
          f'eff={sum(r["reward/efficiency"] for r in sub)/len(sub):+.4f} '
          f'fmt={sum(r["reward/format_penalty"] for r in sub)/len(sub):+.4f} '
          f'f1={sum(r["f1"] for r in sub)/len(sub):.4f}')
```

### Rollout dumps
`trainer.rollout_dump_freq=1` writes
`analysis/rollouts/<EXPERIMENT_NAME>/<step>.jsonl` with 128 rollouts per
file (full `input`/`output` text + reward fields). Useful for post-hoc
inspection, e.g. reviewing what the policy submitted.

### wandb
Project `deepmed-rl`, experiment = `EXPERIMENT_NAME`. Shows per-step
`critic/score/mean`, `critic/score/max`, response_length stats, timing
breakdown.

### Tags in the launcher log
- `[RESET]` — context reset fired. Rate = expected ≈ 200/step.
- `[RESCUE]` — turn-limit rescue (rare in v5y since max_assistant_turns isn't hit).
- `[FORCE_ANSWER]` — soft-cap force-answer at 52K. Rate: ~100% in v5y.
- `[RESCUE]` vs `[RESET]` distinguishes threshold-triggered (prompt crossed
  threshold) from turn-limit-triggered (turn budget exhausted) resets.

---

## 7. Checkpointing

`trainer.save_freq=10` writes a full HF-format checkpoint every 10 steps
to `/fsx-shared/juncheng/EHR/checkpoints/deepmed-rl/<EXPERIMENT_NAME>/global_step_N/`.

Each checkpoint contains the policy weights, optimizer state, and training
metadata. To resume: set `trainer.resume_mode=auto` (already default) and
relaunch with the same `EXPERIMENT_NAME`.

Keep 3 rolling checkpoints: `trainer.max_actor_ckpt_to_keep=3` (yaml default).

---

## 8. Known open issues

### Reward/efficiency is a dead signal
In v5y, 100% of rollouts get force-answer-injected at the 52K soft cap.
The reward/efficiency component is zeroed by design when
`force_answer_injected=True`, so it contributes nothing to score across
all observed steps. Effectively only `correctness` (= 2×F1) and (rarely)
`format_penalty` carry gradient signal.

If a future iteration wants to make efficiency useful, options:
- Raise `force_answer_token_threshold` significantly (e.g. 80K with a
  matching `max_response_length` bump), so a fraction of rollouts commit
  without being forced.
- Remove the `force_answer_injected` gate from `ehr_reward.py` and let
  efficiency reward any rollout based purely on `num_turns`.
- Replace efficiency with a different shaping bonus (e.g. a per-rollout
  bonus scaled by `1 / (1 + num_ehr_tool_calls / 50)`).

### Tool hallucination
Model frequently calls tools that don't exist (e.g. `ehr.get_latest_records`,
`ehr.think`). MCP returns an error; these are logged as
`WARNING: Tool 'X' is not defined in the tools list`. Rate is high
(~hundreds per rollout) but doesn't affect correctness — the error tokens
just consume budget. Consider adding a "retry with valid tool" mechanic
if it starts affecting F1.

### Step-to-step reward variance
v5y step 8 dropped to score 0.202 (vs. steady ~0.45 before/after). This
is PPO noise on small (16-prompt) batches; one bad batch of hard examples
pulls the mean down. Moving averages are more informative than any single
step.

### wandb metric line lag
`Training Progress:` line in the launcher log lags one step (step N
commits are logged during step N+1). To check step 1's full metrics, wait
for step 2's rollout phase. Rollout dumps and reward_debug are immediate.

---

## 9. If the reward doesn't rise past step ~20

Possible actions, in order of increasing complexity:

### 9.1 Data engineering (rejection sampling)
Run the base policy on the full 2922-row train parquet, bucket by
per-task F1 signal. Filter to rows where base F1 > 0 (model can sometimes
hit — signal is trainable) while down-weighting rows where base F1 = 0
(pure noise for the gradient). Produces a tractable curriculum.

Rough script outline:
```python
# For each row in train.parquet, run 4 rollouts with the base policy,
# compute per-row mean F1. Keep rows where this mean > threshold.
# Write to train_filtered.parquet.
```

### 9.2 Increase `max_response_length` beyond 56K
v5y peaked at 132 GB/GPU during update_actor. With 143 GB H200 cap we
have ~12 GB headroom. Pushing to 80K response_length is risky but
probably feasible. 100K likely OOMs.

If you push this: bump `max_response_length` AND
`force_answer_token_threshold` together (keep 4K gap), and also bump
sglang's `max_model_len` past `context_reset_threshold +
max_response_length` to be safe.

### 9.3 Reward reshape
If `efficiency` is truly dead (0 in 100% of rollouts), consider making
it gradient-carrying by decoupling from `force_answer_injected`:
```python
# Current:
# efficiency = max(0, 1 - num_turns/EFFICIENCY_TURN_BUDGET) if not force else 0
# Proposed: just penalize long trajectories regardless of force:
# efficiency = max(0, 1 - num_turns/EFFICIENCY_TURN_BUDGET)
```

### 9.4 Longer training horizon
50 steps × 16 prompts = 800 unique training episodes. For comparison,
v5h showed clear uptrend through 6 steps (96 episodes) at 20K response.
v5y has been stable through 12 steps (192 episodes). Consider
`trainer.total_training_steps=200` or `total_epochs=3` before giving up.

### 9.5 Higher-variance rollouts
`actor_rollout_ref.rollout.n=8` (group size) and `rollout.temperature=1.0`
(yaml default) give 8 rollouts per prompt with high-variance sampling —
appropriate for GRPO. If group variance is too high, try `n=4` to double
the number of distinct prompts per step; if too low, try `n=16`.

---

## 10. Artifacts (in v5y layout)

| Path | Contents |
|---|---|
| `logs/grpo_ehr_grpo_qwen35_v5y_outer_<timestamp>.log` | outer bash + stderr |
| `logs/grpo_ehr_grpo_qwen35_v5y_<timestamp>.log` | inner Python stdout/err |
| `logs/search_service_<timestamp>.log` | Serper service log |
| `logs/mcp_server_v5_<timestamp>.log` | MCP server log |
| `/tmp/reward_debug_ehr_ehr_grpo_qwen35_v5y.jsonl` | per-rollout reward trace |
| `analysis/rollouts/ehr_grpo_qwen35_v5y/<N>.jsonl` | 128 rollouts per step, full trace |
| `checkpoints/deepmed-rl/ehr_grpo_qwen35_v5y/global_step_<N>/` | HF policy weights |
| wandb run `n1onu472` | aggregated metrics |

---

## 11. References

- `docs/debug_logs/09_long_context_rl_oom_marathon.md` — full debug
  narrative of the v5h → v5y marathon, bug-by-bug root causes.
- `docs/multiturn_rl_v5_handoff.md` — older v5 handoff (20K response
  budget era), still relevant for infra (venv, model checkpoint paths,
  data generation).
- `docs/multiturn_rl_training.md` — original runbook with historical
  bug-fix log.
- `verl_rl_ehr/patches/verify_verl_patches.py` — preflight patch
  verifier. Run before every launch.
- `verl_rl_ehr/services/search_service.py` — Serper HTTP service.
- `verl_rl_ehr/reward/ehr_reward.py` — reward function.
- `verl/verl/models/transformers/monkey_patch.py` — contains the
  qwen3_5_moe Ulysses branch that unblocks everything.
