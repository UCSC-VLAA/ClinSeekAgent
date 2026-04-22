# Bedrock model catalog

This doc is the **single source of truth** for which Bedrock models we evaluate
and which AWS regions each model is available in.

The data backing these tables lives at
`openresearcher_ehr/bedrock_model_region_availability.json` (copied and extended
from `/fsx-shared/juncheng/EvolverBench/model_region_availability.json`). The
multi-model launcher `openresearcher_ehr/run_multi_model_eval.py` reads that
JSON at startup; this doc is a human-readable mirror you can scan when picking
models for a new evaluation.

## Friendly-name → JSON-key mapping

The launcher's `--models` flag takes **friendly names** (the keys listed below).
They match the top-level keys of the JSON verbatim.

| Friendly name (`--models`) | Family | Dispatch path | Multimodal? |
|---|---|---|---|
| `Claude Opus 4.6` | Anthropic | `_chat_completion_anthropic` | yes |
| `Claude Sonnet 4.6` | Anthropic | `_chat_completion_anthropic` | yes |
| `Claude Haiku 4.5` | Anthropic | `_chat_completion_anthropic` | yes |
| `gpt-oss-120b` | OpenAI OSS | `_chat_completion_openai` | no (text) |
| `gpt-oss-20b` | OpenAI OSS | `_chat_completion_openai` | no (text) |
| `Qwen3-235B` | Qwen (text-only) | `_chat_completion_openai` | no |
| `Qwen3-VL-235B` | Qwen (vision) | `_chat_completion_openai` | yes |
| `Qwen3-32B` | Qwen (text-only) | `_chat_completion_openai` | no |
| `Kimi K2.5` | Moonshot | `_chat_completion_openai` | yes |
| `MiniMax M2.5` | MiniMax | `_chat_completion_openai` | yes |
| `GLM-4.7` | Z.ai | `_chat_completion_openai` | yes |

Dispatch is chosen inside `bedrock_generator.py:_is_anthropic_model` by looking
for the substring `"anthropic"` in the model id. All other models are routed
through the OpenAI-shape path, where responses are normalized by
`_normalize_openai_response` (content=null → "", tool_calls=null → []).

## Region availability

`Status` values come from `bedrock_model_region_availability.json`:
- `OK` — model is live in this region (the launcher shards into it).
- `FAIL` / `NOT_LISTED` — Bedrock lists the model but access is gated or region
  wasn't confirmed; the launcher filters these out.
- `absent` — the entry is missing for that region.

### Claude Opus 4.6  (6/8 OK)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `us.anthropic.claude-opus-4-6-v1` | OK |
| us-east-2 | `us.anthropic.claude-opus-4-6-v1` | OK |
| us-west-2 | `us.anthropic.claude-opus-4-6-v1` | OK |
| eu-central-1 | `eu.anthropic.claude-opus-4-6-v1` | OK |
| eu-west-1 | `eu.anthropic.claude-opus-4-6-v1` | OK |
| eu-west-3 | `eu.anthropic.claude-opus-4-6-v1` | OK |
| ap-northeast-1 | — | absent |
| ap-southeast-2 | — | absent |

### Claude Sonnet 4.6  (6/8 OK)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `us.anthropic.claude-sonnet-4-6` | OK |
| us-east-2 | `us.anthropic.claude-sonnet-4-6` | OK |
| us-west-2 | `us.anthropic.claude-sonnet-4-6` | OK |
| eu-central-1 | `eu.anthropic.claude-sonnet-4-6` | OK |
| eu-west-1 | `eu.anthropic.claude-sonnet-4-6` | OK |
| eu-west-3 | `eu.anthropic.claude-sonnet-4-6` | OK |
| ap-northeast-1 | — | absent |
| ap-southeast-2 | — | absent |

Note: there's also an alias `global.anthropic.claude-sonnet-4-6` available in
`ca-west-1`. The scoring pipeline (`scorer_mm.py`) uses that variant as its
judge model — see `08_multi_model_bedrock_eval_infra.md`.

### Claude Haiku 4.5  (6/8 OK)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | OK |
| us-east-2 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | OK |
| us-west-2 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | OK |
| eu-central-1 | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | OK |
| eu-west-1 | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | OK |
| eu-west-3 | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | OK |
| ap-northeast-1 | — | absent |
| ap-southeast-2 | — | absent |

