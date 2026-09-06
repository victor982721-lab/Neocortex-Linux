"""Original modality intent and exact per-contribution retrieval witnesses."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_search_content
from neocortex.knowledge.knowledge_contracts import RevisionState
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    RetrievalMode,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search import fuse_evidence_rankings
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge
from neocortex.semantic import (
    image_retrieval_calibration,
    semantic_search_service,
    semantic_service,
)
from neocortex.semantic.semantic_lexical import LexicalAvailability, LexicalRanking
from neocortex.semantic.semantic_models import EmbeddingModelSpec
from neocortex.semantic.semantic_service_contracts import SemanticRanking
from tests.test_knowledge_search import _candidate
from tests.test_semantic_service import (
    _ConstantBackend,
    _calibrated_image_ranking_hit,
    _calibrated_ranking_hit,
    _fixture_image_calibration,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _unexpected_call(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("routing regression attempted an unmocked channel or state access")


def _local_image_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[_ConstantBackend]]:
    """Keep the real query routing/backend interface, never an actual model or DB."""
    database = tmp_path / "semantic.sqlite3"
    database.write_bytes(b"fixture: must never be opened as SQLite")
    backends: list[_ConstantBackend] = []

    def available(path: Path, _model: EmbeddingModelSpec) -> bool:
        assert path == database
        return True

    def backend(model: EmbeddingModelSpec, **kwargs: object) -> _ConstantBackend:
        assert kwargs["local_files_only"] is True
        instance = _ConstantBackend(model)
        backends.append(instance)
        return instance

    def ranked(path: Path, **kwargs: object) -> SemanticRanking:
        assert path == database
        assert kwargs["name"] == "semantic_image"
        hit, resolved = _calibrated_image_ranking_hit(91, score=0.8)
        return SemanticRanking(
            name="semantic_image", hits=(hit,), resolved=(resolved,),
            scanned=1, complete=True, provenance=kwargs["provenance"],
        )

    monkeypatch.setattr(semantic_search_service, "registered_model_available", available)
    monkeypatch.setattr(semantic_search_service, "indexed_model_available", available)
    monkeypatch.setattr(semantic_search_service, "semantic_ranking", ranked)
    monkeypatch.setattr(semantic_search_service, "text_search_rankings", _unexpected_call)
    monkeypatch.setattr(semantic_service, "_backend", backend)
    monkeypatch.setattr(semantic_service, "search_lexical_sources", _unexpected_call)
    monkeypatch.setattr(
        image_retrieval_calibration, "load_image_retrieval_calibration",
        lambda _path: _fixture_image_calibration(),
    )
    monkeypatch.setattr(
        image_retrieval_calibration, "current_image_processing_signature", lambda _path: None
    )
    return database, backends


@pytest.mark.parametrize(
    ("query", "source_kinds", "formats", "intent", "reason"),
    (
        ("documento presión interna", (), (), "explicit_textual", "textual_query_routed_away_from_clip"),
        ("transformador presión interna", (), (), "ambiguous", "ambiguous_query_requires_text_evidence"),
        ("transformador presión interna", ("pdf", "image"), (), "ambiguous", "ambiguous_query_requires_text_evidence"),
        ("transformador presión interna", (), ("pdf", "png"), "ambiguous", "ambiguous_query_requires_text_evidence"),
        ("foto del transformador", (), (), "explicit_visual", None),
        ("transformador presión interna", ("image",), (), "explicit_visual", None),
        ("transformador presión interna", (), ("png",), "explicit_visual", None),
        ("texto en la imagen del radiador", ("image",), (), "explicit_textual", "textual_query_routed_away_from_clip"),
        ("texto en la imagen del radiador", (), ("png",), "explicit_textual", "textual_query_routed_away_from_clip"),
        ("qué dice la imagen", (), (), "explicit_textual", "textual_query_routed_away_from_clip"),
        ("imagen del texto", (), (), "explicit_visual", None),
    ),
)
def test_knowledge_original_intent_survives_isolated_image_step_and_public_semantic_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str,
    source_kinds: tuple[str, ...], formats: tuple[str, ...], intent: str, reason: str | None,
) -> None:
    database, backends = _local_image_seams(tmp_path, monkeypatch)
    plan = plan_knowledge_query(KnowledgeQuery(query, source_kinds=source_kinds, formats=formats))
    image_step = next(step for step in plan.steps if step.ranking_name == "semantic_image")
    if intent == "explicit_textual":
        assert any(step.ranking_name == "semantic_text" for step in plan.steps)
    paths = KnowledgeStatePaths(
        **{name: tmp_path / f"{name}.sqlite3" for name in (
            "inventory", "framework", "catalog", "pdf", "docx", "office", "audio",
            "image", "semantic", "code",
        )}
    )
    assert paths.semantic == database
    context = knowledge_search_content._SemanticRankingContext(
        paths=paths, plan=plan, clock=lambda: 17,
        duration_ns=lambda clock, started: clock() - started,
        materialize_candidate=_unexpected_call,
        materialize_discovery_signal=_unexpected_call,
        semantic_search=semantic_service.search_semantic_index,
        cancellation=SQLiteCancellationBridge(None),
        reraise_captured_cancellation=_unexpected_call,
        sqlite_error_type=sqlite3.Error, evidence_mode=RetrievalMode.EVIDENCE,
    )

    result = knowledge_search_content._search_semantic_step(
        context, image_step, vector_budget=12, include_title=False
    )

    assert len(result.rankings) == 1
    ranking = result.rankings[0]
    routing = ranking.provenance["image_query_routing"]
    assert routing["intent"] == intent
    assert routing["executed"] is (reason is None)
    assert routing["reason"] == reason
    assert len(backends) == (1 if reason is None else 0)
    if backends:
        assert len(backends[0].requests) == 1
        assert backends[0].model.model_signature == semantic_service.clip_text_model().model_signature
        assert ranking.scanned == 1 and ranking.hits
    else:
        assert ranking.scanned == 0 and not ranking.hits
        report = knowledge_search_content._semantic_result_report(
            "semantic_image", ranking, 0, candidate_limit=image_step.candidate_limit,
            clock=lambda: 17, started_ns=0, duration_ns=lambda clock, started: clock() - started,
        )
        assert report.executed is False
        assert report.available is True and report.complete is True
        assert report.reason == reason and report.vectors_scanned == 0
    assert database.read_bytes() == b"fixture: must never be opened as SQLite"


@pytest.mark.parametrize(
    "query", ("transformador presión interna", "documento presión interna", "texto en la imagen del radiador")
)
def test_direct_image_only_api_default_remains_an_explicit_visual_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    database, backends = _local_image_seams(tmp_path, monkeypatch)

    result = semantic_service.search_semantic_index(
        tmp_path, query, semantic_database=database, include_text=False,
        include_images=True, include_lexical=False, model_cache=tmp_path / "cache",
    )

    assert len(backends) == 1
    assert len(backends[0].requests) == 1
    routing = result.rankings[0].provenance["image_query_routing"]
    assert routing["intent"] == "explicit_visual" and routing["executed"] is True


@pytest.mark.parametrize(
    ("option", "value", "error"),
    (
        ("image_query_intent", "unknown", "image_query_intent"),
        ("image_query_intent", "", "image_query_intent"),
        ("image_query_intent", 1, "image_query_intent"),
        ("image_query_intent", [], "image_query_intent"),
        ("allow_ambiguous_images", 0, "allow_ambiguous_images"),
        ("allow_ambiguous_images", "false", "allow_ambiguous_images"),
        ("allow_ambiguous_images", None, "allow_ambiguous_images"),
    ),
)
def test_invalid_image_routing_contract_fails_before_state_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, value: object, error: str,
) -> None:
    monkeypatch.setattr(semantic_search_service, "_prepare_search_context", _unexpected_call)

    with pytest.raises(ValueError, match=error):
        semantic_service.search_semantic_index(tmp_path, "transformador", **{option: value})

    assert tuple(tmp_path.iterdir()) == ()


def test_semantic_fusion_binds_every_contribution_to_its_exact_score_and_witness() -> None:
    body_hit, body = _calibrated_ranking_hit(13, source_kind="pdf", score=0.813)
    body_hit = replace(body_hit, item_id="item:shared", entity_id="shared-entity")
    body = replace(
        body, hit=body_hit, path="/tmp/shared.pdf", section_id="3", start_char=10,
        end_char=80, snippet="presión en otro contexto",
        section_provenance={"snippet_query_support": {
            "term_coverage": 0.5, "phrase_match": False, "minimum_span_terms": 1,
        }},
    )
    lexical_hit = replace(
        body_hit, indexed_model_signature="fixture-fts", query_model_signature=None,
        vector_space="lexical:fixture", generation_id=0, score=2.75,
        provenance={"query_support": {
            "term_coverage": 1.0, "phrase_match": True, "minimum_span_terms": 2,
        }},
    )
    lexical = replace(
        body, hit=lexical_hit, section_id="7", start_char=None, end_char=None,
        snippet="presión interna", section_provenance={},
    )
    semantic = SemanticRanking("semantic_text", (body_hit,), (body,), 1, True)
    lexical_ranking = LexicalRanking(
        "pdf", None, LexicalAvailability.AVAILABLE, '"presión" AND "interna"', (lexical,)
    )

    result, = semantic_search_service._resolve_fused_hits((semantic,), (lexical_ranking,), limit=3)

    assert result.primary_evidence is lexical
    assert result.snippet == lexical.snippet
    assert result.path == lexical.path
    by_ranking = {evidence.ranking: evidence for evidence in result.fused.evidence}
    assert set(by_ranking) == {"semantic_text", "fts_pdf"}
    for name, expected in (("semantic_text", body), ("fts_pdf", lexical)):
        evidence = by_ranking[name]
        assert evidence.witness is expected
        assert evidence.raw_score == expected.hit.score
        assert evidence.ref_id == expected.hit.ref_id
        assert evidence.entity_id == expected.hit.entity_id
        assert evidence.generation_id == expected.hit.generation_id
        assert evidence.indexed_model_signature == expected.hit.indexed_model_signature
        assert evidence.query_model_signature == expected.hit.query_model_signature
    assert by_ranking["semantic_text"].contribution == by_ranking["fts_pdf"].contribution
    assert result.fused.score == pytest.approx(sum(value.contribution for value in by_ranking.values()))
    assert body_hit.score == 0.813 and lexical_hit.score == 2.75


def test_knowledge_overlap_merge_preserves_each_signals_distinct_locator() -> None:
    text = _candidate(
        evidence_id="text-window", section_id="3", start_char=0, end_char=100,
        ranking="semantic_text", source_rank=1, snippet="ventana semántica concreta",
    )
    lexical = _candidate(
        evidence_id="lexical-window", section_id="3", start_char=80, end_char=160,
        ranking="fts_pdf", source_rank=1, snippet="ventana lexical diferente",
    )

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (text,), "fts_pdf": (lexical,)},
        limit=3, max_per_resource=3, min_section_distance=0,
    )

    assert len(hits) == 1 and omitted == 0
    assert "overlapping_evidence_merged" in hits[0].warnings
    signals = {signal.source: signal for signal in hits[0].signals}
    assert set(signals) == {"semantic_text", "fts_pdf"}
    assert signals["semantic_text"].evidence == text.evidence
    assert signals["fts_pdf"].evidence == lexical.evidence
    assert signals["semantic_text"].evidence != signals["fts_pdf"].evidence
    assert signals["semantic_text"].evidence.start_char == 0
    assert signals["fts_pdf"].evidence.start_char == 80
    assert signals["semantic_text"].evidence.snippet == "ventana semántica concreta"
    assert signals["fts_pdf"].evidence.snippet == "ventana lexical diferente"


def test_overlapping_current_and_historical_revisions_remain_separate_hits() -> None:
    current = _candidate(
        evidence_id="current-window", section_id="3", start_char=0, end_char=100,
        ranking="semantic_text", source_rank=1, snippet="misma frase en versiones distintas",
    )
    historical = _candidate(
        evidence_id="historical-window", section_id="3", start_char=0, end_char=100,
        ranking="fts_pdf", source_rank=1, snippet="misma frase en versiones distintas",
        revision_state=RevisionState.HISTORICAL,
    )
    historical = replace(
        historical, revision=replace(historical.revision, revision_id="revision:old"),
        evidence=replace(historical.evidence, revision_id="revision:old"),
    )

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (current,), "fts_pdf": (historical,)},
        limit=3, max_per_resource=3, min_section_distance=0, include_history=True,
    )

    assert len(hits) == 2 and omitted == 0
    by_revision = {hit.revision.revision_id: hit for hit in hits}
    assert set(by_revision) == {"revision:fixture", "revision:old"}
    for candidate in (current, historical):
        hit = by_revision[candidate.revision.revision_id]
        assert hit.evidence == candidate.evidence
        assert len(hit.signals) == 1
        assert hit.signals[0].source == candidate.signal.source
        assert hit.signals[0].evidence == candidate.evidence
        assert "overlapping_evidence_merged" not in hit.warnings
