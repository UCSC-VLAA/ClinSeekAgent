# Long-context multi-turn GRPO: the OOM marathon (v5h → v5y)

**Date range:** 2026-04-28 through 2026-04-30.
**Outcome:** First successful long-context (56K response_length) multi-turn GRPO run
(experiment `ehr_grpo_qwen35_v5y`). 12 clean steps, score +0.41 → +0.47, F1 0.205 → 0.232.

This document records every runtime failure mode encountered while pushing the
Qwen3.5-35B-A3B SFT checkpoint from v5h's 20K response budget toward a true
long-context RL run at 56K. Each subsection is one failure site plus the
actual fix that advanced us past it. Some fixes were wrong and had to be
rolled back; those are called out. **Read with the runbook
[`multiturn_rl_long_context_runbook.md`](../multiturn_rl_long_context_runbook.md)
beside it — the runbook is the "what to do"; this log is the "what broke and
what it taught us".**

All runs share the same model (SFT-ed Qwen3.5-35B-A3B), launcher
(`verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh`), and overall task set
(EHR-Bench 6-task mix). All variables below are Hydra overrides to that
script unless otherwise noted.

---

## Table of runs

| Run | Date | Key change vs. previous | Outcome |
|---|---|---|---|
| v5h | 04-28 | baseline: 20K resp, Ulysses off, old reward | 6 clean steps (score 0.15 → 0.28), OOM at step-7 backward |
| v5j | 04-28 | +new reward (2·F1 + 0.2·eff − 0.2·fmt_pen) | 6 clean steps, trend 0.00 → 0.07 |
| v5k | 04-28 | per-component wandb reward logging | stopped after preflight (proved components accessible) |
| v5l | 04-28 | +Serper search HTTP mode | 3 clean steps, ~0.07 plateau |
| v5m | 04-28 | sliding-window reset (keep-last-N) | 21 clean steps, has_answer rate collapsed 73% → 34%. Reverted. |
| v5n | 04-28 | +Ulysses SP=2 + summarizer reset restore | crashed, config field wrong |
| v5o | 04-28 | SP=4, `val_before_train=false`, 56K | killed before step 1 (stuck in rollout generation, 52K threshold not yet wired) |
| v5p | 04-28 | `force_answer_token_threshold=52000`, `context_reset_max_count=10000` | infinite rollout loop (hard-cap branch called rescue) |
| v5q | 04-28 | fix hard-cap branch: no rescue, just terminate | OOM 107 GiB at `lm_head` in `compute_log_prob`, mbsz=4 |
| v5r | 04-28 | SP=4 → 8 | identical 107 GiB OOM (Ulysses never sliced) |
| v5s | 04-28 | mbsz=1 | past lm_head; flash-attn CE `AssertionError` |
| v5t | 04-29 | mbsz=2 | same shape-assertion bug |
| v5u | 04-29 | `use_fused_kernels=true` (torch) + SP=8 | Hydra config-key error (wrong path for backend selector) |
| v5v | 04-29 | corrected path `fused_kernel_options.impl_backend` | died with `+` prefix collision |
| v5w | 04-29 | drop the `+`; fused + SP=8 + mbsz=2 | different bug: `pad_input shape mismatch: [917823,1] vs [114727,1]` |
| v5x | 04-29 | SP=4 + mbsz=1 no fused | same flash-attn CE shape assertion as v5s/v5t |
| **v5y** | **04-29** | **+verl `monkey_patch.py` branch for `qwen3_5_moe`** | **WORKING.** 12 clean steps, steady F1 climb. |

---

## The actual bugs and fixes

### Bug 1: OOM in `loss.backward()` during actor update
**First seen:** v5h, step 7.
**Traceback:**
```
dp_actor.py:667, loss.backward()
→ torch autograd engine
→ fla/ops/gated_delta_rule/chunk.py::l2norm_bwd
→ CUDA out of memory. Tried to allocate 12.10 GiB. 8.91 GiB free.
```
**Site:** backward of the policy update, in a Triton kernel that builds
additional intermediate buffers past what FSDP + gradient-checkpointing had
allocated during forward.

