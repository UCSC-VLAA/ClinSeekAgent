# Training Crash Debug Log & Pod Recovery Guide

This document consolidates all crash debugging, fixes, and pod recovery procedures for the Qwen3.5-35B-A3B Megatron SFT training.

---

## Crash #1: Host OOM (cgroup kill) - 2026-03-24

### Summary
Training crashed during the **first training step** (Epoch 1/3, 0/73), killing the entire k8s pod. This was a **host memory** issue, not GPU OOM.

### Root Cause: Kubernetes cgroup OOM killer

Evidence from `dmesg`:
```
oom-kill:constraint=CONSTRAINT_MEMCG
Memory cgroup out of memory: Killed process 2682956 (python3)
  total-vm:13834171900kB (~13.2TB virtual)
  anon-rss:58828756kB (~56GB per process)
  shmem-rss:31441176kB (~30GB shared memory per process)
```

### Memory Budget Analysis (1TB pod)

| Component | Memory |
|-----------|--------|
| **Pod memory limit (cgroup)** | **1000 GB** |
| 8 GPU workers x ~56GB anon RSS | ~448 GB |
| Shared memory (NCCL/CUDA IPC) | ~30 GB |
| **~32 torch.compile workers** x ~35GB each | **~1120 GB** |
| **Total estimated** | **~1600 GB** |

### Why It Happened
1. Training script has **full CPU offloading** enabled:
   - `param_offload=True`, `grad_offload=True`
   - `optimizer_offload=True` with `optimizer_offload_fraction=1`
2. This moves ~420GB of optimizer states + params + gradients to CPU
3. `torch.compile` then forks ~32 processes that **inherit** these CPU allocations (~35GB RSS each)
4. The crash happened during torch.compile phase of step 1

### Fix Applied (v1 - 1TB pod, superseded by v2)
1. Reduced `optimizer_offload_fraction` from 1.0 to 0.5
2. Disabled `param_offload` (keep params on GPU)
3. Added system monitoring script (`monitor_training.sh`)

### Fix Applied (v2 - 1.8TB pod, 2026-03-25)
Pod memory extended from 1TB to 1.8TB. Restored full offloading with controlled compile threads:
1. **Kept full offloading** - `optimizer_offload_fraction=1`, `param_offload=True`, `grad_offload=True`, `optimizer_offload=True`
2. **`TORCH_COMPILE_THREADS=8`** - limits forked processes (8 x ~35GB = ~280GB instead of 32 x ~35GB = ~1120GB)
3. **Monitor interval: 10s** (was 30s)
4. **Multi-threshold warnings** at 70%, 80%, 90% of pod limit

Estimated peak: ~1178GB (workers 448 + shm 30 + offload 420 + compile 280). Headroom: ~622GB.

**Result**: Host memory stabilized at ~1091GB / 1800GB (60.7%). Passed torch.compile phase successfully.

---

## Crash #2: GPU OOM (CUDA memory fragmentation) - 2026-03-25

### Summary
Training crashed at **step ~13** (Epoch 1/3) with `torch.OutOfMemoryError` on GPU2 during backward pass. The host memory fix (v2) was working fine; this was a separate GPU memory issue.

### Root Cause: CUDA memory allocator fragmentation

Error message:
```
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 25.98 GiB.
GPU 2 has a total capacity of 139.81 GiB of which 24.95 GiB is free.
Including non-PyTorch memory, this process has 114.77 GiB memory in use.
Of the allocated memory 66.35 GiB is allocated by PyTorch, and
35.59 GiB is reserved by PyTorch but unallocated.
```

### Key Insight
**35.59 GiB reserved but unallocated** > 25.98 GiB needed. There was enough total memory but the allocator couldn't use it due to fragmentation. The default PyTorch allocator creates large contiguous blocks that become fragmented over time, leaving unusable holes.

### GPU Memory at Crash Time (from monitor log)
```
GPU0: 110GB  GPU1: 125GB  GPU2: 114GB (crashed)  GPU3: 132GB
GPU4: 125GB  GPU5: 124GB  GPU6: 90GB   GPU7: 89GB
```
All GPUs were near capacity (140GB H200s). GPU2 happened to OOM first during backward pass.

