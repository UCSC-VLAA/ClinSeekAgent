# EHR-Bench — Evaluation Runbook

A complete, self-contained guide to preparing data, running the agent
pipeline, and scoring results for the **text-only** EHR-Bench benchmark
in this repo.

**Scope.** This doc targets the pipeline shipped under
`openresearcher_ehr/` (`deploy_agent.py` + `run_test_subset.sh` +
`helper/evaluate_results.py`), backed by either a local vLLM server or
AWS Bedrock. For the multimodal variant (EHRXQA + MedMod with chest
X-rays and the image MCP), see
[`multimodal_ehr_benchmark_evaluation.md`](./multimodal_ehr_benchmark_evaluation.md).

**Conventions.** Throughout this doc `$REPO = /fsx-shared/juncheng/EHR`.
Replace it with your checkout path. All paths shown under `$REPO/...`
are relative to that root.

Paths, counts, and defaults were verified against the running tree on
2026-04-22.

---

## 0. Overall pipeline structure

Four stages. Each box below is a separate process.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      (0)  DATA PREPARATION                              │
│  Option A (recommended): pre-built EHR-Bench release                    │
│     HF dataset  Letian2003/Bb21385                                      │
│       → data/EHR-Bench/{*.json, database/patient_*.db}                  │
│  Option B: build from MIMIC-IV raw                                      │
│     HF datasets Letian2003/MA49234 + Letian2003/ry03890                 │
│       → helper/reverse_match.py  (fills subject_id / hadm_id)           │
│       → helper/patient_event2db.py (per-patient .db shards)             │
│  Option C (AgentEHR-Bench):  HF dataset  BlueZeros/AgentEHR-Bench       │
│     → data/AgentEHR-Bench/MIMICIVAgentBench/…  (subset_500 default)     │
└─────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (1)  MCP SERVER                                    │
│   :5103  EHR MCP (single root)   src/run_mcp_server.py                  │
│          exposes 16 ehr.* tools over HTTP/JSON-RPC                      │
│          (--disable-knowledge-tools when using browser for search)      │
└─────────────────────────────────────────────────────────────────────────┘
                               │  HTTP
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (2)  vLLM SERVER  (or AWS Bedrock)                 │
│   :4000  OpenAI-compatible    scripts/run/run_vllm_server_*.sh          │
│          Qwen3.5-35B-A3B / OpenSeeker / Gemma-4 / Meissa-4B …           │
│   (alt)  Bedrock Claude Opus 4.6 — use openresearcher_ehr/run.sh        │
└─────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (3)  AGENT DRIVER                                  │
│   openresearcher_ehr/deploy_agent.py                                    │
│     ├─ builds system + user messages (task_instruction + patient_info)  │
│     ├─ tool schemas: 16 ehr.* + 3 browser.*  = 19 tools                 │
│     ├─ core loop: LLM → tool_calls → route to MCP/Browser → append      │
│     └─ stops on ehr.finish or max_rounds                                │
│   launched by  run_test_subset.sh  (vLLM)  or  run.sh  (Bedrock)        │
└─────────────────────────────────────────────────────────────────────────┘
                               │ one JSON line per sample
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      (4)  SCORER                                        │
│   openresearcher_ehr/helper/evaluate_results.py                         │
│     ├─ extracts prediction from ehr.finish arguments                    │
│     ├─ set-match vs. benchmark ground_truth (case-insensitive)          │
│     └─ prints per-task Precision / Recall / F1 + tool-call stats        │
└─────────────────────────────────────────────────────────────────────────┘
```

The benchmark has two distinct dataset families; either can be
evaluated with the same agent driver:

| Benchmark | Source | Default subset for evaluation | Task family |
|-----------|--------|-------------------------------|-------------|
| **AgentEHR-Bench** | MIMIC-IV Agent Bench | `subset_500/merged_subsets_500.json` (500/task × 6 tasks) | decision-making (labs, Rx, diagnoses, procedures, microbio, transfers) |
| **EHR-Bench** | EHR-R1 (derived from MIMIC-IV) | `ehr_bench_sampled_40_per_task.json` (40/task × 45 tasks = 1,800) | risk-prediction + decision-making |

See §1 for schema and counts.

---

## 1. Dataset: download & preprocess

You have three paths into a runnable dataset. Pick **one** depending on
what you want to evaluate.

### 1.1 (Recommended) Pre-built EHR-Bench release

The fastest start: a packaged release with the matched JSON manifests
**and** all per-patient `.db` files ready for the MCP server.

```bash
# from repo root
huggingface-cli download Letian2003/Bb21385 \
    --repo-type dataset \
    --local-dir data

# unpack every archive in-place
for ext in zip tar.gz csv.gz; do
    find data -name "*.${ext}" -execdir sh -c '
        case "$1" in
            *.zip)    unzip -o "$1" ;;
            *.tar.gz) tar -xzf "$1" ;;
            *.csv.gz) gunzip -f "$1" ;;
        esac' _ {} \;
