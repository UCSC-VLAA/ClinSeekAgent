#!/usr/bin/env bash
# Download BlueZeros/AgentEHR-Bench from Hugging Face.
#
#   bash scripts/download_agentehr_bench.sh
#   AGENTEHR_DATA_ROOT=/somewhere/else bash scripts/download_agentehr_bench.sh
#   SUBSET=MIMICIIIAgentBench bash scripts/download_agentehr_bench.sh   # ~170 MB
#
# The full repo is ~8.7 GB (9857 MIMIC-IV patient DBs + 1002 MIMIC-III ones).
# SUBSET restricts the download to one benchmark tree.
#
# The download resumes: re-running skips files already present, so it is safe
# to re-run after an interruption. Hugging Face rate-limits anonymous clients
# (HTTP 429) — hence the low worker count and the retry loop.
#
# MIMIC-derived data: keep the download local and do not redistribute it. The
# default location, data/AgentEHR-Bench under the repository, is git-ignored.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

AGENTEHR_DATA_ROOT="${AGENTEHR_DATA_ROOT:-${REPO_ROOT}/data/AgentEHR-Bench}"
SUBSET="${SUBSET:-}"
PYTHON_BIN="${PYTHON_BIN:-python}"
MAX_WORKERS="${MAX_WORKERS:-3}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-40}"

mkdir -p "${AGENTEHR_DATA_ROOT}"
echo "repo   : BlueZeros/AgentEHR-Bench"
echo "target : ${AGENTEHR_DATA_ROOT}"
echo "subset : ${SUBSET:-<all>}"

AGENTEHR_DATA_ROOT="${AGENTEHR_DATA_ROOT}" SUBSET="${SUBSET}" \
MAX_WORKERS="${MAX_WORKERS}" MAX_ATTEMPTS="${MAX_ATTEMPTS}" \
exec "${PYTHON_BIN}" - <<'PY'
import os
import time

from huggingface_hub import snapshot_download

target = os.environ["AGENTEHR_DATA_ROOT"]
subset = os.environ.get("SUBSET") or ""
workers = int(os.environ["MAX_WORKERS"])
attempts = int(os.environ["MAX_ATTEMPTS"])

kwargs = {}
if subset:
    kwargs["allow_patterns"] = [f"{subset}/*", "README.md"]

for attempt in range(1, attempts + 1):
    try:
        snapshot_download(
            "BlueZeros/AgentEHR-Bench",
            repo_type="dataset",
            local_dir=target,
            max_workers=workers,
            **kwargs,
        )
        print(f"\nDONE -> {target}")
        break
    except Exception as exc:  # noqa: BLE001 — retry anything transient
        wait = min(30 * attempt, 300)
        print(f"\nattempt {attempt}/{attempts} failed: "
              f"{type(exc).__name__}: {str(exc)[:200]}")
        if attempt == attempts:
            raise
        print(f"retrying in {wait}s (already-downloaded files are skipped)")
        time.sleep(wait)
PY