### Why Variable Memory Usage
- `pad_mode=no_padding` with `use_dynamic_bsz=False` means each micro-batch is one sample of variable length
- Samples near 65K tokens require much more activation memory during backward than shorter ones
- Steps 1-12 had shorter sequences; step 13+ hit a batch with longer sequences
- MoE routing causes uneven expert activation across GPUs, adding load imbalance

### Fix Applied
Changed CUDA allocator config to reduce fragmentation:
```bash
# Before (only expandable segments)
export PYTORCH_ALLOC_CONF=expandable_segments:True

# After (added max_split_size to reduce fragmentation)
export PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
```

`max_split_size_mb:128` forces the allocator to use smaller block splits, reducing the chance of large unusable reserved regions.

### What Did NOT Work
- **CPU activation offloading** (`cpu_offloading=True`): Incompatible with activation recomputation in Megatron. Error: `ValueError: CPU offloading does not work when activation recomputation is enabled`
- These are mutually exclusive in Megatron-Core. We kept activation recomputation (already configured with `recompute_granularity=full`, `recompute_method=uniform`, `recompute_num_layers=1`).

### Also Fixed
- `PYTORCH_CUDA_ALLOC_CONF` is deprecated; must use `PYTORCH_ALLOC_CONF`

**Result**: Training passed step 16+ (previous crash point). GPU memory still tight (up to 136.8GB / 140GB) but allocator handles it without fragmentation holes.

---

## Crash #4: Repeated GPU OOM during training - 2026-03-26

### Summary
Training crashed repeatedly at various steps with GPU OOM when using `max_length=65536`. Multiple mitigation attempts were tried before finding the working combination.

### Crash Timeline

| Config | Crashed At | Cause |
|--------|-----------|-------|
| recompute_num_layers=1, split=128, max_len=65536 | Step ~26 | GPU OOM on long sequence batch |
| recompute_num_layers=2, split=128, max_len=65536 | Step ~100 | GPU OOM on long sequence batch |
| recompute_num_layers=4, split=64, max_len=65536 | Step 1 | NCCL OOM — allocator over-fragmented |
| recompute_num_layers=4, split=128, max_len=65536 | Early steps | GPU OOM |
| **recompute_num_layers=4, split=128, max_len=57344** | **Completed** | **Training succeeded** |

### Root Cause
Variable sequence lengths up to 65536 tokens caused unpredictable GPU memory spikes during backward pass. Some batches with sequences near 65K tokens required more activation memory than available on 140GB H200 GPUs.

### What Did NOT Work

1. **TP=4**: `RuntimeError: shape mismatch` — num_key_value_heads=2, not divisible by 4
2. **PP=2**: `RuntimeError: world_size (8) not divisible by expert_tensor_model_pipeline_parallel size (16)` — TP×PP×EP = 2×2×8 = 32 > 8 GPUs
3. **recompute_num_layers=3**: `IndexError: index 40 is out of range` — 3 doesn't evenly divide 40 layers
4. **max_split_size_mb=64**: NCCL OOM at step 1 — too many tiny blocks fragment the allocator

### Parallelism Constraints (Qwen3.5-35B-A3B)
- **TP**: Must divide num_key_value_heads (=2). Max TP = 2.
- **PP**: TP×PP×EP ≤ world_size. With TP=2, EP=8: PP must = 1.
- **EP**: Already maxed at 8 (= num GPUs).
- **recompute_num_layers**: Must evenly divide num_hidden_layers (=40). Valid: 1, 2, 4, 5, 8, 10, 20, 40.

### Fix Applied
1. **Reduced max_length**: 65536 → 57344
2. **Increased recompute_num_layers**: 1 → 4 (recomputes activations for groups of 4 layers instead of 1)
3. **Re-prepared training data**: `python prepare_deepmed_data.py --max_token_length 57344` → 2218 train + 45 val samples
4. **Updated save_freq**: 73 → 70 (steps per epoch = ceil(2218/32) = 70)

**Result**: Training completed epochs 1 and 2 successfully. Checkpoints uploaded to HuggingFace as `Chtholly17/Qwen3.5-35B-A3B-DeepMed-SFT-epoch1` and `Chtholly17/Qwen3.5-35B-A3B-DeepMed-SFT-epoch2`.

