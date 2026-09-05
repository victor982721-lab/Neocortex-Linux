from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence.sqlite_connection import (
    READONLY_EXISTING,
    SQLiteConnectionPolicy,
    connect_sqlite,
)
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadMode,
    SQLiteReadSession,
    capture_sqlite_read_fence,
    open_immutable_sqlite_connection,
    open_sidecar_safe_sqlite_connection,
    preferred_sqlite_read_mode,
)
from neocortex.workflow import state_health


def _database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe VALUES(7)")


def _policy_reader(path: Path) -> sqlite3.Connection:
    return connect_sqlite(
        path,
        mode=READONLY_EXISTING,
        policy=SQLiteConnectionPolicy(label="audit fixture"),
    )


@pytest.mark.parametrize(
    "opener",
    [open_immutable_sqlite_connection, open_sidecar_safe_sqlite_connection, _policy_reader],
    ids=["immutable-bare", "sidecar-safe-bare", "readonly-policy"],
)
@pytest.mark.parametrize("mutation", ["main", "sidecar", "removed", "replaced", "symlink"])
def test_bare_reader_close_detects_source_drift(
    tmp_path: Path,
    opener: Callable[[Path], sqlite3.Connection],
    mutation: str,
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    reader = opener(path)
    assert reader.execute("SELECT value FROM probe").fetchone()[0] == 7
    if mutation == "main":
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    elif mutation == "sidecar":
        Path(f"{path}-wal").write_bytes(b"")
    elif mutation == "removed":
        path.unlink()
    else:
        replacement = tmp_path / "replacement.sqlite3"
        replacement.write_bytes(path.read_bytes())
        if mutation == "symlink":
            path.unlink()
            path.symlink_to(replacement)
        else:
            replacement.replace(path)

    with pytest.raises(ImmutableSQLiteUnavailable, match="changed during immutable read"):
        reader.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reader.execute("SELECT 1")
    reader.close()  # Closing an already-closed handle is still idempotent.


@pytest.mark.parametrize(
    "opener",
    [open_immutable_sqlite_connection, open_sidecar_safe_sqlite_connection, _policy_reader],
)
def test_bare_reader_normal_close_is_byte_neutral(
    tmp_path: Path, opener: Callable[[Path], sqlite3.Connection]
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    before = capture_sqlite_read_fence(path), path.read_bytes()
    with closing(opener(path)) as reader:
        assert reader.execute("SELECT value FROM probe").fetchone()[0] == 7
    assert (capture_sqlite_read_fence(path), path.read_bytes()) == before


def test_empty_wal_live_writer_is_not_immutable_or_healthy(tmp_path: Path) -> None:
    path = tmp_path / "framework.sqlite3"
    _database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        assert Path(f"{path}-wal").stat().st_size == 0
        assert Path(f"{path}-shm").stat().st_size == 32_768
        before = capture_sqlite_read_fence(path)
        contents = {
            candidate: candidate.read_bytes()
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        }

        assert preferred_sqlite_read_mode(path) is SQLiteReadMode.SNAPSHOT_TEMP
        with pytest.raises(ImmutableSQLiteUnavailable, match="not proven inactive"):
            with SQLiteReadSession(path):
                pytest.fail("a zero-length WAL does not prove writer quiescence")
        result = state_health.inspect_state_health(tmp_path)
        owner = next(item for item in result.owners if item.name == "framework")
        assert owner.status == "blocked"
        assert "not proven inactive" in (owner.detail or "")
        assert capture_sqlite_read_fence(path) == before
        assert all(candidate.read_bytes() == value for candidate, value in contents.items())


@pytest.mark.parametrize("opener", [open_sidecar_safe_sqlite_connection, _policy_reader])
def test_empty_wal_writer_uses_detached_snapshot_without_touching_source(
    tmp_path: Path, opener: Callable[[Path], sqlite3.Connection]
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        before = capture_sqlite_read_fence(path)
        contents = {
            candidate: candidate.read_bytes()
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        }
        reader = opener(path)
        try:
            copied_path = Path(reader.execute("PRAGMA database_list").fetchone()[2])
            assert copied_path != path
            assert reader.execute("SELECT value FROM probe").fetchone()[0] == 7
            assert capture_sqlite_read_fence(path) == before
            assert all(candidate.read_bytes() == value for candidate, value in contents.items())
            # Subsequent owner writes do not invalidate an already detached snapshot.
            writer.execute("INSERT INTO probe VALUES(8)")
            writer.commit()
            after_write = capture_sqlite_read_fence(path)
            assert reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
        finally:
            reader.close()
        assert not copied_path.exists()
        assert capture_sqlite_read_fence(path) == after_write


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_unknown_orphan_sidecar_is_reported_without_opening_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    sidecar = tmp_path / f"rogue.sqlite3{suffix}"
    sidecar.write_bytes(b"orphan")
    monkeypatch.setattr(
        state_health,
        "immutable_sqlite_database",
        lambda *_args, **_kwargs: pytest.fail("absent owners must not be opened"),
    )
    result = state_health.inspect_state_health(tmp_path)
    owner = next(item for item in result.owners if item.name == "unknown:rogue.sqlite3")
    assert owner.status == "orphaned_sidecars"
    assert result.orphaned_sidecar_count == 1
    assert owner.sidecars == (state_health.SQLiteSidecarHealth(suffix, 6),)
    assert sidecar.read_bytes() == b"orphan"
    assert not (tmp_path / "rogue.sqlite3").exists()


def test_unknown_sidecars_group_once_and_do_not_duplicate_known_owners(tmp_path: Path) -> None:
    for name in ("rogue.sqlite3", "framework.sqlite3"):
        for suffix in ("-wal", "-shm", "-journal"):
            (tmp_path / f"{name}{suffix}").write_bytes(b"")
    result = state_health.inspect_state_health(tmp_path)
    rogue = [item for item in result.owners if item.name == "unknown:rogue.sqlite3"]
    assert len(rogue) == 1
    assert len(rogue[0].sidecars) == 3
    assert result.orphaned_sidecar_count == 2
    assert len(result.owners) == len(state_health.STATE_OWNER_DATABASES) + 1


class _InjectedAbort(BaseException):
    pass


def test_snapshot_bare_handle_configuration_abort_closes_both_handles_and_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    configured: list[sqlite3.Connection] = []
    copies: list[Path] = []
    configure = sqlite_immutable._configure_read_connection

    def abort_second(
        connection: sqlite3.Connection, *, timeout_seconds: float, label: str
    ) -> sqlite3.Connection:
        configured.append(connection)
        copies.append(Path(connection.execute("PRAGMA database_list").fetchone()[2]))
        if len(configured) == 2:
            raise _InjectedAbort("snapshot facade configuration interrupted")
        return configure(connection, timeout_seconds=timeout_seconds, label=label)

    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO probe VALUES(8)")
        writer.commit()
        before = capture_sqlite_read_fence(path)
        monkeypatch.setattr(sqlite_immutable, "_configure_read_connection", abort_second)
        with pytest.raises(_InjectedAbort, match="configuration interrupted"):
            open_sidecar_safe_sqlite_connection(path)
        assert len(configured) == 2
        for connection in configured:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        assert copies[0] == copies[1]
        assert all(not copy.parent.exists() for copy in copies)
        assert capture_sqlite_read_fence(path) == before


def test_snapshot_body_abort_closes_handle_and_cleans_temp_without_source_effects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        before = capture_sqlite_read_fence(path)
        session = SQLiteReadSession(path, mode=SQLiteReadMode.SNAPSHOT_TEMP, temp_root=tmp_path)
        with pytest.raises(_InjectedAbort, match="body interrupted"):
            with session as reader:
                copy = session.temporary_database
                assert copy is not None
                raise _InjectedAbort("body interrupted")
        assert not copy.parent.exists()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            reader.execute("SELECT 1")
        assert capture_sqlite_read_fence(path) == before


def test_immutable_session_second_close_does_not_observe_later_owner_changes(tmp_path: Path) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    session = SQLiteReadSession(path)
    with session as reader:
        assert reader.execute("SELECT value FROM probe").fetchone()[0] == 7
    path.unlink()
    session.close()


def test_immutable_close_fence_abort_still_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    reader = open_sidecar_safe_sqlite_connection(path)

    def abort_fence(*_args: object) -> None:
        raise _InjectedAbort("source verification interrupted")

    monkeypatch.setattr(sqlite_immutable, "_verify_immutable_source", abort_fence)
    with pytest.raises(_InjectedAbort, match="source verification interrupted"):
        reader.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reader.execute("SELECT 1")
    reader.close()
