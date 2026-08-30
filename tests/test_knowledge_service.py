"""Stable, retry-bounded service semantics for read-only Knowledge queries."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_service.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import neocortex.knowledge.knowledge_snapshot as knowledge_snapshot
from neocortex.knowledge.knowledge_contracts import (
    ContextBudget,
    ContextBundle,
    ContextGraphBudget,
    ContextPlanRef,
    ContextPlanStepRef,
    EvidenceMethod,
    EvidenceRef,
    KnowledgeCompleteness,
    KnowledgeHit,
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    PhysicalIdentityRef,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
    SnapshotConsistency,
)
from neocortex.knowledge.knowledge_planner import KnowledgePlan, KnowledgeQuery
from neocortex.knowledge.knowledge_search import KnowledgeSearchResult
from neocortex.knowledge.knowledge_context import (
    MAX_CONTEXT_CHARACTER_LIMIT,
    MAX_CONTEXT_HITS,
)
from neocortex.knowledge.knowledge_service import (
    KnowledgeSearchService,
    KnowledgeStateRootError,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
# endregion [01]

# region [02] Implementación


def _snapshot(marker: str) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T02:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "code",
                OwnerAvailability.AVAILABLE,
                2,
                2,
                watermarks=(LogicalWatermark("fixture", marker),),
            ),
            OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
        ),
    )


def _hit(marker: str) -> KnowledgeHit:
    resource = ResourceRef(
        resource_id=f"resource:{marker}",
        source_kind="code",
        owner="code",
        physical_identity=PhysicalIdentityRef("fixture", marker, 1),
        current_path=f"C:/fixture/{marker}.py",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=f"revision:{marker}",
        producer="fixture",
        processing_signature="fixture-v1",
        generation=None,
        state=RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        evidence_id=f"evidence:{marker}",
        resource_id=resource.resource_id,
        revision_id=revision.revision_id,
        method=EvidenceMethod.STRUCTURAL,
        start_line=1,
        end_line=1,
        section_kind="code_symbol",
        section_id=marker,
        snippet=marker,
    )
    return KnowledgeHit(
        rank=1,
        resource=resource,
        revision=revision,
        evidence=evidence,
        signals=(RankingSignal("fixture", "rank", 1.0, 1),),
        fused_score=1.0,
        reasons=("fixture retrieval",),
    )


def _result(
    plan: KnowledgePlan,
    snapshot: KnowledgeSnapshot,
    marker: str,
) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        plan=plan,
        snapshot=snapshot,
        hits=(_hit(marker),),
        rankings=(),
        complete=True,
        truncated=False,
        omitted_candidates=0,
        rows_scanned=1,
        vectors_scanned=0,
        elapsed_milliseconds=1,
    )


class _SnapshotSequence:
    def __init__(self, values: list[KnowledgeSnapshot]) -> None:
        self._values = values
        self.calls = 0
        self.cancellation_checks: list[Callable[[], None] | None] = []

    def __call__(
        self,
        paths: KnowledgeStatePaths,
        *,
        source_version: str,
        cancellation_check: Callable[[], None] | None = None,
    ) -> KnowledgeSnapshot:
        del paths
        assert source_version == "0.7.0"
        self.cancellation_checks.append(cancellation_check)
        value = self._values[self.calls]
        self.calls += 1
        return value


def _service(
    tmp_path: Path,
    snapshots: _SnapshotSequence,
    executor: Callable[..., KnowledgeSearchResult],
    *,
    context_builder: Callable[..., ContextBundle] | None = None,
) -> KnowledgeSearchService:
    return KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(tmp_path / "state"),
        source_version="0.7.0",
        snapshot_collector=snapshots,
        search_executor=executor,
        context_builder=context_builder,
    )


def test_stable_query_uses_one_before_and_after_snapshot(tmp_path: Path) -> None:
    stable = _snapshot("stable")
    snapshots = _SnapshotSequence([stable, stable])
    executions: list[str] = []

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        executions.append(snapshot.snapshot_id)
        return _result(plan, snapshot, "stable")

    result = _service(tmp_path, snapshots, execute).search(KnowledgeQuery("relay protection"))

    assert snapshots.calls == 2
    assert executions == [stable.snapshot_id]
    assert result.complete
    assert result.snapshot.snapshot_id == stable.snapshot_id
    assert result.hits[0].evidence.evidence_id == "evidence:stable"


def test_one_identity_change_retries_the_whole_retrieval_once(
    tmp_path: Path,
) -> None:
    first = _snapshot("first")
    second = _snapshot("second")
    snapshots = _SnapshotSequence([first, second, second, second])
    executions: list[str] = []

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        executions.append(snapshot.snapshot_id)
        return _result(plan, snapshot, str(len(executions)))

    result = _service(tmp_path, snapshots, execute).search(KnowledgeQuery("relay protection"))

    assert snapshots.calls == 4
    assert executions == [first.snapshot_id, second.snapshot_id]
    assert result.complete
    assert result.snapshot.snapshot_id == second.snapshot_id
    assert result.hits[0].evidence.evidence_id == "evidence:2"
    assert "snapshot_retry_succeeded" in result.warnings


def test_second_identity_change_returns_latest_hits_as_observable_partial(
    tmp_path: Path,
) -> None:
    one = _snapshot("one")
    two = _snapshot("two")
    three = _snapshot("three")
    four = _snapshot("four")
    snapshots = _SnapshotSequence([one, two, three, four])
    executions: list[str] = []

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        executions.append(snapshot.snapshot_id)
        return _result(plan, snapshot, str(len(executions)))

    result = _service(tmp_path, snapshots, execute).search(KnowledgeQuery("relay protection"))

    assert executions == [one.snapshot_id, three.snapshot_id]
    assert result.hits[0].evidence.evidence_id == "evidence:2"
    assert not result.complete
    assert result.snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    assert result.snapshot.owners[0].identity_changed
    assert result.snapshot.owners[0].watermarks == three.owners[0].watermarks
    assert result.snapshot.owners[0].data_version_before is None
    assert result.snapshot.owners[0].data_version_after is None
    assert not result.snapshot.owners[1].identity_changed
    assert "snapshot_changed_during_query" in result.warnings
    assert any(warning.startswith("snapshot_after:") for warning in result.warnings)


def test_cancellation_callback_is_propagated_to_retrieval(tmp_path: Path) -> None:
    stable = _snapshot("stable")
    snapshots = _SnapshotSequence([stable, stable])
    callback_calls = 0
    observed_callback = None

    def check_cancelled() -> None:
        nonlocal callback_calls
        callback_calls += 1

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        nonlocal observed_callback
        del paths
        observed_callback = cancellation_check
        assert cancellation_check is not None
        cancellation_check()
        return _result(plan, snapshot, "stable")

    _service(tmp_path, snapshots, execute).search(
        KnowledgeQuery("relay protection"),
        cancellation_check=check_cancelled,
    )

    assert observed_callback is check_cancelled
    assert snapshots.cancellation_checks == [check_cancelled, check_cancelled]
    assert callback_calls >= 5


def test_cancellation_exception_is_not_swallowed(tmp_path: Path) -> None:
    class Cancelled(RuntimeError):
        pass

    snapshots = _SnapshotSequence([_snapshot("unused")])

    def cancel() -> None:
        raise Cancelled

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        raise AssertionError("retrieval must not start after cancellation")

    with pytest.raises(Cancelled):
        _service(tmp_path, snapshots, execute).search(
            KnowledgeQuery("relay protection"),
            cancellation_check=cancel,
        )
    assert snapshots.calls == 0


def test_context_uses_stable_search_result_and_injected_pure_builder(
    tmp_path: Path,
) -> None:
    stable = _snapshot("stable")
    snapshots = _SnapshotSequence([stable, stable])
    builder_calls: list[tuple[int, int | None]] = []

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        return _result(plan, snapshot, "stable")

    def build_context(result, *, max_characters, max_hits):
        builder_calls.append((max_characters, max_hits))
        plan = result.plan
        context_plan = ContextPlanRef(
            plan_id=plan.plan_id,
            normalized_query=plan.normalized_query,
            retrieval_mode=plan.retrieval_mode.value,
            intents=plan.intents,
            exact_terms=plan.exact_terms,
            source_kinds=plan.source_kinds,
            formats=plan.formats,
            project=plan.project,
            date_from=plan.date_from,
            date_to=plan.date_to,
            include_history=plan.include_history,
            limit=plan.limit,
            max_per_resource=plan.max_per_resource,
            min_section_distance=plan.min_section_distance,
            max_vectors=plan.max_vectors,
            steps=tuple(
                ContextPlanStepRef(
                    channel=step.channel,
                    ranking_name=step.ranking_name,
                    reason=step.reason,
                    candidate_limit=step.candidate_limit,
                    required=step.required,
                )
                for step in plan.steps
            ),
            notices=plan.notices,
        )
        return ContextBundle(
            normalized_query=plan.normalized_query,
            intents=plan.intents,
            plan_id=plan.plan_id,
            plan=context_plan,
            snapshot=result.snapshot,
            selected_hits=(),
            citation_ids=(),
            graph_budget=ContextGraphBudget(0, 0, 0),
            budget=ContextBudget(max_characters, 0, 0, "fixture-v1"),
            rendered_context="",
            completeness=KnowledgeCompleteness.NO_EVIDENCE,
        )

    bundle = _service(
        tmp_path,
        snapshots,
        execute,
        context_builder=build_context,
    ).context(
        KnowledgeQuery("relay protection"),
        max_characters=2_048,
        max_hits=4,
    )

    assert builder_calls == [(2_048, 4)]
    assert bundle.snapshot.snapshot_id == stable.snapshot_id


@pytest.mark.parametrize(
    ("context_options", "message"),
    (
        ({"max_hits": MAX_CONTEXT_HITS + 1}, "max_hits"),
        (
            {"max_characters": MAX_CONTEXT_CHARACTER_LIMIT + 1},
            "max_characters",
        ),
    ),
)
def test_context_rejects_upper_limits_before_read_or_builder_dispatch(
    tmp_path: Path,
    context_options: dict[str, Any],
    message: str,
) -> None:
    snapshots = _SnapshotSequence([])
    retrieval_calls = 0
    builder_calls = 0

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        nonlocal retrieval_calls
        retrieval_calls += 1
        raise AssertionError("retrieval must not run for an invalid context limit")

    def build_context(result, *, max_characters, max_hits):
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("builder must not run for an invalid context limit")

    service = _service(
        tmp_path,
        snapshots,
        execute,
        context_builder=build_context,
    )

    with pytest.raises(ValueError, match=message):
        service.context(KnowledgeQuery("relay protection"), **context_options)

    assert snapshots.calls == 0
    assert retrieval_calls == 0
    assert builder_calls == 0
    assert not (tmp_path / "state").exists()


def test_default_context_builder_accepts_service_budget_vocabulary(
    tmp_path: Path,
) -> None:
    stable = _snapshot("stable")
    snapshots = _SnapshotSequence([stable, stable])

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        return _result(plan, snapshot, "stable")

    bundle = _service(tmp_path, snapshots, execute).context(
        KnowledgeQuery("relay protection"),
        max_characters=2_048,
    )

    assert bundle.selected_hits
    assert bundle.citation_ids == (("K1", "evidence:stable"),)
    assert bundle.budget.character_limit == 2_048
    assert len(bundle.rendered_context) <= 2_048

    evidence_first = _service(
        tmp_path,
        _SnapshotSequence([stable, stable]),
        execute,
    ).context(
        KnowledgeQuery("relay protection"),
        max_characters=1_800,
    )

    assert evidence_first.selected_hits
    assert evidence_first.citation_ids == (("K1", "evidence:stable"),)
    assert evidence_first.completeness is KnowledgeCompleteness.COMPLETE
    assert evidence_first.budget.omitted_candidates == 0
    assert evidence_first.missing_information == ()
    assert "[K1] target=" in evidence_first.rendered_context
    assert "diagnostics=[omitted: character budget]" in (evidence_first.rendered_context)
    assert len(evidence_first.rendered_context) <= 1_800


def test_default_status_and_search_do_not_create_absent_owner_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "absent-state"
    service = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )

    snapshot = service.status()
    result = service.search(KnowledgeQuery("relay protection"))

    assert snapshot.consistency is SnapshotConsistency.STABLE
    assert snapshot.owners
    assert all(owner.state is OwnerAvailability.ABSENT for owner in snapshot.owners)
    assert result.hits == ()
    assert not state.exists()


def test_existing_non_directory_state_root_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state-file"
    original = b"not a state directory"
    state.write_bytes(original)
    service = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
    )

    with pytest.raises(KnowledgeStateRootError) as raised:
        service.status()

    assert raised.value.root == state.resolve()
    assert raised.value.reason == "is not a directory"
    assert state.read_bytes() == original


def test_inaccessible_state_root_fails_before_owner_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    snapshots = _SnapshotSequence([_snapshot("must-not-be-read")])
    service = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(state),
        source_version="0.7.0",
        snapshot_collector=snapshots,
    )

    def deny_directory_open(_path: object) -> None:
        raise PermissionError(13, "access denied", str(state))

    monkeypatch.setattr(knowledge_snapshot.os, "scandir", deny_directory_open)

    with pytest.raises(KnowledgeStateRootError) as raised:
        service.status()

    assert raised.value.root == state.resolve()
    assert raised.value.reason == "is inaccessible"
    assert snapshots.calls == 0


# endregion [02]