done
```

Result:

```
data/EHR-Bench/
├── ehr_bench_merged_filtered.json                  # full 20,302-row manifest
├── ehr_bench_decision_making_subset_format.json
├── ehr_bench_risk_prediction_subset_format.json
├── ehr_bench_sampled_20_per_task.json              # 900-row smoke
├── ehr_bench_sampled_40_per_task.json              # 1,800-row eval
└── database/patient_<subject_id>.db                # per-patient SQLite
```

Point the MCP server at `data/EHR-Bench` (see §2) and you're done.

### 1.2 (AgentEHR-Bench) HuggingFace one-shot

If you're evaluating against the original AgentEHR-Bench tasks (the
`run_test_subset.sh` default):

```bash
hf download --repo-type dataset BlueZeros/AgentEHR-Bench \
    --local-dir data/AgentEHR-Bench
hf download --repo-type dataset BlueZeros/EHR-Bench \
    --local-dir data/EHR-Bench
```

Default evaluation data path (used by `run_test_subset.sh`):

```
data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json
```

### 1.3 (Advanced) Build EHR-Bench from MIMIC-IV raw

Use this only if you need to reproduce the full matching + db-generation
pipeline yourself. Two HF mirrors of MIMIC-IV:

| Module | HF repo | Destination |
|--------|---------|-------------|
| hosp / icu / note | `Letian2003/MA49234` | `data/MIMIC-IV/mimic_iv` |
| ED | `Letian2003/ry03890` | `data/MIMIC-IV/mimic_iv_ed` |

```bash
huggingface-cli download Letian2003/MA49234 --repo-type dataset \
    --local-dir data/MIMIC-IV/mimic_iv
huggingface-cli download Letian2003/ry03890 --repo-type dataset \
    --local-dir data/MIMIC-IV/mimic_iv_ed

# unpack everything
for d in data/MIMIC-IV/mimic_iv data/MIMIC-IV/mimic_iv_ed; do
    find "$d" -name "*.zip"    -execdir unzip -o {} \;
    find "$d" -name "*.tar.gz" -execdir tar -xzf {} \;
    find "$d" -name "*.csv.gz" -exec gunzip -f {} \;
done

# merge ED under the main MIMIC tree
mv data/MIMIC-IV/mimic_iv_ed/mimic-iv-ed/2.2/ed data/MIMIC-IV/mimic_iv/
```

Final layout:

```
data/MIMIC-IV/mimic_iv/
├── hosp/    icu/    note/    ed/
```

#### 1.3.1 Reverse-match `subject_id` / `hadm_id`

EHR-Bench strips patient identifiers but keeps second-level timestamps
+ demographics — enough to uniquely recover each patient.

**Matching strategies** (tried in order, first uniquely-matching one
wins):

| # | Benchmark section | MIMIC-IV table | Field |
|---|-------------------|----------------|-------|
| 1 | `## Transfers [ts]` | `transfers.csv` | `intime` |
| 2 | `## EDstays [ts]` | `transfers.csv` / `admissions.csv` | `intime` (ED) / `edregtime` |
| 3 | `## Admissions [ts]` | `admissions.csv` | `admittime` |
| 4 | `## Provider Order Entry [ts]` | `poe.csv` | `ordertime` |
| 5 | `## Pharmacy [ts]` | `pharmacy.csv` | `entertime` |
| 6 | `## Prescriptions [ts]` | `prescriptions.csv` | `starttime` |
| 7 | `## Electronic Medicine Administration Record [ts]` | `emar.csv` | `charttime` |
| 8 | any section (fallback) | `transfers.csv` | `intime` |

Runs from repo root (5–10 min, dominated by loading `poe.csv` /
`emar.csv`):

```bash
python3 helper/reverse_match.py
```

Paths hardcoded in the script:

```python
MIMIC_DIR = "data/MIMIC-IV/mimic_iv"
BENCH_DIR = "data/EHR-Bench"
```

Outputs: `ehr_bench_{decision_making,risk_prediction}_subset_format_matched.json`
(unmatched rows dropped — ~0.16 %).

Match rates:

| Dataset | Original | Matched | Rate | With `hadm_id` |
|---------|---------:|--------:|-----:|---------------:|
| Decision-Making | 13,500 | 13,471 | 99.8 % | 12,682 |
| Risk-Prediction |  7,721 |  7,716 | 99.9 % |  7,011 |

Notes:
- MIMIC-IV timestamps are pre-offset for de-identification; EHR-Bench's
  timestamps use the same offset, so direct equality matching works.
- Some ED-only visits have no `hadm_id` in MIMIC-IV itself — matched
  row will have `subject_id` set but `hadm_id=null`.

#### 1.3.2 Generate per-patient SQLite dbs

`helper/patient_event2db.py` turns matched JSON + MIMIC-IV raw CSVs
into `patient_<subject_id>.db` files (one per patient), which is what
the MCP server loads.

Inputs:

