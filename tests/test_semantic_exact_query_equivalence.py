"""Reference/candidate equivalence tests for the exact Semantic query path.

The reference SQL below is a frozen copy of the pre-P1 ``_search_sql``
contract.  Each test runs the normal candidate and then the same public API
with that SQL function injected as the reference.  The fixture is temporary
and is built through the real Semantic state APIs; these tests intentionally
do not use ANN, an approximate score, or a global top-K assertion across local
cursor pages.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from typing import Callable, Sequence

import pytest

from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic.semantic_chunking import TextChunkingConfig, chunk_text_sections
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    ExactSearchPage,
    ExactSearchQuery,
    SemanticItem,
    TextChunk,
    TextSection,
    fingerprint_bytes,
    fingerprint_text,
)
from neocortex.semantic.semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
)
from neocortex.semantic.semantic_state import (
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_image_item_jobs,
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    finalize_text_chunk_refresh,
    resolve_search_hits,
    search_exact_evidence_page,
    search_exact_page,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from tests.test_retrieval_target_diagnostics import _published_fixture
from tests.test_semantic_state import _image_model, _initialize, _stage_text_item, _text_model


TEST_CAPABILITIES = ("base", "inference", "image")
pytestmark = pytest.mark.capability("base", "inference")


# Frozen pre-P1 SQL oracle.  Keep both modality branches complete: the
# candidate may narrow the projection, but it must not change predicates,
# selected model/head multiplicity, cursor order, or text-scope semantics.
_REFERENCE_TITLE_KIND = "semantic_metadata_title"
_REFERENCE_VIDEO_TITLE_KIND = "video_metadata_title"


def _reference_search_sql(
    modality: EmbeddingModality,
    pair_count: int,
    *,
    text_scope: str = "all",
) -> str:
    if text_scope not in {"all", "content", "title"}:
        raise ValueError("text_scope must be all, content or title")
    if modality is not EmbeddingModality.TEXT and text_scope != "all":
        raise ValueError("text_scope is only valid for text embeddings")
    selected = ",".join("(?,?)" for _ in range(pair_count))
    if modality is EmbeddingModality.TEXT:
        scope_clause = {
            "all": "",
            "content": (
                " AND c.section_kind NOT IN "
                f"('{_REFERENCE_TITLE_KIND}','{_REFERENCE_VIDEO_TITLE_KIND}')"
            ),
            "title": (
                " AND c.section_kind IN "
                f"('{_REFERENCE_TITLE_KIND}','{_REFERENCE_VIDEO_TITLE_KIND}')"
            ),
        }[text_scope]
        return f"""WITH selected(model_signature,generation_id) AS
            (VALUES {selected})
        SELECT e.member_id AS ref_id,e.entity_id,i.item_id,
            e.model_signature,m.vector_space,m.modality,e.generation_id,
            e.provenance_json,p.vector_blob,p.dimensions,p.vector_dtype
        FROM selected s
        JOIN embedding_generation_members e
          ON e.model_signature=s.model_signature
         AND e.generation_id=s.generation_id
        JOIN embedding_models m ON m.model_signature=e.model_signature
        JOIN vector_payloads p ON p.payload_id=e.payload_id
        JOIN semantic_chunk_revisions c
          ON c.chunk_revision_id=e.chunk_revision_id
        JOIN semantic_item_revisions i
          ON i.item_revision_id=e.item_revision_id
        JOIN embedding_generations g ON g.generation_id=e.generation_id
        WHERE e.member_id>? AND e.entity_kind='text_chunk' AND g.status='ready'
          AND e.content_xxh3_128=c.content_xxh3_128
          AND e.content_bytes=c.content_bytes
          AND e.content_xxh3_64_guard=c.content_xxh3_64_guard
          {scope_clause}
        ORDER BY e.member_id LIMIT ?"""
    return f"""WITH selected(model_signature,generation_id) AS
        (VALUES {selected})
    SELECT e.member_id AS ref_id,e.entity_id,i.item_id,
        e.model_signature,m.vector_space,m.modality,e.generation_id,
        e.provenance_json,p.vector_blob,p.dimensions,p.vector_dtype
    FROM selected s
    JOIN embedding_generation_members e
      ON e.model_signature=s.model_signature
     AND e.generation_id=s.generation_id
    JOIN embedding_models m ON m.model_signature=e.model_signature
    JOIN vector_payloads p ON p.payload_id=e.payload_id
    JOIN semantic_item_revisions i ON i.item_revision_id=e.item_revision_id
    JOIN embedding_generations g ON g.generation_id=e.generation_id
    WHERE e.member_id>? AND e.entity_kind='image_item' AND g.status='ready'
      AND e.content_xxh3_128=i.content_xxh3_128
      AND e.content_bytes=i.content_bytes
      AND e.content_xxh3_64_guard=i.content_xxh3_64_guard
    ORDER BY e.member_id LIMIT ?"""


@dataclass(frozen=True)
class _PageOracle:
    page: ExactSearchPage
    score_hex: tuple[str, ...]
    provenance_json: tuple[str, ...]


def _page_oracle(page: ExactSearchPage) -> _PageOracle:
    return _PageOracle(
        page=page,
        score_hex=tuple(hit.score.hex() for hit in page.hits),
        provenance_json=tuple(
            json.dumps(
                hit.provenance,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for hit in page.hits
        ),
    )


def _assert_page_equal(reference: ExactSearchPage, candidate: ExactSearchPage) -> None:
    reference_oracle = _page_oracle(reference)
    candidate_oracle = _page_oracle(candidate)
    # Dataclass equality covers every SearchHit field; score_hex/provenance
    # make the no-rounding/no-re-serialization requirement explicit.
    assert candidate_oracle == reference_oracle
    assert candidate_oracle.score_hex == reference_oracle.score_hex
    assert candidate_oracle.provenance_json == reference_oracle.provenance_json


def _query(
    model: EmbeddingModelSpec,
    *,
    indexed_models: tuple[str, ...] | None = None,
    modality: EmbeddingModality | None = None,
) -> ExactSearchQuery:
    return ExactSearchQuery(
        query_model_signature=model.model_signature,
        vector_space=model.vector_space,
        dimensions=model.dimensions,
        vector=(1.0,) + (0.0,) * (model.dimensions - 1),
        target_modality=model.modality if modality is None else modality,
        indexed_model_signatures=(
            (model.model_signature,) if indexed_models is None else indexed_models
        ),
    )


def _run_pair(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[], ExactSearchPage],
) -> tuple[ExactSearchPage, ExactSearchPage]:
    """Return (reference, candidate) using the same DB, query and batch."""

    candidate = invoke()
    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(repository, "_search_sql", _reference_search_sql)
        reference = invoke()
    return reference, candidate


def _outcome(invoke: Callable[[], ExactSearchPage]) -> tuple[object, ...]:
    try:
        return ("ok", _page_oracle(invoke()))
    except Exception as exc:  # compare the fail-closed boundary exactly
        return ("error", type(exc).__name__, str(exc))


def _run_error_pair(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[], ExactSearchPage],
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(repository, "_search_sql", _reference_search_sql)
        reference = _outcome(invoke)
    candidate = _outcome(invoke)
    return reference, candidate


def _publish_generation(
    database: Path,
    model: EmbeddingModelSpec,
    chunks: Sequence[TextChunk],
    *,
    processing_signature: str,
    started_ns: int,
    vector: tuple[float, ...] | None = None,
) -> int:
    """Publish one complete generation for a whole chunk cohort.

    This deliberately does not call ``_complete_text_job`` per chunk: that
    helper creates one generation per chunk.  A multi-row exact fixture needs
    one generation with all jobs enqueued and completed before finalization.
    """

    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        started_ns=started_ns,
    )
    assert enqueue_text_chunk_jobs(
        database,
        generation,
        tuple(chunk.chunk_id for chunk in chunks),
        now_ns=started_ns + 1,
    ) == len(chunks)
    leases = claim_embedding_jobs(
        database,
        generation,
        worker_id=f"equivalence-worker-{model.model_signature}",
        limit=len(chunks),
        lease_seconds=60,
        now_ns=started_ns + 2,
    )
    assert len(leases) == len(chunks)
    chosen = vector or ((1.0,) + (0.0,) * (model.dimensions - 1))
    for offset, lease in enumerate(leases, 1):
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id=f"equivalence-worker-{model.model_signature}",
            vector=chosen,
            provenance={"fixture": "exact-query-equivalence"},
            now_ns=started_ns + 2 + offset,
        )
    summary = finalize_embedding_generation(
        database,
        generation,
        completed_ns=started_ns + 3 + len(leases),
    )
    assert summary.status == "ready"
    return generation


def _owner_files(database: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in database.parent.glob(f"{database.name}*")
        if path.is_file()
    }


@pytest.mark.parametrize("index_state", ("absent", "different_definition"))
def test_exact_query_equivalence_does_not_require_search_index_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index_state: str,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    with semantic_database(database) as connection:
        connection.execute("DROP INDEX embedding_generation_members_search_idx")
        if index_state == "different_definition":
            connection.execute(
                "CREATE INDEX embedding_generation_members_search_idx "
                "ON embedding_generation_members(item_id)",
            )
        connection.commit()
    before = _owner_files(database)
    reference, candidate = _run_error_pair(
        monkeypatch,
        lambda: search_exact_page(database, _query(model), max_vectors=100, batch_size=2),
    )
    assert reference[0] == "ok"
    assert candidate == reference
    assert _owner_files(database) == before


@pytest.mark.parametrize("cancel_at", (None, 3))
def test_exact_query_equivalence_keeps_callbacks_and_target_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_at: int | None,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    before = _owner_files(database)

    def invoke() -> tuple[tuple[object, ...], int, dict[str, object]]:
        calls = 0
        diagnostics: dict[str, object] = {}

        def check() -> None:
            nonlocal calls
            calls += 1
            if calls == cancel_at:
                raise RuntimeError("exact-query-fixture-cancelled")

        outcome = _outcome(lambda: repository.search_exact_page(
            database, _query(model), limit=2, max_vectors=100, batch_size=2,
            cancellation_check=check,
            diagnostic_item_ids=("item:target", "item:a"), diagnostics=diagnostics,
        ))
        return outcome, calls, diagnostics

    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(repository, "_search_sql", _reference_search_sql)
        reference = invoke()
    candidate = invoke()
    assert candidate == reference
    if cancel_at is None:
        assert reference[0][0] == "ok" and reference[2]["target_diagnostics"]
    else:
        assert reference[0] == ("error", "RuntimeError", "exact-query-fixture-cancelled")
        assert reference[1] == cancel_at
    assert _owner_files(database) == before


@pytest.mark.parametrize("evidence_mode", (False, True))
def test_exact_query_equivalence_single_model_discovery_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_mode: bool,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    query = _query(model)
    search = search_exact_evidence_page if evidence_mode else search_exact_page
    before = _owner_files(database)

    reference, candidate = _run_pair(
        monkeypatch,
        lambda: search(
            database,
            query,
            limit=2,
            max_vectors=100,
            batch_size=2,
        ),
    )

    _assert_page_equal(reference, candidate)
    assert _owner_files(database) == before


@pytest.mark.parametrize("evidence_mode", (False, True))
def test_exact_query_equivalence_two_comparable_heads_duplicate_models_and_ties(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_mode: bool,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model_a = _text_model("equivalence-model-a", "equivalence-shared-space")
    model_b = _text_model("equivalence-model-b", "equivalence-shared-space")
    _initialize(database, model_a, model_b)
    chunks = tuple(
        _stage_text_item(
            database,
            f"equivalence-item-{index}",
            f"equal score fixture item {index} transformer record",
            refresh=f"equivalence-item-refresh-{index}",
        )[1]
        for index in range(3)
    )
    _publish_generation(
        database,
        model_a,
        chunks,
        processing_signature="equivalence-generation-a",
        started_ns=100,
    )
    _publish_generation(
        database,
        model_b,
        chunks,
        processing_signature="equivalence-generation-b",
        started_ns=200,
    )
    # ExactSearchQuery currently accepts repeated indexed signatures.  The
    # acceptance oracle deliberately preserves that behavior rather than
    # silently normalizing/deduplicating the request.
    query = _query(
        model_a,
        indexed_models=(model_b.model_signature, model_a.model_signature, model_b.model_signature),
    )
    search = search_exact_evidence_page if evidence_mode else search_exact_page
    before = _owner_files(database)

    reference, candidate = _run_pair(
        monkeypatch,
        lambda: search(
            database,
            query,
            limit=3 if not evidence_mode else 5,
            max_vectors=100,
            batch_size=2,
        ),
    )

    _assert_page_equal(reference, candidate)
    assert reference.complete is True
    assert reference.scanned >= len(chunks)
    assert _owner_files(database) == before


@pytest.mark.parametrize(
    ("batch_size", "max_vectors"),
    ((1, 1), (2, 3), (512, 6)),
)
@pytest.mark.parametrize("evidence_mode", (False, True))
def test_exact_query_equivalence_cursor_partial_pages_preserve_local_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch_size: int,
    max_vectors: int,
    evidence_mode: bool,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    query = _query(model)
    search = search_exact_evidence_page if evidence_mode else search_exact_page
    before = _owner_files(database)

    def collect(invoke: Callable[[int], ExactSearchPage]) -> tuple[ExactSearchPage, ...]:
        pages: list[ExactSearchPage] = []
        after = 0
        for _ in range(32):
            page = invoke(after)
            pages.append(page)
            if page.complete:
                break
            assert page.next_cursor is not None and page.next_cursor > after
            after = page.next_cursor
        else:  # pragma: no cover - a bounded fixture must terminate
            raise AssertionError("cursor sequence did not terminate")
        return tuple(pages)

    def candidate(after: int) -> ExactSearchPage:
        return search(
            database,
            query,
            limit=2,
            max_vectors=max_vectors,
            after_ref_id=after,
            batch_size=batch_size,
        )

    candidate_pages = collect(candidate)
    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(repository, "_search_sql", _reference_search_sql)
        reference_pages = collect(candidate)

    assert len(candidate_pages) == len(reference_pages)
    for reference, actual in zip(reference_pages, candidate_pages, strict=True):
        # Each page is a local top-K result.  Do not assert global top-K
        # equivalence across concatenated pages or uniqueness of item_id across
        # pages; those are not the public cursor contract.
        _assert_page_equal(reference, actual)
    assert _owner_files(database) == before


def _scoped_fixture(
    database: Path,
    *,
    text_model: EmbeddingModelSpec,
    image_model: EmbeddingModelSpec,
    image_root: Path,
) -> tuple[TextChunk, Path]:
    item = SemanticItem(
        item_id="scope-text-item",
        source_kind="pdf",
        source_identity="scope-text-identity",
        identity_version="scope-fixture-v1",
        fingerprint=fingerprint_text("contenido corporal fixture titulo tecnico"),
        path="/fixtures/scope.pdf",
    )
    upsert_semantic_item(database, item, refresh_token="scope-item-refresh", updated_ns=10)
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunks = chunk_text_sections(
        item.item_id,
        (
            TextSection("pdf_page", "1", "contenido corporal fixture tecnico"),
            TextSection(
                SEMANTIC_TITLE_SECTION_KIND,
                SEMANTIC_TITLE_POLICY,
                "titulo tecnico fixture",
                {"advisory_only": True},
            ),
        ),
        config,
    )
    assert len(chunks) == 2
    stage_text_chunks(database, chunks, refresh_token="chunks:scope-text-item", updated_ns=11)
    finalize_text_chunk_refresh(
        database,
        item_id=item.item_id,
        chunking_signature=config.signature,
        refresh_token="chunks:scope-text-item",
        updated_ns=12,
    )
    _publish_generation(
        database,
        text_model,
        chunks,
        processing_signature="scope-text-generation",
        started_ns=20,
    )

    image_path = image_root / "scope-image.bin"
    image_payload = b"scope-image-fixture"
    image_path.write_bytes(image_payload)
    image_item = SemanticItem(
        item_id="scope-image-item",
        source_kind="image",
        source_identity="scope-image-identity",
        identity_version="scope-image-v1",
        fingerprint=fingerprint_bytes(image_payload),
        path=str(image_path),
        provenance={"fixture": True},
        source_revision={"revision": 1},
    )
    upsert_semantic_item(database, image_item, refresh_token="scope-image-refresh", updated_ns=30)
    image_generation = start_embedding_generation(
        database,
        model_signature=image_model.model_signature,
        processing_signature="scope-image-generation",
        started_ns=31,
    )
    assert enqueue_image_item_jobs(database, image_generation, (image_item.item_id,), now_ns=32) == 1
    lease = claim_embedding_jobs(
        database,
        image_generation,
        worker_id="scope-image-worker",
        limit=1,
        lease_seconds=60,
        now_ns=33,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="scope-image-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        provenance={"fixture": "scope-image"},
        now_ns=34,
    )
    assert finalize_embedding_generation(database, image_generation, completed_ns=35).status == "ready"
    return chunks[0], image_path


@pytest.mark.parametrize("text_scope", ("all", "content", "title"))
def test_exact_query_equivalence_text_scopes_and_image_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text_scope: str,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    text_model = _text_model("scope-text-model", "scope-text-space")
    image_model = _image_model("scope-image-model", "scope-image-space")
    _initialize(database, text_model, image_model)
    _scoped_fixture(
        database,
        text_model=text_model,
        image_model=image_model,
        image_root=tmp_path,
    )
    before = _owner_files(database)

    text_reference, text_candidate = _run_pair(
        monkeypatch,
        lambda: search_exact_page(
            database,
            _query(text_model),
            limit=2,
            max_vectors=100,
            batch_size=2,
            text_scope=text_scope,  # type: ignore[arg-type]
        ),
    )
    _assert_page_equal(text_reference, text_candidate)
    text_resolved = resolve_search_hits(database, text_candidate.hits)
    if text_scope == "content":
        assert text_resolved
        assert all(value.section_kind != SEMANTIC_TITLE_SECTION_KIND for value in text_resolved)
    if text_scope == "title":
        assert text_resolved
        assert all(value.section_kind == SEMANTIC_TITLE_SECTION_KIND for value in text_resolved)

    image_reference, image_candidate = _run_pair(
        monkeypatch,
        lambda: search_exact_page(
            database,
            _query(image_model, modality=EmbeddingModality.IMAGE),
            limit=1,
            max_vectors=100,
            batch_size=1,
        ),
    )
    _assert_page_equal(image_reference, image_candidate)
    assert all(hit.modality is EmbeddingModality.IMAGE for hit in image_candidate.hits)
    assert _owner_files(database) == before


def _corrupt_scanned_member(database: Path, *, kind: str, offset: int) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """SELECT member_id,payload_id FROM embedding_generation_members
               WHERE item_id='item:target'
               ORDER BY member_id LIMIT 1"""
        ).fetchone()
        if row is None:
            raise AssertionError("target fixture member is missing")
        if offset:
            row = connection.execute(
                """SELECT member_id,payload_id FROM embedding_generation_members
                   WHERE generation_id=(
                     SELECT generation_id FROM published_embedding_heads
                     WHERE model_signature=(
                       SELECT model_signature FROM embedding_generation_members
                       WHERE item_id='item:target' ORDER BY member_id LIMIT 1))
                   ORDER BY member_id LIMIT 1 OFFSET ?""",
                (offset,),
            ).fetchone()
            if row is None:
                raise AssertionError("offset fixture member is missing")
        member_id, payload_id = int(row[0]), int(row[1])
        if kind == "dimension":
            connection.execute("DROP TRIGGER vector_payloads_no_update")
            connection.execute(
                "UPDATE vector_payloads SET dimensions=3 WHERE payload_id=?",
                (payload_id,),
            )
        elif kind == "provenance":
            connection.execute(
                "UPDATE embedding_generation_members SET provenance_json='[]' WHERE member_id=?",
                (member_id,),
            )
        else:  # pragma: no cover - parameter table is exhaustive
            raise AssertionError(kind)


@pytest.mark.parametrize("kind", ("dimension", "provenance"))
def test_exact_query_equivalence_corruption_in_scanned_loser_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    _corrupt_scanned_member(database, kind=kind, offset=0)
    query = _query(model)

    def invoke() -> ExactSearchPage:
        return search_exact_page(
            database,
            query,
            limit=1,
            max_vectors=100,
            batch_size=2,
        )
    reference, candidate = _run_error_pair(monkeypatch, invoke)
    assert reference[0] == candidate[0] == "error"
    assert reference == candidate


@pytest.mark.parametrize("kind", ("dimension", "provenance"))
def test_exact_query_equivalence_row_at_max_vectors_plus_one_is_not_evaluated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    # max_vectors=1 fetches one extra row to establish the cursor, but the
    # extra row is not scored/validated in this page.  Corrupt exactly that
    # row; the next page is responsible for observing it.
    _corrupt_scanned_member(database, kind=kind, offset=1)
    query = _query(model)
    def invoke() -> ExactSearchPage:
        return search_exact_page(
            database,
            query,
            limit=1,
            max_vectors=1,
            batch_size=512,
        )
    reference, candidate = _run_pair(monkeypatch, invoke)
    _assert_page_equal(reference, candidate)
    assert reference.complete is False
    assert reference.scanned == 1
    assert reference.next_cursor is not None


def _resolved_oracle(values: Sequence[object]) -> tuple[object, ...]:
    return tuple(
        (
            value.hit,
            value.path,
            value.source_kind,
            value.source_identity,
            value.section_kind,
            value.section_id,
            value.start_char,
            value.end_char,
            value.snippet,
            json.dumps(value.source_revision, sort_keys=True, separators=(",", ":")),
            json.dumps(value.section_provenance, sort_keys=True, separators=(",", ":")),
            value.source_status,
            value.published_revision_id,
            value.current_revision_id,
        )
        for value in values
    )


def test_exact_query_equivalence_preserves_resolved_locators_and_evidence_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    query = _query(model)
    before = _owner_files(database)
    reference_page, candidate_page = _run_pair(
        monkeypatch,
        lambda: search_exact_evidence_page(
            database,
            query,
            limit=3,
            max_vectors=100,
            batch_size=2,
        ),
    )
    reference = resolve_search_hits(database, reference_page.hits, snippet_chars=12, query="presión interna")
    candidate = resolve_search_hits(database, candidate_page.hits, snippet_chars=12, query="presión interna")
    assert _resolved_oracle(candidate) == _resolved_oracle(reference)
    assert _owner_files(database) == before
