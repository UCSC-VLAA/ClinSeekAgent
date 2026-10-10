#!/usr/bin/env python3
"""Render `input_text` for ClinSeek-Bench-MM from locally rebuilt databases.

The public `ClinSeek-Bench_multimodal.jsonl` carries everything the agentic
runner needs (question, labels, linked CXR study/image ids, prediction time,
scope, modalities, snapshot spec) but not `input_text`, the rendered EHR
context of the Curated Input setting, because it contains MIMIC-derived table
rows. This script re-renders it from the patient databases you rebuilt with
`build_ehrxqa_clinseek_mm_subset.py` / `build_medmod_clinseek_mm_subset.py` /
`combine_clinseek_mm_bench.py`, through the same strict runtime view the agent
sees (`EHRManager.load_ehr_for_sample` with the row's trusted cutoff, linked
studies, scope, task and modalities; policy
`multimodal-strict-temporal-scope-v1`):

    input_text = question
                 + "<ehr_context>"
                 + per table: "### <table>", the number of rows visible before
                   the cutoff, and the latest 80 of them as CSV
                 + "</ehr_context>"

The output is the strict manifest consumed by
`clinseekagent/run_multimodal_curated.py --data` and by the corpus builder
(`scripts/clean_data/build_evidence_corpus.py`), which re-renders the
`<ehr_context>` block from its policy-filtered record for the paper's Curated
Input / Full-Record Input settings.

    python scripts/data_build/materialize_multimodal_strict_manifest.py \\
        --manifest /path/to/ClinSeek-Bench/ClinSeek-Bench_multimodal.jsonl \\
        --ehrxqa-root /path/to/ClinSeek-MM-Bench/data/mm_bench/ehrxqa \\
        --medmod-root /path/to/ClinSeek-MM-Bench/data/mm_bench/medmod \\
        --output /path/to/ClinSeek-Bench_multimodal_strict.jsonl
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentlite.commons.EHRManager import EHRManager  # noqa: E402

STRICT_POLICY_VERSION = "multimodal-strict-temporal-scope-v1"
MAX_ROWS_PER_TABLE = 80

TIME_COLUMNS = {
    "events": "charttime",
    "stays": "intime",
    "tb_cxr": "studydatetime",
    "admissions": "admittime",
    "cost": "chargetime",
    "diagnoses_icd": "charttime",
    "procedures_icd": "charttime",
    "labevents": "charttime",
    "chartevents": "charttime",
    "prescriptions": "starttime",
    "transfers": "intime",
    "icustays": "intime",
    "inputevents": "starttime",
    "outputevents": "charttime",
    "microbiologyevents": "charttime",
}


def compact_value(value):
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ")
    return value


def table_to_text(table_name: str, frame: pd.DataFrame,
                  max_rows: int = MAX_ROWS_PER_TABLE) -> str:
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


def render_input_text(manager: EHRManager, row: dict) -> str:
    manager.load_ehr_for_sample(
        str(row["subject_id"]),
        row["prediction_time"],
        linked_study_ids=row.get("study_ids"),
        scope=row.get("scope"),
        task=row.get("task"),
        modalities=row.get("modalities"),
    )
    blocks = [table_to_text(name, manager.ehr_data[name])
              for name in sorted(manager.ehr_data)]
    ehr_text = "\n".join(blocks).strip()
    return "\n\n".join([
        str(row.get("question") or "").strip(),
        "<ehr_context>",
        ehr_text,
        "</ehr_context>",
    ])


def load_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, required=True,
                    help="public ClinSeek-Bench_multimodal.jsonl")
    ap.add_argument("--ehrxqa-root", type=Path, required=True,
                    help="rebuilt EHRXQA runtime root (database/, table_description/)")
    ap.add_argument("--medmod-root", type=Path, required=True,
                    help="rebuilt MedMod runtime root (database/, table_description/)")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--verify-existing", action="store_true",
                    help="compare against an `input_text` already in the "
                         "manifest instead of overwriting it silently")
    ap.add_argument("--quiet", action="store_true",
                    help="silence the EHR loader's per-row log output")
    args = ap.parse_args()

    rows = load_rows(args.manifest)
    roots = {"ehrxqa": args.ehrxqa_root, "medmod": args.medmod_root}
    for name, root in roots.items():
        if not (root / "database").is_dir():
            sys.exit(f"ERROR: --{name}-root {root} has no database/ directory")
    managers = {name: EHRManager(str(root)) for name, root in roots.items()}

    required = ("subject_id", "prediction_time", "scope", "modalities", "question")
    missing = [(r.get("qid"), f) for r in rows for f in required
               if r.get(f) in (None, "", [])]
    if missing:
        sys.exit(f"ERROR: {len(missing)} missing trusted field(s), e.g. "
                 f"{missing[:5]}; is this the public multimodal manifest?")

    stats: Counter = Counter()
    mismatches = []
    out_rows = []
    for index, row in enumerate(rows, 1):
        benchmark = str(row.get("source_benchmark") or "").lower()
        if benchmark not in managers:
            sys.exit(f"ERROR: row {row.get('qid')!r} has source_benchmark "
                     f"{benchmark!r}; expected ehrxqa or medmod")
        sink = io.StringIO()
        with (contextlib.redirect_stdout(sink) if args.quiet
              else contextlib.nullcontext()):
            input_text = render_input_text(managers[benchmark], row)
        policy = managers[benchmark].temporal_policy_version
        if policy != STRICT_POLICY_VERSION:
            sys.exit(f"ERROR: {row.get('qid')!r} loaded under policy {policy!r}, "
                     f"expected {STRICT_POLICY_VERSION!r}; do not override the "
                     "temporal policy for the multimodal roots")
        if args.verify_existing and row.get("input_text") not in (None, input_text):
            mismatches.append(row.get("qid"))
        # Keep the released field order, with `input_text` right after
        # `question`, so the rebuilt file matches the original layout.
        fixed = {}
        for key, value in row.items():
            if key == "input_text":
                continue
            fixed[key] = value
            if key == "question":
                fixed["input_text"] = input_text
        fixed["leakage_policy_version"] = policy
        out_rows.append(fixed)
        stats[f"benchmark:{benchmark}"] += 1
        stats[f"task:{row.get('task')}"] += 1
        if index % 100 == 0 or index == len(rows):
            print(json.dumps({"progress": index, "total": len(rows)}), flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in out_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    metadata = {
        "source_manifest": str(args.manifest),
        "output_manifest": str(args.output),
        "records": len(out_rows),
        "policy_version": STRICT_POLICY_VERSION,
        "max_rows_per_table": MAX_ROWS_PER_TABLE,
        "sha256": digest,
        "stats": dict(sorted(stats.items())),
    }
    args.output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    if args.verify_existing:
        print(f"verify: {len(out_rows) - len(mismatches)}/{len(out_rows)} "
              f"input_text identical", flush=True)
        if mismatches:
            print(f"  differing qids (first 10): {mismatches[:10]}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
