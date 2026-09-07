# region [00] Contexto del módulo
# Módulo: tests/test_semantic_service_contracts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from typing import Any, cast
from dataclasses import FrozenInstanceError, MISSING, fields, replace
from pathlib import Path

import pytest

from neocortex.semantic import semantic_service
from neocortex.semantic import semantic_contract_validation as validation
from neocortex.semantic import semantic_service_contracts as contracts
from neocortex.semantic.semantic_models import canonical_json
from neocortex.semantic.semantic_service_contracts import (
    FusedResolvedHit,
    GenerationWorkResult,
    ImageRetrievalCalibration,
    ModelPreparation,
    SemanticClassificationResult,
    SemanticCostCalibration,
    SemanticEvidencePassResult,
    SemanticIndexResult,
    SemanticPlan,
    SemanticRanking,
    SemanticSearchResult,
    SemanticSourcePlan,
    SemanticStatus,
    SemanticWorkloadPlan,
)


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


CONTRACT_CLASSES = (
    GenerationWorkResult,
    SemanticIndexResult,
    SemanticCostCalibration,
    SemanticSourcePlan,
    SemanticWorkloadPlan,
    SemanticPlan,
    SemanticRanking,
    ImageRetrievalCalibration,
    FusedResolvedHit,
    SemanticSearchResult,
    ModelPreparation,
    SemanticStatus,
    SemanticEvidencePassResult,
    SemanticClassificationResult,
)

EXPECTED_VISIBLE_SURFACE = {
    "DEFAULT_SEARCH_MAX_VECTORS",
    "EVIDENCE_PAGE_SIZE",
    "FusedHit",
    "FusedResolvedHit",
    "GenerationSummary",
    "GenerationWorkResult",
    "IMAGE_OCR_TEXT_CHANNEL",
    "ImageRetrievalCalibration",
    "JOB_BATCH_SIZE",
    "LEASE_HEARTBEAT_INTERVAL_SECONDS",
    "LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS",
    "LexicalAvailability",
    "LexicalRanking",
    "MAX_LEXICAL_CANDIDATE_HITS",
    "MAX_SEMANTIC_CANDIDATE_HITS",
    "MIN_ADVISORY_EVIDENCE_SCORE",
    "Mapping",
    "ModelPreparation",
    "Path",
    "ResolvedSearchHit",
    "SEARCH_RESOLUTION_BATCH_SIZE",
    "SEMANTIC_DATABASE_NAME",
    "SEMANTIC_ONTOLOGY_ID",
    "SEMANTIC_PLAN_TEXT_SOURCE_KINDS",
    "SEMANTIC_PROTOTYPE_VERSION",
    "STAGING_BATCH_SIZE",
    "SearchHit",
    "SemanticClassificationResult",
    "SemanticCostCalibration",
    "SemanticEvidencePassResult",
    "SemanticIndexResult",
    "SemanticPlan",
    "SemanticRanking",
    "SemanticSearchResult",
    "SemanticSourcePlan",
    "SemanticStatus",
    "SemanticWorkloadPlan",
    "WORKER_LEASE_SECONDS",
    "annotations",
    "dataclass",
    "field",
    "json",
    "math",
}

