# region [00] Contexto del módulo
# Módulo: tests/test_semantic_generation_publication_v6.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import json
import inspect
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.semantic import semantic_generation_repository
from neocortex.semantic import semantic_lineage_repository
from neocortex.semantic import semantic_schema
from neocortex.semantic.semantic_chunking import (
    TextChunkingConfig,
    chunk_text_sections,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    ExactSearchQuery,
    GenerationSummary,
    SemanticItem,
    TextChunk,
    TextSection,
    encode_vector,
    fingerprint_text,
)
from neocortex.semantic.semantic_item_repository import _encode_chunk_text
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    fail_embedding_job,
    finalize_embedding_generation,
    finalize_text_chunk_refresh,
    has_active_embeddings,
    initialize_semantic_state,
    prepare_embedding_generation,
    register_embedding_model,
    resolve_search_hits,
    reuse_cached_jobs,
    search_exact_page,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from neocortex.semantic.semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
)
# endregion [01]

# region [02] Implementación


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "publication-model-v1",
        "publication-space-v1",
        EmbeddingModality.TEXT,
        "fixture/publication-model",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _initialize(path: Path) -> EmbeddingModelSpec:
    model = _model()
    initialize_semantic_state(path)
    register_embedding_model(path, model, allow_test_provider=True)
    return model


def _stage(path: Path, item_id: str, text: str, revision: int) -> TextChunk:
    item = SemanticItem(
        item_id,
        "pdf",
        f"identity:{item_id}",
        "fixture-v1",
        fingerprint_text(text),
        path=f"C:/fixtures/{item_id}.pdf",
        provenance={"revision": revision},
    )
    upsert_semantic_item(
        path,
        item,
        refresh_token=f"item-r{revision}",
        updated_ns=revision * 10,
    )
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunks = chunk_text_sections(
        item_id,
        (TextSection("pdf_page", "1", text, {"revision": revision}),),
        config,
    )
    assert len(chunks) == 1
    refresh = f"chunk-r{revision}:{item_id}"
    stage_text_chunks(path, chunks, refresh_token=refresh, updated_ns=revision * 10 + 1)
    finalize_text_chunk_refresh(
        path,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token=refresh,
        updated_ns=revision * 10 + 2,
    )
    return chunks[0]


def _stage_profile(
    path: Path,
    item_id: str,
    text: str,
    revision: int,
    *,
    source_kind: str,
    config: TextChunkingConfig,
) -> TextChunk:
    item = SemanticItem(
        item_id,
        source_kind,
        f"identity:{item_id}",
        "fixture-v1",
        fingerprint_text(text),
        path=f"C:/fixtures/{item_id}",
        provenance={"revision": revision},
    )
    upsert_semantic_item(
        path,
        item,
        refresh_token=f"item-r{revision}:{item_id}",
        updated_ns=revision * 10,
    )
    chunks = chunk_text_sections(
        item_id,
        (TextSection("fixture", "1", text, {"revision": revision}),),
        config,
    )
    assert len(chunks) == 1
    refresh = f"chunk-r{revision}:{item_id}:{config.signature}"
    stage_text_chunks(path, chunks, refresh_token=refresh, updated_ns=revision * 10 + 1)
    finalize_text_chunk_refresh(
        path,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token=refresh,
        updated_ns=revision * 10 + 2,
    )
    return chunks[0]


def _query(model: EmbeddingModelSpec) -> ExactSearchQuery:
    return ExactSearchQuery(
        model.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0, 0.0, 0.0, 0.0),
        EmbeddingModality.TEXT,
        indexed_model_signatures=(model.model_signature,),
    )


def test_successor_replaces_only_the_selected_source_chunking_profile(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    profile_a = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
        algorithm_version="fixture-profile-a",
    )
    profile_b = TextChunkingConfig(
        max_chars=192,
        max_terms=48,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
        algorithm_version="fixture-profile-b",
    )
    pdf_a = _stage_profile(
        database,
        "pdf-item",
        "protección diferencial de transformador",
        1,
        source_kind="pdf",
        config=profile_a,
    )
    docx_a = _stage_profile(
        database,
        "docx-item",
        "mantenimiento preventivo de subestación",
        1,
        source_kind="docx",
        config=profile_a,
    )
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="fixture-profile-a|sources=pdf,docx",
        provenance={
            "sources": ["pdf", "docx"],
            "chunking_signature": profile_a.signature,
        },
        started_ns=90,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            baseline,
            (pdf_a.chunk_id, docx_a.chunk_id),
            now_ns=90,
        )
        == 2
    )
    _complete_jobs(database, baseline, now_ns=100)
    finalize_embedding_generation(database, baseline, completed_ns=110)

    pdf_b = _stage_profile(
        database,
        "pdf-item",
        "protección diferencial de transformador",
        2,
        source_kind="pdf",
        config=profile_b,
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="fixture-profile-b|sources=pdf",
        provenance={
            "sources": ["pdf"],
            "chunking_signature": profile_b.signature,
        },
        started_ns=120,
    )
    assert enqueue_text_chunk_jobs(database, successor, (pdf_b.chunk_id,), now_ns=120) == 1
    _complete_jobs(database, successor, now_ns=130)
    finalize_embedding_generation(database, successor, completed_ns=140)

    with semantic_database(database, readonly=True) as connection:
        profiles = tuple(
            (
                str(row["source_kind"]),
                str(row["chunking_signature"]),
                str(row["entity_id"]),
            )
            for row in connection.execute(
                """SELECT item_revision.source_kind,
                    chunk_revision.chunking_signature,member.entity_id
                FROM embedding_generation_members member
                JOIN semantic_item_revisions item_revision
                  ON item_revision.item_revision_id=member.item_revision_id
                JOIN semantic_chunk_revisions chunk_revision
                  ON chunk_revision.chunk_revision_id=member.chunk_revision_id
                WHERE member.generation_id=? ORDER BY item_revision.source_kind""",
                (successor,),
            )
        )
        retained_historical_chunks = int(
            connection.execute("SELECT COUNT(*) FROM semantic_chunk_revisions").fetchone()[0]
        )
    assert profiles == (
        ("docx", profile_a.signature, docx_a.chunk_id),
        ("pdf", profile_b.signature, pdf_b.chunk_id),
    )
    assert retained_historical_chunks == 3
    assert {hit.entity_id for hit in search_exact_page(database, _query(model)).hits} == {
        docx_a.chunk_id,
        pdf_b.chunk_id,
    }


def _complete_jobs(path: Path, generation_id: int, *, now_ns: int) -> None:
    leases = claim_embedding_jobs(
        path,
        generation_id,
        worker_id="publication-worker",
        limit=32,
        lease_seconds=60,
        now_ns=now_ns,
    )
    for offset, lease in enumerate(leases, 1):
        complete_embedding_job(
            path,
            lease.job_id,
            worker_id="publication-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            provenance={"fixture": "publication"},
            now_ns=now_ns + offset,
        )


def test_provenance_refresh_gets_new_chunk_identity_and_reuses_vector(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    item_id = "adapter-upgrade-document"
    text = "protección diferencial de transformador"
    item = SemanticItem(
        item_id,
        "pdf",
        f"identity:{item_id}",
        "fixture-v1",
        fingerprint_text(text),
        path=f"C:/fixtures/{item_id}.pdf",
    )
    upsert_semantic_item(database, item, refresh_token="item", updated_ns=10)
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )

    original = chunk_text_sections(
        item_id,
        (TextSection("pdf_page", "1", text, {"adapter": "v2"}),),
        config,
    )[0]
    stage_text_chunks(database, (original,), refresh_token="adapter-v2", updated_ns=11)
    finalize_text_chunk_refresh(
        database,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token="adapter-v2",
        updated_ns=12,
    )
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="adapter-v2",
        provenance={"sources": ["pdf"], "chunking_signature": config.signature},
        started_ns=20,
    )
    assert enqueue_text_chunk_jobs(database, baseline, (original.chunk_id,), now_ns=21) == 1
    _complete_jobs(database, baseline, now_ns=22)
    finalize_embedding_generation(database, baseline, completed_ns=30)

    upgraded = chunk_text_sections(
        item_id,
        (TextSection("pdf_page", "1", text, {"adapter": "v3"}),),
        config,
    )[0]
    assert upgraded.fingerprint == original.fingerprint
    assert upgraded.chunk_id != original.chunk_id
    stage_text_chunks(database, (upgraded,), refresh_token="adapter-v3", updated_ns=31)
    finalize_text_chunk_refresh(
        database,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token="adapter-v3",
        updated_ns=32,
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="adapter-v3",
        provenance={"sources": ["pdf"], "chunking_signature": config.signature},
        started_ns=40,
    )
    assert enqueue_text_chunk_jobs(database, successor, (upgraded.chunk_id,), now_ns=41) == 1
    assert reuse_cached_jobs(database, successor, now_ns=42) == 1
    summary = finalize_embedding_generation(database, successor, completed_ns=50)

    with semantic_database(database, readonly=True) as connection:
        payloads = int(connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0])
        revisions = int(
            connection.execute("SELECT COUNT(*) FROM semantic_chunk_revisions").fetchone()[0]
        )
        members = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT entity_id FROM embedding_generation_members "
                "WHERE generation_id=? ORDER BY entity_id",
                (successor,),
            )
        )
    assert summary.status == "ready"
    assert payloads == 1
    assert revisions == 2
    assert members == (upgraded.chunk_id,)