| Flag | Example | Purpose |
|------|---------|---------|
| `--root_path` | `data/MIMIC-IV/mimic_iv` | MIMIC-IV root with `hosp/ icu/ note/ ed/` |
| `--data_file_path` | `data/EHR-Bench/ehr_bench_merged_filtered.json` | matched manifest (source of subject_ids) |
| `--data_dir_path` | _(optional)_ | merge all `.json` files under a directory |
| `--subject_id` | _(optional)_ | single-patient mode for debugging |
| `--data_dirs` | `ed hosp icu note` | subdirs to scan |
| `--output_path` | `data/EHR-Bench/database` | where `patient_*.db` lands |

Preprocessing the script does automatically:
- Fills `charttime` on `diagnoses_icd` (→ `admissions.dischtime − 1 min`).
- Fills `charttime` on ED `diagnosis` (→ `edstays.outtime − 1 min`).
- Copies the "Physical Exam" prefix of `note/discharge.csv.text` onto
  `admissions.text`. If an admission can't be matched to a discharge
  note, that admission's `text` is set to empty (row kept).

Run (from repo root, put it in the background — full build takes a few
hours, peak RSS 30–60 GB because all target patients are grouped in
memory before writing):

```bash
nohup python helper/patient_event2db.py \
    --root_path data/MIMIC-IV/mimic_iv \
    --output_path data/EHR-Bench/database \
    --data_file_path data/EHR-Bench/ehr_bench_merged_filtered.json \
    > logs/ehr_bench_db_gen_$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
```

Single-patient debug:

```bash
python helper/patient_event2db.py \
    --root_path data/MIMIC-IV/mimic_iv \
    --output_path data/EHR-Bench/database \
    --subject_id 10000108
```

Re-running the same command is incremental: existing
`patient_<id>.db` files are skipped (CSV scans still happen, so the
speedup is in the DB-write step only).

Notes on the resulting DBs:
- Table names mirror MIMIC-IV CSV names (`admissions`, `diagnoses_icd`,
  `labevents`, `prescriptions`, `transfers`, `radiology`, …).
- **All columns are stored as `TEXT`** — cast in SQL (or in the agent's
  tool layer) when you need numeric / time comparisons.
- Warning `Directory not found: .../ed` is fine when ED isn't
  downloaded; `diagnoses_icd` / ED `diagnosis` charttime fallback is
  empty string.

### 1.4 EHR-Bench stratified subsets

The full EHR-Bench is **20,302 rows across 45 tasks** (split 2 ways:
`risk_prediction`, `decision_making`). Two pre-sampled subsets ship in
the release and are what you actually want to run:

| File | Per-task | Total | risk / decision | Use case |
|------|---------:|------:|:----------------|----------|
| `data/EHR-Bench/ehr_bench_sampled_20_per_task.json` | 20 | 900 | 360 / 540 | small smoke |
| `data/EHR-Bench/ehr_bench_sampled_40_per_task.json` | 40 | 1,800 | 720 / 1,080 | **recommended** |

Sampling: bucket by `task`, fixed seed, without replacement. Reproduce
with `helper/sample_per_task.py`:

```bash
python helper/sample_per_task.py                           # per-task=40, seed=42
python helper/sample_per_task.py --per-task 20 --seed 42   # 900 rows
```

Supports `--input`, `--output`, `--per-task`, `--seed`.

Switch the driver to the 1,800-row subset:

```bash
DATA_PATH=../data/EHR-Bench/ehr_bench_sampled_40_per_task.json \
    bash openresearcher_ehr/eval_ehrbench.sh
```

When scoring, pass the **same** file as `--benchmark`.

### 1.5 AgentEHR-Bench tasks and prompt templates

Each task has a specialised prompt + candidate table. Definitions live
in `openresearcher_ehr/data_utils.py` (`TASK_PROMPT_TEMPLATES`).

| task | agent role | prediction target | candidate table |
|------|------------|-------------------|-----------------|
| `diagnoses_ccs` | diagnostician | all applicable CCS diagnoses | `diagnoses_ccs_candidates` |
| `procedures_ccs` | surgical planner | all needed CCS procedures | `procedures_ccs_candidates` |
| `labevents` | lab physician | all needed lab tests | `labevents_candidates` |
| `prescriptions` | pharmacist | all needed ATC drug classes | `prescriptions_atc_candidates` |
| `microbiologyevents` | microbiologist | all needed microbio tests | `microbiologyevents_candidates` |
| `transfers` | ward coordinator | next ward / discharge destination | `transfers_candidates` |

Each prompt is a pair:

```xml
<task_instruction>
  role + goal + candidate-table hint + browser.search hint + output format
</task_instruction>
<patient_info>
  Current Time: {prediction_time}
  Patient Subject ID: {subject_id}
</patient_info>
```

Per-task sample row:

```json
{
  "subject_id": 10000032,
  "prediction_time": "2150-12-01 10:00:00",
  "task": "diagnoses_ccs",
  "ground_truth": [{"name": "Coronary atherosclerosis ..."}, ...]
}
```

