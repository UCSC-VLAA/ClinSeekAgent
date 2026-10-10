from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLINSEEKAGENT_ROOT = REPO_ROOT / "clinseekagent"
if str(CLINSEEKAGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLINSEEKAGENT_ROOT))

from run_text import is_ehr_bench_record, resolve_question


class EHRBenchPromptResolutionTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "qid": "ehr_bench_decision_making_9932",
            "instruction": "Predict the next prescription.",
            "prediction_time": "2196-08-08 07:40:01",
            "subject_id": 18520744,
            "candidates": ["sodium bicarbonate"],
            "question": (
                "All patient events needed for this task are already included "
                "below. No EHR database lookup is needed."
            ),
            "task_info": {
                "source_file": "ehr_bench_decision_making.jsonl",
            },
        }

    def test_recognizes_ehr_bench_qid_after_subset_is_renamed(self):
        self.assertTrue(
            is_ehr_bench_record(
                self.record,
                "/tmp/subset89_excluding_over100_qid723.json",
            )
        )

    def test_renamed_subset_uses_agentic_prompt_not_embedded_question(self):
        prompt = resolve_question(
            self.record,
            "/tmp/subset89_excluding_over100_qid723.json",
        )

        self.assertIn("Analyze the patient's EHR records", prompt)
        self.assertIn("Submit your final answer through `ehr.finish`", prompt)
        self.assertNotIn("No EHR database lookup is needed", prompt)

    def test_non_ehr_bench_record_still_uses_explicit_question(self):
        record = {
            "qid": "custom_1",
            "question": "Keep this exact question.",
        }

        self.assertFalse(is_ehr_bench_record(record, "/tmp/custom.json"))
        self.assertEqual(
            "Keep this exact question.",
            resolve_question(record, "/tmp/custom.json"),
        )

    def test_readmission_prompt_declares_episode_complete_boundary(self):
        record = dict(self.record)
        record.update(
            {
                "task": "Readmission_30day",
                "hadm_id": 12345,
                "instruction": "Predict 30-day readmission.",
            }
        )

        prompt = resolve_question(
            record,
            "/tmp/ehr_bench_sampled_40_per_task.json",
        )

        self.assertIn("Readmission episode policy", prompt)
        self.assertIn("Index Admission ID: 12345", prompt)
        self.assertIn("withholds every event after its actual discharge", prompt)


if __name__ == "__main__":
    unittest.main()
