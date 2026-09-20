from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Sequence, cast

import pytest

from neocortex.semantic import semantic_service as service
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128
from neocortex.semantic import semantic_preparation
from neocortex.semantic import semantic_search_service as search_implementation
from neocortex.semantic import semantic_state as state
from neocortex.semantic.semantic_chunking import TextChunkingConfig
from neocortex.semantic.semantic_chunking import iter_text_chunks
from neocortex.semantic.semantic_config import (
    compact_multilingual_text_model,
    fastembed_cache_contract,
)
from neocortex.semantic.semantic_lexical import (
    LEXICAL_MODEL_SIGNATURE,
    MAX_QUERY_CHARS,
    LexicalAvailability,
    LexicalRanking,
    LexicalStatePaths,
)
from neocortex.semantic.semantic_generation_repository import merge_source_head_ledger
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingJobLease,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    EvidenceDisposition,
    ResolvedSearchHit,
    SearchHit,
    SemanticEntityKind,
    SemanticItem,
    TextSection,
    fingerprint_bytes,
    fingerprint_text,
)
from neocortex.semantic.semantic_ontology import CONCEPTS, ONTOLOGY_VERSION
from neocortex.semantic.semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
    ImageSourceRecord,
    SemanticSourceHead,
    TextSourceRecord,
)
from neocortex.semantic.semantic_state import (
    has_active_embeddings,
    list_semantic_evidence,
    semantic_database,
)


TEST_CAPABILITIES = ("base", "inference", "image")
pytestmark = pytest.mark.capability("base", "inference")


# region [01] Deterministic service fixture backend


class _FixtureBackend:
    def __init__(self, model: EmbeddingModelSpec) -> None:
        self._model = model

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return 16

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        output: list[BackendEmbedding] = []
        for request in requests:
            position = int(request.fingerprint.xxh3_128[:16], 16) % self.model.dimensions
            vector = tuple(
                1.0 if index == position else 0.0 for index in range(self.model.dimensions)
            )
            output.append(
                BackendEmbedding(
                    request_id=request.request_id,
                    vector=vector,
                    provenance={"backend": "semantic-service-fixture"},
                )
            )
        return tuple(output)

    def text_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        return tuple(len(text.split()) + 2 for text in texts), 512

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return "semantic-service-fixture-tokenizer-v1", 512


def _fixture_chunking(model: EmbeddingModelSpec) -> TextChunkingConfig:
    return replace(
        service.text_chunking_for_model(model),
        model_token_limit=512,
        tokenizer_signature="semantic-service-fixture-tokenizer-v1",
    )


class _ConstantBackend(_FixtureBackend):
    def __init__(
        self,
        model: EmbeddingModelSpec,
        *,
        oppose_queries: bool = False,
    ) -> None:
        super().__init__(model)
        self.oppose_queries = oppose_queries
        self.requests: list[EmbeddingRequest] = []

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        self.requests.extend(requests)
        output: list[BackendEmbedding] = []
        for request in requests:
            leading = -1.0 if self.oppose_queries and request.role is EmbeddingRole.QUERY else 1.0
            output.append(
                BackendEmbedding(
                    request_id=request.request_id,
                    vector=(leading,) + (0.0,) * (self.model.dimensions - 1),
                    provenance={"backend": "constant-fixture"},
                )
            )
        return tuple(output)


def _patch_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_backend",
        lambda model, **_kwargs: _FixtureBackend(model),
    )


def _fixture_image_calibration(
    *,
    minimum_score: float = -1.0,
) -> service.ImageRetrievalCalibration:
    return service.ImageRetrievalCalibration(
        calibration_signature="fixture-positive-negative-v1",
        query_model_signature=service.clip_text_model().model_signature,
        indexed_model_signature=service.clip_image_model().model_signature,
        pipeline=service.SEMANTIC_PIPELINE_VERSION,
        backend="semantic-service-fixture",
        minimum_score=minimum_score,
        positive_queries=12,
        negative_queries=12,
        sample_items=20,
    )


def _text_record() -> TextSourceRecord:
    text = "Mantenimiento y diagnóstico de un transformador de potencia."
    item = SemanticItem(
        item_id="item:pdf:fixture-pdf",
        source_kind="pdf",
        source_identity="fixture-pdf",
        identity_version="fixture-source-v1",
        fingerprint=fingerprint_text("fixture-pdf-source"),
        path="C:/fixtures/transformador.pdf",
        provenance={"fixture": True},
    )
    return TextSourceRecord(
        item,
        TextSection("pdf_page", "1", text, {"fixture": True}),
    )


def _text_records(count: int) -> tuple[TextSourceRecord, ...]:
    records: list[TextSourceRecord] = []
    for index in range(count):
        identity = f"fixture-pdf-{index}"
        item = SemanticItem(
            item_id=f"item:pdf:{identity}",
            source_kind="pdf",
            source_identity=identity,
            identity_version="fixture-source-v1",
            fingerprint=fingerprint_text(f"{identity}-source"),
            path=f"C:/fixtures/{identity}.pdf",
            provenance={"fixture": True},
        )
        records.append(
            TextSourceRecord(
                item,
                TextSection(
                    "pdf_page",
                    "1",
                    f"Mantenimiento del transformador número {index}.",
                    {"fixture": True},
                ),
            )
        )
    return tuple(records)


def _image_record(
    root: Path,
    identity: str,
    *,
    with_ocr: bool = True,
) -> ImageSourceRecord:
    image_path = root / f"{identity}.png"
    payload = f"fixture-image-payload:{identity}".encode()
    image_path.write_bytes(payload)
    fingerprint = fingerprint_bytes(payload)
    item = SemanticItem(
        item_id=f"item:image:{identity}",
        source_kind="image",
        source_identity=identity,
        identity_version="fixture-image-v1",
        fingerprint=fingerprint,
        path=str(image_path),
        provenance={"fixture": True},
        source_revision={
            "size": len(payload),
            "mtime_ns": image_path.stat().st_mtime_ns,
            "birthtime_ns": image_path.stat().st_ctime_ns,
            "raw_content_xxh3_128": hashlib.sha256(payload).hexdigest(),
        },
    )
    ocr_section = (
        TextSection(
            "image_ocr",
            "ocr",
            f"Transformador industrial {identity} en mantenimiento.",
            {"fixture": True},
        )
        if with_ocr
        else None
    )
    return ImageSourceRecord(item, ocr_section)


def _declare_source_state(state_directory: Path, source_kind: str) -> None:
    with sqlite3.connect(service.semantic_source_database(state_directory, source_kind)) as conn:
        if source_kind == "pdf":
            conn.executescript(
                """CREATE TABLE documents(file_key TEXT PRIMARY KEY,path TEXT,
                    processing_signature TEXT,status TEXT,size INTEGER,mtime_ns INTEGER,
                    birthtime_ns INTEGER,last_seen_run_id INTEGER,is_partial INTEGER,
                    normalized_text_xxh3_128 TEXT,normalized_text_chars INTEGER);
                CREATE TABLE pages(file_key TEXT,page_number INTEGER,source TEXT,
                    text_zlib BLOB,text_chars INTEGER);"""
            )
        elif source_kind == "image":
            conn.execute(
                """CREATE TABLE images(file_key TEXT PRIMARY KEY,path TEXT,size INTEGER,
                    mtime_ns INTEGER,birthtime_ns INTEGER,last_seen_run_id INTEGER,
                    processing_signature TEXT,category TEXT,document_candidate INTEGER,
                    adult_classification TEXT,status TEXT)"""
            )
        else:
            raise ValueError(f"unsupported source-owner fixture: {source_kind}")


def _fixture_image_heads(records: Sequence[ImageSourceRecord]) -> tuple[SemanticSourceHead, ...]:
    """Pair an injected record iterator with the same controlled owner revision."""

    return (
        SemanticSourceHead(
            "image", "image.sqlite3", "fixture-image-owner-v1", 1, len(records),
            "sha256:" + hashlib.sha256(repr(tuple(records)).encode()).hexdigest(), True,
        ),
    )


# endregion [01]


# region [02] Resumable text and multimodal indexing


def test_missing_source_state_cannot_deactivate_or_create_semantic_state(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="semantic source state is missing"):
        service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    with pytest.raises(FileNotFoundError, match="semantic source state is missing"):
        service.index_image_embeddings(tmp_path)

    assert not (tmp_path / service.SEMANTIC_DATABASE_NAME).exists()


def test_text_index_requires_signed_exact_tokenizer_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CounterlessBackend(_FixtureBackend):
        text_token_counts = None
        text_tokenizer_contract = None

    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "_backend",
        lambda model, **_kwargs: CounterlessBackend(model),
    )
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )

    with pytest.raises(RuntimeError, match="no exact tokenizer counter"):
        service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert not (tmp_path / service.SEMANTIC_DATABASE_NAME).exists()


def test_text_index_reuses_cached_vector_on_second_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, source: iter((_text_record(),)) if source == "pdf" else iter(()),
    )

    first = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    second = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert first.items_staged == 1
    assert first.chunks_staged == 2
    assert first.generations[0].embedded == 2
    assert first.generations[0].summary.status == "ready"
    assert second.generations[0].embedded == 0
    assert second.generations[0].reused == 0
    assert second.generations[0].queued == 0
    assert second.new_jobs_staged == 0
    assert second.errors == 0


def test_exact_text_replay_skips_backend_source_enumeration_and_chunking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    head = SemanticSourceHead(
        "pdf",
        "pdf.sqlite3",
        "fixture-adapter-v1",
        1,
        1,
        "sha256:" + "a" * 64,
        True,
    )
    monkeypatch.setattr(service._text_index, "semantic_source_heads", lambda *_args: (head,))
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, source: iter((_text_record(),)) if source == "pdf" else iter(()),
    )
    monkeypatch.setattr("neocortex.runtime.control.cpu_runtime.effective_cpu_count", lambda: 2)
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), threads=2)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("exact replay must not enumerate sources or load a backend")

    monkeypatch.setattr(service, "iter_text_source_records", unexpected)
    monkeypatch.setattr(service, "_backend", unexpected)
    monkeypatch.setattr("neocortex.runtime.control.cpu_runtime.effective_cpu_count", lambda: 24)
    replay = service.index_text_embeddings(tmp_path, source_kinds=("pdf",), threads=24)

    assert baseline.execution_mode == "enumerated"
    assert replay.execution_mode == "exact_replay"
    assert replay.sources_reused == 1
    assert replay.sources_enumerated == 0
    assert replay.items_staged == replay.chunks_staged == replay.new_jobs_staged == 0
    assert (
        replay.generations[0].summary.generation_id == baseline.generations[0].summary.generation_id
    )


