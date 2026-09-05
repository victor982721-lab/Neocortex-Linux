from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator, RouteExecutionError
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.safety.route_filters import CandidateSelection


def _populate(state: FrameworkState, root: Path) -> int:
    run_id = state.begin_initial_run(root, None)
    state.store_route_candidates(
        run_id,
        (
            (
                mime,
                FileSnapshot(str(root / f"{route}-{number}"), 1, number + 1, 20, 30, -1),
            )
            for route, mime in (("text", "text/plain"), ("audio", "audio/wav"))
            for number in range(3)
        ),
    )
    state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 6)
    return run_id


def test_live_route_reader_fails_closed_when_each_copy_overlaps_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        original_copy = sqlite_immutable._copy_regular_file
        attempts = 0

        def copy_with_progress_write(source: Path, destination: Path) -> None:
            nonlocal attempts
            original_copy(source, destination)
            if source == database:
                attempts += 1
                FrameworkRouteState(database).record_event(
                    run_id, "info", "audio", "concurrent progress"
                )

        monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", copy_with_progress_write)
        with pytest.raises(ImmutableSQLiteUnavailable, match="stable temporary snapshot"):
            FrameworkRouteState(database).selected_route_candidate_counts(
                run_id, "text/plain", None, "text", CandidateSelection()
            )
        assert attempts == 8


def test_parallel_routes_share_a_published_candidate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    copies: list[Path] = []
    original_copy = sqlite_immutable._copy_regular_file
    barrier = threading.Barrier(2, timeout=10)
    seen_databases: list[Path] = []
    run_id = 0

    def copy_with_progress_write(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        if source == database:
            copies.append(source)
            FrameworkRouteState(database).record_event(
                run_id, "info", "audio", "concurrent progress"
            )

    def execute(context):
        route = threading.current_thread().name
        # Both workers report progress while candidate readers enumerate their
        # input. The source owner is deliberately busy, not silently ignored.
        context.framework_state.record_event(run_id, "info", "fixture", route)
        barrier.wait()
        candidate_database = getattr(context.framework_state, "candidate_database", None)
        if candidate_database is not None:
            seen_databases.append(candidate_database)
        context.framework_state.CANDIDATE_BATCH_SIZE = 1
        counts = context.framework_state.selected_route_candidate_counts(
            run_id, "text/plain", None, "text", CandidateSelection()
        )
        rows = []
        for candidate in context.framework_state.iter_selected_route_candidates(
            run_id, "text/plain", "text", CandidateSelection()
        ):
            rows.append(candidate)
            context.framework_state.record_event(run_id, "info", "fixture", "batch read")
        assert counts == (3, 3)
        assert len(rows) == 3
        assert len(list(context.framework_state.iter_route_candidates(run_id, "text/plain"))) == 3
        assert (
            len(list(context.framework_state.iter_route_candidates_by_prefix(run_id, "audio/")))
            == 3
        )
        assert (
            len(
                list(
                    context.framework_state.iter_selected_route_candidates_by_prefix(
                        run_id, "audio/", "audio", CandidateSelection()
                    )
                )
            )
            == 3
        )
        return {"processed": len(rows)}

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", copy_with_progress_write)
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        config = FrameworkConfig(root=tmp_path, state_directory=state_directory, route="all")
        orchestrator = FrameworkOrchestrator(
            config,
            route_registry={name: RouteAdapter(name, execute) for name in ("text", "audio")},
        )
        results, _resources = orchestrator._run_content_routes(
            root=tmp_path, state=state, run_id=run_id, scan_id=1
        )
        assert set(results) == {"text", "audio"}
        assert not copies
        assert len(seen_databases) == 2 and seen_databases[0] == seen_databases[1]
        assert not seen_databases[0].exists()
        statuses = state._connection.execute(
            "SELECT status FROM route_runs WHERE run_id=?", (run_id,)
        ).fetchall()
        assert statuses == [("completed",), ("completed",)]
        assert (
            state._connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=? AND phase='fixture'", (run_id,)
            ).fetchone()[0]
            == 8
        )


