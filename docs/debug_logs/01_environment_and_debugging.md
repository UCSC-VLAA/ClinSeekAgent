# Environment Configuration & Debugging History

## 1. Environment Overview

| Component | Version |
|-----------|---------|
| Python | 3.10.12 |
| PyTorch | 2.9.0+cu128 |
| Transformers | 5.3.0 |
| flash-attn | 2.8.3 (built from source) |
| vLLM | 0.12.0 |
| Ray | 2.54.0 |
| NumPy | 1.26.4 |
| CUDA | 12.8 |
| verl | 0.8.0.dev (editable install) |

### Hardware
- **GPUs**: 8x NVIDIA H200 (139.8 GB each), training uses 4 GPUs
- **GPU Memory Usage (during training)**: ~134-136 GB per GPU (93% utilization)
- **OS**: Linux 6.1.134 (Amazon Linux 2023)

### Key Paths
- **Project root**: `/fsx-shared/juncheng/EHR/`
- **Virtual environment**: `/fsx-shared/juncheng/EHR/openresearcher_ehr/.venv`
- **verl source (editable)**: `/fsx-shared/juncheng/EHR/verl/`
- **Dataset**: `~/data/deepmed_trajectory/{train,val}.parquet`
- **Training script**: `verl/examples/sft/deepmed/run_qwen3_5_27b_sft_default.sh`
- **Checkpoints**: `verl/checkpoints/deepmed_qwen3.5_27b/`

---

## 2. Bugs Encountered and Fixes

### Bug 1: Python 3.14 incompatible with Ray
- **Symptom**: `ray` has no wheels for `cp314`
- **Fix**: Recreated venv with Python 3.10: `uv venv --python 3.10 --clear`

### Bug 2: flash-attn prebuilt binary ABI mismatch
- **Symptom**: `undefined symbol: _ZN3c104cuda29c10_cuda_check_implementationEiPKcS2_jb` when importing flash_attn
- **Cause**: Prebuilt flash-attn 2.8.3 wheel was linked against a different PyTorch ABI
- **Fix**: Built flash-attn from source against torch 2.9.0:
  ```bash
  pip install flash-attn --no-build-isolation
  ```
  This compilation takes ~10 minutes.

### Bug 3: torchvision::nms does not exist
- **Symptom**: `RuntimeError: torchvision::nms does not exist` on import
- **Cause**: torch 2.10/torchvision version mismatch
- **Fix**: Downgraded to torch 2.9.0: `uv pip install torch==2.9.0`

### Bug 4: numpy >= 2.0 breaks verl
- **Symptom**: Various numpy compatibility errors
- **Cause**: Other packages install numpy 2.x, but verl requires `numpy<2.0.0`
- **Fix**: `uv pip install 'numpy<2.0.0'`

### Bug 5: Transformers doesn't recognize Qwen3.5
- **Symptom**: `Unknown model type: qwen3_5`
- **Cause**: transformers 4.57.6 predates Qwen3.5 support
- **Fix**: Upgraded to transformers 5.3.0: `uv pip install 'transformers>=5.3.0'`

### Bug 6: `automodel` engine backend not found
- **Symptom**: `Unknown backend: automodel` when launching training
- **Cause**: `automodel` engine requires `nemo_automodel` (NVIDIA NeMo), not installed
- **Fix**: Switched to default FSDP engine (removed `engine=automodel` from script)

### Bug 7: HuggingFace datasets serializes dicts as JSON strings
- **Symptom**: Messages in parquet were JSON strings instead of Python dicts
- **Cause**: HF `datasets.to_parquet()` serializes nested structures as JSON strings
- **Fix**: Used `pandas.DataFrame.to_parquet()` instead (see `prepare_deepmed_data.py`)