---

## 2. Environment & service setup

### 2.1 Virtual environment (uv)

Prereqs: Python ≥ 3.10, CUDA 12.x. Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Create a single venv for both driver and servers:

```bash
uv venv venv/gemma --python 3.12
uv pip install -r requirements.txt                                 --python venv/gemma/bin/python
uv pip install -r openresearcher_ehr/requirements.txt              --python venv/gemma/bin/python
uv pip install "vllm>=0.19.0" "transformers>=5.5"                  --python venv/gemma/bin/python
source venv/gemma/bin/activate
```

> `vllm>=0.19.0` pulls `transformers 4.x`; re-upgrade transformers
> after installing vllm. Required for Gemma4, Qwen3.5 support.

### 2.2 Start the EHR MCP server

```bash
# terminal #1 — defaults: GPU 0, port 5103, AgentEHR-Bench root
bash scripts/run/run_mcp_server.sh

# custom GPU / port
bash scripts/run/run_mcp_server.sh <GPU_ID> <PORT>
```

What the script does: launches
```
CUDA_VISIBLE_DEVICES=$GPU_ID python src/run_mcp_server.py \
    --mode http --host 127.0.0.1 --port $PORT \
    --data_path data/AgentEHR-Bench/MIMICIVAgentBench \
    --disable-knowledge-tools
```

If you packaged EHR-Bench via §1.1, point `--data_path` at
`data/EHR-Bench` instead.

Wait for `Uvicorn running on http://127.0.0.1:5103`. If you change the
port here, update `EHR_MCP_URL` in §4.

`--disable-knowledge-tools` turns off the built-in corpus-retrieval
tools — the agent uses `browser.*` for medical-knowledge lookups
instead.

### 2.3 Start the vLLM server

Pick the script that matches your model. Default port **4000**.

| Model | Script | Notes |
|-------|--------|-------|
| **Qwen3.5-35B-A3B** (default) | `scripts/run/run_vllm_server_3_5.sh` | tool parser `qwen3_xml`, reasoning parser `qwen3`, max_model_len 1,000,000, 8×A100 |
| OpenSeeker-v1-30B-SFT | `scripts/run/run_vllm_server.sh` | auto-loads chat template + tool parser plugin in `openresearcher_ehr/openseeker_vllm/` |
| OpenResearcher-30B-A3B | `scripts/run/run_vllm_server_Nemotron.sh` | Nemotron-style |
| Meissa-4B | `scripts/run/run_vllm_server_Meissa_4B.sh` | single-GPU friendly |
| Gemma-4-26B-A4B-it | `scripts/run/run_vllm_server_gemma4.sh` | single-A100 80 GB |

Override GPU and port:

```bash
bash scripts/run/run_vllm_server_3_5.sh <CUDA_DEVICES> <PORT>
# e.g. 4 cards, port 4001
bash scripts/run/run_vllm_server_3_5.sh 0,1,2,3 4001
```

Reference memory envelope:

| Model | GPUs | max_model_len | ~VRAM/GPU |
|-------|-----:|---------------:|----------:|
| Qwen3.5-35B-A3B | 8 × A100 80 GB | 1,000,000 | ~60 GB |
| OpenSeeker-v1-30B-SFT | 8 × A100 80 GB | 262,144 | ~60 GB |
| OpenResearcher-30B-A3B | 8 × A100 80 GB | 262,144 | ~60 GB |
| Meissa-4B | 1 × A100 80 GB | 8,192 | ~15 GB |
| Gemma-4-26B-A4B-it | 1 × A100 80 GB | 8,192 | ~75 GB |

If you OOM:

```bash
GPU_MEMORY_UTILIZATION=0.7 bash scripts/run/run_vllm_server_3_5.sh
```

Verify vLLM is up:

```bash
curl -s http://127.0.0.1:4000/v1/models | python -m json.tool
# → should list the model id
```

---

## 3. Running the pipeline

### 3.1 Launcher: `run_test_subset.sh` (vLLM backend)

Primary entrypoint. Behaviour:

1. When `VLLM_MODEL_NAME=auto` (default), hits `/v1/models` to pick up
   the running model's real id.
2. Derives a short slug (e.g. `qwen3_5_35b_a3b`) for the output
   directory.
3. Calls `deploy_agent.py` with all config passed as CLI flags.
4. Tees stdout/stderr to `run_test_subset_*.log`.

```bash
cd openresearcher_ehr
bash run_test_subset.sh
```

Default wiring:

| Key | Default |
|-----|---------|
| MCP URL | `http://127.0.0.1:5103/mcp` |
| vLLM URL | `http://127.0.0.1:4000` |
| data | `../data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json` |
| concurrency | 5 |
| max rounds | 200 |
| temperature | 0.0 |
| thinking | on |

Env overrides:

