from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence import sqlite_writer_snapshot
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    capture_sqlite_read_fence,
    immutable_sqlite_database,
)
from neocortex.persistence.sqlite_writer_snapshot import (
    SQLiteProgressConnection,
    writer_coordinated_sqlite_snapshot,
)


def _owner_identity(path: Path) -> tuple[int, int]:
    identity = path.lstat()
    return identity.st_dev, identity.st_ino


def _create_owner(path: Path, *, factory=sqlite3.Connection) -> sqlite3.Connection:
    connection = sqlite3.connect(path, factory=factory)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE probe(value INTEGER NOT NULL, payload BLOB)")
    with connection:
        connection.execute("INSERT INTO probe VALUES(7, zeroblob(2000))")
    return connection


def _source_bytes(path: Path) -> tuple[object, dict[str, str]]:
    fence = capture_sqlite_read_fence(path)
    return fence, {
        suffix: hashlib.sha256(Path(f"{path}{suffix}").read_bytes()).hexdigest()
        for suffix in ("", *(item[0] for item in fence.sidecars))
    }


@pytest.mark.parametrize("control", ("deadline", "cancelled", "raising"))
def test_projection_source_sql_is_interrupted_and_owner_is_reusable(
    tmp_path: Path, control: str
) -> None:
    path = tmp_path / "framework.sqlite3"
    visited = 0
    now = 0.0
    control_error = KeyboardInterrupt("fixture cancellation")

    def visit(value: int) -> int:
        nonlocal visited, now
        visited += 1
        if visited >= 10:
            now = 2.0
        return value

    def cancelled() -> bool:
        if control == "raising" and visited >= 10:
            raise control_error
        return control == "cancelled" and visited >= 10

    def projection(source, target, budget) -> None:
        target.execute("CREATE TABLE projection(value INTEGER)")
        source.execute(
            """WITH RECURSIVE numbers(n) AS (
                VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<10000
            ) SELECT SUM(visit(n)) FROM numbers"""
        ).fetchone()
        budget.checkpoint()

    with closing(_create_owner(path, factory=SQLiteProgressConnection)) as owner:
        owner.create_function("visit", 1, visit)
        previous_timeout = owner.execute("PRAGMA busy_timeout").fetchone()[0]
        error_type = KeyboardInterrupt if control == "raising" else SQLiteSnapshotBudgetExceeded
        with pytest.raises(error_type) as raised:
            with writer_coordinated_sqlite_snapshot(
                owner,
                path,
                owner_identity=_owner_identity(path),
                projection=projection,
                temp_root=tmp_path,
                budget=SQLiteSnapshotBudget(
                    prepare_timeout_seconds=1.0 if control == "deadline" else 60.0,
                    monotonic_clock=lambda: now,
                    cancellation_check=cancelled,
                ),
            ):
                pytest.fail("an interrupted source must not publish its projection")
        assert 10 <= visited < 1000
        if control == "raising":
            assert raised.value is control_error
        assert not owner.in_transaction
        assert owner.execute("PRAGMA busy_timeout").fetchone()[0] == previous_timeout
        assert owner._progress_registration == (None, 0)
        # A fresh read and snapshot prove that cancellation did not poison the
        # borrowed owner or leave a source handler bound to the expired budget.
        assert owner.execute("SELECT value FROM probe").fetchone() == (7,)
        with writer_coordinated_sqlite_snapshot(
            owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
        ) as snapshot:
            assert snapshot.exists()
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_projection_composes_and_restores_the_owner_progress_handler(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    calls = 0

    def owner_progress() -> int:
        nonlocal calls
        calls += 1
        return 0

    query = """WITH RECURSIVE numbers(n) AS (
        VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<100
    ) SELECT SUM(n) FROM numbers"""

    def projection(source, target, _budget) -> None:
        before = calls
        assert source.execute(query).fetchone() == (5050,)
        assert calls > before
        target.execute("CREATE TABLE projection(value INTEGER)")

    with closing(_create_owner(path, factory=SQLiteProgressConnection)) as owner:
        owner.set_progress_handler(owner_progress, 250)
        with writer_coordinated_sqlite_snapshot(
            owner,
            path,
            owner_identity=_owner_identity(path),
            projection=projection,
            temp_root=tmp_path,
        ):
            assert owner._progress_registration == (owner_progress, 250)
            before = calls
            owner.execute(query).fetchone()
            assert calls > before
        assert owner._progress_registration == (owner_progress, 250)
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_untracked_projection_owner_is_rejected_without_replacing_its_handler(
    tmp_path: Path,
) -> None:
    path = tmp_path / "framework.sqlite3"
    calls = 0

    def owner_progress() -> int:
        nonlocal calls
        calls += 1
        return 0

    with closing(_create_owner(path)) as owner:
        owner.set_progress_handler(owner_progress, 1)
        with pytest.raises(ImmutableSQLiteUnavailable, match="progress-aware owner"):
            with writer_coordinated_sqlite_snapshot(
                owner,
                path,
                owner_identity=_owner_identity(path),
                projection=lambda *_: pytest.fail("untracked callback was borrowed"),
                temp_root=tmp_path,
            ):
                pytest.fail("untracked callback was borrowed")
        before = calls
        assert owner.execute("SELECT value FROM probe").fetchone() == (7,)
        assert calls > before
        assert not owner.in_transaction
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


@pytest.mark.parametrize("error_type", (KeyboardInterrupt, RuntimeError, sqlite3.OperationalError))
def test_projection_preserves_an_owner_callback_exception_and_restores_control(
    tmp_path: Path, error_type: type[BaseException],
) -> None:
    path = tmp_path / "framework.sqlite3"
    active = False
    error = error_type("owner control")

    def owner_progress() -> int:
        if active:
            raise error
        return 0

    def projection(source, target, _budget) -> None:
        nonlocal active
        target.execute("CREATE TABLE projection(value INTEGER)")
        active = True
        source.execute("SELECT value FROM probe").fetchone()

    with closing(_create_owner(path, factory=SQLiteProgressConnection)) as owner:
        owner.set_progress_handler(owner_progress, 1)
        try:
            with pytest.raises(error_type) as raised:
                with writer_coordinated_sqlite_snapshot(
                    owner,
                    path,
                    owner_identity=_owner_identity(path),
                    projection=projection,
                    temp_root=tmp_path,
                ):
                    pytest.fail("the owner cancellation must stop the projection")
            assert raised.value is error
        finally:
            active = False
        assert owner._progress_registration == (owner_progress, 1)
        assert not owner.in_transaction
        assert owner.execute("SELECT value FROM probe").fetchone() == (7,)
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_snapshot_reads_wal_commits_and_opens_no_source_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "framework.sqlite3"
    with closing(_create_owner(path)) as owner:
        original_connect = sqlite3.connect
        opened: list[Path] = []

        def connect_only_temporary(database, *args, **kwargs):
            candidate = Path(database)
            assert candidate != path
            opened.append(candidate)
            return original_connect(database, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite_writer_snapshot.sqlite3, "connect", connect_only_temporary)
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ) as snapshot:
                assert opened == [snapshot]
                assert not Path(f"{snapshot}-wal").exists()
                assert not Path(f"{snapshot}-shm").exists()
                patch.setattr(sqlite_writer_snapshot.sqlite3, "connect", original_connect)
                before = _source_bytes(path)
                for _ in range(3):
                    with immutable_sqlite_database(snapshot) as reader:
                        assert reader.execute("SELECT value FROM probe").fetchall()[0][0] == 7
                        with pytest.raises(sqlite3.OperationalError, match="readonly"):
                            reader.execute("DELETE FROM probe")
                assert _source_bytes(path) == before
                assert not owner.in_transaction
        assert not snapshot.exists()


