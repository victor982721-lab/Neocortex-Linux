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
from neocortex.runtime.orchestration.route_registry import build_code_inventory_projection
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
                INSERT INTO review_candidates(
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


def test_code_projection_keeps_only_the_code_admission_boundary(tmp_path: Path) -> None:
    code = FileSnapshot(str(tmp_path / "main.py"), 1, 1, 3, 4, -1)
    marker = FileSnapshot(str(tmp_path / "pyproject.toml"), 1, 2, 3, 4, -1)
    binary = FileSnapshot(str(tmp_path / "photo.bin"), 1, 3, 3, 4, -1)

    class Owner:
        def snapshots(self, scan_id: int):
            assert scan_id == 9
            return iter((code, marker, binary))

    projection = build_code_inventory_projection(Owner(), 9)
    assert projection.records == (code, marker)
    assert tuple(projection.snapshots(9)) == (code, marker)
