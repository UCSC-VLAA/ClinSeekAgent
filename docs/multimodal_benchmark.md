# ClinSeek-Bench-MM

ClinSeek-Bench-MM has 989 image-grounded questions built from EHRXQA (497) and
MedMod (492), both of which pair MIMIC-IV EHRs with MIMIC-CXR chest
radiographs. ClinSeekAgent answers them with 16 EHR tools and 6 chest X-ray
tools (22 in total); the paired Curated Input setting sends the rendered EHR
context plus up to four linked images and the linked report text in one prompt.

The Hugging Face manifest (`ClinSeek-Bench_multimodal.jsonl`) is source-only:
it does not redistribute patient databases, CXR images, report text, or the
rendered EHR context. Rebuild those locally with
[`ClinSeek-Bench_multimodal_data_prepare.md`](ClinSeek-Bench_multimodal_data_prepare.md).

## Manifest

Each row carries the question, labels, and the trusted metadata that bounds
what the model can retrieve:

| field | used for |
| --- | --- |
| `qid`, `task`, `category`, `source_benchmark` | identity; routing to the EHRXQA or MedMod EHR server; per-category reporting |
| `question` | the task prompt (instruction, patient/CXR identifiers, question); contains no EHR rows |
| `ground_truth`, `answer_type` | scoring |
| `subject_id`, `hadm_id`, `stay_id`, `prediction_time` | injected into every `ehr.load_ehr` call, overriding whatever the model passed |
| `study_ids`, `dicom_ids`, `image_paths`, `report_paths` | CXR allowlist: only these studies survive the `tb_cxr` filter and only these images are reachable through `image.*` |
| `scope`, `modalities` | patient- vs cohort-scope handling; image-only tasks get no longitudinal EHR |
| `snapshot_spec`, `leakage_policy_version` | the temporal contract (`multimodal-strict-temporal-scope-v1`) |

`multimodal_manifest_policy.validate_agentic_manifest` raises before the first
API call if `subject_id`, `prediction_time`, `scope`, `modalities`, or (CXR
tasks) `study_ids` is missing. `input_text` (Curated Input only) is rendered
locally by `scripts/data_build/materialize_multimodal_strict_manifest.py`.

Task labels and the paper's six categories:

| `task` | category |
| --- | --- |
| `ehrxqa_image` | CXR finding presence / CXR finding enumeration / CXR change comparison (per question; see `clinseekagent/resources/task_taxonomy.json`) |
| `medmod_radiology` | CXR finding enumeration |
| `medmod_decompensation` | Clinical deterioration |
| `medmod_in_hospital_mortality` | Mortality |
| `medmod_phenotyping` | Phenotype |

## Run ClinSeekAgent

Start **one EHR MCP server per source benchmark** (the two use different
schemas and share subject ids) and the image server. Give the servers no
temporal-policy override: `EHRManager` detects the EHRXQA/MedMod schemas in
its default mode, which is where the strict CXR scoping lives.

```bash
export MM=/path/to/ClinSeek-MM-Bench/data/mm_bench
EHR_DATA_PATH=$MM/ehrxqa EHR_MCP_PORT=5004 bash scripts/run_ehr_mcp.sh &
EHR_DATA_PATH=$MM/medmod EHR_MCP_PORT=5005 bash scripts/run_ehr_mcp.sh &
BENCH_ROOT=$MM bash scripts/run_image_mcp.sh &

DATA_PATH=/path/to/ClinSeek-Bench/ClinSeek-Bench_multimodal.jsonl \
BENCH_ROOT=$MM/ehrxqa:$MM/medmod \
OUTPUT_DIR=outputs/mm_agentic \
BACKEND=vertex MODEL_NAME_OR_PATH=claude-opus-5 \
bash scripts/run_mm_eval.sh
```

Defaults reproduce the paper setting (EHR + image tools, up to 200 rounds, up
to 4 images per question with longest edge 1,568 px, reasoning on).
`ENABLE_IMAGE=0` runs the EHR-only tool-space ablation; `ENABLE_BROWSER=1`
adds the optional browser tools; `RETRIEVAL_MODE=single_shot` runs
Single-Pass Retrieval.

The image MCP needs the Hugging Face cache holding its models, including the
gated `microsoft/maira-2` used by `xray_phrase_grounding` (export `HF_HOME`),
and enough GPU memory (MAIRA-2 alone needs about 26 GB). Pin the models with
`IMAGE_TOOL_DEVICE` or `IMAGE_TOOL_DEVICE_<CLASSIFIER|GROUNDING|REPORT_GENERATOR|SEGMENTATION>`.

## Run Curated Input / Full-Record Input

```bash
python scripts/data_build/materialize_multimodal_strict_manifest.py \
  --manifest /path/to/ClinSeek-Bench/ClinSeek-Bench_multimodal.jsonl \
  --ehrxqa-root $MM/ehrxqa --medmod-root $MM/medmod \
  --output data/ClinSeek-Bench_multimodal_strict.jsonl
```

then build the corpus and run `clinseekagent/run_multimodal_curated.py
--corpus-root ... --view recent|full` as described in
[`ClinSeek-Bench_text_curated_input_evaluation.md`](ClinSeek-Bench_text_curated_input_evaluation.md#multimodal).

## Score

```bash
python scripts/check_results_complete.py outputs/mm_agentic/results.jsonl --expect 989
python clinseekagent/scoring/score_multimodal.py \
  --results outputs/mm_agentic/results.jsonl \
  --output-root outputs/mm_agentic/scores --backend vertex
```

Single-label questions are judged by Claude Sonnet 4.6 (`--backend bedrock`,
`vertex`, or `vllm` for any OpenAI-compatible endpoint with `--judge-model`);
multi-label questions are mapped to the task vocabulary with
`all-MiniLM-L6-v2` (cosine threshold 0.55) before set F1. `summary.json`
reports the unified F1 overall, per task, and per category
(`per_category_unified`).
