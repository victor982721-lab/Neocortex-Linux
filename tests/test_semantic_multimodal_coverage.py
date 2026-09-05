"""Hermetic multimodal coverage guards for Semantic publication."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    finalize_embedding_generation,
    initialize_semantic_state,
    register_embedding_model,
    start_embedding_generation,
)


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')


def _model(path: Path, signature: str) -> EmbeddingModelSpec:
    model = EmbeddingModelSpec(
        signature,
        f"space-{signature}",
        EmbeddingModality.TEXT,
        f"fixture/{signature}",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    initialize_semantic_state(path)
    register_embedding_model(path, model, allow_test_provider=True)
    return model


def _source_head(
    source_kind: str,
    *,
    complete: bool,
    coverage: str,
    source_status: str,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "schema": "semantic-source-head-v1",
        "source_kind": source_kind,
        "database_name": f"{source_kind}.sqlite3",
        "adapter_version": "fixture-adapter-v1",
        "schema_version": 1,
        "row_count": 1,
        "digest": f"sha256:{source_kind:0<64}",
        "complete": complete,
        "coverage": coverage,
        "source_status": source_status,
        "truncated": truncated,
        "reason": None,
    }


@pytest.mark.parametrize(
    "head",
    (
        _source_head(
            "video",
            complete=False,
            coverage="partial",
            source_status="partial",
        ),
        _source_head(
            "image",
            complete=True,
            coverage="complete",
            source_status="done",
            truncated=True,
        ),
        _source_head(
            "image",
            complete=True,
            coverage="complete",
            source_status="partial",
        ),
    ),
)
def test_partial_video_or_image_coverage_never_publishes_ready(
    tmp_path: Path,
    head: dict[str, object],
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _model(database, f"coverage-{head['source_kind']}-{head['source_status']}")
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="coverage-guard-v1",
        provenance={"source_heads": [head]},
        materialize_base=False,
    )

    with pytest.raises(SemanticStateError, match="source coverage"):
        finalize_embedding_generation(database, generation)

    partial = finalize_embedding_generation(
        database,
        generation,
        allow_partial=True,
    )
    assert partial.status == "ready_partial"
    assert partial.errors == partial.stale == partial.unfinished == 0


def test_complete_multimodal_head_still_allows_ready_and_replay_metadata(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _model(database, "coverage-complete")
    head = _source_head(
        "video",
        complete=True,
        coverage="complete",
        source_status="complete",
    )
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="coverage-complete-v1",
        provenance={"source_heads": [head]},
        materialize_base=False,
    )

    summary = finalize_embedding_generation(database, generation)

    assert summary.status == "ready"

