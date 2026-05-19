#!/usr/bin/env python3
"""Convert a source-aligned MedMod subset into ClinSeek-MM-Bench format.

Input is the package produced by build_medmod_release_original_subset.py.
Output is a compact release tree with:

- inputs/mm_bench_medmod.jsonl
- data/mm_bench/medmod/database/patient_<subject_id>.db
- data/mm_bench/medmod/mimic-cxr/2.0.0/files/...
- data/mm_bench/medmod/table_description/*
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_ORIGINAL_ROOT = Path(
    os.environ.get(
        "MEDMOD_ORIGINAL_SUBSET_ROOT",
        "data/build/ClinSeek-MM-Bench-MedMod-source",
    )
)
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "CLINSEEK_MEDMOD_MM_ROOT",
        "data/build/ClinSeek-MM-Bench-MedMod",
    )
)

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

TIME_COLUMNS = {
    "events": "charttime",
    "stays": "intime",
    "tb_cxr": "studydatetime",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, default=DEFAULT_ORIGINAL_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--asset-prefix", default="MedModOriginalLinked_v1")
    parser.add_argument("--max-table-rows", type=int, default=80)
    parser.add_argument(
        "--patient-db-scope",
        choices=("selected_stays", "full_subject"),
        default="selected_stays",
        help=(
            "Build patient DBs from only the official stays selected by the release manifest, or from the "
            "whole extracted subject folder."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--render-input-text",
        action="store_true",
        help="Render input_text from the rebuilt patient DB instead of preserving the released HF JSONL field.",
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


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            rows.append(json.loads(line))
    return rows


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


def compact_value(value: Any) -> Any:
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ")
    return value


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def study_datetime_from_metadata(record: dict[str, Any]) -> str | None:
    date = record.get("StudyDate")
    time = record.get("StudyTime")
    if date in (None, "") or time in (None, ""):
        return None
    try:
        time_text = f"{int(float(time)):06d}"
        dt = datetime.strptime(f"{int(float(date))} {time_text}", "%Y%m%d %H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def load_manifest(original_root: Path) -> list[dict[str, Any]]:
    manifest = original_root / "linked_manifests" / "all.jsonl"
    if not manifest.exists():
        raise FileNotFoundError(f"Missing source manifest: {manifest}")
    return read_jsonl(manifest)


def load_cxr_metadata(original_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    meta_root = original_root / "source_release" / "cxr_metadata"
    metadata_path = meta_root / "mimic-cxr-2.0.0-metadata.csv"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing packaged CXR metadata: {metadata_path}")
    metadata_df = pd.read_csv(metadata_path)
    metadata_by_dicom = {
        str(record["dicom_id"]): record for record in metadata_df.to_dict(orient="records")
    }

    split_by_dicom: dict[str, str] = {}
    split_path = meta_root / "mimic-cxr-2.0.0-split.csv"
    if split_path.exists():
        split_df = pd.read_csv(split_path)
        if {"dicom_id", "split"}.issubset(split_df.columns):
            split_by_dicom = {
                str(record["dicom_id"]): str(record["split"])
                for record in split_df.to_dict(orient="records")
            }
    return metadata_by_dicom, split_by_dicom


def build_tb_cxr_rows(
    manifest_rows: list[dict[str, Any]],
    metadata_by_dicom: dict[str, dict[str, Any]],
    split_by_dicom: dict[str, str],
) -> dict[int, pd.DataFrame]:
    rows_by_subject: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[int, int, str, int | None]] = set()
    for sample in manifest_rows:
        subject_id = safe_int(sample.get("subject_id"))
        hadm_id = safe_int(sample.get("hadm_id"))
        stay_id = safe_int(sample.get("stay_id"))
        if subject_id is None:
            continue
        for study_id, dicom_id, image_relpath in zip(
            sample.get("study_ids") or [],
            sample.get("dicom_ids") or [],
            sample.get("packaged_image_relpaths") or [],
        ):
            study_id_int = safe_int(study_id)
            if study_id_int is None:
                continue
            key = (subject_id, study_id_int, str(dicom_id), stay_id)
            if key in seen:
                continue
            seen.add(key)
            metadata = metadata_by_dicom.get(str(dicom_id), {})
            rows_by_subject[subject_id].append(
                {
                    "subject_id": subject_id,
                    "study_id": study_id_int,
                    "studydatetime": study_datetime_from_metadata(metadata)
                    or sample.get("prediction_time"),
                    "split": split_by_dicom.get(str(dicom_id), sample.get("source_split") or "test"),
                    "image_id": str(dicom_id),
                    "image_path": image_relpath,
                    "viewposition": str(metadata.get("ViewPosition") or "AP"),
                    "hadm_id": hadm_id,
                    "stay_id": stay_id,
                }
            )
    return {
        subject_id: pd.DataFrame(rows).sort_values(["studydatetime", "study_id", "image_id"])
        for subject_id, rows in rows_by_subject.items()
    }


def sanitize_stays(frame: pd.DataFrame) -> pd.DataFrame:
    drop_cols = [col for col in frame.columns if col.lower() in LEAKY_STAY_COLUMNS]
    return frame.drop(columns=drop_cols) if drop_cols else frame


def write_frame(conn: sqlite3.Connection, table_name: str, frame: pd.DataFrame) -> None:
    clean = frame.where(pd.notna(frame), None)
    clean.to_sql(table_name, conn, if_exists="replace", index=False)


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def csv_value(value: Any) -> Any:
    if value is None or value == "":
        return None
    return value


def create_text_table(
    conn: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    rows: list[list[Any]],
) -> None:
    q_table = quote_identifier(table_name)
    column_sql = ", ".join(f"{quote_identifier(column)} TEXT" for column in columns)
    conn.execute(f"CREATE TABLE {q_table} ({column_sql})")
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(columns))
    conn.executemany(f"INSERT INTO {q_table} VALUES ({placeholders})", rows)


def sample_stay_ids(samples: list[dict[str, Any]]) -> set[int]:
    stay_ids: set[int] = set()
    for sample in samples:
        for candidate in (
            sample.get("stay_id"),
            (sample.get("official_data_full_row") or {}).get("stay_id")
            if isinstance(sample.get("official_data_full_row"), dict)
            else None,
            (sample.get("official_listfile_row") or {}).get("stay_id")
            if isinstance(sample.get("official_listfile_row"), dict)
            else None,
        ):
            stay_id = safe_int(candidate)
            if stay_id is not None:
                stay_ids.add(stay_id)
    return stay_ids


def filter_frame_to_stays(frame: pd.DataFrame, stay_ids: set[int]) -> pd.DataFrame:
    if not stay_ids or "stay_id" not in frame.columns:
        return frame
    numeric_stay_ids = pd.to_numeric(frame["stay_id"], errors="coerce").astype("Int64")
    return frame[numeric_stay_ids.isin(stay_ids)].copy()


def write_csv_table(
    conn: sqlite3.Connection,
    table_name: str,
    csv_path: Path,
    *,
    stay_ids: set[int],
    drop_lower_columns: set[str] | None = None,
) -> None:
    drop_lower_columns = drop_lower_columns or set()
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        source_columns = next(reader)
        keep_indexes = [
            index
            for index, column in enumerate(source_columns)
            if column.lower() not in drop_lower_columns
        ]
        columns = [source_columns[index] for index in keep_indexes]
        stay_index = source_columns.index("stay_id") if "stay_id" in source_columns else None
        rows: list[list[Any]] = []
        for row in reader:
            if stay_ids and stay_index is not None:
                stay_id = safe_int(row[stay_index] if stay_index < len(row) else None)
                if stay_id not in stay_ids:
                    continue
            rows.append([csv_value(row[index]) if index < len(row) else None for index in keep_indexes])
    create_text_table(conn, table_name, columns, rows)


def build_patient_db(
    *,
    original_root: Path,
    samples: list[dict[str, Any]],
    output_db: Path,
    tb_cxr: pd.DataFrame | None,
    patient_db_scope: str,
) -> None:
    ensure_dir(output_db.parent)
    if not samples:
        raise ValueError("Cannot build MedMod patient DB from an empty sample list")
    sample = samples[0]
    subject_dirs = sample.get("ehr_subject_relpaths") or []
    if not subject_dirs:
        raise FileNotFoundError(f"Sample has no EHR subject path: {sample.get('qid')}")
    subject_dir = original_root / subject_dirs[0]
    if not subject_dir.is_dir():
        raise FileNotFoundError(f"Missing EHR subject dir: {subject_dir}")
    stay_ids = sample_stay_ids(samples) if patient_db_scope == "selected_stays" else set()
    with sqlite3.connect(output_db) as conn:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        events_path = subject_dir / "events.csv"
        stays_path = subject_dir / "stays.csv"
        if events_path.exists():
            write_csv_table(conn, "events", events_path, stay_ids=stay_ids)
        if stays_path.exists():
            write_csv_table(
                conn,
                "stays",
                stays_path,
                stay_ids=stay_ids,
                drop_lower_columns=LEAKY_STAY_COLUMNS,
            )
        if tb_cxr is not None and not tb_cxr.empty:
            write_frame(conn, "tb_cxr", filter_frame_to_stays(tb_cxr, stay_ids))
        conn.commit()


def materialize_patient_db(
    *,
    original_root: Path,
    samples: list[dict[str, Any]],
    output_db: Path,
    tb_cxr: pd.DataFrame | None,
    patient_db_scope: str,
) -> str:
    if not samples:
        raise ValueError("Cannot materialize MedMod patient DB from an empty sample list")
    sample = samples[0]
    subject_id = safe_int(sample.get("subject_id"))
    if subject_id is None:
        raise ValueError(f"Missing subject_id for {sample.get('qid')}")
    build_patient_db(
        original_root=original_root,
        samples=samples,
        output_db=output_db,
        tb_cxr=tb_cxr,
        patient_db_scope=patient_db_scope,
    )
    return f"source_aligned_{patient_db_scope}"


def filter_by_cutoff(table_name: str, frame: pd.DataFrame, cutoff: datetime | None) -> pd.DataFrame:
    if cutoff is None:
        return frame.copy()
    column = TIME_COLUMNS.get(table_name)
    if not column or column not in frame.columns:
        return frame.copy()
    parsed = pd.to_datetime(frame[column], errors="coerce")
    return frame[parsed.isna() | (parsed <= pd.Timestamp(cutoff))].copy()


def table_to_text(table_name: str, frame: pd.DataFrame, max_rows: int) -> str:
    total = len(frame)
    if total == 0:
        return f"### {table_name}\nRows visible before cutoff: 0\n"
    sort_col = TIME_COLUMNS.get(table_name)
    display = frame
    if sort_col and sort_col in display.columns:
        display = display.sort_values(sort_col, kind="stable")
    if max_rows and len(display) > max_rows:
        display = display.tail(max_rows)
        shown = f"latest {len(display)} of {total}"
    else:
        shown = f"{total} of {total}"
    clean = display.copy()
    for column in clean.columns:
        clean[column] = clean[column].map(compact_value)
    return (
        f"### {table_name}\n"
        f"Rows visible before cutoff: {total}; rows included below: {shown}\n"
        f"{clean.to_csv(index=False)}"
    )


def render_ehr_context_from_manager(manager: Any, sample: dict[str, Any], max_table_rows: int) -> str:
    manager.load_ehr_for_sample(str(sample["subject_id"]), sample["prediction_time"])
    blocks = []
    for table_name in sorted(manager.ehr_data):
        blocks.append(table_to_text(table_name, manager.ehr_data[table_name], max_table_rows))
    return "\n".join(blocks).strip()


def render_ehr_context(db_path: Path, prediction_time: str, max_table_rows: int) -> str:
    cutoff = parse_datetime(prediction_time)
    blocks: list[str] = []
    with sqlite3.connect(db_path) as conn:
        table_names = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        for table_name in table_names:
            frame = pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn)
            visible = filter_by_cutoff(table_name, frame, cutoff)
            blocks.append(table_to_text(table_name, visible, max_table_rows))
    return "\n".join(blocks).strip()


def build_input_text(sample: dict[str, Any], image_paths: list[str], ehr_text: str) -> str:
    return "\n\n".join(
        [
            str(sample.get("question") or "").strip(),
            "<ehr_context>",
            ehr_text,
            "</ehr_context>",
            "<image_inputs>",
            "\n".join(f"- {path}" for path in image_paths) if image_paths else "NONE",
            "</image_inputs>",
        ]
    )


def write_table_descriptions(benchmark_root: Path) -> None:
    database_root = benchmark_root / "database"
    table_desc_root = benchmark_root / "table_description"
    ensure_dir(table_desc_root)
    schemas: dict[str, list[str]] = {}
    for db_path in sorted(database_root.glob("patient_*.db"))[:50]:
        with sqlite3.connect(db_path) as conn:
            for (table_name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')]
                known = schemas.setdefault(table_name, [])
                for column in columns:
                    if column not in known:
                        known.append(column)
    desc_records = [
        {
            "file_name": table_name,
            "class": "ehr",
            "description": f"Schema extracted from MedMod subset table '{table_name}'.",
            "columns": [
                {
                    "column_name": column,
                    "description": f"Column '{column}' in table '{table_name}'.",
                }
                for column in columns
            ],
        }
        for table_name, columns in sorted(schemas.items())
    ]
    with (table_desc_root / "shorten_description.json").open("w", encoding="utf-8") as handle:
        for record in desc_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (table_desc_root / "link_information.json").write_text("", encoding="utf-8")


def write_runtime_metadata(benchmark_root: Path) -> None:
    write_json(
        benchmark_root / "metadata.json",
        {
            "package_name": "ClinSeek-MM-Bench-MedMod-runtime",
            "leakage_policy": {
                "sanitize_datetime_columns": True,
                "mask_future_datetime_columns": True,
                "row_timestamp_columns": TIME_COLUMNS,
                "datetime_columns": {
                    "events": ["charttime"],
                    "stays": ["intime", "admittime"],
                    "tb_cxr": ["studydatetime"],
                },
                "drop_columns": {
                    "stays": sorted(LEAKY_STAY_COLUMNS),
                },
            },
            "path_contract": {
                "db_path_hint": "relative_to_benchmark_root",
                "image_paths": "relative_to_benchmark_root",
                "report_paths": "relative_to_benchmark_root",
                "tb_cxr.image_path": "relative_to_benchmark_root",
            },
        },
    )


def main() -> None:
    args = parse_args()
    reset_dir(args.output_root, args.overwrite)
    manifest_rows = load_manifest(args.original_root)
    metadata_by_dicom, split_by_dicom = load_cxr_metadata(args.original_root)
    tb_cxr_by_subject = build_tb_cxr_rows(manifest_rows, metadata_by_dicom, split_by_dicom)

    bench_root = args.output_root / "data" / "mm_bench" / "medmod"
    database_root = bench_root / "database"
    inputs_root = args.output_root / "inputs"
    ensure_dir(database_root)
    ensure_dir(inputs_root)

    by_subject: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for sample in manifest_rows:
        subject_id = safe_int(sample.get("subject_id"))
        if subject_id is not None:
            by_subject[subject_id].append(sample)

    db_source_counts = Counter()
    for subject_id, samples in sorted(by_subject.items()):
        source = materialize_patient_db(
            original_root=args.original_root,
            samples=samples,
            output_db=database_root / f"patient_{subject_id}.db",
            tb_cxr=tb_cxr_by_subject.get(subject_id),
            patient_db_scope=args.patient_db_scope,
        )
        db_source_counts[source] += 1

    write_table_descriptions(bench_root)
    write_runtime_metadata(bench_root)

    ehr_manager = None
    need_rendered_input_text = args.render_input_text or any(
        not sample.get("released_input_text") for sample in manifest_rows
    )
    if need_rendered_input_text:
        try:
            from agentlite.commons.EHRManager import EHRManager  # type: ignore

            ehr_manager = EHRManager(str(bench_root))
        except Exception as exc:  # pragma: no cover - fallback for portable envs
            print({"ehr_manager_unavailable": repr(exc)}, flush=True)

    output_rows: list[dict[str, Any]] = []
    stats = Counter()
    for index, sample in enumerate(manifest_rows):
        subject_id = safe_int(sample.get("subject_id"))
        if subject_id is None:
            raise ValueError(f"Missing subject_id: {sample.get('qid')}")
        rel_images = list(sample.get("packaged_image_relpaths") or [])
        rel_reports = list(sample.get("packaged_report_relpaths") or [])
        for relpath in rel_images + rel_reports:
            link_or_copy(args.original_root / relpath, bench_root / relpath)
        prefixed_images = [f"{args.asset_prefix}/{relpath}" for relpath in rel_images]
        prefixed_reports = [f"{args.asset_prefix}/{relpath}" for relpath in rel_reports]
        db_path = database_root / f"patient_{subject_id}.db"
        if args.render_input_text or not sample.get("released_input_text"):
            if ehr_manager is not None:
                ehr_text = render_ehr_context_from_manager(ehr_manager, sample, args.max_table_rows)
            else:
                ehr_text = render_ehr_context(db_path, str(sample.get("prediction_time")), args.max_table_rows)
            input_text = build_input_text(sample, prefixed_images, ehr_text)
        else:
            input_text = sample.get("released_input_text")
        output_rows.append(
            {
                "qid": sample.get("qid"),
                "source_index": sample.get("source_index"),
                "source_benchmark": "medmod",
                "task": sample.get("source_task"),
                "source_split": sample.get("source_split"),
                "subject_id": sample.get("subject_id"),
                "hadm_id": sample.get("hadm_id"),
                "stay_id": sample.get("stay_id"),
                "prediction_time": sample.get("prediction_time"),
                "question": sample.get("question"),
                "input_text": input_text,
                "image_paths": prefixed_images,
                "report_paths": prefixed_reports,
                "ground_truth": sample.get("ground_truth"),
                "answer_type": sample.get("answer_type"),
                "modalities": sample.get("modalities") or ["ehr", "cxr"],
            }
        )
        stats[f"task:{sample.get('source_task')}"] += 1
        if (index + 1) % 100 == 0:
            print({"rendered_rows": index + 1, "total": len(manifest_rows)}, flush=True)

    output_path = inputs_root / "mm_bench_medmod.jsonl"
    with output_path.open("w", encoding="utf-8") as handle:
        for row in output_rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    metadata = {
        "package_name": "ClinSeek-MM-Bench-MedMod",
        "original_root": "MEDMOD_ORIGINAL_SUBSET_ROOT",
        "records": len(output_rows),
        "subjects": len(by_subject),
        "patient_dbs": len(list(database_root.glob("patient_*.db"))),
        "patient_db_sources": dict(sorted(db_source_counts.items())),
        "patient_db_scope": args.patient_db_scope,
        "unique_images": len({p for row in output_rows for p in row["image_paths"]}),
        "unique_reports": len({p for row in output_rows for p in row["report_paths"]}),
        "input_file": "inputs/mm_bench_medmod.jsonl",
        "bench_root": "data/mm_bench/medmod",
        "asset_prefix": args.asset_prefix,
        "input_text_source": "rendered_from_db" if need_rendered_input_text else "released_hf_jsonl",
        "stats": dict(sorted(stats.items())),
        "path_contract": {
            "image_paths": "strip asset_prefix, then resolve relative to bench_root",
            "report_paths": "strip asset_prefix, then resolve relative to bench_root",
            "patient_db": "data/mm_bench/medmod/database/patient_<subject_id>.db",
        },
        "leakage_policy": {
            "ground_truth": "kept only in output JSONL field, never in input_text",
            "stays_dropped_columns": sorted(LEAKY_STAY_COLUMNS),
            "diagnoses_table": "not materialized in patient DB",
            "runtime_cutoff_columns": TIME_COLUMNS,
        },
    }
    write_json(args.output_root / "metadata.json", metadata)

    readme = f"""# ClinSeek-MM-Bench-MedMod

This directory contains the MedMod-derived portion of ClinSeek-MM-Bench.

Use:

- `inputs/mm_bench_medmod.jsonl`
- `data/mm_bench/medmod`

The JSONL contains both agentic fields (`question`, `image_paths`, `subject_id`)
and one-shot fields (`input_text`, `image_paths`).  Patient SQLite DBs are under
`data/mm_bench/medmod/database`.
"""
    (args.output_root / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
