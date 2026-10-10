# SFT Training: ClinSeek-35B-A3B

ClinSeek-35B-A3B is Qwen3.5-35B-A3B fine-tuned (full-parameter SFT) on
ClinSeekAgent trajectories of a strong teacher. The checkpoint is released as
[`UCSC-VLAA/ClinSeek-35B-A3B`](https://huggingface.co/UCSC-VLAA/ClinSeek-35B-A3B);
the training trajectories are not released.

## Paper recipe

| Component | Setting |
| --- | --- |
| Teacher | GPT-5.6 Sol (reasoning effort high) running ClinSeekAgent with the 16 EHR tools and the strict temporal policy |
| Prompts | the 3,000 questions of the AgentEHR-Bench training split (diagnoses, procedures, laboratory tests, microbiology tests, prescriptions, transfers) |
| Data | 2,947 trajectories that end with an answer; 2,681 after dropping those longer than 52K tokens; random split 2,628 train / 53 validation (seed 42) |
| Reasoning format | the teacher records its reasoning with `ehr.think` before each action; for training this text is moved into the student's native thinking block of the following turn |
| Loss | assistant tokens only (reasoning, tool calls, final answer) |
| Base model | Qwen3.5-35B-A3B |
| Max sequence length | 52,000 tokens |
| Epochs | 3; the checkpoint after epoch 2 (lowest validation loss) is reported |
| Global / micro batch | 32 / 1 per GPU |
| Optimizer | Adam (0.9, 0.999), fp32 states offloaded to CPU |
| Learning rate | 2e-5, cosine decay to 2e-6, 10 warmup steps; weight decay 0.1; gradient clipping 1.0 |
| MoE | auxiliary loss 0.01, z-loss 0.001 |
| Precision | bfloat16 |
| Backend | verl + Megatron + mbridge (vendored under `verl/`) |
| Hardware / parallelism | 2x H200; tensor 2, expert 2, pipeline 1, context 1; full activation recomputation |
| Seed | 42 |

## 1. Prepare data

Input: ClinSeekAgent `results.jsonl` files of the teacher (one trajectory per
row, OpenAI-style `messages` with `tool_calls`). Output: `train.parquet` /
`val.parquet` with native tool-call messages for the Qwen3.5 chat template.

```bash
python verl/examples/sft/clinseek/prepare_clinseek_data.py \
  --local_path /path/to/teacher/results.jsonl \
  --success_only --merge_think --strip_think_protocol \
  --model_name /path/to/Qwen3.5-35B-A3B \
  --max_token_length 52000 \
  --output_dir data/clinseek_sft_52k
```

- `--success_only` keeps trajectories that end with `ehr.finish`.
- `--merge_think` removes every assistant turn that only calls `ehr.think`
  (together with its tool response) and puts its text into the
  `reasoning_content` of the next assistant turn; consecutive think turns are
  joined. A think call inside a turn with other calls is removed from that
  turn the same way. The Qwen3.5 template renders `reasoning_content` as the
  `<think>...</think>` block. `--keep_provider_reasoning` would also keep the
  teacher API's own reasoning text (dropped by default).
- `--strip_think_protocol` removes the system-prompt section that instructs
  the teacher to call `ehr.think` before every action, so the training prompt
  matches deployment.
- Samples longer than `--max_token_length` after rendering are dropped; the
  script prints the length distribution. `--val_ratio 0.02 --seed 42` (default)
  gives the 53-row validation split. `--split_by_qid` keeps all trajectories
  of a question in one split when several rollouts per question are present.
- `--repo_id` / `--filename` read the JSONL from a Hugging Face dataset instead.

## 2. Prepare the vendored verl tree (once)

The recipe fits 52K-token sequences on two GPUs with a few memory-saving
changes to verl's Megatron engine. They ship as a patch next to the recipe
and are switched on by environment variables that `scripts/train_sft.sh`
sets (keep bf16 logits, chunked vocab-parallel cross entropy, empty the CUDA
cache per micro-batch, CUDA memory fraction; plus dtype-string optimizer
overrides and resume fixes):

```bash
git apply verl/examples/sft/clinseek/patches/verl_megatron_long_context.patch
```

The Megatron engine also imports `verl/verl/models/mcore/`, which the vendored
tree does not include; restore `verl/verl/models/` from upstream verl
(`volcengine/verl`, commit `f056425559af69d52d46b256d554d1ec330b53d5`) before
training. Qwen3.5 needs a recent `transformers` (the paper run used 5.4.0);
see `venvs/requirements/sft_training.txt`.

## 3. Train

```bash
TRAIN_FILES=data/clinseek_sft_52k/train.parquet \
VAL_FILES=data/clinseek_sft_52k/val.parquet \
MODEL_PATH=/path/to/Qwen3.5-35B-A3B \
EXP_NAME=clinseek-35b-a3b-sft \
bash scripts/train_sft.sh
```

Defaults reproduce the table above (`NUM_GPUS=2`, `TP_SIZE=2`, `EP_SIZE=2`,
`TRAIN_BATCH_SIZE=32`, `MAX_LENGTH=52000`, `TOTAL_EPOCHS=3`,
`RECOMPUTE_NUM_LAYERS=1`). A checkpoint is saved after every epoch and the
validation loss is computed every 25 steps on 50 validation samples. Host RAM:
the CPU-resident Adam states of a 35B model need roughly 1 TB for the two
ranks. Extra hydra overrides pass through, e.g. `trainer.resume_mode=auto`.
`verl/examples/sft/clinseek/run_qwen3_5_35b_a3b_sft_megatron_52k.sh` is the
same recipe.

## 4. Convert and serve

```bash
PYTHONPATH=verl:$PYTHONPATH \
python verl/examples/sft/clinseek/convert_dist_ckpt_to_hf.py \
  --ckpt_dir checkpoints/clinseek-sft/clinseek-35b-a3b-sft/global_step_N \
  --output_dir /path/to/ClinSeek-35B-A3B \
  --model_path /path/to/Qwen3.5-35B-A3B

MODEL_PATH=/path/to/ClinSeek-35B-A3B SERVED_MODEL_NAME=ClinSeek-35B-A3B \
TENSOR_PARALLEL_SIZE=4 bash scripts/run_vllm_server.sh
```

Evaluate on AgentEHR-Bench as in [`agentehr_bench.md`](agentehr_bench.md).