EXPECTED_FIELDS = {
    "GenerationWorkResult": ("summary", "queued", "reused", "embedded", "failed"),
    "SemanticIndexResult": (
        "semantic_database",
        "sources",
        "items_staged",
        "chunks_staged",
        "generations",
        "new_jobs_staged",
        "execution_mode",
        "sources_reused",
        "sources_enumerated",
        "truncated",
        "truncation_reason",
    ),
    "SemanticCostCalibration": (
        "calibration_signature",
        "execution_signature",
        "processing_signature",
        "workload",
        "model_signature",
        "role",
        "contents_per_second",
        "sample_contents",
        "sample_input_bytes",
    ),
    "SemanticSourcePlan": (
        "source_kind",
        "database",
        "schema_version",
        "resources",
        "sections",
        "chunks",
        "embedding_entities",
        "source_bytes",
        "section_text_bytes",
        "input_bytes",
        "snapshot_xxh3_128",
    ),
    "SemanticWorkloadPlan": (
        "name",
        "modality",
        "role",
        "model_signature",
        "vector_space",
        "model_id",
        "model_version",
        "dimensions",
        "provider",
        "supported_roles",
        "vector_dtype",
        "normalization",
        "distance",
        "model_provenance_json",
        "processing_signature",
        "embedding_entities",
        "unique_contents",
        "preexisting_reusable_contents",
        "planned_reusable_contents",
        "new_unique_contents",
        "input_bytes",
        "unique_input_bytes",
        "new_vector_blob_bytes_lower_bound",
        "model_request_contents_lower_bound",
        "model_request_contents_upper_bound",
        "estimated_model_seconds_lower_bound",
        "estimated_model_seconds_upper_bound",
        "cost_calibration_signature",
        "cost_execution_signature",
        "cost_calibration_contents_per_second",
        "cost_calibration_sample_contents",
        "cost_calibration_sample_input_bytes",
        "cost_unavailable_reason",
    ),
    "SemanticPlan": (
        "scope",
        "selected_sources",
        "semantic_database",
        "semantic_schema_version",
        "source_plans",
        "workloads",
        "text_chunking_signature",
        "content_set_xxh3_128",
        "semantic_snapshot_xxh3_128",
        "plan_signature",
        "resources",
        "sections",
        "chunks",
        "embedding_entities",
        "source_bytes",
        "section_text_bytes",
        "input_bytes",
        "unique_contents",
        "unique_input_bytes",
        "reusable_unique_contents",
        "new_unique_contents",
        "new_vector_blob_bytes_lower_bound",
        "model_request_contents_lower_bound",
        "model_request_contents_upper_bound",
        "estimated_model_seconds_lower_bound",
        "estimated_model_seconds_upper_bound",
        "scratch_storage_bytes",
        "max_scratch_bytes",
        "originals_verified",
        "execution_ready",
        "dry_run",
        "jobs_created",
        "state_mutated",
        "estimate_kind",
        "vector_bytes_kind",
        "snapshot_scope",
        "sqlite_read_snapshot_may_touch_shm",
    ),
    "SemanticRanking": (
        "name",
        "hits",
        "resolved",
        "scanned",
        "complete",
        "available",
        "unavailable_reason",
        "cutoff_reason",
        "next_cursor",
        "cutoff_score",
        "fusion_weight",
        "provenance",
    ),
    "ImageRetrievalCalibration": (
        "calibration_signature",
        "query_model_signature",
        "indexed_model_signature",
        "pipeline",
        "backend",
        "minimum_score",
        "positive_queries",
        "negative_queries",
        "sample_items",
        "indexed_processing_signature",
    ),
    "FusedResolvedHit": ("fused", "path", "source_kind", "source_identity", "snippet", "primary_evidence"),
    "SemanticSearchResult": ("query", "rankings", "lexical_rankings", "fused"),
    "ModelPreparation": (
        "model_signature",
        "model_id",
        "dimensions",
        "elapsed_seconds",
    ),
    "SemanticStatus": (
        "exists", "schema_version", "counts", "generations", "generation_timings"
    ),
    "SemanticEvidencePassResult": (
        "indexed_model_signature",
        "query_model_signature",
        "vector_space",
        "prototypes",
        "entities_scored",
        "evidence_staged",
        "stale_evidence_deactivated",
        "entities_abstained",
    ),
    "SemanticClassificationResult": (
        "semantic_database",
        "ontology_id",
        "ontology_version",
        "passes",
        "skipped",
    ),
}

