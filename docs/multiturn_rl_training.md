# Multi-turn RL Training — Setup & Runbook

End-to-end setup for multi-turn GRPO of Qwen3.5-35B-A3B on EHR-Bench,
with **context reset** (+ optional Haiku-via-Bedrock summarization)
to keep long trajectories from blowing past sglang's `max_model_len`.

**Scope.** This doc targets `verl_rl_ehr/`, which layers on top of the
in-tree editable verl at `$REPO/verl/` and uses sglang 0.5.9 as the
rollout engine. The vLLM path was tried and abandoned — see §8 if you
are curious why.

**Conventions.** `$REPO = /fsx-shared/juncheng/EHR`. Paths, counts, and
versions were verified on **2026-04-27** against the running tree after
the first successful 1-step smoke (150 s/step on 4×H200).

---

## 0. Overall pipeline structure

```
┌─────────────────────────────────────────────────────────────────────────┐
│ (0) ONE-TIME SETUP                                                      │
│  - venv:  $REPO/venvs/qwen3_5_rl       (separate from SFT venv)         │
│  - verl surgery (3 files, in-tree)                                      │
│  - sglang patch (Qwen3.5-MoE VL arch)                                   │
│  - HF-weights symlink from SFT global_step_*                            │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │
                           v
┌─────────────────────────────────────────────────────────────────────────┐
│ (1) PREPROCESS                                                          │
│  verl_rl_ehr/preprocess/build_ehr_rl_parquet.py                         │
│    reads data/MIMICIVAgentBench/common/*_500.json                       │
│    writes data/ehr_rl_qwen35/{train,val}.parquet                        │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │
                           v
┌─────────────────────────────────────────────────────────────────────────┐
│ (2) LAUNCH SERVICES                                                     │
│  - EHR MCP on :5103   (bash scripts/run/run_mcp_server.sh 0 5103 &)     │
│  - optional web search stack (we use BROWSER_SEARCH_MODE=stub for now)  │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │
                           v
┌─────────────────────────────────────────────────────────────────────────┐
│ (3) TRAIN                                                               │
│  bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh                        │
│    verl.trainer.main_ppo  --config-name ehr_multiturn_grpo_qwen35       │
│    rollout.name=sglang    →    sglang HTTP servers per replica          │
│    FSDP actor + sglang hybrid engine (one node, 8×H200 default)         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Layout

```
$REPO/
  verl/                             # editable verl (no .git); 3 in-tree surgical edits
  verl_rl_ehr/                      # EHR-specific RL scaffolding
    tools/                          # BaseTool subclasses (MCP EHR + browser + finish)
    interactions/                   # BaseInteraction subclass grading final answer
    reward/ehr_reward.py            # custom_reward_function for verl
    config/
      ehr_multiturn_grpo_qwen35.yaml    # top-level Hydra recipe
      tool_config/ehr_tool_config.yaml  # 11 tools: 3 browser + 7 EHR + ehr.finish
      interaction_config/ehr_interaction_config.yaml
    preprocess/build_ehr_rl_parquet.py
    patches/
      verify_verl_patches.py        # preflight sentinel-check
      apply_sglang_patches.sh       # upstream Qwen3.5-MoE sglang patches
    scripts/
      smoke_test_1step.sh           # 1-step smoke on 4 GPUs
      run_grpo_qwen35_ehr.sh        # full run launcher
      convert_sft_ckpt_to_hf.sh     # rarely needed — HF weights already beside Megatron ckpts
  data/ehr_rl_qwen35/               # preprocessed parquet (2922 train / 326 val)
  checkpoints/qwen3_5_35b_a3b_sft_hf     # symlink → SFT global_step_420/huggingface
  checkpoints/deepmed-rl/                # RL outputs land here
