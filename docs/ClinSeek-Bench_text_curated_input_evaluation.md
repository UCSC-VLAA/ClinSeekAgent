# ClinSeek-Bench Text Curated Input Evaluation

This guide describes how to evaluate the text-only split of ClinSeek-Bench
under the Curated Input setting. In this setting, the model answers directly
from the evidence package embedded in each benchmark `question`, without
accessing ClinSeekAgent tools.

Curated Input is the paired baseline for ClinSeekAgent's Automated
Evidence-Seeking setting. Both settings use the same task definitions and
answer labels; only the evidence-access pattern changes.

Run all commands from the repository root:

```bash
cd ClinSeekAgent
export CLINSEEK_DATA_ROOT="${CLINSEEK_DATA_ROOT:-./data/clinseek_bench}"
```

## Evaluation Settings

| Dimension | Automated Evidence-Seeking | Curated Input |
| --- | --- | --- |
| Method | ClinSeekAgent | Curated Input baseline |
| Runner | `scripts/run_text_eval.sh` | `clinseekagent/run_text_curated.py` |
| EHR MCP server | Required | Not used |
| EHR retrieval | Through ClinSeekAgent tools | Not used |
| External knowledge search | Available through browser tools | Not used |
| Interaction | Multi-turn | Single model response |
| Input | Task metadata plus raw-data access | Full `question` field with curated context |
| Output | `results.jsonl` | Compatible `results.jsonl` with synthetic `ehr.finish` |

The release repo packages the Curated Input setting as
`clinseekagent/run_text_curated.py`.

## Current Backend Support

`run_text_curated.py` currently supports the Bedrock backend. Its `vllm`
arguments are present, but the `VLLMInvoker` implementation is still a stub and
raises `NotImplementedError`. Do not use `--backend vllm` for this runner until
that invoker is implemented.

The Automated Evidence-Seeking runner, `scripts/run_text_eval.sh`, does
support vLLM today.

## Data Requirements

Curated Input evaluation only needs the benchmark JSON:

```text
$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json
```

It does not require patient SQLite databases, `candidate_table.db`, or an EHR
MCP server, because no tools are called.

## Run Curated Input Evaluation

### Bedrock Full Run

```bash
python clinseekagent/run_text_curated.py \
  --backend bedrock \
  --model "Claude Opus 4.6" \
  --data "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/clinseek_bench_text_eval/opus46_curated_input \
  --concurrency 10 \
  --temperature 0.0 \
  --max-tokens 16384
```

The runner appends a short answer-format trailer to each benchmark `question`,
asks the model to put its final answer in an `<answer>...</answer>` block, and
then wraps the parsed response into a synthetic `ehr.finish` tool call. This
keeps the output compatible with `clinseekagent/scoring/score_text.py`.

### Smoke Test

```bash
python clinseekagent/run_text_curated.py \
  --backend bedrock \
  --model "Claude Opus 4.6" \
  --data "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/clinseek_bench_text_eval/curated_input_smoke \
  --limit 5 \
  --concurrency 2 \
  --temperature 0.0 \
  --max-tokens 4096
```

### Region Selection

By default, the Bedrock runner chooses one available region for the selected
friendly model name. To pin a region:

```bash
python clinseekagent/run_text_curated.py \
  --backend bedrock \
  --model "Claude Opus 4.6" \
  --regions us-east-1 \
  --data "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/clinseek_bench_text_eval/opus46_curated_input_use1
```

For large runs, use `--multi-region` to distribute rows across every available
Bedrock region for the model.

## Score Curated Input Results

Use the same scorer as the Automated Evidence-Seeking evaluation:

```bash
python clinseekagent/scoring/score_text.py \
  --results outputs/clinseek_bench_text_eval/opus46_curated_input/results.jsonl \
  --benchmark "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/clinseek_bench_text_eval/opus46_curated_input/scored
```

The scorer reports the same metrics as the Automated Evidence-Seeking run.
Tool-call statistics will show the synthetic `ehr.finish` calls only; there
should be no EHR query or browser calls in Curated Input results.

## Output Format

The merged output is:

```text
outputs/clinseek_bench_text_eval/opus46_curated_input/
+-- results.jsonl
+-- region_map.json
+-- <region>/
    +-- results.jsonl
```

Each JSONL record contains the benchmark `qid`, the original row metadata, the
single user prompt, the raw assistant response, and a synthetic `ehr.finish`
tool call with parsed predictions.

## vLLM Backend Notes

The Curated Input runner has `vllm` arguments, but the current release does not
yet implement the local vLLM invoker for this runner. To run Curated Input with
a local model, implement `VLLMInvoker` in `clinseekagent/run_text_curated.py`
against an OpenAI-compatible `/v1/chat/completions` endpoint, then run:

```bash
python clinseekagent/run_text_curated.py \
  --backend vllm \
  --model my-model \
  --api-base-url http://127.0.0.1:4000/v1 \
  --api-key EMPTY \
  --data "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
  --output-dir outputs/clinseek_bench_text_eval/my_model_curated_input
```

Until that implementation exists, use Bedrock for Curated Input runs or use the
Automated Evidence-Seeking runner for vLLM-supported ClinSeekAgent evaluation.

## Troubleshooting

- `NotImplementedError: vLLM backend is not implemented yet`: this is expected
  for `run_text_curated.py --backend vllm` in the current release repo.
- Empty predictions: inspect the raw assistant message in `results.jsonl`; the
  parser first looks for `<answer>...</answer>` and then falls back to plain-text
  salvage.
- No scored rows: confirm the scorer `--benchmark` path points to the same
  `ClinSeek-Bench_text.json` used for the run.
- Missing Bedrock model mapping: check `clinseekagent/bedrock_region_availability.json`
  for supported friendly model names and regions.
