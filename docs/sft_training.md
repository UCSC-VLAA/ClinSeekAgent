# SFT Training

ClinSeek-35B-A3B is trained by supervised fine-tuning on ClinSeekAgent trajectories rendered in native tool-call format.

Paper settings:

- Base model: Qwen3.5-35B-A3B
- Teacher model: Claude Opus 4.6
- Training objective: SFT on ClinSeekAgent trajectories
- Format: native `<tool_call>` / `<tool_response>`
- Maximum sequence length: 52,000 tokens
- Training / validation size after filtering: 7,204 / 147
- Epochs: 3
- Global batch size: 32
- Micro batch size: 1 per GPU
- Learning rate: 2e-5 with cosine decay and 10 warmup steps
- Precision: bfloat16
- Backend: Megatron + mbridge
- Hardware: 8 H200 GPUs
- TP / PP / EP / ETP / CP: 2 / 1 / 8 / 1 / 1

## Prepare Data

```bash
python prepare_clinseek_data.py \
  --repo_id <hf-org-or-user>/<trajectory-dataset> \
  --filename clinseek_trajectories.jsonl \
  --model_name Qwen/Qwen3.5-35B-A3B \
  --max_token_length 52000 \
  --output_dir data/clinseek_trajectory_qwen35_52k
```

## Run Training

The release keeps a vendored VERL tree under `verl/`. `scripts/train_sft.sh`
uses it by default. Set `VERL_ROOT` only if you want to run against another
VERL checkout.

```bash
TRAIN_FILES=data/clinseek_trajectory_qwen35_52k/train.parquet \
VAL_FILES=data/clinseek_trajectory_qwen35_52k/val.parquet \
MODEL_PATH=/path/to/Qwen3.5-35B-A3B \
bash scripts/train_sft.sh
```

The same recipe is also available directly at:

```bash
bash verl/examples/sft/clinseek/run_qwen3_5_35b_a3b_sft_megatron_52k.sh
```

## Convert Checkpoint

```bash
PYTHONPATH=verl:$PYTHONPATH \
python convert_dist_ckpt_to_hf.py \
  --ckpt_dir /path/to/global_step_N \
  --output_dir /path/to/hf_output \
  --model_path /path/to/Qwen3.5-35B-A3B
```