---

## Final Working Configuration

### Environment Variables
```bash
export CUDA_DEVICE_MAX_CONNECTIONS=1
export PYTORCH_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
export TORCH_COMPILE_THREADS=8
```

### Offloading (all on CPU)
```
param_offload=True
grad_offload=True
optimizer_offload=True
optimizer_offload_fraction=1
overlap_cpu_optimizer_d2h_h2d=True
use_precision_aware_optimizer=True
```

### Activation Recomputation (full)
```
recompute_granularity=full
recompute_method=uniform
recompute_num_layers=4
```

### Data
```
max_length=57344
train_samples=2218, val_samples=45
steps_per_epoch=70
```

### Parallelism
```
TP=2  PP=1  CP=1  EP=8  (8 GPUs total)
```

### Memory Footprint (steady state)
| Resource | Usage | Limit |
|----------|-------|-------|
| Host (cgroup) | ~1091 GB | 1800 GB |
| GPU (peak) | ~137 GB | 140 GB |
| Python processes | ~72 (8 workers + compile threads) | - |

### Checkpoint Save Config
```
engine.use_dist_checkpointing=False           # Save HF safetensors (vLLM-loadable)
checkpoint.save_contents='["model","extra"]'  # No optimizer (avoids 550GB spike)
trainer.save_freq=70                          # Once per epoch
trainer.logger="['console','wandb']"          # Log to wandb for training dynamics
ckpts_home=/fsx-shared/juncheng/EHR/verl/checkpoints/...  # Persists on fsx-shared
```

### Safety
```
oom_guard.sh (default: 96% threshold, 5s interval)  # Auto-started by training script
```

---

## Crash #3: CPU OOM during checkpoint save (step 50) - 2026-03-25

### Summary
Training crashed at **step 50** during checkpoint save. Host memory spiked from 1268 GB → 1694 GB in 22 seconds, exceeding 1.8 TB pod limit. OOM killer terminated training processes.

### Root Cause: Checkpoint save materializes optimizer state dict in CPU memory

Evidence from `training_system_monitor.log`:
```
07:41:07 UTC: 1268.5 GB (70.5%) - post-validation
07:41:18 UTC: 1475.0 GB (81.9%) - checkpoint save starting
07:41:29 UTC: 1693.8 GB (94.1%) - CRITICAL → OOM kill
```

`dmesg` confirmed cgroup OOM kills. Each worker process had ~148 GB RSS (up from ~110 GB baseline).

### Why It Happened
With full CPU offloading, optimizer states (~420 GB) are already on CPU. When saving a checkpoint, `generate_state_dict()` creates additional copies/buffers for serialization, spiking memory by ~500-550 GB.

### What Did NOT Work

**Distributed checkpointing** (`engine.use_dist_checkpointing=True`):
- Required a verl bug fix: `transformer_impl.py:248` crashed with `TypeError: stat: path should be string, bytes, os.PathLike or integer, not NoneType` because it tried to load from `dist_checkpointing_path` (None) on fresh training.
- **Fix applied**: Added guard `if self.engine_config.use_dist_checkpointing and self.engine_config.dist_checkpointing_path:` at `verl/verl/workers/engine/megatron/transformer_impl.py:248` so it falls through to mbridge when path is None.
- **Result**: Distributed checkpointing was confirmed active, but memory still spiked from 1231 GB → 1789 GB (+558 GB) during checkpoint save at step 2 in a test run. The `generate_state_dict()` call still materializes optimizer states for serialization.

### Fix Applied (v3)
1. **Skip optimizer state saving**: `checkpoint.save_contents='["model","extra"]'` — saves only model weights and RNG state, not optimizer states (~420 GB saved).
   - Trade-off: Cannot resume training from checkpoint (must restart from step 0). Acceptable for 3-epoch SFT.