def _finalize_with_trace(
    path: Path,
    generation_id: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    completed_ns: int,
) -> tuple[GenerationSummary, tuple[str, ...]]:
    original_database = semantic_generation_repository.semantic_database
    statements: list[str] = []

    @contextmanager
    def traced_database(
        selected_path: Path,
        *,
        readonly: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with original_database(selected_path, readonly=readonly) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    with monkeypatch.context() as scoped:
        scoped.setattr(
            semantic_generation_repository,
            "semantic_database",
            traced_database,
        )
        result = finalize_embedding_generation(
            path,
            generation_id,
            completed_ns=completed_ns,
        )
    normalized = tuple(" ".join(statement.lower().split()) for statement in statements)
    return result, normalized


def _prepare_with_trace(
    path: Path,
    generation_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GenerationSummary | None, tuple[str, ...]]:
    original_database = semantic_generation_repository.semantic_database
    statements: list[str] = []

    @contextmanager
    def traced_database(
        selected_path: Path,
        *,
        readonly: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with original_database(selected_path, readonly=readonly) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    with monkeypatch.context() as scoped:
        scoped.setattr(
            semantic_generation_repository,
            "semantic_database",
            traced_database,
        )
        result = prepare_embedding_generation(
            path,
            generation_id,
            enumeration_complete=True,
        )
    normalized = tuple(" ".join(statement.lower().split()) for statement in statements)
    return result, normalized


def _completed_generation(
    path: Path,
    *,
    member_count: int,
    processing_signature: str,
) -> tuple[EmbeddingModelSpec, int]:
    model = _initialize(path)
    chunks = tuple(
        _stage(
            path,
            f"finalization-{offset}",
            f"published transformer record {offset}",
            1,
        )
        for offset in range(member_count)
    )
    generation_id = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        started_ns=100,
    )
    enqueue_text_chunk_jobs(
        path,
        generation_id,
        tuple(chunk.chunk_id for chunk in chunks),
        now_ns=101,
    )
    _complete_jobs(path, generation_id, now_ns=102)
    return model, generation_id


def _exact_replay_fixture(
    path: Path,
    *,
    member_count: int,
    processing_signature: str,
) -> tuple[EmbeddingModelSpec, int, int]:
    model, baseline = _completed_generation(
        path,
        member_count=member_count,
        processing_signature=processing_signature,
    )
    finalize_embedding_generation(path, baseline, completed_ns=110)
    candidate = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        materialize_base=False,
        started_ns=120,
    )
    return model, baseline, candidate


