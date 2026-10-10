# Quick Start

This guide assumes the code is cloned, the per-role environments are installed
(see the README), and the benchmark data has been prepared outside Git
([text](ClinSeek-Bench_text_data_prepare.md),
[multimodal](ClinSeek-Bench_multimodal_data_prepare.md)).

## 1. Configure

```bash
cp .env.example .env
export CLINSEEK_DATA_ROOT=/path/to/clinseek_bench          # text manifest + EHR DBs
export MM_BENCH_ROOT=/path/to/ClinSeek-MM-Bench/data/mm_bench  # rebuilt multimodal root
```

Backends (`BACKEND=` for the launch scripts, `--backend` for the runners):

| Backend | Models | Credentials |
| --- | --- | --- |
| `bedrock` | Claude on AWS Bedrock, e.g. `us.anthropic.claude-opus-4-6-v1` | AWS credential chain or `BEDROCK_API_KEY` |
| `vertex` | Claude on Google Vertex AI, e.g. `claude-opus-4-6`, `claude-opus-5` | `gcloud auth application-default login` + `ANTHROPIC_VERTEX_PROJECT_ID` |
| `vllm` | any OpenAI-compatible endpoint: a local vLLM server (ClinSeek-35B-A3B), or an API serving e.g. GPT-5.6 | `VLLM_BASE_URL`, `VLLM_API_KEY` |

## 2. Start the MCP servers

The EHR MCP server runs in the EHR MCP environment. Text tasks need one server;
multimodal tasks need **one per source benchmark** plus the image server.

```bash
# ClinSeek-Bench-Text
EHR_DATA_PATH=$CLINSEEK_DATA_ROOT/ehr_bench EHR_MCP_PORT=5003 bash scripts/run_ehr_mcp.sh &

# ClinSeek-Bench-MM
EHR_DATA_PATH=$MM_BENCH_ROOT/ehrxqa EHR_MCP_PORT=5004 bash scripts/run_ehr_mcp.sh &
EHR_DATA_PATH=$MM_BENCH_ROOT/medmod EHR_MCP_PORT=5005 bash scripts/run_ehr_mcp.sh &
BENCH_ROOT=$MM_BENCH_ROOT bash scripts/run_image_mcp.sh &
```

Each EHR server registers 18 MCP tools; the agent sees 16 of them (the
`clear_session_ehr` and `read_resource_data` helpers are internal). The image
server exposes 6 tools. Confirm each server completes the MCP handshake before
a run: a stale server answers every call with an error string, which looks like
a model result rather than an outage.

## 3. Run ClinSeekAgent

```bash
# ClinSeek-Bench-Text: 16 EHR tools, up to 200 rounds
DATA_PATH=$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json \
OUTPUT_DIR=outputs/text_agentic BACKEND=vertex MODEL_NAME_OR_PATH=claude-opus-5 \
bash scripts/run_text_eval.sh

# ClinSeek-Bench-MM: 16 EHR + 6 image tools
DATA_PATH=/path/to/ClinSeek-Bench/ClinSeek-Bench_multimodal.jsonl \
BENCH_ROOT=$MM_BENCH_ROOT/ehrxqa:$MM_BENCH_ROOT/medmod \
OUTPUT_DIR=outputs/mm_agentic BACKEND=vertex MODEL_NAME_OR_PATH=claude-opus-5 \
bash scripts/run_mm_eval.sh
```

The launch scripts default to the paper's inference settings (reasoning on,
temperature 0 where accepted, 8,192 output tokens per call, 100,000-character
tool results). `RETRIEVAL_MODE=single_shot` runs Single-Pass Retrieval. For
Curated Input and Full-Record Input, see
[the curated-input guide](ClinSeek-Bench_text_curated_input_evaluation.md) and
`scripts/run_four_modes.sh`.

## 4. Check and score

```bash
python scripts/check_results_complete.py outputs/text_agentic/results.jsonl --expect 1800

python clinseekagent/scoring/score_text.py \
  --results outputs/text_agentic/results.jsonl \
  --benchmark $CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json \
  --output-dir outputs/text_agentic/scores

python clinseekagent/scoring/score_multimodal.py \
  --results outputs/mm_agentic/results.jsonl \
  --output-root outputs/mm_agentic/scores --backend vertex
```

Both scorers write `per_category` F1 (13 text / 6 multimodal categories) into
`summary.json`. Compare two runs per category with a paired-bootstrap CI:

```bash
python clinseekagent/scoring/report_by_category.py --bench text \
  --run curated=outputs/text_recent/scores --run agentic=outputs/text_agentic/scores \
  --compare curated agentic --markdown
```
