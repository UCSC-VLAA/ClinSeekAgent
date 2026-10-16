"""Strict, table-aware temporal filtering for MIMIC-IV patient databases.

The source SQLite database remains a complete longitudinal patient record.
This module builds a question-specific runtime view. Most tasks use
``cutoff = prediction_time``. Readmission tasks are episode-complete
predictions: their effective cutoff is the index admission's real
``dischtime`` plus one second, and the index discharge summary is exposed as
an explicitly declared episode artifact. It deliberately contains no
MCP/session logic: the same policy can be reused by the runtime loader,
offline builders, and auditors.

For AgentEHR-Bench this runtime view may be stricter than the original
setting. Two synthetic timestamp transformations performed by AgentEHR's
``patient_event2db.py`` are not accepted as evidence of real-time
availability:

* ``diagnoses_icd.charttime = admissions.dischtime - 1 minute``
* ``diagnosis.charttime = edstays.outtime - 1 minute``

The generated timestamps are never exposed. The strict runtime instead
restores codes from completed historical encounters using a disclosed
conservative benchmark proxy: 24 hours after the parent admission discharge
or ED departure. For diagnosis-code prediction tasks, the trusted target
encounter is excluded regardless of elapsed time. This preserves useful
non-target history without turning a fixed delay into an expiring target-label
guard. The proxy is not represented as an original MIMIC timestamp and is
emitted as ``available_time``.

The source patient DB may also contain ``admissions.text`` copied from a
discharge summary. It has lost the source note's ``charttime/storetime`` and
its section extraction fails open when the exact ``"Physical Exam"`` marker is
absent. The strict runtime view therefore always drops ``admissions.text``.
Discharge notes are normally exposed only through the original ``discharge``
table after both their chart and store times precede the cutoff. For
``Readmission_30day`` and ``Readmission_60day`` only, the index admission's
discharge summary is part of the task input even when it was signed later.
Its late source ``storetime`` is redacted and a derived
``episode_available_time = index dischtime`` is emitted. No other
post-discharge row receives this exception.

MIMIC-IV-ED does not publish an independent timestamp for ``triage``. For
that table, the strict runtime retains the source-defined availability
approximation ``edstays.intime + 1 second``. This prevents future ED stays
from crossing the cutoff, but it is not a claim about the original system's
exact write latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

import pandas as pd


POLICY_VERSION = "strict-ehr-runtime-v8"
ADMINISTRATIVE_AVAILABILITY_DELAY_SECONDS = 24 * 60 * 60
READMISSION_EPISODE_EPSILON_SECONDS = 1
READMISSION_TASKS = frozenset({"Readmission_30day", "Readmission_60day"})
TARGET_CODE_TASKS: dict[str, tuple[str, str]] = {
    "diagnoses_icd": ("diagnoses_icd", "hadm_id"),
    "diagnoses_ccs": ("diagnoses_icd", "hadm_id"),
    "diagnosis": ("diagnosis", "stay_id"),
    "diagnosis_ccs": ("diagnosis", "stay_id"),
}
READMISSION_DISCHARGE_TIME_COLUMN = "episode_available_time"
DERIVED_OUTPUT_COLUMNS: dict[str, set[str]] = {
    "discharge": {READMISSION_DISCHARGE_TIME_COLUMN},
}
MISSING_STRINGS = {"", "nan", "nat", "none", "null", "<na>"}
DATE_ONLY_COLUMNS = {
    "chartdate",
    "storedate",
    "dod",
    "expirationdate",
}
FORBIDDEN_EHR_COLUMNS = {
    "label",
    "ground_truth",
    "prediction",
    "answer",
}


class TemporalPolicyError(ValueError):
    """Raised when a runtime view cannot be proven safe under the policy."""


@dataclass(frozen=True)
class ReadmissionEpisodeScope:
    """Trusted index-episode boundary for a readmission benchmark row."""

    task: str
    requested_cutoff: pd.Timestamp
    index_hadm_id: str
    index_dischtime: pd.Timestamp

    @property
    def effective_cutoff(self) -> pd.Timestamp:
        return self.index_dischtime + pd.Timedelta(
            seconds=READMISSION_EPISODE_EPSILON_SECONDS
        )


@dataclass(frozen=True)
class TargetCodeScope:
    """Trusted target encounter whose answer codes must never be exposed."""

    task: str
    table: str
    key: str
    encounter_id: str


@dataclass(frozen=True)
class FieldGuard:
    """Expose ``field`` only when a field-specific timestamp is pre-cutoff."""

    field: str
    available_at: tuple[str, ...]


@dataclass(frozen=True)
class TablePolicy:
    """Temporal policy for a directly filterable or static patient table."""

    event_at: tuple[str, ...] = ()
    available_at: tuple[str, ...] = ()
    available_at_mode: Literal["latest", "coalesce"] = "latest"
    guards: tuple[FieldGuard, ...] = ()
    drop_columns: tuple[str, ...] = ()
    completion_at: tuple[str, ...] = ()
    completion_fields: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ChildPolicy:
    """Retain a table only through keys visible in a filtered parent table."""

    parent: str
    keys: tuple[str, ...]
    note: str = ""
    parent_time: tuple[str, ...] = ()
    derived_time_column: str | None = None
    offset_seconds: int = 0
    keep_columns: tuple[str, ...] = ()
    row_allowlist_column: str | None = None
    row_allowlist_values: tuple[str, ...] = ()
    require_nonempty_columns: tuple[str, ...] = ()


@dataclass
class TableReport:
    """Auditable row and redaction counts for one source table."""

    input_rows: int
    output_rows: int = 0
    dropped_rows: int = 0
    redacted_values: int = 0
    unparseable_time_values_redacted: int = 0
    future_time_values_redacted: int = 0
    dropped_columns: list[str] = field(default_factory=list)
    episode_override_rows: int = 0
    target_masked_rows: int = 0
    policy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "dropped_rows": self.dropped_rows,
            "redacted_values": self.redacted_values,
            "unparseable_time_values_redacted": (
                self.unparseable_time_values_redacted
            ),
            "future_time_values_redacted": self.future_time_values_redacted,
            "dropped_columns": list(self.dropped_columns),
            "episode_override_rows": self.episode_override_rows,
            "target_masked_rows": self.target_masked_rows,
            "policy": self.policy,
        }


@dataclass
class TemporalViewResult:
    """Filtered tables plus the policy metadata needed for an audit trail."""

    tables: dict[str, pd.DataFrame]
    reports: dict[str, TableReport]
    cutoff: pd.Timestamp
    requested_cutoff: pd.Timestamp | None = None
    boundary_mode: str = "point_in_time"
    task: str | None = None
    index_hadm_id: str | None = None
    index_dischtime: pd.Timestamp | None = None
    target_hadm_id: str | None = None
    target_stay_id: str | None = None
    policy_version: str = POLICY_VERSION

    def report_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "cutoff": str(self.cutoff),
            "requested_cutoff": str(
                self.requested_cutoff
                if self.requested_cutoff is not None
                else self.cutoff
            ),
            "boundary_mode": self.boundary_mode,
            "task": self.task,
            "index_hadm_id": self.index_hadm_id,
            "index_dischtime": (
                str(self.index_dischtime)
                if self.index_dischtime is not None
                else None
            ),
            "target_hadm_id": self.target_hadm_id,
            "target_stay_id": self.target_stay_id,
            "tables": {
                table_name: report.as_dict()
                for table_name, report in sorted(self.reports.items())
            },
        }


# ``event_at`` is when the clinical event is associated with the record.
# ``available_at`` is when that record became readable. Alternative event
# columns coalesce left-to-right. Availability columns normally use the latest
# parseable value per row, unless a table explicitly declares an ordered
# coalesce (for example, precise storetime with date-only storedate fallback).
TABLE_POLICIES: dict[str, TablePolicy] = {
    "patients": TablePolicy(
        drop_columns=("dod",),
        note=(
            "Static demographics; date of death is removed because the source "
            "has no timestamp proving when it became queryable."
        ),
    ),
    "admissions": TablePolicy(
        event_at=("admittime",),
        guards=(
            FieldGuard("dischtime", ("dischtime",)),
            FieldGuard("deathtime", ("deathtime",)),
            FieldGuard("edouttime", ("edouttime",)),
            FieldGuard("discharge_location", ("dischtime",)),
            FieldGuard("hospital_expire_flag", ("dischtime",)),
        ),
        drop_columns=("text",),
        note=(
            "Admission is visible at admittime; discharge outcomes are guarded; "
            "copied discharge-summary text is always removed from runtime."
        ),
    ),
    "transfers": TablePolicy(
        event_at=("intime",),
        guards=(FieldGuard("outtime", ("outtime",)),),
    ),
    "edstays": TablePolicy(
        event_at=("intime",),
        guards=(
            FieldGuard("outtime", ("outtime",)),
            FieldGuard("disposition", ("outtime",)),
        ),
    ),
    "icustays": TablePolicy(
        event_at=("intime",),
        guards=(
            FieldGuard("outtime", ("outtime",)),
            FieldGuard("los", ("outtime",)),
            FieldGuard("last_careunit", ("outtime",)),
        ),
    ),
    "services": TablePolicy(event_at=("transfertime",)),
    "prescriptions": TablePolicy(
        event_at=("starttime",),
        guards=(FieldGuard("stoptime", ("stoptime",)),),
    ),
    "poe": TablePolicy(
        event_at=("ordertime",),
        drop_columns=(
            "order_status",
            "discontinue_of_poe_id",
            "discontinued_by_poe_id",
        ),
        note="Order disposition fields have no status-change timestamp.",
    ),
    "medrecon": TablePolicy(event_at=("charttime",)),
    "pyxis": TablePolicy(event_at=("charttime",)),
    "vitalsign": TablePolicy(event_at=("charttime",)),
    "omr": TablePolicy(event_at=("chartdate",)),
    "chartevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "datetimeevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "labevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "outputevents": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "inputevents": TablePolicy(
        event_at=("starttime",),
        completion_at=("endtime", "storetime"),
        completion_fields=(
            "endtime",
            "storetime",
            "amount",
            "amountuom",
            "rate",
            "rateuom",
            "totalamount",
            "totalamountuom",
            "isopenbag",
            "continueinnextdept",
            "statusdescription",
        ),
        note=(
            "Visible after start. Ongoing rows retain order and original plan "
            "fields but redact completion-dependent values."
        ),
    ),
    "ingredientevents": TablePolicy(
        event_at=("starttime",),
        completion_at=("endtime", "storetime"),
        completion_fields=(
            "endtime",
            "storetime",
            "amount",
            "amountuom",
            "rate",
            "rateuom",
            "statusdescription",
        ),
        note=(
            "Visible after start. Ongoing rows retain order and original plan "
            "fields but redact completion-dependent values."
        ),
    ),
    "procedureevents": TablePolicy(
        event_at=("starttime",),
        completion_at=("endtime", "storetime"),
        completion_fields=(
            "endtime",
            "storetime",
            "value",
            "valueuom",
            "isopenbag",
            "continueinnextdept",
            "statusdescription",
        ),
        note=(
            "Visible after start. Ongoing rows retain procedure order and "
            "original plan fields but redact completion-dependent values."
        ),
    ),
    "emar": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
        guards=(FieldGuard("scheduletime", ("scheduletime",)),),
    ),
    "microbiologyevents": TablePolicy(
        event_at=("charttime", "chartdate"),
        available_at=("storetime", "storedate"),
        available_at_mode="coalesce",
        note=(
            "Use precise storetime when parseable; otherwise fall back to "
            "storedate with conservative end-of-day semantics."
        ),
    ),
    "radiology": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "discharge": TablePolicy(
        event_at=("charttime",),
        available_at=("storetime",),
    ),
    "pharmacy": TablePolicy(
        event_at=("entertime",),
        available_at=("verifiedtime",),
        drop_columns=(
            "stoptime",
            "status",
            "expirationdate",
            "dispensation",
            "fill_quantity",
        ),
        note=(
            "Retain verified medication plans; mutable stop, status, "
            "expiration, dispensation, and fill fields are always removed."
        ),
    ),
}


# Tables without a useful, defensible recovery model remain omitted.
DROP_TABLES: dict[str, str] = {
    "cost": "Post-event administrative table with no availability timestamp.",
}


CHILD_POLICIES: dict[str, ChildPolicy] = {
    "diagnoses_icd": ChildPolicy(
        parent="admissions",
        keys=("hadm_id",),
        parent_time=("dischtime",),
        derived_time_column="available_time",
        offset_seconds=ADMINISTRATIVE_AVAILABILITY_DELAY_SECONDS,
        keep_columns=(
            "subject_id",
            "hadm_id",
            "seq_num",
            "icd_code",
            "icd_version",
            "available_time",
        ),
        note=(
            "Historical inpatient diagnoses become visible 24 hours after "
            "parent discharge; synthetic charttime is removed."
        ),
    ),
    "diagnosis": ChildPolicy(
        parent="edstays",
        keys=("stay_id",),
        parent_time=("outtime",),
        derived_time_column="available_time",
        offset_seconds=ADMINISTRATIVE_AVAILABILITY_DELAY_SECONDS,
        keep_columns=(
            "subject_id",
            "stay_id",
            "seq_num",
            "icd_code",
            "icd_version",
            "icd_title",
            "available_time",
        ),
        note=(
            "Historical ED diagnoses become visible 24 hours after parent ED "
            "departure; synthetic charttime is removed."
        ),
    ),
    "procedures_icd": ChildPolicy(
        parent="admissions",
        keys=("hadm_id",),
        parent_time=("dischtime",),
        derived_time_column="available_time",
        offset_seconds=ADMINISTRATIVE_AVAILABILITY_DELAY_SECONDS,
        note=(
            "Historical ICD procedures become visible 24 hours after parent "
            "discharge."
        ),
    ),
    "hcpcsevents": ChildPolicy(
        parent="admissions",
        keys=("hadm_id",),
        parent_time=("dischtime",),
        derived_time_column="available_time",
        offset_seconds=ADMINISTRATIVE_AVAILABILITY_DELAY_SECONDS,
        note=(
            "Historical HCPCS rows become visible 24 hours after parent "
            "discharge."
        ),
    ),
    "drgcodes": ChildPolicy(
        parent="admissions",
        keys=("hadm_id",),
        parent_time=("dischtime",),
        derived_time_column="available_time",
        offset_seconds=ADMINISTRATIVE_AVAILABILITY_DELAY_SECONDS,
        note=(
            "Historical DRG rows become visible 24 hours after parent "
            "discharge."
        ),
    ),
    "discharge_detail": ChildPolicy(
        parent="discharge",
        keys=("note_id",),
        note="Visible only with a pre-cutoff stored discharge note.",
    ),
    "radiology_detail": ChildPolicy(
        parent="radiology",
        keys=("note_id",),
        note="Visible only with a pre-cutoff stored radiology note.",
    ),
    "poe_detail": ChildPolicy(
        parent="poe",
        keys=("poe_id", "poe_seq"),
        row_allowlist_column="field_name",
        row_allowlist_values=(
            "Admit to",
            "Admit category",
            "Tubes & Drains type",
            "Code status",
            "Transfer to",
            "Level of Urgency",
            "Indication",
        ),
        note=(
            "Retain order-time intent fields through a visible POE parent; "
            "mutable discharge and consult status fields are omitted."
        ),
    ),
    "emar_detail": ChildPolicy(
        parent="emar",
        keys=("emar_id", "emar_seq"),
        keep_columns=(
            "subject_id",
            "emar_id",
            "emar_seq",
            "parent_field_ordinal",
            "administration_type",
            "pharmacy_id",
            "barcode_type",
            "dose_due",
            "dose_due_unit",
            "product_unit",
            "product_code",
            "product_description",
            "product_description_other",
            "route",
            "side",
            "site",
        ),
        require_nonempty_columns=(
            "administration_type",
            "pharmacy_id",
            "barcode_type",
            "dose_due",
            "dose_due_unit",
            "product_unit",
            "product_code",
            "product_description",
            "product_description_other",
            "route",
            "side",
            "site",
        ),
        note=(
            "Retain static medication descriptors through a visible stored "
            "EMAR parent; actual dose and mutable completion fields are "
            "omitted."
        ),
    ),
    "triage": ChildPolicy(
        parent="edstays",
        keys=("stay_id",),
        parent_time=("intime",),
        derived_time_column="charttime",
        offset_seconds=1,
        note=(
            "Initial ED assessment linked to a visible stay; MIMIC-IV-ED's "
            "documented closest time approximation and EHR-R1 ordering "
            "semantics place triage at edstays.intime plus one second."
        ),
    ),
}


# These timestamped ED tables must also belong to a stay that is visible at the
# cutoff. They are filtered by their own event time first, then semi-joined.
PARENT_CONSTRAINTS: dict[str, ChildPolicy] = {
    "medrecon": ChildPolicy("edstays", ("stay_id",)),
    "pyxis": ChildPolicy("edstays", ("stay_id",)),
    "vitalsign": ChildPolicy("edstays", ("stay_id",)),
}


MIMIC_IV_PATIENT_TABLES = {
    "admissions",
    "chartevents",
    "datetimeevents",
    "diagnoses_icd",
    "diagnosis",
    "discharge",
    "discharge_detail",
    "drgcodes",
    "edstays",
    "emar",
    "emar_detail",
    "hcpcsevents",
    "icustays",
    "ingredientevents",
    "inputevents",
    "labevents",
    "medrecon",
    "microbiologyevents",
    "omr",
    "outputevents",
    "patients",
    "pharmacy",
    "poe",
    "poe_detail",
    "prescriptions",
    "procedureevents",
    "procedures_icd",
    "pyxis",
    "radiology",
    "radiology_detail",
    "services",
    "transfers",
    "triage",
    "vitalsign",
}


# A new source column has unknown temporal semantics. Missing optional columns
# are allowed for compatibility with released AgentEHR databases, but unknown
# columns in a retained table fail closed.
EXPECTED_SOURCE_COLUMNS: dict[str, set[str]] = {
    "patients": set(
        "subject_id gender anchor_age anchor_year anchor_year_group dod".split()
    ),
    "admissions": set(
        "subject_id hadm_id admittime dischtime deathtime admission_type "
        "admit_provider_id admission_location discharge_location insurance "
        "language marital_status race edregtime edouttime "
        "hospital_expire_flag text".split()
    ),
    "transfers": set(
        "subject_id hadm_id transfer_id eventtype careunit intime outtime".split()
    ),
    "edstays": set(
        "subject_id hadm_id stay_id intime outtime gender race "
        "arrival_transport disposition".split()
    ),
    "icustays": set(
        "subject_id hadm_id stay_id first_careunit last_careunit intime "
        "outtime los".split()
    ),
    "services": set(
        "subject_id hadm_id transfertime prev_service curr_service".split()
    ),
    "prescriptions": set(
        "subject_id hadm_id pharmacy_id poe_id poe_seq order_provider_id "
        "starttime stoptime drug_type drug formulary_drug_cd gsn ndc "
        "prod_strength form_rx dose_val_rx dose_unit_rx form_val_disp "
        "form_unit_disp doses_per_24_hrs route".split()
    ),
    "pharmacy": set(
        "subject_id hadm_id pharmacy_id poe_id starttime stoptime medication "
        "proc_type status entertime verifiedtime route frequency disp_sched "
        "infusion_type sliding_scale lockout_interval basal_rate one_hr_max "
        "doses_per_24_hrs duration duration_interval expiration_value "
        "expiration_unit expirationdate dispensation fill_quantity".split()
    ),
    "poe": set(
        "poe_id poe_seq subject_id hadm_id ordertime order_type order_subtype "
        "transaction_type discontinue_of_poe_id discontinued_by_poe_id "
        "order_provider_id order_status".split()
    ),
    "medrecon": set(
        "subject_id stay_id charttime name gsn ndc etc_rn etccode "
        "etcdescription".split()
    ),
    "pyxis": set("subject_id stay_id charttime med_rn name gsn_rn gsn".split()),
    "vitalsign": set(
        "subject_id stay_id charttime temperature heartrate resprate o2sat "
        "sbp dbp rhythm pain".split()
    ),
    "triage": set(
        "subject_id stay_id temperature heartrate resprate o2sat sbp dbp "
        "pain acuity chiefcomplaint".split()
    ),
    "omr": set(
        "subject_id chartdate seq_num result_name result_value".split()
    ),
    "chartevents": set(
        "subject_id hadm_id stay_id caregiver_id charttime storetime itemid "
        "value valuenum valueuom warning".split()
    ),
    "datetimeevents": set(
        "subject_id hadm_id stay_id caregiver_id charttime storetime itemid "
        "value valueuom warning".split()
    ),
    "labevents": set(
        "labevent_id subject_id hadm_id specimen_id itemid order_provider_id "
        "charttime storetime value valuenum valueuom ref_range_lower "
        "ref_range_upper flag priority comments".split()
    ),
    "outputevents": set(
        "subject_id hadm_id stay_id caregiver_id charttime storetime itemid "
        "value valueuom".split()
    ),
    "inputevents": set(
        "subject_id hadm_id stay_id caregiver_id starttime endtime storetime "
        "itemid amount amountuom rate rateuom orderid linkorderid "
        "ordercategoryname secondaryordercategoryname "
        "ordercomponenttypedescription ordercategorydescription "
        "patientweight totalamount totalamountuom isopenbag continueinnextdept "
        "statusdescription originalamount originalrate".split()
    ),
    "ingredientevents": set(
        "subject_id hadm_id stay_id caregiver_id starttime endtime storetime "
        "itemid amount amountuom rate rateuom orderid linkorderid "
        "statusdescription originalamount originalrate".split()
    ),
    "procedureevents": set(
        "subject_id hadm_id stay_id caregiver_id starttime endtime storetime "
        "itemid value valueuom location locationcategory orderid linkorderid "
        "ordercategoryname ordercategorydescription patientweight isopenbag "
        "continueinnextdept statusdescription originalamount originalrate".split()
    ),
    "emar": set(
        "subject_id hadm_id emar_id emar_seq poe_id pharmacy_id "
        "enter_provider_id charttime medication event_txt scheduletime "
        "storetime".split()
    ),
    "microbiologyevents": set(
        "microevent_id subject_id hadm_id micro_specimen_id order_provider_id "
        "chartdate charttime spec_itemid spec_type_desc test_seq storedate "
        "storetime test_itemid test_name org_itemid org_name isolate_num "
        "quantity ab_itemid ab_name dilution_text dilution_comparison "
        "dilution_value interpretation comments".split()
    ),
    "radiology": set(
        "note_id subject_id hadm_id note_type note_seq charttime storetime text".split()
    ),
    "discharge": set(
        "note_id subject_id hadm_id note_type note_seq charttime storetime text".split()
    ),
    "diagnoses_icd": set(
        "subject_id hadm_id seq_num icd_code icd_version charttime".split()
    ),
    "diagnosis": set(
        "subject_id stay_id seq_num icd_code icd_version icd_title charttime".split()
    ),
    "procedures_icd": set(
        "subject_id hadm_id seq_num chartdate icd_code icd_version".split()
    ),
    "hcpcsevents": set(
        "subject_id hadm_id chartdate hcpcs_cd seq_num short_description".split()
    ),
    "drgcodes": set(
        "subject_id hadm_id drg_type drg_code description drg_severity "
        "drg_mortality".split()
    ),
    "discharge_detail": set(
        "note_id subject_id field_name field_value field_ordinal".split()
    ),
    "radiology_detail": set(
        "note_id subject_id field_name field_value field_ordinal".split()
    ),
    "poe_detail": set(
        "poe_id poe_seq subject_id field_name field_value".split()
    ),
    "emar_detail": set(
        "subject_id emar_id emar_seq parent_field_ordinal administration_type "
        "pharmacy_id barcode_type reason_for_no_barcode "
        "complete_dose_not_given dose_due dose_due_unit dose_given "
        "dose_given_unit will_remainder_of_dose_be_given product_amount_given "
        "product_unit product_code product_description "
        "product_description_other prior_infusion_rate infusion_rate "
        "infusion_rate_adjustment infusion_rate_adjustment_amount "
        "infusion_rate_unit route infusion_complete completion_interval "
        "new_iv_bag_hung continued_infusion_in_other_location restart_interval "
        "side site non_formulary_visual_verification".split()
    ),
}


_DECLARED_POLICY_TABLES = (
    set(TABLE_POLICIES) | set(DROP_TABLES) | set(CHILD_POLICIES)
)
_UNDECLARED_MIMIC_IV_TABLES = MIMIC_IV_PATIENT_TABLES - _DECLARED_POLICY_TABLES
if _UNDECLARED_MIMIC_IV_TABLES:
    raise RuntimeError(
        "MIMIC-IV patient tables missing temporal policies: "
        + ", ".join(sorted(_UNDECLARED_MIMIC_IV_TABLES))
    )


def normalize_cutoff(value: Any) -> pd.Timestamp:
    """Parse one prediction cutoff or fail closed."""

    try:
        cutoff = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise TemporalPolicyError(f"Invalid prediction_time: {value!r}") from exc
    if pd.isna(cutoff):
        raise TemporalPolicyError(f"Invalid prediction_time: {value!r}")
    if cutoff.tzinfo is not None:
        raise TemporalPolicyError(
            "Timezone-aware prediction_time is unsupported for naive MIMIC timestamps"
        )
    return cutoff


def normalize_record_id(value: Any) -> str | None:
    """Normalize an encounter identifier without accepting missing sentinels."""

    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in MISSING_STRINGS:
        return None
    return normalized


def is_readmission_task(task: Any) -> bool:
    """Return whether ``task`` opts into the episode-complete boundary."""

    return str(task).strip() in READMISSION_TASKS


def resolve_target_code_scope(
    task: Any,
    target_hadm_id: Any,
    target_stay_id: Any,
) -> TargetCodeScope | None:
    """Bind a code-prediction task to its trusted target encounter.

    The IDs must come from benchmark metadata, never from model arguments or
    from the visible EHR. Missing metadata fails closed because a fixed
    availability delay cannot permanently protect an unknown target row.
    """

    normalized_task = str(task).strip()
    target = TARGET_CODE_TASKS.get(normalized_task)
    if target is None:
        return None

    table, key = target
    raw_id = target_hadm_id if key == "hadm_id" else target_stay_id
    encounter_id = normalize_record_id(raw_id)
    if encounter_id is None:
        metadata_field = (
            "target_hadm_id" if key == "hadm_id" else "target_stay_id"
        )
        raise TemporalPolicyError(
            f"{normalized_task}: trusted {metadata_field} is required to "
            "permanently mask the target encounter's diagnosis codes"
        )
    return TargetCodeScope(
        task=normalized_task,
        table=table,
        key=key,
        encounter_id=encounter_id,
    )


def clean_missing_values(df: pd.DataFrame) -> pd.DataFrame:
    """Convert string sentinels introduced by the DB builder to real nulls."""

    out = df.copy()
    for column in out.columns:
        series = out[column]
        missing = series.isna()
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(
            series
        ):
            missing |= (
                series.astype(str)
                .str.strip()
                .str.lower()
                .isin(MISSING_STRINGS)
            )
        if missing.any():
            out.loc[missing, column] = None
    return out


def parse_time_column(df: pd.DataFrame, column: str) -> pd.Series:
    """Parse a timestamp with conservative end-of-day date semantics."""

    if column not in df.columns:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    try:
        parsed = pd.to_datetime(df[column], errors="coerce", format="mixed")
    except (TypeError, ValueError):
        # Compatibility with pandas versions that predate ``format="mixed"``.
        parsed = pd.to_datetime(df[column], errors="coerce")
    if column.lower() in DATE_ONLY_COLUMNS:
        parsed = parsed.dt.normalize() + pd.Timedelta(
            hours=23,
            minutes=59,
            seconds=59,
            microseconds=999999,
        )
    return parsed


def coalesce_times(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Use the first parseable timestamp in ``columns`` for each row."""

    result = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    for column in columns:
        result = result.fillna(parse_time_column(df, column))
    return result


