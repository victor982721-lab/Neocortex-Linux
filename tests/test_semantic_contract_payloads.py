# region [00] Contexto del módulo
# Módulo: tests/test_semantic_contract_payloads.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import os
from dataclasses import replace
from pathlib import Path

from pytest import MonkeyPatch

from neocortex.semantic import semantic_planner, semantic_service
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128
from neocortex.semantic.semantic_service_contracts import (
    SemanticPlan,
    SemanticSourcePlan,
    SemanticWorkloadPlan,
)
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

import pytest


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


EXPECTED_PLAN_KEYS = {
    "complete",
    "content_set_xxh3_128",
    "cost_calibrated",
    "cost_complete",
    "dry_run",
    "embedding_entities",
    "estimate_kind",
    "estimated_model_seconds",
    "estimated_model_seconds_lower_bound",
    "estimated_model_seconds_upper_bound",
    "execution_ready",
    "input_bytes",
    "jobs_created",
    "max_scratch_bytes",
    "model_request_contents_lower_bound",
    "model_request_contents_upper_bound",
    "new_unique_contents",
    "new_vector_blob_bytes_lower_bound",
    "originals_verified",
    "plan_signature",
    "resources",
    "reusable_unique_contents",
    "scope",
    "scratch_storage_bytes",
    "section_text_bytes",
    "sections",
    "selected_sources",
    "semantic_database",
    "semantic_schema_version",
    "semantic_snapshot_xxh3_128",
    "snapshot_scope",
    "source_bytes",
    "sources",
    "sqlite_read_snapshot_may_touch_shm",
    "state_mutated",
    "text_chunking_signature",
    "unique_contents",
    "unique_input_bytes",
    "vector_bytes_kind",
    "workloads",
}

EXPECTED_SOURCE_KEYS = {
    "chunks",
    "database",
    "embedding_entities",
    "input_bytes",
    "resources",
    "schema_version",
    "section_text_bytes",
    "sections",
    "source_bytes",
    "source_kind",
    "snapshot_xxh3_128",
}

EXPECTED_WORKLOAD_KEYS = {
    "cost_calibrated",
    "cost_calibration_signature",
    "cost_calibration_contents_per_second",
    "cost_calibration_sample_contents",
    "cost_calibration_sample_input_bytes",
    "cost_complete",
    "cost_execution_signature",
    "cost_unavailable_reason",
    "distance",
    "dimensions",
    "embedding_entities",
    "estimated_model_seconds",
    "estimated_model_seconds_lower_bound",
    "estimated_model_seconds_upper_bound",
    "input_bytes",
    "modality",
    "model_id",
    "model_provenance_json",
    "model_signature",
    "model_version",
    "model_request_contents_lower_bound",
    "model_request_contents_upper_bound",
    "name",
    "new_unique_contents",
    "new_vector_blob_bytes_lower_bound",
    "planned_reusable_contents",
    "preexisting_reusable_contents",
    "processing_signature",
    "provider",
    "role",
    "supported_roles",
    "unique_contents",
    "unique_input_bytes",
    "normalization",
    "vector_dtype",
    "vector_space",
}


def _plan_signature(plan: SemanticPlan) -> str:
    preimage = semantic_planner._plan_payload_for_signature(
        scope=plan.scope,
        selected_sources=plan.selected_sources,
        semantic_schema_version=plan.semantic_schema_version,
        source_plans=plan.source_plans,
        workloads=plan.workloads,
        chunking_signature=plan.text_chunking_signature,
        content_set_xxh3_128=plan.content_set_xxh3_128,
        semantic_snapshot_xxh3_128=plan.semantic_snapshot_xxh3_128,
    )
    return (
        f"{semantic_planner.PLAN_ALGORITHM_VERSION}:{HASH_ALGORITHM_128}:"
        f"{fingerprint_text(canonical_json(preimage)).xxh3_128}"
    )


def _plan() -> SemanticPlan:
    source = SemanticSourcePlan(
        "pdf",
        Path("C:/fixture/pdf.sqlite3"),
        11,
        1,
        1,
        1,
        1,
        100,
        20,
        20,
        "0" * 32,
    )
    workload = SemanticWorkloadPlan(
        "text",
        "text",
        "passage",
        "model-signature",
        "vector-space",
        "model-id",
        "1.0",
        4,
        "fixture-provider",
        ("query", "passage"),
        "float16",
        "l2",
        "cosine",
        canonical_json({"model": "fixture"}),
        "processing-signature",
        1,
        1,
        0,
        0,
        1,
        20,
        20,
        8,
        1,
        1,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "no_exact_cost_calibration",
    )
    plan = SemanticPlan(
        "text",
        ("pdf",),
        Path("C:/fixture/semantic.sqlite3"),
        6,
        (source,),
        (workload,),
        "chunking-v1",
        "1" * 32,
        "2" * 32,
        "placeholder",
        1,
        1,
        1,
        1,
        100,
        20,
        20,
        1,
        20,
        0,
        1,
        8,
        1,
        1,
        None,
        None,
        4096,
        8192,
        None,
        None,
    )
    return replace(plan, plan_signature=_plan_signature(plan))


