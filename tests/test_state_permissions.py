"""Focused owner-only permission contracts for durable SQLite state."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from neocortex.capabilities.formats.image.state import connect_image_state
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.sqlite_connection import (
    READWRITE_CREATE,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)
from neocortex.semantic.semantic_schema import semantic_database


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _assert_private_layout(path: Path) -> None:
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        assert sidecar.is_file()
        assert _mode(sidecar) == 0o600


@contextmanager
def _with_umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def test_generic_sqlite_state_is_private_under_owner_masking_umask(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "generic.sqlite3"
    policy = SQLiteConnectionPolicy(
        label="permission fixture",
        writer_pragmas=SQLiteWriterPragmas(journal_mode="WAL"),
    )
    with _with_umask(0o777):
        connection = connect_sqlite(database, mode=READWRITE_CREATE, policy=policy)
        try:
            connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
            connection.execute("INSERT INTO probe VALUES(1)")
            connection.commit()
            _assert_private_layout(database)
        finally:
            connection.close()


def test_framework_image_and_semantic_state_use_private_sidecars(tmp_path: Path) -> None:
    with _with_umask(0o777):
        framework_path = tmp_path / "framework" / "framework.sqlite3"
        framework = FrameworkState(framework_path)
        try:
            _assert_private_layout(framework_path)
        finally:
            framework.close()

        image_path = tmp_path / "image" / "image.sqlite3"
        image = connect_image_state(image_path)
        try:
            image.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
            image.commit()
            _assert_private_layout(image_path)
        finally:
            image.close()

        semantic_path = tmp_path / "semantic" / "semantic.sqlite3"
        with semantic_database(semantic_path) as semantic:
            semantic.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
            semantic.commit()
            _assert_private_layout(semantic_path)


def test_existing_state_permissions_are_not_rewritten(tmp_path: Path) -> None:
    database = tmp_path / "existing" / "state.sqlite3"
    with _with_umask(0):
        database.parent.mkdir(mode=0o750)
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
            connection.execute("INSERT INTO probe VALUES(1)")
            connection.commit()
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    for candidate in database.parent.iterdir():
        candidate.chmod(0o640)
    before = {candidate.name: _mode(candidate) for candidate in database.parent.iterdir()}

    policy = SQLiteConnectionPolicy(
        label="existing permission fixture",
        writer_pragmas=SQLiteWriterPragmas(journal_mode="WAL"),
    )
    with _with_umask(0o777):
        connection = connect_sqlite(database, mode=READWRITE_CREATE, policy=policy)
        try:
            assert _mode(database.parent) == 0o750
            assert {
                candidate.name: _mode(candidate)
                for candidate in database.parent.iterdir()
            } == before
        finally:
            connection.close()