EXPECTED_SIGNATURES = {
    "GenerationWorkResult": (
        "(summary: 'GenerationSummary', queued: 'int', reused: 'int', "
        "embedded: 'int', failed: 'int') -> None"
    ),
    "SemanticIndexResult": (
        "(semantic_database: 'Path', sources: 'tuple[str, ...]', "
        "items_staged: 'int', chunks_staged: 'int', "
        "generations: 'tuple[GenerationWorkResult, ...]', "
        "new_jobs_staged: 'int' = 0, "
        "execution_mode: \"_Literal['enumerated', 'exact_replay', 'content_compatible_replay']\" = 'enumerated', "
        "sources_reused: 'int' = 0, sources_enumerated: 'int' = 0, "
        "truncated: 'bool' = False, "
        "truncation_reason: 'str | None' = None) -> None"
    ),
    "SemanticCostCalibration": (
        "(calibration_signature: 'str', execution_signature: 'str', "
        "processing_signature: 'str', workload: 'str', model_signature: 'str', "
        "role: 'str', contents_per_second: 'float', sample_contents: 'int', "
        "sample_input_bytes: 'int') -> None"
    ),
    "SemanticSourcePlan": (
        "(source_kind: 'str', database: 'Path', schema_version: 'int', "
        "resources: 'int', sections: 'int', chunks: 'int', "
        "embedding_entities: 'int', source_bytes: 'int', "
        "section_text_bytes: 'int', input_bytes: 'int', "
        "snapshot_xxh3_128: 'str') -> None"
    ),
    "SemanticWorkloadPlan": (
        "(name: 'str', modality: 'str', role: 'str', model_signature: 'str', "
        "vector_space: 'str', model_id: 'str', model_version: 'str', "
        "dimensions: 'int', provider: 'str', supported_roles: 'tuple[str, ...]', "
        "vector_dtype: 'str', normalization: 'str', distance: 'str', "
        "model_provenance_json: 'str', processing_signature: 'str', "
        "embedding_entities: 'int', unique_contents: 'int', "
        "preexisting_reusable_contents: 'int', "
        "planned_reusable_contents: 'int', new_unique_contents: 'int', "
        "input_bytes: 'int', unique_input_bytes: 'int', "
        "new_vector_blob_bytes_lower_bound: 'int', "
        "model_request_contents_lower_bound: 'int', "
        "model_request_contents_upper_bound: 'int', "
        "estimated_model_seconds_lower_bound: 'float | None', "
        "estimated_model_seconds_upper_bound: 'float | None', "
        "cost_calibration_signature: 'str | None', "
        "cost_execution_signature: 'str | None', "
        "cost_calibration_contents_per_second: 'float | None', "
        "cost_calibration_sample_contents: 'int | None', "
        "cost_calibration_sample_input_bytes: 'int | None', "
        "cost_unavailable_reason: 'str | None') -> None"
    ),
    "SemanticPlan": (
        "(scope: 'str', selected_sources: 'tuple[str, ...]', "
        "semantic_database: 'Path', semantic_schema_version: 'int | None', "
        "source_plans: 'tuple[SemanticSourcePlan, ...]', "
        "workloads: 'tuple[SemanticWorkloadPlan, ...]', "
        "text_chunking_signature: 'str | None', content_set_xxh3_128: 'str', "
        "semantic_snapshot_xxh3_128: 'str', plan_signature: 'str', "
        "resources: 'int', sections: 'int', chunks: 'int', "
        "embedding_entities: 'int', source_bytes: 'int', "
        "section_text_bytes: 'int', input_bytes: 'int', unique_contents: 'int', "
        "unique_input_bytes: 'int', reusable_unique_contents: 'int', "
        "new_unique_contents: 'int', new_vector_blob_bytes_lower_bound: 'int', "
        "model_request_contents_lower_bound: 'int', "
        "model_request_contents_upper_bound: 'int', "
        "estimated_model_seconds_lower_bound: 'float | None', "
        "estimated_model_seconds_upper_bound: 'float | None', "
        "scratch_storage_bytes: 'int', max_scratch_bytes: 'int', "
        "originals_verified: 'bool | None', execution_ready: 'bool | None', "
        "dry_run: 'bool' = True, jobs_created: 'int' = 0, "
        "state_mutated: 'bool' = False, estimate_kind: 'str' = "
        "'model_only_request_range_from_exact_content_projection', "
        "vector_bytes_kind: 'str' = 'lower_bound_vector_blob_only', "
        "snapshot_scope: 'str' = "
        "'read_transaction_per_database_with_data_version_fence_not_cross_database_atomic', "
        "sqlite_read_snapshot_may_touch_shm: 'bool' = True) -> None"
    ),
    "SemanticRanking": (
        "(name: 'str', hits: 'tuple[SearchHit, ...]', "
        "resolved: 'tuple[ResolvedSearchHit, ...]', scanned: 'int', "
        "complete: 'bool', available: 'bool' = True, "
        "unavailable_reason: 'str | None' = None, "
        "cutoff_reason: 'str | None' = None, next_cursor: 'int | None' = None, "
        "cutoff_score: 'float | None' = None, fusion_weight: 'float' = 1.0, "
        "provenance: 'Mapping[str, object]' = <factory>) -> None"
    ),
    "ImageRetrievalCalibration": (
        "(calibration_signature: 'str', query_model_signature: 'str', "
        "indexed_model_signature: 'str', pipeline: 'str', backend: 'str', "
        "minimum_score: 'float', positive_queries: 'int', "
        "negative_queries: 'int', sample_items: 'int', "
        "indexed_processing_signature: 'str | None' = None) -> None"
    ),
    "FusedResolvedHit": (
        "(fused: 'FusedHit', path: 'str | None', source_kind: 'str', "
        "source_identity: 'str', snippet: 'str | None', primary_evidence: 'ResolvedSearchHit | None' = None) -> None"
    ),
    "SemanticSearchResult": (
        "(query: 'str', rankings: 'tuple[SemanticRanking, ...]', "
        "lexical_rankings: 'tuple[LexicalRanking, ...]', "
        "fused: 'tuple[FusedResolvedHit, ...]') -> None"
    ),
    "ModelPreparation": (
        "(model_signature: 'str', model_id: 'str', dimensions: 'int', "
        "elapsed_seconds: 'float') -> None"
    ),
    "SemanticStatus": (
        "(exists: 'bool', schema_version: 'int | None' = None, "
        "counts: 'Mapping[str, int]' = <factory>, "
        "generations: 'tuple[GenerationSummary, ...]' = (), "
        "generation_timings: 'Mapping[int, Mapping[str, int]]' = <factory>) -> None"
    ),
    "SemanticEvidencePassResult": (
        "(indexed_model_signature: 'str', query_model_signature: 'str', "
        "vector_space: 'str', prototypes: 'int', entities_scored: 'int', "
        "evidence_staged: 'int', stale_evidence_deactivated: 'int', "
        "entities_abstained: 'int' = 0) -> None"
    ),
    "SemanticClassificationResult": (
        "(semantic_database: 'Path', ontology_id: 'str', ontology_version: 'str', "
        "passes: 'tuple[SemanticEvidencePassResult, ...]', "
        "skipped: 'Mapping[str, str]' = <factory>) -> None"
    ),
}