def test_planner_payload_public_wrapper_identity_is_stable() -> None:
    assert str(inspect.signature(semantic_planner.semantic_plan_payload)) == (
        "(plan: 'SemanticPlan') -> 'dict[str, object]'"
    )
    assert semantic_planner.semantic_plan_payload.__module__ == (
        "neocortex.semantic.semantic_planner"
    )
    assert semantic_planner.__all__ == [
        "CONTENT_BATCH_SIZE",
        "DEFAULT_MAX_SCRATCH_BYTES",
        "PLAN_ALGORITHM_VERSION",
        "SemanticPlanBlocked",
        "SemanticScratchLimitExceeded",
        "plan_semantic_index",
        "semantic_plan_payload",
    ]
    assert semantic_service.semantic_plan_payload is not (semantic_planner.semantic_plan_payload)
    assert semantic_service.semantic_plan_payload.__module__ == (
        "neocortex.semantic.semantic_service"
    )
    assert str(inspect.signature(semantic_service.semantic_plan_payload)) == (
        "(plan: 'SemanticPlan') -> 'dict[str, object]'"
    )


def test_service_payload_wrapper_forwards_without_becoming_an_alias(
    monkeypatch: MonkeyPatch,
) -> None:
    plan = _plan()
    sentinel: dict[str, object] = {"sentinel": True}
    calls: list[SemanticPlan] = []

    def fake_payload(received: SemanticPlan) -> dict[str, object]:
        calls.append(received)
        return sentinel

    monkeypatch.setattr(semantic_service._planner, "semantic_plan_payload", fake_payload)
    assert semantic_service.semantic_plan_payload(plan) is sentinel
    assert calls == [plan]
    assert semantic_service.semantic_plan_payload is not fake_payload


def test_representative_payload_keysets_and_byte_golden_are_stable() -> None:
    plan = _plan()
    payload = semantic_planner.semantic_plan_payload(plan)
    assert set(payload) == EXPECTED_PLAN_KEYS
    assert len(payload) == 40
    source_payloads = payload["sources"]
    workload_payloads = payload["workloads"]
    assert isinstance(source_payloads, list)
    assert isinstance(workload_payloads, list)
    assert set(source_payloads[0]) == EXPECTED_SOURCE_KEYS
    assert len(source_payloads[0]) == 11
    assert set(workload_payloads[0]) == EXPECTED_WORKLOAD_KEYS
    assert len(workload_payloads[0]) == 36
    assert tuple(payload) == (
        "complete",
        "content_set_xxh3_128",
        "cost_calibrated",
        "cost_complete",
        "dry_run",
        "embedding_entities",
        "estimate_kind",
        "estimated_model_seconds",
        "estimated_model_seconds_lower_bound",
        "estimated_model_seconds_upper_bound",
        "execution_ready",
        "input_bytes",
        "jobs_created",
        "max_scratch_bytes",
        "model_request_contents_lower_bound",
        "model_request_contents_upper_bound",
        "new_unique_contents",
        "new_vector_blob_bytes_lower_bound",
        "originals_verified",
        "plan_signature",
        "resources",
        "reusable_unique_contents",
        "scope",
        "scratch_storage_bytes",
        "section_text_bytes",
        "sections",
        "selected_sources",
        "semantic_database",
        "semantic_schema_version",
        "semantic_snapshot_xxh3_128",
        "snapshot_scope",
        "source_bytes",
        "sources",
        "sqlite_read_snapshot_may_touch_shm",
        "state_mutated",
        "text_chunking_signature",
        "unique_contents",
        "unique_input_bytes",
        "vector_bytes_kind",
        "workloads",
    )
    assert tuple(source_payloads[0]) == (
        "chunks",
        "database",
        "embedding_entities",
        "input_bytes",
        "resources",
        "schema_version",
        "section_text_bytes",
        "sections",
        "source_bytes",
        "source_kind",
        "snapshot_xxh3_128",
    )
    assert tuple(workload_payloads[0]) == (
        "cost_calibrated",
        "cost_calibration_signature",
        "cost_calibration_contents_per_second",
        "cost_calibration_sample_contents",
        "cost_calibration_sample_input_bytes",
        "cost_complete",
        "cost_execution_signature",
        "cost_unavailable_reason",
        "distance",
        "dimensions",
        "embedding_entities",
        "estimated_model_seconds",
        "estimated_model_seconds_lower_bound",
        "estimated_model_seconds_upper_bound",
        "input_bytes",
        "modality",
        "model_id",
        "model_provenance_json",
        "model_signature",
        "model_version",
        "model_request_contents_lower_bound",
        "model_request_contents_upper_bound",
        "name",
        "new_unique_contents",
        "new_vector_blob_bytes_lower_bound",
        "planned_reusable_contents",
        "preexisting_reusable_contents",
        "processing_signature",
        "provider",
        "role",
        "supported_roles",
        "unique_contents",
        "unique_input_bytes",
        "normalization",
        "vector_dtype",
        "vector_space",
    )
    expected_separator = "\\" if os.name == "nt" else "/"
    assert payload["semantic_database"] == (
        f"C:{expected_separator}fixture{expected_separator}semantic.sqlite3"
    )
    assert source_payloads[0]["database"] == (
        f"C:{expected_separator}fixture{expected_separator}pdf.sqlite3"
    )
    assert payload["selected_sources"] == ["pdf"]
    assert workload_payloads[0]["supported_roles"] == ["query", "passage"]
    assert plan.plan_signature == _plan_signature(plan)
    encoded = canonical_json(payload).encode("utf-8")
    expected_golden = {
        ("nt", "xxh3-128"): (
            2683,
            "7f9bd971da15b3fb7ce2f1f1128721102195b43b8c7155e0eca5020f4b24110a",
        ),
        ("posix", "xxh3-128"): (
            2679,
            "430ca856b97f5d3518864e120fbdb812ce2c1bdce281b98aafac5266e5870715",
        ),
        ("posix", "sha256-128-fallback-v1"): (
            2693,
            "be679c90f8ce52d014eb27a208ecfeb5972f68eafde40a5d2ea079959ab660c6",
        ),
    }
    expected_length, expected_digest = expected_golden[(
        "nt" if os.name == "nt" else "posix",
        HASH_ALGORITHM_128,
    )]
    assert len(encoded) == expected_length
    assert hashlib.sha256(encoded).hexdigest() == expected_digest
    assert semantic_service.semantic_plan_payload(plan) == payload


