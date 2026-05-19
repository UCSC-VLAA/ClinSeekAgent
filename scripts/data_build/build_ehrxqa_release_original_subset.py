#!/usr/bin/env python3
"""Build the source-aligned EHRXQA subset used by ClinSeek MM-Bench.

This script rebuilds only the EHRXQA rows present in the released
inputs/mm_bench.jsonl.  It keeps the official EHRXQA schema, writes subset
CSV/SQLite tables, and packages the CXR files needed by the selected patients
using relative paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
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
        "EHRXQA_ORIGINAL_SUBSET_ROOT",
        "data/build/ClinSeek-MM-Bench-EHRXQA-source",
    )
)
DEFAULT_EHRXQA_ROOT = Path(
    os.environ.get("EHRXQA_ROOT", "external/ehrxqa/1.0.0")
)
DEFAULT_CXR_ROOT = Path(
    os.environ.get("MIMIC_CXR_ROOT", "external/mimic-cxr/2.0.0")
)
DEFAULT_CXR_JPG_ROOT = Path(
    os.environ.get("MIMIC_CXR_JPG_ROOT", "external/mimic-cxr-jpg")
)
DEFAULT_MIMICIV_ROOT = Path(os.environ.get("MIMICIV_ROOT", "external/mimiciv/3.1"))
DEFAULT_MIMIC_IV_NOTE_ROOT = Path(
    os.environ.get("MIMIC_IV_NOTE_ROOT", "external/mimic-iv-note/2.2")
)

QID_RE = re.compile(r"^ehrxqa_(?P<split>[a-zA-Z0-9-]+)_(?P<source_id>\d+)$")
IMAGE_RE = re.compile(
    r"(?:^|/)files/p\d+/p(?P<subject_id>\d+)/s(?P<study_id>\d+)/(?P<dicom_id>[^/]+)\.jpg$"
)

REFERENCE_TABLE_PREFIXES = ("d_",)
REFERENCE_TABLES = {"d_icd_diagnoses", "d_icd_procedures", "d_items", "d_labitems"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--ehrxqa-root", type=Path, default=DEFAULT_EHRXQA_ROOT)
    parser.add_argument(
        "--cxr-root",
        type=Path,
        default=DEFAULT_CXR_ROOT,
        help="MIMIC-CXR root containing files/ and mimic-cxr-reports/.",
    )
    parser.add_argument(
        "--cxr-jpg-root",
        type=Path,
        default=DEFAULT_CXR_JPG_ROOT,
        help="Optional MIMIC-CXR-JPG root or flat mimic-cxr2 export used as an image fallback.",
    )
    parser.add_argument("--mimiciv-root", type=Path, default=DEFAULT_MIMICIV_ROOT)
    parser.add_argument(
        "--mimic-iv-note-root",
        type=Path,
        default=DEFAULT_MIMIC_IV_NOTE_ROOT,
        help="Optional MIMIC-IV-Note root kept for provenance; this EHRXQA build uses CXR report TXT files.",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--allow-missing-nonlinked-assets",
        action="store_true",
        help=(
            "Deprecated compatibility flag. Non-linked patient-context CXR assets "
            "are optional; linked benchmark assets are still required."
        ),
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


def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    ensure_dir(path.parent)
    kwargs = {"ensure_ascii": False}
    if not compact:
        kwargs["indent"] = 2
    path.write_text(json.dumps(payload, **kwargs) + "\n", encoding="utf-8")


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


def normalize_ehrxqa_root(path: Path) -> tuple[Path, Path]:
    """Return (release_root, ehrxqa_dir)."""
    if (path / "ehrxqa" / "dataset").is_dir():
        return path, path / "ehrxqa"
    if (path / "dataset").is_dir() and (path / "database").is_dir():
        return path.parent, path
    raise FileNotFoundError(
        f"Could not find EHRXQA dataset/database under {path}. "
        "Pass either the 1.0.0 root or the 1.0.0/ehrxqa directory."
    )


def load_release_rows(input_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with input_path.open("r", encoding="utf-8") as handle:
        for line_index, line in enumerate(handle):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("source_benchmark") != "ehrxqa":
                continue
            match = QID_RE.match(str(row.get("qid") or ""))
            if not match:
                raise ValueError(f"Cannot parse EHRXQA qid: {row.get('qid')}")
            row["_source_line_index"] = line_index
            row["_source_split_from_qid"] = match.group("split")
            row["_source_id_from_qid"] = int(match.group("source_id"))
            rows.append(row)
    return rows


def strip_asset_prefix(path: str) -> str:
    text = path.replace("\\", "/")
    if "/" in text and text.split("/", 1)[0].endswith("OriginalLinked_v1"):
        return text.split("/", 1)[1]
    return text


def parse_image_ref(path: str) -> dict[str, Any]:
    stripped = strip_asset_prefix(path)
    match = IMAGE_RE.search(stripped)
    if not match:
        raise ValueError(f"Cannot parse EHRXQA image path: {path}")
    subject_id = int(match.group("subject_id"))
    study_id = int(match.group("study_id"))
    dicom_id = match.group("dicom_id")
    nested_relpath = (
        f"mimic-cxr/2.0.0/files/p{str(subject_id)[:2]}/p{subject_id}/"
        f"s{study_id}/{dicom_id}.jpg"
    )
    report_relpath = (
        f"mimic-cxr/2.0.0/mimic-cxr-reports/files/p{str(subject_id)[:2]}/"
        f"p{subject_id}/s{study_id}.txt"
    )
    return {
        "subject_id": subject_id,
        "study_id": study_id,
        "dicom_id": dicom_id,
        "image_relpath": nested_relpath,
        "report_relpath": report_relpath,
    }


def nested_file_relpath(subject_id: int, study_id: int, dicom_id: str) -> str:
    return (
        f"mimic-cxr/2.0.0/files/p{str(subject_id)[:2]}/p{subject_id}/"
        f"s{study_id}/{dicom_id}.jpg"
    )


def nested_report_relpath(subject_id: int, study_id: int) -> str:
    return (
        f"mimic-cxr/2.0.0/mimic-cxr-reports/files/p{str(subject_id)[:2]}/"
        f"p{subject_id}/s{study_id}.txt"
    )


def find_cxr_image(
    cxr_root: Path,
    cxr_jpg_root: Path,
    subject_id: int,
    study_id: int,
    dicom_id: str,
) -> Path | None:
    nested = Path(nested_file_relpath(subject_id, study_id, dicom_id)).relative_to("mimic-cxr/2.0.0")
    flat_name = f"p{str(subject_id)[:2]}_p{subject_id}_s{study_id}_{dicom_id}.jpg"
    candidates = [
        cxr_root / nested,
        cxr_root / "files" / nested.relative_to("files"),
        cxr_root / "mimic-cxr" / "2.0.0" / nested,
        cxr_root / "2.0.0" / nested,
        cxr_root / "2.1.0" / nested,
        cxr_root / "2.1.0-lite" / nested,
        cxr_root / "2.1.0-working-subset" / nested,
        cxr_root / "mimic-cxr2" / flat_name,
        cxr_root / flat_name,
        cxr_jpg_root / nested,
        cxr_jpg_root / "files" / nested.relative_to("files"),
        cxr_jpg_root / "2.0.0" / nested,
        cxr_jpg_root / "2.1.0" / nested,
        cxr_jpg_root / "2.1.0-lite" / nested,
        cxr_jpg_root / "2.1.0-working-subset" / nested,
        cxr_jpg_root / "mimic-cxr2" / flat_name,
        cxr_jpg_root / flat_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def find_cxr_report(cxr_root: Path, subject_id: int, study_id: int) -> Path | None:
    nested = Path(nested_report_relpath(subject_id, study_id)).relative_to("mimic-cxr/2.0.0")
    candidates = [
        cxr_root / nested,
        cxr_root / "mimic-cxr-reports" / nested.relative_to("mimic-cxr-reports"),
        cxr_root / "mimic-cxr" / "2.0.0" / nested,
        cxr_root / "2.0.0" / nested,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def table_is_reference(table_name: str) -> bool:
    return table_name in REFERENCE_TABLES or table_name.startswith(REFERENCE_TABLE_PREFIXES)


def load_source_records(ehrxqa_dir: Path, split: str, source_ids: set[int]) -> dict[int, dict[str, Any]]:
    path = ehrxqa_dir / "dataset" / f"{split}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    by_id = {int(row["id"]): row for row in rows if int(row["id"]) in source_ids}
    missing = sorted(source_ids - set(by_id))
    if missing:
        raise ValueError(f"Missing EHRXQA source ids in {path}: {missing[:20]}")
    return by_id


def load_tb_cxr(
    table_path: Path,
    selected_subjects: set[int],
    cxr_root: Path,
    cxr_jpg_root: Path,
) -> pd.DataFrame:
    frame = pd.read_csv(table_path)
    frame = frame[frame["subject_id"].isin(selected_subjects)].copy()
    frame = frame.where(pd.notna(frame), None)
    image_paths: list[str | None] = []
    report_paths: list[str | None] = []
    for row in frame.to_dict(orient="records"):
        subject_id = safe_int(row.get("subject_id"))
        study_id = safe_int(row.get("study_id"))
        dicom_id = str(row.get("image_id") or "").strip()
        if subject_id is None or study_id is None or not dicom_id:
            image_paths.append(None)
            report_paths.append(None)
            continue
        image_paths.append(
            nested_file_relpath(subject_id, study_id, dicom_id)
            if find_cxr_image(cxr_root, cxr_jpg_root, subject_id, study_id, dicom_id) is not None
            else None
        )
        report_paths.append(nested_report_relpath(subject_id, study_id))
    frame["image_path"] = image_paths
    frame["report_path"] = report_paths
    return frame


def write_subset_tables(
    *,
    source_tables_dir: Path,
    output_tables_dir: Path,
    selected_subjects: set[int],
    selected_tb_cxr: pd.DataFrame,
) -> list[str]:
    ensure_dir(output_tables_dir)
    written: list[str] = []
    for csv_path in sorted(source_tables_dir.glob("*.csv")):
        table_name = csv_path.stem
        if table_name == "tb_cxr":
            frame = selected_tb_cxr
        else:
            header = pd.read_csv(csv_path, nrows=0)
            if table_is_reference(table_name) or "subject_id" not in header.columns:
                frame = pd.read_csv(csv_path)
            else:
                chunks = []
                for chunk in pd.read_csv(csv_path, chunksize=200_000):
                    chunks.append(chunk[chunk["subject_id"].isin(selected_subjects)])
                frame = pd.concat(chunks, ignore_index=True) if chunks else header
        frame = frame.where(pd.notna(frame), None)
        out_path = output_tables_dir / csv_path.name
        frame.to_csv(out_path, index=False)
        written.append(table_name)
    for extra_name in ("mimic_iv_cxr.sql", "index.html"):
        src = source_tables_dir / extra_name
        if src.exists():
            link_or_copy(src, output_tables_dir / extra_name)
    return written


def write_sqlite_from_csvs(tables_dir: Path, sqlite_path: Path) -> None:
    if sqlite_path.exists():
        sqlite_path.unlink()
    with sqlite3.connect(sqlite_path) as conn:
        for csv_path in sorted(tables_dir.glob("*.csv")):
            frame = pd.read_csv(csv_path)
            frame = frame.where(pd.notna(frame), None)
            frame.to_sql(csv_path.stem, conn, if_exists="replace", index=False)


def copy_asset(src: Path | None, dst: Path, *, required: bool, label: str) -> bool:
    if src is None:
        if required:
            raise FileNotFoundError(f"Missing required {label}: {dst}")
        return False
    link_or_copy(src, dst)
    return True


def main() -> None:
    args = parse_args()
    reset_dir(args.output_root, args.overwrite)

    release_root, ehrxqa_dir = normalize_ehrxqa_root(args.ehrxqa_root)
    rows = load_release_rows(args.input)
    if not rows:
        raise ValueError(f"No EHRXQA rows found in {args.input}")

    split_ids: dict[str, set[int]] = {}
    for row in rows:
        split_ids.setdefault(row["_source_split_from_qid"], set()).add(row["_source_id_from_qid"])
    if set(split_ids) != {"test"}:
        raise ValueError(f"This release subset expects only EHRXQA test rows, got {sorted(split_ids)}")
    source_by_id = load_source_records(ehrxqa_dir, "test", split_ids["test"])

    linked_study_ids: set[int] = set()
    linked_image_refs: dict[tuple[int, int, str], dict[str, Any]] = {}
    for row in rows:
        for image_path in row.get("image_paths") or []:
            ref = parse_image_ref(image_path)
            linked_study_ids.add(ref["study_id"])
            linked_image_refs[(ref["subject_id"], ref["study_id"], ref["dicom_id"])] = ref

    selected_subjects = {int(row["subject_id"]) for row in rows}
    source_tables_dir = ehrxqa_dir / "database" / "gold"
    selected_tb_cxr = load_tb_cxr(
        source_tables_dir / "tb_cxr.csv",
        selected_subjects,
        args.cxr_root,
        args.cxr_jpg_root,
    )
    tb_cxr_by_study = {
        int(record["study_id"]): record
        for record in selected_tb_cxr.to_dict(orient="records")
        if safe_int(record.get("study_id")) is not None
    }

    output_ehrxqa_dir = args.output_root / "source_release" / "1.0.0" / "ehrxqa"
    output_dataset_dir = output_ehrxqa_dir / "dataset"
    output_tables_dir = output_ehrxqa_dir / "database" / "gold"
    subset_source_rows = [source_by_id[row["_source_id_from_qid"]] for row in rows]
    write_json(output_dataset_dir / "test.json", subset_source_rows)
    written_tables = write_subset_tables(
        source_tables_dir=source_tables_dir,
        output_tables_dir=output_tables_dir,
        selected_subjects=selected_subjects,
        selected_tb_cxr=selected_tb_cxr,
    )
    sqlite_relpath = "source_release/1.0.0/ehrxqa/database/gold/mimic_iv_cxr.sqlite"
    write_sqlite_from_csvs(output_tables_dir, args.output_root / sqlite_relpath)

    for rel in ("index.html", "LICENSE.txt", "SHA256SUMS.txt"):
        src = release_root / rel
        if src.exists():
            link_or_copy(src, args.output_root / "source_release" / "1.0.0" / rel)

    copied_images = 0
    copied_reports = 0
    missing_nonlinked_assets = 0
    asset_rows = selected_tb_cxr.to_dict(orient="records")
    for asset_row in asset_rows:
        subject_id = safe_int(asset_row.get("subject_id"))
        study_id = safe_int(asset_row.get("study_id"))
        dicom_id = str(asset_row.get("image_id") or "").strip()
        if subject_id is None or study_id is None or not dicom_id:
            continue
        linked = (subject_id, study_id, dicom_id) in linked_image_refs
        image_rel = nested_file_relpath(subject_id, study_id, dicom_id)
        report_rel = nested_report_relpath(subject_id, study_id)
        image_src = find_cxr_image(args.cxr_root, args.cxr_jpg_root, subject_id, study_id, dicom_id)
        report_src = find_cxr_report(args.cxr_root, subject_id, study_id)
        if copy_asset(image_src, args.output_root / image_rel, required=linked, label="CXR image"):
            copied_images += 1
        else:
            missing_nonlinked_assets += 1
        if copy_asset(report_src, args.output_root / report_rel, required=linked, label="CXR report"):
            copied_reports += 1
        else:
            missing_nonlinked_assets += 1

    manifest_rows: list[dict[str, Any]] = []
    for row in rows:
        source_id = row["_source_id_from_qid"]
        source_row = source_by_id[source_id]
        packaged_images = [strip_asset_prefix(path) for path in row.get("image_paths") or []]
        packaged_reports = [strip_asset_prefix(path) for path in row.get("report_paths") or []]
        study_ids = []
        dicom_ids = []
        raw_images = []
        raw_reports = []
        for image_path in packaged_images:
            ref = parse_image_ref(image_path)
            study_ids.append(ref["study_id"])
            dicom_ids.append(ref["dicom_id"])
            raw_images.append(str(Path("files") / Path(ref["image_relpath"]).relative_to("mimic-cxr/2.0.0/files")))
        for report_path in packaged_reports:
            raw_reports.append(
                str(Path("mimic-cxr-reports") / Path(report_path).relative_to("mimic-cxr/2.0.0/mimic-cxr-reports"))
            )
        manifest_rows.append(
            {
                "qid": row.get("qid"),
                "source_line_index": row.get("_source_line_index"),
                "source_index": row.get("source_index"),
                "source_benchmark": "ehrxqa",
                "task": row.get("task"),
                "source_split": row.get("source_split"),
                "source_id": source_id,
                "variant": "gold",
                "subject_id": row.get("subject_id"),
                "hadm_id": row.get("hadm_id"),
                "stay_id": row.get("stay_id"),
                "prediction_time": row.get("prediction_time"),
                "question": row.get("question"),
                "released_input_text": row.get("input_text"),
                "source_question": source_row.get("question"),
                "source_sql": source_row.get("query"),
                "source_template": source_row.get("template"),
                "source_value": source_row.get("value"),
                "source_answer": source_row.get("answer"),
                "ground_truth": row.get("ground_truth"),
                "answer_type": row.get("answer_type"),
                "modalities": row.get("modalities") or ["cxr_table", "cxr_image"],
                "study_ids": study_ids,
                "dicom_ids": dicom_ids,
                "packaged_db_relpath": sqlite_relpath,
                "packaged_table_root_relpath": "source_release/1.0.0/ehrxqa/database/gold",
                "packaged_image_relpaths": packaged_images,
                "packaged_report_relpaths": packaged_reports,
                "raw_image_source_relpaths": raw_images,
                "raw_report_source_relpaths": raw_reports,
                "source_dataset_relpath": "source_release/1.0.0/ehrxqa/dataset/test.json",
                "tb_cxr_context_rows": len(
                    [record for record in asset_rows if safe_int(record.get("subject_id")) == row.get("subject_id")]
                ),
            }
        )

    write_jsonl(args.output_root / "linked_manifests" / "test.jsonl", manifest_rows)

    readme = """# ClinSeek-MM-Bench-EHRXQA-source

