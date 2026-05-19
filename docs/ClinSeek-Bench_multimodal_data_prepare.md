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

The released Hugging Face dataset provides only the multimodal manifest:

```text
ClinSeek-Bench/
└── inputs/
    └── mm_bench.jsonl
```

The reconstruction scripts live in the GitHub codebase:

```text
ClinSeekAgent/
└── scripts/
    └── data_build/
        ├── build_ehrxqa_release_original_subset.py
        ├── build_ehrxqa_clinseek_mm_subset.py
        ├── build_medmod_release_original_subset.py
        ├── build_medmod_clinseek_mm_subset.py
        ├── combine_clinseek_mm_bench.py
        └── validate_multimodal_release.py
```

`inputs/mm_bench.jsonl` is the source of truth for released `qid`s, questions,
labels, patient identifiers, linked CXR image paths, and report paths. It does
not store pre-rendered `input_text`, because that field contains MIMIC-derived
EHR table rows. The reconstruction scripts render `input_text` locally from the
rebuilt patient databases.

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
  --input "$CLINSEEK_BENCH/inputs/mm_bench.jsonl" \
  --output-root "$BUILD_ROOT/source/EHRXQA" \
  --ehrxqa-root "$EHRXQA_ROOT" \
  --cxr-root "$MIMIC_CXR_ROOT" \
  --cxr-jpg-root "$MIMIC_CXR_JPG_ROOT" \
  --mimiciv-root "$MIMICIV_ROOT" \
  --mimic-iv-note-root "$MIMIC_IV_NOTE_ROOT" \
  --overwrite

python "$CLINSEEK_AGENT/scripts/data_build/build_medmod_release_original_subset.py" \
  --input "$CLINSEEK_BENCH/inputs/mm_bench.jsonl" \
  --output-root "$BUILD_ROOT/source/MedMod" \
  --medmod-repo-root "$MEDMOD_REPO_ROOT" \
  --cxr-jpg-root "$MIMIC_CXR_JPG_ROOT" \
  --cxr-meta-root "$MIMIC_CXR_ROOT" \
  --mimiciv-root "$MIMICIV_ROOT" \
  --overwrite
```

The MedMod script intentionally rebuilds only the MedMod rows present in
`inputs/mm_bench.jsonl`; it does not rebuild the full MedMod benchmark.

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
  --reference-input "$CLINSEEK_BENCH/inputs/mm_bench.jsonl" \
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
as model inputs.
