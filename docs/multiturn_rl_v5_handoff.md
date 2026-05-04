# Multi-turn GRPO v5 — handoff for continuing SFT-ed-model training on another node

**Goal of the v5 lineage:** continue GRPO training from the **SFT-ed** Qwen3.5-35B-A3B
checkpoint (distinct from the v6 lineage, which is training from the pre-SFT
base weights on this node). All filesystem paths below are on a shared FSx
mount, so the other node sees the exact same paths.

**Status at handoff (2026-04-28):** v5b reached step 1 with
`critic/score/mean = -0.053` (vs v4 SFT step 1 = -0.083), parser fix landed
(0 tool-parse errors vs 4000+ before), then was killed by user to pivot to
v6 base-model training. v5 is ready to be re-launched on a new node **with
the same code + config + ckpt** — all surgery below is already in place.

Working directory is `/fsx-shared/juncheng/EHR/` on a shared mount. Same on
every node.

---

## 1. Venv (Python 3.10.12, exact path)

**Path (must be identical on every node):** `/fsx-shared/juncheng/EHR/venvs/qwen3_5_rl`

- **Python:** 3.10.12 — the `uv`-created venv points at `/usr/bin/python3.10`
  (Ubuntu 22.04 system Python, `3.10.12 (main, Jul 29 2024)`, GCC 11.4.0).
  If `/usr/bin/python3.10` is missing on the new node, install system
  Python 3.10 with:
  ```bash
  sudo apt-get install -y python3.10 python3.10-venv python3.10-dev
  ```
  The exact patch version (3.10.12) does not have to match byte-for-byte —
  any Python 3.10.x will work as long as all the pinned packages below
  install cleanly.

- **Activate:** `source /fsx-shared/juncheng/EHR/venvs/qwen3_5_rl/bin/activate`

- **Pinned packages (anchor versions — do NOT bump without re-testing):**
  ```
  torch==2.9.0               (+cu128 build via uv)
  torchvision==0.24.0
  torchaudio==2.9.0
  transformers==5.5.3        # Qwen3.5 fused-expert format, TokenizersBackend
  huggingface-hub==1.12.0    # transformers 5.5 requires ≥1.0
  accelerate==1.13.0         # 1.7–1.12 crash on _is_hf_initialized
  vllm==0.13.0               # installed but unused; kept for schema compat
  sglang==0.5.9              # rollout backend — what's actually used
  sgl-kernel==0.3.21
  flashinfer-python==0.6.3
  flashinfer-cubin==0.6.3
  flash-attn==2.8.3          # must be built from source; see below
  torch-memory-saver==0.0.9
  decord==0.6.0              # critical: without it sglang silently skips
                             # registering Qwen3_5MoeForConditionalGeneration
  qwen-vl-utils==0.0.14
  boto3==1.42.96             # Bedrock Haiku summarizer
  ray==2.54.0
  ```

- **If the venv gets damaged**, rebuild it byte-identical:
  ```bash
  cd /fsx-shared/juncheng/EHR
  # 1) If uv is not on the new node:
  #    curl -LsSf https://astral.sh/uv/install.sh | sh
  uv venv --python 3.10 venvs/qwen3_5_rl
  export VIRTUAL_ENV=$PWD/venvs/qwen3_5_rl

  uv pip install --no-cache "torch==2.9.0" "torchvision==0.24.0" "torchaudio==2.9.0"
  uv pip install --no-cache "vllm==0.13.0"     # brings transformers 4.57; we overwrite
  uv pip install --no-cache --no-deps "transformers==5.5.3" "huggingface_hub>=1.0" "accelerate==1.13.0"
  uv pip install --no-cache --no-deps "sglang[srt]==0.5.9" "sgl-kernel==0.3.21" \
                                      "flashinfer-python==0.6.3" "flashinfer-cubin==0.6.3" \
                                      "torch-memory-saver==0.0.9"
  # sglang pulls cu13 NCCL — purge and force cu12 back to match torch's RPATH
  uv pip uninstall nvidia-cudnn-cu13 nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvshmem-cu13
  uv pip install --force-reinstall --no-deps "nvidia-nccl-cu12==2.27.5"
  # flash-attn has no prebuilt wheel for torch 2.9 + cp310 — compile (~15 min)
  uv pip install --no-cache --force-reinstall --no-deps --no-build-isolation "flash-attn==2.8.3"
  uv pip install --no-cache "decord==0.6.0" "qwen-vl-utils==0.0.14" "boto3==1.42.96"
  # editable verl install
  uv pip install --no-deps -e verl/
  # sglang patches for Qwen3.5-MoE (idempotent)
  bash verl_rl_ehr/patches/apply_sglang_patches.sh
  ```
  If `cp -a` was used to clone the venv from another host, **sed-rewrite the
  old path** out of `bin/*` activation scripts:
  ```bash
  grep -rl "/OLD/VENV/PATH" venvs/qwen3_5_rl/bin \
    | xargs sed -i 's|/OLD/VENV/PATH|/fsx-shared/juncheng/EHR/venvs/qwen3_5_rl|g'
  ```

