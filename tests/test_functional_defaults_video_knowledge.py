"""Video title/frame routing at the Semantic-to-Knowledge boundary."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path
from typing import Any

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    RetrievalMode,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search_contracts import ResourceDiscoverySignal
from neocortex.knowledge.knowledge_search_content import _evidence_from_resolved
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    ResolvedSearchHit,
    SearchHit,
)
from neocortex.semantic.semantic_search_repository import (
    _ResolvedSearchSource,
    _resolved_text_search_hit,
    _search_sql,
)
from neocortex.semantic.semantic_service import SemanticRanking, SemanticSearchResult
from neocortex.semantic.semantic_sources import SEMANTIC_TITLE_SECTION_KIND
from neocortex.semantic.semantic_models import fingerprint_text


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_VIDEO_IDENTITY = "00000000000000000000000000000001:00000000000000000000000000000002"


def _snapshot() -> KnowledgeSnapshot:
    owners = tuple(
        OwnerSnapshot(
            name,
            OwnerAvailability.AVAILABLE if name == "semantic" else OwnerAvailability.ABSENT,
            1,
            1,
        )
        for name in (
            "pdf",
            "docx",
            "office",
            "audio",
            "video",
            "semantic",
            "code",
            "catalog",
            "inventory",
        )
    )
    return KnowledgeSnapshot.create(
        source_version="video-knowledge-fixture-v1",
        captured_at_utc="2026-09-11T12:00:00Z",
        captured_monotonic_ns=1,
        owners=owners,
    )


def _resolved(
    *,
    ref_id: int,
    entity_id: str,
    section_kind: str | None,
    section_id: str | None,
    locator: dict[str, object] | None,
    modality: EmbeddingModality = EmbeddingModality.TEXT,
    source_kind: str = "video",
) -> ResolvedSearchHit:
    provenance: dict[str, object] = {"adapter": "semantic-video-source-v1"}
    if locator is not None:
        provenance["locator"] = locator
    return ResolvedSearchHit(
        hit=SearchHit(
            ref_id=ref_id,
            entity_id=entity_id,
            item_id=f"item:{source_kind}:fixture",
            indexed_model_signature="fixture-model",
            vector_space="fixture-space",
            modality=modality,
            score=0.8,
            generation_id=7,
            query_model_signature="fixture-model",
        ),
        path=f"/fixtures/{source_kind}.mp4" if source_kind == "video" else "/fixtures/image.png",
        source_kind=source_kind,
        source_identity=_VIDEO_IDENTITY,
        section_kind=section_kind,
        section_id=section_id,
        start_char=0 if section_kind is not None else None,
        end_char=24 if section_kind is not None else None,
        snippet="fixture evidence",
        source_revision={
            "birthtime_ns": -1,
            "processing_signature": "fixture-video-v1",
            "last_seen_run_id": 3,
        },
        section_provenance=provenance,
        source_status="complete",
    )


def _ranking(name: str, resolved: ResolvedSearchHit) -> SemanticRanking:
    return SemanticRanking(
        name=name,
        hits=(resolved.hit,),
        resolved=(resolved,),
        scanned=1,
        complete=True,
    )


def _fake_search_result(
    query: str,
    *,
    body: ResolvedSearchHit | None = None,
    title: ResolvedSearchHit | None = None,
    image: ResolvedSearchHit | None = None,
) -> SemanticSearchResult:
    rankings = tuple(
        ranking
        for ranking in (
            None if body is None else _ranking("semantic_text", body),
            None if title is None else _ranking("semantic_title", title),
            None if image is None else _ranking("semantic_image", image),
        )
        if ranking is not None
    )
    return SemanticSearchResult(query, rankings, (), ())


def test_video_title_is_title_scope_only_and_legacy_readback_is_advisory() -> None:
    content_sql = _search_sql(EmbeddingModality.TEXT, 1, text_scope="content")
    title_sql = _search_sql(EmbeddingModality.TEXT, 1, text_scope="title")
    assert "NOT IN ('semantic_metadata_title','video_metadata_title')" in content_sql
    assert "IN ('semantic_metadata_title','video_metadata_title')" in title_sql

    body = _resolved(
        ref_id=1,
        entity_id="frame-1",
        section_kind="video_frame_ocr",
        section_id="0",
        locator={"kind": "video_frame", "timestamp_ms": 2500},
    )
    legacy_title = _resolved(
        ref_id=2,
        entity_id="title-1",
        section_kind="video_metadata_title",
        section_id="title",
        locator={"kind": "video_title"},
    )
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "fixture",
            retrieval_mode=RetrievalMode.DISCOVERY,
            source_kinds=("video",),
            limit=5,
            max_vectors=10,
        )
    )
    calls: list[dict[str, Any]] = []

    def semantic_search(_state: Path, query: str, **kwargs: object) -> SemanticSearchResult:
        calls.append(dict(kwargs))
        return _fake_search_result(query, body=body, title=legacy_title)

    original = knowledge_search.semantic_service.search_semantic_index
    knowledge_search.semantic_service.search_semantic_index = semantic_search
    try:
        rankings, discovery, reports = knowledge_search._semantic_rankings(
            KnowledgeStatePaths.from_directory(Path("/tmp/video-knowledge-fixture")),
            plan,
            _snapshot(),
        )
    finally:
        knowledge_search.semantic_service.search_semantic_index = original

    assert set(rankings) == {"semantic_text"}
    assert rankings["semantic_text"][0].evidence.section_kind == "video_frame_ocr"
    assert rankings["semantic_text"][0].evidence.start_ms == 2500
    assert len(discovery) == 1
    assert isinstance(discovery[0], ResourceDiscoverySignal)
    assert discovery[0].resource.owner == "video"
    assert "advisory_metadata_only" in discovery[0].warnings
    title_report = next(report for report in reports if report.name == "semantic_title")
    assert title_report.complete is True
    assert title_report.returned == 1
    assert len(calls) == 1


def test_legacy_video_title_readback_is_canonicalized_without_touching_cache_bytes() -> None:
    text = "Legacy title metadata"
    fingerprint = fingerprint_text(text)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """SELECT 1 AS member_id,7 AS generation_id,'fixture-model' AS model_signature,
        'text' AS modality,'text_chunk' AS entity_kind,'title-1' AS entity_id,
        'item:video:fixture' AS item_id,'fixture-space' AS vector_space,
        '/fixtures/legacy.mp4' AS path,'video' AS source_kind,
        'fixture-identity' AS source_identity,'{}' AS item_provenance_json,
        '{}' AS current_item_provenance_json,'{}' AS source_revision_json,
        1 AS published_revision_id,1 AS current_revision_id,
        'video_metadata_title' AS section_kind,'title' AS section_id,
        0 AS start_char,20 AS end_char,? AS text_zlib,? AS section_provenance_json,
        ? AS content_xxh3_128,? AS content_bytes,? AS content_xxh3_64_guard""",
        (
            zlib.compress(text.encode("utf-8")),
            '{"adapter":"semantic-video-source-v1","locator":{"kind":"video_title"}}',
            fingerprint.xxh3_128,
            fingerprint.byte_count,
            fingerprint.xxh3_64_guard,
        ),
    ).fetchone()
    assert row is not None
    resolved = _resolved_text_search_hit(
        SearchHit(
            ref_id=1,
            entity_id="title-1",
            item_id="item:video:fixture",
            indexed_model_signature="fixture-model",
            vector_space="fixture-space",
            modality=EmbeddingModality.TEXT,
            score=0.8,
            generation_id=7,
        ),
        _ResolvedSearchSource(
            row=row,
            source_revision={"processing_signature": "fixture-video-v1"},
            source_status="complete",
            published_revision_id=1,
            current_revision_id=1,
        ),
        snippet_chars=128,
        query="legacy",
    )

    assert resolved.section_kind == SEMANTIC_TITLE_SECTION_KIND
    assert resolved.section_id == "title"
    assert resolved.section_provenance["legacy_section_kind"] == "video_metadata_title"
    assert resolved.section_provenance["advisory_only"] is True
    connection.close()


def test_malformed_video_frame_does_not_invalidate_independent_image_ranking() -> None:
    malformed_frame = _resolved(
        ref_id=10,
        entity_id="malformed-frame",
        section_kind="video_frame_ocr",
        section_id="0",
        locator={"kind": "video_frame"},
    )
    image = _resolved(
        ref_id=11,
        entity_id="image-1",
        section_kind=None,
        section_id=None,
        locator=None,
        modality=EmbeddingModality.IMAGE,
        source_kind="image",
    )
    plan = plan_knowledge_query(
        KnowledgeQuery("fixture", retrieval_mode=RetrievalMode.EVIDENCE, limit=5, max_vectors=10)
    )

    def semantic_search(_state: Path, query: str, **kwargs: object) -> SemanticSearchResult:
        return _fake_search_result(
            query,
            body=malformed_frame if kwargs.get("include_text") is True else None,
            image=image if kwargs.get("include_images") is True else None,
        )

    original = knowledge_search.semantic_service.search_semantic_index
    knowledge_search.semantic_service.search_semantic_index = semantic_search
    try:
        rankings, _discovery, reports = knowledge_search._semantic_rankings(
            KnowledgeStatePaths.from_directory(Path("/tmp/video-knowledge-fixture")),
            plan,
            _snapshot(),
        )
    finally:
        knowledge_search.semantic_service.search_semantic_index = original

    assert "semantic_text" not in rankings
    assert len(rankings["semantic_image"]) == 1
    text_report = next(report for report in reports if report.name == "semantic_text")
    image_report = next(report for report in reports if report.name == "semantic_image")
    assert text_report.complete is False
    assert text_report.reason == "owner_read_failed:ValueError"
    assert image_report.complete is True


@pytest.mark.parametrize(
    "section_kind", ("video_metadata_title", SEMANTIC_TITLE_SECTION_KIND, "video_frame_ocr")
)
def test_video_title_cannot_become_temporal_evidence_from_an_incidental_timestamp(
    section_kind: str,
) -> None:
    title = _resolved(
        ref_id=21,
        entity_id="title-with-incidental-time",
        section_kind=section_kind,
        section_id="title",
        locator={"kind": "video_title", "timestamp_ms": 1234},
    )
    with pytest.raises(ValueError, match="advisory-only, not frame evidence"):
        _evidence_from_resolved(
            title,
            resource_id="resource:video:title",
            revision_id="revision:video:title",
            generation=1,
            producer="semantic-v6",
            int_provenance_fn=lambda _value, _key: None,
            evidence_ref_type=EvidenceRef,
            extracted_method=EvidenceMethod.EXTRACTED,
        )