def test_writer_snapshot_rejects_tiny_budget_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    with closing(_create_owner(path)) as owner:
        with pytest.raises(SQLiteSnapshotBudgetExceeded, match="temporary bytes"):
            with writer_coordinated_sqlite_snapshot(
                owner,
                path,
                owner_identity=_owner_identity(path),
                temp_root=tmp_path,
                budget=SQLiteSnapshotBudget(max_temporary_bytes=1),
            ):
                pytest.fail("a tiny budget must not publish a snapshot")
    assert not any(candidate.name.startswith("neocortex-route-snapshot-") for candidate in tmp_path.iterdir())


def test_pinned_backup_excludes_concurrent_wal_commits(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    start_writer = threading.Event()
    written = threading.Event()
    failures: list[BaseException] = []

    class ObservedOwner(sqlite3.Connection):
        def backup(self, target, *, progress, **kwargs):
            def while_copying(status, remaining, total):
                progress(status, remaining, total)
                if remaining and not start_writer.is_set():
                    start_writer.set()
                    assert written.wait(10)

            return super().backup(target, progress=while_copying, **kwargs)

    def writer() -> None:
        try:
            assert start_writer.wait(10)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("INSERT INTO probe VALUES(99, zeroblob(2000))")
        except BaseException as exc:
            failures.append(exc)
        finally:
            written.set()

    with closing(_create_owner(path, factory=ObservedOwner)) as owner:
        with owner:
            owner.executemany(
                "INSERT INTO probe VALUES(8, zeroblob(2000))", (() for _ in range(900))
            )
        background = threading.Thread(target=writer)
        background.start()
        try:
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ) as snapshot:
                with immutable_sqlite_database(snapshot) as reader:
                    assert reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 901
                    assert (
                        reader.execute("SELECT COUNT(*) FROM probe WHERE value=99").fetchone()[0]
                        == 0
                    )
                assert owner.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 902
        finally:
            start_writer.set()
            background.join(timeout=10)
        assert not background.is_alive()
        assert not failures
        assert not snapshot.exists()