- **Verification:**
  ```bash
  source venvs/qwen3_5_rl/bin/activate
  python verl_rl_ehr/patches/verify_verl_patches.py   # must print "verl patches OK"
  ```

---

## 2. Data + model weights

Everything is on the shared FSx mount — just read these paths.

### Training data (parquets)
```
/fsx-shared/juncheng/EHR/data/ehr_rl_qwen35/train.parquet   # 2922 rows
/fsx-shared/juncheng/EHR/data/ehr_rl_qwen35/val.parquet     # 326 rows
```
Preprocessed from `data/MIMICIVAgentBench/common/*_500.json` by
`verl_rl_ehr/preprocess/build_ehr_rl_parquet.py`. System prompt = `DEVELOPER_CONTENT_CLAUDE`
from `openresearcher_ehr/data_utils.py` + an appended `<answer>`-tag nudge
(see section 3.3).

**Regenerate if needed** (no reason to unless you change the preprocessing):
```bash
source venvs/qwen3_5_rl/bin/activate
python verl_rl_ehr/preprocess/build_ehr_rl_parquet.py --out data/ehr_rl_qwen35
```

### Model: SFT-ed Qwen3.5-35B-A3B checkpoint (the one v5 uses)

The HF-formatted SFT weights live at:
```
/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf
```
This is a **symlink into the verl SFT training tree**:
```
→ /fsx-shared/juncheng/EHR/verl/checkpoints/deepmed-sft/deepmed-sft-qwen3_5-35b-a3b-megatron-52k-tp2-pp1-ep8/global_step_420/huggingface
```
verl's Megatron SFT already dumped HF-format weights beside the native
dist_ckpt at every `global_step_*`. **No conversion needed** — the symlink is
already in place. If it ever gets deleted, recreate with:
```bash
ln -sfn /fsx-shared/juncheng/EHR/verl/checkpoints/deepmed-sft/deepmed-sft-qwen3_5-35b-a3b-megatron-52k-tp2-pp1-ep8/global_step_420/huggingface \
       /fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf
```

(For reference: the v6 lineage instead uses the pre-SFT base weights at
`/fsx-shared/juncheng/EHR/models/Qwen3.5-35B-A3B`, not this path.)

### MCP server (EHR tool backend)

Must be running on **port 5103** on localhost (or set `EHR_MCP_URL`). The
script has **broken defaults** — you MUST export `DATA_PATH` and
`PYTHON_BIN`, otherwise it looks for a non-existent `data/EHRAgentBench/`
and `.venv_mcp/`:

```bash
cd /fsx-shared/juncheng/EHR
DATA_PATH=/fsx-shared/juncheng/EHR/data/MIMICIVAgentBench \
PYTHON_BIN=/fsx-shared/juncheng/EHR/venvs/mcp_ehr/bin/python \
bash scripts/run/run_mcp_server.sh 0 5103 &
```

What the running server looks like (resolved from `ps`):
```
/fsx-shared/juncheng/EHR/venvs/mcp_ehr/bin/python ./src/run_mcp_server.py \
  --mode http --host 127.0.0.1 --port 5103 \
  --disable-knowledge-tools \
  --data_path /fsx-shared/juncheng/EHR/data/MIMICIVAgentBench
```