```

---

## 2. Venv stack (known-good pins)

Anchor is the upstream OpenResearcher researcher_v2 recipe
(`docs/04092026_4node_rl_environment.md` and `04172026_10node_env_fixes.md`).
**Do not drift from these pins** unless you want to rebuild flash-attn from
source (≈15 min) and/or redo the whole vllm/torch ABI cascade.

| Package   | Version                 | Notes |
|-----------|-------------------------|---|
| Python    | 3.10.12                 | must match across any future Ray cluster |
| torch     | 2.9.0+cu128             | anchor; everything ABI-compatible with this |
| vllm      | 0.13.0                  | installed but unused; sglang is the rollout backend |
| transformers | 5.5.3                | forced via `uv pip install --no-deps` over vllm's `<5` constraint |
| huggingface_hub | ≥1.0              | transformers 5.5 requires it (`--no-deps` to bypass vllm pin) |
| accelerate | ≥1.13.0                | **critical**: 1.7–1.12 crash on `_is_hf_initialized` under `init_empty_weights` |
| sglang    | 0.5.9                   | `sglang[srt]==0.5.9 --no-deps` |
| sgl-kernel | 0.3.21                 | must match sglang 0.5.9 |
| flashinfer-python | 0.6.3           | `--no-deps` |
| flashinfer-cubin | 0.6.3            | must match flashinfer-python or triggers version-mismatch assertion |
| flash-attn | 2.8.3                  | **rebuilt from source** against torch 2.9; no prebuilt wheel for torch 2.9+cp310 in upstream releases |
| torch-memory-saver | 0.0.9          | 0.0.6 ships without the `.torch_memory_saver` attribute sglang reads at import |
| decord    | 0.6.0                   | sglang's `qwen_vl.py` processor imports it; `import_processors` silently swallows the ImportError, so without decord the Qwen3_5 arch never registers |
| qwen-vl-utils | 0.0.14              | verl's `rl_dataset.process_vision_info` imports it |
| boto3     | 1.42                    | required if `context_reset_summarizer_enabled=true` |
| ray       | 2.54.0                  | carried over from SFT venv |
| Bedrock auth | `AWS_BEARER_TOKEN_BEDROCK` + `AWS_REGION=us-east-1` | falls through boto3 default chain |

**Purge cu13 packages.** sglang install pulls in `nvidia-nccl-cu13`,
`nvidia-cudnn-cu13`, `nvidia-cusparselt-cu13`, `nvidia-nvshmem-cu13`.
Their `libnccl.so.2` overwrites the cu12 variant inside torch's RPATH,
producing a `CUDA driver version is insufficient` runtime error during
NCCL ops. Uninstall the cu13 flavors and `--force-reinstall` the cu12
ones. Our launcher does not do this automatically — `apply_sglang_patches.sh`
handles the qwen3_5 code patches but not the NCCL purge.

**Smoke import check (always works when the venv is healthy):**

```bash
/fsx-shared/juncheng/EHR/venvs/qwen3_5_rl/bin/python - <<'PY'
import torch, transformers, vllm, accelerate, flash_attn_2_cuda, verl, ray
from vllm.config.model import ModelConfig
from sglang.srt.entrypoints.engine import Engine
from sglang.srt.models.qwen3_5 import Qwen3_5MoeForConditionalGeneration
from sglang.srt.managers.multimodal_processor import import_processors, PROCESSOR_MAPPING
import_processors("sglang.srt.multimodal.processors")
assert any("Qwen3_5Moe" in c.__name__ for c in PROCESSOR_MAPPING.keys())
from transformers import AutoTokenizer
AutoTokenizer.from_pretrained("/fsx-shared/juncheng/EHR/checkpoints/qwen3_5_35b_a3b_sft_hf",
                              trust_remote_code=True)
