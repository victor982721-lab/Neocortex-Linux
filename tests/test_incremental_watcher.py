from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TypeAlias, cast

import pytest

from neocortex.enumeration import (
    JournalCursor,
    JournalDiscontinuityError,
    NtfsEntry,
    UsnChangeBatch,
)
from neocortex.deduplication import InventoryCheckpoint
from _04_Nucleo_Operativo.corpus_access import CorpusAccessPolicy
from _04_Nucleo_Operativo.framework_state_writer import (
    DurableInventoryBinding,
    DurableInventoryOwner,
    FrameworkState,
    read_latest_durable_inventory_owner,
)
from _04_Nucleo_Operativo.models import FrameworkConfig
from _04_Nucleo_Operativo.orchestrator import (
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from _04_Nucleo_Operativo.watcher import (
    CheckpointLoader,
    DurableOwnerLoader,
    IncrementalWatcher,
    IncrementalWatcherConfig,
    WatcherAlreadyRunningError,
    WatcherCheckpointError,
    WatcherSummary,
)


# region [01] Deterministic watcher fakes


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def time_ns(self) -> int:
        return int(self.value * 1_000_000_000)

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.advance(seconds)


PollAction: TypeAlias = UsnChangeBatch | None | BaseException | Callable[[], UsnChangeBatch | None]


class ScriptedSource:
    def __init__(self, actions: list[PollAction]):
        self.actions = list(actions)
        self.closed = 0

    def poll(self) -> UsnChangeBatch | None:
        if not self.actions:
            raise AssertionError("scripted source exhausted without cancellation")
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action()
        return action

    def close(self) -> None:
        self.closed += 1


class FakeRun:
    def __init__(
        self,
        run_once: Callable[[], object],
        cancel: Callable[[], None] | None = None,
    ):
        self._run_once = run_once
        self._cancel = cancel or (lambda: None)

    def run_once(self) -> object:
        return self._run_once()

    def request_cancellation(self) -> None:
        self._cancel()


def checkpoint(next_usn: int = 100) -> InventoryCheckpoint:
    return InventoryCheckpoint(
        root=str(Path("C:/corpus")),
        scan_id=7,
        volume="C:",
        journal_id=41,
        next_usn=next_usn,
    )


def portable_checkpoint() -> InventoryCheckpoint:
    return InventoryCheckpoint(
        root=str(Path("C:/corpus")),
        scan_id=7,
        volume=None,
        journal_id=None,
        next_usn=None,
    )


def batch(before: int, after: int, records: int = 1) -> UsnChangeBatch:
    return UsnChangeBatch(
        JournalCursor("C:", 41, before),
        JournalCursor("C:", 41, after),
        cast(tuple[NtfsEntry, ...], tuple(object() for _ in range(records))),
    )


def framework_config(tmp_path: Path) -> FrameworkConfig:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    return FrameworkConfig(
        root=corpus,
        state_directory=tmp_path / "state",
    )


def test_durable_owner_reader_does_not_recreate_framework_sidecars(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass

    sidecars = (Path(f"{database}-wal"), Path(f"{database}-shm"))
    assert not any(path.exists() for path in sidecars)

    assert read_latest_durable_inventory_owner(database, corpus) is None
    assert not any(path.exists() for path in sidecars)


def durable_loaders(
    config: FrameworkConfig,
    loader: CheckpointLoader,
) -> tuple[CheckpointLoader, DurableOwnerLoader]:
    access_policy = CorpusAccessPolicy.capture("normal", config.root)
    state_layout = initialize_authorized_state_directory(
        access_policy,
        config.state_directory,
        require_disjoint=False,
    )
    boundary = build_normal_inventory_boundary(
        config.root,
        state_layout.path,
        access_policy=access_policy,
        state_policy=state_layout.state_policy,
        internal_paths_policy=state_layout.internal_paths_policy,
    )

    def load_checkpoint(root: Path) -> InventoryCheckpoint | None:
        observed = loader(root)
        if observed is None:
            return None
        return replace(
            observed,
            root=str(boundary.access_policy.root),
            inventory_policy_signature=boundary.exclusion_policy.signature,
        )

    def load_owner(root: Path) -> DurableInventoryOwner | None:
        observed = loader(root)
        if observed is None:
            return None
        end_cursor = (
            JournalCursor(
                observed.volume,
                observed.journal_id,
                observed.next_usn,
            )
            if observed.journal_available
            else None
        )
        return DurableInventoryOwner(
            DurableInventoryBinding(
                run_id=17,
                scan_id=observed.scan_id,
                corpus_access_mode="normal",
                inventory_policy_signature=boundary.effective_signature,
                end_cursor=end_cursor,
            ),
            boundary.access_policy,
        )

    return load_checkpoint, load_owner


# endregion [01]


# region [02] Signal, debounce, and durable cursor semantics


def test_batches_only_signal_and_reader_restarts_from_durable_checkpoint(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    durable = checkpoint()
    source_cursors: list[JournalCursor] = []
    sources: list[ScriptedSource] = []
    runs: list[str] = []
    watcher: IncrementalWatcher

    def first_signal() -> UsnChangeBatch:
        clock.advance(0.1)
        return batch(100, 110, 2)

    def second_signal() -> UsnChangeBatch:
        clock.advance(0.1)
        return batch(110, 120, 3)

    def quiet_poll() -> None:
        clock.advance(1.1)
        return None

    def stop_poll() -> None:
        watcher.request_cancellation()
        return None

    scripts: list[list[PollAction]] = [
        [first_signal, second_signal, quiet_poll],
        [stop_poll],
    ]

    def source_factory(
        _volume: str,
        cursor: JournalCursor,
        _timeout: int,
    ) -> ScriptedSource:
        source_cursors.append(cursor)
        source = ScriptedSource(scripts.pop(0))
        sources.append(source)
        return source

    def run_once() -> object:
        runs.append("run")
        return SimpleNamespace(run_id=19, inventory_mode="incremental")

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: durable,
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(
            bootstrap="if-needed",
            debounce_seconds=1.0,
            max_debounce_seconds=10.0,
        ),
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=source_factory,
        run_factory=lambda: FakeRun(run_once),
        monotonic=clock.monotonic,
        time_ns=clock.time_ns,
        waiter=clock.wait,
    )

    summary = watcher.run_foreground()

    assert runs == ["run"]
    assert [cursor.next_usn for cursor in source_cursors] == [100, 100]
    assert durable.next_usn == 100
    assert summary.change_runs == 1
    assert summary.signal_batches == 2
    assert summary.signal_records == 5
    assert summary.successful_runs == 1
    assert all(source.closed == 1 for source in sources)


def test_no_changes_does_not_start_an_integrated_run(tmp_path: Path) -> None:
    clock = FakeClock()
    watcher: IncrementalWatcher
    polls = 0

    def idle() -> None:
        nonlocal polls
        polls += 1
        clock.advance(1)
        if polls == 3:
            watcher.request_cancellation()
        return None

    source = ScriptedSource([idle, idle, idle])
    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: checkpoint(),
    )
    watcher = IncrementalWatcher(
        app_config,
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=lambda _volume, _cursor, _timeout: source,
        run_factory=lambda: pytest.fail("no-change watcher started a run"),
        monotonic=clock.monotonic,
        time_ns=clock.time_ns,
        waiter=clock.wait,
    )

    summary = watcher.run_foreground()

    assert summary.cancelled
    assert summary.idle_polls == 3
    assert summary.successful_runs == 0
    assert summary.failed_runs == 0
    assert source.closed == 1


def test_portable_checkpoint_schedules_integrated_runs_without_a_usn_source(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    durable = portable_checkpoint()
    reasons: list[str] = []
    watcher: IncrementalWatcher

    def run_once() -> object:
        watcher.request_cancellation()
        return SimpleNamespace(run_id=23, inventory_mode="full")

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: durable,
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(portable_interval_seconds=15),
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=lambda *_args: pytest.fail("portable watcher attempted to open USN"),
        run_factory=lambda: FakeRun(run_once),
        run_callback=lambda summary: reasons.append(summary.reason),
        monotonic=clock.monotonic,
        time_ns=clock.time_ns,
        waiter=clock.wait,
    )

    summary = watcher.run_foreground()

    assert reasons == ["portable-poll"]
    assert clock.waits == [15]
    assert summary.portable_runs == 1
    assert summary.change_runs == summary.discontinuity_runs == 0
    assert summary.source_restarts == summary.signal_batches == 0
    assert summary.successful_runs == 1


def test_unavailable_usn_falls_back_to_one_portable_integrated_run(
    tmp_path: Path,
) -> None:
    durable = checkpoint()
    reasons: list[str] = []
    watcher: IncrementalWatcher

    def run_once() -> object:
        watcher.request_cancellation()
        return SimpleNamespace(run_id=24, inventory_mode="full")

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: durable,
    )
    watcher = IncrementalWatcher(
        app_config,
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=lambda *_args: (_ for _ in ()).throw(
            OSError("raw journal unavailable")
        ),
        run_factory=lambda: FakeRun(run_once),
        run_callback=lambda summary: reasons.append(summary.reason),
    )

    summary = watcher.run_foreground()

    assert reasons == ["portable-fallback"]
    assert summary.portable_runs == 1
    assert summary.source_errors == 1
    assert summary.successful_runs == 1


