# Session modifications — 2026-04-20/21

This doc inventories the non-trivial code changes made in this session so the
next reader can orient quickly without re-deriving them. Every entry links to
exact `file:line` anchors.

## Context

The session started with a single-model multimodal pipeline that only
consumed Claude Opus 4.6 via the Anthropic Bedrock dispatch. Over the run we:

1. Scored two full 2,703-row runs (image-off vs image-on) with a Sonnet-4.6
   LLM judge, split by gold-label shape.
2. Extended the pipeline to run any Bedrock model — Anthropic or OpenAI-shape
   OSS — uncovering four distinct OSS compatibility issues and patching each.
3. Built a multi-model, multi-region launcher driven by a shared region
   catalog so one eval fans out across every OK region per model.

The fixes below all landed during that work.

## 1. `openresearcher_ehr/deploy_agent_mm.py`

### 1.1 Multi-root `BENCH_ROOT` (accepts a colon-/comma-separated list)
`deploy_agent_mm.py:163` `resolve_asset_path(path, bench_root)`.
Before: resolved each sample's relative `image_paths` against one bench root,
so a combined EHRXQA + MedMod jsonl couldn't work. Now iterates a list of
roots. The same multi-root handling is mirrored in the image MCP server
(see §4 below).

### 1.2 `<run_context>` preamble builder
`deploy_agent_mm.py:253-317`. Every user turn is now prefixed with a small
`<run_context>` block stating `images_attached: N`, `reports_inlined: M`,
`patient_ehr_available: yes|no`, and (when images exist) an explicit list of
the absolute image paths the agent should quote verbatim. This prevents the
model from inventing paths like `images/<study_id>.jpg` from raw metadata.

### 1.3 Tool-namespace resolver + fuzzy fallback
`deploy_agent_mm.py:65-142`. Some OSS models (Kimi especially) drop the
namespace prefix in tool calls — e.g. they emit `ehr.chest_xray_classifier`
(wrong family) or a bare `chest_xray_classifier` (no namespace) or even
`visualizer` (truncated suffix). Fixed by:

- `_build_tool_namespace_lookup()` (parses `COMBINED_TOOL_CONTENT_MM` as JSON
  into a suffix → namespace dict).
- `_TOOL_NAMESPACE_BY_SUFFIX` (the lookup).
- `_fuzzy_match_suffix(bare)` — substring-contains match against registered
  suffixes, ties broken by shortest-distance.
- `_resolve_tool_namespace(fn)` — exact lookup first, then fuzzy. Logs a
  `TOOL_NAME_FIX` line when it rewrites.

Call site at `deploy_agent_mm.py:535` rewrites the function name before
dispatching to the EHR / browser / image MCP pools.

### 1.4 Plain-text answer salvage (synthesized `ehr.finish`)
`deploy_agent_mm.py:493-525`. Some OSS models (Qwen in particular) follow the
OpenAI contract literally: once they have an answer, they emit text and stop
calling tools, treating empty `tool_calls` as end-of-turn. Without a salvage,
the run loop marks the row `incomplete` with no extractable prediction. Now:
when the model emits non-empty content and zero tool_calls, the last
assistant turn is rewritten in place to carry a synthetic
`ehr.finish({"response": [...items]})` call where `items` comes from
splitting content on newlines / bullets. A matching `role: tool` message is
appended. Downstream scoring sees a well-formed finish trajectory.

Net effect in the 6-model smoke:
- Qwen: 0/10 → 10/10 success (9 via salvage).
- Sonnet: 6/10 → 10/10 success (3 via salvage).
- Kimi: 2/10 → 8/10 success (3 salvage + 16 name_fix events).

## 2. `openresearcher_ehr/bedrock_generator.py`

### 2.1 Clean Anthropic-vs-OpenAI dispatch split
`bedrock_generator.py:460-493` keeps `_is_anthropic_model` (substring match on
`"anthropic"`) as the routing predicate, `_chat_completion_anthropic` at
`:495` handles Anthropic content-blocks, `_chat_completion_openai` at `:638`
handles OpenAI chat-completions shape. No model now slips through to the
wrong path.

