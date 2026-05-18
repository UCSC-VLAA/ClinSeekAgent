# Multimodal Benchmark

The multimodal pipeline adds native image inputs and image MCP tools to the text pipeline. It supports EHRXQA-style question answering and MedMod-style prediction tasks when manifests provide resolvable image/report paths.

## Input Manifest

```json
{
  "qid": "mm_sample_id",
  "source_benchmark": "ehrxqa",
  "subject_id": "10000032",
  "prediction_time": "2150-12-01 10:00:00",
  "question": "Clinical question...",
  "image_paths": ["relative/or/absolute/image.jpg"],
  "report_paths": ["relative/or/absolute/report.txt"],
  "ground_truth": ["answer"]
}
```

Set `BENCH_ROOT` to the directory used to resolve relative image and report paths.

## Run

```bash
EHR_DATA_PATH=$CLINSEEK_DATA_ROOT/multimodal/ehrxqa bash scripts/run_ehr_mcp.sh
BENCH_ROOT=$CLINSEEK_DATA_ROOT/multimodal bash scripts/run_image_mcp.sh

DATA_PATH=$CLINSEEK_DATA_ROOT/multimodal/test.jsonl \
OUTPUT_DIR=outputs/mm_eval \
bash scripts/run_mm_eval.sh
```

Use `--enable_image` only when the image MCP server and model dependencies are available.

## Score

```bash
python openresearcher_ehr/helper/scorer_mm.py \
  --results outputs/mm_eval/results.jsonl \
  --output-root outputs/mm_eval/scored
```
