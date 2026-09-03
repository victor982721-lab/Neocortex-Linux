"""Read-only service boundary for stable cross-owner Knowledge queries.

The service owns bounded consistency retries, not any owner database.  It
captures a logical snapshot before and after retrieval, retries the entire
retrieval once when that view is unstable, and exposes a partial result with a
``snapshot_changed`` marker if the second attempt also changes.
"""

from __future__ import annotations
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, cast

from neocortex import __version__

from .knowledge_contracts import (
    ContextBundle,
    KnowledgePhaseTiming,
    KnowledgeQueryTelemetry,
    KnowledgeSnapshot,
    KnowledgeTelemetryClock,
    KnowledgeTelemetryOperation,
    KnowledgeTimingPhase,
    OwnerSnapshot,
    SnapshotConsistency,
)
from .knowledge_planner import KnowledgePlan, KnowledgeQuery, plan_knowledge_query
from .knowledge_snapshot import (
    KnowledgeStatePaths,
    KnowledgeStateRootError,
    collect_knowledge_snapshot,
)

if TYPE_CHECKING:
    from .knowledge_search import KnowledgeSearchResult
else:
    KnowledgeSearchResult = Any


# region [01] Injectable read-only boundaries


CancellationCheck = Callable[[], None]
ClockNanoseconds = Callable[[], int]


def _default_snapshot_collector(
    paths: KnowledgeStatePaths,
    *,
    source_version: str,
    cancellation_check: CancellationCheck | None = None,
) -> KnowledgeSnapshot:
    """Collect the public Knowledge view through the immutable read kernel."""

    return collect_knowledge_snapshot(
        paths,
        source_version=source_version,
        cancellation_check=cancellation_check,
        # Select immutable_strict for quiescent owners and a detached
        # snapshot_temp session when a WAL/journal is active.
        _immutable_owners=None,
    )


class SnapshotCollector(Protocol):
    def __call__(
        self,
        paths: KnowledgeStatePaths,
        *,
        source_version: str,
        cancellation_check: CancellationCheck | None = None,
    ) -> KnowledgeSnapshot: ...


class QueryPlanner(Protocol):
    def __call__(self, query: KnowledgeQuery) -> KnowledgePlan: ...


