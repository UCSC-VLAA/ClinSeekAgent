# ClinSeek-Bench's Text-Based Benchmark Data Preparation

This document describes how to prepare the patient-level EHR assets required by
ClinSeek-Bench-Text. The benchmark is derived from EHR-Bench (EHR-R1): 40
questions from each of its 45 subtasks, 1,800 in total, grouped into 13 task
categories, and reformulated for ClinSeekAgent's Automated Evidence-Seeking
setting, where the model retrieves evidence from the raw patient record rather
than answering from pre-selected context.

The public release does not redistribute raw MIMIC files. Users must obtain
MIMIC-IV through PhysioNet under their own credentialed access. The released
ClinSeek-Bench text-based evaluation JSON already includes `subject_id`, so
patient databases can be generated directly from those IDs.

Set a data root before running the commands below:

```bash
export CLINSEEK_DATA_ROOT=./data/clinseek_bench
```

Expected final layout:

```text
$CLINSEEK_DATA_ROOT/
├── text/
│   └── ClinSeek-Bench_text.json
├── ehr_bench/
│   ├── database/
│   │   ├── candidate_table.db
│   │   ├── patient_10000108.db
│   │   └── ...
│   └── table_description/
└── raw/
    └── MIMIC-IV/
        └── mimic_iv/
            ├── hosp/
            ├── icu/
            ├── note/
            └── ed/
```

`ehr_bench/` is the EHR data root consumed by the MCP server for the
ClinSeek-Bench text-based benchmark.

---

## MIMIC-IV Raw Data Preparation

The patient database generation step needs three credentialed PhysioNet
resources. Download them directly from PhysioNet after completing the required
credentialing and data-use agreements:

| Dataset | Version | Official Source | Needed Directory |
| --- | --- | --- | --- |
| MIMIC-IV | 3.1 | https://physionet.org/content/mimiciv/3.1/ | `hosp/`, `icu/` |
| MIMIC-IV-Note | 2.2 | https://physionet.org/content/mimic-iv-note/2.2/ | `note/` |
| MIMIC-IV-ED | 2.2 | https://physionet.org/content/mimic-iv-ed/2.2/ | `ed/` |

Do not download MIMIC data from third-party mirrors for release use. Users must
obtain the files from PhysioNet under their own credentialed access.

### 1. Arrange The Local MIMIC-IV Tree

After downloading the three PhysioNet resources, copy or move the relevant
module directories into one local root:

```bash
mkdir -p "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv"

# Replace these with your actual PhysioNet download locations.
MIMIC_IV_SRC=./externals/mimiciv/3.1
MIMIC_NOTE_SRC=./externals/mimic-iv-note/2.2
MIMIC_ED_SRC=./externals/mimic-iv-ed/2.2

rsync -a "$MIMIC_IV_SRC/hosp" "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/"
rsync -a "$MIMIC_IV_SRC/icu" "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/"
rsync -a "$MIMIC_NOTE_SRC/note" "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/"
rsync -a "$MIMIC_ED_SRC/ed" "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/"
```

Final expected layout:

```text
$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/
├── hosp/
├── icu/
├── note/
└── ed/
```

### 2. Decompress Table Files To CSV

Prepare plain `.csv` files inside all four module directories before generating
patient databases. PhysioNet releases many MIMIC-IV tables as `.csv.gz`; keep
the original `.csv.gz` files if desired, but each compressed table should have a
same-directory `.csv` sibling:

```text
$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/
├── hosp/*.csv
├── icu/*.csv
├── note/*.csv
└── ed/*.csv
```

One safe way to decompress only missing `.csv` files is:

```bash
for d in hosp icu note ed; do
    for f in "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv/$d"/*.csv.gz; do
        [ -e "$f" ] || continue
        csv="${f%.gz}"
        [ -f "$csv" ] || gzip -dk "$f"
    done
done
```

