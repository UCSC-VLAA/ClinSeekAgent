"""
Reverse-match EHR-Bench entries to MIMIC-IV subject_id / hadm_id.

Strategy (in order of priority):
1. Transfers ED intime  (## Transfers [...] with Eventtype: ED)
2. EDstays time         (## EDstays [...])
3. Admissions edregtime (## Transfers / ## Admissions timestamps vs admissions.edregtime)
4. Admissions admittime
5. POE ordertime        (## Provider Order Entry [...])
6. Pharmacy entertime   (## Pharmacy [...])
7. Prescriptions start  (## Prescriptions [...])

For each entry we narrow candidates by (gender, anchor_age), then try timestamps
until we get exactly 1 match.  Entries with 0 or >1 matches are dropped.
"""

import csv
import json
import re
import sys
from collections import defaultdict

# Force unbuffered output
print = lambda *a, **kw: (sys.stdout.write(" ".join(str(x) for x in a) + kw.get("end", "\n")), sys.stdout.flush())

MIMIC_DIR = "data/MIMIC-IV/mimic_iv"
BENCH_DIR = "data/EHR-Bench"

INPUT_FILES = [
    ("ehr_bench_decision_making_subset_format.json",
     "ehr_bench_decision_making_subset_format_matched.json"),
    ("ehr_bench_risk_prediction_subset_format.json",
     "ehr_bench_risk_prediction_subset_format_matched.json"),
]

# ── Load MIMIC-IV reference tables ───────────────────────────────────────────

def load_patients():
    """Return dict: (gender, anchor_age) -> [subject_id, ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/patients.csv") as f:
        for row in csv.DictReader(f):
            out[(row["gender"], row["anchor_age"])].append(row["subject_id"])
    print(f"  patients: {sum(len(v) for v in out.values())} rows")
    return out


def load_transfers():
    """Return dict: subject_id -> [(eventtype, intime, hadm_id), ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/transfers.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["eventtype"], row["intime"], row["hadm_id"])
            )
    print(f"  transfers: {sum(len(v) for v in out.values())} rows")
    return out


def load_admissions():
    """Return dict: subject_id -> [(admittime, edregtime, hadm_id), ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/admissions.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["admittime"], row.get("edregtime", ""), row["hadm_id"])
            )
    print(f"  admissions: {sum(len(v) for v in out.values())} rows")
    return out


def load_poe():
    """Return dict: subject_id -> [(ordertime, hadm_id), ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/poe.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["ordertime"], row["hadm_id"])
            )
    print(f"  poe: {sum(len(v) for v in out.values())} rows")
    return out