print("OK")
PY
```

---

## 3. Verl in-tree surgery (3 files)

Enforced by `verl_rl_ehr/patches/verify_verl_patches.py`; run it before
every training launch (the launcher does this automatically).

### 3a. `verl/verl/workers/config/rollout.py::MultiTurnConfig`

Adds context-management knobs:

```python
context_reset_enabled:         bool = False
context_reset_threshold:       int  = 0
context_reset_message:         str  = "[Context was reset to manage length. ...]"
context_reset_summarizer_enabled:    bool = False
context_reset_summarizer_model:      str  = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
context_reset_summarizer_region:     str  = "us-east-1"
context_reset_summarizer_max_tokens: int  = 1024
context_reset_summarizer_aws_key:    Optional[str] = None
context_reset_summarizer_aws_secret: Optional[str] = None
```

### 3b. `verl/verl/experimental/agent_loop/tool_agent_loop.py`

Four edits:
1. **AgentData reset state** — four new fields for tracking in-place reset.
2. **`_get_generation_prompt()` + `_maybe_reset_context()`** — when the
   generation prompt crosses `context_reset_threshold`, swap the vLLM
   input to `[system, original user task, reset note]` (optionally with
   a Haiku summary of findings) while `prompt_ids` continues to
   accumulate the full trajectory for training / reward / `response_mask`.
3. **`_maybe_force_answer()`** — on turn-limit or response-length hit,
   append a short `<answer>` prefix and re-enter `GENERATING` once so
   the model has a chance to close out instead of hitting a zero-reward
   cutoff.
4. **Fallback tokenization** — `try/except` around
   `apply_chat_template(add_messages, remove_system_prompt=True)` in
   `_handle_processing_tools_state` + `_handle_interacting_state`;
   falls back to full-conversation re-tokenize + delta, then raw
   `<|im_start|>user\n<tool_response>…</tool_response><|im_end|>`
   encoding as last resort. Needed because Qwen3.5's chat template
   rejects standalone `[{"role":"tool", ...}]` lists.

Also wires `ContextSummarizer.from_config(mt)` in `ToolAgentLoop.__init__`
and calls it in `_maybe_reset_context` before falling back to the
static reset message.

### 3c. `verl/verl/tools/schemas.py`

```python
model_config = ConfigDict(extra="allow")       # 3 schema classes
type: str | list[str]                          # widen for browser.open's union `id`
```

Without this, tool-schema load drops `default` / union-type fields
(e.g. `browser.open`'s `id: ["integer", "string"]`).

### 3d. `verl/verl/experimental/agent_loop/context_summarizer.py` (new file)

Fetched verbatim from OpenResearcher researcher_v2 (commit 2026-04-26).
Calls Claude Haiku via `boto3.client("bedrock-runtime")` and wraps the
trajectory as bullet-point findings.

---

## 4. sglang upstream patch (Qwen3.5-MoE VLM)

Run once per fresh venv (idempotent — backs up originals to `*.orig`):

```bash
bash $REPO/verl_rl_ehr/patches/apply_sglang_patches.sh
```

This:
- drops upstream's patched `sglang/srt/{configs,models}/qwen3_5.py` + `utils/hf_transformers_utils.py` into the venv's site-packages;
- installs `decord` so the qwen_vl image processor actually registers (sglang's `import_processors` swallows any ImportError, so a missing dep manifests as `No processor registered for architecture: ['Qwen3_5MoeForConditionalGeneration']` at rollout launch instead of at import).

---

## 5. Preprocessing

One-time (or whenever you regenerate benchmark JSONs):

```bash
source $REPO/venvs/qwen3_5_rl/bin/activate
python $REPO/verl_rl_ehr/preprocess/build_ehr_rl_parquet.py \
  --in-dir  $REPO/data/MIMICIVAgentBench/common \
  --out     $REPO/data/ehr_rl_qwen35 \
  --val-frac 0.1
