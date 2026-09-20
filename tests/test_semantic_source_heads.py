"""Compact source-head projections for no-work Semantic replay."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from neocortex.semantic.semantic_sources import (
    _source_head_query,
    _update_head_digest,
    semantic_source_heads,
)


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')


def _pdf_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                processing_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                last_seen_run_id INTEGER,
                is_partial INTEGER NOT NULL,
                normalized_text_xxh3_128 TEXT,
                normalized_text_chars INTEGER NOT NULL
            );
            CREATE TABLE pages(
                file_key TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                source TEXT NOT NULL,
                text_zlib BLOB NOT NULL,
                text_chars INTEGER NOT NULL,
                PRIMARY KEY(file_key,page_number)
            );"""
        )
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("file:1", "/corpus/a.pdf", "pdf-v1", "done", 20, 10, 5, 1, 0, "a" * 32, 8),
        )
        connection.execute(
            "INSERT INTO pages VALUES(?,?,?,?,?)",
            ("file:1", 1, "text", b"compressed-is-never-read-by-head", 8),
        )


def test_source_head_excludes_observation_clocks_but_tracks_semantic_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    _pdf_state(database)

    first = semantic_source_heads(tmp_path, ("pdf",))[0]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE documents SET last_seen_run_id=2")
    observation_only = semantic_source_heads(tmp_path, ("pdf",))[0]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE documents SET normalized_text_xxh3_128=?",
            ("b" * 32,),
        )
    changed = semantic_source_heads(tmp_path, ("pdf",))[0]

    assert first.complete
    assert first.row_count == 1
    assert observation_only.digest == first.digest
    assert changed.digest != first.digest


@pytest.mark.parametrize(
    ("source_kind", "expected_fragment", "parameters"),
    (
        ("pdf", "JOIN pages", ()),
        ("docx", "JOIN document_parts", ()),
        ("xlsx", "format=?", ("xlsx",)),
        ("pptx", "format=?", ("pptx",)),
        ("odt", "format=?", ("odt",)),
        ("audio", "JOIN segments", ()),
        ("archive", "JOIN containers", ()),
    ),
)
def test_source_head_queries_cover_each_supported_owner_projection(
    source_kind: str,
    expected_fragment: str,
    parameters: tuple[str, ...],
) -> None:
    with sqlite3.connect(":memory:") as connection:
        query, observed_parameters = _source_head_query(connection, source_kind)

    assert expected_fragment in query
    assert observed_parameters == parameters


def test_text_source_head_projection_supports_legacy_and_revision_owners() -> None:
    with sqlite3.connect(":memory:") as legacy:
        legacy.execute("CREATE TABLE documents(file_key TEXT)")
        legacy_query, _ = _source_head_query(legacy, "text")
    with sqlite3.connect(":memory:") as revisioned:
        revisioned.execute("CREATE TABLE documents(file_key TEXT,revision_id TEXT)")
        revisioned.execute("CREATE TABLE text_input_revisions(revision_id TEXT)")
        revisioned_query, _ = _source_head_query(revisioned, "text")

    assert "NULL,NULL,NULL" in legacy_query
    assert "text_input_revisions" not in legacy_query
    assert "LEFT JOIN text_input_revisions" in revisioned_query
    assert "materialization.materialization_id" in revisioned_query


def test_source_head_digest_frames_all_sqlite_value_domains() -> None:
    values = (None, b"1", memoryview(b"1"), 1, 1.0, "1")
    digests = []
    for value in values:
        hasher = hashlib.sha256()
        _update_head_digest(hasher, value)
        digests.append(hasher.hexdigest())

    assert len(set(digests)) == len(values) - 1  # bytes and memoryview are equivalent


def test_missing_and_invalid_source_heads_fail_closed(tmp_path: Path) -> None:
    missing = semantic_source_heads(tmp_path, ("docx",))[0]

    assert missing.complete is False
    assert missing.reason == "OperationalError"
    with pytest.raises(ValueError, match="source head kinds are invalid"):
        semantic_source_heads(tmp_path, ())
    with pytest.raises(ValueError, match="source head kinds are invalid"):
        semantic_source_heads(tmp_path, ("unknown",))
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(ValueError, match="unsupported semantic text source"):
            _source_head_query(connection, "unknown")
