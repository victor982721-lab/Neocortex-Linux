"""Real source-owner changes cannot bypass text publication freshness checks."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.semantic import semantic_service as service
from neocortex.semantic import semantic_sources as sources
from neocortex.semantic.semantic_generation_repository import (
    invalidate_embedding_generations_for_source_change,
    start_embedding_generation,
)
from neocortex.semantic.semantic_models import EmbeddingModality, ExactSearchQuery, fingerprint_text
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    search_exact_page,
    semantic_database,
)
from tests.test_semantic_service import _FixtureBackend
from tests.test_semantic_source_heads import _pdf_state


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _replace_pdf_text(database: Path, text: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE documents SET normalized_text_xxh3_128=?,
                normalized_text_chars=?,mtime_ns=mtime_ns+1""",
            (fingerprint_text(text).xxh3_128, len(text)),
        )
        connection.execute(
            "UPDATE pages SET text_zlib=?,text_chars=?",
            (zlib.compress(text.encode("utf-8")), len(text)),
        )


def test_incomplete_initial_head_cannot_hide_a_changed_source_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = sources.semantic_source_database(tmp_path, "pdf")
    _pdf_state(owner)
    _replace_pdf_text(owner, "Verified transformer maintenance record before refresh")
    model = service.multilingual_text_model()
    monkeypatch.setattr(service, "_backend", lambda model, **_kwargs: _FixtureBackend(model))
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    published = baseline.generations[0].summary.generation_id
    _replace_pdf_text(owner, "Intermediate transformer winding inspection record")

    original_stamp = sources._owner_stamp
    stamp_calls = 0

    def change_owner_after_head_projection(path: Path):
        nonlocal stamp_calls
        stamp_calls += 1
        if path == owner and stamp_calls == 2:
            # A real writer commits after the head's read transaction.  The
            # adapter detects it and returns an incomplete/blocked projection;
            # the ordinary record iterator subsequently reads the new revision.
            _replace_pdf_text(owner, "Latest transformer pressure inspection record")
        return original_stamp(path)

    monkeypatch.setattr(sources, "_owner_stamp", change_owner_after_head_projection)
    original_heads = service._text_index.semantic_source_heads
    observed_heads: list[tuple[sources.SemanticSourceHead, ...]] = []

    def capture_heads(state_directory: Path, source_kinds):
        heads = original_heads(state_directory, source_kinds)
        observed_heads.append(heads)
        return heads

    monkeypatch.setattr(service._text_index, "semantic_source_heads", capture_heads)
    with pytest.raises(sources.SemanticSourceError, match="source heads are blocked"):
        service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)

    assert len(observed_heads) == 1
    assert not observed_heads[0][0].complete
    assert observed_heads[0][0].coverage == "blocked"
    current_head = original_heads(tmp_path, ("pdf",))[0]
    assert current_head.complete
    assert observed_heads[0][0].digest != current_head.digest
    database = baseline.semantic_database
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()[0]
            == published
        )
        assert connection.execute("SELECT COUNT(*) FROM embedding_generations").fetchone()[0] == 1
    query = ExactSearchQuery(
        model.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0,) + (0.0,) * (model.dimensions - 1),
        EmbeddingModality.TEXT,
        indexed_model_signatures=(model.model_signature,),
    )
    assert {hit.generation_id for hit in search_exact_page(database, query).hits} == {published}

    # A stable retry still publishes normally and its exact replay does no work.
    refreshed = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    assert refreshed.generations[0].summary.status == "ready"
    assert refreshed.generations[0].summary.generation_id != published
    replay = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    assert replay.execution_mode == "exact_replay"
    assert replay.generations[0].summary.generation_id == (
        refreshed.generations[0].summary.generation_id
    )


