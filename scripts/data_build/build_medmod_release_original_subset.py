#!/usr/bin/env python3
"""Build the source-aligned MedMod subset used by ClinSeek MM-Bench.

This script intentionally does not rebuild the full MedMod benchmark.  It reads
the released ClinSeek multimodal input file, keeps only the MedMod rows that are
actually evaluated, verifies them against the official MedMod listfiles and
MIMIC-CXR labels/metadata, and packages the required source-format EHR folders
and CXR files with relative paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_INPUT = Path(
    os.environ.get(
        "CLINSEEK_MM_BENCH_JSONL",
        "data/ClinSeek-Bench/inputs/mm_bench.jsonl",
    )
)
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "MEDMOD_ORIGINAL_SUBSET_ROOT",
        "data/build/ClinSeek-MM-Bench-MedMod-source",
    )
)
DEFAULT_MEDMOD_REPO = Path(os.environ.get("MEDMOD_REPO_ROOT", "external/MedMod"))
DEFAULT_CXR_JPG_ROOT = Path(
    os.environ.get("MIMIC_CXR_JPG_ROOT", "external/mimic-cxr-jpg")
)
DEFAULT_CXR_META_ROOT = Path(
    os.environ.get("MIMIC_CXR_META_ROOT", "external/mimic-cxr/2.0.0")
)
DEFAULT_MIMICIV_ROOT = Path(os.environ.get("MIMICIV_ROOT", "external/mimiciv/3.1"))

IMAGE_RE = re.compile(
    r"(?:^|/)files/p\d+/p(?P<subject_id>\d+)/s(?P<study_id>\d+)/(?P<dicom_id>[^/]+)\.jpg$"
)

TASK_MAP = {
    "medmod_decompensation": "decompensation",
    "medmod_in_hospital_mortality": "in-hospital-mortality",
    "medmod_length_of_stay": "length-of-stay",
    "medmod_phenotyping": "phenotyping",
    "medmod_radiology": "radiology",
    "decompensation": "decompensation",
    "in-hospital-mortality": "in-hospital-mortality",
    "length-of-stay": "length-of-stay",
    "phenotyping": "phenotyping",
    "radiology": "radiology",
}

PHENOTYPE_CLASSES = [
    "Acute and unspecified renal failure",
    "Acute cerebrovascular disease",
    "Acute myocardial infarction",
    "Cardiac dysrhythmias",
    "Chronic kidney disease",
    "Chronic obstructive pulmonary disease and bronchiectasis",
    "Complications of surgical procedures or medical care",
    "Conduction disorders",
    "Congestive heart failure; nonhypertensive",
    "Coronary atherosclerosis and other heart disease",
    "Diabetes mellitus with complications",
    "Diabetes mellitus without complication",
    "Disorders of lipid metabolism",
    "Essential hypertension",
    "Fluid and electrolyte disorders",
    "Gastrointestinal hemorrhage",
    "Hypertension with complications and secondary hypertension",
    "Other liver diseases",
    "Other lower respiratory disease",
    "Other upper respiratory disease",
    "Pleurisy; pneumothorax; pulmonary collapse",
    "Pneumonia (except that caused by tuberculosis or sexually transmitted disease)",
    "Respiratory failure; insufficiency; arrest (adult)",
    "Septicemia (except in labor)",
    "Shock",
]

RADIOLOGY_CLASSES = [
    "Atelectasis",
    "Cardiomegaly",
    "Consolidation",
    "Edema",
    "Enlarged Cardiomediastinum",
    "Fracture",
    "Lung Lesion",
    "Lung Opacity",
    "No Finding",
    "Pleural Effusion",
    "Pleural Other",
    "Pneumonia",
    "Pneumothorax",
    "Support Devices",
]

LEAKY_STAY_COLUMNS = {
    "outtime",
    "los",
    "dischtime",
    "deathtime",
    "dod",
    "mortality_inunit",
    "mortality",
    "mortality_inhospital",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--medmod-repo-root", type=Path, default=DEFAULT_MEDMOD_REPO)
    parser.add_argument("--cxr-jpg-root", type=Path, default=DEFAULT_CXR_JPG_ROOT)
    parser.add_argument("--cxr-meta-root", type=Path, default=DEFAULT_CXR_META_ROOT)
    parser.add_argument("--mimiciv-root", type=Path, default=DEFAULT_MIMICIV_ROOT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--include-reports",
        action="store_true",
        help="Also package CXR report TXT files. Keep disabled for the ClinSeek MedMod release.",
    )
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="Deprecated compatibility flag; warnings are non-fatal unless --strict-official-match is set.",
    )
    parser.add_argument(
        "--strict-official-match",
        action="store_true",
        help="Exit non-zero if official pairing/label validation warnings are found.",
    )
    return parser.parse_args()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def reset_dir(path: Path, overwrite: bool) -> None:
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"Output root already exists: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def link_or_copy(src: Path, dst: Path) -> None:
    src = src.resolve()
    ensure_dir(dst.parent)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def copytree_links(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    shutil.copytree(src, dst, copy_function=lambda s, d: (link_or_copy(Path(s), Path(d)) or str(d)))


def relpath_or_name(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def normalize_split(split: str) -> str:
    return "valid" if split in {"val", "validate", "validation"} else split


def medmod_listfile_name(split: str, *, mml_ssl: bool) -> str:
    split = normalize_split(split)
    if split == "valid":
        return "validate_listfile.csv" if mml_ssl else "val_listfile.csv"
    return f"{split}_listfile.csv"


def canonical_task(task: str) -> str:
    if task not in TASK_MAP:
        raise ValueError(f"Unsupported MedMod task: {task}")
    return TASK_MAP[task]


def ground_truth_names(row: dict[str, Any]) -> list[str]:
    values = row.get("ground_truth")
    if values is None:
        values = row.get("label")
    if isinstance(values, list):
        names = []
        for item in values:
            if isinstance(item, dict) and item.get("name") is not None:
                names.append(str(item["name"]))
            elif item is not None:
                names.append(str(item))
        return names
    if values is None:
        return []
    return [str(values)]


def parse_image_ref(path: str) -> dict[str, Any]:
    stripped = path
    if "/" in stripped and stripped.split("/", 1)[0].endswith("OriginalLinked_v1"):
        stripped = stripped.split("/", 1)[1]
    match = IMAGE_RE.search(stripped)
    if not match:
        raise ValueError(f"Cannot parse CXR image path: {path}")
    subject_id = int(match.group("subject_id"))
    study_id = int(match.group("study_id"))
    dicom_id = match.group("dicom_id")
    nested_relpath = (
        f"mimic-cxr/2.0.0/files/p{str(subject_id)[:2]}/p{subject_id}/"
        f"s{study_id}/{dicom_id}.jpg"
    )
    return {
        "subject_id": subject_id,
        "study_id": study_id,
        "dicom_id": dicom_id,
        "nested_relpath": nested_relpath,
    }


def parse_stay_period_from_qid(qid: str, task: str) -> tuple[int | None, float | None]:
    if task == "radiology":
        return None, None
    try:
        _, stay_text, period_text = qid.rsplit("_", 2)
    except ValueError:
        return None, None
    return safe_int(stay_text), safe_float(period_text)


def load_medmod_rows(input_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with input_path.open("r", encoding="utf-8") as handle:
        for line_index, line in enumerate(handle):
            row = json.loads(line)
            if row.get("source_benchmark") != "medmod":
                continue
            task = canonical_task(str(row.get("task")))
            stay_from_qid, period_from_qid = parse_stay_period_from_qid(str(row.get("qid")), task)
            image_refs = [parse_image_ref(path) for path in row.get("image_paths") or []]
            row["_source_line_index"] = line_index
            row["_canonical_task"] = task
            row["_stay_id_from_qid"] = stay_from_qid
            row["_period_length_from_qid"] = period_from_qid
            row["_image_refs"] = image_refs
            rows.append(row)
    return rows


def row_key(row: dict[str, Any], task: str) -> tuple[int, float | None]:
    stay_id = safe_int(row.get("stay_id"))
    if stay_id is None:
        raise ValueError(f"Listfile row missing stay_id: {row}")
    if task in {"decompensation", "length-of-stay", "phenotyping"}:
        period = safe_float(row.get("period_length"))
        return stay_id, period
    return stay_id, None


def sample_key(sample: dict[str, Any]) -> tuple[int, float | None]:
    task = sample["_canonical_task"]
    stay_id = safe_int(sample.get("stay_id")) or sample.get("_stay_id_from_qid")
    if stay_id is None:
        raise ValueError(f"Sample missing stay_id: {sample.get('qid')}")
    if task in {"decompensation", "length-of-stay", "phenotyping"}:
        return int(stay_id), safe_float(sample.get("_period_length_from_qid"))
    return int(stay_id), None


def load_filtered_csv_index(
    path: Path,
    *,
    task: str,
    needed_keys: set[tuple[int, float | None]],
) -> tuple[dict[tuple[int, float | None], dict[str, Any]], list[str]]:
    if not path.exists():
        return {}, []
    rows: dict[tuple[int, float | None], dict[str, Any]] = {}
    columns: list[str] = []
    for chunk in pd.read_csv(path, chunksize=200_000):
        if not columns:
            columns = list(chunk.columns)
        for record in chunk.to_dict(orient="records"):
            key = row_key(record, task)
            if key in needed_keys and key not in rows:
                rows[key] = record
        if len(rows) == len(needed_keys):
            break
    return rows, columns


def load_listfile_index(
    repo_root: Path,
    task: str,
    split: str,
    needed_keys: set[tuple[int, float | None]],
) -> dict[str, Any]:
    split_name = normalize_split(split)
    mml_path = repo_root / "mml-ssl-full" / task / medmod_listfile_name(split_name, mml_ssl=True)
    data_path = repo_root / "data_full" / task / medmod_listfile_name(split_name, mml_ssl=False)
    if not mml_path.exists():
        raise FileNotFoundError(f"Missing official MedMod mml-ssl listfile: {mml_path}")
    mml_by_key, mml_columns = load_filtered_csv_index(
        mml_path,
        task=task,
        needed_keys=needed_keys,
    )
    data_by_key, data_columns = load_filtered_csv_index(
        data_path,
        task=task,
        needed_keys=needed_keys,
    )

    return {
        "mml_path": mml_path,
        "data_path": data_path if data_path.exists() else None,
        "mml_by_key": mml_by_key,
        "data_by_key": data_by_key,
        "mml_columns": mml_columns,
        "data_columns": data_columns,
    }


def expected_label_from_listfile(task: str, record: dict[str, Any]) -> list[str]:
    if task in {"decompensation", "in-hospital-mortality"}:
        return ["yes" if safe_int(record.get("y_true")) == 1 else "no"]
    if task == "phenotyping":
        return [label for label in PHENOTYPE_CLASSES if safe_int(record.get(label)) == 1]
    if task == "length-of-stay":
        value = safe_float(record.get("y_true"))
        return [] if value is None else [str(value)]
    raise ValueError(f"No listfile label parser for task: {task}")


def find_subject_dir(repo_root: Path, subject_id: int) -> tuple[Path, str]:
    for split in ("test", "train"):
        path = repo_root / "data_full" / "root" / split / str(subject_id)
        if path.is_dir():
            return path, split
    raise FileNotFoundError(f"Missing MedMod subject directory for subject_id={subject_id}")


def local_episode_filename(subject_id: int, official_stay: str | None) -> str | None:
    if not official_stay:
        return None
    prefix = f"{subject_id}_"
    if official_stay.startswith(prefix):
        return official_stay[len(prefix) :]
    return official_stay


def task_data_split_dir(split: str) -> str:
    split = normalize_split(split)
    if split == "valid":
        return "train"
    return split


def find_cxr_file(cxr_jpg_root: Path, image_ref: dict[str, Any], suffix: str) -> Path | None:
    subject_id = image_ref["subject_id"]
    study_id = image_ref["study_id"]
    dicom_id = image_ref["dicom_id"]
    flat_name = f"p{str(subject_id)[:2]}_p{subject_id}_s{study_id}_{dicom_id}.{suffix}"
    nested = Path(image_ref["nested_relpath"]).with_suffix(f".{suffix}")
    candidates = [
        cxr_jpg_root / "mimic-cxr2" / flat_name,
        cxr_jpg_root / nested.relative_to("mimic-cxr/2.0.0"),
        cxr_jpg_root / "2.1.0-lite" / nested.relative_to("mimic-cxr/2.0.0"),
        cxr_jpg_root / "2.1.0-working-subset" / nested.relative_to("mimic-cxr/2.0.0"),
        cxr_jpg_root / nested,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def read_csv_subset(path: Path, column: str, values: set[Any]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if column not in df.columns:
        return df.iloc[0:0].copy()
    return df[df[column].isin(values)].copy()


def build_metadata_indexes(cxr_meta_root: Path) -> dict[str, Any]:
    metadata_path = cxr_meta_root / "mimic-cxr-2.0.0-metadata.csv"
    chexpert_path = cxr_meta_root / "mimic-cxr-2.0.0-chexpert.csv"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing MIMIC-CXR metadata: {metadata_path}")
    if not chexpert_path.exists():
        raise FileNotFoundError(f"Missing MIMIC-CXR CheXpert labels: {chexpert_path}")

    metadata_df = pd.read_csv(metadata_path)
    chexpert_df = pd.read_csv(chexpert_path)
    chexpert_df[RADIOLOGY_CLASSES] = chexpert_df[RADIOLOGY_CLASSES].fillna(0)
    chexpert_df = chexpert_df.replace(-1.0, 0.0)
    metadata_by_dicom = {
        str(record["dicom_id"]): record for record in metadata_df.to_dict(orient="records")
    }
    ap_rows_by_subject: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in metadata_df.to_dict(orient="records"):
        if str(record.get("ViewPosition") or "").upper() != "AP":
            continue
        subject_id = safe_int(record.get("subject_id"))
        study_id = safe_int(record.get("study_id"))
        if subject_id is None or study_id is None:
            continue
        study_datetime = parse_cxr_datetime(record)
        if study_datetime is None:
            continue
        ap_rows_by_subject[subject_id].append(
            {
                "subject_id": subject_id,
                "study_id": study_id,
                "dicom_id": str(record.get("dicom_id") or ""),
                "study_datetime": study_datetime,
                "view_position": "AP",
            }
        )
    for rows in ap_rows_by_subject.values():
        rows.sort(key=lambda item: item["study_datetime"])
    chexpert_by_study: dict[int, list[str]] = {}
    for record in chexpert_df.to_dict(orient="records"):
        study_id = safe_int(record.get("study_id"))
        if study_id is None:
            continue
        chexpert_by_study[study_id] = [
            label for label in RADIOLOGY_CLASSES if safe_float(record.get(label)) == 1.0
        ]
    return {
        "metadata_path": metadata_path,
        "chexpert_path": chexpert_path,
        "metadata_df": metadata_df,
        "chexpert_df": chexpert_df,
        "metadata_by_dicom": metadata_by_dicom,
        "ap_rows_by_subject": ap_rows_by_subject,
        "chexpert_by_study": chexpert_by_study,
    }


def parse_cxr_datetime(record: dict[str, Any]) -> pd.Timestamp | None:
    date = record.get("StudyDate")
    time = record.get("StudyTime")
    if date in (None, "") or time in (None, ""):
        return None
    try:
        time_text = f"{int(float(time)):06d}"
        return pd.Timestamp(datetime.strptime(f"{int(float(date))} {time_text}", "%Y%m%d %H%M%S"))
    except (TypeError, ValueError):
        return None


def parse_timestamp(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed


def validate_latest_ap_pairing(
    sample: dict[str, Any],
    official_row: dict[str, Any] | None,
    cxr_indexes: dict[str, Any],
) -> list[str]:
    if official_row is None:
        return []
    task = sample["_canonical_task"]
    if task == "radiology":
        return []

    subject_id = safe_int(sample.get("subject_id"))
    selected_study_ids = {ref["study_id"] for ref in sample["_image_refs"]}
    intime = parse_timestamp(official_row.get("intime"))
    prediction_time = parse_timestamp(sample.get("prediction_time") or official_row.get("prediction_time"))
    if subject_id is None or intime is None or prediction_time is None or not selected_study_ids:
        return ["pairing_window_unverifiable"]

    in_window = [
        row
        for row in cxr_indexes["ap_rows_by_subject"].get(subject_id, [])
        if row["study_datetime"] >= intime and row["study_datetime"] <= prediction_time
    ]
    if not in_window:
        return ["official_pairing_no_ap_in_window"]
    latest_time = max(row["study_datetime"] for row in in_window)
    latest_studies = {row["study_id"] for row in in_window if row["study_datetime"] == latest_time}
    if selected_study_ids.isdisjoint(latest_studies):
        return [
            "official_pairing_latest_ap_mismatch "
            f"expected_study_ids={sorted(latest_studies)} got={sorted(selected_study_ids)}"
        ]
    return []


def same_label_set(left: list[str], right: list[str]) -> bool:
    return set(left) == set(right)


def validate_sample(
    sample: dict[str, Any],
    *,
    list_indexes: dict[tuple[str, str], dict[str, Any]],
    cxr_indexes: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any] | None, dict[str, Any] | None]:
    task = sample["_canonical_task"]
    split = normalize_split(str(sample.get("source_split") or "test"))
    gt = ground_truth_names(sample)
    warnings: list[str] = []
    official_row = None
    official_source_row = None

    if task == "radiology":
        study_ids = [ref["study_id"] for ref in sample["_image_refs"]]
        expected = sorted({label for study_id in study_ids for label in cxr_indexes["chexpert_by_study"].get(study_id, [])})
        if not same_label_set(gt, expected):
            warnings.append(f"radiology_label_mismatch expected={expected} got={gt}")
    else:
        index = list_indexes[(task, split)]
        key = sample_key(sample)
        official_row = index["mml_by_key"].get(key)
        official_source_row = index["data_by_key"].get(key)
        if official_row is None:
            warnings.append(f"missing_official_listfile_row key={key}")
        else:
            expected = expected_label_from_listfile(task, official_row)
            if not same_label_set(gt, expected):
                warnings.append(f"listfile_label_mismatch expected={expected} got={gt}")
            for field in ("subject_id", "hadm_id", "prediction_time"):
                if field in official_row and sample.get(field) is not None:
                    if str(official_row.get(field)) != str(sample.get(field)):
                        warnings.append(
                            f"{field}_mismatch official={official_row.get(field)} got={sample.get(field)}"
                        )
            warnings.extend(validate_latest_ap_pairing(sample, official_row, cxr_indexes))

    for image_ref in sample["_image_refs"]:
        metadata = cxr_indexes["metadata_by_dicom"].get(image_ref["dicom_id"])
        if metadata is None:
            warnings.append(f"missing_cxr_metadata dicom_id={image_ref['dicom_id']}")
            continue
        if safe_int(metadata.get("study_id")) != image_ref["study_id"]:
            warnings.append(f"cxr_metadata_study_mismatch dicom_id={image_ref['dicom_id']}")
        if str(metadata.get("ViewPosition") or "").upper() != "AP":
            warnings.append(f"cxr_not_ap dicom_id={image_ref['dicom_id']}")

    validation = {
        "official_match_ok": not warnings,
        "warnings": warnings,
    }
    return validation, warnings, official_row, official_source_row


def write_subset_csvs(
    *,
    output_root: Path,
    medmod_repo_root: Path,
    rows_by_task_split: dict[tuple[str, str], list[dict[str, Any]]],
    list_indexes: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for (task, split), rows in sorted(rows_by_task_split.items()):
        if task == "radiology":
            continue
        index = list_indexes[(task, split)]
        mml_rows = [row["official_listfile_row"] for row in rows if row.get("official_listfile_row")]
        data_rows = [row["official_data_full_row"] for row in rows if row.get("official_data_full_row")]
        mml_out = (
            output_root
            / "source_release"
            / "mml-ssl-full"
            / task
            / medmod_listfile_name(split, mml_ssl=True)
        )
        ensure_dir(mml_out.parent)
        pd.DataFrame(mml_rows, columns=index["mml_columns"]).to_csv(mml_out, index=False)
        if data_rows:
            data_out = (
                output_root
                / "source_release"
                / "data_full"
                / task
                / medmod_listfile_name(split, mml_ssl=False)
            )
            ensure_dir(data_out.parent)
            pd.DataFrame(data_rows, columns=index["data_columns"]).to_csv(data_out, index=False)

    for name in ("README.md", "LICENSE"):
        src = medmod_repo_root / name
        if src.exists():
            link_or_copy(src, output_root / "source_release" / "repo_docs" / name)


def write_root_table_subsets(output_root: Path, repo_root: Path, subject_ids: set[int], stay_ids: set[int]) -> None:
    root_out = output_root / "source_release" / "root_tables"
    ensure_dir(root_out)
    for table_name in ("all_stays.csv", "all_diagnoses.csv", "diagnosis_counts.csv", "phenotype_labels.csv"):
        src = repo_root / "data_full" / "root" / table_name
        if not src.exists():
            continue
        df = pd.read_csv(src)
        if "subject_id" in df.columns:
            df = df[df["subject_id"].isin(subject_ids)].copy()
        if "stay_id" in df.columns and stay_ids:
            stay_filtered = df[df["stay_id"].isin(stay_ids)].copy()
            if not stay_filtered.empty:
                df = stay_filtered
        df.to_csv(root_out / table_name, index=False)


def write_cxr_metadata_subsets(output_root: Path, cxr_meta_root: Path, cxr_indexes: dict[str, Any], image_refs: list[dict[str, Any]]) -> None:
    meta_out = output_root / "source_release" / "cxr_metadata"
    ensure_dir(meta_out)
    dicom_ids = {ref["dicom_id"] for ref in image_refs}
    study_ids = {ref["study_id"] for ref in image_refs}

    cxr_indexes["metadata_df"][cxr_indexes["metadata_df"]["dicom_id"].isin(dicom_ids)].to_csv(
        meta_out / "mimic-cxr-2.0.0-metadata.csv", index=False
    )
    cxr_indexes["chexpert_df"][cxr_indexes["chexpert_df"]["study_id"].isin(study_ids)].to_csv(
        meta_out / "mimic-cxr-2.0.0-chexpert.csv", index=False
    )

    optional_specs = [
        ("mimic-cxr-2.0.0-split.csv", "dicom_id", dicom_ids),
        ("mimic-cxr-ehr-split.csv", "dicom_id", dicom_ids),
    ]
    for filename, column, values in optional_specs:
        subset = read_csv_subset(cxr_meta_root / filename, column, values)
        if not subset.empty:
            subset.to_csv(meta_out / filename, index=False)


def main() -> None:
    args = parse_args()
    reset_dir(args.output_root, args.overwrite)

    rows = load_medmod_rows(args.input)
    if not rows:
        raise ValueError(f"No MedMod rows found in {args.input}")

    needed_keys_by_index: dict[tuple[str, str], set[tuple[int, float | None]]] = defaultdict(set)
    for row in rows:
        task = row["_canonical_task"]
        if task == "radiology":
            continue
        split = normalize_split(str(row.get("source_split") or "test"))
        needed_keys_by_index[(task, split)].add(sample_key(row))

    list_indexes = {
        key: load_listfile_index(
            args.medmod_repo_root,
            key[0],
            key[1],
            needed_keys=needed_keys,
        )
        for key, needed_keys in sorted(needed_keys_by_index.items())
    }
    cxr_indexes = build_metadata_indexes(args.cxr_meta_root)

    manifest_rows: list[dict[str, Any]] = []
    rows_by_task_split: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    subject_ids: set[int] = set()
    stay_ids: set[int] = set()
    all_image_refs: list[dict[str, Any]] = []
    stats = Counter()
    warning_rows: list[dict[str, Any]] = []

    for row in rows:
        task = row["_canonical_task"]
        split = normalize_split(str(row.get("source_split") or "test"))
        validation, warnings, official_row, official_source_row = validate_sample(
            row,
            list_indexes=list_indexes,
            cxr_indexes=cxr_indexes,
        )
        if warnings:
            stats["rows_with_warnings"] += 1
            warning_rows.append({"qid": row.get("qid"), "warnings": warnings})

        subject_id = safe_int(row.get("subject_id"))
        stay_id = safe_int(row.get("stay_id"))
        if subject_id is None:
            raise ValueError(f"Sample missing subject_id: {row.get('qid')}")
        subject_ids.add(subject_id)
        if stay_id is not None:
            stay_ids.add(stay_id)

        subject_dir, subject_partition = find_subject_dir(args.medmod_repo_root, subject_id)
        subject_rel = f"source_release/data_full/root/{subject_partition}/{subject_id}"
        copytree_links(subject_dir, args.output_root / subject_rel)

        official_stay = None
        if official_row is not None:
            official_stay = official_row.get("stay")
        if official_stay is None and official_source_row is not None:
            official_stay = official_source_row.get("stay")
        official_stay = str(official_stay) if official_stay not in (None, "") else None
        official_period = None
        if official_row is not None:
            official_period = official_row.get("period_length")
        if official_period is None and official_source_row is not None:
            official_period = official_source_row.get("period_length")
        local_stay = local_episode_filename(subject_id, official_stay)
        ehr_timeseries_relpath = f"{subject_rel}/{local_stay}" if local_stay else None
        ehr_episode_relpath = None
        if local_stay and local_stay.endswith("_timeseries.csv"):
            candidate_episode = f"{subject_rel}/{local_stay.replace('_timeseries.csv', '.csv')}"
            if (args.output_root / candidate_episode).exists():
                ehr_episode_relpath = candidate_episode
        ehr_task_timeseries_relpath = None
        if official_stay and task != "radiology":
            split_dir = task_data_split_dir(split)
            task_src = args.medmod_repo_root / "data_full" / task / split_dir / official_stay
            if task_src.exists():
                ehr_task_timeseries_relpath = (
                    f"source_release/data_full/{task}/{split_dir}/{official_stay}"
                )
                link_or_copy(task_src, args.output_root / ehr_task_timeseries_relpath)

        packaged_images: list[str] = []
        packaged_reports: list[str] = []
        raw_images: list[str] = []
        raw_reports: list[str] = []
        for image_ref in row["_image_refs"]:
            all_image_refs.append(image_ref)
            jpg_src = find_cxr_file(args.cxr_jpg_root, image_ref, "jpg")
            if jpg_src is None:
                raise FileNotFoundError(f"Missing CXR JPG for {row.get('qid')}: {image_ref}")
            jpg_rel = image_ref["nested_relpath"]
            link_or_copy(jpg_src, args.output_root / jpg_rel)
            packaged_images.append(jpg_rel)
            raw_images.append(relpath_or_name(jpg_src, args.cxr_jpg_root))

            txt_src = find_cxr_file(args.cxr_jpg_root, image_ref, "txt") if args.include_reports else None
            if txt_src is not None:
                txt_rel = str(Path(jpg_rel).with_suffix(".txt"))
                link_or_copy(txt_src, args.output_root / txt_rel)
                packaged_reports.append(txt_rel)
                raw_reports.append(relpath_or_name(txt_src, args.cxr_jpg_root))

        sidecar = {
            "qid": row.get("qid"),
            "source_line_index": row.get("_source_line_index"),
            "source_index": row.get("source_index"),
            "task": task,
            "source_task": row.get("task"),
            "source_split": split,
            "subject_id": row.get("subject_id"),
            "hadm_id": row.get("hadm_id"),
            "stay_id": row.get("stay_id"),
            "prediction_time": row.get("prediction_time"),
            "question": row.get("question"),
            "released_input_text": row.get("input_text"),
            "ground_truth": row.get("ground_truth"),
            "answer_type": row.get("answer_type"),
            "modalities": row.get("modalities") or ["ehr", "cxr"],
            "study_ids": [ref["study_id"] for ref in row["_image_refs"]],
            "dicom_ids": [ref["dicom_id"] for ref in row["_image_refs"]],
            "packaged_image_relpaths": packaged_images,
            "packaged_report_relpaths": packaged_reports,
            "raw_image_source_relpaths": raw_images,
            "raw_report_source_relpaths": raw_reports,
            "ehr_subject_relpaths": [subject_rel],
            "official_stay": official_stay,
            "official_period_length": official_period,
            "ehr_timeseries_relpath": ehr_timeseries_relpath,
            "ehr_episode_relpath": ehr_episode_relpath,
            "ehr_task_timeseries_relpath": ehr_task_timeseries_relpath,
            "official_listfile_row": official_row,
            "official_data_full_row": official_source_row,
            "official_validation": validation,
            "raw_join_key": {
                "stay_id": row.get("stay_id"),
                "period_length_hours": row.get("_period_length_from_qid"),
            },
        }
        manifest_rows.append(sidecar)
        rows_by_task_split[(task, split)].append(sidecar)
        stats[f"task:{task}"] += 1
        stats[f"split:{split}"] += 1

    write_subset_csvs(
        output_root=args.output_root,
        medmod_repo_root=args.medmod_repo_root,
        rows_by_task_split=rows_by_task_split,
        list_indexes=list_indexes,
    )
    write_root_table_subsets(args.output_root, args.medmod_repo_root, subject_ids, stay_ids)
    write_cxr_metadata_subsets(args.output_root, args.cxr_meta_root, cxr_indexes, all_image_refs)

    write_jsonl(args.output_root / "linked_manifests" / "all.jsonl", manifest_rows)
    for (task, split), task_rows in sorted(rows_by_task_split.items()):
        write_jsonl(args.output_root / "linked_manifests" / task / f"{split}.jsonl", task_rows)

    readme = f"""# ClinSeek-MM-Bench-MedMod-source