@pytest.mark.parametrize("cancel", (False, True))
def test_snapshot_lives_until_workers_finish_on_failure_or_cancellation(
    tmp_path: Path, cancel: bool
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    rendezvous = threading.Barrier(2, timeout=10)
    interrupted = threading.Event()
    snapshot_paths: list[Path] = []
    finished: list[bool] = []

    def fail_or_cancel(context):
        snapshot_paths.append(context.framework_state.candidate_database)
        rendezvous.wait()
        if cancel:
            context.cancellation.cancel()
        interrupted.set()
        if not cancel:
            raise ValueError("fixture route failure")
        return {"processed": 0}

    def finish_reader(context):
        snapshot = context.framework_state.candidate_database
        snapshot_paths.append(snapshot)
        rendezvous.wait()
        assert interrupted.wait(10)
        assert snapshot.is_file()
        assert (
            len(list(context.framework_state.iter_route_candidates(context.run_id, "text/plain")))
            == 3
        )
        finished.append(True)
        return {"processed": 3}

    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        config = FrameworkConfig(root=tmp_path, state_directory=state_directory, route="all")
        orchestrator = FrameworkOrchestrator(
            config,
            route_registry={
                "text": RouteAdapter("text", fail_or_cancel),
                "audio": RouteAdapter("audio", finish_reader),
            },
        )
        with pytest.raises(KeyboardInterrupt if cancel else RouteExecutionError):
            orchestrator._run_content_routes(root=tmp_path, state=state, run_id=run_id, scan_id=1)
        assert finished == [True]
        assert len(snapshot_paths) == 2
        assert not any(path.exists() for path in snapshot_paths)


@pytest.mark.parametrize("replace_during", ("connect", "initialize"))
def test_framework_owner_identity_is_bound_before_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_during: str
) -> None:
    path = tmp_path / "framework.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    with closing(sqlite3.connect(replacement)) as other, other:
        other.execute("CREATE TABLE marker(value INTEGER)")
        other.execute("INSERT INTO marker VALUES(999)")

    class ReplacedState(FrameworkState):
        def _initialize(self):
            super()._initialize()
            with self._connection:
                self._connection.execute("CREATE TABLE marker(value INTEGER)")
                self._connection.execute("INSERT INTO marker VALUES(7)")
            if replace_during == "initialize":
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                replacement.replace(path)

    if replace_during == "connect":
        original_connect = sqlite3.connect

        def connect_then_replace(database, *args, **kwargs):
            connection = original_connect(database, *args, **kwargs)
            if Path(database) == path:
                replacement.replace(path)
            return connection

        monkeypatch.setattr(sqlite3, "connect", connect_then_replace)
    with pytest.raises(ImmutableSQLiteUnavailable, match="owner changed during"):
        with ReplacedState(path) as state:
            with state.route_candidate_snapshot():
                pytest.fail("an old open owner must not be published under a replacement identity")


def test_only_terminal_resume_source_phases_are_frozen_not_current_lifecycle(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        source = _populate(state, tmp_path)
        state.begin_route_runs(source, ("text",))
        state.begin_route_phase(source, "text", "extraction")
        state.complete_route_phase(source, "text", "extraction", {"processed": 3})
        state.fail_initial_run(source)
        current = _populate(state, tmp_path)
        state.begin_route_runs(current, ("text",))
        with state.route_candidate_snapshot() as snapshot:
            route = FrameworkRouteState(
                database, candidate_database=snapshot, resume_source_run_id=source
            )
            assert route.completed_route_phases(source, "text") == frozenset({"extraction"})
            state.begin_route_phase(current, "text", "extraction")
            state.complete_route_phase(current, "text", "extraction", {"processed": 3})
            # This was not in the pre-worker snapshot. A current-run lifecycle
            # query must observe the live committed owner, never old input.
            assert route.completed_route_phases(current, "text") == frozenset({"extraction"})


@pytest.mark.parametrize("missing_source", (False, True))
def test_resume_phase_snapshot_rejects_a_nonterminal_or_missing_source(
    tmp_path: Path, missing_source: bool
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        selected = run_id + 1 if missing_source else run_id
        with state.route_candidate_snapshot() as snapshot:
            route = FrameworkRouteState(
                database, candidate_database=snapshot, resume_source_run_id=selected
            )
            with pytest.raises(ImmutableSQLiteUnavailable, match="terminal source run"):
                route.completed_route_phases(selected, "text")