def _duplicate_generation_member_rows(
    path: Path,
    generation_id: int,
    *,
    count: int,
) -> None:
    with semantic_database(path) as connection:
        row = connection.execute(
            """SELECT model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,updated_ns
            FROM embedding_generation_members WHERE generation_id=? LIMIT 1""",
            (generation_id,),
        ).fetchone()
        assert row is not None
        existing_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()[0]
        )
        connection.executemany(
            """INSERT INTO embedding_generation_members(
                generation_id,model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,updated_ns,base_member_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                (
                    generation_id,
                    str(row["model_signature"]),
                    str(row["entity_kind"]),
                    f"{row['entity_id']}:clone-fixture:{existing_count + offset}",
                    str(row["item_id"]),
                    int(row["item_revision_id"]),
                    int(row["chunk_revision_id"]),
                    int(row["payload_id"]),
                    str(row["content_xxh3_128"]),
                    int(row["content_bytes"]),
                    str(row["content_xxh3_64_guard"]),
                    str(row["provenance_json"]),
                    int(row["updated_ns"]) + offset,
                )
                for offset in range(1, count + 1)
            ),
        )


def _published_fixture_with_members(
    path: Path,
    *,
    extra_members: int,
) -> tuple[EmbeddingModelSpec, int]:
    model = _initialize(path)
    chunk = _stage(path, "clone-base", "published transformer record", 1)
    generation_id = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature="clone-base-v1",
        provenance={"fixture": "clone-base-v1"},
        started_ns=100,
    )
    enqueue_text_chunk_jobs(path, generation_id, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(path, generation_id, now_ns=102)
    finalize_embedding_generation(path, generation_id, completed_ns=110)
    # Populate the immutable-base fixture directly so clone pagination can be
    # exercised without staging hundreds of otherwise unrelated source items.
    _duplicate_generation_member_rows(
        path,
        generation_id,
        count=extra_members,
    )
    return model, generation_id


def _replace_item_identity(
    path: Path,
    *,
    item_id: str,
    text: str,
    identity_version: str,
    updated_ns: int,
    metadata_revision: str | None = None,
) -> None:
    upsert_semantic_item(
        path,
        SemanticItem(
            item_id,
            "pdf",
            f"identity:{item_id}:{identity_version}",
            identity_version,
            fingerprint_text(text),
            path=f"C:/fixtures/{item_id}.pdf",
            provenance={
                "fixture": "replacement-identity",
                "metadata_revision": metadata_revision or identity_version,
            },
        ),
        refresh_token=f"item:{identity_version}",
        updated_ns=updated_ns,
    )


def test_base_clone_skips_target_entities_with_existing_jobs(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    text = "protección de transformador con identidad reemplazada"
    model = _initialize(database)
    chunk = _stage(database, "clone-target", text, 1)
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="clone-target-base-v1",
        provenance={"fixture": "clone-target-base-v1"},
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, baseline, (chunk.chunk_id,), now_ns=101) == 1
    _complete_jobs(database, baseline, now_ns=102)
    finalize_embedding_generation(database, baseline, completed_ns=110)

    _replace_item_identity(
        database,
        item_id="clone-target",
        text=text,
        identity_version="fixture-v2",
        updated_ns=120,
    )
    stage_text_chunks(
        database,
        (chunk,),
        refresh_token="clone-target-v2",
        updated_ns=121,
    )
    finalize_text_chunk_refresh(
        database,
        item_id="clone-target",
        chunking_signature=chunk.chunking_signature,
        refresh_token="clone-target-v2",
        updated_ns=122,
    )
    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="clone-target-successor-v1",
        provenance={"fixture": "clone-target-successor-v1"},
        materialize_base=False,
        started_ns=130,
    )
    assert enqueue_text_chunk_jobs(database, candidate, (chunk.chunk_id,), now_ns=131) == 1
    assert (
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )
        is None
    )

    with semantic_database(database, readonly=True) as connection:
        generation = connection.execute(
            """SELECT base_clone_complete,cursor_json FROM embedding_generations
            WHERE generation_id=?""",
            (candidate,),
        ).fetchone()
        member_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (candidate,),
            ).fetchone()[0]
        )
        clone_receipts = int(
            connection.execute(
                """SELECT COUNT(*) FROM semantic_work_receipts
                WHERE generation_id=? AND stage_id='semantic.embedding.clone'""",
                (candidate,),
            ).fetchone()[0]
        )
    assert generation is not None
    assert int(generation["base_clone_complete"]) == 1
    assert json.loads(str(generation["cursor_json"]))["base_clone"]["scanned_members"] == 1
    assert member_count == 0
    assert clone_receipts == 0

    assert reuse_cached_jobs(database, candidate, now_ns=140) == 1
    finalize_embedding_generation(database, candidate, completed_ns=150)
    with semantic_database(database, readonly=True) as connection:
        member = connection.execute(
            """SELECT member_id,base_member_id FROM embedding_generation_members
            WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?""",
            (candidate, chunk.chunk_id),
        ).fetchone()
        assert member is not None
        materialization_id = f"materialization:semantic:embedding-member:{int(member['member_id'])}"
        producers = connection.execute(
            """SELECT DISTINCT receipt.stage_id,receipt.execution_mode
            FROM semantic_work_receipts receipt,
                 json_each(receipt.receipt_json,'$.outputs') output
            WHERE json_extract(
                output.value,'$.materialization.materialization_id'
            )=? ORDER BY receipt.receipt_id""",
            (materialization_id,),
        ).fetchall()
    assert member["base_member_id"] is None
    assert tuple(map(tuple, producers)) == (("semantic.embedding", "cache_hit"),)


def test_historical_overwritten_clone_uses_exact_physical_producer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    text = "protección de transformador con productor histórico"
    model = _initialize(database)
    chunk = _stage(database, "historical-clone", text, 1)
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="historical-clone-base-v1",
        provenance={"fixture": "historical-clone-base-v1"},
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, baseline, (chunk.chunk_id,), now_ns=101) == 1
    _complete_jobs(database, baseline, now_ns=102)
    finalize_embedding_generation(database, baseline, completed_ns=110)

    historical = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="historical-clone-corrupt-v1",
        provenance={"fixture": "historical-clone-corrupt-v1"},
        materialize_base=False,
        started_ns=120,
    )
    semantic_generation_repository._clone_published_members(database, historical)
    _replace_item_identity(
        database,
        item_id="historical-clone",
        text=text,
        identity_version="fixture-v2",
        updated_ns=130,
    )
    stage_text_chunks(
        database,
        (chunk,),
        refresh_token="historical-clone-v2",
        updated_ns=131,
    )
    finalize_text_chunk_refresh(
        database,
        item_id="historical-clone",
        chunking_signature=chunk.chunking_signature,
        refresh_token="historical-clone-v2",
        updated_ns=132,
    )
    assert enqueue_text_chunk_jobs(database, historical, (chunk.chunk_id,), now_ns=133) == 1
    assert reuse_cached_jobs(database, historical, now_ns=140) == 1

    with semantic_database(database, readonly=True) as connection:
        member = connection.execute(
            """SELECT member_id FROM embedding_generation_members
            WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?""",
            (historical, chunk.chunk_id),
        ).fetchone()
        assert member is not None
        member_id = int(member["member_id"])
        materialization_id = f"materialization:semantic:embedding-member:{member_id}"
        all_producers = connection.execute(
            """SELECT DISTINCT receipt.receipt_id,receipt.stage_id
            FROM semantic_work_receipts receipt,
                 json_each(receipt.receipt_json,'$.outputs') output
            WHERE json_extract(
                output.value,'$.materialization.materialization_id'
            )=? ORDER BY receipt.receipt_id""",
            (materialization_id,),
        ).fetchall()
        exact_producer = semantic_lineage_repository._producer_receipts_for_embedding_members(
            connection,
            (member_id,),
        )[member_id]
    assert tuple(str(row["stage_id"]) for row in all_producers) == (
        "semantic.embedding.clone",
        "semantic.embedding",
    )
    assert exact_producer == int(all_producers[1]["receipt_id"])

    finalize_embedding_generation(database, historical, completed_ns=150)
    _replace_item_identity(
        database,
        item_id="historical-clone",
        text=text,
        identity_version="fixture-v2",
        metadata_revision="fixture-v3",
        updated_ns=155,
    )
    stage_text_chunks(
        database,
        (chunk,),
        refresh_token="historical-clone-v3",
        updated_ns=156,
    )
    finalize_text_chunk_refresh(
        database,
        item_id="historical-clone",
        chunking_signature=chunk.chunking_signature,
        refresh_token="historical-clone-v3",
        updated_ns=157,
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="historical-clone-successor-v1",
        provenance={"fixture": "historical-clone-successor-v1"},
        materialize_base=False,
        started_ns=160,
    )
    assert enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=161) == 0
    prepare_embedding_generation(
        database,
        successor,
        enumeration_complete=True,
    )
    finalize_embedding_generation(database, successor, completed_ns=170)

    with semantic_database(database, readonly=True) as connection:
        selected_producer_key = str(
            connection.execute(
                "SELECT receipt_key FROM semantic_work_receipts WHERE receipt_id=?",
                (exact_producer,),
            ).fetchone()[0]
        )
        successor_member = connection.execute(
            """SELECT member_id,base_member_id FROM embedding_generation_members
            WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?""",
            (successor, chunk.chunk_id),
        ).fetchone()
        successor_receipt = connection.execute(
            """SELECT execution_mode,receipt_json FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
            ORDER BY receipt_id DESC LIMIT 1""",
            (successor,),
        ).fetchone()
    assert successor_member is not None
    assert successor_member["base_member_id"] is None
    assert successor_receipt is not None
    assert str(successor_receipt["execution_mode"]) == "replay"
    assert json.loads(str(successor_receipt["receipt_json"]))["causation_id"] == (
        selected_producer_key
    )


def test_new_processing_signature_closes_empty_uncloned_candidate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model, baseline = _published_fixture_with_members(database, extra_members=0)
    stale = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="sources=pdf,archive",
        provenance={"sources": ["pdf", "archive"]},
        cursor={"selected_sources": ["pdf", "archive"]},
        materialize_base=False,
        started_ns=120,
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="sources=pdf",
        provenance={"sources": ["pdf"]},
        cursor={"selected_sources": ["pdf"]},
        materialize_base=False,
        started_ns=130,
    )

    with semantic_database(database, readonly=True) as connection:
        stale_row = connection.execute(
            "SELECT status,completed_ns,cursor_json FROM embedding_generations "
            "WHERE generation_id=?",
            (stale,),
        ).fetchone()
        successor_row = connection.execute(
            "SELECT status,base_generation_id FROM embedding_generations WHERE generation_id=?",
            (successor,),
        ).fetchone()
    assert stale_row is not None
    assert str(stale_row["status"]) == "failed"
    assert int(stale_row["completed_ns"]) == 130
    assert json.loads(str(stale_row["cursor_json"])) == {
        "failure_reason": "processing_signature_superseded_before_work",
        "retryable": False,
        "selected_sources": ["pdf", "archive"],
        "superseded_by_processing_signature": "sources=pdf",
    }
    assert successor_row is not None
    assert str(successor_row["status"]) == "building"
    assert int(successor_row["base_generation_id"]) == baseline


def test_base_clone_deadline_persists_cursor_and_replays_to_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model, baseline = _published_fixture_with_members(database, extra_members=4)
    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="clone-successor-v1",
        provenance={"fixture": "clone-successor-v1"},
        materialize_base=False,
        started_ns=120,
    )
    monkeypatch.setattr(semantic_generation_repository, "MAX_WRITE_BATCH", 2)
    ticks = iter((0.0, 2.0))
    budget = SemanticWorkBudget(deadline=1.0, _clock=lambda: next(ticks))

    with pytest.raises(SemanticIndexDeadlineExceeded):
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
            work_budget=budget,
        )

    with semantic_database(database, readonly=True) as connection:
        paused = connection.execute(
            """SELECT base_clone_complete,cursor_json FROM embedding_generations
            WHERE generation_id=?""",
            (candidate,),
        ).fetchone()
        paused_members = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (candidate,),
            ).fetchone()[0]
        )
    assert paused is not None
    assert int(paused["base_clone_complete"]) == 0
    assert paused_members == 2
    paused_cursor = json.loads(str(paused["cursor_json"]))["base_clone"]
    assert paused_cursor["protocol"] == "base-member-snapshot-v1"
    assert paused_cursor["base_generation_id"] == baseline
    assert paused_cursor["scanned_members"] == 2
    assert paused_cursor["complete"] is False
    assert budget.truncation_reason == "time_budget"

    assert (
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )
        is None
    )

    with semantic_database(database, readonly=True) as connection:
        resumed = connection.execute(
            """SELECT base_clone_complete,cursor_json FROM embedding_generations
            WHERE generation_id=?""",
            (candidate,),
        ).fetchone()
        resumed_members = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (candidate,),
            ).fetchone()[0]
        )
        clone_receipts = connection.execute(
            """SELECT receipt_json FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding.clone'
            ORDER BY receipt_id""",
            (candidate,),
        ).fetchall()
        clone_events = int(
            connection.execute(
                """SELECT COUNT(*) FROM semantic_derivation_outbox outbox
                JOIN semantic_work_receipts receipt
                  ON receipt.receipt_id=outbox.receipt_id
                WHERE receipt.generation_id=?
                  AND receipt.stage_id='semantic.embedding.clone'""",
                (candidate,),
            ).fetchone()[0]
        )
    assert resumed is not None
    assert int(resumed["base_clone_complete"]) == 1
    assert resumed_members == 5
    assert len(clone_receipts) == 3
    assert clone_events == 3
    assert sum(len(json.loads(str(row[0]))["outputs"]) for row in clone_receipts) == 5
    resumed_cursor = json.loads(str(resumed["cursor_json"]))["base_clone"]
    assert resumed_cursor["scanned_members"] == 5
    assert resumed_cursor["base_member_count"] == 5
    assert resumed_cursor["complete"] is True


def test_base_clone_page_rolls_back_before_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model, baseline = _published_fixture_with_members(database, extra_members=2)
    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="clone-rollback-v1",
        provenance={"fixture": "clone-rollback-v1"},
        materialize_base=False,
        started_ns=120,
    )
    insert_page = semantic_generation_repository._insert_base_clone_rows

    def fail_after_insert(
        connection: sqlite3.Connection,
        generation_id: int,
        rows: Sequence[sqlite3.Row],
    ) -> None:
        insert_page(connection, generation_id, rows)
        raise RuntimeError("injected base clone failure")

    monkeypatch.setattr(
        semantic_generation_repository,
        "_insert_base_clone_rows",
        fail_after_insert,
    )
    with pytest.raises(RuntimeError, match="injected base clone failure"):
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )

    with semantic_database(database, readonly=True) as connection:
        candidate_row = connection.execute(
            """SELECT base_clone_complete,cursor_json
            FROM embedding_generations WHERE generation_id=?""",
            (candidate,),
        ).fetchone()
        candidate_members = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (candidate,),
            ).fetchone()[0]
        )
    assert candidate_row is not None
    assert int(candidate_row["base_clone_complete"]) == 0
    assert json.loads(str(candidate_row["cursor_json"])) == {}
    assert candidate_members == 0

    monkeypatch.setattr(
        semantic_generation_repository,
        "_insert_base_clone_rows",
        insert_page,
    )
    assert (
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )
        is None
    )
    with semantic_database(database, readonly=True) as connection:
        completed = connection.execute(
            """SELECT base_clone_complete FROM embedding_generations
            WHERE generation_id=?""",
            (candidate,),
        ).fetchone()
        member_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (candidate,),
            ).fetchone()[0]
        )
    assert completed is not None
    assert int(completed["base_clone_complete"]) == 1
    assert member_count == 3
    assert baseline > 0


def test_base_clone_uses_bounded_page_receipts_below_contract_size(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model, _baseline = _published_fixture_with_members(database, extra_members=499)
    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="clone-bounded-receipts-v1",
        provenance={"fixture": "clone-bounded-receipts-v1"},
        materialize_base=False,
        started_ns=120,
    )

    assert (
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )
        is None
    )

    with semantic_database(database, readonly=True) as connection:
        receipts = connection.execute(
            """SELECT receipt_json FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding.clone'
            ORDER BY receipt_id""",
            (candidate,),
        ).fetchall()
        event_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM semantic_derivation_outbox outbox
                JOIN semantic_work_receipts receipt
                  ON receipt.receipt_id=outbox.receipt_id
                WHERE receipt.generation_id=?
                  AND receipt.stage_id='semantic.embedding.clone'""",
                (candidate,),
            ).fetchone()[0]
        )
    payloads = tuple(str(row[0]) for row in receipts)
    assert len(payloads) == 2
    assert event_count == 2
    assert sum(len(json.loads(payload)["outputs"]) for payload in payloads) == 500
    assert max(len(payload.encode("utf-8")) for payload in payloads) < 1_000_000


def test_base_clone_rejects_a_changed_pinned_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model, baseline = _published_fixture_with_members(database, extra_members=2)
    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="clone-pinned-v1",
        provenance={"fixture": "clone-pinned-v1"},
        materialize_base=False,
        started_ns=120,
    )
    monkeypatch.setattr(semantic_generation_repository, "MAX_WRITE_BATCH", 1)
    ticks = iter((0.0, 2.0))
    budget = SemanticWorkBudget(deadline=1.0, _clock=lambda: next(ticks))
    with pytest.raises(SemanticIndexDeadlineExceeded):
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
            work_budget=budget,
        )

    _duplicate_generation_member_rows(database, baseline, count=1)

    with pytest.raises(
        SemanticStateError,
        match="base snapshot changed during resumable clone",
    ):
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )


