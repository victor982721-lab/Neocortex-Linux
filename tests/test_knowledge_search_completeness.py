"""Required-ranking completeness and filter-alias regressions for Knowledge Search."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_completeness.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeHit,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgePlan,
    KnowledgeQuery,
    RetrievalMode,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
    RankingExecution,
    execute_knowledge_search,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.api.cli.cli_knowledge import (
    KnowledgeExitCode,
    knowledge_search_exit_code,
)
from neocortex.semantic.semantic_service_contracts import (
    SemanticRanking,
    SemanticSearchResult,
)
# endregion [01]

# region [02] Implementación


_LEXICAL_RANKINGS = ("fts_audio", "fts_docx", "fts_office", "fts_pdf")


def _snapshot(
    *,
    semantic: OwnerAvailability = OwnerAvailability.ABSENT,
    code: OwnerAvailability = OwnerAvailability.ABSENT,
) -> KnowledgeSnapshot:
    versions = {
        "pdf": 11,
        "docx": 5,
        "office": 1,
        "audio": 1,
        "semantic": 6,
        "code": 2,
        "catalog": 6,
        "inventory": 7,
    }
    states = {
        "semantic": semantic,
        "code": code,
    }
    owners = tuple(
        OwnerSnapshot(
            owner,
            states.get(owner, OwnerAvailability.ABSENT),
            version,
            (
                version
                if states.get(owner, OwnerAvailability.ABSENT) is OwnerAvailability.AVAILABLE
                else None
            ),
        )
        for owner, version in versions.items()
    )
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T01:02:03Z",
        captured_monotonic_ns=1,
        owners=owners,
    )


def _ranking(
    name: str,
    channel: str,
    *,
    available: bool,
    complete: bool,
) -> RankingExecution:
    owner = None
    if channel == "semantic":
        owner = "semantic"
    elif channel == "lexical":
        owner = name.removeprefix("fts_")
    return RankingExecution(
        name=name,
        channel=channel,
        executed=True,
        available=available,
        complete=complete,
        returned=0,
        reason=None if complete else "fixture_modality_unavailable",
        owner=owner,
    )


def _ranking_stub(
    reports: tuple[RankingExecution, ...],
) -> Callable[
    ...,
    tuple[dict[str, tuple[KnowledgeCandidate, ...]], list[RankingExecution]],
]:
    def run(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[
        dict[str, tuple[KnowledgeCandidate, ...]],
        list[RankingExecution],
    ]:
        return {}, list(reports)

    return run


def _install_complete_lexical(monkeypatch: pytest.MonkeyPatch) -> None:
    reports = tuple(
        _ranking(name, "lexical", available=True, complete=True) for name in _LEXICAL_RANKINGS
    )
    monkeypatch.setattr(
        knowledge_search,
        "_lexical_rankings",
        _ranking_stub(reports),
    )


def _install_complete_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    def complete_catalog(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
        return (), _ranking(
            "catalog_metadata",
            "catalog",
            available=True,
            complete=True,
        )

    monkeypatch.setattr(knowledge_search, "_catalog_ranking", complete_catalog)


@pytest.mark.parametrize(
    ("query", "expected_names"),
    (
        pytest.param(
            KnowledgeQuery("proteccion interruptor"),
            ("semantic_text", "semantic_image"),
            id="broad",
        ),
        pytest.param(
            KnowledgeQuery("proteccion interruptor", source_kinds=("pdf",)),
            ("semantic_text",),
            id="text-only",
        ),
        pytest.param(
            KnowledgeQuery("substation maintenance", source_kinds=("image",)),
            ("semantic_image",),
            id="image-only",
        ),
        pytest.param(
            KnowledgeQuery(
                "proteccion interruptor",
                source_kinds=("pdf", "image"),
            ),
            ("semantic_text", "semantic_image"),
            id="mixed",
        ),
    ),
)
def test_semantic_plan_steps_are_the_only_required_modalities(
    query: KnowledgeQuery,
    expected_names: tuple[str, ...],
) -> None:
    plan = plan_knowledge_query(query)

    assert (
        tuple(step.ranking_name for step in plan.steps if step.channel == "semantic")
        == expected_names
    )
    assert knowledge_search._required_semantic_ranking_names(plan) == frozenset(expected_names)


@pytest.mark.parametrize(
    ("query", "missing_name"),
    (
        pytest.param(
            KnowledgeQuery("proteccion interruptor"),
            "semantic_text",
            id="broad-text-missing",
        ),
        pytest.param(
            KnowledgeQuery("proteccion interruptor"),
            "semantic_image",
            id="broad-image-missing",
        ),
        pytest.param(
            KnowledgeQuery("proteccion interruptor", source_kinds=("pdf",)),
            "semantic_text",
            id="text-only-text-missing",
        ),
        pytest.param(
            KnowledgeQuery("substation maintenance", source_kinds=("image",)),
            "semantic_image",
            id="image-only-image-missing",
        ),
        pytest.param(
            KnowledgeQuery(
                "proteccion interruptor",
                source_kinds=("pdf", "image"),
            ),
            "semantic_text",
            id="mixed-text-missing",
        ),
        pytest.param(
            KnowledgeQuery(
                "proteccion interruptor",
                source_kinds=("pdf", "image"),
            ),
            "semantic_image",
            id="mixed-image-missing",
        ),
    ),
)
def test_each_required_semantic_modality_controls_completeness_and_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: KnowledgeQuery,
    missing_name: str,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)
    plan = plan_knowledge_query(query)
    semantic_names = tuple(step.ranking_name for step in plan.steps if step.channel == "semantic")
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        _ranking_stub(
            tuple(
                _ranking(
                    name,
                    "semantic",
                    available=name != missing_name,
                    complete=name != missing_name,
                )
                for name in semantic_names
            )
        ),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    assert not result.complete
    assert result.warnings == (
        f"ranking_partial:{missing_name}:fixture_modality_unavailable",
        f"ranking_unavailable:{missing_name}",
    )
    assert result.blocking_owners == ("semantic",)
    assert result.to_dict()["blocking_owners"] == ["semantic"]
    assert result.plan.plan_id == plan.plan_id
    assert result.to_json() == result.to_json()


def test_multiple_missing_semantic_modalities_have_deterministic_warning_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)
    plan = plan_knowledge_query(KnowledgeQuery("proteccion interruptor"))
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        _ranking_stub(
            (
                _ranking(
                    "semantic_image",
                    "semantic",
                    available=False,
                    complete=False,
                ),
                _ranking(
                    "semantic_text",
                    "semantic",
                    available=False,
                    complete=False,
                ),
            )
        ),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    assert result.warnings == (
        "ranking_partial:semantic_image:fixture_modality_unavailable",
        "ranking_partial:semantic_text:fixture_modality_unavailable",
        "ranking_unavailable:semantic_image",
        "ranking_unavailable:semantic_text",
    )
    assert result.blocking_owners == ("semantic",)
    assert not result.complete


@pytest.mark.parametrize(
    ("query", "max_vectors", "expected_calls", "expected_complete"),
    (
        pytest.param(
            KnowledgeQuery("proteccion interruptor"),
            8,
            (("semantic_text", 4), ("semantic_image", 4)),
            True,
            id="broad-even",
        ),
        pytest.param(
            KnowledgeQuery("proteccion interruptor"),
            7,
            (("semantic_text", 4), ("semantic_image", 3)),
            True,
            id="broad-odd",
        ),
        pytest.param(
            KnowledgeQuery("proteccion interruptor", source_kinds=("pdf",)),
            7,
            (("semantic_text", 7),),
            True,
            id="text-only",
        ),
        pytest.param(
            KnowledgeQuery("substation maintenance", source_kinds=("image",)),
            7,
            (("semantic_image", 7),),
            True,
            id="image-only",
        ),
        pytest.param(
            KnowledgeQuery(
                "proteccion interruptor",
                source_kinds=("pdf", "image"),
            ),
            7,
            (("semantic_text", 4), ("semantic_image", 3)),
            True,
            id="mixed-odd",
        ),
        pytest.param(
            KnowledgeQuery("proteccion interruptor"),
            1,
            (("semantic_text", 1),),
            False,
            id="broad-minimum",
        ),
    ),
)
def test_semantic_execution_uses_only_planned_steps_and_exact_global_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: KnowledgeQuery,
    max_vectors: int,
    expected_calls: tuple[tuple[str, int], ...],
    expected_complete: bool,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)
    observed: list[tuple[str, int]] = []
    custom_semantic = tmp_path / "custom" / "knowledge-vectors.sqlite3"
    paths = replace(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        semantic=custom_semantic,
    )

    def complete_semantic_search(
        state_directory: Path,
        query_text: str,
        **kwargs: object,
    ) -> SemanticSearchResult:
        assert state_directory == custom_semantic.parent
        assert kwargs["semantic_database"] == custom_semantic
        include_text = kwargs["include_text"]
        include_images = kwargs["include_images"]
        vector_budget = kwargs["max_vectors"]
        candidate_limit = kwargs["candidate_limit"]
        assert isinstance(include_text, bool)
        assert isinstance(include_images, bool)
        assert isinstance(vector_budget, int)
        assert isinstance(candidate_limit, int)
        assert kwargs["limit"] == candidate_limit
        modality = "text" if include_text else "image"
        expected_name = f"semantic_{modality}"
        planned_step = next(step for step in plan.steps if step.ranking_name == expected_name)
        assert candidate_limit == planned_step.candidate_limit
        observed.append((expected_name, vector_budget))
        return SemanticSearchResult(
            query=query_text,
            rankings=(
                SemanticRanking(
                    name=expected_name,
                    hits=(),
                    resolved=(),
                    scanned=0,
                    complete=True,
                    available=True,
                ),
            ),
            lexical_rankings=(),
            fused=(),
        )

    monkeypatch.setattr(
        knowledge_search.semantic_service,
        "search_semantic_index",
        complete_semantic_search,
    )
    plan = plan_knowledge_query(replace(query, max_vectors=max_vectors))

    result = execute_knowledge_search(
        paths,
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    assert tuple(observed) == expected_calls
    assert sum(budget for _, budget in observed) <= plan.max_vectors
    assert result.complete is expected_complete
    if expected_complete:
        assert result.warnings == ()
    else:
        missing_report = next(
            report for report in result.rankings if report.name == "semantic_image"
        )
        assert not missing_report.executed
        assert not missing_report.complete
        assert missing_report.reason == "semantic_vector_budget_unavailable"
        assert result.warnings == ("ranking_unavailable:semantic_image",)


def test_optional_title_absence_is_reported_without_blocking_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)

    def semantic_without_titles(
        _state_directory: Path,
        query_text: str,
        **kwargs: object,
    ) -> SemanticSearchResult:
        assert kwargs["include_text"] is True
        assert kwargs["include_title"] is True
        assert kwargs["include_images"] is False
        return SemanticSearchResult(
            query=query_text,
            rankings=(
                SemanticRanking(
                    name="semantic_text",
                    hits=(),
                    resolved=(),
                    scanned=0,
                    complete=True,
                ),
                SemanticRanking(
                    name="semantic_title",
                    hits=(),
                    resolved=(),
                    scanned=0,
                    complete=False,
                    available=False,
                    unavailable_reason="title_channel_not_indexed",
                    fusion_weight=0.5,
                ),
            ),
            lexical_rankings=(),
            fused=(),
        )

    monkeypatch.setattr(
        knowledge_search.semantic_service,
        "search_semantic_index",
        semantic_without_titles,
    )
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "proteccion interruptor",
            retrieval_mode=RetrievalMode.DISCOVERY,
            source_kinds=("pdf",),
        )
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    title_report = next(report for report in result.rankings if report.name == "semantic_title")
    assert title_report.channel == "semantic_discovery"
    assert title_report.reason == "title_channel_not_indexed"
    assert not title_report.available
    assert result.complete
    assert result.warnings == ("ranking_partial:semantic_title:title_channel_not_indexed",)


@pytest.mark.parametrize("cutoff_reason", ("top_k", "candidate_limit_reached"))
def test_semantic_candidate_cutoff_is_a_complete_bounded_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cutoff_reason: str,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)

    def cutoff_semantic_search(
        _state_directory: Path,
        query_text: str,
        **_kwargs: object,
    ) -> SemanticSearchResult:
        return SemanticSearchResult(
            query=query_text,
            rankings=(
                SemanticRanking(
                    name="semantic_text",
                    hits=(),
                    resolved=(),
                    scanned=7,
                    complete=True,
                    available=True,
                    cutoff_reason=cutoff_reason,
                ),
            ),
            lexical_rankings=(),
            fused=(),
        )

    monkeypatch.setattr(
        knowledge_search.semantic_service,
        "search_semantic_index",
        cutoff_semantic_search,
    )
    plan = plan_knowledge_query(KnowledgeQuery("proteccion interruptor", source_kinds=("pdf",)))

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    report = next(value for value in result.rankings if value.name == "semantic_text")
    assert report.complete
    assert report.reason is None
    assert report.result_window_full
    assert report.next_cursor is None
    assert result.warnings == ()
    assert not result.truncated
    assert result.omitted_candidates == 0
    assert result.result_window_full
    assert result.window_omitted_candidates == 0
    assert result.complete


def test_semantic_vector_cutoff_remains_incomplete_and_exposes_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)

    def cutoff_semantic_search(
        _state_directory: Path,
        query_text: str,
        **_kwargs: object,
    ) -> SemanticSearchResult:
        return SemanticSearchResult(
            query=query_text,
            rankings=(
                SemanticRanking(
                    name="semantic_text",
                    hits=(),
                    resolved=(),
                    scanned=7,
                    complete=False,
                    available=True,
                    cutoff_reason="max_vectors_reached",
                    next_cursor=25,
                    cutoff_score=0.25,
                ),
            ),
            lexical_rankings=(),
            fused=(),
        )

    monkeypatch.setattr(
        knowledge_search.semantic_service,
        "search_semantic_index",
        cutoff_semantic_search,
    )
    plan = plan_knowledge_query(KnowledgeQuery("proteccion interruptor", source_kinds=("pdf",)))

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    report = next(value for value in result.rankings if value.name == "semantic_text")
    assert not report.complete
    assert report.reason == "semantic_vector_limit_reached"
    assert report.next_cursor == 25
    assert report.cutoff_score == 0.25
    assert report.to_dict()["next_cursor"] == 25
    assert report.to_dict()["cutoff_score"] == 0.25
    assert result.truncated
    assert result.omitted_candidates == 1
    assert not result.complete


def test_final_top_k_window_does_not_turn_a_stable_search_into_exit_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        _ranking_stub(
            (
                _ranking(
                    "semantic_text",
                    "semantic",
                    available=True,
                    complete=True,
                ),
            )
        ),
    )
    resource = ResourceRef("resource:fixture", "pdf", "pdf")
    revision = RevisionRef(
        resource.resource_id,
        "revision:fixture",
        "pdf-v1",
        "fixture-signature",
        1,
        RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        "evidence:fixture",
        resource.resource_id,
        revision.revision_id,
        EvidenceMethod.EXTRACTED,
        page=1,
        snippet="Q52 shall remain open.",
    )
    hit = KnowledgeHit(
        1,
        resource,
        revision,
        evidence,
        (RankingSignal("fts_pdf", "bm25", 1.0, 1, contribution=0.1),),
        0.1,
        ("fixture matched",),
    )
    monkeypatch.setattr(
        knowledge_search,
        "fuse_evidence_rankings",
        lambda _rankings, **_kwargs: ((hit,), 7),
    )
    plan = plan_knowledge_query(
        KnowledgeQuery("proteccion interruptor", source_kinds=("pdf",), limit=1)
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    assert result.complete
    assert not result.truncated
    assert result.result_window_full
    assert result.omitted_candidates == 7
    assert result.window_omitted_candidates == 7
    assert knowledge_search_exit_code(result) is KnowledgeExitCode.SUCCESS


def test_code_only_required_semantic_failure_degrades_completeness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        _ranking_stub(
            (
                _ranking(
                    "semantic_text",
                    "semantic",
                    available=False,
                    complete=False,
                ),
            )
        ),
    )

    def complete_code(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
        return (), _ranking(
            "code_structural",
            "structural_code",
            available=True,
            complete=True,
        )

    monkeypatch.setattr(knowledge_search, "_code_ranking", complete_code)
    plan = plan_knowledge_query(KnowledgeQuery("definition breaker", source_kinds=("code",)))
    assert knowledge_search._required_direct_ranking_names(plan) == frozenset({"code_structural"})

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(code=OwnerAvailability.AVAILABLE),
    )

    semantic_step = next(step for step in plan.steps if step.channel == "semantic")
    assert semantic_step.required
    assert not result.complete
    assert result.warnings == (
        "ranking_partial:semantic_text:fixture_modality_unavailable",
        "ranking_unavailable:semantic_text",
    )


def test_exact_truncated_without_known_omissions_is_still_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    _install_complete_catalog(monkeypatch)
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        _ranking_stub(
            (
                _ranking(
                    "semantic_text",
                    "semantic",
                    available=True,
                    complete=True,
                ),
                _ranking(
                    "semantic_image",
                    "semantic",
                    available=True,
                    complete=True,
                ),
            )
        ),
    )

    def truncated_exact(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[
        dict[str, tuple[KnowledgeCandidate, ...]],
        list[RankingExecution],
        int,
        bool,
        tuple[object, ...],
    ]:
        return (
            {},
            [
                _ranking(
                    "exact_coverage",
                    "exact",
                    available=True,
                    complete=True,
                )
            ],
            0,
            True,
            (),
        )

    monkeypatch.setattr(knowledge_search, "_exact_rankings", truncated_exact)
    plan = plan_knowledge_query(KnowledgeQuery("IEC-61850"))

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    assert result.truncated
    assert result.omitted_candidates == 0
    assert not result.complete


def test_required_direct_ranking_cannot_be_substituted_within_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        _ranking_stub(
            (
                _ranking(
                    "semantic_text",
                    "semantic",
                    available=True,
                    complete=True,
                ),
            )
        ),
    )

    def sibling_code_ranking(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
        return (), _ranking(
            "code_relations",
            "structural_code",
            available=True,
            complete=True,
        )

    monkeypatch.setattr(knowledge_search, "_code_ranking", sibling_code_ranking)
    plan = plan_knowledge_query(KnowledgeQuery("definition breaker", source_kinds=("code",)))

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(
            semantic=OwnerAvailability.AVAILABLE,
            code=OwnerAvailability.AVAILABLE,
        ),
    )

    assert not result.complete
    assert result.warnings == ("ranking_unavailable:code_structural",)


def test_semantic_outer_failure_reports_optional_planned_image_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_complete_lexical(monkeypatch)

    def semantic_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("semantic image fixture failed")

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", semantic_failure)
    plan = plan_knowledge_query(KnowledgeQuery("substation maintenance", source_kinds=("image",)))
    plan = replace(
        plan,
        plan_id="knowledge-plan-v1:optional-image-fallback-fixture",
        steps=tuple(
            replace(step, required=False) if step.channel == "semantic" else step
            for step in plan.steps
        ),
    )
    serialized_plan = plan.to_json()

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(semantic=OwnerAvailability.AVAILABLE),
    )

    semantic_reports = tuple(report for report in result.rankings if report.channel == "semantic")
    assert tuple(report.name for report in semantic_reports) == ("semantic_image",)
    assert semantic_reports[0].reason == "owner_read_failed:RuntimeError"
    assert result.complete
    assert result.warnings == ("ranking_partial:semantic_image:owner_read_failed:RuntimeError",)
    assert result.plan is plan
    assert plan.to_json() == serialized_plan


@pytest.mark.parametrize("exception_type", [RuntimeError, ValueError])
def test_semantic_callback_exception_is_repropagated_by_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception],
) -> None:
    _install_complete_lexical(monkeypatch)
    expected = exception_type("semantic cancellation fixture")
    inside_semantic = False

    def cancel() -> None:
        if inside_semantic:
            raise expected

    def semantic_failure(
        _paths: KnowledgeStatePaths,
        _plan: KnowledgePlan,
        _snapshot_value: KnowledgeSnapshot,
        cancellation_check: Callable[[], None] | None = None,
        clock_ns: Callable[[], int] | None = None,
    ) -> tuple[
        dict[str, tuple[KnowledgeCandidate, ...]],
        list[RankingExecution],
    ]:
        nonlocal inside_semantic
        inside_semantic = True
        assert cancellation_check is not None
        assert clock_ns is not None
        cancellation_check()
        pytest.fail("raising semantic callback unexpectedly returned")

    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        semantic_failure,
    )
    plan = plan_knowledge_query(KnowledgeQuery("proteccion interruptor"))

    with pytest.raises(exception_type) as raised:
        execute_knowledge_search(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            plan,
            _snapshot(semantic=OwnerAvailability.AVAILABLE),
            cancellation_check=cancel,
        )

    assert raised.value is expected


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            KnowledgeQuery("manual", source_kinds=("unmapped-owner",)),
            id="source-kind",
        ),
        pytest.param(
            KnowledgeQuery("manual", formats=("unknown-format",)),
            id="format",
        ),
    ],
)
def test_unknown_explicit_scope_never_reports_complete(
    tmp_path: Path,
    query: KnowledgeQuery,
) -> None:
    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan_knowledge_query(query),
        _snapshot(),
    )

    assert not result.complete
    assert "ranking_unavailable:lexical_scope_unsupported" in result.warnings


def _candidate_for_alias(
    *,
    owner: str,
    source_kind: str,
    path: str,
    section_kind: str = "fixture",
) -> KnowledgeCandidate:
    resource = ResourceRef(
        resource_id=f"resource:{owner}:fixture",
        source_kind=source_kind,
        owner=owner,
        current_path=path,
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=f"revision:{owner}:fixture",
        producer=f"{owner}-fixture",
        processing_signature="fixture-v1",
        generation=1,
        state=RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        evidence_id=f"evidence:{owner}:fixture",
        resource_id=resource.resource_id,
        revision_id=revision.revision_id,
        method=EvidenceMethod.EXTRACTED,
        section_kind=section_kind,
        section_id="1",
        snippet="filter alias fixture",
    )
    return KnowledgeCandidate(
        resource=resource,
        revision=revision,
        evidence=evidence,
        signal=RankingSignal("fixture", "fixture-v1", 1.0, 1),
        reason="filter alias fixture",
    )


@pytest.mark.parametrize(
    ("candidate", "query"),
    [
        pytest.param(
            _candidate_for_alias(
                owner="inventory",
                source_kind="file",
                path="C:/docs/protection.pdf",
            ),
            KnowledgeQuery("protection", source_kinds=("pdf",)),
            id="inventory-file-to-pdf",
        ),
        pytest.param(
            _candidate_for_alias(
                owner="catalog",
                source_kind="odt",
                path="C:/docs/manual.odt",
            ),
            KnowledgeQuery(
                "manual",
                source_kinds=("office",),
                formats=("odt",),
            ),
            id="catalog-odt-to-office",
        ),
    ],
)
def test_owner_aliases_survive_explicit_scope_postfilter(
    candidate: KnowledgeCandidate,
    query: KnowledgeQuery,
) -> None:
    filtered = knowledge_search._apply_plan_filters(
        {"fixture": (candidate,)},
        plan_knowledge_query(query),
    )

    assert filtered == {"fixture": (candidate,)}


def test_image_ocr_filter_accepts_only_ocr_evidence_from_image_resources() -> None:
    ocr = _candidate_for_alias(
        owner="semantic",
        source_kind="image",
        path="C:/images/breaker.jpg",
        section_kind="image_ocr",
    )
    visual = _candidate_for_alias(
        owner="semantic",
        source_kind="image",
        path="C:/images/breaker.jpg",
        section_kind="image_region",
    )
    plan = plan_knowledge_query(KnowledgeQuery("breaker label", source_kinds=("image_ocr",)))

    filtered = knowledge_search._apply_plan_filters(
        {"semantic_image": (ocr, visual)},
        plan,
    )

    assert filtered == {"semantic_image": (ocr,)}


# endregion [02]
