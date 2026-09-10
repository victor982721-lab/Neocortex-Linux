"""Evidence-key fusion and real owner reuse for Knowledge search."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.knowledge import knowledge_search as knowledge_search_module
from neocortex.semantic import semantic_preparation, semantic_service
from neocortex.capabilities.formats.archive.state import (
    archive_database,
    initialize_archive_state,
)
from neocortex.code.code_contracts import (
    CodeRelationEndpoint,
    CodeRouteConfig,
    CodeSearchHit,
    CodeSearchQuery,
    CodeSearchRelation,
)
from neocortex.code.ingestion.code_detection import DETECTOR_VERSION
from neocortex.code.code_route import CodeRoute
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    PhysicalIdentityRef,
    PublicationHead,
    RankingSignal,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    RetrievalMode,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search_contracts import (
    ResourceDiscoverySignal,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
    _candidate_from_resolved,
    execute_knowledge_search,
    fuse_evidence_rankings,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.capabilities.formats.video.models import VideoMediaProbe, VideoStreamProbe
from neocortex.capabilities.formats.video.state import (
    VideoFrameEvidence,
    initialize_video_state,
    store_video_success,
    video_database,
)
from neocortex.semantic.semantic_chunking import TextChunkingConfig
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    ResolvedSearchHit,
    SearchHit,
    fingerprint_text,
)
from tests.semantic_test_backend import DeterministicTestBackend
# endregion [01]

# region [02] Implementación


def _snapshot(*owners: OwnerSnapshot) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T01:02:03Z",
        captured_monotonic_ns=1,
        owners=owners,
    )


def _file_snapshot(path: Path) -> FileSnapshot:
    observed = path.stat()
    return FileSnapshot(
        str(path),
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )


def test_knowledge_search_returns_timestamped_video_ocr_evidence(tmp_path: Path) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path)
    initialize_video_state(paths.video)
    source = tmp_path / "Case.mp4"
    source.write_bytes(b"video fixture")
    file = _file_snapshot(source)
    with video_database(paths.video, create=False) as connection:
        store_video_success(
            connection,
            file,
            "video/mp4",
            "fixture-video-v1",
            VideoMediaProbe(
                5.0,
                "mp4",
                (VideoStreamProbe(0, "h264", 10, 10, 1.0, 5.0, None),),
                0,
                (),
                0,
            ),
            (
                VideoFrameEvidence(
                    0,
                    2500,
                    ("interval",),
                    10,
                    10,
                    "a" * 32,
                    True,
                    "MALPASO transformer plate",
                    95.0,
                    "fixture",
                ),
            ),
            (),
            None,
            1,
        )
        connection.commit()
    snapshot = _snapshot(OwnerSnapshot("video", OwnerAvailability.AVAILABLE, 2, 2))
    plan = plan_knowledge_query(KnowledgeQuery("MALPASO", source_kinds=("video",), limit=5))

    result = execute_knowledge_search(paths, plan, snapshot)

    assert any(hit.resource.owner == "video" for hit in result.hits)
    video = next(hit for hit in result.hits if hit.resource.owner == "video")
    assert video.evidence.start_ms == 2500
    assert video.evidence.end_ms == 2501
    assert video.evidence.section_kind == "video_frame_ocr"
    assert video.evidence.identifiers == (("neocortex.video.timestamp", "00:00:02.500"),)
    assert video.resource.physical_identity is not None
    assert video.revision.processing_signature == "fixture-video-v1"


class _CodeInventory:
    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot

    def snapshots(self, _scan_id: int) -> Iterator[FileSnapshot]:
        return iter((self.snapshot,))


class _CodeFramework:
    def begin_route_phase(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def complete_route_phase(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def fail_route_phase(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


def _candidate(
    *,
    evidence_id: str,
    section_id: str,
    start_char: int,
    end_char: int,
    ranking: str,
    source_rank: int,
    disposition: ResourceDisposition = ResourceDisposition.CANONICAL,
    revision_state: RevisionState = RevisionState.CURRENT,
    snippet: str | None = None,
) -> KnowledgeCandidate:
    resource = ResourceRef(
        resource_id="resource:pdf:fixture",
        source_kind="pdf",
        owner="pdf",
        physical_identity=PhysicalIdentityRef("owner_file_key", "fixture", 1),
        current_path="C:/docs/fixture.pdf",
        disposition=disposition,
        canonical_resource_id=(
            "resource:pdf:canonical" if disposition is ResourceDisposition.DUPLICATE else None
        ),
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id="revision:fixture",
        producer="pdf-route",
        processing_signature="pdf-v11:fixture",
        generation=None,
        state=revision_state,
    )
    evidence = EvidenceRef(
        evidence_id=evidence_id,
        resource_id=resource.resource_id,
        revision_id=revision.revision_id,
        method=EvidenceMethod.EXTRACTED,
        page=int(section_id),
        start_char=start_char,
        end_char=end_char,
        section_kind="pdf_page",
        section_id=section_id,
        snippet=snippet or f"evidence {evidence_id}",
    )
    return KnowledgeCandidate(
        resource=resource,
        revision=revision,
        evidence=evidence,
        signal=RankingSignal(ranking, "fixture", 1.0, source_rank),
        reason=f"retrieved by {ranking}",
    )


def _cluster_reranking_candidates() -> dict[str, tuple[KnowledgeCandidate, ...]]:
    leader = _candidate(
        evidence_id="cluster-b",
        section_id="2",
        start_char=0,
        end_char=100,
        ranking="fts_pdf",
        source_rank=1,
    )
    cluster_a = _candidate(
        evidence_id="cluster-a-base",
        section_id="1",
        start_char=0,
        end_char=100,
        ranking="fts_pdf",
        source_rank=2,
    )
    cluster_a_overlap = _candidate(
        evidence_id="cluster-a-overlap",
        section_id="1",
        start_char=50,
        end_char=150,
        ranking="semantic_text",
        source_rank=2,
    )
    return {
        "fts_pdf": (leader, cluster_a),
        "semantic_text": (cluster_a_overlap,),
    }


def test_evidence_fusion_keeps_multiple_sections_and_merges_exact_evidence() -> None:
    first = _candidate(
        evidence_id="page-1",
        section_id="1",
        start_char=0,
        end_char=100,
        ranking="semantic_text",
        source_rank=1,
        snippet="same normalized passage",
    )
    same_first = _candidate(
        evidence_id="page-1",
        section_id="1",
        start_char=0,
        end_char=100,
        ranking="fts_pdf",
        source_rank=2,
        snippet="same   normalized passage",
    )
    overlap = _candidate(
        evidence_id="page-1-overlap",
        section_id="1",
        start_char=50,
        end_char=120,
        ranking="fts_pdf",
        source_rank=1,
    )
    second = _candidate(
        evidence_id="page-2",
        section_id="2",
        start_char=0,
        end_char=80,
        ranking="semantic_text",
        source_rank=2,
    )
    duplicate_content = _candidate(
        evidence_id="same-content-other-id",
        section_id="1",
        start_char=0,
        end_char=100,
        ranking="zz_duplicate",
        source_rank=7,
        snippet="SAME NORMALIZED PASSAGE",
    )

    hits, omitted = fuse_evidence_rankings(
        {
            "fts_pdf": (overlap, same_first),
            "semantic_text": (first, second),
            "zz_duplicate": (duplicate_content,),
        },
        limit=3,
        max_per_resource=3,
        min_section_distance=32,
    )

    assert [hit.evidence.evidence_id for hit in hits] == ["page-1", "page-2"]
    assert {signal.source for signal in hits[0].signals} == {
        "fts_pdf",
        "semantic_text",
        "zz_duplicate",
    }
    # Overlapping evidence is merged into the selected hit, not silently
    # discarded, so it must not be reported as an omitted candidate.
    assert omitted == 0


def test_title_discovery_signal_boosts_only_best_grounded_evidence() -> None:
    best = _candidate(
        evidence_id="page-1",
        section_id="1",
        start_char=0,
        end_char=80,
        ranking="semantic_text",
        source_rank=1,
    )
    other = _candidate(
        evidence_id="page-2",
        section_id="2",
        start_char=0,
        end_char=80,
        ranking="semantic_text",
        source_rank=2,
    )
    title = ResourceDiscoverySignal(
        resource=best.resource,
        revision=best.revision,
        signal=RankingSignal(
            "semantic_title",
            "cosine_similarity",
            0.9,
            1,
            model_signature="title-model",
            generation=7,
            query_model_signature="title-model",
        ),
        reason="resource basename matched the query",
        fusion_weight=0.5,
    )

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (best, other)},
        discovery_signals=(title,),
        limit=2,
        max_per_resource=2,
        min_section_distance=0,
    )

    title_signals = tuple(
        (hit, signal) for hit in hits for signal in hit.signals if signal.source == "semantic_title"
    )
    assert len(hits) == 2
    assert omitted == 0
    assert len(title_signals) == 1
    boosted_hit, title_signal = title_signals[0]
    assert boosted_hit.evidence.evidence_id == "page-1"
    body_signal = next(signal for signal in boosted_hit.signals if signal.source == "semantic_text")
    assert title_signal.contribution == pytest.approx(
        body_signal.contribution * title.fusion_weight
    )
    assert boosted_hit.fused_score == pytest.approx(
        sum(signal.contribution or 0.0 for signal in boosted_hit.signals)
    )


def test_ungrounded_title_discovery_signal_never_creates_a_hit() -> None:
    grounded = _candidate(
        evidence_id="grounded-page",
        section_id="1",
        start_char=0,
        end_char=80,
        ranking="semantic_text",
        source_rank=1,
    )
    unmatched_revision = replace(
        grounded.revision,
        revision_id="revision:title-without-evidence",
    )
    title = ResourceDiscoverySignal(
        resource=grounded.resource,
        revision=unmatched_revision,
        signal=RankingSignal(
            "semantic_title",
            "cosine_similarity",
            0.95,
            1,
        ),
        reason="resource basename matched the query",
        fusion_weight=0.5,
    )

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (grounded,)},
        discovery_signals=(title,),
        limit=2,
        max_per_resource=2,
        min_section_distance=0,
    )

    assert len(hits) == 1
    assert omitted == 0
    assert hits[0].revision == grounded.revision
    assert {signal.source for signal in hits[0].signals} == {"semantic_text"}


def test_overlap_cluster_is_reranked_after_merged_signals() -> None:
    hits, omitted = fuse_evidence_rankings(
        _cluster_reranking_candidates(),
        limit=3,
        max_per_resource=3,
        min_section_distance=0,
    )

    assert [hit.evidence.evidence_id for hit in hits] == [
        "cluster-a-base",
        "cluster-b",
    ]
    assert [hit.rank for hit in hits] == [1, 2]
    assert hits[0].fused_score > hits[1].fused_score
    assert {signal.source for signal in hits[0].signals} == {
        "fts_pdf",
        "semantic_text",
    }
    assert omitted == 0


def test_limit_is_applied_after_overlap_clusters_are_complete() -> None:
    hits, omitted = fuse_evidence_rankings(
        _cluster_reranking_candidates(),
        limit=1,
        max_per_resource=3,
        min_section_distance=0,
    )

    assert len(hits) == 1
    assert hits[0].evidence.evidence_id == "cluster-a-base"
    assert hits[0].rank == 1
    assert {signal.source for signal in hits[0].signals} == {
        "fts_pdf",
        "semantic_text",
    }
    assert omitted == 1


def test_fusion_preserves_owner_rank_after_policy_and_identity_filters() -> None:
    candidate = _candidate(
        evidence_id="deep-owner-hit",
        section_id="1",
        start_char=0,
        end_char=10,
        ranking="fts_pdf",
        source_rank=50,
    )

    hits, omitted = fuse_evidence_rankings(
        {"fts_pdf": (candidate,)},
        limit=5,
        max_per_resource=5,
        min_section_distance=0,
    )

    assert omitted == 0
    assert hits[0].signals[0].source_rank == 50
    assert hits[0].fused_score == pytest.approx(1.0 / 110.0)


def test_common_truncated_snippet_prefix_at_distinct_locators_is_not_merged() -> None:
    shared_prefix = "shared-prefix-" * 20
    first = _candidate(
        evidence_id="chunk-a",
        section_id="1",
        start_char=0,
        end_char=240,
        ranking="semantic_text",
        source_rank=1,
        snippet=f"{shared_prefix}alpha",
    )
    second = _candidate(
        evidence_id="chunk-b",
        section_id="2",
        start_char=240,
        end_char=480,
        ranking="semantic_text",
        source_rank=2,
        snippet=f"{shared_prefix}beta",
    )

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (first, second)},
        limit=5,
        max_per_resource=5,
        min_section_distance=0,
    )

    assert [hit.evidence.evidence_id for hit in hits] == ["chunk-a", "chunk-b"]
    assert omitted == 0


def test_pdf_fts_and_semantic_locators_normalize_into_one_cluster() -> None:
    source_identity = "00000000000000000000000000000001:00000000000000000000000000000002"
    source_revision = {
        "birthtime_ns": 10,
        "processing_signature": "pdf-v11:fixture",
        "last_seen_run_id": 7,
    }
    lexical = _candidate_from_resolved(
        ResolvedSearchHit(
            hit=SearchHit(
                ref_id=1,
                entity_id="lexical-page-7",
                item_id="document",
                indexed_model_signature="fts5-v1",
                vector_space="lexical:fts5:pdf:v1",
                modality=EmbeddingModality.TEXT,
                score=1.0,
                generation_id=0,
            ),
            path="C:/docs/proteccion.pdf",
            source_kind="pdf",
            source_identity=source_identity,
            section_kind="page",
            section_id="7",
            start_char=None,
            end_char=None,
            snippet="protección interruptor",
            source_revision=source_revision,
            source_status="done",
        ),
        ranking_name="fts_pdf",
        source_rank=1,
        producer="lexical-fts5-v1",
    )
    semantic = _candidate_from_resolved(
        ResolvedSearchHit(
            hit=SearchHit(
                ref_id=2,
                entity_id="semantic-page-7",
                item_id="document",
                indexed_model_signature="semantic-text-v1",
                vector_space="semantic:text:v1",
                modality=EmbeddingModality.TEXT,
                score=0.9,
                generation_id=7,
                query_model_signature="semantic-query-v1",
            ),
            path="C:/docs/proteccion.pdf",
            source_kind="pdf",
            source_identity=source_identity,
            section_kind="pdf_page",
            section_id="7",
            start_char=0,
            end_char=80,
            snippet="protección interruptor primario",
            source_revision=source_revision,
            source_status="done",
        ),
        ranking_name="semantic_text",
        source_rank=1,
        producer="semantic-v6",
    )

    assert lexical.evidence.section_kind == "pdf_page"
    assert semantic.evidence.section_kind == "pdf_page"
    hits, omitted = fuse_evidence_rankings(
        {"fts_pdf": (lexical,), "semantic_text": (semantic,)},
        limit=5,
        max_per_resource=5,
        min_section_distance=0,
    )

    assert len(hits) == 1
    assert hits[0].evidence.page == 7
    assert hits[0].evidence.section_kind == "pdf_page"
    assert {signal.source for signal in hits[0].signals} == {
        "fts_pdf",
        "semantic_text",
    }
    assert "overlapping_evidence_merged" in hits[0].warnings
    assert omitted == 0


def test_unlocated_text_document_evidence_does_not_absorb_distant_passages() -> None:
    base = _candidate(
        evidence_id="document",
        section_id="1",
        start_char=0,
        end_char=80,
        ranking="fts_text",
        source_rank=1,
    )
    resource = replace(
        base.resource,
        source_kind="text",
        owner="text",
        current_path="/tmp/fixture.txt",
    )
    document = replace(
        base,
        resource=resource,
        evidence=replace(
            base.evidence,
            page=None,
            section_kind="document",
            section_id="fulltext",
            start_char=None,
            end_char=None,
        ),
        signal=replace(base.signal, source="fts_text"),
    )
    def passage(evidence_id: str, start: int, rank: int) -> KnowledgeCandidate:
        candidate = _candidate(
            evidence_id=evidence_id,
            section_id="1",
            start_char=start,
            end_char=start + 80,
            ranking="semantic_text",
            source_rank=rank,
        )
        return replace(
            candidate,
            resource=resource,
            evidence=replace(
                candidate.evidence,
                page=None,
                section_kind="document",
                section_id="fulltext",
            ),
        )

    passages = (passage("passage-1", 0, 2), passage("passage-2", 500, 3))

    hits, omitted = fuse_evidence_rankings(
        {"fts_text": (document,), "semantic_text": passages},
        limit=10,
        max_per_resource=10,
        min_section_distance=0,
    )

    assert [hit.evidence.evidence_id for hit in hits] == [
        "document",
        "passage-1",
        "passage-2",
    ]
    assert omitted == 0


def test_default_fusion_filters_duplicate_and_superseded_evidence() -> None:
    current = _candidate(
        evidence_id="current",
        section_id="1",
        start_char=0,
        end_char=10,
        ranking="fts_pdf",
        source_rank=1,
    )
    duplicate = _candidate(
        evidence_id="duplicate",
        section_id="2",
        start_char=0,
        end_char=10,
        ranking="fts_pdf",
        source_rank=2,
        disposition=ResourceDisposition.DUPLICATE,
    )
    superseded = _candidate(
        evidence_id="old",
        section_id="3",
        start_char=0,
        end_char=10,
        ranking="fts_pdf",
        source_rank=3,
        revision_state=RevisionState.SUPERSEDED,
    )
    superseded_resource = _candidate(
        evidence_id="old-resource",
        section_id="4",
        start_char=0,
        end_char=10,
        ranking="fts_pdf",
        source_rank=4,
        disposition=ResourceDisposition.SUPERSEDED,
    )

    current_hits, policy_omitted = fuse_evidence_rankings(
        {"fts_pdf": (current, duplicate, superseded, superseded_resource)},
        limit=10,
        max_per_resource=10,
        min_section_distance=0,
    )
    historical_hits, _ = fuse_evidence_rankings(
        {"fts_pdf": (current, superseded, superseded_resource)},
        limit=10,
        max_per_resource=10,
        min_section_distance=0,
        include_history=True,
    )

    assert [hit.evidence.evidence_id for hit in current_hits] == ["current"]
    assert policy_omitted == 0
    assert {hit.evidence.evidence_id for hit in historical_hits} == {
        "current",
        "old",
        "old-resource",
    }


def test_stale_published_semantic_revision_is_historical_and_opt_in() -> None:
    resolved = ResolvedSearchHit(
        hit=SearchHit(
            ref_id=1,
            entity_id="old-chunk",
            item_id="document",
            indexed_model_signature="indexed-model",
            vector_space="fixture-space",
            modality=EmbeddingModality.TEXT,
            score=0.9,
            generation_id=7,
            query_model_signature="query-model",
        ),
        path="C:/docs/proteccion.pdf",
        source_kind="pdf",
        source_identity=("00000000000000000000000000000001:00000000000000000000000000000002"),
        section_kind="pdf_page",
        section_id="1",
        start_char=0,
        end_char=24,
        snippet="old transformer record",
        source_status="done",
        source_revision={
            "birthtime_ns": 10,
            "processing_signature": "pdf-v11:fixture",
        },
        published_revision_id=1,
        current_revision_id=2,
    )
    candidate = _candidate_from_resolved(
        resolved,
        ranking_name="semantic_text",
        source_rank=1,
        producer="semantic-v6",
    )

    assert candidate.revision.state is RevisionState.HISTORICAL
    assert candidate.warnings == ("stale_revision",)
    assert candidate.signal.model_signature == "indexed-model"
    assert candidate.signal.query_model_signature == "query-model"
    current, _ = fuse_evidence_rankings(
        {"semantic_text": (candidate,)},
        limit=5,
        max_per_resource=5,
        min_section_distance=0,
    )
    historical, _ = fuse_evidence_rankings(
        {"semantic_text": (candidate,)},
        limit=5,
        max_per_resource=5,
        min_section_distance=0,
        include_history=True,
    )
    assert current == ()
    assert len(historical) == 1
    assert historical[0].revision.state is RevisionState.HISTORICAL


def _create_lexical_states(state: Path) -> None:
    state.mkdir()
    with sqlite3.connect(state / "pdf.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,
                size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                '00000000000000000000000000000001:00000000000000000000000000000002',
                'C:/docs/proteccion.pdf','done',0,100,20,10,
                'pdf-v11:fixture',7
            );
            INSERT INTO page_fts VALUES(
                '00000000000000000000000000000001:00000000000000000000000000000002',
                'C:/docs/proteccion.pdf',1,
                'protección interruptor disparo primario'
            );
            INSERT INTO page_fts VALUES(
                '00000000000000000000000000000001:00000000000000000000000000000002',
                'C:/docs/proteccion.pdf',7,
                'protección interruptor respaldo secundario'
            );
            """
        )
    with sqlite3.connect(state / "docx.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,title,author,body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                '00000000000000000000000000000003:00000000000000000000000000000004',
                'C:/docs/estudio.docx','complete',100,30,11,
                'docx-v5:fixture',8
            );
            INSERT INTO document_fts VALUES(
                '00000000000000000000000000000003:00000000000000000000000000000004',
                'C:/docs/estudio.docx','Estudio','Victor',
                'protección interruptor coordinación'
            );
            """
        )


def _create_archive_lexical_state(state: Path) -> None:
    state.mkdir(exist_ok=True)
    database = state / "archive.sqlite3"
    initialize_archive_state(database)
    with archive_database(database, create=False) as connection:
        connection.execute(
            """INSERT INTO containers(
            container_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            member_count,indexed_count,nested_archive_count,last_seen_run_id,updated_ns)
            VALUES('container:key','C:/docs/contenedor.zip',100,20,-1,
            'archive-v1','complete',2,1,1,9,30)"""
        )
        connection.execute(
            """INSERT INTO documents(
            file_key,container_key,path,container_path,member_chain,member_path,
            archive_depth,content_kind,media_type,size,compressed_size,crc32,
            mtime_ns,birthtime_ns,processing_signature,status,text_zlib,text_chars,
            text_xxh3_128,last_seen_run_id,updated_ns)
            VALUES('archive:member','container:key',
            'C:/docs/contenedor.zip!/interno.zip!/proteccion.txt',
            'C:/docs/contenedor.zip','interno.zip!/proteccion.txt','proteccion.txt',
            2,'text','text/plain',45,20,1,20,-1,'archive-v1','indexed',NULL,45,
            '00000000000000000000000000000000',9,30)"""
        )
        connection.execute(
            """INSERT INTO document_fts(
            file_key,path,container_path,container_name,member_chain,content_kind,body)
            VALUES('archive:member',
            'C:/docs/contenedor.zip!/interno.zip!/proteccion.txt',
            'C:/docs/contenedor.zip','contenedor.zip',
            'interno.zip!/proteccion.txt','text',
            'protección diferencial dentro de un ZIP anidado')"""
        )
        connection.commit()


def _deterministic_semantic_backend(
    model: EmbeddingModelSpec,
    *,
    cache_dir: Path,
    local_files_only: bool,
    threads: int | None,
) -> DeterministicTestBackend:
    del cache_dir, local_files_only, threads
    return DeterministicTestBackend(replace(model, provider="test-deterministic"))


class _RecordingDeterministicBackend(DeterministicTestBackend):
    def __init__(
        self,
        model: EmbeddingModelSpec,
        requests: list[EmbeddingRequest],
    ) -> None:
        super().__init__(replace(model, provider="test-deterministic"))
        self._requests = requests

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        self._requests.extend(requests)
        return super().embed(requests)


def _create_published_semantic_pdf_state(
    state: Path,
) -> tuple[EmbeddingModelSpec, int]:
    file_key = "00000000000000000000000000000001:00000000000000000000000000000002"
    pages = (
        (1, "protección interruptor disparo primario"),
        (7, "protección interruptor respaldo secundario"),
    )
    combined_text = " ".join(text for _, text in pages)
    with sqlite3.connect(state / "pdf.sqlite3") as connection:
        connection.executescript(
            """
            ALTER TABLE documents ADD COLUMN normalized_text_xxh3_128 TEXT;
            ALTER TABLE documents ADD COLUMN normalized_text_chars INTEGER
                NOT NULL DEFAULT 0;
            CREATE TABLE pages(
                file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
                source TEXT NOT NULL,text_zlib BLOB NOT NULL,
                text_chars INTEGER NOT NULL,
                PRIMARY KEY(file_key,page_number)
            );
            """
        )
        connection.execute(
            """UPDATE documents
            SET normalized_text_xxh3_128=?,normalized_text_chars=?
            WHERE file_key=?""",
            (fingerprint_text(combined_text).xxh3_128, len(combined_text), file_key),
        )
        connection.executemany(
            "INSERT INTO pages VALUES(?,?,?,?,?)",
            (
                (
                    file_key,
                    page_number,
                    "native",
                    zlib.compress(text.encode("utf-8")),
                    len(text),
                )
                for page_number, text in pages
            ),
        )

    model = multilingual_text_model()
    indexed = semantic_service.index_text_embeddings(
        state,
        source_kinds=("pdf",),
        model=model,
        local_files_only=True,
        chunking=TextChunkingConfig(
            max_chars=256,
            max_terms=64,
            overlap_chars=0,
            overlap_terms=0,
            min_natural_break_chars=32,
        ),
    )
    assert indexed.items_staged == 1
    assert indexed.chunks_staged == 3
    assert len(indexed.generations) == 1
    summary = indexed.generations[0].summary
    assert summary.status == "ready"
    assert summary.done == 3
    assert summary.errors == 0
    return model, summary.generation_id


def _create_catalog_state(state: Path) -> None:
    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with sqlite3.connect(catalog) as connection:
        connection.executescript(
            """
            INSERT INTO catalog_generations(
                generation_id,source_kind,status,started_ns,completed_ns,published_ns
            ) VALUES(1,'pdf','published',1,2,3);
            INSERT INTO catalog_publications(source_kind,generation_id,published_ns)
            VALUES('pdf',1,3);
            INSERT INTO catalog_generation_documents(
                generation_id,source_kind,file_key,path,volume_id,file_id,size,
                mtime_ns,birthtime_ns,source_status,processing_signature,
                classifier_signature,primary_kind,primary_subtype,primary_project,
                confidence,uncertainty,standard_references_json,organizations_json,
                clients_json,projects_json,workstreams_json,topics_json,
                equipment_json,activities_json,classification_json,catalog_status,
                active,last_seen_catalog_run_id,updated_ns
            ) VALUES(
                1,'pdf',
                '00000000000000000000000000000001:00000000000000000000000000000002',
                'C:/docs/proteccion.pdf','1','2',100,20,10,'done',
                'pdf-v11:fixture','classifier-v1',
                'estudio','coordinacion','Alpha',0.9,'baja',
                '[{"identifier":"IEC-61850"}]','[]','[]',
                '[{"label":"Alpha"}]','[]','[]','[]','[]','{}','classified',
                1,7,40
            );
            """
        )


def _mutate_published_catalog(path: Path, statement: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE catalog_generations SET status='building' WHERE generation_id=1"
        )
        connection.execute(statement)
        connection.execute(
            "UPDATE catalog_generations SET status='published' WHERE generation_id=1"
        )


def _create_inventory_duplicate_state(state: Path) -> None:
    inventory = state / "dedup.sqlite3"
    initialize_inventory_schema(inventory)

    def identity_blob(value: int) -> bytes:
        return value.to_bytes(16, "little")

    with sqlite3.connect(inventory) as connection:
        connection.executescript(
            """
            INSERT INTO scans(
                scan_id,root,started_ns,completed_ns,files_seen,directories_seen,
                bytes_seen,skipped_links,excluded_directories,errors,status
            ) VALUES(1,'C:/docs',1,2,2,0,300,0,0,0,'complete');
            INSERT INTO inventory_checkpoints(
                root,scan_id,volume,journal_id,next_usn,valid,updated_ns
            ) VALUES('C:/docs',1,'C:','journal',10,1,3);
            INSERT INTO duplicate_plan_summaries(
                scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns
            ) VALUES(1,1,1,100,4);
            INSERT INTO planned_duplicate_groups(
                group_id,scan_id,size,keep_path,redundant_count,
                reclaimable_bytes,full_fingerprint
            ) VALUES(
                1,1,100,'C:/docs/estudio.docx',1,100,
                '00000000000000000000000000000000'
            );
            """
        )
        connection.executemany(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                (
                    1,
                    "C:/docs/proteccion.pdf",
                    identity_blob(1),
                    identity_blob(2),
                    100,
                    20,
                    10,
                ),
                (
                    1,
                    "C:/docs/estudio.docx",
                    identity_blob(3),
                    identity_blob(4),
                    100,
                    30,
                    11,
                ),
            ),
        )
        connection.executemany(
            """INSERT INTO planned_duplicate_members(
            group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                (
                    1,
                    0,
                    "keep",
                    "C:/docs/estudio.docx",
                    identity_blob(3),
                    identity_blob(4),
                    100,
                    30,
                    11,
                ),
                (
                    1,
                    1,
                    "redundant",
                    "C:/docs/proteccion.pdf",
                    identity_blob(1),
                    identity_blob(2),
                    100,
                    20,
                    10,
                ),
            ),
        )


