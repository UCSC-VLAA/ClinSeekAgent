# `clinseekagent/` — agent driver package

This package contains everything that runs **on the agent / client side** during evaluation: the four entry-point runners, the LLM backends (Bedrock, vLLM), the three tool-pool clients (browser, EHR, image), prompt registries, scoring utilities, and a few data-prep helpers. The MCP **servers** (which actually expose the EHR DB and the medical-image models) live separately under `src/`.

The package is not installed as a Python module; the scripts under `../scripts/` add this directory to `PYTHONPATH` so the files import each other by bare name (e.g. `from run_text import ...`).

## Layout

| Path | Purpose |
| --- | --- |
| `run_text.py` | **Agentic** evaluation driver, text-only EHR tasks (paper's *Automated Evidence-Seeking* mode). |
| `run_multimodal.py` | Agentic driver for multimodal tasks (EHR + CXR + browser). Reuses helpers from `run_text.py`. |
| `run_text_curated.py` | One-shot **Curated Input** baseline, text-only. Sends pre-selected EHR context to the model without tools. |
| `run_multimodal_curated.py` | Curated-Input baseline for multimodal tasks (attaches CXR images + report text in a single shot). |
| `bedrock_backend.py` | Async generator wrapping AWS Bedrock (Anthropic content-blocks API). |
| `vllm_backend.py` | Async generator wrapping a vLLM OpenAI-compatible server. Handles tool-call extraction from native + several fallback formats. |
| `bedrock_region_availability.json` | Friendly-name → region availability map used by the Bedrock invokers in the curated baselines. |
| `browser_tool.py` | Client wrapper around the OpenAI Harmony / `gpt_oss` browser tool stack. |
| `ehr_tool_pool.py` | Per-qid MCP client pool that forwards `ehr.*` tool calls to the EHR MCP server (`src/run_mcp_server.py`). |
| `image_tool_pool.py` | Per-qid MCP client pool that forwards `image.*` tool calls to the image MCP server (`src/mcp_image/run_image_mcp_server.py`). |
| `prompts_text.py` | Task-prompt templates, `<developer>` system prompts, and the canonical browser + EHR **tool schemas** (loaded by both text and multimodal runners). |
| `prompts_multimodal.py` | Multimodal additions: 6 image-tool schemas + combined registry. Re-exports browser + EHR pieces from `prompts_text.py` unchanged. |
| `prepare_multimodal_data.py` | Filters a source multimodal manifest into a JSONL the runners can consume. |
| `generate_ehr_tools_schema.py` | One-off utility for regenerating the EHR tool JSON schema. |
| `scoring/score_text.py` | F1 scorer for text-only EHR-Bench results. |
| `scoring/score_multimodal.py` | F1 scorer for multimodal results; uses Bedrock as a judge for free-form answers. |

## Environment variables consumed

These are all read at startup and exposed in `.env.example` at the repo root:

| Variable | Used by | Note |
| --- | --- | --- |
| `EHR_MCP_URL` | `ehr_tool_pool.py` | Default `http://127.0.0.1:5003/mcp`. |
| `IMAGE_MCP_URL` | `image_tool_pool.py` | Default `http://127.0.0.1:5203/mcp`. |
| `BENCH_ROOT` | `run_multimodal.py`, `run_multimodal_curated.py` | Root dir where relative image/report paths in manifests are resolved. |
| `IMAGE_ARTIFACT_DIR` | `image_tool_pool.py` / image MCP | Where image tool outputs (visualizations, masks) are written. |
| `BEDROCK_API_KEY` / `AWS_BEARER_TOKEN_BEDROCK` | `bedrock_backend.py` | Optional bearer token; falls back to the default AWS credential chain. |
| `BEDROCK_REGION` / `AWS_DEFAULT_REGION` | `bedrock_backend.py` | Defaults to `us-east-1`. |
| `VLLM_BASE_URL`, `VLLM_API_KEY`, `VLLM_MODEL` | `vllm_backend.py` | OpenAI-compatible endpoint for self-hosted models. |
| `SERPER_API_KEY` | `browser_tool.py` | Optional — enables the Serper search backend; otherwise falls back to the local browser. |

## Two evaluation modes the runners implement

The paper defines two paired modes for every benchmark example:

- **Curated Input** — the source benchmark's pre-selected EHR/CXR context is handed to the model in one shot, with no tools. Run by `run_text_curated.py` / `run_multimodal_curated.py`.
- **Automated Evidence-Seeking (ClinSeekAgent)** — the curated context is stripped; the model receives only the patient identifier + a prediction-time cutoff + access to the ClinSeekAgent tool space, and must retrieve evidence itself across multiple turns. Run by `run_text.py` / `run_multimodal.py`.

The same input manifest and same ground-truth labels are used for both, so the difference in F1 measures the contribution of active evidence seeking.

## Entry points (typical command lines)

```bash
# Start the MCP servers in their own venvs first (see venvs/requirements/).
bash scripts/run_ehr_mcp.sh
bash scripts/run_image_mcp.sh  # multimodal only

# Agentic text-only evaluation
DATA_PATH=... OUTPUT_DIR=outputs/text bash scripts/run_text_eval.sh

# Agentic multimodal evaluation
DATA_PATH=... OUTPUT_DIR=outputs/mm   bash scripts/run_mm_eval.sh

# Curated-input baselines (no tool calls)
python clinseekagent/run_text_curated.py        --backend bedrock --model "Claude Opus 4.6" --data .../ehr_bench.json --output-dir outputs/text_curated
python clinseekagent/run_multimodal_curated.py  --backend bedrock --model "Claude Opus 4.6" --data .../mm_bench.jsonl --bench-root .../mm_bench --output-dir outputs/mm_curated

# Score
python clinseekagent/scoring/score_text.py        --results outputs/text/results.jsonl --benchmark .../ehr_bench.json --output outputs/text/summary.json
python clinseekagent/scoring/score_multimodal.py  --results outputs/mm/results.jsonl  --output-root outputs/mm/scored
```
