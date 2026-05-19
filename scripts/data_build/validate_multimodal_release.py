#!/usr/bin/env python3
"""Validate the released ClinSeek multimodal benchmark manifest or package.

The validator checks the ClinSeek-MM-Bench release tree:

- every row in inputs/mm_bench.jsonl has an expected source/task distribution;
- in package mode, every referenced patient database exists;
- in package mode, every referenced image/report path resolves under data/mm_bench/<source>/;
- in manifest-only mode, protected MIMIC-derived assets are not required;
- optional git-tree mode can validate a Hugging Face repository checkout without
  downloading all LFS file contents.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


EXPECTED_SOURCE_COUNTS = {"ehrxqa": 497, "medmod": 492}
EXPECTED_TASK_COUNTS = {
    "ehrxqa_image": 497,
    "medmod_decompensation": 125,
    "medmod_in_hospital_mortality": 125,
    "medmod_phenotyping": 120,
    "medmod_radiology": 122,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bench-root",
        type=Path,
        default=Path("data/ClinSeek-Bench"),
        help="Root of the ClinSeek-Bench repository or downloaded package.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Optional explicit mm_bench.jsonl path. Defaults to <bench-root>/inputs/mm_bench.jsonl.",
    )
    parser.add_argument(
        "--use-git-tree",
        action="store_true",
        help="Validate file presence against git ls-tree instead of local filesystem contents.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Validate only inputs/mm_bench.jsonl counts/schema and referenced path strings. Do not require DB/JPG/report files.",
    )
    parser.add_argument(
        "--allow-unexpected-counts",
        action="store_true",
        help="Do not fail if source/task counts differ from the frozen release counts.",
    )
    return parser.parse_args()


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
        return int(float(str(value)))
    except ValueError:
        return None


def git_tree_paths(root: Path) -> set[str]:
    output = subprocess.check_output(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD"],
        text=True,
    )
    return {line.strip() for line in output.splitlines() if line.strip()}


def release_asset_relpath(source: str, raw_path: str) -> str:
    """Map JSONL asset paths to their release-relative locations.

    The JSONL preserves original package prefixes such as
    EHRXQAOriginalLinked_v1/ or MedModOriginalLinked_v1/.  The released HF tree
    stores the actual assets under data/mm_bench/<source>/.
    """
    parts = Path(raw_path).parts
    if parts and parts[0].endswith("OriginalLinked_v1"):
        parts = parts[1:]
    return str(Path("data") / "mm_bench" / source / Path(*parts))


def exists(path: str, *, root: Path, tree: set[str] | None) -> bool:
    if tree is not None:
        return path in tree
    return (root / path).exists()


def validate(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bench_root = args.bench_root
    input_path = args.input or bench_root / "inputs" / "mm_bench.jsonl"
    rows = read_jsonl(input_path)
    tree = git_tree_paths(bench_root) if args.use_git_tree and not args.manifest_only else None

    source_counts = Counter(row.get("source_benchmark") for row in rows)
    task_counts = Counter(row.get("task") for row in rows)
    subjects_by_source: dict[str, set[int]] = defaultdict(set)
    images_by_source: dict[str, set[str]] = defaultdict(set)
    reports_by_source: dict[str, set[str]] = defaultdict(set)

    errors: list[dict[str, Any]] = []
    missing_by_kind = Counter()

    for row in rows:
        qid = row.get("qid")
        source = str(row.get("source_benchmark") or "")
        subject_id = safe_int(row.get("subject_id"))
        if source not in {"ehrxqa", "medmod"}:
            errors.append({"qid": qid, "kind": "bad_source", "value": source})
            continue
        if subject_id is None:
            errors.append({"qid": qid, "kind": "missing_subject_id"})
            continue

        subjects_by_source[source].add(subject_id)
        db_rel = str(Path("data") / "mm_bench" / source / "database" / f"patient_{subject_id}.db")
        if not args.manifest_only and not exists(db_rel, root=bench_root, tree=tree):
            missing_by_kind["database"] += 1
            errors.append({"qid": qid, "kind": "missing_database", "path": db_rel})

        for raw_path in row.get("image_paths") or []:
            rel = release_asset_relpath(source, str(raw_path))
            images_by_source[source].add(rel)
            if not args.manifest_only and not exists(rel, root=bench_root, tree=tree):
                missing_by_kind["image"] += 1
                errors.append({"qid": qid, "kind": "missing_image", "path": rel})

        for raw_path in row.get("report_paths") or []:
            rel = release_asset_relpath(source, str(raw_path))
            reports_by_source[source].add(rel)
            if not args.manifest_only and not exists(rel, root=bench_root, tree=tree):
                missing_by_kind["report"] += 1
                errors.append({"qid": qid, "kind": "missing_report", "path": rel})

    if not args.allow_unexpected_counts:
        if dict(source_counts) != EXPECTED_SOURCE_COUNTS:
            errors.append(
                {
                    "kind": "unexpected_source_counts",
                    "actual": dict(source_counts),
                    "expected": EXPECTED_SOURCE_COUNTS,
                }
            )
        if dict(task_counts) != EXPECTED_TASK_COUNTS:
            errors.append(
                {
                    "kind": "unexpected_task_counts",
                    "actual": dict(task_counts),
                    "expected": EXPECTED_TASK_COUNTS,
                }
            )

    summary = {
        "rows": len(rows),
        "source_counts": dict(source_counts),
        "task_counts": dict(task_counts),
        "subjects": {key: len(value) for key, value in subjects_by_source.items()},
        "unique_images": {key: len(value) for key, value in images_by_source.items()},
        "unique_reports": {key: len(value) for key, value in reports_by_source.items()},
        "missing_by_kind": dict(missing_by_kind),
        "error_count": len(errors),
        "validated_against": "manifest_only"
        if args.manifest_only
        else ("git_tree" if args.use_git_tree else "filesystem"),
        "bench_root": str(bench_root),
        "input": str(input_path),
    }
    return summary, errors


def main() -> int:
    args = parse_args()
    summary, errors = validate(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        print("First errors:", file=sys.stderr)
        for error in errors[:20]:
            print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
