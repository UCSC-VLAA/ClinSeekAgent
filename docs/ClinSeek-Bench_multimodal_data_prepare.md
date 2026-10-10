# ClinSeek-Bench Multimodal Data Preparation

This document describes how to reconstruct the multimodal split of
ClinSeek-Bench from the released Hugging Face manifest and official source
datasets. The multimodal split contains 989 image-grounded examples:

- 497 EHRXQA-derived CXR question-answering rows.
- 492 MedMod-derived ICU/CXR prediction rows.

ClinSeek-Bench does not redistribute protected MIMIC-derived patient databases,
chest X-ray JPG files, or radiology report text. Users must download the
required source datasets under their own credentialed access and data-use
agreements, then run the reconstruction scripts locally.

The released Hugging Face dataset provides the multimodal manifest:

```text
ClinSeek-Bench/
└── ClinSeek-Bench_multimodal.jsonl
```

The reconstruction scripts are maintained in the GitHub codebase under
`ClinSeekAgent/scripts/data_build/`, matching the text benchmark preparation
workflow.

`ClinSeek-Bench_multimodal.jsonl` is the source of truth for released `qid`s,
questions, labels, task categories, patient identifiers, prediction times,
linked CXR study / image ids, image and report paths, and the trusted scope
metadata (`scope`, `modalities`, `study_ids`, `dicom_ids`, `snapshot_spec`,
`leakage_policy_version`). It does not store the rendered EHR context
(`input_text`) of the Curated Input setting, because that contains
MIMIC-derived table rows; Step 4 renders it locally from the rebuilt patient
databases through the strict temporal view.

---

## Required Source Data

Download the following resources directly from their official sources:

| Dataset | Official Source | Used For |
| --- | --- | --- |
| MIMIC-IV | https://physionet.org/content/mimiciv/ | EHR provenance and selected source table checks |
| MIMIC-CXR | https://physionet.org/content/mimic-cxr/ | CXR metadata and report text |
| MIMIC-CXR-JPG | https://physionet.org/content/mimic-cxr-jpg/ | Linked CXR JPG files |
| EHRXQA | https://physionet.org/content/ehrxqa/ | EHRXQA-derived source-aligned rows |
| MedMod repository | https://github.com/nyuad-cai/MedMod | MedMod task listfiles and extraction outputs |
| MIMIC-IV-Note | https://physionet.org/content/mimic-iv-note/ | Optional; kept as a path option for provenance |

MIMIC-CXR-JPG v2.0.0 or v2.1.0 local layouts are both acceptable if JPG files
can be resolved by subject, study, and DICOM path.

Clone the released benchmark manifest and the ClinSeekAgent codebase:

```bash
git clone https://huggingface.co/datasets/UCSC-VLAA/ClinSeek-Bench
git clone https://github.com/UCSC-VLAA/ClinSeekAgent.git
```

Then set local paths:

```bash
export CLINSEEK_BENCH=/path/to/ClinSeek-Bench
export CLINSEEK_MULTIMODAL_MANIFEST="$CLINSEEK_BENCH/ClinSeek-Bench_multimodal.jsonl"
export CLINSEEK_AGENT=/path/to/ClinSeekAgent
export BUILD_ROOT=/path/to/clinseek-mm-build

export EHRXQA_ROOT=/path/to/ehrxqa/1.0.0
export MIMIC_CXR_ROOT=/path/to/mimic-cxr/2.0.0
export MIMIC_CXR_JPG_ROOT=/path/to/mimic-cxr-jpg
export MIMICIV_ROOT=/path/to/mimiciv
export MIMIC_IV_NOTE_ROOT=/path/to/mimic-iv-note

export MEDMOD_REPO_ROOT=/path/to/MedMod
```

---

## Step 1: Build Source-Aligned Subsets

The first stage reconstructs audit-oriented source-aligned subsets. These
directories preserve upstream pairing metadata and task definitions so the
released rows can be traced back to EHRXQA and MedMod. They are not runtime
model inputs, because they may contain label-bearing metadata.