def test_search_reuses_real_fts_owners_and_preserves_two_pdf_pages(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.AVAILABLE, 5, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    query = KnowledgeQuery(
        "protección interruptor",
        retrieval_mode=RetrievalMode.EVIDENCE,
        limit=5,
        max_per_resource=3,
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(query),
        snapshot,
    )

    assert [hit.evidence.page for hit in result.hits if hit.resource.source_kind == "pdf"] == [
        1,
        7,
    ]
    assert {hit.resource.source_kind for hit in result.hits} == {"pdf", "docx"}
    assert {ranking.name for ranking in result.rankings if ranking.executed} >= {
        "fts_pdf",
        "fts_docx",
    }
    assert result.rows_scanned == 3
    assert result.vectors_scanned == 0
    assert all(hit.revision.state is RevisionState.CURRENT for hit in result.hits)
    assert {hit.revision.processing_signature for hit in result.hits} == {
        "pdf-v11:fixture",
        "docx-v5:fixture",
    }
    assert result.to_json() == result.to_json()


def test_search_returns_archive_member_with_explicit_nested_zip_evidence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_archive_lexical_state(state)
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.ABSENT, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("archive", OwnerAvailability.AVAILABLE, 1, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("protección diferencial", limit=5)),
        snapshot,
    )

    assert len(result.hits) == 1
    hit = result.hits[0]
    identifiers = dict(hit.evidence.identifiers)
    assert hit.resource.owner == "archive"
    assert hit.resource.source_kind == "archive"
    # An archive member is a virtual resource; its member key and locators do
    # not prove the physical identity of the containing file.
    assert hit.resource.physical_identity is None
    assert "physical_identity_unresolved" in hit.warnings
    assert hit.resource.current_path == ("C:/docs/contenedor.zip!/interno.zip!/proteccion.txt")
    assert hit.evidence.section_kind == "archive_member"
    assert identifiers["inside_zip"] == "1"
    assert identifiers["container_path"] == "C:/docs/contenedor.zip"
    assert identifiers["member_chain"] == "interno.zip!/proteccion.txt"
    assert identifiers["archive_depth"] == "2"
    assert any(ranking.name == "fts_archive" for ranking in result.rankings)