### Bug 8: Qwen3.5 strict Jinja2 chat template
- **Symptom**: `jinja2.exceptions.TemplateError: System message must be at the beginning` and `No user query found in messages`
- **Cause**: verl's `MultiTurnSFTDataset` calls `apply_chat_template` per-message (e.g., `messages=[single_msg]`), but Qwen3.5's template requires system message first and user message before assistant
- **Fix**: Added fallback in `multiturn_sft_dataset.py` `__getitem__()` that tokenizes the full conversation at once, then builds loss mask via incremental prefix diffing
- **File**: `verl/verl/utils/dataset/multiturn_sft_dataset.py` lines 317-365

### Bug 9: `split_with_sizes` nested tensor error with `use_remove_padding=True`
- **Symptom**: `RuntimeError: split_with_sizes expects split_sizes to sum exactly to 4096 but got split_sizes=[4, 4, ..., 4]`
- **Cause**: Dynamic batching with `use_remove_padding=True` creates nested tensors incompatible with Qwen3.5's batch rearrangement
- **Fix**: Set `model.use_remove_padding=False` and `data.use_dynamic_bsz=False` in training script

### Bug 10: Rotary position embedding shape mismatch (MRoPE)
- **Symptom**: `RuntimeError: The size of tensor a (4096) must match the size of tensor b (4) at non-singleton dimension 2`
- **Cause**: Qwen3.5 is a VL model (`Qwen3_5ForConditionalGeneration`) that uses 3D MRoPE (Multi-Resolution Rotary Position Embedding) with `position_ids` shape `(4, batch, seq)` for text/temporal/height/width. verl's FSDP engine passes 2D `position_ids` `(batch, seq)` which gets incorrectly expanded, or nested tensor position_ids get wrong dimensions during collation.
- **Fix**: Set `position_ids=None` in `verl/verl/workers/engine/fsdp/transformer_impl.py` (line ~976), letting the model auto-compute position_ids internally from `cache_position`. This works because for text-only data (no vision tokens), the model creates simple ascending position_ids.
- **File**: `verl/verl/workers/engine/fsdp/transformer_impl.py` lines 973-981

---

## 3. Code Changes Summary

### Modified Files in verl

1. **`verl/verl/utils/dataset/multiturn_sft_dataset.py`**
   - Added try/except fallback in `__getitem__()` for strict chat templates
   - Fallback tokenizes full conversation, builds loss mask via incremental prefix tokenization
   - Skips system messages in the prefix loop (they can't be tokenized alone)

2. **`verl/verl/workers/engine/fsdp/transformer_impl.py`**
   - Set `position_ids=None` in the `use_remove_padding=False` + `pad_mode=no_padding` branch
   - This lets VL models like Qwen3.5 auto-compute position_ids from cache_position

---

## 4. Working Training Configuration

```bash
bash examples/sft/deepmed/run_qwen3_5_27b_sft_default.sh 4 ./checkpoints/deepmed_qwen3.5_27b
```

Key flags that were required:
- `model.use_remove_padding=False` - disables nested tensor packing
- `data.use_dynamic_bsz=False` - uses fixed batch sizes
- `data.ignore_input_ids_mismatch=True` - allows fallback tokenization path
- No explicit `engine=` setting (uses default FSDP)
- No explicit `model.attn_implementation=` (Qwen3.5 defaults to flash_attention_2, which works since flash-attn is installed)

---

## 5. Known Issues (Current)

1. **Loss not converging** - Default optimizer config has `lr=0.001` (too high for SFT), `lr_scheduler_type=constant` (no decay), `lr_warmup_steps_ratio=0.0` (no warmup). These need tuning.
2. **`enable_thinking` warnings** - Harmless: `Keyword argument 'enable_thinking' is not a valid argument for this processor and will be ignored.`
3. **Token length warnings** - Expected: `Token indices sequence length is longer than the specified maximum sequence length for this model (364567 > 262144)`. Sequences are truncated to `max_length=4096`.
4. **Flash Attention dtype warning** - Harmless: `Flash Attention 2 only supports torch.float16 and torch.bfloat16 dtypes, but the current dype in Qwen3_5Model is torch.float32`. Training uses bf16 autocast at engine level.
5. **Slow first step** - First training step takes ~10 min due to `torch.compile` warmup. Subsequent steps are ~5 min each.