**Contributing factors:** `max_response_length=20000`, mbsz=1,
`gpu_memory_utilization=0.35` for sglang. Peak `perf/max_memory_allocated_gb`
on step 6 was 79.6 GB per GPU.

**Diagnosis:** the backward pass was the memory cliff, not rollout.
Reset/summarizer doesn't help here — they only cap sglang-side KV cache.
Reducing mbsz doesn't help — it's already 1. Backward activation memory
scales with `response_length`.

**Fix applied much later, via v5y:** Ulysses sequence parallelism (SP=8)
shards the sequence dimension across 8 ranks at the attention layer,
reducing per-rank activation memory by ~8× for the attention block and by
a smaller factor for MLP. But: getting Ulysses to actually take effect on
Qwen3.5-MoE required Bug 6's monkey-patch fix.

---

### Bug 2: Sliding-window reset collapses submission rate
**First seen:** v5m.
**Context:** in v5k/l, we observed that summarizer-based resets (Claude
Haiku summarizing prior rounds into a brief "here's what you found" note)
kept rollouts submitting answers ~73% of the time. To simplify, v5m switched
to a naive sliding-window reset — keep `[system, original_user, last N
rounds]`, drop the middle verbatim.

**Symptom:** `has_answer` rate collapsed from 73% → 34%. Model kept
restarting exploration from scratch after each reset because the truncated
tail didn't give it enough context to resume reasoning.

**Fix:** restored summarizer as the primary path. Kept sliding-window as an
optional `context_reset_mode: "sliding_window"` knob for experiments.

**Config surface:** added `MultiTurnConfig.context_reset_mode: str`
("summarizer" | "sliding_window"), default "summarizer".

---

### Bug 3: Hard-cap rescue loop
**First seen:** v5p.
**Symptom:** rollouts reached `len(prompt_ids) = 95,391` — far past the
supposed `max_response_length=56000` wall. No errors, but each rollout
burned hours and never terminated.

**Root cause:** the termination check at `len(response_mask) >=
response_length` fell through to `_turn_limit_rescue`, which unconditionally
did a context reset + turn-budget extension and returned True. After
`context_reset_max_count=10000` effectively removed the rescue cap, the
rescue-> generate -> hit cap -> rescue cycle was unbounded.

**Fix (v5q):** In the hard `response_length` exhaustion branch of
`_handle_generating_state`, skipped `_turn_limit_rescue`. The ladder is now:
force-answer once, else TERMINATE. Turn-limit rescues only fire when
`assistant_turns >= max_assistant_turns` (a soft cap); the hard response-
length cap is a final wall.

---

### Bug 4: Pre-slice materialization OOM in `compute_log_prob`
**First seen:** v5q (mbsz=4, SP=4), v5r (mbsz=4, SP=8).
**Traceback:**
```
compute_log_prob → _forward_micro_batch line 244
→ self.actor_module(input_ids=input_ids_rmpad, ...)
→ Qwen3_5MoeForCausalLM.forward line 2137
→ logits = self.lm_head(hidden_states[:, slice_indices, :])
→ F.linear
→ CUDA OOM. Tried to allocate 106.95 GiB.
```

**Math:** 107 GiB = `log_prob_micro_batch_size_per_gpu × response_length × vocab × 2 bytes`
= `4 × 56000 × 262144 × 2` = 106.9 GiB. Exactly the size for the un-sliced
`(mbsz × response_length, vocab)` logits tensor.

**Expectation:** with Ulysses SP=8, each rank should only materialize
`total_nnz / sp` ≈ 7000 tokens worth of lm_head output, not the full
mbsz × response_length.

**Observation:** SP had zero effect. The allocation attempt was identical
at SP=4 and SP=8. Something was bypassing the slicing.

**Workaround that let us proceed:** drop `log_prob_micro_batch_size_per_gpu`
from 4 → 2 (v5t) or 1 (v5s). This made the pre-slice tensor small enough
to fit. Got past the lm_head but hit Bug 5 downstream.

**Actual root cause:** Bug 6 — Ulysses wasn't patching the Qwen3.5-MoE text
model at all. The slicing code path was a no-op for our model_type.

