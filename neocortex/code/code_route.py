"""Incremental, non-destructive ingestion and structural analysis of source code.

The route never walks the filesystem independently.  It consumes immutable
``FileSnapshot`` records, binds every read to the observed physical identity,
and publishes one complete file version and its children in a single SQLite
transaction.  Optional language analyzers are lazy and always degrade to a
searchable textual representation.
"""

from __future__ import annotations
import os
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
)
from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from .ingestion.code_analyzers import AnalyzerRegistry, builtin_analyzer_registry
from .ingestion.code_candidate_scope import ProjectCandidateScope, is_project_marker
from .code_contracts import (
    AnalysisStatus,
    CodeAnalysis,
    CodeRouteConfig,
    CodeRouteSummary,
    DiagnosticSeverity,
)
from .ingestion.code_detection import (
    DETECTOR_VERSION,
    classify_artifact,
    likely_code_candidate,
)
from .code_schema import checkpoint_code_wal, remove_checkpointed_code_sidecars
from .code_state import CachedCodeVersion, CodeState
from .code_processing import (
    CodeCandidateResult,
    CodeCandidateTask,
    CodeContentProcessor,
    _diagnostic,
    _read_exact_snapshot,
    process_code_candidate,
    validate_code_snapshot,
)
from neocortex.semantic.semantic_models import fingerprint_bytes

# region [01] Structural collaborators and safe I/O


class CodeFrameworkState(Protocol):
    """Small shared-state surface used for resumable route phases."""

    def begin_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None: ...

    def complete_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        summary: Mapping[str, object] | None = None,
    ) -> None: ...

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None: ...


class CodeInventory(Protocol):
    """Read-only inventory projection consumed by the code route."""

    def snapshots(self, scan_id: int) -> Iterable[FileSnapshot]: ...


class CodeResourceGate(Protocol):
    """Minimal admission surface; keeps direct route execution independent."""

    def admit(self, estimated_bytes: int) -> AbstractContextManager[None]: ...


_MIB = 1024 * 1024
_CODE_ANALYSIS_FIXED_BYTES = 4 * _MIB
_CODE_ANALYSIS_RAW_BYTES_FACTOR = 2
_CODE_ANALYSIS_TEXT_BYTES_FACTOR = 12
_CODE_GRAPH_FIXED_BYTES = 8 * _MIB
_CODE_GRAPH_DATABASE_BYTES_CAP = 64 * _MIB
_PROJECT_SCOPE_SKIP_COUNTERS = {
    "outside_project": "outside_project_skips",
    "dependency": "dependency_skips",
    "generated": "generated_scope_skips",
    "cache": "cache_skips",
}


@dataclass(slots=True)
class _CodeRouteRun:
    """Mutable state shared by one analysis/graph orchestration."""

    counters: dict[str, int]
    elapsed_nanoseconds: dict[str, int]
    graph_inputs_changed: bool = False
    analysis_run_id: int | None = None
    current_phase: str = "analysis"

    @classmethod
    def create(cls) -> _CodeRouteRun:
        return cls(
            counters={
                field: 0
                for field in CodeRouteSummary.__dataclass_fields__
                if field not in {"processing_signature", "catalog_complete"}
            },
            elapsed_nanoseconds={
                "read": 0,
                "analyze": 0,
                "persist": 0,
                "cache_lookup": 0,
                "cache_update": 0,
                "cache_commit": 0,
            },
        )

    def require_analysis_run_id(self) -> int:
        if self.analysis_run_id is None:
            raise RuntimeError("code analysis run did not start")
        return self.analysis_run_id


def estimate_code_analysis_memory_bytes(observed_size: int, max_text_chars: int) -> int:
    """Estimate one in-memory decode, parser tree and persisted result.

    The route retains raw bytes, decoded text, source maps and structural
    records until atomic publication.  Twelve bytes per possible character is
    a conservative allowance for Unicode storage plus parser/result objects;
    two raw-byte equivalents cover the immutable payload and bounded working
    copies.  The estimate is per candidate, never for the entire route.
    """

    if observed_size < 0 or max_text_chars < 1:
        raise ValueError("code memory estimate requires non-negative size and text")
    text_chars_upper_bound = min(observed_size, max_text_chars)
    return (
        _CODE_ANALYSIS_FIXED_BYTES
        + observed_size * _CODE_ANALYSIS_RAW_BYTES_FACTOR
        + text_chars_upper_bound * _CODE_ANALYSIS_TEXT_BYTES_FACTOR
    )


