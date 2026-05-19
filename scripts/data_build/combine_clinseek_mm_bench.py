#!/usr/bin/env python3
"""Combine EHRXQA and MedMod subsets into the ClinSeek-MM-Bench package."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REFERENCE_INPUT = Path(
    os.environ.get(
        "CLINSEEK_MM_BENCH_JSONL",
        str(REPO_ROOT / "inputs" / "mm_bench.jsonl"),
    )
)
DEFAULT_EHRXQA_SUBSET_ROOT = Path(
    os.environ.get("CLINSEEK_EHRXQA_MM_ROOT", "data/build/ClinSeek-MM-Bench-EHRXQA")
)
DEFAULT_MEDMOD_SUBSET_ROOT = Path(
    os.environ.get("CLINSEEK_MEDMOD_MM_ROOT", "data/build/ClinSeek-MM-Bench-MedMod")
)
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get("CLINSEEK_MM_RELEASE_ROOT", "data/build/ClinSeek-MM-Bench")
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-input", type=Path, default=DEFAULT_REFERENCE_INPUT)
    parser.add_argument("--ehrxqa-root", type=Path, default=DEFAULT_EHRXQA_SUBSET_ROOT)
    parser.add_argument("--medmod-root", type=Path, default=DEFAULT_MEDMOD_SUBSET_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
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
        raise FileNotFoundError(f"Missing source directory: {src}")
    if dst.exists():
        return
    shutil.copytree(src, dst, copy_function=lambda s, d: (link_or_copy(Path(s), Path(d)) or str(d)))


def drop_runtime_sidecars(output_root: Path) -> None:
    """Keep final data/mm_bench file layout aligned with the HF release tree."""
    for relpath in (
        "data/mm_bench/ehrxqa/database/reference_table.db",
        "data/mm_bench/ehrxqa/metadata.json",
        "data/mm_bench/medmod/metadata.json",
    ):
        path = output_root / relpath
        if path.exists():
            path.unlink()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_subset_rows(root: Path, filename: str) -> dict[str, dict[str, Any]]:
    path = root / "inputs" / filename
    rows = read_jsonl(path)
    return {str(row["qid"]): row for row in rows}


def main() -> None:
    args = parse_args()
    reset_dir(args.output_root, args.overwrite)

    reference_rows = read_jsonl(args.reference_input)
    ehrxqa_by_qid = load_subset_rows(args.ehrxqa_root, "mm_bench_ehrxqa.jsonl")
    medmod_by_qid = load_subset_rows(args.medmod_root, "mm_bench_medmod.jsonl")
    by_qid = {**ehrxqa_by_qid, **medmod_by_qid}

    missing = [row["qid"] for row in reference_rows if row.get("qid") not in by_qid]
    extra = sorted(set(by_qid) - {row.get("qid") for row in reference_rows})
    if missing or extra:
        raise ValueError({"missing": missing[:20], "extra": extra[:20]})

    output_rows = [by_qid[str(row["qid"])] for row in reference_rows]
    write_jsonl(args.output_root / "inputs" / "mm_bench.jsonl", output_rows)

    copytree_links(
        args.ehrxqa_root / "data" / "mm_bench" / "ehrxqa",
        args.output_root / "data" / "mm_bench" / "ehrxqa",
    )
    copytree_links(
        args.medmod_root / "data" / "mm_bench" / "medmod",
        args.output_root / "data" / "mm_bench" / "medmod",
    )
    drop_runtime_sidecars(args.output_root)

    metadata = {
        "package_name": "ClinSeek-MM-Bench",
        "reference_input": "CLINSEEK_MM_BENCH_JSONL",
        "ehrxqa_root": "CLINSEEK_EHRXQA_MM_ROOT",
        "medmod_root": "CLINSEEK_MEDMOD_MM_ROOT",
        "records": len(output_rows),
        "source_counts": {
            "ehrxqa": len(ehrxqa_by_qid),
            "medmod": len(medmod_by_qid),
        },
        "input_file": "inputs/mm_bench.jsonl",
        "bench_root": "data/mm_bench",
    }
    write_json(args.output_root / "metadata.json", metadata)

    readme = """# ClinSeek-MM-Bench

This package combines the EHRXQA-derived and MedMod-derived subsets into the
final ClinSeek multimodal benchmark layout.

Use:

- `inputs/mm_bench.jsonl`
- `data/mm_bench`
"""
    (args.output_root / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
