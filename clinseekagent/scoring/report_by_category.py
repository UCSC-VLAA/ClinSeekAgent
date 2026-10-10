#!/usr/bin/env python3
"""Per-category F1 for one or more scored runs, with paired comparisons.

Reads the per-question outputs of the scorers:

* text: `<scores_dir>/per_question.jsonl` from `score_text.py --output-dir`
  (per-question `avg_f1`);
* mm:   `<scores_dir>/scored.jsonl` from `score_multimodal.py`
  (per-question unified `f1`: the judge's 0/1 verdict for single-label rows,
  set F1 for multi-label rows).

Each `--run NAME=SCORES_DIR` becomes one column. With `--compare A B`, the
report adds the paired difference B - A per category and overall, with a
paired-bootstrap 95% CI over questions (4,000 resamples), as in the paper.

    python clinseekagent/scoring/report_by_category.py --bench text \\
        --run curated=outputs/text_recent/scores \\
        --run agentic=outputs/text_agentic/scores \\
        --compare curated agentic --markdown
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from categories import category_of, category_order, mean, paired_bootstrap_ci  # noqa: E402

SCORE_FILE = {"text": ("per_question.jsonl", "avg_f1"),
              "mm": ("scored.jsonl", "f1")}


def load_run(bench: str, scores_dir: Path) -> Dict[str, Tuple[str, float]]:
    """`qid -> (category, score)` for one scored run."""
    fname, key = SCORE_FILE[bench]
    path = scores_dir / fname if scores_dir.is_dir() else scores_dir
    out: Dict[str, Tuple[str, float]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            qid = str(row.get("qid"))
            out[qid] = (category_of(bench, row) or "uncategorized",
                        float(row.get(key) or 0.0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bench", choices=["text", "mm"], required=True)
    ap.add_argument("--run", action="append", required=True, metavar="NAME=DIR",
                    help="scores directory (or file) of one run; repeatable")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "OTHER"),
                    help="report OTHER - BASE with a paired bootstrap CI")
    ap.add_argument("--n-boot", type=int, default=4000)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    runs: Dict[str, Dict[str, Tuple[str, float]]] = {}
    for spec in args.run:
        name, _, path = spec.partition("=")
        if not path:
            ap.error(f"--run expects NAME=DIR, got {spec!r}")
        runs[name] = load_run(args.bench, Path(path))

    cats = category_order(args.bench)
    seen = {c for r in runs.values() for c, _ in r.values()}
    cats += sorted(seen - set(cats))
    report: Dict[str, Dict[str, object]] = {}
    for cat in cats + ["Overall"]:
        entry: Dict[str, object] = {}
        for name, rows in runs.items():
            vals = [s for c, s in rows.values() if cat == "Overall" or c == cat]
            if vals:
                entry[name] = {"n": len(vals), "f1": 100 * mean(vals)}
        if args.compare:
            base, other = (runs[n] for n in args.compare)
            shared = [q for q in base if q in other
                      and (cat == "Overall" or base[q][0] == cat)]
            deltas = [100 * (other[q][1] - base[q][1]) for q in shared]
            if deltas:
                d, lo, hi = paired_bootstrap_ci(deltas, n_boot=args.n_boot)
                entry["delta"] = {"n_paired": len(deltas), "mean": d,
                                  "ci95": [lo, hi],
                                  "significant": lo > 0 or hi < 0}
        if entry:
            report[cat] = entry

    names = list(runs)
    if args.markdown:
        head = "| category | " + " | ".join(names)
        sep = "|---|" + "---:|" * len(names)
        if args.compare:
            head += f" | Δ ({args.compare[1]} − {args.compare[0]}) | 95% CI"
            sep += "---:|---|"
        print(head + " |\n" + sep)
    for cat, entry in report.items():
        cells = []
        for name in names:
            v = entry.get(name)
            cells.append(f"{v['f1']:.1f} (n={v['n']})" if v else "—")
        if args.compare:
            d = entry.get("delta")
            if d:
                mark = "*" if d["significant"] else ""
                cells += [f"{d['mean']:+.1f}{mark}",
                          f"[{d['ci95'][0]:+.1f}, {d['ci95'][1]:+.1f}]"]
            else:
                cells += ["—", "—"]
        if args.markdown:
            print(f"| {cat} | " + " | ".join(cells) + " |")
        else:
            print(f"{cat:28s} " + "  ".join(f"{c:>18s}" for c in cells))
    if args.compare:
        print("\n* = 95% paired-bootstrap CI excludes zero")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