def estimate_code_graph_memory_bytes(state_path: os.PathLike[str] | str) -> int:
    """Estimate bounded SQLite graph working memory from live database bytes."""

    database = os.fspath(state_path)
    observed_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            observed_bytes += os.stat(database + suffix).st_size
        except OSError:
            continue
    return _CODE_GRAPH_FIXED_BYTES + min(observed_bytes, _CODE_GRAPH_DATABASE_BYTES_CAP)


# endregion [01]


# region [02] Bounded route runtime


class CodeRoute(CodeContentProcessor):
    """Analyze code artifacts incrementally without mutating source files."""

    route_name = "code"

    def __init__(
        self,
        config: CodeRouteConfig,
        dedup_index: CodeInventory,
        framework_state: CodeFrameworkState,
        framework_run_id: int,
        scan_id: int,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
        analyzers: AnalyzerRegistry | None = None,
        memory_gate: CodeResourceGate | None = None,
    ):
        self.config = config
        self.dedup_index = dedup_index
        self.framework_state = framework_state
        self.framework_run_id = framework_run_id
        self.scan_id = scan_id
        self.progress = progress
        self.cancellation = cancellation or CancellationToken()
        self.analyzers = analyzers or builtin_analyzer_registry()
        self.memory_gate = memory_gate
        self.processing_signature = (
            f"{self.config.processing_signature}|"
            f"artifact-detector={DETECTOR_VERSION}|"
            f"{self.analyzers.processing_signature}"
        )
        self._selected_paths = frozenset(
            os.path.normcase(os.path.abspath(item)) for item in self.config.selection.paths
        )

    def _emit(
        self,
        completed: int,
        *,
        finished: bool = False,
        errors: int = 0,
        cache_hits: int = 0,
    ) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                operation="code",
                phase="analysis",
                description="Análisis incremental de código",
                completed=completed,
                total=self.config.max_documents,
                unit="archivos",
                finished=finished,
                metrics=(
                    ProgressMetric("cache_hits", cache_hits),
                    ProgressMetric("errors", errors),
                ),
            ),
        )

    def _selected_path(self, path: str) -> bool:
        if not self._selected_paths:
            return True
        normalized = os.path.normcase(os.path.abspath(path))
        return normalized in self._selected_paths

    def _discover_project_scope(self) -> ProjectCandidateScope | None:
        """Derive one immutable project boundary from the shared inventory."""

        if self.config.candidate_scope != "projects" or self._selected_paths:
            return None

        def inventory_paths() -> Iterable[str]:
            for snapshot in self.dedup_index.snapshots(self.scan_id):
                self.cancellation.checkpoint()
                yield snapshot.path

        return ProjectCandidateScope.discover(
            inventory_paths(),
            include_generated=self.config.include_generated,
            include_vendored=self.config.include_vendored,
            explicit_roots=self.config.explicit_project_roots,
        )

    def _candidate_admission(self, snapshot: FileSnapshot) -> AbstractContextManager[None]:
        if self.memory_gate is None:
            return nullcontext()
        estimated_bytes = estimate_code_analysis_memory_bytes(
            snapshot.size, self.config.max_text_chars
        )
        return self.memory_gate.admit(estimated_bytes)

    def _graph_admission(self) -> AbstractContextManager[None]:
        if self.memory_gate is None:
            return nullcontext()
        return self.memory_gate.admit(estimate_code_graph_memory_bytes(self.config.state_path))

    def _resolve_analyzer_identity(
        self,
        language: str | None,
        generic_only: bool,
    ) -> tuple[str, str]:
        """Resolve the analyzer selected by the current lazy registry."""

        analyzer = self.analyzers.analyzer_for(None if generic_only else language)
        return analyzer.analyzer_id, analyzer.analyzer_version

    def _record_cache_hit(
        self,
        cached: CachedCodeVersion,
        counters: dict[str, int],
    ) -> None:
        status = AnalysisStatus(cached.status)
        counters["cache_hits"] += 1
        counters["fts_rows_repaired"] += cached.fts_rows_repaired
        counters["generated"] += int(cached.generated)
        counters["vendored"] += int(cached.vendored)
        counters["symbols"] += cached.symbols
        counters["references"] += cached.references
        counters["diagnostics"] += cached.diagnostics
        counters["binary_skips"] += int(status is AnalysisStatus.BINARY)
        counters["skipped_limit"] += int(status is AnalysisStatus.SKIPPED_LIMIT)
        counters["text_only"] += int(status is AnalysisStatus.TEXT_ONLY)
        counters["partial"] += int(status is AnalysisStatus.PARTIAL)
        counters["errors"] += int(status is AnalysisStatus.ERROR)
        self._emit(
            counters["candidates"],
            errors=counters["errors"],
            cache_hits=counters["cache_hits"],
        )

    def _reuse_cached_candidate(
        self,
        state: CodeState,
        snapshot: FileSnapshot,
        run_counters: dict[str, int],
        elapsed_nanoseconds: dict[str, int],
        raw: bytes | None = None,
    ) -> bool:
        fingerprint = None if raw is None else fingerprint_bytes(raw)
        cached = state.reuse_cached(
            snapshot,
            self.processing_signature,
            self.framework_run_id,
            retry_errors=self.config.retry_errors,
            retry_recoverable_errors=self.config.retry_recoverable_errors,
            raw_xxh3_128=None if fingerprint is None else fingerprint.xxh3_128,
            raw_xxh3_64_guard=None if fingerprint is None else fingerprint.xxh3_64_guard,
            resolve_analyzer_identity=self._resolve_analyzer_identity,
            commit=False,
            elapsed_nanoseconds=elapsed_nanoseconds,
        )
        if cached is None:
            return False
        self._record_cache_hit(cached, run_counters)
        return True

    def _process_cached_or_oversized_candidate(
        self,
        state: CodeState,
        snapshot: FileSnapshot,
        counters: dict[str, int],
        elapsed_nanoseconds: dict[str, int],
    ) -> bool | None:
        """Resolve cached or oversized observations on the SQLite owner."""

        oversized = snapshot.size > self.config.max_file_bytes
        if (self.config.cache_validation == "metadata" or oversized) and (
            self._reuse_cached_candidate(state, snapshot, counters, elapsed_nanoseconds)
        ):
            return False
        if not oversized:
            return None
        observation = self._skipped_observation(
            snapshot,
            classify_artifact(snapshot.path, ""),
            AnalysisStatus.SKIPPED_LIMIT,
            _diagnostic(
                "file_limit",
                f"file size {snapshot.size} exceeds limit {self.config.max_file_bytes}",
                severity=DiagnosticSeverity.WARNING,
            ),
            provenance={
                "observed_size": snapshot.size,
                "max_file_bytes": self.config.max_file_bytes,
            },
        )
        return self._store_candidate_result(
            state, CodeCandidateResult(observation), counters, elapsed_nanoseconds
        )

    def _prepare_candidate_work(
        self,
        state: CodeState,
        snapshot: FileSnapshot,
        counters: dict[str, int],
        elapsed_nanoseconds: dict[str, int],
    ) -> CodeCandidateTask | CodeCandidateResult | None:
        """Verify full-cache content on the owner, inside its memory lease."""

        self.cancellation.checkpoint()
        preloaded_raw: bytes | None = None
        if self.config.cache_validation == "full":
            try:
                read_started = time.perf_counter_ns()
                preloaded_raw = _read_exact_snapshot(
                    snapshot, self.config.max_file_bytes, self.cancellation
                )
                elapsed_nanoseconds["read"] += time.perf_counter_ns() - read_started
                counters["bytes_read"] += len(preloaded_raw)
                self.cancellation.checkpoint()
                if self._reuse_cached_candidate(
                    state, snapshot, counters, elapsed_nanoseconds, preloaded_raw
                ):
                    return None
            except CancellationRequested:
                raise
            except (FileChangedError, OSError, UnicodeError, ValueError) as exc:
                return CodeCandidateResult(
                    self.error_observation(snapshot, exc),
                    stale_inventory=isinstance(exc, FileChangedError),
                )
        return CodeCandidateTask(
            snapshot,
            self.config,
            self.processing_signature,
            self.analyzers.specs,
            preloaded_raw,
        )

    def _store_candidate_result(
        self,
        state: CodeState,
        outcome: CodeCandidateResult,
        counters: dict[str, int],
        elapsed_nanoseconds: dict[str, int],
    ) -> bool:
        """Publish in the SQLite owner while the result still holds its lease."""

        self.cancellation.checkpoint()
        result = outcome.observation
        if result.status not in {AnalysisStatus.ERROR, AnalysisStatus.SKIPPED_LIMIT}:
            snapshot = result.input.snapshot if isinstance(result, CodeAnalysis) else result.snapshot
            try:
                validate_code_snapshot(snapshot)
            except FileChangedError as exc:
                result = self.error_observation(snapshot, exc)
                outcome = replace(outcome, observation=result, stale_inventory=True, text_chars=0)
        counters["bytes_read"] += outcome.bytes_read
        counters["text_chars"] += outcome.text_chars
        counters["stale_inventory"] += int(outcome.stale_inventory)
        elapsed_nanoseconds["read"] += outcome.read_ns
        elapsed_nanoseconds["analyze"] += outcome.analyze_ns
        persist_started = time.perf_counter_ns()
        if isinstance(result, CodeAnalysis):
            _, replaced_version = state.store_analysis(result, self.framework_run_id)
            counters["symbols"] += len(result.symbols)
            counters["references"] += len(result.references)
            counters["diagnostics"] += len(result.diagnostics)
            counters["text_only"] += int(result.status is AnalysisStatus.TEXT_ONLY)
            counters["partial"] += int(result.status is AnalysisStatus.PARTIAL)
            counters["generated"] += int(result.input.classification.generated)
            counters["vendored"] += int(result.input.classification.vendored)
        else:
            _, replaced_version = state.store_skipped(result, self.framework_run_id)
            counters["diagnostics"] += 1
            counters["binary_skips"] += int(result.status is AnalysisStatus.BINARY)
            counters["skipped_limit"] += int(result.status is AnalysisStatus.SKIPPED_LIMIT)
            counters["text_only"] += int(result.status is AnalysisStatus.TEXT_ONLY)
            counters["generated"] += int(result.classification.generated)
            counters["vendored"] += int(result.classification.vendored)
        elapsed_nanoseconds["persist"] += time.perf_counter_ns() - persist_started
        counters["invalidated_versions"] += int(replaced_version)
        counters["processed"] += 1
        counters["errors"] += int(result.status is AnalysisStatus.ERROR)
        self._emit(
            counters["candidates"],
            errors=counters["errors"],
            cache_hits=counters["cache_hits"],
        )
        return True

    def _process_candidate(
        self,
        state: CodeState,
        snapshot: FileSnapshot,
        counters: dict[str, int],
        elapsed_nanoseconds: dict[str, int],
    ) -> bool:
        """Compatibility path for direct calls and gates without elastic capacity."""

        quick_result = self._process_cached_or_oversized_candidate(
            state, snapshot, counters, elapsed_nanoseconds
        )
        if quick_result is not None:
            return quick_result
        with self._candidate_admission(snapshot):
            prepared = self._prepare_candidate_work(
                state, snapshot, counters, elapsed_nanoseconds
            )
            if prepared is None:
                return False
            outcome = (
                self.process_candidate(
                    snapshot,
                    prepared.preloaded_raw,
                    cancellation=self.cancellation,
                    reader=_read_exact_snapshot,
                )
                if isinstance(prepared, CodeCandidateTask)
                else prepared
            )
            return self._store_candidate_result(state, outcome, counters, elapsed_nanoseconds)

    def _candidate_selected(
        self,
        state: CodeState,
        snapshot: FileSnapshot,
        project_scope: ProjectCandidateScope | None,
        counters: dict[str, int],
    ) -> bool:
        if (
            not likely_code_candidate(snapshot.path) and not is_project_marker(snapshot.path)
        ) or not self._selected_path(snapshot.path):
            return False
        if project_scope is not None:
            decision = project_scope.decision(snapshot.path)
            if decision != "admit":
                counters[_PROJECT_SCOPE_SKIP_COUNTERS[decision]] += 1
                return False
        return state.matches_selection(snapshot, self.config.selection)

    def _analyze_parallel_inventory(
        self,
        state: CodeState,
        run: _CodeRouteRun,
        candidates: Iterable[FileSnapshot],
        finish_observation: Callable[[bool], None],
    ) -> None:
        """Keep cache/SQLite in the owner and parse in elastic spawn workers."""

        from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map

        def prepare(snapshot: FileSnapshot):
            # A metadata hit may decompress durable text and rebuild FTS.
            # Do that owner work only after CPU/RAM/I/O have been admitted.
            quick_result = self._process_cached_or_oversized_candidate(
                state, snapshot, run.counters, run.elapsed_nanoseconds
            )
            if quick_result is not None:
                return ImmediateResult(quick_result)
            prepared = self._prepare_candidate_work(
                state, snapshot, run.counters, run.elapsed_nanoseconds
            )
            if isinstance(prepared, CodeCandidateTask):
                return prepared
            return ImmediateResult(prepared)

        def estimated_bytes(snapshot: FileSnapshot) -> int:
            if snapshot.size > self.config.max_file_bytes:
                return _CODE_ANALYSIS_FIXED_BYTES
            # Account for the bounded pipe serialization and simultaneous child
            # and owner copies until SQLite publication consumes the result.
            return 2 * estimate_code_analysis_memory_bytes(
                snapshot.size, self.config.max_text_chars
            )

        worker: Callable[[CodeCandidateTask], CodeCandidateResult | bool | None] = process_code_candidate
        with elastic_map(
            worker,
            candidates,
            gate=self.memory_gate,
            estimated_bytes=estimated_bytes,
            native_threads=1,
            io_slots=1,
            io_device=lambda snapshot: str(snapshot.volume_id),
            phase="analysis",
            prepare=prepare,
            executor_kind="process",
            cancellation=self.cancellation,
        ) as results:
            for outcome in results:
                self.cancellation.checkpoint()
                changed = outcome if isinstance(outcome, bool) else (
                    outcome is not None and self._store_candidate_result(
                        state, outcome, run.counters, run.elapsed_nanoseconds
                    )
                )
                finish_observation(changed)

    def _analyze_inventory(self, state: CodeState, run: _CodeRouteRun) -> None:
        project_scope = self._discover_project_scope()
        if project_scope is not None:
            run.counters["project_scope_enabled"] = 1
            run.counters["project_roots"] = project_scope.root_count
        pending_cache_updates = 0
        observed_cache_hits = 0

        def commit_cache_batch() -> None:
            nonlocal pending_cache_updates
            if not state.connection.in_transaction:
                pending_cache_updates = 0
                return
            started = time.perf_counter_ns()
            state.connection.commit()
            run.elapsed_nanoseconds["cache_commit"] += time.perf_counter_ns() - started
            run.counters["cache_batches"] += 1
            pending_cache_updates = 0

        def finish_observation(changed: bool) -> None:
            nonlocal pending_cache_updates, observed_cache_hits
            run.graph_inputs_changed = run.graph_inputs_changed or changed
            new_cache_hits = run.counters["cache_hits"] - observed_cache_hits
            observed_cache_hits = run.counters["cache_hits"]
            if not state.connection.in_transaction:
                pending_cache_updates = 0
            else:
                pending_cache_updates += new_cache_hits
                if pending_cache_updates >= 128:
                    commit_cache_batch()

        def candidates() -> Iterable[FileSnapshot]:
            for snapshot in self.dedup_index.snapshots(self.scan_id):
                self.cancellation.checkpoint()
                if not self._candidate_selected(state, snapshot, project_scope, run.counters):
                    continue
                if (
                    self.config.max_documents is not None
                    and run.counters["candidates"] >= self.config.max_documents
                ):
                    break
                run.counters["candidates"] += 1
                yield snapshot

        try:
            if callable(getattr(self.memory_gate, "worker_capacity", None)):
                self._analyze_parallel_inventory(state, run, candidates(), finish_observation)
            else:
                for snapshot in candidates():
                    finish_observation(
                        self._process_candidate(
                            state, snapshot, run.counters, run.elapsed_nanoseconds
                        )
                    )
        except BaseException:
            if state.connection.in_transaction:
                state.connection.rollback()
            raise
        else:
            commit_cache_batch()

    def _run_analysis_phase(self, state: CodeState, run: _CodeRouteRun) -> None:
        run.analysis_run_id = state.begin_run(
            self.framework_run_id,
            self.scan_id,
            self.processing_signature,
        )
        self._emit(0)
        self._analyze_inventory(state, run)
        for operation, elapsed in run.elapsed_nanoseconds.items():
            run.counters[f"{operation}_milliseconds"] = elapsed // 1_000_000
        self.framework_state.complete_route_phase(
            self.framework_run_id,
            self.route_name,
            run.current_phase,
            {"processing_signature": self.processing_signature, **run.counters},
        )

    def _advance_to_graph_phase(self, run: _CodeRouteRun) -> None:
        run.current_phase = "graph"
        self.framework_state.begin_route_phase(
            self.framework_run_id,
            self.route_name,
            run.current_phase,
        )

    def _graph_reuse_count(
        self,
        state: CodeState,
        run: _CodeRouteRun,
        full_reconciliation: bool,
    ) -> int | None:
        if not (
            full_reconciliation
            and not run.graph_inputs_changed
            and run.counters["processed"] == 0
            and run.counters["cache_hits"] == run.counters["candidates"]
            and run.counters["invalidated_versions"] == 0
        ):
            return None
        return state.reusable_graph_project_count(
            run.require_analysis_run_id(),
            self.processing_signature,
        )

    def _run_graph_phase(
        self,
        state: CodeState,
        run: _CodeRouteRun,
    ) -> CodeRouteSummary:
        graph_started = time.perf_counter_ns()
        full_reconciliation = self.config.max_documents is None and not self.config.selection.active
        self.cancellation.checkpoint()
        if full_reconciliation:
            missing_versions = state.mark_missing(self.framework_run_id)
            run.counters["invalidated_versions"] += missing_versions
            run.graph_inputs_changed = run.graph_inputs_changed or missing_versions > 0
            self.cancellation.checkpoint()
        reusable_projects = self._graph_reuse_count(
            state,
            run,
            full_reconciliation,
        )
        if reusable_projects is None:
            with self._graph_admission():
                self.cancellation.checkpoint()
                run.counters["projects"] = state.finalize_graph(
                    self.framework_run_id,
                    cancellation_check=self.cancellation.checkpoint,
                )
                self.cancellation.checkpoint()
        else:
            run.counters["projects"] = reusable_projects
            self.cancellation.checkpoint()
        run.counters["graph_milliseconds"] = (time.perf_counter_ns() - graph_started) // 1_000_000
        self.cancellation.checkpoint()
        summary = CodeRouteSummary(
            processing_signature=self.processing_signature,
            catalog_complete=None,
            **run.counters,
        )
        payload = asdict(summary)
        with self._graph_admission() if reusable_projects is None else nullcontext():
            state.complete_run(
                run.require_analysis_run_id(),
                payload,
                partial=(self.config.max_documents is not None or self.config.selection.active),
                graph_current=True,
                cancellation_check=self.cancellation.checkpoint,
            )
        summary = replace(
            summary,
            publication_milliseconds=state.last_graph_publication_milliseconds,
            graph_generation_reused=int(state.last_graph_publication_reused),
        )
        payload = asdict(summary)
        self.framework_state.complete_route_phase(
            self.framework_run_id,
            self.route_name,
            run.current_phase,
            payload,
        )
        self._emit(
            run.counters["candidates"],
            finished=True,
            errors=run.counters["errors"],
            cache_hits=run.counters["cache_hits"],
        )
        checkpoint_code_wal(state.connection)
        return summary

    def _persist_run_failure(self, run: _CodeRouteRun, exc: BaseException) -> None:
        if run.analysis_run_id is None:
            return
        try:
            with CodeState(
                self.config.state_path,
                retention_policy=self.config.retention_policy,
            ) as state:
                state.fail_run(run.analysis_run_id, exc)
        except Exception as cleanup_exc:
            exc.add_note(
                "code run failure could not be persisted: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            )

    def run(self) -> CodeRouteSummary:
        """Run analysis and graph publication with durable phase boundaries."""

        run = _CodeRouteRun.create()
        self.framework_state.begin_route_phase(
            self.framework_run_id,
            self.route_name,
            run.current_phase,
        )
        try:
            with CodeState(
                self.config.state_path,
                retention_policy=self.config.retention_policy,
            ) as state:
                self._run_analysis_phase(state, run)
                self._advance_to_graph_phase(run)
                summary = self._run_graph_phase(state, run)
            remove_checkpointed_code_sidecars(
                self.config.state_path,
                require_removal=False,
            )
            return summary
        except BaseException as exc:
            self._persist_run_failure(run, exc)
            self.framework_state.fail_route_phase(
                self.framework_run_id,
                self.route_name,
                run.current_phase,
                exc,
            )
            raise


# endregion [02]


__all__ = [
    "CodeRoute",
    "CodeRouteConfig",
    "CodeRouteSummary",
    "estimate_code_analysis_memory_bytes",
    "estimate_code_graph_memory_bytes",
]
