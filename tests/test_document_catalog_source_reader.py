"""Fenced source-reader regressions for the document catalog."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.documents import document_catalog as catalog
from neocortex.foundation.file_identity import FileIdentityError
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    SQLiteReadMode,
    SQLiteSnapshotBudgetExceeded,
    preferred_sqlite_read_mode,
)


def _open_text_wal_source(path: Path, *, file_key: str = "1:2") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE documents(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        status TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        text_xxh3_128 TEXT,
        title TEXT,
        author TEXT,
        metadata_json TEXT NOT NULL
        )"""
    )
    assert str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,status,processing_signature,
        text_xxh3_128,title,author,metadata_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (file_key, "/tmp/catalog-source.txt", 1, 1, 1, "complete", "fixture", "fp", "", "", "{}"),
    )
    connection.commit()
    return connection


def test_catalog_reads_a_live_wal_source_within_snapshot_budget(tmp_path: Path) -> None:
    source = tmp_path / "text.sqlite3"
    writer = _open_text_wal_source(source)
    try:
        assert preferred_sqlite_read_mode(source) is SQLiteReadMode.SNAPSHOT_TEMP
        with catalog._readonly_source(source) as connection:
            documents = tuple(catalog._iter_source_documents(connection, "text"))
        assert len(documents) == 1
        assert documents[0].file_key == "1:2"
    finally:
        writer.close()


def test_catalog_preserves_snapshot_budget_error_without_generic_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "text.sqlite3"
    writer = _open_text_wal_source(source)
    try:
        # Keep the fixture small while exercising the same bounded preflight
        # as the canonical 256 MiB production budget.
        monkeypatch.setattr(sqlite_immutable, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 64 * 1024)
        with source.open("ab") as handle:
            handle.truncate(128 * 1024)
        with pytest.raises(SQLiteSnapshotBudgetExceeded, match="temporary bytes") as raised:
            with catalog._readonly_source(source):
                pytest.fail("the oversized active WAL owner must not be copied")
        assert raised.value.reason == "temporary_bytes"
        assert "catalog source reader unavailable" not in str(raised.value)
        assert raised.value.__cause__ is None
    finally:
        writer.close()


def test_catalog_preserves_source_identity_error_without_generic_wrapper(tmp_path: Path) -> None:
    source = tmp_path / "text.sqlite3"
    writer = _open_text_wal_source(source, file_key="malformed")
    try:
        with pytest.raises(FileIdentityError, match="exactly one ':' separator") as raised:
            with catalog._readonly_source(source) as connection:
                next(catalog._iter_source_documents(connection, "text"))
        assert raised.value.__cause__ is None
        assert "catalog source reader unavailable" not in str(raised.value)
    finally:
        writer.close()


def test_catalog_preserves_reader_exception_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "text.sqlite3"
    writer = _open_text_wal_source(source)
    cause = OSError("fixture source detail")
    failure = ValueError("fixture reader failure")

    def broken_reader(*_args: object, **_kwargs: object):
        raise failure from cause

    monkeypatch.setattr(catalog, "_iter_source_documents", broken_reader)
    try:
        with pytest.raises(ValueError) as raised:
            with catalog._readonly_source(source) as connection:
                next(catalog._iter_source_documents(connection, "text"))
        assert raised.value is failure
        assert raised.value.__cause__ is cause
    finally:
        writer.close()