Key points:
- Uses a **separate venv** `venvs/mcp_ehr/` (Python 3.12, fastmcp, BioLord
  embedder for the semantic-similarity candidate tool — holds ~1.3 GB on
  the chosen GPU).
- `--disable-knowledge-tools` is **baked in** — so the agent doesn't have
  knowledge lookup tools available. Only 7 EHR tools + 3 browser + 1 finish.
- `DATA_PATH` must point to `.../data/MIMICIVAgentBench` (which has
  subdirs `all/`, `common/`, `database/`, `item_set/`, `rare/`).
- The MCP stays up for the full experiment; it's CPU + one-GPU-slot for
  embeddings, not part of the training GPU pool.

**Preflight check** (launcher does this before training starts):
```bash
curl -s -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  http://127.0.0.1:5103/mcp | head -c 300
# expect: JSON response with "serverInfo":{"name":"EHR Service"
```
If the MCP is down, the launcher preflight aborts before starting training.

### Credentials (put in `/fsx-shared/juncheng/EHR/.env`)
```
WANDB_API_KEY=<your key>        # wandb logging
AWS_BEARER_TOKEN_BEDROCK=<key>  # Claude Haiku summarizer via Bedrock
# optional: CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID / _SECRET_ACCESS_KEY
# or:       CONTEXT_SUMMARIZER_BEDROCK_KEY=base64(key:secret)
```
Launcher sources `.env` and threads these into Ray workers via `RAY_RUNTIME_ENV`.

---

## 3. Code state — in-tree edits that MUST survive

The verl source lives at `/fsx-shared/juncheng/EHR/verl/` (editable, **no `.git`**).
Seven files are surgically patched. `verl_rl_ehr/patches/verify_verl_patches.py`
greps for sentinel strings and fails loud if any is missing — run it in the
launcher preflight (already wired).

### 3.1. `verl/verl/workers/config/rollout.py` — `MultiTurnConfig`
Adds 9 fields for context reset + Bedrock Haiku summarizer:
```python
context_reset_enabled: bool = False
context_reset_threshold: int = 0
context_reset_message: str = "[Context was reset...]"
context_reset_summarizer_enabled: bool = False
context_reset_summarizer_model: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
context_reset_summarizer_region: str = "us-east-1"
context_reset_summarizer_max_tokens: int = 1024
context_reset_summarizer_aws_key: Optional[str] = None
context_reset_summarizer_aws_secret: Optional[str] = None
```
Also adds `"context_reset_enabled"` and `"context_reset_threshold"` to `_mutable_fields`.

### 3.2. `verl/verl/experimental/agent_loop/tool_agent_loop.py` — agent loop core

Major additions (all already committed to the working tree):

1. **AgentData state for context reset** (lines ~95-98):
   `_context_was_reset`, `_reset_generation_prefix`, `_post_reset_offset`, `_num_resets`.

2. **`_get_generation_prompt(agent_data)`** — returns the token list fed to
   sglang. Short-circuits to `_reset_generation_prefix + prompt_ids[_post_reset_offset:]`
   once a reset has fired, keeping vLLM under `max_model_len` without
   shrinking the training trajectory.

3. **`_maybe_reset_context(agent_data)`** — threshold-gated wrapper.

4. **`_do_context_reset(agent_data, reason=...)`** — unconditional body
   (shared between threshold path and turn-limit-rescue path). Invokes the
   Claude Haiku summarizer via `self.context_summarizer.summarize(messages)`
   when enabled; falls back to the static `context_reset_message` on any
   boto3 error. Re-tokenizes `[system, user, <reset note>]` with
   `apply_chat_template(..., tools=self.tool_schemas, ...)` and stashes
   `_reset_generation_prefix`. Records `(reason, index)` on
   `agent_data.metrics["context_reset_reasons"]`.

