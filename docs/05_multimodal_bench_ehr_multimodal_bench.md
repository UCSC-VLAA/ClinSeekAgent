# `Chtholly17/EHR_multimodal_bench` — Data Analysis & Inference Pipeline Integration

This document describes the HuggingFace dataset
[`Chtholly17/EHR_multimodal_bench`](https://huggingface.co/datasets/Chtholly17/EHR_multimodal_bench)
after it has been downloaded and decompressed into
`/fsx-shared/juncheng/EHR/data/`. It focuses on:

1. What the new bench actually contains (two benchmarks, schemas, splits, sizes).
2. How it differs from the previous `MIMICIVAgentBench` / `MIMICIIIAgentBench`
   data that already lives in `/fsx-shared/juncheng/EHR/data/`.
3. How it can be consumed by the inference pipeline at
   `/fsx-shared/juncheng/EHR/openresearcher_ehr/` (and the underlying MCP
   stack under `src/`), including the concrete changes that would be needed —
   without modifying any existing code file.

> All counts / schemas in this doc were verified directly against the extracted
> files on 2026-04-18. Sources: per-archive `release_manifest.json`,
> `build_summary.json`, `metadata.json`, and `common/ready/summary.json`, plus
> direct `sqlite_master` / `PRAGMA table_info` probing of patient DBs.

---

## 1. Download & layout

Dataset was downloaded with:

```bash
hf download Chtholly17/EHR_multimodal_bench \
    --repo-type dataset \
    --local-dir /fsx-shared/juncheng/EHR/data/EHR_multimodal_bench
```

Top-level layout of the HF repo after download:

```text
/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/
├── EHRXQAAgentBench_v3.zip            # single 5.7 GB archive for EHRXQA bench
└── MedMod/                             # multi-part archives for MedMod bench
    ├── MedModAgentBench_v3_database_patients_10m.zip  ... _19m.zip   (10 shards)
    ├── MedModAgentBench_v3_database_seed.zip          (reference + candidate DBs)
    ├── MedModAgentBench_v3_full_manifests_*.zip       (6 task-level manifests)
    ├── MedModAgentBench_v3_ready_manifests.zip        (default entry)
    ├── MedModAgentBench_v3_ready_databases.zip        (patient DBs referenced by ready)
    ├── MedModAgentBench_v3_ready_images.zip           (CXR JPGs referenced by ready)
    ├── MedModAgentBench_v3_test_only_portable.zip     (tiny smoke test)
    ├── MedModAgentBench_v3_mimic_cxr_files.zip        (full 108 k JPG mirror)
    ├── README.md / checksums.json / release_manifest.json / complete_packaging_plan.json
```

There are **two independent benchmarks** packaged in this repo:

| Benchmark | Source QA | Modalities | Default entry |
|-----------|-----------|------------|----------------|
| **EHRXQAAgentBench_v3** | EHRXQA 1.0 questions | CXR image + relational EHR | `common/ready/{train,valid,test,merged}.json` |
| **MedModAgentBench_v3** | MedMod clinical outcome tasks (mortality, decomp, LOS, phenotyping, radiology) | CXR image + ICU EHR | `common/ready/{train,valid,test,merged}.json` |

The current **decompressed copy** lives at:

```text
/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/
├── EHRXQAAgentBench_v3/
│   ├── common/{ready,full,ready_subset_500,subset_500}/
│   ├── database/                      (2 924 patient DBs + candidate_table.db)
│   ├── mimic-cxr/2.0.0/{files,mimic-cxr-reports}/
│   ├── source_release/ehrxqa_1.0.0/
│   ├── table_description/, asset_manifest/, README.md, metadata.json
└── MedModAgentBench_v3/
    ├── common/ready/                  (5 task JSONs + merged + 3 splits + summary.json)
    ├── database/                      (9 075 patient DBs + candidate + reference)
    ├── mimic-cxr/2.0.0/files/         (34 899 JPGs for ready)
    ├── table_description/, asset_manifest/, README.md, metadata.json
```

Only the **ready-tier** of MedMod was extracted (manifests + ready databases +
ready images). The `full` manifests and the 60 k full patient-DB pool remain as
zip archives in `MedMod/` and can be extracted on demand (they add ~40 GB).

### 1.1 Storage footprint

| Path | Size |
|------|------|
| `EHR_multimodal_bench/EHRXQAAgentBench_v3.zip` | 5.7 GB |
| `EHR_multimodal_bench/MedMod/*.zip` (all 22 archives) | ~10 GB |
| `extracted/EHRXQAAgentBench_v3/` | 4.5 GB (db 5.1 GB — sparse files) |
| `extracted/MedModAgentBench_v3/` | ~5.6 GB (db 4.4 GB, cxr 1.1 GB) |

`/fsx-shared` currently has ~8.3 TB free (83 % used) — extracting the full MedMod tier is safe if needed.

---

## 2. Per-benchmark content

### 2.1 EHRXQAAgentBench_v3 (EHRXQA in AgentEHR clothing)

This is an AgentEHR-style repackaging of the official answered EHRXQA 1.0
release. Patient SQLite DBs follow the MIMIC-IV `hosp`/`icu` schema. A single
added table `tb_cxr` links each patient to their chest-X-ray studies.

**Splits (`common/ready/`):**

| split | rows | unique subjects | image rows | report rows |
|-------|------|-----------------|-----------|-------------|
| train | 16 789 | 1 780 | 3 905 | 3 905 |
| valid | 2 420 | 925 | 581 | 581 |
| test  | 2 220 | 665 | 514 | 514 |
| merged (train+valid+test) | 21 429 | 2 509 | 5 000 | 5 000 |
| full (source-aligned, incl. non-ready rows) | 46 152 | 2 922 | — | — |

**Task (`task` field):**

- `ehrxqa_table`   — table-only EHR QA (≈ 77 %)
- `ehrxqa_image`   — CXR-image-only QA (≈ 22 %)
- `ehrxqa_image_table` — joint CXR + EHR QA (≈ 1 %)

**Scope:**

- `patient_scope` — question about one subject (~70 %)
- `cohort_scope` — cohort / global counting question (~30 %)

**Convenience subsets** (for debugging / smoke tests):

- `common/ready_subset_500/merged_subsets_500.json` (500 ready rows)
- `common/subset_500/merged_subsets_500.json` (500 rows including non-ready)

**Source provenance:** `source_release/ehrxqa_1.0.0/{dataset,database,index.html}`
retains the raw official EHRXQA release for traceability.

### 2.2 MedModAgentBench_v3 (MedMod multimodal clinical tasks)

MedMod is a simpler, **leakage-controlled** multimodal ICU benchmark. Every
ready row has exactly one linked CXR image, which makes it the primary target
for "CXR+EHR" agent evaluation.

**Splits (`common/ready/`), from `build_summary.json`:**

| split | rows |
|-------|------|
| train | 1 499 855 |
| valid | 152 113 |
| test  | 392 811 |
| merged | 2 044 779 |

Ready subjects: **9 073**. Benchmark subjects (incl. 26 orphan DBs): 60 731.
Source-aligned `full` layer is ~12.2 M rows (not extracted by default).

**Tasks (`task` field, ready-tier):**

| Task | Ready rows | Answer format |
|------|-----------:|---------------|
| `medmod_decompensation` | 994 244 | yes/no (per-hour ICU deterioration) |
| `medmod_length_of_stay` | 998 984 | regression/bucketed ICU LOS |
| `medmod_phenotyping`    |  10 447 | set of HCUP phenotype labels |
| `medmod_in_hospital_mortality` | 6 205 | yes/no |
| `medmod_radiology`      |  34 899 | list of CXR findings (image-only) |

All ready rows carry `modalities` that include `cxr_image` + `ehr` (except
`medmod_radiology`, which is `cxr_image` + `cxr_table` only). Every ready row
has exactly one linked study ID, one DICOM, one JPG.

**Key design differences from AgentEHR:**

- **Leakage policy is enforced in the manifest.** `metadata.json` declares a
  `leakage_policy` that drops the `diagnoses` table, strips outcome columns
  (`outtime`, `los`, `dischtime`, `deathtime`, `dod`, `mortality_*`) from `stays`,
  and sets `tb_cxr.studydatetime` / `events.charttime` / `stays.intime` as
  the row-timestamp columns that must be truncated at `prediction_time`.
- **Radically collapsed EHR schema.** Patient DBs contain only three tables
  (see §3.2): `events`, `stays`, `tb_cxr` — unlike the MIMIC-IV hosp/icu
  expansion used by EHRXQA and AgentEHR.
- **Deterministic evaluation contract.** Manifests declare `answer_type`,
  `evaluation_json_allowlist`, and `evaluation_record_allowlist` so that
  evaluation code knows exactly which fields it may observe.

---

## 3. Schema comparison

### 3.1 Sample manifest schema

A MedMod ready sample (trimmed):

```json
{
  "qid": "medmod_radiology_test_50376803",
  "task": "medmod_radiology",
  "task_family": "qa",
  "answer_type": "list",
  "scope": "patient_scope",
  "subject_id": 10001884,
  "hadm_id": 26184834,
  "stay_id": 37510196,
  "prediction_time": "2131-01-15 04:45:09",
  "question": "<task_instruction>...<patient_info>...<cxr_info>...<question>...",
  "label": [{"name": "Cardiomegaly"}, {"name": "Support Devices"}],
  "modalities": ["cxr_table", "cxr_image"],
  "db_path_hint": "database/patient_10001884.db",
  "study_ids": [50376803], "dicom_ids": ["..."],
  "image_paths": ["mimic-cxr/2.0.0/files/p10/p10001884/s50376803/....jpg"],
  "report_paths": [],
  "snapshot_spec": {
    "cutoff_time": "2131-01-15 04:45:09",
    "time_policy": "inclusive_leq_cutoff",
    "cxr_policy": "linked_studies_only",
    "table_policy": "rows_visible_if_event_time_leq_cutoff",
    "cohort_materialization": null
  },
  "source_benchmark": "medmod",
  "source_split": "train",
  "official_split": "train"
}
```

An EHRXQA ready sample has the same core fields plus may include `report_paths`
pointing at `mimic-cxr/2.0.0/mimic-cxr-reports/files/...txt`. For cohort-scope
questions `subject_id`/`hadm_id`/`stay_id` may be `null`.

### 3.2 Per-benchmark patient-DB schema

| Benchmark | Tables in each `patient_<id>.db` |
|-----------|-----------------------------------|
| **AgentEHR MIMIC-IV** (old) | `patients`, `admissions` (impl. via transfers), `labevents`, `prescriptions`, `microbiologyevents`, `procedures_icd`, `diagnoses_icd` (in some tasks), `transfers`, `edstays`, `triage`, `vitalsign`, `pyxis`, `omr`, `diagnosis` |
| **AgentEHR MIMIC-III** (old) | similar MIMIC-III hosp tables |
| **EHRXQAAgentBench_v3** | `patients`, `admissions`, `chartevents`, `cost`, `diagnoses_icd`, `icustays`, `inputevents`, `labevents`, `microbiologyevents`, `outputevents`, `prescriptions`, `procedures_icd`, `transfers`, **`tb_cxr`** |
| **MedModAgentBench_v3** | **`events`**, **`stays`**, **`tb_cxr`** only |

Notes:

- MedMod's `events` is an itemid-keyed long-table (`subject_id, hadm_id, stay_id,
  charttime, itemid, value, valuenum`). The `d_items` / `d_labitems` mapping
  tables live in the shared `database/reference_table.db`. This is a **major
  schema shift** vs. the old MIMIC-IV hosp/icu style split that all existing
  agentlite/MCP tools assume.
- MedMod `stays` ships with leakage-sensitive columns **already stripped**
  (`outtime`, `los`, `dischtime`, `deathtime`, `dod`, `mortality_*`).
- `tb_cxr` is the new image-link table. Columns:
  `subject_id, study_id, studydatetime, split, image_id, image_path,
  viewposition, hadm_id, stay_id` (+ `report_path` in EHRXQA).
- EHRXQA also ships a `cost` table absent from both AgentEHR and MedMod.

### 3.3 Shared reference / candidate DBs

`database/candidate_table.db` exists in **all three** benchmarks with exactly
the same seven `*_candidates` tables used by `ehr.get_candidates_by_*` tools:

- `diagnoses_ccs_candidates` (87 103 rows in MedMod)
- `procedures_ccs_candidates` (83 131)
- `labevents_candidates` (1 619)
- `microbiologyevents_candidates` (171)
- `prescriptions_atc_candidates` (156 201)
- `transfers_candidates` (38)
- `radiology_candidates` (963)

MedMod additionally ships `database/reference_table.db` with
`d_icd_diagnoses`, `d_icd_procedures`, `d_items`, `d_labitems` — this is the
ref layer our `EHRManager._load_reference_data` already expects.

### 3.4 Table description layer

| Benchmark | `table_description/` content |
|-----------|------------------------------|
| AgentEHR (old) | Rich human-written `description` / `columns` for every MIMIC-IV table (well-engineered prompt priming) |
| EHRXQA | **Empty placeholder files** (`link_information.json`, `shorten_description.json` 0 bytes in the ready archive) |
| MedMod | **Minimal auto-generated** per-column stubs (no human description, just "Column 'x' in table 'y'") |

This is the biggest **prompt-priming regression**: the new bench ships without
hand-written schema docs that the old agents rely on.

---

## 4. What's different vs. the existing `/fsx-shared/juncheng/EHR/data/`

Side-by-side summary:

| Dimension | Old `MIMICIVAgentBench` / `MIMICIIIAgentBench` | New `EHR_multimodal_bench` (EHRXQA + MedMod) |
|-----------|------------------------------------------------|---------------------------------------------|
| **Modalities** | EHR tables only | EHR tables **+ CXR images + CXR reports** |
| **Patient DB count** | 9 857 (IV) + 1 002 (III) | 2 924 (EHRXQA ready) + 9 075 (MedMod ready); 60 757 total MedMod available |
| **Task framing** | Diagnosis, procedures, labevents, prescriptions, microbio, transfers (set-prediction) | 2 families: (a) EHRXQA free-form QA, (b) MedMod outcome prediction (binary/regression/set) |
| **Answer schema** | `label`: list of `{name, icd_code, itemid, ...}` w/ code fields | `label`: list of `{name}` (flat) plus `answer_type` |
| **Prediction framing** | Always `patient_scope`; prompt is regenerated at runtime from `TASK_PROMPT_TEMPLATES` using `subject_id` + `prediction_time` | Prompt is **pre-rendered** into the `question` field (`<task_instruction>`, `<patient_info>`, `<cxr_info>`, `<question>` blocks); also has cohort scope |
| **CXR evidence** | None | `study_ids`, `dicom_ids`, `image_paths`, `report_paths`, `snapshot_spec.cxr_policy` |
| **Leakage control** | Implicit (runtime truncation at `prediction_time` via `EHRManager`) | Explicit `leakage_policy` in `metadata.json`; outcome columns already physically removed from DBs |
| **Identifier style (MedMod)** | subject_id like `10008257` (8 digit) | subject_id like `110021093` (9 digit, packed into 10m–19m shards) |
| **DB tables per patient** | 10–14 (hosp/icu/ed) | EHRXQA: 14 (MIMIC-IV-like + `tb_cxr`); MedMod: 3 only (`events`, `stays`, `tb_cxr`) |
| **Table descriptions** | Hand-written | Empty (EHRXQA ready) or auto-stub (MedMod) |
| **Candidate tables** | Same 7 `*_candidates` tables | Identical (plus MedMod has reference_table.db) |
| **Path contract** | `ehr_path + "database/patient_<id>.db"`, hardcoded in `EHRManager._get_db_file_path` | Each sample carries `db_path_hint` (relative) — loader joins with benchmark root |
| **Default entry files** | `MIMICIVAgentBench/common/*_500.json`, `all/*_all.json`, `train/mix_600.json` | `<benchmark_root>/common/ready/{train,valid,test,merged}.json` |
| **Splits** | `common/` (500 eval), `all/` (full eval), `rare/` (OOD), `train/` (SFT) | Real `train/valid/test` splits with much larger scale (MedMod: 1.5 M train rows) |

Implications:

1. The **manifest schema is a near-superset** of the old one for non-image tasks
   (same `subject_id`, `prediction_time`, `task`, `label` primitives). So a
   consumer that filters to EHR-only tasks can plug in with minimal code
   changes.
2. The **DB schema is NOT compatible** for MedMod — the hosp/icu tables that
   `record_tools` / `table_tools` queries expect simply don't exist in
   `patient_<id>.db` under MedMod. The 3-table `events/stays/tb_cxr` layout
   requires a different query surface.
3. The **prompt is pre-rendered**, so the pipeline's
   `generate_question_from_task` template logic must be bypassed — `item["question"]` should be used verbatim.
4. The **CXR pathway is entirely new** and there is no image-tool path today in
   the pipeline (no Bedrock multimodal payload, no `image_url`/`base64` handling
   in `ehr_pool.py`, and no `tb_cxr` awareness).

---

## 5. Fit with the existing inference pipeline
   (`/fsx-shared/juncheng/EHR/openresearcher_ehr/`)

### 5.1 Pipeline-relevant facts

From `openresearcher_ehr/`:

- `deploy_agent.py` loads queries via `load_query_data()` and dispatches them
  to Bedrock Claude (`us.anthropic.claude-opus-4-6-v1` by default).
- `resolve_question(item, data_path)` returns `item["question"]` if present,
  else falls back to `generate_question_from_task(item)` (the old
  `TASK_PROMPT_TEMPLATES`).
- `resolve_qid(item)` accepts either an explicit `qid` or builds one from
  `task_subject_id(_hadm_id)`.
- EHR access is entirely mediated by `ehr.load_ehr(subject_id, timestamp)` →
  MCP server → `EHRManager.load_ehr_for_sample(subject_id, timestamp)` →
  hardcoded `os.path.join(self.data_path, "database", f"patient_{subject_id}.db")`.
- The agent has **no image capability**: tool schemas only expose
  `browser.*` and `ehr.*` (7 EHR tools), none of which deal with pixels.

Consequence: **EHRXQA-table and MedMod non-radiology tasks can almost run
end-to-end today** if we point the MCP server at the right benchmark root and
bypass the AgentEHR prompt generator. Image-heavy tasks (`ehrxqa_image`,
`medmod_radiology`) cannot — we'd need a multimodal content builder.

### 5.2 What runs out-of-the-box

Out-of-the-box = no code changes, only configuration.

| New task | Works today? | Notes |
|---------|:---:|-------|
| `ehrxqa_table` (patient_scope) | ✅ | Same MIMIC-IV-like schema ⇒ existing `record_tools` / `table_tools` work. `question` field pre-rendered, so `resolve_question` already uses it verbatim. |
| `ehrxqa_table` (cohort_scope) | ⚠️ | No `subject_id` ⇒ `ehr.load_ehr` cannot be called. Cohort tool path doesn't exist; tool calls will fail. |
| `ehrxqa_image` / `image_table` | ❌ | Needs multimodal input pipeline. |
| `medmod_radiology` | ❌ | Image-only. |
| `medmod_in_hospital_mortality` / `decompensation` / `length_of_stay` / `phenotyping` | ⚠️ | EHR schema is different (`events`/`stays` only). Existing tools that expect `labevents`, `prescriptions`, etc. will return empty; SQL fallback works but prompts must be re-engineered. |

### 5.3 Concrete config recipe for an EHRXQA-table-only run

Start the MCP server against the EHRXQA root:

```bash
cd /fsx-shared/juncheng/EHR
python src/run_mcp_server.py \
    --mode http \
    --host 127.0.0.1 \
    --port 5103 \
    --data_path /fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3
```

Invoke the pipeline — existing `run.sh` works after swapping env vars:

```bash
cd /fsx-shared/juncheng/EHR/openresearcher_ehr
EHR_MCP_URL=http://127.0.0.1:5103/mcp \
DATA_PATH=/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3/common/ready_subset_500/merged_subsets_500.json \
OUTPUT_DIR=./results/ehrxqa_ready500 \
bash run.sh
```

Required filter so the agent doesn't try to call tools on cohort-scope rows:

```bash
# e.g. a JSON filter step prior to run.sh
python -c "
import json, sys
src='.../EHRXQAAgentBench_v3/common/ready/test.json'
data=json.load(open(src))
data=[r for r in data if r['task']=='ehrxqa_table' and r.get('scope')=='patient_scope' and r.get('subject_id')]
json.dump(data, open('ehrxqa_table_patient_test.json','w'))
"
```

`db_path_hint` is relative to the benchmark root, which matches what
`EHRManager._get_db_file_path` already constructs
(`<data_path>/database/patient_<id>.db`) — so no path rewriting is needed as
long as `--data_path` points at the extracted benchmark root.

### 5.4 What to change to fully support the new benches

These are the minimum edits needed; **this doc is exploration-only and will
not edit the code.** Each bullet lists the file path + the nature of the
change.

**5.4.1 EHRXQA-table full support (incl. cohort_scope):**

- `src/agentlite/commons/EHRManager.py`
  - Teach `load_ehr_for_sample` about a "cohort mode" where `subject_id=None`
    is accepted and no patient DB is loaded (only ref + candidate remain).
- `src/run_mcp_server.py`
  - Expose a new tool (e.g. `load_cohort`) or make `subject_id` optional on
    `load_ehr`.
- `openresearcher_ehr/deploy_agent.py`
  - `resolve_qid`: if `scope=="cohort_scope"`, use `qid` directly without
    requiring `subject_id`.
  - Branch on `scope` when seeding the first tool call in the prompt.

**5.4.2 MedMod EHR tasks (IHM / decomp / LOS / phenotyping):**

- Prompt engineering: existing `TASK_PROMPT_TEMPLATES` in
  `openresearcher_ehr/data_utils.py` only cover the old 6 tasks. MedMod rows
  already carry a fully rendered `question`, so `resolve_question` picks it up
  — no template work needed.
- Tool surface mismatch: `agentlite/mcp_tools/record_tools.py` and
  `table_tools.py` assume `labevents`, `prescriptions`, ... — under MedMod
  only `events`/`stays`/`tb_cxr` exist. Either:
  - ship a per-benchmark `table_description/*.json` override directory, or
  - rely on `ehr.run_sql_query` (already in the tool surface) plus a custom
    schema-priming prefix in the system prompt.
- New `table_description/shorten_description.json` should be written (the one
  MedMod ships is the auto-stub one). Reuse the LLM to generate descriptions
  from `d_items` + `d_labitems` keyed by `itemid`.
- Leakage policy: `EHRManager` currently does row-level truncation at
  `prediction_time` by scanning for `time`/`date` columns. MedMod's
  `leakage_policy` in `metadata.json` is already enforced at build time, but
  our manager should also know to **drop** tables/columns listed there if
  someone ever plugs in the `common/full` tier.

**5.4.3 Multimodal (CXR) support:**

- Bedrock payload: `deploy_agent.py` currently builds text-only
  `{"role": "user", "content": question}`. For Anthropic/Bedrock Claude
  multimodal we'd need to convert the `image_paths` from a sample into
  `{"type": "image", "source": {...}}` blocks injected next to the `user`
  content. Reports (`report_paths`) can be read inline as extra `text` blocks.
- New MCP tool for CXR metadata: e.g. `ehr.get_linked_cxr_studies(subject_id,
  timestamp)` that queries `tb_cxr` honoring `snapshot_spec.cxr_policy`
  (`linked_studies_only` vs. `all_studies_before_cutoff`). Today
  `tb_cxr` is invisible to the tool schemas.
- Evaluation: `medmod_radiology` is set-F1 over finding names, so the existing
  set-F1 evaluator reuses directly. `in_hospital_mortality` and
  `decompensation` need a dedicated yes/no evaluator;
  `length_of_stay` needs a regression evaluator. The build summary mentions
  "phenotyping is closest to AgentEHR set-F1".

### 5.5 Integration checklist

A minimal "add MedMod to our pipeline" checklist:

- [ ] Extract `MedModAgentBench_v3_ready_{manifests,databases,images}.zip`
      (already done: `extracted/MedModAgentBench_v3/`).
- [ ] Start MCP server with `--data_path ...extracted/MedModAgentBench_v3`.
- [ ] Filter manifests by `task` / `scope` to restrict to what the pipeline
      can currently handle (EHR-only, patient_scope).
- [ ] Override `table_description/shorten_description.json` with richer
      descriptions (auto-generate from `events` itemid frequency, or hand-write
      3 tables — small surface).
- [ ] Verify one `medmod_phenotyping` sample end-to-end with
      `run_test_subset_bedrock.sh` (smallest task — 10 447 rows).
- [ ] Add multimodal support incrementally only if image tasks are in scope.

---

## 6. Known gotchas

1. **Cohort-scope rows have `subject_id: null`.** The old agent always
   required a non-null subject_id — any run over `ehrxqa_table` must filter
   to `scope == "patient_scope"` until cohort support is added.
2. **Zero `report_paths`** on MedMod ready (every `report_paths` is `[]`),
   despite `metadata.json` listing the field. Reports must be read via
   `tb_cxr.report_path` against the DB, or via the raw MIMIC-CXR report tree
   (only shipped for EHRXQA, not MedMod).
3. **MedMod patient DBs have ~9 digit subject_ids** (e.g. `110021093`). These
   do **not** collide with existing 8-digit AgentEHR ids, but string-typed
   `subject_id` fields in the pipeline should stay as strings — avoid any
   `int(subject_id)` coercion.
4. **`source_current_time` == `prediction_time`** in MedMod. Use
   `prediction_time` as the authoritative cutoff — the other is informational.
5. **`snapshot_spec.cohort_materialization`** is `null` for every extracted
   ready row, so cohort materialization is always "lazy" → our SQL execution
   needs to respect `inclusive_leq_cutoff` (our current code uses `<`; the
   spec says `≤`). For strict parity a fix to
   `EHRManager.load_ehr_for_sample` (`<` → `≤`) is needed.
6. **Table description layer is empty/stub** — agents that rely on
   `ehr.get_table_description` (or prompt it in) will receive low-signal text.
7. **No `full` tier extracted** — if full-bench evaluation is ever needed,
   the 20 additional `MedModAgentBench_v3_*.zip` archives and 18 GB of full
   manifests must be unpacked. Space is available.

---

## 7. Appendix — concrete file-path contracts

All sample paths below are relative to the benchmark root:

| Field | Example |
|-------|---------|
| `db_path_hint` | `database/patient_10001884.db` |
| `image_paths[*]` | `mimic-cxr/2.0.0/files/p10/p10001884/s50376803/469d0d94-....jpg` |
| `report_paths[*]` (EHRXQA only) | `mimic-cxr/2.0.0/mimic-cxr-reports/files/p12/p12215941/s55608075.txt` |
| `tb_cxr.image_path` | `mimic-cxr/2.0.0/files/p10/p10001884/s50376803/....jpg` |
| `tb_cxr.report_path` (EHRXQA only) | `mimic-cxr/2.0.0/mimic-cxr-reports/files/...txt` |

Resolve via `os.path.join(BENCH_ROOT, field_value)` where

- `BENCH_ROOT = .../extracted/EHRXQAAgentBench_v3` **or**
- `BENCH_ROOT = .../extracted/MedModAgentBench_v3`

---

## 8. Quick-reference numbers

```text
# Storage
HF archive total: ~16 GB (download)
Extracted EHRXQA:   ~4.5 GB
Extracted MedMod (ready tier only): ~5.6 GB

# EHRXQA ready manifest
train 16 789 / valid 2 420 / test 2 220 / merged 21 429
unique subjects (merged): 2 509
CXR-linked samples: 5 000
q_tag families: 238

# MedMod ready manifest
train 1 499 855 / valid 152 113 / test 392 811 / merged 2 044 779
unique subjects (ready): 9 073
total patient DBs available (incl. full): 60 757

# Candidate tables (shared schema with AgentEHR)
diagnoses_ccs_candidates : 87 103
procedures_ccs_candidates: 83 131
prescriptions_atc_candidates: 156 201
labevents_candidates : 1 619
microbiologyevents_candidates: 171
radiology_candidates : 963
transfers_candidates : 38
```

---

**Bottom line:** the new bench is a strictly multimodal + leakage-controlled
cousin of AgentEHR. Manifest schema is a drop-in superset; DB schema is
drop-in for EHRXQA but incompatible for MedMod. The inference pipeline can
run EHRXQA-table patient-scope today with only a `--data_path` swap; image
tasks and MedMod outcome tasks require targeted, localized extensions.
