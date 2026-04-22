"""Multi-model, multi-region Bedrock evaluation driver.

Reads `bedrock_model_region_availability.json` to find every OK region for
each requested model, partitions the input JSONL round-robin across those
regions (one shard per region, per model), spawns a `deploy_agent_mm.py`
subprocess per shard, tees logs, and aggregates a summary.

The driver does NOT import `bedrock_generator` or `deploy_agent_mm`; it
shells out. Existing single-model `run_mm_pipeline.sh` is untouched.

Typical usage:
    # Dry run — print commands + partition sizes only
    python run_multi_model_eval.py \
        --models "Claude Sonnet 4.6" "MiniMax M2.5" "GLM-4.7" \
        --data /fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/combined_test_set_nonempty.jsonl \
        --mode smoke --dry-run

    # Smoke: 10 samples per shard; assumes MCPs already up on :5103/5104/5203
    python run_multi_model_eval.py \
        --models "Claude Sonnet 4.6" "Qwen3-VL-235B" \
        --data .../combined_test_set_nonempty.jsonl \
        --mode smoke --no-start-mcp

    # Full: partition the 2,703-row test set across each model's regions
    python run_multi_model_eval.py \
        --models "Claude Sonnet 4.6" "Qwen3-VL-235B" "MiniMax M2.5" "GLM-4.7" \
        --data .../combined_test_set_nonempty.jsonl \
        --mode full --max-parallel-shards 6
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CATALOG_PATH = SCRIPT_DIR / "bedrock_model_region_availability.json"
DEFAULT_PYBIN = os.environ.get(
    "PYBIN", "/fsx-shared/juncheng/OpenResearcher/.venv/bin/python"
)
DEFAULT_BENCH_ROOT = (
    "/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3:"
    "/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3"
)
DEFAULT_EHR_EHRXQA = "http://127.0.0.1:5103/mcp"
DEFAULT_EHR_MEDMOD = "http://127.0.0.1:5104/mcp"
DEFAULT_IMAGE_MCP = "http://127.0.0.1:5203/mcp"


def _slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return s or "model"


def load_catalog(path: Path = CATALOG_PATH) -> Dict[str, List[Dict[str, str]]]:
    """Return the augmented region-availability JSON, keyed by friendly name."""
    if not path.exists():
        raise FileNotFoundError(f"catalog not found at {path}")
    return json.loads(path.read_text())


def ok_entries(entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [e for e in entries if isinstance(e, dict) and e.get("status") == "OK"]


def regions_for(entries: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """Return sorted list of (region, model_id) tuples for OK rows.

    If a region appears multiple times (rare), the first occurrence wins.
    """
    seen: Dict[str, str] = {}
    for e in ok_entries(entries):
        region = e["region"]
        model_id = e["model_id"]
        seen.setdefault(region, model_id)
    return sorted(seen.items())


@dataclass
class Shard:
    friendly_name: str
    model_tag: str            # slugified friendly_name
    region: str
    model_id: str
    input_path: Path
    shard_dir: Path
    rows: int                 # number of rows in the shard input
    cmd: List[str] = field(default_factory=list)
    pid: Optional[int] = None
    returncode: Optional[int] = None
    steal_runs: int = 0       # how many steal subprocesses this shard has served as HELPER


# Serializes work-stealing decisions across threads so two fast shards don't
# both try to drain the same donor simultaneously.
_STEAL_LOCK = threading.Lock()


def read_jsonl(path: Path) -> List[str]:
    """Return raw lines (with trailing newline) — avoids reparsing on write."""
    with path.open() as f:
        return [line for line in f if line.strip()]


def partition_rows(
    rows: List[str], regions: List[Tuple[str, str]], smoke_size: Optional[int]
) -> Dict[str, List[str]]:
    """Round-robin rows across regions; optionally truncate each shard to smoke_size."""
    shards: Dict[str, List[str]] = {r: [] for r, _ in regions}
    region_list = [r for r, _ in regions]
    K = len(region_list)
    for i, row in enumerate(rows):
        shards[region_list[i % K]].append(row)
    if smoke_size and smoke_size > 0:
        for r in shards:
            shards[r] = shards[r][:smoke_size]
    return shards


def build_shards(
    *,
    friendly_names: List[str],
    catalog: Dict[str, List[Dict[str, str]]],
    rows: List[str],
    output_root: Path,
    smoke_size: Optional[int],
) -> List[Shard]:
    shards: List[Shard] = []
    for name in friendly_names:
        if name not in catalog:
            raise SystemExit(
                f"[ERROR] Unknown friendly name {name!r}. "
                f"Available: {sorted(catalog.keys())}"
            )
        regions = regions_for(catalog[name])
        if not regions:
            raise SystemExit(f"[ERROR] No OK regions for {name!r}")
        partitions = partition_rows(rows, regions, smoke_size)
        model_tag = _slugify(name)
        for region, model_id in regions:
            shard_rows = partitions[region]
            shard_dir = output_root / model_tag / region
            shard_dir.mkdir(parents=True, exist_ok=True)
            input_path = shard_dir / "input.jsonl"
            input_path.write_text("".join(shard_rows))
            shards.append(
                Shard(
                    friendly_name=name,
                    model_tag=model_tag,
                    region=region,
                    model_id=model_id,
                    input_path=input_path,
                    shard_dir=shard_dir,
                    rows=len(shard_rows),
                )
            )
    return shards


def build_cmd(shard: Shard, args: argparse.Namespace, pybin: str) -> List[str]:
    cmd = [
        pybin,
        str(SCRIPT_DIR / "deploy_agent_mm.py"),
        "--data_path", str(shard.input_path),
        "--output_dir", str(shard.shard_dir),
        "--bedrock_model_id", shard.model_id,
        "--bedrock_region", shard.region,
        "--bench_root", args.bench_root,
        "--max_rounds", str(args.max_rounds),
        "--max_concurrency", str(args.concurrency),
        "--runs_per_question", str(args.runs_per_question),
        "--max_tool_result_chars", str(args.max_tool_result_chars),
        "--image_max_edge", str(args.image_max_edge),
        "--verbose",
    ]
    if args.enable_ehr:
        cmd += [
            "--enable_ehr",
            "--ehr_mcp_url", args.ehr_mcp_url_ehrxqa,
            "--ehr_mcp_url_ehrxqa", args.ehr_mcp_url_ehrxqa,
            "--ehr_mcp_url_medmod", args.ehr_mcp_url_medmod,
        ]
    if args.enable_image:
        cmd += ["--enable_image", "--image_mcp_url", args.image_mcp_url]
    if args.enable_thinking:
        cmd.append("--enable_thinking")
    else:
        cmd.append("--disable_thinking")
    return cmd


def run_shard(shard: Shard, cmd: List[str], env: Dict[str, str]) -> int:
    """Spawn the subprocess, tee stdout/stderr to run.log, write status.json."""
    shard.cmd = cmd
    (shard.shard_dir / "cmd.txt").write_text(shlex.join(cmd) + "\n")
    log_path = shard.shard_dir / "run.log"
    with log_path.open("w") as log_f:
        p = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(SCRIPT_DIR),
        )
        shard.pid = p.pid
        (shard.shard_dir / "pid.txt").write_text(f"{p.pid}\n")
        rc = p.wait()
    shard.returncode = rc
    status = {
        "friendly_name": shard.friendly_name,
        "model_tag": shard.model_tag,
        "region": shard.region,
        "model_id": shard.model_id,
        "input_rows": shard.rows,
        "pid": shard.pid,
        "returncode": rc,
        "results_rows": _count_lines(shard.shard_dir / "results.jsonl"),
    }
    (shard.shard_dir / "status.json").write_text(json.dumps(status, indent=2))
    return rc


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for line in f if line.strip())


def _donor_progress(donor: Shard) -> Tuple[int, int, List[str]]:
    """Return (results_count, input_count, input_lines) for a running donor.

    Used to decide whether any unattempted tail exists and to slice it out.
    """
    results = _count_lines(donor.shard_dir / "results.jsonl")
    with donor.input_path.open() as f:
        input_lines = [line for line in f if line.strip()]
    return results, len(input_lines), input_lines


def try_steal_work(
    helper: Shard,
    all_shards: List[Shard],
    args: argparse.Namespace,
    env: Dict[str, str],
    steal_safety_gap: int,
    min_tail: int,
) -> Optional[int]:
    """If there is a slow sibling of the helper's model with un-started rows,
    spawn a second `deploy_agent_mm.py` on `helper`'s region/model to drain
    the tail. Returns the new subprocess's returncode (or None if no steal).

    Safety:
    - Only steals rows at index >= (donor.results_count + steal_safety_gap);
      `steal_safety_gap = per-shard concurrency`, i.e. the rows possibly
      in-flight when we snapshot. Duplicates from any race are resolved at
      aggregate time by qid.
    - Holds `_STEAL_LOCK` so two fast helpers don't both steal from the same
      donor.
    - Never steals from a shard of a different model.
    """
    model = helper.friendly_name
    with _STEAL_LOCK:
        # Candidate donors: same model, still running, with long un-started tail.
        best_donor: Optional[Shard] = None
        best_tail: List[str] = []
        best_start_idx: int = 0
        for donor in all_shards:
            if donor.friendly_name != model:
                continue
            if donor is helper:
                continue
            if donor.returncode is not None:
                continue
            results_n, input_n, input_lines = _donor_progress(donor)
            # Leave the donor's in-flight rows alone.
            start_idx = results_n + steal_safety_gap
            tail = input_lines[start_idx:]
            if len(tail) >= min_tail and len(tail) > len(best_tail):
                best_donor = donor
                best_tail = tail
                best_start_idx = start_idx
        if best_donor is None:
            return None

        # Snapshot by writing the tail into the helper's dir; no mutation to
        # the donor's files (its own agent is still reading its input.jsonl).
        helper.steal_runs += 1
        run_id = helper.steal_runs
        steal_input = helper.shard_dir / f"input_steal_{run_id:02d}.jsonl"
        steal_input.write_text("".join(best_tail))
        steal_output = helper.shard_dir  # results_steal_NN.jsonl lives here
        donor_tag = f"{best_donor.model_tag}/{best_donor.region}"
        print(
            f"[driver] steal: {helper.model_tag}/{helper.region} helping "
            f"{donor_tag} — {len(best_tail)} rows (idx {best_start_idx}+), "
            f"run #{run_id}",
            flush=True,
        )

    # Build a modified command: different --data_path / --output_dir so the
    # helper's original results.jsonl isn't clobbered. deploy_agent_mm.py
    # writes to {output_dir}/results.jsonl so we point it at a per-steal subdir.
    steal_subdir = helper.shard_dir / f"steal_{run_id:02d}"
    steal_subdir.mkdir(exist_ok=True)
    steal_cmd = list(helper.cmd)
    # Replace --data_path and --output_dir args
    for i in range(len(steal_cmd) - 1):
        if steal_cmd[i] == "--data_path":
            steal_cmd[i + 1] = str(steal_input)
        elif steal_cmd[i] == "--output_dir":
            steal_cmd[i + 1] = str(steal_subdir)
    log_path = helper.shard_dir / f"run_steal_{run_id:02d}.log"
    (helper.shard_dir / f"cmd_steal_{run_id:02d}.txt").write_text(
        shlex.join(steal_cmd) + "\n"
    )
    with log_path.open("w") as log_f:
        p = subprocess.Popen(
            steal_cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(SCRIPT_DIR),
        )
        rc = p.wait()
    # Attribute the stolen results back into the helper's directory under a
    # stable name, so aggregate() can discover them.
    produced = steal_subdir / "results.jsonl"
    if produced.exists():
        final = helper.shard_dir / f"results_steal_{run_id:02d}.jsonl"
        final.write_bytes(produced.read_bytes())
    print(
        f"[driver] steal done: {helper.model_tag}/{helper.region} "
        f"(donor={donor_tag}, run #{run_id}, rc={rc}, "
        f"rows={_count_lines(steal_subdir / 'results.jsonl')})",
        flush=True,
    )
    return rc


def poll_loop(shards: List[Shard], stop_event: threading.Event) -> None:
    """Periodically print a compact progress table to stderr."""
    while not stop_event.wait(30):
        snapshot = []
        for s in shards:
            done = _count_lines(s.shard_dir / "results.jsonl")
            snapshot.append((s.model_tag, s.region, s.rows, done, s.returncode))
        snapshot.sort()
        now = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        print(f"\n=== poll {now} ===", file=sys.stderr)
        for tag, region, total, done, rc in snapshot:
            rc_str = "alive" if rc is None else f"rc={rc}"
            print(
                f"  {tag:28} {region:16} rows={done:>4}/{total:<4} {rc_str}",
                file=sys.stderr,
            )


def aggregate(shards: List[Shard], output_root: Path) -> Dict:
    """Aggregate per-shard and per-model stats with **cross-shard dedup**.

    Work-stealing means one qid can be scored in multiple regions of the same
    model. We credit each qid to the **first region that wrote it** (primary
    shard wins over a helper steal). Salvage/name_fix counters are summed
    across all logs within a model to avoid under-counting.

    Per-shard row counts reflect UNIQUE qids attributed to that shard. Across
    all shards of a model the sum equals len(unique qids for that model),
    which equals the input size when coverage is complete.
    """
    # Pass 1 — per model, walk every shard's primary + stolen files in
    # stable order (primary first; shards sorted by region) and attribute
    # each qid to whichever (shard, origin_file) reports it first.
    model_to_shards: Dict[str, List[Shard]] = {}
    for s in shards:
        model_to_shards.setdefault(s.friendly_name, []).append(s)
    # Stable ordering inside each model bucket
    for name in model_to_shards:
        model_to_shards[name].sort(key=lambda s: s.region)

    # shard_id -> Counter of statuses, and shard_id -> dedup row count.
    shard_key = lambda s: (s.model_tag, s.region)
    per_shard_cnt: Dict[Tuple[str, str], Counter] = {}
    per_shard_rows: Dict[Tuple[str, str], int] = {}
    # Also track stolen-row contribution per shard for the summary table.
    per_shard_stolen: Dict[Tuple[str, str], int] = {}

    for model_name, m_shards in model_to_shards.items():
        model_seen_qids: set = set()
        for shard in m_shards:
            cnt: Counter = Counter()
            rows = 0
            stolen_unique = 0
            # Primary first, then any steal outputs.
            candidate_files = [
                (shard.shard_dir / "results.jsonl", False),
            ]
            for extra in sorted(shard.shard_dir.glob("results_steal_*.jsonl")):
                candidate_files.append((extra, True))
            for pth, is_steal in candidate_files:
                if not pth.exists():
                    continue
                with pth.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            cnt["parse_error"] += 1
                            rows += 1
                            continue
                        qid = r.get("qid")
                        # CROSS-SHARD dedup — attribute to first shard only.
                        if qid is not None:
                            if qid in model_seen_qids:
                                continue
                            model_seen_qids.add(qid)
                        rows += 1
                        if is_steal:
                            stolen_unique += 1
                        cnt[r.get("status") or "unknown"] += 1
            per_shard_cnt[shard_key(shard)] = cnt
            per_shard_rows[shard_key(shard)] = rows
            per_shard_stolen[shard_key(shard)] = stolen_unique

    # Pass 2 — assemble summary_rows + per-model totals
    summary_rows = []
    per_model: Dict[str, Counter] = {}
    totals = Counter()
    for shard in shards:
        key = shard_key(shard)
        cnt = per_shard_cnt[key]
        rows = per_shard_rows[key]
        stolen = per_shard_stolen[key]

        run_log = shard.shard_dir / "run.log"
        salvage = 0
        name_fix = 0
        if run_log.exists():
            text = run_log.read_text(errors="ignore")
            salvage = text.count("synthesized ehr.finish")
            name_fix = text.count("TOOL_NAME_FIX")
        for steal_log in sorted(shard.shard_dir.glob("run_steal_*.log")):
            text = steal_log.read_text(errors="ignore")
            salvage += text.count("synthesized ehr.finish")
            name_fix += text.count("TOOL_NAME_FIX")
        row = {
            "friendly_name": shard.friendly_name,
            "model_tag": shard.model_tag,
            "region": shard.region,
            "model_id": shard.model_id,
            "input_rows": shard.rows,
            "results_rows": rows,
            "stolen_unique": stolen,
            "success": cnt.get("success", 0),
            "incomplete": cnt.get("incomplete", 0),
            "error": cnt.get("error", 0),
            "salvage": salvage,
            "name_fix": name_fix,
            "returncode": shard.returncode,
        }
        summary_rows.append(row)
        bucket = per_model.setdefault(shard.friendly_name, Counter())
        for k in ("input_rows", "results_rows", "success", "incomplete", "error",
                  "salvage", "name_fix", "stolen_unique"):
            bucket[k] += row[k]
            totals[k] += row[k]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shards": summary_rows,
        "per_model": {k: dict(v) for k, v in per_model.items()},
        "totals": dict(totals),
    }
    (output_root / "summary.json").write_text(json.dumps(out, indent=2))

    # Markdown companion. Row counts are UNIQUE qids after cross-shard dedup;
    # `stolen` is the subset of that row count that came from this shard's
    # steal activity (as a helper). Sum of `out` per model equals unique qids
    # for that model (= 2,695 when coverage is complete).
    md_lines = [
        f"# Multi-model eval summary — {out['generated_at']}",
        "",
        "## Per-shard (unique qids attributed to each shard)",
        "",
        "| model | region | model_id | in | out | stolen | success | incomplete | error | salvage | name_fix | rc |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(summary_rows, key=lambda x: (x["friendly_name"], x["region"])):
        md_lines.append(
            "| {friendly_name} | {region} | `{model_id}` | {input_rows} | "
            "{results_rows} | {stolen_unique} | {success} | {incomplete} | "
            "{error} | {salvage} | {name_fix} | {returncode} |".format(**r)
        )
    md_lines.extend(["", "## Per-model totals", "",
                     "| model | in | out | success | incomplete | error | salvage | name_fix |",
                     "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for name, c in per_model.items():
        md_lines.append(
            f"| {name} | {c['input_rows']} | {c['results_rows']} | "
            f"{c['success']} | {c['incomplete']} | {c['error']} | "
            f"{c['salvage']} | {c['name_fix']} |"
        )
    (output_root / "summary.md").write_text("\n".join(md_lines) + "\n")
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--models", nargs="+", required=True,
                   help="One or more friendly names from the catalog (see docs/07_bedrock_model_catalog.md).")
    p.add_argument("--data", type=str, required=True,
                   help="Path to input JSONL (one sample per line).")
    p.add_argument("--output-root", type=str, default=None,
                   help="Default: ./results/multi_eval/<UTC-stamp>")
    p.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    p.add_argument("--smoke-size", type=int, default=10)
    p.add_argument("--max-parallel-shards", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=6,
                   help="Per-shard --max_concurrency")
    p.add_argument("--max-rounds", type=int, default=200)
    p.add_argument("--runs_per_question", type=int, default=1)
    p.add_argument("--max_tool_result_chars", type=int, default=100_000)
    p.add_argument("--image_max_edge", type=int, default=1568)
    p.add_argument("--enable-image", dest="enable_image", action="store_true", default=True)
    p.add_argument("--no-enable-image", dest="enable_image", action="store_false")
    p.add_argument("--enable-ehr", dest="enable_ehr", action="store_true", default=True)
    p.add_argument("--no-enable-ehr", dest="enable_ehr", action="store_false")
    p.add_argument("--enable-thinking", dest="enable_thinking", action="store_true", default=False)
    p.add_argument("--ehr-mcp-url-ehrxqa", type=str, default=DEFAULT_EHR_EHRXQA)
    p.add_argument("--ehr-mcp-url-medmod", type=str, default=DEFAULT_EHR_MEDMOD)
    p.add_argument("--image-mcp-url", type=str, default=DEFAULT_IMAGE_MCP)
    p.add_argument("--bench-root", type=str, default=DEFAULT_BENCH_ROOT)
    p.add_argument("--no-start-mcp", action="store_true",
                   help="Trust externally-managed MCPs. Driver never starts MCPs itself; "
                        "this flag exists for symmetry with run_mm_pipeline.sh.")
    p.add_argument("--no-work-steal", action="store_true",
                   help="Disable work-stealing: when one region finishes, do "
                        "NOT reallocate un-started rows from slower siblings "
                        "of the same model.")
    p.add_argument("--pybin", type=str, default=DEFAULT_PYBIN)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    # Map dashed → underscored names used in build_cmd
    args.ehr_mcp_url_ehrxqa = args.ehr_mcp_url_ehrxqa
    args.ehr_mcp_url_medmod = args.ehr_mcp_url_medmod
    args.image_mcp_url = args.image_mcp_url
    args.bench_root = args.bench_root
    return args


def main() -> int:
    args = parse_args()
    catalog = load_catalog()

    data_path = Path(args.data).resolve()
    if not data_path.exists():
        raise SystemExit(f"--data not found: {data_path}")
    rows = read_jsonl(data_path)
    if not rows:
        raise SystemExit(f"--data {data_path} is empty")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.output_root:
        output_root = Path(args.output_root).resolve()
    else:
        output_root = SCRIPT_DIR / "results" / "multi_eval" / f"{args.mode}_{stamp}"
    output_root.mkdir(parents=True, exist_ok=True)

    smoke_size = args.smoke_size if args.mode == "smoke" else None
    shards = build_shards(
        friendly_names=args.models,
        catalog=catalog,
        rows=rows,
        output_root=output_root,
        smoke_size=smoke_size,
    )
    if not shards:
        raise SystemExit("No shards built — check --models and catalog")

    # Build commands
    env = os.environ.copy()
    # Propagate per-shard region so the subprocess's default boto3 region is right
    for shard in shards:
        shard.cmd = build_cmd(shard, args, args.pybin)

    print(f"[driver] output_root: {output_root}")
    print(f"[driver] mode={args.mode} | shards={len(shards)} | "
          f"max_parallel_shards={args.max_parallel_shards} | "
          f"per_shard_concurrency={args.concurrency}")
    for s in shards:
        print(f"  {s.friendly_name:28} {s.region:16} {s.model_id:45} rows={s.rows}")
        if args.dry_run:
            print(f"    cmd: {shlex.join(s.cmd)}")

    if args.dry_run:
        return 0

    # Write the top-level manifest before spawning
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "data": str(data_path),
        "output_root": str(output_root),
        "models": args.models,
        "per_shard_concurrency": args.concurrency,
        "max_parallel_shards": args.max_parallel_shards,
        "shards": [
            {
                "friendly_name": s.friendly_name,
                "model_tag": s.model_tag,
                "region": s.region,
                "model_id": s.model_id,
                "input_path": str(s.input_path),
                "shard_dir": str(s.shard_dir),
                "rows": s.rows,
            }
            for s in shards
        ],
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2))

    stop = threading.Event()
    poll_thread = threading.Thread(target=poll_loop, args=(shards, stop), daemon=True)
    poll_thread.start()

    max_rc = 0
    # Work-stealing params (approach B). `steal_safety_gap` = per-shard
    # concurrency, to avoid racing with the donor's in-flight rows.
    # `min_tail` = 2 ensures we only steal when the donor has a non-trivial
    # amount of un-started work.
    steal_safety_gap = args.concurrency
    min_tail = max(2, args.concurrency // 2) if not args.no_work_steal else 10**9
    work_steal_enabled = not args.no_work_steal

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.max_parallel_shards
    ) as pool:
        primary_futures = {
            pool.submit(run_shard, s, s.cmd, env.copy() | {
                "AWS_DEFAULT_REGION": s.region,
                "BEDROCK_REGION": s.region,
            }): (s, "primary") for s in shards
        }
        active: Dict = dict(primary_futures)

        while active:
            done, _ = concurrent.futures.wait(
                list(active.keys()),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for f in done:
                s, kind = active.pop(f)
                try:
                    rc = f.result()
                except Exception as exc:
                    print(f"[driver] shard {s.model_tag}/{s.region} ({kind}) "
                          f"raised: {exc}", file=sys.stderr)
                    rc = 99
                tag = f"{s.model_tag}/{s.region}"
                if kind == "steal" and rc is None:
                    # `try_steal_work` returns None when no donor was found;
                    # quietly retire this helper (do not re-submit).
                    print(f"[driver] {tag}: no more donors; helper retired")
                    continue
                if rc is not None and rc > max_rc:
                    max_rc = rc
                print(f"[driver] {kind} done: {tag} rc={rc} "
                      f"rows={_count_lines(s.shard_dir / 'results.jsonl')}/{s.rows}")
                # Try to steal more work for this helper. Keep stealing until
                # no donor has an un-started tail.
                if work_steal_enabled and rc == 0:
                    shard_env = env.copy() | {
                        "AWS_DEFAULT_REGION": s.region,
                        "BEDROCK_REGION": s.region,
                    }
                    steal_future = pool.submit(
                        try_steal_work, s, shards, args, shard_env,
                        steal_safety_gap, min_tail,
                    )
                    active[steal_future] = (s, "steal")
    stop.set()
    poll_thread.join(timeout=1)

    print("\n[driver] aggregating summary...")
    agg = aggregate(shards, output_root)
    # Print a compact human-readable view
    print("\n=== per-shard summary ===")
    for r in sorted(agg["shards"], key=lambda x: (x["friendly_name"], x["region"])):
        print(f"  {r['friendly_name']:28} {r['region']:16} "
              f"out={r['results_rows']:>3}/{r['input_rows']:<3} "
              f"ok={r['success']:<3} inc={r['incomplete']:<3} "
              f"err={r['error']:<3} sal={r['salvage']:<3} fix={r['name_fix']:<3}")

    if args.mode == "smoke" and max_rc == 0:
        print(f"\n[driver] smoke passed. To run full partition:")
        args_quoted = " ".join(f'"{m}"' for m in args.models)
        print(
            f"  python {sys.argv[0]} --models {args_quoted} --data {data_path} "
            f"--mode full --max-parallel-shards {args.max_parallel_shards}"
        )

    return max_rc


if __name__ == "__main__":
    sys.exit(main())
