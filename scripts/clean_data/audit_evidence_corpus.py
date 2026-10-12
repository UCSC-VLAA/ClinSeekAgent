#!/usr/bin/env python3
"""Audit a corpus built by build_evidence_corpus.py for temporal / scope leakage.

Runs against the produced CSV files and per-case `_manifest.json`, not against
the builder, so it cannot inherit a builder bug. Every case is checked; nothing
is sampled.

Checks
  1  no timestamp in any time/date column of any CSV is after the case cutoff
  2  forbidden columns never appear (dod, admissions.text, cost, MM outcome
     columns of encounters still open at the cutoff)
  3  recent is a row-level subset of full
  4  MM only: tb_cxr study_ids stay inside the manifest allowlist, and
     image-only cases carry no longitudinal EHR table
  5  (--expect-policy) every case records the expected policy_version

Usage:
    python scripts/clean_data/audit_evidence_corpus.py \
        --corpus /path/to/corpora/text_final_aggressive --bench text \
        --expect-policy strict-ehr-runtime-v8 --output audit_text.json
    python scripts/clean_data/audit_evidence_corpus.py \
        --corpus /path/to/corpora/mm_final_aggressive --bench mm \
        --expect-policy multimodal-strict-temporal-scope-v1 --output audit_mm.json

Exit status 0 = PASS, 1 = violations found.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

# Columns the policy documents as "still not provided to the model".
FORBIDDEN_COLUMNS = {
    "patients": {"dod"},
    "admissions": {"text"},
}
# MM outcome columns. They describe how an encounter ended: readable once it
# has ended, unreadable before that. Presence is therefore judged against the
# row's own end time, never against the column name alone (a name-only rule
# would flag outcomes of encounters that had legitimately ended).
MM_OUTCOME_COLUMNS = {
    "last_careunit", "los", "mortality", "mortality_inunit",
    "mortality_inhospital", "deathtime",
}
# Anchors that establish when the encounter ended, in priority order.
MM_OUTCOME_ANCHORS = ("outtime", "dischtime")
# `dod` has no availability anchor of its own, so it stays unconditional.
MM_FORBIDDEN_ANY = {"dod"}
FORBIDDEN_TABLES = {"cost"}

EHR_TABLE_HINTS = {"events", "stays", "labevents", "chartevents",
                   "prescriptions", "admissions", "icustays", "transfers"}


def time_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns
            if "time" in str(c).lower() or "date" in str(c).lower()]


def audit_case(case_dir: Path, bench: str) -> list[dict[str, Any]]:
    man_path = case_dir / "_manifest.json"
    if not man_path.exists():
        return [{"case": case_dir.name, "type": "missing_manifest"}]
    man = json.loads(man_path.read_text(encoding="utf-8"))
    cid = man.get("id", case_dir.name)
    cutoff = pd.to_datetime(man.get("cutoff"), errors="coerce")
    bad: list[dict[str, Any]] = []

    if pd.isna(cutoff):
        bad.append({"case": cid, "type": "unparseable_cutoff",
                    "detail": man.get("cutoff")})
        return bad

    views: dict[str, dict[str, pd.DataFrame]] = {}
    for view in ("full", "recent"):
        vdir = case_dir / view
        loaded: dict[str, pd.DataFrame] = {}
        if not vdir.exists():
            bad.append({"case": cid, "type": "missing_view", "detail": view})
            continue
        for csv in sorted(vdir.glob("*.csv")):
            table = csv.stem
            try:
                # dtype=str disables per-file type inference. Without it the
                # same value reads back as '15' from a mixed-type column and
                # '15.0' from an all-numeric slice of that same column, and the
                # recent-subset-of-full check would report false deficits.
                df = pd.read_csv(csv, dtype=str, keep_default_na=False,
                                 low_memory=False)
            except Exception as exc:  # noqa: BLE001
                bad.append({"case": cid, "type": "unreadable_csv",
                            "detail": f"{view}/{csv.name}: {exc}"})
                continue
            loaded[table] = df

            # 2 - forbidden tables / columns
            if table in FORBIDDEN_TABLES:
                bad.append({"case": cid, "type": "forbidden_table",
                            "detail": f"{view}/{table}"})
            for col in FORBIDDEN_COLUMNS.get(table, set()):
                if col in df.columns:
                    bad.append({"case": cid, "type": "forbidden_column",
                                "detail": f"{view}/{table}.{col}"})
            if bench == "mm":
                for col in df.columns:
                    lname = str(col).lower()
                    if lname in MM_FORBIDDEN_ANY:
                        bad.append({"case": cid, "type": "forbidden_mm_column",
                                    "detail": f"{view}/{table}.{col}"})
                    elif lname in MM_OUTCOME_COLUMNS:
                        # Readable only once the encounter has ended. Judge each
                        # row against its own end time.
                        anchor = next(
                            (a for a in MM_OUTCOME_ANCHORS if a in df.columns),
                            None)
                        if anchor is None:
                            bad.append({
                                "case": cid,
                                "type": "unresolved_outcome_column_no_anchor",
                                "detail": f"{view}/{table}.{col}"})
                            continue
                        ends = pd.to_datetime(df[anchor], errors="coerce")
                        present = df[col].notna()
                        still_open = present & (ends.isna() | (ends > cutoff))
                        if bool(still_open.any()):
                            bad.append({
                                "case": cid,
                                "type": "outcome_column_before_encounter_ended",
                                "detail": f"{view}/{table}.{col}",
                                "n": int(still_open.sum())})

            # 1 - no future timestamps
            for col in time_columns(df):
                stamps = pd.to_datetime(df[col], errors="coerce")
                future = stamps[stamps > cutoff]
                if len(future):
                    bad.append({
                        "case": cid, "type": "future_timestamp",
                        "detail": f"{view}/{table}.{col}",
                        "n": int(len(future)),
                        "max": str(future.max()),
                        "cutoff": str(cutoff),
                    })
        views[view] = loaded

    # 3 - recent must be a row subset of full
    full, recent = views.get("full", {}), views.get("recent", {})
    for table, rdf in recent.items():
        fdf = full.get(table)
        if fdf is None:
            bad.append({"case": cid, "type": "recent_table_not_in_full",
                        "detail": table})
            continue
        if list(rdf.columns) != list(fdf.columns):
            bad.append({"case": cid, "type": "column_mismatch",
                        "detail": table})
            continue
        fset = set(map(tuple, fdf.astype(str).to_numpy().tolist()))
        rset = set(map(tuple, rdf.astype(str).to_numpy().tolist()))
        if not rset <= fset:
            bad.append({"case": cid, "type": "recent_not_subset_of_full",
                        "detail": table,
                        "n": int(len(rset - fset))})

    # 4 - MM scope
    if bench == "mm":
        allow = {str(s) for s in (man.get("study_ids") or [])}
        if not allow:
            # A silently-skipped scope check is worse than a loud one.
            bad.append({"case": cid, "type": "missing_study_id_allowlist"})
        modalities = {str(m).lower() for m in (man.get("modalities") or [])}
        image_only = not (modalities & {"ehr", "ehr_table", "clinical_ehr"})
        for view, loaded in views.items():
            cxr = loaded.get("tb_cxr")
            if cxr is not None and "study_id" in cxr.columns and allow:
                seen = set()
                for v in cxr["study_id"]:
                    s = str(v).strip()
                    if not s or s.lower() in {"nan", "none"}:
                        continue
                    try:
                        seen.add(str(int(float(s))))
                    except ValueError:
                        seen.add(s)
                extra = seen - allow
                if extra:
                    bad.append({"case": cid, "type": "cxr_scope_violation",
                                "detail": f"{view}: {sorted(extra)[:5]}"})
            if image_only:
                for table, df in loaded.items():
                    if table != "tb_cxr" and len(df):
                        bad.append({
                            "case": cid,
                            "type": "image_only_case_has_ehr",
                            "detail": f"{view}/{table} rows={len(df)}",
                        })
    return bad


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audit an evidence corpus")
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--bench", choices=["text", "mm"], required=True)
    p.add_argument("--expect-policy", type=str)
    p.add_argument("--output", type=Path)
    p.add_argument("--progress-every", type=int, default=200)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ev = args.corpus / "evidence"
    cases = sorted(d for d in ev.iterdir() if d.is_dir())
    counts = Counter(cases=len(cases))
    violations: list[dict[str, Any]] = []

    for i, case in enumerate(cases, 1):
        v = audit_case(case, args.bench)
        for item in v:
            counts[item["type"]] += 1
        violations.extend(v)
        if args.expect_policy:
            man = case / "_manifest.json"
            if man.exists():
                got = json.loads(man.read_text(encoding="utf-8")).get(
                    "policy_version")
                if got != args.expect_policy:
                    counts["wrong_policy_version"] += 1
                    violations.append({"case": case.name,
                                       "type": "wrong_policy_version",
                                       "detail": got})
        if args.progress_every and i % args.progress_every == 0:
            print(f"  {i}/{len(cases)}  violations={len(violations)}",
                  flush=True)

    counts["cases_with_violations"] = len({v["case"] for v in violations})
    print("\n=== AUDIT RESULT ===")
    for k, v in sorted(counts.items()):
        print(f"  {k:<34} {v}")
    verdict = "PASS" if not violations else "FAIL"
    print(f"\n  VERDICT: {verdict}  ({len(violations)} violations)")
    if violations:
        print("\n  first 10:")
        for v in violations[:10]:
            print("   ", v)
    if args.output:
        args.output.write_text(json.dumps(
            {"counts": dict(counts), "verdict": verdict,
             "violations": violations[:2000]},
            indent=2, default=str), encoding="utf-8")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
