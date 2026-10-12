"""Fail-closed manifest checks for multimodal agentic and curated runners."""

from typing import Any, Dict, List


STRICT_POLICY_VERSION = "multimodal-strict-temporal-scope-v1"


def validate_agentic_manifest(items: List[Dict[str, Any]]) -> None:
    """Require trusted prediction-time and CXR-scope metadata."""
    failures = []
    for item in items:
        benchmark = str(item.get("source_benchmark") or "").lower()
        if benchmark not in {"ehrxqa", "medmod"}:
            continue
        missing = [
            field
            for field in ("subject_id", "prediction_time", "scope", "modalities")
            if item.get(field) in (None, "", [])
        ]
        task = str(item.get("task") or item.get("source_task") or "")
        needs_linked_cxr = (
            task.startswith("medmod_")
            or task in {"ehrxqa_image", "ehrxqa_image_table"}
        )
        if needs_linked_cxr and not item.get("study_ids"):
            missing.append("study_ids")
        if missing:
            failures.append(
                {
                    "qid": item.get("qid"),
                    "missing": sorted(set(missing)),
                }
            )
    if failures:
        raise ValueError(
            "Unsafe multimodal manifest: trusted temporal/scope metadata is "
            f"missing for {len(failures)} row(s), e.g. {failures[:3]}. "
            "Use ClinSeek_MM_Bench_image_subset_strict_v1.jsonl or the "
            "benchmark ready manifests."
        )


def validate_curated_manifest(items: List[Dict[str, Any]]) -> None:
    """Reject pre-rendered multimodal text not produced by the strict view."""
    unsafe = [
        item.get("qid")
        for item in items
        if str(item.get("source_benchmark") or "").lower()
        in {"ehrxqa", "medmod"}
        and item.get("input_text")
        and item.get("leakage_policy_version") != STRICT_POLICY_VERSION
    ]
    if unsafe:
        raise ValueError(
            "Refusing pre-rendered multimodal input_text without the strict "
            "leakage policy marker. Use "
            "ClinSeek_MM_Bench_image_subset_strict_v1.jsonl. "
            f"Unsafe rows: {len(unsafe)}, e.g. {unsafe[:3]}"
        )