2. **Reduced checkpoint frequency**: `trainer.save_freq=73` (once per epoch, was 50)
3. **Checkpoint dir on /fsx-shared**: `ckpts_home=/fsx-shared/juncheng/EHR/verl/checkpoints/...` (persists across pod restarts)
4. **OOM guard script**: `oom_guard.sh` kills training at 96% cgroup usage to prevent pod crash. Run: `bash oom_guard.sh 96 5 &`
5. **Enhanced monitoring**: `monitor_training_enhanced.sh` with adaptive intervals (2s during checkpoint save) and `log_memory_per_step.sh` for per-step CSV logging.

### Expected Memory During Checkpoint Save (post-fix)
Without optimizer states, checkpoint save should only materialize model weights (~68 GB for Qwen3.5-35B-A3B). Expected spike: ~100-150 GB (well within the ~600 GB headroom).

### Files Modified
1. `verl/verl/workers/engine/megatron/transformer_impl.py` (line 248) — guard dist_checkpointing_path for None
2. `verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh` — added checkpoint.save_contents, save_freq=73, ckpts on fsx-shared, enhanced monitoring, oom_guard

### New Files
3. `oom_guard.sh` — kills training at configurable memory threshold to prevent pod crash
4. `monitor_training_enhanced.sh` — phase-aware adaptive monitoring (2s during checkpoint saves)
5. `log_memory_per_step.sh` — per-step CPU memory CSV logger
6. `analyze_memory_leak.py` — memory leak trend analyzer (linear regression)

---

## Checkpoint Format: dist_checkpointing vs HF safetensors - 2026-03-25

### Summary
Tested both `use_dist_checkpointing=True` and `False` for checkpoint saving.

### With `use_dist_checkpointing=True` (current setting)
- **Saves**: `dist_ckpt/*.distcp` (Megatron distributed format) + `huggingface/` (config/tokenizer only, no weights)
- **Memory**: Model-only save spiked +99GB (1154→1253GB, 64→69%). Safe.
- **vLLM loading**: NOT directly compatible. vLLM needs HF safetensors.
- **Conversion**: verl provides `python -m verl.model_merger merge --backend megatron --local_dir <ckpt_dir> --target_dir <hf_dir>` but it does not yet support `Qwen3_5MoeForConditionalGeneration` architecture (VL config with nested `text_config`). A custom conversion script (`convert_dist_ckpt_to_hf.py`) was started but hit `rope_theta` attribute mismatch.

### With `use_dist_checkpointing=False`
- **Saves**: HF safetensors via mbridge (directly vLLM-loadable)
- **Memory**: Not yet tested — first test attempt hit GPU OOM due to leftover processes from prior run, not from the config change itself.

### Decision
Proceed with `use_dist_checkpointing=False` for full training. This saves model in HF safetensors format directly, avoiding the need for post-training conversion. If GPU OOM recurs during initialization, it's a residual process issue, not a config issue.

### Checkpoint Conversion (for dist_ckpt if needed later)
```bash
# Standard verl merger (works for Qwen3MoE, LLaMA, etc.)
python -m verl.model_merger merge \
    --backend megatron \
    --trust-remote-code \
    --use_cpu_initialization \
    --local_dir checkpoints/global_step_N \
    --target_dir /path/to/hf_output

# For Qwen3.5 MoE (VL config): needs custom handling
# See convert_dist_ckpt_to_hf.py (WIP)
```

---

## OOM Guard (Default Protection)

**All training scripts now include OOM guard by default.** This prevents the cgroup OOM killer from destroying the entire pod by pre-emptively killing training when memory exceeds a threshold.

### How It Works
`oom_guard.sh` runs in the background alongside training:
- Checks cgroup memory every 5 seconds
- If usage exceeds threshold (default: 96%), sends SIGTERM then SIGKILL to torchrun
- Logs kill event to `/fsx-shared/juncheng/EHR/oom_guard.log`
- Uses pure bash integer math (no python dependency in hot path)

### Usage
```bash
# Standalone (default: 96% threshold, 5s interval)
bash oom_guard.sh &

# Custom threshold and interval
bash oom_guard.sh 90 3 &

# Override threshold in training script via env var
OOM_THRESHOLD=90 bash verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh
```

### Integration
OOM guard is automatically started by the training script (`run_qwen3_5_35b_a3b_sft_megatron.sh`). No manual setup needed. The threshold can be overridden via `OOM_THRESHOLD` env var (default: 96).