---

### Bug 5: flash-attn cross-entropy shape assertion
**First seen:** v5s, v5t, v5x (various SP sizes, mbsz 1 or 2, no fused).
**Traceback:**
```
dp_actor.py:265, log_probs = logprobs_from_logits(logits, labels, ...)
→ flash_attn/ops/triton/cross_entropy.py:171
→ assert labels.shape == (n_rows,)  where n_rows = logits.shape[0]
→ AssertionError
```

**Root cause:** `logits` came from the model forward pass (shape `(1,
total_nnz + pad, vocab)` → squeezed to `(total_nnz + pad, vocab)`), while
`input_ids_rmpad_rolled` (the labels) was pre-sliced by Ulysses to
`(total_nnz/sp + pad_labels)`. The two were expected to match on dim 0,
but because the model forward wasn't slicing (Bug 6), its output was the
full un-sliced length while labels were sliced. Shapes differed by a factor
of `sp_size`.

**Misdirected debugging:** I spent time examining `ulysses_pad_and_slice_inputs`
for off-by-one padding differences between its two invocations (on
`input_ids_rmpad` and `input_ids_rmpad_rolled`). Those turned out to match.
The real divergence was that the MODEL output wasn't sliced — only the
labels were.

---

### Bug 6: verl's `monkey_patch.py` has no `qwen3_5_moe` branch (the real fix)
**Location:** `verl/verl/models/transformers/monkey_patch.py`.
**Discovery:** user pointed out that the upstream sglang patches we have
target Qwen3.5 specifically, but verl's Ulysses integration is
model-type-dispatched and might not know about `qwen3_5_moe`.

**The bug, precisely:** `apply_monkey_patch` has an if/elif ladder
branching on `model.config.model_type`. Branches exist for:
- `qwen2_vl`, `qwen2_5_vl`
- `qwen3_vl`, `qwen3_vl_moe`
- `glm4v`
- `kimi_vl`
- `gemma4`

Not for `qwen3_5_moe`. For unmatched types, verl falls through to the
default (text-only, dense) forward handling, which does NOT install the
`patch_vlm_for_ulysses_input_slicing` wrapper on the text sub-module.
Result: `Qwen3_5MoeTextModel.forward` never slices its input along the
sequence dimension, even when verl's upstream `dp_actor` pre-slices
`input_ids_rmpad` before calling `self.actor_module(...)`.

Concretely: verl sliced `input_ids_rmpad` (shape `(1, total_nnz/sp + pad)`),
passed it in, and the model dutifully ran the full forward pass on the
short input. But because nothing flagged "this is SP-sliced, please
coordinate", the model's internal attention did NOT do the all-to-all
transpose, MLP did NOT expect `total_nnz/sp` tokens, and crucially lm_head
emitted `(total_nnz/sp + pad, vocab)` logits for a SINGLE rank. When the
downstream `gather_outputs_and_unpad` gathered across all 8 SP ranks and
unpadded, the per-rank data was already "full" (just in duplicate/
inconsistent form across ranks), producing shape mismatches in
`pad_input`'s scatter step (Bug 5 / Bug 7).

**The fix (committed in verl/verl/models/transformers/monkey_patch.py):**

```python
elif model.config.model_type == "qwen3_5_moe":
    # Qwen3.5-MoE is a VLM-style wrapper (Qwen3_5MoeForConditionalGeneration)
    # that dispatches to Qwen3_5MoeTextModel for text-only inputs. Mirror the
    # qwen3_vl_moe path: install the Ulysses input-slicing wrapper on the text
    # model so that when verl pre-slices `input_ids_rmpad` by sp_size at the
    # dp_actor layer, the text model forward also slices `inputs_embeds` /
    # `position_ids` consistently at the decoder layer boundary. Without this,
    # the text model re-materializes full-seq hidden states, producing shape
    # mismatches in the downstream rmpad / cross-entropy / pad_input code paths.
    from transformers.models.qwen3_5_moe.modeling_qwen3_5_moe import Qwen3_5MoeTextModel

    if ulysses_sp_size > 1:
        patch_vlm_for_ulysses_input_slicing(Qwen3_5MoeTextModel)
```

