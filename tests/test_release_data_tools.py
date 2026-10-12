"""Schema helpers shipped with the release: input restoration, categories."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "clinseekagent" / "scoring"))

from categories import (  # noqa: E402
    category_of,
    category_order,
    group_scores,
    load_taxonomy,
    paired_bootstrap_ci,
)
from run_text import is_ehr_bench_record  # noqa: E402


class RestoreEhrBenchInputs(unittest.TestCase):
    def test_restores_input_and_question(self):
        upstream = {
            "idx": 7,
            "instruction": "Given the events, please predict whether the patient dies",
            "input": "## Patient Demographics [None]\n- Gender: F\n\n",
            "output": "no",
            "candidates": ["yes", "no"],
            "task_info": {"metric": "aucroc", "label": "no"},
        }
        row = {
            "qid": "ehr_bench_risk_prediction_7", "source_file": "ehr_bench_risk_prediction.jsonl",
            "source_idx": 7, "task": "Inpatient_Mortality", "task_type": "risk_prediction",
            "label": "no", "candidates": ["yes", "no"],
            "prediction_time": "2150-01-01 10:00:01", "latest_event_time": "2150-01-01 10:00:00",
            "instruction": "Please predict whether the patient dies",
            "task_info": {"metric": "aucroc"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "ehr_bench_risk_prediction.jsonl").write_text(json.dumps(upstream) + "\n")
            (tmp / "manifest.json").write_text(json.dumps([row]))
            out = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts/data_build/restore_ehr_bench_inputs.py"),
                 "--manifest", str(tmp / "manifest.json"), "--ehr-bench-dir", str(tmp),
                 "--output", str(tmp / "restored.json")],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            restored = json.loads((tmp / "restored.json").read_text())[0]
        self.assertEqual(restored["input"], upstream["input"])
        q = restored["question"]
        self.assertTrue(q.startswith("<task_instruction>\nGiven the events"))
        self.assertIn("exactly one second after the latest observed event at "
                      "2150-01-01 10:00:00", q)
        self.assertIn("Return only the final answer text as a single line.", q)
        self.assertIn("<patient_timeline>\n## Patient Demographics [None]\n- Gender: F\n"
                      "</patient_timeline>", q)
        self.assertTrue(q.endswith('<candidate_answers>\n["yes", "no"]\n</candidate_answers>'))


class Categories(unittest.TestCase):
    def test_taxonomy_matches_paper(self):
        tax = load_taxonomy()
        self.assertEqual(len(category_order("text")), 13)
        self.assertEqual(len(category_order("mm")), 6)
        self.assertEqual(sum(tax["text"]["row_counts"].values()), 1800)
        self.assertEqual(sum(tax["mm"]["row_counts"].values()), 989)

    def test_category_resolution(self):
        self.assertEqual(category_of("text", {"task": "labevents"}),
                         "Laboratory & microbiology")
        self.assertEqual(category_of("mm", {"task": "medmod_phenotyping"}), "Phenotype")
        self.assertEqual(category_of("text", {"task": "x", "category": "Orders"}), "Orders")
        grouped = group_scores("text", [{"task": "poe", "avg_f1": 1.0},
                                        {"task": "nope", "avg_f1": 0.0}], "avg_f1")
        self.assertEqual(grouped["Orders"], {"count": 1, "f1": 1.0})
        self.assertIn("uncategorized", grouped)

    def test_paired_bootstrap(self):
        mean, lo, hi = paired_bootstrap_ci([1.0] * 50 + [0.0] * 50, n_boot=500)
        self.assertAlmostEqual(mean, 0.5)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)

    def test_public_manifest_rows_are_ehr_bench(self):
        row = {"qid": "x", "source_file": "ehr_bench_decision_making.jsonl"}
        self.assertTrue(is_ehr_bench_record(row, "ClinSeek-Bench_text.json"))


if __name__ == "__main__":
    unittest.main()