`scripts/patient_event2db.py` still skips duplicate table loads when both
`table.csv` and `table.csv.gz` exist in the same directory, and it prefers
`table.csv`.

---

## Released Text-Based Benchmark Manifest

The released ClinSeek-Bench text-based benchmark file is a JSON list whose rows
already include the MIMIC-IV identifiers needed to build patient databases. It
contains 40 sampled examples from each of 45 EHR-Bench subtasks, resulting in
1,800 text-based evaluation examples.

Download `ClinSeek-Bench_text.json` from the ClinSeek-Bench Hugging Face
dataset:

- Dataset: https://huggingface.co/datasets/UCSC-VLAA/ClinSeek-Bench

Example path:

```text
$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json
```

Representative row schema:

```json
{
  "qid": "ehr_bench_risk_prediction_329",
  "subject_id": 11824833,
  "hadm_id": 24876618,
  "target_hadm_id": null,
  "target_stay_id": null,
  "prediction_time": "2183-08-13 01:15:18",
  "latest_event_time": "2183-08-13 01:15:17",
  "task": "ED_Critical_Outcomes",
  "task_type": "risk_prediction",
  "category": "Escalation of care",
  "instruction": "Please predict whether the patient will die during hospitalization or ...",
  "label": "yes",
  "output": "yes",
  "candidates": ["yes", "no"],
  "task_info": {"metric": "aucroc", "task_type": "risk_prediction", "...": "..."},
  "source_file": "ehr_bench_risk_prediction.jsonl",
  "source_idx": 329
}
```

Only `subject_id` is required for patient DB generation. The agentic runner
builds its prompt from `instruction`, `task`, `prediction_time`,
`subject_id`, `hadm_id`, and `candidates`, and binds `subject_id`,
`prediction_time`, `task`, `hadm_id`, `target_hadm_id`, and `target_stay_id`
into every EHR load (the model cannot change them). `label` is used for
scoring and `category` for per-category reporting.

`target_hadm_id` / `target_stay_id` name the encounter whose codes a
diagnosis-code task (`diagnoses_icd`, `diagnoses_ccs`, `diagnosis`,
`diagnosis_ccs`) asks for; the strict temporal policy hides that encounter's
codes regardless of the cutoff and refuses to load those rows without them.

### Upstream `input` / `question` (not in the public file)