# optional: --limit-per-task 10  (smoke)
```

Writes `train.parquet` (2922) and `val.parquet` (326). Schema matches
`verl.utils.dataset.rl_dataset.RLHFDataset`:

```
data_source :: str   # e.g. "ehr_bench/diagnoses_ccs"
prompt      :: list[dict]   # [system, user]
ability     :: str   # "ehr-multiturn"
reward_model :: {"style": "rule", "ground_truth": list[str]}
extra_info  :: {
  tools_kwargs:        {tool_name: {"create_kwargs": {subject_id, prediction_time, task_type}}},
  interaction_kwargs:  {"name": "ehr_eval", "ground_truth": [...], "task_type": ...},
  need_tools_kwargs:   True,
  subject_id:          "13713802",
  prediction_time:     "2135-05-25 14:37:58",
  task_type:           "transfers",
}
```

Six tasks supported: diagnoses_ccs, procedures_ccs, labevents,
prescriptions, microbiologyevents, transfers. Stratified 90/10 split.

---

## 6. Launch

### 6a. Smoke test (4 GPUs, 1 step, ~10 min)

```bash
cd $REPO
# Start MCP once (leave running):
bash scripts/run/run_mcp_server.sh 0 5103 &     # GPU 0, port 5103

# Smoke:
bash verl_rl_ehr/scripts/smoke_test_1step.sh
```

The smoke script overrides:
- `data.train_batch_size=2`, `actor.ppo_mini_batch_size=2`, `rollout.n=2`, `rollout.agent.num_workers=2`
- `rollout.multi_turn.max_{assistant,user}_turns=4`
- `rollout.multi_turn.context_reset_threshold=2000` (tight enough to exercise the reset path on 4-turn rollouts; in practice the SFT trajectories are short so reset rarely fires at 2000 — if you want to force it, drop to 800)
- `rollout.max_model_len=4096`, `data.max_response_length=3072`
- `rollout.gpu_memory_utilization=0.35`, `rollout.free_cache_engine=false`
- `fsdp_config.param_offload=true`, `optimizer_offload=true` (both actor and ref)
- `trainer.total_training_steps=1`, `val_before_train=false`, `save_freq=-1`, `test_freq=-1`

Preflight `verify_verl_patches.py` + MCP probe run automatically.

### 6b. Full run (8 GPUs, default)

```bash
bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh
```

Defaults:
- 8 GPUs, TP=4 (two sglang replicas × 4 GPUs each)
- `rollout.multi_turn.max_assistant_turns=15`, `max_user_turns=15`
- `rollout.multi_turn.context_reset_threshold=14000`
  (vLLM/sglang `max_model_len=16384` minus 2k headroom)
- Adam lr `1e-6`, `clip_grad=0.5`, `kl_loss_coef=0.001`, `kl_loss_type=low_var_kl`
- `rollout.n=8`, GRPO advantages, `kl_ctrl.kl_coef=0.001`
- Checkpoints land under `$REPO/checkpoints/deepmed-rl/<experiment_name>/`
- wandb project: `deepmed-rl`

Override via env:

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 N_GPUS=8 TP_SIZE=4 \
EXPERIMENT_NAME=ehr_grpo_qwen35_v0 \
CONTEXT_SUMMARIZER_ENABLED=true \
BROWSER_SEARCH_MODE=stub \
  bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh
```

The launcher builds `RAY_RUNTIME_ENV` to thread `SGLANG_USE_MESSAGE_QUEUE_BROADCASTER=false`,
`EHR_MCP_URL`, `BROWSER_SEARCH_MODE`, `REWARD_DEBUG_LOG`, `WANDB_API_KEY`,
and all Bedrock credentials into Ray worker processes. Without this Ray
worker child processes do not inherit env, and sglang deadlocks on
`resume_memory_occupation` (3 × 60 s timeouts → trainer death).

---

## 7. Context management (the headline feature)

Not a policy-model summarizer. When the vLLM/sglang prompt crosses
`context_reset_threshold` tokens, the agent loop:

1. **Optionally** calls Claude Haiku via Bedrock
   (`context_reset_summarizer_enabled=true`) to produce a bullet-point
   summary of the trajectory so far — ~500 words, 12k-char input cap,
   `max_tokens=1024`. Falls back to static reset message on any boto3
   error.
