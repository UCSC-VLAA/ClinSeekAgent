import asyncio
import json

from clinseekagent.ehr_tool_pool import EHRToolPool


def test_load_ehr_uses_trusted_manifest_metadata():
    async def exercise():
        pool = EHRToolPool()
        pool.configure_session(
            "qid-1",
            {
                "subject_id": 123,
                "prediction_time": "2100-01-01 10:00:00",
                "study_ids": [7, 8],
                "scope": "patient_scope",
                "task": "ehrxqa_image",
                "hadm_id": 456,
                "target_hadm_id": 457,
                "target_stay_id": 458,
                "modalities": ["cxr_table", "cxr_image"],
            },
        )
        captured = {}

        async def fake_call(qid, tool_name, tool_args):
            captured.update(tool_args)
            return tool_args

        pool._call_mcp_tool = fake_call
        result = await pool.call_tool(
            "qid-1",
            "load_ehr",
            {
                "subject_id": "attacker-chosen-patient",
                "timestamp": "2999-01-01 00:00:00",
            },
        )
        await pool.close()
        return captured, json.loads(result)

    captured, result = asyncio.run(exercise())
    assert captured == {
        "subject_id": "123",
        "timestamp": "2100-01-01 10:00:00",
        "linked_study_ids": ["7", "8"],
        "scope": "patient_scope",
        "task": "ehrxqa_image",
        "hadm_id": "456",
        "target_hadm_id": "457",
        "target_stay_id": "458",
        "modalities": ["cxr_table", "cxr_image"],
    }
    assert result == captured


def test_load_ehr_fails_without_trusted_manifest_metadata():
    async def exercise():
        pool = EHRToolPool()
        result = await pool.call_tool(
            "qid-untrusted",
            "load_ehr",
            {
                "subject_id": "attacker-chosen-patient",
                "timestamp": "2999-01-01 00:00:00",
            },
        )
        await pool.close()
        return result

    result = asyncio.run(exercise())
    assert "no trusted benchmark metadata" in result


def test_patient_query_subject_id_is_bound_to_manifest():
    async def exercise():
        pool = EHRToolPool()
        pool.configure_session(
            "qid-bound",
            {
                "subject_id": 123,
                "prediction_time": "2100-01-01 10:00:00",
            },
        )
        captured = {}

        async def fake_call(qid, tool_name, tool_args):
            captured.update(tool_args)
            return tool_args

        pool._call_mcp_tool = fake_call
        result = await pool.call_tool(
            "qid-bound",
            "get_latest_records",
            {
                "subject_id": "attacker-chosen-patient",
                "table_name": "labevents",
            },
        )
        await pool.close()
        return captured, json.loads(result)

    captured, result = asyncio.run(exercise())
    assert captured["subject_id"] == "123"
    assert result["subject_id"] == "123"
