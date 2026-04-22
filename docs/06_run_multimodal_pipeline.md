# Running the multimodal EHR tool-calling pipeline

This doc tells you how to take the multimodal pipeline in
`openresearcher_ehr/` from cold machine to a finished `results.jsonl` for
either (a) the prepared 2,703-row mixed test set or (b) the full EHRXQA +
MedMod test sets.

> The existing text-only pipeline (`deploy_agent.py`, `run.sh`) is not
> touched — everything here lives in parallel files (`deploy_agent_mm.py`,
> `data_utils_mm.py`, `image_pool.py`, `run_mm_pipeline.sh`, …) plus
> `src/mcp_image/`. See
> `docs/05_multimodal_bench_ehr_multimodal_bench.md` for the data-side
> background and the code plan in `/root/.claude/plans/ok-i-need-to-shimmying-whistle.md`.

---

## 1. Inputs & prerequisites

| What | Where |
|---|---|
| Bench root — EHRXQA | `/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3/` |
| Bench root — MedMod | `/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3/` |
| **Prepared test set (2,703 rows)** | `/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/combined_test_set.jsonl` |
| Full EHRXQA test (2,220 rows) | `…/EHRXQAAgentBench_v3/common/ready/test.json` |
| Full MedMod test (392,811 rows) | `…/MedModAgentBench_v3/common/ready/test.json` |
| Pipeline code | `/fsx-shared/juncheng/EHR/openresearcher_ehr/` |
| Image MCP server | `/fsx-shared/juncheng/EHR/src/mcp_image/` |
| **Main venv** (agent + EHR MCPs) — has boto3, fastmcp, gpt_oss, PIL, sentence_transformers, transformers 4.57 | `/fsx-shared/juncheng/OpenResearcher/.venv/bin/python` |
| **Image MCP venv** (dedicated, pinned `transformers==4.46.3` for MAIRA-2) | `/fsx-shared/juncheng/EHR/src/mcp_image/.venv/bin/python` |

Prepared test set composition (from `combined_test_set.stats.json`):

| Source | Task | Rows |
|---|---|---:|
| MedMod (125 each, seed 42) | `medmod_radiology` | 125 |
| | `medmod_decompensation` | 125 |
| | `medmod_phenotyping` | 125 |
| | `medmod_in_hospital_mortality` | 125 |
| EHRXQA (all test rows) | `ehrxqa_table` | 1,706 |
| | `ehrxqa_image` | 497 |
| **Total** | | **2,703** |

Cohort-scope EHRXQA rows (626 of the 1,706 `ehrxqa_table`) have no patient EHR
to load; image-less rows get no image content block. The pipeline respects both
— it never force-attaches an image or force-loads an EHR.

---

## 2. What the pipeline runs under the hood

```
┌─────────────────────────────────────────────────────────────────────────┐
│ deploy_agent_mm.py                                                      │
│   ├─ system prompt  = data_utils_mm.DEVELOPER_CONTENT_CLAUDE_MM         │
│   ├─ tools          = data_utils_mm.COMBINED_TOOL_CONTENT_MM            │
│   │                    (20 EHR + 3 browser + 6 image = 29 tools)         │
│   ├─ generator      = bedrock_generator.BedrockAsyncGenerator           │
│   │                    (Anthropic Messages API, Opus 4.6 by default)    │
│   └─ per-sample flow                                                    │
│        1. build user content: text + base64 images + inlined reports    │
│           (auto-skipped when the sample has none)                       │
│        2. prepend a <run_context> preamble stating exactly what is      │
│           attached & which tool preconditions hold                      │
│        3. multi-round tool loop — routes ehr.* by source_benchmark      │
│           (ehrxqa → :5103, medmod → :5104), image.* → :5203              │
└─────────────────────────────────────────────────────────────────────────┘

MCP services (all localhost):
  :5103  EHR MCP (EHRXQA root)   — mirrors src/run_mcp_server.py
  :5104  EHR MCP (MedMod root)   — same server, different --data_path
  :5203  Image MCP               — src/mcp_image/run_image_mcp_server.py
                                   registers 6 Meissa-ported tools
```

### 2.1 Model / run constants (defaults)

| Setting | Default | Where |
|---|---|---|
| Bedrock model | `us.anthropic.claude-opus-4-6-v1` | `deploy_agent_mm.py:DEFAULT_BEDROCK_MODEL_ID` |
| Bedrock region | `us-east-1` | `deploy_agent_mm.py:DEFAULT_BEDROCK_REGION` |
| `max_rounds` | **200** | `run_mm_pipeline.sh:MAX_ROUNDS` |
| `max_concurrency` | 6 (capped at 12 globally) | `run_mm_pipeline.sh:MAX_CONCURRENCY` |
| `runs_per_question` | 1 | `run_mm_pipeline.sh:RUNS_PER_QUESTION` |
| `max_tool_result_chars` | 100,000 | `run_mm_pipeline.sh:MAX_TOOL_RESULT_CHARS` |
| `image_max_edge` | 1568 (Anthropic-recommended max) | `run_mm_pipeline.sh:IMAGE_MAX_EDGE` |
| Thinking | off | `--disable_thinking` |

