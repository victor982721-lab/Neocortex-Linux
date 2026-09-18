"""Regression coverage for Code owner identity normalization in the catalog."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.code.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
)
from neocortex.code.code_state import CodeState
from neocortex.deduplication import snapshot_path
from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
    update_document_catalog_source,
)
from neocortex.documents.document_resource_binding import parse_resource_binding
from neocortex.semantic.semantic_models import fingerprint_bytes, fingerprint_text


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


def test_catalog_consumes_the_current_code_state_and_replays(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    text = "def current_owner():\n    return True\n"
    source.write_text(text, encoding="utf-8")
    snapshot = snapshot_path(source)
    text_fingerprint = fingerprint_text(text)
    raw_fingerprint = fingerprint_bytes(text.encode("utf-8"))
    owner = tmp_path / "code.sqlite3"
    catalog = tmp_path / "catalog.sqlite3"
    with CodeState(owner) as state:
        state.store_analysis(
            CodeAnalysis(
                input=CodeFileInput(
                    snapshot, text, text.encode("utf-8"), "utf-8",
                    ArtifactClassification("python", ArtifactKind.SOURCE, 1.0, ("fixture",)),
                    "catalog-current-owner-fixture",
                ),
                status=AnalysisStatus.COMPLETE,
                analyzer_id="fixture", analyzer_version="1", parser_kind="fixture",
                text_xxh3_128=text_fingerprint.xxh3_128,
                text_xxh3_64_guard=text_fingerprint.xxh3_64_guard,
                normalized_xxh3_128=text_fingerprint.xxh3_128,
                token_xxh3_128=None, structure_xxh3_128=None,
                raw_xxh3_128=raw_fingerprint.xxh3_128,
                raw_xxh3_64_guard=raw_fingerprint.xxh3_64_guard,
            ),
            1,
        )
        assert state.connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "9"

    first = update_document_catalog_source(catalog, owner, "code", source_root=tmp_path)
    replay = update_document_catalog_source(catalog, owner, "code", source_root=tmp_path)
    assert (first.candidates, first.classified, first.source_stale) == (1, 1, 0)
    assert replay.publication_state == "unchanged"
    assert replay.generation_id == first.generation_id
    assert replay.cache_hits == 1
    with document_catalog_database(catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT volume_id,file_id,path,catalog_status,resource_binding_json "
            "FROM documents WHERE active=1"
        ).fetchone()
    assert tuple(row[:4]) == (
        str(snapshot.volume_id), str(snapshot.file_id), str(source), "review",
    )
    assert parse_resource_binding(row[4])["physical_anchor_path"] == str(source)


@pytest.mark.parametrize("verify_source_paths", (False, True))
def test_code_v9_numeric_hex_is_never_reinterpreted_as_matching_decimal(
    tmp_path: Path, verify_source_paths: bool,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("def current_owner(): pass\n", encoding="utf-8")
    observed = source.stat()
    assert (int(str(observed.st_dev), 16), int(str(observed.st_ino), 16)) != (
        observed.st_dev, observed.st_ino,
    )
    owner = tmp_path / "code.sqlite3"
    _make_hex_code_owner(owner, source)
    with sqlite3.connect(owner) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES('schema_version','9')")
        connection.execute(
            "UPDATE files SET volume_id=?,physical_file_id=?",
            (str(observed.st_dev), str(observed.st_ino)),
        )
    catalog = tmp_path / "catalog.sqlite3"
    result = update_document_catalog_source(
        catalog, owner, "code", verify_source_paths=verify_source_paths,
    )
    with document_catalog_database(catalog, readonly=True) as connection:
        rows = connection.execute("SELECT volume_id,file_id FROM documents WHERE active=1").fetchall()
    if verify_source_paths:
        assert (result.classified, result.source_stale) == (0, 1)
        assert rows == []
    else:
        assert (result.classified, result.source_stale) == (1, 0)
        assert [tuple(row) for row in rows] == [
            (str(int(str(observed.st_dev), 16)), str(int(str(observed.st_ino), 16))),
        ]


@pytest.mark.parametrize(
    ("version", "message"),
    (("10", "Code owner schema is unsupported"), ("6", "identity encoding requires owner or physical evidence")),
)
def test_catalog_still_rejects_future_schema_and_ambiguous_legacy_code_identity(
    tmp_path: Path, version: str, message: str,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("def legacy_owner(): pass\n", encoding="utf-8")
    owner = tmp_path / "code.sqlite3"
    _make_hex_code_owner(owner, source)
    with sqlite3.connect(owner) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES('schema_version',?)", (version,))
        connection.execute("UPDATE files SET volume_id='10',physical_file_id='20'")
    catalog = tmp_path / "catalog.sqlite3"
    with pytest.raises(RuntimeError, match=message):
        update_document_catalog_source(catalog, owner, "code", verify_source_paths=False)
    with document_catalog_database(catalog, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM catalog_publications").fetchone()[0] == 0