def test_real_lexical_and_semantic_sqlite_share_physical_resource_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        _deterministic_semantic_backend,
    )
    model, generation_id = _create_published_semantic_pdf_state(state)
    with sqlite3.connect(state / "semantic.sqlite3") as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 7

    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot(
            "semantic",
            OwnerAvailability.AVAILABLE,
            6,
            6,
            publications=(
                PublicationHead(
                    f"model:{model.model_signature}",
                    f"semantic:{generation_id}",
                    generation_id,
                    model_signature=model.model_signature,
                ),
            ),
        ),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(
            KnowledgeQuery(
                "protección interruptor",
                retrieval_mode=RetrievalMode.EVIDENCE,
                limit=10,
                max_per_resource=10,
                min_section_distance=0,
            )
        ),
        snapshot,
    )

    lexical_hits = tuple(
        hit for hit in result.hits if any(signal.source == "fts_pdf" for signal in hit.signals)
    )
    semantic_hits = tuple(
        hit
        for hit in result.hits
        if any(signal.source == "semantic_text" for signal in hit.signals)
    )
    assert lexical_hits
    assert semantic_hits
    assert {hit.resource.resource_id for hit in (*lexical_hits, *semantic_hits)} == {
        "resource:file:1:2:10"
    }
    assert {
        hit.resource.physical_identity.value
        for hit in (*lexical_hits, *semantic_hits)
        if hit.resource.physical_identity is not None
    } == {"1:2:10"}
    assert {hit.revision.revision_id for hit in lexical_hits} == {
        hit.revision.revision_id for hit in semantic_hits
    }
    assert {
        signal.generation
        for hit in semantic_hits
        for signal in hit.signals
        if signal.source == "semantic_text"
    } == {generation_id}
    semantic_signals = {
        signal
        for hit in semantic_hits
        for signal in hit.signals
        if signal.source == "semantic_text"
    }
    assert {signal.model_signature for signal in semantic_signals} == {model.model_signature}
    assert {signal.query_model_signature for signal in semantic_signals} == {model.model_signature}
    semantic_report = next(
        ranking for ranking in result.rankings if ranking.name == "semantic_text"
    )
    assert semantic_report.executed
    assert semantic_report.available
    assert semantic_report.returned == 2
    assert semantic_report.vectors_scanned == 2