Any of these can be overridden with env vars on the command line.

### 2.2 Auth

The script forwards AWS creds via the default boto3 chain. Two practical options:

- **AWS access keys** (exports work):
  `AWS_ACCESS_KEY_ID=…`, `AWS_SECRET_ACCESS_KEY=…`, optional `AWS_SESSION_TOKEN=…`.
- **Bedrock bearer token**:
  `export BEDROCK_API_KEY=…`  (also aliased as `AWS_BEARER_TOKEN_BEDROCK`).

Browser tools are on but inert unless `SERPER_API_KEY` is exported (then
`browser.search` routes through Serper). Without it, Claude simply doesn't
call browser tools; EHR + image paths work fine alone.

### 2.3 Image tool weights / deps

The image MCP is optional. If you pass `--no-image`, the image tools are
**not** registered in the request schema, so the model can't call them; it
still sees the CXR via the base64 content block.

**Dedicated venv**: the image MCP runs out of its own venv at
`/fsx-shared/juncheng/EHR/src/mcp_image/.venv/` pinned to
`transformers==4.46.3` (MAIRA-2's custom modeling code was forked from
`LlavaForConditionalGeneration` in 4.44 and breaks on 4.50+). The main
pipeline venv keeps transformers 4.57.

Deps already installed in the image venv (no action needed on this host):

```
fastmcp, pydantic, Pillow, numpy, scikit-image, matplotlib, pydicom,
torch==2.9.0+cu128, torchvision==0.24.0+cu128, torchxrayvision>=1.2,
transformers==4.46.3, accelerate, einops, hf_transfer, huggingface_hub,
protobuf, sentencepiece
```

To rebuild from scratch on a new host:

```bash
uv venv /fsx-shared/juncheng/EHR/src/mcp_image/.venv --python 3.12
IMG=/fsx-shared/juncheng/EHR/src/mcp_image/.venv/bin/python
uv pip install --python $IMG \
    fastmcp pydantic Pillow numpy scikit-image matplotlib pydicom \
    torchxrayvision einops accelerate hf_transfer huggingface_hub \
    protobuf sentencepiece "transformers==4.46.3"
uv pip install --python $IMG --reinstall \
    "torch==2.9.0+cu128" "torchvision==0.24.0+cu128" \
    --index-url https://download.pytorch.org/whl/cu128
```

**HuggingFace token** (for MAIRA-2, which is a gated repo): `~/.cache/huggingface/token`
must hold a token from an account that has accepted the MAIRA-2 license at
https://huggingface.co/microsoft/maira-2. `run_mm_pipeline.sh` reads this file
and exports `HF_TOKEN` into the image MCP process.

First call to each tool lazy-downloads its weights (maira-2 ~4 GB,
IAMJB/chexpert-mimic-cxr-findings ~1 GB, torchxrayvision densenet/pspnet
~500 MB) into `$HF_HOME` — plan for ~8 GB disk + a GPU. `run_mm_pipeline.sh`
starts this MCP with `BENCH_ROOT` exported so relative `image_paths` from
both benchmarks resolve. Default GPU is `CUDA_VISIBLE_DEVICES=0`; override
with `IMAGE_CUDA_VISIBLE_DEVICES=<id>` before invoking the launcher.

#### Registered image tools (all verified working)

| Tool | Status | Backing model | Notes |
|---|---|---|---|
| `image.image_visualizer` | ✅ | none | Renders an annotated copy of the image to the artifact dir |
| `image.dicom_processor` | ✅ (code path tested) | pydicom | Converts DICOM → PNG + metadata |
| `image.chest_xray_classifier` | ✅ | torchxrayvision DenseNet121 | 18-pathology probabilities |
| `image.chest_xray_report_generator` | ✅ | IAMJB/chexpert-mimic-cxr-findings + impression ViT-BERT | FINDINGS + IMPRESSION |
| `image.xray_phrase_grounding` | ✅ | microsoft/maira-2 | Returns normalized + image-coord bboxes, saves overlay PNG |
| `image.chest_xray_segmentation` | ✅ | torchxrayvision PSPNet | 14 anatomical structures + per-region metrics |

---

## 3. Ready-to-use launcher — `openresearcher_ehr/run_mm_pipeline.sh`

One script drives everything. It starts the two EHR MCPs and (optionally) the
image MCP, then invokes `deploy_agent_mm.py`.