def test_changed_source_head_falls_back_to_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    heads = [
        SemanticSourceHead(
            "pdf",
            "pdf.sqlite3",
            "fixture-adapter-v1",
            1,
            1,
            "sha256:" + "a" * 64,
            True,
        )
    ]
    monkeypatch.setattr(
        service._text_index,
        "semantic_source_heads",
        lambda *_args: tuple(heads),
    )
    enumerations = [0]

    def records(_state, source):
        enumerations[0] += 1
        return iter((_text_record(),)) if source == "pdf" else iter(())

    monkeypatch.setattr(service, "iter_text_source_records", records)
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    heads[0] = replace(heads[0], digest="sha256:" + "b" * 64)
    refreshed = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert refreshed.execution_mode == "enumerated"
    assert refreshed.sources_enumerated == 1
    assert enumerations[0] == 2


def test_source_head_ledger_replaces_only_overlapping_channel_scopes() -> None:
    ledger = {
        "text:pdf,docx": {
            "channel": "text",
            "source_kinds": ["pdf", "docx"],
            "source_heads": ["old"],
        },
        "text:audio": {
            "channel": "text",
            "source_kinds": ["audio"],
        },
        "image:vectors": {
            "channel": "image-vector",
            "source_kinds": ["image"],
        },
        7: "malformed",
    }
    entry = {
        "channel": "text",
        "source_kinds": ["pdf"],
        "source_heads": ["new"],
    }

    merged = merge_source_head_ledger(ledger, scope_key="text:pdf", entry=entry)

    assert "text:pdf,docx" not in merged
    assert merged["text:audio"] == ledger["text:audio"]
    assert merged["image:vectors"] == ledger["image:vectors"]
    assert merged["text:pdf"] == entry
    with pytest.raises(ValueError, match="entry is invalid"):
        merge_source_head_ledger({}, scope_key="scope", entry={})
    with pytest.raises(ValueError, match="scope is invalid"):
        merge_source_head_ledger(
            {},
            scope_key="",
            entry={"channel": "text", "source_kinds": ["pdf"]},
        )
    oversized = {
        f"scope:{index}": {"channel": "image-vector", "source_kinds": [f"kind:{index}"]}
        for index in range(64)
    }
    with pytest.raises(ValueError, match="exceeds its bound"):
        merge_source_head_ledger(
            oversized,
            scope_key="text:pdf",
            entry={"channel": "text", "source_kinds": ["pdf"]},
        )


def test_exact_published_replay_is_free_across_item_and_job_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    records = _text_records(35)
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, source: iter(records) if source == "pdf" else iter(()),
    )
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    model_signature = service.multilingual_text_model().model_signature
    baseline_generation_id = baseline.generations[0].summary.generation_id
    with semantic_database(database, readonly=True) as connection:
        baseline_counts = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "embedding_generations",
                "embedding_generation_members",
                "embedding_jobs",
            )
        )

    replay = service.index_text_embeddings(
        tmp_path,
        source_kinds=("pdf",),
        work_budget=service.SemanticWorkBudget(max_items=1, max_new_jobs=1),
    )

    assert baseline.complete
    assert replay.complete
    assert not replay.truncated
    assert replay.new_jobs_staged == 0
    assert replay.generations[0].queued == 0
    assert replay.generations[0].summary.status == "ready"
    assert replay.generations[0].summary.generation_id == baseline_generation_id
    with semantic_database(database, readonly=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                    (model_signature,),
                ).fetchone()[0]
            )
            == baseline_generation_id
        )
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM semantic_items WHERE source_kind='pdf' AND active=1"
                ).fetchone()[0]
            )
            == 35
        )
        assert (
            tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "embedding_generations",
                    "embedding_generation_members",
                    "embedding_jobs",
                )
            )
            == baseline_counts
        )
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                    (baseline_generation_id,),
                ).fetchone()[0]
            )
            == 70
        )


def test_title_policy_upgrade_embeds_only_title_and_preserves_body_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = service.multilingual_text_model()
    backend = _FixtureBackend(model)
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    record = _text_record()
    _declare_source_state(tmp_path, "pdf")
    service._initialize_models(database, (model,))
    state.upsert_semantic_item(
        database,
        record.item,
        refresh_token="legacy-item",
    )
    body_chunks = tuple(
        iter_text_chunks(
            record.item.item_id,
            (record.section,),
            _fixture_chunking(model),
            token_counter=backend.text_token_counts,
        )
    )
    assert len(body_chunks) == 1
    state.stage_text_chunks(
        database,
        body_chunks,
        refresh_token="legacy-body",
    )
    state.finalize_text_chunk_refresh(
        database,
        item_id=record.item.item_id,
        chunking_signature=_fixture_chunking(model).signature,
        refresh_token="legacy-body",
    )
    legacy_generation = state.start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="legacy-without-title-policy-v1",
    )
    state.enqueue_text_chunk_jobs(
        database,
        legacy_generation,
        (body_chunks[0].chunk_id,),
    )
    legacy_work = service._worker.run_generation(
        database,
        legacy_generation,
        backend,
        queued=1,
    )
    assert legacy_work.embedded == 1
    assert legacy_work.summary.status == "ready"

    def published_body_identity() -> tuple[int, int, int]:
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                """SELECT e.payload_id,e.item_revision_id,e.chunk_revision_id
                    FROM published_embedding_heads h
                    JOIN embedding_generation_members e
                      ON e.generation_id=h.generation_id
                     AND e.model_signature=h.model_signature
                    JOIN semantic_chunk_revisions c
                      ON c.chunk_revision_id=e.chunk_revision_id
                    WHERE h.model_signature=? AND c.section_kind='pdf_page'""",
                (model.model_signature,),
            ).fetchone()
        assert row is not None
        return tuple(int(value) for value in row)

    body_before = published_body_identity()
    monkeypatch.setattr(
        service,
        "_backend",
        lambda requested_model, **_kwargs: _FixtureBackend(requested_model),
    )
    legacy_search = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_images=False,
        include_lexical=False,
    )
    assert tuple(ranking.name for ranking in legacy_search.rankings) == ("semantic_text",)
    assert legacy_search.rankings[0].available is True
    assert legacy_search.complete

    legacy_discovery = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_title=True,
        include_images=False,
        include_lexical=False,
    )
    assert tuple(ranking.name for ranking in legacy_discovery.rankings) == ("semantic_text",)
    assert legacy_discovery.complete
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((record,)),
    )

    upgraded = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert upgraded.complete
    assert upgraded.new_jobs_staged == 1
    assert upgraded.generations[0].embedded == 1
    assert upgraded.generations[0].reused == 0
    assert body_before == published_body_identity()
    with semantic_database(database, readonly=True) as connection:
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """SELECT c.section_kind,COUNT(*)
                    FROM published_embedding_heads h
                    JOIN embedding_generation_members e
                      ON e.generation_id=h.generation_id
                     AND e.model_signature=h.model_signature
                    JOIN semantic_chunk_revisions c
                      ON c.chunk_revision_id=e.chunk_revision_id
                    WHERE h.model_signature=? GROUP BY c.section_kind""",
                (model.model_signature,),
            )
        }
    assert counts == {"pdf_page": 1, "semantic_metadata_title": 1}

    replay = service.index_text_embeddings(
        tmp_path,
        source_kinds=("pdf",),
        work_budget=service.SemanticWorkBudget(max_items=1, max_new_jobs=1),
    )
    assert replay.complete
    assert replay.new_jobs_staged == 0
    assert replay.generations[0].queued == 0


def test_title_policy_bump_prunes_old_title_and_retains_unselected_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = service.multilingual_text_model()
    backend = _FixtureBackend(model)
    chunking = _fixture_chunking(model)
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    pdf_record = _text_record()
    docx_item = SemanticItem(
        item_id="item:docx:unselected",
        source_kind="docx",
        source_identity="unselected",
        identity_version="fixture-source-v1",
        fingerprint=fingerprint_text("unselected-docx-source"),
        path="C:/fixtures/unselected.docx",
        provenance={"fixture": True},
    )
    docx_section = TextSection(
        "docx_body",
        "document",
        "contenido no seleccionado",
        {"fixture": True},
    )
    old_title = TextSection(
        SEMANTIC_TITLE_SECTION_KIND,
        "semantic-basename-title-v0",
        "transformador",
        {
            "policy_signature": "semantic-basename-title-v0",
            "advisory_only": True,
        },
    )
    pdf_chunks = tuple(
        iter_text_chunks(
            pdf_record.item.item_id,
            (pdf_record.section, old_title),
            chunking,
            token_counter=backend.text_token_counts,
        )
    )
    docx_chunks = tuple(
        iter_text_chunks(
            docx_item.item_id,
            (docx_section,),
            chunking,
            token_counter=backend.text_token_counts,
        )
    )
    assert len(pdf_chunks) == 2
    assert len(docx_chunks) == 1
    service._initialize_models(database, (model,))
    for item, chunks, refresh in (
        (pdf_record.item, pdf_chunks, "legacy-pdf"),
        (docx_item, docx_chunks, "legacy-docx"),
    ):
        state.upsert_semantic_item(database, item, refresh_token=refresh)
        state.stage_text_chunks(database, chunks, refresh_token=refresh)
        state.finalize_text_chunk_refresh(
            database,
            item_id=item.item_id,
            chunking_signature=chunking.signature,
            refresh_token=refresh,
        )
    legacy_generation = state.start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="legacy-title-policy-v0",
    )
    state.enqueue_text_chunk_jobs(
        database,
        legacy_generation,
        (chunk.chunk_id for chunk in (*pdf_chunks, *docx_chunks)),
    )
    legacy_work = service._worker.run_generation(
        database,
        legacy_generation,
        backend,
        queued=3,
    )
    assert legacy_work.embedded == 3
    assert legacy_work.summary.status == "ready"

    def published_rows() -> dict[tuple[str, str, str], tuple[int, int, int]]:
        with semantic_database(database, readonly=True) as connection:
            rows = connection.execute(
                """SELECT i.source_kind,c.section_kind,c.section_id,
                    e.payload_id,e.item_revision_id,e.chunk_revision_id
                    FROM published_embedding_heads h
                    JOIN embedding_generation_members e
                      ON e.generation_id=h.generation_id
                     AND e.model_signature=h.model_signature
                    JOIN semantic_item_revisions i
                      ON i.item_revision_id=e.item_revision_id
                    JOIN semantic_chunk_revisions c
                      ON c.chunk_revision_id=e.chunk_revision_id
                    WHERE h.model_signature=?
                    ORDER BY i.source_kind,c.section_kind,c.section_id""",
                (model.model_signature,),
            ).fetchall()
        return {
            (str(row[0]), str(row[1]), str(row[2])): (
                int(row[3]),
                int(row[4]),
                int(row[5]),
            )
            for row in rows
        }

    before = published_rows()
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "_backend",
        lambda requested_model, **_kwargs: _FixtureBackend(requested_model),
    )
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((pdf_record,)),
    )

    upgraded = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert upgraded.complete
    assert upgraded.new_jobs_staged == 1
    assert upgraded.generations[0].embedded == 0
    assert upgraded.generations[0].reused == 1
    after = published_rows()
    assert set(after) == {
        ("docx", "docx_body", "document"),
        ("pdf", "pdf_page", "1"),
        ("pdf", SEMANTIC_TITLE_SECTION_KIND, SEMANTIC_TITLE_POLICY),
    }
    assert after[("docx", "docx_body", "document")] == before[("docx", "docx_body", "document")]
    assert after[("pdf", "pdf_page", "1")] == before[("pdf", "pdf_page", "1")]
    assert (
        "pdf",
        SEMANTIC_TITLE_SECTION_KIND,
        "semantic-basename-title-v0",
    ) not in after