def test_knowledge_discovery_uses_title_only_as_grounded_resource_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        _deterministic_semantic_backend,
    )
    model, generation_id = _create_published_semantic_pdf_state(state)
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot(
            "semantic",
            OwnerAvailability.AVAILABLE,
            6,
            6,
            publications=(
                PublicationHead(
                    f"model:{model.model_signature}",
                    f"semantic:{generation_id}",
                    generation_id,
                    model_signature=model.model_signature,
                ),
            ),
        ),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "proteccion",
            retrieval_mode=RetrievalMode.DISCOVERY,
            source_kinds=("pdf",),
            limit=10,
            max_per_resource=10,
            min_section_distance=0,
            max_vectors=10,
        )
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan,
        snapshot,
    )

    title_report = next(ranking for ranking in result.rankings if ranking.name == "semantic_title")
    title_signals = tuple(
        (hit, signal)
        for hit in result.hits
        for signal in hit.signals
        if signal.source == "semantic_title"
    )
    assert title_report.channel == "semantic_discovery"
    assert title_report.returned == 1
    assert title_signals
    assert len(title_signals) == 1
    boosted_hit, title_signal = title_signals[0]
    assert title_signal.contribution == pytest.approx(0.5 / 61.0)
    assert boosted_hit.evidence.section_kind != "semantic_metadata_title"
    assert boosted_hit.evidence.snippet != "proteccion"
    assert result.vectors_scanned <= plan.max_vectors
    assert '"section_kind":"semantic_metadata_title"' not in result.to_json()