def test_signature_preimage_is_private_separate_and_byte_stable() -> None:
    plan = _plan()
    preimage = semantic_planner._plan_payload_for_signature(
        scope=plan.scope,
        selected_sources=plan.selected_sources,
        semantic_schema_version=plan.semantic_schema_version,
        source_plans=plan.source_plans,
        workloads=plan.workloads,
        chunking_signature=plan.text_chunking_signature,
        content_set_xxh3_128=plan.content_set_xxh3_128,
        semantic_snapshot_xxh3_128=plan.semantic_snapshot_xxh3_128,
    )
    assert semantic_planner._plan_payload_for_signature.__module__ == (
        "neocortex.semantic.semantic_planner"
    )
    assert tuple(preimage) == (
        "algorithm",
        "scope",
        "selected_sources",
        "semantic_schema_version",
        "semantic_snapshot_xxh3_128",
        "chunking_signature",
        "content_set_xxh3_128",
        "sources",
        "workloads",
    )
    encoded = canonical_json(preimage).encode("utf-8")
    assert len(encoded) == 1117
    assert hashlib.sha256(encoded).hexdigest() == (
        "f20597ff2eae7661d778adfbb5333dc3da0bdc0f60896831f7fee639ef8383a3"
    )
    derived_signature = (
        f"{semantic_planner.PLAN_ALGORITHM_VERSION}:{HASH_ALGORITHM_128}:"
        f"{fingerprint_text(canonical_json(preimage)).xxh3_128}"
    )
    assert derived_signature == plan.plan_signature
    public_payload = semantic_planner.semantic_plan_payload(plan)
    assert preimage != public_payload
    assert "algorithm" not in public_payload
    assert "complete" not in preimage


def test_planner_payload_wrapper_forwards_when_builder_is_present(
    monkeypatch: MonkeyPatch,
) -> None:
    if not hasattr(semantic_planner, "_build_semantic_plan_payload"):
        return
    plan = _plan()
    sentinel: dict[str, object] = {"sentinel": True}
    calls: list[SemanticPlan] = []

    def fake_builder(received: SemanticPlan) -> dict[str, object]:
        calls.append(received)
        return sentinel

    monkeypatch.setattr(semantic_planner, "_build_semantic_plan_payload", fake_builder)
    assert semantic_planner.semantic_plan_payload(plan) is sentinel
    assert calls == [plan]
    assert semantic_planner.semantic_plan_payload is not fake_builder


def test_extracted_payload_builder_matches_wrapper_when_present() -> None:
    module_name = "neocortex.semantic.semantic_contract_payloads"
    if importlib.util.find_spec(module_name) is None:
        return
    module = importlib.import_module(module_name)
    builder = module.build_semantic_plan_payload
    assert str(inspect.signature(builder)) == ("(plan: 'SemanticPlan') -> 'dict[str, object]'")
    assert builder(_plan()) == semantic_planner.semantic_plan_payload(_plan())


# endregion [02]
