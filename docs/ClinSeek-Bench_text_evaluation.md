# ClinSeek-Bench-Text: ClinSeekAgent Evaluation

This guide evaluates ClinSeekAgent on ClinSeek-Bench-Text under the Automated
Evidence-Seeking setting. The model receives the task instruction, the patient
identifier, the prediction time, and (when the task has one) the candidate
answer list. It retrieves evidence from the patient's raw EHR tables through
the 16 ClinSeekAgent EHR tools, under the strict temporal policy, and answers
with `ehr.finish`.

Run all commands from the repository root:

```bash
export CLINSEEK_DATA_ROOT="${CLINSEEK_DATA_ROOT:-./data/clinseek_bench}"
```

## Data Requirements

Prepare the data with
[ClinSeek-Bench_text_data_prepare.md](./ClinSeek-Bench_text_data_prepare.md):

```text
$CLINSEEK_DATA_ROOT/
+-- text/
|   +-- ClinSeek-Bench_text.json
+-- ehr_bench/
    +-- database/
    |   +-- patient_<subject_id>.db
    |   +-- candidate_table.db      (optional; candidate lookup tools)
    +-- table_description/          (optional; schema descriptions)
```

The public manifest is enough for this setting: the agentic prompt is built
from `instruction` and the row metadata, and never from the upstream
`input` / `question` fields that the public file omits.

## Tool Space

| Tool family | Count | Default |
| --- | ---: | --- |
| EHR (`ehr.*`): `load_ehr`, table/column/description/unique-value lookup, time-, keyword- and value-based retrieval, event counts, latest records, `run_sql_query`, candidate lookup (keyword, fuzzy, semantic), `think`, `finish` | 16 | on |
| Browser (`browser.search/open/find`) | 3 | off; `ENABLE_BROWSER=1` / `--enable_browser` |

The EHR MCP server registers 18 tools; `clear_session_ehr` and
`read_resource_data` are internal helpers that the agent does not see.

## Start The EHR MCP Server

In the EHR MCP environment:

```bash
EHR_DATA_PATH="$CLINSEEK_DATA_ROOT/ehr_bench" bash scripts/run_ehr_mcp.sh
```

Defaults: host `127.0.0.1`, port `5003` (`EHR_MCP_PORT`), URL
`http://127.0.0.1:5003/mcp` (`EHR_MCP_URL` for the runner). A stale server
answers every call with an error string; confirm the handshake before a run.

## Start A Model Backend

| `BACKEND` | Model id (`MODEL_NAME_OR_PATH`) | Setup |
| --- | --- | --- |
| `bedrock` | e.g. `us.anthropic.claude-opus-4-6-v1` (default) | AWS credential chain, or `BEDROCK_API_KEY`; `BEDROCK_REGION` |
| `vertex` | e.g. `claude-opus-4-6`, `claude-opus-5` (`auto` picks the first served) | `gcloud auth application-default login` (or `GOOGLE_APPLICATION_CREDENTIALS`), `ANTHROPIC_VERTEX_PROJECT_ID`, `VERTEX_REGION`; check with `python scripts/check_vertex.py <model>` |
| `vllm` | served model name, e.g. `ClinSeek-35B-A3B` | `VLLM_BASE_URL` (with `/v1`), `VLLM_API_KEY`; any OpenAI-compatible endpoint works (OpenAI reasoning models get `reasoning_effort` and `max_completion_tokens`) |

Serve ClinSeek-35B-A3B (or its Qwen3.5-35B-A3B base) locally with native tool
calls:

```bash
MODEL_PATH=/path/to/ClinSeek-35B-A3B SERVED_MODEL_NAME=ClinSeek-35B-A3B \
TENSOR_PARALLEL_SIZE=4 bash scripts/run_vllm_server.sh
curl -s http://127.0.0.1:4000/v1/models
```

For these models the script enables `--reasoning-parser qwen3`,
`--enable-auto-tool-choice --tool-call-parser qwen3_coder`, and a 196,608-token
context (override with `MAX_MODEL_LEN`, `KV_CACHE_MEMORY`).

## Run

