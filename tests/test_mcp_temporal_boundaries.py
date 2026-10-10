from __future__ import annotations

import asyncio
import sys
import unittest

import pandas as pd

# Runs in the EHR MCP environment (venvs/requirements/mcp_ehr.txt).
try:
    import fastmcp  # noqa: F401
except ImportError:  # pragma: no cover - agent environment
    raise unittest.SkipTest("fastmcp not installed; run in the EHR MCP environment")

from agentlite.mcp_tools.record_tools import (  # noqa: E402
    bounded_time_range,
    query_timestamp_series,
)
from agentlite.mcp_tools.tool_utils import (  # noqa: E402
    SESSION_EHR_DATA_KEY,
    SESSION_EHR_SUBJECT_ID_KEY,
    SESSION_EHR_TABLE_NAMES_KEY,
    SESSION_EHR_TIMESTAMP_KEY,
    get_resource,
    get_resource_df,
)


class FakeContext:
    def __init__(self, session_id: str):
        self.session_id = session_id

    async def read_resource(self, _uri):
        raise AssertionError("patient resources must not use global fallback")


class McpTemporalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.main_module = sys.modules["__main__"]
        self.previous_store = getattr(
            self.main_module, "ehr_session_store", None
        )
        self.had_store = hasattr(self.main_module, "ehr_session_store")
        self.main_module.ehr_session_store = {
            "session-safe": {
                SESSION_EHR_SUBJECT_ID_KEY: "123",
                SESSION_EHR_TIMESTAMP_KEY: "2200-01-02 10:00:00",
                SESSION_EHR_TABLE_NAMES_KEY: ["labevents"],
                SESSION_EHR_DATA_KEY: {
                    "labevents": {
                        "subject_id": ["123"],
                        "charttime": ["2200-01-02 08:00:00"],
                        "storetime": ["2200-01-02 09:00:00"],
                        "value": ["safe"],
                    }
                },
            }
        }

    def tearDown(self):
        if self.had_store:
            self.main_module.ehr_session_store = self.previous_store
        else:
            delattr(self.main_module, "ehr_session_store")

    def test_patient_resources_require_matching_bound_subject(self):
        async def exercise():
            ctx = FakeContext("session-safe")
            correct = await get_resource_df(
                ctx,
                "cache://ehr/ehr_data/123/labevents.json",
            )
            wrong = await get_resource_df(
                ctx,
                "cache://ehr/ehr_data/999/labevents.json",
            )
            wrong_tables = await get_resource(
                ctx,
                "cache://ehr/ehr_data/999/table_list.json",
            )
            return correct, wrong, wrong_tables

        correct, wrong, wrong_tables = asyncio.run(exercise())
        self.assertEqual(["safe"], correct["value"].tolist())
        self.assertIsNone(wrong)
        self.assertIsNone(wrong_tables)

    def test_missing_session_fails_closed_without_resource_fallback(self):
        async def exercise():
            return await get_resource_df(
                FakeContext("missing-session"),
                "cache://ehr/ehr_data/123/labevents.json",
            )

        self.assertIsNone(asyncio.run(exercise()))

    def test_requested_time_range_is_clamped_below_cutoff(self):
        async def exercise():
            return await bounded_time_range(
                FakeContext("session-safe"),
                "2200-01-01 00:00:00",
                "2250-01-01 00:00:00",
            )

        start, end, error = asyncio.run(exercise())
        self.assertIsNone(error)
        self.assertEqual(pd.Timestamp("2200-01-01"), start)
        self.assertLess(end, pd.Timestamp("2200-01-02 10:00:00"))

    def test_lab_query_time_uses_storetime_not_first_time_column(self):
        frame = pd.DataFrame(
            {
                "charttime": [
                    "2200-01-02 08:00:00",
                    "2200-01-02 09:00:00",
                ],
                "storetime": [
                    "2200-01-02 09:30:00",
                    "2200-01-02 09:15:00",
                ],
            }
        )

        query_times = query_timestamp_series("labevents", frame)

        self.assertEqual(
            [
                pd.Timestamp("2200-01-02 09:30:00"),
                pd.Timestamp("2200-01-02 09:15:00"),
            ],
            query_times.tolist(),
        )

    def test_microbiology_query_time_prefers_storetime_then_storedate(self):
        frame = pd.DataFrame(
            {
                "storetime": ["2200-01-02 09:00:00", None],
                "storedate": ["2200-01-02", "2200-01-01"],
            }
        )

        query_times = query_timestamp_series("microbiologyevents", frame)

        self.assertEqual(
            [
                pd.Timestamp("2200-01-02 09:00:00"),
                pd.Timestamp("2200-01-01 23:59:59.999999"),
            ],
            query_times.tolist(),
        )

    def test_triage_query_time_uses_derived_charttime(self):
        frame = pd.DataFrame(
            {
                "stay_id": ["100"],
                "charttime": ["2200-01-02 08:00:01"],
                "chiefcomplaint": ["Chest pain"],
            }
        )

        query_times = query_timestamp_series("triage", frame)

        self.assertEqual(
            [pd.Timestamp("2200-01-02 08:00:01")],
            query_times.tolist(),
        )

    def test_historical_diagnosis_query_time_uses_derived_availability(self):
        frame = pd.DataFrame(
            {
                "hadm_id": ["10"],
                "icd_code": ["A123"],
                "available_time": ["2200-01-01 08:00:00"],
            }
        )

        query_times = query_timestamp_series("diagnoses_icd", frame)

        self.assertEqual(
            [pd.Timestamp("2200-01-01 08:00:00")],
            query_times.tolist(),
        )

    def test_readmission_discharge_uses_episode_boundary_for_queries(self):
        frame = pd.DataFrame(
            {
                "note_id": ["index", "prior"],
                "charttime": [
                    "2200-01-02 00:00:00",
                    "2199-12-20 00:00:00",
                ],
                "storetime": [None, "2199-12-20 12:00:00"],
                "episode_available_time": [
                    "2200-01-02 09:00:00",
                    None,
                ],
            }
        )

        query_times = query_timestamp_series("discharge", frame)

        self.assertEqual(
            [
                pd.Timestamp("2200-01-02 09:00:00"),
                pd.Timestamp("2199-12-20 12:00:00"),
            ],
            query_times.tolist(),
        )


if __name__ == "__main__":
    unittest.main()