2. Re-tokenizes `[system, original user task, (summary +) reset note]`
   with the full tool schema so the model still knows its tools.
3. Sets `prompt_ids` unchanged (training trajectory), but routes
   subsequent vLLM/sglang generations through the shortened prefix.
4. Tracks `num_context_resets` in `AgentLoopOutput.extra_fields` (wandb).

Credentials for the summarizer:
- `AWS_BEARER_TOKEN_BEDROCK` + `AWS_REGION=us-east-1` (default boto3 chain),
- or `CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY`,
- or `CONTEXT_SUMMARIZER_BEDROCK_KEY` (base64-encoded `key:secret`).

Known trade-off (inherited from OpenResearcher): the actor re-computes
logprobs against the full `prompt_ids`, but sampled tokens saw the
shortened post-reset prompt. Mismatch is bounded by PPO ratio clipping
+ `response_mask=0` on tool tokens. If we see policy divergence spikes
correlated with `num_context_resets` in wandb, drop `actor.clip_ratio`
from 0.2 → 0.1.

### Forced `<answer>` prefix

On `assistant_turns >= max_assistant_turns` or
`response_mask + response_length >= rollout.response_length`, the loop
appends:

```
<|im_start|>assistant
<think>
I've reached my turn limit. Based on all my research so far, I need to provide my final answer now.
</think>

<answer>
```

and re-enters `GENERATING` once (with `ignore_termination=True`) so the
model has one more turn to close with `</answer>`. Fires at most once
per rollout, tracked in `extra_fields["forced_answer_injected"]`.

---

## 8. Why sglang, not vLLM

Upstream OpenResearcher uses sglang for all Qwen3.5-35B recipes
(`verl_rl/run_grpo_fullparam_qwen35_35b.sh` and
`run_grpo_lora_qwen35_35b_sglang.sh`). vLLM is only used for their
Qwen3-8B baseline.

The vLLM path led to a dependency cascade (vllm 0.11 wants
transformers 4.x → can't read `TokenizersBackend`; vllm 0.13 wants
transformers <5 but we need 5.x for Qwen3.5; vllm 0.19 forces torch 2.10
and cu13 → NCCL breaks). sglang 0.5.9 works directly on the upstream
pinned stack with Qwen3.5-MoE after applying their three-file patch.

---

## 9. Smoke-test baseline (2026-04-27, 4× H200, 1 step)

Measured in the wandb run `ehr_grpo_qwen35_smoke` (project
`deepmed-rl`):

| Stage | Time |
|---|---|
| Rollout generation (sglang) | 16.84 s |
| Reward compute | 0.03 ms |
| Ref-model logprobs | 68.89 s |
| Advantage | 6.7 ms |
| Actor update | 33.06 s |
| **sglang weight-sync** | **14.24 s** |
| **Total step** | **150.93 s** |

Rollout metrics:
- 9 turns per rollout
- 8–10 EHR tool calls per rollout
- 0 `ehr.finish` calls → every rollout scored `-0.20` (format penalty floor)
- 0 `num_context_resets` (rollouts stayed under 2000-token smoke threshold)
- `actor/grad_norm: 0.00115`, `actor/kl_loss: 0.00119` — tiny, stable

Reward trace at `/tmp/reward_debug_ehr_smoke.jsonl` (one JSONL row per
sample). Sample from smoke:

```json
{"data_source": "ehr_bench/procedures_ccs",
 "ground_truth": ["Injections and aspirations of muscles...", ...],
 "answer_extracted": null, "num_turns": 9,
 "n_ehr_tool_calls": 8, "n_browser_tool_calls": 0, "n_finish_calls": 0,
 "score": -0.2, "f1": 0.0,
 "has_answer": false, "has_format_signal": false}
```

---

## 10. Known issues / open items

