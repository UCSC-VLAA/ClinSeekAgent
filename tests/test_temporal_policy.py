from __future__ import annotations

import unittest

import pandas as pd

from agentlite.commons.temporal_policy import (
    CHILD_POLICIES,
    DROP_TABLES,
    MIMIC_IV_PATIENT_TABLES,
    POLICY_VERSION,
    TABLE_POLICIES,
    TemporalPolicyError,
    build_runtime_view,
)


class TemporalPolicyTests(unittest.TestCase):
    cutoff = pd.Timestamp("2200-01-02 10:00:00")

    def test_all_34_mimic_iv_patient_tables_have_explicit_policies(self):
        declared = set(TABLE_POLICIES) | set(DROP_TABLES) | set(CHILD_POLICIES)

        self.assertEqual(34, len(MIMIC_IV_PATIENT_TABLES))
        self.assertEqual(set(), MIMIC_IV_PATIENT_TABLES - declared)

    def test_requires_event_and_store_time_before_cutoff(self):
        source = {
            "labevents": pd.DataFrame(
                [
                    {
                        "labevent_id": "1",
                        "subject_id": "1",
                        "charttime": "2200-01-02 08:00:00",
                        "storetime": "2200-01-02 09:00:00",
                        "value": "safe",
                    },
                    {
                        "labevent_id": "2",
                        "subject_id": "1",
                        "charttime": "2200-01-02 08:30:00",
                        "storetime": "2200-01-02 10:30:00",
                        "value": "late result",
                    },
                    {
                        "labevent_id": "3",
                        "subject_id": "1",
                        "charttime": "2200-01-02 10:00:00",
                        "storetime": "2200-01-02 09:30:00",
                        "value": "at cutoff",
                    },
                    {
                        "labevent_id": "4",
                        "subject_id": "1",
                        "charttime": "2200-01-02 09:00:00",
                        "storetime": "",
                        "value": "unknown availability",
                    },
                ]
            )
        }

        result = build_runtime_view(source, self.cutoff)

        self.assertEqual(POLICY_VERSION, result.policy_version)
        self.assertEqual(["1"], result.tables["labevents"]["labevent_id"].tolist())
        self.assertEqual(4, result.reports["labevents"].input_rows)
        self.assertEqual(1, result.reports["labevents"].output_rows)

    def test_date_only_values_are_visible_only_after_the_day(self):
        source = {
            "omr": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "chartdate": "2200-01-01",
                        "seq_num": "1",
                        "result_name": "Weight",
                        "result_value": "70",
                    },
                    {
                        "subject_id": "1",
                        "chartdate": "2200-01-02",
                        "seq_num": "2",
                        "result_name": "Weight",
                        "result_value": "71",
                    },
                    {
                        "subject_id": "1",
                        "chartdate": "not-a-date",
                        "seq_num": "3",
                        "result_name": "Weight",
                        "result_value": "72",
                    },
                ]
            )
        }

        result = build_runtime_view(source, self.cutoff)

        self.assertEqual(["1"], result.tables["omr"]["seq_num"].tolist())

    def test_microbiology_prefers_storetime_and_falls_back_to_storedate(self):
        source = {
            "microbiologyevents": pd.DataFrame(
                [
                    {
                        "microevent_id": "precise-before-cutoff",
                        "subject_id": "1",
                        "charttime": "2200-01-02 08:00:00",
                        "chartdate": "2200-01-02",
                        "storetime": "2200-01-02 09:00:00",
                        "storedate": "2200-01-02",
                    },
                    {
                        "microevent_id": "precise-at-cutoff",
                        "subject_id": "1",
                        "charttime": "2200-01-02 08:00:00",
                        "chartdate": "2200-01-02",
                        "storetime": "2200-01-02 10:00:00",
                        "storedate": "2200-01-01",
                    },
                    {
                        "microevent_id": "fallback-prior-day",
                        "subject_id": "1",
                        "charttime": "2200-01-01 08:00:00",
                        "chartdate": "2200-01-01",
                        "storetime": None,
                        "storedate": "2200-01-01",
                    },
                    {
                        "microevent_id": "fallback-same-day",
                        "subject_id": "1",
                        "charttime": "2200-01-02 08:00:00",
                        "chartdate": "2200-01-02",
                        "storetime": None,
                        "storedate": "2200-01-02",
                    },
                ]
            )
        }

        result = build_runtime_view(source, self.cutoff)

        self.assertEqual(
            ["precise-before-cutoff", "fallback-prior-day"],
            result.tables["microbiologyevents"][
                "microevent_id"
            ].tolist(),
        )

    def test_admission_text_is_removed_and_notes_use_discharge_times(self):
        source = {
            "admissions": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "admittime": "2200-01-01 08:00:00",
                        "dischtime": "2200-01-03 08:00:00",
                        "deathtime": "2200-01-03 07:00:00",
                        "edouttime": "2200-01-02 11:00:00",
                        "discharge_location": "HOME",
                        "hospital_expire_flag": "0",
                        "text": "Chief Complaint: fever",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "9",
                        "admittime": "2199-12-20 08:00:00",
                        "dischtime": "2199-12-21 08:00:00",
                        "deathtime": None,
                        "edouttime": None,
                        "discharge_location": "HOME",
                        "hospital_expire_flag": "0",
                        "text": "Prior admission summary prefix",
                    },
                ]
            ),
            "discharge": pd.DataFrame(
                [
                    {
                        "note_id": "future-note",
                        "subject_id": "1",
                        "hadm_id": "10",
                        "note_type": "DS",
                        "note_seq": "1",
                        "charttime": "2200-01-03 00:00:00",
                        "storetime": "2200-01-03 09:00:00",
                        "text": "Discharge Disposition: HOME",
                    },
                    {
                        "note_id": "prior-note",
                        "subject_id": "1",
                        "hadm_id": "9",
                        "note_type": "DS",
                        "note_seq": "1",
                        "charttime": "2199-12-21 00:00:00",
                        "storetime": "2199-12-22 09:00:00",
                        "text": "Prior admission summary",
                    },
                ]
            ),
        }

        result = build_runtime_view(source, self.cutoff)
        admissions = result.tables["admissions"]
        current = admissions.loc[admissions["hadm_id"] == "10"].iloc[0]
        prior = admissions.loc[admissions["hadm_id"] == "9"].iloc[0]

        self.assertNotIn("text", admissions.columns)
        self.assertTrue(pd.isna(current["dischtime"]))
        self.assertTrue(pd.isna(current["deathtime"]))
        self.assertTrue(pd.isna(current["edouttime"]))
        self.assertTrue(pd.isna(current["discharge_location"]))
        self.assertTrue(pd.isna(current["hospital_expire_flag"]))
        self.assertEqual("HOME", prior["discharge_location"])
        self.assertEqual("0", prior["hospital_expire_flag"])
        self.assertEqual(
            ["prior-note"],
            result.tables["discharge"]["note_id"].tolist(),
        )
        self.assertEqual(
            ["text"],
            result.reports["admissions"].dropped_columns,
        )

    def test_readmission_uses_index_episode_and_restores_its_discharge_note(self):
        source = {
            "admissions": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "9",
                        "admittime": "2199-12-20 08:00:00",
                        "dischtime": "2199-12-21 08:00:00",
                        "discharge_location": "HOME",
                        "hospital_expire_flag": "0",
                        "text": "prior copied summary",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "admittime": "2200-01-01 08:00:00",
                        "dischtime": "2200-01-02 09:00:00",
                        "discharge_location": "HOME HEALTH CARE",
                        "hospital_expire_flag": "0",
                        "text": "index copied summary",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "11",
                        "admittime": "2200-01-03 08:00:00",
                        "dischtime": "2200-01-04 09:00:00",
                        "discharge_location": "HOME",
                        "hospital_expire_flag": "0",
                        "text": "future copied summary",
                    },
                ]
            ),
            "discharge": pd.DataFrame(
                [
                    {
                        "note_id": "prior-note",
                        "subject_id": "1",
                        "hadm_id": "9",
                        "note_type": "DS",
                        "note_seq": "1",
                        "charttime": "2199-12-21 00:00:00",
                        "storetime": "2199-12-21 12:00:00",
                        "text": "prior summary",
                    },
                    {
                        "note_id": "index-note",
                        "subject_id": "1",
                        "hadm_id": "10",
                        "note_type": "DS",
                        "note_seq": "1",
                        "charttime": "2200-01-02 00:00:00",
                        "storetime": "2200-01-02 12:00:00",
                        "text": "index summary",
                    },
                    {
                        "note_id": "future-note",
                        "subject_id": "1",
                        "hadm_id": "11",
                        "note_type": "DS",
                        "note_seq": "1",
                        "charttime": "2200-01-04 00:00:00",
                        "storetime": "2200-01-04 12:00:00",
                        "text": "future summary",
                    },
                ]
            ),
            "discharge_detail": pd.DataFrame(
                [
                    {
                        "note_id": "prior-note",
                        "subject_id": "1",
                        "field_name": "Discharge Diagnosis",
                        "field_value": "prior",
                        "field_ordinal": "1",
                    },
                    {
                        "note_id": "index-note",
                        "subject_id": "1",
                        "field_name": "Discharge Diagnosis",
                        "field_value": "index",
                        "field_ordinal": "1",
                    },
                    {
                        "note_id": "future-note",
                        "subject_id": "1",
                        "field_name": "Discharge Diagnosis",
                        "field_value": "future",
                        "field_ordinal": "1",
                    },
                ]
            ),
            "labevents": pd.DataFrame(
                [
                    {
                        "labevent_id": "before-discharge",
                        "subject_id": "1",
                        "hadm_id": "10",
                        "charttime": "2200-01-02 08:00:00",
                        "storetime": "2200-01-02 08:30:00",
                        "value": "safe index result",
                    },
                    {
                        "labevent_id": "after-discharge",
                        "subject_id": "1",
                        "hadm_id": "10",
                        "charttime": "2200-01-02 10:00:00",
                        "storetime": "2200-01-02 10:30:00",
                        "value": "post-discharge result",
                    },
                    {
                        "labevent_id": "readmission-result",
                        "subject_id": "1",
                        "hadm_id": "11",
                        "charttime": "2200-01-03 09:00:00",
                        "storetime": "2200-01-03 09:30:00",
                        "value": "future readmission result",
                    },
                ]
            ),
        }

        result = build_runtime_view(
            source,
            "2200-01-02 00:00:01",
            task="Readmission_30day",
            index_hadm_id="10",
        )

        self.assertEqual(
            pd.Timestamp("2200-01-02 09:00:01"),
            result.cutoff,
        )
        self.assertEqual("readmission_index_episode", result.boundary_mode)
        self.assertEqual("10", result.index_hadm_id)
        self.assertEqual(
            pd.Timestamp("2200-01-02 09:00:00"),
            result.index_dischtime,
        )
        admissions = result.tables["admissions"]
        self.assertEqual(["9", "10"], admissions["hadm_id"].tolist())
        index_admission = admissions.loc[
            admissions["hadm_id"] == "10"
        ].iloc[0]
        self.assertEqual(
            pd.Timestamp("2200-01-02 09:00:00"),
            pd.Timestamp(index_admission["dischtime"]),
        )
        self.assertEqual("HOME HEALTH CARE", index_admission["discharge_location"])
        self.assertNotIn("text", admissions.columns)

        notes = result.tables["discharge"].set_index("note_id")
        self.assertEqual({"prior-note", "index-note"}, set(notes.index))
        self.assertEqual("index summary", notes.loc["index-note", "text"])
        self.assertTrue(pd.isna(notes.loc["index-note", "storetime"]))
        self.assertEqual(
            pd.Timestamp("2200-01-02 09:00:00"),
            pd.Timestamp(
                notes.loc["index-note", "episode_available_time"]
            ),
        )
        self.assertEqual(
            {"prior-note", "index-note"},
            set(result.tables["discharge_detail"]["note_id"]),
        )
        self.assertEqual(
            ["before-discharge"],
            result.tables["labevents"]["labevent_id"].tolist(),
        )
        self.assertEqual(
            1,
            result.reports["discharge"].episode_override_rows,
        )

    def test_readmission_can_infer_one_missing_index_hadm_id(self):
        source = {
            "admissions": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "admittime": "2200-01-01 08:00:00",
                        "dischtime": "2200-01-02 09:00:00",
                    }
                ]
            ),
            "discharge": pd.DataFrame(
                [
                    {
                        "note_id": "index-note",
                        "subject_id": "1",
                        "hadm_id": "10",
                        "note_type": "DS",
                        "note_seq": "1",
                        "charttime": "2200-01-02 00:00:00",
                        "storetime": "2200-01-02 12:00:00",
                        "text": "index summary",
                    }
                ]
            ),
        }

        result = build_runtime_view(
            source,
            "2200-01-02 00:00:01",
            task="Readmission_60day",
        )

        self.assertEqual("10", result.index_hadm_id)
        self.assertEqual(
            ["index-note"],
            result.tables["discharge"]["note_id"].tolist(),
        )

    def test_readmission_rejects_an_index_admission_from_another_date(self):
        source = {
            "admissions": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "future",
                        "admittime": "2200-01-03 08:00:00",
                        "dischtime": "2200-01-04 09:00:00",
                    }
                ]
            )
        }

        with self.assertRaisesRegex(
            TemporalPolicyError,
            "does not match benchmark event date",
        ):
            build_runtime_view(
                source,
                "2200-01-02 00:00:01",
                task="Readmission_30day",
                index_hadm_id="future",
            )

    def test_historical_codes_use_delayed_parent_availability(self):
        source = {
            "admissions": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "prior-admission",
                        "admittime": "2199-12-20 08:00:00",
                        "dischtime": "2199-12-21 08:00:00",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "recent-admission",
                        "admittime": "2200-01-01 08:00:00",
                        "dischtime": "2200-01-02 09:30:00",
                    },
                ]
            ),
            "edstays": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "stay_id": "prior-stay",
                        "intime": "2199-12-21 08:00:00",
                        "outtime": "2199-12-21 10:00:00",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "recent-stay",
                        "intime": "2200-01-02 08:00:00",
                        "outtime": "2200-01-02 09:30:00",
                    },
                ]
            ),
            "diagnoses_icd": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "prior-admission",
                        "seq_num": "1",
                        "icd_code": "A",
                        "icd_version": "10",
                        "charttime": "2199-12-21 07:59:00",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "recent-admission",
                        "seq_num": "2",
                        "icd_code": "B",
                        "icd_version": "10",
                        "charttime": "2200-01-02 09:29:00",
                    },
                ]
            ),
            "diagnosis": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "stay_id": "prior-stay",
                        "seq_num": "1",
                        "icd_code": "C",
                        "icd_version": "10",
                        "icd_title": "historical",
                        "charttime": "2199-12-21 09:59:00",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "recent-stay",
                        "seq_num": "2",
                        "icd_code": "D",
                        "icd_version": "10",
                        "icd_title": "too recent",
                        "charttime": "2200-01-02 09:29:00",
                    },
                ]
            ),
        }

        result = build_runtime_view(source, self.cutoff)
        inpatient = result.tables["diagnoses_icd"]
        emergency = result.tables["diagnosis"]

        self.assertEqual(["A"], inpatient["icd_code"].tolist())
        self.assertEqual(["C"], emergency["icd_code"].tolist())
        self.assertNotIn("charttime", inpatient.columns)
        self.assertNotIn("charttime", emergency.columns)
        self.assertEqual(
            pd.Timestamp("2199-12-22 08:00:00"),
            pd.Timestamp(inpatient.iloc[0]["available_time"]),
        )
        self.assertEqual(
            pd.Timestamp("2199-12-22 10:00:00"),
            pd.Timestamp(emergency.iloc[0]["available_time"]),
        )

    def test_code_prediction_permanently_masks_only_target_encounter(self):
        source = {
            "admissions": pd.DataFrame(
            [
                {
                    "subject_id": "1",
                    "hadm_id": "history-hadm",
                    "admittime": "2199-12-01 08:00:00",
                    "dischtime": "2199-12-02 08:00:00",
                },
                {
                    "subject_id": "1",
                    "hadm_id": "target-hadm",
                    "admittime": "2199-12-10 08:00:00",
                    "dischtime": "2199-12-11 08:00:00",
                },
            ]
            ),
            "edstays": pd.DataFrame(
            [
                {
                    "subject_id": "1",
                    "stay_id": "history-stay",
                    "intime": "2199-12-02 08:00:00",
                    "outtime": "2199-12-02 10:00:00",
                },
                {
                    "subject_id": "1",
                    "stay_id": "target-stay",
                    "intime": "2199-12-11 08:00:00",
                    "outtime": "2199-12-11 10:00:00",
                },
            ]
            ),
            "diagnoses_icd": pd.DataFrame(
            [
                {
                    "subject_id": "1",
                    "hadm_id": "history-hadm",
                    "seq_num": "1",
                    "icd_code": "HISTORY",
                    "icd_version": "10",
                    "charttime": "2199-12-02 07:59:00",
                },
                {
                    "subject_id": "1",
                    "hadm_id": "target-hadm",
                    "seq_num": "1",
                    "icd_code": "GOLD",
                    "icd_version": "10",
                    "charttime": "2199-12-11 07:59:00",
                },
            ]
            ),
            "diagnosis": pd.DataFrame(
            [
                {
                    "subject_id": "1",
                    "stay_id": "history-stay",
                    "seq_num": "1",
                    "icd_code": "HISTORY",
                    "icd_version": "10",
                    "icd_title": "history",
                    "charttime": "2199-12-02 09:59:00",
                },
                {
                    "subject_id": "1",
                    "stay_id": "target-stay",
                    "seq_num": "1",
                    "icd_code": "GOLD",
                    "icd_version": "10",
                    "icd_title": "answer",
                    "charttime": "2199-12-11 09:59:00",
                },
            ]
            ),
        }

        for task in ("diagnoses_icd", "diagnoses_ccs"):
            with self.subTest(task=task):
                result = build_runtime_view(
                    source,
                    self.cutoff,
                    task=task,
                    target_hadm_id="target-hadm",
                )
                self.assertEqual(
                    ["HISTORY"],
                    result.tables["diagnoses_icd"]["icd_code"].tolist(),
                )
                self.assertEqual("target-hadm", result.target_hadm_id)
                self.assertEqual(
                    1,
                    result.reports["diagnoses_icd"].target_masked_rows,
                )

        for task in ("diagnosis", "diagnosis_ccs"):
            with self.subTest(task=task):
                result = build_runtime_view(
                    source,
                    self.cutoff,
                    task=task,
                    target_stay_id="target-stay",
                )
                self.assertEqual(
                    ["HISTORY"],
                    result.tables["diagnosis"]["icd_code"].tolist(),
                )
                self.assertEqual("target-stay", result.target_stay_id)
                self.assertEqual(
                    1,
                    result.reports["diagnosis"].target_masked_rows,
                )

    def test_code_prediction_requires_trusted_target_encounter(self):
        with self.assertRaisesRegex(
            TemporalPolicyError,
            "trusted target_hadm_id is required",
        ):
            build_runtime_view(
                {},
                self.cutoff,
                task="diagnoses_icd",
            )

        with self.assertRaisesRegex(
            TemporalPolicyError,
            "trusted target_stay_id is required",
        ):
            build_runtime_view(
                {},
                self.cutoff,
                task="diagnosis_ccs",
            )

    def test_historical_procedure_and_billing_rows_use_parent_discharge(self):
        source = {
            "admissions": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "prior",
                        "admittime": "2199-12-20 08:00:00",
                        "dischtime": "2199-12-21 08:00:00",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "recent",
                        "admittime": "2200-01-01 08:00:00",
                        "dischtime": "2200-01-02 09:30:00",
                    },
                ]
            ),
            "procedures_icd": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "prior",
                        "seq_num": "1",
                        "chartdate": "2199-12-20",
                        "icd_code": "P1",
                        "icd_version": "10",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "recent",
                        "seq_num": "2",
                        "chartdate": "2200-01-02",
                        "icd_code": "P2",
                        "icd_version": "10",
                    },
                ]
            ),
            "hcpcsevents": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "prior",
                        "chartdate": "2199-12-20",
                        "hcpcs_cd": "H1",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "recent",
                        "chartdate": "2200-01-02",
                        "hcpcs_cd": "H2",
                    },
                ]
            ),
            "drgcodes": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "prior",
                        "drg_type": "APR",
                        "drg_code": "D1",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "recent",
                        "drg_type": "APR",
                        "drg_code": "D2",
                    },
                ]
            ),
        }

        result = build_runtime_view(source, self.cutoff)

        self.assertEqual(
            ["P1"],
            result.tables["procedures_icd"]["icd_code"].tolist(),
        )
        self.assertEqual(
            ["H1"],
            result.tables["hcpcsevents"]["hcpcs_cd"].tolist(),
        )
        self.assertEqual(
            ["D1"],
            result.tables["drgcodes"]["drg_code"].tolist(),
        )
        for table_name in ("procedures_icd", "hcpcsevents", "drgcodes"):
            self.assertEqual(
                pd.Timestamp("2199-12-22 08:00:00"),
                pd.Timestamp(
                    result.tables[table_name].iloc[0]["available_time"]
                ),
            )

    def test_triage_uses_visible_ed_intime_plus_one_second(self):
        source = {
            "edstays": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "stay_id": "safe-stay",
                        "intime": "2200-01-02 08:00:00",
                        "outtime": "2200-01-02 11:00:00",
                        "disposition": "ADMITTED",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "boundary-stay",
                        "intime": "2200-01-02 09:59:59",
                        "outtime": "2200-01-02 11:00:00",
                        "disposition": "ADMITTED",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "future-stay",
                        "intime": "2200-01-02 11:00:00",
                        "outtime": "2200-01-02 12:00:00",
                        "disposition": "HOME",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "ambiguous-stay",
                        "intime": "2200-01-02 08:30:00",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "ambiguous-stay",
                        "intime": "2200-01-02 08:45:00",
                    },
                ]
            ),
            "triage": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "stay_id": "safe-stay",
                        "heartrate": "110",
                        "acuity": "2",
                        "chiefcomplaint": "Chest pain",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "boundary-stay",
                        "heartrate": "100",
                        "acuity": "2",
                        "chiefcomplaint": "Dyspnea",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "future-stay",
                        "heartrate": "90",
                        "acuity": "3",
                        "chiefcomplaint": "Fever",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "orphan-stay",
                        "heartrate": "80",
                        "acuity": "4",
                        "chiefcomplaint": "Headache",
                    },
                    {
                        "subject_id": "1",
                        "stay_id": "ambiguous-stay",
                        "heartrate": "70",
                        "acuity": "5",
                        "chiefcomplaint": "Conflicting parent time",
                    },
                ]
            ),
        }

        result = build_runtime_view(source, self.cutoff)
        triage = result.tables["triage"]

        self.assertEqual(["safe-stay"], triage["stay_id"].tolist())
        self.assertEqual(
            pd.Timestamp("2200-01-02 08:00:01"),
            pd.Timestamp(triage.iloc[0]["charttime"]),
        )
        self.assertEqual("Chest pain", triage.iloc[0]["chiefcomplaint"])
        self.assertNotIn("outtime", triage.columns)
        self.assertNotIn("disposition", triage.columns)
        self.assertEqual(5, result.reports["triage"].input_rows)
        self.assertEqual(1, result.reports["triage"].output_rows)

    def test_detail_tables_restore_only_safe_parent_and_whitelisted_fields(self):
        source = {
            "poe": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "poe_id": "P1",
                        "poe_seq": "1",
                        "ordertime": "2200-01-02 08:00:00",
                    },
                    {
                        "subject_id": "1",
                        "poe_id": "P2",
                        "poe_seq": "2",
                        "ordertime": "2200-01-02 11:00:00",
                    },
                ]
            ),
            "poe_detail": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "poe_id": "P1",
                        "poe_seq": "1",
                        "field_name": "Code status",
                        "field_value": "Resuscitate (Full code)",
                    },
                    {
                        "subject_id": "1",
                        "poe_id": "P1",
                        "poe_seq": "1",
                        "field_name": "Discharge Planning",
                        "field_value": "Finalized",
                    },
                    {
                        "subject_id": "1",
                        "poe_id": "P2",
                        "poe_seq": "2",
                        "field_name": "Indication",
                        "field_value": "Hydration",
                    },
                ]
            ),
            "emar": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "emar_id": "E1",
                        "emar_seq": "1",
                        "charttime": "2200-01-02 08:00:00",
                        "storetime": "2200-01-02 09:00:00",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "emar_id": "E1",
                        "emar_seq": "2",
                        "charttime": "2200-01-02 08:00:00",
                        "storetime": "2200-01-02 11:00:00",
                    },
                ]
            ),
            "emar_detail": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "emar_id": "E1",
                        "emar_seq": "1",
                        "product_code": "NACL",
                        "product_description": "Normal saline",
                        "route": "IV",
                        "dose_given": "1",
                    },
                    {
                        "subject_id": "1",
                        "emar_id": "E1",
                        "emar_seq": "2",
                        "product_code": "LATE",
                        "product_description": "Future medication",
                        "route": "IV",
                        "dose_given": "2",
                    },
                ]
            ),
        }

        result = build_runtime_view(source, self.cutoff)
        poe_detail = result.tables["poe_detail"]
        emar_detail = result.tables["emar_detail"]

        self.assertEqual(["Code status"], poe_detail["field_name"].tolist())
        self.assertEqual(["NACL"], emar_detail["product_code"].tolist())
        self.assertEqual(
            ["Normal saline"],
            emar_detail["product_description"].tolist(),
        )
        self.assertNotIn("dose_given", emar_detail.columns)
        self.assertEqual(1, result.reports["poe_detail"].output_rows)
        self.assertEqual(1, result.reports["emar_detail"].output_rows)

    def test_date_of_death_is_removed_without_availability_time(self):
        source = {
            "patients": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "anchor_year": "2200",
                        "dod": "2200-01-01",
                    }
                ]
            )
        }

        result = build_runtime_view(source, self.cutoff)

        self.assertNotIn("dod", result.tables["patients"].columns)

    def test_interval_restores_ongoing_plan_but_hides_final_state(self):
        source = {
            "inputevents": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "starttime": "2200-01-02 07:00:00",
                        "endtime": "2200-01-02 09:00:00",
                        "storetime": "2200-01-02 09:30:00",
                        "itemid": "1",
                        "amount": "100",
                        "rate": "50",
                        "statusdescription": "FinishedRunning",
                        "originalrate": "50",
                    },
                    {
                        "subject_id": "1",
                        "starttime": "2200-01-02 08:00:00",
                        "endtime": "2200-01-02 11:00:00",
                        "storetime": "2200-01-02 09:00:00",
                        "itemid": "2",
                        "amount": "200",
                        "rate": "75",
                        "statusdescription": "FinishedRunning",
                        "originalrate": "25",
                    },
                    {
                        "subject_id": "1",
                        "starttime": "2200-01-02 11:00:00",
                        "endtime": "2200-01-02 12:00:00",
                        "storetime": "2200-01-02 12:30:00",
                        "itemid": "3",
                        "amount": "300",
                        "originalrate": "30",
                    },
                ]
            )
        }

        events = build_runtime_view(source, self.cutoff).tables["inputevents"]
        complete = events.loc[events["itemid"] == "1"].iloc[0]
        ongoing = events.loc[events["itemid"] == "2"].iloc[0]

        self.assertEqual(["1", "2"], events["itemid"].tolist())
        self.assertEqual("100", complete["amount"])
        self.assertEqual("FinishedRunning", complete["statusdescription"])
        self.assertTrue(pd.isna(ongoing["endtime"]))
        self.assertTrue(pd.isna(ongoing["storetime"]))
        self.assertTrue(pd.isna(ongoing["amount"]))
        self.assertTrue(pd.isna(ongoing["rate"]))
        self.assertTrue(pd.isna(ongoing["statusdescription"]))
        self.assertEqual("25", ongoing["originalrate"])

    def test_pharmacy_restores_verified_plan_without_mutable_final_state(self):
        source = {
            "pharmacy": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "pharmacy_id": "P1",
                        "entertime": "2200-01-02 08:00:00",
                        "verifiedtime": "2200-01-02 09:00:00",
                        "starttime": "2200-01-02 09:30:00",
                        "stoptime": "2200-01-03 09:00:00",
                        "medication": "Heparin",
                        "route": "IV",
                        "frequency": "Q8H",
                        "status": "Discontinued",
                        "expirationdate": "2200-01-03 09:00:00",
                        "dispensation": "Omnicell",
                        "fill_quantity": "3",
                    },
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "pharmacy_id": "P2",
                        "entertime": "2200-01-02 08:00:00",
                        "verifiedtime": "2200-01-02 11:00:00",
                        "starttime": "2200-01-02 11:30:00",
                        "medication": "Future",
                    },
                ]
            )
        }

        pharmacy = build_runtime_view(source, self.cutoff).tables["pharmacy"]

        self.assertEqual(["Heparin"], pharmacy["medication"].tolist())
        self.assertEqual(["IV"], pharmacy["route"].tolist())
        for column in (
            "stoptime",
            "status",
            "expirationdate",
            "dispensation",
            "fill_quantity",
        ):
            self.assertNotIn(column, pharmacy.columns)

    def test_icu_final_fields_are_hidden_for_ongoing_stay(self):
        source = {
            "icustays": pd.DataFrame(
                [
                    {
                        "subject_id": "1",
                        "hadm_id": "10",
                        "stay_id": "100",
                        "first_careunit": "MICU",
                        "last_careunit": "SICU",
                        "intime": "2200-01-02 08:00:00",
                        "outtime": "2200-01-03 08:00:00",
                        "los": "1.0",
                    }
                ]
            )
        }

        stay = build_runtime_view(source, self.cutoff).tables["icustays"].iloc[0]

        self.assertEqual("MICU", stay["first_careunit"])
        self.assertTrue(pd.isna(stay["last_careunit"]))
        self.assertTrue(pd.isna(stay["outtime"]))
        self.assertTrue(pd.isna(stay["los"]))

    def test_unknown_table_and_unknown_column_fail_closed(self):
        with self.assertRaises(TemporalPolicyError):
            build_runtime_view(
                {"mystery": pd.DataFrame([{"subject_id": "1"}])},
                self.cutoff,
            )

        with self.assertRaises(TemporalPolicyError):
            build_runtime_view(
                {
                    "admissions": pd.DataFrame(
                        [
                            {
                                "subject_id": "1",
                                "admittime": "2200-01-01 00:00:00",
                                "future_answer": "yes",
                            }
                        ]
                    )
                },
                self.cutoff,
            )


if __name__ == "__main__":
    unittest.main()