@pytest.mark.capability('image','inference')
def test_image_and_ocr_use_separate_embedding_generations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "image")
    image_path = tmp_path / "subestacion.png"
    payload = b"fixture-image-payload"
    image_path.write_bytes(payload)
    item = SemanticItem(
        item_id="item:image:fixture-image",
        source_kind="image",
        source_identity="fixture-image",
        identity_version="fixture-image-v1",
        fingerprint=fingerprint_bytes(payload),
        path=str(image_path),
        provenance={"fixture": True},
        source_revision={
            "size": len(payload),
            "mtime_ns": image_path.stat().st_mtime_ns,
            "birthtime_ns": image_path.stat().st_ctime_ns,
            "raw_content_xxh3_128": hashlib.sha256(payload).hexdigest(),
        },
    )
    record = ImageSourceRecord(
        item,
        TextSection(
            "image_ocr",
            "ocr",
            "Transformador de potencia en mantenimiento.",
            {"fixture": True},
        ),
    )
    monkeypatch.setattr(
        service._image_index, "semantic_source_heads",
        lambda *_args: _fixture_image_heads((record,)),
    )
    monkeypatch.setattr(
        service,
        "iter_image_source_records",
        lambda _state: iter((record,)),
    )

    result = service.index_image_embeddings(tmp_path, embed_ocr_text=True)

    assert result.sources == ("image", "image-ocr")
    assert result.items_staged == 1
    assert result.chunks_staged == 1
    assert len(result.generations) == 2
    assert all(generation.embedded == 1 for generation in result.generations)
    assert {generation.summary.model_signature for generation in result.generations} == {
        service.clip_image_model().model_signature,
        service.multilingual_text_model().model_signature,
    }

    def unexpected_query_vector(*_args, **_kwargs):
        raise AssertionError("CLIP query backend must not load while retrieval is uncalibrated")

    with monkeypatch.context() as no_uncalibrated_clip:
        no_uncalibrated_clip.setattr(
            search_implementation,
            "query_vector",
            unexpected_query_vector,
        )
        uncalibrated = service.search_semantic_index(
            tmp_path,
            "subestación con transformador",
            include_text=False,
            include_images=True,
            include_lexical=False,
        )
    assert uncalibrated.rankings[0].hits == ()
    assert uncalibrated.rankings[0].scanned == 0
    assert uncalibrated.rankings[0].complete is True
    assert uncalibrated.rankings[0].provenance["retrieval_abstention"] == {
        "status": "not_calibrated",
        "query_abstained": True,
        "abstention_reason": "image_retrieval_not_calibrated",
        "score_interpretation": "cosine_similarity_retrieval_floor_not_probability",
        "raw_hits": 0,
        "retained_hits": 0,
        "rejected_hits": 0,
    }
    search = service.search_semantic_index(
        tmp_path,
        "subestación con transformador",
        include_text=False,
        include_images=True,
        include_lexical=False,
        image_calibration=_fixture_image_calibration(),
    )
    image_ranking = search.rankings[0]
    assert image_ranking.available
    assert len(image_ranking.hits) == 1
    assert image_ranking.hits[0].indexed_model_signature == (
        service.clip_image_model().model_signature
    )
    assert image_ranking.hits[0].query_model_signature == (
        service.clip_text_model().model_signature
    )
    assert image_ranking.resolved[0].hit.query_model_signature == (
        service.clip_text_model().model_signature
    )
    fusion_evidence = search.fused[0].fused.evidence[0]
    assert fusion_evidence.indexed_model_signature == (service.clip_image_model().model_signature)
    assert fusion_evidence.query_model_signature == (service.clip_text_model().model_signature)

    with monkeypatch.context() as no_textual_clip:
        no_textual_clip.setattr(
            search_implementation,
            "query_vector",
            unexpected_query_vector,
        )
        textual = service.search_semantic_index(
            tmp_path,
            "qué dice el documento del transformador",
            include_text=False,
            include_images=True,
            include_lexical=True,
            image_calibration=_fixture_image_calibration(),
        )
    textual_image = textual.rankings[0]
    assert textual_image.hits == ()
    assert textual_image.scanned == 0
    assert textual_image.provenance["image_query_routing"] == {
        "policy_signature": "semantic-image-query-routing-v1",
        "intent": "explicit_textual",
        "executed": False,
        "reason": "textual_query_routed_away_from_clip",
    }

    compact_model = compact_multilingual_text_model()
    compact = service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=compact_model,
    )
    assert compact.chunks_staged == 1
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    with semantic_database(database, readonly=True) as connection:
        active_signatures = connection.execute(
            """SELECT chunking_signature FROM text_chunks
            WHERE item_id=? AND active=1""",
            (item.item_id,),
        ).fetchall()
        assert {str(row[0]) for row in active_signatures} == {
            _fixture_chunking(service.multilingual_text_model()).signature,
            _fixture_chunking(compact_model).signature,
        }
    assert has_active_embeddings(
        database,
        service.multilingual_text_model().model_signature,
    )
    assert has_active_embeddings(database, compact_model.model_signature)

    without_ocr = service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=False,
        chunking=TextChunkingConfig(
            max_chars=96,
            max_terms=24,
            overlap_chars=0,
            overlap_terms=0,
            min_natural_break_chars=20,
        ),
    )
    assert without_ocr.sources == ("image",)
    assert without_ocr.chunks_staged == 0
    with semantic_database(
        tmp_path / service.SEMANTIC_DATABASE_NAME,
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM text_chunks WHERE item_id=? AND active=1",
                (item.item_id,),
            ).fetchone()[0]
            == 0
        )
    # Model-specific published snapshots remain stable until each model publishes
    # a successor; disabling OCR does not silently rewrite unrelated heads.
    assert has_active_embeddings(
        database,
        service.multilingual_text_model().model_signature,
    )
    assert has_active_embeddings(database, compact_model.model_signature)

    service.index_image_embeddings(tmp_path, embed_ocr_text=True)
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=compact_model,
    )
    assert has_active_embeddings(
        database,
        service.multilingual_text_model().model_signature,
    )
    assert has_active_embeddings(database, compact_model.model_signature)
    record = ImageSourceRecord(item, None)
    absent_ocr = service.index_image_embeddings(tmp_path, embed_ocr_text=True)
    assert absent_ocr.chunks_staged == 0
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM text_chunks WHERE item_id=? AND active=1",
                (item.item_id,),
            ).fetchone()[0]
            == 0
        )
    assert not has_active_embeddings(
        database,
        service.multilingual_text_model().model_signature,
    )
    assert has_active_embeddings(database, compact_model.model_signature)
    compact_cleanup = state.start_embedding_generation(
        database,
        model_signature=compact_model.model_signature,
        processing_signature="fixture-compact-ocr-cleanup",
    )
    state.finalize_embedding_generation(database, compact_cleanup)
    assert not has_active_embeddings(database, compact_model.model_signature)


@pytest.mark.capability('image','inference')
def test_exact_image_replay_skips_stat_enumeration_chunking_and_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "image")
    head = SemanticSourceHead(
        "image",
        "image.sqlite3",
        "fixture-adapter-v1",
        1,
        1,
        "sha256:" + "b" * 64,
        True,
    )
    monkeypatch.setattr(service._image_index, "semantic_source_heads", lambda *_args: (head,))
    record = _image_record(tmp_path, "exact-replay")
    monkeypatch.setattr(service, "iter_image_source_records", lambda _state: iter((record,)))
    baseline = service.index_image_embeddings(tmp_path, embed_ocr_text=True)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("exact image replay must not enumerate or load backends")

    monkeypatch.setattr(service, "iter_image_source_records", unexpected)
    monkeypatch.setattr(service, "_backend", unexpected)
    replay = service.index_image_embeddings(tmp_path, embed_ocr_text=True)

    assert baseline.execution_mode == "enumerated"
    assert replay.execution_mode == "exact_replay"
    assert replay.sources_reused == 2
    assert replay.sources_enumerated == 0
    assert replay.items_staged == replay.chunks_staged == replay.new_jobs_staged == 0


@pytest.mark.capability('image','inference')
def test_image_and_ocr_share_new_job_budget_and_resume_same_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "image")
    record = _image_record(tmp_path, "shared-job-budget")
    monkeypatch.setattr(
        service,
        "iter_image_source_records",
        lambda _state: iter((record,)),
    )

    paused = service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        work_budget=service.SemanticWorkBudget(max_items=1, max_new_jobs=1),
    )

    assert paused.truncated
    assert paused.truncation_reason == "max_new_jobs"
    assert paused.new_jobs_staged == 1
    assert len(paused.generations) == 2
    assert all(result.summary.status == "building" for result in paused.generations)
    assert all(
        result.summary.cursor["enumeration_complete"] is False for result in paused.generations
    )
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    with semantic_database(database, readonly=True) as connection:
        counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT generation_id,COUNT(*) FROM embedding_jobs GROUP BY generation_id"
            )
        }
        assert (
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0] == 0
        )
    assert counts == {paused.generations[0].summary.generation_id: 1}

    resumed = service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        work_budget=service.SemanticWorkBudget(max_items=1, max_new_jobs=1),
    )

    assert resumed.complete
    assert resumed.new_jobs_staged == 1
    assert tuple(result.summary.generation_id for result in resumed.generations) == tuple(
        result.summary.generation_id for result in paused.generations
    )
    assert all(result.summary.status == "ready" for result in resumed.generations)
    with semantic_database(database, readonly=True) as connection:
        counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT generation_id,COUNT(*) FROM embedding_jobs GROUP BY generation_id"
            )
        }
    assert counts == {result.summary.generation_id: 1 for result in resumed.generations}