def test_semantic_text_and_title_share_vector_budget_and_query_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        _deterministic_semantic_backend,
    )
    model, _generation_id = _create_published_semantic_pdf_state(state)

    requests: list[EmbeddingRequest] = []

    def recording_backend(
        selected_model: EmbeddingModelSpec,
        *,
        cache_dir: Path,
        local_files_only: bool,
        threads: int | None,
    ) -> DeterministicTestBackend:
        del cache_dir, local_files_only, threads
        return _RecordingDeterministicBackend(selected_model, requests)

    monkeypatch.setattr(semantic_service, "_backend", recording_backend)
    result = semantic_service.search_semantic_index(
        state,
        "protección interruptor",
        semantic_database=state / "semantic.sqlite3",
        text_model=model,
        limit=10,
        candidate_limit=10,
        max_vectors=2,
        include_text=True,
        include_title=True,
        include_images=False,
        include_lexical=False,
        local_files_only=True,
    )

    rankings = {ranking.name: ranking for ranking in result.rankings}
    assert set(rankings) == {"semantic_text", "semantic_title"}
    assert rankings["semantic_text"].scanned > 0
    assert sum(ranking.scanned for ranking in rankings.values()) == 2
    query_requests = tuple(request for request in requests if request.role is EmbeddingRole.QUERY)
    assert len(requests) == 1
    assert len(query_requests) == 1
    assert query_requests[0].text == "protección interruptor"


