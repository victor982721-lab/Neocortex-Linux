"""Catalog publications retain bindings with one staging write per document."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.deduplication import snapshot_path
from neocortex.documents import document_catalog as catalog
from neocortex.documents.document_resource_binding import parse_resource_binding
from neocortex.foundation.file_identity import FileIdentity


def _source(tmp_path: Path, count: int) -> tuple[Path, Path]:
    root = tmp_path / "corpus"
    root.mkdir()
    database = tmp_path / "docx.sqlite3"
    initialize_docx_state(database)
    with sqlite3.connect(database) as connection:
        for number in range(count):
            path = root / f"{number:04d}.docx"
            path.write_bytes(b"synthetic document")
            snapshot = snapshot_path(path)
            connection.execute(
                """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
                integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
                updated_ns,title,author) VALUES(?,?,?,?,?,?,'complete','valid',?,?,?,?,?,?,?)""",
                (
                    f"{snapshot.volume_id}:{snapshot.file_id}", snapshot.path,
                    snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns, "fixture-v1",
                    zlib.compress(b"IEEE switchgear standard"), 23, "fixture-text-v1",
                    1, 1, "IEEE C37.20.2", "",
                ),
            )
    return root, database


def _bindings(database: Path) -> dict[str, str]:
    with catalog.document_catalog_database(database, readonly=True) as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT path,resource_binding_json FROM documents WHERE active=1 ORDER BY path"
            )
        }


def _trace_catalog(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statements: list[str] = []
    original = catalog.connect_document_catalog

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(catalog, "connect_document_catalog", traced)
    return statements


def _assert_no_binding_updates(statements: list[str]) -> None:
    assert not any(
        statement.startswith("UPDATE catalog_generation_documents SET resource_binding_json=")
        for statement in statements
    )


def test_cold_delta_and_replay_preserve_bindings_without_extra_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _source(tmp_path, 40)
    database = tmp_path / "catalog.sqlite3"
    statements = _trace_catalog(monkeypatch)
    cold = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert cold.classified == 40
    before = _bindings(database)
    assert len(before) == 40
    for path, raw in before.items():
        binding = parse_resource_binding(raw)
        snapshot = snapshot_path(path)
        assert binding["representation_kind"] == "physical_file"
        assert binding["physical_anchor_path"] == path
        assert binding["physical_identity"]["packed_key"] == FileIdentity(
            snapshot.volume_id, snapshot.file_id,
        ).packed_key
    _assert_no_binding_updates(statements)

    statements.clear()
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET title='Changed title' WHERE path=?", (str(root / "0000.docx"),))
    delta = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert (delta.classified, delta.cache_hits) == (1, 39)
    assert _bindings(database) == before
    _assert_no_binding_updates(statements)

    replay = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert replay.publication_state == "unchanged"
    assert replay.generation_id == delta.generation_id
    assert replay.cache_hits == 40
    assert _bindings(database) == before
    _assert_no_binding_updates(statements)


def test_error_missing_source_and_recovery_keep_the_physical_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _source(tmp_path, 1)
    database = tmp_path / "catalog.sqlite3"
    statements = _trace_catalog(monkeypatch)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET text_zlib=?", (b"invalid-zlib",))
    failed = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert failed.errors == 1
    bindings = _bindings(database)
    binding = parse_resource_binding(next(iter(bindings.values())))
    assert binding["physical_anchor_path"] == str(root / "0000.docx")

    missing = catalog.update_document_catalog_source(
        database, tmp_path / "missing.sqlite3", "docx", source_root=root,
    )
    assert missing.source_missing and missing.publication_state == "unavailable"
    assert _bindings(database) == bindings

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET text_zlib=?", (zlib.compress(b"IEEE switchgear standard"),))
    recovered = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert recovered.classified == 1 and recovered.errors == 0
    assert _bindings(database) == bindings
    _assert_no_binding_updates(statements)


def test_missing_cached_binding_and_renamed_source_rebuild_the_binding(tmp_path: Path) -> None:
    root, source = _source(tmp_path, 1)
    database = tmp_path / "catalog.sqlite3"
    first = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    bindings = _bindings(database)
    with catalog.document_catalog_database(database) as connection:
        connection.execute("UPDATE documents SET resource_binding_json=NULL")
        connection.commit()
    rebuilt = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert (rebuilt.classified, rebuilt.cache_hits) == (1, 0)
    assert _bindings(database) == bindings

    renamed = root / "renamed.docx"
    (root / "0000.docx").rename(renamed)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET path=?", (str(renamed),))
    moved = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert (moved.classified, moved.cache_hits) == (1, 0)
    current = _bindings(database)
    assert set(current) == {str(renamed)}
    assert parse_resource_binding(current[str(renamed)])["physical_anchor_path"] == str(renamed)
    with catalog.document_catalog_database(database, readonly=True) as connection:
        historical = connection.execute(
            "SELECT path,resource_binding_json FROM catalog_generation_documents WHERE generation_id=?",
            (first.generation_id,),
        ).fetchall()
    assert {str(row[0]): str(row[1]) for row in historical} == bindings
