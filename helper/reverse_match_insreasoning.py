"""
Reverse-match EHR-Ins-Reasoning JSONL entries to MIMIC-IV subject_id / hadm_id.

Adapted from helper/reverse_match.py (EHR-Bench) for the EHR-Ins-Reasoning
sample file, which is JSONL (not a JSON array) and lacks `qid`.

Strategy (priority, same as EHR-Bench matcher):
  1. ## Transfers [ts]                         -> transfers.intime
  2. ## EDstays [ts]                           -> transfers.intime (ED) / admissions.edregtime
  3. ## Admissions [ts]                        -> admissions.admittime
  4. ## Provider Order Entry [ts]              -> poe.ordertime
  5. ## Pharmacy [ts]                          -> pharmacy.entertime
  6. ## Prescriptions [ts]                     -> prescriptions.starttime
  7. ## Electronic Medicine Administration Record [ts] -> emar.charttime
  8. any first timestamp                       -> transfers.intime (fallback)

For each entry we narrow candidates by (gender, anchor_age), then try timestamps
until we get exactly 1 match. Entries with 0 or >1 matches are kept in output
with subject_id/hadm_id set to null, but also counted as "no_match" in the
summary. Pass --drop-unmatched to drop them instead.

Outputs:
  <stem>_matched.jsonl  — full records with subject_id/hadm_id added
  <stem>_ids.jsonl      — compact {idx, task, subject_id, hadm_id} per line
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# Force unbuffered output
def _p(*a, **kw):
    sys.stdout.write(" ".join(str(x) for x in a) + kw.get("end", "\n"))
    sys.stdout.flush()


print = _p


# ── Loaders ─────────────────────────────────────────────────────────────────

def load_patients(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/patients.csv") as f:
        for row in csv.DictReader(f):
            out[(row["gender"], row["anchor_age"])].append(row["subject_id"])
    print(f"  patients: {sum(len(v) for v in out.values())} rows")
    return out


def load_transfers(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/transfers.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["eventtype"], row["intime"], row["hadm_id"])
            )
    print(f"  transfers: {sum(len(v) for v in out.values())} rows")
    return out


def load_admissions(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/admissions.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["admittime"], row.get("edregtime", ""), row["hadm_id"])
            )
    print(f"  admissions: {sum(len(v) for v in out.values())} rows")
    return out


def load_poe(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/poe.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append((row["ordertime"], row["hadm_id"]))
    print(f"  poe: {sum(len(v) for v in out.values())} rows")
    return out


def load_pharmacy(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/pharmacy.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append((row["entertime"], row["hadm_id"]))
    print(f"  pharmacy: {sum(len(v) for v in out.values())} rows")
    return out


def load_prescriptions(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/prescriptions.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append((row["starttime"], row["hadm_id"]))
    print(f"  prescriptions: {sum(len(v) for v in out.values())} rows")
    return out


def load_emar(mimic_dir: Path):
    out = defaultdict(list)
    with open(mimic_dir / "hosp/emar.csv") as f:
        for row in csv.DictReader(f):
            if row["charttime"]:
                out[row["subject_id"]].append((row["charttime"], row["hadm_id"]))
    print(f"  emar: {sum(len(v) for v in out.values())} rows")
    return out


# ── Extraction ─────────────────────────────────────────────────────────────

TS_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
SECTION_PATTERN = re.compile(
    r"## ([\w\s]+?)\s*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"
)


def extract_demographics(inp: str):
    age = re.search(r"Anchor_Age:\s*(\d+)", inp)
    gen = re.search(r"Gender:\s*(\w)", inp)
    if age and gen:
        return (gen.group(1), age.group(1))
    return None


def extract_section_times(inp: str):
    sections = defaultdict(list)
    for m in SECTION_PATTERN.finditer(inp):
        sections[m.group(1).strip()].append(m.group(2))
    return sections


# ── Matching ───────────────────────────────────────────────────────────────

def try_match(candidate_sids, tables, sections):
    transfers, admissions, poe, pharmacy, prescriptions, emar = tables

    # 1. Transfers
    for ts in sections.get("Transfers", []):
        hits = set()
        for sid in candidate_sids:
            for _etype, intime, hadm in transfers.get(sid, []):
                if intime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 2. EDstays
    for ts in sections.get("EDstays", []):
        hits = set()
        for sid in candidate_sids:
            for etype, intime, hadm in transfers.get(sid, []):
                if etype == "ED" and intime == ts:
                    hits.add((sid, hadm))
            for _admittime, edregtime, hadm in admissions.get(sid, []):
                if edregtime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 3. Admissions
    for ts in sections.get("Admissions", []):
        hits = set()
        for sid in candidate_sids:
            for admittime, _edregtime, hadm in admissions.get(sid, []):
                if admittime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 4. POE
    for ts in sections.get("Provider Order Entry", []):
        hits = set()
        for sid in candidate_sids:
            for ordertime, hadm in poe.get(sid, []):
                if ordertime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 5. Pharmacy
    for ts in sections.get("Pharmacy", []):
        hits = set()
        for sid in candidate_sids:
            for entertime, hadm in pharmacy.get(sid, []):
                if entertime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 6. Prescriptions
    for ts in sections.get("Prescriptions", []):
        hits = set()
        for sid in candidate_sids:
            for starttime, hadm in prescriptions.get(sid, []):
                if starttime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 7. EMAR
    for ts in sections.get("Electronic Medicine Administration Record", []):
        hits = set()
        for sid in candidate_sids:
            for charttime, hadm in emar.get(sid, []):
                if charttime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # 8. Fallback: first few timestamps against transfers.intime
    all_ts = []
    for _sec, ts_list in sections.items():
        all_ts.extend(ts_list)
    for ts in all_ts[:5]:
        hits = set()
        for sid in candidate_sids:
            for _etype, intime, hadm in transfers.get(sid, []):
                if intime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    return None


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--src",
        default="/home/efs/zlt/autoehr/data/EHR-Ins-Reasoning/ehr_ins_reasoning_sample300_per_task.jsonl",
        help="Input JSONL",
    )
    ap.add_argument(
        "--mimic-dir",
        default="/home/efs/zlt/autoehr/data/MIMIC-IV/mimic_iv",
        help="MIMIC-IV root (contains hosp/)",
    )
    ap.add_argument(
        "--out-matched",
        default=None,
        help="Output JSONL with full records + subject_id/hadm_id "
             "(default: <src_stem>_matched.jsonl)",
    )
    ap.add_argument(
        "--out-ids",
        default=None,
        help="Output JSONL with just {idx, task, subject_id, hadm_id} "
             "(default: <src_stem>_ids.jsonl)",
    )
    ap.add_argument(
        "--drop-unmatched",
        action="store_true",
        help="Drop entries with no unique match (mirrors EHR-Bench matcher). "
             "Default: keep with nulls.",
    )
    args = ap.parse_args()

    src = Path(args.src)
    mimic_dir = Path(args.mimic_dir)
    stem = src.with_suffix("").name  # strip .jsonl
    out_matched = Path(args.out_matched) if args.out_matched else src.parent / f"{stem}_matched.jsonl"
    out_ids = Path(args.out_ids) if args.out_ids else src.parent / f"{stem}_ids.jsonl"

    print("Loading MIMIC-IV reference tables...")
    patients = load_patients(mimic_dir)
    transfers = load_transfers(mimic_dir)
    admissions = load_admissions(mimic_dir)
    poe = load_poe(mimic_dir)
    pharmacy = load_pharmacy(mimic_dir)
    prescriptions = load_prescriptions(mimic_dir)
    emar = load_emar(mimic_dir)
    tables = (transfers, admissions, poe, pharmacy, prescriptions, emar)
    print("Done loading.\n")

    print(f"Processing {src}")
    n_total = 0
    n_no_demo = 0
    n_no_match = 0
    n_matched = 0
    n_hadm = 0

    with open(src) as fin, open(out_matched, "w") as f_full, open(out_ids, "w") as f_ids:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_total += 1
            entry = json.loads(line)
            inp = entry["input"]
            task_info = entry.get("task_info", {})
            task = task_info.get("task", "")
            idx = entry.get("idx", task_info.get("idx"))

            demo = extract_demographics(inp)
            candidate_sids = patients.get(demo, []) if demo else []
            result = None
            if demo is None:
                n_no_demo += 1
            elif not candidate_sids:
                n_no_match += 1
            else:
                sections = extract_section_times(inp)
                result = try_match(candidate_sids, tables, sections)
                if result is None:
                    n_no_match += 1

            if result is not None:
                sid, hadm = result
                sid_out = int(sid) if sid else None
                hadm_out = int(hadm) if hadm else None
                n_matched += 1
                if hadm_out is not None:
                    n_hadm += 1
            else:
                sid_out = None
                hadm_out = None

            drop = args.drop_unmatched and result is None
            if not drop:
                entry["subject_id"] = sid_out
                entry["hadm_id"] = hadm_out
                f_full.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f_ids.write(
                    json.dumps(
                        {"idx": idx, "task": task, "subject_id": sid_out, "hadm_id": hadm_out},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            if n_total % 1000 == 0:
                print(f"  ... {n_total}  matched so far: {n_matched}")

    print()
    print(f"Total:           {n_total}")
    print(f"Matched:         {n_matched} ({n_matched/n_total*100:.1f}%)")
    print(f"  with hadm_id:  {n_hadm}")
    print(f"No demographics: {n_no_demo}")
    print(f"No unique match: {n_no_match}")
    print(f"Full output:     {out_matched}")
    print(f"IDs-only output: {out_ids}")


if __name__ == "__main__":
    main()