def test_lazy_exact_replay_returns_published_head_without_member_clone(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    chunk = _stage(database, "document", "published transformer record", 1)
    provenance = {"fixture": "stable-replay"}
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="stable-replay",
        provenance=provenance,
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, baseline, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, baseline, now_ns=102)
    finalize_embedding_generation(database, baseline, completed_ns=110)

    candidate = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="stable-replay",
        provenance=provenance,
        materialize_base=False,
        started_ns=120,
    )
    assert candidate != baseline
    with semantic_database(database, readonly=True) as connection:
        candidate_row = connection.execute(
            "SELECT base_generation_id,base_clone_complete "
            "FROM embedding_generations WHERE generation_id=?",
            (candidate,),
        ).fetchone()
        candidate_members = int(
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (candidate,),
            ).fetchone()[0]
        )
    assert candidate_row is not None
    assert int(candidate_row["base_generation_id"]) == baseline
    assert int(candidate_row["base_clone_complete"]) == 0
    assert candidate_members == 0
    assert (
        enqueue_text_chunk_jobs(
            database,
            candidate,
            (chunk.chunk_id,),
            now_ns=121,
        )
        == 0
    )

    no_op = prepare_embedding_generation(
        database,
        candidate,
        enumeration_complete=True,
    )

    assert no_op is not None
    assert no_op.generation_id == baseline
    assert no_op.status == "ready"
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM embedding_generations WHERE generation_id=?",
                (candidate,),
            ).fetchone()
            is None
        )
        assert (
            int(
                connection.execute(
                    "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                    (model.model_signature,),
                ).fetchone()[0]
            )
            == baseline
        )
        assert (
            int(
                connection.execute("SELECT COUNT(*) FROM embedding_generation_members").fetchone()[
                    0
                ]
            )
            == 1
        )


def test_done_job_metadata_restage_rebinds_current_item_revision_without_inference(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    text = "published transformer record"
    chunk = _stage(database, "metadata-document", text, 1)
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="metadata-rebind",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, generation, now_ns=102)
    with semantic_database(database, readonly=True) as connection:
        baseline_member = connection.execute(
            "SELECT member_id,item_revision_id,chunk_revision_id,payload_id "
            "FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?",
            (generation, chunk.chunk_id),
        ).fetchone()
        baseline_payloads = int(
            connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]
        )
    assert baseline_member is not None
    before_rebind = semantic_lineage_repository.explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        published_only=False,
    )
    assert tuple(item.execution_mode for item in before_rebind.embeddings) == ("executed",)

    upsert_semantic_item(
        database,
        SemanticItem(
            "metadata-document",
            "pdf",
            "identity:metadata-document",
            "fixture-v1",
            fingerprint_text(text),
            path="C:/fixtures/moved/metadata-document.pdf",
            provenance={"revision": 1},
        ),
        refresh_token="metadata-move",
        updated_ns=120,
    )
    assert enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=121) == 0
    first_rebind = semantic_lineage_repository.explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        published_only=False,
    )
    assert tuple(item.execution_mode for item in first_rebind.embeddings) == ("cache_hit",)

    resumed = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="metadata-rebind",
        started_ns=130,
    )
    assert resumed == generation
    upsert_semantic_item(
        database,
        SemanticItem(
            "metadata-document",
            "pdf",
            "identity:metadata-document",
            "fixture-v1",
            fingerprint_text(text),
            path="C:/fixtures/moved-again/metadata-document.pdf",
            provenance={"revision": 1},
        ),
        refresh_token="metadata-move-again",
        updated_ns=131,
    )
    assert enqueue_text_chunk_jobs(database, resumed, (chunk.chunk_id,), now_ns=132) == 0
    second_rebind = semantic_lineage_repository.explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        published_only=False,
    )
    assert tuple(item.execution_mode for item in second_rebind.embeddings) == ("cache_hit",)
    assert second_rebind.embeddings[0].receipt_id != first_rebind.embeddings[0].receipt_id

    summary = finalize_embedding_generation(database, generation, completed_ns=140)

    assert summary.status == "ready"
    published = semantic_lineage_repository.explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
    )
    assert tuple(item.execution_mode for item in published.embeddings) == ("cache_hit",)
    assert published.embeddings[0].receipt_id == second_rebind.embeddings[0].receipt_id
    with semantic_database(database, readonly=True) as connection:
        rebound_member = connection.execute(
            "SELECT member_id,item_revision_id,chunk_revision_id,payload_id "
            "FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?",
            (generation, chunk.chunk_id),
        ).fetchone()
        assert rebound_member is not None
        rebound_revision = connection.execute(
            "SELECT path FROM semantic_item_revisions WHERE item_revision_id=?",
            (int(rebound_member["item_revision_id"]),),
        ).fetchone()
        payloads = int(connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0])
        published_head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()[0]
        )
        embedding_receipts = connection.execute(
            """SELECT receipt_key,execution_mode,item_revision_id,receipt_json
            FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
            ORDER BY receipt_id""",
            (generation,),
        ).fetchall()
    assert int(rebound_member["item_revision_id"]) != int(baseline_member["item_revision_id"])
    assert int(rebound_member["chunk_revision_id"]) == int(baseline_member["chunk_revision_id"])
    assert int(rebound_member["payload_id"]) == int(baseline_member["payload_id"])
    assert rebound_revision is not None
    assert str(rebound_revision["path"]) == "C:/fixtures/moved-again/metadata-document.pdf"
    assert payloads == baseline_payloads
    assert published_head == generation
    assert [str(row["execution_mode"]) for row in embedding_receipts] == [
        "executed",
        "cache_hit",
        "cache_hit",
    ]
    assert len({str(row["receipt_key"]) for row in embedding_receipts}) == 3
    assert len({int(row["item_revision_id"]) for row in embedding_receipts}) == 3
    producer_key = str(embedding_receipts[0]["receipt_key"])
    assert {
        str(json.loads(str(row["receipt_json"]))["causation_id"]) for row in embedding_receipts[1:]
    } == {producer_key}
    assert int(rebound_member["member_id"]) not in {
        int(baseline_member["member_id"]),
    }


def test_done_replaced_title_job_is_reconciled_and_successor_can_publish(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    item_id = "title-document"
    content_fingerprint = fingerprint_text("stable document body")
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )

    upsert_semantic_item(
        database,
        SemanticItem(
            item_id,
            "pdf",
            f"identity:{item_id}",
            "fixture-v1",
            content_fingerprint,
            path="C:/fixtures/original-title.pdf",
            provenance={"revision": 1},
        ),
        refresh_token="title-item-original",
        updated_ns=10,
    )
    original_title = chunk_text_sections(
        item_id,
        (
            TextSection(
                "semantic_metadata_title",
                "basename",
                "original-title",
                {"policy": "fixture-title-v1"},
            ),
        ),
        config,
    )[0]
    stage_text_chunks(
        database,
        (original_title,),
        refresh_token="title-original",
        updated_ns=11,
    )
    finalize_text_chunk_refresh(
        database,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token="title-original",
        updated_ns=12,
    )
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="title-replacement",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (original_title.chunk_id,),
        now_ns=101,
    )
    _complete_jobs(database, generation, now_ns=102)

    upsert_semantic_item(
        database,
        SemanticItem(
            item_id,
            "pdf",
            f"identity:{item_id}",
            "fixture-v1",
            content_fingerprint,
            path="C:/fixtures/renamed-title.pdf",
            provenance={"revision": 1},
        ),
        refresh_token="title-item-renamed",
        updated_ns=120,
    )
    renamed_title = chunk_text_sections(
        item_id,
        (
            TextSection(
                "semantic_metadata_title",
                "basename",
                "renamed-title",
                {"policy": "fixture-title-v1"},
            ),
        ),
        config,
    )[0]
    assert renamed_title.chunk_id != original_title.chunk_id
    stage_text_chunks(
        database,
        (renamed_title,),
        refresh_token="title-renamed",
        updated_ns=121,
    )
    finalize_text_chunk_refresh(
        database,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token="title-renamed",
        updated_ns=122,
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (renamed_title.chunk_id,),
        now_ns=123,
    )
    _complete_jobs(database, generation, now_ns=124)

    summary = finalize_embedding_generation(database, generation, completed_ns=130)

    assert summary.status == "ready"
    with semantic_database(database, readonly=True) as connection:
        members = tuple(
            str(row["entity_id"])
            for row in connection.execute(
                "SELECT entity_id FROM embedding_generation_members "
                "WHERE generation_id=? AND entity_kind='text_chunk' "
                "ORDER BY entity_id",
                (generation,),
            )
        )
        published_head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()[0]
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    assert members == (renamed_title.chunk_id,)
    assert published_head == generation
    assert tuple(hit.entity_id for hit in search_exact_page(database, _query(model)).hits) == (
        renamed_title.chunk_id,
    )


