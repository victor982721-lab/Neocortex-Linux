"""Foreground watcher that schedules bounded portable inventory passes.

The Linux product has no journal-backed signal source.  A watcher therefore
reloads the durable inventory owner, waits for the configured interval, and
delegates a fresh integrated run.  The old signal counters remain in the
summary for output compatibility and are always zero.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from neocortex.deduplication import DedupIndex, InventoryCheckpoint, InventoryError
from neocortex.progress import ProgressCallback
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
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)
from neocortex.safety.corpus_access import CorpusAccessPolicy
from neocortex.runtime.control.watcher_life_lease import WatcherLifeLease

BootstrapMode = Literal["if-needed", "always", "never"]
WatcherRunReason = Literal["bootstrap", "portable-poll"]


class WatcherAlreadyRunningError(RuntimeError):
    """The same watcher instance already owns a foreground loop."""


class WatcherCheckpointError(RuntimeError):
    """No valid durable inventory checkpoint is available for observation."""


@dataclass(frozen=True, slots=True)
class IncrementalWatcherConfig:
    """Bounded timing policy for one foreground portable watcher."""

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
        if not isinstance(self.poll_timeout_seconds, int) or not 0 <= self.poll_timeout_seconds <= 300:
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
    checkpoint_before: None = None
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
CheckpointLoader = Callable[[Path], InventoryCheckpoint | None]
DurableOwnerLoader = Callable[[Path], DurableInventoryOwner | None]


@runtime_checkable
class WatchRun(Protocol):
    def run_once(self) -> object: ...

    def request_cancellation(self) -> None: ...


WatchRunFactory = Callable[[], WatchRun]


class _OrchestratorWatchRun:
    def __init__(self, config: FrameworkConfig, progress: ProgressCallback | None):
        from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

        self._orchestrator = FrameworkOrchestrator(config, progress=progress)

    def run_once(self) -> object:
        return self._orchestrator.run_initial()

    def request_cancellation(self) -> None:
        self._orchestrator.request_cancellation()


@dataclass(slots=True)
class _WatcherCounters:
    bootstrap_runs: int = 0
    portable_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    idle_polls: int = 0
    source_errors: int = 0
    backoff_waits: int = 0
    checkpoint_loads: int = 0
    last_run: WatcherRunSummary | None = None


class IncrementalWatcher:
    """Serialize portable integrated runs in the foreground thread."""

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
        run_factory: WatchRunFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        time_ns: Callable[[], int] = time.time_ns,
        waiter: Callable[[float], None] | None = None,
    ) -> None:
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
            observe_regenerable_artifacts=bool(
                normalize_route_selection(framework_config.route, BUILTIN_ROUTE_ORDER)
            ),
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
            self._run_factory = lambda: _OrchestratorWatchRun(self.framework_config, progress)
        else:
            self._run_factory = run_factory

        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._active_run_lock = threading.Lock()
        self._active_run: WatchRun | None = None
        self._event_sequence = 0

    def request_cancellation(self) -> None:
        self._stop_event.set()
        with self._active_run_lock:
            active = self._active_run
        if active is not None:
            active.request_cancellation()

    def _emit(self, kind: str, message: str, **details: object) -> None:
        if self._event_callback is None:
            return
        self._event_sequence += 1
        self._event_callback(
            WatcherEvent(self._event_sequence, self._time_ns(), kind, message, dict(details))
        )

    def _load_checkpoint(self, counters: _WatcherCounters) -> InventoryCheckpoint | None:
        counters.checkpoint_loads += 1
        checkpoint = self._checkpoint_loader(self.root)
        if checkpoint is None or not checkpoint.valid:
            return None
        self._boundary.verify()
        owner = self._durable_owner_loader(self.root)
        if owner is None:
            self._emit("checkpoint-incompatible", "Checkpoint sin propietario durable compatible")
            return None
        expected = (
            self._boundary.access_policy.root_device_id,
            self._boundary.access_policy.root_file_id,
            self._boundary.access_policy.root_birthtime_ns,
        )
        observed = (
            owner.access_policy.root_device_id,
            owner.access_policy.root_file_id,
            owner.access_policy.root_birthtime_ns,
        )
        if (
            owner.access_policy.mode != "normal"
            or os.path.normcase(os.fspath(owner.access_policy.root))
            != os.path.normcase(os.fspath(self.root))
            or observed != expected
            or owner.binding.corpus_access_mode != "normal"
            or owner.binding.inventory_policy_signature != self._boundary.effective_signature
            or owner.binding.scan_id != checkpoint.scan_id
            or checkpoint.inventory_policy_signature != self._boundary.exclusion_policy.signature
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

    def _wait(self, delay: float, counters: _WatcherCounters) -> None:
        counters.backoff_waits += 1
        if delay <= 0 or self._stop_event.is_set():
            return
        if self._waiter is not None:
            self._waiter(delay)
        else:
            self._stop_event.wait(delay)

    def _run_once(
        self,
        reason: WatcherRunReason,
        counters: _WatcherCounters,
    ) -> bool:
        if self._stop_event.is_set():
            return False
        if reason == "bootstrap":
            counters.bootstrap_runs += 1
        else:
            counters.portable_runs += 1
        started_ns = self._time_ns()
        started = self._monotonic()
        self._emit("run-started", "Ejecución integrada portable iniciada", reason=reason)
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
            return False
        except Exception as exc:
            counters.failed_runs += 1
            summary = WatcherRunSummary(
                reason,
                False,
                started_ns,
                max(0, int((self._monotonic() - started) * 1_000_000_000)),
                None,
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            counters.last_run = summary
            self._emit("run-failed", "Ejecución integrada fallida", reason=reason, detail=str(exc))
            if self._run_callback is not None:
                self._run_callback(summary)
            return False
        finally:
            if run is not None:
                with self._active_run_lock:
                    if self._active_run is run:
                        self._active_run = None

        summary = WatcherRunSummary(
            reason,
            True,
            started_ns,
            max(0, int((self._monotonic() - started) * 1_000_000_000)),
            None,
            run_id=getattr(result, "run_id", None),
            inventory_mode=getattr(result, "inventory_mode", None),
        )
        counters.successful_runs += 1
        counters.last_run = summary
        self._emit("run-completed", "Ejecución integrada completada", reason=reason)
        if self._run_callback is not None:
            self._run_callback(summary)
        return True

    def _summary(self, started_ns: int, counters: _WatcherCounters) -> WatcherSummary:
        return WatcherSummary(
            started_ns=started_ns,
            finished_ns=self._time_ns(),
            cancelled=self._stop_event.is_set(),
            bootstrap_runs=counters.bootstrap_runs,
            change_runs=0,
            discontinuity_runs=0,
            portable_runs=counters.portable_runs,
            successful_runs=counters.successful_runs,
            failed_runs=counters.failed_runs,
            signal_batches=0,
            signal_records=0,
            idle_polls=counters.idle_polls,
            source_restarts=0,
            source_errors=counters.source_errors,
            backoff_waits=counters.backoff_waits,
            checkpoint_loads=counters.checkpoint_loads,
            last_run=counters.last_run,
        )

    def _watch_loop(self, counters: _WatcherCounters) -> None:
        first_check = True
        bootstrap_pending = self.config.bootstrap == "always"
        backoff = self.config.error_backoff_initial_seconds
        while not self._stop_event.is_set():
            try:
                checkpoint = self._load_checkpoint(counters)
            except Exception as exc:
                counters.source_errors += 1
                self._emit("checkpoint-error", "No se pudo leer el checkpoint durable", detail=str(exc))
                self._wait(backoff, counters)
                backoff = min(self.config.error_backoff_max_seconds, backoff * self.config.error_backoff_multiplier)
                continue
            if first_check:
                first_check = False
                if checkpoint is None and self.config.bootstrap == "never":
                    raise WatcherCheckpointError("no valid inventory checkpoint; bootstrap is disabled")
                bootstrap_pending = bootstrap_pending or checkpoint is None
            if bootstrap_pending:
                succeeded = self._run_once("bootstrap", counters)
                if succeeded:
                    bootstrap_pending = False
                    backoff = self.config.error_backoff_initial_seconds
                else:
                    self._wait(backoff, counters)
                    backoff = min(self.config.error_backoff_max_seconds, backoff * self.config.error_backoff_multiplier)
                continue
            interval = self.config.portable_interval_seconds
            self._emit("portable-source-started", "Inventario portable programado", interval_seconds=interval)
            if self._waiter is not None:
                self._waiter(interval)
            elif self._stop_event.wait(interval):
                break
            if self._stop_event.is_set():
                break
            counters.idle_polls += 1
            succeeded = self._run_once("portable-poll", counters)
            if succeeded:
                backoff = self.config.error_backoff_initial_seconds
            else:
                self._wait(backoff, counters)
                backoff = min(self.config.error_backoff_max_seconds, backoff * self.config.error_backoff_multiplier)

    def run_foreground(self) -> WatcherSummary:
        if not self._lifecycle_lock.acquire(blocking=False):
            raise WatcherAlreadyRunningError("incremental watcher is already running")
        try:
            with WatcherLifeLease(self.root, self.framework_config.state_directory) as life_lease:
                del life_lease
                started_ns = self._time_ns()
                counters = _WatcherCounters()
                self._emit("started", "Watcher portable iniciado", root=str(self.root))
                try:
                    self._watch_loop(counters)
                finally:
                    summary = self._summary(started_ns, counters)
                    self._emit("stopped", "Watcher portable detenido", cancelled=summary.cancelled)
                return summary
        finally:
            self._lifecycle_lock.release()


__all__ = [
    "BootstrapMode",
    "CheckpointLoader",
    "IncrementalWatcher",
    "IncrementalWatcherConfig",
    "WatchRun",
    "WatchRunFactory",
    "WatcherAlreadyRunningError",
    "WatcherCheckpointError",
    "WatcherEvent",
    "WatcherEventCallback",
    "WatcherRunCallback",
    "WatcherRunReason",
    "WatcherRunSummary",
    "WatcherSummary",
]