**Preflight sentinel** added to `verl_rl_ehr/patches/verify_verl_patches.py`:

```python
(VERL_ROOT / "models" / "transformers" / "monkey_patch.py", 'model_type == "qwen3_5_moe"'),
```

**Confirmation:** at launch, look for 8 lines of
`Monkey patch Qwen3_5MoeTextModel.forward for Ulysses SP input slicing.`
(once per worker rank).

---

### Bug 7: Fused-kernels path — `pad_input` scatter mismatch
**First seen:** v5w.
**Traceback:**
```
compute_log_prob → _forward_micro_batch
→ line 318, full_entropy = pad_input(hidden_states=entropy_rmpad.unsqueeze(-1), ...)
→ flash_attn/bert_padding.py:51, output[indices] = values
→ RuntimeError: shape mismatch: value tensor of shape [917823, 1] cannot
  be broadcast to indexing result of shape [114727, 1]
```

**Math:** 917823 / 114727 ≈ 8.0, exactly `sp_size`. So `values` was the
gathered-across-all-ranks tensor (`sp` times too big), but `indices` was
for the per-rank slice.

**Root cause (same as Bugs 4-5):** Ulysses wasn't actually slicing the model
forward, so the "gather post-forward" step (line 290-305 in `dp_actor.py`)
stacked the already-full-size tensors from each rank and produced an
`sp`× larger result than the indices expected.

**Fixed by Bug 6's patch.**

---

## Things that did NOT help (rolled back)

### Dynamic batching (`use_dynamic_bsz`)
I proposed this as a memory-relief alternative to Ulysses.
**Why it doesn't help here:** the assertion
`max_token_len >= max_seq_len` in `rearrange_micro_batches`
(`verl/utils/seqlen_balancing.py:384`) forbids splitting a single rollout
across multiple micro-batches. For our 56K rollouts, `max_token_len` must
be ≥ 56000, making the chunking useless (each rollout already runs one-
at-a-time at mbsz=1). Dynamic-bsz only helps when rollout lengths vary
wildly and short+long can be packed together — not our regime.

### `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
Tried in v5i. **Broke sglang:**
```
RuntimeError: TorchMemorySaver is disabled for the current process
because expandable_segments is not supported yet.
```
sglang's `torch-memory-saver` package requires the default CUDA allocator.
Do not set this env var in this venv.

### `log_prob_micro_batch_size_per_gpu=4`
Default. Allocates `4 × response_length × vocab × 2 bytes` per rank in
`compute_log_prob`'s lm_head forward. At 56K response length that's 107 GiB
per rank, which OOMs. Dropped to 2 for v5y.

### Ulysses without the monkey-patch fix
SP=4 and SP=8 both hit identical bugs without the Bug-6 fix. Don't bother
bumping SP; it's a no-op without the patch.

---

## Wait times and dead-end overhead

Rough timeline for each failed run (rollout generation through crash site):

- Preflight boot (weights + sglang capture): ~8-10 min.
- Val pass (when enabled, 326 rows → 163 rows half-val): ~6-10 min.
- One step of rollouts + compute_log_prob + update_actor: ~27-32 min.

So the marginal cost of a "crashes at step 1 backward" run is ~35-45 min.
Runs that died in rollout phase cost ~15-25 min. The full cycle v5h → v5y
consumed ~15-20 hours of wall-clock, almost entirely in these dead-end
iterations.

**Lesson:** always run `val_before_train=false` while iterating on an OOM
bug. Skip to the critical section as fast as possible.

---

## What v5y actually uses

Hydra overrides (what the launcher now runs):

