#!/usr/bin/env python3
"""Convert a source-aligned EHRXQA subset into ClinSeek-MM-Bench format."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_ORIGINAL_ROOT = Path(
    os.environ.get(
        "EHRXQA_ORIGINAL_SUBSET_ROOT",
        "data/build/ClinSeek-MM-Bench-EHRXQA-source",
    )
)
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "CLINSEEK_EHRXQA_MM_ROOT",
        "data/build/ClinSeek-MM-Bench-EHRXQA",
    )
)

TIME_COLUMNS = {
    "admissions": "admittime",
    "chartevents": "charttime",
    "cost": "chargetime",
    "diagnoses_icd": "charttime",
    "icustays": "intime",
    "inputevents": "starttime",
    "labevents": "charttime",
    "microbiologyevents": "charttime",
    "outputevents": "charttime",
    "prescriptions": "starttime",
    "procedures_icd": "charttime",
    "tb_cxr": "studydatetime",
    "transfers": "intime",
}

LEAKAGE_POLICY = {
    "sanitize_datetime_columns": True,
    "mask_future_datetime_columns": True,
    "row_timestamp_columns": TIME_COLUMNS,
    "datetime_columns": {
        "admissions": ["admittime", "dischtime"],
        "chartevents": ["charttime"],
        "cost": ["chargetime"],
        "diagnoses_icd": ["charttime"],
        "icustays": ["intime", "outtime"],
        "inputevents": ["starttime"],
        "labevents": ["charttime"],
        "microbiologyevents": ["charttime"],
        "outputevents": ["charttime"],
        "patients": ["dod"],
        "prescriptions": ["starttime", "stoptime"],
        "procedures_icd": ["charttime"],
        "tb_cxr": ["studydatetime"],
        "transfers": ["intime", "outtime"],
    },
}

REFERENCE_TABLES = {"d_icd_diagnoses", "d_icd_procedures", "d_items", "d_labitems"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, default=DEFAULT_ORIGINAL_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--asset-prefix", default="EHRXQAOriginalLinked_v1")
    parser.add_argument("--max-table-rows", type=int, default=80)
    parser.add_argument(
        "--render-input-text",
        action="store_true",
        help="Render input_text from the rebuilt patient DB instead of preserving the released HF JSONL field.",
    )
    parser.add_argument(
        "--copy-cxr-context",
        choices=("linked", "all"),
        default="linked",
        help="Copy only JSONL-linked assets or all CXR assets packaged by the source-aligned subset.",
    )
    parser.add_argument("--overwrite", action="store_true")
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
    if not src.exists():
        return
    if dst.exists():
        return
    shutil.copytree(src, dst, copy_function=lambda s, d: (link_or_copy(Path(s), Path(d)) or str(d)))


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
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


def table_to_text(table_name: str, frame: pd.DataFrame, max_rows: int) -> str:
    total = len(frame)
    if total == 0:
        return f"### {table_name}\nRows visible before cutoff: 0\n"
    display = frame
    sort_col = TIME_COLUMNS.get(table_name)
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


def parse_datetime(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed


def filter_by_cutoff(table_name: str, frame: pd.DataFrame, cutoff: pd.Timestamp | None) -> pd.DataFrame:
    if cutoff is None:
        return frame
    time_column = TIME_COLUMNS.get(table_name)
    if not time_column or time_column not in frame.columns:
        return frame
    parsed = pd.to_datetime(frame[time_column], errors="coerce")
    return frame.loc[parsed.notna() & (parsed <= cutoff)].copy()


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


def strip_asset_prefix(path: str) -> str:
    text = str(path).replace("\\", "/")
    if "/" in text and text.split("/", 1)[0].endswith("OriginalLinked_v1"):
        return text.split("/", 1)[1]
    return text


def load_manifest(original_root: Path) -> list[dict[str, Any]]:
    manifest = original_root / "linked_manifests" / "test.jsonl"
    if not manifest.exists():
        raise FileNotFoundError(f"Missing source manifest: {manifest}")
    return read_jsonl(manifest)


def source_tables_dir(original_root: Path) -> Path:
    path = original_root / "source_release" / "1.0.0" / "ehrxqa" / "database" / "gold"
    if not path.is_dir():
        raise FileNotFoundError(f"Missing EHRXQA gold table root: {path}")
    return path


def table_is_reference(table_name: str) -> bool:
    return table_name in REFERENCE_TABLES or table_name.startswith("d_")


def write_frame(conn: sqlite3.Connection, table_name: str, frame: pd.DataFrame, *, append: bool = False) -> None:
    clean = frame.where(pd.notna(frame), None)
    clean.to_sql(table_name, conn, if_exists="append" if append else "replace", index=False)


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def list_sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]


def list_attached_sqlite_tables(conn: sqlite3.Connection, schema: str) -> list[str]:
    q_schema = quote_identifier(schema)
    return [
        row[0]
        for row in conn.execute(f"SELECT name FROM {q_schema}.sqlite_master WHERE type='table' ORDER BY name")
    ]


def sqlite_table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})")]


def source_sqlite_path(original_root: Path) -> Path:
    return source_tables_dir(original_root) / "mimic_iv_cxr.sqlite"


def build_reference_database(tables_dir: Path, output_db: Path) -> list[str]:
    written: list[str] = []
    if output_db.exists():
        output_db.unlink()
    with sqlite3.connect(output_db) as conn:
        for csv_path in sorted(tables_dir.glob("*.csv")):
            table_name = csv_path.stem
            if not table_is_reference(table_name):
                continue
            write_frame(conn, table_name, pd.read_csv(csv_path, low_memory=False))
            written.append(table_name)
    return written


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


def csv_value(value: Any) -> Any:
    if value is None or value == "":
        return None
    return value


def build_reference_database_from_csvs(tables_dir: Path, output_db: Path) -> list[str]:
    written: list[str] = []
    if output_db.exists():
        output_db.unlink()
    with sqlite3.connect(output_db) as conn:
        for csv_path in sorted(tables_dir.glob("*.csv")):
            table_name = csv_path.stem
            if not table_is_reference(table_name):
                continue
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                columns = next(reader)
                rows = [[csv_value(value) for value in row] for row in reader]
            create_text_table(conn, table_name, columns, rows)
            written.append(table_name)
        conn.commit()
    return written


def build_patient_databases_from_source_csvs(
    *,
    original_root: Path,
    database_root: Path,
    subject_ids: set[int],
) -> dict[str, Any]:
    tables_dir = source_tables_dir(original_root)
    subject_lookup = {str(subject_id): subject_id for subject_id in subject_ids}
    subject_tables: dict[int, list[tuple[str, list[str], list[list[Any]]]]] = {
        subject_id: [] for subject_id in subject_ids
    }
    built_tables: list[str] = []
    for csv_path in sorted(tables_dir.glob("*.csv")):
        table_name = csv_path.stem
        if table_is_reference(table_name):
            continue
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "subject_id" not in reader.fieldnames:
                continue
            columns = list(reader.fieldnames)
            if table_name == "patients":
                columns = [column for column in columns if column != "dod"]
            grouped: dict[int, list[list[Any]]] = {}
            for row in reader:
                subject_id = subject_lookup.get(str(row.get("subject_id") or ""))
                if subject_id is None:
                    continue
                grouped.setdefault(subject_id, []).append([csv_value(row.get(column)) for column in columns])
        if not grouped:
            continue
        built_tables.append(table_name)
        for subject_id, rows in grouped.items():
            subject_tables[subject_id].append((table_name, columns, rows))

    built_subjects = {subject_id for subject_id, tables in subject_tables.items() if tables}
    missing = sorted(subject_ids - built_subjects)
    if missing:
        raise FileNotFoundError(f"Missing built patient DBs for subjects: {missing[:20]}")
    ensure_dir(database_root)
    for subject_id, tables in sorted(subject_tables.items()):
        db_path = database_root / f"patient_{subject_id}.db"
        if db_path.exists():
            db_path.unlink()
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            for table_name, columns, rows in tables:
                create_text_table(conn, table_name, columns, rows)
            conn.commit()
    reference_tables = build_reference_database_from_csvs(tables_dir, database_root / "reference_table.db")
    return {
        "patient_db_source": "source_aligned_subset_csv",
        "source_table_root": "source_release/1.0.0/ehrxqa/database/gold",
        "built_subjects": len(built_subjects),
        "built_tables": built_tables,
        "reference_tables": reference_tables,
    }


def build_reference_database_from_sqlite(source_db: Path, output_db: Path) -> list[str]:
    written: list[str] = []
    if output_db.exists():
        output_db.unlink()
    with sqlite3.connect(output_db) as conn:
        conn.execute("ATTACH DATABASE ? AS src", (str(source_db),))
        for table_name in list_attached_sqlite_tables(conn, "src"):
            if not table_is_reference(table_name):
                continue
            q_table = quote_identifier(table_name)
            conn.execute(f"CREATE TABLE {q_table} AS SELECT * FROM src.{q_table}")
            written.append(table_name)
        conn.commit()
    return written


def build_patient_databases_from_source_sqlite(
    *,
    original_root: Path,
    database_root: Path,
    subject_ids: set[int],
) -> dict[str, Any]:
    source_db = source_sqlite_path(original_root)
    if not source_db.exists():
        raise FileNotFoundError(f"Missing EHRXQA source SQLite: {source_db}")
    ensure_dir(database_root)
    with sqlite3.connect(source_db) as src_conn:
        table_names = list_sqlite_tables(src_conn)
        patient_tables = [
            table_name
            for table_name in table_names
            if not table_is_reference(table_name)
            and "subject_id" in sqlite_table_columns(src_conn, table_name)
        ]

    built_subjects = set()
    for subject_id in sorted(subject_ids):
        db_path = database_root / f"patient_{subject_id}.db"
        if db_path.exists():
            db_path.unlink()
        with sqlite3.connect(db_path) as dst_conn:
            dst_conn.execute("PRAGMA journal_mode=OFF")
            dst_conn.execute("PRAGMA synchronous=OFF")
            dst_conn.execute("ATTACH DATABASE ? AS src", (str(source_db),))
            rows_written = 0
            for table_name in patient_tables:
                q_table = quote_identifier(table_name)
                row_count = dst_conn.execute(
                    f"SELECT COUNT(*) FROM src.{q_table} WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
                if row_count == 0:
                    continue
                columns = [
                    row[1]
                    for row in dst_conn.execute(f"PRAGMA src.table_info({quote_identifier(table_name)})")
                ]
                if table_name == "patients":
                    columns = [column for column in columns if column != "dod"]
                select_columns = ", ".join(quote_identifier(column) for column in columns)
                dst_conn.execute(
                    f"CREATE TABLE {q_table} AS SELECT {select_columns} FROM src.{q_table} WHERE subject_id = ?",
                    (subject_id,),
                )
                rows_written += row_count
            dst_conn.commit()
        if rows_written:
            built_subjects.add(subject_id)

    missing = sorted(subject_ids - built_subjects)
    if missing:
        raise FileNotFoundError(f"Missing built patient DBs for subjects: {missing[:20]}")
    reference_tables = build_reference_database_from_sqlite(source_db, database_root / "reference_table.db")
    return {
        "patient_db_source": "source_aligned_subset_sqlite",
        "source_sqlite": "source_release/1.0.0/ehrxqa/database/gold/mimic_iv_cxr.sqlite",
        "built_subjects": len(built_subjects),
        "built_tables": patient_tables,
        "reference_tables": reference_tables,
    }


def build_patient_databases_from_source(
    *,
    original_root: Path,
    database_root: Path,
    subject_ids: set[int],
) -> dict[str, Any]:
    tables_dir = source_tables_dir(original_root)
    if any(tables_dir.glob("*.csv")):
        return build_patient_databases_from_source_csvs(
            original_root=original_root,
            database_root=database_root,
            subject_ids=subject_ids,
        )

    sqlite_path = source_sqlite_path(original_root)
    if sqlite_path.exists():
        return build_patient_databases_from_source_sqlite(
            original_root=original_root,
            database_root=database_root,
            subject_ids=subject_ids,
        )

    built_tables: list[str] = []
    subject_frames: dict[int, list[tuple[str, pd.DataFrame]]] = {subject_id: [] for subject_id in subject_ids}
    for csv_path in sorted(tables_dir.glob("*.csv")):
        table_name = csv_path.stem
        if table_is_reference(table_name):
            continue
        header = pd.read_csv(csv_path, nrows=0)
        if "subject_id" not in header.columns:
            continue
        built_tables.append(table_name)
        frame = pd.read_csv(csv_path, low_memory=False)
        subset = frame[frame["subject_id"].isin(subject_ids)]
        if subset.empty:
            continue
        for subject_id, group in subset.groupby("subject_id", sort=True):
            subject_frames[int(subject_id)].append((table_name, group.copy()))

    built_subjects = {subject_id for subject_id, frames in subject_frames.items() if frames}
    missing = sorted(subject_ids - built_subjects)
    if missing:
        raise FileNotFoundError(f"Missing built patient DBs for subjects: {missing[:20]}")
    ensure_dir(database_root)
    for subject_id, frames in sorted(subject_frames.items()):
        db_path = database_root / f"patient_{subject_id}.db"
        if db_path.exists():
            db_path.unlink()
        with sqlite3.connect(db_path) as conn:
            for table_name, frame in frames:
                write_frame(conn, table_name, frame)
    reference_tables = build_reference_database(tables_dir, database_root / "reference_table.db")
    return {
        "patient_db_source": "source_aligned_subset",
        "built_subjects": len(built_subjects),
        "built_tables": built_tables,
        "reference_tables": reference_tables,
    }


def write_runtime_metadata(bench_root: Path) -> None:
    write_json(
        bench_root / "metadata.json",
        {
            "package_name": "ClinSeek-MM-Bench-EHRXQA-runtime",
            "leakage_policy": LEAKAGE_POLICY,
            "path_contract": {
                "db_path_hint": "relative_to_benchmark_root",
                "image_paths": "relative_to_benchmark_root",
                "report_paths": "relative_to_benchmark_root",
                "tb_cxr.image_path": "relative_to_benchmark_root",
                "tb_cxr.report_path": "relative_to_benchmark_root",
            },
        },
    )


def copy_linked_assets(original_root: Path, bench_root: Path, row: dict[str, Any]) -> tuple[list[str], list[str]]:
    rel_images = [strip_asset_prefix(path) for path in row.get("packaged_image_relpaths") or []]
    rel_reports = [strip_asset_prefix(path) for path in row.get("packaged_report_relpaths") or []]
    for relpath in rel_images + rel_reports:
        link_or_copy(original_root / relpath, bench_root / relpath)
    return rel_images, rel_reports


def copy_all_cxr_context(original_root: Path, bench_root: Path) -> None:
    copytree_links(original_root / "mimic-cxr", bench_root / "mimic-cxr")


def write_table_descriptions(bench_root: Path) -> None:
    dst = bench_root / "table_description"
    ensure_dir(dst)
    (dst / "link_information.json").write_text("", encoding="utf-8")
    (dst / "shorten_description.json").write_text("", encoding="utf-8")


def main() -> None:
    args = parse_args()
    reset_dir(args.output_root, args.overwrite)
    manifest_rows = load_manifest(args.original_root)

    bench_root = args.output_root / "data" / "mm_bench" / "ehrxqa"
    database_root = bench_root / "database"
    inputs_root = args.output_root / "inputs"
    ensure_dir(database_root)
    ensure_dir(inputs_root)

    by_subject: dict[int, dict[str, Any]] = {}
    for row in manifest_rows:
        subject_id = safe_int(row.get("subject_id"))
        if subject_id is None:
            raise ValueError(f"Missing subject_id: {row.get('qid')}")
        by_subject.setdefault(subject_id, row)

    db_build_summary = build_patient_databases_from_source(
        original_root=args.original_root,
        database_root=database_root,
        subject_ids=set(by_subject),
    )
    write_table_descriptions(bench_root)
    ehr_manager_root = bench_root
    write_runtime_metadata(bench_root)

    if args.copy_cxr_context == "all":
        copy_all_cxr_context(args.original_root, bench_root)

    ehr_manager = None
    need_rendered_input_text = args.render_input_text or any(
        not row.get("released_input_text") for row in manifest_rows
    )
    if need_rendered_input_text:
        try:
            from agentlite.commons.EHRManager import EHRManager  # type: ignore

            ehr_manager = EHRManager(str(ehr_manager_root))
        except Exception as exc:  # pragma: no cover - fallback for portable envs
            print({"ehr_manager_unavailable": repr(exc)}, flush=True)
    output_rows: list[dict[str, Any]] = []
    stats = Counter()
    linked_image_count = 0
    linked_report_count = 0
    for index, row in enumerate(manifest_rows):
        rel_images, rel_reports = copy_linked_assets(args.original_root, bench_root, row)
        linked_image_count += len(rel_images)
        linked_report_count += len(rel_reports)
        prefixed_images = [f"{args.asset_prefix}/{relpath}" for relpath in rel_images]
        prefixed_reports = [f"{args.asset_prefix}/{relpath}" for relpath in rel_reports]
        if args.render_input_text or not row.get("released_input_text"):
            if ehr_manager is not None:
                ehr_text = render_ehr_context_from_manager(ehr_manager, row, args.max_table_rows)
            else:
                subject_id = safe_int(row.get("subject_id"))
                if subject_id is None:
                    raise ValueError(f"Missing subject_id: {row.get('qid')}")
                ehr_text = render_ehr_context(
                    database_root / f"patient_{subject_id}.db",
                    str(row.get("prediction_time")),
                    args.max_table_rows,
                )
            input_text = build_input_text(row, prefixed_images, ehr_text)
        else:
            input_text = row.get("released_input_text")
        output_rows.append(
            {
                "qid": row.get("qid"),
                "source_index": row.get("source_index"),
                "source_benchmark": "ehrxqa",
                "task": row.get("task"),
                "source_split": row.get("source_split"),
                "subject_id": row.get("subject_id"),
                "hadm_id": row.get("hadm_id"),
                "stay_id": row.get("stay_id"),
                "prediction_time": row.get("prediction_time"),
                "question": row.get("question"),
                "input_text": input_text,
                "image_paths": prefixed_images,
                "report_paths": prefixed_reports,
                "ground_truth": row.get("ground_truth"),
                "answer_type": row.get("answer_type"),
                "modalities": row.get("modalities") or ["cxr_table", "cxr_image"],
            }
        )
        stats[f"task:{row.get('task')}"] += 1
        if (index + 1) % 100 == 0:
            print({"rendered_rows": index + 1, "total": len(manifest_rows)}, flush=True)

    output_path = inputs_root / "mm_bench_ehrxqa.jsonl"
    with output_path.open("w", encoding="utf-8") as handle:
        for row in output_rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    metadata = {
        "package_name": "ClinSeek-MM-Bench-EHRXQA",
        "original_root": "EHRXQA_ORIGINAL_SUBSET_ROOT",
        "records": len(output_rows),
        "subjects": len(by_subject),
        "patient_dbs": len(list(database_root.glob("patient_*.db"))),
        "db_build_summary": db_build_summary,
        "unique_linked_images": len({p for row in output_rows for p in row["image_paths"]}),
        "unique_linked_reports": len({p for row in output_rows for p in row["report_paths"]}),
        "linked_image_refs": linked_image_count,
        "linked_report_refs": linked_report_count,
        "copy_cxr_context": args.copy_cxr_context,
        "input_text_source": "rendered_from_db" if need_rendered_input_text else "released_hf_jsonl",
        "input_file": "inputs/mm_bench_ehrxqa.jsonl",
        "bench_root": "data/mm_bench/ehrxqa",
        "asset_prefix": args.asset_prefix,
        "stats": dict(sorted(stats.items())),
        "path_contract": {
            "image_paths": "strip asset_prefix, then resolve relative to bench_root",
            "report_paths": "strip asset_prefix, then resolve relative to bench_root",
            "patient_db": "data/mm_bench/ehrxqa/database/patient_<subject_id>.db",
        },
        "leakage_policy": {
            "ground_truth": "kept only in output JSONL field, never in input_text",
            "patient_db_source": db_build_summary.get("patient_db_source"),
            "runtime_policy": "EHRManager uses benchmark metadata leakage_policy at load time",
        },
    }
    write_json(args.output_root / "metadata.json", metadata)

    readme = """# ClinSeek-MM-Bench-EHRXQA

This directory contains the EHRXQA-derived portion of ClinSeek-MM-Bench.

Use:

- `inputs/mm_bench_ehrxqa.jsonl`
- `data/mm_bench/ehrxqa`

The JSONL contains both agentic fields (`question`, `image_paths`, `subject_id`)
and curated-input fields (`input_text`, `image_paths`).  Patient SQLite DBs are
under `data/mm_bench/ehrxqa/database`.
"""
    (args.output_root / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
