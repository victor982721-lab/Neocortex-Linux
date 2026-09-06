"""Regression coverage for Code owner identity normalization in the catalog."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
    update_document_catalog_source,
)


def _make_hex_code_owner(path: Path, source: Path) -> None:
    stat = source.stat()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                file_id INTEGER PRIMARY KEY,
                volume_id TEXT NOT NULL,
                physical_file_id TEXT NOT NULL,
                current_path TEXT NOT NULL,
                current_version_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE file_versions(
                version_id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                analysis_status TEXT NOT NULL,
                processing_signature TEXT NOT NULL,
                language TEXT,
                artifact_kind TEXT,
                text_xxh3_128 TEXT,
                text_truncated INTEGER NOT NULL,
                text_zlib BLOB,
                provenance_json TEXT NOT NULL
            );
            CREATE TABLE code_chunks(
                chunk_id INTEGER PRIMARY KEY,
                version_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (
                1,
                format(stat.st_dev, "x"),
                format(stat.st_ino, "x"),
                str(source),
                1,
                "current",
            ),
        )
        connection.execute(
            "INSERT INTO file_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                1,
                stat.st_size,
                stat.st_mtime_ns,
                -1,
                "complete",
                "code-route-v3-fixture",
                "python",
                "source",
                "code-text-fingerprint",
                0,
                zlib.compress(b"def status(): return 'U2'"),
                "{}",
            ),
        )


def test_code_catalog_accepts_hex_owner_identity_and_replays(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def status(): return 'U2'\n", encoding="utf-8")
    owner = tmp_path / "code.sqlite3"
    catalog = tmp_path / "document_catalog.sqlite3"
    _make_hex_code_owner(owner, source)
    initialize_document_catalog(catalog)

    first = update_document_catalog_source(catalog, owner, "code", verify_source_paths=True)
    second = update_document_catalog_source(catalog, owner, "code", verify_source_paths=True)

    assert (first.candidates, first.classified, first.cache_hits, first.source_stale) == (
        1,
        1,
        0,
        0,
    )
    assert (second.candidates, second.classified, second.cache_hits, second.source_stale) == (
        1,
        0,
        1,
        0,
    )
    with document_catalog_database(catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT source_kind,path,catalog_status FROM documents WHERE active=1"
        ).fetchone()
    assert tuple(row) == ("code", str(source), "review")
