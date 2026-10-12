from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path

import pandas as pd

from agentlite.commons import EHRManager
from agentlite.commons.multimodal_temporal_policy import (
    detect_multimodal_schema,
)


class EHRManagerTemporalLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        database = self.root / "database"
        database.mkdir()
        db_path = database / "patient_1.db"

        patients = pd.DataFrame(
            [{"subject_id": "1", "anchor_year": "2200", "dod": None}]
        )
        admissions = pd.DataFrame(
            [
                {
                    "subject_id": "1",
                    "hadm_id": "10",
                    "admittime": "2200-01-01 08:00:00",
                    "dischtime": "2200-01-03 08:00:00",
                    "discharge_location": "HOME",
                    "hospital_expire_flag": "0",
                    "text": "Chief Complaint: fever",
                }
            ]
        )
        labevents = pd.DataFrame(
            [
                {
                    "labevent_id": "1",
                    "subject_id": "1",
                    "charttime": "2200-01-02 08:00:00",
                    "storetime": "2200-01-02 09:00:00",
                    "value": "early",
                },
                {
                    "labevent_id": "2",
                    "subject_id": "1",
                    "charttime": "2200-01-02 09:00:00",
                    "storetime": "2200-01-02 11:00:00",
                    "value": "late",
                },
            ]
        )
        with sqlite3.connect(db_path) as connection:
            patients.to_sql("patients", connection, index=False)
            admissions.to_sql("admissions", connection, index=False)
            labevents.to_sql("labevents", connection, index=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _manager(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return EHRManager(
                str(self.root),
                temporal_policy_mode="strict_mimic_iv",
            )

    def test_two_cutoffs_build_independent_views(self):
        manager = self._manager()

        manager.load_ehr_for_sample("1", "2200-01-02 10:00:00")
        early_view = {
            table: frame.copy(deep=True)
            for table, frame in manager.ehr_data.items()
        }
        early_report = json.loads(json.dumps(manager.last_temporal_report))

        manager.load_ehr_for_sample("1", "2200-01-02 12:00:00")

        self.assertEqual(
            ["1"],
            early_view["labevents"]["labevent_id"].tolist(),
        )
        self.assertEqual(
            ["1", "2"],
            manager.ehr_data["labevents"]["labevent_id"].tolist(),
        )
        self.assertEqual(
            1,
            early_report["tables"]["labevents"]["output_rows"],
        )
        self.assertEqual(
            2,
            manager.last_temporal_report["tables"]["labevents"]["output_rows"],
        )

    def test_loader_drops_copied_discharge_text_and_serializes_null(self):
        manager = self._manager()

        load_log = manager.load_ehr_for_sample(
            "1",
            "2200-01-02 10:00:00",
        )
        admission = manager.ehr_data["admissions"].iloc[0]
        serialized = manager.get_ehr_data_json()["admissions"]

        self.assertIn("strict-ehr-runtime-v8", load_log)
        self.assertNotIn("text", manager.ehr_data["admissions"].columns)
        self.assertNotIn("text", serialized)
        self.assertTrue(pd.isna(admission["dischtime"]))
        self.assertIsNone(serialized["dischtime"][0])
        self.assertIsNone(serialized["discharge_location"][0])
        self.assertIsNone(serialized["hospital_expire_flag"][0])
        self.assertEqual(
            ["text"],
            manager.last_temporal_report["tables"]["admissions"][
                "dropped_columns"
            ],
        )

    def test_auto_mode_detects_mimic_iv(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            manager = EHRManager(str(self.root), temporal_policy_mode="auto")

        manager.load_ehr_for_sample("1", "2200-01-02 10:00:00")

        self.assertEqual("strict-ehr-runtime-v8", manager.temporal_policy_version)

    def test_loader_passes_trusted_readmission_episode_metadata(self):
        discharge = pd.DataFrame(
            [
                {
                    "note_id": "index-note",
                    "subject_id": "1",
                    "hadm_id": "10",
                    "note_type": "DS",
                    "note_seq": "1",
                    "charttime": "2200-01-03 00:00:00",
                    "storetime": "2200-01-03 12:00:00",
                    "text": "index discharge summary",
                }
            ]
        )
        db_path = self.root / "database" / "patient_1.db"
        with sqlite3.connect(db_path) as connection:
            discharge.to_sql("discharge", connection, index=False)

        manager = self._manager()
        manager.load_ehr_for_sample(
            "1",
            "2200-01-03 00:00:01",
            task="Readmission_30day",
            hadm_id="10",
        )

        self.assertEqual(
            "2200-01-03 08:00:01",
            manager.last_prediction_time,
        )
        self.assertEqual(
            "readmission_index_episode",
            manager.last_temporal_report["boundary_mode"],
        )
        self.assertEqual(
            ["index-note"],
            manager.ehr_data["discharge"]["note_id"].tolist(),
        )

    def test_loader_passes_trusted_target_code_metadata(self):
        manager = self._manager()
        manager.load_ehr_for_sample(
            "1",
            "2200-01-02 10:00:00",
            task="diagnoses_ccs",
            target_hadm_id="10",
        )

        self.assertEqual(
            "10",
            manager.last_temporal_report["target_hadm_id"],
        )
        self.assertIsNone(
            manager.last_temporal_report["target_stay_id"],
        )

    def test_full_mimic_schema_is_not_misdetected_as_ehrxqa(self):
        source = {
            "patients": pd.DataFrame(
                [{"subject_id": "1", "anchor_year": "2200"}]
            ),
            "admissions": pd.DataFrame(
                [{"subject_id": "1", "admittime": "2200-01-01"}]
            ),
            "transfers": pd.DataFrame(
                [{"subject_id": "1", "intime": "2200-01-01"}]
            ),
        }

        self.assertIsNone(detect_multimodal_schema(source))


if __name__ == "__main__":
    unittest.main()