def load_pharmacy():
    """Return dict: subject_id -> [(entertime, hadm_id), ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/pharmacy.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["entertime"], row["hadm_id"])
            )
    print(f"  pharmacy: {sum(len(v) for v in out.values())} rows")
    return out


def load_prescriptions():
    """Return dict: subject_id -> [(starttime, hadm_id), ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/prescriptions.csv") as f:
        for row in csv.DictReader(f):
            out[row["subject_id"]].append(
                (row["starttime"], row["hadm_id"])
            )
    print(f"  prescriptions: {sum(len(v) for v in out.values())} rows")
    return out



def load_emar():
    """Return dict: subject_id -> [(charttime, hadm_id), ...]"""
    out = defaultdict(list)
    with open(f"{MIMIC_DIR}/hosp/emar.csv") as f:
        for row in csv.DictReader(f):
            if row["charttime"]:
                out[row["subject_id"]].append(
                    (row["charttime"], row["hadm_id"])
                )
    print(f"  emar: {sum(len(v) for v in out.values())} rows")
    return out


# ── Extraction helpers ───────────────────────────────────────────────────────

TS_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

def extract_demographics(inp: str):
    """Return (gender, anchor_age) or None."""
    age = re.search(r"Anchor_Age:\s*(\d+)", inp)
    gen = re.search(r"Gender:\s*(\w)", inp)
    if age and gen:
        return (gen.group(1), age.group(1))
    return None


def extract_section_times(inp: str):
    """Return dict of section_name -> [timestamps]."""
    sections = defaultdict(list)
    for m in re.finditer(r"## ([\w\s]+?)\s*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", inp):
        sections[m.group(1).strip()].append(m.group(2))
    return sections


# ── Matching logic ───────────────────────────────────────────────────────────

def try_match(candidate_sids, tables, sections):
    """Try multiple strategies; return (subject_id, hadm_id) or None."""

    transfers, admissions, poe, pharmacy, prescriptions, emar = tables

    # Strategy 1: Transfers ED intime
    for ts in sections.get("Transfers", []):
        hits = set()
        for sid in candidate_sids:
            for etype, intime, hadm in transfers.get(sid, []):
                if intime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 2: EDstays time -> match transfers ED intime or admissions edregtime
    for ts in sections.get("EDstays", []):
        hits = set()
        for sid in candidate_sids:
            for etype, intime, hadm in transfers.get(sid, []):
                if etype == "ED" and intime == ts:
                    hits.add((sid, hadm))
            for admittime, edregtime, hadm in admissions.get(sid, []):
                if edregtime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 3: Admissions section time -> admissions.admittime
    for ts in sections.get("Admissions", []):
        hits = set()
        for sid in candidate_sids:
            for admittime, edregtime, hadm in admissions.get(sid, []):
                if admittime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 4: POE ordertime
    for ts in sections.get("Provider Order Entry", []):
        hits = set()
        for sid in candidate_sids:
            for ordertime, hadm in poe.get(sid, []):
                if ordertime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 5: Pharmacy entertime
    for ts in sections.get("Pharmacy", []):
        hits = set()
        for sid in candidate_sids:
            for entertime, hadm in pharmacy.get(sid, []):
                if entertime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 6: Prescriptions starttime
    for ts in sections.get("Prescriptions", []):
        hits = set()
        for sid in candidate_sids:
            for starttime, hadm in prescriptions.get(sid, []):
                if starttime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 7: EMAR charttime
    for ts in sections.get("Electronic Medicine Administration Record", []):
        hits = set()
        for sid in candidate_sids:
            for charttime, hadm in emar.get(sid, []):
                if charttime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    # Strategy 8: any first timestamp from any section -> transfers intime
    all_ts = []
    for sec, ts_list in sections.items():
        all_ts.extend(ts_list)
    for ts in all_ts[:5]:  # try first few
        hits = set()
        for sid in candidate_sids:
            for etype, intime, hadm in transfers.get(sid, []):
                if intime == ts:
                    hits.add((sid, hadm))
        if len(hits) == 1:
            return hits.pop()

    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Loading MIMIC-IV reference tables...")
    patients = load_patients()
    transfers = load_transfers()
    admissions = load_admissions()
    poe = load_poe()
    pharmacy = load_pharmacy()
    prescriptions = load_prescriptions()
    emar = load_emar()
    tables = (transfers, admissions, poe, pharmacy, prescriptions, emar)
    print("Done loading.\n")

    for in_file, out_file in INPUT_FILES:
        in_path = f"{BENCH_DIR}/{in_file}"
        out_path = f"{BENCH_DIR}/{out_file}"

        print(f"Processing {in_file} ...")
        with open(in_path) as f:
            data = json.load(f)

        matched_entries = []
        no_demo = 0
        no_match = 0

        for i, entry in enumerate(data):
            inp = entry["input"]
            demo = extract_demographics(inp)
            if demo is None:
                no_demo += 1
                continue

            candidate_sids = patients.get(demo, [])
            if not candidate_sids:
                no_match += 1
                continue

            sections = extract_section_times(inp)
            result = try_match(candidate_sids, tables, sections)

            if result is None:
                no_match += 1
                continue

            sid, hadm = result
            entry["subject_id"] = int(sid) if sid else None
            entry["hadm_id"] = int(hadm) if hadm else None
            matched_entries.append(entry)

            if (i + 1) % 2000 == 0:
                print(f"  ... {i+1}/{len(data)}  matched so far: {len(matched_entries)}")

        with open(out_path, "w") as f:
            json.dump(matched_entries, f, indent=2, ensure_ascii=False)

        total = len(data)
        ok = len(matched_entries)
        print(f"  Total: {total}")
        print(f"  Matched: {ok} ({ok/total*100:.1f}%)")
        print(f"  No demographics: {no_demo}")
        print(f"  No match: {no_match}")
        print(f"  Saved to: {out_path}\n")


if __name__ == "__main__":
    main()