### gpt-oss-120b  (7/8 OK) — text-only

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `openai.gpt-oss-120b-1:0` | OK |
| us-east-2 | `openai.gpt-oss-120b-1:0` | OK |
| us-west-2 | `openai.gpt-oss-120b-1:0` | OK |
| eu-central-1 | `openai.gpt-oss-120b-1:0` | OK |
| eu-west-1 | `openai.gpt-oss-120b-1:0` | OK |
| eu-west-3 | `openai.gpt-oss-120b-1:0` | FAIL |
| ap-northeast-1 | `openai.gpt-oss-120b-1:0` | OK |
| ap-southeast-2 | `openai.gpt-oss-120b-1:0` | OK |

Cannot accept image content blocks — the OpenAI dispatch in
`bedrock_generator._prepare_openai_messages` flattens image blocks to
`[image attached ({media_type}) — not inlined for this model]` so the text
portion still flows.

### gpt-oss-20b  (7/8 OK) — text-only

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `openai.gpt-oss-20b-1:0` | OK |
| us-east-2 | `openai.gpt-oss-20b-1:0` | OK |
| us-west-2 | `openai.gpt-oss-20b-1:0` | OK |
| eu-central-1 | `openai.gpt-oss-20b-1:0` | OK |
| eu-west-1 | `openai.gpt-oss-20b-1:0` | OK |
| eu-west-3 | `openai.gpt-oss-20b-1:0` | FAIL |
| ap-northeast-1 | `openai.gpt-oss-20b-1:0` | OK |
| ap-southeast-2 | `openai.gpt-oss-20b-1:0` | OK |

### Qwen3-235B  (5/8 OK) — text-only MoE (a22b-2507)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `qwen.qwen3-235b-a22b-2507-v1:0` | FAIL |
| us-east-2 | `qwen.qwen3-235b-a22b-2507-v1:0` | OK |
| us-west-2 | `qwen.qwen3-235b-a22b-2507-v1:0` | OK |
| eu-central-1 | `qwen.qwen3-235b-a22b-2507-v1:0` | OK |
| eu-west-1 | `qwen.qwen3-235b-a22b-2507-v1:0` | FAIL |
| eu-west-3 | `qwen.qwen3-235b-a22b-2507-v1:0` | FAIL |
| ap-northeast-1 | `qwen.qwen3-235b-a22b-2507-v1:0` | OK |
| ap-southeast-2 | `qwen.qwen3-235b-a22b-2507-v1:0` | OK |

### Qwen3-VL-235B  (6/8 OK) — multimodal

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `qwen.qwen3-vl-235b-a22b` | OK |
| us-east-2 | `qwen.qwen3-vl-235b-a22b` | OK |
| us-west-2 | `qwen.qwen3-vl-235b-a22b` | OK |
| eu-central-1 | `qwen.qwen3-vl-235b-a22b` | NOT_LISTED |
| eu-west-1 | `qwen.qwen3-vl-235b-a22b` | OK |
| eu-west-3 | `qwen.qwen3-vl-235b-a22b` | NOT_LISTED |
| ap-northeast-1 | `qwen.qwen3-vl-235b-a22b` | OK |
| ap-southeast-2 | `qwen.qwen3-vl-235b-a22b` | OK |

Appended to the catalog in this session (live-probed via the Bedrock API
because the EvolverBench source JSON only covers the text-only 235B variant).

### Qwen3-32B  (7/8 OK) — text-only dense

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `qwen.qwen3-32b-v1:0` | OK |
| us-east-2 | `qwen.qwen3-32b-v1:0` | OK |
| us-west-2 | `qwen.qwen3-32b-v1:0` | OK |
| eu-central-1 | `qwen.qwen3-32b-v1:0` | OK |
| eu-west-1 | `qwen.qwen3-32b-v1:0` | OK |
| eu-west-3 | `qwen.qwen3-32b-v1:0` | FAIL |
| ap-northeast-1 | `qwen.qwen3-32b-v1:0` | OK |
| ap-southeast-2 | `qwen.qwen3-32b-v1:0` | OK |

### Kimi K2.5  (4/8 OK)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `moonshotai.kimi-k2.5` | OK |
| us-east-2 | `moonshotai.kimi-k2.5` | OK |
| us-west-2 | `moonshotai.kimi-k2.5` | OK |
| eu-central-1 | `moonshotai.kimi-k2.5` | FAIL |
| eu-west-1 | `moonshotai.kimi-k2.5` | FAIL |
| eu-west-3 | `moonshotai.kimi-k2.5` | FAIL |
| ap-northeast-1 | `moonshotai.kimi-k2.5` | OK |
| ap-southeast-2 | `moonshotai.kimi-k2.5` | FAIL (dropped 2026-04-21 — TLS hangs on invoke_model in this region) |