@pytest.mark.capability('image','inference')
def test_image_deadline_at_end_of_enumeration_preserves_unvisited_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "image")
    records = (
        _image_record(tmp_path, "deadline-visible"),
        _image_record(tmp_path, "deadline-unvisited"),
    )
    monkeypatch.setattr(
        service._image_index, "semantic_source_heads",
        lambda *_args: _fixture_image_heads(records),
    )
    monkeypatch.setattr(
        service,
        "iter_image_source_records",
        lambda _state: iter(records),
    )
    baseline = service.index_image_embeddings(tmp_path, embed_ocr_text=False)
    assert baseline.complete
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    model_signature = service.clip_image_model().model_signature
    with semantic_database(database, readonly=True) as connection:
        baseline_head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (model_signature,),
            ).fetchone()[0]
        )

    records = (
        replace(records[0], ocr_section=TextSection("image_ocr", "ocr", "Changed owner OCR")),
        records[1],
    )
    now = [0.0]
    original_stage = service._image_index.stage_image_batch

    def stage_then_expire(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        now[0] = 20.0
        return staged

    monkeypatch.setattr(
        service._image_index,
        "stage_image_batch",
        stage_then_expire,
    )
    budget = service.SemanticWorkBudget(deadline=10.0, _clock=lambda: now[0])
    paused = service._image_index.index_image_embeddings(
        tmp_path,
        model_cache_override=None,
        local_files_only=True,
        threads=None,
        embed_ocr_text=False,
        ocr_model=None,
        chunking=None,
        backend_factory=lambda model, **_kwargs: _FixtureBackend(model),
        source_record_iterator=lambda _state: iter((records[0],)),
        generation_runner=service._run_generation,
        work_budget=budget,
    )

    assert paused.truncated
    assert paused.truncation_reason == "time_budget"
    assert paused.generations[0].summary.status == "building"
    assert paused.generations[0].summary.cursor["enumeration_complete"] is False
    with semantic_database(database, readonly=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                    (model_signature,),
                ).fetchone()[0]
            )
            == baseline_head
        )
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM semantic_items WHERE source_kind='image' AND active=1"
                ).fetchone()[0]
            )
            == 2
        )

    monkeypatch.setattr(
        service._image_index,
        "stage_image_batch",
        original_stage,
    )
    resumed = service.index_image_embeddings(tmp_path, embed_ocr_text=False)
    assert resumed.complete
    assert resumed.generations[0].summary.generation_id == (
        paused.generations[0].summary.generation_id
    )
    assert resumed.generations[0].summary.generation_id != baseline_head
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT status FROM embedding_generations WHERE generation_id=?",
                (paused.generations[0].summary.generation_id,),
            ).fetchone()[0]
            == "ready"
        )


@pytest.mark.capability('image','inference')
def test_changed_ocr_revision_keeps_other_head_until_its_model_republishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "image")
    image_path = tmp_path / "breaker.png"
    payload = b"stable-visual-payload"
    image_path.write_bytes(payload)
    fingerprint = fingerprint_bytes(payload)
    item = SemanticItem(
        item_id="item:image:ocr-revision",
        source_kind="image",
        source_identity="ocr-revision",
        identity_version="fixture-image-v1",
        fingerprint=fingerprint,
        path=str(image_path),
        source_revision={
            "size": len(payload),
            "mtime_ns": image_path.stat().st_mtime_ns,
            "birthtime_ns": image_path.stat().st_ctime_ns,
            "raw_content_xxh3_128": hashlib.sha256(payload).hexdigest(),
        },
    )
    current_record = [
        ImageSourceRecord(
            item,
            TextSection("image_ocr", "ocr", "Old breaker OCR text."),
        )
    ]
    monkeypatch.setattr(
        service._image_index, "semantic_source_heads",
        lambda *_args: _fixture_image_heads(current_record),
    )
    monkeypatch.setattr(
        service,
        "iter_image_source_records",
        lambda _state: iter(current_record),
    )
    quality_model = service.multilingual_text_model()
    compact_model = compact_multilingual_text_model()
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=quality_model,
    )
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=compact_model,
    )
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    assert has_active_embeddings(database, quality_model.model_signature)
    assert has_active_embeddings(database, compact_model.model_signature)
    assert has_active_embeddings(
        database,
        service.clip_image_model().model_signature,
    )

    current_record[0] = ImageSourceRecord(
        item,
        TextSection("image_ocr", "ocr", "New transformer OCR text."),
    )
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=quality_model,
    )

    with semantic_database(database, readonly=True) as connection:
        active_signatures = connection.execute(
            """SELECT chunking_signature FROM text_chunks
            WHERE item_id=? AND active=1""",
            (item.item_id,),
        ).fetchall()
        revisions = connection.execute(
            """SELECT channel,revision_token FROM text_channel_revisions
            WHERE item_id=?""",
            (item.item_id,),
        ).fetchall()
    assert {str(row[0]) for row in active_signatures} == {
        _fixture_chunking(quality_model).signature
    }
    assert len(revisions) == 1
    assert str(revisions[0][0]) == service.IMAGE_OCR_TEXT_CHANNEL
    assert f"{HASH_ALGORITHM_128}=" in str(revisions[0][1])
    assert has_active_embeddings(database, quality_model.model_signature)
    assert has_active_embeddings(database, compact_model.model_signature)
    assert has_active_embeddings(
        database,
        service.clip_image_model().model_signature,
    )
    with semantic_database(database, readonly=True) as connection:
        prior_compact_head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (compact_model.model_signature,),
            ).fetchone()[0]
        )
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=compact_model,
    )
    with semantic_database(database, readonly=True) as connection:
        current_compact_head = int(
            connection.execute(
                "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
                (compact_model.model_signature,),
            ).fetchone()[0]
        )
    assert current_compact_head != prior_compact_head


@pytest.mark.capability('image','inference')
def test_visual_fingerprint_change_preserves_same_revision_ocr_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "image")
    image_path = tmp_path / "same-ocr.png"

    def image_item(payload: bytes) -> SemanticItem:
        image_path.write_bytes(payload)
        fingerprint = fingerprint_bytes(payload)
        stat_result = image_path.stat()
        return SemanticItem(
            item_id="item:image:same-ocr",
            source_kind="image",
            source_identity="same-ocr",
            identity_version="fixture-image-v1",
            fingerprint=fingerprint,
            path=str(image_path),
            source_revision={
                "size": len(payload),
                "mtime_ns": stat_result.st_mtime_ns,
                "birthtime_ns": stat_result.st_ctime_ns,
                "raw_content_xxh3_128": hashlib.sha256(payload).hexdigest(),
            },
        )

    ocr_section = TextSection(
        "image_ocr",
        "ocr",
        "Unchanged OCR text for transformer maintenance.",
    )
    current_record = [ImageSourceRecord(image_item(b"visual-v1"), ocr_section)]
    monkeypatch.setattr(
        service,
        "iter_image_source_records",
        lambda _state: iter(current_record),
    )
    quality_model = service.multilingual_text_model()
    compact_model = compact_multilingual_text_model()
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=quality_model,
    )
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=compact_model,
    )
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    assert has_active_embeddings(database, quality_model.model_signature)
    assert has_active_embeddings(database, compact_model.model_signature)

    current_record[0] = ImageSourceRecord(image_item(b"visual-v2"), ocr_section)
    service.index_image_embeddings(
        tmp_path,
        embed_ocr_text=True,
        ocr_model=quality_model,
    )

    with semantic_database(database, readonly=True) as connection:
        active_chunks = connection.execute(
            """SELECT chunking_signature FROM text_chunks
            WHERE item_id=? AND active=1""",
            (current_record[0].item.item_id,),
        ).fetchall()
    assert {str(row[0]) for row in active_chunks} == {
        _fixture_chunking(quality_model).signature,
        _fixture_chunking(compact_model).signature,
    }
    assert has_active_embeddings(database, quality_model.model_signature)
    assert has_active_embeddings(database, compact_model.model_signature)
    assert has_active_embeddings(
        database,
        service.clip_image_model().model_signature,
    )


# endregion [02]


# region [03] Retrieval and advisory ontology evidence


def test_resolved_hit_preserves_legacy_positional_mapping_arguments() -> None:
    hit = SearchHit(
        ref_id=1,
        entity_id="entity",
        item_id="item",
        indexed_model_signature="model",
        vector_space="space",
        modality=EmbeddingModality.TEXT,
        score=1.0,
        generation_id=1,
    )
    resolved = ResolvedSearchHit(
        hit,
        None,
        "pdf",
        "identity",
        None,
        None,
        None,
        None,
        None,
        {"revision": 1},
        {"section": 1},
    )

    assert resolved.source_revision == {"revision": 1}
    assert resolved.section_provenance == {"section": 1}
    assert resolved.source_status is None


def _calibrated_ranking_hit(
    ref_id: int,
    *,
    source_kind: str,
    score: float,
    backend: str = "fastembed",
) -> tuple[SearchHit, ResolvedSearchHit]:
    model = service.multilingual_text_model()
    hit = SearchHit(
        ref_id=ref_id,
        entity_id=f"chunk:{ref_id}",
        item_id=f"item:{source_kind}:{ref_id}",
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        modality=EmbeddingModality.TEXT,
        score=score,
        generation_id=7,
        provenance={
            "backend": backend,
            "pipeline": service.SEMANTIC_PIPELINE_VERSION,
        },
        query_model_signature=model.model_signature,
    )
    return hit, ResolvedSearchHit(
        hit=hit,
        path=f"C:/fixtures/{source_kind}-{ref_id}",
        source_kind=source_kind,
        source_identity=f"{source_kind}-{ref_id}",
        section_kind="content",
        section_id=str(ref_id),
        start_char=0,
        end_char=10,
        snippet="fixture",
    )