### 2.2 OpenAI response normalizer
`bedrock_generator.py:716-734` `_normalize_openai_response(resp)`. OpenAI-shape
models return `content: null` on tool-only turns (and occasionally
`tool_calls: null` on plain-text turns). The agent loop previously crashed
on `len(None)`. The normalizer runs once per response and coerces both fields
to `""` / `[]` in place. It preserves CoT strings some models inline in
`content` (e.g. MiniMax's `<reasoning>…</reasoning>`) unchanged.

### 2.3 Image-block flatten for OSS models
`bedrock_generator.py:563-615` `_prepare_openai_messages`. Anthropic-style
content blocks (`[{"type":"text",...}, {"type":"image","source":{...base64...}}]`)
are rejected by the OSS Bedrock shim. We flatten each user message to a
single text string: text blocks concatenated, image blocks replaced with a
short marker `[image attached (image/jpeg) — not inlined for this model]`.
The `<run_context>` preamble (§1.2) still flows through so the model knows
there was an image even when it can't see it.

## 3. `openresearcher_ehr/run_mm_pipeline.sh`

### 3.1 Per-tool GPU pinning
`run_mm_pipeline.sh:189-205`. Image MCP now starts with
`CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7` plus four per-tool overrides
(`IMAGE_TOOL_DEVICE_CLASSIFIER=cuda:2`,
`IMAGE_TOOL_DEVICE_REPORT_GENERATOR=cuda:3`,
`IMAGE_TOOL_DEVICE_GROUNDING=cuda:4`,
`IMAGE_TOOL_DEVICE_SEGMENTATION=cuda:5`) so the four GPU-backed tools
lazy-load onto distinct devices. EHR MCPs pin to cuda:0 (ehrxqa) and
cuda:1 (medmod) via `EHR_EHRXQA_GPU` / `EHR_MEDMOD_GPU` env vars.

### 3.2 Python-socket port probe
The launcher's `wait_for_port` used to call `ss -lnt`, which fails silently
in restricted shells where `/proc/net/tcp` is unreadable. Now uses
`python -c "import socket; ..."` to do a real TCP connect — works everywhere.

### 3.3 Multi-root `BENCH_ROOT` + separate image venv
`run_mm_pipeline.sh:80` passes a colon-separated pair of bench roots to both
the agent and the image MCP. `run_mm_pipeline.sh:96` uses a dedicated
image-MCP venv (`/fsx-shared/juncheng/EHR/venvs/mcp_image/bin/python`) pinned
to `transformers==4.46.3` (compatible with MAIRA-2's custom modeling code),
keeping the main agent's `transformers==4.57` untouched.

## 4. `src/mcp_image/`

### 4.1 `tools/base.py` multi-root + per-tool device
- `get_bench_roots()` at `base.py:8` returns a list so relative image paths
  resolve across both EHRXQA and MedMod roots.
- `pick_tool_device(tool_tag)` at `base.py:37` reads
  `IMAGE_TOOL_DEVICE_<TAG>` first, falls back to `IMAGE_TOOL_DEVICE`, then
  auto-detects `cuda` / `cpu`.

### 4.2 `run_image_mcp_server.py` warmup at startup
`run_image_mcp_server.py:60-97` adds `_warmup_models()` which eagerly loads
all four GPU-backed models before `mcp.run_async()` binds the port. First
inference request sees zero cold-start latency. `--skip-warmup` kept as an
escape hatch for dev iteration.

## 5. `openresearcher_ehr/scorer_mm.py`

New file. Two-track scorer for the multimodal results:
- `len(gold) == 1` → LLM judge. Uses Claude Sonnet 4.6
  (`global.anthropic.claude-sonnet-4-6` in `ca-west-1` by default).
  Six prompt subtypes picked by gold content + task: `yesno`, `count`,
  `date_time`, `id`, `label_name`, `generic_string`.
- `len(gold) >= 2` → rule-based set F1 / precision / recall / Jaccard
  accuracy / subset-match over normalized name strings.

Judge dispatch uses the same `BedrockAsyncGenerator` the agent uses, with a
runtime shim (`scorer_mm.py:40`) that injects a stub `use_reasoning_content`
global to work around a latent `NameError` in
`bedrock_generator._chat_completion_anthropic`. Same shim as
`deploy_agent_mm.py`.

### 5.1 Multi-region judge pool
`scorer_mm.py:--regions` (multi-value flag) + `_anthropic_id_for_region`
build one `BedrockAsyncGenerator` per region with its own
`asyncio.Semaphore(concurrency)`, then round-robin judge tasks across the
pool. For Claude Sonnet 4.6 across its 6 OK regions this yields an
effective 6× throughput boost without exceeding any single region's rate
limit. Anthropic model-id prefix (`us.` / `eu.`) is swapped per region
automatically; non-Anthropic model ids pass through unchanged.

Example: 6 regions × 10 per-region concurrency = 60 global concurrent
judges. Empirically the full 6-model × 2,695-row judge run (10,080 judge
calls) completes in ~17 min, down from ~45 min at single-region
concurrency=30.

Also: `--concurrency` default lowered from 20 → 10 (per-region cap),
since global concurrency now scales with pool size.

## 6. Multi-model launcher

New files:
- `openresearcher_ehr/bedrock_model_region_availability.json` — copied from
  EvolverBench, extended with `GLM-4.7` (5 OK regions) and
  `Qwen3-VL-235B` (6 OK regions), both live-probed against the Bedrock API.
  Kimi K2.5's `ap-southeast-2` entry was later flipped `OK → FAIL` after TLS
  hangs in that region (see §8.1 below).
- `openresearcher_ehr/run_multi_model_eval.py` — partitions an input JSONL
  round-robin across each requested model's OK regions, runs one
  `deploy_agent_mm.py` shard per `(model, region)` via `ThreadPoolExecutor`,
  writes `manifest.json` up front and `summary.json` / `summary.md` at the
  end. Never modifies `deploy_agent_mm.py` or `bedrock_generator.py` — pure
  subprocess fan-out.
- `docs/07_bedrock_model_catalog.md` — human-readable companion to the JSON.

## 7. Work-stealing across regions (approach B)

### 7.1 Driver work-stealing primitive
`run_multi_model_eval.py:248-351` `try_steal_work(helper, all_shards, ...)`.
When any `(model, region)` shard finishes cleanly, the driver looks for the
**same model's** still-running siblings whose `input.jsonl` has an
un-attempted tail, slices that tail into `input_steal_NN.jsonl` in the
helper's directory, and relaunches `deploy_agent_mm.py` against it with the
helper's region/model_id. The helper keeps stealing until no donor has a
long-enough tail; then it retires.

Key safety properties:
- `_STEAL_LOCK` (a `threading.Lock`) serializes steal decisions so two fast
  helpers don't both drain the same donor.
- `steal_safety_gap = --concurrency` — never steal rows at index `<
  donor.results_count + concurrency`, because those N in-flight rows might
  land in the donor's `results.jsonl` after we snapshot. Any surviving
  duplicate is deduped by `qid` in the aggregator.
- `min_tail = max(2, concurrency // 2)` — avoid launching a helper for 1-2
  rows where the fixed `deploy_agent_mm.py` startup cost dominates the work
  being stolen.
- `--no-work-steal` flag to disable entirely.

### 7.2 Aggregator dedup (within-shard)
`run_multi_model_eval.py:418-478`. `aggregate()` now reads each shard's
`results.jsonl` **plus** any `results_steal_*.jsonl` files it produced as a
helper, dedupes by `qid`, and also pulls `salvage` / `name_fix` counts from
each `run_steal_*.log`. Shard-level counts reflect total unique rows
processed, including stolen work.

### 7.3 Cross-shard dedup (within a model)
Initial full-set run produced `success > in` because work-stealing can
write the same qid into both the donor's primary results (in-flight rows
that landed after the steal snapshot) and the helper's `results_steal_*`.
`aggregate()` now does a two-pass sweep per model: pass 1 walks every
shard's primary + stolen files in stable (region-sorted) order and
attributes each qid to the **first shard that wrote it** (primary beats
steal). Pass 2 assembles per-shard + per-model totals from that
attribution. Side effect: summary now also reports a `stolen` column
showing how many of a shard's rows were donated-to-help work (distinct
from its own primary output). After the fix, `out == in == 2,695` per
model for the headline full-set run.