@pytest.mark.parametrize("source_kind", ("pdf", "image"))
def test_stably_blocked_owner_never_initializes_backend_or_semantic_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
) -> None:
    database = sources.semantic_source_database(tmp_path, source_kind)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    first = sources.semantic_source_heads(tmp_path, (source_kind,))
    assert first == sources.semantic_source_heads(tmp_path, (source_kind,))
    assert first[0].coverage == "blocked" and not first[0].complete

    def no_backend(*_args, **_kwargs):
        raise AssertionError("a blocked source must be rejected before loading a model")

    monkeypatch.setattr(service, "_backend", no_backend)
    for _attempt in range(2):
        with pytest.raises(sources.SemanticSourceError, match="source heads are blocked"):
            if source_kind == "image":
                service.index_image_embeddings(tmp_path)
            else:
                service.index_text_embeddings(tmp_path, source_kinds=(source_kind,))
    assert not (tmp_path / service.SEMANTIC_DATABASE_NAME).exists()


def test_changed_complete_owner_fails_only_its_candidate_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = sources.semantic_source_database(tmp_path, "pdf")
    _pdf_state(owner)
    _replace_pdf_text(owner, "Published transformer baseline before the next inspection")
    model = service.multilingual_text_model()
    monkeypatch.setattr(service, "_backend", lambda model, **_kwargs: _FixtureBackend(model))
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    published = baseline.generations[0].summary.generation_id
    database = baseline.semantic_database
    unrelated = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="unrelated-build-with-retained-work",
    )
    _replace_pdf_text(owner, "Intermediate winding inspection before the concurrent commit")
    original_records = service.iter_text_source_records
    changed = False

    def records_then_commit(state_directory: Path, source_kind: str):
        nonlocal changed
        yield from original_records(state_directory, source_kind)
        if not changed:
            changed = True
            _replace_pdf_text(owner, "Latest pressure inspection after the concurrent commit")

    monkeypatch.setattr(service, "iter_text_source_records", records_then_commit)
    with pytest.raises(RuntimeError, match="source heads changed during text enumeration"):
        service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)

    with semantic_database(database, readonly=True) as connection:
        candidate = connection.execute(
            "SELECT generation_id,status FROM embedding_generations ORDER BY generation_id DESC"
        ).fetchone()
        candidate_id = int(candidate[0])
        assert candidate_id not in {published, unrelated}
        assert candidate[1] == "failed"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?", (candidate_id,)
            ).fetchone()[0]
            > 0
        )
        assert (
            connection.execute(
                "SELECT status FROM embedding_generations WHERE generation_id=?", (unrelated,)
            ).fetchone()[0]
            == "building"
        )
        assert (
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()[0]
            == published
        )

    refreshed = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    assert refreshed.complete
    assert refreshed.generations[0].summary.generation_id not in {
        published,
        candidate_id,
        unrelated,
    }
    replay = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    assert replay.execution_mode == "exact_replay" and replay.complete


@pytest.mark.parametrize("protected_kind", ("published", "different_source_heads"))
def test_source_invalidation_batch_preserves_published_or_unrelated_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_kind: str,
) -> None:
    owner = sources.semantic_source_database(tmp_path, "pdf")
    _pdf_state(owner)
    _replace_pdf_text(owner, "Published transformer condition for invalidation isolation")
    model = service.multilingual_text_model()
    monkeypatch.setattr(service, "_backend", lambda model, **_kwargs: _FixtureBackend(model))
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), model=model)
    database = baseline.semantic_database
    published = baseline.generations[0].summary.generation_id
    expected = [head.as_payload() for head in sources.semantic_source_heads(tmp_path, ("pdf",))]
    observed = [{**expected[0], "digest": "sha256:" + "b" * 64}]
    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="candidate-to-preserve-on-invalid-batch",
        provenance={"source_heads": expected},
    )
    protected = published
    if protected_kind == "different_source_heads":
        protected = start_embedding_generation(
            database,
            model_signature=model.model_signature,
            processing_signature="unrelated-owner-build",
            provenance={"source_heads": observed},
        )

    with pytest.raises(SemanticStateError, match="source invalidation"):
        invalidate_embedding_generations_for_source_change(
            database,
            (candidate, protected),
            expected_source_heads=expected,
            observed_source_heads=observed,
        )

    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT status FROM embedding_generations WHERE generation_id=?", (candidate,)
            ).fetchone()[0]
            == "building"
        )
        assert connection.execute(
            "SELECT status FROM embedding_generations WHERE generation_id=?", (protected,)
        ).fetchone()[0] == ("ready" if protected_kind == "published" else "building")
        assert (
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()[0]
            == published
        )
