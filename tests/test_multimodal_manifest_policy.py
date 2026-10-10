import pytest

from clinseekagent.multimodal_manifest_policy import (
    STRICT_POLICY_VERSION,
    validate_agentic_manifest,
    validate_curated_manifest,
)


def test_agentic_manifest_requires_trusted_study_scope():
    valid = {
        "qid": "x",
        "source_benchmark": "medmod",
        "task": "medmod_radiology",
        "subject_id": 1,
        "prediction_time": "2100-01-01 00:00:00",
        "scope": "patient_scope",
        "modalities": ["cxr_table"],
        "study_ids": [7],
    }
    validate_agentic_manifest([valid])
    invalid = {key: value for key, value in valid.items() if key != "study_ids"}
    with pytest.raises(ValueError, match="study_ids"):
        validate_agentic_manifest([invalid])


def test_curated_manifest_rejects_legacy_rendered_text():
    base = {
        "qid": "x",
        "source_benchmark": "ehrxqa",
        "input_text": "<ehr_context>...</ehr_context>",
    }
    with pytest.raises(ValueError, match="strict"):
        validate_curated_manifest([base])
    validate_curated_manifest(
        [{**base, "leakage_policy_version": STRICT_POLICY_VERSION}]
    )