```bash
cd /fsx-shared/juncheng/EHR/openresearcher_ehr

# (A) Prepared 2,703-row test set — the default.
bash run_mm_pipeline.sh

# (B) Full test sets (EHRXQA 2,220 + MedMod 392,811 back-to-back)
bash run_mm_pipeline.sh --mode full

# (C) Just one of the full sets
bash run_mm_pipeline.sh --mode full-ehrxqa
bash run_mm_pipeline.sh --mode full-medmod

# (D) Smoke test — first N rows
bash run_mm_pipeline.sh --mode prepared --limit 20

# (E) MCPs already running? Don't start / kill them.
bash run_mm_pipeline.sh --no-start-mcp

# (F) Skip the image MCP (routing stays — tools just error out)
bash run_mm_pipeline.sh --no-image
```

### 3.1 CLI options

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `prepared` | `prepared` \| `full` \| `full-ehrxqa` \| `full-medmod` |
| `--limit N` | 0 | Truncate input to the first N rows (useful for smoke tests) |
| `--no-start-mcp` | off | Don't spawn the EHR MCPs — assume they're already up |
| `--no-image` | off | Don't register image tools (implies `--no-start-image`) |
| `--no-start-image` | off | Don't spawn the image MCP (but still let the model call image.* if the MCP is reachable on :5203) |
| `--output-dir DIR` | `./results/mm_<mode>_<utc-timestamp>` | Where `results.jsonl` lands |
| `--data-path PATH` | auto per `--mode` | Point to a custom input file |

### 3.2 Env-var overrides (all optional)

```bash
BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1
BEDROCK_REGION=us-east-1
BEDROCK_API_KEY=...                # or use standard AWS chain
SERPER_API_KEY=...                 # enables browser.search
MAX_ROUNDS=200
MAX_CONCURRENCY=6
RUNS_PER_QUESTION=1
MAX_TOOL_RESULT_CHARS=100000
IMAGE_MAX_EDGE=1568
ENABLE_THINKING=0                  # 1 turns on Opus extended thinking
PYBIN=/path/to/python              # default: /fsx-shared/juncheng/OpenResearcher/.venv/bin/python
IMAGE_PYBIN=/path/to/python        # default: /fsx-shared/juncheng/EHR/src/mcp_image/.venv/bin/python
IMAGE_CUDA_VISIBLE_DEVICES=0       # GPU for the image MCP (main venv is CPU-only on agent side)
HF_TOKEN=...                       # default: read from ~/.cache/huggingface/token
```

### 3.3 Output layout

```
results/mm_<mode>_<UTC>/
├── results.jsonl         # one JSON per sample: messages, multimodal_summary,
│                          # attached source fields from the manifest, ...
├── run.log               # mirrored stdout/stderr of the agent
├── mcp_ehrxqa.log
├── mcp_medmod.log
└── mcp_image.log         # (only when image MCP was started)
```

Resume is automatic: if `results.jsonl` already has `completed: true` rows for
some `qid`s, they are skipped on the next run.

---

## 4. Recommended run recipes

### 4.1 Prepared test set (2,703 rows)

```bash
cd /fsx-shared/juncheng/EHR/openresearcher_ehr
bash run_mm_pipeline.sh \
    --mode prepared \
    --output-dir ./results/mm_prepared
# max_rounds=200 by default (as specified)
```

Estimated cost / time at 6-way concurrency: rough back-of-envelope based on
the smoke-6 observations (~10-40 rounds per sample, 1-4 images per Anthropic
call, Opus 4.6). Expect hours, not minutes — run overnight with `nohup` or
`tmux`.

### 4.2 Full EHRXQA + MedMod test sets

```bash
bash run_mm_pipeline.sh --mode full \
    --output-dir ./results/mm_full
```

This runs two back-to-back sub-jobs:
`results/mm_full/ehrxqa/results.jsonl` (2,220 rows) and
`results/mm_full/medmod/results.jsonl` (392,811 rows).

> MedMod's 392,811 is expensive. Practical tip: run the per-task full manifests
> separately (they are smaller and can be scheduled in parallel on different
> boxes):
>
> ```bash
> for task in medmod_radiology medmod_in_hospital_mortality \
>             medmod_phenotyping medmod_decompensation medmod_length_of_stay; do
>   python /fsx-shared/juncheng/EHR/openresearcher_ehr/prepare_mm_data.py \
>     --src /fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3/common/ready/test.json \
>     --tasks "$task" --scope patient_scope --require_images --require_subject_id \
>     --out "./data_mm/full_${task}.jsonl"
>   bash run_mm_pipeline.sh --mode prepared \
>     --data-path "./data_mm/full_${task}.jsonl" \
>     --output-dir "./results/full_${task}"
> done
> ```

### 4.3 Smoke test (any input)

```bash
bash run_mm_pipeline.sh --mode prepared --limit 5
```

