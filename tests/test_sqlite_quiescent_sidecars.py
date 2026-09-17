"""Regression coverage for quiescent SQLite sidecars.

These fixtures deliberately live below ``tmp_path``.  In particular, the
residual ``-wal``/``-shm`` pair is recreated after SQLite has closed its last
handle so the tests exercise the same on-disk shape as a closed owner without
touching NeoCortex's real state.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import subprocess
import sys
from typing import cast

import pytest

from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadMode,
    SQLiteReadSession,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
    require_inactive_sqlite_sidecars,
)
from neocortex.workflow.retention import planner as retention_module
from neocortex.workflow.retention.planner import RetentionPolicy, plan_retention
from neocortex.workflow import state_health


def _database(path: Path) -> Path:
    """Create a small real SQLite owner with data that can be read back."""

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        connection.executemany("INSERT INTO probe VALUES(?)", [(7,), (8,)])
    return path


def _sidecar(path: Path, suffix: str) -> Path:
    return Path(f"{path}{suffix}")


def _install_quiescent_residual_sidecars(path: Path) -> None:
    """Install the canonical closed-owner WAL=0/SHM=32 KiB residue.

    First obtain real SHM bytes from SQLite, including its normal header, then
    close the last connection (which commonly unlinks both sidecars).  The
    saved regular SHM file is restored together with an empty WAL.  This keeps
    the fixture realistic while ensuring no process owns the files.
    """

    saved_shm: bytes | None = None
    with closing(sqlite3.connect(path)) as connection:
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        assert journal_mode.lower() == "wal"
        # BEGIN IMMEDIATE creates SQLite's normal SHM header without requiring
        # any schema-specific table or changing the fixture's durable bytes.
        connection.execute("BEGIN IMMEDIATE")
        shm = _sidecar(path, "-shm")
        if shm.exists():
            saved_shm = shm.read_bytes()
            assert len(saved_shm) == 32_768
        connection.rollback()

    wal = _sidecar(path, "-wal")
    shm = _sidecar(path, "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(saved_shm if saved_shm is not None else b"\0" * 32_768)
    assert wal.is_file() and wal.stat().st_size == 0
    assert shm.is_file() and shm.stat().st_size == 32_768


def _open_live_wal_writer(path: Path) -> subprocess.Popen[str]:
    """Open a separate-process writer with the exact empty-WAL/32 KiB-SHM shape."""

    # F_GETLK does not necessarily report a lock held by the probing process
    # itself.  Keep the writer in a child process so a real lock probe cannot
    # accidentally classify an active owner as quiescent in this test.
    script = (
        "import sqlite3, sys\n"
        "connection = sqlite3.connect(sys.argv[1])\n"
        "assert str(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]).lower() == 'wal'\n"
        "connection.execute('BEGIN IMMEDIATE')\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "connection.rollback()\n"
        "connection.close()\n"
    )
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None
    ready = writer.stdout.readline().strip()
    if ready != "READY":
        stderr = "" if writer.stderr is None else writer.stderr.read()
        writer.kill()
        writer.wait()
        raise AssertionError(f"live SQLite writer did not become ready: {stderr}")
    wal = _sidecar(path, "-wal")
    shm = _sidecar(path, "-shm")
    assert wal.is_file() and wal.stat().st_size == 0
    assert shm.is_file() and shm.stat().st_size == 32_768
    return writer


def _close_live_wal_writer(writer: subprocess.Popen[str]) -> None:
    if writer.poll() is None:
        assert writer.stdin is not None
        writer.stdin.write("\n")
        writer.stdin.flush()
    writer.wait(timeout=5)


def _assert_readable_without_snapshot(path: Path) -> None:
    session = SQLiteReadSession(
        path,
        mode=SQLiteReadMode.IMMUTABLE_STRICT,
        max_attempts=1,
    )
    with session as connection:
        assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
        assert session.temporary_database is None


def test_no_sidecars_selects_strict_and_reads_without_snapshot(tmp_path: Path) -> None:
    path = _database(tmp_path / "owner.sqlite3")

    assert preferred_sqlite_read_mode(path) is SQLiteReadMode.IMMUTABLE_STRICT
    fence = capture_sqlite_read_fence(path)
    require_inactive_sqlite_sidecars(fence, path=path)
    _assert_readable_without_snapshot(path)


def test_empty_wal_and_canonical_shm_are_quiescent_and_zero_copy(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    _install_quiescent_residual_sidecars(path)

    assert preferred_sqlite_read_mode(path) is SQLiteReadMode.IMMUTABLE_STRICT
    fence = capture_sqlite_read_fence(path)
    require_inactive_sqlite_sidecars(fence, path=path)
    session = SQLiteReadSession(path, mode=preferred_sqlite_read_mode(path), max_attempts=1)
    with session as connection:
        assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
        assert session.temporary_database is None


def test_residual_fence_without_owner_path_fails_closed(tmp_path: Path) -> None:
    """A detached residual fence cannot claim lock quiescence by itself."""

    path = _database(tmp_path / "owner.sqlite3")
    _install_quiescent_residual_sidecars(path)
    fence = capture_sqlite_read_fence(path)
    with pytest.raises(ImmutableSQLiteUnavailable, match="owner path"):
        require_inactive_sqlite_sidecars(fence)


def test_strict_session_holds_writer_guard_until_close(tmp_path: Path) -> None:
    """A writer cannot enter the residual owner while strict read is open."""

    path = _database(tmp_path / "owner.sqlite3")
    _install_quiescent_residual_sidecars(path)
    shm = _sidecar(path, "-shm")
    script = """