```bash
python "$CLINSEEK_AGENT/scripts/data_build/build_ehrxqa_release_original_subset.py" \
  --input "$CLINSEEK_MULTIMODAL_MANIFEST" \
  --output-root "$BUILD_ROOT/source/EHRXQA" \
  --ehrxqa-root "$EHRXQA_ROOT" \
  --cxr-root "$MIMIC_CXR_ROOT" \
  --cxr-jpg-root "$MIMIC_CXR_JPG_ROOT" \
  --mimiciv-root "$MIMICIV_ROOT" \
  --mimic-iv-note-root "$MIMIC_IV_NOTE_ROOT" \
  --overwrite

python "$CLINSEEK_AGENT/scripts/data_build/build_medmod_release_original_subset.py" \
  --input "$CLINSEEK_MULTIMODAL_MANIFEST" \
  --output-root "$BUILD_ROOT/source/MedMod" \
  --medmod-repo-root "$MEDMOD_REPO_ROOT" \
  --cxr-jpg-root "$MIMIC_CXR_JPG_ROOT" \
  --cxr-meta-root "$MIMIC_CXR_ROOT" \
  --mimiciv-root "$MIMICIV_ROOT" \
  --overwrite
```

The MedMod script intentionally rebuilds only the MedMod rows present in
`ClinSeek-Bench_multimodal.jsonl`; it does not rebuild the full MedMod
benchmark.

---

## Step 2: Convert To ClinSeek-MM-Bench Runtime Format

The second stage creates local patient SQLite databases, copies linked CXR
assets, and renders model-ready `input_text` from the rebuilt local databases.

```bash
python "$CLINSEEK_AGENT/scripts/data_build/build_ehrxqa_clinseek_mm_subset.py" \
  --original-root "$BUILD_ROOT/source/EHRXQA" \
  --output-root "$BUILD_ROOT/runtime/EHRXQA" \
  --overwrite

python "$CLINSEEK_AGENT/scripts/data_build/build_medmod_clinseek_mm_subset.py" \
  --original-root "$BUILD_ROOT/source/MedMod" \
  --output-root "$BUILD_ROOT/runtime/MedMod" \
  --overwrite
```

---

## Step 3: Combine The Final Multimodal Package

```bash
python "$CLINSEEK_AGENT/scripts/data_build/combine_clinseek_mm_bench.py" \
  --reference-input "$CLINSEEK_MULTIMODAL_MANIFEST" \
  --ehrxqa-root "$BUILD_ROOT/runtime/EHRXQA" \
  --medmod-root "$BUILD_ROOT/runtime/MedMod" \
  --output-root "$BUILD_ROOT/final/ClinSeek-MM-Bench" \
  --overwrite
```

Expected final layout:

```text
$BUILD_ROOT/final/ClinSeek-MM-Bench/
├── inputs/
│   └── mm_bench.jsonl
└── data/
    └── mm_bench/
        ├── ehrxqa/
        │   ├── database/
        │   ├── mimic-cxr/2.0.0/files/
        │   └── table_description/
        └── medmod/
            ├── database/
            ├── mimic-cxr/2.0.0/files/
            └── table_description/
```

---

## Validation

Validate the source-only Hugging Face manifest without requiring protected
assets:

```bash
python "$CLINSEEK_AGENT/scripts/data_build/validate_multimodal_release.py" \
  --bench-root "$CLINSEEK_BENCH" \
  --input "$CLINSEEK_MULTIMODAL_MANIFEST" \
  --manifest-only
```

Validate the locally rebuilt runtime package:

```bash
python "$CLINSEEK_AGENT/scripts/data_build/validate_multimodal_release.py" \
  --bench-root "$BUILD_ROOT/final/ClinSeek-MM-Bench"
```

Expected counts:

- 989 total multimodal rows.
- 497 EHRXQA-derived rows.
- 492 MedMod-derived rows.
- Per task: `ehrxqa_image` 497, `medmod_decompensation` 125,
  `medmod_in_hospital_mortality` 125, `medmod_phenotyping` 120,
  `medmod_radiology` 122.