def _create_populated_v5(path: Path) -> tuple[EmbeddingModelSpec, TextChunk]:
    """Build a genuine populated v5 fixture from its sequential migrations."""

    model = _model()
    text = "legacy published transformer record"
    fingerprint = fingerprint_text(text)
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunk = chunk_text_sections(
        "legacy-document",
        (TextSection("pdf_page", "1", text),),
        config,
    )[0]
    vector_blob, norm = encode_vector(
        (1.0, 0.0, 0.0, 0.0),
        model.dimensions,
        model.vector_dtype,
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for version in range(1, 6):
            getattr(semantic_schema, f"_migrate_to_v{version}")(
                connection,
                version,
            )
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','5')")
        connection.execute("PRAGMA user_version=5")
        connection.execute(
            """INSERT INTO vector_spaces(
                vector_space,dimensions,distance,normalization,created_ns)
            VALUES(?,?,'cosine','l2',1)""",
            (model.vector_space, model.dimensions),
        )
        connection.execute(
            """INSERT INTO embedding_models(
                model_signature,vector_space,modality,model_id,model_version,
                dimensions,provider,supported_roles_json,vector_dtype,
                normalization,distance,provenance_json,active,created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,'l2','cosine','{}',1,1)""",
            (
                model.model_signature,
                model.vector_space,
                model.modality.value,
                model.model_id,
                model.model_version,
                model.dimensions,
                model.provider,
                json.dumps(
                    [role.value for role in model.supported_roles],
                    separators=(",", ":"),
                ),
                model.vector_dtype.value,
            ),
        )
        connection.execute(
            """INSERT INTO semantic_items(
                item_id,source_kind,source_identity,identity_version,path,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,refresh_token,active,updated_ns,
                source_revision_json)
            VALUES('legacy-document','pdf','legacy-identity','fixture-v1',?,
                ?,?,?,'{}','legacy-refresh',1,2,'{}')""",
            (
                "C:/fixtures/legacy-document.pdf",
                fingerprint.xxh3_128,
                fingerprint.byte_count,
                fingerprint.xxh3_64_guard,
            ),
        )
        connection.execute(
            """INSERT INTO text_chunks(
                chunk_id,item_id,ordinal,section_kind,section_id,start_char,
                end_char,text_zlib,text_chars,content_xxh3_128,content_bytes,
                content_xxh3_64_guard,chunking_signature,provenance_json,
                refresh_token,active,updated_ns)
            VALUES(?,'legacy-document',?,?,?,?,?,?,?,?,?,?,?,'{}',
                'legacy-refresh',1,3)""",
            (
                chunk.chunk_id,
                chunk.ordinal,
                chunk.section_kind,
                chunk.section_id,
                chunk.start_char,
                chunk.end_char,
                _encode_chunk_text(chunk),
                len(chunk.text),
                chunk.fingerprint.xxh3_128,
                chunk.fingerprint.byte_count,
                chunk.fingerprint.xxh3_64_guard,
                chunk.chunking_signature,
            ),
        )
        generation_cursor = connection.execute(
            """INSERT INTO embedding_generations(
                model_signature,processing_signature,status,provenance_json,
                cursor_json,started_ns,completed_ns,done_count)
            VALUES(?,'legacy-run','ready','{}','{}',4,5,1)""",
            (model.model_signature,),
        )
        payload_cursor = connection.execute(
            """INSERT INTO vector_payloads(
                model_signature,content_xxh3_128,content_bytes,
                content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
                original_norm,provenance_json,created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,6)""",
            (
                model.model_signature,
                chunk.fingerprint.xxh3_128,
                chunk.fingerprint.byte_count,
                chunk.fingerprint.xxh3_64_guard,
                model.dimensions,
                model.vector_dtype.value,
                vector_blob,
                norm,
                '{"fixture":"legacy-v5"}',
            ),
        )
        generation_id = generation_cursor.lastrowid
        payload_id = payload_cursor.lastrowid
        assert generation_id is not None
        assert payload_id is not None
        connection.execute(
            """INSERT INTO text_embeddings(
                chunk_id,model_signature,payload_id,generation_id,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,updated_ns)
            VALUES(?,?,?,?,?,?,?,'{"fixture":"legacy-v5"}',7)""",
            (
                chunk.chunk_id,
                model.model_signature,
                payload_id,
                generation_id,
                chunk.fingerprint.xxh3_128,
                chunk.fingerprint.byte_count,
                chunk.fingerprint.xxh3_64_guard,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return model, chunk


def _create_receiptless_current_base(
    path: Path,
) -> tuple[EmbeddingModelSpec, TextChunk, int]:
    model = _initialize(path)
    chunk = _stage(path, "receiptless-current", "current receiptless record", 1)
    generation_id = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature="receiptless-current-base-v1",
        materialize_base=False,
        started_ns=100,
    )
    vector_blob, norm = encode_vector(
        (1.0, 0.0, 0.0, 0.0),
        model.dimensions,
        model.vector_dtype,
    )
    with semantic_database(path) as connection:
        item_revision_id = int(
            connection.execute(
                """SELECT item_revision_id FROM semantic_item_revisions
                WHERE item_id=? ORDER BY item_revision_id DESC LIMIT 1""",
                (chunk.item_id,),
            ).fetchone()[0]
        )
        chunk_revision_id = int(
            connection.execute(
                "SELECT chunk_revision_id FROM semantic_chunk_revisions WHERE chunk_id=?",
                (chunk.chunk_id,),
            ).fetchone()[0]
        )
        payload_id = int(
            connection.execute(
                """INSERT INTO vector_payloads(
                    model_signature,content_xxh3_128,content_bytes,
                    content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
                    original_norm,provenance_json,created_ns)
                VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING payload_id""",
                (
                    model.model_signature,
                    chunk.fingerprint.xxh3_128,
                    chunk.fingerprint.byte_count,
                    chunk.fingerprint.xxh3_64_guard,
                    model.dimensions,
                    model.vector_dtype.value,
                    vector_blob,
                    norm,
                    '{"fixture":"receiptless-current"}',
                    101,
                ),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO embedding_generation_members(
                generation_id,model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
                content_bytes,content_xxh3_64_guard,provenance_json,updated_ns,
                base_member_id)
            VALUES(?,?,'text_chunk',?,?,?,?,?,?,?,?,?,102,NULL)""",
            (
                generation_id,
                model.model_signature,
                chunk.chunk_id,
                chunk.item_id,
                item_revision_id,
                chunk_revision_id,
                payload_id,
                chunk.fingerprint.xxh3_128,
                chunk.fingerprint.byte_count,
                chunk.fingerprint.xxh3_64_guard,
                '{"fixture":"receiptless-current"}',
            ),
        )
        connection.execute(
            """UPDATE embedding_generations
            SET status='ready',completed_ns=103,done_count=1,base_clone_complete=1
            WHERE generation_id=?""",
            (generation_id,),
        )
        connection.execute(
            """INSERT INTO published_embedding_heads(
                model_signature,generation_id,published_ns) VALUES(?,?,103)""",
            (model.model_signature, generation_id),
        )
    return model, chunk, generation_id


def test_unchanged_receiptless_legacy_base_member_replays_without_rebind(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-base.sqlite3"
    model, chunk = _create_populated_v5(database)
    initialize_semantic_state(database)
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="legacy-base-successor-v1",
        materialize_base=False,
        started_ns=100,
    )

    assert enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=101) == 0

    assert (
        prepare_embedding_generation(
            database,
            successor,
            enumeration_complete=True,
        )
        is None
    )
    summary = finalize_embedding_generation(database, successor, completed_ns=110)

    assert summary.status == "ready"

    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM semantic_work_receipts
                WHERE generation_id=? AND stage_id='semantic.embedding.clone'""",
                (successor,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (successor,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM embedding_jobs
                WHERE generation_id=?""",
                (successor,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT status FROM embedding_generations WHERE generation_id=?",
                (successor,),
            ).fetchone()[0]
            == "ready"
        )


def test_receiptless_legacy_base_member_rebinds_through_exact_attestation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-rebind.sqlite3"
    model, chunk = _create_populated_v5(database)
    initialize_semantic_state(database)
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="legacy-rebind-successor-v1",
        materialize_base=False,
        started_ns=100,
    )
    with semantic_database(database) as connection:
        source_revision = json.loads(
            str(
                connection.execute(
                    "SELECT source_revision_json FROM semantic_items WHERE item_id=?",
                    ("legacy-document",),
                ).fetchone()[0]
            )
        )
        source_revision["last_seen_run_id"] = 61
        connection.execute(
            "UPDATE semantic_items SET source_revision_json=?,updated_ns=? WHERE item_id=?",
            (
                json.dumps(source_revision, separators=(",", ":"), sort_keys=True),
                101,
                "legacy-document",
            ),
        )

    assert enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=102) == 0

    prepare_embedding_generation(
        database,
        successor,
        enumeration_complete=True,
    )
    summary = finalize_embedding_generation(database, successor, completed_ns=110)
    assert summary.status == "ready"

    with semantic_database(database, readonly=True) as connection:
        rows = connection.execute(
            """SELECT stage_id,execution_mode,receipt_key,receipt_json
            FROM semantic_work_receipts
            WHERE stage_id IN (
                'semantic.vector_payload.legacy_attest','semantic.embedding'
            ) ORDER BY receipt_id"""
        ).fetchall()
        member = connection.execute(
            """SELECT base_member_id,item_revision_id,payload_id
            FROM embedding_generation_members
            WHERE generation_id=? AND entity_id=?""",
            (successor, chunk.chunk_id),
        ).fetchone()
    assert [tuple(row[key] for key in ("stage_id", "execution_mode")) for row in rows] == [
        ("semantic.vector_payload.legacy_attest", "executed"),
        ("semantic.embedding", "cache_hit"),
    ]
    assert json.loads(str(rows[1]["receipt_json"]))["causation_id"] == str(rows[0]["receipt_key"])
    assert member is not None
    assert member["base_member_id"] is None
    assert int(member["item_revision_id"]) > 1
    assert int(member["payload_id"]) == 1


def test_receiptless_nonlegacy_base_member_rebind_stays_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nonlegacy-rebind.sqlite3"
    model, chunk, _base = _create_receiptless_current_base(database)
    with semantic_database(database, readonly=True) as connection:
        baseline_receipt_count = int(
            connection.execute("SELECT COUNT(*) FROM semantic_work_receipts").fetchone()[0]
        )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="nonlegacy-rebind-successor-v1",
        materialize_base=False,
        started_ns=100,
    )
    upsert_semantic_item(
        database,
        SemanticItem(
            chunk.item_id,
            "pdf",
            f"identity:{chunk.item_id}",
            "fixture-v1",
            chunk.fingerprint,
            path=f"C:/fixtures/moved/{chunk.item_id}.pdf",
            provenance={"fixture": "metadata-move"},
        ),
        refresh_token="nonlegacy-metadata-move",
        updated_ns=101,
    )

    with pytest.raises(
        SemanticStateError,
        match="semantic source embedding member has no exact producer receipt",
    ):
        enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=102)

    with semantic_database(database, readonly=True) as connection:
        receipt_count = int(
            connection.execute("SELECT COUNT(*) FROM semantic_work_receipts").fetchone()[0]
        )
        target_member_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (successor,),
            ).fetchone()[0]
        )
    assert receipt_count == baseline_receipt_count
    assert target_member_count == 0


def test_receiptless_legacy_rebind_rejects_mismatched_candidate_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-mismatched-receipt.sqlite3"
    model, chunk = _create_populated_v5(database)
    initialize_semantic_state(database)
    with semantic_database(database) as connection:
        member_id = int(
            connection.execute("SELECT member_id FROM embedding_generation_members").fetchone()[0]
        )
        connection.execute(
            """INSERT INTO semantic_work_receipts(
                receipt_key,contract_version,stage_id,stage_version,
                processing_signature,status,execution_mode,
                reproducibility_class,entity_kind,entity_id,receipt_json,
                committed_ns)
            VALUES(?,'neocortex.work-receipt/v1','fixture.corrupt','fixture-v1',
                'fixture-v1','succeeded','unknown','non_replayable',
                'fixture',?, ?,100)""",
            (
                f"fixture-mismatched:{member_id}",
                str(member_id),
                json.dumps(
                    {
                        "outputs": [
                            {
                                "materialization": {
                                    "materialization_id": (
                                        f"materialization:semantic:embedding-member:{member_id}"
                                    )
                                },
                                "fingerprint": "not-the-physical-fingerprint",
                                "fingerprint_algorithm": (
                                    "semantic-embedding-member-contract-xxh3-128-v1"
                                ),
                            }
                        ]
                    },
                    separators=(",", ":"),
                ),
            ),
        )
        connection.execute(
            """UPDATE semantic_items SET source_revision_json=?,updated_ns=?
            WHERE item_id=?""",
            ('{"last_seen_run_id":61}', 101, chunk.item_id),
        )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="legacy-mismatch-successor-v1",
        materialize_base=False,
        started_ns=102,
    )

    with pytest.raises(
        SemanticStateError,
        match="semantic source embedding member has no exact producer receipt",
    ):
        enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=103)

    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM semantic_work_receipts
                WHERE stage_id IN (
                    'semantic.vector_payload.legacy_attest','semantic.embedding'
                )"""
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (successor,),
            ).fetchone()[0]
            == 0
        )


def test_rebind_rejects_multiple_exact_physical_producers(tmp_path: Path) -> None:
    database = tmp_path / "multiple-exact-producers.sqlite3"
    model = _initialize(database)
    chunk = _stage(database, "multiple-producers", "multiple producer record", 1)
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="multiple-producers-base-v1",
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, baseline, (chunk.chunk_id,), now_ns=101) == 1
    _complete_jobs(database, baseline, now_ns=102)
    finalize_embedding_generation(database, baseline, completed_ns=110)
    with semantic_database(database) as connection:
        producer = connection.execute(
            """SELECT * FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
              AND execution_mode='executed'""",
            (baseline,),
        ).fetchone()
        assert producer is not None
        connection.execute(
            """INSERT INTO semantic_work_receipts(
                receipt_key,contract_version,stage_id,stage_version,
                processing_signature,status,execution_mode,
                reproducibility_class,entity_kind,entity_id,item_revision_id,
                chunk_revision_id,generation_id,model_signature,payload_id,
                job_id,attempt,started_ns,finished_ns,duration_ns,receipt_json,
                committed_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{producer['receipt_key']}:duplicate-fixture",
                str(producer["contract_version"]),
                str(producer["stage_id"]),
                str(producer["stage_version"]),
                str(producer["processing_signature"]),
                str(producer["status"]),
                str(producer["execution_mode"]),
                str(producer["reproducibility_class"]),
                str(producer["entity_kind"]),
                str(producer["entity_id"]),
                int(producer["item_revision_id"]),
                int(producer["chunk_revision_id"]),
                int(producer["generation_id"]),
                str(producer["model_signature"]),
                int(producer["payload_id"]),
                int(producer["job_id"]),
                int(producer["attempt"]),
                int(producer["started_ns"]),
                int(producer["finished_ns"]),
                int(producer["duration_ns"]),
                str(producer["receipt_json"]),
                111,
            ),
        )
        connection.execute(
            """UPDATE semantic_items SET source_revision_json=?,updated_ns=?
            WHERE item_id=?""",
            ('{"last_seen_run_id":61}', 120, chunk.item_id),
        )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="multiple-producers-successor-v1",
        materialize_base=False,
        started_ns=121,
    )

    with pytest.raises(
        SemanticStateError,
        match="semantic source embedding member has multiple exact producer receipts",
    ):
        enqueue_text_chunk_jobs(database, successor, (chunk.chunk_id,), now_ns=122)

    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (successor,),
            ).fetchone()[0]
            == 0
        )


def test_rebind_exact_producers_are_resolved_once_for_128_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "batched-rebind-producers.sqlite3"
    model, baseline = _completed_generation(
        database,
        member_count=128,
        processing_signature="batched-rebind-base-v1",
    )
    for offset in range(3):
        _complete_jobs(database, baseline, now_ns=200 + offset * 40)
    finalize_embedding_generation(database, baseline, completed_ns=400)
    with semantic_database(database) as connection:
        chunk_ids = tuple(
            str(row[0])
            for row in connection.execute(
                """SELECT entity_id FROM embedding_generation_members
                WHERE generation_id=? ORDER BY member_id""",
                (baseline,),
            )
        )
        connection.execute(
            """UPDATE semantic_items SET source_revision_json=?,updated_ns=?
            WHERE active=1""",
            ('{"last_seen_run_id":61}', 410),
        )
    assert len(chunk_ids) == 128
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="batched-rebind-successor-v1",
        materialize_base=False,
        started_ns=420,
    )
    original = semantic_generation_repository._producer_receipts_for_embedding_members
    calls: list[tuple[int, ...]] = []

    def traced_producers(
        connection: sqlite3.Connection,
        member_ids: Sequence[int],
    ) -> dict[int, int]:
        calls.append(tuple(member_ids))
        return original(connection, member_ids)

    monkeypatch.setattr(
        semantic_generation_repository,
        "_producer_receipts_for_embedding_members",
        traced_producers,
    )

    assert enqueue_text_chunk_jobs(database, successor, chunk_ids, now_ns=430) == 0
    assert len(calls) == 1
    assert len(calls[0]) == 128


def test_same_generation_payload_producers_are_resolved_once_for_128_rebinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "batched-same-generation-rebind.sqlite3"
    _model_spec, generation = _completed_generation(
        database,
        member_count=128,
        processing_signature="batched-same-generation-v1",
    )
    for offset in range(3):
        _complete_jobs(database, generation, now_ns=200 + offset * 40)
    with semantic_database(database) as connection:
        chunk_ids = tuple(
            str(row[0])
            for row in connection.execute(
                """SELECT entity_id FROM embedding_generation_members
                WHERE generation_id=? ORDER BY member_id""",
                (generation,),
            )
        )
        connection.execute(
            """UPDATE semantic_items SET source_revision_json=?,updated_ns=?
            WHERE active=1""",
            ('{"last_seen_run_id":61}', 410),
        )
    assert len(chunk_ids) == 128
    original = semantic_generation_repository._payload_causation_receipts
    calls: list[tuple[int, ...]] = []

    def traced_payload_producers(
        connection: sqlite3.Connection,
        payload_ids: Sequence[int],
        *,
        now_ns: int | None,
    ) -> dict[int, int]:
        calls.append(tuple(payload_ids))
        return original(connection, payload_ids, now_ns=now_ns)

    monkeypatch.setattr(
        semantic_generation_repository,
        "_payload_causation_receipts",
        traced_payload_producers,
    )

    assert enqueue_text_chunk_jobs(database, generation, chunk_ids, now_ns=420) == 0
    assert len(calls) == 1
    assert len(calls[0]) == 128


def test_building_rows_are_invisible_until_atomic_head_publication(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    chunks = (
        _stage(database, "document-a", "old transformer record", 1),
        _stage(database, "document-b", "old breaker record", 1),
    )
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="initial-build",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (chunk.chunk_id for chunk in chunks),
        now_ns=101,
    )
    leases = claim_embedding_jobs(
        database,
        generation,
        worker_id="publication-worker",
        limit=2,
        lease_seconds=60,
        now_ns=102,
    )
    complete_embedding_job(
        database,
        leases[0].job_id,
        worker_id="publication-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=103,
    )
    assert not has_active_embeddings(database, model.model_signature)
    assert search_exact_page(database, _query(model)).hits == ()

    complete_embedding_job(
        database,
        leases[1].job_id,
        worker_id="publication-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=104,
    )
    assert search_exact_page(database, _query(model)).hits == ()

    finalized = finalize_embedding_generation(database, generation, completed_ns=105)
    assert finalized.status == "ready"
    page = search_exact_page(database, _query(model), limit=10)
    assert {hit.item_id for hit in page.hits} == {"document-a", "document-b"}
    assert {hit.generation_id for hit in page.hits} == {generation}


def test_source_change_preserves_old_snapshot_until_successor_is_published(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    old_chunk = _stage(database, "document", "old transformer record", 1)
    first = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="first",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, first, (old_chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, first, now_ns=102)
    finalize_embedding_generation(database, first, completed_ns=110)
    old_hit = search_exact_page(database, _query(model)).hits[0]

    second = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="second",
        started_ns=120,
    )
    new_chunk = _stage(database, "document", "new breaker record", 2)
    enqueue_text_chunk_jobs(database, second, (new_chunk.chunk_id,), now_ns=121)
    _complete_jobs(database, second, now_ns=122)
    assert search_exact_page(database, _query(model)).hits == (old_hit,)
    old_resolved = resolve_search_hits(database, (old_hit,))[0]
    assert old_resolved.snippet == "old transformer record"
    assert old_resolved.published_revision_id is not None
    assert old_resolved.current_revision_id is not None
    assert old_resolved.published_revision_id != old_resolved.current_revision_id
    assert old_resolved.source_revision_is_current is False
    finalize_embedding_generation(database, second, completed_ns=130)

    current = search_exact_page(database, _query(model)).hits
    assert len(current) == 1
    assert current[0].generation_id == second
    assert current[0].entity_id == new_chunk.chunk_id
    current_resolved = resolve_search_hits(database, current)[0]
    assert current_resolved.snippet == "new breaker record"
    assert current_resolved.published_revision_id is not None
    assert current_resolved.published_revision_id == current_resolved.current_revision_id
    assert current_resolved.source_revision_is_current is True


def test_resolve_preserves_evidence_without_replacement_locator(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    text = "published transformer record"
    chunk = _stage(database, "document", text, 1)
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="published",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, generation, now_ns=102)
    finalize_embedding_generation(database, generation, completed_ns=110)
    hit = search_exact_page(database, _query(model)).hits[0]

    upsert_semantic_item(
        database,
        SemanticItem(
            "document",
            "pdf",
            "identity:replacement",
            "fixture-v1",
            fingerprint_text(text),
            path="C:/fixtures/replacement.pdf",
        ),
        refresh_token="replacement",
        updated_ns=120,
    )

    assert search_exact_page(database, _query(model)).hits == (hit,)
    resolved = resolve_search_hits(database, (hit,))[0]
    assert resolved.source_identity == "identity:document"
    assert resolved.path is None
    assert resolved.snippet == text
    assert resolved.published_revision_id is not None
    assert resolved.current_revision_id is None
    assert resolved.source_revision_is_current is False


def test_resolve_uses_current_path_without_marking_safe_move_stale(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    text = "published transformer record"
    chunk = _stage(database, "document", text, 1)
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="published",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, generation, now_ns=102)
    finalize_embedding_generation(database, generation, completed_ns=110)
    hit = search_exact_page(database, _query(model)).hits[0]
    with semantic_database(database, readonly=True) as connection:
        baseline_member = connection.execute(
            "SELECT payload_id,item_revision_id FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?",
            (generation, chunk.chunk_id),
        ).fetchone()
    assert baseline_member is not None

    upsert_semantic_item(
        database,
        SemanticItem(
            "document",
            "pdf",
            "identity:document",
            "fixture-v1",
            fingerprint_text(text),
            path="C:/fixtures/moved-document.pdf",
            provenance={
                "revision": 1,
                "source_status": "  ",
                "analysis_status": "complete",
            },
        ),
        refresh_token="safe-move",
        updated_ns=120,
    )
    moved_generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="published",
        materialize_base=False,
        started_ns=121,
    )
    with semantic_database(database, readonly=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                    (moved_generation,),
                ).fetchone()[0]
            )
            == 0
        )
        generation_row = connection.execute(
            "SELECT base_generation_id,base_clone_complete "
            "FROM embedding_generations WHERE generation_id=?",
            (moved_generation,),
        ).fetchone()
    assert generation_row is not None
    assert int(generation_row["base_generation_id"]) == generation
    assert int(generation_row["base_clone_complete"]) == 0

    assert (
        enqueue_text_chunk_jobs(
            database,
            moved_generation,
            (chunk.chunk_id,),
            now_ns=122,
        )
        == 0
    )
    with semantic_database(database, readonly=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?",
                    (moved_generation,),
                ).fetchone()[0]
            )
            == 0
        )

    assert (
        prepare_embedding_generation(
            database,
            moved_generation,
            enumeration_complete=True,
        )
        is None
    )
    finalize_embedding_generation(database, moved_generation, completed_ns=123)
    with semantic_database(database, readonly=True) as connection:
        moved_member = connection.execute(
            "SELECT payload_id,item_revision_id FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_kind='text_chunk' AND entity_id=?",
            (moved_generation, chunk.chunk_id),
        ).fetchone()
        assert moved_member is not None
        published_path = connection.execute(
            "SELECT path FROM semantic_item_revisions WHERE item_revision_id=?",
            (int(moved_member["item_revision_id"]),),
        ).fetchone()
        published_head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model.model_signature,),
            ).fetchone()[0]
        )
    assert int(moved_member["payload_id"]) == int(baseline_member["payload_id"])
    assert int(moved_member["item_revision_id"]) != int(baseline_member["item_revision_id"])
    assert published_path is not None
    assert str(published_path["path"]) == "C:/fixtures/moved-document.pdf"
    assert published_head == moved_generation

    resolved = resolve_search_hits(database, (hit,))[0]
    assert resolved.path == "C:/fixtures/moved-document.pdf"
    assert resolved.source_status == "complete"
    assert resolved.published_revision_id == resolved.current_revision_id
    assert resolved.source_revision_is_current is True

    moved_hit = search_exact_page(database, _query(model)).hits[0]
    moved_resolved = resolve_search_hits(database, (moved_hit,))[0]
    assert moved_resolved.path == "C:/fixtures/moved-document.pdf"
    assert moved_resolved.published_revision_id == moved_resolved.current_revision_id
    assert moved_resolved.source_revision_is_current is True


def test_exact_search_checks_cancellation_at_scan_batches(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    chunk = _stage(database, "document", "transformer record", 1)
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="published",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, generation, now_ns=102)
    finalize_embedding_generation(database, generation, completed_ns=110)
    checkpoints = 0

    class SearchCancelled(Exception):
        pass

    def cancellation_check() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise SearchCancelled

    with pytest.raises(SearchCancelled):
        search_exact_page(
            database,
            _query(model),
            cancellation_check=cancellation_check,
        )
    assert checkpoints == 2


def test_embedding_generation_preparation_public_contract(tmp_path: Path) -> None:
    assert str(inspect.signature(prepare_embedding_generation)) == (
        "(path: 'Path', generation_id: 'int', *, enumeration_complete: 'bool', "
        "work_budget: 'SemanticWorkBudget | None' = None) -> "
        "'GenerationSummary | None'"
    )
    assert (
        semantic_generation_repository.prepare_embedding_generation is prepare_embedding_generation
    )

    database = tmp_path / "semantic.sqlite3"
    model, baseline, candidate = _exact_replay_fixture(
        database,
        member_count=1,
        processing_signature="preparation-contract-v1",
    )
    summary = prepare_embedding_generation(
        database,
        candidate,
        enumeration_complete=True,
    )

    assert summary == GenerationSummary(
        generation_id=baseline,
        model_signature=model.model_signature,
        processing_signature="preparation-contract-v1",
        status="ready",
        pending=0,
        leased=0,
        done=1,
        errors=0,
        stale=0,
        cursor={},
    )


def test_embedding_generation_preparation_order_and_work_are_row_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small_database = tmp_path / "small" / "semantic.sqlite3"
    _, small_baseline, small_candidate = _exact_replay_fixture(
        small_database,
        member_count=1,
        processing_signature="bounded-preparation-small-v1",
    )
    small_summary, small_trace = _prepare_with_trace(
        small_database,
        small_candidate,
        monkeypatch,
    )

    large_database = tmp_path / "large" / "semantic.sqlite3"
    _, large_baseline, large_candidate = _exact_replay_fixture(
        large_database,
        member_count=24,
        processing_signature="bounded-preparation-large-v1",
    )
    large_summary, large_trace = _prepare_with_trace(
        large_database,
        large_candidate,
        monkeypatch,
    )

    assert small_summary is not None
    assert large_summary is not None
    assert (small_summary.generation_id, large_summary.generation_id) == (
        small_baseline,
        large_baseline,
    )
    assert len(small_trace) == len(large_trace)
    assert len(small_trace) <= 36
    ordered_phases = (
        "begin immediate",
        "select model_signature,status from embedding_generations",
        "select processing_signature,provenance_json,base_generation_id",
        "select generation_id from published_embedding_heads",
        "select count(*) from embedding_jobs",
        "select count(*) from embedding_generation_members",
        "select status,processing_signature,provenance_json",
        "select provenance_json from embedding_generations",
        "select (select count(*) from text_embeddings",
        "delete from embedding_generations",
        "from embedding_generations g left join embedding_jobs j",
        "commit",
    )
    phase_positions: list[int] = []
    for phase in ordered_phases:
        matches = tuple(index for index, statement in enumerate(small_trace) if phase in statement)
        assert matches, (phase, small_trace)
        phase_positions.append(matches[0])
    assert phase_positions == sorted(phase_positions)


def test_embedding_generation_preparation_rolls_back_then_retries_exact_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model, baseline, candidate = _exact_replay_fixture(
        database,
        member_count=1,
        processing_signature="preparation-rollback-v1",
    )
    with semantic_database(database) as connection:
        connection.execute(
            f"""CREATE TRIGGER fail_embedding_generation_preparation
            BEFORE DELETE ON embedding_generations
            WHEN OLD.generation_id={candidate}
            BEGIN
                SELECT RAISE(ABORT, 'injected preparation failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected preparation failure"):
        prepare_embedding_generation(
            database,
            candidate,
            enumeration_complete=True,
        )

    with semantic_database(database, readonly=True) as connection:
        candidate_snapshot = connection.execute(
            """SELECT status,base_generation_id,base_clone_complete
            FROM embedding_generations WHERE generation_id=?""",
            (candidate,),
        ).fetchone()
        published_head = int(
            connection.execute(
                """SELECT generation_id FROM published_embedding_heads
                WHERE model_signature=?""",
                (model.model_signature,),
            ).fetchone()[0]
        )
    assert candidate_snapshot is not None
    assert tuple(candidate_snapshot) == ("building", baseline, 0)
    assert published_head == baseline

    with semantic_database(database) as connection:
        connection.execute("DROP TRIGGER fail_embedding_generation_preparation")
    retry = prepare_embedding_generation(
        database,
        candidate,
        enumeration_complete=True,
    )
    assert retry is not None
    assert retry.generation_id == baseline

    replay = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="preparation-rollback-v1",
        materialize_base=False,
        started_ns=130,
    )
    repeated = prepare_embedding_generation(
        database,
        replay,
        enumeration_complete=True,
    )
    assert repeated == retry


def test_embedding_generation_finalization_public_contract(tmp_path: Path) -> None:
    assert str(inspect.signature(finalize_embedding_generation)) == (
        "(path: 'Path', generation_id: 'int', *, allow_partial: 'bool' = False, "
        "completed_ns: 'int | None' = None) -> 'GenerationSummary'"
    )
    assert (
        semantic_generation_repository.finalize_embedding_generation
        is finalize_embedding_generation
    )

    database = tmp_path / "semantic.sqlite3"
    model, generation = _completed_generation(
        database,
        member_count=1,
        processing_signature="finalization-contract-v1",
    )
    summary = finalize_embedding_generation(database, generation, completed_ns=110)

    assert summary == GenerationSummary(
        generation_id=generation,
        model_signature=model.model_signature,
        processing_signature="finalization-contract-v1",
        status="ready",
        pending=0,
        leased=0,
        done=1,
        errors=0,
        stale=0,
        cursor={},
    )


def test_embedding_generation_finalization_order_and_work_are_row_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small_database = tmp_path / "small" / "semantic.sqlite3"
    _, small_generation = _completed_generation(
        small_database,
        member_count=1,
        processing_signature="bounded-finalization-small-v1",
    )
    small_summary, small_trace = _finalize_with_trace(
        small_database,
        small_generation,
        monkeypatch,
        completed_ns=110,
    )

    large_database = tmp_path / "large" / "semantic.sqlite3"
    _, large_generation = _completed_generation(
        large_database,
        member_count=24,
        processing_signature="bounded-finalization-large-v1",
    )
    large_summary, large_trace = _finalize_with_trace(
        large_database,
        large_generation,
        monkeypatch,
        completed_ns=110,
    )

    assert (small_summary.done, large_summary.done) == (1, 24)
    assert len(small_trace) == len(large_trace)
    assert len(small_trace) <= 36
    ordered_phases = (
        "begin immediate",
        "select model_signature,status from embedding_generations",
        "update embedding_jobs set status='stale'",
        "delete from embedding_jobs where generation_id=",
        "from embedding_generations g left join embedding_jobs j",
        "select base_generation_id,base_clone_complete",
        "select provenance_json from embedding_generations",
        "delete from embedding_generation_members as member",
        "select j.job_id from embedding_jobs j",
        "select generation_id from published_embedding_heads",
        "update embedding_generations set status='ready'",
        "insert into published_embedding_heads",
        "commit",
    )
    phase_positions: list[int] = []
    for phase in ordered_phases:
        matches = tuple(index for index, statement in enumerate(small_trace) if phase in statement)
        assert matches, (phase, small_trace)
        phase_positions.append(matches[0])
    assert phase_positions == sorted(phase_positions)


def test_embedding_generation_finalization_rolls_back_then_retries_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    original_chunk = _stage(database, "rollback-document", "original record", 1)
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="finalization-rollback-v1",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (original_chunk.chunk_id,),
        now_ns=101,
    )
    _complete_jobs(database, generation, now_ns=102)
    _stage(database, "rollback-document", "replacement record", 2)

    with semantic_database(database) as connection:
        connection.execute(
            f"""CREATE TRIGGER fail_embedding_generation_finalize
            BEFORE UPDATE OF status ON embedding_generations
            WHEN OLD.generation_id={generation} AND NEW.status='ready'
            BEGIN
                SELECT RAISE(ABORT, 'injected finalization failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected finalization failure"):
        finalize_embedding_generation(database, generation, completed_ns=110)

    with semantic_database(database, readonly=True) as connection:
        rolled_back = connection.execute(
            """SELECT status,completed_ns FROM embedding_generations
            WHERE generation_id=?""",
            (generation,),
        ).fetchone()
        rolled_back_jobs = int(
            connection.execute(
                "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?",
                (generation,),
            ).fetchone()[0]
        )
        rolled_back_members = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_generation_members
                WHERE generation_id=?""",
                (generation,),
            ).fetchone()[0]
        )
        rolled_back_head = connection.execute(
            """SELECT generation_id FROM published_embedding_heads
            WHERE model_signature=?""",
            (model.model_signature,),
        ).fetchone()
    assert rolled_back is not None
    assert (str(rolled_back["status"]), rolled_back["completed_ns"]) == (
        "building",
        None,
    )
    assert (rolled_back_jobs, rolled_back_members, rolled_back_head) == (1, 1, None)

    with semantic_database(database) as connection:
        connection.execute("DROP TRIGGER fail_embedding_generation_finalize")
    summary = finalize_embedding_generation(database, generation, completed_ns=111)
    assert (summary.status, summary.done, summary.errors, summary.stale) == (
        "ready",
        0,
        0,
        0,
    )

    with semantic_database(database, readonly=True) as connection:
        published_snapshot = (
            tuple(
                connection.execute(
                    """SELECT status,completed_ns,pending_count,leased_count,
                        done_count,error_count,stale_count
                    FROM embedding_generations WHERE generation_id=?""",
                    (generation,),
                ).fetchone()
            ),
            connection.execute(
                """SELECT generation_id,published_ns FROM published_embedding_heads
                WHERE model_signature=?""",
                (model.model_signature,),
            ).fetchone(),
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?",
                    (generation,),
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM embedding_generation_members
                    WHERE generation_id=?""",
                    (generation,),
                ).fetchone()[0]
            ),
        )
    with pytest.raises(SemanticStateError, match="is not building"):
        finalize_embedding_generation(database, generation, completed_ns=112)
    with semantic_database(database, readonly=True) as connection:
        replay_snapshot = (
            tuple(
                connection.execute(
                    """SELECT status,completed_ns,pending_count,leased_count,
                        done_count,error_count,stale_count
                    FROM embedding_generations WHERE generation_id=?""",
                    (generation,),
                ).fetchone()
            ),
            connection.execute(
                """SELECT generation_id,published_ns FROM published_embedding_heads
                WHERE model_signature=?""",
                (model.model_signature,),
            ).fetchone(),
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?",
                    (generation,),
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM embedding_generation_members
                    WHERE generation_id=?""",
                    (generation,),
                ).fetchone()[0]
            ),
        )
    assert replay_snapshot == published_snapshot


