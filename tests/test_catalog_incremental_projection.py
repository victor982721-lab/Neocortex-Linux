"""Incremental publication releases only the active paths that must change."""

from __future__ import annotations

import os
import sqlite3
import zlib
from pathlib import Path

import pytest

from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.deduplication import snapshot_path
from neocortex.documents import document_catalog as catalog
from neocortex.documents.document_catalog_replay import current_projection_matches
from neocortex.documents.document_catalog_schema import _GENERATION_DIGEST_COLUMNS
from neocortex.documents.document_resource_binding import parse_resource_binding
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken


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


def _projection(database: Path) -> tuple[int, str, tuple[tuple[object, ...], ...]]:
    with catalog.document_catalog_database(database, readonly=True) as connection:
        manifest = catalog.read_catalog_publication_manifest(connection, "docx")
        columns = ",".join(_GENERATION_DIGEST_COLUMNS)
        current = tuple(map(tuple, connection.execute(
            f"SELECT {columns} FROM documents WHERE source_kind='docx' AND active=1 "
            "ORDER BY source_kind,file_key",
        )))
        published = tuple(map(tuple, connection.execute(
            f"SELECT {columns} FROM catalog_generation_documents "
            "WHERE generation_id=? ORDER BY source_kind,file_key", (manifest.generation_id,),
        )))
        assert current == published
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for row in connection.execute(
            "SELECT path,resource_binding_json FROM documents WHERE active=1",
        ):
            assert parse_resource_binding(row[1])["physical_anchor_path"] == row[0]
        return manifest.generation_id, manifest.generation_digest, current


def test_delta_and_retirement_avoid_retiring_stable_rows_and_preserve_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _source(tmp_path, 24)
    database = tmp_path / "catalog.sqlite3"
    original = catalog._replace_catalog_projection
    writes: list[int] = []

    def measure(connection, *args, **kwargs):
        before = connection.total_changes
        original(connection, *args, **kwargs)
        writes.append(connection.total_changes - before)

    monkeypatch.setattr(catalog, "_replace_catalog_projection", measure)
    first = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert first.classified == 24
    assert writes == [48]  # Current rows and first classification history.
    before = _projection(database)
    replay = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert replay.publication_state == "unchanged"
    assert _projection(database) == before
    assert writes == [48]

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET title='Changed title' WHERE path=?", (str(root / "0000.docx"),))
    delta = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert (delta.classified, delta.cache_hits) == (1, 23)
    assert writes[-1] == 24
    assert _projection(database)[0] == delta.generation_id
    with catalog.document_catalog_database(database, readonly=True) as connection:
        assert connection.execute(
            "SELECT DISTINCT last_seen_catalog_run_id FROM documents WHERE active=1",
        ).fetchall()[0][0] == delta.catalog_run_id
        assert connection.execute(
            "SELECT 1 FROM documents AS current JOIN catalog_generation_documents AS staged "
            "USING(source_kind,file_key) WHERE staged.generation_id=? "
            "AND (current.updated_ns<>staged.updated_ns "
            "OR current.last_seen_catalog_run_id<>staged.last_seen_catalog_run_id)",
            (delta.generation_id,),
        ).fetchone() is None

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET status='failed' WHERE path=?", (str(root / "0001.docx"),))
    retired = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert (retired.cache_hits, retired.stale_marked) == (23, 1)
    assert writes[-1] == 24  # One retirement and 23 refreshed current rows.
    assert len(_projection(database)[2]) == 23

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET status='complete' WHERE path=?", (str(root / "0001.docx"),))
    restored = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert (restored.cache_hits, restored.stale_marked) == (24, 0)
    assert writes[-1] == 24
    assert len(_projection(database)[2]) == 24


