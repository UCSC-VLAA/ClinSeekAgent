# Multimodal Benchmark

The multimodal pipeline adds native image inputs and image MCP tools to the text
pipeline. It supports EHRXQA-style question answering and MedMod-style
prediction tasks when manifests provide resolvable image/report paths.

The released ClinSeek-Bench multimodal split is source-only on Hugging Face. It
contains the 989-row `inputs/mm_bench.jsonl` manifest, but it does not
redistribute protected MIMIC-derived patient databases, CXR JPG files, or report
text. The reconstruction scripts live in this GitHub repository under
`scripts/data_build/`; users rebuild the assets locally from official source
downloads.

The multimodal reconstruction workflow is documented in
[`docs/ClinSeek-Bench_multimodal_data_prepare.md`](ClinSeek-Bench_multimodal_data_prepare.md).

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

## Validate Download

```bash
python scripts/data_build/validate_multimodal_release.py \
  --bench-root "$CLINSEEK_BENCH_ROOT" \
  --manifest-only
```

After local reconstruction, validate the rebuilt runtime package without
`--manifest-only`.

## Run

```bash
EHR_DATA_PATH=$CLINSEEK_DATA_ROOT/mm_bench/ehrxqa bash scripts/run_ehr_mcp.sh
BENCH_ROOT=$CLINSEEK_DATA_ROOT/mm_bench bash scripts/run_image_mcp.sh

DATA_PATH=$CLINSEEK_BENCH_ROOT/inputs/mm_bench.jsonl \
OUTPUT_DIR=outputs/mm_eval \
bash scripts/run_mm_eval.sh
```

Use `--enable_image` only when the image MCP server and model dependencies are available.

## Score

```bash
python clinseekagent/scoring/score_multimodal.py \
  --results outputs/mm_eval/results.jsonl \
  --output-root outputs/mm_eval/scored
```