def _source_plan() -> SemanticSourcePlan:
    return SemanticSourcePlan(
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


def _workload_plan() -> SemanticWorkloadPlan:
    return SemanticWorkloadPlan(
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


def _semantic_plan() -> SemanticPlan:
    return SemanticPlan(
        "text",
        ("pdf",),
        Path("C:/fixture/semantic.sqlite3"),
        6,
        (_source_plan(),),
        (_workload_plan(),),
        "chunking-v1",
        "1" * 32,
        "2" * 32,
        "semantic-readonly-plan-v3:xxh3-128:bf07d55c2bbea631b94feace627e7317",
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


def _calibration() -> SemanticCostCalibration:
    return SemanticCostCalibration(
        "calibration-signature",
        "execution-signature",
        "processing-signature",
        "text",
        "model-signature",
        "passage",
        2.0,
        10,
        200,
    )


def _assert_value_error(message: str, constructor: object) -> None:
    with pytest.raises(ValueError) as exc_info:
        constructor()  # type: ignore[operator]
    assert str(exc_info.value) == message


def test_contract_module_visible_surface_is_characterized_without_all() -> None:
    assert "__all__" not in vars(contracts)
    assert {name for name in vars(contracts) if not name.startswith("_")} == (
        EXPECTED_VISIBLE_SURFACE
    )


def test_operational_constants_are_exact() -> None:
    assert contracts.SEMANTIC_DATABASE_NAME == "semantic.sqlite3"
    assert contracts.SEMANTIC_ONTOLOGY_ID == "neocortex-industrial"
    assert contracts.SEMANTIC_PROTOTYPE_VERSION == "bilingual-domain-prototype-v1"
    assert contracts.STAGING_BATCH_SIZE == 128
    assert contracts.JOB_BATCH_SIZE == 128
    assert contracts.DEFAULT_SEARCH_MAX_VECTORS == 500_000
    assert contracts.MAX_SEMANTIC_CANDIDATE_HITS == 1_000
    assert contracts.SEARCH_RESOLUTION_BATCH_SIZE == 500
    assert contracts.MAX_LEXICAL_CANDIDATE_HITS == 1_000
    assert contracts.WORKER_LEASE_SECONDS == 900.0
    assert contracts.LEASE_HEARTBEAT_INTERVAL_SECONDS == 300.0
    assert contracts.LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS == 65.0
    assert contracts.EVIDENCE_PAGE_SIZE == 256
    assert contracts.MIN_ADVISORY_EVIDENCE_SCORE == 0.0
    assert contracts.IMAGE_OCR_TEXT_CHANNEL == "image_ocr"
    assert contracts.SEMANTIC_PLAN_TEXT_SOURCE_KINDS == frozenset(
        {
            "pdf",
            "docx",
            "xlsx",
            "pptx",
            "odt",
            "audio",
            "archive",
            "text",
            "code",
            "video",
        }
    )


def test_contract_cold_import_stays_free_of_owners_pil_planner_and_service() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        "import neocortex.semantic.semantic_service_contracts\n"
        "print('\\n'.join(sorted(name for name in sys.modules "
        "if name.startswith('neocortex') or "
        "name.startswith('neocortex.semantic') or "
        "name.startswith('neocortex.sqlite') or name.startswith('PIL'))))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = frozenset(completed.stdout.splitlines())
    baseline = frozenset(
        {
            "neocortex",
            "neocortex.semantic",
            "neocortex.semantic.semantic_contract_validation",
            "neocortex.semantic.semantic_lexical",
            "neocortex.semantic.semantic_models",
            "neocortex.semantic.semantic_service_contracts",
            "neocortex.persistence.sqlite_cancellation",
            "neocortex.persistence",
        }
    )
    assert loaded == baseline


def test_validation_is_structural_and_does_not_import_its_contract_owner() -> None:
    assert validation.__file__ is not None
    source_path = Path(validation.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    owner_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("semantic_service_contracts")
    ]
    assert owner_imports == []

    calibration = _calibration()
    source = _source_plan()
    workload = _workload_plan()
    plan = _semantic_plan()
    validation.validate_semantic_cost_calibration(calibration)
    validation.validate_semantic_source_plan(
        source,
        text_source_kinds=contracts.SEMANTIC_PLAN_TEXT_SOURCE_KINDS,
    )
    validation.validate_semantic_workload_plan(workload)
    validation.validate_semantic_plan(
        plan,
        text_source_kinds=contracts.SEMANTIC_PLAN_TEXT_SOURCE_KINDS,
        image_ocr_text_channel=contracts.IMAGE_OCR_TEXT_CHANNEL,
        source_plan_type=SemanticSourcePlan,
        workload_plan_type=SemanticWorkloadPlan,
    )


def test_contract_signatures_fields_defaults_and_dataclass_shape_are_stable() -> None:
    for contract in CONTRACT_CLASSES:
        expected_fields = EXPECTED_FIELDS[contract.__name__]
        assert str(inspect.signature(contract)) == EXPECTED_SIGNATURES[contract.__name__]
        assert tuple(item.name for item in fields(contract)) == expected_fields
        assert tuple(contract.__slots__) == expected_fields
        assert contract.__match_args__ == expected_fields
        assert contract.__module__ == "neocortex.semantic.semantic_service_contracts"
        parameters = contract.__dataclass_params__
        assert parameters.frozen is True
        assert parameters.slots is True
        assert parameters.kw_only is False

    plan_fields = {item.name: item for item in fields(SemanticPlan)}
    assert plan_fields["dry_run"].default is True
    assert plan_fields["jobs_created"].default == 0
    assert plan_fields["state_mutated"].default is False
    assert plan_fields["estimate_kind"].default == (
        "model_only_request_range_from_exact_content_projection"
    )
    assert plan_fields["vector_bytes_kind"].default == "lower_bound_vector_blob_only"
    assert plan_fields["snapshot_scope"].default == (
        "read_transaction_per_database_with_data_version_fence_not_cross_database_atomic"
    )
    assert plan_fields["sqlite_read_snapshot_may_touch_shm"].default is True
    index_fields = {item.name: item for item in fields(SemanticIndexResult)}
    assert index_fields["new_jobs_staged"].default == 0
    assert index_fields["truncated"].default is False
    assert index_fields["truncation_reason"].default is None
    ranking_fields = {item.name: item for item in fields(SemanticRanking)}
    assert ranking_fields["fusion_weight"].default == 1.0
    assert ranking_fields["provenance"].default is MISSING
    assert ranking_fields["provenance"].default_factory is dict
    status_fields = {item.name: item for item in fields(SemanticStatus)}
    assert status_fields["counts"].default is MISSING
    assert status_fields["counts"].default_factory is dict
    classification_fields = {item.name: item for item in fields(SemanticClassificationResult)}
    assert classification_fields["skipped"].default is MISSING
    assert classification_fields["skipped"].default_factory is dict


def test_semantic_service_reexports_contract_objects_by_identity() -> None:
    reexported = (
        "DEFAULT_SEARCH_MAX_VECTORS",
        "EVIDENCE_PAGE_SIZE",
        "IMAGE_OCR_TEXT_CHANNEL",
        "SEMANTIC_DATABASE_NAME",
        "SEMANTIC_ONTOLOGY_ID",
        "SEMANTIC_PROTOTYPE_VERSION",
        "FusedResolvedHit",
        "GenerationWorkResult",
        "ImageRetrievalCalibration",
        "ModelPreparation",
        "SemanticClassificationResult",
        "SemanticCostCalibration",
        "SemanticEvidencePassResult",
        "SemanticIndexResult",
        "SemanticPlan",
        "SemanticRanking",
        "SemanticSearchResult",
        "SemanticSourcePlan",
        "SemanticStatus",
        "SemanticWorkloadPlan",
    )
    assert (
        tuple(
            name
            for name in semantic_service.__all__
            if hasattr(contracts, name)
            and getattr(semantic_service, name) is getattr(contracts, name)
        )
        == reexported
    )
    for name in reexported:
        assert getattr(semantic_service, name) is getattr(contracts, name)


def test_cost_calibration_validation_messages_and_priority_are_stable() -> None:
    calibration = _calibration()
    cases = (
        (
            "calibration_signature cannot be blank",
            {"calibration_signature": "", "role": "query"},
        ),
        (
            "semantic cost calibration role is unsupported",
            {"role": "query", "contents_per_second": 0.0},
        ),
        (
            "contents_per_second must be finite and positive",
            {"contents_per_second": 0.0, "sample_contents": 0},
        ),
        ("semantic calibration sample bounds are invalid", {"sample_contents": 0}),
    )
    for message, changes in cases:
        _assert_value_error(message, lambda changes=changes: replace(calibration, **changes))


def test_source_plan_validation_messages_and_priority_are_stable() -> None:
    source = _source_plan()
    cases = (
        ("source_kind cannot be blank", {"source_kind": "", "database": "bad"}),
        ("source_kind is unsupported", {"source_kind": "audio_bad", "database": "bad"}),
        (
            "source database must be a Path",
            {"database": "bad", "schema_version": False},
        ),
        ("schema_version must be positive", {"schema_version": False, "resources": -1}),
        (
            "resources must be a non-negative integer",
            {"resources": -1, "snapshot_xxh3_128": "bad"},
        ),
        ("source chunks cannot exceed embedding entities", {"chunks": 2}),
        (
            "snapshot_xxh3_128 must be a lowercase XXH3-128 digest",
            {"snapshot_xxh3_128": "bad"},
        ),
    )
    for message, changes in cases:
        _assert_value_error(message, lambda changes=changes: replace(source, **changes))


def test_workload_plan_validation_messages_and_priority_are_stable() -> None:
    workload = _workload_plan()
    cases = (
        ("name cannot be blank", {"name": "", "modality": "audio"}),
        (
            "workload modality is unsupported",
            {"modality": "audio", "supported_roles": ()},
        ),
        ("workload roles are incompatible with its model", {"supported_roles": ()}),
        (
            "dimensions must be between 1 and 65536",
            {"dimensions": True, "model_provenance_json": "[]"},
        ),
        (
            "model_provenance_json must contain an object",
            {"model_provenance_json": "[]"},
        ),
        (
            "model_provenance_json must be canonical",
            {"model_provenance_json": '{"b":1, "a":2}'},
        ),
        (
            "workload reuse counts do not partition unique contents",
            {"preexisting_reusable_contents": 1},
        ),
        (
            "estimated_model_seconds_lower_bound must be a finite non-negative number",
            {"estimated_model_seconds_lower_bound": float("inf")},
        ),
    )
    for message, changes in cases:
        _assert_value_error(message, lambda changes=changes: replace(workload, **changes))


def test_semantic_plan_validation_messages_and_priority_are_stable() -> None:
    plan = _semantic_plan()
    source = plan.source_plans[0]
    workload = plan.workloads[0]
    cases = (
        ("semantic plan scope must be a string", {"scope": 1}),
        (
            "semantic plan scope is unsupported",
            {"scope": "bad", "selected_sources": ["bad"]},
        ),
        (
            "selected_sources must contain supported text sources",
            {"selected_sources": ["pdf"]},
        ),
        (
            "semantic_database must be a Path",
            {"semantic_database": "bad", "semantic_schema_version": False},
        ),
        (
            "semantic_schema_version must be positive",
            {"semantic_schema_version": False, "source_plans": [source]},
        ),
        (
            "source_plans must contain SemanticSourcePlan values",
            {"source_plans": [source]},
        ),
        (
            "source plans cannot contain duplicate owners",
            {"source_plans": (source, source), "workloads": (workload, workload)},
        ),
        (
            "plan resource aggregate is inconsistent",
            {"resources": 2, "state_mutated": True},
        ),
        (
            "semantic plans must remain non-mutating dry runs",
            {"dry_run": False, "originals_verified": True},
        ),
    )
    for message, changes in cases:
        _assert_value_error(message, lambda changes=changes: replace(plan, **changes))


def test_passive_contracts_remain_unvalidated_and_shallowly_frozen() -> None:
    passive_contracts = (
        GenerationWorkResult,
        SemanticIndexResult,
        SemanticRanking,
        FusedResolvedHit,
        SemanticSearchResult,
        ModelPreparation,
        SemanticStatus,
        SemanticEvidencePassResult,
        SemanticClassificationResult,
    )
    assert all("__post_init__" not in contract.__dict__ for contract in passive_contracts)

    ranking = cast(Any, SemanticRanking)(
        name=1,
        hits=[],
        resolved=[],
        scanned=-1,
        complete="yes",
    )
    preparation = ModelPreparation("", "", -1, float("nan"))
    status = SemanticStatus(exists="yes")  # type: ignore[arg-type]
    classification = SemanticClassificationResult(Path("."), "", "", ())
    assert ranking.name == 1
    assert preparation.dimensions == -1
    status.counts["mutable"] = -1  # type: ignore[index]
    classification.skipped["mutable"] = "yes"  # type: ignore[index]
    assert status.counts == {"mutable": -1}
    assert classification.skipped == {"mutable": "yes"}
    with pytest.raises(FrozenInstanceError):
        status.exists = False  # type: ignore[misc]


def test_representative_valid_contract_graph_is_stable() -> None:
    plan = _semantic_plan()
    assert plan.plan_signature == (
        "semantic-readonly-plan-v3:xxh3-128:bf07d55c2bbea631b94feace627e7317"
    )
    assert plan.estimated_model_seconds is None
    assert plan.cost_complete is False
    assert plan.cost_calibrated is False
    assert plan.complete is False


# endregion [02]
