"""Compatibility contract for the stable semantic-service facade."""
# region [00] Contexto del módulo
# Módulo: tests/test_semantic_service_facade.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

from neocortex.semantic import semantic_service as service

import pytest


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


EXPECTED_PUBLIC_API = {
    "DEFAULT_SEARCH_MAX_VECTORS",
    "EVIDENCE_PAGE_SIZE",
    "FusedResolvedHit",
    "GenerationWorkResult",
    "IMAGE_OCR_TEXT_CHANNEL",
    "ImageRetrievalCalibration",
    "ModelPreparation",
    "SEMANTIC_DATABASE_NAME",
    "SEMANTIC_ONTOLOGY_ID",
    "SEMANTIC_PROTOTYPE_VERSION",
    "SemanticClassificationResult",
    "SemanticCostCalibration",
    "SemanticEvidencePassResult",
    "SemanticIndexResult",
    "SemanticPlan",
    "SemanticPlanBlocked",
    "SemanticRanking",
    "SemanticSearchResult",
    "SemanticSourcePlan",
    "SemanticStatus",
    "SemanticWorkloadPlan",
    "calibrate_image_retrieval",
    "classify_semantic_index",
    "index_image_embeddings",
    "index_text_embeddings",
    "plan_semantic_index",
    "prepare_semantic_models",
    "search_semantic_index",
    "semantic_plan_payload",
    "semantic_status",
}

EXPECTED_SIGNATURES = {
    "plan_semantic_index": (
        "(state_directory: 'Path', *, scope: 'str' = 'all', source_kinds: "
        "'Sequence[str]' = ('pdf', 'docx', 'xlsx', 'pptx', 'odt', 'audio', "
        "'archive', 'text', 'code'), text_model: "
        "'EmbeddingModelSpec | None' = None, "
        "embed_ocr_text: 'bool' = True, chunking: "
        "'TextChunkingConfig | None' = None, cost_calibrations: "
        "'Sequence[SemanticCostCalibration]' = (), execution_signature: "
        "'str | None' = None, scratch_directory: 'Path | None' = None, "
        "max_scratch_bytes: 'int' = 536870912, "
        "cancellation_check: 'Callable[[], None] | None' = None) -> "
        "'SemanticPlan'"
    ),
    "semantic_plan_payload": "(plan: 'SemanticPlan') -> 'dict[str, object]'",
    "prepare_semantic_models": (
        "(state_directory: 'Path', *, model_cache: 'Path | None' = None, "
        "include_compact: 'bool' = False, local_files_only: 'bool' = False, "
        "threads: 'int | None' = None) -> 'tuple[ModelPreparation, ...]'"
    ),
    "index_text_embeddings": (
        "(state_directory: 'Path', *, source_kinds: 'Sequence[str]' = "
        "('pdf', 'docx', 'xlsx', 'pptx', 'odt', 'audio', 'archive', 'text', "
        "'code'), model: "
        "'EmbeddingModelSpec | None' = None, model_cache: 'Path | None' = None, "
        "local_files_only: 'bool' = True, threads: 'int | None' = None, "
        "chunking: 'TextChunkingConfig | None' = None, work_budget: "
        "'SemanticWorkBudget | None' = None, progress: "
        "'ProgressCallback | None' = None) -> 'SemanticIndexResult'"
    ),
    "index_image_embeddings": (
        "(state_directory: 'Path', *, model_cache: 'Path | None' = None, "
        "local_files_only: 'bool' = True, threads: 'int | None' = None, "
        "embed_ocr_text: 'bool' = True, ocr_model: "
        "'EmbeddingModelSpec | None' = None, chunking: "
        "'TextChunkingConfig | None' = None, work_budget: "
        "'SemanticWorkBudget | None' = None, progress: "
        "'ProgressCallback | None' = None) -> 'SemanticIndexResult'"
    ),
    "search_semantic_index": (
        "(state_directory: 'Path', query: 'str', *, limit: 'int' = 20, "
        "candidate_limit: 'int | None' = None, max_vectors: 'int' = 500000, "
        "include_text: 'bool' = True, include_title: 'bool' = False, "
        "include_images: 'bool' = True, "
        "include_lexical: 'bool' = True, lexical_paths: "
        "'LexicalStatePaths | None' = None, semantic_database: "
        "'Path | None' = None, text_model: 'EmbeddingModelSpec | None' = None, "
        "model_cache: 'Path | None' = None, "
        "local_files_only: 'bool' = True, threads: 'int | None' = None, "
        "evidence_mode: 'bool' = False, image_calibration: "
        "'ImageRetrievalCalibration | None' = None, cancellation_check: "
        "'Callable[[], None] | None' = None) -> "
        "'SemanticSearchResult'"
    ),
    "classify_semantic_index": (
        "(state_directory: 'Path', *, include_text: 'bool' = True, "
        "include_images: 'bool' = True, max_evidence_per_entity: 'int' = 8, "
        "page_size: 'int' = 256, text_model: 'EmbeddingModelSpec | None' = None, "
        "model_cache: 'Path | None' = None, local_files_only: 'bool' = True, "
        "threads: 'int | None' = None) -> 'SemanticClassificationResult'"
    ),
    "semantic_status": (
        "(state_directory: 'Path', *, generation_limit: 'int' = 10) -> 'SemanticStatus'"
    ),
}


