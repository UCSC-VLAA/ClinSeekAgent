# Gemma-4-26B-A4B-it FSDP SFT Training

## Status Summary (2026-04-16)

- **Training**: Working at `max_length=8192` with SDPA on 8x H200 FSDP. 40-step validation passed (loss 3.85 → 0.26, ~16.5s/step).
- **5 bugs fixed**: loss_mask corruption, FSDP wrap crash, mm_token_type_ids, flash-attn head_dim, flex_attention Triton overflow.
- **64K blocker**: Gemma4's `global_head_dim=512` exceeds the hard limit of ALL kernel-based attention (flash-attn 2/3: 256 cap, flex_attention/Triton: shared memory overflow). Only SDPA works, but it's O(n^2) → OOM at 32K.
- **Megatron**: Not implemented. Would require new registry entries, config converter, weight converter. Even with Megatron, the head_dim=512 kernel limit persists.
- **Next step**: SDPA + CPU offloading to push max_length to ~16K-32K.

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
| `train_batch_size` | 32 | Global batch size |
| `micro_batch_size_per_gpu` | 1 | 1 sample per GPU per micro-step |
| `max_length` | 8192 | Limited by SDPA memory (see Bug 4) |
| `truncation` | left | Keep most recent context |
| `use_dynamic_bsz` | False | Standard fixed batching |
| `use_remove_padding` | False | Conservative setting |
| `ignore_input_ids_mismatch` | True | Enables fallback tokenization (required for Bug 1 fix) |

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

### Training Metrics (40-step validation run, max_length=8192, 8x H200)

| Step | train/loss | train/grad_norm | train/lr |
|------|-----------|----------------|----------|
| 1 | 3.854 | 1098.1 | 1e-5 |
| 5 | 1.155 | 78.4 | 1.97e-5 |
| 10 | 0.409 | 12.3 | 1.79e-5 |
| 20 | 0.277 | 5.3 | 1.11e-5 |
| 30 | 0.213 | 4.3 | 3.4e-6 |
| 40 | 0.255 | 6.3 | 0.0 |

- **Speed**: ~16.5 s/step (first step ~43s due to compilation warmup)
- **Throughput**: ~262K tokens/step = ~16K tokens/s
- **Memory**: ~96 GB / 140 GB per GPU (69%)

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

### Bug 6: Mixed per-layer attention dispatch fails

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

4. **`verl/examples/sft/deepmed/run_gemma4_26b_sft.sh`** (Bug 4)
   - Added `+model.override_config.attn_implementation=sdpa` to use SDPA instead of flash_attention_2

5. **`verl/verl/models/transformers/monkey_patch.py`** (Bug 5/6 investigation)
   - Added documentation comment noting that Gemma4's `global_head_dim=512` exceeds ALL kernel-based attention limits (flash-attn, flex_attention, Triton). Only SDPA and eager work.

### Previously Existing Files (from prior session)

6. **`verl/verl/utils/tokenizer.py`** line 149
   - Added `case "Gemma4Processor": pass` in the processor match statement

---

## 6. Running the Training

```bash
source /fsx-shared/juncheng/EHR/venvs/gemma4/bin/activate
cd /fsx-shared/juncheng/EHR/verl

# 40-step validation run (max_length=8192, ~11 min)
bash examples/sft/deepmed/run_gemma4_26b_sft.sh 8 \
    ./checkpoints/deepmed-sft/gemma4-26b-test \
    data.max_length=8192 \
    trainer.total_training_steps=40 \
    trainer.total_epochs=999 \
    trainer.save_freq=40 \
    trainer.test_freq=20 \
    trainer.logger=console

# Full training run (3 epochs, max_length=8192)
nohup bash examples/sft/deepmed/run_gemma4_26b_sft.sh 8 \
    ./checkpoints/deepmed-sft/gemma4-26b \
    data.max_length=8192 \
    > /fsx-shared/juncheng/EHR/gemma4_training.log 2>&1 &
```

---

## 7. Current Limitations & The 64K Token Problem

### Root Cause: `global_head_dim=512`

