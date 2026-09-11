"""Framework heartbeat read isolation for the integrated Semantic budget."""

from __future__ import annotations

import sqlite3
import os
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from neocortex.api.cli import cli_semantic
from neocortex.persistence import framework_connection
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _framework_fixture(
    tmp_path: Path,
    *,
    max_duration_seconds: float | None = None,
) -> tuple[Path, int]:
    root = tmp_path / "corpus"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        if max_duration_seconds is not None:
            state.publish_run_budget(
                run_id,
                {"max_duration_seconds": max_duration_seconds},
            )
    return database, run_id


def _budget_args(state_directory: Path, cancellation_check) -> Namespace:
    return Namespace(
        state_directory=state_directory,
        semantic_max_items=None,
        semantic_max_new_jobs=None,
        semantic_time_budget_seconds=None,
        _semantic_cancellation_check=cancellation_check,
        all=True,
    )


def test_forced_framework_snapshot_survives_writer_pulse_after_open(tmp_path: Path) -> None:
    database, run_id = _framework_fixture(tmp_path)
    reader = framework_connection.connect_existing_framework(
        database,
        readonly=True,
        force_snapshot=True,
        timeout_seconds=1.0,
    )
    try:
        snapshot_path = Path(reader.execute("PRAGMA database_list").fetchone()[2])
        assert snapshot_path != database
        before = reader.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]

        with FrameworkState(database) as writer:
            writer.record_event(run_id, "info", "fixture", "writer pulse")

        # The reader is detached from the Framework owner and therefore its
        # close cannot observe the heartbeat/source fence changing afterward.
        assert reader.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == before
    finally:
        reader.close()


def test_force_snapshot_is_restricted_to_readonly_framework_connections(
    tmp_path: Path,
) -> None:
    database, _run_id = _framework_fixture(tmp_path)
    with pytest.raises(ValueError, match="readonly"):
        framework_connection.connect_existing_framework(
            database,
            readonly=False,
            force_snapshot=True,
        )