def test_semantic_service_public_facade_contract() -> None:
    assert set(service.__all__) == EXPECTED_PUBLIC_API
    assert all(hasattr(service, name) for name in EXPECTED_PUBLIC_API)


def test_semantic_service_public_signatures_are_stable() -> None:
    actual = {name: str(inspect.signature(getattr(service, name))) for name in EXPECTED_SIGNATURES}
    assert actual == EXPECTED_SIGNATURES


def test_semantic_search_facade_forwards_exact_database_and_candidate_limit() -> None:
    state = Path("C:/fixture/state")
    database = Path("C:/fixture/generations/semantic-000042.sqlite3")
    cache = Path("C:/fixture/model-cache")

    def cancel() -> None:
        return None

    sentinel = object()
    with patch.object(
        service._search,
        "search_semantic_index",
        return_value=sentinel,
    ) as backend:
        result = service.search_semantic_index(
            state,
            "transformador",
            limit=9,
            candidate_limit=27,
            max_vectors=1_234,
            include_text=True,
            include_images=False,
            include_lexical=False,
            lexical_paths=None,
            semantic_database=database,
            text_model=None,
            model_cache=cache,
            local_files_only=False,
            threads=3,
            evidence_mode=True,
            cancellation_check=cancel,
        )

    assert result is sentinel
    assert backend.call_args.kwargs["semantic_database"] is database
    backend.assert_called_once_with(
        state,
        "transformador",
        limit=9,
        candidate_limit=27,
        max_vectors=1_234,
        include_text=True,
        include_title=False,
        include_images=False,
        include_lexical=False,
        lexical_paths=None,
        semantic_database=database,
        text_model=None,
        model_cache_override=cache,
        local_files_only=False,
        threads=3,
        backend_factory=service._backend,
        lexical_search=service.search_lexical_sources,
        evidence_mode=True,
        image_calibration=None,
        cancellation_check=cancel,
    )


def test_semantic_plan_facade_forwards_the_complete_read_only_request() -> None:
    state = Path("C:/fixture/state")

    def cancel() -> None:
        return None

    sentinel = object()
    with patch.object(
        service._planner,
        "plan_semantic_index",
        return_value=sentinel,
    ) as backend:
        result = service.plan_semantic_index(
            state,
            scope="text",
            source_kinds=("pdf",),
            text_model=None,
            embed_ocr_text=False,
            chunking=None,
            cost_calibrations=(),
            execution_signature="fixture-execution-v1",
            scratch_directory=Path("C:/fixture/scratch"),
            max_scratch_bytes=131_072,
            cancellation_check=cancel,
        )

    assert result is sentinel
    backend.assert_called_once_with(
        state,
        scope="text",
        source_kinds=("pdf",),
        text_model=None,
        embed_ocr_text=False,
        chunking=None,
        cost_calibrations=(),
        execution_signature="fixture-execution-v1",
        scratch_directory=Path("C:/fixture/scratch"),
        max_scratch_bytes=131_072,
        cancellation_check=cancel,
    )


# endregion [02]