## 8. Bedrock timeout hardening

### 8.1 Bounded read timeout + asyncio hard ceiling
`bedrock_generator.py:66-72` sets `BotoConfig(read_timeout=90,
connect_timeout=10, retries.max_attempts=0)`. Previously the read timeout
was 300 s — long enough that a silent TLS hang (observed for Kimi in
`ap-southeast-2`) could block a whole driver waiting on one wedged shard.

Layered on top, both invoke paths now wrap the `run_in_executor(...)` call
in `asyncio.wait_for(..., timeout=120)`:
- Anthropic path: `bedrock_generator.py:175-196` in
  `_invoke_model_with_retry` — `TimeoutError` is retriable up to
  `API_MAX_RETRIES`.
- OpenAI path: `bedrock_generator.py:705-742` in
  `_chat_completion_openai` — explicit `asyncio.TimeoutError` branch re-raises
  as `TimeoutError` after retry budget is exhausted.

This means a hung Bedrock invoke can never block the async event loop for
more than ~2 min, regardless of what boto3 / the TLS stack does. The
original Kimi `ap-southeast-2` hang motivated this; the region is also
marked `FAIL` in the catalog so future runs skip it outright.

## 9. Vocabulary-normalized re-scoring (`rescore_mm_vocab.py`)

### 9.1 Why it exists
The original scorer penalizes exact-string mismatches on multi-label
tasks. Phenotyping gold labels come from the 25-entry AHRQ CCS vocabulary
(`"Respiratory failure; insufficiency; arrest (adult)"`,
`"Septicemia (except in labor)"`, ...). Models that answered with the right
concept in different wording (`"Acute Respiratory Failure"`,
`"Sepsis"`) scored **F1 = 0** under exact-set match. Same problem on
`medmod_radiology` (14-entry CheXpert-style vocabulary) and `ehrxqa_image`
(~80-entry CXR-finding vocabulary). Sonnet phenotyping F1 was **0.03**
pre-normalization; per-prediction inspection showed it was identifying the
correct phenotypes nearly every time.