def test_partial_and_cas_loser_generations_never_replace_the_published_head(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    chunk = _stage(database, "document", "published transformer record", 1)
    initial = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="initial",
        started_ns=100,
    )
    enqueue_text_chunk_jobs(database, initial, (chunk.chunk_id,), now_ns=101)
    _complete_jobs(database, initial, now_ns=102)
    finalize_embedding_generation(database, initial, completed_ns=110)

    partial = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="partial",
        started_ns=120,
    )
    failed_chunk = _stage(
        database,
        "failed-document",
        "unpublished breaker maintenance record",
        1,
    )
    enqueue_text_chunk_jobs(database, partial, (failed_chunk.chunk_id,), now_ns=121)
    lease = claim_embedding_jobs(
        database,
        partial,
        worker_id="publication-worker",
        limit=1,
        lease_seconds=60,
        now_ns=122,
    )[0]
    fail_embedding_job(
        database,
        lease.job_id,
        worker_id="publication-worker",
        error_type="fixture_failure",
        error_message="injected",
        retryable=False,
        now_ns=123,
    )
    summary = finalize_embedding_generation(
        database,
        partial,
        allow_partial=True,
        completed_ns=124,
    )
    assert summary.status == "ready_partial"
    assert {hit.generation_id for hit in search_exact_page(database, _query(model)).hits} == {
        initial
    }

    winner = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="winner",
        started_ns=130,
    )
    loser = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="loser",
        started_ns=131,
    )
    finalize_embedding_generation(database, winner, completed_ns=132)
    with pytest.raises(SemanticStateError, match="must be rebased"):
        finalize_embedding_generation(database, loser, completed_ns=133)
    rebased = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="loser",
        started_ns=134,
    )
    assert rebased != loser
    assert {hit.generation_id for hit in search_exact_page(database, _query(model)).hits} == {
        winner
    }
    with semantic_database(database, readonly=True) as connection:
        loser_row = connection.execute(
            "SELECT status,completed_ns FROM embedding_generations WHERE generation_id=?",
            (loser,),
        ).fetchone()
        rebased_row = connection.execute(
            "SELECT status,base_generation_id FROM embedding_generations WHERE generation_id=?",
            (rebased,),
        ).fetchone()
        assert loser_row is not None
        assert str(loser_row["status"]) == "failed"
        assert loser_row["completed_ns"] is not None
        assert rebased_row is not None
        assert str(rebased_row["status"]) == "building"
        assert int(rebased_row["base_generation_id"]) == winner
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