```bash
DATA_PATH="$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
OUTPUT_DIR=outputs/text_agentic_opus5 \
BACKEND=vertex MODEL_NAME_OR_PATH=claude-opus-5 \
bash scripts/run_text_eval.sh

DATA_PATH="$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
OUTPUT_DIR=outputs/text_agentic_clinseek35b \
BACKEND=vllm MODEL_NAME_OR_PATH=ClinSeek-35B-A3B \
bash scripts/run_text_eval.sh

# GPT-5.6 Sol through an OpenAI-compatible endpoint (reasoning_effort=high)
DATA_PATH="$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
OUTPUT_DIR=outputs/text_agentic_gpt56sol \
BACKEND=vllm MODEL_NAME_OR_PATH=gpt-5.6-sol \
VLLM_BASE_URL=https://api.openai.com/v1 VLLM_API_KEY=$OPENAI_API_KEY \
bash scripts/run_text_eval.sh
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_PATH` | required | Benchmark JSON or JSONL manifest |
| `OUTPUT_DIR` | `outputs/text_eval` | Writes `results.jsonl`, `run_config.json`, `usage.json` |
| `BACKEND` / `MODEL_NAME_OR_PATH` | `bedrock` / backend default | see above |
| `RETRIEVAL_MODE` | `agentic` | `single_shot` = Single-Pass Retrieval (one retrieval turn, then answer) |
| `MAX_ROUNDS` | `200` | Maximum agent rounds |
| `MAX_TOKENS` | `8192` | Output tokens per model call |
| `MAX_TOOL_RESULT_CHARS` | `100000` | Per-tool-result truncation |
| `TEMPERATURE` | `0.0` | Dropped where not accepted (Claude with thinking, sampling-free models) |
| `ENABLE_THINKING` | `1` | Reasoning on |
| `THINKING_BUDGET` | `4096` | Claude models that take a fixed thinking budget (4.6 and older) |
| `REASONING_EFFORT` | `high` | Adaptive-only Claude models (Opus 4.7+, Opus 5) and OpenAI reasoning models |
| `ENABLE_BROWSER` | `0` | Optional browser tools |
| `MAX_CONCURRENCY` | `6` | Concurrent questions (ceiling `CLINSEEK_MAX_PARALLEL_QUERIES`, default 20) |
| `RUNS_PER_QUESTION` | `1` | Repeated runs per row |

Extra `run_text.py` flags pass through, e.g. `--save_raw_responses` (keep each
round's provider payload) or `--max_consecutive_identical_tool_rounds N`
(stop a run that repeats the same tool batch N times; off by default).

For a smoke test point `DATA_PATH` at a few rows of the manifest; the agentic
runner has no row limit flag.

### Resume, incomplete rows, and the forced final answer

Re-running the same command resumes: rows already in `results.jsonl` are kept,
except rows with `status=error` (the request never reached the model, e.g. a
rate limit or a dead MCP session), which are re-run. A run whose last turn has
no `ehr.finish` call is recorded as `status=incomplete` and scores 0.

Some reasoning models spend the whole output budget reasoning and never answer.
The paper appends one answer-only round to such rows for GLM-5.2 and Kimi K2.6
(both settings): the conversation is replayed and the model is asked to call
`ehr.finish` from the evidence already gathered, with no further retrieval:

```bash
python scripts/force_final_answer.py \
  --src outputs/text_agentic_glm --dst outputs/text_agentic_glm_forcedfinal \
  --backend vllm --model <served-model> --api_base_url <endpoint>/v1
```

## Check And Score

```bash
python scripts/check_results_complete.py outputs/text_agentic_opus5/results.jsonl --expect 1800

python clinseekagent/scoring/score_text.py \
  --results outputs/text_agentic_opus5/results.jsonl \
  --benchmark "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/text_agentic_opus5/scores
```

`check_results_complete.py` refuses a file with missing or duplicate qids,
rows that failed before reaching the model, or a high rate of tool
infrastructure errors. The scorer writes `per_question.jsonl`, `summary.json`
(overall, `per_task`, `per_task_type`, and `per_category` over the paper's 13
categories), and `summary.txt`. Sample-wise F1 is computed after whitespace /
case normalization; questions without a result score 0.

Compare runs per category with a paired-bootstrap 95% CI (4,000 resamples):

```bash
python clinseekagent/scoring/report_by_category.py --bench text \
  --run curated=outputs/text_recent/scores --run agentic=outputs/text_agentic_opus5/scores \
  --compare curated agentic --markdown
```

`python scripts/render_trajectory.py <results.jsonl> -o traj.md` renders a run
as readable Markdown (it contains patient records; keep it local).

## Troubleshooting

- `Connection refused` for `EHR_MCP_URL`: start `scripts/run_ehr_mcp.sh`, or
  update `EHR_MCP_URL` if you changed the port.
- `Error: EHR session has no trusted benchmark metadata`: the row lacks
  `subject_id` / `prediction_time`; use the public manifest.
- Diagnosis-code rows fail at `load_ehr`: the row lacks `target_hadm_id` /
  `target_stay_id` (see the data preparation guide).
- `patient_<subject_id>.db` missing: rerun `scripts/patient_event2db.py` with
  `--data_file_path "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json"`.
