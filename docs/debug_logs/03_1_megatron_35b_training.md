# Qwen3.5-35B-A3B Megatron SFT Training

## 1. Model Overview

| Property | Value |
|----------|-------|
| Model | Qwen3.5-35B-A3B |
| Architecture | MoE (Mixture of Experts) with Gated Delta Net (GDN) linear attention |
| Total params | ~35B |
| Active params | ~3B per token |
| Experts | 256 total |
| Attention | MRoPE (Multi-Resolution Rotary Position Embedding) |
| Backend | Megatron-Core 0.16.0 via verl |

### Why Megatron (not FSDP)?
The previous Qwen3.5-27B training used FSDP backend with 4 GPUs and max_length=4096. For the 35B-A3B MoE model:
- FSDP does not natively support Expert Parallelism (EP), which is essential for distributing 256 experts across GPUs
- Megatron-Core provides EP, TP, PP and mBridge (model bridging for HF<->Megatron weight conversion)
- The MoE architecture requires specialized parallelism: EP=8 distributes 32 experts per GPU

---

## 2. Training Configuration

### Script
`verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh`

### Parallelism (8x H200 GPUs)

| Parallelism | Size | Notes |
|-------------|------|-------|
| Tensor Parallel (TP) | 2 | Splits attention/FFN across 2 GPUs |
| Expert Parallel (EP) | 8 | 256 experts / 8 = 32 experts per GPU |
| Pipeline Parallel (PP) | 1 | No pipeline splitting |
| Context Parallel (CP) | 1 | GDN attention incompatible with CP |

### Data Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| `train_batch_size` | 32 | Global batch size |
| `micro_batch_size_per_gpu` | 1 | 1 sample per GPU per micro-step |
| `max_length` | 57344 | Samples >57344 tokens filtered out during data prep |
| `truncation` | left | Keep most recent context |
| `use_dynamic_bsz` | False | Required: GDN attention uses bshd format |
| `use_remove_padding` | False | Required: no packed sequence support for GDN |

### Optimizer & Offloading

| Parameter | Value |
|-----------|-------|
| optimizer | Adam (bf16 precision-aware) |
| lr | 2e-5 (cosine decay to 2e-6) |
| warmup | 10 steps |
| weight_decay | 0.1 |
| clip_grad | 1.0 |
| param_offload | True (CPU) |
| grad_offload | True (CPU) |
| optimizer_offload | True (CPU, fraction=1.0) |
| overlap_cpu_d2h_h2d | True |

### Activation Recomputation

| Parameter | Value |
|-----------|-------|
| recompute_granularity | full |
| recompute_method | uniform |
| recompute_num_layers | 4 |

### MoE-Specific

| Parameter | Value |
|-----------|-------|
| moe_aux_loss_coeff | 0.01 |
| moe_z_loss_coeff | 0.001 |
| moe_permute_fusion | False |

### Checkpointing & Logging

| Parameter | Value | Notes |
|-----------|-------|-------|
| use_dist_checkpointing | False | Saves HF safetensors via mbridge (vLLM-loadable) |
| save_contents | `["model","extra"]` | No optimizer (avoids 550GB CPU memory spike) |
| save_freq | 70 | Once per epoch (steps 70, 140, 210) |
| logger | `['console','wandb']` | Training dynamics logged to W&B |

### Training Schedule
- **Steps per epoch**: 70 (2218 samples / 32 batch_size, rounded)
- **Total epochs**: 3
- **Total steps**: 210
- **Val frequency**: every 25 steps
- **Checkpoint frequency**: every 70 steps (once per epoch)

---

## 3. Debug History

### Phase 1: Qwen3.5-27B with FSDP (Bugs 1-10)

See `01_environment_and_debugging.md` for the full history of getting the 27B model running with FSDP backend. Key issues resolved:
- Python 3.14 incompatible with Ray
- flash-attn ABI mismatch (built from source)
- Transformers didn't recognize Qwen3.5 (upgraded to 5.3.0)
- Qwen3.5 strict Jinja2 chat template (added fallback tokenization)
- MRoPE position_ids shape mismatch (set position_ids=None for FSDP)
- Nested tensor errors with remove_padding (disabled)

### Phase 2: Switching to Megatron for 35B-A3B MoE

#### Bug 11: `automodel` engine backend not available
- **Symptom**: `Unknown backend: automodel`
- **Fix**: Used `engine=megatron` with mBridge for HF weight conversion

#### Bug 12: Qwen3.5-35B-A3B requires Expert Parallelism
- **Symptom**: Single GPU cannot hold 256 experts
- **Fix**: Set `EP=8` to distribute 32 experts per GPU, `TP=2` for attention layers

