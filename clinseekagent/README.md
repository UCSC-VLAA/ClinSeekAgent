# `clinseekagent/` — agent driver package

This package contains everything that runs **on the agent / client side**
during evaluation: the runners for the four inference modes, the LLM backends
(AWS Bedrock, Google Vertex AI, OpenAI-compatible / vLLM), the tool-pool
clients (EHR, image, optional browser), prompt registries, record renderers,
and scoring. The MCP **servers** (which expose the EHR databases and the
medical-image models) live under `src/`.

The package is not installed as a Python module; the scripts under
`../scripts/` add this directory to `PYTHONPATH` so the files import each other
by bare name (e.g. `from run_text import ...`), and the repository's
`conftest.py` does the same for the tests.

## Layout

| Path | Purpose |
| --- | --- |
| `run_text.py` | ClinSeekAgent on text tasks: the agentic loop (default) or Single-Pass Retrieval (`--retrieval_mode single_shot`). 16 EHR tools; browser tools only with `--enable_browser`. |
| `run_multimodal.py` | ClinSeekAgent on multimodal tasks: EHR + image tools (`--enable_image`), CXR images attached to the first turn, one EHR MCP server per source benchmark. |
| `run_text_curated.py` | Curated Input (`--view recent`) and Full-Record Input (`--view full`) for text: one prompt rendered from the policy-filtered record (`--corpus-root`), no tools. |
| `run_multimodal_curated.py` | The same for multimodal tasks (rendered EHR context + up to 4 images + report text). |
| `curated_invokers.py` | Single-call invokers (Bedrock, Vertex, OpenAI-compatible) shared by the curated runners, with the same reasoning settings as the agentic backends. |
| `record_context.py` | Renders a corpus row's record at the `recent` / `full` width and truncates the full record oldest-first to the prompt budget. |
| `question_render.py`, `mm_context_render.py`, `ehr_r1_selection.py` | Record renderers used by `record_context.py` and the corpus builder (`scripts/clean_data/`). |
| `context_limits.py` | Model context windows, the 100k-token prompt window of the record modes, and the calibrated characters-per-token ratio. |
| `bedrock_backend.py` | Async generator for AWS Bedrock (Anthropic Messages API; OpenAI shape for other Bedrock models). Holds the Claude thinking / sampling rules. |
| `vertex_backend.py` | Async generator for Claude on Google Vertex AI (reuses the Bedrock message conversion). |
| `vllm_backend.py` | Async generator for OpenAI-compatible endpoints: native tool calls for vLLM-served models, `reasoning_effort` / `max_completion_tokens` for OpenAI reasoning models, retries. |
| `ehr_tool_pool.py` | Per-question MCP client for `ehr.*` calls; binds the trusted row metadata (patient, cutoff, task, target encounter, CXR scope) into every `load_ehr`. |
| `image_tool_pool.py` | Per-question MCP client for `image.*` calls; restricts calls to the row's linked images. |
| `multimodal_manifest_policy.py` | Fail-closed checks on multimodal manifests (trusted metadata present; strict marker on rendered context). |
| `browser_tool.py` | Optional browser tools (OpenAI Harmony / `gpt_oss` stack). |
| `prompts_text.py` | System prompts and the EHR (+ optional browser) tool schemas; the EHR-Bench agentic prompt. |
| `prompts_multimodal.py` | Multimodal prompts and the 6 image-tool schemas; `get_tools_mm(browser, image)`. |
| `resources/task_taxonomy.json` | The paper's 13 text / 6 multimodal task categories. |
| `resources/agentehr_common_sample500.json` | qids of the paper's AgentEHR-Bench evaluation sample. |
| `scoring/score_text.py` | Sample-wise F1 for text results (and AgentEHR-Bench); per task, task type, and category. |
| `scoring/score_multimodal.py` | Multimodal scorer: LLM judge for single-label rows, vocabulary-mapped set F1 for multi-label rows; unified F1 per task and category. |
| `scoring/report_by_category.py`, `scoring/categories.py` | Per-category tables and paired-bootstrap comparisons between runs. |
| `prepare_multimodal_data.py`, `generate_ehr_tools_schema.py` | Small data / schema utilities. |
| `bedrock_region_availability.json` | Friendly-name -> region map for the Bedrock curated invoker. |

## Environment variables

| Variable | Used by | Note |
| --- | --- | --- |
| `EHR_MCP_URL` | `ehr_tool_pool.py` | Default `http://127.0.0.1:5003/mcp`. |
| `IMAGE_MCP_URL` | `image_tool_pool.py` | Default `http://127.0.0.1:5203/mcp`. |
| `BENCH_ROOT` | multimodal runners | Root(s) against which manifest image/report paths resolve (colon-separated). |
| `BEDROCK_API_KEY` / `AWS_BEARER_TOKEN_BEDROCK`, `BEDROCK_REGION` | `bedrock_backend.py` | Optional bearer token; otherwise the AWS credential chain. |
| `ANTHROPIC_VERTEX_PROJECT_ID`, `VERTEX_REGION` / `CLOUD_ML_REGION`, `VERTEX_MODEL_ID` | `vertex_backend.py` | Application Default Credentials. |
| `VLLM_BASE_URL`, `VLLM_API_KEY`, `VLLM_MODEL` | `vllm_backend.py` | Any OpenAI-compatible endpoint. |
| `CLINSEEK_MAX_PARALLEL_QUERIES` | runners | Concurrency ceiling (default 20). |
| `SERPER_API_KEY` | `browser_tool.py` | Optional; Serper search for `--enable_browser`. |

## Inference modes

| Mode | Runner | Evidence access |
| --- | --- | --- |
| ClinSeekAgent | `run_text.py` / `run_multimodal.py` | multi-round tool calls over the whole pre-cutoff record (up to 200 rounds) |
| Single-Pass Retrieval | same, `--retrieval_mode single_shot` | same tools, but all retrieval calls in one turn, then answer |
| Curated Input | `run_*_curated.py --view recent` | the source benchmark's evidence package, re-rendered from the policy-filtered record |
| Full-Record Input | `run_*_curated.py --view full` | the whole pre-cutoff record in the prompt, truncated oldest-first to 100k tokens |

All four share the strict temporal policy and the same reasoning and output
settings; `scripts/run_four_modes.sh` runs them, `scripts/score_four_modes.sh`
scores them.

```bash
# MCP servers first (see the README); then, e.g.
DATA_PATH=... OUTPUT_DIR=outputs/text bash scripts/run_text_eval.sh
DATA_PATH=... BENCH_ROOT=... OUTPUT_DIR=outputs/mm bash scripts/run_mm_eval.sh
python clinseekagent/run_text_curated.py --backend vertex --model claude-opus-4-6 \
  --corpus-root /path/to/text_corpus --view recent --output-dir outputs/text_recent
python clinseekagent/scoring/score_text.py --results outputs/text/results.jsonl \
  --benchmark /path/to/ClinSeek-Bench_text.json --output-dir outputs/text/scores
```