import errno, fcntl, os, struct, sys
fd = os.open(sys.argv[1], os.O_RDWR)
request = struct.pack('@hhqqi', fcntl.F_WRLCK, os.SEEK_SET, 120, 1, 0)
try:
    fcntl.fcntl(fd, fcntl.F_SETLK, request)
except OSError as exc:
    print('BLOCKED' if exc.errno in (errno.EACCES, errno.EAGAIN) else 'ERROR')
else:
    print('ACQUIRED')
finally:
    os.close(fd)
"""

    with SQLiteReadSession(path, mode=SQLiteReadMode.IMMUTABLE_STRICT, max_attempts=1):
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(shm)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert blocked.stdout.strip() == "BLOCKED"
    released = subprocess.run(
        [sys.executable, "-c", script, str(shm)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert released.stdout.strip() == "ACQUIRED"


def test_sidecar_free_strict_session_holds_rollback_writer_guard(
    tmp_path: Path,
) -> None:
    """The sidecar-free layout is fenced against a writer-after-probe race."""

    path = _database(tmp_path / "owner.sqlite3")
    script = """
import errno, fcntl, os, struct, sys
fd = os.open(sys.argv[1], os.O_RDWR)
request = struct.pack('@hhqqi', fcntl.F_WRLCK, os.SEEK_SET, 0x40000000, 1, 0)
try:
    fcntl.fcntl(fd, fcntl.F_SETLK, request)
except OSError as exc:
    print('BLOCKED' if exc.errno in (errno.EACCES, errno.EAGAIN) else 'ERROR')
else:
    print('ACQUIRED')
finally:
    os.close(fd)