| Env | Default | Meaning |
|-----|---------|---------|
| `VLLM_BASE_URL` | `http://127.0.0.1:4000` | vLLM server |
| `VLLM_MODEL_NAME` | `auto` | explicit id or `auto` |
| `VLLM_API_KEY` | `EMPTY` | OpenAI-style API key |
| `EHR_MCP_URL` | `http://127.0.0.1:5103/mcp` | EHR MCP |
| `DATA_PATH` | subset_500 file | evaluation data |
| `OUTPUT_DIR` | `./results/subset_500_<slug>` | results dir |
| `MAX_CONCURRENCY` | 5 | parallel samples |
| `MAX_ROUNDS` | 200 | turn cap |
| `MAX_TOOL_RESULT_CHARS` | 100,000 | truncation |
| `RUNS_PER_QUESTION` | 1 | rollouts per row |
| `ENABLE_THINKING` | 1 | Qwen thinking on/off |
| `TEMPERATURE` | 0.0 | sampling |

Examples:

```bash
# different port, more concurrency, nonzero T
VLLM_BASE_URL=http://127.0.0.1:4001 \
MAX_CONCURRENCY=10 TEMPERATURE=0.6 \
  bash run_test_subset.sh

# 1,800-row EHR-Bench subset
DATA_PATH=../data/EHR-Bench/ehr_bench_sampled_40_per_task.json \
  bash run_test_subset.sh

# custom output dir (avoids clobbering a previous run)
OUTPUT_DIR=./results/exp_thinking_off \
ENABLE_THINKING=0 \
  bash run_test_subset.sh
```

### 3.2 Launcher: `run.sh` (Bedrock backend, Claude)

If you want Claude on AWS Bedrock instead of a local vLLM:

```bash
cd openresearcher_ehr
BEDROCK_API_KEY=<token> bash run.sh
```

Defaults:

| Key | Default |
|-----|---------|
| model | `us.anthropic.claude-opus-4-6-v1` |
| region | `us-east-1` |
| concurrency | 12 |
| runs per question | 4 |
| thinking | off |

Or export the standard AWS credential chain (IAM role, `AWS_*` env
vars, `~/.aws/credentials` …); `boto3` picks them up automatically.

### 3.3 Under the hood: `deploy_agent.py`

Single async main:

```python
1. parse args
2. build generator
     bedrock → BedrockAsyncGenerator
     vllm    → VLLMOpenAIAsyncGenerator  (OpenAI SDK, async-wrapped via ThreadPoolExecutor)
3. BrowserPool(search_url, backend)
4. EHRToolPool(mcp_url)
5. load_query_data(args.data_path)
6. for run in 1..runs_per_question:
       asyncio.gather(process_query_item(item) for item in data)
```

Concurrency is bounded by `asyncio.Semaphore(max_concurrency)`. Every
completed row is appended to `results.jsonl` under an `asyncio.Lock` —
interrupting mid-run loses only the in-flight samples.

Full flag set:

| Flag | Default | Meaning |
|------|---------|---------|
| `--backend` | `bedrock` | `bedrock` or `vllm` |
| `--model_name_or_path` | Claude Opus 4.6 | model id / path |
| `--api_base_url` | `http://127.0.0.1:4000` | vLLM base URL |
| `--api_key` | `EMPTY` | vLLM API key |
| `--enable_ehr` | flag | register `ehr.*` tools |
| `--ehr_mcp_url` | `http://127.0.0.1:5003/mcp` | EHR MCP URL |
| `--max_rounds` | 200 | hard cap on tool rounds |
| `--max_concurrency` | 12 | parallel samples |
| `--runs_per_question` | 1 | rollouts per row |
| `--max_tool_result_chars` | 100,000 | per-tool truncation |
| `--temperature` | 1.0 | sampling |
| `--enable_thinking` | flag | Qwen/Opus extended thinking |
| `--data_path` | required | JSON or JSONL |
| `--output_dir` | `./results` | output location |
| `--search_url` / `--browser_backend` | `http://localhost:8001` / `local` | browser config |

### 3.4 Tool catalogue (19 tools)

Schemas defined in `openresearcher_ehr/data_utils.py`
(`COMBINED_TOOL_CONTENT_FULL`). The system prompt
(`DEVELOPER_CONTENT_CLAUDE`) wires them up and requires
`ehr.load_ehr` first.

**Browser (3)** — `BrowserPool` per-qid sessions, local or Serper
backend:

| Tool | Purpose |
|------|---------|
| `browser.search` | web search, top-N results |
| `browser.open`   | open a URL |
| `browser.find`   | literal in-page find |

**EHR (16)** — routed through `EHRToolPool` (JSON-RPC over HTTP to
the MCP server, with SSE and plain-JSON response parsing):

