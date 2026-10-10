# Curated Input, Full-Record Input, and the Four Inference Modes

The paper compares four inference modes on the same questions, labels, and
temporal policy:

| Mode | Runner | What the model gets |
| --- | --- | --- |
| **ClinSeekAgent** | `run_text.py` / `run_multimodal.py` | task, patient id, prediction time; retrieves evidence with the tools over up to 200 rounds |
| **Single-Pass Retrieval** | same, `--retrieval_mode single_shot` | same tools and record, but all retrieval calls in one turn, then the answer |
| **Curated Input** | `run_text_curated.py` / `run_multimodal_curated.py --view recent` | the source benchmark's evidence package, re-rendered from the policy-filtered record, in one prompt; no tools |
| **Full-Record Input** | same, `--view full` | the patient's whole pre-cutoff record rendered the same way, truncated oldest-first to a 100k-token window |

This guide covers the two record modes (Curated Input and Full-Record Input)
on both benchmarks and the script that runs all four. All four share the
strict temporal policy: the record modes render their prompts from the same
policy-filtered view the agent can query.

## 1. Build the corpus

The record modes read a corpus root built by
`scripts/clean_data/build_evidence_corpus.py`. For every question it loads the
patient through the strict runtime view, applies an independent availability
filter (a row or value is kept only if its information was readable before
the cutoff) and, in the paper's `aggressive` policy mode, blanks any cell whose
text holds a later date. From that one filtered pool it writes two views:

```text
<corpus>/all_data.jsonl                      one row per question (with its prompt template)
<corpus>/evidence/<qid>/recent/<table>.csv   Curated Input: the source benchmark's selection
<corpus>/evidence/<qid>/full/<table>.csv     Full-Record Input: everything visible at the cutoff
<corpus>/_corpus_manifest.json               build summary
```

`recent` is a row subset of `full` by construction. For ClinSeek-Bench-Text,
`recent` is EHR-Bench's original curated selection (parsed from the upstream
`input`) intersected with the filtered pool, and the row's `question` is
re-rendered from it; for ClinSeek-Bench-MM it is the most recent rows of each
table.

### Text