def latest_times(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Use the latest parseable availability timestamp for each row."""

    parsed = [
        parse_time_column(df, column).rename(column)
        for column in columns
        if column in df.columns
    ]
    if not parsed:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    return pd.concat(parsed, axis=1).max(axis=1)


def availability_times(df: pd.DataFrame, policy: TablePolicy) -> pd.Series:
    """Resolve a table's declared record-availability timestamp."""

    if policy.available_at_mode == "coalesce":
        return coalesce_times(df, policy.available_at)
    return latest_times(df, policy.available_at)


def nonempty_mask(series: pd.Series) -> pd.Series:
    return series.notna() & ~(
        series.astype(str).str.strip().str.lower().isin(MISSING_STRINGS)
    )


def resolve_readmission_episode_scope(
    source_tables: Mapping[str, pd.DataFrame],
    requested_cutoff: pd.Timestamp,
    task: str,
    index_hadm_id: Any,
) -> ReadmissionEpisodeScope:
    """Resolve one readmission row to an unambiguous completed admission.

    ``hadm_id`` normally comes from trusted manifest metadata. One released
    EHR-Bench row omits it, so a missing ID may be inferred only from a unique
    discharge note charted exactly one second before the manifest cutoff.
    The resolved discharge must be on the same calendar day as that cutoff;
    this prevents an arbitrary later admission from being selected as the
    index episode.
    """

    admissions_source = source_tables.get("admissions")
    if (
        admissions_source is None
        or "hadm_id" not in admissions_source.columns
        or "dischtime" not in admissions_source.columns
    ):
        raise TemporalPolicyError(
            f"{task}: admissions.hadm_id/dischtime are required for the "
            "readmission episode boundary"
        )

    admissions = clean_missing_values(admissions_source)
    resolved_hadm_id = normalize_record_id(index_hadm_id)
    if resolved_hadm_id is None:
        discharge_source = source_tables.get("discharge")
        if (
            discharge_source is None
            or "hadm_id" not in discharge_source.columns
            or "charttime" not in discharge_source.columns
        ):
            raise TemporalPolicyError(
                f"{task}: missing index hadm_id cannot be inferred without "
                "discharge.hadm_id/charttime"
            )
        discharge = clean_missing_values(discharge_source)
        charttime = parse_time_column(discharge, "charttime")
        expected_charttime = requested_cutoff - pd.Timedelta(seconds=1)
        exact = discharge.loc[charttime == expected_charttime, "hadm_id"]
        inferred = {
            normalized
            for value in exact.tolist()
            if (normalized := normalize_record_id(value)) is not None
        }
        if len(inferred) != 1:
            raise TemporalPolicyError(
                f"{task}: missing index hadm_id has {len(inferred)} exact "
                "discharge-note candidate(s); refusing an ambiguous episode"
            )
        resolved_hadm_id = next(iter(inferred))

    admission_ids = admissions["hadm_id"].map(normalize_record_id)
    matches = admissions.loc[admission_ids == resolved_hadm_id].copy()
    dischtimes = parse_time_column(matches, "dischtime").dropna().unique()
    if len(dischtimes) != 1:
        raise TemporalPolicyError(
            f"{task}: index hadm_id {resolved_hadm_id!r} has "
            f"{len(dischtimes)} unambiguous dischtime value(s)"
        )
    index_dischtime = pd.Timestamp(dischtimes[0])
    if index_dischtime.normalize() != requested_cutoff.normalize():
        raise TemporalPolicyError(
            f"{task}: index admission discharge date {index_dischtime.date()} "
            f"does not match benchmark event date {requested_cutoff.date()}"
        )

    return ReadmissionEpisodeScope(
        task=str(task).strip(),
        requested_cutoff=requested_cutoff,
        index_hadm_id=resolved_hadm_id,
        index_dischtime=index_dischtime,
    )


def redact_field(
    df: pd.DataFrame,
    guard: FieldGuard,
    cutoff: pd.Timestamp,
) -> int:
    """Set a field to null unless its availability timestamp is pre-cutoff."""

    if guard.field not in df.columns:
        return 0
    available = coalesce_times(df, guard.available_at)
    unsafe = nonempty_mask(df[guard.field]) & (
        available.isna() | (available >= cutoff)
    )
    count = int(unsafe.sum())
    if count:
        df.loc[unsafe, guard.field] = None
    return count


def sanitize_all_timestamp_values(
    df: pd.DataFrame,
    cutoff: pd.Timestamp,
) -> tuple[int, int]:
    """Redact any remaining future or unparseable timestamp-like value."""

    future_count = 0
    unparseable_count = 0
    for column in df.columns:
        name = column.lower()
        if "time" not in name and "date" not in name and name != "dod":
            continue
        present = nonempty_mask(df[column])
        parsed = parse_time_column(df, column)
        future = present & parsed.notna() & (parsed >= cutoff)
        unparseable = present & parsed.isna()
        if future.any():
            df.loc[future, column] = None
            future_count += int(future.sum())
        if unparseable.any():
            df.loc[unparseable, column] = None
            unparseable_count += int(unparseable.sum())
    return future_count, unparseable_count


def validate_source_schema(
    schema: Mapping[str, set[str]],
    unknown_table_policy: str = "error",
) -> None:
    """Reject unknown retained tables/columns and missing policy time keys."""

    if unknown_table_policy not in {"error", "drop"}:
        raise TemporalPolicyError(
            f"Unsupported unknown_table_policy: {unknown_table_policy!r}"
        )
    known = set(TABLE_POLICIES) | set(DROP_TABLES) | set(CHILD_POLICIES)
    unknown_tables = sorted(set(schema) - known)
    if unknown_tables and unknown_table_policy == "error":
        raise TemporalPolicyError(
            "No strict temporal policy exists for table(s): "
            + ", ".join(unknown_tables)
        )

    for table_name, columns in schema.items():
        expected = EXPECTED_SOURCE_COLUMNS.get(table_name)
        if expected is None:
            # Fully dropped and unknown tables cannot expose their columns.
            continue
        unexpected = sorted(columns - expected)
        if unexpected:
            raise TemporalPolicyError(
                f"{table_name}: column(s) have no temporal policy: "
                + ", ".join(unexpected)
            )
        policy = TABLE_POLICIES.get(table_name)
        if policy is not None:
            if policy.event_at and not set(policy.event_at) & columns:
                raise TemporalPolicyError(
                    f"{table_name}: required event time missing; expected one of "
                    f"{policy.event_at}"
                )
            if policy.available_at and not set(policy.available_at) & columns:
                raise TemporalPolicyError(
                    f"{table_name}: required availability time missing; "
                    f"expected one of {policy.available_at}"
                )
        child_policy = CHILD_POLICIES.get(table_name)
        if child_policy is not None:
            missing = sorted(set(child_policy.keys) - columns)
            if missing:
                raise TemporalPolicyError(
                    f"{table_name}: parent-link column(s) missing: "
                    + ", ".join(missing)
                )


def apply_table_policy(
    table_name: str,
    source: pd.DataFrame,
    policy: TablePolicy,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, TableReport]:
    """Apply row filters and field guards to one directly handled table."""

    df = clean_missing_values(source)
    report = TableReport(
        input_rows=len(df),
        policy=policy.note or "Explicit table-aware temporal policy.",
    )

    if policy.event_at:
        event_time = coalesce_times(df, policy.event_at)
        df = df.loc[event_time.notna() & (event_time < cutoff)].copy()

    if policy.available_at and not df.empty:
        available_time = availability_times(df, policy)
        df = df.loc[
            available_time.notna() & (available_time < cutoff)
        ].copy()

    if policy.completion_at and not df.empty:
        completed = pd.Series(True, index=df.index, dtype=bool)
        for column in policy.completion_at:
            parsed = parse_time_column(df, column)
            completed &= parsed.notna() & (parsed < cutoff)
        for column in policy.completion_fields:
            if column not in df.columns:
                continue
            unsafe = ~completed & nonempty_mask(df[column])
            report.redacted_values += int(unsafe.sum())
            if unsafe.any():
                df.loc[unsafe, column] = None

    for guard in policy.guards:
        report.redacted_values += redact_field(df, guard, cutoff)

    for column in policy.drop_columns:
        if column in df.columns:
            report.redacted_values += int(nonempty_mask(df[column]).sum())
            df = df.drop(columns=[column])
            report.dropped_columns.append(column)

    future, unparseable = sanitize_all_timestamp_values(df, cutoff)
    report.future_time_values_redacted = future
    report.unparseable_time_values_redacted = unparseable
    report.redacted_values += future + unparseable
    report.output_rows = len(df)
    report.dropped_rows = report.input_rows - report.output_rows
    return df.reset_index(drop=True), report


def restore_readmission_index_discharge(
    source: pd.DataFrame,
    safe: pd.DataFrame,
    report: TableReport,
    scope: ReadmissionEpisodeScope,
) -> tuple[pd.DataFrame, TableReport]:
    """Expose only the index episode's discharge summary for readmission.

    The note text is an explicitly declared task input. A source ``storetime``
    at or after the episode boundary is not rewritten as if it were observed
    earlier: it is redacted. ``episode_available_time`` records the benchmark
    episode boundary used by time-aware retrieval tools.
    """

    current = clean_missing_values(source)
    if "hadm_id" not in current.columns or "charttime" not in current.columns:
        return safe, report

    ids = current["hadm_id"].map(normalize_record_id)
    charttime = parse_time_column(current, "charttime")
    current = current.loc[
        (ids == scope.index_hadm_id)
        & charttime.notna()
        & (charttime < scope.effective_cutoff)
    ].copy()
    if current.empty:
        return safe, report

    redacted_storetimes = 0
    if "storetime" in current.columns:
        storetime = parse_time_column(current, "storetime")
        unsafe_storetime = nonempty_mask(current["storetime"]) & (
            storetime.isna() | (storetime >= scope.effective_cutoff)
        )
        redacted_storetimes = int(unsafe_storetime.sum())
        if unsafe_storetime.any():
            current.loc[unsafe_storetime, "storetime"] = None

    current[READMISSION_DISCHARGE_TIME_COLUMN] = scope.index_dischtime
    combined = safe.copy()
    if READMISSION_DISCHARGE_TIME_COLUMN not in combined.columns:
        combined[READMISSION_DISCHARGE_TIME_COLUMN] = pd.NaT

    # Replace, rather than duplicate, an index note that happened to satisfy
    # the ordinary storetime rule already.
    if "hadm_id" in combined.columns:
        combined_ids = combined["hadm_id"].map(normalize_record_id)
        combined = combined.loc[combined_ids != scope.index_hadm_id].copy()
    combined = pd.concat([combined, current], ignore_index=True, sort=False)

    report.output_rows = len(combined)
    report.dropped_rows = report.input_rows - report.output_rows
    report.redacted_values += redacted_storetimes
    report.episode_override_rows = len(current)
    report.policy += (
        " Readmission episode override: expose only the index hadm_id "
        "discharge summary; redact late source storetime and derive "
        f"{READMISSION_DISCHARGE_TIME_COLUMN}=index dischtime."
    )
    return combined.reset_index(drop=True), report


def _visible_parent_rows(
    source: pd.DataFrame,
    policy: ChildPolicy,
    safe_tables: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    """Semi-join ``source`` through visible parent keys."""

    parent = safe_tables.get(policy.parent)
    if parent is None:
        return source.iloc[0:0].copy()
    if not all(
        key in source.columns and key in parent.columns for key in policy.keys
    ):
        return source.iloc[0:0].copy()
    parent_keys = parent[list(policy.keys)].dropna().drop_duplicates()
    parent_keys = parent_keys.assign(__strict_parent_visible=1)
    merged = source.merge(parent_keys, on=list(policy.keys), how="left")
    safe = merged.loc[
        merged["__strict_parent_visible"] == 1,
        list(source.columns),
    ]
    return safe.copy()


def _visible_parent_times(
    policy: ChildPolicy,
    safe_tables: Mapping[str, pd.DataFrame],
    cutoff: pd.Timestamp,
) -> pd.DataFrame:
    """Return unambiguous visible parent keys and their derived child time."""

    parent = safe_tables.get(policy.parent)
    output_column = policy.derived_time_column
    if (
        parent is None
        or output_column is None
        or not policy.parent_time
        or not all(key in parent.columns for key in policy.keys)
    ):
        return pd.DataFrame(
            columns=[*policy.keys, "__strict_derived_time"]
        )

    parent_times = coalesce_times(parent, policy.parent_time)
    lookup = parent[list(policy.keys)].copy()
    lookup["__strict_derived_time"] = parent_times + pd.Timedelta(
        seconds=policy.offset_seconds
    )
    lookup = lookup.dropna(
        subset=[*policy.keys, "__strict_derived_time"]
    )
    lookup = lookup.loc[
        lookup["__strict_derived_time"] < cutoff
    ].drop_duplicates()

    # A child key with conflicting parent times cannot be assigned a unique
    # point-in-time value and therefore fails closed.
    ambiguous = lookup.duplicated(subset=list(policy.keys), keep=False)
    return lookup.loc[~ambiguous].reset_index(drop=True)


def filter_child_table(
    source: pd.DataFrame,
    policy: ChildPolicy,
    safe_tables: Mapping[str, pd.DataFrame],
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, TableReport]:
    """Filter an untimestamped child table through its safe parent."""

    child = clean_missing_values(source)
    if policy.derived_time_column is not None:
        parent_times = _visible_parent_times(
            policy,
            safe_tables,
            cutoff,
        )
        if not all(key in child.columns for key in policy.keys):
            safe = child.iloc[0:0].copy()
            safe[policy.derived_time_column] = pd.Series(
                dtype="datetime64[ns]"
            )
        else:
            safe = child.merge(
                parent_times,
                on=list(policy.keys),
                how="inner",
            )
            safe[policy.derived_time_column] = safe.pop(
                "__strict_derived_time"
            )
    else:
        safe = _visible_parent_rows(child, policy, safe_tables)

    if policy.row_allowlist_column is not None:
        column = policy.row_allowlist_column
        if column not in safe.columns:
            safe = safe.iloc[0:0].copy()
        else:
            safe = safe.loc[
                safe[column].isin(policy.row_allowlist_values)
            ].copy()

    dropped_columns: list[str] = []
    if policy.keep_columns:
        keep = [
            column
            for column in policy.keep_columns
            if column in safe.columns
        ]
        dropped_columns = sorted(set(safe.columns) - set(keep))
        safe = safe.loc[:, keep].copy()

    if policy.require_nonempty_columns and not safe.empty:
        present = [
            column
            for column in policy.require_nonempty_columns
            if column in safe.columns
        ]
        if not present:
            safe = safe.iloc[0:0].copy()
        else:
            informative = pd.Series(False, index=safe.index, dtype=bool)
            for column in present:
                informative |= nonempty_mask(safe[column])
            safe = safe.loc[informative].copy()

    future, unparseable = sanitize_all_timestamp_values(safe, cutoff)
    report = TableReport(
        input_rows=len(child),
        output_rows=len(safe),
        dropped_rows=len(child) - len(safe),
        redacted_values=future + unparseable,
        future_time_values_redacted=future,
        unparseable_time_values_redacted=unparseable,
        dropped_columns=dropped_columns,
        policy=policy.note or f"Parent-filtered through {policy.parent}.",
    )
    return safe.reset_index(drop=True), report


def mask_target_code_rows(
    table_name: str,
    safe: pd.DataFrame,
    report: TableReport,
    scope: TargetCodeScope | None,
) -> tuple[pd.DataFrame, TableReport]:
    """Remove the target encounter's answer rows after ordinary filtering."""

    if scope is None or table_name != scope.table:
        return safe, report
    if scope.key not in safe.columns:
        raise TemporalPolicyError(
            f"{scope.task}: {table_name}.{scope.key} is required for target "
            "encounter masking"
        )

    ids = safe[scope.key].map(normalize_record_id)
    target = ids == scope.encounter_id
    removed = int(target.sum())
    if removed:
        safe = safe.loc[~target].copy()
    report.output_rows = len(safe)
    report.dropped_rows = report.input_rows - report.output_rows
    report.target_masked_rows = removed
    report.policy += (
        f" Target-label guard: permanently exclude {scope.key}="
        f"{scope.encounter_id!r} for task {scope.task}."
    )
    return safe.reset_index(drop=True), report


def _apply_parent_constraints(
    safe_tables: dict[str, pd.DataFrame],
    reports: dict[str, TableReport],
) -> None:
    """Apply parent visibility constraints to already time-filtered tables."""

    for table_name, constraint in PARENT_CONSTRAINTS.items():
        source = safe_tables.get(table_name)
        if source is None:
            continue
        safe = _visible_parent_rows(source, constraint, safe_tables)
        report = reports[table_name]
        removed = len(source) - len(safe)
        report.output_rows = len(safe)
        report.dropped_rows += removed
        if removed:
            suffix = (
                f" Parent constraint: {constraint.parent}"
                f"({', '.join(constraint.keys)})."
            )
            report.policy += suffix
        safe_tables[table_name] = safe.reset_index(drop=True)


def _key_tuples(
    df: pd.DataFrame,
    keys: tuple[str, ...],
) -> set[tuple[str, ...]]:
    if not all(key in df.columns for key in keys):
        return set()
    values = df[list(keys)].dropna()
    return {
        tuple(str(value).strip() for value in row)
        for row in values.itertuples(index=False, name=None)
    }


def audit_runtime_view(
    safe_tables: Mapping[str, pd.DataFrame],
    cutoff: pd.Timestamp,
    target_code_scope: TargetCodeScope | None = None,
) -> list[str]:
    """Return all violations that remain after filtering."""

    violations: list[str] = []

    if target_code_scope is not None:
        target_table = safe_tables.get(target_code_scope.table)
        if target_table is not None:
            if target_code_scope.key not in target_table.columns:
                violations.append(
                    f"{target_code_scope.table}: target-mask key "
                    f"{target_code_scope.key} is absent"
                )
            else:
                target_ids = target_table[target_code_scope.key].map(
                    normalize_record_id
                )
                exposed = target_ids == target_code_scope.encounter_id
                if exposed.any():
                    violations.append(
                        f"{target_code_scope.table}: {int(exposed.sum())} "
                        f"target-code row(s) remain visible for "
                        f"{target_code_scope.task}"
                    )
    allowed = set(TABLE_POLICIES) | set(CHILD_POLICIES)
    unknown = sorted(set(safe_tables) - allowed)
    if unknown:
        violations.append(
            "Tables without a strict output policy are present: "
            + ", ".join(unknown)
        )

    for dropped in DROP_TABLES:
        if dropped in safe_tables:
            violations.append(f"Dropped table unexpectedly present: {dropped}")

    for table_name, df in safe_tables.items():
        expected = EXPECTED_SOURCE_COLUMNS.get(table_name)
        if expected is None:
            violations.append(f"{table_name}: no output column allowlist exists")
        else:
            allowed_output = set(expected)
            allowed_output.update(DERIVED_OUTPUT_COLUMNS.get(table_name, set()))
            child_policy = CHILD_POLICIES.get(table_name)
            if (
                child_policy is not None
                and child_policy.derived_time_column is not None
            ):
                allowed_output.add(child_policy.derived_time_column)
            unexpected = sorted(set(df.columns) - allowed_output)
            if unexpected:
                violations.append(
                    f"{table_name}: unhandled output columns: "
                    + ", ".join(unexpected)
                )
        policy = TABLE_POLICIES.get(table_name)
        if policy is not None:
            forbidden_output = sorted(
                set(policy.drop_columns) & set(df.columns)
            )
            if forbidden_output:
                violations.append(
                    f"{table_name}: required dropped columns remain: "
                    + ", ".join(forbidden_output)
                )
            if policy.completion_at and not df.empty:
                completed = pd.Series(True, index=df.index, dtype=bool)
                for column in policy.completion_at:
                    parsed = parse_time_column(df, column)
                    completed &= parsed.notna() & (parsed < cutoff)
                for column in policy.completion_fields:
                    if column not in df.columns:
                        continue
                    exposed = ~completed & nonempty_mask(df[column])
                    if exposed.any():
                        violations.append(
                            f"{table_name}.{column}: "
                            f"{int(exposed.sum())} completion-dependent "
                            "value(s) visible on ongoing rows"
                        )
        child_policy = CHILD_POLICIES.get(table_name)
        if child_policy is not None and child_policy.keep_columns:
            unexpected = sorted(
                set(df.columns) - set(child_policy.keep_columns)
            )
            if unexpected:
                violations.append(
                    f"{table_name}: column(s) outside child whitelist: "
                    + ", ".join(unexpected)
                )
        forbidden = sorted(FORBIDDEN_EHR_COLUMNS & set(df.columns))
        if forbidden:
            violations.append(
                f"{table_name}: benchmark answer columns present: {forbidden}"
            )
        for column in df.columns:
            name = column.lower()
            if "time" not in name and "date" not in name and name != "dod":
                continue
            present = nonempty_mask(df[column])
            parsed = parse_time_column(df, column)
            unparseable = present & parsed.isna()
            future = present & parsed.notna() & (parsed >= cutoff)
            if unparseable.any():
                violations.append(
                    f"{table_name}.{column}: {int(unparseable.sum())} "
                    "unparseable value(s)"
                )
            if future.any():
                violations.append(
                    f"{table_name}.{column}: {int(future.sum())} "
                    "value(s) at/after cutoff"
                )

    admissions = safe_tables.get("admissions")
    if admissions is not None and "dischtime" in admissions.columns:
        discharged = parse_time_column(admissions, "dischtime").notna()
        for field_name in ("discharge_location", "hospital_expire_flag"):
            if field_name not in admissions.columns:
                continue
            orphaned = nonempty_mask(admissions[field_name]) & ~discharged
            if orphaned.any():
                violations.append(
                    f"admissions.{field_name}: {int(orphaned.sum())} "
                    "value(s) visible without a safe dischtime"
                )

    icustays = safe_tables.get("icustays")
    if icustays is not None and "outtime" in icustays.columns:
        completed = parse_time_column(icustays, "outtime").notna()
        for field_name in ("los", "last_careunit"):
            if field_name not in icustays.columns:
                continue
            orphaned = nonempty_mask(icustays[field_name]) & ~completed
            if orphaned.any():
                violations.append(
                    f"icustays.{field_name}: {int(orphaned.sum())} "
                    "value(s) visible without a safe outtime"
                )

    all_parent_rules = dict(CHILD_POLICIES)
    all_parent_rules.update(PARENT_CONSTRAINTS)
    for child_name, policy in all_parent_rules.items():
        child = safe_tables.get(child_name)
        if child is None or child.empty:
            continue
        parent = safe_tables.get(policy.parent)
        if parent is None:
            violations.append(
                f"{child_name}: rows exist but parent {policy.parent} is absent"
            )
            continue
        child_keys = _key_tuples(child, policy.keys)
        parent_keys = _key_tuples(parent, policy.keys)
        orphaned = child_keys - parent_keys
        if orphaned:
            violations.append(
                f"{child_name}: {len(orphaned)} parent key(s) are not visible"
            )

        if policy.derived_time_column is not None:
            parent_times = _visible_parent_times(
                policy,
                safe_tables,
                cutoff,
            )
            checked = child.merge(
                parent_times,
                on=list(policy.keys),
                how="left",
            )
            actual = parse_time_column(
                checked,
                policy.derived_time_column,
            )
            expected_time = checked["__strict_derived_time"]
            inconsistent = (
                actual.isna()
                | expected_time.isna()
                | (actual != expected_time)
            )
            if inconsistent.any():
                violations.append(
                    f"{child_name}.{policy.derived_time_column}: "
                    f"{int(inconsistent.sum())} value(s) do not match "
                    f"{policy.parent}.{policy.parent_time} plus "
                    f"{policy.offset_seconds} second(s)"
                )

    return violations


def build_runtime_view(
    source_tables: Mapping[str, pd.DataFrame],
    cutoff: Any,
    *,
    task: str | None = None,
    index_hadm_id: Any = None,
    target_hadm_id: Any = None,
    target_stay_id: Any = None,
    unknown_table_policy: str = "error",
) -> TemporalViewResult:
    """Build and audit a strict question-specific MIMIC-IV runtime view."""

    requested_cutoff = normalize_cutoff(cutoff)
    target_code_scope = resolve_target_code_scope(
        task,
        target_hadm_id,
        target_stay_id,
    )
    readmission_scope = None
    normalized_cutoff = requested_cutoff
    if is_readmission_task(task):
        readmission_scope = resolve_readmission_episode_scope(
            source_tables,
            requested_cutoff,
            str(task),
            index_hadm_id,
        )
        normalized_cutoff = readmission_scope.effective_cutoff
    validate_source_schema(
        {
            table_name: set(source.columns)
            for table_name, source in source_tables.items()
        },
        unknown_table_policy=unknown_table_policy,
    )

    safe_tables: dict[str, pd.DataFrame] = {}
    reports: dict[str, TableReport] = {}

    # Direct/static tables are built before dependent child tables.
    for table_name, source in source_tables.items():
        if table_name in DROP_TABLES:
            reports[table_name] = TableReport(
                input_rows=len(source),
                output_rows=0,
                dropped_rows=len(source),
                policy="Dropped: " + DROP_TABLES[table_name],
            )
            continue
        if table_name in CHILD_POLICIES:
            continue
        policy = TABLE_POLICIES.get(table_name)
        if policy is None:
            reports[table_name] = TableReport(
                input_rows=len(source),
                output_rows=0,
                dropped_rows=len(source),
                policy="Dropped: unknown table under fail-closed policy.",
            )
            continue
        safe, report = apply_table_policy(
            table_name,
            source,
            policy,
            normalized_cutoff,
        )
        safe_tables[table_name] = safe
        reports[table_name] = report

    if readmission_scope is not None and "discharge" in source_tables:
        restored, report = restore_readmission_index_discharge(
            source_tables["discharge"],
            safe_tables.get(
                "discharge",
                source_tables["discharge"].iloc[0:0].copy(),
            ),
            reports.get(
                "discharge",
                TableReport(input_rows=len(source_tables["discharge"])),
            ),
            readmission_scope,
        )
        safe_tables["discharge"] = restored
        reports["discharge"] = report

    _apply_parent_constraints(safe_tables, reports)

    for table_name, policy in CHILD_POLICIES.items():
        source = source_tables.get(table_name)
        if source is None:
            continue
        safe, report = filter_child_table(
            source,
            policy,
            safe_tables,
            normalized_cutoff,
        )
        safe, report = mask_target_code_rows(
            table_name,
            safe,
            report,
            target_code_scope,
        )
        safe_tables[table_name] = safe
        reports[table_name] = report

    violations = audit_runtime_view(
        safe_tables,
        normalized_cutoff,
        target_code_scope=target_code_scope,
    )
    if violations:
        raise TemporalPolicyError(
            "Strict runtime view audit failed:\n- " + "\n- ".join(violations)
        )

    return TemporalViewResult(
        tables=safe_tables,
        reports=reports,
        cutoff=normalized_cutoff,
        requested_cutoff=requested_cutoff,
        boundary_mode=(
            "readmission_index_episode"
            if readmission_scope is not None
            else "point_in_time"
        ),
        task=str(task).strip() if task is not None else None,
        index_hadm_id=(
            readmission_scope.index_hadm_id
            if readmission_scope is not None
            else normalize_record_id(index_hadm_id)
        ),
        index_dischtime=(
            readmission_scope.index_dischtime
            if readmission_scope is not None
            else None
        ),
        target_hadm_id=(
            target_code_scope.encounter_id
            if target_code_scope is not None
            and target_code_scope.key == "hadm_id"
            else None
        ),
        target_stay_id=(
            target_code_scope.encounter_id
            if target_code_scope is not None
            and target_code_scope.key == "stay_id"
            else None
        ),
    )