def test_failed_run_backs_off_and_restarts_from_unchanged_checkpoint(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    durable = checkpoint(100)
    observed: list[int] = []
    watcher: IncrementalWatcher

    def signal() -> UsnChangeBatch:
        clock.advance(0.1)
        return batch(100, 110)

    def quiet() -> None:
        clock.advance(0.2)
        return None

    def stop() -> None:
        watcher.request_cancellation()
        return None

    def fail_run() -> object:
        raise RuntimeError("transient run failure")

    scripts: list[list[PollAction]] = [[signal, quiet], [stop]]

    def source_factory(
        _volume: str,
        cursor: JournalCursor,
        _timeout: int,
    ) -> ScriptedSource:
        observed.append(cursor.next_usn)
        return ScriptedSource(scripts.pop(0))

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: durable,
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(
            debounce_seconds=0.1,
            max_debounce_seconds=1,
            error_backoff_initial_seconds=0.25,
            error_backoff_max_seconds=0.5,
        ),
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=source_factory,
        run_factory=lambda: FakeRun(fail_run),
        monotonic=clock.monotonic,
        time_ns=clock.time_ns,
        waiter=clock.wait,
    )

    summary = watcher.run_foreground()

    assert observed == [100, 100]
    assert durable.next_usn == 100
    assert clock.waits == [0.25]
    assert summary.failed_runs == 1
    assert summary.backoff_waits == 1
    assert summary.last_run is not None
    assert summary.last_run.error_type == "RuntimeError"