def test_populated_v5_migration_preserves_legacy_rows_and_publishes_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-v5.sqlite3"
    model, chunk = _create_populated_v5(database)

    initialize_semantic_state(database)

    page = search_exact_page(database, _query(model), limit=10)
    assert tuple(hit.entity_id for hit in page.hits) == (chunk.chunk_id,)
    resolved = resolve_search_hits(database, page.hits)
    assert resolved[0].path == "C:/fixtures/legacy-document.pdf"
    assert resolved[0].snippet == "legacy published transformer record"
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM text_embeddings").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM embedding_generation_members").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0] == 1
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    before = database.read_bytes()
    initialize_semantic_state(database)
    assert database.read_bytes() == before


def test_v6_migration_rolls_back_on_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic-v5-rollback.sqlite3"
    _create_populated_v5(database)
    real_migration = semantic_schema._MIGRATIONS_BY_TARGET[6]

    def interrupt_after_migration(
        connection: sqlite3.Connection,
        applied_ns: int,
    ) -> None:
        real_migration(connection, applied_ns)
        raise KeyboardInterrupt("injected migration interruption")

    monkeypatch.setitem(
        semantic_schema._MIGRATIONS_BY_TARGET,
        6,
        interrupt_after_migration,
    )
    with pytest.raises(KeyboardInterrupt, match="injected migration interruption"):
        initialize_semantic_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("5",)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='published_embedding_heads'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM text_embeddings").fetchone()[0] == 1


