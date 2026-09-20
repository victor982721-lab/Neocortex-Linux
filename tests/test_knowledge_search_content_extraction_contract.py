"""Compatibility and late-binding contracts for Knowledge Search CL4."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_content_extraction_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import pickle
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge import knowledge_search_content as content_implementation
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    PhysicalIdentityRef,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgePlan,
    KnowledgeQuery,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
    RankingExecution,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    LexicalRanking,
    LexicalStatePaths,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    ResolvedSearchHit,
    SearchHit,
)
from neocortex.semantic.semantic_service import (
    SemanticRanking,
    SemanticSearchResult,
)
# endregion [01]

# region [02] Implementación


PUBLIC_MODULE = "neocortex.knowledge.knowledge_search"
EXPECTED_SIGNATURES = {
    "_revision_identity": (
        "(resolved: 'ResolvedSearchHit', producer: 'str') -> "
        "'tuple[str, str, RevisionState, tuple[str, ...]]'"
    ),
    "_int_provenance": ("(provenance: 'Mapping[str, object]', name: 'str') -> 'int | None'"),
    "_resolved_physical_identity": ("(resolved: 'ResolvedSearchHit') -> 'str | None'"),
    "_direct_resource_ref": (
        "(*, source_kind: 'str', owner: 'str', source_identity: 'str', "
        "identity: 'FileIdentity', birthtime_ns: 'object', path: 'str | None') "
        "-> 'tuple[ResourceRef, tuple[str, ...]]'"
    ),
    "_candidate_from_resolved": (
        "(resolved: 'ResolvedSearchHit', *, ranking_name: 'str', "
        "source_rank: 'int', producer: 'str') -> 'KnowledgeCandidate'"
    ),
    "_lexical_rankings": (
        "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
        "snapshot: 'KnowledgeSnapshot', *, cancellation_check: "
        "'Callable[[], None] | None' = None, clock_ns: "
        "'Callable[[], int] | None' = None) -> "
        "'tuple[dict[str, tuple[KnowledgeCandidate, ...]], "
        "list[RankingExecution]]'"
    ),
    "_semantic_rankings": (
        "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
        "snapshot: 'KnowledgeSnapshot', cancellation_check: "
        "'Callable[[], None] | None' = None, clock_ns: "
        "'Callable[[], int] | None' = None) -> "
        "'tuple[dict[str, tuple[KnowledgeCandidate, ...]], "
        "tuple[ResourceDiscoverySignal, ...], list[RankingExecution]]'"
    ),
    "_exact_rankings": (
        "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
        "snapshot: 'KnowledgeSnapshot', *, cancellation_check: "
        "'Callable[[], None] | None' = None, clock_ns: "
        "'Callable[[], int] | None' = None) -> "
        "'tuple[dict[str, tuple[KnowledgeCandidate, ...]], "
        "list[RankingExecution], int, bool, tuple[ExactOwnerTiming, ...]]'"
    ),
}

LOWER_SEMANTIC_SIGNATURE = (
    "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
    "snapshot: 'KnowledgeSnapshot', cancellation_check: "
    "'Callable[[], None] | None', clock_ns: 'Callable[[], int] | None', *, "
    "planned_steps: 'PlannedSteps', owner_available: 'OwnerAvailable', "
    "duration_ns: 'DurationNanoseconds', materialize_candidate: "
    "'MaterializeCandidate', materialize_discovery_signal: "
    "'MaterializeDiscoverySignal', default_clock: 'Callable[[], int]', "
    "semantic_search: 'SemanticSearch', cancellation_bridge_type: "
    "'type[SQLiteCancellationBridge]', reraise_captured_cancellation: "
    "'ReraiseCapturedCancellation', sqlite_error_type: 'type[Exception]', "
    "evidence_mode: 'RetrievalMode') -> "
    "'tuple[dict[str, tuple[KnowledgeCandidate, ...]], "
    "tuple[ResourceDiscoverySignal, ...], list[RankingExecution]]'"
)


def _snapshot() -> KnowledgeSnapshot:
    versions = {
        "pdf": 11,
        "docx": 5,
        "office": 1,
        "audio": 1,
        "semantic": 6,
        "catalog": 6,
        "inventory": 7,
    }
    return KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=tuple(
            OwnerSnapshot(owner, OwnerAvailability.AVAILABLE, version, version)
            for owner, version in versions.items()
        ),
    )


def _resolved(*, source_kind: str = "pdf") -> ResolvedSearchHit:
    return ResolvedSearchHit(
        hit=SearchHit(
            ref_id=1,
            entity_id="fixture-entity",
            item_id="fixture-item",
            indexed_model_signature="fixture-model",
            vector_space="fixture-space",
            modality=EmbeddingModality.TEXT,
            score=0.75,
            generation_id=1,
        ),
        path="C:/fixture/report.pdf",
        source_kind=source_kind,
        source_identity=("00000000000000000000000000000001:00000000000000000000000000000002"),
        section_kind="pdf_page",
        section_id="1",
        start_char=0,
        end_char=10,
        snippet="breaker protection",
        source_revision={
            "birthtime_ns": 3,
            "processing_signature": "fixture-v1",
        },
        source_status="done",
    )


def test_semantic_report_explains_calibrated_retrieval_abstention() -> None:
    ranking = SemanticRanking(
        name="semantic_text",
        hits=(),
        resolved=(),
        scanned=65,
        complete=True,
        provenance={
            "retrieval_abstention": {
                "query_abstained": True,
                "policy_signature": "fixture-calibration-v1",
            }
        },
    )

    report = content_implementation._semantic_result_report(
        "semantic_text",
        ranking,
        0,
        candidate_limit=10,
        clock=lambda: 150,
        started_ns=100,
        duration_ns=lambda clock, started: clock() - started,
    )

    assert report.available is True
    assert report.complete is True
    assert report.returned == 0
    assert report.reason == "semantic_retrieval_abstained_below_calibrated_floor"


def _candidate() -> KnowledgeCandidate:
    physical = "1:2:3"
    resource = ResourceRef(
        f"resource:file:{physical}",
        "pdf",
        "pdf",
        PhysicalIdentityRef("posix_device_inode_birthtime", physical, 1),
        "C:/fixture/report.pdf",
    )
    revision = RevisionRef(
        resource.resource_id,
        "revision:fixture",
        "fixture-owner",
        "fixture-v1",
        1,
        RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        "evidence:fixture",
        resource.resource_id,
        revision.revision_id,
        EvidenceMethod.EXTRACTED,
        section_kind="pdf_page",
        section_id="1",
        start_char=0,
        end_char=10,
    )
    return KnowledgeCandidate(
        resource,
        revision,
        evidence,
        RankingSignal("fts_pdf", "fixture", 1.0, 1),
        "content extraction fixture",
    )


def test_content_facade_seam_signatures_metadata_and_pickle_are_stable() -> None:
    for name, expected_signature in EXPECTED_SIGNATURES.items():
        seam = getattr(knowledge_search, name)
        assert str(inspect.signature(seam)) == expected_signature
        assert seam.__module__ == PUBLIC_MODULE
        assert seam.__qualname__ == name
        assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam


def test_lower_semantic_ranking_signature_is_frozen() -> None:
    assert str(inspect.signature(content_implementation.semantic_rankings)) == (
        LOWER_SEMANTIC_SIGNATURE
    )


def test_lexical_wrapper_resolves_all_lower_dependencies_per_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    plan = plan_knowledge_query(KnowledgeQuery("breaker", formats=("pdf",)))
    snapshot = _snapshot()
    resolved = _resolved()
    candidate = _candidate()
    owners: list[str] = []
    candidate_calls: list[tuple[object, str, int, str]] = []

    def owner_available(
        received_snapshot: KnowledgeSnapshot,
        owner: str,
    ) -> bool:
        assert received_snapshot is snapshot
        owners.append(owner)
        return owner == "pdf"

    def planned_limit(received_plan: KnowledgePlan, channel: str) -> int:
        assert received_plan is plan
        assert channel == "lexical"
        return 2

    def materialize(
        value: object,
        *,
        ranking_name: str,
        source_rank: int,
        producer: str,
    ) -> KnowledgeCandidate:
        candidate_calls.append((value, ranking_name, source_rank, producer))
        return candidate

    def cancellation() -> None:
        return None

    def clock() -> int:
        return 10

    def lexical_search(
        state_paths: LexicalStatePaths,
        query: str,
        *,
        limit: int,
        cancellation_check: Callable[[], None] | None,
        clock_ns: Callable[[], int] | None,
    ) -> tuple[LexicalRanking, ...]:
        assert state_paths.pdf == paths.pdf
        assert state_paths.docx is None
        assert state_paths.office is None
        assert state_paths.audio is None
        assert query == plan.normalized_query
        assert limit == 1
        assert cancellation_check is cancellation
        assert clock_ns is clock
        return (
            LexicalRanking(
                "pdf",
                paths.pdf,
                LexicalAvailability.AVAILABLE,
                query,
                (resolved,),
                elapsed_ns=17,
            ),
        )

    monkeypatch.setattr(knowledge_search, "_owner_available", owner_available)
    monkeypatch.setattr(
        knowledge_search,
        "_planned_candidate_limit",
        planned_limit,
    )
    monkeypatch.setattr(
        knowledge_search,
        "_candidate_from_resolved",
        materialize,
    )
    monkeypatch.setattr(knowledge_search, "search_lexical_sources", lexical_search)
    monkeypatch.setattr(knowledge_search, "MAX_KNOWLEDGE_CANDIDATES", 1)

    rankings, reports = knowledge_search._lexical_rankings(
        paths,
        plan,
        snapshot,
        cancellation_check=cancellation,
        clock_ns=clock,
    )

    assert owners == ["pdf", "docx", "office", "audio", "video", "text"]
    assert candidate_calls == [(resolved, "fts_pdf", 1, "pdf-fts-v1")]
    assert rankings == {"fts_pdf": (candidate,)}
    assert len(reports) == 1
    assert reports[0].to_dict() == {
        "name": "fts_pdf",
        "channel": "lexical",
        "executed": True,
        "available": True,
        "complete": True,
        "returned": 1,
        "rows_scanned": 1,
        "row_count_semantics": "materialized_lower_bound",
        "vectors_scanned": 0,
        "owner": "pdf",
        "elapsed_ns": 17,
    }


def test_semantic_wrapper_resolves_provider_materializer_clock_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    plan = plan_knowledge_query(
        KnowledgeQuery("breaker protection", source_kinds=("pdf",), max_vectors=5)
    )
    snapshot = _snapshot()
    step = next(value for value in plan.steps if value.ranking_name == "semantic_text")
    resolved = _resolved()
    candidate = _candidate()
    clock_calls = 0
    duration_calls: list[tuple[Callable[[], int], int]] = []

    def default_clock() -> int:
        nonlocal clock_calls
        clock_calls += 1
        return 100

    def duration(clock: Callable[[], int], started: int) -> int:
        duration_calls.append((clock, started))
        return 77

    def semantic_search(
        state_directory: Path,
        query: str,
        **kwargs: object,
    ) -> SemanticSearchResult:
        assert state_directory == paths.semantic.parent
        assert query == plan.normalized_query
        assert kwargs == {
            "semantic_database": paths.semantic,
            "candidate_limit": step.candidate_limit,
            "limit": step.candidate_limit,
            "max_vectors": plan.max_vectors,
            "include_text": True,
            "include_title": False,
            "include_images": False,
            "include_lexical": False,
            "image_query_intent": "ambiguous",
            "allow_ambiguous_images": False,
            "local_files_only": True,
            "evidence_mode": True,
            "cancellation_check": None,
        }
        return SemanticSearchResult(
            query=query,
            rankings=(
                SemanticRanking(
                    name="semantic_text",
                    hits=(),
                    resolved=(resolved,),
                    scanned=4,
                    complete=True,
                    available=True,
                ),
            ),
            lexical_rankings=(),
            fused=(),
        )

    def materialize(value: object, **kwargs: object) -> KnowledgeCandidate:
        assert value is resolved
        assert kwargs == {
            "ranking_name": "semantic_text",
            "source_rank": 1,
            "producer": "semantic-v6",
        }
        return candidate

    monkeypatch.setattr(
        knowledge_search,
        "_planned_steps",
        lambda received_plan, channel: (
            (step,) if received_plan is plan and channel == "semantic" else ()
        ),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_owner_available",
        lambda received_snapshot, owner: received_snapshot is snapshot and owner == "semantic",
    )
    monkeypatch.setattr(knowledge_search, "_duration_ns", duration)
    monkeypatch.setattr(
        knowledge_search,
        "_candidate_from_resolved",
        materialize,
    )
    monkeypatch.setattr(knowledge_search.time, "perf_counter_ns", default_clock)
    monkeypatch.setattr(
        knowledge_search.semantic_service,
        "search_semantic_index",
        semantic_search,
    )

    rankings, discovery_signals, reports = knowledge_search._semantic_rankings(
        paths,
        plan,
        snapshot,
    )

    assert rankings == {"semantic_text": (candidate,)}
    assert discovery_signals == ()
    assert reports[0].elapsed_ns == 77
    assert reports[0].vectors_scanned == 4
    assert clock_calls == 1
    assert duration_calls == [(default_clock, 100)]

    expected = ValueError("semantic lower cancellation fixture")

    def cancel() -> None:
        raise expected

    def cancelled_search(*_args: object, **kwargs: object) -> None:
        callback = kwargs["cancellation_check"]
        assert callable(callback)
        callback()

    monkeypatch.setattr(
        knowledge_search.semantic_service,
        "search_semantic_index",
        cancelled_search,
    )
    with pytest.raises(ValueError) as raised:
        knowledge_search._semantic_rankings(
            paths,
            plan,
            snapshot,
            cancellation_check=cancel,
            clock_ns=lambda: 200,
        )
    assert raised.value is expected


def test_execute_preserves_semantic_cancellation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    plan = plan_knowledge_query(
        KnowledgeQuery("breaker protection", source_kinds=("pdf",), max_vectors=5)
    )
    snapshot = _snapshot()
    expected = ValueError("semantic outer cancellation fixture")
    cancellation_calls = 0

    def cancel() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        if cancellation_calls == 3:
            raise expected

    def semantic_rankings(
        _paths: KnowledgeStatePaths,
        _plan: KnowledgePlan,
        _snapshot: KnowledgeSnapshot,
        cancellation_check: Callable[[], None] | None = None,
        *,
        clock_ns: Callable[[], int] | None = None,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], list[RankingExecution]]:
        assert cancellation_check is not None
        assert clock_ns is not None
        cancellation_check()
        raise AssertionError("cancellation callback returned")

    monkeypatch.setattr(
        knowledge_search,
        "_lexical_rankings",
        lambda *_args, **_kwargs: ({}, []),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_semantic_rankings",
        semantic_rankings,
    )
    monkeypatch.setattr(
        knowledge_search,
        "_planned",
        lambda _plan, channel: channel == "semantic",
    )

    with pytest.raises(ValueError) as raised:
        knowledge_search.execute_knowledge_search(
            paths,
            plan,
            snapshot,
            cancellation_check=cancel,
            clock_ns=lambda: 100,
        )
    assert raised.value is expected
    assert cancellation_calls == 3


def test_exact_wrapper_forwards_current_lookup_limit_paths_and_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    plan = plan_knowledge_query(KnowledgeQuery("IEC-61850", limit=1))
    snapshot = _snapshot()

    def cancellation() -> None:
        return None

    def clock() -> int:
        return 10

    calls: list[tuple[object, ...]] = []

    def planned_limit(received_plan: KnowledgePlan, channel: str) -> int:
        assert received_plan is plan
        assert channel == "exact"
        return 7

    def exact_lookup(
        received_paths: KnowledgeStatePaths,
        received_plan: KnowledgePlan,
        received_snapshot: KnowledgeSnapshot,
        *,
        candidate_limit: int,
        cancellation_check: Callable[[], None] | None,
        clock_ns: Callable[[], int] | None,
    ) -> None:
        calls.append(
            (
                received_paths,
                received_plan,
                received_snapshot,
                candidate_limit,
                cancellation_check,
                clock_ns,
            )
        )

    monkeypatch.setattr(
        knowledge_search,
        "_planned_candidate_limit",
        planned_limit,
    )
    monkeypatch.setattr(knowledge_search, "lookup_plan_exact", exact_lookup)

    result = knowledge_search._exact_rankings(
        paths,
        plan,
        snapshot,
        cancellation_check=cancellation,
        clock_ns=clock,
    )

    assert result == ({}, [], 0, False, ())
    assert calls == [(paths, plan, snapshot, 7, cancellation, clock)]

    expected = RuntimeError("exact lower cancellation fixture")

    def cancelled_lookup(*_args: object, **kwargs: object) -> None:
        callback = kwargs["cancellation_check"]
        assert callable(callback)
        callback()

    monkeypatch.setattr(knowledge_search, "lookup_plan_exact", cancelled_lookup)
    with pytest.raises(RuntimeError) as raised:
        knowledge_search._exact_rankings(
            paths,
            plan,
            snapshot,
            cancellation_check=lambda: (_ for _ in ()).throw(expected),
            clock_ns=clock,
        )
    assert raised.value is expected


def test_materialization_wrapper_resolves_current_lower_helpers_and_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved(source_kind="custom-office")
    calls: list[str] = []

    def physical(value: ResolvedSearchHit) -> None:
        assert value is resolved
        calls.append("physical")
        return None

    def provenance(_mapping: object, name: str) -> None:
        calls.append(name)
        return None

    def revision(
        value: ResolvedSearchHit,
        producer: str,
    ) -> tuple[str, str, RevisionState, tuple[str, ...]]:
        assert value is resolved
        assert producer == "fixture-producer"
        calls.append("revision")
        return (
            "revision:late-bound",
            "late-bound-v1",
            RevisionState.CURRENT,
            ("late_bound_revision",),
        )

    monkeypatch.setattr(
        knowledge_search,
        "_resolved_physical_identity",
        physical,
    )
    monkeypatch.setattr(knowledge_search, "_int_provenance", provenance)
    monkeypatch.setattr(knowledge_search, "_revision_identity", revision)
    monkeypatch.setattr(
        knowledge_search,
        "_LEXICAL_OWNER_FORMATS",
        {
            "pdf": frozenset({"pdf"}),
            "docx": frozenset({"docx"}),
            "office": frozenset({"custom-office"}),
            "audio": frozenset({"audio"}),
        },
    )

    candidate = knowledge_search._candidate_from_resolved(
        resolved,
        ranking_name="semantic_text",
        source_rank=3,
        producer="fixture-producer",
    )

    assert candidate.resource.owner == "office"
    assert candidate.revision.revision_id == "revision:late-bound"
    assert candidate.revision.processing_signature == "late-bound-v1"
    assert candidate.signal.source_rank == 3
    assert candidate.signal.score_kind == "cosine"
    assert candidate.warnings == (
        "late_bound_revision",
        "physical_identity_unresolved",
    )
    assert calls == [
        "physical",
        "birthtime_ns",
        "revision",
    ]


def test_revision_identity_prefers_a_valid_owner_native_revision() -> None:
    resolved = _resolved(source_kind="text")
    resolved = replace(
        resolved,
        source_revision={
            **resolved.source_revision,
            "revision_id": "revision:text:owner-native-fixture",
        },
    )

    revision_id, signature, state, warnings = knowledge_search._revision_identity(
        resolved,
        "text-route-v2",
    )

    assert revision_id == "revision:text:owner-native-fixture"
    assert signature == "fixture-v1"
    assert state is RevisionState.CURRENT
    assert warnings == ()


# endregion [02]