# endregion [02]


# region [03] Bootstrap, discontinuity, and bounded recovery


def test_if_needed_bootstrap_publishes_checkpoint_before_observation(
    tmp_path: Path,
) -> None:
    durable: InventoryCheckpoint | None = None
    watcher: IncrementalWatcher
    observed: list[int] = []

    def run_once() -> object:
        nonlocal durable
        durable = checkpoint(200)
        return SimpleNamespace(run_id=1, inventory_mode="full")

    def source_factory(
        _volume: str,
        cursor: JournalCursor,
        _timeout: int,
    ) -> ScriptedSource:
        observed.append(cursor.next_usn)

        def stop() -> None:
            watcher.request_cancellation()
            return None

        return ScriptedSource([stop])

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: durable,
    )
    watcher = IncrementalWatcher(
        app_config,
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=source_factory,
        run_factory=lambda: FakeRun(run_once),
    )

    summary = watcher.run_foreground()

    assert summary.bootstrap_runs == 1
    assert summary.successful_runs == 1
    assert observed == [200]


def test_bootstrap_never_requires_an_existing_valid_checkpoint(tmp_path: Path) -> None:
    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: None,
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(bootstrap="never"),
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
    )

    with pytest.raises(WatcherCheckpointError, match="bootstrap is disabled"):
        watcher.run_foreground()


def test_custom_checkpoint_without_compatible_owner_fails_closed(
    tmp_path: Path,
) -> None:
    app_config = framework_config(tmp_path)
    checkpoint_loader, _ = durable_loaders(
        app_config,
        lambda _root: checkpoint(),
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(bootstrap="never"),
        checkpoint_loader=checkpoint_loader,
    )

    with pytest.raises(WatcherCheckpointError, match="bootstrap is disabled"):
        watcher.run_foreground()


