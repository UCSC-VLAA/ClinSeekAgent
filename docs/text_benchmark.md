# Text Benchmark

The text-only pipeline evaluates ClinSeekAgent on EHR tasks where the model must retrieve patient evidence through EHR tools and optionally use browser search for external medical knowledge.

## Input Manifest

Each JSONL row should include:

```json
{
  "qid": "sample_id",
  "subject_id": "10000032",
  "prediction_time": "2150-12-01 10:00:00",
  "question": "Clinical task instruction...",
  "candidates": ["candidate A", "candidate B"],
  "ground_truth": ["candidate A"]
}
```

Rows can also follow the EHR-Bench style fields consumed by `clinseekagent/prompts_text.py`.

## Run

```bash
EHR_DATA_PATH=$CLINSEEK_DATA_ROOT/ehr_bench bash scripts/run_ehr_mcp.sh

DATA_PATH=$CLINSEEK_DATA_ROOT/text/test.jsonl \
OUTPUT_DIR=outputs/text_eval \
bash scripts/run_text_eval.sh
```

## Score

```bash
python clinseekagent/scoring/score_text.py \
  --results outputs/text_eval/results.jsonl \
  --benchmark $CLINSEEK_DATA_ROOT/text/test.jsonl \
  --output outputs/text_eval/summary.json
```
