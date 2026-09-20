"""Executable Phase-1 Knowledge golden-suite acceptance tests."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_evaluation.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from neocortex.knowledge import knowledge_context, knowledge_evaluation as evaluation_module, knowledge_planner, knowledge_search, knowledge_service
from neocortex.knowledge.knowledge_evaluation import (
    MAX_GOLDEN_FIXTURE_BYTES,
    REQUIRED_GOLDEN_CATEGORIES,
    SCRIPTED_FIXTURE_LIMITATION,
    CandidateEvidence,
    CitationRef,
    EvaluationDisposition,
    EvaluationOutcome,
    EvaluationOwnerCondition,
    EvidenceLocator,
    GoldenCase,
    GoldenRun,
    GoldenSuite,
    evaluate_golden_suite,
    golden_suite_from_mapping,
    load_golden_suite,
    run_golden_suite,
)
from neocortex.knowledge.knowledge_planner import KnowledgeQuery
from neocortex.knowledge.knowledge_search import KnowledgeSearchResult
# endregion [01]

# region [02] Implementación


FIXTURE = Path(__file__).parent / "fixtures" / "knowledge" / "phase1_golden_v1.json"


def _case_map(suite: GoldenSuite) -> dict[str, GoldenCase]:
    return {case.category: case for case in suite.cases}


def _candidate_map(case: GoldenCase) -> dict[str, CandidateEvidence]:
    return {
        candidate.evidence_id: candidate
        for ranking in case.rankings
        for candidate in ranking.candidates
    }


def _all_mapping_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_mapping_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_mapping_keys(item) for item in value))
    return set()


def _replace_candidate_locator(
    case: GoldenCase,
    evidence_id: str,
    locator: EvidenceLocator,
) -> GoldenCase:
    return replace(
        case,
        rankings=tuple(
            replace(
                ranking,
                candidates=tuple(
                    replace(candidate, locator=locator)
                    if candidate.evidence_id == evidence_id
                    else candidate
                    for candidate in ranking.candidates
                ),
            )
            for ranking in case.rankings
        ),
    )


def test_fixture_is_input_only_and_covers_all_surviving_contracts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    suite = load_golden_suite(FIXTURE)
    cases = _case_map(suite)
    keys = _all_mapping_keys(raw)

    assert raw["schema_version"] == 1
    assert raw["provenance"] == "scripted_candidates"
    assert len(raw["cases"]) == 14
    assert len(suite.cases) == 14
    assert suite.covered_categories == REQUIRED_GOLDEN_CATEGORIES
    assert SCRIPTED_FIXTURE_LIMITATION in suite.limitations
    assert not any(key.startswith("actual_") for key in keys)
    assert {
        "retrieved_evidence",
        "produced_citations",
        "telemetry",
        "scripted_fixture",
    }.isdisjoint(keys)

    assert cases["exact_identifier"].relevant_evidence[0].locator.kind == "identifier"
    assert "lexical" in cases["lexical"].required_plan_steps
    assert "semantic" in cases["semantic_paraphrase"].required_plan_steps

    multiple = cases["multiple_evidence_same_resource"]
    multiple_candidates = _candidate_map(multiple)
    assert Counter(
        multiple_candidates[evidence_id].resource_id
        for evidence_id in multiple.expected_retrieved_ids
    ) == {"resource:inspection:alpha": 2}

    sources = cases["two_sources_formats_same_answer"]
    source_candidates = _candidate_map(sources)
    selected_sources = [
        source_candidates[evidence_id] for evidence_id in sources.expected_retrieved_ids
    ]
    assert {item.source_kind for item in selected_sources} == {"pdf", "audio"}
    assert {item.format for item in selected_sources} == {"pdf", "wav"}
    assert set(sources.claims[0].evidence_ids) == set(sources.expected_retrieved_ids)

    revisions = _candidate_map(cases["current_vs_superseded"])
    assert {item.revision_state.value for item in revisions.values()} == {
        "current",
        "superseded",
    }
    duplicate_case = cases["exact_duplicate"]
    duplicates = [
        candidate
        for candidate in _candidate_map(duplicate_case).values()
        if candidate.disposition is EvaluationDisposition.DUPLICATE
    ]
    assert len(duplicates) == 1
    canonical_id = _candidate_map(duplicate_case)[
        duplicate_case.expected_retrieved_ids[0]
    ].resource_id
    assert duplicates[0].canonical_resource_id == canonical_id

    contradiction = cases["contradiction"]
    assert len(contradiction.claims) == 2
    assert len(contradiction.contradictions) == 1

    assert cases["no_answer"].expected_abstain
    assert not cases["no_answer"].relevant_evidence
    assert cases["incomplete_by_limit"].expected_omitted_by_limit == 2
    assert cases["snapshot_changes"].snapshot_transition is not None
    assert cases["absent_owner_base"].owner_conditions[0].condition is (
        EvaluationOwnerCondition.ABSENT
    )
    future = cases["future_schema"].owner_conditions[0]
    assert future.condition is EvaluationOwnerCondition.FUTURE
    assert future.observed_schema_version == future.expected_schema_version + 1
    assert cases["unicode_spaces_hash_path"].expected_outcome is (
        EvaluationOutcome.PARTIAL
    )
    path_candidate = _candidate_map(cases["unicode_spaces_hash_path"])[
        cases["unicode_spaces_hash_path"].expected_retrieved_ids[0]
    ]
    assert path_candidate.current_path is not None
    assert "Área técnica #1" in path_candidate.current_path

    owner_versions = {
        "inventory": 7,
        "framework": 19,
        "catalog": 6,
        "pdf": 11,
        "docx": 5,
        "office": 1,
        "audio": 1,
        "image": 6,
        "semantic": 6,
    }
    for case in suite.cases:
        for owner in case.owner_conditions:
            assert owner.owner in owner_versions
            assert owner.expected_schema_version == owner_versions[owner.owner]


def test_runner_crosses_live_planner_fusion_context_and_snapshot_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = load_golden_suite(FIXTURE)
    planner_spy = Mock(wraps=knowledge_planner.plan_knowledge_query)
    fusion_spy = Mock(wraps=knowledge_search.fuse_evidence_rankings)
    context_spy = Mock(wraps=knowledge_context.build_context_bundle)
    monkeypatch.setattr(knowledge_planner, "plan_knowledge_query", planner_spy)
    monkeypatch.setattr(knowledge_search, "fuse_evidence_rankings", fusion_spy)
    monkeypatch.setattr(knowledge_context, "build_context_bundle", context_spy)

    service_calls = 0
    original_service_search = knowledge_service.KnowledgeSearchService.search

    def counted_service_search(
        self: knowledge_service.KnowledgeSearchService,
        query: KnowledgeQuery,
        cancellation_check: Callable[[], None] | None = None,
    ) -> KnowledgeSearchResult:
        nonlocal service_calls
        service_calls += 1
        return original_service_search(
            self,
            query,
            cancellation_check=cancellation_check,
        )

    monkeypatch.setattr(
        knowledge_service.KnowledgeSearchService,
        "search",
        counted_service_search,
    )
    report = evaluate_golden_suite(suite, cutoff_k=2)

    assert report.gate_passed
    assert report.scenario_count == 14
    assert len(report.observations) == 14
    assert planner_spy.call_count == 14
    assert fusion_spy.call_count == 14
    assert context_spy.call_count == 14
    assert service_calls == 1

    for case, observation in zip(suite.cases, report.observations, strict=True):
        assert observation.acceptance_passed
        assert not observation.diagnostics
        assert set(case.required_plan_steps).issubset(observation.plan_steps)
        assert set(case.forbidden_plan_steps).isdisjoint(observation.plan_steps)
        assert (
            tuple(item.evidence_id for item in observation.retrieved_evidence)
            == case.expected_retrieved_ids
        )
        assert tuple(
            item.evidence_id for item in observation.produced_citations
        ) == tuple(item.evidence_id for item in observation.retrieved_evidence)
        assert observation.telemetry.latency_milliseconds >= 0
        assert observation.telemetry.context_characters > 0

    observations = {item.category: item for item in report.observations}
    assert all(
        len(observation.plan_steps) == len(set(observation.plan_steps))
        for observation in report.observations
    )
    assert observations["semantic_paraphrase"].plan_steps == (
        "lexical",
        "semantic",
    )
    assert observations["current_vs_superseded"].excluded_revisions == 1
    assert observations["exact_duplicate"].filtered_duplicates == 1
    assert observations["incomplete_by_limit"].omitted_by_limit == 2
    assert observations["contradiction"].contradictions == 1
    assert observations["snapshot_changes"].snapshot_changed
    assert observations["snapshot_changes"].actual_abstained
    assert observations["unicode_spaces_hash_path"].actual_outcome is (
        EvaluationOutcome.PARTIAL
    )
    assert report.telemetry.rows_scanned.total == 19
    assert report.telemetry.vectors_scanned.total == 3


def test_metrics_use_nontrivial_formulas_and_explicit_integrity_denominators() -> None:
    report = evaluate_golden_suite(load_golden_suite(FIXTURE), cutoff_k=2)
    scenarios = {item.category: item for item in report.scenarios}
    intermediate_ndcg = 7 / (7 + 3 / math.log2(3))

    assert report.retrieval.evaluated_queries == 10
    assert report.retrieval.relevant_evidence == 16
    assert report.retrieval.covered_evidence == 13
    assert math.isclose(
        report.retrieval.recall_at_k,
        0.8833333333333334,
    )
    assert math.isclose(report.retrieval.mean_reciprocal_rank, 0.95)
    assert math.isclose(
        report.retrieval.ndcg_at_k,
        0.9205238959553401,
    )
    assert math.isclose(report.retrieval.evidence_coverage or 0.0, 13 / 16)

    lexical = scenarios["lexical"]
    assert lexical.recall_at_k == 1.0
    assert lexical.reciprocal_rank == 0.5
    assert math.isclose(lexical.ndcg_at_k or 0.0, 1 / math.log2(3))
    assert lexical.citation_precision == 0.5
    assert lexical.locator_precision == 1.0
    assert lexical.diagnostics == ("invalid_citations:1",)

    semantic = scenarios["semantic_paraphrase"]
    assert semantic.recall_at_k == 0.5
    assert semantic.evidence_coverage == 0.5
    assert semantic.diagnostics == ("missing_relevant:text:substation:chunk5",)
    limited = scenarios["incomplete_by_limit"]
    assert math.isclose(limited.recall_at_k or 0.0, 1 / 3)
    assert "omitted_by_limit:2" in limited.diagnostics

    assert math.isclose(report.integrity.citation_precision or 0.0, 13 / 14)
    assert report.integrity.locator_precision == 1.0
    assert report.integrity.valid_citations == 13
    assert report.integrity.evidence_valid_citations == 13
    assert report.integrity.produced_citations == 14
    assert report.integrity.expected_abstention_rate == 4 / 14
    assert report.integrity.actual_abstention_rate == 4 / 14
    assert report.integrity.abstention_accuracy == 1.0
    assert report.integrity.outcome_accuracy == 1.0

    stale = scenarios["current_vs_superseded"]
    assert (stale.stale_retrieved, stale.stale_candidates, stale.stale_rate) == (
        0,
        1,
        0.0,
    )
    duplicate = scenarios["exact_duplicate"]
    assert (
        duplicate.duplicate_retrieved,
        duplicate.duplicate_candidates,
        duplicate.duplicate_rate,
    ) == (0, 1, 0.0)
    assert (
        report.integrity.stale_retrieved,
        report.integrity.stale_candidates,
        report.integrity.stale_rate,
    ) == (0, 1, 0.0)
    assert (
        report.integrity.duplicate_retrieved,
        report.integrity.duplicate_candidates,
        report.integrity.duplicate_rate,
    ) == (0, 1, 0.0)


@pytest.mark.parametrize(
    ("category", "evidence_id", "wrong_locator", "expected_precision"),
    (
        ("lexical", "pdf:manual:sf6:p7", EvidenceLocator("page", "70"), 0.0),
        (
            "two_sources_formats_same_answer",
            "audio:breaker:pressure:t45",
            EvidenceLocator("timestamp_ms", "450000-461000"),
            0.5,
        ),
    ),
)
def test_locator_precision_comes_from_live_evidence_not_golden_lookup(
    category: str,
    evidence_id: str,
    wrong_locator: EvidenceLocator,
    expected_precision: float,
) -> None:
    suite = load_golden_suite(FIXTURE)
    mutated_case = _replace_candidate_locator(
        _case_map(suite)[category],
        evidence_id,
        wrong_locator,
    )
    report = evaluate_golden_suite(
        replace(suite, cases=(mutated_case,)),
        cutoff_k=2,
        require_all_categories=False,
    )
    result = report.scenarios[0]

    assert not report.gate_passed
    assert result.citation_precision == expected_precision
    assert result.locator_precision == expected_precision
    assert "locator_mismatches:1" in result.diagnostics
    assert "expected_citation_missing_or_wrong_locator" in (
        report.observations[0].diagnostics
    )


def test_citation_requires_expected_and_retrieved_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = load_golden_suite(FIXTURE)
    semantic = _case_map(suite)["semantic_paraphrase"]
    missing_judgment = next(
        item
        for item in semantic.relevant_evidence
        if item.evidence_id == "text:substation:chunk5"
    )
    missing_citation = CitationRef(
        missing_judgment.evidence_id,
        missing_judgment.locator,
    )
    mutated_case = replace(
        semantic,
        expected_citations=(*semantic.expected_citations, missing_citation),
    )
    subset = replace(suite, cases=(mutated_case,))
    live_run = run_golden_suite(subset, require_all_categories=False)
    injected_observation = replace(
        live_run.observations[0],
        produced_citations=(missing_citation,),
    )
    injected_run = GoldenRun(subset, (injected_observation,))

    def return_injected_run(
        _suite: GoldenSuite,
        *,
        require_all_categories: bool = True,
    ) -> GoldenRun:
        del require_all_categories
        return injected_run

    monkeypatch.setattr(
        evaluation_module,
        "run_golden_suite",
        return_injected_run,
    )
    result = evaluate_golden_suite(
        subset,
        cutoff_k=2,
        require_all_categories=False,
    ).scenarios[0]
    assert result.produced_citations == 1
    assert result.evidence_valid_citations == 0
    assert result.valid_citations == 0
    assert result.citation_precision == 0.0
    assert result.locator_precision is None


def test_duplicate_evidence_and_citations_are_rejected_and_ndcg_is_bounded() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    lexical = next(case for case in raw["cases"] if case["category"] == "lexical")
    duplicate = dict(lexical["rankings"][0]["candidates"][1])
    duplicate["source_rank"] = 3
    lexical["rankings"][0]["candidates"].append(duplicate)
    with pytest.raises(ValueError, match="cannot repeat an evidence_id"):
        golden_suite_from_mapping(raw)

    report = evaluate_golden_suite(load_golden_suite(FIXTURE), cutoff_k=3)
    observation = report.observations[0]
    with pytest.raises(ValueError, match="repeated evidence_id"):
        replace(
            observation,
            retrieved_evidence=(
                *observation.retrieved_evidence,
                observation.retrieved_evidence[0],
            ),
        )
    with pytest.raises(ValueError, match="repeated produced citations"):
        replace(
            observation,
            produced_citations=(
                *observation.produced_citations,
                observation.produced_citations[0],
            ),
        )
    assert all(
        item.ndcg_at_k is None or 0.0 <= item.ndcg_at_k <= 1.0
        for item in report.scenarios
    )


def test_empty_relevance_subset_has_explicit_zero_and_none_semantics() -> None:
    suite = load_golden_suite(FIXTURE)
    no_answer = _case_map(suite)["no_answer"]
    report = evaluate_golden_suite(
        replace(suite, cases=(no_answer,)),
        cutoff_k=2,
        require_all_categories=False,
    )

    assert report.retrieval.evaluated_queries == 0
    assert report.retrieval.recall_at_k == 0.0
    assert report.retrieval.mean_reciprocal_rank == 0.0
    assert report.retrieval.ndcg_at_k == 0.0
    assert report.retrieval.evidence_coverage is None
    assert report.retrieval.relevant_evidence == 0
    assert report.retrieval.covered_evidence == 0


@pytest.mark.parametrize("cutoff", (0, 1001, True, 1.5, "2"))
def test_cutoff_is_a_strict_bounded_integer(cutoff: object) -> None:
    with pytest.raises(ValueError, match="cutoff_k"):
        evaluate_golden_suite(load_golden_suite(FIXTURE), cutoff_k=cutoff)  # type: ignore[arg-type]


def test_provenance_is_derived_and_report_json_is_canonical() -> None:
    suite = load_golden_suite(FIXTURE)
    assert suite.scripted_fixture
    with pytest.raises(ValueError, match="EvaluationProvenance"):
        replace(suite, provenance=False)  # type: ignore[arg-type]

    report = evaluate_golden_suite(suite, cutoff_k=2)
    assert report.scripted_fixture
    assert report.to_json() == report.to_json()
    payload = json.loads(report.to_json())
    assert payload["schema_version"] == 1
    assert payload["kind"] == "knowledge_evaluation_report"
    assert len(payload["scenarios"]) == 14
    assert len(payload["observations"]) == 14


def test_loader_is_strict_canonical_and_bounded(tmp_path: Path) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["cases"][0]["actual_outcome"] = "success"
    with pytest.raises(ValueError, match="unknown fields"):
        golden_suite_from_mapping(raw)

    suite = load_golden_suite(FIXTURE)
    canonical = json.dumps(
        suite.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert golden_suite_from_mapping(json.loads(canonical)).to_dict() == suite.to_dict()

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_GOLDEN_FIXTURE_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        load_golden_suite(oversized)


def test_integrity_rate_guards_reject_incoherent_denominators() -> None:
    report = evaluate_golden_suite(load_golden_suite(FIXTURE), cutoff_k=2)
    stale = next(
        item for item in report.scenarios if item.category == "current_vs_superseded"
    )
    with pytest.raises(ValueError, match="stale retrieved exceeds"):
        replace(stale, stale_retrieved=2, stale_candidates=1, stale_rate=2.0)
    with pytest.raises(ValueError, match="duplicate rate/denominator"):
        replace(report.integrity, duplicate_rate=1.0)
# endregion [02]