### 9.2 Design
New offline post-processor — agent + judge untouched. The script:

1. Builds per-task vocabularies from all gold labels in
   `combined_test_set_nonempty.jsonl` (25 phenotypes, 14 radiology,
   81 ehrxqa_image entries, 2 yes/no for decompensation/mortality).
2. Loads `sentence-transformers/all-MiniLM-L6-v2` on CPU.
3. For each `scored.jsonl` len≥2 row, maps every predicted string to the
   nearest vocab entry by cosine similarity. Below a 0.55 threshold the
   raw prediction is kept (so out-of-vocabulary guesses can still score via
   exact match).
4. Recomputes precision / recall / F1 / Jaccard accuracy / subset match on
   the mapped predictions, writes `rescored.jsonl` + `summary_vocab.md` /
   `summary_vocab.json` alongside the originals.
5. `--skip-tasks` defaults to `ehrxqa_table` because its "vocabulary" is
   2,580 cohort IDs / numbers / dates that can't meaningfully be
   normalized. `len=1` judge verdicts pass through unchanged.

`rescore_mm_vocab.py:VocabMatcher` caches task → vocab-embedding matrix so
each vocab is embedded once. Predictions are batched per row via
`map_many` (single `encode(...)` call per row → dot-product over the cached
matrix).

### 9.3 Effect on the 6-model comparison
Every model gains on overall len≥2 F1. The gap between Opus and Sonnet on
phenotyping narrows from **40 pp → 21 pp**; absolute Sonnet phenotyping F1
jumps 0.03 → 0.26.

| Model | len≥2 F1 before | after | Δ |
|---|---:|---:|---:|
| Claude Opus 4.6 | 0.2935 | 0.3780 | +8.5 pp |
| Claude Sonnet 4.6 | 0.1575 | 0.2634 | +10.6 pp |
| MiniMax M2.5 | 0.0680 | 0.1540 | +8.6 pp |
| Kimi K2.5 | 0.1491 | 0.2189 | +7.0 pp |
| Qwen3-VL-235B | 0.0788 | 0.1327 | +5.4 pp |
| GLM-4.7 | 0.0403 | 0.0936 | +5.3 pp |

Outputs: `results/multi_eval/full5_20260421T091517Z/scored_vocab/<model>/
{rescored.jsonl,summary_vocab.md,summary_vocab.json}`. Each `rescored.jsonl`
row carries both the original `prediction_strs` and the new
`prediction_strs_mapped` + `mapping_similarity` so every mapping decision
is auditable.

## Untouched by this session

- `openresearcher_ehr/deploy_agent.py` (text-only path, byte-identical).
- `openresearcher_ehr/run.sh` (single-model text launcher, unchanged).
- `openresearcher_ehr/data_utils.py` (shared schemas, untouched — the
  multimodal variant lives in `data_utils_mm.py`).
- `src/run_mcp_server.py` (EHR MCP server, unchanged).