def test_journal_discontinuity_triggers_integrated_rescan_and_fresh_checkpoint(
    tmp_path: Path,
) -> None:
    durable = checkpoint(100)
    clock = FakeClock()
    reasons: list[str] = []
    observed: list[int] = []
    watcher: IncrementalWatcher

    first = ScriptedSource([JournalDiscontinuityError("journal wrapped")])

    def stop() -> None:
        watcher.request_cancellation()
        return None

    second = ScriptedSource([stop])
    sources = [first, second]

    def source_factory(
        _volume: str,
        cursor: JournalCursor,
        _timeout: int,
    ) -> ScriptedSource:
        observed.append(cursor.next_usn)
        return sources.pop(0)

    def run_once() -> object:
        nonlocal durable
        durable = checkpoint(250)
        return SimpleNamespace(run_id=8, inventory_mode="full")

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: durable,
    )
    watcher = IncrementalWatcher(
        app_config,
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=source_factory,
        run_factory=lambda: FakeRun(run_once),
        run_callback=lambda summary: reasons.append(summary.reason),
        monotonic=clock.monotonic,
        time_ns=clock.time_ns,
        waiter=clock.wait,
    )

    summary = watcher.run_foreground()

    assert reasons == ["journal-discontinuity"]
    assert observed == [100, 250]
    assert summary.discontinuity_runs == 1
    assert summary.successful_runs == 1
    assert first.closed == 1
    assert second.closed == 1


def test_observation_errors_use_bounded_exponential_backoff(tmp_path: Path) -> None:
    clock = FakeClock()
    attempts = 0
    watcher: IncrementalWatcher

    def source_factory(
        _volume: str,
        _cursor: JournalCursor,
        _timeout: int,
    ) -> ScriptedSource:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise RuntimeError(f"temporary failure {attempts}")

        def stop() -> None:
            watcher.request_cancellation()
            return None

        return ScriptedSource([stop])

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: checkpoint(),
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(
            error_backoff_initial_seconds=0.25,
            error_backoff_max_seconds=0.5,
            error_backoff_multiplier=2,
        ),
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        journal_source_factory=source_factory,
        run_factory=lambda: pytest.fail("source errors must not start a run"),
        monotonic=clock.monotonic,
        time_ns=clock.time_ns,
        waiter=clock.wait,
    )

    summary = watcher.run_foreground()

    assert clock.waits == [0.25, 0.5, 0.5]
    assert summary.source_errors == 3
    assert summary.backoff_waits == 3
    assert summary.successful_runs == 0


# endregion [03]


# region [04] Cancellation, serialization, and destructive-mode rejection


def test_cancellation_reaches_active_run_and_instance_cannot_run_twice(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    cancelled = threading.Event()
    completed: list[WatcherSummary] = []

    def blocked_run() -> object:
        entered.set()
        if not cancelled.wait(2):
            raise AssertionError("active run did not receive cancellation")
        raise RuntimeError("cancelled")

    app_config = framework_config(tmp_path)
    checkpoint_loader, owner_loader = durable_loaders(
        app_config,
        lambda _root: checkpoint(),
    )
    watcher = IncrementalWatcher(
        app_config,
        IncrementalWatcherConfig(bootstrap="always"),
        checkpoint_loader=checkpoint_loader,
        durable_owner_loader=owner_loader,
        run_factory=lambda: FakeRun(blocked_run, cancelled.set),
    )

    thread = threading.Thread(
        target=lambda: completed.append(watcher.run_foreground()),
        daemon=False,
    )
    thread.start()
    assert entered.wait(1)

    with pytest.raises(WatcherAlreadyRunningError):
        watcher.run_foreground()

    watcher.request_cancellation()
    thread.join(2)

    assert not thread.is_alive()
    assert len(completed) == 1
    summary = completed[0]
    assert summary.cancelled
    assert summary.failed_runs == 0
    assert summary.last_run is not None
    assert not summary.last_run.succeeded


def test_apply_actions_is_rejected_before_any_watcher_work(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not allow apply_actions"):
        IncrementalWatcher(
            FrameworkConfig(
                root=Path("C:/corpus"),
                state_directory=tmp_path / "state",
                apply_actions=True,
            )
        )


# endregion [04]
