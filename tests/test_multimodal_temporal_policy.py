import pandas as pd

from agentlite.commons.multimodal_temporal_policy import (
    POLICY_VERSION,
    build_multimodal_runtime_view,
    detect_multimodal_schema,
)


def test_medmod_removes_outcomes_and_unlinked_cxr_but_keeps_boundary():
    source = {
        "events": pd.DataFrame(
            {
                "charttime": ["2100-01-01 10:00:00", "2100-01-01 10:00:01"],
                "value": ["known", "future"],
            }
        ),
        "stays": pd.DataFrame(
            {
                "intime": ["2100-01-01 09:00:00"],
                "last_careunit": ["MICU"],
            }
        ),
        "tb_cxr": pd.DataFrame(
            {
                "study_id": [1, 2],
                "studydatetime": [
                    "2100-01-01 10:00:00",
                    "2100-01-01 09:00:00",
                ],
            }
        ),
    }
    result = build_multimodal_runtime_view(
        source,
        "2100-01-01 10:00:00",
        linked_study_ids=[1],
        modalities=["ehr", "cxr_table"],
    )
    assert result.policy_version == POLICY_VERSION
    assert result.tables["events"]["value"].tolist() == ["known"]
    assert "last_careunit" not in result.tables["stays"].columns
    assert result.tables["tb_cxr"]["study_id"].tolist() == [1]


def test_medmod_cxr_only_suppresses_longitudinal_ehr():
    source = {
        "events": pd.DataFrame(
            {"charttime": ["2100-01-01 09:00:00"], "value": ["secret"]}
        ),
        "stays": pd.DataFrame(
            {"intime": ["2100-01-01 09:00:00"], "last_careunit": ["MICU"]}
        ),
        "tb_cxr": pd.DataFrame(
            {
                "study_id": [1],
                "studydatetime": ["2100-01-01 10:00:00"],
            }
        ),
    }
    result = build_multimodal_runtime_view(
        source,
        "2100-01-01 10:00:00",
        linked_study_ids=[1],
        modalities=["cxr_table", "cxr_image"],
    )
    assert result.tables["events"].empty
    assert result.tables["stays"].empty
    assert len(result.tables["tb_cxr"]) == 1


def test_ehrxqa_availability_gates_secondary_and_posthoc_fields():
    source = {
        "admissions": pd.DataFrame(
            {
                "hadm_id": [10, 11],
                "admittime": [
                    "2100-01-01 08:00:00",
                    "2099-12-01 08:00:00",
                ],
                "dischtime": [
                    "2100-01-02 08:00:00",
                    "2099-12-02 08:00:00",
                ],
                "discharge_location": ["HOME", "SNF"],
            }
        ),
        "patients": pd.DataFrame(
            {"subject_id": [1], "dod": ["2100-01-01"]}
        ),
        "diagnoses_icd": pd.DataFrame(
            {"hadm_id": [10, 11], "icd_code": ["future", "known"]}
        ),
        "prescriptions": pd.DataFrame(
            {
                "starttime": ["2100-01-01 09:00:00"],
                "stoptime": ["2100-01-03 09:00:00"],
            }
        ),
        "transfers": pd.DataFrame(
            {
                "intime": ["2100-01-01 09:00:00"],
                "outtime": ["2100-01-01 11:00:00"],
            }
        ),
        "tb_cxr": pd.DataFrame(
            {
                "study_id": [7, 8],
                "studydatetime": [
                    "2100-01-01 10:00:00",
                    "2099-01-01 10:00:00",
                ],
            }
        ),
    }
    result = build_multimodal_runtime_view(
        source,
        "2100-01-01 10:00:00",
        linked_study_ids=[7],
        modalities=["ehr", "cxr_table"],
    )
    current = result.tables["admissions"].set_index("hadm_id").loc[10]
    assert pd.isna(current["dischtime"])
    assert pd.isna(current["discharge_location"])
    assert result.tables["diagnoses_icd"]["icd_code"].tolist() == ["known"]
    assert pd.isna(result.tables["prescriptions"].loc[0, "stoptime"])
    assert pd.isna(result.tables["transfers"].loc[0, "outtime"])
    assert pd.isna(result.tables["patients"].loc[0, "dod"])
    assert result.tables["tb_cxr"]["study_id"].tolist() == [7]


def test_ehrxqa_cxr_only_exposes_only_linked_cxr():
    source = {
        "admissions": pd.DataFrame(
            {
                "hadm_id": [10],
                "admittime": ["2100-01-01 08:00:00"],
                "dischtime": ["2100-01-02 08:00:00"],
            }
        ),
        "patients": pd.DataFrame({"subject_id": [1]}),
        "tb_cxr": pd.DataFrame(
            {
                "study_id": [7, 8],
                "studydatetime": [
                    "2100-01-01 10:00:00",
                    "2099-01-01 10:00:00",
                ],
            }
        ),
    }
    result = build_multimodal_runtime_view(
        source,
        "2100-01-01 10:00:00",
        linked_study_ids=[7],
        modalities=["cxr_table", "cxr_image"],
    )
    assert result.tables["admissions"].empty
    assert result.tables["patients"].empty
    assert result.tables["tb_cxr"]["study_id"].tolist() == [7]


def test_ehrxqa_cohort_pseudo_subject_schema_is_detected():
    source = {
        "admissions": pd.DataFrame(
            {
                "row_id": [1],
                "hadm_id": [10],
                "admittime": ["2100-01-01 08:00:00"],
                "dischtime": ["2100-01-02 08:00:00"],
            }
        ),
        "diagnoses_icd": pd.DataFrame(
            {
                "row_id": [1],
                "hadm_id": [10],
                "charttime": ["2100-01-02 08:00:00"],
            }
        ),
    }
    assert detect_multimodal_schema(source) == "ehrxqa"
    result = build_multimodal_runtime_view(
        source,
        "2100-01-01 10:00:00",
        modalities=["ehr"],
    )
    assert result.tables["diagnoses_icd"].empty
