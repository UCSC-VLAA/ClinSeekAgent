# AgentEHR-Bench Evaluation

The paper evaluates ClinSeek-35B-A3B and the baselines on
[AgentEHR-Bench](https://huggingface.co/datasets/BlueZeros/AgentEHR-Bench):
100 questions for each of five tasks (diagnoses_ccs, procedures_ccs,
labevents, microbiologyevents, transfers) from the MIMIC-IV `common` test
split, 500 in total. All models run ClinSeekAgent with the 16 EHR tools, the
strict temporal policy, up to 200 rounds, 8,192 output tokens per call, and
temperature 0; the base and fine-tuned models are served locally with vLLM.

**Relation to the original AgentEHR setting.** This evaluation uses the
questions and labels of AgentEHR-Bench but runs them through ClinSeekAgent's
own interface, which may differ from the original setup in a few respects: the
agent uses the 16 EHR tools described in the README; the strict temporal policy
builds a question-specific view of the patient database (the synthetic
`diagnoses_icd` / `diagnosis` chart times produced when the databases were built
are not accepted as evidence of availability, `admissions.text` is dropped, and
for diagnosis tasks the target encounter's codes are excluded); and we evaluate a fixed
sample of 100 questions per task over five of the six tasks. Numbers obtained
here are therefore best compared with each other rather than with those reported
in the AgentEHR paper.

## 1. Download

AgentEHR-Bench contains MIMIC-derived patient databases; keep them local.

```bash
SUBSET=MIMICIVAgentBench AGENTEHR_DATA_ROOT=/path/to/AgentEHR-Bench \
  bash scripts/download_agentehr_bench.sh
```

The default `AGENTEHR_DATA_ROOT` is `data/AgentEHR-Bench` under the repository
(git-ignored). The download resumes after interruptions.

## 2. Rebuild the fixed 500-question sample

The qids of the paper sample ship with the repository
(`clinseekagent/resources/agentehr_common_sample500.json`). Rebuild the rows
from your download; the script checks that it reproduces exactly those qids:

```bash
python scripts/build_agentehr_sample.py \
  --root /path/to/AgentEHR-Bench/MIMICIVAgentBench \
  --out data/agentehr_sample500
```

## 3. Serve the model

```bash
MODEL_PATH=/path/to/ClinSeek-35B-A3B SERVED_MODEL_NAME=ClinSeek-35B-A3B \
TENSOR_PARALLEL_SIZE=4 bash scripts/run_vllm_server.sh
```

## 4. Run

`scripts/run_agentehr_bench.sh` starts an EHR MCP server on the benchmark tree
(unless one is already listening on `EHR_MCP_PORT`), copies the cases into the
output directory, and runs the agent:

```bash
for t in diagnoses_ccs procedures_ccs labevents microbiologyevents transfers; do
  SUBSET=mimic4-common TASK=$t CASES_OVERRIDE=data/agentehr_sample500/${t}_100.json \
  AGENTEHR_DATA_ROOT=/path/to/AgentEHR-Bench \
  BACKEND=vllm VLLM_MODEL=ClinSeek-35B-A3B MAX_CONCURRENCY=2 \
  MCP_PYTHON_BIN=/path/to/mcp-ehr-env/bin/python \
  OUTPUT_DIR=outputs/agentehr500/clinseek35b/$t \
    bash scripts/run_agentehr_bench.sh
done
```

`BACKEND=vertex VERTEX_MODEL_ID=...` or `BACKEND=bedrock BEDROCK_MODEL_ID=...`
run Claude; `BACKEND=vllm` with `VLLM_BASE_URL` / `VLLM_API_KEY` runs any
OpenAI-compatible endpoint. AgentEHR-Bench diagnosis rows carry no explicit
`target_hadm_id`; their `hadm_id` is the target admission and is used as such.

Optionally, `scripts/data_build/build_strict_ehr_snapshots.py` writes physical
per-subject snapshots at the prediction cutoff (future rows and fields removed
from the files themselves) as a second line of defense behind the runtime
policy.

## 5. Score

```bash
for t in diagnoses_ccs procedures_ccs labevents microbiologyevents transfers; do
  python scripts/check_results_complete.py outputs/agentehr500/clinseek35b/$t/results.jsonl --expect 100
done
cat outputs/agentehr500/clinseek35b/*/results.jsonl > outputs/agentehr500/clinseek35b/results_500.jsonl
cat outputs/agentehr500/clinseek35b/*/cases.jsonl > outputs/agentehr500/clinseek35b/cases_500.jsonl
python clinseekagent/scoring/score_text.py \
  --results outputs/agentehr500/clinseek35b/results_500.jsonl \
  --benchmark outputs/agentehr500/clinseek35b/cases_500.jsonl \
  --output-dir outputs/agentehr500/clinseek35b/scores
```

`summary.json` reports F1 per task and the average over the 500 questions.