| Tool | Purpose |
|------|---------|
| `ehr.load_ehr` | **must be called first**; loads the patient DB |
| `ehr.get_table_names` | list tables |
| `ehr.get_column_names` | columns for a table |
| `ehr.get_table_description` | table + schema blurb |
| `ehr.get_unique_values` | distinct values in a column |
| `ehr.get_records_by_time` | query by time range |
| `ehr.get_event_counts_by_time` | counts in window |
| `ehr.get_latest_records` | latest-N records |
| `ehr.get_records_by_keyword` | keyword search over text columns |
| `ehr.get_records_by_value` | exact-value filter |
| `ehr.run_sql_query` | raw SQL |
| `ehr.get_candidates_by_keyword` | keyword search in candidate table |
| `ehr.get_candidates_by_fuzzy_matching` | fuzzy match candidates |
| `ehr.get_candidates_by_semantic_similarity` | BioLORD cosine search |
| `ehr.think` | scratchpad (no-op) |
| `ehr.finish` | **submit answer + terminate** |

MCP session lifecycle (per qid):
```
init_session(qid)   → JSON-RPC initialize + notifications/initialized
call_tool(qid, …)   → tools/call
cleanup(qid)        → clear_session_ehr + release session
```

On a "please call `load_ehr` first" error, `EHRToolPool` auto-reloads
and retries once.

### 3.5 Core action loop (`run_one_native`)

```python
messages = [
    {"role": "system", "content": DEVELOPER_CONTENT_CLAUDE},
    {"role": "user",   "content": question},
]
tools = json.loads(COMBINED_TOOL_CONTENT_FULL)

while round_num < max_rounds:
    round_num += 1
    resp = await generator.chat_completion(
        messages=messages, tools=tools, tool_choice="auto",
        temperature=temperature, max_tokens=8192)

    msg = resp["choices"][0]["message"]
    tool_calls = normalize_tool_calls(msg.get("tool_calls", []))
    messages.append(msg)

    if not tool_calls:
        break

    for call in tool_calls:
        name = normalize_tool_call_name(call["function"]["name"])
        args = call["function"]["arguments"]
        if name.startswith("ehr."):
            result = await ehr_pool.call_tool(qid, short_name, args)
        else:
            result = await browser_pool.call_tool(qid, short_name, args)
        messages.append({"role":"tool","tool_call_id":...,"content":truncate(result)})
        if short_name == "finish":
            finish_called = True

    if finish_called: break
```

Termination conditions:
- `ehr.finish` called → success
- `round_num >= max_rounds` (default 200)
- model returns no tool calls
- tool or LLM exception

**Tool-name normalisation** handles the variants open-source models
emit:

```
browser_search, search          → browser.search
ehr_load_ehr, load_ehr          → ehr.load_ehr
get_records_by_time             → ehr.get_records_by_time
```

### 3.6 Tool-call parsing (vLLM)

vLLM's OpenAI-compatible path returns structured `tool_calls` when
started with `--enable-auto-tool-choice --tool-call-parser <parser>`.
If a model emits tool calls inline in `content` or `reasoning_content`
instead, `VLLMOpenAIAsyncGenerator._convert_response_to_openai` walks
through these fallbacks **in priority order, first-match wins**, first
over `content`, then over `reasoning_content`:

| Prio | Format | Extractor | Example |
|-----:|--------|-----------|---------|
| 0 | native tool_calls | OpenAI SDK | — |
| 1 | XML `<tool_call>` block | `_extract_xml_tool_calls` | `<tool_call><function=fn><parameter=k>v</parameter></function></tool_call>` |
| 2 | naked XML | `_extract_naked_xml_function_calls` | `<function=fn><parameter=k>v</parameter></function>` |
| 3 | inline function | `_extract_inline_function_calls` | `<function=fn({"k":"v"})` |
| 4 | JSON + `</tool_call>` | `_extract_json_tool_calls` | `{"name":"fn","arguments":{...}}</tool_call>` |
| 5 | brackets | `_extract_bracket_tool_calls` | `[Tool Call: fn({"k":"v"})]` |

OpenSeeker models go down a separate `completions` (not
`chat.completions`) path with a bespoke chat template
(`openresearcher_ehr/openseeker_vllm/chat_template.jinja`) and parser
(`_extract_openseeker_tool_calls_repo_like`):

```
<tool_calls_begin>
<tool_call>
{"name":"fn","arguments":{...}}
</tool_call>
</tool_calls_end>
```

Field aliases accepted: `tool_name`/`name`, `tool_args`/`arguments`.

### 3.7 Messages sent to the model

#### Path A — non-OpenSeeker (OpenAI SDK `chat.completions`)

`_prepare_messages` renames `reasoning_content` → `reasoning` (vLLM
schema), normalises `tool_calls.arguments` to JSON strings, and sets
assistant `content` to `None` when tool_calls are present:

```python
{"role":"system","content":"<sys>"}
{"role":"user","content":"<q>"}
{"role":"assistant","content": None,
 "tool_calls":[{"id":"call_1","type":"function",
                "function":{"name":"ehr.load_ehr","arguments":"{...}"}}],
 "reasoning":"<think>"}
{"role":"tool","content":"<result>","tool_call_id":"call_1"}
```

#### Path B — OpenSeeker (`completions` + Jinja)