class SearchExecutor(Protocol):
    def __call__(
        self,
        paths: KnowledgeStatePaths,
        plan: KnowledgePlan,
        snapshot: KnowledgeSnapshot,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> KnowledgeSearchResult: ...


class _ClockAwareSearchExecutor(Protocol):
    def __call__(
        self,
        paths: KnowledgeStatePaths,
        plan: KnowledgePlan,
        snapshot: KnowledgeSnapshot,
        *,
        cancellation_check: CancellationCheck | None = None,
        telemetry_clock: KnowledgeTelemetryClock | None = None,
    ) -> KnowledgeSearchResult: ...


class ContextBuilder(Protocol):
    def __call__(
        self,
        result: KnowledgeSearchResult,
        *,
        max_characters: int,
        max_hits: int | None,
    ) -> ContextBundle: ...


class _ContextCompiler(Protocol):
    def __call__(
        self,
        result: KnowledgeSearchResult,
        *,
        character_limit: int,
        max_hits: int = 12,
    ) -> ContextBundle: ...


def _default_search_executor(
    paths: KnowledgeStatePaths,
    plan: KnowledgePlan,
    snapshot: KnowledgeSnapshot,
    *,
    cancellation_check: CancellationCheck | None = None,
    telemetry_clock: KnowledgeTelemetryClock | None = None,
) -> KnowledgeSearchResult:
    module = import_module(f"{__package__}.knowledge_search")
    executor = cast(
        _ClockAwareSearchExecutor,
        getattr(module, "execute_knowledge_search"),  # noqa: B009
    )
    return executor(
        paths,
        plan,
        snapshot,
        cancellation_check=cancellation_check,
        telemetry_clock=telemetry_clock,
    )


def _context_limits() -> tuple[int, int, int]:
    module = import_module(f"{__package__}.knowledge_context")
    return (
        int(getattr(module, "DEFAULT_CONTEXT_CHARACTER_LIMIT")),  # noqa: B009
        int(getattr(module, "MAX_CONTEXT_CHARACTER_LIMIT")),  # noqa: B009
        int(getattr(module, "MAX_CONTEXT_HITS")),  # noqa: B009
    )


def _default_context_builder(
    result: KnowledgeSearchResult,
    *,
    max_characters: int,
    max_hits: int | None,
) -> ContextBundle:
    # Kept lazy so status and search do not depend on the context compiler.
    module = import_module(f"{__package__}.knowledge_context")
    builder = cast(_ContextCompiler, getattr(module, "build_context_bundle"))  # noqa: B009
    if max_hits is None:
        return builder(result, character_limit=max_characters)
    return builder(result, character_limit=max_characters, max_hits=max_hits)


# endregion [01]


# region [02] Consistency helpers


def _checkpoint(cancellation_check: CancellationCheck | None) -> None:
    if cancellation_check is not None:
        cancellation_check()


def _duration_ns(clock_ns: ClockNanoseconds, started_ns: int) -> int:
    finished_ns = clock_ns()
    if (
        isinstance(finished_ns, bool)
        or not isinstance(finished_ns, int)
        or finished_ns < started_ns
    ):
        raise RuntimeError("Knowledge service clock moved backwards or was invalid")
    return finished_ns - started_ns


def _attempt_phases(
    telemetry: KnowledgeQueryTelemetry | None,
    service_attempt: int,
    clock: KnowledgeTelemetryClock,
    *,
    trust_unidentified: bool,
) -> tuple[KnowledgePhaseTiming, ...]:
    if (
        telemetry is None
        or telemetry.operation is not KnowledgeTelemetryOperation.SEARCH
        or not clock.compatible_with(
            telemetry.clock_signature,
            trust_unidentified=trust_unidentified,
        )
    ):
        return ()
    return tuple(
        replace(phase, service_attempt=service_attempt)
        if phase.service_attempt
        else phase
        for phase in telemetry.phases
    )


def _stable_identity(
    before: KnowledgeSnapshot,
    after: KnowledgeSnapshot,
) -> bool:
    return (
        before.consistency is SnapshotConsistency.STABLE
        and after.consistency is SnapshotConsistency.STABLE
        and before.snapshot_id == after.snapshot_id
    )


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _changed_owner_names(
    before: KnowledgeSnapshot,
    after: KnowledgeSnapshot,
) -> tuple[str, ...]:
    before_by_name = {
        owner.owner: owner.identity_dict() for owner in before.owners
    }
    after_by_name = {owner.owner: owner.identity_dict() for owner in after.owners}
    names = before_by_name.keys() | after_by_name.keys()
    return tuple(
        sorted(
            name
            for name in names
            if before_by_name.get(name) != after_by_name.get(name)
        )
    )


def _marked_retrieval_owners(
    retrieval_snapshot: KnowledgeSnapshot,
    after: KnowledgeSnapshot,
) -> tuple[OwnerSnapshot, ...]:
    after_by_name = {owner.owner: owner for owner in after.owners}
    retrieval_names = {owner.owner for owner in retrieval_snapshot.owners}
    marked: list[OwnerSnapshot] = []
    for owner in retrieval_snapshot.owners:
        after_owner = after_by_name.get(owner.owner)
        identity_changed = (
            after_owner is None
            or owner.identity_dict() != after_owner.identity_dict()
        )
        marked.append(replace(owner, identity_changed=identity_changed))

    for owner in sorted(after.owners, key=lambda item: item.owner):
        if owner.owner not in retrieval_names:
            marked.append(replace(owner, identity_changed=True))

    return tuple(marked)


def _changed_snapshot_marker(
    retrieval_snapshot: KnowledgeSnapshot,
    after: KnowledgeSnapshot,
) -> KnowledgeSnapshot:
    changed_owners = _changed_owner_names(retrieval_snapshot, after)
    warnings = list(retrieval_snapshot.warnings)
    warnings.append("snapshot_changed_during_query")
    if changed_owners:
        warnings.append(f"snapshot_changed_owners:{','.join(changed_owners)}")
    warnings.append(f"snapshot_after:{after.snapshot_id}")
    return KnowledgeSnapshot.create(
        source_version=retrieval_snapshot.source_version,
        captured_at_utc=retrieval_snapshot.captured_at_utc,
        captured_monotonic_ns=retrieval_snapshot.captured_monotonic_ns,
        owners=_marked_retrieval_owners(retrieval_snapshot, after),
        active_models=retrieval_snapshot.active_models,
        consistency=SnapshotConsistency.SNAPSHOT_CHANGED,
        attempts=2,
        warnings=_deduplicate(tuple(warnings)),
    )


# endregion [02]


# region [03] Public service


@dataclass(frozen=True, slots=True)
class KnowledgeSearchService:
    """Coordinate deterministic read-only status, search and context calls."""

    paths: KnowledgeStatePaths
    source_version: str = __version__
    snapshot_collector: SnapshotCollector = _default_snapshot_collector
    query_planner: QueryPlanner = plan_knowledge_query
    search_executor: SearchExecutor = _default_search_executor
    context_builder: ContextBuilder | None = None
    clock_ns: ClockNanoseconds = field(
        default=time.perf_counter_ns,
        compare=False,
        repr=False,
    )
    telemetry_clock: KnowledgeTelemetryClock | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.source_version.strip():
            raise ValueError("Knowledge source version cannot be blank")
        if not callable(self.clock_ns):
            raise ValueError("Knowledge clock_ns must be callable")
        if self.telemetry_clock is not None and not isinstance(
            self.telemetry_clock,
            KnowledgeTelemetryClock,
        ):
            raise ValueError("telemetry_clock must be a KnowledgeTelemetryClock")
        if (
            self.telemetry_clock is not None
            and self.clock_ns is not time.perf_counter_ns
        ):
            raise ValueError(
                "telemetry_clock and legacy clock_ns cannot both be provided"
            )
        self._clock_contract()

    def _clock_contract(self) -> KnowledgeTelemetryClock:
        if self.telemetry_clock is not None:
            return self.telemetry_clock
        return KnowledgeTelemetryClock.from_legacy(self.clock_ns)

    def _collect_snapshot(
        self,
        cancellation_check: CancellationCheck | None,
    ) -> KnowledgeSnapshot:
        _checkpoint(cancellation_check)
        self.paths.validate_roots()
        _checkpoint(cancellation_check)
        snapshot = self.snapshot_collector(
            self.paths,
            source_version=self.source_version,
            cancellation_check=cancellation_check,
        )
        _checkpoint(cancellation_check)
        return snapshot

    def status(
        self,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> KnowledgeSnapshot:
        """Return one bounded logical status snapshot without creating state."""

        return self._collect_snapshot(cancellation_check)

    def search(
        self,
        query: KnowledgeQuery,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> KnowledgeSearchResult:
        """Execute against a stable view, retrying the whole retrieval once."""

        clock_contract = self._clock_contract()
        clock = clock_contract.now_ns
        operation_started_ns = clock()
        _checkpoint(cancellation_check)
        planner_started_ns = clock()
        plan = self.query_planner(query)
        phase_timings: list[KnowledgePhaseTiming] = [
            KnowledgePhaseTiming(
                KnowledgeTimingPhase.PLANNER,
                _duration_ns(clock, planner_started_ns),
            )
        ]
        _checkpoint(cancellation_check)

        first_view_changed = False
        for service_attempt in (1, 2):
            snapshot_started_ns = clock()
            before = self._collect_snapshot(cancellation_check)
            phase_timings.append(
                KnowledgePhaseTiming(
                    KnowledgeTimingPhase.SNAPSHOT_BEFORE,
                    _duration_ns(clock, snapshot_started_ns),
                    service_attempt=service_attempt,
                    snapshot_id=before.snapshot_id,
                )
            )
            executor_started_ns = clock()
            trusted_clock_handoff = self.search_executor is _default_search_executor
            if trusted_clock_handoff:
                result = _default_search_executor(
                    self.paths,
                    plan,
                    before,
                    cancellation_check=cancellation_check,
                    telemetry_clock=clock_contract,
                )
            else:
                result = self.search_executor(
                    self.paths,
                    plan,
                    before,
                    cancellation_check=cancellation_check,
                )
            executor_duration_ns = _duration_ns(clock, executor_started_ns)
            attempt_phases = _attempt_phases(
                result.telemetry,
                service_attempt,
                clock_contract,
                trust_unidentified=trusted_clock_handoff,
            )
            if attempt_phases:
                phase_timings.extend(attempt_phases)
            else:
                phase_timings.append(
                    KnowledgePhaseTiming(
                        KnowledgeTimingPhase.BROKER,
                        executor_duration_ns,
                        service_attempt=service_attempt,
                    )
                )
            _checkpoint(cancellation_check)
            snapshot_started_ns = clock()
            after = self._collect_snapshot(cancellation_check)
            phase_timings.append(
                KnowledgePhaseTiming(
                    KnowledgeTimingPhase.SNAPSHOT_AFTER,
                    _duration_ns(clock, snapshot_started_ns),
                    service_attempt=service_attempt,
                    snapshot_id=after.snapshot_id,
                )
            )
            if _stable_identity(before, after):
                warnings = result.warnings
                if first_view_changed:
                    warnings = _deduplicate(
                        (*warnings, "snapshot_retry_succeeded")
                    )
                return replace(
                    result,
                    snapshot=before,
                    warnings=warnings,
                    telemetry=KnowledgeQueryTelemetry(
                        KnowledgeTelemetryOperation.SEARCH,
                        _duration_ns(clock, operation_started_ns),
                        tuple(phase_timings),
                        clock_signature=clock_contract.signature,
                    ),
                )

            if service_attempt == 1:
                first_view_changed = True
                _checkpoint(cancellation_check)
                continue

            changed_snapshot = _changed_snapshot_marker(before, after)
            warnings = _deduplicate(
                (
                    *result.warnings,
                    "snapshot_changed_during_query",
                    f"snapshot_before:{before.snapshot_id}",
                    f"snapshot_after:{after.snapshot_id}",
                )
            )
            return replace(
                result,
                snapshot=changed_snapshot,
                complete=False,
                warnings=warnings,
                telemetry=KnowledgeQueryTelemetry(
                    KnowledgeTelemetryOperation.SEARCH,
                    _duration_ns(clock, operation_started_ns),
                    tuple(phase_timings),
                    clock_signature=clock_contract.signature,
                ),
            )

        raise AssertionError("bounded Knowledge service loop did not return")

    def context(
        self,
        query: KnowledgeQuery,
        *,
        max_characters: int | None = None,
        max_hits: int | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> ContextBundle:
        """Search a stable view and compile a bounded context from its hits."""

        default_characters, max_character_limit, max_context_hits = (
            _context_limits()
        )
        resolved_characters = (
            default_characters if max_characters is None else max_characters
        )
        if isinstance(resolved_characters, bool) or not (
            1 <= resolved_characters <= max_character_limit
        ):
            raise ValueError(
                "max_characters must be between 1 and "
                f"{max_character_limit}"
            )
        if max_hits is not None and (
            isinstance(max_hits, bool) or not 1 <= max_hits <= max_context_hits
        ):
            raise ValueError(
                f"max_hits must be between 1 and {max_context_hits} when present"
            )
        clock_contract = self._clock_contract()
        clock = clock_contract.now_ns
        operation_started_ns = clock()
        result = self.search(query, cancellation_check=cancellation_check)
        _checkpoint(cancellation_check)
        builder = self.context_builder or _default_context_builder
        context_started_ns = clock()
        bundle = builder(
            result,
            max_characters=resolved_characters,
            max_hits=max_hits,
        )
        context_timing = KnowledgePhaseTiming(
            KnowledgeTimingPhase.CONTEXT_COMPILE,
            _duration_ns(clock, context_started_ns),
        )
        _checkpoint(cancellation_check)
        search_phases = (
            result.telemetry.phases
            if result.telemetry is not None
            and clock_contract.compatible_with(
                result.telemetry.clock_signature,
                trust_unidentified=True,
            )
            else ()
        )
        return replace(
            bundle,
            telemetry=KnowledgeQueryTelemetry(
                KnowledgeTelemetryOperation.CONTEXT,
                _duration_ns(clock, operation_started_ns),
                (*search_phases, context_timing),
                clock_signature=clock_contract.signature,
            ),
        )


# endregion [03]


__all__ = (
    "CancellationCheck",
    "ContextBuilder",
    "KnowledgeSearchService",
    "KnowledgeStateRootError",
    "QueryPlanner",
    "SearchExecutor",
    "SnapshotCollector",
)