def _calibrated_image_ranking_hit(
    ref_id: int,
    *,
    score: float,
    backend: str = "semantic-service-fixture",
) -> tuple[SearchHit, ResolvedSearchHit]:
    indexed_model = service.clip_image_model()
    hit = SearchHit(
        ref_id=ref_id,
        entity_id=f"image:{ref_id}",
        item_id=f"item:image:{ref_id}",
        indexed_model_signature=indexed_model.model_signature,
        vector_space=indexed_model.vector_space,
        modality=EmbeddingModality.IMAGE,
        score=score,
        generation_id=9,
        provenance={
            "backend": backend,
            "pipeline": service.SEMANTIC_PIPELINE_VERSION,
        },
        query_model_signature=service.clip_text_model().model_signature,
    )
    return hit, ResolvedSearchHit(
        hit=hit,
        path=f"C:/fixtures/image-{ref_id}.png",
        source_kind="image",
        source_identity=f"image-{ref_id}",
        section_kind=None,
        section_id=None,
        start_char=None,
        end_char=None,
        snippet=None,
    )


def test_image_retrieval_calibration_filters_floor_and_unknown_contracts_closed() -> None:
    kept_hit, kept_resolved = _calibrated_image_ranking_hit(1, score=0.61)
    low_hit, low_resolved = _calibrated_image_ranking_hit(2, score=0.59)
    unknown_hit, unknown_resolved = _calibrated_image_ranking_hit(
        3,
        score=0.95,
        backend="unknown-backend",
    )
    ranking = service.SemanticRanking(
        name="semantic_image",
        hits=(kept_hit, low_hit, unknown_hit),
        resolved=(kept_resolved, low_resolved, unknown_resolved),
        scanned=3,
        complete=True,
    )

    calibrated = search_implementation.apply_image_retrieval_calibration(
        ranking,
        calibration=_fixture_image_calibration(minimum_score=0.60),
    )

    assert calibrated.hits == (kept_hit,)
    assert calibrated.resolved == (kept_resolved,)
    metadata = calibrated.provenance["retrieval_abstention"]
    assert metadata["status"] == "applied"
    assert metadata["rejected_by_reason"] == {
        "below_calibrated_score_floor": 1,
        "backend_not_calibrated": 1,
    }
    assert metadata["query_abstained"] is False


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("minimum_score", 1.1, "minimum_score"),
        ("positive_queries", 0, "positive_queries"),
        ("negative_queries", 0, "negative_queries"),
        ("sample_items", 0, "sample_items"),
    ),
)
def test_image_retrieval_calibration_requires_measured_bounded_evidence(
    field: str,
    value: object,
    match: str,
) -> None:
    kwargs = {
        "calibration_signature": "fixture-v1",
        "query_model_signature": service.clip_text_model().model_signature,
        "indexed_model_signature": service.clip_image_model().model_signature,
        "pipeline": service.SEMANTIC_PIPELINE_VERSION,
        "backend": "semantic-service-fixture",
        "minimum_score": 0.5,
        "positive_queries": 1,
        "negative_queries": 1,
        "sample_items": 1,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=match):
        service.ImageRetrievalCalibration(**kwargs)


def test_text_retrieval_calibration_abstains_below_exact_owner_floors() -> None:
    pdf_hit, pdf_resolved = _calibrated_ranking_hit(
        1,
        source_kind="pdf",
        score=0.419999,
    )
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(pdf_hit,),
        resolved=(pdf_resolved,),
        scanned=1,
        complete=True,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=service.multilingual_text_model(),
    )

    assert calibrated.hits == ()
    assert calibrated.resolved == ()
    metadata = calibrated.provenance["retrieval_abstention"]
    assert metadata["status"] == "applied"
    assert metadata["query_abstained"] is True
    assert metadata["rejected_by_source_kind"] == {"pdf": 1}
    assert metadata["score_floor_by_source_kind"] == {
        "archive": 0.42,
        "audio": 0.42,
        "docx": 0.42,
        "image": 0.42,
        "odt": 0.42,
        "pdf": 0.42,
        "pptx": 0.42,
        "text": 0.42,
        "xlsx": 0.42,
    }
    assert metadata["score_interpretation"].endswith("not_probability")


def test_title_retrieval_uses_the_same_calibrated_abstention_floor() -> None:
    hit, resolved = _calibrated_ranking_hit(
        1,
        source_kind="text",
        score=0.419999,
    )
    ranking = service.SemanticRanking(
        name="semantic_title",
        hits=(hit,),
        resolved=(replace(resolved, section_kind="semantic_metadata_title"),),
        scanned=1,
        complete=True,
        fusion_weight=0.5,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=service.multilingual_text_model(),
    )

    assert calibrated.name == "semantic_title"
    assert calibrated.hits == ()
    assert calibrated.resolved == ()
    assert calibrated.provenance["retrieval_abstention"]["query_abstained"] is True


def test_text_retrieval_calibration_accepts_exact_reused_payload_provenance() -> None:
    hit, resolved = _calibrated_ranking_hit(
        1,
        source_kind="pdf",
        score=0.419999,
    )
    hit = replace(
        hit,
        provenance={
            "reuse": "exact-xxh3-content",
            "payload_provenance": hit.provenance,
        },
    )
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(hit,),
        resolved=(replace(resolved, hit=hit),),
        scanned=1,
        complete=True,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=service.multilingual_text_model(),
    )

    assert calibrated.hits == ()
    metadata = calibrated.provenance["retrieval_abstention"]
    assert metadata["calibrated_hits"] == 1
    assert metadata["query_abstained"] is True


def test_text_retrieval_calibration_does_not_hide_provenance_conflicts() -> None:
    hit, resolved = _calibrated_ranking_hit(
        1,
        source_kind="pdf",
        score=-1.0,
    )
    hit = replace(
        hit,
        provenance={
            **hit.provenance,
            "payload_provenance": {
                "backend": "other-backend",
                "pipeline": service.SEMANTIC_PIPELINE_VERSION,
            },
        },
    )
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(hit,),
        resolved=(replace(resolved, hit=hit),),
        scanned=1,
        complete=True,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=service.multilingual_text_model(),
    )

    assert calibrated.hits == (hit,)
    metadata = calibrated.provenance["retrieval_abstention"]
    assert metadata["calibrated_hits"] == 0
    assert metadata["uncalibrated_by_reason"] == {
        "provenance_contract_conflict": 1,
    }


def test_text_retrieval_calibration_keeps_boundaries_and_unknown_contracts() -> None:
    pdf_hit, pdf_resolved = _calibrated_ranking_hit(
        1,
        source_kind="pdf",
        score=0.42,
    )
    docx_hit, docx_resolved = _calibrated_ranking_hit(
        2,
        source_kind="docx",
        score=-0.25,
    )
    fixture_hit, fixture_resolved = _calibrated_ranking_hit(
        3,
        source_kind="pdf",
        score=-0.50,
        backend="semantic-service-fixture",
    )
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(pdf_hit, docx_hit, fixture_hit),
        resolved=(pdf_resolved, docx_resolved, fixture_resolved),
        scanned=3,
        complete=True,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=service.multilingual_text_model(),
    )

    assert calibrated.hits == (pdf_hit, fixture_hit)
    metadata = calibrated.provenance["retrieval_abstention"]
    assert metadata["status"] == "partial"
    assert metadata["calibrated_hits"] == 2
    assert metadata["uncalibrated_by_reason"] == {
        "backend_not_calibrated": 1,
    }
    assert metadata["rejected_by_source_kind"] == {"docx": 1}
    assert metadata["query_abstained"] is False


def test_compact_text_model_is_not_filtered_by_quality_calibration() -> None:
    hit, resolved = _calibrated_ranking_hit(1, source_kind="pdf", score=-1.0)
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(hit,),
        resolved=(resolved,),
        scanned=1,
        complete=True,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=compact_multilingual_text_model(),
    )

    assert calibrated.hits == (hit,)
    metadata = cast(dict[str, object], calibrated.provenance["retrieval_abstention"])
    assert metadata["status"] == "model_not_calibrated"
    assert metadata["rejected_hits"] == 0
    assert metadata["query_abstained"] is False


def test_lower_order_unknown_model_signature_is_not_calibrated() -> None:
    hit, resolved = _calibrated_ranking_hit(1, source_kind="pdf", score=-1.0)
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(hit,),
        resolved=(resolved,),
        scanned=1,
        complete=True,
    )
    selected_model = replace(
        service.multilingual_text_model(),
        model_signature="0|unregistered-text-model",
    )
    assert selected_model.model_signature < service.multilingual_text_model().model_signature

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=selected_model,
    )

    assert calibrated.hits == (hit,)
    metadata = cast(dict[str, object], calibrated.provenance["retrieval_abstention"])
    assert metadata["status"] == "model_not_calibrated"
    assert metadata["rejected_hits"] == 0


def test_equal_nonidentical_model_signature_is_calibrated() -> None:
    hit, resolved = _calibrated_ranking_hit(1, source_kind="pdf", score=-1.0)
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(hit,),
        resolved=(resolved,),
        scanned=1,
        complete=True,
    )
    expected_model = service.multilingual_text_model()
    equal_signature = expected_model.model_signature.encode().decode()
    assert equal_signature == expected_model.model_signature
    assert equal_signature is not expected_model.model_signature
    selected_model = replace(expected_model, model_signature=equal_signature)

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking,
        selected_model=selected_model,
    )

    assert calibrated.hits == ()
    metadata = cast(dict[str, object], calibrated.provenance["retrieval_abstention"])
    assert metadata["status"] == "applied"
    assert metadata["query_abstained"] is True


def test_text_retrieval_calibration_preserves_keyword_call_contract() -> None:
    ranking = service.SemanticRanking(
        name="semantic_text",
        hits=(),
        resolved=(),
        scanned=0,
        complete=True,
    )

    calibrated = search_implementation.apply_text_retrieval_calibration(
        ranking=ranking,
        selected_model=service.multilingual_text_model(),
    )

    assert calibrated.hits == ()
    assert calibrated.resolved == ()


def test_search_reports_unindexed_space_without_losing_available_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    result = service.search_semantic_index(
        tmp_path,
        "mantenimiento del transformador",
        include_text=True,
        include_title=True,
        include_images=True,
        include_lexical=False,
    )

    assert result.rankings[0].name == "semantic_text"
    assert result.rankings[0].available is True
    assert result.rankings[0].hits
    assert result.rankings[1].name == "semantic_title"
    assert result.rankings[1].available is True
    assert result.rankings[1].hits
    assert result.rankings[2].name == "semantic_image"
    assert result.rankings[2].available is False
    assert result.rankings[2].complete is False
    assert result.rankings[2].unavailable_reason == "clip_models_not_indexed"
    assert result.complete is False
    assert result.fused[0].source_kind == "pdf"