The public file does not redistribute the upstream EHR-Bench serialization of
the curated record (`input`) or the source benchmark's single-turn prompt built
from it (`question`). Every row maps 1:1 to a row of
[BlueZeros/EHR-Bench](https://huggingface.co/datasets/BlueZeros/EHR-Bench)
through `source_file` + `source_idx`. Only the Curated Input corpus builder
(`scripts/clean_data/`) and `run_text_curated.py --data` need them; restore
both fields byte-for-byte with:

```bash
huggingface-cli download BlueZeros/EHR-Bench --repo-type dataset \
    --local-dir "$CLINSEEK_DATA_ROOT/raw/EHR-Bench"
python scripts/data_build/restore_ehr_bench_inputs.py \
    --manifest "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
    --ehr-bench-dir "$CLINSEEK_DATA_ROOT/raw/EHR-Bench" \
    --output "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text_with_inputs.json"
```

The script exits non-zero if an upstream row is missing or its label disagrees
with the manifest.

Optional sanity check:

```bash
python - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["CLINSEEK_DATA_ROOT"]) / "text/ClinSeek-Bench_text.json"
rows = json.load(path.open())
subject_ids = {row["subject_id"] for row in rows}
print(f"rows: {len(rows)}")
print(f"unique subject_id: {len(subject_ids)}")
print(f"tasks: {sorted({row.get('task') for row in rows})}")
PY
```

---

## Generate Patient SQLite Databases

Use `scripts/patient_event2db.py` to generate one SQLite database per
`subject_id` referenced by the released ClinSeek-Bench text-based benchmark
manifest. These databases are served by `src/run_mcp_server.py` and queried by
the agent through EHR MCP tools.

### 1. What The Script Does

`scripts/patient_event2db.py`:

1. Collects all `subject_id` values from `--data_file_path` or
   `--data_dir_path`
2. Skips `patient_<subject_id>.db` files that already exist in `--output_path`
3. Scans `hosp/`, `icu/`, `note/`, and `ed/` for `.csv` / `.csv.gz` files
4. Filters rows by target `subject_id`
5. Applies preprocessing:
   - Adds `charttime` to `diagnoses_icd` from `admissions.dischtime - 1min`
   - Adds `charttime` to ED `diagnosis` from `edstays.outtime - 1min`
   - Copies discharge-note text before `Physical Exam` into `admissions.text`
   - Leaves `admissions.text` empty if no matching discharge note exists
6. Writes each patient to `patient_<subject_id>.db`, with one SQLite table per
   source CSV table and all columns stored as `TEXT`

### 2. Inputs And Outputs

Inputs:

| Parameter | Example | Meaning |
| --- | --- | --- |
| `--root_path` | `$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv` | MIMIC-IV root containing `hosp/`, `icu/`, `note/`, `ed/` |
| `--data_file_path` | `$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json` | Released ClinSeek-Bench text-based JSON containing `subject_id` |
| `--data_dir_path` | optional directory | All `.json` files in the directory are read for `subject_id` |
| `--subject_id` | optional integer | Generate only one patient DB for debugging |
| `--data_dirs` | `ed hosp icu note` | MIMIC-IV subdirectories to scan |

Output:

```text
$CLINSEEK_DATA_ROOT/ehr_bench/database/
├── patient_10000108.db
├── patient_10025995.db
└── ...
```

Each DB table name matches the corresponding MIMIC-IV CSV filename, such as
`admissions`, `diagnoses_icd`, `labevents`, `transfers`, or `radiology`.

### 3. Run Database Generation

From the repository root:

```bash
mkdir -p "$CLINSEEK_DATA_ROOT/ehr_bench/database" logs

nohup python scripts/patient_event2db.py \
    --root_path "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv" \
    --output_path "$CLINSEEK_DATA_ROOT/ehr_bench/database" \
    --data_file_path "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
    > "logs/ehr_bench_db_gen_$(date -u +%Y%m%dT%H%M%SZ).log" 2>&1 &
```

Incremental reruns are safe: existing `patient_*.db` files are skipped.

Generate one patient for debugging:

```bash
python scripts/patient_event2db.py \
    --root_path "$CLINSEEK_DATA_ROOT/raw/MIMIC-IV/mimic_iv" \
    --output_path "$CLINSEEK_DATA_ROOT/ehr_bench/database" \
    --subject_id 10000108
```

### 4. Verify Coverage Against The Text-Based Benchmark File

After generation, verify that every `subject_id` in the ClinSeek-Bench
text-based benchmark file has a corresponding patient DB:

```bash
python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["CLINSEEK_DATA_ROOT"])
manifest = root / "text/ClinSeek-Bench_text.json"
db_dir = root / "ehr_bench/database"

rows = json.load(manifest.open())
subject_ids = {str(row["subject_id"]) for row in rows}
missing = sorted(
    sid for sid in subject_ids
    if not (db_dir / f"patient_{sid}.db").exists()
)

print(f"manifest rows: {len(rows)}")
print(f"unique subject_id: {len(subject_ids)}")
print(f"missing patient DBs: {len(missing)}")
if missing[:10]:
    print("examples:", ", ".join(missing[:10]))
PY
```

### 5. Candidate Table And Table Descriptions

The EHR MCP server also loads candidate and schema-description assets from the
same EHR data root:

```text
$CLINSEEK_DATA_ROOT/ehr_bench/
├── database/
│   ├── candidate_table.db
│   └── patient_*.db
└── table_description/
    ├── link_information.json
    └── shorten_description.json
```

If you are preparing a full release artifact, include `candidate_table.db` and
`table_description/` alongside the generated patient DBs. Without
`candidate_table.db`, candidate-search tools will be unavailable; without
`table_description/`, table-description prompts will be under-specified.

### 6. Resource And Runtime Notes

- Raw MIMIC-IV CSVs are large. `icu/chartevents.csv(.gz)` and
  `hosp/labevents.csv(.gz)` dominate runtime.
- The DB generator groups all target patients in memory before writing SQLite
  files. For thousands of patients, expect tens of GB of RAM.
- A full run can take several hours because it must scan large CSV files.
- Incremental runs still scan the CSV files; only the write phase becomes
  shorter.
- Redirected Python stdout is block-buffered by default. If logs appear empty
  for the first few minutes, use `PYTHONUNBUFFERED=1` or `python -u`.

### 7. Target Encounters And Runtime Validation

The public manifest already carries `target_hadm_id` / `target_stay_id`. To
re-derive them (for example for a different EHR-Bench sample), run the
materializer. It selects the admission (or ED stay) whose codes match the
label, verifying against the ICD / CCS reference tables
(`d_icd_diagnoses`, `d_ccs_diagnoses`; e.g. the `reference_table.db` shipped
with AgentEHR-Bench, see `scripts/download_agentehr_bench.sh`):

```bash
python scripts/data_build/materialize_ehr_bench_target_encounters.py \
    --manifest "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
    --data-root "$CLINSEEK_DATA_ROOT/ehr_bench" \
    --reference-db /path/to/reference_table.db \
    --output "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text_targets.json"
```

Then check the strict runtime view (`strict-ehr-runtime-v8`) on every row:
every row builds (diagnosis-code rows need their target encounter),
administrative code tables carry an availability time instead of a synthetic
`charttime`, and outcome-bearing or late-filled fields (`patients.dod`,
`admissions.text`, `cost`, mutable `poe_detail` / `emar_detail` / `pharmacy`
fields) are not exposed:

```bash
python scripts/data_build/validate_text_strict_runtime.py \
    --data-root "$CLINSEEK_DATA_ROOT/ehr_bench" \
    --manifest "$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
    --output outputs/text_strict_runtime_gate.json
```

Both run in the EHR MCP environment (they load patients through `EHRManager`).

---

## Serve The Prepared Text-Based Benchmark Data

Start the EHR MCP server against the prepared EHR data root for the
ClinSeek-Bench text-based benchmark:

```bash
EHR_DATA_PATH="$CLINSEEK_DATA_ROOT/ehr_bench" \
bash scripts/run_ehr_mcp.sh
```

Equivalent direct invocation:

```bash
CUDA_VISIBLE_DEVICES=0 python src/run_mcp_server.py \
    --mode http \
    --host 127.0.0.1 \
    --port 5003 \
    --data_path "$CLINSEEK_DATA_ROOT/ehr_bench"
```

Then run the ClinSeek-Bench text-based evaluation with the released manifest:

```bash
DATA_PATH="$CLINSEEK_DATA_ROOT/text/ClinSeek-Bench_text.json" \
OUTPUT_DIR=outputs/text_eval \
bash scripts/run_text_eval.sh
```

---

## Common Issues

1. `Directory not found: .../ed`: The DB generator can run without `ed/`, but
   ED-derived tables and ED diagnosis `charttime` enrichment will be missing.
   For release parity, include MIMIC-IV-ED v2.2.
2. Empty `admissions.text`: If a hospital admission has no matching discharge
   note in MIMIC-IV-Note, the script keeps the patient and stores an empty
   string for `admissions.text`.
3. SQLite columns are all `TEXT`: Cast numeric and timestamp fields explicitly
   in SQL queries when needed.
4. Missing `patient_<subject_id>.db`: confirm the `subject_id` appears in the
   released ClinSeek-Bench text-based JSON and rerun
   `scripts/patient_event2db.py` with the same `--data_file_path`.