def _swap_source_paths(source: Path, root: Path) -> None:
    first, second, holding = root / "0000.docx", root / "0001.docx", root / "holding.docx"
    first.rename(holding)
    second.rename(first)
    holding.rename(second)
    with sqlite3.connect(source) as connection:
        for old, new in ((first, holding), (second, first), (holding, second)):
            connection.execute("UPDATE documents SET path=? WHERE path=?", (str(new), str(old)))


@pytest.mark.parametrize("failure", (None, "error", "cancel"))
def test_swapped_paths_publish_atomically_and_roll_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    root, source = _source(tmp_path, 3)
    database = tmp_path / "catalog.sqlite3"
    catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    before = _projection(database)
    _swap_source_paths(source, root)
    token = CancellationToken()
    original = catalog._replace_catalog_projection

    def fail_after_projection(*args, **kwargs):
        original(*args, **kwargs)
        if failure == "error":
            raise RuntimeError("injected failure after swapped projection")
        if failure == "cancel":
            token.cancel()

    monkeypatch.setattr(catalog, "_replace_catalog_projection", fail_after_projection)
    if failure is None:
        result = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
        assert (result.classified, result.cache_hits, result.stale_marked) == (2, 1, 0)
        assert _projection(database)[0] == result.generation_id
        with catalog.document_catalog_database(database, readonly=True) as connection:
            for row in connection.execute("SELECT file_id,path FROM documents WHERE active=1"):
                assert int(row[0]) == Path(row[1]).stat().st_ino
    else:
        with pytest.raises(RuntimeError if failure == "error" else CancellationRequested):
            catalog.update_document_catalog_source(
                database, source, "docx", source_root=root, cancellation=token,
            )
        assert _projection(database) == before


def test_cross_source_path_collision_retires_the_previous_owner(tmp_path: Path) -> None:
    root, source = _source(tmp_path, 2)
    database = tmp_path / "catalog.sqlite3"
    pdf = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(pdf)
    snapshot = snapshot_path(root / "0000.docx")
    with sqlite3.connect(pdf) as connection:
        connection.execute(
            """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,normalized_text_xxh3_128,metadata_json,updated_ns)
            VALUES(?,?,?,?,?,'pdf-fixture','done','pdf-text','{}',1)""",
            (f"{snapshot.volume_id}:{snapshot.file_id}", snapshot.path, snapshot.size,
             snapshot.mtime_ns, snapshot.birthtime_ns),
        )
    catalog.update_document_catalog_source(database, pdf, "pdf", source_root=root)
    result = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert len(_projection(database)[2]) == 2
    with catalog.document_catalog_database(database, readonly=True) as connection:
        assert connection.execute("SELECT active FROM documents WHERE source_kind='pdf'").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM documents WHERE active=1 AND last_seen_catalog_run_id=?",
            (result.catalog_run_id,),
        ).fetchone()[0] == 2


def test_backdated_source_change_after_preparation_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _source(tmp_path, 2)
    database = tmp_path / "catalog.sqlite3"
    catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    before = _projection(database)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET title='requested change'")
    original = catalog._prepare_catalog_publication

    def mutate_after_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        metadata = source.stat()
        with sqlite3.connect(source) as connection:
            connection.execute("UPDATE documents SET title='concurrent drift'")
        os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        assert source.stat().st_mtime_ns == metadata.st_mtime_ns
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", mutate_after_prepare)
    with pytest.raises(catalog.CatalogSourceDrift):
        catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert _projection(database) == before


def _except_matches(connection: sqlite3.Connection, generation_id: int) -> bool:
    columns = ",".join(_GENERATION_DIGEST_COLUMNS)
    current = f"SELECT {columns} FROM documents WHERE source_kind='docx' AND active=1"
    published = f"SELECT {columns} FROM catalog_generation_documents WHERE generation_id=?"
    return all(connection.execute(query, (generation_id,)).fetchone() is None for query in (
        f"SELECT 1 FROM ({current} EXCEPT {published}) LIMIT 1",
        f"SELECT 1 FROM ({published} EXCEPT {current}) LIMIT 1",
    ))


