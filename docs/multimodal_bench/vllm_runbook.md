# Multimodal EHR Benchmark — vLLM Runbook

How to run the `prepared` multimodal test set (2,703 rows) end-to-end against a
**local vLLM server** instead of AWS Bedrock, using Qwen3.5-35B-A3B as the
agent and the same 4 image MCP tools + 2 EHR MCP servers.

Scorer still uses Bedrock (Claude Sonnet 4.6 as LLM judge) — only the agent
driver swaps to vLLM.

All paths assume `$REPO = /home/efs/zlt/autoehr`.

---

## 0. Prerequisites — already in place

Done during initial setup (don't redo unless rebuilding):

| Asset | Location | Notes |
|-------|----------|-------|
| Data (EHRXQA + MedMod ready tier) | `$REPO/data/EHR_multimodal_bench/extracted/` | 16 GB, 12k patient `.db` files + 34k CXR jpg |
| Prepared 2,703-row test set | `$REPO/data/EHR_multimodal_bench_tests/combined_test_set.jsonl` (symlink) | symlinked from `data/EHR_multimodal_bench/EHR_multimodal_bench_tests/` |
| `deploy_agent` venv | `$REPO/venvs/deploy_agent/` | agent driver + scorer (Bedrock + openai-compat client) |
| `mcp_ehr` venv | `$REPO/venvs/mcp_ehr/` | EHR MCP server (torch cu128 + sentence-transformers 5.4) |
| `mcp_image` venv | `$REPO/venvs/mcp_image/` | image MCP server (torch cu128 + transformers==4.46.3 for MAIRA-2) |
| BioLORD-2023 weights | `$REPO/models/BioLORD-2023/` | needed by EHR MCP `candidate_tools` |
| MAIRA-2 weights | `~/.cache/huggingface/hub/models--microsoft--maira-2/` | 14 GB, first-time download only |

If any of these are missing, rebuild them before running the pipeline.

---

## 0.1 Venvs — which script runs in which interpreter

There are **4 separate Python environments**. You never activate any of them
manually (except for vLLM startup) — the launcher picks the right one per
role:

| Role | Interpreter | Env name in launcher | Used by |
|------|-------------|----------------------|---------|
| vLLM server | `$REPO/venv/gemma/bin/python` (shared with other Qwen3.5 work) | — | `scripts/run/run_vllm_server_3_5.sh` (needs manual `source .../activate` before launch) |
| Agent driver + scorer | `$REPO/venvs/deploy_agent/bin/python` | `$PYBIN` | `deploy_agent_mm.py`, `scorer_mm.py` |
| EHR MCP (both EHRXQA + MedMod) | `$REPO/venvs/mcp_ehr/bin/python` | `$EHR_PYBIN` | `src/run_mcp_server.py` |
| Image MCP | `$REPO/venvs/mcp_image/bin/python` | `$IMAGE_PYBIN` | `src/mcp_image/run_image_mcp_server.py` |

`run_mm_pipeline.sh` resolves all three `PYBIN` variables from
`$REPO_ROOT` automatically — **no activation needed** if your directory layout
is standard. Override them only if you moved a venv:

```bash
PYBIN=/custom/path/deploy_agent/bin/python \
EHR_PYBIN=/custom/path/mcp_ehr/bin/python \
IMAGE_PYBIN=/custom/path/mcp_image/bin/python \
bash run_mm_pipeline.sh --mode prepared
```

Quick sanity-check before the first run:

```bash
$REPO/venvs/deploy_agent/bin/python -c "import boto3, openai, fastmcp; print('deploy_agent OK')"
$REPO/venvs/mcp_ehr/bin/python     -c "import fastmcp, sentence_transformers, torch; print('mcp_ehr OK cuda=', torch.cuda.is_available())"
$REPO/venvs/mcp_image/bin/python   -c "import fastmcp, transformers, torchxrayvision, torch; assert transformers.__version__.startswith('4.46'); print('mcp_image OK')"
```

### When to manually activate
- **vLLM**: yes. The launcher wraps `source $REPO/venv/gemma/bin/activate`
  inside the nohup command (see section 1).
- **Scoring**: call `$REPO/venvs/deploy_agent/bin/python scorer_mm.py` directly
  — no `activate` needed. `.env` only provides env vars, not a venv.
- **Pipeline**: no activation needed; `run_mm_pipeline.sh` shells out with
  absolute interpreter paths.

---

## 1. Start vLLM (multimodal mode)

vLLM is launched via `scripts/run/run_vllm_server_3_5.sh`. **Two things matter**:

1. **`LANGUAGE_MODEL_ONLY=0`** — required for image input. Default is `1` (text only).
2. **`GPU_MEMORY_UTILIZATION=0.60`** — leaves ~28 GB free per card for image
   tools on the same GPUs. Higher values (0.8+) cause MAIRA-2 to OOM because
   it needs ~22 GB peak and lives on `cuda:4` alongside vLLM.

```bash
# Kill any old vLLM
pgrep -af "vllm serve" | awk '{print $1}' | xargs -r kill -9
sleep 5

# Launch fresh
LOG=$REPO/logs/vllm_$(date -u +%Y%m%dT%H%M%SZ).log
LANGUAGE_MODEL_ONLY=0 GPU_MEMORY_UTILIZATION=0.60 \
  nohup bash -c "source $REPO/venv/gemma/bin/activate && bash $REPO/scripts/run/run_vllm_server_3_5.sh" \
  > "$LOG" 2>&1 &
echo "vLLM launched, log=$LOG"
```

Wait until `curl -sf http://127.0.0.1:4000/v1/models` returns 200 (~2 minutes).

Expected per-GPU footprint after startup: ~52 GB / 80 GB.

### Common failure
- `gpu-memory-utilization` too high → MAIRA-2 tool calls fail with `CUDA OOM`.
  Fix: drop to 0.60, relaunch.
- `--language-model-only` flag still set → image tool calls return
  `Image tools not available` even when image MCP is up.

---

## 2. Run the pipeline

The unified launcher `run_mm_pipeline.sh` starts the two EHR MCPs + image MCP,
then invokes `deploy_agent_mm.py`. It supports both `bedrock` and `vllm`
backends (the latter was added in this repo's fork).

### 2.1 Full 2,703-row run

```bash
cd $REPO/openresearcher_ehr

BACKEND=vllm \
VLLM_API_BASE_URL=http://127.0.0.1:4000/v1 \
HF_TOKEN=$(cat ~/.cache/huggingface/token) \
MAX_CONCURRENCY=6 \
MAX_ROUNDS=200 \
nohup bash run_mm_pipeline.sh --mode prepared \
  > $REPO/logs/full_run.log 2>&1 &
```

Output lands in `results/mm_prepared_<UTC>/`:
- `results.jsonl` — one row per sample, includes full message trace
- `mcp_ehrxqa.log`, `mcp_medmod.log`, `mcp_image.log` — server logs
- `run.log` — mirrored agent stdout

### 2.2 Smoke test first (recommended)

```bash
BACKEND=vllm VLLM_API_BASE_URL=http://127.0.0.1:4000/v1 \
HF_TOKEN=$(cat ~/.cache/huggingface/token) \
bash run_mm_pipeline.sh --mode prepared --limit 20
```

3 samples take ~30 s; 20 samples take ~5 min; 2,703 samples take ~2–3 h at
concurrency=6.

### 2.3 Key env overrides

| Variable | Default | Meaning |
|----------|---------|---------|
| `BACKEND` | `bedrock` | Set to `vllm` to use the local server |
| `VLLM_API_BASE_URL` | `http://127.0.0.1:4000/v1` | OpenAI-compatible endpoint |
| `VLLM_API_KEY` | `EMPTY` | API key (vLLM ignores it) |
| `VLLM_MODEL` | empty (auto-resolve) | Fixed served-model id; empty = ask `/v1/models` |
| `MAX_CONCURRENCY` | 6 | Parallel samples in flight |
| `MAX_ROUNDS` | 200 | Hard cap on tool rounds per sample |
| `ENABLE_THINKING` | 0 | Set 1 to enable Qwen3 thinking mode |
| `IMAGE_CUDA_VISIBLE_DEVICES` | `0,1,2,3,4,5,6,7` | GPUs visible to image MCP |
| `HF_TOKEN` | `~/.cache/huggingface/token` | Needed for MAIRA-2 (gated) |

### 2.4 Control flags

```bash
# Skip image MCP (classifier/grounding/segmentation disabled):
bash run_mm_pipeline.sh --mode prepared --no-image

# Reuse MCPs that are already running:
bash run_mm_pipeline.sh --no-start-mcp

# Write to a custom output dir:
bash run_mm_pipeline.sh --mode prepared --output-dir ./results/my_run

# Limit rows:
bash run_mm_pipeline.sh --mode prepared --limit 100
```

### 2.5 Resume

If the agent driver crashes midway, just re-run the **same command** against
the same `--output-dir`. Samples whose `completed: true` is already in
`results.jsonl` are skipped automatically.

---

## 3. Monitoring a live run

```bash
OUT=$REPO/openresearcher_ehr/results/mm_prepared_<UTC>   # your run dir

# Progress
wc -l $OUT/results.jsonl

# Is the driver alive?
pgrep -af deploy_agent_mm

# Latest MCP activity
tail -20 $OUT/mcp_image.log
tail -20 $OUT/mcp_ehrxqa.log
tail -20 $OUT/mcp_medmod.log

# Ports that should be listening
ss -lnt | awk '$4 ~ /(4000|5103|5104|5203)$/'

# vLLM throughput + GPU free memory
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader
```

---

## 4. Scoring (Bedrock LLM judge)

The scorer is **Bedrock-only by design** — it uses Claude Sonnet 4.6 as the
LLM judge for `len(gold)==1` rows, and rule-based set F1/precision/recall for
`len(gold)>=2` rows. Don't point it at vLLM.

Credentials live in `$REPO/.env` (mode 600, git-ignored). Source it:

```bash
set -a; source $REPO/.env; set +a
```

Then run:

```bash
RESULTS=$REPO/openresearcher_ehr/results/mm_prepared_<UTC>/results.jsonl

$REPO/venvs/deploy_agent/bin/python \
  $REPO/openresearcher_ehr/scorer_mm.py \
  --results "$RESULTS" \
  --output-root $REPO/openresearcher_ehr/results/scored \
  --region us-east-1 \
  --concurrency 10
```

Output (`results/scored/<run_dirname>/`):
- `scored.jsonl` — per-row precision/recall/F1/judge_match/etc.
- `summary.json` — aggregate stats per task + overall
- `summary.md` — human-readable summary

### Multi-region for throughput
```bash
... scorer_mm.py ... --regions us-east-1 us-west-2 --concurrency 10
```
Global concurrency = `--concurrency × len(regions)`.

---

## 5. Cheat-sheet: end-to-end from scratch

```bash
export REPO=/home/efs/zlt/autoehr
set -a; source $REPO/.env; set +a   # loads BEDROCK_API_KEY, HF_TOKEN, etc.

# (1) Launch vLLM, wait ~2 min
LANGUAGE_MODEL_ONLY=0 GPU_MEMORY_UTILIZATION=0.60 \
  nohup bash -c "source $REPO/venv/gemma/bin/activate && bash $REPO/scripts/run/run_vllm_server_3_5.sh" \
  > $REPO/logs/vllm.log 2>&1 &
while ! curl -sf http://127.0.0.1:4000/v1/models > /dev/null; do sleep 10; done
echo "vLLM ready"

# (2) Run pipeline (auto-starts the 3 MCPs)
cd $REPO/openresearcher_ehr
BACKEND=vllm VLLM_API_BASE_URL=http://127.0.0.1:4000/v1 \
HF_TOKEN=$(cat ~/.cache/huggingface/token) \
MAX_CONCURRENCY=6 MAX_ROUNDS=200 \
nohup bash run_mm_pipeline.sh --mode prepared \
  > $REPO/logs/full_run.log 2>&1 &

# (3) Monitor (in another shell)
OUT=$(ls -td $REPO/openresearcher_ehr/results/mm_prepared_* | head -1)
watch -n 30 "wc -l $OUT/results.jsonl"

# (4) Score after completion  (creds already in env from step 0)
$REPO/venvs/deploy_agent/bin/python \
  $REPO/openresearcher_ehr/scorer_mm.py \
  --results $OUT/results.jsonl \
  --output-root $REPO/openresearcher_ehr/results/scored \
  --region us-east-1 --concurrency 10

cat $REPO/openresearcher_ehr/results/scored/$(basename $OUT)/summary.md
```

---

## 6. Architecture — what runs where

```
┌──────────────────────────────┐
│   vLLM :4000 (TP=8, all GPUs)│   Qwen3.5-35B-A3B multimodal
│   gmu=0.60 → 52 GB/GPU used  │   tool-call parser: qwen3_xml
│                              │   max_model_len=1M
└───────▲──────────────────────┘
        │ HTTP /v1/chat/completions  (OpenAI-compatible)
        │
┌───────┴──────────────────────┐
│   deploy_agent_mm.py         │   venvs/deploy_agent
│   --backend vllm             │   builds Anthropic-style user content
│   (vllm_generator.py         │   (text + base64 images), generator
│    auto-converts image       │   converts to OpenAI image_url blocks.
│    blocks to OpenAI format)  │
└───┬──────────┬──────────┬────┘
    │ :5103    │ :5104    │ :5203
    │          │          │
┌───▼──┐  ┌────▼──┐  ┌────▼────────┐
│EHR MCP│  │EHR MCP│  │ Image MCP   │
│EHRXQA │  │MedMod │  │ (4 tools)   │
│ GPU 0 │  │ GPU 1 │  │ GPU 2/3/4/5 │
└───────┘  └───────┘  └─────────────┘
  venvs/mcp_ehr          venvs/mcp_image
```

Per-tool GPU pinning (image MCP):
- classifier (DenseNet121) → cuda:2
- report_generator (ViT-BERT) → cuda:3
- **grounding (MAIRA-2, 15 GB) → cuda:4** ← the heaviest
- segmentation (PSPNet) → cuda:5

All 4 image tools share a GPU with vLLM (TP=8, 52 GB each). With gmu=0.60,
there's ~28 GB free per card for image workloads — enough even for MAIRA-2
peak (~22 GB).

---

## 7. Known quirks / gotchas

1. **`--language-model-only` in vLLM**. Text-only mode; multimodal content
   blocks are ignored. Must be `0` (not set) for this pipeline.
2. **`gpu-memory-utilization=0.80` kills MAIRA-2**. Use 0.60 when image MCP
   shares GPUs with vLLM.
3. **MAIRA-2 first-run download**. 14 GB (6 shards) from HF. The launcher
   now waits up to 20 min for image MCP (`IMAGE_WAIT_TIMEOUT=1200`). After
   first download, subsequent startups take <60 s.
4. **Artifact dir is `./tmp/mm_artifacts`** (relative to CWD). Segmentation
   and phrase-grounding write PNG overlays there. Override with
   `IMAGE_ARTIFACT_DIR=/abs/path` if CWD is read-only.
5. **MedMod `report_paths` are always empty** on the ready tier. Reports
   live in `tb_cxr.report_path` in the patient DB instead.
6. **9-digit MedMod `subject_id`s** (e.g. `110021093`). Keep as string,
   don't `int()`-cast.
7. **`label=[]` in some radiology rows** (e.g. `medmod_radiology_test_52209266`)
   means "no abnormal findings" — scorer currently skips these. The
   `summary.json → skipped_empty_gold` counter shows how many.
8. **Agent may prefix tool names wrong** (e.g. `ehr.chest_xray_classifier`).
   `vllm_generator.py::_normalize_tool_name` fixes this at runtime, no action
   needed.
9. **Running 2 pipelines concurrently**: override
   `EHR_EHRXQA_PORT` / `EHR_MEDMOD_PORT` / `IMAGE_PORT` to avoid bind
   collisions.

---

## 8. What was modified in the fork

Changes from upstream required to support vLLM multimodal:

| File | Change |
|------|--------|
| `openresearcher_ehr/deploy_agent_mm.py:788` | `--backend` accepts `vllm`; added `--api_base_url`/`--api_key` |
| `openresearcher_ehr/deploy_agent_mm.py:855+` | Branch on `selected_backend` to construct `VLLMOpenAIAsyncGenerator` or `BedrockAsyncGenerator` |
| `openresearcher_ehr/vllm_generator.py` | Added `_anthropic_to_openai_blocks` + `_normalize_user_content` helpers, rewired `_prepare_messages` to preserve list-typed content for multimodal |
| `openresearcher_ehr/run_mm_pipeline.sh` | Added `BACKEND`/`VLLM_*` env vars, split `EHR_PYBIN` from `PYBIN` (EHR MCP needs sentence-transformers venv), fixed `DATA_BASE` and script paths to resolve from `$REPO_ROOT`, bumped image MCP wait to 1200 s |
| `src/mcp_image/tools/base.py:26` | Default artifact dir changed from hardcoded `/fsx-shared/...` to relative `./tmp/mm_artifacts` |
| `venvs/requirements/deploy_agent.txt` | Added `openai`, `jinja2`, `requests`, `openai-harmony`, `gpt-oss` |
| `venvs/requirements/mcp_image.txt` | Added `protobuf`, `sentencepiece` (required by MAIRA-2 tokenizer) |

Scorer (`scorer_mm.py`) is **intentionally unchanged** — it still uses
Bedrock for the LLM judge.