- 989, not 997: 5 `medmod_phenotyping` and 3 `medmod_radiology` rows carry an
  empty `ground_truth` (no phenotype group, no radiographic finding) and cannot
  be scored, so they are excluded. Every remaining row's linked CXR resolves on
  disk; none are dropped for missing assets.
- 165 EHRXQA patient databases.
- 395 MedMod patient databases.
- 350 unique EHRXQA linked CXR JPG paths.
- 477 unique MedMod linked CXR JPG paths.
- 356 unique EHRXQA linked CXR report paths.
- 0 missing DB, image, or report references after local reconstruction.

---

## Runtime Notes

The released `image_paths` and `report_paths` keep an upstream package prefix,
such as `EHRXQAOriginalLinked_v1/...` or `MedModOriginalLinked_v1/...`. The
runtime loader strips the first path segment and resolves the remaining path
under the corresponding rebuilt bench root:

```text
$BUILD_ROOT/final/ClinSeek-MM-Bench/data/mm_bench/ehrxqa/
$BUILD_ROOT/final/ClinSeek-MM-Bench/data/mm_bench/medmod/
```

Use the final `ClinSeek-MM-Bench` directory as the multimodal benchmark root
for evaluation. Do not use the source-aligned `linked_manifests/` directories
as model inputs. For the agentic runner pass both roots,
`BENCH_ROOT=$BUILD_ROOT/final/ClinSeek-MM-Bench/data/mm_bench/ehrxqa:$BUILD_ROOT/final/ClinSeek-MM-Bench/data/mm_bench/medmod`,
and serve each with its own EHR MCP server (see
[`multimodal_benchmark.md`](multimodal_benchmark.md)).

---

## Step 4: Render The Strict Curated-Input Manifest

The agentic runner uses the released manifest as is. The Curated Input
setting additionally needs `input_text`: the row's `question` followed by an
`<ehr_context>` block that lists, for every table, the number of rows visible
before the prediction cutoff and the latest 80 of them. It is rendered through
the same strict runtime view the agent sees (`EHRManager.load_ehr_for_sample`
with the row's cutoff, linked studies, scope, task, and modalities; policy
`multimodal-strict-temporal-scope-v1`):

```bash
MM=$BUILD_ROOT/final/ClinSeek-MM-Bench/data/mm_bench
python "$CLINSEEK_AGENT/scripts/data_build/materialize_multimodal_strict_manifest.py" \
  --manifest "$CLINSEEK_MULTIMODAL_MANIFEST" \
  --ehrxqa-root "$MM/ehrxqa" --medmod-root "$MM/medmod" \
  --output "$BUILD_ROOT/final/ClinSeek-Bench_multimodal_strict.jsonl"
```

Validate the runtime contract on every row (no post-cutoff values, only linked
studies in `tb_cxr`, no longitudinal EHR for image-only tasks, no MedMod outcome
columns):

```bash
python "$CLINSEEK_AGENT/scripts/data_build/validate_multimodal_strict_runtime.py" \
  --manifest "$CLINSEEK_MULTIMODAL_MANIFEST" \
  --ehrxqa-root "$MM/ehrxqa" --medmod-root "$MM/medmod" \
  --output "$BUILD_ROOT/final/mm_strict_runtime_gate.json"
```

`scripts/data_build/audit_multimodal_db_temporal.py` additionally audits the
physical databases against each row's cutoff. The strict manifest is the input
of the Curated Input corpus builder (`scripts/clean_data/`), see
[`ClinSeek-Bench_text_curated_input_evaluation.md`](ClinSeek-Bench_text_curated_input_evaluation.md#multimodal).

## Task Labels And Categories

The paper reports ClinSeek-Bench-MM by six categories; the manifest's
`category` field carries them:

| `task` | rows | category |
| --- | ---: | --- |
| `ehrxqa_image` | 497 | CXR finding presence (215), CXR finding enumeration (60), CXR change comparison (222) |
| `medmod_radiology` | 122 | CXR finding enumeration |
| `medmod_decompensation` | 125 | Clinical deterioration |
| `medmod_in_hospital_mortality` | 125 | Mortality |
| `medmod_phenotyping` | 120 | Phenotype |
