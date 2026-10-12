# Changelog

## v2 (revised paper, arXiv v2)

This release aligns the code with the revised paper: a strict temporal policy
for every EHR view, the four inference modes (ClinSeekAgent, Single-Pass
Retrieval, Curated Input, Full-Record Input), per-category reporting, the
AgentEHR-Bench evaluation of ClinSeek-35B-A3B, and the 2x H200 SFT recipe.
Several defaults and behaviours change; results produced with the May 2026
release are not directly comparable.

### Breaking behaviour changes

**Temporal policy (EHR MCP server, `src/`)**

- Every EHR view is built by the strict temporal policy
  `strict-ehr-runtime-v8` (text) / `multimodal-strict-temporal-scope-v1`
  (multimodal): rows are kept only if their *availability* time (charting /
  storage / report finalization) precedes the prediction cutoff, late-filled
  fields are withheld, diagnosis and procedure codes become visible 24 h after
  discharge / ED departure, and codes of the encounter being predicted are
  always hidden. Readmission tasks are cut at the index discharge.
- `ehr.load_ehr` refuses sessions without trusted benchmark metadata. The
  runners bind `subject_id`, `prediction_time`, `task`, `hadm_id`,
  `target_hadm_id`, `target_stay_id`, and (multimodal) `scope`,
  `study_ids`, `modalities` from the benchmark row before the model can call
  any tool; whatever the model passes to `load_ehr` is overridden, and a
  session cannot be re-bound to another patient, cutoff, task, or encounter.
- Diagnosis-code tasks fail closed without `target_hadm_id` /
  `target_stay_id`. The public manifest carries them; for other manifests run
  `scripts/data_build/materialize_ehr_bench_target_encounters.py`.
  (AgentEHR-Bench rows fall back to their `hadm_id`, which is the target
  admission for those tasks.)
- `ehr.run_sql_query` accepts a single read-only `SELECT` / `WITH` statement.
- Tables with no pre-cutoff rows are hidden from `get_table_names` and the
  other tools (a table's presence alone can reveal a future event category).

**Multimodal**

- Multimodal manifests are rejected unless every EHRXQA / MedMod row carries
  `subject_id`, `prediction_time`, `scope`, `modalities`, and (CXR tasks)
  `study_ids` (`clinseekagent/multimodal_manifest_policy.py`). Image tools only
  reach the linked studies.
- Multimodal runs need **two EHR MCP servers**, one per source benchmark
  (`--ehr_mcp_url_ehrxqa`, `--ehr_mcp_url_medmod`); the two benchmarks use
  different schemas and share subject ids.
- The public `ClinSeek-Bench_multimodal.jsonl` no longer ships `input_text`
  (the rendered EHR context). Render it locally with
  `scripts/data_build/materialize_multimodal_strict_manifest.py`.

**Benchmark manifests**

- The public `ClinSeek-Bench_text.json` no longer ships `input` / `question`
  (the upstream EHR-Bench serialization). The agentic runner never needed
  them; restore them with `scripts/data_build/restore_ehr_bench_inputs.py`
  when you need the source prompt. It adds `target_hadm_id`,
  `target_stay_id`, and `category`.

**Runners**

- The tool space defaults to the paper setting: 16 EHR tools for text, 16 EHR
  + 6 image tools for multimodal (`--enable_image`). Browser tools are an
  optional extra (`--enable_browser`); `--disable_browser` is a no-op.
- Defaults follow the paper's inference settings: temperature 0 where accepted
  (dropped for Claude with extended thinking), 8,192 output tokens per call,
  4,096-token thinking budget for Claude models that accept a fixed budget,
  `effort=high` for adaptive-only Claude models and OpenAI reasoning models.
  The launch scripts turn reasoning on (`ENABLE_THINKING=1`).
- `--backend vllm` is the generic OpenAI-compatible backend: native tool calls
  for vLLM-served models (serve Qwen3.5 / ClinSeek-35B-A3B with the
  `qwen3_coder` tool parser, see `scripts/run_vllm_server.sh`, 196,608-token
  context), `reasoning_effort` / `max_completion_tokens` for OpenAI reasoning
  models.
- Plain-text answer salvage was removed from the agentic loop: a run whose
  last turn has no `ehr.finish` call is recorded as `status=incomplete` and
  scores 0. `scripts/force_final_answer.py` adds one answer-only round to such
  rows (the paper applies it to GLM-5.2 and Kimi K2.6).
- Resume re-runs rows with `status=error` (requests that never reached the
  model) and keeps `incomplete` rows.
- `MAX_PARALLEL_QUERIES` (concurrency ceiling) 12 -> 20, overridable with
  `CLINSEEK_MAX_PARALLEL_QUERIES`.
- `run_text_curated.py` / `run_multimodal_curated.py` read a corpus built by
  `scripts/clean_data/build_evidence_corpus.py` (`--corpus-root`,
  `--view recent|full`, 100k-token window) and refuse rows without a prompt
  instead of sending an empty one. Both support Bedrock, Vertex AI, and any
  OpenAI-compatible endpoint, with the same reasoning settings as the agentic
  runners.

### Added

- Google Vertex AI backend for Claude (`clinseekagent/vertex_backend.py`,
  `--backend vertex`).
- Single-Pass Retrieval (`--retrieval_mode single_shot`), Full-Record Input
  (`--view full`), and `scripts/run_four_modes.sh` /
  `scripts/score_four_modes.sh`.
- Identical-tool-round circuit breaker
  (`--max_consecutive_identical_tool_rounds`, off by default).
- Per-category reporting (13 text / 6 multimodal categories):
  `per_category` in the scorers' `summary.json` and
  `clinseekagent/scoring/report_by_category.py` with paired-bootstrap CIs.
- Multimodal judge on Bedrock, Vertex AI, or an OpenAI-compatible endpoint
  (`score_multimodal.py --backend`); `--extract-text-answer-without-finish`
  in both scorers (off by default).
- AgentEHR-Bench: `scripts/download_agentehr_bench.sh`,
  `scripts/run_agentehr_bench.sh`, `scripts/build_agentehr_sample.py` and the
  paper's fixed 500-question sample (`clinseekagent/resources/`).
- Data tools: target-encounter materialization, strict runtime validators
  (text and multimodal), multimodal DB temporal audit, trajectory leakage audit
  (L0-L2), strict AgentEHR snapshots, Curated Input corpus builder.
- `scripts/check_results_complete.py` (gate before scoring),
  `scripts/render_trajectory.py`, `scripts/check_vertex.py`.
- SFT: `prepare_clinseek_data.py --merge_think` folds the teacher's
  `ehr.think` calls into the student's native thinking block;
  `scripts/train_sft.sh` defaults to the paper's 2x H200 recipe; long-context
  memory patch for the vendored verl in `verl/examples/sft/clinseek/patches/`.

### Removed

- The NVIDIA inference-gateway backend used during development.

### Dependencies

- Agent environment: `anthropic[vertex]>=0.69,<1`, `pandas==3.0.5` (the
  record renderers reproduce byte-identical prompts only under this version),
  `transformers`, `sentence-transformers`, `tqdm`.
- EHR MCP environment: `pandas>=3.0.5`; torch from the cu126 index.
- Image MCP environment: `pandas>=3.0.5`, `tqdm`.