#### Bug 13: GDN (Gated Delta Net) incompatible with THD format
- **Symptom**: Errors when using packed sequences (remove_padding=True)
- **Cause**: Qwen3.5's GDN linear attention layers require bshd (batch-first) format
- **Fix**: Set `use_remove_padding=False` and `use_dynamic_bsz=False`

#### Bug 14: OOM at step ~41 with max_length=65536
- **Symptom**: Training ran successfully for 41 steps then crashed silently (no explicit CUDA OOM error in logs, process killed by OS OOM killer)
- **Cause**: Non-contiguous `position_ids` tensors in the Ulysses sequence parallel `all_gather` operation caused CUDA memory fragmentation. When `all_gather` receives non-contiguous tensors, PyTorch allocates temporary contiguous copies, doubling the position_ids memory footprint during the collective. Over 41 steps, accumulated fragmentation pushed peak memory past the 140GB H200 limit on certain batches with longer sequences.
- **Analysis**: The Qwen3.5 MRoPE produces 3D position_ids `(mrope_channels, batch, seq_len)` which may not be contiguous after slicing in the Ulysses SP path. The `all_gather` then implicitly copies each shard to contiguous memory, fragmenting the allocator.
- **Fix**: Added `position_ids = position_ids.contiguous()` before `all_gather` in `verl/models/transformers/monkey_patch.py:139`. This ensures the tensor is contiguous before the collective, avoiding implicit copies and fragmentation.
- **File**: `verl/verl/models/transformers/monkey_patch.py` line 139

#### Bug 15: Host OOM kills k8s pod during first training step
- **Symptom**: Training crashed during step 1 (torch.compile phase), killing the entire k8s pod
- **Cause**: Kubernetes cgroup OOM killer. Pod had 1TB limit, but full CPU offloading + torch.compile forking ~32 processes (~35GB RSS each) pushed total to ~1.6TB
- **Fix (v2, 1.8TB pod)**: Pod upgraded to 1.8TB, `TORCH_COMPILE_THREADS=8` limits forked processes, full offloading restored
- **Files**: `run_qwen3_5_35b_a3b_sft_megatron.sh`, `monitor_training.sh`

#### Bug 16: GPU OOM during backward pass at step ~13
- **Symptom**: `torch.OutOfMemoryError: Tried to allocate 25.98 GiB` on GPU2 during backward. Training ran 12 steps fine then crashed on a batch with longer sequences.
- **Cause**: CUDA memory allocator fragmentation. GPU2 had 35.59 GiB reserved-but-unallocated (fragmented), more than the 25.98 GiB needed but unusable due to non-contiguous free blocks. Variable sequence lengths (up to 65K tokens) cause unpredictable peak memory per batch.
- **What did NOT work**: `cpu_offloading=True` in transformer config - incompatible with activation recomputation (`ValueError: CPU offloading does not work when activation recomputation is enabled`)
- **Fix**: Added `max_split_size_mb:128` to `PYTORCH_ALLOC_CONF` to reduce allocator fragmentation by using smaller block splits
- **Also fixed**: Renamed `PYTORCH_CUDA_ALLOC_CONF` (deprecated) to `PYTORCH_ALLOC_CONF`
- **Files**: `run_qwen3_5_35b_a3b_sft_megatron.sh`

#### Bug 17: CPU OOM during checkpoint save at step 50
- **Symptom**: Host memory spiked from 1268GB → 1694GB in 22 seconds during checkpoint save, hitting 1.8TB pod limit
- **Cause**: `generate_state_dict()` materializes optimizer states (~420GB already on CPU) for serialization, causing ~550GB spike
- **Fix**: `checkpoint.save_contents='["model","extra"]'` skips optimizer (spike reduced to ~100GB). Added OOM guard script. Changed to `use_dist_checkpointing=False` for HF-format saves.
- **Also fixed**: verl bug in `transformer_impl.py:248` — guard for None `dist_checkpointing_path`

