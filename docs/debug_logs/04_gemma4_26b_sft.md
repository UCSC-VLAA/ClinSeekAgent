# Gemma-4-26B-A4B-it FSDP SFT Training

## Status Summary (2026-04-20)

- **Training**: 3-step validation passed at `max_length=60000` with LoRA + mixed per-layer attention dispatch on 8× H200 FSDP1 (loss 2.72 → 1.29, ~30s/step, `train_batch_size=8`).
- **Full run unstable**: 60K crashed on step 5 with OOM; 52K crashed on step 1 with OOM. Memory ceiling is ~50K per-sample with bs=8, no SP. See §8 Modification Log for context.
- **Configuration**: LoRA rank=32, alpha=64, lr=1e-4, targets `language_model.*.{q,k,v,o,gate,up,down}_proj` via regex. Base weights frozen (vision tower also frozen).
- **Attention**: `_attn_implementation=flash_attention_2` + monkey-patch at `verl/models/transformers/gemma4.py` that intercepts `Gemma4TextAttention.forward`. Sliding-window layers (head_dim=256) go to flash-attn; global layers (head_dim=512) go to SDPA EFFICIENT with `is_causal=True, attn_mask=None`.
- **7 bugs fixed**: loss_mask corruption, FSDP wrap crash, mm_token_type_ids, flash-attn head_dim, flex_attention Triton overflow, PEFT rejecting `Gemma4ClippableLinear` (vision tower) — scope LoRA via regex, SDPA MATH fallback on sliding layers at >1024 tokens — use flash_attention_2 as base to force None/2D masks.
- **Pending**: Ulysses SP=2 adaptation for the attention monkey-patch to halve per-GPU activation memory and unblock 52K/60K training.

---

## 1. Model Overview

| Property | Value |
|----------|-------|
| Model | google/gemma-4-26B-A4B-it (instruction-tuned) |
| Architecture | MoE (Mixture of Experts) with hybrid sliding-window + global attention |
| Total params | ~25.8B (includes vision tower) |
| Active params | ~4B per token (8 of 128 experts + shared layers) |
| Experts | 128 total, top_k=8 |
| Layers | 30 `Gemma4TextDecoderLayer` |
| Attention | Hybrid: 5 sliding-window (1024 tokens) + 1 full global, repeating (5:1 pattern). Last layer always global |
| RoPE | Per-layer-type: sliding uses default (theta=10K), global uses proportional (theta=1M, partial_rotary_factor=0.25) |
| KV heads | 8 (sliding layers), 2 (global layers via `num_global_key_value_heads`) |
| Head dim | 256 (sliding), 512 (global via `global_head_dim`) |
| Hidden size | 2816 |
| Vocab size | 262,144 |
| Context length | 262,144 (max_position_embeddings) |
| `tie_word_embeddings` | True |
| `attention_k_eq_v` | True (shared K/V projections) |
| `final_logit_softcapping` | 30.0 |
| `enable_moe_block` | True |
| `moe_intermediate_size` | 704 |
| Backend | FSDP via verl (transformers 5.5.4) |

### Architecture Notes
- `Gemma4ForConditionalGeneration` is a VLM wrapper containing:
  - `model.language_model` → `Gemma4TextModel` (the text MoE backbone)
  - `model.vision_tower` → `Gemma4VisionModel` (unused for text-only SFT)
  - `model.audio_tower` → None (26B-A4B has no audio support)