"""

    with SQLiteReadSession(path, mode=SQLiteReadMode.IMMUTABLE_STRICT, max_attempts=1):
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert blocked.stdout.strip() == "BLOCKED"
    released = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert released.stdout.strip() == "ACQUIRED"


def test_live_empty_wal_and_canonical_shm_use_snapshot_and_strict_fails_closed(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    writer = _open_live_wal_writer(path)
    try:
        # The filesystem shape alone is intentionally insufficient.  The
        # kernel must detect the writer's live SHM lock (or equivalent owner
        # coordination evidence) before choosing immutable_strict.
        assert preferred_sqlite_read_mode(path) is SQLiteReadMode.SNAPSHOT_TEMP
        with pytest.raises(ImmutableSQLiteUnavailable):
            with SQLiteReadSession(
                path,
                mode=SQLiteReadMode.IMMUTABLE_STRICT,
                max_attempts=1,
            ):
                pytest.fail("a live BEGIN IMMEDIATE owner must not be read immutably")
    finally:
        _close_live_wal_writer(writer)


def test_state_health_uses_central_sidecar_contract_for_quiescent_and_live_owners(
    tmp_path: Path,
) -> None:
    quiescent = _database(tmp_path / "quiescent.sqlite3")
    _install_quiescent_residual_sidecars(quiescent)
    assert state_health._sidecar_safety(quiescent) == (None, None)

    live = _database(tmp_path / "live.sqlite3")
    writer = _open_live_wal_writer(live)
    try:
        status, detail = state_health._sidecar_safety(live)
        assert status == "blocked"
        assert "not proven inactive" in (detail or "")
    finally:
        _close_live_wal_writer(writer)


def test_nonempty_wal_remains_snapshot_temp_and_source_is_untouched(tmp_path: Path) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    writer = sqlite3.connect(path)
    try:
        journal_mode = str(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        assert journal_mode.lower() == "wal"
        writer.execute("INSERT INTO probe VALUES(9)")
        writer.commit()
        wal = _sidecar(path, "-wal")
        shm = _sidecar(path, "-shm")
        assert wal.is_file() and wal.stat().st_size > 0
        assert shm.is_file() and shm.stat().st_size == 32_768
        before = capture_sqlite_read_fence(path)

        assert preferred_sqlite_read_mode(path) is SQLiteReadMode.SNAPSHOT_TEMP
        session = SQLiteReadSession(
            path,
            mode=preferred_sqlite_read_mode(path),
            temp_root=tmp_path,
            max_attempts=1,
        )
        with session as connection:
            copied = session.temporary_database
            assert copied is not None and copied != path
            assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 3
        assert not copied.exists()
        assert capture_sqlite_read_fence(path) == before
    finally:
        writer.close()


def test_nonempty_rollback_journal_is_not_quiescent(tmp_path: Path) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO probe VALUES(9)")
        journal = _sidecar(path, "-journal")
        assert journal.is_file() and journal.stat().st_size > 0

        assert preferred_sqlite_read_mode(path) is SQLiteReadMode.SNAPSHOT_TEMP
        with pytest.raises(ImmutableSQLiteUnavailable):
            with SQLiteReadSession(
                path,
                mode=SQLiteReadMode.IMMUTABLE_STRICT,
                max_attempts=1,
            ):
                pytest.fail("a non-empty rollback journal must not be read immutably")
    finally:
        writer.rollback()
        writer.close()


def test_live_rollback_writer_without_journal_is_not_strict(tmp_path: Path) -> None:
    """BEGIN IMMEDIATE must fail closed before SQLite creates its journal."""

    path = _database(tmp_path / "owner.sqlite3")
    writer = sqlite3.connect(path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert not _sidecar(path, "-journal").exists()
        assert preferred_sqlite_read_mode(path) is SQLiteReadMode.SNAPSHOT_TEMP
        with pytest.raises(ImmutableSQLiteUnavailable):
            with SQLiteReadSession(path, mode=SQLiteReadMode.IMMUTABLE_STRICT, max_attempts=1):
                pytest.fail("a rollback writer must not be read immutably")
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize(
    ("case", "install"),
    [
        ("unexpected-shm-size", lambda path: _sidecar(path, "-shm").write_bytes(b"x")),
        ("empty-wal-without-shm", lambda path: _sidecar(path, "-wal").write_bytes(b"")),
        ("isolated-shm", lambda path: _sidecar(path, "-shm").write_bytes(b"\0" * 32_768)),
    ],
    ids=["unexpected-shm-size", "empty-wal-without-shm", "isolated-shm"],
)
def test_incomplete_or_ambiguous_sidecar_sets_fail_closed(
    tmp_path: Path,
    case: str,
    install: Callable[[Path], object],
) -> None:
    path = _database(tmp_path / f"{case}.sqlite3")
    install(path)

    assert preferred_sqlite_read_mode(path) is SQLiteReadMode.SNAPSHOT_TEMP
    with pytest.raises(ImmutableSQLiteUnavailable):
        with SQLiteReadSession(
            path,
            mode=SQLiteReadMode.IMMUTABLE_STRICT,
            max_attempts=1,
        ):
            pytest.fail(f"{case} must not be treated as an immutable owner")


@pytest.mark.parametrize("kind", ["owner-symlink", "sidecar-symlink", "sidecar-directory"])
def test_symlink_and_nonregular_sqlite_paths_fail_closed(tmp_path: Path, kind: str) -> None:
    target = _database(tmp_path / "target.sqlite3")
    if kind == "owner-symlink":
        selected = tmp_path / "owner.sqlite3"
        selected.symlink_to(target)
    else:
        selected = target
        sidecar = _sidecar(selected, "-wal")
        if kind == "sidecar-symlink":
            sidecar_target = tmp_path / "sidecar-bytes"
            sidecar_target.write_bytes(b"")
            sidecar.symlink_to(sidecar_target)
        else:
            sidecar.mkdir()

    with pytest.raises(ImmutableSQLiteUnavailable):
        preferred_sqlite_read_mode(selected)


def test_unexpected_sqlite_sibling_is_not_ignored(tmp_path: Path) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    _sidecar(path, "-unexpected").write_bytes(b"ambiguous")

    with pytest.raises(ImmutableSQLiteUnavailable, match="unexpected sidecar"):
        preferred_sqlite_read_mode(path)


def test_strict_read_detects_sidecar_mutation_during_fenced_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    _install_quiescent_residual_sidecars(path)
    shm = _sidecar(path, "-shm")
    real_connect = cast(Callable[..., sqlite3.Connection], sqlite_immutable.sqlite3.connect)

    def connect_then_mutate(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        before = shm.stat()
        # Preserve inode and size while changing fenced metadata.  This is a
        # source-side mutation, not a cleanup operation, and must invalidate
        # the immutable session at its final fence.
        os.utime(shm, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        return connection

    monkeypatch.setattr(sqlite_immutable.sqlite3, "connect", connect_then_mutate)
    with pytest.raises(
        ImmutableSQLiteUnavailable,
        match=r"changed (?:during|before) immutable read|source fence|sidecar",
    ):
        with SQLiteReadSession(
            path,
            mode=SQLiteReadMode.IMMUTABLE_STRICT,
            max_attempts=1,
        ) as connection:
            assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2


def test_residual_lock_guard_is_released_when_immutable_connect_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path / "owner.sqlite3")
    _install_quiescent_residual_sidecars(path)
    real_connect = sqlite_immutable.sqlite3.connect

    def fail_immutable_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        database = str(args[0]) if args else str(kwargs.get("database", ""))
        if "immutable=1" in database:
            raise sqlite3.OperationalError("injected immutable open failure")
        return cast(Callable[..., sqlite3.Connection], real_connect)(*args, **kwargs)

    monkeypatch.setattr(sqlite_immutable.sqlite3, "connect", fail_immutable_connect)
    with pytest.raises(sqlite3.OperationalError, match="injected immutable open failure"):
        with SQLiteReadSession(path, mode=SQLiteReadMode.IMMUTABLE_STRICT, max_attempts=1):
            pytest.fail("injected immutable open must fail")
    # A second probe must still be able to acquire the guard; a leaked child
    # would retain the OFD locks and make the quiescent owner look active.
    monkeypatch.setattr(sqlite_immutable.sqlite3, "connect", real_connect)
    assert preferred_sqlite_read_mode(path) is SQLiteReadMode.IMMUTABLE_STRICT


def test_oversized_quiescent_owner_uses_zero_copy_retention_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reuse the real semantic schema so this exercises the retention planner,
    # not only a mocked owner-size calculation.
    from tests.test_retention_planner import NOW_NS, _populate_semantic

    path = tmp_path / "semantic.sqlite3"
    _populate_semantic(path)
    _install_quiescent_residual_sidecars(path)
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
    monkeypatch.setattr(
        sqlite_immutable,
        "_copy_regular_file",
        lambda *_args, **_kwargs: pytest.fail("quiescent oversized owner must not be copied"),
    )

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )

    store = plan.stores[0]
    assert store.status == "ready"
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0


def test_oversized_active_owner_is_bounded_before_temporary_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_retention_planner import NOW_NS, _populate_semantic

    path = tmp_path / "semantic.sqlite3"
    _populate_semantic(path)
    writer = _open_live_wal_writer(path)
    try:
        monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)
        monkeypatch.setattr(
            sqlite_immutable,
            "_copy_regular_file",
            lambda *_args, **_kwargs: pytest.fail("over-budget owner must fail before copying"),
        )

        plan = plan_retention(
            tmp_path,
            stores=("semantic",),
            now_ns=NOW_NS,
            policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
        )

        store = plan.stores[0]
        assert store.status == "blocked"
        assert "temporary bytes" in (store.detail or "")
        assert plan.snapshot_metrics is not None
        assert plan.snapshot_metrics["prepared_views"] == 0
        assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
        assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    finally:
        _close_live_wal_writer(writer)
