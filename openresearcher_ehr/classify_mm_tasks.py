"""Classify every mm_bench row into a fine-grained task + multimodal flag.

For each row in a JSONL (default: combined_test_set_nonempty.jsonl), call
Claude on AWS Bedrock once with:
  - a taxonomy of candidate tasks (45 ehr_bench tasks + 7 mm_bench clusters)
  - the row's question, scope, modalities, label shape

Claude returns a <classification> block containing JSON with:
  {
    "task":           <one of the candidate names, or "other">,
    "is_multimodal":  true|false,       # chest-x-ray image / report attached
    "multimodal_kind":<one of ["cxr_image","cxr_report","none"]>,
    "confidence":     <"high"|"medium"|"low">,
    "reason":         "<=25-word justification"
  }

The script writes a per-row JSONL alongside the input with one classification
per line, plus a summary table printed to stdout.

Usage:
    python classify_mm_tasks.py \
        --data /fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/combined_test_set_nonempty.jsonl \
        --output /fsx-shared/juncheng/EHR/openresearcher_ehr/analysis/mm_bench_task_classification.jsonl \
        --model "Claude Sonnet 4.6" \
        --concurrency 12
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Tuple


# -----------------------------------------------------------------------------
# Taxonomy
# -----------------------------------------------------------------------------

EHR_BENCH_TASKS: List[Tuple[str, str, str]] = [
    # (task_name, group, 1-line description)
    ("ED_Critical_Outcomes", "risk_prediction", "will patient die or be transferred to ICU within 12h after ER"),
    ("ED_Hospitalization", "risk_prediction", "will the ED visit result in inpatient admission"),
    ("ED_ICU_Tranfer_12hour", "risk_prediction", "will the patient transfer to ICU within 12h after ED"),
    ("ED_Inpatient_Mortality", "risk_prediction", "will the ED patient die during hospitalization"),
    ("ED_Reattendance_3day", "risk_prediction", "will the patient return to the ED within 3 days"),
    ("ICU_Mortality_1day", "risk_prediction", "will the patient die within 1 day in ICU"),
    ("ICU_Mortality_2day", "risk_prediction", "will the patient die within 2 days in ICU"),
    ("ICU_Mortality_3day", "risk_prediction", "will the patient die within 3 days in ICU"),
    ("ICU_Mortality_7day", "risk_prediction", "will the patient die within 7 days in ICU"),
    ("ICU_Mortality_14day", "risk_prediction", "will the patient die within 14 days in ICU"),
    ("ICU_Readmission", "risk_prediction", "will the patient be readmitted to ICU"),
    ("ICU_Stay_7day", "risk_prediction", "will the ICU stay exceed 7 days"),
    ("ICU_Stay_14day", "risk_prediction", "will the ICU stay exceed 14 days"),
    ("Inpatient_Mortality", "risk_prediction", "will the patient die during hospitalization"),
    ("LengthOfStay_3day", "risk_prediction", "will the hospital stay exceed 3 days"),
    ("LengthOfStay_7day", "risk_prediction", "will the hospital stay exceed 7 days"),
    ("Readmission_30day", "risk_prediction", "will the patient be readmitted within 30 days"),
    ("Readmission_60day", "risk_prediction", "will the patient be readmitted within 60 days"),
    ("admissions", "decision_making", "next admission type / location suggestion"),
    ("chartevents", "decision_making", "next ICU chart event item to record"),
    ("datetimeevents", "decision_making", "next datetime event to record (ICU)"),
    ("diagnoses_ccs", "decision_making", "next CCS diagnosis group to assign"),
    ("diagnoses_icd", "decision_making", "next ICD diagnosis code to assign"),
    ("diagnosis", "decision_making", "next ED diagnosis free-text"),
    ("diagnosis_ccs", "decision_making", "next ED CCS diagnosis group"),
    ("emar", "decision_making", "next eMAR medication administration to record"),
    ("ingredientevents", "decision_making", "next ingredient event item"),
    ("inputevents", "decision_making", "next ICU input event item"),
    ("labevents", "decision_making", "next laboratory test to order"),
    ("medrecon", "decision_making", "next ED medication reconciliation item"),
    ("medrecon_atc", "decision_making", "next ED medication reconciliation ATC class"),
    ("microbiologyevents", "decision_making", "next microbiology test to order"),
    ("next_event", "decision_making", "next generic hospital event (any category)"),
    ("omr", "decision_making", "next outpatient medical record item"),
    ("outputevents", "decision_making", "next ICU output event item"),
    ("poe", "decision_making", "next provider order entry"),
    ("prescriptions", "decision_making", "next prescription drug to suggest"),
    ("prescriptions_atc", "decision_making", "next prescription ATC class"),
    ("procedureevents", "decision_making", "next ICU procedure event"),
    ("procedures_ccs", "decision_making", "next CCS procedure group"),
    ("procedures_icd", "decision_making", "next ICD procedure code"),
    ("pyxis", "decision_making", "next Pyxis (unit-dose medication dispense)"),
    ("radiology", "decision_making", "next radiology exam to order"),
    ("services", "decision_making", "next service transfer (medicine/surgery/etc.)"),
    ("transfers", "decision_making", "next ward / unit transfer destination"),
]

MM_CLUSTERS: List[Tuple[str, str, str]] = [
    ("ehrxqa_cohort_query", "multi_patient_query",
     "EHRXQA question over a population/counts across patients (cohort_scope)"),
    ("ehrxqa_patient_lookup", "single_patient_query",
     "EHRXQA factual lookup on one patient's EHR tables (dates, values, counts)"),
    ("cxr_finding_presence", "cxr_image_qa",
     "yes/no or binary presence of a specific chest X-ray finding / anatomy pair"),
    ("cxr_finding_enumeration", "cxr_image_qa",
     "enumerate all findings / technical assessments visible in chest X-ray study"),
    ("cxr_change_comparison", "cxr_image_qa",
     "compare two chest X-ray studies (what changed / resolved / new)"),
    ("mortality_24h", "risk_prediction",
     "predict patient death within next 24h using EHR + CXR evidence"),
    ("phenotype_group_assignment", "decision_making",
     "multi-label CCS-style phenotype groups that apply to patient"),
]

ALL_TASKS: List[Tuple[str, str, str]] = EHR_BENCH_TASKS + MM_CLUSTERS
TASK_NAMES: List[str] = [t[0] for t in ALL_TASKS]

# Used by LLM if nothing fits
OTHER_TOKEN = "other"


def _build_taxonomy_block() -> str:
    lines = ["Candidate tasks (pick exactly one, or 'other' if nothing fits):"]
    for name, group, desc in ALL_TASKS:
        lines.append(f"  - [{group}] {name}: {desc}")
    lines.append(f"  - {OTHER_TOKEN}: none of the above")
    return "\n".join(lines)


TAXONOMY_BLOCK = _build_taxonomy_block()


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

SYSTEM = (
    "You are a medical-benchmark task classifier. Read the question and "
    "metadata, then pick exactly one task from the taxonomy. Also decide "
    "whether the task needs multimodal input (a chest X-ray image or "
    "free-text radiology report) beyond plain EHR tables."
)


def _row_preview(row: Dict[str, Any]) -> str:
    # Show the row's <question> block + the task / scope / modalities / label
    # shape so the classifier has the full picture without a 100k-char blob.
    q = row.get("question") or ""
    m = re.search(r"<question>\s*(.*?)\s*</question>", q, re.DOTALL)
    question_text = (m.group(1) if m else q).strip()
    # Truncate very long embedded timelines — classifier only needs the task gist
    if len(question_text) > 2500:
        question_text = question_text[:1200] + "\n...[truncated]...\n" + question_text[-800:]
    parts = [
        f"coarse_task: {row.get('task')}",
        f"source_benchmark: {row.get('source_benchmark')}",
        f"scope: {row.get('scope')}",
        f"modalities: {json.dumps(row.get('modalities'))}",
        f"has_image_paths: {bool(row.get('image_paths'))}",
        f"has_report_paths: {bool(row.get('report_paths'))}",
        f"label_shape: {_label_shape(row.get('label'))}",
        f"",
        f"question_text: {question_text}",
    ]
    return "\n".join(parts)


def _label_shape(label: Any) -> str:
    if isinstance(label, list):
        if not label:
            return "empty_list"
        first = label[0]
        if isinstance(first, dict):
            names = [str(x.get("name") or x.get("value") or "") for x in label[:3]]
        else:
            names = [str(x) for x in label[:3]]
        return f"list[len={len(label)}] sample={names!r}"
    if isinstance(label, str):
        return f"str={label!r}"
    return f"{type(label).__name__}"


def build_user_prompt(row: Dict[str, Any]) -> str:
    return (
        f"{TAXONOMY_BLOCK}\n\n"
        f"--- Row ---\n"
        f"{_row_preview(row)}\n\n"
        f"--- Answer ---\n"
        f"Return a <classification> block containing strict JSON with these "
        f"keys (and nothing else):\n"
        f"  task              one of the candidate names (or 'other')\n"
        f"  is_multimodal     true|false — true iff the task fundamentally "
        f"requires a chest X-ray image or radiology report as input\n"
        f"  multimodal_kind   'cxr_image' | 'cxr_report' | 'both' | 'none'\n"
        f"  confidence        'high' | 'medium' | 'low'\n"
        f"  reason            <= 25-word justification\n\n"
        f"Example:\n"
        f'<classification>{{"task":"cxr_finding_enumeration",'
        f'"is_multimodal":true,"multimodal_kind":"cxr_image",'
        f'"confidence":"high","reason":"..."}}</classification>'
    )


# -----------------------------------------------------------------------------
# Bedrock invoker (Anthropic path only — classifier defaults to Sonnet 4.6)
# -----------------------------------------------------------------------------

_REGION_CATALOG = Path(
    "/fsx-shared/juncheng/EHR/openresearcher_ehr/bedrock_model_region_availability.json"
)


def _load_first_ok_region(model_name: str, preferred: str | None = None) -> Tuple[str, str]:
    j = json.loads(_REGION_CATALOG.read_text())
    if model_name not in j:
        raise SystemExit(f"Unknown model {model_name!r}. Known: {sorted(j.keys())}")
    ok = [e for e in j[model_name] if e["status"] == "OK"]
    if preferred:
        for e in ok:
            if e["region"] == preferred:
                return preferred, e["model_id"]
    for pref in ("us-east-1", "us-west-2", "us-east-2"):
        for e in ok:
            if e["region"] == pref:
                return pref, e["model_id"]
    return ok[0]["region"], ok[0]["model_id"]


_RETRYABLE_KWS = (
    "timeout", "throttl", "too many requests", "service unavailable",
    "internal server error", "internalserver", "unexpected error", "busy",
)


class BedrockClassifier:
    def __init__(self, model_name: str, region: str | None,
                 max_tokens: int, temperature: float, max_retries: int,
                 read_timeout: int):
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError

        self._boto3 = boto3
        self._BotoCoreError = BotoCoreError
        self._ClientError = ClientError

        region, model_id = _load_first_ok_region(model_name, preferred=region)
        self.region = region
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=BotoConfig(
                read_timeout=read_timeout,
                connect_timeout=10,
                retries={"max_attempts": 0},
            ),
        )

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, self._ClientError):
            code = (exc.response or {}).get("Error", {}).get("Code", "") or ""
            if code in {
                "InternalServerException", "ModelNotReadyException",
                "RequestTimeoutException", "ServiceUnavailableException",
                "ThrottlingException", "TooManyRequestsException",
            }:
                return True
        if isinstance(exc, self._BotoCoreError):
            return True
        return any(kw in str(exc).lower() for kw in _RETRYABLE_KWS)

    def invoke(self, prompt_text: str) -> Tuple[str, str]:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": SYSTEM,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": prompt_text}]}
            ],
        }
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.invoke_model(
                    modelId=self.model_id, body=json.dumps(body)
                )
                out = json.loads(resp["body"].read())
                text = "\n".join(
                    blk.get("text", "")
                    for blk in (out.get("content") or [])
                    if blk.get("type") == "text"
                ).strip()
                return text, "ok"
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    break
                time.sleep(min(2**attempt, 10))
        return f"[ERROR] {type(last_err).__name__}: {last_err}", "error"


# -----------------------------------------------------------------------------
# Parsing
# -----------------------------------------------------------------------------

_VALID_MM_KINDS = {"cxr_image", "cxr_report", "both", "none"}
_VALID_CONF = {"high", "medium", "low"}


def parse_classification(text: str) -> Dict[str, Any] | None:
    """Extract the <classification>{...}</classification> JSON payload."""
    if not text:
        return None
    m = re.search(
        r"<classification[^>]*>\s*(.*?)\s*</classification\s*>",
        text, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        # Unterminated — take tail
        m2 = re.search(r"<classification[^>]*>\s*(.*)$", text, re.DOTALL | re.IGNORECASE)
        if not m2:
            return None
        blob = m2.group(1).strip()
        # Try to parse an opening brace to a matching close
        if "{" not in blob:
            return None
        blob = blob[blob.index("{"):]
        # Best-effort balance
        depth = 0
        end = -1
        for i, ch in enumerate(blob):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > 0:
            blob = blob[: end + 1]
    else:
        blob = m.group(1).strip()

    try:
        obj = json.loads(blob)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def normalize_classification(raw: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    task = str(raw.get("task") or "").strip()
    if task not in TASK_NAMES and task != OTHER_TOKEN:
        task = OTHER_TOKEN
    mm = raw.get("is_multimodal")
    if not isinstance(mm, bool):
        # Heuristic fallback: image_paths/report_paths present
        mm = bool(row.get("image_paths") or row.get("report_paths"))
    kind = str(raw.get("multimodal_kind") or "").strip().lower()
    if kind not in _VALID_MM_KINDS:
        if row.get("image_paths") and row.get("report_paths"):
            kind = "both"
        elif row.get("image_paths"):
            kind = "cxr_image"
        elif row.get("report_paths"):
            kind = "cxr_report"
        else:
            kind = "none"
    conf = str(raw.get("confidence") or "").strip().lower()
    if conf not in _VALID_CONF:
        conf = "low"
    reason = str(raw.get("reason") or "")[:200]
    return {
        "task": task,
        "is_multimodal": bool(mm),
        "multimodal_kind": kind,
        "confidence": conf,
        "reason": reason,
    }


# -----------------------------------------------------------------------------
# Row worker
# -----------------------------------------------------------------------------


def process_row(row: Dict[str, Any], invoker: BedrockClassifier) -> Dict[str, Any]:
    qid = row.get("qid") or f"unknown_{uuid.uuid4().hex[:8]}"
    prompt = build_user_prompt(row)
    text, status = invoker.invoke(prompt)
    parsed = parse_classification(text) if status == "ok" else None
    out = {
        "qid": qid,
        "coarse_task": row.get("task"),
        "source_benchmark": row.get("source_benchmark"),
        "scope": row.get("scope"),
        "modalities": row.get("modalities"),
        "_status": status,
        "raw_response": text[:400],
    }
    if parsed is None:
        out["classification"] = None
        # Heuristic fallback so downstream always has something
        out["classification_fallback"] = {
            "task": OTHER_TOKEN,
            "is_multimodal": bool(row.get("image_paths") or row.get("report_paths")),
            "multimodal_kind": (
                "both" if row.get("image_paths") and row.get("report_paths")
                else "cxr_image" if row.get("image_paths")
                else "cxr_report" if row.get("report_paths")
                else "none"
            ),
            "confidence": "low",
            "reason": "parse_failure_fallback",
        }
    else:
        out["classification"] = normalize_classification(parsed, row)
    return out


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data",
        default="/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/combined_test_set_nonempty.jsonl",
    )
    ap.add_argument("--output", required=True,
                    help="Where to write the per-row JSONL classifications.")
    ap.add_argument("--summary",
                    help="Optional path for a human-readable summary table "
                    "(default: <output>.summary.md).")
    ap.add_argument("--model", default="Claude Sonnet 4.6")
    ap.add_argument("--region", default=None,
                    help="Bedrock region override (default: first OK for model).")
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--read-timeout", type=int, default=90)
    ap.add_argument("--limit", type=int, default=0,
                    help="Only classify first N rows (smoke).")
    args = ap.parse_args()

    # Input loader — JSONL or JSON list
    rows: List[Dict[str, Any]] = []
    raw = Path(args.data).read_text()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            rows = parsed
        else:
            raise ValueError("not a list")
    except Exception:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    if args.limit > 0:
        rows = rows[: args.limit]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    invoker = BedrockClassifier(
        model_name=args.model, region=args.region,
        max_tokens=args.max_tokens, temperature=args.temperature,
        max_retries=args.max_retries, read_timeout=args.read_timeout,
    )
    print(
        f"Classifier: {args.model} @ {invoker.region} (model_id={invoker.model_id})",
        flush=True,
    )
    print(f"rows: {len(rows)}, concurrency: {args.concurrency}", flush=True)

    results: List[Dict[str, Any]] = []
    lock = Lock()
    done = 0
    with out_path.open("w") as fout, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(process_row, r, invoker): r for r in rows}
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = futures[fut]
                res = {
                    "qid": row.get("qid"),
                    "coarse_task": row.get("task"),
                    "_status": "worker_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "classification": None,
                }
            with lock:
                fout.write(json.dumps(res) + "\n")
                fout.flush()
                results.append(res)
                done += 1
                if done % 50 == 0 or done == len(rows):
                    print(f"  done={done}/{len(rows)}", flush=True)

    _write_summary(results, args.summary or (str(out_path) + ".summary.md"))
    return 0


def _write_summary(results: List[Dict[str, Any]], path: str) -> None:
    import collections
    lines: List[str] = []
    total = len(results)
    parsed = sum(1 for r in results if r.get("classification"))
    fallback = sum(1 for r in results if r.get("classification_fallback"))
    lines.append("# mm_bench task classification — summary\n")
    lines.append(f"Total rows: {total}")
    lines.append(f"Parsed OK: {parsed}   parse-failure fallback: {fallback}\n")

    # Effective classification = classification or classification_fallback
    def eff(r):
        return r.get("classification") or r.get("classification_fallback") or {}

    # By coarse_task -> fine task counts
    coarse_to_fine: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in results:
        coarse_to_fine[r.get("coarse_task") or "?"][eff(r).get("task", "?")] += 1

    lines.append("## Fine-grained task counts per coarse task\n")
    for coarse in sorted(coarse_to_fine.keys()):
        lines.append(f"### coarse_task = `{coarse}`")
        lines.append(f"| task | count | % |")
        lines.append(f"| --- | ---: | ---: |")
        total_c = sum(coarse_to_fine[coarse].values())
        for task, n in coarse_to_fine[coarse].most_common():
            pct = 100 * n / total_c if total_c else 0
            lines.append(f"| {task} | {n} | {pct:.1f}% |")
        lines.append("")

    # Multimodal distribution per coarse task
    lines.append("## Multimodal distribution per coarse task\n")
    lines.append(f"| coarse_task | rows | is_mm=true | mm_kind breakdown |")
    lines.append(f"| --- | ---: | ---: | --- |")
    for coarse in sorted(coarse_to_fine.keys()):
        sub = [r for r in results if r.get("coarse_task") == coarse]
        n = len(sub)
        n_mm = sum(1 for r in sub if eff(r).get("is_multimodal"))
        kinds = collections.Counter(eff(r).get("multimodal_kind", "none") for r in sub)
        kind_str = ", ".join(f"{k}={v}" for k, v in kinds.most_common())
        lines.append(f"| {coarse} | {n} | {n_mm} | {kind_str} |")
    lines.append("")

    # Global fine-grained task distribution
    global_fine = collections.Counter(eff(r).get("task", "?") for r in results)
    global_mm = collections.Counter(
        (eff(r).get("task", "?"), eff(r).get("is_multimodal", False)) for r in results
    )
    lines.append("## Global fine-grained task distribution (across all rows)\n")
    lines.append(f"| task | count | is_mm=true | is_mm=false |")
    lines.append(f"| --- | ---: | ---: | ---: |")
    for task, n in global_fine.most_common():
        mm_true = global_mm.get((task, True), 0)
        mm_false = global_mm.get((task, False), 0)
        lines.append(f"| {task} | {n} | {mm_true} | {mm_false} |")
    lines.append("")

    Path(path).write_text("\n".join(lines))
    print(f"\nSummary written to: {path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