def test_snapshot_excludes_other_writers_uncommitted_wal(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    with closing(_create_owner(path)) as owner, closing(sqlite3.connect(path)) as writer:
        writer.execute("INSERT INTO probe VALUES(88, NULL)")
        with writer_coordinated_sqlite_snapshot(
            owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
        ) as snapshot:
            with immutable_sqlite_database(snapshot) as reader:
                assert reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
        assert writer.in_transaction
        writer.rollback()


@pytest.mark.parametrize("write", (False, True))
def test_pending_owner_transaction_is_rejected_without_rollback(
    tmp_path: Path, write: bool
) -> None:
    path = tmp_path / "framework.sqlite3"
    with closing(_create_owner(path)) as owner:
        owner.execute("BEGIN")
        if write:
            owner.execute("INSERT INTO probe VALUES(8, NULL)")
        with pytest.raises(ImmutableSQLiteUnavailable, match="idle owner transaction"):
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ):
                pytest.fail("an uncommitted owner cannot publish a snapshot")
        assert owner.in_transaction
        assert owner.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1 + int(write)
        owner.rollback()
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


@pytest.mark.parametrize("replace_after_backup", (False, True))
def test_physical_owner_replacement_is_rejected(tmp_path: Path, replace_after_backup: bool) -> None:
    path = tmp_path / "framework.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"

    class ReplacingOwner(sqlite3.Connection):
        def backup(self, target, **kwargs):
            result = super().backup(target, **kwargs)
            replacement.replace(path)
            return result

    factory = ReplacingOwner if replace_after_backup else sqlite3.Connection
    with closing(_create_owner(path, factory=factory)) as owner:
        identity = _owner_identity(path)
        replacement.write_bytes(path.read_bytes())
        if not replace_after_backup:
            replacement.replace(path)
        with pytest.raises(ImmutableSQLiteUnavailable, match="owner identity changed"):
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=identity, temp_root=tmp_path
            ):
                pytest.fail("a replaced owner cannot publish a snapshot")
        assert not owner.in_transaction
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_corrupt_schema_never_publishes_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    with closing(_create_owner(path)) as owner:
        with owner:
            owner.execute("PRAGMA writable_schema=ON")
            owner.execute("UPDATE sqlite_schema SET rootpage=9999999 WHERE name='probe'")
        with pytest.raises((sqlite3.DatabaseError, ImmutableSQLiteUnavailable)):
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ):
                pytest.fail("a corrupt snapshot cannot be published")
        assert not owner.in_transaction
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_backup_timeout_rolls_back_only_its_read_transaction_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "framework.sqlite3"
    now = [0.0]

    class ExpiringOwner(sqlite3.Connection):
        def backup(self, target, **kwargs):
            now[0] = 2.0
            return super().backup(target, **kwargs)

    with closing(_create_owner(path, factory=ExpiringOwner)) as owner:
        monkeypatch.setattr(sqlite_writer_snapshot.time, "monotonic", lambda: now[0])
        with pytest.raises(ImmutableSQLiteUnavailable, match="time budget"):
            with writer_coordinated_sqlite_snapshot(
                owner,
                path,
                owner_identity=_owner_identity(path),
                timeout_seconds=1,
                temp_root=tmp_path,
            ):
                pytest.fail("a timed-out partial snapshot cannot be published")
        assert not owner.in_transaction
        assert owner.execute("SELECT value FROM probe").fetchone()[0] == 7
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_read_lock_acquisition_uses_the_remaining_budget_and_restores_timeout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "framework.sqlite3"
    locked = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def blocker() -> None:
        try:
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("BEGIN EXCLUSIVE")
                locked.set()
                assert release.wait(5)
                writer.rollback()
        except BaseException as exc:
            failures.append(exc)

    with closing(_create_owner(path)) as owner:
        owner.execute("PRAGMA journal_mode=DELETE")
        owner.execute("PRAGMA busy_timeout=2000")
        background = threading.Thread(target=blocker)
        background.start()
        try:
            assert locked.wait(5)
            started = time.monotonic()
            with pytest.raises(ImmutableSQLiteUnavailable) as raised:
                with writer_coordinated_sqlite_snapshot(
                    owner,
                    path,
                    owner_identity=_owner_identity(path),
                    timeout_seconds=0.05,
                    temp_root=tmp_path,
                ):
                    pytest.fail("a blocked source cannot exceed its read-lock acquisition budget")
            elapsed = time.monotonic() - started
            assert elapsed < 0.4
            assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
            assert not owner.in_transaction
            assert owner.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
        finally:
            release.set()
            background.join(timeout=5)
        assert not background.is_alive()
        assert not failures
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_unrelated_connection_is_rejected_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    with closing(_create_owner(path)), closing(_create_owner(tmp_path / "other.sqlite3")) as other:
        with pytest.raises(ImmutableSQLiteUnavailable, match="does not own source"):
            with writer_coordinated_sqlite_snapshot(
                other, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ):
                pytest.fail("a connection for another owner cannot publish this source")
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_integrity_timeout_has_typed_error_and_sqlite_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "framework.sqlite3"
    now = [0.0]

    class ExpiringTarget(sqlite3.Connection):
        def set_progress_handler(self, callback, steps, /):
            if callback is not None:
                now[0] = 2.0
                steps = 1
            return super().set_progress_handler(callback, steps)

    with closing(_create_owner(path)) as owner:
        original_connect = sqlite3.connect
        monkeypatch.setattr(sqlite_writer_snapshot.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(
            sqlite_writer_snapshot.sqlite3,
            "connect",
            lambda database: original_connect(database, factory=ExpiringTarget),
        )
        with pytest.raises(ImmutableSQLiteUnavailable, match="time budget") as raised:
            with writer_coordinated_sqlite_snapshot(
                owner,
                path,
                owner_identity=_owner_identity(path),
                timeout_seconds=1,
                temp_root=tmp_path,
            ):
                pytest.fail("an interrupted integrity check cannot publish a snapshot")
        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        assert raised.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        assert not owner.in_transaction
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


@pytest.mark.parametrize("stage", ("begin", "backup"))
def test_sqlite_errors_are_typed_without_losing_their_cause(tmp_path: Path, stage: str) -> None:
    path = tmp_path / "framework.sqlite3"
    failure = sqlite3.OperationalError("fixture owner busy")

    class FailingOwner(sqlite3.Connection):
        fail_stage: str | None = None
        rollbacks = 0

        def execute(self, sql, *args, **kwargs):
            if sql == "BEGIN" and self.fail_stage == "begin":
                raise failure
            return super().execute(sql, *args, **kwargs)

        def backup(self, target, **kwargs):
            if self.fail_stage == "backup":
                raise failure
            return super().backup(target, **kwargs)

        def rollback(self):
            self.rollbacks += 1
            return super().rollback()

    with closing(_create_owner(path, factory=FailingOwner)) as owner:
        owner.fail_stage = stage
        with pytest.raises(ImmutableSQLiteUnavailable, match="fixture owner busy") as raised:
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ):
                pytest.fail("a failed SQLite operation cannot publish a snapshot")
        assert raised.value.__cause__ is failure
        assert not owner.in_transaction
        assert owner.rollbacks == int(stage == "backup")
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


