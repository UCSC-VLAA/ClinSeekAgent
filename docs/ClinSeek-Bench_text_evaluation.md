# ClinSeek-Bench Text Automated Evidence-Seeking Evaluation

This guide describes how to evaluate ClinSeekAgent on the text-only split of
ClinSeek-Bench under the Automated Evidence-Seeking setting. In this setting,
the model receives the task, patient identifier, and prediction-time cutoff,
then retrieves patient-specific evidence from raw EHR tables through
ClinSeekAgent tools before producing the final answer.

Run all commands from the repository root:

```bash
cd ClinSeekAgent
export CLINSEEK_DATA_ROOT="${CLINSEEK_DATA_ROOT:-./data/clinseek_bench}"
```

## Data Requirements

Prepare the data with
[ClinSeek-Bench_text_data_prepare.md](./ClinSeek-Bench_text_data_prepare.md)
before running evaluation. The expected release layout is:

```text
$CLINSEEK_DATA_ROOT/
+-- text/
|   +-- ClinSeek-Bench_text.json
+-- ehr_bench/
    +-- database/
    |   +-- patient_<subject_id>.db
    |   +-- ...
    +-- candidate_table.db
    +-- table_description/
```

`candidate_table.db` enables candidate lookup tools. `table_description/`
provides schema descriptions used by the EHR tools. The patient SQLite files
must cover every `subject_id` referenced by `ClinSeek-Bench_text.json`.

## Environment

Activate the Python environment you use for the release repo, then make sure
the backend-specific dependencies are available:

```bash
# Bedrock runs
python -c "import boto3"

# vLLM runs
python -c "import openai"
vllm --help >/dev/null
```

The wrapper scripts set `PYTHONPATH` automatically. If your environment uses a
non-default interpreter, pass it through `PYTHON_BIN`:

```bash
PYTHON_BIN=.venvs/agent/bin/python DATA_PATH=... bash scripts/run_text_eval.sh
```

## Start The EHR MCP Server

Open a terminal and serve the prepared ClinSeek-Bench EHR data:

```bash
EHR_DATA_PATH="$CLINSEEK_DATA_ROOT/ehr_bench" \
bash scripts/run_ehr_mcp.sh
```

Defaults:

| Setting | Default |
| --- | --- |
| Host | `127.0.0.1` |
| Port | `5003` |
| MCP URL | `http://127.0.0.1:5003/mcp` |
| Data path | `$CLINSEEK_DATA_ROOT/ehr_bench` |

Override the port if needed:

```bash
EHR_MCP_PORT=5013 EHR_DATA_PATH="$CLINSEEK_DATA_ROOT/ehr_bench" \
bash scripts/run_ehr_mcp.sh
```

If you change the port, use the same URL in `EHR_MCP_URL` when launching the
evaluation.

## Start A Model Backend

### Bedrock

For AWS Bedrock, configure either the default AWS credential chain or a Bedrock
bearer token:

```bash
export BEDROCK_REGION=us-east-1
# optional:
# export BEDROCK_API_KEY=...
# export AWS_BEARER_TOKEN_BEDROCK=...
```

The text evaluation wrapper defaults to
`us.anthropic.claude-opus-4-6-v1` when `BACKEND=bedrock`.

### vLLM

For a local OpenAI-compatible vLLM backend, start a model server in another
terminal:

```bash
MODEL_PATH=./models/my-model \
SERVED_MODEL_NAME=my-model \
TENSOR_PARALLEL_SIZE=1 \
MAX_MODEL_LEN=65536 \
bash scripts/run_vllm_server.sh
```

The default vLLM port is `4000`. Verify readiness:

```bash
curl -s http://127.0.0.1:4000/v1/models | python -m json.tool
```

`scripts/run_text_eval.sh` expects the OpenAI-compatible base URL to include
`/v1`; its default is `http://127.0.0.1:4000/v1`.

## Run Automated Evidence-Seeking Evaluation

### Bedrock Example

```bash
DATA_PATH="$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
OUTPUT_DIR=outputs/clinseek_bench_text_eval/opus46_automated_evidence_seeking \
BACKEND=bedrock \
BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=6 \
bash scripts/run_text_eval.sh
```

### vLLM Example

```bash
DATA_PATH="$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
OUTPUT_DIR=outputs/clinseek_bench_text_eval/my_model_automated_evidence_seeking \
BACKEND=vllm \
VLLM_BASE_URL=http://127.0.0.1:4000/v1 \
VLLM_API_KEY=EMPTY \
VLLM_MODEL=my-model \
EHR_MCP_URL=http://127.0.0.1:5003/mcp \
MAX_CONCURRENCY=6 \
bash scripts/run_text_eval.sh
```

Useful environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_PATH` | required | Benchmark JSON or JSONL manifest |
| `OUTPUT_DIR` | `outputs/text_eval` | Directory containing `results.jsonl` |
| `BACKEND` | `bedrock` | `bedrock` or `vllm` |
| `MODEL_NAME_OR_PATH` | backend-dependent | Explicit model id override |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-opus-4-6-v1` | Bedrock model id |
| `BEDROCK_REGION` | `us-east-1` | Bedrock region |
| `VLLM_BASE_URL` | `http://127.0.0.1:4000/v1` | vLLM OpenAI-compatible endpoint |
| `VLLM_MODEL` | `auto` | Served model id for vLLM |
| `EHR_MCP_URL` | `http://127.0.0.1:5003/mcp` | EHR MCP endpoint |
| `MAX_ROUNDS` | `200` | Maximum agent turns per question |
| `MAX_CONCURRENCY` | `6` | Concurrent questions |
| `RUNS_PER_QUESTION` | `1` | Repeated runs per benchmark row |
| `MAX_TOOL_RESULT_CHARS` | `100000` | Per-tool-result truncation limit |

For a smoke test, create a small JSON subset of
`ClinSeek-Bench_text.json` and point `DATA_PATH` to that file. The packaged
Automated Evidence-Seeking wrapper does not currently expose a `--limit` flag.

## Score Results

Score the generated `results.jsonl` against the same benchmark file:

```bash
python clinseekagent/scoring/score_text.py \
  --results outputs/clinseek_bench_text_eval/my_model_automated_evidence_seeking/results.jsonl \
  --benchmark "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/clinseek_bench_text_eval/my_model_automated_evidence_seeking/scored
```

The scorer writes:

```text
outputs/clinseek_bench_text_eval/my_model_automated_evidence_seeking/scored/
+-- per_question.jsonl
+-- summary.json
+-- summary.txt
```

It also prints overall, per-task, and `task_type` grouped metrics, plus tool-call
statistics such as average tool calls, browser-call percentage, and average
turn count.

## Troubleshooting

- `Results file not found`: pass the exact `results.jsonl` path or the output
  directory that contains it.
- `Connection refused` for `EHR_MCP_URL`: start `scripts/run_ehr_mcp.sh`, or
  update `EHR_MCP_URL` if you changed the MCP port.
- `patient_<subject_id>.db` missing: rerun the DB preparation step with
  `--data_file_path "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json"`.
- Candidate tools return empty results: confirm `candidate_table.db` is under
  `$CLINSEEK_DATA_ROOT/ehr_bench`.
- Table-description prompts are weak or missing: confirm
  `$CLINSEEK_DATA_ROOT/ehr_bench/table_description/` exists.
- Very slow first phase: large MIMIC tables such as `chartevents.csv` and
  `labevents.csv` dominate the DB-generation step. This is expected during data
  preparation, not during evaluation.