---

## 5. What the agent actually does with each row

For every sample, `deploy_agent_mm.py`:

1. Reads `question`, `image_paths`, `report_paths`, `subject_id`, `scope`,
   `source_benchmark` from the JSONL.
2. Resolves each `image_path` against **both** bench roots via the multi-root
   `resolve_asset_path` helper — MedMod row paths resolve under the MedMod
   root, EHRXQA row paths under the EHRXQA root.
3. Builds an Anthropic-style content list:
   - a `<run_context>` text preamble ("images_attached: N, reports_inlined: M,
     patient_ehr_available: yes|no"),
   - then the pre-rendered `question`,
   - then any loaded images as base64 blocks (max 4, resized to
     `IMAGE_MAX_EDGE` on the longest edge),
   - then any linked radiology reports inlined as additional text blocks.
4. Issues `chat_completion()` through the Bedrock Anthropic API with `tools=
   COMBINED_TOOL_CONTENT_MM` and `tool_choice="auto"`.
5. Routes tool calls:
   - `ehr.*` → the MCP whose port matches `source_benchmark` (`ehrxqa` ↔ :5103,
     `medmod` ↔ :5104).
   - `image.*` → image MCP (:5203).
   - `browser.*` → the existing browser backend (serper / local).
6. Loops until the model calls `ehr.finish` or hits `max_rounds=200`.
7. Writes one JSON line per sample to `results.jsonl` containing:
   `qid`, `subject_id`, `task`, `scope`, original `label`, `messages` (full
   tool-call trace), `multimodal_summary` (how many images/reports were
   actually attached), `completed`, `stop_reason`, and everything from the
   source manifest.

---

## 6. Verification & debugging

| Symptom | Check |
|---|---|
| `ConnectionError: http://127.0.0.1:5103/mcp` | EHR MCPs not up. `ss -lnt | grep -E '5103|5104'`; look at `mcp_ehrxqa.log` / `mcp_medmod.log`. |
| `Error: Image tools not available` | Image MCP off or unreachable. `--no-image` suppresses the tools entirely; otherwise check `mcp_image.log`. |
| `NameError: use_reasoning_content` | Should no longer happen — a runtime shim in `deploy_agent_mm.py` patches this pre-existing bug in `bedrock_generator.py` without editing the text-only file. |
| All samples finish with `stop_reason="no_final_answer"` | Likely `max_rounds` too small or thinking mode misaligned. The default is 200 rounds. |
| Cohort-scope rows call `ehr.load_ehr` anyway | The preamble says `patient_ehr_available: no`, but Claude may still try. Unless you want a hard block, this is best-effort; add a tool guard if required. |
| No image block shows up in `messages[0]` | That row had no `image_paths`, or none of them resolved. The `multimodal_summary.dropped_image_paths` field in the result records what got dropped. |

### Quick health check

```bash
# Does an end-to-end call work?
bash run_mm_pipeline.sh --mode prepared --limit 3 --no-image
tail -n 3 results/mm_prepared_<timestamp>/results.jsonl | jq '.qid, .status'
```

---

## 7. File inventory (what this change added / moved)

New / moved this session:

```
data/
  EHR_multimodal_bench_tests/
    combined_test_set.jsonl          # 2,703 prepared test rows (moved here)
    combined_test_set.stats.json

openresearcher_ehr/
  deploy_agent_mm.py                 # multimodal driver (new file)
  data_utils_mm.py                   # image tool schemas + mm system prompt
  image_pool.py                      # MCP client for image tools
  prepare_mm_data.py                 # slice / sample filter
  run_mm.sh                          # earlier minimal launcher (kept for reference)
  run_mm_pipeline.sh                 # primary launcher — described above
  results/mm_smoke5/                 # prior smoke run, 5 rows
  results/mm_mix6/                   # prior smoke run, 6 mixed-shape rows

src/mcp_image/
  run_image_mcp_server.py            # FastMCP HTTP server
  fastmcp_app.py
  tools/
    __init__.py
    base.py
    image_visualizer.py
    dicom_processor.py
    chest_xray_classifier.py
    chest_xray_report_generator.py
    xray_phrase_grounding.py
    chest_xray_segmentation.py
  requirements_image_mcp.txt

docs/
  05_multimodal_bench_ehr_multimodal_bench.md
  06_run_multimodal_pipeline.md      # (this file)
```

Not touched:

```
openresearcher_ehr/deploy_agent.py    # text-only pipeline entry
openresearcher_ehr/run.sh             # text-only launcher
openresearcher_ehr/data_utils.py
openresearcher_ehr/ehr_pool.py
openresearcher_ehr/browser.py
openresearcher_ehr/bedrock_generator.py
openresearcher_ehr/vllm_generator.py
src/run_mcp_server.py
```