```
<|im_start|>system
<sys>

# Tools
...JSON tool defs...
<|im_end|>
<|im_start|>user
<q><|im_end|>
<|im_start|>assistant
<think>
<reasoning>
</think>

<tool_call>
{"name":"ehr.load_ehr","arguments":{...}}
</tool_call><|im_end|>
<|im_start|>user
<tool_response>
EHR loaded successfully
</tool_response><|im_end|>
<|im_start|>assistant
<think>
```

Notes:
- Tool results become `user` messages wrapped in `<tool_response>`.
- Consecutive tool results merge into a single `user` block.
- Generation prompt always ends with `<|im_start|>assistant\n<think>\n`.
- `tool_call_id` is a random UUID (parser-generated).

### 3.8 Output files

```
results/subset_500_<slug>/
├── results.jsonl            # one JSON per sample; each line is:
│                            # {qid, run_index, session_id, question,
│                            #  messages, completed, status, stop_reason,
│                            #  subject_id, task, ground_truth}
└── run_test_subset_*.log
```

`stop_reason` values, set by `summarize_conversation_completion()`:

| Value | Meaning |
|-------|---------|
| `finish_tool_call` | last assistant msg called `ehr.finish` (success) |
| `exact_answer_text_*` | parsed `"exact answer:"` / `"confidence:"` markers in text |
| `no_final_answer` | neither signal; treated as incomplete |

> `run_test_subset.sh` opens `results.jsonl` in `w` mode — no resume.
> Back up the file (or switch `OUTPUT_DIR`) before re-running.

### 3.9 Per-sample timing diagram

```
┌──────────────┐   ┌───────────┐   ┌──────────────┐   ┌──────────────┐
│ deploy_agent │   │ Generator │   │  EHRToolPool │   │ BrowserPool  │
│  (scheduler) │   │  (LLM)    │   │  (MCP client)│   │  (search)    │
└──────┬───────┘   └─────┬─────┘   └──────┬───────┘   └──────┬───────┘
       │  init session                         (JSON-RPC initialize → MCP)
       │
       │  messages=[system,user]
 ┌─────┤  round 1
 │     │  chat_completion → tool_calls=[ehr.load_ehr]
 │     │  call_tool(ehr.load_ehr) → "EHR loaded"
 ├─────┤  round 2: [ehr.get_table_names]
 │     │  ...
 ├─────┤  round k: [browser.search]
 │     │  ...
 ├─────┤  round N: [ehr.finish(["Dx A","Dx B"])]
 └─────┤  finish_called → break
       │  cleanup + append results.jsonl
```

---

## 4. Scoring: `helper/evaluate_results.py`

Reads `results.jsonl`, reconstructs predictions from `ehr.finish`
arguments, diffs against `ground_truth` with case-insensitive set
matching.

```bash
python openresearcher_ehr/helper/evaluate_results.py \
    --results   openresearcher_ehr/results/<exp_name> \
    --benchmark data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json
```

`--results` accepts either a `results.jsonl` file or its parent
directory.

For EHR-Bench sampled subsets:

```bash
python openresearcher_ehr/helper/evaluate_results.py \
    --results   openresearcher_ehr/results/ehrbench_1800_<slug> \
    --benchmark data/EHR-Bench/ehr_bench_sampled_40_per_task.json
```

Flags:

| Flag | Meaning |
|------|---------|
| `--extract-text-answer-without-finish` | fall back to text parsing when no `ehr.finish` |
| `--output scores.json` | also write a JSON with per-row breakdowns |

> `--benchmark` must match the `DATA_PATH` used during evaluation —
> qids are `{task}_{subject_id}`.

Output:

```
Task                   Total  Done  Runs    Prec     Rec      F1  ToolAvg  Brows%
--------------------------------------------------------------------------------
diagnoses_ccs            500   500   500  0.4391  0.6371  0.4773     43.5    8.9%
labevents                500   500   500  0.3311  0.7737  0.4407     83.0    9.6%
...
--------------------------------------------------------------------------------
Overall                 3000  3000  3000  0.2423  0.6447  0.3192     54.8    9.6%
```

Columns:

| Column | Meaning |
|--------|---------|
| Total | samples in the benchmark for that task |
| Done | samples with a result line |
| Runs | Done × `runs_per_question` |
| Prec / Rec / F1 | averaged, case-insensitive set match |
| ToolAvg | mean tool calls per run |
| Brows% | fraction of tool calls that were browser tools |

For EHR-Bench (has a `task_type` field on each row), the script also
breaks down by `risk_prediction` vs `decision_making` in addition to
per-task.

---

## 5. Errors & gotchas

### MCP connection refused
- **Symptoms:** `Error calling MCP tool`, `Connection refused`.
- **Checks:** MCP terminal logs; `lsof -i :5103`; the MCP port in
  `run_mcp_server.sh` must match `EHR_MCP_URL` in `run_test_subset.sh`.

