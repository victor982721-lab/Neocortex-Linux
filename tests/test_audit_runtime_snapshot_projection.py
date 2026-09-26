"""Runtime/persistence audit regressions for bounded SQLite owner views."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadMode,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    immutable_sqlite_database,
)
from neocortex.persistence.sqlite_writer_snapshot import (
    SQLiteProgressConnection,
    writer_coordinated_sqlite_snapshot,
)


def _owner_bytes(path: Path) -> dict[str, tuple[int, str]]:
    return {
        candidate.name: (candidate.stat().st_size, hashlib.sha256(candidate.read_bytes()).hexdigest())
        for candidate in path.parent.glob(f"{path.name}*")
        if candidate.is_file()
    }


def test_snapshot_rejects_a_symlinked_owner_ancestor_before_copy(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-owner"
    linked_parent = tmp_path / "linked-owner"
    snapshot_root = tmp_path / "snapshots"
    real_parent.mkdir()
    snapshot_root.mkdir()
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    database = real_parent / "owner.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe VALUES(7)")

    with pytest.raises(ImmutableSQLiteUnavailable, match="symlink"):
        with SQLiteReadSession(
            linked_parent / database.name,
            mode=SQLiteReadMode.SNAPSHOT_TEMP,
            temp_root=snapshot_root,
        ):
            pytest.fail("a linked owner ancestor must not enter the snapshot copier")

    assert not tuple(snapshot_root.glob("neocortex-sqlite-read-*"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO fixtures require POSIX")
def test_snapshot_rejects_a_fifo_owner_without_blocking(tmp_path: Path) -> None:
    owner = tmp_path / "owner.sqlite3"
    os.mkfifo(owner)

    with pytest.raises(ImmutableSQLiteUnavailable, match="regular"):
        with SQLiteReadSession(owner, mode=SQLiteReadMode.SNAPSHOT_TEMP):
            pytest.fail("a FIFO owner must be rejected before any blocking open")


def test_writer_projection_reads_small_heads_without_copying_a_large_wal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "owner.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE heads(model TEXT PRIMARY KEY,generation INTEGER) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            (("schema", "1"),),
        )
        connection.execute("INSERT INTO heads VALUES('model',1)")

    owner = sqlite3.connect(database, factory=SQLiteProgressConnection)
    try:
        owner.execute("PRAGMA journal_mode=WAL")
        owner.execute("PRAGMA wal_autocheckpoint=0")
        owner.execute(
            "INSERT INTO metadata(key,value) VALUES('large-fixture',?)",
            ("x" * (8 * 1024 * 1024),),
        )
        owner.commit()
        source_before = _owner_bytes(database)
        assert sum(size for size, _digest in source_before.values()) > 256 * 1024
        source_main_and_wal = {
            name: value
            for name, value in source_before.items()
            if name != f"{database.name}-shm"
        }

        def projection(source, target, budget) -> None:
            target.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID"
            )
            target.execute(
                "CREATE TABLE heads(model TEXT PRIMARY KEY,generation INTEGER) WITHOUT ROWID"
            )
            metadata = source.execute(
                "SELECT key,value FROM metadata WHERE key='schema'"
            ).fetchall()
            budget.before_write(8192)
            target.executemany("INSERT INTO metadata VALUES(?,?)", metadata)
            heads = source.execute("SELECT model,generation FROM heads").fetchall()
            budget.before_write(8192)
            target.executemany("INSERT INTO heads VALUES(?,?)", heads)
            budget.checkpoint()

        owner_identity = (database.stat().st_dev, database.stat().st_ino)
        with writer_coordinated_sqlite_snapshot(
            owner,
            database,
            owner_identity=owner_identity,
            projection=projection,
            temp_root=tmp_path,
            budget=SQLiteSnapshotBudget(
                max_temporary_bytes=256 * 1024,
                prepare_timeout_seconds=30.0,
            ),
        ) as snapshot:
            with immutable_sqlite_database(snapshot) as reader:
                assert [tuple(row) for row in reader.execute("SELECT * FROM metadata")] == [
                    ("schema", "1")
                ]
                assert [tuple(row) for row in reader.execute("SELECT * FROM heads")] == [
                    ("model", 1)
                ]

        # The owned source connection may advance SQLite's WAL read marks in
        # SHM while pinning its transaction.  The durable main/WAL bytes and
        # their identities remain untouched by the projection itself.
        assert {
            name: value
            for name, value in _owner_bytes(database).items()
            if name != f"{database.name}-shm"
        } == source_main_and_wal
        assert not tuple(tmp_path.glob("neocortex-route-snapshot-*"))
    finally:
        owner.close()