def test_framework_reader_does_not_relabel_nonowner_enoent_as_missing_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _run_id = _framework_fixture(tmp_path)
    sidecar = f"{database}-wal"

    def missing_sidecar(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise FileNotFoundError(2, "sidecar disappeared", sidecar)

    monkeypatch.setattr(
        framework_connection,
        "open_sidecar_safe_sqlite_connection",
        missing_sidecar,
    )
    with pytest.raises(FileNotFoundError) as raised:
        framework_connection.connect_existing_framework(database, readonly=True)
    assert raised.value.filename == sidecar


def test_framework_control_read_fails_closed_if_owner_identity_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, run_id = _framework_fixture(tmp_path)
    original_validate = framework_connection._validate_existing_owner
    calls = 0

    def replace_before_final_validation(path: Path) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 3:
            replacement = path.with_name("framework-replacement.sqlite3")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return original_validate(path)

    monkeypatch.setattr(
        framework_connection,
        "_validate_existing_owner",
        replace_before_final_validation,
    )
    with pytest.raises(sqlite3.OperationalError, match="identity changed"):
        framework_connection._read_framework_cancellation_requested(
            database,
            run_id,
            timeout_seconds=0.5,
        )
    assert calls == 3


def test_integrated_budget_uses_owner_control_read_and_preserves_external_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, run_id = _framework_fixture(tmp_path)
    cancelled = False
    calls: list[dict[str, object]] = []

    def external_cancellation() -> bool:
        return cancelled

    original = framework_connection._read_framework_cancellation_requested

    def observe(path: Path, run: int, **kwargs: Any) -> bool:
        calls.append(dict(kwargs))
        return original(path, run, **kwargs)

    def forbidden_snapshot(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise AssertionError("owner-coordinated cancellation must not create a snapshot")

    monkeypatch.setattr(
        framework_connection,
        "_read_framework_cancellation_requested",
        observe,
    )
    monkeypatch.setattr(
        framework_connection,
        "open_sidecar_safe_sqlite_connection",
        forbidden_snapshot,
    )
    args = _budget_args(tmp_path, external_cancellation)
    budget = cli_semantic._integrated_semantic_budget(args, run_id)

    assert budget.cancellation_check is not None
    assert budget.cancellation_check() is False
    assert len(calls) == 1
    assert callable(calls[0]["control_checkpoint"])
    assert calls[0]["timeout_seconds"] == pytest.approx(1.0)

    cancelled = True
    assert budget.cancellation_check() is True
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        budget.checkpoint()
    assert len(calls) == 1


def test_framework_control_read_reuses_owner_without_snapshot_io_under_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, run_id = _framework_fixture(tmp_path)
    with FrameworkState(database, existing_only=True) as state:
        before_events = state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]

    snapshot_calls: list[object] = []

    def forbidden_snapshot(*args: object, **kwargs: object) -> sqlite3.Connection:
        snapshot_calls.append((args, kwargs))
        raise AssertionError("Framework control probes must not create snapshots")

    monkeypatch.setattr(
        framework_connection,
        "open_sidecar_safe_sqlite_connection",
        forbidden_snapshot,
    )
    from neocortex.runtime.control.locking import FrameworkRunLock
    from neocortex.runtime.orchestration.run_lifecycle import RunHeartbeat

    def no_cancel() -> None:
        return None

    with FrameworkRunLock(tmp_path / "framework.lock"):
        heartbeat = RunHeartbeat(
            database,
            run_id,
            interval_seconds=0.002,
        ).start()
        try:
            probes: list[bool] = []
            for _ in range(12):
                probes.append(
                    framework_connection._read_framework_cancellation_requested(
                        database,
                        run_id,
                        timeout_seconds=0.5,
                        control_checkpoint=no_cancel,
                    )
                )
                time.sleep(0.004)
        finally:
            heartbeat.stop()
        assert probes == [False] * len(probes)

        with FrameworkState(database) as state:
            assert state.request_run_cancellation(run_id, "manual") is True

        assert framework_connection._read_framework_cancellation_requested(
            database,
            run_id,
            timeout_seconds=0.5,
            control_checkpoint=no_cancel,
        ) is True

    with FrameworkState(database, existing_only=True) as state:
        after_events = state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    assert after_events >= before_events + 1
    assert snapshot_calls == []


def test_framework_control_read_rejects_updates_on_the_real_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, run_id = _framework_fixture(tmp_path)
    real_connect = framework_connection.connect_existing_framework
    connections: list[sqlite3.Connection] = []
    attempted = False

    def capture_connect(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(path, **kwargs)
        connections.append(connection)
        return connection

    def reject_write() -> None:
        nonlocal attempted
        connection = connections[0]
        if attempted or not connection.in_transaction:
            return
        attempted = True
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        before = connection.execute(
            "SELECT heartbeat_ns FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "UPDATE initial_runs SET heartbeat_ns=1 WHERE run_id=?", (run_id,)
            )
        assert connection.execute(
            "SELECT heartbeat_ns FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0] == before

    monkeypatch.setattr(framework_connection, "connect_existing_framework", capture_connect)
    assert framework_connection._read_framework_cancellation_requested(
        database, run_id, timeout_seconds=0.5, control_checkpoint=reject_write
    ) is False
    assert attempted
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_integrated_budget_throttle_starts_after_control_read_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, run_id = _framework_fixture(tmp_path)
    clock = [100.0]
    calls: list[float] = []

    def never_cancelled() -> bool:
        return False

    class _Cursor:
        def __init__(self, value: tuple[int, ...] | None = None) -> None:
            self.value = value

        def fetchone(self) -> tuple[int, ...] | None:
            return self.value

    class _Connection:
        in_transaction = False

        def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            statement = str(_args[0]) if _args else ""
            if statement == "PRAGMA query_only=ON" or statement == "PRAGMA query_only":
                return _Cursor((1,))
            if statement == "BEGIN":
                self.in_transaction = True
            return _Cursor()

        def set_progress_handler(self, _callback: object, _instructions: int) -> None:
            return None

        def rollback(self) -> None:
            self.in_transaction = False

        def close(self) -> None:
            return None

    def fake_connect(_path: Path, **_kwargs: object) -> _Connection:
        calls.append(clock[0])
        # Model a control read that takes longer than the 100 ms polling interval.
        clock[0] += 0.2
        return _Connection()

    monkeypatch.setattr(cli_semantic.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(framework_connection, "connect_existing_framework", fake_connect)
    budget = cli_semantic._integrated_semantic_budget(
        _budget_args(tmp_path, never_cancelled),
        run_id,
    )
    assert budget.cancellation_check is not None

    assert budget.cancellation_check() is False
    assert calls == [100.0]
    # The second checkpoint follows the close at t=100.2 and must not reopen
    # the same Framework control read merely because the old interval elapsed.
    assert budget.cancellation_check() is False
    assert calls == [100.0]

    clock[0] = 100.31
    assert budget.cancellation_check() is False
    assert calls == [100.0, pytest.approx(100.31)]


def test_framework_control_read_preserves_keyboard_interrupt_from_sql_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, run_id = _framework_fixture(tmp_path)
    progress_handlers: list[tuple[object, int]] = []
    control_calls = 0

    class _Cursor:
        def __init__(self, value: tuple[int, ...] | None = None) -> None:
            self.value = value

        def fetchone(self) -> tuple[int, ...] | None:
            return self.value

    class _Connection:
        in_transaction = False
        closed = False
        progress: object | None = None

        def execute(self, *args: object, **_kwargs: object) -> _Cursor:
            statement = str(args[0]) if args else ""
            if statement in {"PRAGMA query_only=ON", "PRAGMA query_only"}:
                return _Cursor((1,))
            if statement == "BEGIN":
                self.in_transaction = True
                return _Cursor()
            if statement.startswith("SELECT 1 FROM run_events"):
                assert callable(self.progress)
                if self.progress() == 1:
                    raise sqlite3.OperationalError("interrupted")
            return _Cursor()

        def set_progress_handler(self, callback: object, instructions: int) -> None:
            self.progress = callback
            progress_handlers.append((callback, instructions))

        def rollback(self) -> None:
            self.in_transaction = False

        def close(self) -> None:
            self.closed = True

    connection = _Connection()

    def fake_connect(_path: Path, **_kwargs: object) -> _Connection:
        return connection

    def control_checkpoint() -> None:
        nonlocal control_calls
        control_calls += 1
        if control_calls >= 3:
            raise KeyboardInterrupt("manual cancellation")

    monkeypatch.setattr(framework_connection, "connect_existing_framework", fake_connect)
    with pytest.raises(KeyboardInterrupt, match="manual cancellation"):
        framework_connection._read_framework_cancellation_requested(
            database,
            run_id,
            timeout_seconds=0.5,
            control_checkpoint=control_checkpoint,
        )
    assert control_calls == 3
    assert progress_handlers[0][1] == 1_000
    assert progress_handlers[-1] == (None, 0)
    assert connection.closed
    assert not connection.in_transaction


def test_integrated_budget_caps_control_read_to_remaining_global_and_rejects_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, run_id = _framework_fixture(tmp_path, max_duration_seconds=10.0)
    with FrameworkState(tmp_path / "framework.sqlite3", existing_only=True) as state:
        snapshot = state.read_run_budget(run_id)
    assert snapshot is not None
    deadline_ns = int(snapshot["deadline_ns"])

    calls: list[dict[str, object]] = []

    class _Cursor:
        def __init__(self, value: tuple[int, ...] | None = None) -> None:
            self.value = value

        def fetchone(self) -> tuple[int, ...] | None:
            return self.value

    class _Connection:
        in_transaction = False

        def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            statement = str(_args[0]) if _args else ""
            if statement == "PRAGMA query_only=ON" or statement == "PRAGMA query_only":
                return _Cursor((1,))
            if statement == "BEGIN":
                self.in_transaction = True
            return _Cursor()

        def set_progress_handler(self, _callback: object, _instructions: int) -> None:
            return None

        def rollback(self) -> None:
            self.in_transaction = False

        def close(self) -> None:
            return None

    def fake_connect(_path: Path, **kwargs: object) -> _Connection:
        calls.append(dict(kwargs))
        return _Connection()

    def never_cancelled() -> bool:
        return False

    monkeypatch.setattr(framework_connection, "connect_existing_framework", fake_connect)
    args = _budget_args(tmp_path, never_cancelled)
    budget = cli_semantic._integrated_semantic_budget(args, run_id)

    # Exercise a positive sub-second remainder without waiting in real time.
    monkeypatch.setattr(
        cli_semantic.time,
        "time_ns",
        lambda: deadline_ns - 500_000_000,
    )
    assert budget.cancellation_check is not None
    assert budget.cancellation_check() is False
    assert calls[0]["readonly"] is False
    assert "force_snapshot" not in calls[0]
    timeout = calls[0]["timeout_seconds"]
    assert isinstance(timeout, (int, float))
    assert 0.49 <= float(timeout) <= 0.51

    # The callback must not turn an expired durable budget into a normal false
    # result that could later be reported as complete.
    after_first_check = cli_semantic.time.monotonic()
    monkeypatch.setattr(
        cli_semantic.time,
        "monotonic",
        lambda: after_first_check + 0.2,
    )
    monkeypatch.setattr(cli_semantic.time, "time_ns", lambda: deadline_ns + 1)
    with pytest.raises(RunBudgetExceeded, match="time"):
        budget.cancellation_check()
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("error", "expects_model_hint"),
    [
        (sqlite3.OperationalError("framework owner changed"), False),
        (None, True),
    ],
)
def test_semantic_index_failure_only_suggests_model_cache_for_typed_model_error(
    error: Exception | None,
    expects_model_hint: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.semantic.semantic_config import SemanticModelUnavailableError

    execution = cast(
        cli_semantic._SemanticIndexExecution,
        SimpleNamespace(results=(), scope_timings=()),
    )
    failure = (
        SemanticModelUnavailableError("local_snapshot_missing")
        if error is None
        else error
    )

    assert cli_semantic._semantic_index_failure(execution, failure, print_output=True) == 2
    output = capsys.readouterr().out
    assert ("--semantic-model-cache" in output) is expects_model_hint
    if not expects_model_hint:
        assert "HINT" not in output
