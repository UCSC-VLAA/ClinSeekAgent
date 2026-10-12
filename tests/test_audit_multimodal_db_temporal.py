import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent / "scripts" / "data_build"):
    if (candidate / "audit_multimodal_db_temporal.py").exists():
        sys.path.insert(0, str(candidate))
        break

from audit_multimodal_db_temporal import audit_one


class AuditMultimodalDbTemporalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "database").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_db(self, subject_id=1):
        path = self.root / "database" / f"patient_{subject_id}.db"
        return path, sqlite3.connect(path)

    def test_physical_future_event_is_storage_only_when_loader_filters_it(self):
        path, conn = self.make_db()
        conn.execute(
            "CREATE TABLE events "
            "(subject_id, charttime, itemid, value)"
        )
        conn.executemany(
            "INSERT INTO events VALUES (?,?,?,?)",
            [
                (1, "2020-01-01 09:00:00", 1, "past"),
                (1, "2020-01-01 11:00:00", 2, "future"),
            ],
        )
        conn.commit()
        conn.close()
        result = audit_one(
            {
                "qid": "q1",
                "source_benchmark": "medmod",
                "task": "medmod_decompensation",
                "subject_id": 1,
                "prediction_time": "2020-01-01 10:00:00",
            },
            self.root,
        )
        self.assertEqual(
            result["physical_future_by_table_column"]["events.charttime"], 1
        )
        self.assertFalse(result["runtime_secondary_future_by_table_column"])
        self.assertNotIn(
            "CONFIRMED_LEGACY_RUNTIME_TEMPORAL_EXPOSURE",
            result["policy_findings"],
        )

    def test_secondary_discharge_time_is_runtime_exposure(self):
        path, conn = self.make_db()
        conn.execute(
            "CREATE TABLE admissions "
            "(subject_id, admittime, dischtime, discharge_location)"
        )
        conn.execute(
            "INSERT INTO admissions VALUES (?,?,?,?)",
            (
                1,
                "2020-01-01 08:00:00",
                "2020-01-03 12:00:00",
                "HOME",
            ),
        )
        conn.commit()
        conn.close()
        result = audit_one(
            {
                "qid": "q2",
                "source_benchmark": "ehrxqa",
                "task": "ehrxqa_image",
                "subject_id": 1,
                "prediction_time": "2020-01-02 00:00:00",
            },
            self.root,
        )
        self.assertEqual(
            result["runtime_secondary_future_by_table_column"][
                "admissions.dischtime"
            ],
            1,
        )
        self.assertEqual(
            result["runtime_future_outcome_by_table_column"][
                "admissions.discharge_location"
            ],
            1,
        )
        self.assertIn(
            "CONFIRMED_LEGACY_RUNTIME_TEMPORAL_EXPOSURE",
            result["policy_findings"],
        )

    def test_linked_cxr_after_cutoff_and_unlinked_visible_row(self):
        path, conn = self.make_db()
        conn.execute(
            "CREATE TABLE tb_cxr "
            "(subject_id, study_id, studydatetime)"
        )
        conn.executemany(
            "INSERT INTO tb_cxr VALUES (?,?,?)",
            [
                (1, 10, "2020-01-01 09:00:00"),
                (1, 20, "2020-01-01 11:00:00"),
            ],
        )
        conn.commit()
        conn.close()
        result = audit_one(
            {
                "qid": "q3",
                "source_benchmark": "medmod",
                "task": "medmod_radiology",
                "subject_id": 1,
                "prediction_time": "2020-01-01 10:00:00",
                "image_paths": ["/x/s20/image.jpg"],
            },
            self.root,
        )
        self.assertEqual(
            result["linked_study_time_relation"]["20"], "after"
        )
        self.assertEqual(result["visible_unlinked_cxr_rows"], 1)
        self.assertIn("DIRECT_CXR_AFTER_CUTOFF", result["policy_findings"])
        self.assertIn(
            "LINKED_STUDIES_ONLY_POLICY_VIOLATION",
            result["policy_findings"],
        )

    def test_future_dod_survives_legacy_unbounded_patients_table(self):
        path, conn = self.make_db()
        conn.execute("CREATE TABLE patients (subject_id, gender, dod)")
        conn.execute("INSERT INTO patients VALUES (1, 'F', '2020-01-02')")
        conn.commit()
        conn.close()
        result = audit_one(
            {
                "qid": "q4",
                "source_benchmark": "ehrxqa",
                "task": "ehrxqa_table",
                "subject_id": 1,
                "prediction_time": "2020-01-02 23:59:00",
            },
            self.root,
        )
        self.assertEqual(
            result["runtime_future_outcome_by_table_column"]["patients.dod"], 1
        )
        self.assertIn(
            "CONFIRMED_LEGACY_RUNTIME_TEMPORAL_EXPOSURE",
            result["policy_findings"],
        )

    def test_synthetic_diagnosis_time_uses_parent_discharge_availability(self):
        path, conn = self.make_db()
        conn.execute(
            "CREATE TABLE admissions "
            "(subject_id, hadm_id, admittime, dischtime)"
        )
        conn.execute(
            "CREATE TABLE diagnoses_icd "
            "(subject_id, hadm_id, icd_code, charttime)"
        )
        conn.execute(
            "INSERT INTO admissions VALUES "
            "(1, 10, '2020-01-01 08:00:00', '2020-01-03 12:00:00')"
        )
        conn.execute(
            "INSERT INTO diagnoses_icd VALUES "
            "(1, 10, 'Z99', '2020-01-01 08:00:00')"
        )
        conn.commit()
        conn.close()
        result = audit_one(
            {
                "qid": "q5",
                "source_benchmark": "ehrxqa",
                "task": "ehrxqa_image",
                "subject_id": 1,
                "prediction_time": "2020-01-02 00:00:00",
            },
            self.root,
        )
        self.assertEqual(
            result["runtime_future_outcome_by_table_column"][
                "diagnoses_icd.synthetic_admission_time"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