Gemma4's global attention layers use `global_head_dim=512` (6 out of 30 layers). This exceeds the hard limits of **every** kernel-based attention implementation:

| Backend | Head Dim Limit | Status | Error |
|---------|---------------|--------|-------|
| flash-attn 2.8.3 | ≤ 256 | BLOCKED | `FlashAttention forward only supports head dimension at most 256` |
| flash-attn 3 (Hopper) | ≤ 256 | BLOCKED | Same kernel limit; built from source, confirmed `assert head_dim <= 256` in both `flash_attn_interface.py` and `cute/interface.py` |
| flex_attention (Triton) | ≤ ~256 | BLOCKED | `OutOfMemoryError: out of resource: Required: 262144 Hardware limit: 232448` — Triton shared memory overflow at head_dim=512 |
| SDPA (PyTorch matmul) | Unlimited | WORKS | O(n^2) memory → OOM at max_length=32768 on 8x H200 |
| Eager | Unlimited | WORKS | Slowest, O(n^2) memory |

**Conclusion**: `global_head_dim=512` is a fundamental hardware constraint on H200 (sm_90). No currently available attention kernel supports it. Only SDPA and eager work, limiting max_length to ~8192 on 8x H200 with FSDP.

### Investigation Log

1. **Flash-attn 3 (Hopper)**: No PyPI package. Built from `github.com/Dao-AILab/flash-attention/hopper/`. Both `flash_attn_interface.py:round_up_headdim()` and `cute/interface.py` enforce `head_dim <= 256`. The `"64_512"` kernel variant is for q_headdim=64 / v_headdim=512 (different Q/V dims), not for head_dim=512 on both Q and K.

2. **flex_attention**: Available in PyTorch 2.9.0 and supported by Gemma4 (`_supports_flex_attn = True`). However, the compiled Triton kernel requires shared memory proportional to head_dim. At 512, it exceeds H200's 232KB limit (needs 256KB). Fails at **any** sequence length, not just long ones.

3. **Mixed per-layer dispatch** (flash-attn for sliding, SDPA for global): Attempted via monkey patch, but failed because attention masks are pre-computed at the model level in `create_causal_mask_mapping()` for a single `_attn_implementation`. Different backends expect incompatible mask formats.

### Megatron Backend: What Would Be Needed

Adding Megatron support for Gemma4 requires substantial work in verl:

| Component | File | Status |
|-----------|------|--------|
| `SupportedModel.GEMMA4` enum | `verl/models/mcore/registry.py` | Not implemented |
| Config converter (`hf_to_mcore_config_gemma4`) | `verl/models/mcore/config_converter.py` | Not implemented |
| Model initializer | `verl/models/mcore/model_initializer.py` | Can reuse existing MoE initializer |
| Weight converter | `verl/models/mcore/weight_converter.py` | Not implemented |
| mBridge compatibility | External `mbridge` package | Unknown — needs verification |
| All 6 registry mappings | `registry.py` | Not implemented |

Key Gemma4 architecture challenges for Megatron:
- **Hybrid attention**: Different head_dim per layer type (256 vs 512) — Megatron TransformerConfig assumes uniform head_dim
- **Per-layer RoPE**: Different theta/type per layer — not standard in Megatron
- **`attention_k_eq_v`**: Shared K/V projections on global layers — requires custom attention module
- **Nested config**: `Gemma4Config.text_config` vs flat config — config converter must extract text_config

**Note**: Even with Megatron, the global_head_dim=512 constraint remains — Megatron uses the same flash-attn kernels. The benefit would be from CPU offloading, activation recomputation, and expert parallelism allowing more memory headroom for SDPA on fewer layers.

### Viable Path Forward

The most practical approach for longer sequences:

1. **SDPA at max_length=8192** (current, working): Production-ready, validated with 40 steps
2. **SDPA with CPU offloading** (next step): Add `engine.param_offload=True`, `engine.optimizer_offload=True` to free GPU memory for longer sequences. This could potentially push max_length to ~16K-32K
3. **Wait for kernel support**: Google/DeepMind's custom Pallas kernels or future flash-attn releases may add head_dim=512 support
