from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteSnapshotBudgetExceeded,
    immutable_sqlite_database,
)
from neocortex.safety.route_filters import CandidateSelection


def _populate(state: FrameworkState, root: Path) -> int:
    run_id = state.begin_initial_run(root, None)
    state.store_route_candidates(
        run_id,
        (
            (
                "text/plain",
                FileSnapshot(str(root / f"candidate-{number}"), 1, number + 1, 20, 30, -1),
            )
            for number in range(3)
        ),
    )
    state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 3)
    return run_id


def test_route_snapshot_projects_candidates_not_large_framework_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        with state._connection:
            state._connection.execute("CREATE TABLE unrelated_history(payload BLOB NOT NULL)")
            state._connection.execute(
                "INSERT INTO unrelated_history(payload) VALUES(zeroblob(?))",
                (128 * 1024 * 1024,),
            )
        source_size = database.stat().st_size
        assert source_size >= 128 * 1024 * 1024

        with state.route_candidate_snapshot(run_id=run_id) as snapshot:
            assert snapshot.stat().st_size < 256 * 1024 * 1024
            with immutable_sqlite_database(snapshot) as connection:
                assert connection.execute(
                    "SELECT COUNT(*) FROM route_candidates WHERE run_id=?", (run_id,)
                ).fetchone()[0] == 3
                assert connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='unrelated_history'"
                ).fetchone()[0] == 0

        assert not snapshot.exists()


def test_route_snapshot_projection_preserves_review_selection_and_cancellation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        with state._connection:
            state._connection.execute(
                """
                INSERT INTO findings(
                    route_name,volume_id,file_id,reason_code,path,size,mtime_ns,
                    birthtime_ns,source_status,recommendation,retryable,confidence,
                    evidence_json,detector_version,status,first_detected_ns,
                    last_detected_ns,last_seen_run_id
                ) VALUES('text','1','2','fixture',?,20,30,-1,'error',
                    'retry',1,1.0,'{}','fixture','open',1,1,?)
                """,
                (str(tmp_path / "candidate-1"), run_id),
            )

        with state.route_candidate_snapshot(run_id=run_id) as snapshot:
            route = FrameworkRouteState(database, candidate_database=snapshot)
            assert route.selected_route_candidate_counts(
                run_id,
                "text/plain",
                None,
                "text",
                CandidateSelection.from_values(recommendations=("retry",)),
            ) == (1, 1)

        def cancelled() -> bool:
            return True
        with pytest.raises(SQLiteSnapshotBudgetExceeded, match="cancelled"):
            with state.route_candidate_snapshot(
                run_id=run_id,
                cancellation_check=cancelled,
            ):
                pytest.fail("cancelled route projection must not publish a view")
        assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_route_snapshot_projection_keeps_owner_transaction_idle_after_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        with pytest.raises(ImmutableSQLiteUnavailable):
            with state.route_candidate_snapshot(
                run_id=run_id,
                cancellation_check=lambda: True,
            ):
                pytest.fail("cancelled projection cannot publish")
        assert not state._connection.in_transaction


def test_route_projection_joins_historical_reviews_by_indexed_identity(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    count = 1000
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(tmp_path, None)
        state.store_route_candidates(run_id, (
            ("text/plain", FileSnapshot(str(tmp_path / f"current-{i:05}"), 1, i + 1, 20, 30, -1))
            for i in range(count)
        ))
        with state._connection:
            state._connection.executemany(
                """INSERT INTO findings(
                    route_name,volume_id,file_id,reason_code,path,size,mtime_ns,
                    birthtime_ns,source_status,recommendation,retryable,confidence,
                    evidence_json,detector_version,status,first_detected_ns,
                    last_detected_ns,last_seen_run_id
                ) VALUES('text','1',?,'fixture',?,20,30,-1,'error',
                    'retry',1,1.0,'{}','fixture','open',1,1,?)""",
                ((f"{i + count + 1:x}", str(tmp_path / f"old-{i:05}"), run_id) for i in range(count)),
            )
        source_instructions = 0
        source_queries: list[str] = []

        def source_progress() -> int:
            nonlocal source_instructions
            source_instructions += 1000
            # Quadratic work previously used ~8 million VM instructions for
            # this empty join. A generous bound avoids timing-dependent tests.
            return int(source_instructions >= 300_000)

        state._connection.set_progress_handler(source_progress, 1000)
        state._connection.set_trace_callback(source_queries.append)
        try:
            with state.route_candidate_snapshot(run_id=run_id) as snapshot:
                with immutable_sqlite_database(snapshot) as reader:
                    assert reader.execute("SELECT COUNT(*) FROM route_candidates").fetchone()[0] == count
                    assert reader.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
            assert source_instructions < 300_000
        finally:
            state._connection.set_progress_handler(None, 0)
            state._connection.set_trace_callback(None)
        review_query = next(query for query in source_queries if "FROM findings r" in query)
        plan = tuple(str(row[3]) for row in state._connection.execute("EXPLAIN QUERY PLAN " + review_query))
        assert any(
            "route_candidates_identity_idx" in step
            and "run_id=? AND volume_id=? AND file_id=?" in step
            for step in plan
        )


def test_projection_source_does_not_open_another_framework_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _populate(state, tmp_path)
        original_connect = sqlite3.connect
        opened: list[object] = []

        def only_temporary(database_arg, *args, **kwargs):
            opened.append(database_arg)
            return original_connect(database_arg, *args, **kwargs)

        # The route projection receives the already-open owner connection;
        # exactly one extra handle, for the disposable target, is expected.
        import neocortex.persistence.sqlite_writer_snapshot as snapshot_module

        monkeypatch.setattr(snapshot_module.sqlite3, "connect", only_temporary)
        with state.route_candidate_snapshot(run_id=run_id) as snapshot:
            assert opened == [snapshot]
        assert not snapshot.exists()
