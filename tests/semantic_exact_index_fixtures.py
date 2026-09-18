"""Canonical small Semantic-owner fixtures for exact-index tests.

The helper uses the real state and publication APIs with deterministic local
vectors.  It never loads an encoder or relies on a production corpus; every
database and source path belongs to the caller's temporary directory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path

from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    ExactSearchQuery,
    SemanticItem,
    TextChunk,
    VectorDType,
    fingerprint_text,
)
from neocortex.semantic.semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
)
from neocortex.semantic.semantic_state import (
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    finalize_text_chunk_refresh,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)

from tests.test_semantic_state import _initialize, _text_model


TEST_CAPABILITIES = ("inference",)


@dataclass(frozen=True, slots=True)
class PublishedTextFixture:
    """Published text owner state plus its deterministic query."""

    database: Path
    model: EmbeddingModelSpec
    query: ExactSearchQuery
    item_ids: tuple[str, ...]
    row_count: int


def _vector_for(index: int, *, duplicate: bool) -> tuple[float, ...]:
    # Distinct scores exercise top-K retention.  The duplicate item's two
    # concrete chunks intentionally tie so discovery/evidence grouping and the
    # canonical entity tie rule remain observable.
    score = 0.82 if duplicate else 0.20 + 0.025 * (index % 20)
    return (score, math.sqrt(1.0 - score * score), 0.0, 0.0)


def published_text_fixture(
    tmp_path: Path,
    *,
    rows: int = 24,
    dtype: str = "float16",
    with_titles: bool = False,
) -> PublishedTextFixture:
    """Create a ready text generation with deterministic, published vectors.

    ``rows`` counts concrete chunks.  One resource has two chunks; all other
    resources have one.  With ``with_titles=True``, up to four of the
    single-chunk resources use the canonical title section kind and the rest
    use content sections.
    """

    if isinstance(rows, bool) or not isinstance(rows, int) or not 2 <= rows <= 512:
        raise ValueError("rows must be an integer between 2 and 512")
    if dtype not in {"float16", "float32"}:
        raise ValueError("dtype must be float16 or float32")
    if not isinstance(with_titles, bool):
        raise ValueError("with_titles must be boolean")

    database = tmp_path / "semantic-exact-index.sqlite3"
    model = _text_model(
        f"fixture-exact-index-{dtype}",
        f"fixture-exact-index-space-{dtype}",
    )
    if dtype == "float32":
        model = replace(model, vector_dtype=VectorDType.FLOAT32)
    _initialize(database, model)

    chunks: list[TextChunk] = []
    vectors: dict[str, tuple[float, ...]] = {}
    item_ids: list[str] = []

    def add_item(
        item_id: str,
        *,
        section_kind: str,
        section_id: str,
        chunk_count: int = 1,
        duplicate: bool = False,
    ) -> None:
        item_ids.append(item_id)
        body = f"exact index fixture {item_id} owner evidence"
        item = SemanticItem(
            item_id=item_id,
            source_kind="pdf",
            source_identity=f"exact-index-identity:{item_id}",
            identity_version="exact-index-fixture-v1",
            fingerprint=fingerprint_text(body),
            path=str(tmp_path / f"{item_id}.pdf"),
            provenance={"fixture": "semantic-exact-index", "item": item_id},
            source_revision={"fixture_revision": 1},
        )
        upsert_semantic_item(
            database,
            item,
            refresh_token=f"items:{item_id}",
            updated_ns=100 + len(item_ids),
        )
        item_chunks: list[TextChunk] = []
        for ordinal in range(chunk_count):
            text = f"{body}, section {ordinal + 1}"
            chunk_id = f"exact-index-chunk:{item_id}:{ordinal}"
            item_chunks.append(
                TextChunk(
                    chunk_id=chunk_id,
                    item_id=item_id,
                    ordinal=ordinal,
                    section_kind=section_kind,
                    section_id=(
                        section_id
                        if chunk_count == 1
                        else f"{section_id}-{ordinal + 1}"
                    ),
                    start_char=0,
                    end_char=len(text),
                    text=text,
                    fingerprint=fingerprint_text(text),
                    chunking_signature="exact-index-fixture-chunking-v1",
                    provenance={"fixture": True},
                )
            )
            vectors[chunk_id] = _vector_for(len(chunks) + ordinal, duplicate=duplicate)
        stage_text_chunks(
            database,
            tuple(item_chunks),
            refresh_token=f"chunks:{item_id}",
            updated_ns=200 + len(item_ids),
        )
        finalize_text_chunk_refresh(
            database,
            item_id=item_id,
            chunking_signature="exact-index-fixture-chunking-v1",
            refresh_token=f"chunks:{item_id}",
            updated_ns=300 + len(item_ids),
        )
        chunks.extend(item_chunks)

    add_item(
        "exact-index-duplicate",
        section_kind="pdf_page",
        section_id="duplicate",
        chunk_count=2,
        duplicate=True,
    )
    remaining = rows - 2
    title_count = min(4, remaining) if with_titles else 0
    for index in range(remaining):
        is_title = index < title_count
        add_item(
            f"exact-index-item-{index:03d}",
            section_kind=SEMANTIC_TITLE_SECTION_KIND if is_title else "pdf_page",
            section_id=SEMANTIC_TITLE_POLICY if is_title else str(index + 1),
        )

    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="exact-index-fixture-processing-v1",
        started_ns=1_000,
    )
    assert enqueue_text_chunk_jobs(
        database,
        generation,
        tuple(chunk.chunk_id for chunk in chunks),
        now_ns=1_001,
    ) == rows
    worker = "exact-index-fixture-worker"
    leases = claim_embedding_jobs(
        database,
        generation,
        worker_id=worker,
        limit=rows,
        lease_seconds=60,
        now_ns=1_002,
    )
    assert len(leases) == rows
    for offset, lease in enumerate(leases, 1):
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id=worker,
            vector=vectors[lease.entity_id],
            provenance={"fixture": "semantic-exact-index", "entity": lease.entity_id},
            now_ns=1_002 + offset,
        )
    assert finalize_embedding_generation(
        database,
        generation,
        completed_ns=2_000,
    ).status == "ready"

    query = ExactSearchQuery(
        query_model_signature=model.model_signature,
        vector_space=model.vector_space,
        dimensions=model.dimensions,
        vector=(1.0, 0.0, 0.0, 0.0),
        target_modality=EmbeddingModality.TEXT,
        indexed_model_signatures=(model.model_signature,),
    )
    return PublishedTextFixture(
        database=database,
        model=model,
        query=query,
        item_ids=tuple(item_ids),
        row_count=rows,
    )


__all__ = ["PublishedTextFixture", "published_text_fixture"]
