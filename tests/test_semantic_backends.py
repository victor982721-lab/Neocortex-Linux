from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import pytest

from neocortex.semantic.semantic_backends import (
    FastEmbedBackend,
    SourceRevisionMismatchError,
    _verify_image_source,
    fastembed_availability,
    iter_embedding_batches,
    merge_exact_search_pages,
    reciprocal_rank_fusion,
)
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    ExactSearchPage,
    SearchHit,
    fingerprint_bytes,
    fingerprint_text,
)
from tests.semantic_test_backend import DeterministicTestBackend


# region [01] Fixtures and strict backend validation


def _text_model(
    *,
    dimensions: int = 4,
    provider: str = "test-deterministic",
) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "text-model-v1",
        "text-space-v1",
        EmbeddingModality.TEXT,
        "fixture/text",
        "1",
        dimensions,
        provider,
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


class _NonNormalizedBackend:
    def __init__(self) -> None:
        self._model = _text_model(dimensions=2, provider="fixture")

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return 2

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        return tuple(
            BackendEmbedding(request.request_id, (3.0, 4.0), {"raw": True})
            for request in requests
        )


def test_backend_boundary_normalizes_non_normalized_model_output() -> None:
    text = "transformador de potencia"
    request = EmbeddingRequest(
        "1",
        EmbeddingRole.PASSAGE,
        fingerprint_text(text),
        text=text,
    )
    result = tuple(iter_embedding_batches(_NonNormalizedBackend(), (request,)))[0]
    assert result.vector == pytest.approx((0.6, 0.8))
    assert math.sqrt(sum(value * value for value in result.vector)) == pytest.approx(
        1.0
    )
    assert result.provenance["normalized_by_adapter"] is True
    assert result.provenance["input_l2_norm"] == pytest.approx(5.0)


def test_deterministic_test_support_backend_is_stable() -> None:
    model = _text_model()
    with pytest.raises(ValueError, match="test-deterministic provider"):
        DeterministicTestBackend(_text_model(provider="production"))
    backend = DeterministicTestBackend(model, batch_size=2)
    text = "substation protection"
    request = EmbeddingRequest(
        "r",
        EmbeddingRole.QUERY,
        fingerprint_text(text),
        text=text,
    )
    first = backend.embed((request,))[0]
    second = backend.embed((request,))[0]
    assert first.vector == second.vector
    assert math.sqrt(sum(value * value for value in first.vector)) == pytest.approx(1.0)


# endregion [01]


# region [02] Optional providers and mutation safety


def test_fastembed_adapter_checks_declared_dimensions_before_model_loading(
    tmp_path: Path,
) -> None:
    availability = fastembed_availability()
    if not availability.installed:
        pytest.skip(availability.detail)
    wrong = EmbeddingModelSpec(
        "minilm-wrong-dim",
        "minilm-space",
        EmbeddingModality.TEXT,
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "fastembed-0.8.0",
        3,
        "fastembed",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    with pytest.raises(ValueError, match="FastEmbed reports 384"):
        FastEmbedBackend(wrong, cache_dir=tmp_path, local_files_only=True)
    assert list(tmp_path.iterdir()) == []


def test_image_source_xxh3_and_declared_revision_are_checked(tmp_path: Path) -> None:
    path = tmp_path / "subestación-á.jpg"
    path.write_bytes(b"fixture-image-content")
    stat = path.stat()
    fingerprint = fingerprint_bytes(path.read_bytes())
    request = EmbeddingRequest(
        "image",
        EmbeddingRole.IMAGE,
        fingerprint,
        image_path=path,
        source_revision={
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "raw_content_xxh3_128": fingerprint.xxh3_128,
        },
    )
    revision = _verify_image_source(request)
    assert revision[:2] == (stat.st_size, stat.st_mtime_ns)
    path.write_bytes(b"mutated-image-content")
    with pytest.raises(SourceRevisionMismatchError, match="no longer match"):
        _verify_image_source(request)


# endregion [02]


# region [03] Rank-only fusion


def _hit(
    item: str,
    entity: str,
    score: float,
    ref_id: int,
    *,
    model: str = "model",
    query_model: str | None = None,
) -> SearchHit:
    return SearchHit(
        ref_id=ref_id,
        entity_id=entity,
        item_id=item,
        indexed_model_signature=model,
        vector_space="space",
        modality=EmbeddingModality.TEXT,
        score=score,
        generation_id=1,
        query_model_signature=query_model,
    )


def test_rrf_uses_rank_not_incompatible_raw_scores_and_deduplicates_item_chunks() -> (
    None
):
    text = (
        _hit("shared", "chunk-1", 0.01, 1, model="text"),
        _hit("shared", "chunk-2", 0.99, 2, model="text"),
        _hit("text-only", "chunk-3", 0.98, 3, model="text"),
    )
    image = (
        _hit(
            "shared",
            "image-1",
            -0.8,
            4,
            model="clip-image",
            query_model="clip-text",
        ),
        _hit(
            "image-only",
            "image-2",
            1.0,
            5,
            model="clip-image",
            query_model="clip-text",
        ),
    )
    fused = reciprocal_rank_fusion({"text": text, "image": image}, k=10)
    assert fused[0].item_id == "shared"
    assert len(fused[0].evidence) == 2
    assert {value.raw_score for value in fused[0].evidence} == {0.01, -0.8}
    image_evidence = next(
        value for value in fused[0].evidence if value.ranking == "image"
    )
    assert image_evidence.indexed_model_signature == "clip-image"
    assert image_evidence.query_model_signature == "clip-text"


def test_exact_page_merge_keeps_global_top_k() -> None:
    pages = (
        ExactSearchPage((_hit("a", "a", 0.2, 1), _hit("b", "b", 0.8, 2)), 2, 2, False),
        ExactSearchPage(
            (_hit("c", "c", 0.9, 3), _hit("d", "d", 0.1, 4)), 2, None, True
        ),
    )
    merged = merge_exact_search_pages(pages, limit=2)
    assert [hit.item_id for hit in merged] == ["c", "b"]


# endregion [03]
