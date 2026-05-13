# Multimodal EHR Benchmark — Evaluation Runbook

A complete, self-contained guide to running the multimodal EHR benchmark
evaluation pipeline on your own machine.

**Scope.** This doc targets the pipeline as it ships today:
`openresearcher_ehr/deploy_agent_mm.py` + `src/mcp_image/` +
`helper/scorer_mm.py`, driven by AWS **Bedrock** (Anthropic Messages API). A
vLLM-only porting appendix (§9) lists the code touchpoints required to swap
Bedrock out for a local vLLM server.

**Conventions.** Throughout this doc `$REPO = /fsx-shared/juncheng/EHR`.
Replace it with your checkout path. All paths shown under `$REPO/...` are
relative to that root.

Paths and counts were verified against the running tree on 2026-04-22.

---

## 0. Overall pipeline structure

The benchmark has four stages. Each box below is a separate process.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      (0)  DATA PREPARATION                              │
│  HF dataset  Chtholly17/EHR_multimodal_bench                            │
│     └─ EHRXQAAgentBench_v3.zip         (5.7 GB)                         │
│     └─ MedMod/*.zip                    (~10 GB, 22 archives)            │
│  → unzip into $REPO/data/EHR_multimodal_bench/extracted/                │
│  → prepare_mm_data.py filters/samples → data_mm/*.jsonl                 │
└─────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (1)  MCP SERVERS                                   │
│                                                                         │
│   :5103   EHR MCP (EHRXQA root)    src/run_mcp_server.py                │
│   :5104   EHR MCP (MedMod root)    src/run_mcp_server.py                │
│   :5203   Image MCP (6 CXR tools)  src/mcp_image/run_image_mcp_server.py│
│                                                                         │
│   run in their own venvs (mcp_ehr, mcp_image)                           │
└─────────────────────────────────────────────────────────────────────────┘
                               │  HTTP
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (2)  AGENT DRIVER                                  │
│   deploy_agent_mm.py   ── Anthropic Messages on Bedrock (Opus 4.6 def.) │
│     ├─ builds user content: <run_context> + question + base64 images   │
│     │                       + inlined reports                           │
│     ├─ tool schemas: 20 ehr.* + 3 browser.* + 6 image.*  = 29 tools     │
│     └─ routes tool calls:                                               │
│         ehr.*  → port by source_benchmark (ehrxqa → :5103, medmod →5104)│
│         image.*→ :5203                                                  │
│         browser.* → local or Serper                                     │
│   run in venv bedrock_agent                                              │
└─────────────────────────────────────────────────────────────────────────┘
                               │ one JSON per sample
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (3)  SCORER                                        │
│   helper/scorer_mm.py                                                   │
│     ├─ extracts prediction from the last ehr.finish tool call           │
│     ├─ if len(gold)==1: LLM-judge (Claude Sonnet 4.6 on Bedrock)        │
│     │       sub-routed by yesno / count / date_time / id / label_name / │
│     │       generic_string templates                                    │
│     ├─ if len(gold)>=2: rule-based set F1 / precision / recall /        │
│     │                    subset_accuracy, computed raw + vocab-mapped   │
│     │                    (MiniLM cosine to nearest gold vocab entry)    │
│     └─ unified per-task F1 folds both routes                            │
│   → scored.jsonl, summary.json, summary.md                              │
└─────────────────────────────────────────────────────────────────────────┘
```

The two "independent benchmarks" inside the HF repo:

| Benchmark | Source QA | Modalities | Task names |
|-----------|-----------|------------|------------|
| **EHRXQAAgentBench_v3** | EHRXQA 1.0 | CXR + EHR | `ehrxqa_table`, `ehrxqa_image`, `ehrxqa_image_table` |
| **MedModAgentBench_v3** | MedMod clinical outcome tasks | CXR + ICU EHR | `medmod_decompensation`, `medmod_length_of_stay`, `medmod_phenotyping`, `medmod_in_hospital_mortality`, `medmod_radiology` |

See §1 for schema / counts.

---

## 1. Dataset: download & preprocess

### 1.1 Download

The benchmark ships on HuggingFace as
[`Chtholly17/EHR_multimodal_bench`](https://huggingface.co/datasets/Chtholly17/EHR_multimodal_bench).
The zip archives land in `$REPO/data/EHR_multimodal_bench/` as-is.

```bash
hf download Chtholly17/EHR_multimodal_bench \
    --repo-type dataset \
    --local-dir $REPO/data/EHR_multimodal_bench
```

Or, with the older `huggingface-cli`:

```bash
pip install "huggingface-hub>=0.35"
huggingface-cli download Chtholly17/EHR_multimodal_bench \
    --repo-type dataset \
    --local-dir $REPO/data/EHR_multimodal_bench
```

After download the layout is:

```
$REPO/data/EHR_multimodal_bench/
├── EHRXQAAgentBench_v3.zip                                    (5.7 GB)
└── MedMod/
    ├── MedModAgentBench_v3_database_patients_10m.zip ... _19m.zip   (10 shards)
    ├── MedModAgentBench_v3_database_seed.zip
    ├── MedModAgentBench_v3_full_manifests_*.zip               (6 task-level)
    ├── MedModAgentBench_v3_ready_manifests.zip                (DEFAULT entry)
    ├── MedModAgentBench_v3_ready_databases.zip
    ├── MedModAgentBench_v3_ready_images.zip
    ├── MedModAgentBench_v3_test_only_portable.zip             (smoke)
    ├── MedModAgentBench_v3_mimic_cxr_files.zip                (108 k JPG mirror)
    └── README.md / checksums.json / release_manifest.json
```

### 1.2 Extract

For a **normal evaluation run you only need the "ready tier"** of each
benchmark (~10 GB total). Unzip into
`$REPO/data/EHR_multimodal_bench/extracted/`:

```bash
cd $REPO/data/EHR_multimodal_bench
mkdir -p extracted

# EHRXQA — single archive
unzip -q EHRXQAAgentBench_v3.zip -d extracted/

# MedMod — ready tier
for f in MedMod/MedModAgentBench_v3_ready_manifests.zip \
         MedMod/MedModAgentBench_v3_ready_databases.zip \
         MedMod/MedModAgentBench_v3_ready_images.zip; do
    unzip -q "$f" -d extracted/
done
```

Final structure (ready tier only, ~10 GB):

```
extracted/
├── EHRXQAAgentBench_v3/
│   ├── common/{ready,full,ready_subset_500,subset_500}/
│   ├── database/                       (2 924 patient DBs + candidate_table.db)
│   ├── mimic-cxr/2.0.0/{files,mimic-cxr-reports}/
│   ├── source_release/ehrxqa_1.0.0/
│   ├── table_description/, asset_manifest/, README.md, metadata.json
└── MedModAgentBench_v3/
    ├── common/ready/                   (5 task JSONs + merged + 3 splits)
    ├── database/                       (9 075 patient DBs + candidate + reference)
    ├── mimic-cxr/2.0.0/files/          (34 899 JPGs)
    ├── table_description/, asset_manifest/, README.md, metadata.json
```

Optional — full MedMod tier (+~40 GB): also unzip the 10
`MedModAgentBench_v3_database_patients_*m.zip` shards and the 6
`MedModAgentBench_v3_full_manifests_*.zip` files.

### 1.3 Prepared 2,703-row combined test set

A ready-to-use mixed test set lives directly in the same HF dataset, under
the `EHR_multimodal_bench_tests/` subfolder:

| HF path | Local destination | Size |
|---------|-------------------|------|
| `EHR_multimodal_bench_tests/combined_test_set.jsonl` | `$REPO/data/EHR_multimodal_bench_tests/combined_test_set.jsonl` | 4.1 MB |
| `EHR_multimodal_bench_tests/combined_test_set.stats.json` | `$REPO/data/EHR_multimodal_bench_tests/combined_test_set.stats.json` | <1 KB |

Download:

```bash
hf download Chtholly17/EHR_multimodal_bench \
    --repo-type dataset \
    --include "EHR_multimodal_bench_tests/*" \
    --local-dir $REPO/data/EHR_multimodal_bench
# After: $REPO/data/EHR_multimodal_bench/EHR_multimodal_bench_tests/combined_test_set.jsonl
```

Or move/symlink it under the path `run_mm_pipeline.sh` expects:

```bash
mv $REPO/data/EHR_multimodal_bench/EHR_multimodal_bench_tests \
   $REPO/data/EHR_multimodal_bench_tests
```

Composition (from `combined_test_set.stats.json`):

| Source | Task | Rows |
|--------|------|-----:|
| MedMod (125 each, seed 42) | `medmod_radiology` | 125 |
| | `medmod_decompensation` | 125 |
| | `medmod_phenotyping` | 125 |
| | `medmod_in_hospital_mortality` | 125 |
| EHRXQA (all test rows, `ehrxqa_image_table` dropped) | `ehrxqa_table` | 1,706 |
| | `ehrxqa_image` | 497 |
| **Total** | | **2,703** |

Notes:
- 626 EHRXQA `ehrxqa_table` rows are `cohort_scope` (placeholder
  `subject_id` in the `99100000x` range). The pipeline will not try to load
  a patient DB for those; `_mm_has_ehr_subject=false` on those rows.
- 997 rows have a linked CXR image (`_mm_has_image=true`).
- `medmod_length_of_stay` is intentionally **not** sampled into this set.

`run_mm_pipeline.sh` defaults to this file when invoked with
`--mode prepared`.

### 1.4 Preprocess raw manifests (`prepare_mm_data.py`)

`openresearcher_ehr/prepare_mm_data.py` filters a raw manifest JSON (list
of records) into a per-task JSONL suitable for feeding to
`deploy_agent_mm.py`.

**Parameters:**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--src` | str | required | Source manifest JSON (e.g. `.../common/ready/test.json`). Must be a list. |
| `--tasks` | str | required | Comma-separated task names to keep. e.g. `ehrxqa_image,ehrxqa_image_table` or `medmod_radiology`. |
| `--scope` | str | `patient_scope` | One of `patient_scope`, `cohort_scope`, `any`. Cohort rows have `subject_id=null` and cannot call `ehr.load_ehr`. |
| `--require_images` | flag | off | Keep only rows with non-empty `image_paths`. |
| `--require_subject_id` | flag | off | Keep only rows where `subject_id` is set. |
| `--sample_size` | int | 0 | If > 0, shuffle and keep this many rows. |
| `--seed` | int | 42 | RNG seed for `--sample_size`. |
| `--out` | str | required | Output path (`.jsonl` or `.json`). |
| `--out_format` | str | `jsonl` | `jsonl` or `json`. |

**Examples.**

```bash
PY=$REPO/venvs/bedrock_agent/bin/python

# 1) EHRXQA image-only test set, keep everything usable by the image path
$PY $REPO/openresearcher_ehr/prepare_mm_data.py \
    --src $REPO/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3/common/ready/test.json \
    --tasks ehrxqa_image,ehrxqa_image_table \
    --scope patient_scope \
    --require_images --require_subject_id \
    --out $REPO/openresearcher_ehr/data_mm/ehrxqa_image_test.jsonl

# 2) MedMod radiology smoke set (125 rows, seed 42)
$PY $REPO/openresearcher_ehr/prepare_mm_data.py \
    --src $REPO/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3/common/ready/test.json \
    --tasks medmod_radiology \
    --scope patient_scope \
    --require_images --require_subject_id \
    --sample_size 125 --seed 42 \
    --out $REPO/openresearcher_ehr/data_mm/medmod_radiology_125.jsonl

# 3) One file per MedMod task, full
for task in medmod_radiology medmod_in_hospital_mortality \
            medmod_phenotyping medmod_decompensation medmod_length_of_stay; do
  $PY $REPO/openresearcher_ehr/prepare_mm_data.py \
      --src $REPO/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3/common/ready/test.json \
      --tasks "$task" --scope patient_scope \
      --require_images --require_subject_id \
      --out "$REPO/openresearcher_ehr/data_mm/full_${task}.jsonl"
done
```

A pre-built mixed **2,703-row test set** ships under
`EHR_multimodal_bench_tests/` on the HF repo (see §1.3). It's the default
input for `run_mm_pipeline.sh --mode prepared`.

### 1.5 Manifest and DB schema primer

Each ready row carries:

```json
{
  "qid": "medmod_radiology_test_50376803",
  "task": "medmod_radiology",
  "scope": "patient_scope",
  "subject_id": 10001884,
  "prediction_time": "2131-01-15 04:45:09",
  "question": "<task_instruction>...<cxr_info>...<question>...",
  "label": [{"name": "Cardiomegaly"}, {"name": "Support Devices"}],
  "modalities": ["cxr_table", "cxr_image"],
  "db_path_hint": "database/patient_10001884.db",
  "study_ids": [50376803],
  "image_paths": ["mimic-cxr/2.0.0/files/p10/p10001884/s50376803/....jpg"],
  "report_paths": [],
  "snapshot_spec": { "cutoff_time": "...", "time_policy": "inclusive_leq_cutoff",
                     "cxr_policy": "linked_studies_only", ... },
  "source_benchmark": "medmod"
}
```

Per-patient DB schema differs by benchmark:

| Benchmark | Tables in `patient_<id>.db` |
|-----------|-----------------------------|
| EHRXQAAgentBench_v3 | `patients`, `admissions`, `chartevents`, `cost`, `diagnoses_icd`, `icustays`, `inputevents`, `labevents`, `microbiologyevents`, `outputevents`, `prescriptions`, `procedures_icd`, `transfers`, **`tb_cxr`** |
| MedModAgentBench_v3 | **`events`**, **`stays`**, **`tb_cxr`** only |

MedMod's `events` is an itemid-keyed long-table; `d_items` / `d_labitems`
live in the shared `database/reference_table.db`.

Shared candidate tables (used by `ehr.get_candidates_by_*`): 7 per root,
including `diagnoses_ccs_candidates` (87 103 rows), `prescriptions_atc_candidates`
(156 201), `labevents_candidates` (1 619), `radiology_candidates` (963).

### 1.6 Split sizes (reference)

```
EHRXQA ready: train 16 789 / valid 2 420 / test 2 220 / merged 21 429
              unique subjects 2 509; CXR-linked 5 000

MedMod ready: train 1 499 855 / valid 152 113 / test 392 811
              unique subjects 9 073
```

---

## 2. Virtual environments

Three venvs, split by purpose. Requirements files live in
`$REPO/venvs/requirements/`:

| Venv | Requirements file | Purpose |
|------|-------------------|---------|
| `bedrock_agent` | `bedrock_agent.txt` | agent driver + scorer (CPU-only, talks to MCPs via HTTP) |
| `mcp_ehr` | `mcp_ehr.txt` | EHR MCP server; needs GPU for BioLORD-2023 |
| `mcp_image` | `mcp_image.txt` | Image MCP server; needs GPU(s); **pins `transformers==4.46.3`** |

### 2.1 Create the three venvs

```bash
cd $REPO/venvs
mkdir -p requirements

# ---- A) bedrock_agent ------------------------------------------------------
uv venv bedrock_agent --python 3.12
uv pip install --python bedrock_agent/bin/python \
    -r requirements/bedrock_agent.txt

# ---- B) EHR MCP -----------------------------------------------------------
uv venv mcp_ehr --python 3.12
# Install torch first against your CUDA. Example (CUDA 12.1):
uv pip install --python mcp_ehr/bin/python \
    torch==2.8.0 torchvision \
    --index-url https://download.pytorch.org/whl/cu121
uv pip install --python mcp_ehr/bin/python \
    -r requirements/mcp_ehr.txt

# ---- C) Image MCP (must pin transformers==4.46.3) --------------------------
uv venv mcp_image --python 3.12
# Torch must be the CUDA build — the MAIRA-2 path was tested on cu128:
uv pip install --python mcp_image/bin/python --reinstall \
    torch==2.9.0+cu128 torchvision==0.24.0+cu128 \
    --index-url https://download.pytorch.org/whl/cu128
uv pip install --python mcp_image/bin/python \
    -r requirements/mcp_image.txt
```

If you don't use `uv`, swap `uv venv` / `uv pip install` for
`python3.12 -m venv` / `<venv>/bin/pip install`.

### 2.2 Verify each venv

```bash
# bedrock_agent
$REPO/venvs/bedrock_agent/bin/python -c "import boto3, httpx, aiohttp, PIL, dotenv; print('bedrock_agent OK')"

# EHR MCP
$REPO/venvs/mcp_ehr/bin/python -c "import fastmcp, pandas, sentence_transformers, thefuzz, torch; print('mcp_ehr OK, cuda=', torch.cuda.is_available())"

# Image MCP
$REPO/venvs/mcp_image/bin/python -c "import fastmcp, torchxrayvision, transformers, pydicom, torch; print('mcp_image OK, transformers', transformers.__version__)"
# Must print transformers 4.46.3
```

### 2.3 Model weights & credentials

Needed at runtime (first call to each tool lazy-downloads weights into
`$HF_HOME`):

| Resource | Where it's used | Where to put it |
|----------|-----------------|-----------------|
| **BioLORD-2023** (embedding, ~500 MB) | EHR MCP semantic search (`candidate_tools.py`) | `$REPO/models/BioLORD-2023` or pulled on first use |
| **microsoft/maira-2** (~4 GB, **gated**) | `image.xray_phrase_grounding` | HF cache; requires accepted license + `HF_TOKEN` |
| **IAMJB/chexpert-mimic-cxr-findings** (~1 GB) | `image.chest_xray_report_generator` | HF cache |
| **torchxrayvision DenseNet121 + PSPNet** (~500 MB) | `image.chest_xray_classifier`, `image.chest_xray_segmentation` | torchxrayvision auto-downloads |
| **AWS Bedrock creds** | `deploy_agent_mm.py`, `helper/scorer_mm.py` | env vars (below) |

HuggingFace token (must have accepted the MAIRA-2 license at
<https://huggingface.co/microsoft/maira-2>):

```bash
huggingface-cli login            # writes ~/.cache/huggingface/token
# or:  export HF_TOKEN=hf_xxx
```

AWS Bedrock — one of:

```bash
# Option 1: bearer token
export BEDROCK_API_KEY=...
export AWS_BEARER_TOKEN_BEDROCK=$BEDROCK_API_KEY

# Option 2: standard AWS chain
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...                    # if using STS
export AWS_DEFAULT_REGION=us-east-1             # Opus 4.6 / 3rd-party models
# Claude cross-region profiles also work from ca-west-1; the scorer remaps
# the `us.`/`eu.`/`global.` prefix for you.
```

Sanity-check Bedrock auth:

```bash
aws sts get-caller-identity
$REPO/venvs/bedrock_agent/bin/python -c "
import boto3, json
c = boto3.client('bedrock-runtime', region_name='us-east-1')
resp = c.invoke_model(
    modelId='us.anthropic.claude-opus-4-6-v1',
    body=json.dumps({
        'anthropic_version':'bedrock-2023-05-31',
        'max_tokens':16,
        'messages':[{'role':'user','content':[{'type':'text','text':'hi'}]}]}))
print(json.loads(resp['body'].read())['content'][0]['text'])"
```

Browser tools are on but inert unless `SERPER_API_KEY` is exported; EHR +
image paths work fine without it.

---

## 3. Launching the MCP servers

Three servers — two EHR MCPs (one per benchmark root) and one image MCP.
The unified launcher `run_mm_pipeline.sh` will start them for you, but the
explicit commands are below for debugging and for reusing a running MCP
across runs.

### 3.1 EHR MCP — EHRXQA on :5103

```bash
cd $REPO
CUDA_VISIBLE_DEVICES=0 \
$REPO/venvs/mcp_ehr/bin/python src/run_mcp_server.py \
    --mode http --host 127.0.0.1 --port 5103 \
    --data_path $REPO/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3 \
    --disable-knowledge-tools \
    > $REPO/logs/mcp_ehrxqa.log 2>&1 &
```

### 3.2 EHR MCP — MedMod on :5104

```bash
CUDA_VISIBLE_DEVICES=1 \
$REPO/venvs/mcp_ehr/bin/python src/run_mcp_server.py \
    --mode http --host 127.0.0.1 --port 5104 \
    --data_path $REPO/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3 \
    --disable-knowledge-tools \
    > $REPO/logs/mcp_medmod.log 2>&1 &
```

BioLORD-2023 loads at startup (~20 s). Wait for `Uvicorn running on
http://127.0.0.1:<port>` before exercising tools.

### 3.3 Image MCP on :5203

Six image tools registered. Per-tool GPU pinning keeps the ~7 GB of model
weights spread across 4 GPUs:

```bash
BENCH_ROOT="$REPO/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3:$REPO/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3" \
HF_TOKEN=$(cat ~/.cache/huggingface/token) \
HF_HUB_ENABLE_HF_TRANSFER=1 \
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5 \
IMAGE_TOOL_DEVICE_CLASSIFIER=cuda:2 \
IMAGE_TOOL_DEVICE_REPORT_GENERATOR=cuda:3 \
IMAGE_TOOL_DEVICE_GROUNDING=cuda:4 \
IMAGE_TOOL_DEVICE_SEGMENTATION=cuda:5 \
$REPO/venvs/mcp_image/bin/python $REPO/src/mcp_image/run_image_mcp_server.py \
    --mode http --host 127.0.0.1 --port 5203 \
    > $REPO/logs/mcp_image.log 2>&1 &
```

Tools it exposes:

| Tool | Status | Backing model |
|------|--------|----------------|
| `image.image_visualizer` | CPU | none (annotated copy) |
| `image.dicom_processor` | CPU | pydicom |
| `image.chest_xray_classifier` | GPU | torchxrayvision DenseNet121 |
| `image.chest_xray_report_generator` | GPU | IAMJB chexpert-mimic-cxr-findings + ViT-BERT |
| `image.xray_phrase_grounding` | GPU | microsoft/maira-2 (gated) |
| `image.chest_xray_segmentation` | GPU | torchxrayvision PSPNet (14 regions) |

If you pass `--no-image` to the launcher (§4), image tools are **not
registered** in the request schema — the model can't call them, but it
still sees the CXR via the base64 content block that
`deploy_agent_mm.py` attaches to the user message.

### 3.4 Health-check MCPs

```bash
for port in 5103 5104 5203; do
    python - <<PY
import socket; s=socket.socket(); s.settimeout(1)
try: s.connect(("127.0.0.1", $port)); print("$port UP")
except Exception as e: print("$port DOWN:", e)
PY
done
```

---

## 4. Running the pipeline

### 4.1 Launcher: `run_mm_pipeline.sh`

The primary entrypoint. It (a) starts the two EHR MCPs and (optionally)
the image MCP, (b) invokes `deploy_agent_mm.py` with both bench roots, (c)
tees logs into the output directory, (d) cleans up MCP PIDs on exit.

```bash
cd $REPO/openresearcher_ehr

# (A) prepared 2,703-row test set — default
bash run_mm_pipeline.sh

# (B) full EHRXQA + MedMod test sets back-to-back
bash run_mm_pipeline.sh --mode full

# (C) one bench at a time
bash run_mm_pipeline.sh --mode full-ehrxqa
bash run_mm_pipeline.sh --mode full-medmod

# (D) smoke N rows
bash run_mm_pipeline.sh --mode prepared --limit 5

# (E) reuse already-running MCPs
bash run_mm_pipeline.sh --no-start-mcp

# (F) disable image path entirely (tools de-registered)
bash run_mm_pipeline.sh --no-image
```

CLI flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--mode` | `prepared` | `prepared` \| `full` \| `full-ehrxqa` \| `full-medmod` |
| `--limit N` | 0 (no limit) | Truncate input to first N rows |
| `--no-start-mcp` | off | Don't spawn EHR MCPs |
| `--no-image` | off | Don't register image tools (implies `--no-start-image`) |
| `--no-start-image` | off | Don't spawn image MCP |
| `--output-dir DIR` | `./results/mm_<mode>_<utc>` | Where `results.jsonl` lands |
| `--data-path PATH` | auto per `--mode` | Custom input JSONL |

Environment overrides (all optional; values shown are defaults):

```bash
BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1
BEDROCK_REGION=us-east-1
BEDROCK_API_KEY=...              # or AWS_* chain
SERPER_API_KEY=...               # enables browser.search

MAX_ROUNDS=200
MAX_CONCURRENCY=6
RUNS_PER_QUESTION=1
MAX_TOOL_RESULT_CHARS=100000
IMAGE_MAX_EDGE=1568              # Anthropic-recommended max (longest edge)
ENABLE_THINKING=0                # 1 = Opus extended thinking

PYBIN=$REPO/venvs/bedrock_agent/bin/python
IMAGE_PYBIN=$REPO/venvs/mcp_image/bin/python
IMAGE_CUDA_VISIBLE_DEVICES=0,1,2,3,4,5
HF_TOKEN=...                     # default: ~/.cache/huggingface/token

EHR_EHRXQA_PORT=5103
EHR_MEDMOD_PORT=5104
IMAGE_PORT=5203
```

> **Important** — the upstream script has `PYBIN` defaulting to
> `/fsx-shared/juncheng/OpenResearcher/.venv/bin/python`. On your host
> override it to point at `$REPO/venvs/bedrock_agent/bin/python`, or edit
> `run_mm_pipeline.sh`.

### 4.2 All `deploy_agent_mm.py` flags

If you call the driver directly, these are the complete flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--data_path` | required | JSONL (or JSON list) of samples |
| `--output_dir` | `./results` | Writes `results.jsonl` + `run.log` here |
| `--backend` | `bedrock` | **Only `bedrock` is supported today** (§9) |
| `--bedrock_model_id` | `us.anthropic.claude-opus-4-6-v1` | Model for the agent |
| `--bedrock_region` | `us-east-1` | Bedrock region |
| `--bedrock_api_key` | `None` | Override; else standard AWS chain |
| `--enable_ehr` | off | Register `ehr.*` tools |
| `--ehr_mcp_url` | `http://127.0.0.1:5003/mcp` | Default EHR MCP; used when per-source overrides aren't set |
| `--ehr_mcp_url_ehrxqa` | None | Per-source override for EHRXQA rows |
| `--ehr_mcp_url_medmod` | None | Per-source override for MedMod rows |
| `--enable_image` | off | Register `image.*` tools |
| `--image_mcp_url` | `http://127.0.0.1:5203/mcp` | Image MCP URL |
| `--bench_root` | None | One or more bench roots (colon-separated) for relative path resolution |
| `--image_max_edge` | 1568 | Downscale longest edge before base64 encoding |
| `--max_rounds` | 200 | Hard cap on tool rounds per sample |
| `--max_concurrency` | 6 | Parallel samples in flight |
| `--runs_per_question` | 1 | Rollouts per question |
| `--max_tool_result_chars` | 100000 | Per-tool truncation |
| `--temperature` | 1.0 | Sampling temperature |
| `--enable_thinking` / `--disable_thinking` | off | Opus extended thinking |
| `--search_url` | `http://localhost:8001` | Browser backend when local |
| `--browser_backend` | `local` | `local` or `serper` |
| `--verbose` | off | Verbose agent logging |

### 4.3 What the agent actually does per sample

1. Reads `question`, `image_paths`, `report_paths`, `subject_id`, `scope`,
   `source_benchmark` from the JSONL row.
2. Resolves each `image_paths[*]` against each bench root in `--bench_root`
   (colon-separated). MedMod rows resolve under MedMod; EHRXQA under EHRXQA.
3. Builds an Anthropic-style `content` list:
   - a `<run_context>` preamble (images_attached, reports_inlined,
     patient_ehr_available),
   - the pre-rendered `question`,
   - up to 4 images (resized to `image_max_edge`) as base64 blocks,
   - any linked reports as extra text blocks.
4. Calls Bedrock with `tools=COMBINED_TOOL_CONTENT_MM` (20 ehr + 3 browser
   + 6 image = 29 tools) and `tool_choice="auto"`.
5. Routes tool calls: `ehr.*` by `source_benchmark` to port 5103 or 5104;
   `image.*` to :5203; `browser.*` to local or Serper.
6. Loops until `ehr.finish` or `max_rounds=200`.
7. Writes one JSON line per sample to `results.jsonl`.

Resume is automatic: on re-run, samples whose `completed: true` is already
in `results.jsonl` are skipped.

### 4.4 Output directory layout

```
results/mm_<mode>_<UTC>/
├── results.jsonl         # one JSON per sample: messages (full trace),
│                           multimodal_summary, attached manifest fields
├── run.log               # mirrored stdout/stderr of the agent
├── mcp_ehrxqa.log
├── mcp_medmod.log
└── mcp_image.log         # (only when image MCP was started)
```

### 4.5 Cost / time estimate

Smoke-6 observations: ~10–40 rounds per sample, 1–4 images per Anthropic
call, Opus 4.6 default.
- Prepared set (2,703 rows, concurrency=6): **hours** — run under `tmux` /
  `nohup`.
- MedMod full test (392,811 rows): shard by task with `prepare_mm_data.py`
  and run per-task jobs in parallel on different hosts.

---

## 5. Errors & gotchas worth knowing

These are issues we've hit while bringing the pipeline up on a new host.
None are open bugs — they are environment pitfalls.

### 5.1 From the multimodal dataset integration

1. **Cohort-scope rows have `subject_id: null`.** A tool call to
   `ehr.load_ehr` will fail. Filter with
   `prepare_mm_data.py --scope patient_scope --require_subject_id` or
   accept that Claude may try and back off.
2. **MedMod `report_paths` are always empty** on the ready tier (reports
   are embedded in `tb_cxr.report_path` against the DB, or in the raw
   MIMIC-CXR report tree shipped only for EHRXQA).
3. **MedMod has 9-digit `subject_id`s** (e.g. `110021093`). Don't
   `int()`-coerce them anywhere — string-typed subject_ids should stay
   strings.
4. **MedMod schema is 3-tables-only** (`events`, `stays`, `tb_cxr`).
   `ehr.get_records_by_time` on `labevents`, `prescriptions`, etc. will
   return empty — rely on `ehr.run_sql_query` against `events` and the
   shared `reference_table.db`.
5. **`snapshot_spec.time_policy = inclusive_leq_cutoff`** but the existing
   `EHRManager.load_ehr_for_sample` uses strict `<`. For strict parity, fix
   the comparison operator in `src/agentlite/commons/EHRManager.py`.
6. **Empty `table_description`** on the ready tier for both benchmarks
   (EHRXQA) or auto-stubs (MedMod). Prompts that rely on hand-written
   schema docs will be under-primed — either write your own or rely on
   `ehr.run_sql_query` with schema introspection.
7. **No `full` tier extracted by default** (~40 GB). If you need full
   MedMod, unzip the 10 database shards and 6 full manifests yourself.

### 5.2 From running the multimodal pipeline

1. **`NameError: use_reasoning_content`** — a pre-existing latent bug in
   `bedrock_generator._chat_completion_anthropic`. `deploy_agent_mm.py` and
   `helper/scorer_mm.py` both patch it with a runtime shim; if you ever call
   `bedrock_generator` from new code, make sure you set
   `bedrock_generator.__dict__.setdefault("use_reasoning_content", True)`
   first.
2. **`ConnectionError: http://127.0.0.1:5103/mcp`** — EHR MCP not up. Check
   `ss -lnt | grep -E '5103|5104'` and read `mcp_ehrxqa.log` /
   `mcp_medmod.log`. BioLORD load takes ~20 s; don't fire samples before
   it's ready.
3. **`Error: Image tools not available`** — image MCP off or unreachable.
   `--no-image` suppresses the tools entirely; otherwise check
   `mcp_image.log` for GPU / HF download failures.
4. **All samples end with `stop_reason="no_final_answer"`** — usually
   `max_rounds` too small or thinking mode misaligned. Default is 200.
5. **No image block in `messages[0]`** — that row had no `image_paths`, or
   none of them resolved against any `--bench_root`. The
   `multimodal_summary.dropped_image_paths` field in the result records
   what was dropped.
6. **MAIRA-2 crash with "modeling code not found"** — you are on
   `transformers >= 4.50`. The image venv **must** pin to
   `transformers==4.46.3`. Re-run `pip install
   transformers==4.46.3 --force-reinstall`.
7. **Bedrock `ValidationException` / 403 on `moonshotai.*` or
   `openai.*` models** — third-party Bedrock models are in `us-east-1`;
   Anthropic Claude is available in `us-east-1` and cross-region profiles
   (`ca-west-1`). Pick `BEDROCK_REGION` accordingly.
8. **`HF_TOKEN` missing** — MAIRA-2 is gated. Accept the license at
   <https://huggingface.co/microsoft/maira-2> and `huggingface-cli login`
   before starting the image MCP.
9. **Port collision** — override `EHR_EHRXQA_PORT`, `EHR_MEDMOD_PORT`,
   `IMAGE_PORT` if other services already bind to the defaults.

---

## 6. Scoring: `helper/scorer_mm.py`

End-to-end scorer. One script covers: prediction extraction, routing,
LLM-judge for len=1 rows, rule-based set metrics for len≥2 rows,
(optional) vocabulary normalization of set-metric predictions, and
per-task unified F1 that folds both routes into a single aggregator.
Both **raw** and **vocab-mapped** metrics are retained in every output —
no second re-score pass.

> **Validation.** The integrated raw and mapped set-F1 tracks reproduce
> the outputs of the old two-stage pipeline (`scored.jsonl` +
> `rescored.jsonl`) bit-for-bit on all 635 len≥2 `set_f1` rows of the
> Claude Sonnet 4.6 run at
> `openresearcher_ehr/results/multi_eval/full5_20260421T091517Z/`. Raw
> normalization (strip + lower) and mapped normalization (strip + lower
> + whitespace-collapse) are preserved as distinct paths so you can
> compare strict set-overlap against lenient vocab-aligned set-overlap.

### 6.1 What it does (overview)

```
  results.jsonl
    │  (one row per sample — has messages, label, task, scope, ...)
    ▼
  ┌──────────── extract prediction ────────────┐
  │  walk messages in reverse; find the last   │
  │  tool_call whose name is in                │
  │  {"ehr.finish","finish"}; JSON-parse args; │
  │  return args["response"].                  │
  └─────────────────────────────────────────────┘
    │ flatten to list[str] (list-of-dicts → `name`/`value`)
    ▼
  ┌─── route by gold-label shape ──────────────┐
  │  len(gold) >= 2  → rule-based set metrics  │
  │                    (raw + vocab-mapped)    │
  │  len(gold) == 1  → LLM judge               │
  │  empty/no-finish → incomplete (all zeros)  │
  └─────────────────────────────────────────────┘
    │
    ▼
  ┌──── aggregate ─────────────────────────────┐
  │  per-task len=1 accuracy                   │
  │  per-task len≥2 {f1, p, r, jac, subset}    │
  │                    × {raw, mapped}         │
  │  per-task unified F1 (judge folded in)     │
  │                    × {raw, mapped}         │
  └─────────────────────────────────────────────┘
    │
    ▼
  scored.jsonl + summary.json + summary.md
```

### 6.2 Metrics

**Rule-based (for `len(gold) >= 2`)** — set-style operations over
normalized (lowercased, stripped) `name` strings:

- **precision** = |pred ∩ gold| / |pred|
- **recall** = |pred ∩ gold| / |gold|
- **F1** = harmonic mean of precision and recall
- **accuracy** (Jaccard-style) = |pred ∩ gold| / |pred ∪ gold|
- **subset_match** = 1.0 if pred set == gold set, else 0.0

All five are computed **twice** for each set-F1 row:

- **raw** — over the model's free-text predictions as returned.
- **vocab-mapped** — each predicted string first replaced by the nearest
  gold-vocabulary entry whose cosine similarity ≥ `--vocab-threshold`
  (default 0.55). Below threshold, the raw string is kept so OOV guesses
  still have a chance via the exact-string path.

**LLM judge (for `len(gold) == 1`)** — Claude Sonnet 4.6 on Bedrock returns
`{"match": true|false, "reason": "<= 25 words"}`. The scorer fills
precision/recall/F1/accuracy/subset_match all as 1.0 on match, 0.0 on
mismatch. Judge verdicts don't depend on vocab mapping — raw == mapped
for these rows.

**Incomplete rows** (no `ehr.finish`, bad JSON args, or no gold label) are
counted and scored as 0 across the board (raw and mapped).

**Unified F1** — a per-task F1 that averages together len=1 judge
verdicts (correct → F1=1, else F1=0) and len≥2 set F1s. Also reported
in both raw and mapped variants so you can compare.

### 6.3 Vocabulary normalization

Motivation: phenotyping, radiology, and mortality tasks draw gold labels
from a closed vocabulary. Models that paraphrase ("enlarged cardiac
silhouette" vs. "Cardiomegaly") score 0 under exact set overlap even
when they name the right concept. The mapping step normalizes both
sides.

How it works:

1. Build a per-task vocabulary = union of all gold `name` strings seen
   in the dataset.
2. Embed the vocabulary once per task with `all-MiniLM-L6-v2`
   (sentence-transformers, L2-normalized → dot = cosine).
3. For each `len(gold)>=2` sample, embed its predictions and replace each
   with the nearest vocab entry when cosine ≥ threshold.
4. Compute set metrics over the mapped predictions; keep the raw metrics
   side-by-side.

Default skip list: `ehrxqa_table` — its "vocabulary" is cohort IDs /
numbers / dates, which don't meaningfully embed. Override with
`--vocab-skip-tasks`.

### 6.4 Prediction extraction + answer mapping

Implemented in `helper/scorer_mm.py`:

1. **`extract_prediction(row)`** — walks `row["messages"]` in reverse;
   finds the last `tool_call` whose name is in `{"ehr.finish","finish"}`;
   parses `arguments` as JSON; returns `arguments["response"]`.
   Returns `(None, "no_finish_call")` if absent, `(raw, "bad_json_args")`
   if JSON parse fails, or `(args, "no_response_key")` if `response` key
   missing.
2. **`pred_to_strings(pred)`** — flattens whatever the model returned into
   a `list[str]`:
   - `list[dict]` → try `name`/`value`/`text`/`answer` fields; fall back to
     `json.dumps(x)`.
   - `str` → `[str]`.
   - `None` → `[]`.
3. **`gold_to_strings(gold)`** — the manifest always stores `label` as
   `list[dict]`; extracts `name` (fallback `value`) into `list[str]`.
4. **`_normalize_name(s)`** — `str(s).strip().lower()` before set ops.
5. **`VocabMatcher`** — for set-F1 rows, maps each predicted string to
   its nearest task-vocabulary entry when cosine ≥ threshold.
6. **`build_task_vocabularies(rows)`** — harvests the per-task gold
   vocabulary from the results file (or a separate `--vocab-gold` JSONL).

### 6.5 LLM-judge subtype router

When `len(gold)==1`, the scorer picks one of six templates based on gold
content + task:

| Subtype | Heuristic | Prompt emphasis |
|---------|-----------|-----------------|
| `yesno` | gold ∈ {yes, no, true, false, 0, 1} | polarity match; accept "Yes, ..." / "No, ..." prefixes |
| `date_time` | gold matches `YYYY-MM-DD(?:[ T]HH:MM(:SS)?)?` | exact date; minute precision on time; different TZ ≠ match |
| `id` | task==`ehrxqa_table` and gold matches `^\d{6,10}$` | exact id match only |
| `count` | gold is int/float (and/or question has count-like keywords) | number equality; `'two'` matches `'2'`; unit differences OK |
| `label_name` | polar question prefix with short gold, or task ∈ {`medmod_radiology`, `medmod_phenotyping`, `ehrxqa_image`} | synonym/abbrev tolerance; partial/adjacent concepts don't match |
| `generic_string` | fallback | semantic equivalence; minor wording differences fine |

See `classify_subtype()` in `helper/scorer_mm.py` for the full branching.

### 6.6 How to run it

```bash
PY=$REPO/venvs/bedrock_agent/bin/python

$PY $REPO/openresearcher_ehr/helper/scorer_mm.py \
    --results $REPO/openresearcher_ehr/results/mm_prepared_<UTC>/results.jsonl \
    --output-root $REPO/openresearcher_ehr/results/scored/
```

CLI flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--results PATH` (repeatable) | required | Results JSONL; repeat for multiple runs. Each lands in `<output-root>/<parent-dirname>/`. |
| `--output-root DIR` | required | Root for per-run scored output |
| `--judge-model` | `global.anthropic.claude-sonnet-4-6` | LLM judge model id |
| `--region` | `$BEDROCK_REGION` or `ca-west-1` | Single-region fallback |
| `--regions r1 r2 ...` | None | Round-robin pool for the judge (Anthropic id prefix remapped per region automatically) |
| `--concurrency N` | 10 | Per-region concurrency. Global = N × len(regions) |
| `--max-rows N` | 0 (all) | Cap rows for smoke scoring |
| `--no-resume` | off | Re-judge rows already in `scored.jsonl` |
| `--vocab` / `--no-vocab` | on | Enable / disable vocabulary normalization for len≥2 rows |
| `--vocab-model` | `all-MiniLM-L6-v2` | sentence-transformers embedder (CPU by default) |
| `--vocab-threshold` | 0.55 | Min cosine similarity to replace a prediction with a vocab entry |
| `--vocab-device` | `cpu` | Torch device for the embedder (`cuda:0`, etc.) |
| `--vocab-skip-tasks` | `[ehrxqa_table]` | Tasks excluded from vocab mapping (cohort ids / numbers / dates) |
| `--vocab-gold PATH` | None | Separate JSONL source for gold labels; default reads `label` from `--results` |

Multi-region example (2× throughput), vocab on:

```bash
$PY $REPO/openresearcher_ehr/helper/scorer_mm.py \
    --results path/to/results.jsonl \
    --output-root ./scored \
    --regions us-east-1 us-west-2 \
    --concurrency 10 \
    --vocab-threshold 0.55
```

Strict raw-metric-only run:

```bash
$PY $REPO/openresearcher_ehr/helper/scorer_mm.py \
    --results path/to/results.jsonl \
    --output-root ./scored_raw \
    --no-vocab
```

### 6.7 Output files

```
<output-root>/<run-dirname>/
├── scored.jsonl    # one JSON per sample:
│                   # qid, task, scope, len_gold, judge_subtype (if len=1),
│                   # prediction_strs, prediction_flat, gold_names,
│                   # route ∈ {incomplete, set_f1, llm_judge},
│                   # correct, precision, recall, f1, accuracy, subset_match,
│                   # + precision_raw/recall_raw/f1_raw/accuracy_raw/
│                   #   subset_match_raw (len≥2 rows),
│                   # + prediction_strs_mapped, mapping_similarity,
│                   #   vocab_mapped (len≥2 rows, when vocab on),
│                   # judge_match, judge_reason, judge_raw (judge route only).
├── summary.json    # aggregate stats:
│                   #   total, incomplete,
│                   #   len1_accuracy / len1_count,
│                   #   len2plus_{f1, precision, recall, accuracy,
│                   #              subset_match} + *_raw counterparts,
│                   #   per_task_len1 (accuracy + subtype breakdown),
│                   #   per_task_len2plus (raw + mapped averages),
│                   #   unified_{f1, precision, recall, accuracy,
│                   #              subset_match} + *_raw counterparts,
│                   #   per_task_unified (raw + mapped averages),
│                   #   vocab_model, vocab_threshold, vocab_skip_tasks
│                   #   (when vocab on)
└── summary.md      # human-readable companion of summary.json,
                    # with raw → mapped comparison per task.
```

---

## 7. End-to-end dry run (copy-paste quickstart)

```bash
# 0) paths
export REPO=/fsx-shared/juncheng/EHR

# 1) data — HF download + extract ready tier + grab prepared 2,703-row test set
hf download Chtholly17/EHR_multimodal_bench --repo-type dataset \
    --local-dir $REPO/data/EHR_multimodal_bench
cd $REPO/data/EHR_multimodal_bench && mkdir -p extracted
unzip -q EHRXQAAgentBench_v3.zip -d extracted/
for f in MedMod/MedModAgentBench_v3_ready_{manifests,databases,images}.zip; do
    unzip -q "$f" -d extracted/
done
# Prepared 2,703-row mixed test set (default input to run_mm_pipeline.sh)
mv $REPO/data/EHR_multimodal_bench/EHR_multimodal_bench_tests \
   $REPO/data/EHR_multimodal_bench_tests

# 2) venvs
cd $REPO/venvs
uv venv bedrock_agent --python 3.12
uv pip install --python bedrock_agent/bin/python -r requirements/bedrock_agent.txt

uv venv mcp_ehr --python 3.12
uv pip install --python mcp_ehr/bin/python torch==2.8.0 torchvision \
    --index-url https://download.pytorch.org/whl/cu121
uv pip install --python mcp_ehr/bin/python -r requirements/mcp_ehr.txt

uv venv mcp_image --python 3.12
uv pip install --python mcp_image/bin/python --reinstall \
    torch==2.9.0+cu128 torchvision==0.24.0+cu128 \
    --index-url https://download.pytorch.org/whl/cu128
uv pip install --python mcp_image/bin/python -r requirements/mcp_image.txt

# 3) creds
export BEDROCK_API_KEY=...      # or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
huggingface-cli login           # for MAIRA-2 (gated)

# 4) smoke run (5 rows, no image MCP) — fastest sanity check
cd $REPO/openresearcher_ehr
PYBIN=$REPO/venvs/bedrock_agent/bin/python \
IMAGE_PYBIN=$REPO/venvs/mcp_image/bin/python \
bash run_mm_pipeline.sh --mode prepared --limit 5

# 5) score (single end-to-end pass; vocab normalization on by default)
$REPO/venvs/bedrock_agent/bin/python helper/scorer_mm.py \
    --results $REPO/openresearcher_ehr/results/mm_prepared_*/results.jsonl \
    --output-root $REPO/openresearcher_ehr/results/scored/