def test_missing_semantic_cache_preserves_lexical_and_creates_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        _deterministic_semantic_backend,
    )
    model, generation_id = _create_published_semantic_pdf_state(state)
    model_cache = tmp_path / "models" / "fastembed"
    assert not model_cache.exists()
    monkeypatch.setattr(
        semantic_preparation,
        "default_semantic_model_cache",
        lambda _state_directory: model_cache,
    )

    def unexpected_backend(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("FastEmbedBackend must not be constructed without a local model")

    monkeypatch.setattr(semantic_preparation, "FastEmbedBackend", unexpected_backend)
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        semantic_preparation.backend,
    )
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot(
            "semantic",
            OwnerAvailability.AVAILABLE,
            6,
            6,
            publications=(
                PublicationHead(
                    f"model:{model.model_signature}",
                    f"semantic:{generation_id}",
                    generation_id,
                    model_signature=model.model_signature,
                ),
            ),
        ),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("protección interruptor", limit=5)),
        snapshot,
    )

    assert any(signal.source == "fts_pdf" for hit in result.hits for signal in hit.signals)
    semantic_report = next(
        ranking for ranking in result.rankings if ranking.name == "semantic_text"
    )
    assert semantic_report.executed
    assert not semantic_report.available
    assert not semantic_report.complete
    assert semantic_report.reason == "semantic_model_cache_missing"
    assert not result.complete
    assert "ranking_partial:semantic_text:semantic_model_cache_missing" in result.warnings
    assert not model_cache.exists()
    assert not (tmp_path / "models").exists()


def test_legacy_decimal_file_key_aligns_with_canonical_physical_resource(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    with sqlite3.connect(state / "pdf.sqlite3") as connection:
        connection.execute("UPDATE documents SET file_key='26:43',birthtime_ns=30")
        connection.execute("UPDATE page_fts SET file_key='26:43'")
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("primario", limit=5)),
        snapshot,
    )

    assert result.hits
    assert {hit.resource.resource_id for hit in result.hits} == {"resource:file:26:43:30"}


def test_linux_birthtime_sentinel_claims_posix_physical_identity(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    with sqlite3.connect(state / "pdf.sqlite3") as connection:
        connection.execute("UPDATE documents SET birthtime_ns=-1")
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("primario", limit=5)),
        snapshot,
    )

    assert result.hits
    hit = result.hits[0]
    assert hit.resource.resource_id == "resource:file:1:2:-1"
    assert hit.resource.physical_identity is not None
    assert hit.resource.physical_identity.scheme == "posix_device_inode_birthtime"
    assert "physical_identity_unresolved" not in hit.warnings


def test_unverified_inventory_plan_is_exposed_but_never_filters_evidence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    _create_inventory_duplicate_state(state)
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.AVAILABLE, 5, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot(
            "inventory",
            OwnerAvailability.AVAILABLE,
            7,
            7,
            publications=(
                PublicationHead(
                    "C:/docs",
                    "inventory-scan:1",
                    1,
                    model_signature="duplicate-plan-v1:4:1:1:100",
                ),
            ),
        ),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(
            KnowledgeQuery(
                "protección interruptor",
                retrieval_mode=RetrievalMode.EVIDENCE,
                limit=5,
                max_per_resource=3,
            )
        ),
        snapshot,
    )

    assert result.hits
    assert {hit.resource.resource_id for hit in result.hits} == {
        "resource:file:1:2:10",
        "resource:file:3:4:11",
    }
    redundant_hits = tuple(
        hit for hit in result.hits if hit.resource.resource_id == "resource:file:1:2:10"
    )
    assert redundant_hits
    assert all(hit.resource.disposition is None for hit in redundant_hits)
    assert all("inventory_planned_duplicate_unverified" in hit.warnings for hit in redundant_hits)
    duplicate_report = next(
        ranking for ranking in result.rankings if ranking.name == "inventory_duplicate_plan"
    )
    assert duplicate_report.executed
    assert not duplicate_report.complete
    assert duplicate_report.returned == 1
    assert duplicate_report.rows_scanned == 2
    assert duplicate_report.reason == "inventory_exact_verification_unavailable"
    assert not result.complete


def test_incompatible_inventory_snapshot_never_reads_duplicate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(
        evidence_id="inventory-v7-gate",
        section_id="1",
        start_char=0,
        end_char=10,
        ranking="fts_pdf",
        source_rank=1,
    )
    rankings = {"fts_pdf": (candidate,)}
    snapshot = _snapshot(
        OwnerSnapshot(
            "inventory",
            OwnerAvailability.INCOMPATIBLE,
            8,
            7,
            error_code="schema_too_old",
        ),
    )

    def forbidden_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("incompatible inventory state must not be opened")

    monkeypatch.setattr(
        knowledge_search_module,
        "_open_direct_readonly_sqlite",
        forbidden_open,
    )

    updated, report = knowledge_search_module._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        snapshot,
        rankings,
    )

    assert updated == rankings
    assert not report.executed
    assert not report.available
    assert report.complete
    assert report.reason == "inventory_owner_unavailable"


def test_catalog_head_constrains_all_rankings_and_aligns_physical_resource(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    _create_catalog_state(state)
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.AVAILABLE, 5, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot(
            "catalog",
            OwnerAvailability.AVAILABLE,
            6,
            6,
            publications=(PublicationHead("pdf", "catalog:1", 1),),
        ),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    query = KnowledgeQuery(
        "protección interruptor",
        formats=("pdf",),
        project="Alpha",
        limit=5,
        max_per_resource=3,
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(query),
        snapshot,
    )

    assert result.hits
    assert {hit.resource.source_kind for hit in result.hits} == {"pdf"}
    assert {hit.resource.resource_id for hit in result.hits} == {"resource:file:1:2:10"}
    assert {signal.source for hit in result.hits for signal in hit.signals} == {"fts_pdf"}
    assert next(
        ranking for ranking in result.rankings if ranking.name == "catalog_metadata"
    ).complete

    exact = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("IEC-61850", limit=5)),
        snapshot,
    )

    assert exact.hits
    assert any(
        ("standard_identifier", "IEC-61850") in hit.evidence.identifiers for hit in exact.hits
    )

    prefix = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("IEC-6185", limit=5)),
        snapshot,
    )
    assert prefix.hits == ()

    _mutate_published_catalog(
        state / "document_catalog.sqlite3",
        "UPDATE catalog_generation_documents SET birthtime_ns=-1",
    )
    unresolved = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("IEC-61850", limit=5)),
        snapshot,
    )
    unresolved_hit = next(
        hit for hit in unresolved.hits if hit.evidence.method is EvidenceMethod.INFERRED
    )
    assert unresolved_hit.resource.resource_id == "resource:file:1:2:-1"
    assert unresolved_hit.resource.physical_identity is not None
    assert unresolved_hit.resource.physical_identity.scheme == ("posix_device_inode_birthtime")
    assert "physical_identity_unresolved" not in unresolved_hit.warnings