This package is the source-aligned MedMod subset used by ClinSeek MM-Bench.
It contains only the MedMod rows present in `$CLINSEEK_MM_BENCH_JSONL`.

The layout preserves the official MedMod task/listfile style where possible:

- `source_release/mml-ssl-full/<task>/*_listfile.csv`: subset of official MedMod rows.
- `source_release/data_full/<task>/*_listfile.csv`: subset of original task listfiles when available.
- `source_release/data_full/root/<split>/<subject_id>/`: official extracted MedMod EHR folders.
- `source_release/root_tables/`: filtered MedMod root tables kept only for provenance.
  Some files in this directory contain labels and must not be mounted as runtime
  EHR tables for agent inference.
- `source_release/cxr_metadata/`: subset MIMIC-CXR metadata and CheXpert labels.
- `mimic-cxr/2.0.0/files/...`: packaged JPG files. TXT reports are only included
  when `--include-reports` is explicitly set.
- `linked_manifests/`: row-level sidecar manifests with questions, gold labels, and relative paths.

Important: each manifest row records `official_stay`, `ehr_timeseries_relpath`,
and `ehr_episode_relpath` so downstream rendering can use the exact MedMod
episode from the official listfile instead of all episodes for the same subject.

Input root contract:

- ClinSeek multimodal input: `$CLINSEEK_MM_BENCH_JSONL`
- MedMod repository: `$MEDMOD_REPO_ROOT`
- Raw MIMIC-CXR JPG root: `$MIMIC_CXR_JPG_ROOT`
- Raw MIMIC-CXR metadata root: `$MIMIC_CXR_META_ROOT`
- Raw MIMIC-IV latest local release kept for provenance: `$MIMICIV_ROOT`
"""
    (args.output_root / "README.md").write_text(readme, encoding="utf-8")

    metadata = {
        "package_name": "ClinSeek-MM-Bench-MedMod-source",
        "input": "CLINSEEK_MM_BENCH_JSONL",
        "records": len(manifest_rows),
        "subjects": len(subject_ids),
        "unique_images": len({p for row in manifest_rows for p in row["packaged_image_relpaths"]}),
        "unique_reports": len({p for row in manifest_rows for p in row["packaged_report_relpaths"]}),
        "reports_included": bool(args.include_reports),
        "stats": dict(sorted(stats.items())),
        "warning_rows": warning_rows,
        "source_path_env_vars": {
            "medmod_repo_root": "MEDMOD_REPO_ROOT",
            "cxr_jpg_root": "MIMIC_CXR_JPG_ROOT",
            "cxr_meta_root": "MIMIC_CXR_META_ROOT",
            "mimiciv_root": "MIMICIV_ROOT",
        },
        "path_contract": {
            "manifest_paths": "relative_to_package_root",
            "packaged_image_relpaths": "relative_to_package_root",
            "packaged_report_relpaths": "relative_to_package_root",
            "ehr_subject_relpaths": "relative_to_package_root",
            "raw_image_source_relpaths": "relative_to_MIMIC_CXR_JPG_ROOT",
            "raw_report_source_relpaths": "relative_to_MIMIC_CXR_JPG_ROOT",
        },
    }
    write_json(args.output_root / "metadata.json", metadata)

    if warning_rows and args.strict_official_match and not args.allow_warnings:
        raise SystemExit(
            f"Built package but found {len(warning_rows)} validation warning rows. "
            "Inspect metadata.json or rerun without --strict-official-match."
        )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