```

---

## 8. File inventory

```
$REPO/
├── data/
│   └── EHR_multimodal_bench/
│       ├── EHRXQAAgentBench_v3.zip                 (source archives)
│       ├── MedMod/*.zip
│       └── extracted/
│           ├── EHRXQAAgentBench_v3/                (ready tier)
│           └── MedModAgentBench_v3/                (ready tier)
├── data/EHR_multimodal_bench_tests/
│   └── combined_test_set.jsonl                    (prepared 2,703-row set)
├── venvs/
│   ├── bedrock_agent/                             (venv A)
│   ├── mcp_ehr/                                   (venv B)
│   ├── mcp_image/                                 (venv C)
│   └── requirements/
│       ├── bedrock_agent.txt
│       ├── mcp_ehr.txt
│       └── mcp_image.txt
├── src/
│   ├── run_mcp_server.py                          (EHR MCP entry)
│   ├── agentlite/mcp_tools/                       (20 ehr.* tools)
│   └── mcp_image/
│       ├── run_image_mcp_server.py                (Image MCP entry)
│       ├── fastmcp_app.py
│       └── tools/                                 (6 image.* tools)
├── openresearcher_ehr/
│   ├── deploy_agent_mm.py                         (multimodal driver)
│   ├── bedrock_generator.py
│   ├── vllm_generator.py                          (text-only, no images)
│   ├── ehr_pool.py  image_pool.py  browser.py
│   ├── data_utils_mm.py                           (MM system prompt + tool schemas)
│   ├── prepare_mm_data.py
│   ├── run_mm_pipeline.sh                         (unified launcher)
│   ├── run_mm.sh                                  (earlier minimal launcher)
│   ├── helper/scorer_mm.py   (end-to-end scorer; vocab + unified F1)
│   └── data_mm/                                   (filtered JSONL inputs)
└── docs/
    ├── debug_logs/
    │   ├── 05_multimodal_bench_ehr_multimodal_bench.md
    │   └── 06_run_multimodal_pipeline.md
    └── multimodal_ehr_benchmark_evaluation.md     (this file)
```

---

## 9. Appendix — Porting to a vLLM-only host (no Bedrock)

The multimodal pipeline today is **Bedrock-only** at the source level.
Specifically:

- `deploy_agent_mm.py` hardcodes `--backend` `choices=["bedrock"]`
  (`deploy_agent_mm.py:788`) and the generator is
  `BedrockAsyncGenerator`.
- `vllm_generator.py` has **no image content handling** — it builds
  text-only user messages and will error on multimodal content blocks.
- `helper/scorer_mm.py` uses `BedrockAsyncGenerator` for the LLM judge.

To run end-to-end against a local vLLM server with a multimodal model
(e.g. `Qwen/Qwen2.5-VL-7B-Instruct`, `llava-hf/llava-1.5-*`,
`google/gemma-3-*-it`), you need the following changes. None of them are
speculative — each references a concrete file + line.

### 9.1 Agent driver: add a vLLM branch

- `openresearcher_ehr/deploy_agent_mm.py:788` — change
  `choices=["bedrock"]` to `choices=["bedrock","vllm"]` and expose
  `--api_base_url`, `--api_key`, `--model_name_or_path` (already used
  by the text-only `deploy_agent.py:766–770`).
- After arg parsing, branch: if `args.backend == "vllm"`, construct a
  `VLLMAsyncGenerator` (new class, or import from a modified
  `vllm_generator.py`) instead of `BedrockAsyncGenerator`. The call
  surface (`chat_completion(messages, tools, tool_choice, temperature,
  max_tokens)`) is already uniform.

### 9.2 `vllm_generator.py`: add multimodal content support

vLLM's OpenAI-compatible API accepts image content via
`{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}`.
The existing `bedrock_generator._prepare_anthropic_messages` produces
`{"type":"image","source":{"type":"base64",...}}`. Changes required:

- Add a helper that converts `deploy_agent_mm.py`'s Anthropic-style
  `content` list into OpenAI-style `content`:
  - Anthropic `image.source.data` → OpenAI `image_url.url =
    f"data:{media_type};base64,{data}"`.
  - Anthropic `text` → OpenAI `text`.
- Hook this into the `chat_completion` path before sending to vLLM.
- vLLM's `--enable-auto-tool-choice --tool-call-parser hermes` already
  handles the 29-tool schema — no changes to the tool schemas.

### 9.3 vLLM server startup

Launch with a multimodal model and the tool-call parser:

```bash
CUDA_VISIBLE_DEVICES=6,7 vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
    --port 4000 \
    --dtype auto \
    --limit-mm-per-prompt '{"image": 4}' \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --gpu-memory-utilization 0.85 \
    --max-model-len 64000
```

(The 4-image cap matches `deploy_agent_mm.py`'s `_MAX_IMAGES_PER_PROMPT`.)

Then run:

```bash
$REPO/venvs/bedrock_agent/bin/python $REPO/openresearcher_ehr/deploy_agent_mm.py \
    --backend vllm \
    --api_base_url http://127.0.0.1:4000/v1 \
    --api_key EMPTY \
    --model_name_or_path Qwen/Qwen2.5-VL-7B-Instruct \
    --data_path $REPO/data/EHR_multimodal_bench_tests/combined_test_set.jsonl \
    --output_dir ./results/vllm_qwen25vl \
    --enable_ehr --ehr_mcp_url_ehrxqa http://127.0.0.1:5103/mcp \
                  --ehr_mcp_url_medmod http://127.0.0.1:5104/mcp \
    --enable_image --image_mcp_url http://127.0.0.1:5203/mcp \
    --bench_root $REPO/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3:$REPO/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3 \
    --max_rounds 200 --max_concurrency 6 --disable_thinking --verbose
```

### 9.4 Scorer: swap the judge backend

- `openresearcher_ehr/helper/scorer_mm.py` (around the `bedrock_generator` import block) replaces
  `BedrockAsyncGenerator` with the vLLM generator class (same
  `chat_completion` surface).
- `helper/scorer_mm.py` — the `_anthropic_id_for_region` region-swap logic
  becomes a no-op; pass through the local model id.
- The `--regions` flag is meaningless locally — use plain
  `--concurrency` with one server. (Multi-server round-robin is trivial
  if you run >1 vLLM process.)
- The six judge templates (`yesno` / `count` / `date_time` / `id` /
  `label_name` / `generic_string`) are prompt-only — model-agnostic.
  Any instruction-tuned model that returns `{"match": ..., "reason":
  ...}` JSON reliably works; a 70B-class chat model is recommended for
  fidelity parity with Claude Sonnet 4.6.

### 9.5 Minimum touchpoints summary

| File | What to change |
|------|----------------|
| `openresearcher_ehr/deploy_agent_mm.py:788, 790–794` | Expand `--backend` to accept `vllm`; wire vLLM generator |
| `openresearcher_ehr/vllm_generator.py` | Add OpenAI-style multimodal content helper; accept image blocks in `chat_completion` |
| `openresearcher_ehr/helper/scorer_mm.py` | Swap `BedrockAsyncGenerator` for vLLM generator; make region remap a no-op |
| `openresearcher_ehr/run_mm_pipeline.sh` | Add `VLLM_*` env vars + a `--backend vllm` pass-through |

All MCP servers (EHR + image) and the scorer's routing/metric logic are
untouched — the only code that needs to know about vLLM is the agent
driver and the generator wrappers.