5. **`_turn_limit_rescue(agent_data)`** — fires on
   `assistant_turns >= max_assistant_turns` (or user variant). Does a
   `_do_context_reset(reason="turn_limit")` **and** extends the turn budget
   via `agent_data.metrics["max_assistant_turns_effective"]`
   = `agent_data.assistant_turns + extension`. Current tuning (v7):
   - `extension = self.max_assistant_turns` (i.e. each rescue doubles the
     remaining budget — ~18 extra turns at the configured
     `max_assistant_turns: 18`).
   - Up to **`MAX_TURN_LIMIT_RESCUES = 2`** rescues per trajectory. After
     two rescues the trajectory falls through to `_maybe_force_answer` on
     any subsequent turn-limit hit.
   - **Per-agent-data caps** (not mutating `self.max_*`) since the
     ToolAgentLoop instance is shared across concurrent rollouts.
   - `agent_data.metrics["turn_limit_rescues"]` holds the rescue count;
     `"turn_limit_rescued"` bool is kept in sync for legacy consumers.

6. **`_maybe_force_answer(agent_data)`** — last-resort fallback (used only
   if rescue disabled / fails, or on `response_length` exhaustion where a
   reset wouldn't help). Injects the Qwen3-native assistant prefix ending
   in `<answer>` so the model can submit at least a graded attempt.

7. **Three turn-limit call sites** in `_handle_generating_state` prefer
   rescue → force-answer → terminate (in that order). The
   `response_length` check only does force-answer (reset can't shrink the
   response mask).

8. **`_handle_interacting_state`**: if the interaction handler returns
   `(False, "", ...)` (empty response, don't terminate), **skip injecting
   the empty user turn** — the agent loop just goes back to GENERATING.
   This is what stops the repeated `"Please submit your final answer via
   ehr.finish."` nag from cluttering trajectories. Paired with the
   interaction change in 3.4.

9. **Fallback tokenization** wraps two `apply_chat_template(remove_system_prompt=True)`
   call sites in try/except — Qwen3.5's strict chat template rejects a
   standalone `[{"role":"tool", ...}]` delta. Fallback encodes
   `<|im_start|>tool\n{content}<|im_end|>\n` directly.

10. **`_finalize_output`** (end of `run`): seeds defaults on
    **`output.extra_fields`** (NOT `agent_data.extra_fields` — pydantic copies
    dicts on construction) for `final_answer`, `final_answer_submitted`,
    `turn_scores`, `tool_rewards`, `num_context_resets`,
    `forced_answer_injected`, `turn_limit_rescued`, `turn_limit_rescues`
    (int count). Required: `DataProto.concat` asserts all samples share the
    same keys, and some rollouts never populate `final_answer` (only rollouts
    that hit `ehr.finish` do).

11. **Log tags for observability** — rescue/reset/force-answer events are
    logged at `WARNING` level (visible at default verl log level) with
    grep-friendly tags:
    - `[RESCUE]` — turn-limit rescue fired (includes rescue count + new
      effective caps)
    - `[RESET]` — context reset body ran (includes reason: `threshold` or
      `turn_limit`, and new prompt size)
    - `[FORCE_ANSWER]` — forced `<answer>` prefix injection fallback
    Verify the mechanism is working with:
    ```bash
    grep -c "\[RESCUE\]" logs/grpo_*.log
    grep -c "\[RESET\]"  logs/grpo_*.log
    grep -c "\[FORCE_ANSWER\]" logs/grpo_*.log
    ```

### 3.3. `verl/verl/tools/schemas.py`

Adds `model_config = ConfigDict(extra="allow")` to
`OpenAIFunctionPropertySchema`, `OpenAIFunctionParametersSchema`,
`OpenAIFunctionSchema` — needed so tool schemas with `default` / `anyOf`
fields survive pydantic validation.

### 3.4. `verl/verl/experimental/agent_loop/context_summarizer.py` — NEW FILE

Ported verbatim from OpenResearcher researcher_v2. Calls Claude Haiku
(`global.anthropic.claude-haiku-4-5-20251001-v1:0`) via boto3
`bedrock-runtime` client in a `ThreadPoolExecutor`. Credential resolution:
config fields → `CONTEXT_SUMMARIZER_AWS_*` env → `CONTEXT_SUMMARIZER_BEDROCK_KEY`
(base64) → boto3 default chain (picks up `AWS_BEARER_TOKEN_BEDROCK`).
Truncates input to ~12K chars, strips `<think>` blocks before summarizing,
returns `None` on any boto3 error (caller falls back to static reset).

### 3.5. `verl/verl/trainer/ppo/ray_trainer.py`

Two surgical edits:

1. **`_dump_generations` json default**: numpy int64/float64/bool_/ndarray
   aren't JSON-serializable. Added a `_json_default` helper that calls
   `.item()` / `.tolist()`. Without it, `rollout_data_dir` dumping crashes
   at step 2.

2. **`rollout_dump_freq` gate** (line ~1575): the original
   `if rollout_data_dir: _log_rollout_data(...)` ran **every step**. Now
   gated by `trainer.rollout_dump_freq` (default 1), so you can pass
   `+trainer.rollout_dump_freq=5` (or 2) to dump periodically.

### 3.6. `verl_rl_ehr/preprocess/build_ehr_rl_parquet.py` — `<answer>` nudge

`_FINISH_NUDGE` (v2) was replaced with `_ANSWER_NUDGE`. The SFT'd model
only fired `ehr.finish` in ~5% of rollouts, but emits `<answer>...</answer>`
tags in ~85% when prompted. System prompt = `DEVELOPER_CONTENT_CLAUDE` +
a paragraph mandating `<answer>[\"item\", \"item\"]</answer>` format.

### 3.7. `verl_rl_ehr/interactions/ehr_evaluation_interaction.py` — no-nag

When no answer is extracted, the interaction now returns `(False, "", 0.0, {"no_answer": True})`
(empty string, do-not-terminate) instead of the old "Please submit your
final answer via ehr.finish." nag. Paired with 3.2-8: the agent loop sees
empty + `should_terminate=False` and quietly re-enters GENERATING.

### 3.8. `verl_rl_ehr/reward/ehr_reward.py` — answer extractor

Three extraction strategies, in order of preference:
1. `<answer>...</answer>` regex (preferred; what the nudge steers toward).
2. Full JSON parse of the last `<tool_call>{…}</tool_call>` body, or
   the last `<function=ehr.finish>…</function>` body — pulls out
   `obj["arguments"]["answer"]` or `obj["answer"]`.
3. Regex fallback: `"answer":\s*(\[...\])` or `"answer":\s*"..."`.

Writes a JSONL row per sample to `$REWARD_DEBUG_LOG`
(default `/tmp/reward_debug_ehr.jsonl`). Reward =
`F1 + 0.1*tool_engagement + 0.05*efficiency − 0.2*format_penalty`,
clamped to `[-0.3, 1.15]`.

### 3.9. `verl_rl_ehr/tools/ehr_common_tool.py` — `ehr.load_ehr` short-circuit

`ehr.load_ehr` returns a ~1KB candidate-table listing that burns tokens
without adding clinical info. `EHRMCPTool.execute` short-circuits it to
`"EHR loaded. Use ehr.get_table_names to list available tables."` (~40 chars).
User confirmed this is fine — the model can call `ehr.get_table_names` if
it wants the table list.

### 3.10. sglang patches (applied into the venv)

`verl_rl_ehr/patches/apply_sglang_patches.sh` fetches the upstream
OpenResearcher Qwen3.5-MoE patches into `sglang/srt/models/qwen3_5.py`,
`sglang/srt/configs/qwen3_5.py`, and `sglang/srt/utils/hf_transformers_utils.py`.
Idempotent. Must be run once per fresh venv.

### 3.11. Tool parser (Hydra config)
**Critical** — was silently broken for weeks:
`verl_rl_ehr/config/ehr_multiturn_grpo_qwen35.yaml`:
```yaml
multi_turn:
  format: qwen3_coder    # was "hermes" — mismatch dropped every tool call
engine_kwargs:
  sglang:
    tool_call_parser: qwen3_xml
```
`qwen3_coder` is verl's registered name for the Qwen3XMLToolParser; sglang
calls the same wire format `qwen3_xml`. Both must be consistent with the
Qwen3.5 SFT emission format (`<tool_call><function=...><parameter=...>`).

**Why this matters especially for the SFT-ed model (v5 lineage):**

The SFT-ed Qwen3.5 model was trained to submit final answers via the
`ehr.finish` tool call (Qwen3-XML format). With the old `format: hermes`
config, the Hermes parser tried to `json.loads()` the Qwen3-XML blob inside
`<tool_call>...</tool_call>` and failed on **every** tool call (producing
the `Failed to decode tool call: Expecting value: line 2 column 1` flood we
saw earlier). Consequence:

- `extract_tool_calls` returned `[]` for every assistant turn.
- The agent loop routed the turn to `INTERACTING` state instead of
  `PROCESSING_TOOLS`.
- `EHRFinishTool.execute()` was **never called** — so
  `agent_data.extra_fields["final_answer"]` was never populated.
- The interaction couldn't find an answer → returned the
  "Please submit your final answer via ehr.finish." nag → model kept
  re-submitting → every attempt silently dropped.
- Reward regex `n_finish_calls > 0` matched the text blob, so the metric
  appeared non-zero, but the answer-extraction regex (written for
  Hermes JSON shape `"name":"ehr.finish"`) didn't match the Qwen3-XML
  text → `has_answer=False`.

So the SFT model was **submitting answers correctly all along** — we just
weren't routing them to the reward/interaction path. The parser flip +
reward extractor fix (section 3.8) together restore proper grading for the
SFT lineage.

### 3.12. Thinking mode
For v5 (SFT-ed model), **consider leaving `enable_thinking=False`** in the
Hydra data config:
```yaml
data:
  apply_chat_template_kwargs:
    enable_thinking: false
```
The SFT model was observed to open `<think>\n` from the chat-template
pre-seed and jump straight to `<tool_call>` without closing — unclosed
think blocks polluted trajectories. Base model doesn't have this issue,
so v6 has `apply_chat_template_kwargs: {}` (defaults to True). For v5
continuation: test both, the currently-committed yaml has `{}`.

---

## 4. Launch command for v5 (SFT-model GRPO, other node)

Exactly what to run on the other node:

```bash
cd /fsx-shared/juncheng/EHR

# 1) Start the MCP server on :5103 (if not already running)
bash scripts/run/run_mcp_server.sh 0 5103 &
sleep 10   # wait for it to come up

# 2) Source credentials + configure experiment
set -a; source .env; set +a
export CONTEXT_SUMMARIZER_ENABLED=true       # turn on Haiku via Bedrock
export EXPERIMENT_NAME=ehr_grpo_qwen35_v5c   # pick a fresh name per run
export REWARD_DEBUG_LOG=/tmp/reward_debug_ehr_${EXPERIMENT_NAME}.jsonl
: > "$REWARD_DEBUG_LOG"

ROLLOUT_DIR=/fsx-shared/juncheng/EHR/analysis/rollouts/${EXPERIMENT_NAME}
mkdir -p "$ROLLOUT_DIR"

# 3) Activate venv (python 3.10.12 inside)
source venvs/qwen3_5_rl/bin/activate

# 4) Preflight (exits 2 if any patch is missing)
python verl_rl_ehr/patches/verify_verl_patches.py

# 5) Launch
LOG=logs/grpo_${EXPERIMENT_NAME}_$(date +%Y%m%d_%H%M%S).log
nohup bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh \
  trainer.total_training_steps=50 \
  trainer.save_freq=10 \
  trainer.test_freq=10 \
  trainer.val_before_train=true \
  trainer.rollout_data_dir="$ROLLOUT_DIR" \
  +trainer.rollout_dump_freq=2 \
  actor_rollout_ref.rollout.gpu_memory_utilization=0.35 \
  actor_rollout_ref.rollout.free_cache_engine=false \
  actor_rollout_ref.actor.fsdp_config.param_offload=true \
  actor_rollout_ref.actor.fsdp_config.optimizer_offload=true \
  actor_rollout_ref.ref.fsdp_config.param_offload=true \
  >"$LOG" 2>&1 < /dev/null &
PID=$!; disown $PID
echo "v5 PID=$PID  LOG=$LOG"
```

**Hardware / GPU overrides are locked in for a reason — do NOT loosen:**
- `gpu_memory_utilization=0.35` + `free_cache_engine=false` + 3× FSDP
  `param_offload=true` is the exact memory profile that cleared the
  sglang `resume_memory_occupation` OOM during v4 debugging. Peak per-GPU
  memory with these settings is ~130 GB on H200 (143 GB total) during
  `update_actor`. Doubling `gpu_memory_utilization` will OOM.
- TP=4, N_GPUS=8 (single-node default baked into `run_grpo_qwen35_ehr.sh`).
  If the other node has a different GPU count, override `N_GPUS=` and
  `TP_SIZE=` in the env.

**The model used by v5:** `$MODEL_PATH` defaults to
`/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf` (the SFT symlink).
To be explicit: `export MODEL_PATH=/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf`
before the launcher.

**Turn-limit tuning (v7 defaults in the yaml):**
- `multi_turn.max_assistant_turns: 18` (up from 15 in v4/v5b/v6)
- `MAX_TURN_LIMIT_RESCUES = 2` (hard-coded class attribute on `ToolAgentLoop`)
- `extension = max_assistant_turns` in `_turn_limit_rescue` (each rescue
  doubles the remaining budget)

Total turn budget per rollout now scales: 18 turns → (rescue) 36 → (rescue) 54.
If you want shorter rollouts for a cheaper debug iteration, override:
`actor_rollout_ref.rollout.multi_turn.max_assistant_turns=10` (etc.).

### Supervision during the run
- **wandb:** project `deepmed-rl`, experiment matches `$EXPERIMENT_NAME`.
  Requires `WANDB_API_KEY` in env (sourced from `.env`).
- **reward trace:** tail `$REWARD_DEBUG_LOG` — one JSONL row per sample per
  step with `f1`, `score`, `has_format_signal`, `n_ehr_tool_calls`,
  `n_finish_calls`, `num_context_resets`, `forced_answer_injected`,
  `answer_extracted`.
- **full rollouts:** `${ROLLOUT_DIR}/{2,4,6,...}.jsonl` — 128 full samples
  per dump file with `input`, `output`, `gts`, `score`, all reward_extra
  keys. Readable by hand.
- **stderr:** watch for `ContextSummarizer.from_config failed` (silent
  summarizer breakage), sglang `resume_memory_occupation` timeouts (drop
  `gpu_memory_utilization` to 0.30), or `Failed to decode tool call`
  (upstream docs say benign but sudden flood = parser config regression).

---

## 5. Known issues + open items

### 5.1. Verifying rescue is firing (solved for v6; keep the diagnostic for v7)

**Quick test from reward-debug trace:** `num_turns` (stored per sample)
equals `user_turns + assistant_turns + 1`. If `num_turns > 2*max_turns+1`,
the rescue path must have run (since without rescue the cap is
`2*max_turns+1`).

- With `max_assistant_turns=15` (v6 setting), cap without rescue = 31.
  Observed dominant value: 44 → rescue extension of `+7 = max(5, 15//2)`
  fired on ~99.5% of rollouts. Mechanism confirmed working.
- With `max_assistant_turns=18` + `extension=18` + `MAX_TURN_LIMIT_RESCUES=2`
  (v7 setting), caps without rescue: 37; with 1 rescue: 73; with 2 rescues:
  109. If `num_turns` stays at 37, something's blocking rescue; if it
  floats between 38–73, one rescue fired; if 74–109, two fired.

**Log grep** is the other check — see section 3.2-11 for `[RESCUE]`,
`[RESET]`, `[FORCE_ANSWER]` tags.

### 5.2. Tool hallucination
The SFT model (and base) sometimes call `ehr.get_latest_records`,
`ehr.think`, or tables like `medical_conditions` / `deaths` / `drg` that
don't exist in our MCP schema. Current behavior: MCP returns an error,
EHRMCPTool wraps it in a `"Error executing {tool_name}"` ToolResponse.
Model usually doesn't recover. **Not fixed.** Options: add a retry+warn
tool-call filter, or fine-tune a tool-policy head.

### 5.3. ehr.finish rarely fires
Base model: 0% `ehr.finish` (submits via `<answer>` tag instead, which is
fine).
SFT model: ~5% `ehr.finish` (the other 95% either submit `<answer>` after
the nudge, or don't submit at all).
Reward extractor handles both paths, so this is mostly a cosmetic observation.

### 5.4. Summarizer rarely triggers at threshold=14000
With `max_assistant_turns=15` and current response lengths (~3200 tokens
mean), generation prompts stay well below the 14K threshold. If you want
to force the summarizer path: drop `context_reset_threshold` to ~4000 or
raise `max_assistant_turns` to 30+.

### 5.5. "zombies" appear in `pgrep ray::`
After killing a run, Ray workers show as `<defunct>` (Zombie, `stat=Z`).
They don't hold GPU memory (`fds=0, nvidia_maps=0`) — init will reap them
eventually. `ray stop` shortcuts it.

---

## 6. Key on-disk artifacts (reference)

| Path | What |
|---|---|
| `/fsx-shared/juncheng/EHR/verl/` | editable verl install (no `.git`) |
| `/fsx-shared/juncheng/EHR/verl_rl_ehr/` | our RL pipeline (tools, config, reward, preprocess, scripts) |
| `/fsx-shared/juncheng/EHR/venvs/qwen3_5_rl/` | Python 3.10 venv with pinned stack |
| `/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf` | symlink → SFT HF weights (the model v5 uses) |
| `/fsx-shared/juncheng/EHR/models/Qwen3.5-35B-A3B` | pre-SFT base weights (v6 lineage only) |
| `/fsx-shared/juncheng/EHR/data/ehr_rl_qwen35/{train,val}.parquet` | training data (2922 + 326 rows) |
| `/fsx-shared/juncheng/EHR/checkpoints/deepmed-rl/` | ckpt output root; subdirs per experiment |
| `/fsx-shared/juncheng/EHR/analysis/rollouts/<exp>/` | per-step rollout dumps (when `rollout_data_dir` is set) |
| `/fsx-shared/juncheng/EHR/logs/grpo_*.log` | launcher tee'd stderr+stdout |
| `/fsx-shared/juncheng/EHR/docs/multiturn_rl_training.md` | older runbook — has historical bug-fix log |
| `/fsx-shared/juncheng/EHR/verl_rl_ehr/patches/verify_verl_patches.py` | patch sentinel checker; runs in preflight |

---

## 7. Quick sanity-check pre-flight (copy-paste on new node)

```bash
cd /fsx-shared/juncheng/EHR

# venv works
./venvs/qwen3_5_rl/bin/python -c "import torch, transformers, sglang, ray; print(torch.__version__, transformers.__version__, sglang.__version__, ray.__version__)"
# expect: 2.9.0  5.5.3  0.5.9  2.54.0

# patches intact
./venvs/qwen3_5_rl/bin/python verl_rl_ehr/patches/verify_verl_patches.py
# expect: "verl patches OK"

# SFT weights visible
ls checkpoints/qwen3_5_35b_a3b_sft_hf/config.json
# expect: the file exists

# parquets visible
./venvs/qwen3_5_rl/bin/python -c "import pyarrow.parquet as pq; t = pq.read_table('data/ehr_rl_qwen35/train.parquet', columns=['prompt']); print(t.num_rows, 'rows')"
# expect: 2922 rows

# MCP reachable
curl -s -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  http://127.0.0.1:5103/mcp | head -c 200
# expect: JSON with "serverInfo":{"name":"EHR Service"

# bedrock creds work
./venvs/qwen3_5_rl/bin/python -c "
import os, sys; sys.path.insert(0, 'verl')
os.environ['CONTEXT_SUMMARIZER_ENABLED'] = 'true'
from verl.experimental.agent_loop.context_summarizer import ContextSummarizer
class Cfg:
    context_reset_summarizer_enabled = True
    context_reset_summarizer_model = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'
    context_reset_summarizer_region = 'us-east-1'
    context_reset_summarizer_max_tokens = 200
s = ContextSummarizer.from_config(Cfg())
import asyncio
print(asyncio.run(s.summarize([{'role':'assistant','content':'I found diabetes and hypertension.'}]))[:200])
"
# expect: a bullet-point summary
```

If all five pass, launch with section 4.
