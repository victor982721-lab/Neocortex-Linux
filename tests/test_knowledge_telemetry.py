"""Phase-0 timing contracts and retry-safe Knowledge telemetry."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_telemetry.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.knowledge_search as knowledge_search
from _04_Nucleo_Operativo.knowledge_context import build_context_bundle
from _04_Nucleo_Operativo.knowledge_contracts import (
    KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE,
    KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE,
    KnowledgePhaseTiming,
    KnowledgeQueryTelemetry,
    KnowledgeSnapshot,
    KnowledgeTelemetryClock,
    KnowledgeTelemetryOperation,
    KnowledgeTimingPhase,
    OwnerAvailability,
    OwnerSnapshot,
)
from _04_Nucleo_Operativo.knowledge_exact import (
    ExactLookupKind,
    ExactLookupRequest,
    ExactLookupTerm,
    ExactOwnerTiming,
    lookup_exact,
)
from _04_Nucleo_Operativo.knowledge_planner import KnowledgeQuery, plan_knowledge_query
from _04_Nucleo_Operativo.knowledge_search import (
    RankingExecution,
    execute_knowledge_search,
)
from _04_Nucleo_Operativo.knowledge_service import KnowledgeSearchService
from _04_Nucleo_Operativo.knowledge_snapshot import KnowledgeStatePaths
from _04_Nucleo_Operativo.semantic_lexical import (
    LexicalStatePaths,
    search_lexical_sources,
)
from tests.test_knowledge_context import _hit as _context_hit
from tests.test_knowledge_context import _result as _context_result
from tests.test_knowledge_service import _result as _service_result
from tests.test_knowledge_service import _snapshot as _service_snapshot
from tests.test_knowledge_service import _SnapshotSequence
# endregion [01]

# region [02] Implementación


class _SequenceClock:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


class _IncrementingClock:
    def __init__(self, step: int = 100) -> None:
        self.value = 0
        self.step = step

    def __call__(self) -> int:
        current = self.value
        self.value += self.step
        return current


def _snapshot(*owners: OwnerSnapshot) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-29T12:00:00Z",
        captured_monotonic_ns=1,
        owners=owners,
    )


def test_timing_contracts_are_versioned_strict_and_bounded() -> None:
    phase = KnowledgePhaseTiming(
        KnowledgeTimingPhase.OWNER_RANKING,
        500_000,
        service_attempt=1,
        owner="pdf",
        ranking_names=("fts_pdf",),
    )
    telemetry = KnowledgeQueryTelemetry(
        KnowledgeTelemetryOperation.SEARCH,
        900_000,
        (phase,),
    )

    assert telemetry.to_dict()["schema_version"] == 1
    assert telemetry.to_dict()["clock_signature"] == "python-perf-counter-ns-v1"
    assert telemetry.to_dict()["phases"][0]["duration_ns"] == 500_000

    with pytest.raises(ValueError, match="duration_ns"):
        replace(phase, duration_ns=0.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="service_attempt"):
        replace(phase, service_attempt=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tuple"):
        replace(phase, ranking_names=["fts_pdf"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tuple"):
        replace(telemetry, phases=[phase])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="context compilation"):
        KnowledgeQueryTelemetry(
            KnowledgeTelemetryOperation.CONTEXT,
            1,
            (phase,),
        )


def test_clock_contract_reserves_the_runtime_signature_for_perf_counter() -> None:
    assert KnowledgeTelemetryClock().signature == KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE
    with pytest.raises(ValueError, match="reserved"):
        KnowledgeTelemetryClock(
            _IncrementingClock(),
            KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE,
        )

    legacy = KnowledgeTelemetryClock.from_legacy(_IncrementingClock())
    assert not legacy.identified
    assert legacy.signature == KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE
    assert not legacy.compatible_with(legacy.signature)
    assert legacy.compatible_with(legacy.signature, trust_unidentified=True)


def test_telemetry_never_changes_context_identity_or_rendered_payload() -> None:
    original = _context_result(_context_hit(1, suffix="stable", snippet="known", page=2))
    timing = KnowledgePhaseTiming(
        KnowledgeTimingPhase.FUSION,
        500_000,
        service_attempt=1,
    )
    instrumented = replace(
        original,
        telemetry=KnowledgeQueryTelemetry(
            KnowledgeTelemetryOperation.SEARCH,
            750_000,
            (timing,),
        ),
    )

    plain = build_context_bundle(original, character_limit=4_000)
    observed = build_context_bundle(instrumented, character_limit=4_000)

    assert plain == observed
    assert plain.to_dict() == observed.to_dict()
    assert plain.rendered_context == observed.rendered_context
    assert plain.budget == observed.budget
    assert plain.citation_ids == observed.citation_ids
    assert original.plan.plan_id == instrumented.plan.plan_id
    assert original.snapshot.snapshot_id == instrumented.snapshot.snapshot_id
    assert "telemetry" not in original.to_dict()
    assert "telemetry" in instrumented.to_dict()


def test_lexical_records_each_owner_independently_in_nanoseconds() -> None:
    clock = _SequenceClock(0, 11, 20, 33, 40, 57, 60, 83, 90, 119)

    rankings = search_lexical_sources(
        LexicalStatePaths(),
        "relay protection",
        clock_ns=clock,
    )

    assert [ranking.ranking_name for ranking in rankings] == [
        "fts_pdf",
        "fts_docx",
        "fts_office",
        "fts_audio",
        "fts_video",
    ]
    assert [ranking.elapsed_ns for ranking in rankings] == [11, 13, 17, 23, 29]
    assert all(not ranking.hits for ranking in rankings)


def test_lexical_rejects_a_regressing_monotonic_clock() -> None:
    with pytest.raises(RuntimeError, match="clock moved backwards"):
        search_lexical_sources(
            LexicalStatePaths(),
            "relay protection",
            clock_ns=_SequenceClock(10, 9),
        )


def test_exact_times_one_owner_batch_for_multiple_term_rankings(tmp_path: Path) -> None:
    state = tmp_path / "absent-state"
    snapshot = _snapshot(
        OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
    )
    request = ExactLookupRequest(
        (
            ExactLookupTerm(ExactLookupKind.PATH, "C:/A/report.pdf"),
            ExactLookupTerm(ExactLookupKind.PATH, "C:/B/report.pdf"),
        ),
        owner_scope=("inventory",),
    )

    result = lookup_exact(
        KnowledgeStatePaths.from_directory(state),
        snapshot,
        request,
        clock_ns=_SequenceClock(100, 175),
    )

    assert len(result.owner_timings) == 1
    timing = result.owner_timings[0]
    assert timing.owner == "inventory"
    assert timing.duration_ns == 75
    assert not timing.executed
    assert len(timing.ranking_names) == 2
    assert len(set(timing.ranking_names)) == 2
    assert not state.exists()


def test_broker_exposes_one_signed_clock_across_every_owner_and_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_contract = KnowledgeTelemetryClock(
        _IncrementingClock(100),
        "fixture-incrementing-clock-ns-v1",
    )
    received_clock_contracts: list[object | None] = []

    def measured_duration(kwargs: dict[str, object]) -> int:
        reader = kwargs["clock_ns"]
        assert callable(reader)
        received_clock_contracts.append(getattr(reader, "__self__", None))
        started_ns = reader()
        return reader() - started_ns

    def lexical(*_args: object, **kwargs: object):
        return {}, [
            RankingExecution(
                "fts_pdf",
                "lexical",
                True,
                True,
                True,
                0,
                owner="pdf",
                elapsed_ns=measured_duration(kwargs),
            )
        ]

    def semantic(*_args: object, **kwargs: object):
        return {}, [
            RankingExecution(
                "semantic_text",
                "semantic",
                True,
                True,
                True,
                0,
                owner="semantic",
                elapsed_ns=measured_duration(kwargs),
            )
        ]

    def exact(*_args: object, **kwargs: object):
        return (
            {},
            [],
            0,
            False,
            (
                ExactOwnerTiming(
                    "inventory",
                    ("exact_inventory_path:fixture",),
                    measured_duration(kwargs),
                    True,
                ),
            ),
        )

    def code(*_args: object, **_kwargs: object):
        return (), RankingExecution("code_structural", "structural_code", True, True, True, 0)

    def catalog(*_args: object, **_kwargs: object):
        return (), RankingExecution("catalog_metadata", "catalog", True, True, True, 0)

    def dispositions(_paths, _snapshot_value, rankings, **_kwargs):
        return dict(rankings), RankingExecution(
            "inventory_duplicate_plan", "relationship", True, True, True, 0
        )

    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical)
    monkeypatch.setattr(knowledge_search, "_semantic_rankings", semantic)
    monkeypatch.setattr(knowledge_search, "_exact_rankings", exact)
    monkeypatch.setattr(knowledge_search, "_code_ranking", code)
    monkeypatch.setattr(knowledge_search, "_catalog_ranking", catalog)
    monkeypatch.setattr(knowledge_search, "_apply_inventory_dispositions", dispositions)
    monkeypatch.setattr(
        knowledge_search,
        "_planned",
        lambda _plan, channel: channel in {"semantic", "exact", "structural_code", "catalog"},
    )
    monkeypatch.setattr(
        knowledge_search,
        "fuse_evidence_rankings",
        lambda _rankings, **_kwargs: ((), 0),
    )

    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan_knowledge_query(KnowledgeQuery("report.pdf", source_kinds=("pdf",))),
        _snapshot(),
        telemetry_clock=clock_contract,
    )

    assert received_clock_contracts == [clock_contract] * 3
    assert result.telemetry is not None
    assert result.telemetry.clock_signature == clock_contract.signature
    owner_phases = tuple(
        phase
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.OWNER_RANKING
    )
    assert {(phase.owner, phase.ranking_names) for phase in owner_phases} == {
        ("pdf", ("fts_pdf",)),
        ("semantic", ("semantic_text",)),
        ("inventory", ("exact_inventory_path:fixture",)),
        ("code", ("code_structural",)),
        ("catalog", ("catalog_metadata",)),
        ("inventory", ("inventory_duplicate_plan",)),
    }
    assert {phase.duration_ns for phase in owner_phases} == {100}
    assert [
        phase.duration_ns
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.FUSION
    ] == [100]
    assert [
        phase.duration_ns
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.BROKER
    ] == [1_500]
    assert result.telemetry.total_duration_ns == 1_500
    assert result.elapsed_milliseconds == 0
    assert result.to_dict()["elapsed_milliseconds"] == 0


def test_legacy_custom_broker_clock_never_masquerades_as_perf_counter(
    tmp_path: Path,
) -> None:
    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "absent-state"),
        plan_knowledge_query(KnowledgeQuery("relay protection")),
        _snapshot(),
        clock_ns=_IncrementingClock(),
    )

    assert result.telemetry is not None
    assert result.telemetry.clock_signature == KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE
    assert result.telemetry.clock_signature != KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE


@pytest.mark.parametrize(
    ("child_signature", "expected_child_timing"),
    (
        ("fixture-service-clock-ns-v1", True),
        ("foreign-clock-ns-v1", False),
    ),
)
def test_service_merges_only_compatible_identified_clock_domains(
    tmp_path: Path,
    child_signature: str,
    expected_child_timing: bool,
) -> None:
    stable = _service_snapshot("stable")
    snapshots = _SnapshotSequence([stable, stable])
    clock_contract = KnowledgeTelemetryClock(
        _IncrementingClock(),
        "fixture-service-clock-ns-v1",
    )

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        child_phases = (
            KnowledgePhaseTiming(
                KnowledgeTimingPhase.OWNER_RANKING,
                17,
                service_attempt=1,
                owner="child",
                ranking_names=("child_ranking",),
            ),
            KnowledgePhaseTiming(
                KnowledgeTimingPhase.BROKER,
                19,
                service_attempt=1,
            ),
        )
        return replace(
            _service_result(plan, snapshot, "stable"),
            telemetry=KnowledgeQueryTelemetry(
                KnowledgeTelemetryOperation.SEARCH,
                23,
                child_phases,
                clock_signature=child_signature,
            ),
        )

    result = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(tmp_path / "state"),
        source_version="0.7.0",
        snapshot_collector=snapshots,
        search_executor=execute,
        telemetry_clock=clock_contract,
    ).search(KnowledgeQuery("relay protection"))

    assert result.telemetry is not None
    assert result.telemetry.clock_signature == clock_contract.signature
    child_timings = tuple(phase for phase in result.telemetry.phases if phase.owner == "child")
    broker_durations = [
        phase.duration_ns
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.BROKER
    ]
    if expected_child_timing:
        assert len(child_timings) == 1
        assert broker_durations == [19]
    else:
        assert not child_timings
        assert broker_durations == [100]


def test_service_retry_retains_both_attempts_with_fake_clock(tmp_path: Path) -> None:
    first = _service_snapshot("first")
    second = _service_snapshot("second")
    snapshots = _SnapshotSequence([first, second, second, second])
    executions = 0

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        nonlocal executions
        del paths, cancellation_check
        executions += 1
        return _service_result(plan, snapshot, str(executions))

    service = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(tmp_path / "state"),
        source_version="0.7.0",
        snapshot_collector=snapshots,
        search_executor=execute,
        clock_ns=_IncrementingClock(),
    )
    result = service.search(KnowledgeQuery("relay protection"))

    assert result.telemetry is not None
    assert result.elapsed_milliseconds == 1
    assert executions == 2
    assert [
        phase.service_attempt
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.SNAPSHOT_BEFORE
    ] == [1, 2]
    assert [
        phase.service_attempt
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.BROKER
    ] == [1, 2]
    assert [
        phase.service_attempt
        for phase in result.telemetry.phases
        if phase.phase is KnowledgeTimingPhase.SNAPSHOT_AFTER
    ] == [1, 2]
    assert (
        sum(phase.phase is KnowledgeTimingPhase.PLANNER for phase in result.telemetry.phases) == 1
    )
    assert "snapshot_retry_succeeded" in result.warnings


def test_context_timing_is_outside_the_pure_rendered_bundle(tmp_path: Path) -> None:
    stable = _service_snapshot("stable")
    snapshots = _SnapshotSequence([stable, stable])
    observed_results = []

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        del paths, cancellation_check
        return _service_result(plan, snapshot, "stable")

    def build(result, *, max_characters, max_hits):
        observed_results.append(result)
        return build_context_bundle(
            result,
            character_limit=max_characters,
            max_hits=max_hits or 12,
        )

    service = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(tmp_path / "state"),
        source_version="0.7.0",
        snapshot_collector=snapshots,
        search_executor=execute,
        context_builder=build,
        clock_ns=_IncrementingClock(),
    )
    bundle = service.context(
        KnowledgeQuery("relay protection"),
        max_characters=4_000,
        max_hits=4,
    )
    pure = build_context_bundle(
        observed_results[0],
        character_limit=4_000,
        max_hits=4,
    )

    assert bundle.telemetry is not None
    assert bundle.telemetry.operation is KnowledgeTelemetryOperation.CONTEXT
    assert (
        sum(
            phase.phase is KnowledgeTimingPhase.CONTEXT_COMPILE for phase in bundle.telemetry.phases
        )
        == 1
    )
    assert bundle == pure
    assert bundle.rendered_context == pure.rendered_context
    assert bundle.budget == pure.budget
    assert bundle.citation_ids == pure.citation_ids
    assert "telemetry" in bundle.to_dict()
    assert "knowledge_query_telemetry" not in bundle.rendered_context


# endregion [02]
