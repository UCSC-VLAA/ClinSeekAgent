#!/usr/bin/env python3
"""Build the ClinSeek-Bench evidence corpora read by the curated runners.

The Curated Input (`recent`) and Full-Record Input (`full`) inference modes
(`clinseekagent/run_text_curated.py` / `run_multimodal_curated.py
--corpus-root <out> --view recent|full`) read a corpus root produced here:

    <out>/all_data.jsonl                      one record per question
    <out>/all_data.json                       the same records as a JSON array
    <out>/evidence/<qid>/full/<table>.csv     every row visible at the cutoff
    <out>/evidence/<qid>/recent/<table>.csv   the curated subset of `full/`
    <out>/evidence/<qid>/_manifest.json       per-question provenance
    <out>/_corpus_manifest.json               build summary

Paper setting (`--policy-mode aggressive`)
------------------------------------------
Text (EHR-Bench, 1,800 questions):

    python scripts/clean_data/build_evidence_corpus.py \\
        --bench text \\
        --manifest /path/to/ehr_bench_sampled_40_per_task.json \\
        --data-root /path/to/EHR-Bench \\
        --out /path/to/corpora/text_final_aggressive \\
        --policy-mode aggressive

Multimodal (989 questions):

    python scripts/clean_data/build_evidence_corpus.py \\
        --bench mm \\
        --manifest /path/to/mm_bench.jsonl \\
        --ehrxqa-root /path/to/mm_bench/ehrxqa \\
        --medmod-root /path/to/mm_bench/medmod \\
        --raw-mimic-root /path/to/MIMIC-IV/mimic_iv \\
        --cache-dir /path/to/build_cache \\
        --out /path/to/corpora/mm_final_aggressive \\
        --policy-mode aggressive

`--data-root` holds `database/patient_<subject_id>.db`; each MM source root
holds `database/` and the packaged `mimic-cxr/` assets; `--raw-mimic-root` holds
raw MIMIC-IV `hosp/`, `icu/` and `note/` (MIMIC-IV-Note) CSVs.

Both builds are single-process by default. To parallelise, run N processes with
the same arguments plus `--shard I/N` (I = 0..N-1), then merge once:

    python scripts/clean_data/build_evidence_corpus.py --bench text \\
        --manifest ... --out ... --merge-shards N

Pipeline (one filtered pool per question, two views of it)
-----------------------------------------------------------

    manifest row
      -> EHRManager.load_ehr_for_sample()   temporal runtime policy
           text: strict-ehr-runtime-v8, MM: multimodal-strict-temporal-scope-v1
           (with the target-label guard disabled, see policy_overrides.py)
      -> AvailabilityFilter.apply()          independent availability filter
           (rows/values whose information became readable after the cutoff)
      -> sweep_future_values()               aggressive mode: blank any cell
                                             whose text holds a later date
      -> [MM] keep only linked images in multi-image tb_cxr cells
      -> FILTERED POOL
           |-- full   : every row the filters leave visible
           +-- recent : a row subset of that same pool
                  text: EHR-R1's original curated selection (parsed from the
                        manifest `input`) intersected with the pool
                  MM:   per-table most recent K rows (K = --recent-per-table)

Both views come out of one pool in one pass, so they share one policy and
`recent` is a row subset of `full` by construction (audit_evidence_corpus.py
re-verifies both). The benchmark's `prediction_time` is never moved: it is the
cutoff and is printed verbatim as the prompt's `Current Time`. No rule looks at
labels or outcomes; information available before the cutoff stays even when it
reveals the answer.

Prompts
-------
Text: `question` is re-rendered from `recent/` (the manifest `question` is used
only as the template around `<patient_timeline>`; its embedded EHR-R1 timeline
was rendered from the unfiltered record). The Full-Record prompt is rendered at
run time from `full/` by `clinseekagent/record_context.py`.
MM: `input_text` is the manifest template with `<ehr_context>` re-rendered from
`full/` (`input_text_recent` from `recent/`); record_context re-renders both
views from the CSVs at run time. `Linked Report Count` follows the reports that
survive the availability filter.

Renderers (`question_render`, `mm_context_render`, `ehr_r1_selection`) are
imported from `clinseekagent/`, the same copies record_context uses.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _p in (HERE, REPO_ROOT / "clinseekagent", REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from agentlite.commons.EHRManager import EHRManager  # noqa: E402
from agentlite.commons import temporal_policy as text_temporal_policy  # noqa: E402
from agentlite.commons import multimodal_temporal_policy as mm_temporal_policy  # noqa: E402

import availability_filter  # noqa: E402
import ehr_r1_selection  # noqa: E402
import mm_context_render  # noqa: E402
import policy_overrides  # noqa: E402
import question_render  # noqa: E402

TEXT_POLICY = text_temporal_policy.POLICY_VERSION
MM_POLICY = mm_temporal_policy.POLICY_VERSION

#: MM `question` opens with the task instruction wrapped in this tag. Text
#: carries the same string in its own `instruction` manifest field; lifting it
#: out lets the two benchmarks share one record schema.
TASK_INSTRUCTION_RE = re.compile(
    r"<task_instruction>(.*?)</task_instruction>", re.DOTALL
)
#: The MM prompt states how many reports are attached; it must follow the
#: reports that survive the availability filter.
REPORT_COUNT_RE = re.compile(r"(?m)^Linked Report Count: \d+$")

# Manifest keys copied into each record (and its nested `query` object).
# `target_hadm_id` / `target_stay_id` name the encounter a decision-making task
# asks about; the agentic tool pool forwards them into `load_ehr`.
TEXT_QUERY_KEYS = [
    "prediction_time", "instruction", "task", "task_type",
    "subject_id", "hadm_id", "question", "candidates",
    "target_hadm_id", "target_stay_id",
    "source_file", "source_idx",
]
# `image_paths` / `report_paths` / `dicom_ids` are load-bearing for MM: the
# runners attach images and inline reports from them.
MM_QUERY_KEYS = [
    "prediction_time", "task", "source_benchmark", "subject_id", "hadm_id",
    "stay_id", "question", "input_text", "scope", "study_ids", "modalities",
    "answer_type", "image_paths", "report_paths", "dicom_ids",
    "source_split", "source_index",
]
MM_SOURCES = ("ehrxqa", "medmod")

#: `tb_cxr` columns that hold one pipe-joined member per image of a study.
CXR_ALIGNED_FIELDS = ("image_id", "image_path", "viewposition")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def row_recency(df: pd.DataFrame) -> pd.Series | None:
    """Per-row recency key: the latest parseable timestamp on that row.

    Every value here has already passed the temporal filters, so taking the
    max is safe: a post-cutoff stamp cannot survive to this point. Tables with
    no usable temporal column return None and are treated as static.
    """
    cols = [c for c in df.columns
            if "time" in str(c).lower() or "date" in str(c).lower()]
    if not cols:
        return None
    parsed = pd.DataFrame(
        {c: pd.to_datetime(df[c], errors="coerce") for c in cols},
        index=df.index,
    )
    if bool(parsed.isna().all().all()):
        return None
    return parsed.max(axis=1)


def extract_instruction(question: Any) -> str | None:
    """The text inside `<task_instruction>...</task_instruction>`, or None."""
    m = TASK_INSTRUCTION_RE.search(str(question or ""))
    return m.group(1).strip() if m else None


def select_recent_topk(df: pd.DataFrame, top_k: int) -> tuple[pd.DataFrame, str]:
    """Most recent top_k rows, returned in source row order."""
    if top_k <= 0 or len(df) <= top_k:
        return df, "all_rows"
    key = row_recency(df)
    if key is None:
        # Static / timeless table: keep the tail, the source-order analogue of
        # "latest", and record that choice in the manifest.
        return df.tail(top_k), "static_tail"
    order = key.sort_values(ascending=False, kind="mergesort").index[:top_k]
    return df.loc[df.index.isin(order)], "recency_top_k"


def dump_view(
    tables: dict[str, pd.DataFrame],
    out_dir: Path,
    max_rows_per_table: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    info: dict[str, dict[str, Any]] = {}
    truncated = 0
    for name, df in sorted(tables.items()):
        if df is None or len(df) == 0:
            info[name] = {"rows": 0, "written": False}
            continue
        kept = df
        was_trunc = False
        if max_rows_per_table > 0 and len(df) > max_rows_per_table:
            key = row_recency(df)
            if key is not None:
                order = key.sort_values(
                    ascending=False, kind="mergesort"
                ).index[:max_rows_per_table]
                kept = df.loc[df.index.isin(order)]
            else:
                kept = df.tail(max_rows_per_table)
            was_trunc = True
            truncated += 1
        path = out_dir / f"{name}.csv"
        # date_format is pinned because pandas formats a datetime64 column
        # per-column: a slice whose values are all midnight would otherwise be
        # written date-only while the parent view writes full timestamps, so
        # the same row would serialise differently in recent/ and full/.
        kept.to_csv(path, index=False, date_format="%Y-%m-%d %H:%M:%S")
        info[name] = {
            "rows": int(len(kept)),
            "rows_before_cap": int(len(df)),
            "truncated": was_trunc,
            "columns": [str(c) for c in kept.columns],
            "file": path.name,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    return info, truncated


def prune_unlinked_image_members(cxr: pd.DataFrame | None,
                                 linked: set[str]) -> int:
    """Keep, in multi-image `tb_cxr` cells, only images the benchmark links.

    A study can hold several images, stored as pipe-joined members of
    `image_path` (aligned with `image_id` and `viewposition`). The benchmark
    packages only the images its questions link, so the other members point at
    files that do not exist in the benchmark package. They are dropped from all
    aligned fields, keeping member order. A cell is changed only when at least
    one, but not every, member is linked; single-image cells are never touched.
    Returns the number of rows changed.
    """
    if cxr is None or cxr.empty or "image_path" not in cxr.columns:
        return 0
    changed = 0
    present = availability_filter._present
    for index, value in cxr["image_path"].items():
        if not present(value):
            continue
        members = [m.strip() for m in str(value).split("|") if m.strip()]
        if len(members) < 2:
            continue
        keep = [k for k, member in enumerate(members) if member in linked]
        if not keep or len(keep) == len(members):
            continue
        for field in CXR_ALIGNED_FIELDS:
            if field not in cxr.columns or not present(cxr.at[index, field]):
                continue
            parts = [m.strip() for m in str(cxr.at[index, field]).split("|")
                     if m.strip()]
            if len(parts) == len(members):
                cxr.at[index, field] = "|".join(parts[k] for k in keep)
        changed += 1
    return changed


def load_row(manager: EHRManager, bench: str, row: dict[str, Any]) -> None:
    if bench == "text":
        manager.load_ehr_for_sample(
            str(row["subject_id"]),
            row["prediction_time"],
            task=row.get("task"),
            hadm_id=row.get("hadm_id"),
            target_hadm_id=row.get("target_hadm_id"),
            target_stay_id=row.get("target_stay_id"),
        )
    else:
        manager.load_ehr_for_sample(
            str(row["subject_id"]),
            row["prediction_time"],
            linked_study_ids=row.get("study_ids"),
            scope=row.get("scope"),
            task=row.get("task"),
            modalities=row.get("modalities"),
        )


def read_manifest(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{path} must hold a JSON list")
    return data


def row_id(row: dict[str, Any], position: int) -> str:
    return str(row.get("qid") or row.get("id") or f"row_{position}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build a ClinSeek-Bench evidence corpus (Curated Input + "
                    "Full-Record Input)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--bench", choices=["text", "mm"], required=True)
    p.add_argument("--manifest", type=Path, required=True,
                   help="benchmark manifest (JSON list or JSONL)")
    p.add_argument("--out", type=Path, required=True, help="corpus root to write")
    p.add_argument("--data-root", type=Path,
                   help="text: EHR-Bench root holding database/patient_<id>.db")
    p.add_argument("--ehrxqa-root", type=Path,
                   help="MM: EHRXQA source root (database/, mimic-cxr/)")
    p.add_argument("--medmod-root", type=Path,
                   help="MM: MedMod source root (database/, mimic-cxr/)")
    p.add_argument("--raw-mimic-root", type=Path,
                   help="MM: raw MIMIC-IV root with hosp/, icu/ and note/ "
                        "CSVs, used for storetime provenance")
    p.add_argument("--cache-dir", type=Path,
                   help="MM: cache for raw storetime scans (reused across runs)")
    p.add_argument("--policy-mode", choices=["aggressive", "faithful"],
                   default="aggressive",
                   help="aggressive (paper setting): additionally blank every "
                        "post-cutoff timestamp value; faithful: keep planned "
                        "times recorded before the cutoff")
    p.add_argument("--recent-mode", choices=["topk", "ehr_r1"], default=None,
                   help="how the Recent view is drawn from the filtered pool. "
                        "ehr_r1 = EHR-R1's original curated selection parsed "
                        "from the manifest `input` (text only); topk = "
                        "per-table most recent K rows. Default: ehr_r1 for "
                        "text, topk for mm.")
    p.add_argument("--recent-per-table", type=int, default=20,
                   help="K for --recent-mode topk")
    p.add_argument("--full-per-table", type=int, default=0,
                   help="Full view row cap per table; 0 = every visible row")
    p.add_argument("--no-availability-filter", dest="availability_filter",
                   action="store_false", default=True,
                   help="diagnostic only: skip the independent availability "
                        "filter")
    p.add_argument("--only-qids", type=Path, default=None,
                   help="build only these ids (one per line, or a JSON list)")
    p.add_argument("--limit", type=int, default=0,
                   help="build only the first N manifest rows")
    p.add_argument("--shard", type=str, default=None,
                   help="I/N: build rows whose manifest position %% N == I into "
                        "<out>/_shards/; combine with --merge-shards N")
    p.add_argument("--merge-shards", type=int, default=0, metavar="N",
                   help="merge N finished shards in <out>/_shards/ into the "
                        "corpus files, in manifest order")
    p.add_argument("--progress-every", type=int, default=50)
    args = p.parse_args()

    if args.recent_mode is None:
        args.recent_mode = "ehr_r1" if args.bench == "text" else "topk"
    if args.recent_mode == "ehr_r1" and args.bench != "text":
        p.error("--recent-mode ehr_r1 is text-only: there is no EHR-R1 "
                "curated input for the MM benchmark")
    if args.merge_shards:
        return args
    if args.bench == "text" and not args.data_root:
        p.error("--data-root is required for --bench text")
    if args.bench == "mm":
        missing = [flag for flag, value in (
            ("--ehrxqa-root", args.ehrxqa_root),
            ("--medmod-root", args.medmod_root),
            ("--raw-mimic-root", args.raw_mimic_root),
            ("--cache-dir", args.cache_dir)) if value is None]
        if missing and args.availability_filter:
            p.error(f"--bench mm requires {', '.join(missing)}")
        if args.ehrxqa_root is None or args.medmod_root is None:
            p.error("--bench mm requires --ehrxqa-root and --medmod-root")
    return args


def select_rows(args: argparse.Namespace,
                rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if args.limit:
        rows = rows[: args.limit]
    if args.only_qids:
        raw = args.only_qids.read_text(encoding="utf-8").strip()
        try:
            parsed = json.loads(raw)
            wanted = {str(x.get("qid") or x.get("id")) if isinstance(x, dict)
                      else str(x) for x in parsed}
        except json.JSONDecodeError:
            wanted = {line.strip() for line in raw.splitlines() if line.strip()}
        rows = [r for k, r in enumerate(rows, 1) if row_id(r, k) in wanted]
        print(f"--only-qids: {len(rows)} rows selected "
              f"({len(wanted)} ids requested)", flush=True)
        if not rows:
            raise SystemExit("--only-qids matched nothing")
    shard_tag = ""
    if args.shard:
        index, count = (int(x) for x in str(args.shard).split("/"))
        if not 0 <= index < count:
            raise SystemExit(f"--shard {args.shard}: index out of range")
        rows = [r for k, r in enumerate(rows) if k % count == index]
        shard_tag = f".part{index}"
        print(f"shard {index}/{count}: {len(rows)} rows", flush=True)
    return rows, shard_tag


def module_provenance() -> dict[str, Any]:
    """sha256 of the policy and renderer code actually imported."""
    out: dict[str, Any] = {}
    for name in ("agentlite.commons.temporal_policy",
                 "agentlite.commons.multimodal_temporal_policy",
                 "agentlite.commons.EHRManager",
                 "availability_filter", "policy_overrides",
                 "ehr_r1_selection", "question_render", "mm_context_render"):
        module = importlib.import_module(name)
        path = Path(module.__file__)
        out[name] = {
            "file": str(path.relative_to(REPO_ROOT))
            if path.is_relative_to(REPO_ROOT) else path.name,
            "sha256": sha256_file(path),
            "version": getattr(module, "POLICY_VERSION", None)
            or getattr(module, "RENDER_VERSION", None)
            or getattr(module, "SELECTION_VERSION", None)
            or getattr(module, "FILTER_VERSION", None),
        }
    return out


def write_corpus_files(out: Path, lines: list[str]) -> None:
    (out / "all_data.json").write_text(
        "[" + ",".join(lines) + "]\n", encoding="utf-8")
    (out / "all_data.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8")


def build(args: argparse.Namespace) -> int:
    warnings.filterwarnings("ignore")
    override = policy_overrides.apply_overrides(args.policy_mode)
    print(f"policy override: {override['override_version']}", flush=True)

    all_rows = read_manifest(args.manifest)
    rows, shard_tag = select_rows(args, all_rows)
    expected_policy = TEXT_POLICY if args.bench == "text" else MM_POLICY

    # MM: images the benchmark links, per source, over the WHOLE manifest (not
    # the shard), so a shard cannot see a different set.
    linked_images: dict[str, set[str]] = {s: set() for s in MM_SOURCES}
    source_roots: dict[str, Path] = {}
    managers: dict[str, EHRManager] = {}
    if args.bench == "text":
        managers["_"] = EHRManager(
            str(args.data_root), temporal_policy_mode="strict_mimic_iv")
    else:
        source_roots = {"ehrxqa": args.ehrxqa_root, "medmod": args.medmod_root}
        for key, root in source_roots.items():
            managers[key] = EHRManager(str(root))
        for row in all_rows:
            source = str(row.get("source_benchmark", "")).lower()
            for path in row.get("image_paths") or []:
                linked_images.setdefault(source, set()).add(
                    availability_filter._strip_package_prefix(str(path)))

    avail = None
    if args.availability_filter:
        avail = availability_filter.AvailabilityFilter(
            bench=args.bench,
            rows=rows,
            data_root=args.data_root if args.bench == "text" else None,
            source_roots=source_roots or None,
            cache_root=args.cache_dir if args.bench == "mm" else None,
            raw_mimic_root=args.raw_mimic_root if args.bench == "mm" else None,
        )
        print(f"availability filter: {availability_filter.FILTER_VERSION}",
              flush=True)

    provenance = module_provenance()

    ev_root = args.out / "evidence"
    ev_root.mkdir(parents=True, exist_ok=True)

    index_lines: list[str] = []
    failures: list[dict[str, Any]] = []
    stats: dict[str, int] = {
        "availability_filter_applied_cases": 0,
        "availability_filter_failed_cases": 0,
        "aggressive_values_swept": 0,
        "full_tables_truncated": 0,
        "recent_rows_total": 0,
        "full_rows_total": 0,
        "ehr_r1_selected_keys": 0,
        "ehr_r1_matched_keys": 0,
        "ehr_r1_rendered_rows": 0,
        "ehr_r1_recent_rows_emitted": 0,
        "cxr_rows_unlinked_images_pruned": 0,
        "rows_without_task_instruction": 0,
    }
    prompt_status: dict[str, int] = {}

    for i, row in enumerate(rows, 1):
        rid = row_id(row, i)
        bench_key = "_" if args.bench == "text" else str(
            row.get("source_benchmark", "")).lower()
        manager = managers.get(bench_key)
        if manager is None:
            failures.append({"id": rid, "error": f"no root for {bench_key!r}"})
            continue
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                load_row(manager, args.bench, row)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            failures.append({"id": rid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if manager.temporal_policy_version != expected_policy:
            failures.append({
                "id": rid,
                "error": f"policy {manager.temporal_policy_version!r} "
                         f"!= expected {expected_policy!r}",
            })
            continue

        # ---- 1: the temporal runtime policy view ---------------------------
        clean = manager.ehr_data or {}
        eff_cutoff = manager.last_prediction_time

        # ---- 2: independent availability filter ---------------------------
        # Only removes rows whose information became readable after the cutoff
        # and ids naming encounters that had not started. Label-blind.
        avail_report: dict = {"applied": False}
        report_paths = row.get("report_paths")
        if avail is not None:
            try:
                result = avail.apply(clean, row=row, policy_cutoff=eff_cutoff)
                clean = dict(result.tables)
                avail_report = dict(result.report or {})
                avail_report["applied"] = True
                avail_report["filter_version"] = availability_filter.FILTER_VERSION
                if result.report_paths is not None:
                    report_paths = list(result.report_paths)
                if result.cutoff is not None:
                    eff_cutoff = result.cutoff
                stats["availability_filter_applied_cases"] += 1
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                avail_report = {"applied": False,
                                "error": f"{type(exc).__name__}: {exc}"}
                stats["availability_filter_failed_cases"] += 1

        # ---- 3: aggressive mode masks by VALUE, not by column name ----------
        if args.policy_mode == "aggressive":
            swept = policy_overrides.sweep_future_values(clean, eff_cutoff)
            if swept:
                avail_report.setdefault("aggressive_value_sweep", {}).update(swept)
                stats["aggressive_values_swept"] += sum(swept.values())

        # ---- 4 (MM): drop unpackaged images from multi-image cells ---------
        if args.bench == "mm":
            stats["cxr_rows_unlinked_images_pruned"] += \
                prune_unlinked_image_members(
                    clean.get("tb_cxr"), linked_images.get(bench_key, set()))

        # `clean` is now the FILTERED POOL. Both views come from it.
        case_dir = ev_root / rid
        full_info, truncated = dump_view(clean, case_dir / "full",
                                         args.full_per_table)
        stats["full_tables_truncated"] += truncated

        recent_tables: dict[str, pd.DataFrame] = {}
        recent_rules: dict[str, str] = {}
        sel_report: dict[str, Any] | None = None
        if args.recent_mode == "ehr_r1":
            blocks = ehr_r1_selection.parse_selection_intervals(row.get("input"))
            recent_tables, sel_report = ehr_r1_selection.select_recent(
                clean, blocks, cutoff=eff_cutoff)
            stats["ehr_r1_selected_keys"] += sel_report["selected_keys"]
            stats["ehr_r1_matched_keys"] += sel_report["matched_keys"]
            stats["ehr_r1_recent_rows_emitted"] += sel_report["rows_emitted"]
            stats["ehr_r1_rendered_rows"] += sel_report["rendered_rows"]
        else:
            for name, df in clean.items():
                if df is None or len(df) == 0:
                    continue
                sel, rule = select_recent_topk(df, args.recent_per_table)
                recent_tables[name] = sel
                recent_rules[name] = rule
        recent_info, _ = dump_view(recent_tables, case_dir / "recent", 0)
        stats["recent_rows_total"] += sum(int(v.get("rows", 0))
                                          for v in recent_info.values())
        stats["full_rows_total"] += sum(int(v.get("rows", 0))
                                        for v in full_info.values())

        # ---- 5: prompts ------------------------------------------------------
        last_event_ts: Any = None
        q_status = "not_applicable"
        if args.bench == "text":
            _, last_event_ts = question_render.restated_times(clean, eff_cutoff)
            # The benchmark's prediction_time and latest_event_time are kept as
            # stated; passing the same strings as old and new makes the time
            # restatement inside rebuild_question an exact identity.
            question_text, q_status = question_render.rebuild_question(
                row.get("question"),
                row.get("input"),
                question_render.render_timeline(recent_tables),
                old_prediction_time=row.get("prediction_time"),
                old_latest_event_time=row.get("latest_event_time"),
                new_prediction_time=row.get("prediction_time"),
                new_latest_event_time=row.get("latest_event_time"),
            )
            prompt_status[q_status] = prompt_status.get(q_status, 0) + 1
            if question_text is None:
                # Falling back to the source `question` would ship EHR-R1's
                # unfiltered timeline. Record the failure instead.
                failures.append({"id": rid,
                                 "error": f"question rebuild failed: {q_status}"})
                continue
        else:
            count = len(report_paths or [])
            question_text = REPORT_COUNT_RE.sub(
                f"Linked Report Count: {count}", str(row.get("question") or ""))
            template = REPORT_COUNT_RE.sub(
                f"Linked Report Count: {count}", str(row.get("input_text") or ""))
            # "Rows visible before cutoff" is the Full view's count in both.
            visible = {k: int(v.get("rows", 0)) for k, v in full_info.items()}
            input_text_full, st_full = mm_context_render.rebuild_input_text(
                template, case_dir / "full", visible)
            input_text_recent, st_recent = mm_context_render.rebuild_input_text(
                template, case_dir / "recent", visible)
            q_status = st_full if st_full == st_recent else f"{st_full}/{st_recent}"
            prompt_status[q_status] = prompt_status.get(q_status, 0) + 1
            if input_text_full is None or input_text_recent is None:
                failures.append({"id": rid,
                                 "error": f"input_text rebuild failed: {q_status}"})
                continue

        case_manifest = {
            "id": rid,
            "benchmark": args.bench,
            "source_benchmark": row.get("source_benchmark"),
            "policy_version": manager.temporal_policy_version,
            # The cutoff actually enforced (the availability filter clamps the
            # policy cutoff to the manifest prediction_time).
            "cutoff": str(avail_report.get("effective_cutoff")
                          or manager.last_prediction_time),
            "policy_cutoff": manager.last_prediction_time,
            "prediction_time": row.get("prediction_time"),
            "latest_visible_event": (None if last_event_ts is None
                                     else str(last_event_ts)),
            "prompt_render": {
                "status": q_status,
                "render_version": (question_render.RENDER_VERSION
                                   if args.bench == "text"
                                   else mm_context_render.RENDER_VERSION),
            },
            # Scope fields copied out flat for audit_evidence_corpus.py.
            "task": row.get("task"),
            "scope": row.get("scope"),
            "study_ids": row.get("study_ids"),
            "modalities": row.get("modalities"),
            "recent_mode": args.recent_mode,
            "recent_rule": (
                {
                    "kind": "ehr_r1_original_selection",
                    "selection_version": ehr_r1_selection.SELECTION_VERSION,
                    "source": "manifest `input` field, parsed as "
                              "`## <Section> [<timestamp>]` blocks",
                    "match": "interval: block i of a table covers "
                             "[t_i, t_i+1), last block runs to the cutoff",
                    "intersected_with": "filtered pool",
                    "cutoff_used": str(eff_cutoff),
                }
                if args.recent_mode == "ehr_r1" else
                {
                    "kind": "per_table_top_k_by_recency",
                    "top_k": args.recent_per_table,
                    "order_key": "row max parseable timestamp, descending",
                    "per_table": recent_rules,
                }
            ),
            "ehr_r1_selection": sel_report,
            "full_rule": {
                "kind": ("all_visible_rows" if args.full_per_table <= 0
                         else "per_table_cap_by_recency"),
                "cap": args.full_per_table,
            },
            "temporal_report": manager.last_temporal_report,
            "policy_override": policy_overrides.override_report(),
            "availability_filter": avail_report,
            "tables": {"full": full_info, "recent": recent_info},
        }
        (case_dir / "_manifest.json").write_text(
            json.dumps(case_manifest, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        # ---- 6: the record ---------------------------------------------------
        if args.bench == "mm":
            row = dict(row)
            row["report_paths"] = list(report_paths or [])
            row["question"] = question_text
            row["input_text"] = input_text_full
        qkeys = TEXT_QUERY_KEYS if args.bench == "text" else MM_QUERY_KEYS
        query = {k: row[k] for k in qkeys if k in row}
        if args.bench == "mm":
            instruction = extract_instruction(row.get("question"))
            if instruction is None:
                stats["rows_without_task_instruction"] += 1
            else:
                query["instruction"] = instruction
            query["input_text_recent"] = input_text_recent
            query["input_text_view"] = "full"
        else:
            query["prediction_time"] = str(row.get("prediction_time"))
            query["requested_prediction_time"] = row.get("prediction_time")
            query["latest_visible_event"] = (None if last_event_ts is None
                                             else str(last_event_ts))
            query["question"] = question_text
            query["question_view"] = "recent"

        # Runners read the query fields at the top level and identify a record
        # by `qid`; `query` keeps the same fields nested for provenance (record
        # loaders drop it).
        record: dict[str, Any] = {"qid": rid, "id": rid}
        record.update(query)
        record["query"] = query
        record["recent_evidence_list"] = str(
            (case_dir / "recent").relative_to(args.out))
        record["full_evidence_list"] = str(
            (case_dir / "full").relative_to(args.out))
        record["policy_version"] = manager.temporal_policy_version
        record["ground_truth"] = row.get("label", row.get("ground_truth"))
        # The scorers read `label`; it is the same object as `ground_truth`.
        record["label"] = record["ground_truth"]
        if args.bench == "mm":
            # The MM curated runner refuses a pre-rendered `input_text` that
            # does not carry the strict-policy marker.
            query["leakage_policy_version"] = manager.temporal_policy_version
            record["leakage_policy_version"] = manager.temporal_policy_version
        # ensure_ascii=True: clinical note text contains raw U+0085 / U+2028 /
        # U+2029, which str.splitlines() -- used by line-oriented loaders --
        # treats as line breaks. Escaping every non-ASCII codepoint keeps one
        # record per line.
        index_lines.append(json.dumps(record, ensure_ascii=True, default=str))

        if args.progress_every and i % args.progress_every == 0:
            print(f"  {i}/{len(rows)}  ok={len(index_lines)}  "
                  f"fail={len(failures)}", flush=True)

    corpus = {
        "benchmark": args.bench,
        "source_manifest": args.manifest.name,
        "policy_version": expected_policy,
        "policy_mode": args.policy_mode,
        "policy_override": policy_overrides.override_report(),
        "availability_filter_enabled": bool(args.availability_filter),
        "availability_filter_version": availability_filter.FILTER_VERSION,
        "availability_preparation": (avail.preparation_report
                                     if avail is not None else None),
        "records_in": len(rows),
        "records_built": len(index_lines),
        "records_failed": len(failures),
        "recent_mode": args.recent_mode,
        "recent_per_table": (args.recent_per_table
                             if args.recent_mode == "topk" else None),
        "full_per_table_cap": args.full_per_table,
        "ehr_r1_selection_version": (ehr_r1_selection.SELECTION_VERSION
                                     if args.recent_mode == "ehr_r1" else None),
        # `ehr_r1_recent_rows_emitted` / `ehr_r1_rendered_rows` are parent-table
        # rows only (the selection retention); `recent_rows_total` also counts
        # child-detail rows that follow a selected parent.
        **stats,
        "prompt_render_status": prompt_status,
        "prompt_render_version": (question_render.RENDER_VERSION
                                  if args.bench == "text"
                                  else mm_context_render.RENDER_VERSION),
        "module_provenance": provenance,
        "failures": failures[:200],
    }

    if shard_tag:
        shard_dir = args.out / "_shards"
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"all_data{shard_tag}.jsonl").write_text(
            "".join(line + "\n" for line in index_lines), encoding="utf-8")
        (shard_dir / f"_corpus_manifest{shard_tag}.json").write_text(
            json.dumps(corpus, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
    else:
        write_corpus_files(args.out, index_lines)
        (args.out / "_corpus_manifest.json").write_text(
            json.dumps(corpus, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
    summary = {k: v for k, v in corpus.items()
               if k not in ("failures", "module_provenance",
                            "availability_preparation", "policy_override")}
    print(json.dumps(summary, indent=2, default=str))
    if failures:
        print(f"\n{len(failures)} failures, first 5:")
        for failure in failures[:5]:
            print("  ", failure)
    return 0 if not failures else 1


def merge_shards(args: argparse.Namespace) -> int:
    """Combine `--shard I/N` outputs into the corpus files, in manifest order."""
    shard_dir = args.out / "_shards"
    parts = [shard_dir / f"all_data.part{i}.jsonl"
             for i in range(args.merge_shards)]
    manifests = [shard_dir / f"_corpus_manifest.part{i}.json"
                 for i in range(args.merge_shards)]
    missing = [str(p) for p in parts + manifests if not p.exists()]
    if missing:
        raise SystemExit(f"missing shard outputs: {missing}")
    by_qid: dict[str, str] = {}
    for part in parts:
        for line in part.read_text(encoding="utf-8").splitlines():
            if line.strip():
                by_qid[json.loads(line)["qid"]] = line
    order = [row_id(r, k) for k, r in enumerate(read_manifest(args.manifest), 1)]
    lines = [by_qid[q] for q in order if q in by_qid]
    if len(lines) != len(by_qid):
        raise SystemExit("shard records not found in the manifest")
    shard_reports = [json.loads(p.read_text(encoding="utf-8")) for p in manifests]
    corpus = dict(shard_reports[0])
    for key, value in shard_reports[0].items():
        if isinstance(value, int) and not isinstance(value, bool):
            corpus[key] = sum(int(r.get(key) or 0) for r in shard_reports)
        elif isinstance(value, dict) and key in ("prompt_render_status",):
            merged: dict[str, int] = {}
            for report in shard_reports:
                for k, v in (report.get(key) or {}).items():
                    merged[k] = merged.get(k, 0) + v
            corpus[key] = merged
    corpus["failures"] = [f for r in shard_reports for f in r.get("failures", [])]
    corpus["availability_preparation"] = [r.get("availability_preparation")
                                          for r in shard_reports]
    corpus["shards_merged"] = args.merge_shards
    write_corpus_files(args.out, lines)
    (args.out / "_corpus_manifest.json").write_text(
        json.dumps(corpus, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")
    for path in parts + manifests:
        path.unlink()
    try:
        shard_dir.rmdir()
    except OSError:
        pass
    print(f"merged {args.merge_shards} shards: {len(lines)} records, "
          f"{len(corpus['failures'])} failures")
    return 0 if not corpus["failures"] else 1


def main() -> int:
    args = parse_args()
    if args.merge_shards:
        return merge_shards(args)
    return build(args)


if __name__ == "__main__":
    raise SystemExit(main())
