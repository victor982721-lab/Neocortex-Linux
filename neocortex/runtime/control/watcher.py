"""Foreground incremental watcher driven by durable inventory checkpoints.

USN batches are deliberately treated only as optional Windows wake-up signals.
When no durable USN cursor exists, the watcher schedules a portable integrated
inventory pass instead of inventing another index or cursor.  Every cycle
delegates reconciliation to a fresh ``FrameworkOrchestrator`` run and reloads
the checkpoint that the integrated run committed.
"""

from __future__ import annotations

import os
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TYPE_CHECKING, Literal, Protocol, runtime_checkable

from neocortex.enumeration.errors import JournalDiscontinuityError, NtfsUsnError
from neocortex.enumeration.models import JournalCursor
from neocortex.deduplication import DedupIndex, InventoryCheckpoint, InventoryError
from neocortex.progress import ProgressCallback

from neocortex.safety.corpus_access import CorpusAccessPolicy
from neocortex.persistence.framework_state_writer import (
    DurableInventoryOwner,
    read_latest_durable_inventory_owner,
)
from neocortex.integrations.inventory.inventory_boundary import (
    NormalInventoryBoundary,
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.control.watcher_life_lease import (
    WatcherLifeLease,
    WatcherLifeLeaseConflict,
)

if TYPE_CHECKING:
    from neocortex.enumeration.models import UsnChangeBatch


def UsnJournalReader(volume: str, cursor: JournalCursor, **kwargs: Any):
    """Lazy compatibility seam for the optional NTFS/USN watcher source."""

    from neocortex.enumeration.ntfs.journal import UsnJournalReader as reader

    return reader(volume, cursor, **kwargs)
# region [01] Public configuration and observability contracts

BootstrapMode = Literal["if-needed", "always", "never"]
WatcherRunReason = Literal[
    "bootstrap",
    "changes",
    "journal-discontinuity",
    "portable-poll",
    "portable-fallback",
]


class WatcherAlreadyRunningError(RuntimeError):
    """The same watcher instance already owns a foreground loop."""


class WatcherCheckpointError(RuntimeError):
    """No valid durable inventory checkpoint is available for observation."""


@dataclass(frozen=True, slots=True)
class IncrementalWatcherConfig:
    """Bounded timing policy for one foreground watcher."""

    bootstrap: BootstrapMode = "if-needed"
    poll_timeout_seconds: int = 1
    debounce_seconds: float = 2.0
    max_debounce_seconds: float = 30.0
    error_backoff_initial_seconds: float = 1.0
    error_backoff_max_seconds: float = 60.0
    error_backoff_multiplier: float = 2.0
    portable_interval_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.bootstrap not in {"if-needed", "always", "never"}:
            raise ValueError("bootstrap must be 'if-needed', 'always', or 'never'")
        if not isinstance(self.poll_timeout_seconds, int) or not (
            0 <= self.poll_timeout_seconds <= 300
        ):
            raise ValueError("poll_timeout_seconds must be an integer from 0 to 300")
        if self.poll_timeout_seconds == 0:
            raise ValueError("poll_timeout_seconds must be positive for a watcher")
        if self.debounce_seconds < 0:
            raise ValueError("debounce_seconds cannot be negative")
        if self.max_debounce_seconds <= 0:
            raise ValueError("max_debounce_seconds must be positive")
        if self.max_debounce_seconds < self.debounce_seconds:
            raise ValueError("max_debounce_seconds cannot be shorter than debounce_seconds")
        if self.error_backoff_initial_seconds < 0:
            raise ValueError("error_backoff_initial_seconds cannot be negative")
        if self.error_backoff_max_seconds < self.error_backoff_initial_seconds:
            raise ValueError("error_backoff_max_seconds cannot be shorter than the initial backoff")
        if self.error_backoff_multiplier < 1:
            raise ValueError("error_backoff_multiplier must be at least 1")
        if not math.isfinite(self.portable_interval_seconds) or self.portable_interval_seconds <= 0:
            raise ValueError("portable_interval_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class WatcherEvent:
    sequence: int
    timestamp_ns: int
    kind: str
    message: str
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class WatcherRunSummary:
    reason: WatcherRunReason
    succeeded: bool
    started_ns: int
    elapsed_ns: int
    checkpoint_before: JournalCursor | None
    run_id: int | None = None
    inventory_mode: str | None = None
    error_type: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class WatcherSummary:
    started_ns: int
    finished_ns: int
    cancelled: bool
    bootstrap_runs: int
    change_runs: int
    discontinuity_runs: int
    portable_runs: int
    successful_runs: int
    failed_runs: int
    signal_batches: int
    signal_records: int
    idle_polls: int
    source_restarts: int
    source_errors: int
    backoff_waits: int
    checkpoint_loads: int
    last_run: WatcherRunSummary | None


WatcherEventCallback = Callable[[WatcherEvent], None]
WatcherRunCallback = Callable[[WatcherRunSummary], None]


# endregion [01]


# region [02] Injectable boundaries and default adapters


@runtime_checkable
class JournalSignalSource(Protocol):
    """One non-durable, bounded source of filesystem change signals."""

    def poll(self) -> UsnChangeBatch | None: ...

    def close(self) -> None: ...


@runtime_checkable
class WatchRun(Protocol):
    """One integrated reconciliation run with cooperative cancellation."""

    def run_once(self) -> object: ...

    def request_cancellation(self) -> None: ...


CheckpointLoader = Callable[[Path], InventoryCheckpoint | None]
DurableOwnerLoader = Callable[[Path], DurableInventoryOwner | None]
JournalSourceFactory = Callable[[str, JournalCursor, int], JournalSignalSource]
WatchRunFactory = Callable[[], WatchRun]


class _OrchestratorWatchRun:
    def __init__(
        self,
        config: FrameworkConfig,
        progress: ProgressCallback | None,
    ):
        self._orchestrator = FrameworkOrchestrator(config, progress=progress)

    def run_once(self) -> object:
        return self._orchestrator.run_initial()

    def request_cancellation(self) -> None:
        self._orchestrator.request_cancellation()


def _journal_source_factory(
    volume: str,
    cursor: JournalCursor,
    timeout_seconds: int,
) -> JournalSignalSource:
    return UsnJournalReader(
        volume,
        cursor,
        timeout_seconds=timeout_seconds,
        bytes_to_wait_for=1,
    )


# endregion [02]


# region [03] Internal counters


@dataclass(slots=True)
class _WatcherCounters:
    bootstrap_runs: int = 0
    change_runs: int = 0
    discontinuity_runs: int = 0
    portable_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    signal_batches: int = 0
    signal_records: int = 0
    idle_polls: int = 0
    source_restarts: int = 0
    source_errors: int = 0
    backoff_waits: int = 0
    checkpoint_loads: int = 0
    last_run: WatcherRunSummary | None = None


# endregion [03]


# region [04] Foreground watcher


class IncrementalWatcher:
    """Observe filesystem activity and serialize non-destructive integrated runs.

    ``run_foreground`` owns the calling thread until ``request_cancellation`` is
    invoked.  No service, subprocess, persistent worker, or independent cursor
    is created by this class.
    """

    def __init__(
        self,
        framework_config: FrameworkConfig,
        config: IncrementalWatcherConfig | None = None,
        *,
        event_callback: WatcherEventCallback | None = None,
        run_callback: WatcherRunCallback | None = None,
        progress: ProgressCallback | None = None,
        checkpoint_loader: CheckpointLoader | None = None,
        durable_owner_loader: DurableOwnerLoader | None = None,
        journal_source_factory: JournalSourceFactory | None = None,
        run_factory: WatchRunFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        time_ns: Callable[[], int] = time.time_ns,
        waiter: Callable[[float], None] | None = None,
    ):
        if framework_config.apply_actions:
            raise ValueError("incremental watcher does not allow apply_actions")
        if framework_config.route_only or framework_config.resume_run_id is not None:
            raise ValueError("incremental watcher requires integrated initial runs")
        if framework_config.candidate_run_id is not None:
            raise ValueError("incremental watcher cannot use a retained candidate run")

        self.config = config or IncrementalWatcherConfig()
        requested_root = Path(os.path.abspath(os.fspath(framework_config.root.expanduser())))
        access_policy = CorpusAccessPolicy.capture("normal", requested_root)
        state_layout = initialize_authorized_state_directory(
            access_policy,
            framework_config.state_directory,
            require_disjoint=False,
        )
        self._boundary: NormalInventoryBoundary = build_normal_inventory_boundary(
            requested_root,
            state_layout.path,
            access_policy=access_policy,
            state_policy=state_layout.state_policy,
            internal_paths_policy=state_layout.internal_paths_policy,
        )
        self.root = self._boundary.access_policy.root
        self.framework_config = replace(
            framework_config,
            root=self.root,
            state_directory=state_layout.path,
        )
        self._event_callback = event_callback
        self._run_callback = run_callback
        self._monotonic = monotonic
        self._time_ns = time_ns
        self._waiter = waiter
        self._journal_source_factory = journal_source_factory or _journal_source_factory
        self._checkpoint_loader: CheckpointLoader
        self._durable_owner_loader: DurableOwnerLoader

        if checkpoint_loader is None:
            database = self.framework_config.dedup_database

            def load_checkpoint(root: Path) -> InventoryCheckpoint | None:
                with DedupIndex(database) as index:
                    checkpoint = index.inventory_checkpoint(root)
                    if checkpoint is None:
                        return None
                    try:
                        index.require_scan_inventory_policy_signature(
                            checkpoint.scan_id,
                            self._boundary.exclusion_policy.signature,
                        )
                    except InventoryError:
                        return None
                    return checkpoint

            self._checkpoint_loader = load_checkpoint
        else:
            self._checkpoint_loader = checkpoint_loader

        if durable_owner_loader is None:
            framework_database = self.framework_config.framework_database

            def load_owner(root: Path) -> DurableInventoryOwner | None:
                return read_latest_durable_inventory_owner(framework_database, root)

            self._durable_owner_loader = load_owner
        else:
            self._durable_owner_loader = durable_owner_loader

        if run_factory is None:

            def build_run() -> WatchRun:
                return _OrchestratorWatchRun(self.framework_config, progress)

            self._run_factory = build_run
        else:
            self._run_factory = run_factory

        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._active_run_lock = threading.Lock()
        self._active_run: WatchRun | None = None
        self._event_sequence = 0

    def request_cancellation(self) -> None:
        """Stop at the next safe boundary and cancel an active integrated run."""

        self._stop_event.set()
        with self._active_run_lock:
            active_run = self._active_run
        if active_run is not None:
            active_run.request_cancellation()

    def _emit(self, kind: str, message: str, **details: object) -> None:
        callback = self._event_callback
        if callback is None:
            return
        self._event_sequence += 1
        callback(
            WatcherEvent(
                sequence=self._event_sequence,
                timestamp_ns=self._time_ns(),
                kind=kind,
                message=message,
                details=dict(details),
            )
        )

    def _load_checkpoint(
        self,
        counters: _WatcherCounters,
    ) -> InventoryCheckpoint | None:
        counters.checkpoint_loads += 1
        checkpoint = self._checkpoint_loader(self.root)
        if checkpoint is None or not checkpoint.valid:
            return None
        self._boundary.verify()
        owner = self._durable_owner_loader(self.root)
        if owner is None:
            self._emit(
                "checkpoint-incompatible",
                "Checkpoint sin propietario durable compatible",
                reason="missing_completed_owner",
            )
            return None
        binding = owner.binding
        expected_identity = (
            self._boundary.access_policy.root_device_id,
            self._boundary.access_policy.root_file_id,
            self._boundary.access_policy.root_birthtime_ns,
        )
        observed_identity = (
            owner.access_policy.root_device_id,
            owner.access_policy.root_file_id,
            owner.access_policy.root_birthtime_ns,
        )
        durable_cursor = binding.end_cursor
        cursor_values = (
            checkpoint.volume,
            checkpoint.journal_id,
            checkpoint.next_usn,
        )
        checkpoint_cursor_complete = all(value is not None for value in cursor_values)
        checkpoint_cursor_absent = all(value is None for value in cursor_values)
        cursor_binding_matches = (durable_cursor is None and checkpoint_cursor_absent) or (
            durable_cursor is not None
            and checkpoint_cursor_complete
            and checkpoint.volume == durable_cursor.volume
            and checkpoint.journal_id == durable_cursor.journal_id
            and checkpoint.next_usn == durable_cursor.next_usn
        )
        if (
            owner.access_policy.mode != "normal"
            or os.path.normcase(os.fspath(owner.access_policy.root))
            != os.path.normcase(os.fspath(self.root))
            or observed_identity != expected_identity
            or binding.corpus_access_mode != "normal"
            or binding.inventory_policy_signature != self._boundary.effective_signature
            or binding.scan_id != checkpoint.scan_id
            or checkpoint.inventory_policy_signature != self._boundary.exclusion_policy.signature
            or not cursor_binding_matches
        ):
            self._emit(
                "checkpoint-incompatible",
                "Checkpoint fuera de la frontera durable vigente",
                reason="policy_or_owner_mismatch",
            )
            return None
        owner.access_policy.verify_root_identity()
        self._boundary.verify()
        return checkpoint

    @staticmethod
    def _cursor(checkpoint: InventoryCheckpoint) -> JournalCursor:
        if (
            checkpoint.volume is None
            or checkpoint.journal_id is None
            or checkpoint.next_usn is None
        ):
            raise ValueError("portable inventory publication has no USN cursor")
        return JournalCursor(
            checkpoint.volume,
            checkpoint.journal_id,
            checkpoint.next_usn,
        )

    def _wait_for_backoff(
        self,
        delay: float,
        counters: _WatcherCounters,
    ) -> None:
        counters.backoff_waits += 1
        self._emit("backoff", "Esperando antes de reintentar", seconds=delay)
        if delay <= 0 or self._stop_event.is_set():
            return
        if self._waiter is not None:
            self._waiter(delay)
            return
        self._stop_event.wait(delay)

    def _execute_run(
        self,
        reason: WatcherRunReason,
        checkpoint_before: JournalCursor | None,
        counters: _WatcherCounters,
    ) -> bool:
        if self._stop_event.is_set():
            return False

        if reason == "bootstrap":
            counters.bootstrap_runs += 1
        elif reason == "changes":
            counters.change_runs += 1
        elif reason == "journal-discontinuity":
            counters.discontinuity_runs += 1
        else:
            counters.portable_runs += 1

        started_ns = self._time_ns()
        started = self._monotonic()
        self._emit(
            "run-started",
            "Ejecución integrada iniciada",
            reason=reason,
            checkpoint_usn=(None if checkpoint_before is None else checkpoint_before.next_usn),
        )

        run: WatchRun | None = None
        try:
            run = self._run_factory()
            with self._active_run_lock:
                if self._stop_event.is_set():
                    return False
                self._active_run = run
            result = run.run_once()
        except KeyboardInterrupt:
            self.request_cancellation()
            summary = WatcherRunSummary(
                reason=reason,
                succeeded=False,
                started_ns=started_ns,
                elapsed_ns=max(0, int((self._monotonic() - started) * 1_000_000_000)),
                checkpoint_before=checkpoint_before,
                error_type="KeyboardInterrupt",
                error_detail="foreground watcher cancellation requested",
            )
            counters.last_run = summary
            if self._run_callback is not None:
                self._run_callback(summary)
            return False
        except Exception as exc:
            summary = WatcherRunSummary(
                reason=reason,
                succeeded=False,
                started_ns=started_ns,
                elapsed_ns=max(0, int((self._monotonic() - started) * 1_000_000_000)),
                checkpoint_before=checkpoint_before,
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            counters.last_run = summary
            if self._stop_event.is_set():
                self._emit(
                    "run-cancelled",
                    "Ejecución integrada cancelada",
                    reason=reason,
                    error_type=summary.error_type,
                )
            else:
                counters.failed_runs += 1
                self._emit(
                    "run-failed",
                    "Ejecución integrada fallida",
                    reason=reason,
                    error_type=summary.error_type,
                    detail=summary.error_detail,
                )
            if self._run_callback is not None:
                self._run_callback(summary)
            return False
        finally:
            if run is not None:
                with self._active_run_lock:
                    if self._active_run is run:
                        self._active_run = None

        summary = WatcherRunSummary(
            reason=reason,
            succeeded=True,
            started_ns=started_ns,
            elapsed_ns=max(0, int((self._monotonic() - started) * 1_000_000_000)),
            checkpoint_before=checkpoint_before,
            run_id=getattr(result, "run_id", None),
            inventory_mode=getattr(result, "inventory_mode", None),
        )
        counters.successful_runs += 1
        counters.last_run = summary
        self._emit(
            "run-completed",
            "Ejecución integrada completada",
            reason=reason,
            run_id=summary.run_id,
            inventory_mode=summary.inventory_mode,
            elapsed_ns=summary.elapsed_ns,
        )
        if self._run_callback is not None:
            self._run_callback(summary)
        return True

    def _close_source(self, source: JournalSignalSource | None) -> None:
        if source is None:
            return
        try:
            source.close()
        except Exception as exc:
            self._emit(
                "source-close-failed",
                "No se pudo cerrar limpiamente el lector USN",
                error_type=type(exc).__name__,
                detail=str(exc),
            )

    def _summary(
        self,
        started_ns: int,
        counters: _WatcherCounters,
    ) -> WatcherSummary:
        return WatcherSummary(
            started_ns=started_ns,
            finished_ns=self._time_ns(),
            cancelled=self._stop_event.is_set(),
            bootstrap_runs=counters.bootstrap_runs,
            change_runs=counters.change_runs,
            discontinuity_runs=counters.discontinuity_runs,
            portable_runs=counters.portable_runs,
            successful_runs=counters.successful_runs,
            failed_runs=counters.failed_runs,
            signal_batches=counters.signal_batches,
            signal_records=counters.signal_records,
            idle_polls=counters.idle_polls,
            source_restarts=counters.source_restarts,
            source_errors=counters.source_errors,
            backoff_waits=counters.backoff_waits,
            checkpoint_loads=counters.checkpoint_loads,
            last_run=counters.last_run,
        )

    def _next_backoff(self, delay: float) -> float:
        return min(
            self.config.error_backoff_max_seconds,
            delay * self.config.error_backoff_multiplier,
        )

    def _recover_observation_error(
        self,
        *,
        event_kind: str,
        message: str,
        error: Exception,
        backoff: float,
        counters: _WatcherCounters,
    ) -> float:
        counters.source_errors += 1
        self._emit(
            event_kind,
            message,
            error_type=type(error).__name__,
            detail=str(error),
        )
        self._wait_for_backoff(backoff, counters)
        return self._next_backoff(backoff)

    def _run_with_recovery(
        self,
        reason: WatcherRunReason,
        cursor: JournalCursor | None,
        backoff: float,
        counters: _WatcherCounters,
    ) -> tuple[float, bool]:
        succeeded = self._execute_run(reason, cursor, counters)
        if self._stop_event.is_set():
            return backoff, False
        if succeeded:
            return self.config.error_backoff_initial_seconds, True
        self._wait_for_backoff(backoff, counters)
        return self._next_backoff(backoff), False

    def _poll_until_debounced(
        self,
        source: JournalSignalSource,
        counters: _WatcherCounters,
        successful_poll: list[bool],
    ) -> bool:
        """Return true once a quiet or maximum signal window must be reconciled."""

        first_signal_at: float | None = None
        last_signal_at: float | None = None
        while not self._stop_event.is_set():
            batch = source.poll()
            successful_poll[0] = True
            now = self._monotonic()
            if batch is None:
                counters.idle_polls += 1
            else:
                counters.signal_batches += 1
                counters.signal_records += len(batch.records)
                if first_signal_at is None:
                    first_signal_at = now
                last_signal_at = now
                self._emit(
                    "change-signal",
                    "Actividad USN detectada",
                    records=len(batch.records),
                    cursor_before=batch.cursor_before.next_usn,
                    cursor_after=batch.cursor_after.next_usn,
                )

            if first_signal_at is None or last_signal_at is None:
                continue
            quiet_for = now - last_signal_at
            pending_for = now - first_signal_at
            if (
                quiet_for >= self.config.debounce_seconds
                or pending_for >= self.config.max_debounce_seconds
            ):
                return True
        return False

    def _observe_checkpoint(
        self,
        checkpoint: InventoryCheckpoint,
        backoff: float,
        counters: _WatcherCounters,
    ) -> float:
        """Observe from one checkpoint until a run, error, or cancellation."""

        cursor = self._cursor(checkpoint)
        source: JournalSignalSource | None = None
        successful_poll = [False]
        try:
            source = self._journal_source_factory(
                cursor.volume,
                cursor,
                self.config.poll_timeout_seconds,
            )
            counters.source_restarts += 1
            self._emit(
                "source-started",
                "Lector USN iniciado desde checkpoint durable",
                journal_id=cursor.journal_id,
                next_usn=cursor.next_usn,
            )
            if not self._poll_until_debounced(source, counters, successful_poll):
                return backoff

            self._close_source(source)
            source = None
            return self._run_with_recovery(
                "changes",
                cursor,
                self.config.error_backoff_initial_seconds,
                counters,
            )[0]
        except JournalDiscontinuityError as exc:
            self._close_source(source)
            source = None
            self._emit(
                "journal-discontinuity",
                "Discontinuidad USN; se requiere inventario integrado",
                detail=str(exc),
                checkpoint_usn=cursor.next_usn,
            )
            return self._run_with_recovery(
                "journal-discontinuity",
                cursor,
                backoff,
                counters,
            )[0]
        except (NtfsUsnError, OSError) as exc:
            self._close_source(source)
            source = None
            counters.source_errors += 1
            self._emit(
                "source-portable-fallback",
                "USN no disponible; se ejecutará inventario portable",
                error_type=type(exc).__name__,
                detail=str(exc),
                checkpoint_usn=cursor.next_usn,
            )
            return self._run_with_recovery(
                "portable-fallback",
                cursor,
                backoff,
                counters,
            )[0]
        except KeyboardInterrupt:
            self.request_cancellation()
            return backoff
        except Exception as exc:
            if successful_poll[0]:
                backoff = self.config.error_backoff_initial_seconds
            return self._recover_observation_error(
                event_kind="source-error",
                message="Falló la observación USN",
                error=exc,
                backoff=backoff,
                counters=counters,
            )
        finally:
            self._close_source(source)

    def _observe_portable_checkpoint(
        self,
        backoff: float,
        counters: _WatcherCounters,
    ) -> float:
        """Schedule one normal inventory pass without fabricating change signals."""

        interval = self.config.portable_interval_seconds
        self._emit(
            "portable-source-started",
            "Inventario portable programado desde checkpoint durable",
            interval_seconds=interval,
        )
        if self._waiter is not None:
            self._waiter(interval)
        elif self._stop_event.wait(interval):
            return backoff
        if self._stop_event.is_set():
            return backoff
        counters.idle_polls += 1
        return self._run_with_recovery(
            "portable-poll",
            None,
            self.config.error_backoff_initial_seconds,
            counters,
        )[0]

    def _watch_loop(self, counters: _WatcherCounters) -> None:
        backoff = self.config.error_backoff_initial_seconds
        first_checkpoint_check = True
        bootstrap_pending = self.config.bootstrap == "always"

        while not self._stop_event.is_set():
            try:
                checkpoint = self._load_checkpoint(counters)
            except Exception as exc:
                backoff = self._recover_observation_error(
                    event_kind="checkpoint-error",
                    message="No se pudo leer el checkpoint durable",
                    error=exc,
                    backoff=backoff,
                    counters=counters,
                )
                continue

            if first_checkpoint_check:
                first_checkpoint_check = False
                if checkpoint is None and self.config.bootstrap == "never":
                    raise WatcherCheckpointError(
                        "no valid inventory checkpoint; bootstrap is disabled"
                    )
                bootstrap_pending = bootstrap_pending or checkpoint is None

            if checkpoint is None:
                bootstrap_pending = True

            if bootstrap_pending:
                cursor = (
                    None
                    if checkpoint is None or not checkpoint.journal_available
                    else self._cursor(checkpoint)
                )
                backoff, succeeded = self._run_with_recovery(
                    "bootstrap",
                    cursor,
                    backoff,
                    counters,
                )
                if succeeded:
                    bootstrap_pending = False
                # The integrated run owns durable publication. Always reload
                # its checkpoint before constructing another signal reader.
                continue

            assert checkpoint is not None
            if checkpoint.journal_available:
                backoff = self._observe_checkpoint(checkpoint, backoff, counters)
            else:
                backoff = self._observe_portable_checkpoint(backoff, counters)

    def run_foreground(self) -> WatcherSummary:
        """Run serially in the calling thread until cancellation is requested."""

        if not self._lifecycle_lock.acquire(blocking=False):
            raise WatcherAlreadyRunningError("incremental watcher is already running")

        try:
            with WatcherLifeLease(
                self.root,
                self.framework_config.state_directory,
            ) as life_lease:
                started_ns = self._time_ns()
                counters = _WatcherCounters()
                try:
                    self._emit(
                        "life-lease-acquired",
                        "Exclusión de vida del watcher adquirida",
                        lock=str(life_lease.path),
                        pid=os.getpid(),
                        replaced_stale_metadata=(life_lease.replaced_stale_metadata),
                    )
                    self._emit(
                        "started",
                        "Watcher incremental iniciado en primer plano",
                        root=str(self.root),
                        bootstrap=self.config.bootstrap,
                    )
                    self._watch_loop(counters)
                finally:
                    summary = self._summary(started_ns, counters)
                    self._emit(
                        "stopped",
                        "Watcher incremental detenido",
                        cancelled=summary.cancelled,
                        successful_runs=summary.successful_runs,
                        failed_runs=summary.failed_runs,
                        signal_batches=summary.signal_batches,
                    )
                return summary
        finally:
            self._lifecycle_lock.release()


# endregion [04]


__all__ = [
    "BootstrapMode",
    "CheckpointLoader",
    "IncrementalWatcher",
    "IncrementalWatcherConfig",
    "JournalSignalSource",
    "JournalSourceFactory",
    "WatchRun",
    "WatchRunFactory",
    "WatcherAlreadyRunningError",
    "WatcherCheckpointError",
    "WatcherEvent",
    "WatcherEventCallback",
    "WatcherLifeLeaseConflict",
    "WatcherRunCallback",
    "WatcherRunReason",
    "WatcherRunSummary",
    "WatcherSummary",
]