def test_unsupported_content_date_filter_abstains_instead_of_using_mtime(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _create_lexical_states(state)
    _create_catalog_state(state)
    snapshot = _snapshot(
        OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 11, 11),
        OwnerSnapshot("docx", OwnerAvailability.AVAILABLE, 5, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot(
            "catalog",
            OwnerAvailability.AVAILABLE,
            6,
            6,
            publications=(PublicationHead("pdf", "catalog:1", 1),),
        ),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    query = KnowledgeQuery(
        "protección interruptor",
        formats=("pdf",),
        date_from="2026-01-01",
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(query),
        snapshot,
    )

    assert result.hits == ()
    catalog = next(ranking for ranking in result.rankings if ranking.name == "catalog_metadata")
    assert not catalog.complete
    assert catalog.reason == "catalog_content_date_filter_unsupported"


def test_search_reuses_real_structured_code_owner(tmp_path: Path) -> None:
    source = tmp_path / "breaker.py"
    source.write_text(
        "def calculate_breaker(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    config = CodeRouteConfig(
        state_path=state / "code.sqlite3",
        dedup_path=state / "dedup.sqlite3",
        chunk_chars=1_024,
    )
    source_snapshot = _file_snapshot(source)
    CodeRoute(
        config,
        _CodeInventory(source_snapshot),
        _CodeFramework(),
        1,
        1,
    ).run()
    snapshot = _snapshot(
        OwnerSnapshot("code", OwnerAvailability.AVAILABLE, 2, 2),
        OwnerSnapshot("pdf", OwnerAvailability.ABSENT, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    query = KnowledgeQuery(
        "definition calculate_breaker",
        source_kinds=("code",),
        limit=5,
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(query),
        snapshot,
    )

    assert result.hits
    hit = next(hit for hit in result.hits if hit.resource.source_kind == "code")
    assert hit.resource.resource_id == (
        f"resource:file:{source_snapshot.volume_id}:"
        f"{source_snapshot.file_id}:{source_snapshot.birthtime_ns}"
    )
    assert hit.evidence.start_line == 1
    assert hit.evidence.symbol is not None
    assert hit.revision.processing_signature.startswith(
        f"{config.processing_signature}|artifact-detector={DETECTOR_VERSION}|code-analyzers-v1:"
    )
    assert next(
        ranking for ranking in result.rankings if ranking.name == "code_structural"
    ).executed
    assert not result.complete
    assert "ranking_unavailable:semantic_text" in result.warnings

    with sqlite3.connect(state / "code.sqlite3") as connection:
        connection.execute("UPDATE file_versions SET birthtime_ns=-1")
    unresolved = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(query),
        snapshot,
    )
    unresolved_hit = next(hit for hit in unresolved.hits if hit.resource.source_kind == "code")
    assert unresolved_hit.resource.resource_id == (
        f"resource:file:{source_snapshot.volume_id}:{source_snapshot.file_id}:-1"
    )
    assert unresolved_hit.resource.physical_identity is not None
    assert unresolved_hit.resource.physical_identity.scheme == ("posix_device_inode_birthtime")
    assert "physical_identity_unresolved" not in unresolved_hit.warnings


def test_inventory_linux_birthtime_sentinel_uses_posix_namespace(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_inventory_duplicate_state(state)
    exact_path = r"C:\docs\proteccion.pdf"
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        connection.execute(
            "UPDATE files SET path=?,birthtime_ns=-1 WHERE file_id=?",
            (exact_path, (2).to_bytes(16, "little")),
        )
    snapshot = _snapshot(
        OwnerSnapshot(
            "inventory",
            OwnerAvailability.AVAILABLE,
            7,
            7,
            publications=(
                PublicationHead(
                    "C:/docs",
                    "inventory-scan:1",
                    1,
                    model_signature="duplicate-plan-v1:4:1:1:100",
                ),
            ),
            watermarks=(
                LogicalWatermark("published_roots", "1"),
                LogicalWatermark("latest_checkpoint_updated_ns", "3"),
            ),
        ),
        OwnerSnapshot("pdf", OwnerAvailability.ABSENT, 11),
        OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
        OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
        OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
        OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery(exact_path, limit=5)),
        snapshot,
    )

    hit = next(hit for hit in result.hits if hit.resource.owner == "inventory")
    assert hit.resource.resource_id == "resource:file:1:2:-1"
    assert hit.resource.physical_identity is not None
    assert hit.resource.physical_identity.scheme == "posix_device_inode_birthtime"
    assert "physical_identity_unresolved" not in hit.warnings


def test_explicit_filters_keep_exact_inventory_aliases_and_real_image_ocr() -> None:
    def candidate(
        *,
        path: str,
        source_kind: str,
        owner: str,
        section_kind: str = "current_path",
    ) -> KnowledgeCandidate:
        resource = ResourceRef(
            f"resource:{owner}:filter",
            source_kind,
            owner,
            current_path=path,
        )
        revision = RevisionRef(
            resource.resource_id,
            f"revision:{owner}:filter",
            "fixture",
            "fixture-v1",
            None,
            RevisionState.CURRENT,
        )
        evidence = EvidenceRef(
            f"evidence:{owner}:filter",
            resource.resource_id,
            revision.revision_id,
            EvidenceMethod.STRUCTURAL,
            section_kind=section_kind,
            section_id=path,
        )
        return KnowledgeCandidate(
            resource,
            revision,
            evidence,
            RankingSignal("exact_fixture", "exact", 1.0, 1),
            "fixture exact match",
        )

    inventory_pdf = candidate(
        path="C:/docs/report.pdf",
        source_kind="file",
        owner="inventory",
    )
    inventory_python = candidate(
        path="C:/src/control.py",
        source_kind="file",
        owner="inventory",
    )
    image_ocr = candidate(
        path="C:/images/panel.webp",
        source_kind="image",
        owner="image",
        section_kind="image_ocr",
    )

    assert knowledge_search_module._matches_explicit_source_filters(
        inventory_pdf,
        plan_knowledge_query(KnowledgeQuery("report.pdf", source_kinds=("pdf",))),
    )
    assert knowledge_search_module._matches_explicit_source_filters(
        inventory_python,
        plan_knowledge_query(KnowledgeQuery("control.py", source_kinds=("code",))),
    )
    assert knowledge_search_module._matches_explicit_source_filters(
        inventory_python,
        plan_knowledge_query(KnowledgeQuery("control.py", formats=("python",))),
    )
    assert knowledge_search_module._matches_explicit_source_filters(
        image_ocr,
        plan_knowledge_query(KnowledgeQuery("panel.webp", source_kinds=("image_ocr",))),
    )


def test_final_result_window_omission_is_transported_once_without_truncation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_inventory_duplicate_state(state)
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        connection.execute(
            "UPDATE files SET path='C:/A/shared.pdf' WHERE file_id=?",
            ((2).to_bytes(16, "little"),),
        )
        connection.execute(
            "UPDATE files SET path='C:/B/shared.pdf' WHERE file_id=?",
            ((4).to_bytes(16, "little"),),
        )
    snapshot = _snapshot(
        OwnerSnapshot(
            "inventory",
            OwnerAvailability.AVAILABLE,
            7,
            7,
            publications=(PublicationHead("C:/docs", "inventory-scan:1", 1),),
            watermarks=(
                LogicalWatermark("published_roots", "1"),
                LogicalWatermark("latest_checkpoint_updated_ns", "3"),
            ),
        ),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan_knowledge_query(KnowledgeQuery("shared.pdf", formats=("pdf",), limit=1)),
        snapshot,
    )

    assert len(result.hits) == 1
    assert not result.truncated
    assert result.omitted_candidates == 1
    assert result.result_window_full
    assert result.window_omitted_candidates == 1


def test_invalid_exact_lookahead_preserves_truncation_without_inventing_omitted(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_inventory_duplicate_state(state)
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        connection.execute(
            "UPDATE files SET path='C:/A/shared.pdf',volume_id=? WHERE file_id=?",
            (sqlite3.Binary(b"invalid"), (2).to_bytes(16, "little")),
        )
        connection.execute(
            "UPDATE files SET path='C:/B/shared.pdf' WHERE file_id=?",
            ((4).to_bytes(16, "little"),),
        )
    snapshot = _snapshot(
        OwnerSnapshot(
            "inventory",
            OwnerAvailability.AVAILABLE,
            7,
            7,
            publications=(PublicationHead("C:/docs", "inventory-scan:1", 1),),
            watermarks=(
                LogicalWatermark("published_roots", "1"),
                LogicalWatermark("latest_checkpoint_updated_ns", "3"),
            ),
        ),
        OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
    )

    plan = plan_knowledge_query(KnowledgeQuery("shared.pdf", formats=("pdf",), limit=1))
    plan = replace(
        plan,
        plan_id="knowledge-plan-v1:exact-lookahead-fixture",
        steps=tuple(
            replace(step, candidate_limit=1) if step.channel == "exact" else step
            for step in plan.steps
        ),
    )
    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan,
        snapshot,
    )

    assert len(result.hits) == 1
    assert result.truncated
    assert result.omitted_candidates == 0


def test_exact_budget_truncation_survives_with_zero_known_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = knowledge_search_module.RankingExecution(
        "exact_fixture",
        "exact",
        True,
        True,
        False,
        0,
        reason="exact_work_budget_exhausted",
    )

    def exact_fixture(*args: object, **kwargs: object):
        del args, kwargs
        return {}, [partial], 0, True

    monkeypatch.setattr(knowledge_search_module, "_exact_rankings", exact_fixture)
    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan_knowledge_query(KnowledgeQuery("IEC-61850", limit=1)),
        _snapshot(),
    )

    assert result.omitted_candidates == 0
    assert result.truncated


def test_lexical_execution_uses_the_planned_candidate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_limits: list[int] = []

    def lexical_search_fixture(
        _paths: object,
        _query: str,
        *,
        limit: int,
        **_kwargs: object,
    ) -> tuple[object, ...]:
        observed_limits.append(limit)
        return ()

    monkeypatch.setattr(
        knowledge_search_module,
        "search_lexical_sources",
        lexical_search_fixture,
    )
    plan = plan_knowledge_query(KnowledgeQuery("proteccion", formats=("pdf",), limit=1))
    step = next(value for value in plan.steps if value.channel == "lexical")

    rankings, reports = knowledge_search_module._lexical_rankings(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    assert rankings == {}
    assert reports == []
    assert observed_limits == [step.candidate_limit + 1]


def test_code_execution_uses_the_planned_candidate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_limits: list[int] = []

    def code_search_fixture(
        _path: Path,
        query: CodeSearchQuery,
        **_kwargs: object,
    ) -> tuple[object, ...]:
        observed_limits.append(query.limit)
        return ()

    monkeypatch.setattr(knowledge_search_module, "search_code", code_search_fixture)
    monkeypatch.setattr(
        knowledge_search_module,
        "_code_version_metadata",
        lambda *_args, **_kwargs: {},
    )
    plan = plan_knowledge_query(KnowledgeQuery("definition breaker", formats=("py",), limit=1))
    step = next(value for value in plan.steps if value.channel == "structural_code")

    candidates, report = knowledge_search_module._code_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(OwnerSnapshot("code", OwnerAvailability.AVAILABLE, 2, 2)),
    )

    assert candidates == ()
    assert report.complete
    assert observed_limits == [step.candidate_limit + 1]


def test_code_candidate_limit_bounds_relation_processing_as_complete_top_k(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_knowledge_query(KnowledgeQuery("definition breaker", formats=("py",), limit=1))
    step = next(value for value in plan.steps if value.channel == "structural_code")
    source = CodeRelationEndpoint(1, "C:/src/breaker.py")
    relations = tuple(
        CodeSearchRelation(
            "dependency",
            "python_import",
            f"dependency_{index}",
            source,
            None,
            None,
            False,
            True,
            0.9,
            "fixture",
            "dependencies",
            index,
        )
        for index in range(1, 9)
    )
    hit = CodeSearchHit(
        "C:/src/breaker.py",
        None,
        "python",
        "source",
        None,
        None,
        1,
        1,
        "breaker fixture",
        1.0,
        ("literal",),
        ("literal:breaker",),
        1,
        100,
        200,
        "complete",
        relations,
    )
    base = _candidate(
        evidence_id="code-base",
        section_id="1",
        start_char=0,
        end_char=10,
        ranking="code_structural",
        source_rank=1,
    )
    observed_version_ids: list[int] = []
    processed_relations: list[int] = []

    def code_search_fixture(
        _path: Path,
        query: CodeSearchQuery,
        **_kwargs: object,
    ) -> tuple[CodeSearchHit, ...]:
        assert query.limit == step.candidate_limit + 1
        return (hit,)

    def metadata_fixture(
        _path: Path,
        version_ids: tuple[int, ...],
        **_kwargs: object,
    ) -> dict[int, dict[str, str]]:
        observed_version_ids.extend(version_ids)
        return {
            1: {
                "analyzer_id": "fixture",
                "analyzer_version": "1",
                "processing_signature": "fixture-v1",
            }
        }

    def relation_fixture(
        _metadata: object,
        *,
        source_rank: int,
        hit: CodeSearchHit,
        relation: CodeSearchRelation,
    ) -> tuple[KnowledgeCandidate, bool]:
        del hit
        processed_relations.append(relation.source_row_id)
        return (
            _candidate(
                evidence_id=f"code-relation-{relation.source_row_id}",
                section_id=str(relation.source_row_id + 1),
                start_char=0,
                end_char=10,
                ranking="code_structural",
                source_rank=source_rank,
            ),
            False,
        )

    monkeypatch.setattr(knowledge_search_module, "search_code", code_search_fixture)
    monkeypatch.setattr(
        knowledge_search_module,
        "_code_version_metadata",
        metadata_fixture,
    )
    monkeypatch.setattr(
        knowledge_search_module,
        "_code_resource_revision",
        lambda *_args, **_kwargs: (base.resource, base.revision, ()),
    )
    monkeypatch.setattr(
        knowledge_search_module,
        "_code_relation_candidate",
        relation_fixture,
    )

    candidates, report = knowledge_search_module._code_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(OwnerSnapshot("code", OwnerAvailability.AVAILABLE, 2, 2)),
    )

    assert len(candidates) == step.candidate_limit
    assert processed_relations == [1, 2, 3]
    assert len(observed_version_ids) == step.candidate_limit + 1
    assert report.rows_scanned == step.candidate_limit + 1
    assert report.complete
    assert report.reason is None
    assert report.result_window_full


def test_exact_execution_passes_the_planned_candidate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_limits: list[int] = []

    def exact_lookup_fixture(
        _paths: KnowledgeStatePaths,
        _plan: object,
        _snapshot_value: KnowledgeSnapshot,
        *,
        candidate_limit: int,
        **_kwargs: object,
    ) -> None:
        observed_limits.append(candidate_limit)
        return None

    monkeypatch.setattr(
        knowledge_search_module,
        "lookup_plan_exact",
        exact_lookup_fixture,
    )
    plan = plan_knowledge_query(KnowledgeQuery("IEC-61850", limit=1))
    step = next(value for value in plan.steps if value.channel == "exact")

    result = knowledge_search_module._exact_rankings(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    assert result == ({}, [], 0, False, ())
    assert observed_limits == [step.candidate_limit]


# endregion [02]
