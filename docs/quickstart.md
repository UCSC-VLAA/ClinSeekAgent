# Quick Start

This guide assumes the code is cloned, dependencies are installed, and benchmark data has been prepared outside Git.

## 1. Configure Environment

```bash
cp .env.example .env
export CLINSEEK_DATA_ROOT=/path/to/clinseek_bench
```

For Bedrock runs, use either the default AWS credential chain or set `BEDROCK_API_KEY` / `AWS_BEARER_TOKEN_BEDROCK`. For local models, start a vLLM OpenAI-compatible server and set `VLLM_BASE_URL`.

## 2. Start The EHR MCP Server

```bash
EHR_DATA_PATH=$CLINSEEK_DATA_ROOT/ehr_bench \
bash scripts/run_ehr_mcp.sh
```

## 3. Run Text Evaluation

```bash
DATA_PATH=$CLINSEEK_DATA_ROOT/text/test.jsonl \
OUTPUT_DIR=outputs/text_eval \
BACKEND=bedrock \
bash scripts/run_text_eval.sh
```

## 4. Run Multimodal Evaluation

```bash
BENCH_ROOT=$CLINSEEK_DATA_ROOT/multimodal \
bash scripts/run_image_mcp.sh

DATA_PATH=$CLINSEEK_DATA_ROOT/multimodal/test.jsonl \
OUTPUT_DIR=outputs/mm_eval \
BACKEND=bedrock \
bash scripts/run_mm_eval.sh
```

## 5. Score Outputs

Text-only scoring:

```bash
python openresearcher_ehr/helper/evaluate_results.py \
  --results outputs/text_eval/results.jsonl \
  --benchmark $CLINSEEK_DATA_ROOT/text/test.jsonl \
  --output outputs/text_eval/summary.json
```

Multimodal scoring:

```bash
python openresearcher_ehr/helper/scorer_mm.py \
  --results outputs/mm_eval/results.jsonl \
  --output-root outputs/mm_eval/scored
```
