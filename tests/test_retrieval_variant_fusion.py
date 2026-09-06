"""Named-query observations share one ranking and one bounded vector budget."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.semantic import semantic_search_service, semantic_service
from neocortex.semantic.semantic_backends import TextTokenLimitExceededError
from neocortex.semantic.semantic_service_contracts import SemanticRanking
from tests.test_semantic_service import _ConstantBackend, _calibrated_ranking_hit


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")
_QUERY = "equipos de enfriamiento sin presión después del 20 de junio"


def _variant(name: str) -> dict[str, object]:
    return {"variant_id": name, "effective_query": f"{name}: {_QUERY}", "original_query": _QUERY}


def _witness(item: str, entity: str, ref: int, score: float, *, generation: int = 7):
    hit, resolved = _calibrated_ranking_hit(ref, source_kind="pdf", score=score)
    hit = replace(hit, item_id=item, entity_id=entity, generation_id=generation)
    return hit, replace(
        resolved, hit=hit, path=f"/tmp/{item}.pdf", source_identity=item,
        section_id=entity, snippet=f"witness {entity}",
        section_provenance={"query_support": {"interpretation": "literal_overlap_not_entailment"}},
    )


def _ranking(witnesses, *, complete: bool = True, scanned: int = 5, diagnostics=()):
    return SemanticRanking(
        "semantic_text", tuple(hit for hit, _ in witnesses), tuple(value for _, value in witnesses),
        scanned, complete, cutoff_reason=None if complete else "max_vectors_reached",
        next_cursor=None if complete else 99,
        provenance={"fixture_marker": "preserved", "target_diagnostics": list(diagnostics)},
    )


def _target(hit, *, raw_rank: int | None, complete: bool = True):
    return {
        "item_id": hit.item_id, "ref_id": hit.ref_id, "entity_id": hit.entity_id,
        "generation_id": hit.generation_id, "raw_score": hit.score,
        "raw_rank": raw_rank, "rank_is_global": complete,
        "observed_in_published_scope": True, "within_candidate_window": True,
        "candidate_rank": raw_rank, "stage": "candidate_selected",
    }


@pytest.mark.parametrize("evidence_mode", (False, True))
def test_variant_union_selects_one_maximum_observation_per_item_or_evidence(evidence_mode: bool) -> None:
    original = _witness("item-target", "chunk-original", 11, 0.61)
    report = _witness("item-target", "chunk-report", 22, 0.91)
    aliases = _witness("item-target", "chunk-aliases", 33, 0.81)
    inputs = tuple((_variant(name), _ranking((witness,))) for name, witness in (
        ("original", original), ("report_context", report), ("cooling_pressure_aliases", aliases),
    ))

    result = semantic_search_service._merge_text_query_variants(inputs, limit=5, evidence_mode=evidence_mode)

    assert result.name == "semantic_text" and result.scanned == 15 and result.complete is True
    assert result.provenance["fixture_marker"] == "preserved"
    assert len(result.hits) == (3 if evidence_mode else 1)
    assert result.hits[0].score == 0.91 and result.hits[0].ref_id == 22
    assert result.resolved[0].snippet == report[1].snippet
    aggregation = result.provenance["candidate_selection"]
    assert aggregation["aggregation"] == ("best_single_variant_per_evidence" if evidence_mode else "best_single_variant_per_item")
    assert aggregation["variants_executed"] == 3 and aggregation["union_candidates"] == len(result.hits)
    observations = result.hits[0].provenance["retrieval_query_variants"]
    assert observations["winning_variant"]["variant_id"] == "report_context"
    assert observations["aggregation"] == "best_single_observation_no_additional_channel_votes"
    assert observations["unlisted_variant_scores"] == "not_observed_in_that_candidate_window"
    assert len(observations["observations"]) == (1 if evidence_mode else 3)
    if not evidence_mode:
        assert [(row["ref_id"], row["entity_id"], row["generation_id"], row["raw_score"])
                for row in observations["observations"]] == [
            (11, "chunk-original", 7, 0.61), (22, "chunk-report", 7, 0.91), (33, "chunk-aliases", 7, 0.81),
        ]
    assert [pair[1].hits[0].score for pair in inputs] == [0.61, 0.91, 0.81]
    assert all("retrieval_query_variants" not in pair[1].hits[0].provenance for pair in inputs)


def test_equal_score_ties_keep_original_observation_and_one_rrf_contribution() -> None:
    witness = _witness("item-target", "chunk-stable", 11, 0.81)
    variants = tuple((_variant(name), _ranking((witness,))) for name in (
        "original", "report_context", "cooling_pressure_aliases",
    ))

    merged = semantic_search_service._merge_text_query_variants(variants, limit=3, evidence_mode=True)
    fused, = semantic_search_service._resolve_fused_hits((merged,), (), limit=3)

    assert len(merged.hits) == 1
    metadata = merged.hits[0].provenance["retrieval_query_variants"]
    assert metadata["winning_variant"]["variant_id"] == "original"
    assert len(metadata["observations"]) == 3
    assert len(fused.fused.evidence) == 1 and fused.fused.evidence[0].ranking == "semantic_text"
    assert fused.fused.score == pytest.approx(1 / 61)
    assert fused.fused.evidence[0].raw_score == 0.81
    assert fused.fused.evidence[0].ref_id == 11 and fused.fused.evidence[0].generation_id == 7


def test_candidate_limit_follows_union_and_target_rank_names_its_query_variant_basis() -> None:
    a_original = _witness("item-a", "chunk-a", 1, 0.8)
    b_original = _witness("item-b", "chunk-b", 2, 0.7)
    b_report = _witness("item-b", "chunk-b", 2, 0.9)
    a_report = _witness("item-a", "chunk-a", 1, 0.75)
    c_alias = _witness("item-c", "chunk-c", 3, 0.95)
    b_alias = _witness("item-b", "chunk-b", 2, 0.85)
    inputs = (
        (_variant("original"), _ranking((a_original, b_original), diagnostics=(_target(b_original[0], raw_rank=2),))),
        (_variant("report_context"), _ranking((b_report, a_report), diagnostics=(_target(b_report[0], raw_rank=1),))),
        (_variant("cooling_pressure_aliases"), _ranking((c_alias, b_alias), diagnostics=(_target(b_alias[0], raw_rank=2),))),
    )

    merged = semantic_search_service._merge_text_query_variants(inputs, limit=2, evidence_mode=False)

    assert [hit.item_id for hit in merged.hits] == ["item-c", "item-b"]
    assert merged.cutoff_reason == "top_k" and merged.cutoff_score == 0.9
    selection = merged.provenance["candidate_selection"]
    assert selection["union_candidates"] == 3 and selection["selected_candidates"] == 2
    diagnostic, = merged.provenance["target_diagnostics"]
    assert diagnostic["candidate_rank"] == 2 and diagnostic["raw_rank"] == 1
    assert diagnostic["raw_rank_basis"] == diagnostic["variant_id"] == "report_context"
    assert diagnostic["raw_score"] == 0.9 and diagnostic["ref_id"] == 2
    assert len(diagnostic["query_variant_diagnostics"]) == 3
    assert diagnostic["stage"] == "candidate_selected" and diagnostic["within_candidate_window"] is True
    assert inputs[0][1].provenance["target_diagnostics"][0]["raw_rank"] == 2


def test_partial_variant_does_not_turn_its_target_rank_into_a_global_claim() -> None:
    original = _witness("item-a", "chunk-a", 1, 0.8)
    partial = _witness("item-b", "chunk-b", 2, 0.9)
    inputs = (
        (_variant("original"), _ranking((original,), scanned=5)),
        (_variant("report_context"), _ranking((partial,), scanned=3, complete=False,
                                             diagnostics=(_target(partial[0], raw_rank=None, complete=False),))),
    )

    merged = semantic_search_service._merge_text_query_variants(inputs, limit=2, evidence_mode=False)

    assert merged.complete is False and merged.scanned == 8
    target, = merged.provenance["target_diagnostics"]
    assert target["rank_is_global"] is False and target["raw_rank"] is None
    assert target["raw_rank_basis"] == "report_context"
    assert target["candidate_rank"] == 1


def test_mixed_published_generations_for_one_model_are_rejected() -> None:
    original = _witness("item-a", "chunk-a", 1, 0.8, generation=7)
    changed = _witness("item-a", "chunk-a", 2, 0.9, generation=8)

    with pytest.raises(RuntimeError, match="generation changed between query variants"):
        semantic_search_service._merge_text_query_variants(
            ((_variant("original"), _ranking((original,))),
             (_variant("report_context"), _ranking((changed,)))),
            limit=2, evidence_mode=False,
        )
    assert original[0].generation_id == 7 and changed[0].generation_id == 8


class _QueryBackend(_ConstantBackend):
    def __init__(self, model, *, reject_optional: bool = False):
        super().__init__(model)
        self.reject_optional = reject_optional
        self.attempted_texts: list[str] = []

    def embed(self, requests):
        self.attempted_texts.extend(request.text for request in requests)
        if self.reject_optional and len(self.attempted_texts) > 1:
            raise TextTokenLimitExceededError("optional query fixture token limit")
        return super().embed(requests)


def _run_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, max_vectors: int,
    query: str = _QUERY, original_complete: bool = True, original_scanned: int = 5,
    reject_optional: bool = False, custom_model: bool = False,
):
    model = semantic_service.multilingual_text_model()
    if custom_model:
        model = replace(model, model_signature="other-compatible-fixture-model")
    backends = []
    calls = []
    raw_rankings = []

    def factory(selected_model, **kwargs):
        assert selected_model == model and kwargs["local_files_only"] is True
        backend = _QueryBackend(model, reject_optional=reject_optional)
        backends.append(backend)
        return backend

    def ranked(database, **kwargs):
        assert database == tmp_path / "unopened.sqlite3"
        assert kwargs["text_scope"] == "content"
        assert kwargs["name"] == "semantic_text" and kwargs["query_model"] == model
        assert len(kwargs["vector"]) == model.dimensions
        calls.append(kwargs)
        if original_scanned:
            hit, resolved = _witness("item-a", "chunk-a", 1, 0.65 + 0.1 * (len(calls) - 1))
            hit = replace(hit, indexed_model_signature=model.model_signature)
            ranking = _ranking(((hit, replace(resolved, hit=hit)),), scanned=original_scanned,
                               complete=original_complete if len(calls) == 1 else True)
        else:
            ranking = _ranking((), scanned=0)
        raw_rankings.append(ranking)
        return ranking

    monkeypatch.setattr(semantic_search_service, "indexed_model_available", lambda *_args: True)
    monkeypatch.setattr(semantic_search_service, "semantic_ranking", ranked)
    result = semantic_search_service.text_search_rankings(
        tmp_path / "unopened.sqlite3", database_exists=True, selected_model=model, query=query,
        cache=tmp_path / "cache", local_files_only=True, threads=None, limit=3,
        max_vectors=max_vectors, backend_factory=factory, include_title=False,
    )
    return result, backends, calls, raw_rankings


@pytest.mark.parametrize(("budget", "executed"), ((5, 1), (9, 1), (10, 2), (14, 2), (15, 3), (50, 3)))
def test_pressure_expansions_share_one_backend_and_the_original_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int, executed: int,
) -> None:
    result, backends, calls, originals = _run_loop(tmp_path, monkeypatch, max_vectors=budget)

    assert len(result) == len(backends) == 1 and result[0].name == "semantic_text"
    assert len(calls) == len(backends[0].attempted_texts) == executed <= 3
    assert [call["max_vectors"] for call in calls] == [budget - 5 * index for index in range(executed)]
    assert calls[0]["query"] == backends[0].attempted_texts[0] == _QUERY
    assert all(_QUERY in call["query"] for call in calls)
    assert result[0].scanned == executed * 5 <= budget
    assert result[0].complete is True
    assert len(result[0].provenance["query_variants"]) == executed
    assert len(result[0].provenance.get("query_variants_not_executed", ())) == 3 - executed
    assert result[0].hits[0].score == pytest.approx(0.65 + 0.1 * (executed - 1))
    assert originals[0].hits[0].score == 0.65
    assert "retrieval_query_variants" not in originals[0].hits[0].provenance
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(("scanned", "complete"), ((5, False), (0, True)))
def test_incomplete_or_empty_original_scope_never_starts_optional_query_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scanned: int, complete: bool,
) -> None:
    result, backends, calls, _ = _run_loop(
        tmp_path, monkeypatch, max_vectors=50, original_scanned=scanned, original_complete=complete,
    )

    assert len(backends) == len(calls) == len(backends[0].attempted_texts) == 1
    assert result[0].scanned == scanned and result[0].complete is complete
    assert len(result[0].provenance["query_variants_not_executed"]) == 2


@pytest.mark.parametrize(("query", "custom_model"), (("presión de memoria del worker", False), (_QUERY, True)))
def test_unrelated_query_or_nonexact_jina_model_does_not_expand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str, custom_model: bool,
) -> None:
    result, backends, calls, _ = _run_loop(
        tmp_path, monkeypatch, max_vectors=50, query=query, custom_model=custom_model,
    )

    assert len(result) == len(backends) == len(calls) == 1
    assert backends[0].attempted_texts == [query]
    assert "query_variants" not in result[0].provenance


def test_optional_token_limit_keeps_original_results_and_explicit_skip_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, backends, calls, originals = _run_loop(tmp_path, monkeypatch, max_vectors=50, reject_optional=True)

    assert len(backends) == len(calls) == 1 and len(backends[0].attempted_texts) == 3
    assert result[0].complete is True and result[0].scanned == 5
    assert result[0].hits[0].score == originals[0].hits[0].score == 0.65
    skipped = result[0].provenance["query_variants_not_executed"]
    assert len(skipped) == 2
    assert all(value["executed"] is False for value in skipped)
    assert all(value["reason"] == "optional_query_expansion_unavailable:TextTokenLimitExceededError" for value in skipped)
    assert result[0].hits[0].provenance["retrieval_query_variants"]["winning_variant"]["variant_id"] == "original"
