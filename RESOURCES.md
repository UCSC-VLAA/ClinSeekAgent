# Resources

This file records the external artifacts needed to reproduce or inspect the
ClinSeek release. Large data, model, result, and analysis artifacts are not
stored in this Git repository.

## Artifact Inventory

| Category | Location | Access | Approx size |
| --- | --- | --- | --- |
| Code | this public GitHub repository | public after release | small |
| Benchmark inputs and DBs | `huggingface.co/datasets/UCSC-VLAA/ClinSeek-Bench` | controlled access | 2.6 GB |
| Evaluation results | `huggingface.co/datasets/UCSC-VLAA/ClinSeek-Evaluation-Results` | public dataset | 11.1 GB |
| Model weights | Hugging Face model repository, TBD | public or controlled, depending on release decision | TBD |
| Training trajectories | Hugging Face dataset repository, TBD | controlled until cleared | TBD |

## Code Entry Points

Agentic evaluation:

- `openresearcher_ehr/deploy_agent.py` - text-only EHR benchmark driver.
- `openresearcher_ehr/deploy_agent_mm.py` - multimodal benchmark driver.
- `src/run_mcp_server.py` - EHR MCP server.
- `src/mcp_image/run_image_mcp_server.py` - image MCP server.

One-shot baseline evaluation:

- `openresearcher_ehr/deploy_reasoning_model.py` - text-only one-shot driver.
- `openresearcher_ehr/deploy_reasoning_model_mm.py` - multimodal one-shot driver.

Public launchers:

- `scripts/run_ehr_mcp.sh`
- `scripts/run_image_mcp.sh`
- `scripts/run_text_eval.sh`
- `scripts/run_mm_eval.sh`
- `scripts/run_vllm_server.sh`

Training:

- `prepare_clinseek_data.py` - trajectory JSONL to parquet preparation.
- `scripts/train_sft.sh` - public SFT launcher using the vendored `verl/`.
- `verl/examples/sft/clinseek/` - direct VERL SFT recipes.
- `convert_dist_ckpt_to_hf.py` - distributed checkpoint conversion.

## ClinSeek-Bench

Hugging Face dataset:

```text
UCSC-VLAA/ClinSeek-Bench
```

The benchmark package is expected to contain the exact inputs and local assets
needed for end-to-end evaluation: benchmark manifests, per-patient SQLite DBs,
candidate/reference tables, table descriptions, linked chest X-ray files, and
radiology reports.

Expected release layout:

```text
ClinSeek-Bench/
+-- README.md
+-- inputs/
|   +-- ehr_bench.json
|   +-- mm_bench.jsonl
+-- data/
    +-- ehr_bench/{database,table_description}/
    +-- mm_bench/{ehrxqa,medmod}/{database,mimic-cxr,table_description}/
```

Expected splits:

| Split | Data points | Modality | Bundled assets |
| --- | ---: | --- | --- |
| `ehr_bench` | 1,800 | text-only | patient DBs and candidate tables |
| `mm_bench` | 989 | text plus chest X-ray | patient DBs, CXR images, and radiology reports |

`mm_bench` is the image-grounded multimodal evaluation track. Text-only rows
from upstream multimodal sources are not part of this public multimodal track.

## Evaluation Results

Hugging Face dataset:

```text
UCSC-VLAA/ClinSeek-Evaluation-Results
```

The result package is expected to contain scored predictions for the paper's
model, benchmark, and evaluation-mode combinations. The canonical paper-facing
layout is the hierarchical scored output:

```text
ClinSeek-Evaluation-Results/
+-- README.md
+-- consolidated result spreadsheet
+-- ehr_bench/
+-- mm_bench/
+-- hierarchical/
    +-- agentic/{ehr_bench,mm_bench}/<model>/L1.<modality>/L2.<family>/L3.<sub>/L4.<task>/
    |   +-- rows.jsonl
    |   +-- summary.json
    +-- oneshot/{ehr_bench,mm_bench}/<model>/...
```

This code release keeps the public scoring utilities, but does not include the
large scored-result trees directly in Git.

## Download Commands

```bash
# Benchmark inputs and DBs. Access may require membership or approval.
hf download UCSC-VLAA/ClinSeek-Bench \
  --repo-type dataset \
  --local-dir data/ClinSeek-Bench

# Published evaluation outputs.
hf download UCSC-VLAA/ClinSeek-Evaluation-Results \
  --repo-type dataset \
  --local-dir results/ClinSeek-Evaluation-Results
```

After downloading the benchmark package, point the public launch scripts at the
prepared tree:

```bash
export CLINSEEK_DATA_ROOT=$PWD/data/ClinSeek-Bench/data
export EHR_DATA_PATH=$CLINSEEK_DATA_ROOT/ehr_bench
export BENCH_ROOT=$CLINSEEK_DATA_ROOT/mm_bench
```

## Release Boundary

The following artifacts remain outside Git:

- raw MIMIC-IV tables and MIMIC-CXR images;
- generated patient SQLite databases;
- benchmark parquet/JSONL data files larger than synthetic examples;
- model weights and training checkpoints;
- private trajectories and raw tool observations;
- internal analysis snapshots, qualitative case files, and HTML trajectory viewers;
- experiment logs and local infrastructure paths.