- For text-only SFT, vision/audio inputs are `None` and the towers are skipped in the forward pass
- The model is loaded via `AutoModelForImageTextToText` (registered in transformers 5.5.4's `MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES`)
- `hidden_size_per_layer_input = 0` for 26B-A4B (per-layer residual embeddings disabled)

---

## 2. Environment

### Separate Venv (Qwen3.5 venv unchanged)

| Component | Version |
|-----------|---------|
| Python | 3.10 |
| PyTorch | 2.9.0+cu128 |
| Transformers | 5.5.4 (required for `gemma4` model type; 5.3.0 does NOT support it) |
| flash-attn | 2.8.3 (does NOT support head_dim > 256) |
| verl | 0.8.0.dev (editable install from `/fsx-shared/juncheng/EHR/verl/`) |
| CUDA | 12.8 |

### Key Paths
- **Gemma4 venv**: `/fsx-shared/juncheng/EHR/venvs/gemma4/`
- **Qwen3.5 venv** (unchanged): `/fsx-shared/juncheng/EHR/openresearcher_ehr/.venv/`
- **Model weights**: `/fsx-shared/juncheng/EHR/models/gemma-4-26B-A4B-it/`
- **Training script**: `verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`
- **Training data**: `/fsx-shared/juncheng/EHR/data/deepmed_trajectory/{train,val}.parquet`
- **Checkpoints**: `verl/checkpoints/deepmed-sft/gemma4-26b-*`

### Hardware
- **GPUs**: 8x NVIDIA H200 (139.8 GB each)
- **GPU Memory Usage (8-GPU FSDP, max_length=8192)**: ~96 GB per GPU (69% utilization)

---

## 3. Training Configuration

### Script
`verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`

### Data Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| `train_batch_size` | 8 | Global batch size (1 sample per GPU, no accumulation) |
| `micro_batch_size_per_gpu` | 1 | 1 sample per GPU per micro-step |
| `max_length` | 60000 | Covers p99 of data; max sample is 60169 tokens |
| `truncation` | left | Keep most recent context |
| `use_dynamic_bsz` | False | Standard fixed batching |
| `use_remove_padding` | False | Conservative (varlen packing would need custom SDPA for head_dim=512) |
| `ignore_input_ids_mismatch` | True | Enables fallback tokenization (required for Bug 1 fix) |

### LoRA Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| `lora_rank` | 32 | Per verl doc — ≥32 preserves full-FT convergence |
| `lora_alpha` | 64 | 2× rank, standard choice |
| `target_modules` | regex | `^.*language_model\..*\.(q_proj\|k_proj\|v_proj\|o_proj\|gate_proj\|up_proj\|down_proj)$` — text layers only, not vision tower |
| `lr` | 1e-4 | 5× higher than full-FT's 2e-5 (LoRA rule of thumb) |

### Optimizer & Training

| Parameter | Value |
|-----------|-------|
| optimizer | AdamW |
| lr | 2e-5 (cosine decay) |
| warmup | 5% of total steps |
| weight_decay | 0.01 |
| clip_grad | 1.0 |
| gradient_checkpointing | True |
| attn_implementation | sdpa (not flash_attention_2; see Bug 4) |
| total_epochs | 3 |

### Training Metrics (3-step validation, max_length=60000, 8× H200, LoRA + mixed attention)

| Step | train/loss | train/grad_norm | train/lr | tokens |
|------|-----------|----------------|----------|--------|
| 1 | 2.72 | 98.5 | 7.5e-5 | 280K |
| 2 | 1.68 | 55.6 | 2.5e-5 | 302K |
| 3 | 1.29 | 15.2 | 0.0 | 245K |

- **Speed**: ~30 s/step (first step ~36s with compile warmup; subsequent ~27s)
- **Throughput**: ~10K tokens/s
- **Memory**: Stable on 8× H200; no offload required

### Prior Full-FT Run (deprecated, kept for reference)

40-step validation at max_length=8192, full-FT (no LoRA), 8× H200: loss 3.85 → 0.26, ~16.5 s/step, ~96 GB/GPU. Only 8K max due to SDPA MATH fallback on sliding layers at >1024 tokens. The LoRA + 60K configuration supersedes this.

---

## 4. Bugs Encountered and Fixes

### Bug 1 (CRITICAL): Loss mask corruption — model learns nothing

- **Symptom**: Training runs but loss stays high; model outputs are random
- **Root Cause**: `extract_system_prompt_and_generation()` computes `generation_prompt` by comparing `add_generation_prompt=True` vs `False`. For Gemma-4-it, `add_generation_prompt=True` appends thinking-mode tokens `<|channel>thought\n<channel|>`, producing a **7-token** generation_prompt. But the actual assistant turn header is only **3 tokens** (`<|turn>model\n`).
  
  In `_process_single_message()`, the per-message path does:
  ```python
  loss_mask = torch.ones_like(attention_mask)
  loss_mask[: len(self.generation_prompt)] = 0  # masks first 7 tokens
  ```
  A typical assistant message has only 6 tokens after BOS removal → ALL tokens masked → zero training signal.

- **Fix**: Added generation_prompt validation at dataset init time. If the generation_prompt length doesn't match the actual assistant turn header length (detected by test tokenization), set `_force_fallback_tokenization = True`. In `__getitem__()`, this flag raises immediately to trigger the fallback path (full-conversation tokenization with incremental prefix diffing), which correctly computes loss_mask boundaries.
- **File**: `verl/verl/utils/dataset/multiturn_sft_dataset.py` lines 183-213 (validation) and line 333 (flag check)
- **Verification**: `loss_mask.sum() > 0` for all samples; decoded masked tokens are assistant content only

### Bug 2: FSDP wrapping crash — missing Gemma4AudioLayer

- **Symptom**: `Exception: Could not find the transformer layer class to wrap in the model.`
- **Cause**: `_no_split_modules = ["Gemma4TextDecoderLayer", "Gemma4VisionEncoderLayer", "Gemma4AudioLayer"]` but 26B-A4B has `audio_config=None` → no audio tower → `Gemma4AudioLayer` not in module tree → `get_module_class_from_name()` returns `None` → hard crash
- **Fix**: Changed `fsdp_utils.py` to skip missing layer classes with `logging.warning()` instead of raising. Only error if NO classes are found at all.
- **File**: `verl/verl/utils/fsdp_utils.py` lines 127-135

### Bug 3: `mm_token_type_ids` required during training

- **Symptom**: `ValueError: 'mm_token_type_ids' is required as a model input when training`
- **Cause**: Gemma4's `create_causal_mask_mapping()` requires `mm_token_type_ids` to distinguish text (0) vs vision (1,2) tokens when constructing the hybrid attention mask. verl's FSDP engine doesn't pass this.
- **Fix**: For `gemma4` model type, add `mm_token_type_ids = torch.zeros_like(input_ids)` to model inputs (all zeros = all text tokens).
- **File**: `verl/verl/workers/engine/fsdp/transformer_impl.py` lines 983-987

### Bug 4: FlashAttention head_dim > 256 not supported

- **Symptom**: `RuntimeError: FlashAttention forward only supports head dimension at most 256`
- **Cause**: Gemma4's global attention layers use `global_head_dim=512`. flash-attn 2.8.3 only supports head_dim ≤ 256.
- **Fix**: Set `+model.override_config.attn_implementation=sdpa` in training script to use PyTorch's SDPA instead of flash-attn.
- **Impact**: SDPA materializes the full O(n^2) attention matrix, limiting `max_length` to ~8192 on 8x H200. At `max_length=32768`, SDPA tried to allocate 64 GiB per GPU → OOM.
- **File**: `verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`

### Bug 5: flex_attention Triton shared memory overflow

- **Symptom**: `RuntimeError: No valid triton configs. OutOfMemoryError: out of resource: Required: 262144 Hardware limit: 232448`
- **Cause**: `flex_attention` (PyTorch 2.9, `torch.nn.attention.flex_attention`) compiles attention into Triton kernels. The kernel for `head_dim=512` requires 256KB shared memory, but H200 (sm_90) only has 232KB. This is a hardware constraint — fails at **any** sequence length, not just long ones.
- **Attempted**: Set `+model.override_config.attn_implementation=flex_attention`. Model loaded successfully, but first forward pass crashed on Triton compilation.
- **Status**: Not viable. Reverted to SDPA.

### Bug 7: PEFT rejects Gemma4ClippableLinear in vision tower

- **Symptom**: `ValueError: Target module Gemma4ClippableLinear(...) is not supported. Currently, only torch.nn.Linear, torch.nn.Embedding, ...`
- **Cause**: Gemma4's vision tower uses `Gemma4ClippableLinear` (a wrapper around `nn.Linear`), not vanilla `nn.Linear`. PEFT's LoRA wrapper only handles specific module classes. Using `target_modules=[q_proj,k_proj,...]` matches the vision tower's q_proj too.
- **Fix**: Use a regex that anchors to `language_model.` so only text layers match:
  ```
  model.target_modules="^.*language_model\..*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$"
  ```
  In Hydra override syntax, wrap the whole value in double quotes and the whole CLI arg in single quotes to avoid lexer errors on `^` / `$`.
- **File**: `verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`

### Bug 8: SDPA falls back to MATH on sliding layers at seq_len > 1024

- **Symptom**: Even with the EFFICIENT kernel working at head_dim=512 (per isolated test), the full model OOMs at 60K with `_attn_implementation=sdpa`. The OOM allocation is huge (100+ GB) — clearly the O(n²) MATH kernel.
- **Root cause**: `_ignore_causal_mask_sdpa` in `masking_utils.py` blocks the skip when `local_attention_size is not None and kv_length >= local_attention_size`. For Gemma4 sliding layers, `sliding_window=1024` and `kv_length=60000`, so the mask cannot be replaced by `is_causal=True`. A 4D float mask is materialized → SDPA dispatches to MATH.
- **Observation**: EFFICIENT works only when `attn_mask=None, is_causal=True`. At 60K × head_dim=512, measured peak memory **6.88 GB** with None mask vs **17.35 GB** with 4D causal mask — and much higher inside the real model.
- **Fix**: Set `_attn_implementation=flash_attention_2` (not `sdpa`) so the mask interface is `flash_attention_mask`, which returns `None` or 2D — never 4D. Then monkey-patch `Gemma4TextAttention.forward` to route head_dim=512 global layers to SDPA with `is_causal=True, attn_mask=None` directly — bypassing `ALL_ATTENTION_FUNCTIONS['flash_attention_2']` which would crash on head_dim>256.
- **File**: `verl/models/transformers/gemma4.py` (new), hooked into `verl/models/transformers/monkey_patch.py:apply_monkey_patch()`.

### Bug 9: FSDP1 param/optimizer offload incompatible with LoRA

- **Symptom 1**: With `engine.param_offload=True`, init crashes in `offload_fsdp_model_to_cpu` with `AssertionError` on `flat_param.data.size() == flat_param._local_shard.size()`.
- **Symptom 2**: With `engine.optimizer_offload=True` alone (param_offload=False), init crashes with `AssertionError: Model must be moved to device along with optimizer and grad` — verl requires both or neither.
- **Cause**: FSDP1's `CPUOffload` invariants break when LoRA introduces mixed trainable/frozen parameters in the same `FlatParamHandle`. The base weights are frozen, LoRA adapters are trainable — FSDP treats the combined flat param inconsistently.
- **Workaround**: Leave both `param_offload=False` and `optimizer_offload=False`. LoRA's optimizer state is tiny (~48 MB) so this is fine.
- **Future**: FSDP2 supposedly handles this better, but in this environment FSDP2 made step 1 run >25 min without completing, so FSDP1 wins on wall-clock time.

### Bug 10: Grad-accumulation OOM on step 2 with train_batch_size=32

- **Symptom**: Step 1 succeeds (loss 2.72, 118s). Step 2 OOMs with ~28 GB allocation attempted against ~92 GB free-but-fragmented.
- **Cause**: With `train_batch_size=32, micro_batch_size_per_gpu=1` on 8 GPUs, each optimizer step accumulates 4 micro-batches. Activations from earlier micro-batches are retained until backward — with highly variable seq lengths (2K to 60K), fragmented allocator can't find a contiguous block for the next micro-batch's activations.
- **Fix**: Set `train_batch_size=8` (1 sample per GPU, no accumulation). Also set `PYTORCH_ALLOC_CONF=expandable_segments:True` (no `max_split_size_mb` — that flag can make things worse).

### Bug 11: Activation offloading crashes with AssertionError

- **Symptom**: With `model.enable_activation_offload=True`, backward crashes in `verl/utils/activation_offload.py:86` (`AssertionError` in `on_get_saved_tensor` → `tensor_pop`).
- **Cause**: Not investigated — likely an interaction with LoRA's `enable_input_require_grads()` or bf16 params.
- **Fix**: Leave `enable_activation_offload=False`. Not needed at 60K thanks to EFFICIENT attention kernel.

### Bug 6: Mixed per-layer attention dispatch fails (superseded — see Bug 8 fix)

- **Symptom**: `torch.AcceleratorError: CUDA error: device-side assert triggered` in `_upad_input` during flash-attn forward
- **Cause**: Attempted monkey patch to use `flash_attention_2` for sliding-window layers (head_dim=256) and `sdpa` for global layers (head_dim=512). Failed because Gemma4's `create_causal_mask_mapping()` pre-computes attention masks at the model level for a single `_attn_implementation`. The masks produced by SDPA (4D dense tensors) are incompatible with flash-attn's expected format (2D padding mask or None).
- **Attempted**: Monkey-patched `Gemma4TextAttention.forward` to swap `config._attn_implementation` per-layer. The mask format mismatch caused CUDA assertion failures.
- **Status**: Not viable with current transformers architecture. Would require Gemma4 to compute masks per-layer rather than globally.

---

## 5. Code Changes Summary

### Modified Files in verl

1. **`verl/verl/utils/dataset/multiturn_sft_dataset.py`** (Bug 1)
   - Added `from verl.utils.tokenizer import normalize_token_ids` import
   - Added generation_prompt validation block after line 181: tokenizes a test conversation to measure the actual assistant turn header length, compares against `self.generation_prompt`, sets `self._force_fallback_tokenization = True` on mismatch
   - Added early raise in `__getitem__()` when `_force_fallback_tokenization` is set, triggering the existing fallback path (full-conversation tokenization with prefix diffing)

2. **`verl/verl/utils/fsdp_utils.py`** (Bug 2)
   - Added `import logging`
   - Changed the FSDP wrap policy loop to `logging.warning()` and skip missing layer classes instead of raising
   - Added a final check: only raise if `transformer_cls_to_wrap` is empty (no classes found at all)

3. **`verl/verl/workers/engine/fsdp/transformer_impl.py`** (Bug 3)
   - After constructing `model_inputs` dict in the `pad_mode=NO_PADDING` branch, added Gemma4 detection:
     ```python
     config = getattr(self.module, "module", self.module).config
     if getattr(config, "model_type", None) == "gemma4":
         model_inputs["mm_token_type_ids"] = torch.zeros_like(input_ids)
     ```

4. **`verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`** (Bugs 4, 7, 8, 10)
   - `+model.override_config.attn_implementation=flash_attention_2` — base config so mask interface is None/2D
   - `model.lora_rank=32`, `model.lora_alpha=64`, regex-based `target_modules` scoped to `language_model.*`
   - `data.max_length=60000`, `data.train_batch_size=8` (avoids grad-accum fragmentation)
   - `optim.lr=1e-4` (tuned for LoRA)
   - `engine.strategy=fsdp` (FSDP2 regressed perf)
   - `PYTORCH_ALLOC_CONF=expandable_segments:True` without `max_split_size_mb`

5. **`verl/verl/models/transformers/gemma4.py`** NEW (Bug 8)
   - `apply_gemma4_mixed_attention_patch()`: monkey-patches `Gemma4TextAttention.forward`
   - Routes head_dim=512 global layers to `F.scaled_dot_product_attention(..., is_causal=True, attn_mask=None)` — dispatches to EFFICIENT kernel
   - Routes head_dim=256 sliding layers to `ALL_ATTENTION_FUNCTIONS[config._attn_implementation]` (flash-attn 2)

6. **`verl/verl/models/transformers/monkey_patch.py`** (Bug 8)
   - In `apply_monkey_patch()`: if `model.config.model_type == "gemma4"`, invoke `apply_gemma4_mixed_attention_patch()` before the existing flash-attn/ulysses patches

### Previously Existing Files (from prior session)

7. **`verl/verl/utils/tokenizer.py`** line 149
   - Added `case "Gemma4Processor": pass` in the processor match statement

---

## 6. Running the Training

```bash
source /fsx-shared/juncheng/EHR/venvs/gemma4/bin/activate
cd /fsx-shared/juncheng/EHR/verl

# Short validation (3 steps, max_length=60000, ~10 min including model load)
bash examples/sft/deepmed/run_gemma4_26b_sft.sh 8 \
    ./checkpoints/deepmed-sft/gemma4-26b-test \
    trainer.total_training_steps=3 \
    trainer.total_epochs=999 \
    trainer.save_freq=-1 \
    trainer.test_freq=-1 \
    trainer.logger=console \
    data.val_max_samples=0

# Full training run (3 epochs, max_length=60000, LoRA)
nohup bash examples/sft/deepmed/run_gemma4_26b_sft.sh 8 \
    ./checkpoints/deepmed-sft/gemma4-26b \
    > /fsx-shared/juncheng/EHR/gemma4_training.log 2>&1 &
```

---

## 7. How 60K Works — Attention Kernel Dispatch

### Why the 8K barrier fell: SDPA EFFICIENT at head_dim=512

The prior analysis assumed SDPA on head_dim=512 materializes an O(n²) MATH matrix. That's only true when a 4D `attn_mask` is passed. With `attn_mask=None, is_causal=True`, PyTorch SDPA dispatches to the **EFFICIENT** kernel (xformers memory-efficient attention), which is O(n) memory **and supports head_dim up to 512 on H200**.

| SDPA backend | head_dim limit | head_dim=512 @ N=60K peak mem |
|---|---|---|
| FLASH_ATTENTION | ≤256 | blocked |
| CUDNN_ATTENTION | ≤128 | blocked |
| **EFFICIENT_ATTENTION** | unlimited | **6.88 GB** (is_causal=True, attn_mask=None) |
| MATH | unlimited | 17+ GB with 4D mask |

### Why the fix needs both a config change AND a monkey-patch

1. **Mask format**: With `_attn_implementation=sdpa`, the mask interface `sdpa_mask()` is forced to return a 4D bool tensor for sliding layers when `kv_length ≥ sliding_window=1024`. SDPA sees the 4D mask and falls back to MATH → OOM.

2. **Solution**: set `_attn_implementation=flash_attention_2` so the mask interface becomes `flash_attention_mask`, which returns `None` or 2D bool — never 4D.

3. **New problem**: `ALL_ATTENTION_FUNCTIONS['flash_attention_2']` would be called on *every* layer, including head_dim=512 global layers, which crashes inside flash-attn at the head_dim check.

4. **Monkey patch** (`verl/models/transformers/gemma4.py`): override `Gemma4TextAttention.forward` to choose the attention function per-layer:
   - `head_dim=256` (sliding, 24/30 layers): use `ALL_ATTENTION_FUNCTIONS['flash_attention_2']` — flash-attn handles head_dim=256 + sliding_window natively.
   - `head_dim=512` (global, 6/30 layers): call `F.scaled_dot_product_attention(q, k, v, attn_mask=None, is_causal=True)` directly — dispatches to EFFICIENT, O(n) memory.

This is exactly the strategy Bug 6 attempted but failed at because it kept `sdpa` as the base config and its mask interface produces 4D tensors.

### Megatron note (historic)

Adding Megatron support for Gemma4 was previously considered. It's no longer needed: the mixed SDPA/flash-attn approach above works on FSDP1 at 60K with LoRA. Megatron's kernel path would hit the same head_dim=512 limit anyway.

### Investigation Log (historical)

1. **Flash-attn 3 (Hopper)**: No PyPI package. Built from `github.com/Dao-AILab/flash-attention/hopper/`. Both `flash_attn_interface.py:round_up_headdim()` and `cute/interface.py` enforce `head_dim <= 256`. The `"64_512"` kernel variant is for q_headdim=64 / v_headdim=512 (different Q/V dims), not for head_dim=512 on both Q and K.

2. **flex_attention**: Available in PyTorch 2.9.0 and supported by Gemma4 (`_supports_flex_attn = True`). However, the compiled Triton kernel requires shared memory proportional to head_dim. At 512, it exceeds H200's 232KB limit (needs 256KB). Fails at **any** sequence length, not just long ones.

3. **Mixed per-layer dispatch v1** (Bug 6, `_attn_implementation=sdpa` + per-layer swap): Attempted via monkey patch, but failed because SDPA's mask interface pre-computes 4D tensors at the model level. Different backends expect incompatible mask formats. **Resolution (Bug 8)**: use `_attn_implementation=flash_attention_2` as the base so masks are None/2D, then only do per-layer routing inside `Gemma4TextAttention.forward`.

### Megatron Backend: dormant

Adding Megatron support for Gemma4 was previously considered to work around head_dim=512 limits, but the mixed-attention patch makes it unnecessary. Key blockers if ever revisited:

- **Hybrid attention**: Different head_dim per layer type (256 vs 512) — Megatron TransformerConfig assumes uniform head_dim
- **Per-layer RoPE**: Different theta/type per layer — not standard in Megatron
- **`attention_k_eq_v`**: Shared K/V projections on global layers — requires custom attention module
- **Nested config**: `Gemma4Config.text_config` vs flat config — config converter must extract text_config

**Note**: Even with Megatron, the global_head_dim=512 constraint remains — Megatron uses the same flash-attn kernels. No longer relevant now that the SDPA EFFICIENT path is unblocked.

### Next Possible Optimizations

1. **Ulysses SP** (`engine.ulysses_sequence_parallel_size=2`): shard sequence dim across 2 GPUs for even longer contexts. Would require verifying the monkey-patch works under Ulysses.
2. **use_remove_padding=True with custom varlen SDPA**: write a varlen SDPA wrapper that loops per-sample to handle cross-sample attention correctly on head_dim=512 layers. Would remove padding overhead and let us use `use_dynamic_bsz=True` for better GPU utilization.
3. **Bigger batch**: if a full run shows memory headroom at `train_batch_size=8`, raise to 16 (grad accum = 2). Monitor for the fragmentation seen at bs=32.

---

## 8. Modification Log (2026-04-19 → 2026-04-20 session)

Chronological record of what was changed in this session, with outcomes. Intended as handoff notes for the next session.

### Changes made

1. **Added LoRA + flash_attention_2 + offload flags to training script** — `verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`. Set `max_length=60000`, `lora_rank=32`, `lora_alpha=64`, `lr=1e-4`, `target_modules=[q_proj,k_proj,...]`.
   - Issue: PEFT rejected `Gemma4ClippableLinear` in the vision tower because it's not a vanilla `nn.Linear`.
   - Fix: Changed `target_modules` to a regex anchored to `language_model.*`: `'model.target_modules="^.*language_model\..*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$"'` (wrap in single quotes for Hydra lexer, double-quote the regex inside).

2. **Tried `engine.param_offload=True, optimizer_offload=True`** with LoRA on FSDP1.
   - Issue: `param_offload=True` crashed in `offload_fsdp_model_to_cpu` with `AssertionError` on `flat_param.data.size() == flat_param._local_shard.size()` — FSDP1's flat-param invariant breaks when LoRA mixes trainable/frozen params in one handle.
   - Fix: Both disabled. LoRA's optimizer state is ~48 MB, so offloading doesn't help much anyway.

3. **Tried `engine.optimizer_offload=True` alone** (param_offload=False).
   - Issue: Crashed with `AssertionError: Model must be moved to device along with optimizer and grad` from `verl/workers/engine/base.py:180` — verl requires both or neither.
   - Fix: Leave both False.

4. **Tried `model.enable_activation_offload=True`**.
   - Issue: Backward crashed in `verl/utils/activation_offload.py:86` with `AssertionError` in `on_get_saved_tensor` → `tensor_pop`. Likely incompatible with LoRA's `enable_input_require_grads()` or bf16 params.
   - Fix: Disabled. Not needed once EFFICIENT attention unblocked memory.

5. **Kept `_attn_implementation=sdpa` with LoRA** on first real training attempt at 60K.
   - Issue: OOM during forward with 100+ GB allocation. `_ignore_causal_mask_sdpa` cannot return `None` for sliding layers when `kv_length ≥ sliding_window=1024`, so a 4D float mask is built and SDPA dispatches to MATH (O(n²)).
   - Fix: Switched to `_attn_implementation=flash_attention_2`. The `flash_attention_mask` interface returns `None` / 2D only — no 4D tensor.

6. **Wrote `verl/models/transformers/gemma4.py`** implementing `apply_gemma4_mixed_attention_patch()`.
   - Monkey-patches `Gemma4TextAttention.forward`.
   - Per-layer dispatch based on `self.head_dim`:
     - `head_dim=256` (sliding, 24/30 layers): use `ALL_ATTENTION_FUNCTIONS[config._attn_implementation]` → flash-attn 2 with native sliding window support.
     - `head_dim=512` (global, 6/30 layers): call `F.scaled_dot_product_attention(q, k, v, attn_mask=None, is_causal=True)` directly → SDPA EFFICIENT kernel, O(n) memory. Manually repeats KV for GQA (module.num_key_value_groups).
   - Hooked into `verl/models/transformers/monkey_patch.py:apply_monkey_patch()` via an `if model.config.model_type == "gemma4"` branch.
   - Result: 3-step validation at 60K worked: loss 2.72 → 1.68 → 1.29, step time ~30s.

7. **Tried FSDP2** (`engine.strategy=fsdp2`) hoping it would handle LoRA CPU offload better.
   - Issue: Step 1 didn't complete within 25 minutes (GPUs at 100% util, no step output). Never investigated further.
   - Fix: Reverted to FSDP1.

8. **Adjusted `train_batch_size=32 → 8`** to avoid grad-accumulation fragmentation.
   - With bs=32 on 8 GPUs (micro_bs=1), optimizer step accumulates 4 micro-batches. Variable seq lengths + retained activations across accum → allocator fragmentation → OOM on step 2 with 28 GB allocation attempt against 92 GB "free" fragmented memory.
   - With bs=8, 1 sample per GPU, no accumulation. Step 2 reproduced OK in validation.

9. **Swapped `PYTORCH_ALLOC_CONF`** from `expandable_segments:True,max_split_size_mb:128` to just `expandable_segments:True`. `max_split_size_mb:128` can make fragmentation worse with highly variable allocations.

10. **Added Gemma4 tokenizer fallback in `prepare_deepmed_data.py`**. `AutoProcessor.from_pretrained(gemma-4-26B-A4B-it)` fails because `Gemma4VideoProcessor` requires torchvision which isn't installed. Added try/except that falls back to `AutoTokenizer`. Also added `--output_dir` flag so we don't overwrite the Qwen3.5 data.

11. **Re-prepped data at max_token_length=52000 using Gemma4 tokenizer** → `data/deepmed_trajectory_gemma4_52k/` (2085 train, 42 val, 371 samples filtered = 14.9%).
    - Original Qwen data at `data/deepmed_trajectory/` is untouched.

### Current issues (unresolved)

- **Full run OOMs at 60K** on step 5 (fragmentation + a long sample draw) and **at 52K on step 1** (single sample near 52K exceeds per-GPU activation budget). The problem is per-*sample* memory, not per-*batch*: filtering the tail only changes which step OOMs, not whether.
- **Root cause**: a single 50K-token Gemma4 forward/backward (with gradient checkpointing) is very close to 140 GB on one H200. Any sample in that range OOMs.

### Possible paths forward

- **Ulysses SP=2** (started, not finished): shard the sequence across 2 GPUs, halving per-GPU activation memory. Keeps 4 DP × 2 SP = all 8 GPUs productive. Requires adapting `_sdpa_efficient_attention` in `verl/models/transformers/gemma4.py` to gather/scatter query/key/value across the SP group (mirroring what `_ulysses_flash_attention_forward` does in `monkey_patch.py` for flash-attn).
- **Filter harder** (~40K): lose 25-30% of samples but no code changes needed. Simplest, worst training data coverage.
- **Custom varlen SDPA path with `use_remove_padding=True`**: pack samples in a batch, process each per-sample in a loop for global layers. More work than Ulysses.

### Files modified in this session

| File | Change |
|------|--------|
| `verl/examples/sft/deepmed/run_gemma4_26b_sft.sh` | LoRA flags, 60K max_length, flash_attention_2, bs=8, expandable_segments |
| `verl/verl/models/transformers/gemma4.py` (NEW) | Mixed-attention monkey-patch |
| `verl/verl/models/transformers/monkey_patch.py` | Hook to call Gemma4 patch when `model_type == "gemma4"` |
| `prepare_deepmed_data.py` | AutoTokenizer fallback, `--output_dir` flag |
| `docs/04_gemma4_26b_sft.md` | This doc |

### Data artifacts

| Path | Tokenizer | Max tok | Train/Val |
|------|-----------|---------|-----------|
| `data/deepmed_trajectory/` | Qwen3.5 | 65536 | 2218 / 45 (original, untouched) |
| `data/deepmed_trajectory_gemma4_52k/` | Gemma4 | 52000 | 2085 / 42 |

### Wandb runs from this session

- `https://wandb.ai/jwu418-uc-santa-cruz-banana-slugs/deepmed-sft/runs/ueohecpu` — 60K LoRA, 4 steps before OOM
- `https://wandb.ai/jwu418-uc-santa-cruz-banana-slugs/deepmed-sft/runs/j8cinp8t` — 52K LoRA, OOM on step 1