- **0% `ehr.finish` rate from the SFT model.** In the smoke, the agent
  runs the full turn budget using EHR tools but never submits via
  `ehr.finish` — so the reward function only ever returns the format
  penalty. This is an SFT behavior, not a pipeline bug (`_maybe_force_answer`
  didn't fire because the rollouts hit the response-length limit, not
  the turn limit). For the full run, consider: (a) nudging the system
  prompt with "you MUST call ehr.finish to submit your final answer",
  (b) adding a soft-terminal penalty that stops the trajectory after
  N turns without progress, (c) using the forced-answer prefix more
  aggressively (e.g. at 80% of `max_assistant_turns`).

- **Haiku summarizer not yet exercised.** Disabled in smoke. Credentials
  (`AWS_BEARER_TOKEN_BEDROCK`) are in env and boto3 + Bedrock SDK calls
  work in isolation from the RL venv. To enable, export
  `CONTEXT_SUMMARIZER_ENABLED=true` before the launcher.

- **wandb teardown `BrokenPipeError`.** Appears in the smoke log after
  the trainer exits cleanly — harmless, from the wandb service
  connection closing after the main process wrote its final metrics.

- **`num_context_resets` not exercised.** Threshold 2000 was loose
  enough that 9-turn rollouts stayed under it. To force the reset
  path in a future smoke: drop to 800 tokens.

- **Multi-node.** Our current launcher assumes single-node. Upstream's
  `run_grpo_fullparam_qwen35_35b.sh` adds Ray cluster coordination and
  needs `NNODES` env wiring. Not yet ported.

---

## 11. Reproduce the session's bug fixes (for posterity)

The smoke passed on the 7th attempt. The bugs, in order:

1. MCP default port 5003 → canonical 5103.
2. Preflight GET → POST (FastMCP rejects GET with 406).
3. `train_batch_size < ppo_mini_batch_size` — smoke now overrides both.
4. Megatron→HF conversion unnecessary; verl SFT training already saves
   safetensors in every `global_step_*/huggingface/`. Symlink instead.
5. `rope_theta` under `rope_parameters` in transformers 5.x Qwen3.5
   configs — patched `verl/models/mcore/model_initializer.py` to fall
   back; patched `convert_dist_ckpt_to_hf.py` to flatten.
6. Transformers/vllm ABI cascade → switched to sglang per upstream.
7. `cp -a` preserves hardcoded venv paths in `bin/activate` — sed
   rewrite required when cloning venvs.
8. Transformers 4.57.1 can't load Qwen3.5 tokenizer (`TokenizersBackend`
   is 5.x-only).
9. vLLM 0.12 `ALLOWED_LAYER_TYPES` missing in transformers 5.3.
10. Flash-attn's build auto-bumps torch; use `--no-deps --no-build-isolation`.
11. Pinned to upstream recipe.
12. Flash-attn rebuilt against torch 2.9 (~15 min).
13. Ported `ContextSummarizer` (Haiku via Bedrock) from researcher_v2
    2026-04-26 commit.
14. sglang `get_hf_text_config` asserted on dict `text_config` — fixed
    by upstream's three-file Qwen3_5 sglang patch.
15. `decord` missing → sglang's `qwen_vl.py` processor silently fails
    to import → `No processor registered for architecture:
    Qwen3_5MoeForConditionalGeneration`.
16. `qwen-vl-utils` missing — verl's dataset imports it.
17. `DataProto 4 chunk 8` — smoke has 4 prompts but default
    `agent.num_workers=8`. Override to 2.
18. Hydra struct-mode rejects new config keys — every `context_reset_*`
    field must also live in the yaml (not just the dataclass).
19. `SGLANG_USE_MESSAGE_QUEUE_BROADCASTER=false` threaded via Ray
    runtime env.
20. `gpu_memory_utilization=0.6` + `free_cache_engine=true` → sglang
    OOM in `torch_memory_saver` during `resume_memory_occupation`.
    Fix: `0.35` + `free_cache_engine=false` + FSDP offloads.

If you hit an unfamiliar error during a future launch, compare against
this list first.
