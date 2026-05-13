"""Stage external Letian2003/fh37931 results into our canonical result dirs.

The HF dump at /fsx-shared/juncheng/EHR/data/external_results/fh37931/upload/
has four families of run directories:

  * qa_ehr_bench_sampled_40_per_task_<model>   one-shot QA EHR-bench (1800 rows)
  * ehrbench_1800_<model>                      agentic EHR-bench (1800 rows)
  * mm_prepared_<model>                        agentic MM-bench (scored.jsonl)
  * mm_qa_model_ready_combined_test_set_<model> one-shot MM-bench (scored.jsonl)
  * subset_500_* / subsets_600_* / ...         legacy subsets; skip

For each recognized run dir we:
  1. pick a canonical model slug (e.g. qwen3_5_35b_a3b, gemma4_26b, ehr_r1_72b, ...)
  2. stage it under results/ehr_bench/{paradigm}/full1800/<slug>/ or
     results/mm_bench/{paradigm}/scored/<slug>/
  3. for the qa_* EHR-bench runs, normalize the `ground_truth` -> `label` field
     so helper/evaluate_results.py can score it

The script writes SYMLINKS (or tiny wrapper files) into the canonical tree so
we don't duplicate 256 MB of jsonl. The reorganize_results.py script then
picks them up automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

SRC_ROOT = Path("/fsx-shared/juncheng/EHR/data/external_results/fh37931/upload")
RESULTS_ROOT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/results")


# slug mapping: external dir name -> (paradigm, bench, canonical model slug)
MAPPING: Dict[str, Tuple[str, str, str]] = {
    # EHR-bench agentic
    "ehrbench_1800_qwen3_5_35b_a3b":       ("agentic", "ehr_bench", "qwen3_5_35b_a3b"),
    "ehrbench_1800_gemma_4_26b_a4b_it":    ("agentic", "ehr_bench", "gemma4_26b_a4b_it"),
    "ehrbench_1800_tongyi_deepresearch":   ("agentic", "ehr_bench", "tongyi_deepresearch_30b_a3b"),
    # EHR-bench one-shot (QA)
    "qa_ehr_bench_sampled_40_per_task_ehr_r1_72b":          ("oneshot", "ehr_bench", "ehr_r1_72b"),
    "qa_ehr_bench_sampled_40_per_task_ehr_r1_8b":           ("oneshot", "ehr_bench", "ehr_r1_8b"),
    "qa_ehr_bench_sampled_40_per_task_gemma_4_26b_a4b_it":  ("oneshot", "ehr_bench", "gemma4_26b_a4b_it"),
    "qa_ehr_bench_sampled_40_per_task_medgemma_27b_it":     ("oneshot", "ehr_bench", "medgemma_27b_it"),
    "qa_ehr_bench_sampled_40_per_task_qwen3_5_35b_a3b":     ("oneshot", "ehr_bench", "qwen3_5_35b_a3b"),
    "qa_ehr_bench_sampled_40_per_task_tongyi_deepresearch_30b_a3b":
        ("oneshot", "ehr_bench", "tongyi_deepresearch_30b_a3b"),
    # MM-bench agentic
    "mm_prepared_gemma4_thinking":                                    ("agentic", "mm_bench", "gemma4_26b_a4b_it"),
    "mm_prepared_Qwen3.5-35B-A3B_thinking_20260428T231731Z":           ("agentic", "mm_bench", "qwen3_5_35b_a3b"),
    # MM-bench one-shot
    "mm_qa_model_ready_combined_test_set_gemma_4_26b_a4b_it": ("oneshot", "mm_bench", "gemma4_26b_a4b_it"),
    "mm_qa_model_ready_combined_test_set_medgemma_27b_it":    ("oneshot", "mm_bench", "medgemma_27b_it"),
    "mm_qa_model_ready_combined_test_set_qwen3_5_35b_a3b":    ("oneshot", "mm_bench", "qwen3_5_35b_a3b"),
    # legacy subsets — skip
}


def _normalize_qa_row(r: Dict) -> Dict:
    """Rename `ground_truth` -> `label` for qa_ehr_bench runs; ensure float str preds."""
    if "label" not in r and "ground_truth" in r:
        r["label"] = r["ground_truth"]
    return r


def _safe_parse_jsonl_line(line: str) -> List[Dict] | None:
    """Parse one physical line. Handles two concatenation pathologies:
      (a) rows joined at `}{` — use raw_decode to walk them;
      (b) rows joined mid-field where the first row is truncated and the
          second row's `{"qid":...` is spliced in — recover the second row.
    """
    try:
        return [json.loads(line)]
    except Exception:
        pass

    # Strategy A: raw-decode walk
    dec = json.JSONDecoder()
    out: List[Dict] = []
    idx = 0
    n = len(line)
    while idx < n:
        while idx < n and line[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = dec.raw_decode(line, idx)
            out.append(obj)
            idx = end
        except Exception:
            break
    if out:
        return out

    # Strategy B: find later `{"qid":` and parse from there
    import re as _re
    for m in _re.finditer(r'\{"qid":', line):
        start = m.start()
        if start == 0:
            continue
        try:
            obj, _ = dec.raw_decode(line, start)
            return [obj]
        except Exception:
            continue
    return None


def ingest_ehr_bench(src_dir: Path, paradigm: str, model: str,
                     is_qa: bool, dry_run: bool) -> None:
    dst_dir = RESULTS_ROOT / "ehr_bench" / paradigm / "full1800" / model
    if dst_dir.exists():
        print(f"  SKIP: {dst_dir} already exists")
        return
    if dry_run:
        print(f"  DRY: would stage {src_dir.name} -> {dst_dir}")
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_results = src_dir / "results.jsonl"
    dst_results = dst_dir / "results.jsonl"

    if is_qa:
        # normalize ground_truth -> label; recover from concatenated rows.
        bad = 0
        recovered = 0
        with src_results.open() as f_in, dst_results.open("w") as f_out:
            for lineno, line in enumerate(f_in, 1):
                line = line.strip()
                if not line:
                    continue
                rows = _safe_parse_jsonl_line(line)
                if rows is None:
                    bad += 1
                    if bad <= 3:
                        print(f"    skip bad line {lineno}")
                    continue
                if len(rows) > 1:
                    recovered += len(rows) - 1
                for r in rows:
                    r = _normalize_qa_row(r)
                    f_out.write(json.dumps(r) + "\n")
        if bad or recovered:
            print(f"    bad={bad} recovered={recovered}")
    else:
        # shape already matches deploy_agent.py output — symlink
        os.symlink(src_results, dst_results)

    # Copy summary.json if it exists (rename to scores.json for compatibility)
    for name in ("summary.json", "summary.txt"):
        if (src_dir / name).exists():
            shutil.copy2(src_dir / name, dst_dir / name)
    print(f"  OK:  {src_dir.name} -> {dst_dir}")


def ingest_mm_bench(src_dir: Path, paradigm: str, model: str, dry_run: bool) -> None:
    dst_dir = RESULTS_ROOT / "mm_bench" / paradigm / "scored" / model
    if dst_dir.exists():
        print(f"  SKIP: {dst_dir} already exists")
        return
    if dry_run:
        print(f"  DRY: would stage {src_dir.name} -> {dst_dir}")
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_scored = src_dir / "scored.jsonl"
    dst_scored = dst_dir / "scored.jsonl"
    os.symlink(src_scored, dst_scored)
    for name in ("summary.json", "summary.md"):
        if (src_dir / name).exists():
            shutil.copy2(src_dir / name, dst_dir / name)
    print(f"  OK:  {src_dir.name} -> {dst_dir}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not SRC_ROOT.exists():
        print(f"ERROR: {SRC_ROOT} not found", file=sys.stderr)
        return 2

    # Skip any external dir not in MAPPING (subsets etc.)
    all_dirs = sorted(p.name for p in SRC_ROOT.iterdir() if p.is_dir())
    print(f"External dirs: {len(all_dirs)}")
    for d in all_dirs:
        if d not in MAPPING:
            print(f"  IGNORE (legacy): {d}")

    for name, (paradigm, bench, model) in MAPPING.items():
        src = SRC_ROOT / name
        if not src.exists():
            print(f"  MISS: {name} not in src tree")
            continue
        if bench == "ehr_bench":
            is_qa = name.startswith("qa_ehr_bench_")
            ingest_ehr_bench(src, paradigm, model, is_qa, args.dry_run)
        else:
            ingest_mm_bench(src, paradigm, model, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