### After OOM Guard Kills Training
1. Check `oom_guard.log` for kill timestamp and memory level
2. Check `memory_per_step.csv` for per-step memory trend
3. Run `python analyze_memory_leak.py memory_per_step.csv` for trend analysis
4. If memory leak detected: investigate and fix before restarting
5. If checkpoint save spike: reduce `checkpoint.save_contents` or increase pod memory

---

## Monitoring

### System Monitor Script
`monitor_training.sh` runs alongside training (started automatically by the training script):
- Logs every **10 seconds** to `/fsx-shared/juncheng/EHR/training_system_monitor.log`
- Tracks: cgroup memory, free memory, python3 process count/RSS, GPU memory/utilization
- Multi-level warnings at 70%, 80%, 90% of pod memory limit

### Training Log
```bash
# Check latest progress
tail -5 training_v2.log

# Check for errors
grep -E "Error|OOM|FAILED" training_v2.log

# Check memory monitor
tail -20 /fsx-shared/juncheng/EHR/training_system_monitor.log
```

---

## Pod Crash Recovery

When a pod crashes and is relaunched, follow these steps to restore the environment.

### Quick Recovery (copy-paste)
```bash
cd /fsx-shared/juncheng/EHR

# 1. Login to HuggingFace
export HF_TOKEN=$(grep HF_ACCESS_TOKEN .env | cut -d'=' -f2)
huggingface-cli login --token $HF_TOKEN

# 2. Login to wandb
export WANDB_API_KEY=$(grep WANDB_API_KEY .env | cut -d'=' -f2)
wandb login $WANDB_API_KEY

# 3. Verify model (should persist on /fsx-shared)
ls -lh /fsx-shared/juncheng/EHR/models/Qwen3.5-35B-A3B/config.json

# 4. Regenerate data if needed
if [ ! -f "data/deepmed_trajectory/train.parquet" ]; then
    python prepare_deepmed_data.py --max_token_length 57344 --model_name Qwen/Qwen3.5-35B-A3B
    mkdir -p data/deepmed_trajectory
    cp ~/data/deepmed_trajectory/*.parquet data/deepmed_trajectory/
fi

# 5. Start OOM guard (kills training at 96% to save the pod)
nohup bash oom_guard.sh 96 5 > /dev/null 2>&1 &

# 6. Launch training (saves model-only checkpoints to /fsx-shared)
nohup bash verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh > training_v3.log 2>&1 &

# 7. Start per-step memory logger (for memory leak detection)
nohup bash log_memory_per_step.sh training_v3.log memory_per_step.csv > /dev/null 2>&1 &
```

### Credentials
All API keys stored in `/fsx-shared/juncheng/EHR/.env` (persists on fsx-shared):
- `HF_ACCESS_TOKEN` - HuggingFace (user: Chtholly17)
- `WANDB_API_KEY` - Weights & Biases
- `SERPER_API_KEY` - Search API

### Key Paths
| Resource | Path |
|----------|------|
| Training script | `verl/examples/sft/deepmed/run_qwen3_5_35b_a3b_sft_megatron.sh` |
| Model weights | `/fsx-shared/juncheng/EHR/models/Qwen3.5-35B-A3B/` (~68GB) |
| Training data | `data/deepmed_trajectory/{train,val}.parquet` |
| Data prep script | `prepare_deepmed_data.py` |
| Monitor script | `monitor_training.sh` |
| Monitor log | `training_system_monitor.log` |
| Training log | `training_v3.log` |
| Checkpoints | `/fsx-shared/juncheng/EHR/verl/checkpoints/deepmed-sft/` |
| This document | `docs/debug_training_crash.md` |

### What Persists on /fsx-shared (survives pod crash)
- Model weights, training data, code, `.env` credentials, logs, this doc
- Everything under `/fsx-shared/juncheng/EHR/`

### What Does NOT Persist (needs rebuild)
- HuggingFace / wandb login sessions (stored in `/root/`)
- pip packages (if venv is on `/root/`)
- Checkpoints (if stored in `/root/verl/checkpoints/`)