def test_text_search_reuses_one_query_vector_and_fuses_durable_title_at_half_weight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = service.multilingual_text_model()
    backend = _ConstantBackend(model)
    monkeypatch.setattr(service, "_backend", lambda *_args, **_kwargs: backend)
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    backend.requests.clear()

    result = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_title=True,
        include_images=False,
        include_lexical=False,
    )

    assert tuple(ranking.name for ranking in result.rankings) == (
        "semantic_text",
        "semantic_title",
    )
    body, title = result.rankings
    assert body.fusion_weight == 1.0
    assert title.fusion_weight == 0.5
    assert title.provenance["expected_policy_signature"] == ("semantic-content-aware-title-v3")
    assert title.provenance["observed_policy_signatures"] == ["semantic-content-aware-title-v3"]
    assert title.scanned == 1
    assert title.resolved[0].section_kind == "semantic_metadata_title"
    assert [(request.role, request.text) for request in backend.requests] == [
        (EmbeddingRole.QUERY, "transformador")
    ]
    evidence = {value.ranking: value for value in result.fused[0].fused.evidence}
    assert evidence["semantic_title"].contribution == pytest.approx(
        evidence["semantic_text"].contribution / 2.0
    )

    evidence_result = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_title=True,
        include_images=False,
        include_lexical=False,
        evidence_mode=True,
    )
    assert tuple(ranking.name for ranking in evidence_result.rankings) == ("semantic_text",)


def test_empty_requested_semantic_and_lexical_rankings_are_incomplete(
    tmp_path: Path,
) -> None:
    all_rankings = service.search_semantic_index(tmp_path, "transformador")

    assert all_rankings.complete is False
    assert all(not ranking.available for ranking in all_rankings.rankings)
    assert all(not ranking.complete for ranking in all_rankings.rankings)
    assert all(
        ranking.availability is not LexicalAvailability.AVAILABLE
        for ranking in all_rankings.lexical_rankings
    )
    assert not (tmp_path / service.SEMANTIC_DATABASE_NAME).exists()

    lexical_only = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=False,
        include_images=False,
        include_lexical=True,
    )
    assert lexical_only.rankings == ()
    assert lexical_only.complete is False


def test_semantic_vector_cap_exposes_incomplete_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    records = _text_records(4)
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter(records),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    result = service.search_semantic_index(
        tmp_path,
        "transformador",
        limit=1,
        max_vectors=1,
        include_text=True,
        include_images=False,
        include_lexical=False,
    )

    ranking = result.rankings[0]
    assert ranking.available is True
    assert ranking.complete is False
    assert ranking.scanned == 1
    assert ranking.cutoff_reason == "max_vectors_reached"
    assert ranking.next_cursor is not None
    assert result.complete is False


def test_semantic_top_k_cutoff_is_observable_without_marking_scan_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    records = _text_records(4)
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter(records),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    result = service.search_semantic_index(
        tmp_path,
        "transformador",
        limit=1,
        max_vectors=100,
        include_text=True,
        include_images=False,
        include_lexical=False,
    )

    ranking = result.rankings[0]
    assert ranking.complete is True
    assert ranking.scanned == 4
    assert len(ranking.hits) == 3
    assert ranking.cutoff_reason == "top_k"
    assert ranking.next_cursor is None
    assert ranking.cutoff_score == ranking.hits[-1].score
    assert result.complete is True


def test_explicit_candidate_limit_is_honored_without_multiplication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    records = _text_records(4)
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter(records),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    result = service.search_semantic_index(
        tmp_path,
        "transformador",
        limit=1,
        candidate_limit=2,
        max_vectors=100,
        include_text=True,
        include_images=False,
        include_lexical=False,
    )

    ranking = result.rankings[0]
    assert ranking.complete is True
    assert ranking.scanned == 4
    assert len(ranking.hits) == 2
    assert ranking.cutoff_reason == "top_k"
    assert ranking.cutoff_score == ranking.hits[-1].score
    assert len(result.fused) == 1