@pytest.mark.parametrize("unknown_kind", ("table", "column", "index", "trigger"))
def test_v5_migration_abstains_from_unknown_objects_without_mutation(
    tmp_path: Path,
    unknown_kind: str,
) -> None:
    database = tmp_path / f"semantic-v5-{unknown_kind}.sqlite3"
    _create_populated_v5(database)
    with sqlite3.connect(database) as connection:
        if unknown_kind == "table":
            connection.execute("CREATE TABLE vendor_extension(value TEXT)")
            connection.execute("INSERT INTO vendor_extension(value) VALUES('preserve-me')")
        elif unknown_kind == "column":
            connection.execute("ALTER TABLE semantic_items ADD COLUMN vendor_payload TEXT")
        elif unknown_kind == "index":
            connection.execute(
                "CREATE INDEX vendor_semantic_items_idx ON semantic_items(updated_ns)"
            )
        else:
            connection.execute(
                """CREATE TRIGGER vendor_semantic_items_trigger
                AFTER INSERT ON semantic_items BEGIN SELECT 1; END"""
            )
    with sqlite3.connect(database) as connection:
        before_objects = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )

    with pytest.raises(SemanticStateError, match=r"unexpected|incompatible"):
        initialize_semantic_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM text_embeddings").fetchone()[0] == 1
        if unknown_kind == "table":
            assert connection.execute("SELECT value FROM vendor_extension").fetchone() == (
                "preserve-me",
            )
        assert (
            tuple(
                connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
            == before_objects
        )


# endregion [02]