def test_replay_comparison_checks_every_field_and_both_key_sets(tmp_path: Path) -> None:
    root, source = _source(tmp_path, 2)
    database = tmp_path / "catalog.sqlite3"
    result = catalog.update_document_catalog_source(database, source, "docx", source_root=root)
    assert result.generation_id is not None
    generation_id = result.generation_id
    with catalog.document_catalog_database(database) as connection:
        assert current_projection_matches(connection, generation_id, "docx")
        originals = connection.execute("SELECT * FROM documents ORDER BY file_key").fetchall()
        key = originals[0]["file_key"]
        # Every digest field is observed, including NULL, physical identities,
        # bindings, status, JSON and the exact Linux path case.
        for column in _GENERATION_DIGEST_COLUMNS:
            previous = originals[0][column]
            replacement = (
                0 if column == "active"
                else previous + 1 if isinstance(previous, (int, float))
                else "changed-field" if previous is None
                else str(previous) + "-changed"
            )
            connection.execute("SAVEPOINT changed_field")
            connection.execute(f"UPDATE documents SET {column}=? WHERE file_key=?", (replacement, key))
            assert current_projection_matches(connection, generation_id, "docx") is False, column
            assert _except_matches(connection, generation_id) is False, column
            connection.execute("ROLLBACK TO changed_field")
            connection.execute("RELEASE changed_field")

        for mutation in (
            "DELETE FROM documents",
            "UPDATE documents SET active=0",
            "UPDATE documents SET source_kind='foreign'",
            "UPDATE documents SET file_key=file_key||'-new'",
            "UPDATE documents SET path=upper(path)",
            "UPDATE documents SET confidence=CAST(confidence AS BLOB)",
            "UPDATE documents SET primary_organization=0",
        ):
            connection.execute("SAVEPOINT changed_projection")
            connection.execute(mutation)
            assert current_projection_matches(connection, generation_id, "docx") is False, mutation
            assert _except_matches(connection, generation_id) is False, mutation
            connection.execute("ROLLBACK TO changed_projection")
            connection.execute("RELEASE changed_projection")
        assert current_projection_matches(connection, generation_id, "docx")


@pytest.mark.parametrize("current_value,published_value,matches", (
    (None, None, True), (None, "", False),
    (1, 1.0, True), (1, "1", False),
    (b"1", "1", False), ("Case", "case", False),
))
def test_replay_comparison_preserves_except_storage_semantics(
    current_value: object, published_value: object, matches: bool,
) -> None:
    # A deliberately affinity-free value column makes int/text and blob/text
    # distinctions observable. Keys retain the canonical unique topology.
    columns = ",".join(_GENERATION_DIGEST_COLUMNS)
    declaration = ",".join(
        f"{column} TEXT" if column in {"source_kind", "file_key"} else column
        for column in _GENERATION_DIGEST_COLUMNS
    )
    with sqlite3.connect(":memory:") as connection:
        connection.execute(f"CREATE TABLE documents({declaration},PRIMARY KEY(source_kind,file_key)) WITHOUT ROWID")
        connection.execute(
            f"CREATE TABLE catalog_generation_documents(generation_id INTEGER,{declaration},"
            "PRIMARY KEY(generation_id,source_kind,file_key)) WITHOUT ROWID",
        )
        values: dict[str, object] = dict.fromkeys(_GENERATION_DIGEST_COLUMNS)
        values.update(source_kind="docx", file_key="1:2", active=1, confidence=current_value)
        placeholders = ",".join("?" for _ in values)
        connection.execute(f"INSERT INTO documents({columns}) VALUES({placeholders})", tuple(values.values()))
        values["confidence"] = published_value
        connection.execute(
            f"INSERT INTO catalog_generation_documents(generation_id,{columns}) VALUES(1,{placeholders})",
            tuple(values.values()),
        )
        assert _except_matches(connection, 1) is matches
        assert current_projection_matches(connection, 1, "docx") is matches