```bash
trainer.val_before_train=false
trainer.total_training_steps=50
trainer.rollout_dump_freq=1

+data.apply_chat_template_kwargs.enable_thinking=false
data.max_response_length=56000

actor_rollout_ref.model.path=<SFT-ed Qwen3.5-35B-A3B HF ckpt>
actor_rollout_ref.rollout.multi_turn.context_reset_mode=summarizer
actor_rollout_ref.rollout.multi_turn.force_finish_tool_enabled=true
actor_rollout_ref.rollout.multi_turn.force_answer_token_threshold=52000
actor_rollout_ref.rollout.multi_turn.context_reset_max_count=10000
actor_rollout_ref.actor.ulysses_sequence_parallel_size=8
actor_rollout_ref.ref.ulysses_sequence_parallel_size=8
actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2
actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2
actor_rollout_ref.rollout.gpu_memory_utilization=0.30
actor_rollout_ref.rollout.free_cache_engine=false
actor_rollout_ref.actor.fsdp_config.param_offload=true
actor_rollout_ref.actor.fsdp_config.optimizer_offload=true
actor_rollout_ref.ref.fsdp_config.param_offload=true
```

Env:
```bash
BROWSER_SEARCH_MODE=http
SEARCH_SERVICE_URL=http://127.0.0.1:8090
CONTEXT_SUMMARIZER_ENABLED=true
AWS_BEARER_TOKEN_BEDROCK=<Haiku key>
```

Pre-launch:
- Start MCP server on `:5103`.
- Start Serper-backed search service on `:8090`
  (`python -m verl_rl_ehr.services.search_service --port 8090`).
- `python verl_rl_ehr/patches/verify_verl_patches.py` must print `verl patches OK`.

See the companion runbook for full launch instructions.

---

## Reward trajectory (v5y, first 12 steps)

| step | score | correctness (2×F1) | efficiency | format_pen | F1 | force_inj |
|------|-------|--------|-----|-----|------|-----|
| 1 | +0.410 | +0.410 | 0.000 | 0.000 | 0.205 | 100% |
| 2 | +0.284 | +0.285 | 0.000 | 0.000 | 0.143 | 100% |
| 3 | +0.437 | +0.443 | 0.000 | 0.000 | 0.221 | 100% |
| 4 | +0.472 | +0.485 | 0.000 | 0.000 | 0.243 | 100% |
| 5 | +0.438 | +0.448 | 0.000 | 0.000 | 0.224 | 100% |
| 6 | **+0.519** | **+0.523** | 0.000 | 0.000 | **0.261** | 100% |
| 7 | +0.455 | +0.456 | 0.000 | 0.000 | 0.228 | 100% |
| 8 | +0.202 | +0.202 | 0.000 | 0.000 | 0.101 | 100% |
| 9 | +0.402 | +0.402 | 0.000 | 0.000 | 0.201 | 100% |
| 10 | +0.427 | +0.427 | 0.000 | 0.000 | 0.214 | 100% |
| 11 | +0.439 | +0.449 | 0.000 | 0.000 | 0.225 | 100% |
| 12 | +0.452 | +0.464 | 0.000 | -0.002 | 0.232 | 99.2% |

**Observations:**
- `correctness` (= 2×F1) is the only component driving score changes.
- `efficiency` is permanently zero: all rollouts get force-answer-injected
  at the 52K threshold, which zeros the efficiency component by design.
  That component is essentially a dead signal in this configuration.
- `format_penalty` first non-zero at step 12 (one rollout in 128 failed to
  commit any answer signal), suggesting the policy is just barely starting
  to produce non-finish trajectories.
- F1 trend: 0.205 → 0.232 over 12 steps, with noise. Slow but positive.

If F1 plateaus or stops climbing, consider:
1. **Data engineering / rejection sampling:** filter `data/ehr_rl_qwen35/train.parquet`
   to rows where the base policy achieves some signal (F1 > 0), focus
   training on tractable examples instead of ones that would stall.
2. **Larger max_response_length:** 56K might still be truncating heavy
   cases; 80-100K with the same Ulysses+SP=8 setup should now fit given
   peak `perf/max_memory_allocated_gb` was only 132/143 GB in v5y.
3. **Reward reshape:** efficiency is dead; replace it with a different
   shaping signal (e.g., a smaller bonus for submitting within N turns
   regardless of force-answer flag).
4. **Longer training:** 50 steps on 16-prompt batches × 8 rollouts is only
   ~6400 episodes. Signal-to-noise may improve past step 30.