#### Bug 18: Repeated GPU OOM during training with max_length=65536
- **Symptom**: Training crashed repeatedly at various steps (step ~26, ~100, step 1) with GPU OOM or NCCL OOM
- **Root cause**: Variable sequence lengths up to 65536 tokens caused unpredictable GPU memory spikes during backward pass. Longer sequences in some batches exceeded the 140GB H200 GPU limit.
- **Attempted fixes that were insufficient alone**:
  - `recompute_num_layers=2` (survived to step 100, then crashed)
  - `recompute_num_layers=4` with `max_split_size_mb=64` (crashed step 1 — allocator over-fragmented)
  - `recompute_num_layers=3` (crashed — 3 doesn't evenly divide 40 layers, `IndexError`)
  - `TP=4` (failed — num_key_value_heads=2, not divisible by 4)
  - `PP=2` (failed — TP×PP×EP = 2×2×8 = 32 > 8 GPUs)
- **Final fix**: Reduced `max_length` from 65536 to 57344 + `recompute_num_layers=4` + `max_split_size_mb=128`
  - Data re-prepared with `--max_token_length 57344`: 2218 train + 45 val samples (was 2449 + 50)
  - Steps per epoch: 70 (was 73), save_freq: 70 (was 73)
- **Key constraints documented**:
  - TP must divide num_key_value_heads (=2), so TP max = 2
  - PP×TP×EP must ≤ world_size (8), so PP must = 1 with TP=2, EP=8
  - recompute_num_layers must evenly divide num_hidden_layers (=40): valid values 1,2,4,5,8,10,20,40
- **Result**: Training completed epochs 1 and 2 successfully. Checkpoints uploaded to HuggingFace.

See `docs/debug_training_crash.md` for full crash analysis, memory budgets, and pod recovery guide.

---

## 4. Code Changes Summary (for 35B-A3B Megatron)

### Modified Files in verl

1. **`verl/models/transformers/monkey_patch.py`** (line 139)
   - Added `.contiguous()` call on `position_ids` before `all_gather` in Ulysses flash attention
   - Prevents memory fragmentation from non-contiguous MRoPE position_ids

2. **`verl/utils/dataset/multiturn_sft_dataset.py`** (lines 299-365)
   - Added try/except fallback for strict chat templates (Qwen3.5)
   - Fallback: tokenize full conversation, build loss mask via incremental prefix diffing
   - (Same change as for 27B model)

3. **`verl/workers/engine/fsdp/transformer_impl.py`** (line 973)
   - Set `position_ids=None` for FSDP path to avoid MRoPE shape mismatch
   - (Only affects FSDP backend, not Megatron, but kept for compatibility)

### New Files

4. **`verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh`**
   - Full training launch script for Qwen3.5-35B-A3B with Megatron backend
   - Configures EP=8, TP=2, mBridge, CPU offloading, activation recomputation

---

## 5. Training Metrics (from 41-step run before OOM fix)

The initial run completed 41 steps before the OOM fix was applied:

| Metric | Step 1 | Step 10 | Step 20 | Step 30 | Step 41 |
|--------|--------|---------|---------|---------|---------|
| train/loss | 0.430 | 0.322 | 0.289 | 0.311 | 0.258 |
| train/grad_norm | 5.12 | 1.00 | 0.61 | 0.49 | 0.46 |
| train/lr | 2e-6 | 2e-5 | 1.99e-5 | 1.96e-5 | 1.90e-5 |
| val/loss (step 25) | 0.292 | - | - | - | - |

Loss shows good convergence from 0.43 to 0.26 over 41 steps.

---

## 6. Running the Training

```bash
cd /fsx-shared/juncheng/EHR

# Launch training (8 GPUs, ~5 hours for 219 steps)
# Includes OOM guard (kills at 96% to prevent pod crash),
# enhanced monitoring, and per-step memory logging by default.
nohup bash verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh > training_v3.log 2>&1 &
```

### Monitoring
- **wandb**: Project `deepmed-sft`, experiment `deepmed-sft-qwen3_5-35b-a3b-megatron-tp2-pp1-ep8`
- **Training log**: `training_v3.log` (stdout/stderr)
- **System monitor**: `training_system_monitor.log` (adaptive interval: 10s normal, 2s during checkpoint save)
- **Per-step memory**: `memory_per_step.csv` (CPU memory after each training step, for leak detection)
- **OOM guard log**: `oom_guard.log` (logs kill events if memory exceeds 96%)
- **Checkpoints**: `/fsx-shared/juncheng/EHR/verl/checkpoints/deepmed-sft/...` (on fsx-shared, persists)
- **Debug & recovery**: `docs/debug_training_crash.md` (Bugs 15-17 + pod recovery guide)

### Safety: OOM Guard
The training script automatically starts `oom_guard.sh` which kills training at 96% cgroup memory to prevent the pod from crashing. Override threshold: `OOM_THRESHOLD=90 bash verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh`

### Expected Timeline
- Steps 1-10: warmup (lr ramps from 2e-6 to 2e-5)
- Steps 11-70: Epoch 1
- Steps 71-140: Epoch 2
- Steps 141-210: Epoch 3
- Validation at steps 25, 50, 75, 100, 125, 150, 175, 200
- Checkpoints at steps 70, 140, 210 (once per epoch, model-only)

### Trained Model Checkpoints (on HuggingFace)
- **Epoch 1**: `Chtholly17/Qwen3.5-35B-A3B-DeepMed-SFT-epoch1` (global_step_70)
- **Epoch 2**: `Chtholly17/Qwen3.5-35B-A3B-DeepMed-SFT-epoch2` (global_step_140)