The builder needs the upstream `input` / `question`; restore them first
(see [the data guide](ClinSeek-Bench_text_data_prepare.md#upstream-input--question-not-in-the-public-file)):

```bash
python scripts/data_build/restore_ehr_bench_inputs.py \
  --manifest $CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json \
  --ehr-bench-dir $CLINSEEK_DATA_ROOT/raw/EHR-Bench \
  --output $CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text_with_inputs.json

python scripts/clean_data/build_evidence_corpus.py --bench text \
  --manifest $CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text_with_inputs.json \
  --data-root $CLINSEEK_DATA_ROOT/ehr_bench \
  --out $CLINSEEK_DATA_ROOT/corpora/text \
  --policy-mode aggressive
```

<a id="multimodal"></a>
### Multimodal

The builder needs the strict multimodal manifest with `input_text`
([Step 4 of the multimodal data guide](ClinSeek-Bench_multimodal_data_prepare.md#step-4-render-the-strict-curated-input-manifest))
and the raw MIMIC-IV `hosp/`, `icu/`, `note/` tables for the availability
filter:

```bash
python scripts/clean_data/build_evidence_corpus.py --bench mm \
  --manifest /path/to/ClinSeek-Bench_multimodal_strict.jsonl \
  --ehrxqa-root $MM_BENCH_ROOT/ehrxqa --medmod-root $MM_BENCH_ROOT/medmod \
  --raw-mimic-root $CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv \
  --cache-dir $CLINSEEK_DATA_ROOT/corpora/_cache \
  --out $CLINSEEK_DATA_ROOT/corpora/mm \
  --policy-mode aggressive
```

The multimodal availability filter scans `hosp/admissions.csv`,
`hosp/labevents.csv`, `icu/chartevents.csv`, `icu/outputevents.csv`, and
`note/radiology.csv` (the first run reads the multi-GB `chartevents.csv`; the
result is cached under `--cache-dir`) and the packaged report `.txt` files.
Rebuilt on samples (45 text and 38 multimodal questions), the corpus CSVs and
the prompts produced by `record_context` are byte-identical to the paper
corpus; for some multimodal rows the stored `input_text` field differs, but
only its `<ehr_context>` body, which `record_context` re-renders from the CSVs.

Both builds can be sharded (`--shard I/N`, then `--merge-shards N`); run them
in the agent environment (pandas 3.0.5 is required for byte-identical renders).
`scripts/clean_data/audit_evidence_corpus.py` re-checks a finished corpus.

## 2. Run Curated Input / Full-Record Input

```bash
# ClinSeek-Bench-Text
python clinseekagent/run_text_curated.py \
  --backend vertex --model claude-opus-5 \
  --corpus-root $CLINSEEK_DATA_ROOT/corpora/text --view recent \
  --output-dir outputs/text_recent_opus5

# ClinSeek-Bench-MM (up to 4 images, longest edge 1,568 px, report text up to 4,000 chars)
python clinseekagent/run_multimodal_curated.py \
  --backend vertex --model claude-opus-5 \
  --corpus-root $CLINSEEK_DATA_ROOT/corpora/mm --view full \
  --bench-root $MM_BENCH_ROOT/ehrxqa:$MM_BENCH_ROOT/medmod \
  --output-dir outputs/mm_full_opus5
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--backend` | `bedrock` | `bedrock` (`--model` is a friendly name from `bedrock_region_availability.json`, e.g. `"Claude Opus 4.6"`, or a raw Bedrock model id), `vertex` (Vertex model id), `vllm` (served model name; `--api-base-url`, `--api-key`; any OpenAI-compatible endpoint) |
| `--view` | `recent` | `recent` = Curated Input, `full` = Full-Record Input |
| `--max-context-window` | `100000` | prompt window in tokens (paper: 100k); the Full-Record prompt is truncated oldest-first to a clean timestamp boundary to fit |
| `--context-window` | registry | the model's own window (`clinseekagent/context_limits.py` raises on unknown models) |
| `--max-tokens` | `8192` | output tokens per call |
| `--enable-thinking` / `--disable-thinking` | on | reasoning; `--thinking-budget 4096` for Claude models that accept a fixed budget, `--reasoning-effort high` otherwise |
| `--temperature` | `0.0` | dropped where not accepted |
| `--limit`, `--qids` | — | smoke tests / targeted reruns |

The runner asks the model to end with an `<answer>...</answer>` block, parses
it into a synthetic `ehr.finish` call (so both settings are scored by the same
code), and records per row under `record_context` whether the record was
truncated and from which timestamp (`window_start`). Reruns resume: finished
rows are skipped and failed rows retried.

`--data <manifest>` instead sends each row's own prompt (`question` for text,
`input_text` for MM). For text that prompt is the upstream EHR-Bench rendering,
which predates the temporal policy, so it is **not** the paper's Curated Input;
for MM the strict manifest's `input_text` is policy-filtered but uses the
latest 80 rows of each table rather than the corpus selection. The runners
refuse rows that lack the prompt instead of sending an empty one.

## 3. All four modes

```bash
BENCH=text MODES="agentic single_shot recent_record full_record" \
BACKEND=vertex MODEL=claude-opus-5 \
TEXT_DATA=$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json \
TEXT_CORPUS=$CLINSEEK_DATA_ROOT/corpora/text \
bash scripts/run_four_modes.sh

BENCH=text MODEL_TAG=claude-opus-5 TEXT_DATA=$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json \
bash scripts/score_four_modes.sh
```

For ClinSeek-Bench-MM set `BENCH=mm`, `MM_DATA`, `MM_CORPUS`, `BENCH_ROOT`
(the agentic modes use EHR + image tools by default; `AGENTIC_TOOLS=ehr` for
the EHR-only ablation) and `JUDGE_BACKEND` for scoring. Arms land in
`outputs/four_modes/<bench>_<model tag>_<mode>/`; a finished arm is skipped.
The scoring script gates every arm with `scripts/check_results_complete.py`,
scores it, and prints per-category F1 of each mode against Curated Input with
paired-bootstrap 95% CIs.

Models that exhaust the output budget inside their reasoning (GLM-5.2 and
Kimi K2.6 in the paper) get one answer-only round before scoring, in every
mode: `scripts/force_final_answer.py --src <arm> --dst <arm>_forcedfinal
--backend ... --model ...`.

## Troubleshooting

- `pass exactly one of --data or --corpus-root`: choose the corpus (paper
  setting) or a manifest.
- `UnknownContextWindow`: add the model to `CONTEXT_WINDOWS` in
  `clinseekagent/context_limits.py` or pass `--context-window`.
- Empty predictions: inspect the raw assistant message in `results.jsonl`; the
  parser looks for `<answer>...</answer>`, then embedded JSON, then the final
  lines of the reply.