### MiniMax M2.5  (7/8 OK)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `minimax.minimax-m2.5` | OK |
| us-east-2 | `minimax.minimax-m2.5` | OK |
| us-west-2 | `minimax.minimax-m2.5` | OK |
| eu-central-1 | `minimax.minimax-m2.5` | OK |
| eu-west-1 | `minimax.minimax-m2.5` | OK |
| eu-west-3 | `minimax.minimax-m2.5` | FAIL |
| ap-northeast-1 | `minimax.minimax-m2.5` | OK |
| ap-southeast-2 | `minimax.minimax-m2.5` | OK |

### GLM-4.7  (5/8 OK)

| region | bedrock_model_id | status |
|---|---|---|
| us-east-1 | `zai.glm-4.7` | OK |
| us-east-2 | `zai.glm-4.7` | OK |
| us-west-2 | `zai.glm-4.7` | OK |
| eu-central-1 | `zai.glm-4.7` | NOT_LISTED |
| eu-west-1 | `zai.glm-4.7` | NOT_LISTED |
| eu-west-3 | `zai.glm-4.7` | NOT_LISTED |
| ap-northeast-1 | `zai.glm-4.7` | OK |
| ap-southeast-2 | `zai.glm-4.7` | OK |

Appended to the catalog in this session (live-probed via the Bedrock API;
EvolverBench's copy does not yet list GLM-4.7).

## How to run

The multi-model launcher sits at
`openresearcher_ehr/run_multi_model_eval.py`. It fans a single input file
across every OK region for each requested model, runs each
`(model, region)` shard as a separate `deploy_agent_mm.py` process, and
aggregates a summary.

```bash
cd /fsx-shared/juncheng/EHR/openresearcher_ehr

# Dry-run — prints commands + partition sizes, no subprocesses launched
python run_multi_model_eval.py \
  --models "Claude Sonnet 4.6" "GLM-4.7" "Qwen3-VL-235B" \
  --data /fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/combined_test_set_nonempty.jsonl \
  --mode smoke --dry-run

# Smoke (10 samples per shard). Assumes MCPs are already up on :5103/5104/5203.
python run_multi_model_eval.py \
  --models "Claude Sonnet 4.6" "GLM-4.7" "Qwen3-VL-235B" \
  --data .../combined_test_set_nonempty.jsonl \
  --mode smoke --no-start-mcp

# Full partition (2,695 rows round-robin across each model's OK regions)
python run_multi_model_eval.py \
  --models "Claude Sonnet 4.6" "GLM-4.7" "Qwen3-VL-235B" \
  --data .../combined_test_set_nonempty.jsonl \
  --mode full --max-parallel-shards 6
```

Key flags:
- `--models NAME [NAME ...]` — friendly names from the tables above.
- `--data PATH` — input JSONL.
- `--mode {smoke,full}` — smoke truncates each shard to `--smoke-size=10`.
- `--max-parallel-shards N` — how many `(model, region)` shards run at once
  across the whole job (default 4).
- `--concurrency N` — per-shard `--max_concurrency` (default 6).
- `--max-rounds N` — per-shard `--max_rounds` (default 200).
- `--enable-image / --no-enable-image` — image MCP (default on).
- `--no-start-mcp` — driver never starts MCPs; the flag is for symmetry with
  `run_mm_pipeline.sh` and trusts existing servers on the default ports.

Per-shard layout inside `--output-root`:
```
{output_root}/{model_tag}/{region}/
    input.jsonl       shard's share of the input
    results.jsonl     deploy_agent_mm.py output
    run.log           tee'd stdout/stderr
    cmd.txt           exact command run
    pid.txt           subprocess PID
    status.json       {returncode, row counts} written on exit
```

Aggregate output at the top of `--output-root`:
- `manifest.json` — written before any shard starts (replay-friendly).
- `summary.json` + `summary.md` — written after all shards exit with per-shard
  counts of `success`, `incomplete`, `error`, plus `salvage` and `name_fix`
  counters sourced from each shard's `run.log`.

## Related docs

- `08_multi_model_bedrock_eval_infra.md` — notable code changes made in the session that
  shipped this catalog (tool-namespace resolver, plain-text salvage, OpenAI
  `content=null` guard, image-block flatten, per-tool GPU pinning, …).
- `06_run_multimodal_pipeline.md` — the single-model `run_mm_pipeline.sh`
  launcher (unchanged — the multi-model driver composes on top of it without
  modifying the single-model path).