This package is the source-aligned EHRXQA subset used by ClinSeek MM-Bench.
It contains only the EHRXQA rows present in `inputs/mm_bench.jsonl`.

Required source downloads:

- EHRXQA 1.0.0 answered release: `$EHRXQA_ROOT`
- MIMIC-CXR / MIMIC-CXR-JPG files and reports: `$MIMIC_CXR_ROOT`
- MIMIC-CXR-JPG fallback root: `$MIMIC_CXR_JPG_ROOT`
- MIMIC-IV latest local release kept for provenance: `$MIMICIV_ROOT`
- MIMIC-IV-Note kept for provenance when local notes are inspected: `$MIMIC_IV_NOTE_ROOT`

Contents:

- `source_release/1.0.0/ehrxqa/dataset/test.json`: official EHRXQA rows selected by ClinSeek.
- `source_release/1.0.0/ehrxqa/database/gold/`: official-schema subset tables.
- `source_release/1.0.0/ehrxqa/database/gold/mimic_iv_cxr.sqlite`: SQLite materialization of the subset tables.
	- `linked_manifests/test.jsonl`: row-level provenance and package-relative asset paths.
	- `mimic-cxr/2.0.0/...`: packaged CXR JPG and report TXT assets for selected patient context.

The linked manifest is a provenance artifact. It contains gold answers and the
original EHRXQA SQL fields, so it must not be used as the runtime model input.
	"""
    (args.output_root / "README.md").write_text(readme, encoding="utf-8")

    metadata = {
        "package_name": "ClinSeek-MM-Bench-EHRXQA-source",
        "input": "CLINSEEK_MM_BENCH_JSONL",
        "records": len(manifest_rows),
        "subjects": len(selected_subjects),
        "variant": "gold",
        "source_splits": dict(Counter(row["source_split"] for row in manifest_rows)),
        "written_tables": written_tables,
        "tb_cxr_context_rows": len(asset_rows),
        "unique_linked_images": len({p for row in manifest_rows for p in row["packaged_image_relpaths"]}),
        "unique_linked_reports": len({p for row in manifest_rows for p in row["packaged_report_relpaths"]}),
        "copied_images": copied_images,
        "copied_reports": copied_reports,
        "missing_nonlinked_context_assets": missing_nonlinked_assets,
        "source_path_env_vars": {
            "ehrxqa_root": "EHRXQA_ROOT",
            "cxr_root": "MIMIC_CXR_ROOT",
            "cxr_jpg_root": "MIMIC_CXR_JPG_ROOT",
            "mimiciv_root": "MIMICIV_ROOT",
            "mimic_iv_note_root": "MIMIC_IV_NOTE_ROOT",
        },
        "path_contract": {
            "manifest_paths": "relative_to_package_root",
            "packaged_image_relpaths": "relative_to_package_root",
            "packaged_report_relpaths": "relative_to_package_root",
            "raw_image_source_relpaths": "relative_to_MIMIC_CXR_ROOT_or_MIMIC_CXR_JPG_ROOT",
            "raw_report_source_relpaths": "relative_to_MIMIC_CXR_ROOT",
        },
    }
    write_json(args.output_root / "metadata.json", metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
