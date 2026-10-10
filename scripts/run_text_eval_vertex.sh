#!/usr/bin/env bash
# ClinSeek-Bench-Text on Google Vertex AI with an explicitly pinned Claude model.
#
# A thin wrapper around scripts/run_text_eval.sh. The generic wrapper with
# BACKEND=vertex and no model auto-selects the first Claude model the project
# serves; pin one here so repeated runs are comparable.
#
#   DATA_PATH=/path/to/ClinSeek-Bench_text.json bash scripts/run_text_eval_vertex.sh
#   VERTEX_MODEL_ID=claude-opus-5 DATA_PATH=... bash scripts/run_text_eval_vertex.sh
#
# Credentials: Application Default Credentials (`gcloud auth
# application-default login`, or GOOGLE_APPLICATION_CREDENTIALS pointing at a
# service-account key) plus ANTHROPIC_VERTEX_PROJECT_ID, both read from .env.
# Requires an EHR MCP server on EHR_MCP_URL (scripts/run_ehr_mcp.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Values passed on the command line must outrank .env, but sourcing .env
# overwrites them -- so stash them first and put them back afterwards.
_cli_model="${VERTEX_MODEL_ID:-}"
_cli_region="${VERTEX_REGION:-}"
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a; . "${REPO_ROOT}/.env"; set +a
fi
[[ -n "${_cli_model}" ]] && VERTEX_MODEL_ID="${_cli_model}"
[[ -n "${_cli_region}" ]] && VERTEX_REGION="${_cli_region}"

export VERTEX_MODEL_ID="${VERTEX_MODEL_ID:-claude-opus-4-6}"
export VERTEX_PROJECT="${VERTEX_PROJECT:-${ANTHROPIC_VERTEX_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}}"
if [[ -z "${VERTEX_PROJECT}" ]]; then
  echo "ERROR: no GCP project. Set ANTHROPIC_VERTEX_PROJECT_ID in .env." >&2
  exit 1
fi

BACKEND=vertex MODEL_NAME_OR_PATH="${VERTEX_MODEL_ID}" \
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/outputs/text_eval_vertex}" \
  exec bash "${SCRIPT_DIR}/run_text_eval.sh" "$@"