### vLLM connection refused
- **Symptoms:** `No served models reported by vLLM`, `Connection refused`.
- **Checks:** wait for `Uvicorn running on ...`; `curl
  http://127.0.0.1:4000/v1/models`; `VLLM_BASE_URL` port must match the
  vLLM startup port.

### OOM on vLLM start
- `GPU_MEMORY_UTILIZATION=0.7 bash scripts/run/run_vllm_server_3_5.sh`
- reduce `--max-model-len`
- add tensor-parallel cards

### Resume after interruption
- Not supported: `run_test_subset.sh` opens `results.jsonl` in `w`
  mode.
- Workaround: back up the partial `results.jsonl`, then either shrink
  `DATA_PATH` to the remaining rows or re-run the whole set into a
  fresh `OUTPUT_DIR`.

### Score-time qid mismatch
- `evaluate_results.py` matches on `{task}_{subject_id}`.
- Ensure `--benchmark` points at the exact JSON used as `DATA_PATH`
  during evaluation.

### All runs end `no_final_answer`
- `max_rounds` too small, or the model isn't emitting tool calls.
- Double-check thinking-mode flag vs model expectations; verify tool
  parser / chat template for the model.

### MIMIC-IV build warnings
- `"Directory not found: .../ed"` — ED data is optional; fine for
  AgentEHR-Bench.
- Columns stored as `TEXT` — use `CAST(... AS INTEGER)` etc. in SQL.
- Missing discharge note → `admissions.text` is empty but the patient
  is kept (earlier versions used to drop them).

---

## 6. Recommended order

```bash
# 1. venv + deps                           (§2.1)
uv venv venv/gemma --python 3.12
source venv/gemma/bin/activate
uv pip install -r requirements.txt                    --python venv/gemma/bin/python
uv pip install -r openresearcher_ehr/requirements.txt --python venv/gemma/bin/python
uv pip install "vllm>=0.19.0" "transformers>=5.5"     --python venv/gemma/bin/python

# 2. data — pick one of
hf download --repo-type dataset BlueZeros/AgentEHR-Bench --local-dir data/AgentEHR-Bench   # §1.2
# OR
huggingface-cli download Letian2003/Bb21385 --repo-type dataset --local-dir data           # §1.1

# 3. MCP server                            (§2.2)
bash scripts/run/run_mcp_server.sh

# 4. vLLM server                           (§2.3)  — or skip for Bedrock
bash scripts/run/run_vllm_server_3_5.sh

# 5. evaluate                              (§3.1)
cd openresearcher_ehr && bash run_test_subset.sh

# 6. score                                 (§4)
python openresearcher_ehr/helper/evaluate_results.py \
    --results   openresearcher_ehr/results/<exp_name> \
    --benchmark data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json
```

For Bedrock Claude instead of vLLM, skip step 4 and use
`bash run.sh` in step 5.

---

## 7. File inventory

```
$REPO/
├── data/
│   ├── AgentEHR-Bench/MIMICIVAgentBench/…              # BlueZeros dataset
│   ├── EHR-Bench/                                      # Letian2003/Bb21385 release
│   │   ├── ehr_bench_merged_filtered.json
│   │   ├── ehr_bench_sampled_{20,40}_per_task.json
│   │   └── database/patient_<id>.db
│   └── MIMIC-IV/mimic_iv/{hosp,icu,note,ed}/           # optional raw
├── helper/
│   ├── reverse_match.py                                # §1.3.1
│   ├── patient_event2db.py                             # §1.3.2
│   └── sample_per_task.py                              # §1.4
├── src/
│   ├── run_mcp_server.py                               # EHR MCP entrypoint
│   └── agentlite/
│       ├── mcp_tools/                                  # 16 ehr.* tools
│       └── commons/EHRManager.py                       # load_ehr_for_sample
├── scripts/run/
│   ├── run_mcp_server.sh
│   ├── run_vllm_server.sh              (OpenSeeker)
│   ├── run_vllm_server_3_5.sh          (Qwen3.5-35B-A3B, default)
│   ├── run_vllm_server_Nemotron.sh     (OpenResearcher)
│   ├── run_vllm_server_Meissa_4B.sh
│   └── run_vllm_server_gemma4.sh
├── openresearcher_ehr/
│   ├── run_test_subset.sh                              # vLLM launcher
│   ├── run.sh                                          # Bedrock launcher
│   ├── deploy_agent.py                                 # main driver
│   ├── data_utils.py                                   # prompts + tool schemas
│   ├── ehr_pool.py                                     # MCP client pool
│   ├── vllm_generator.py
│   ├── bedrock_generator.py
│   ├── browser.py
│   ├── openseeker_vllm/{chat_template.jinja,tool_parser.py}
│   └── helper/evaluate_results.py                      # §4 scorer
└── docs/
    ├── data_prepare.md
    ├── agent_execution.md
    ├── evaluation.md
    ├── ehr_benchmark_evaluation.md                     # this file
    └── multimodal_ehr_benchmark_evaluation.md
```
