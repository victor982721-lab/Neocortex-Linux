"""Evidence-role preference preserves witnesses and precedes the final window."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_search_content
from neocortex.knowledge.knowledge_contracts import RevisionState
from neocortex.knowledge.knowledge_search import fuse_evidence_rankings
from neocortex.semantic import semantic_search_service
from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    LexicalRanking,
    query_term_support,
)
from neocortex.semantic.semantic_service_contracts import SemanticRanking
from tests.test_knowledge_search import _candidate
from tests.test_semantic_service import _calibrated_ranking_hit


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_QUERY = "Encuentra el registro del incidente durante el montaje"
_RECORDED = "El registro confirmó que el incidente ocurrió durante el montaje."
_NONRECORDS = (
    "No se presentó incidente durante el montaje.",
    "Esta guía no es un registro de un incidente ni demuestra su ejecución.",
)


def _supported_candidate(name: str, *, section: str, ranking: str, rank: int, text: str):
    candidate = _candidate(
        evidence_id=name, section_id=section, start_char=0, end_char=100,
        ranking=ranking, source_rank=rank, snippet=text,
    )
    return replace(candidate, signal=replace(
        candidate.signal, raw_score=0.91 if rank == 1 else 0.71,
        generation=7, model_signature="fixture-query-model",
        query_support=query_term_support(_QUERY, text, basis="scored_chunk"),
    ))


@pytest.mark.parametrize("nonrecord", _NONRECORDS)
@pytest.mark.parametrize("limit", (1, 2))
def test_knowledge_role_tier_precedes_limit_and_retains_demoted_evidence_when_room_exists(
    nonrecord: str, limit: int,
) -> None:
    caution = _supported_candidate("caution", section="1", ranking="semantic_text", rank=1, text=nonrecord)
    corroborating = replace(caution, signal=replace(caution.signal, source="fts_pdf", raw_score=7.25))
    recorded = _supported_candidate("recorded", section="2", ranking="semantic_text", rank=2, text=_RECORDED)

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (caution, recorded), "fts_pdf": (corroborating,)},
        limit=limit, max_per_resource=3, min_section_distance=0,
    )

    assert hits[0].evidence == recorded.evidence
    assert hits[0].fused_score == pytest.approx(1 / 62)
    assert hits[0].signals[0].raw_score == recorded.signal.raw_score
    assert hits[0].signals[0].evidence == recorded.evidence
    assert omitted == (1 if limit == 1 else 0)
    if limit == 2:
        assert len(hits) == 2 and hits[1].evidence == caution.evidence
        assert hits[1].fused_score == pytest.approx(2 / 61)
        assert hits[1].fused_score > hits[0].fused_score
        assert {signal.source: signal.raw_score for signal in hits[1].signals} == {
            "semantic_text": 0.91, "fts_pdf": 7.25,
        }
        assert all(signal.generation == 7 for signal in hits[1].signals)


@pytest.mark.parametrize("unassessed", (False, True))
def test_knowledge_one_clean_or_unassessed_contribution_prevents_counterevidence_contagion(
    unassessed: bool,
) -> None:
    caution = _supported_candidate("caution", section="1", ranking="semantic_text", rank=1, text=_NONRECORDS[0])
    clean = _supported_candidate("clean", section="1", ranking="fts_pdf", rank=1, text=_RECORDED)
    clean = replace(clean, evidence=replace(clean.evidence, start_char=80, end_char=180))
    if unassessed:
        clean = replace(clean, signal=replace(clean.signal, query_support={}))
    other = _supported_candidate("other", section="2", ranking="semantic_text", rank=2, text=_RECORDED)

    hits, _ = fuse_evidence_rankings(
        {"semantic_text": (caution, other), "fts_pdf": (clean,)},
        limit=2, max_per_resource=3, min_section_distance=0,
    )

    assert hits[0].fused_score == pytest.approx(2 / 61)
    assert hits[1].evidence == other.evidence
    signals = {signal.source: signal for signal in hits[0].signals}
    assert signals["semantic_text"].query_support["role_counterevidence"]
    assert not signals["fts_pdf"].query_support.get("role_counterevidence")
    assert signals["semantic_text"].evidence == caution.evidence
    assert signals["fts_pdf"].evidence == clean.evidence
    assert signals["semantic_text"].evidence.start_char != signals["fts_pdf"].evidence.start_char


def test_role_tiers_do_not_mix_historical_and_current_revision_signals() -> None:
    current = _supported_candidate("current", section="1", ranking="semantic_text", rank=2, text=_RECORDED)
    historical = _supported_candidate("old", section="1", ranking="fts_pdf", rank=1, text=_NONRECORDS[0])
    historical = replace(
        historical, revision=replace(historical.revision, revision_id="revision:old", state=RevisionState.HISTORICAL),
        evidence=replace(historical.evidence, revision_id="revision:old"),
        signal=replace(historical.signal, generation=6),
    )

    hits, omitted = fuse_evidence_rankings(
        {"semantic_text": (current,), "fts_pdf": (historical,)},
        limit=2, max_per_resource=3, min_section_distance=0, include_history=True,
    )

    assert omitted == 0 and len(hits) == 2
    assert [hit.revision.revision_id for hit in hits] == ["revision:fixture", "revision:old"]
    assert hits[0].signals[0].generation == 7 and hits[1].signals[0].generation == 6
    for hit, candidate in zip(hits, (current, historical), strict=True):
        assert hit.evidence == candidate.evidence
        assert len(hit.signals) == 1 and hit.signals[0].evidence.revision_id == hit.revision.revision_id
        assert hit.signals[0].raw_score == candidate.signal.raw_score
        assert "overlapping_evidence_merged" not in hit.warnings


def _semantic_sources(nonrecord: str, *, mixed: bool = False, unassessed: bool = False):
    caution_hit, caution = _calibrated_ranking_hit(1, source_kind="pdf", score=0.91)
    good_hit, good = _calibrated_ranking_hit(2, source_kind="pdf", score=0.71)
    caution_support = query_term_support(_QUERY, nonrecord, basis="scored_chunk")
    good_support = query_term_support(_QUERY, _RECORDED, basis="scored_chunk")
    caution = replace(caution, snippet=nonrecord, section_provenance={
        "query_support": caution_support, "snippet_query_support": caution_support,
    })
    good = replace(good, snippet=_RECORDED, section_provenance={
        "query_support": good_support, "snippet_query_support": good_support,
    })
    lexical_text = _RECORDED if mixed else nonrecord
    lexical_support = {} if unassessed else query_term_support(_QUERY, lexical_text, basis="fts_snippet")
    lexical_hit = replace(
        caution_hit, ref_id=101, entity_id="lexical:pdf:page:3", score=7.25,
        generation_id=0, indexed_model_signature="fixture-fts", query_model_signature=None,
        vector_space="lexical:fixture", provenance={"query_support": lexical_support},
    )
    lexical = replace(caution, hit=lexical_hit, section_id="3", snippet=lexical_text, section_provenance={})
    return (
        SemanticRanking("semantic_text", (caution_hit, good_hit), (caution, good), 2, True),
        LexicalRanking("pdf", None, LexicalAvailability.AVAILABLE, '"incidente"', (lexical,)),
        caution, good, lexical,
    )


@pytest.mark.parametrize("nonrecord", _NONRECORDS)
@pytest.mark.parametrize("limit", (1, 2))
def test_semantic_role_tier_materializes_candidates_before_final_limit_without_changing_scores(
    nonrecord: str, limit: int,
) -> None:
    semantic, lexical_ranking, caution, good, lexical = _semantic_sources(nonrecord)

    hits = semantic_search_service._resolve_fused_hits((semantic,), (lexical_ranking,), limit=limit)

    assert hits[0].primary_evidence is good
    assert hits[0].fused.score == pytest.approx(1 / 62)
    assert hits[0].fused.evidence[0].raw_score == 0.71
    assert hits[0].fused.evidence[0].witness is good
    if limit == 2:
        assert len(hits) == 2 and hits[1].fused.item_id == caution.hit.item_id
        assert hits[1].fused.score == pytest.approx(2 / 61)
        assert hits[1].fused.score > hits[0].fused.score
        by_ranking = {evidence.ranking: evidence for evidence in hits[1].fused.evidence}
        for name, expected in (("semantic_text", caution), ("fts_pdf", lexical)):
            evidence = by_ranking[name]
            assert evidence.witness is expected
            assert evidence.raw_score == expected.hit.score
            assert evidence.ref_id == expected.hit.ref_id
            assert evidence.generation_id == expected.hit.generation_id
            assert evidence.entity_id == expected.hit.entity_id
            assert evidence.indexed_model_signature == expected.hit.indexed_model_signature


@pytest.mark.parametrize("unassessed", (False, True))
def test_semantic_one_clean_or_unassessed_contribution_prevents_counterevidence_contagion(
    unassessed: bool,
) -> None:
    semantic, lexical_ranking, caution, good, lexical = _semantic_sources(
        _NONRECORDS[0], mixed=True, unassessed=unassessed,
    )

    hits = semantic_search_service._resolve_fused_hits((semantic,), (lexical_ranking,), limit=2)

    assert hits[0].fused.item_id == caution.hit.item_id
    assert hits[0].fused.score == pytest.approx(2 / 61)
    assert hits[1].primary_evidence is good
    witnesses = {evidence.ranking: evidence.witness for evidence in hits[0].fused.evidence}
    assert witnesses == {"semantic_text": caution, "fts_pdf": lexical}
    assert caution.section_provenance["query_support"]["role_counterevidence"]
    assert not lexical.hit.provenance["query_support"].get("role_counterevidence")


def test_query_support_exposes_missing_requested_witnesses_without_dropping_related_text() -> None:
    text = "El incidente se registró durante el montaje."
    support = query_term_support("¿Quién autorizó el montaje después del incidente?", text, basis="scored_chunk")

    assert support["role_counterevidence"] == []
    assert support["requested_witness_checks"]["status"] == "missing"
    assert support["requested_witness_checks"]["retrieval_disposition"] == "related_evidence_only"
    assert support["interpretation"] == "literal_overlap_not_entailment"
    assert support["matched_terms"]


@pytest.mark.parametrize("database_exists", (False, True))
@pytest.mark.parametrize(
    ("intent", "reason"),
    (("explicit_textual", "textual_query_routed_away_from_clip"),
     ("ambiguous", "ambiguous_query_requires_text_evidence")),
)
def test_intentional_image_omission_is_complete_before_database_or_model_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_exists: bool, intent: str, reason: str,
) -> None:
    probes = []

    def unavailable(*_args, **_kwargs):
        probes.append("model")
        return False

    def forbidden_backend(*_args, **_kwargs):
        raise AssertionError("deliberate image routing omission must never load a backend")

    monkeypatch.setattr(semantic_search_service, "registered_model_available", unavailable)
    monkeypatch.setattr(semantic_search_service, "indexed_model_available", unavailable)
    ranking = semantic_search_service.image_search_ranking(
        tmp_path / "unopened.sqlite3", database_exists=database_exists, query="registro de presión",
        cache=tmp_path / "cache", local_files_only=True, threads=None, limit=3, max_vectors=10,
        backend_factory=forbidden_backend, query_intent=intent, allow_ambiguous_images=False,
    )

    assert probes == [] and ranking.available is True and ranking.complete is True
    assert ranking.hits == () and ranking.scanned == 0
    assert ranking.provenance["image_query_routing"]["reason"] == reason
    report = knowledge_search_content._semantic_result_report(
        "semantic_image", ranking, 0, candidate_limit=3, clock=lambda: 1,
        started_ns=0, duration_ns=lambda clock, start: clock() - start,
    )
    assert report.executed is False and report.available is True and report.complete is True
    assert report.reason == reason
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("database_exists", (False, True))
def test_explicit_visual_request_still_requires_available_database_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_exists: bool,
) -> None:
    probes = []

    def unavailable(*_args, **_kwargs):
        probes.append("model")
        return False

    def forbidden_backend(*_args, **_kwargs):
        raise AssertionError("missing image owner/model must not load a backend")

    monkeypatch.setattr(semantic_search_service, "registered_model_available", unavailable)
    monkeypatch.setattr(semantic_search_service, "indexed_model_available", unavailable)
    ranking = semantic_search_service.image_search_ranking(
        tmp_path / "unopened.sqlite3", database_exists=database_exists, query="foto del equipo",
        cache=tmp_path / "cache", local_files_only=True, threads=None, limit=3, max_vectors=10,
        backend_factory=forbidden_backend, query_intent="explicit_visual",
    )

    assert ranking.available is False and ranking.complete is False
    assert ranking.unavailable_reason == ("clip_models_not_indexed" if database_exists else "semantic_index_missing")
    assert len(probes) == (1 if database_exists else 0)
    assert ranking.hits == () and tuple(tmp_path.iterdir()) == ()