@pytest.mark.parametrize("primary_is_sqlite", (False, True))
def test_rollback_error_never_masks_the_primary_failure(
    tmp_path: Path, primary_is_sqlite: bool
) -> None:
    path = tmp_path / "framework.sqlite3"
    failure = (
        sqlite3.OperationalError("primary backup failure")
        if primary_is_sqlite
        else RuntimeError("unexpected primary failure")
    )

    class FailedCleanupOwner(sqlite3.Connection):
        def backup(self, target, **kwargs):
            raise failure

        def rollback(self):
            raise sqlite3.OperationalError("secondary cleanup failure")

    with closing(_create_owner(path, factory=FailedCleanupOwner)) as owner:
        try:
            with pytest.raises(
                ImmutableSQLiteUnavailable if primary_is_sqlite else RuntimeError
            ) as raised:
                with writer_coordinated_sqlite_snapshot(
                    owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
                ):
                    pytest.fail("a failed backup cannot publish a snapshot")
            if primary_is_sqlite:
                assert raised.value.__cause__ is failure
            else:
                assert raised.value is failure
            assert any("secondary cleanup failure" in note for note in raised.value.__notes__)
        finally:
            sqlite3.Connection.rollback(owner)
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))


def test_temporary_cleanup_does_not_mask_snapshot_body_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "framework.sqlite3"
    real_rmtree = sqlite_writer_snapshot.shutil.rmtree

    def failing_rmtree(path_arg, *args, **kwargs):
        real_rmtree(path_arg, *args, **kwargs)
        raise RuntimeError("injected snapshot temporary cleanup failure")

    monkeypatch.setattr(sqlite_writer_snapshot.shutil, "rmtree", failing_rmtree)
    with closing(_create_owner(path)) as owner:
        with pytest.raises(RuntimeError, match="primary snapshot body failure") as raised:
            with writer_coordinated_sqlite_snapshot(
                owner, path, owner_identity=_owner_identity(path), temp_root=tmp_path
            ):
                raise RuntimeError("primary snapshot body failure")
        assert any(
            "injected snapshot temporary cleanup failure" in note
            for note in raised.value.__notes__
        )
    assert not list(tmp_path.glob("neocortex-route-snapshot-*"))