def test_semantic_database_override_uses_the_exact_generation_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    indexed_state = tmp_path / "indexed-state"
    indexed_state.mkdir()
    _declare_source_state(indexed_state, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    service.index_text_embeddings(indexed_state, source_kinds=("pdf",))
    generated_database = tmp_path / "semantic-generation-000042.sqlite3"
    (indexed_state / service.SEMANTIC_DATABASE_NAME).replace(generated_database)
    query_state = tmp_path / "query-state"

    result = service.search_semantic_index(
        query_state,
        "transformador",
        include_text=True,
        include_images=False,
        include_lexical=False,
        semantic_database=generated_database,
    )

    assert not (query_state / service.SEMANTIC_DATABASE_NAME).exists()
    assert result.rankings[0].available is True
    assert result.rankings[0].hits
    assert result.fused[0].source_identity == "fixture-pdf"


@pytest.mark.parametrize("candidate_limit", (True, 0, 1_001, "2"))
def test_semantic_candidate_limit_rejects_invalid_values(
    tmp_path: Path,
    candidate_limit,
) -> None:
    with pytest.raises(ValueError, match="semantic candidate_limit"):
        service.search_semantic_index(
            tmp_path,
            "transformador",
            candidate_limit=candidate_limit,
            include_text=True,
            include_images=False,
            include_lexical=False,
        )


@pytest.mark.parametrize(
    ("query", "message"),
    (
        (None, "semantic query must be a string"),
        (7, "semantic query must be a string"),
        ("   ", "semantic query cannot be blank"),
        ("transformador\x00", "semantic query cannot contain control"),
        ("transformador\nmantenimiento", "semantic query cannot contain control"),
        ("x" * (MAX_QUERY_CHARS + 1), "cannot exceed 4096 characters"),
    ),
)
def test_public_semantic_search_rejects_invalid_query_with_controlled_error(
    tmp_path: Path,
    query: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        service.search_semantic_index(
            tmp_path,
            cast(str, query),
            include_text=True,
            include_images=False,
            include_lexical=False,
        )

    assert not (tmp_path / service.SEMANTIC_DATABASE_NAME).exists()


@pytest.mark.parametrize("limit", (True, False, 0, 1_001, 1.5, "2", None))
def test_public_semantic_search_rejects_invalid_limit_with_controlled_error(
    tmp_path: Path,
    limit: object,
) -> None:
    with pytest.raises(ValueError, match="semantic search limit"):
        service.search_semantic_index(
            tmp_path,
            "transformador",
            limit=cast(int, limit),
            include_text=True,
            include_images=False,
            include_lexical=False,
        )


@pytest.mark.parametrize(
    "max_vectors",
    (True, False, 0, 10_000_001, 1.5, "2", None),
)
def test_public_semantic_search_rejects_invalid_vector_limit_with_controlled_error(
    tmp_path: Path,
    max_vectors: object,
) -> None:
    with pytest.raises(ValueError, match="semantic max_vectors"):
        service.search_semantic_index(
            tmp_path,
            "transformador",
            max_vectors=cast(int, max_vectors),
            include_text=True,
            include_images=False,
            include_lexical=False,
        )


@pytest.mark.parametrize(
    ("limit", "max_vectors"),
    ((1, 1), (1_000, 10_000_000)),
)
def test_public_semantic_search_accepts_inclusive_query_and_integer_bounds(
    tmp_path: Path,
    limit: int,
    max_vectors: int,
) -> None:
    result = service.search_semantic_index(
        tmp_path,
        "x" * MAX_QUERY_CHARS,
        limit=limit,
        max_vectors=max_vectors,
        include_text=True,
        include_images=False,
        include_lexical=False,
    )

    assert result.rankings[0].unavailable_reason == "semantic_index_missing"


def test_search_orchestration_freezes_order_limits_provenance_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    database = tmp_path / "generations" / "semantic-000042.sqlite3"
    cache = tmp_path / "model-cache"
    lexical_paths = LexicalStatePaths(
        pdf=tmp_path / "pdf.sqlite3",
        docx=tmp_path / "docx.sqlite3",
        office=tmp_path / "office.sqlite3",
        audio=tmp_path / "audio.sqlite3",
    )
    model = service.multilingual_text_model()
    body = service.SemanticRanking(
        name="semantic_text",
        hits=(),
        resolved=(),
        scanned=3,
        complete=True,
        provenance={"channel": "source_content"},
    )
    title = service.SemanticRanking(
        name="semantic_title",
        hits=(),
        resolved=(),
        scanned=2,
        complete=True,
        provenance={"channel": "durable_title"},
    )
    image = service.SemanticRanking(
        name="semantic_image",
        hits=(),
        resolved=(),
        scanned=1,
        complete=False,
        provenance={"channel": "image"},
    )

    def cancellation_check() -> None:
        events.append("cancel")

    def text_rankings(selected_database: Path, **kwargs):
        events.append("text")
        assert selected_database is database
        assert kwargs == {
            "database_exists": False,
            "selected_model": model,
            "query": "transformador",
            "cache": cache,
            "local_files_only": False,
            "threads": 3,
            "limit": 7,
            "max_vectors": 11,
            "backend_factory": service._backend,
            "evidence_mode": True,
            "include_title": True,
            "cancellation_check": cancellation_check,
        }
        return body, title

    def image_ranking(selected_database: Path, **kwargs):
        events.append("image")
        assert selected_database is database
        assert kwargs["query"] == "transformador"
        assert kwargs["cache"] is cache
        assert kwargs["limit"] == 7
        assert kwargs["max_vectors"] == 11
        assert kwargs["evidence_mode"] is True
        assert kwargs["cancellation_check"] is cancellation_check
        return image

    def lexical_search(paths, query, *, limit, cancellation_check=None):
        events.append("lexical")
        assert paths is lexical_paths
        assert query == "transformador"
        assert limit == 6
        assert cancellation_check is not None
        return ()

    def resolve_fused(rankings, lexical_rankings, *, limit):
        events.append("fuse")
        assert tuple(rankings) == (body, title, image)
        assert tuple(lexical_rankings) == ()
        assert limit == 2
        return ()

    monkeypatch.setattr(search_implementation, "text_search_rankings", text_rankings)
    monkeypatch.setattr(search_implementation, "image_search_ranking", image_ranking)
    monkeypatch.setattr(search_implementation, "_resolve_fused_hits", resolve_fused)
    monkeypatch.setattr(service, "search_lexical_sources", lexical_search)

    result = service.search_semantic_index(
        tmp_path,
        "  transformador  ",
        limit=2,
        candidate_limit=7,
        max_vectors=11,
        include_text=True,
        include_title=True,
        include_images=True,
        include_lexical=True,
        lexical_paths=lexical_paths,
        semantic_database=database,
        text_model=model,
        model_cache=cache,
        local_files_only=False,
        threads=3,
        evidence_mode=True,
        cancellation_check=cancellation_check,
    )

    assert events == ["cancel", "text", "image", "cancel", "lexical", "cancel", "fuse"]
    assert result.rankings == (body, title, image)
    assert result.lexical_rankings == ()
    assert result.fused == ()
    assert not database.exists()
    assert not cache.exists()


def _fixture_lexical_ranking(tmp_path: Path) -> LexicalRanking:
    lexical_hit = SearchHit(
        ref_id=1,
        entity_id="fts:pdf:fixture-pdf:1",
        item_id="item:pdf:fixture-pdf",
        indexed_model_signature=LEXICAL_MODEL_SIGNATURE,
        vector_space="lexical:fts5:pdf:v1",
        modality=EmbeddingModality.TEXT,
        score=1.0,
        generation_id=0,
    )
    lexical_resolved = ResolvedSearchHit(
        hit=lexical_hit,
        path="C:/fixtures/transformador.pdf",
        source_kind="pdf",
        source_identity="fixture-pdf",
        section_kind="page",
        section_id="1",
        start_char=None,
        end_char=None,
        snippet="mantenimiento de transformador",
    )
    lexical = LexicalRanking(
        source_kind="pdf",
        state_path=tmp_path / "pdf.sqlite3",
        availability=LexicalAvailability.AVAILABLE,
        normalized_query="transformador",
        hits=(lexical_resolved,),
    )
    return lexical


def test_missing_local_query_model_preserves_lexical_without_cache_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    lexical = _fixture_lexical_ranking(tmp_path)
    monkeypatch.setattr(
        service,
        "search_lexical_sources",
        lambda _paths, _query, *, limit: (lexical,),
    )
    missing_cache = tmp_path / "missing-fastembed-cache"

    def unexpected_backend(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("FastEmbedBackend must not run when its cache is absent")

    monkeypatch.setattr(semantic_preparation, "FastEmbedBackend", unexpected_backend)
    monkeypatch.setattr(service, "_backend", semantic_preparation.backend)
    result = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_images=False,
        include_lexical=True,
        model_cache=missing_cache,
    )

    assert result.rankings[0].available is False
    assert result.rankings[0].unavailable_reason == "semantic_model_cache_missing"
    assert result.lexical_rankings == (lexical,)
    assert result.fused[0].source_identity == "fixture-pdf"
    assert not missing_cache.exists()


def test_unloadable_cached_model_is_optional_and_preserves_lexical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.semantic.semantic_backends import BackendAvailability

    _patch_backend(monkeypatch)
    monkeypatch.setattr(
        semantic_preparation, "fastembed_availability",
        lambda: BackendAvailability(True, "0.8.0", ("CPUExecutionProvider",), "fixture"),
    )
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    lexical = _fixture_lexical_ranking(tmp_path)
    monkeypatch.setattr(
        service,
        "search_lexical_sources",
        lambda _paths, _query, *, limit: (lexical,),
    )
    model = service.multilingual_text_model()
    contract = fastembed_cache_contract(model.model_signature)
    cache = tmp_path / "corrupt-fastembed-cache"
    repository = cache / ("models--" + contract.repository_id.replace("/", "--"))
    commit = "a" * 40
    reference = repository / "refs" / "main"
    reference.parent.mkdir(parents=True)
    reference.write_text(commit, encoding="ascii")
    snapshot = repository / "snapshots" / commit
    for relative_path in contract.required_files:
        cached_file = snapshot.joinpath(*relative_path.split("/"))
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(b"corrupt-cached-model-fixture")

    class CorruptCachedBackend(_FixtureBackend):
        def embed(
            self,
            requests: Sequence[EmbeddingRequest],
        ) -> Sequence[BackendEmbedding]:
            del requests
            raise OSError("cached model cannot be loaded")

    monkeypatch.setattr(
        semantic_preparation,
        "FastEmbedBackend",
        lambda selected_model, **_kwargs: CorruptCachedBackend(selected_model),
    )
    monkeypatch.setattr(service, "_backend", semantic_preparation.backend)
    result = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_images=False,
        include_lexical=True,
        model_cache=cache,
    )

    assert result.rankings[0].available is False
    assert result.rankings[0].unavailable_reason == ("semantic_query_model_unloadable")
    assert result.lexical_rankings == (lexical,)
    assert result.fused[0].source_identity == "fixture-pdf"


def test_public_search_propagates_cancellation_into_exact_vector_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    original_search = search_implementation.search_exact_page
    entered_repository = False

    def observing_search(*args, **kwargs):
        nonlocal entered_repository
        entered_repository = True
        return original_search(*args, **kwargs)

    monkeypatch.setattr(
        search_implementation,
        "search_exact_page",
        observing_search,
    )

    class SearchCancelled(Exception):
        pass

    def cancellation_check() -> None:
        if entered_repository:
            raise SearchCancelled

    with pytest.raises(SearchCancelled):
        service.search_semantic_index(
            tmp_path,
            "transformador",
            include_text=True,
            include_images=False,
            include_lexical=False,
            cancellation_check=cancellation_check,
        )
    assert entered_repository


def test_public_search_passes_cancellation_into_lexical_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_lexical = False

    class SearchCancelled(RuntimeError):
        pass

    def cancellation_check() -> None:
        if entered_lexical:
            raise SearchCancelled

    def lexical_search(
        _paths,
        _query,
        *,
        limit,
        cancellation_check=None,
    ):
        nonlocal entered_lexical
        del limit
        entered_lexical = True
        assert cancellation_check is not None
        cancellation_check()
        return ()

    monkeypatch.setattr(service, "search_lexical_sources", lexical_search)
    with pytest.raises(SearchCancelled):
        service.search_semantic_index(
            tmp_path,
            "transformador",
            include_text=False,
            include_images=False,
            include_lexical=True,
            cancellation_check=cancellation_check,
        )
    assert entered_lexical


def test_registered_model_without_active_vectors_is_reported_unindexed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = service.multilingual_text_model()
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    service._initialize_models(database, (model,))
    monkeypatch.setattr(
        service,
        "_backend",
        lambda *_args, **_kwargs: pytest.fail(
            "an empty registered model must not initialize an inference backend"
        ),
    )

    search = service.search_semantic_index(
        tmp_path,
        "transformador",
        include_text=True,
        include_images=False,
        include_lexical=False,
        text_model=model,
    )
    classification = service.classify_semantic_index(
        tmp_path,
        include_text=True,
        include_images=False,
        text_model=model,
    )

    assert search.rankings[0].available is False
    assert search.rankings[0].unavailable_reason == "text_model_not_indexed"
    assert classification.passes == ()
    assert classification.skipped == {"text": "text_model_not_indexed"}


def test_missing_semantic_index_preserves_lexical_ranking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hit = SearchHit(
        ref_id=1,
        entity_id="fts:pdf:fixture-pdf:1",
        item_id="item:pdf:fixture-pdf",
        indexed_model_signature=LEXICAL_MODEL_SIGNATURE,
        vector_space="lexical:fts5:pdf:v1",
        modality=EmbeddingModality.TEXT,
        score=1.0,
        generation_id=0,
    )
    resolved = ResolvedSearchHit(
        hit=hit,
        path="C:/fixtures/transformador.pdf",
        source_kind="pdf",
        source_identity="fixture-pdf",
        section_kind="page",
        section_id="1",
        start_char=None,
        end_char=None,
        snippet="mantenimiento de transformador",
    )
    lexical = LexicalRanking(
        source_kind="pdf",
        state_path=tmp_path / "pdf.sqlite3",
        availability=LexicalAvailability.AVAILABLE,
        normalized_query="transformador",
        hits=(resolved,),
    )
    monkeypatch.setattr(
        service,
        "search_lexical_sources",
        lambda _paths, _query, *, limit: (lexical,),
    )

    result = service.search_semantic_index(tmp_path, "transformador")

    assert not (tmp_path / service.SEMANTIC_DATABASE_NAME).exists()
    assert tuple(ranking.unavailable_reason for ranking in result.rankings) == (
        "semantic_index_missing",
        "semantic_index_missing",
    )
    assert result.lexical_rankings == (lexical,)
    assert result.fused[0].source_identity == "fixture-pdf"


def test_semantic_status_handles_uri_fragment_character_in_path(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "semantic#state"
    database = state_directory / service.SEMANTIC_DATABASE_NAME
    service._initialize_models(database, ())

    status = service.semantic_status(state_directory)

    assert status.exists is True
    assert status.schema_version == state.SEMANTIC_SCHEMA_VERSION
    assert status.counts["semantic_items"] == 0
    assert status.counts["text_channel_revisions"] == 0


def test_semantic_status_reads_v4_state_without_migrating_or_missing_table_error(
    tmp_path: Path,
) -> None:
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    with semantic_database(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state._migrate_to_v1(connection, 1)
        state._migrate_to_v2(connection, 2)
        state._migrate_to_v3(connection, 3)
        state._migrate_to_v4(connection, 4)
        connection.execute("PRAGMA user_version=4")
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','4')")

    status = service.semantic_status(tmp_path)

    assert status.schema_version == 4
    assert status.counts["text_channel_revisions"] == 0
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name='text_channel_revisions'"""
            ).fetchone()[0]
            == 0
        )


@pytest.mark.capability('inference')
def test_classification_persists_only_uncalibrated_advisory_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_backend",
        lambda model, **_kwargs: _ConstantBackend(model),
    )
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    selected_concepts = (
        CONCEPTS["industrial.equipment.transformer"],
        CONCEPTS["industrial.activity.maintenance"],
    )
    monkeypatch.setattr(
        service,
        "_classification_concepts",
        lambda _modality: selected_concepts,
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    result = service.classify_semantic_index(
        tmp_path,
        include_text=True,
        include_images=False,
        max_evidence_per_entity=2,
        page_size=1,
    )
    evidence = list_semantic_evidence(
        tmp_path / service.SEMANTIC_DATABASE_NAME,
        item_id="item:pdf:fixture-pdf",
        ontology_id=service.SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
    )

    assert result.skipped == {}
    assert len(result.passes) == 1
    assert result.passes[0].prototypes == 2
    assert result.passes[0].entities_scored == 1
    assert result.passes[0].entities_abstained == 0
    assert result.passes[0].evidence_staged == 2
    assert len(evidence) == 2
    assert {value.concept_id for value in evidence} == {
        concept.concept_id for concept in selected_concepts
    }
    assert all(value.disposition is EvidenceDisposition.ADVISORY for value in evidence)
    assert all(value.calibration_status.value == "uncalibrated" for value in evidence)


def test_prototype_preparation_reuses_complete_active_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected_concepts = (
        CONCEPTS["industrial.equipment.transformer"],
        CONCEPTS["industrial.activity.maintenance"],
    )
    monkeypatch.setattr(
        service,
        "_classification_concepts",
        lambda _modality: selected_concepts,
    )
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    model = service.multilingual_text_model()
    service._initialize_models(database, (model,))
    backend = _ConstantBackend(model)

    first = service._prepare_label_prototypes(
        database,
        backend,
        target_modality=model.modality,
    )
    first_request_count = len(backend.requests)
    second = service._prepare_label_prototypes(
        database,
        backend,
        target_modality=model.modality,
    )

    assert first_request_count == len(selected_concepts)
    assert len(backend.requests) == first_request_count
    assert second == first


@pytest.mark.capability('inference')
def test_classification_abstains_from_unsupported_negative_scores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_backend",
        lambda model, **_kwargs: _ConstantBackend(model, oppose_queries=True),
    )
    _declare_source_state(tmp_path, "pdf")
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((_text_record(),)),
    )
    selected_concepts = (
        CONCEPTS["industrial.equipment.transformer"],
        CONCEPTS["industrial.activity.maintenance"],
    )
    monkeypatch.setattr(
        service,
        "_classification_concepts",
        lambda _modality: selected_concepts,
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    result = service.classify_semantic_index(
        tmp_path,
        include_text=True,
        include_images=False,
        max_evidence_per_entity=2,
        page_size=1,
    )
    evidence = list_semantic_evidence(
        tmp_path / service.SEMANTIC_DATABASE_NAME,
        item_id="item:pdf:fixture-pdf",
        ontology_id=service.SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
    )

    assert result.passes[0].entities_scored == 1
    assert result.passes[0].entities_abstained == 1
    assert result.passes[0].evidence_staged == 0
    assert evidence == ()


@pytest.mark.capability('inference')
def test_interrupted_evidence_refresh_keeps_published_entity_coherent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_backend",
        lambda model, **_kwargs: _ConstantBackend(model),
    )
    _declare_source_state(tmp_path, "pdf")
    first = _text_record()
    second_text = "Inspección termográfica de interruptores de potencia."
    second_item = SemanticItem(
        item_id="item:pdf:fixture-pdf-2",
        source_kind="pdf",
        source_identity="fixture-pdf-2",
        identity_version="fixture-source-v1",
        fingerprint=fingerprint_text("fixture-pdf-source-2"),
        path="C:/fixtures/interruptor.pdf",
        provenance={"fixture": True},
    )
    second = TextSourceRecord(
        second_item,
        TextSection("pdf_page", "1", second_text, {"fixture": True}),
    )
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((first, second)),
    )
    selected_concepts = (
        CONCEPTS["industrial.equipment.transformer"],
        CONCEPTS["industrial.activity.maintenance"],
    )
    monkeypatch.setattr(
        service,
        "_classification_concepts",
        lambda _modality: selected_concepts,
    )
    service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    service.classify_semantic_index(
        tmp_path,
        include_text=True,
        include_images=False,
        max_evidence_per_entity=2,
        page_size=1,
    )
    database = tmp_path / service.SEMANTIC_DATABASE_NAME
    assert (
        len(
            list_semantic_evidence(
                database,
                item_id=first.item.item_id,
                ontology_id=service.SEMANTIC_ONTOLOGY_ID,
                ontology_version=ONTOLOGY_VERSION,
            )
        )
        == 2
    )
    assert (
        len(
            list_semantic_evidence(
                database,
                item_id=second.item.item_id,
                ontology_id=service.SEMANTIC_ONTOLOGY_ID,
                ontology_version=ONTOLOGY_VERSION,
            )
        )
        == 2
    )

    monkeypatch.setattr(
        service,
        "_selected_prototype_indices",
        lambda _scores, _prototypes, _families, _maximum: (0,),
    )
    real_publish = service.publish_semantic_evidence_entities
    publication_calls = 0

    def fail_after_first_publication(*args, **kwargs):
        nonlocal publication_calls
        publication_calls += 1
        if publication_calls == 2:
            raise RuntimeError("deliberate interruption after first evidence page")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(
        service,
        "publish_semantic_evidence_entities",
        fail_after_first_publication,
    )
    with pytest.raises(RuntimeError, match="deliberate interruption"):
        service.classify_semantic_index(
            tmp_path,
            include_text=True,
            include_images=False,
            max_evidence_per_entity=2,
            page_size=1,
        )

    first_evidence = list_semantic_evidence(
        database,
        item_id=first.item.item_id,
        ontology_id=service.SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
    )
    second_evidence = list_semantic_evidence(
        database,
        item_id=second.item.item_id,
        ontology_id=service.SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
    )
    assert publication_calls == 2
    assert len(first_evidence) == 1
    assert len(second_evidence) == 2
    assert {value.concept_id for value in first_evidence}.issubset(
        {value.concept_id for value in second_evidence}
    )


def test_payload_local_embedding_failure_isolated_without_losing_peers() -> None:
    model = service.multilingual_text_model()

    class PayloadLocalBackend(_ConstantBackend):
        def __init__(self, selected_model: EmbeddingModelSpec) -> None:
            super().__init__(selected_model)
            self.calls = 0

        def embed(
            self,
            requests: Sequence[EmbeddingRequest],
        ) -> Sequence[BackendEmbedding]:
            self.calls += 1
            if any(request.request_id == "mutable" for request in requests):
                raise service.SourceRevisionMismatchError("source changed")
            return super().embed(requests)

    backend = PayloadLocalBackend(model)
    requests = tuple(
        EmbeddingRequest(
            request_id=request_id,
            role=EmbeddingRole.QUERY,
            fingerprint=fingerprint_text(request_id),
            text=request_id,
        )
        for request_id in ("stable-1", "mutable", "stable-2")
    )

    successes, failures = service._embed_requests_isolated(backend, requests)

    assert tuple(index for index, _output in successes) == (0, 2)
    assert tuple(index for index, _error in failures) == (1,)
    assert backend.calls > 1


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_systemic_embedding_failure_is_not_bisected(
    error_type: type[Exception],
) -> None:
    model = service.multilingual_text_model()

    class SystemicBackend(_ConstantBackend):
        def __init__(self, selected_model: EmbeddingModelSpec) -> None:
            super().__init__(selected_model)
            self.calls = 0

        def embed(
            self,
            _requests: Sequence[EmbeddingRequest],
        ) -> Sequence[BackendEmbedding]:
            self.calls += 1
            raise error_type("systemic failure")

    backend = SystemicBackend(model)
    requests = tuple(
        EmbeddingRequest(
            request_id=f"request-{index}",
            role=EmbeddingRole.QUERY,
            fingerprint=fingerprint_text(f"request-{index}"),
            text=f"request-{index}",
        )
        for index in range(3)
    )

    successes, failures = service._embed_requests_isolated(backend, requests)

    assert successes == ()
    assert tuple(index for index, _error in failures) == (0, 1, 2)
    assert backend.calls == 1


def test_embedding_heartbeat_is_joined_and_surfaces_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model = service.multilingual_text_model()
    text = "relay protection heartbeat"
    request = EmbeddingRequest(
        request_id="1",
        role=EmbeddingRole.PASSAGE,
        fingerprint=fingerprint_text(text),
        text=text,
    )
    lease = EmbeddingJobLease(
        job_id=1,
        generation_id=7,
        model_signature=model.model_signature,
        vector_space=model.vector_space,
        modality=EmbeddingModality.TEXT,
        role=EmbeddingRole.PASSAGE,
        entity_kind=SemanticEntityKind.TEXT_CHUNK,
        entity_id="chunk:1",
        item_id="item:1",
        fingerprint=request.fingerprint,
        attempt=1,
        lease_until_ns=time.time_ns() + 1_000_000_000,
        text=text,
    )

    class SlowBackend(_FixtureBackend):
        def embed(
            self,
            requests: Sequence[EmbeddingRequest],
        ) -> Sequence[BackendEmbedding]:
            time.sleep(0.05)
            return super().embed(requests)

    calls: list[tuple[int, ...]] = []

    def successful_heartbeat(
        _database: Path,
        job_ids: Sequence[int],
        **_kwargs: object,
    ) -> int:
        calls.append(tuple(job_ids))
        return time.time_ns() + 1_000_000_000

    monkeypatch.setattr(service, "heartbeat_embedding_jobs", successful_heartbeat)
    successes, failures = service._embed_requests_with_heartbeat(
        tmp_path / "semantic.sqlite3",
        (lease,),
        worker_id="worker",
        backend=SlowBackend(model),
        requests=(request,),
        lease_seconds=1.0,
        heartbeat_interval_seconds=0.01,
    )
    assert len(successes) == 1
    assert failures == ()
    assert calls
    assert all(value == (lease.job_id,) for value in calls)
    assert not any(
        thread.name.startswith("neocortex-semantic-lease:") for thread in threading.enumerate()
    )

    def failed_heartbeat(
        _database: Path,
        _job_ids: Sequence[int],
        **_kwargs: object,
    ) -> int:
        raise state.SemanticStateError("lease disappeared")

    monkeypatch.setattr(service, "heartbeat_embedding_jobs", failed_heartbeat)
    with pytest.raises(RuntimeError, match="heartbeat failed"):
        service._embed_requests_with_heartbeat(
            tmp_path / "semantic.sqlite3",
            (lease,),
            worker_id="worker",
            backend=SlowBackend(model),
            requests=(request,),
            lease_seconds=1.0,
            heartbeat_interval_seconds=0.01,
        )
    assert not any(
        thread.name.startswith("neocortex-semantic-lease:") for thread in threading.enumerate()
    )


# endregion [03]
