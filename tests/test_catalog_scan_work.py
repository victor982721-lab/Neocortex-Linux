"""Catalog scans seek bounded pages and read one Code owner declaration."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from neocortex.documents import document_catalog as catalog
from neocortex.documents.document_catalog_schema import create_document_catalog_schema
from neocortex.documents.document_resource_binding import ResourceBindingError
from tests.test_document_catalog_multimodal import _make_code_owner


def _catalog_rows(connection: sqlite3.Connection, root: Path, count: int) -> None:
    """Populate the canonical schema with advisory-only legacy bindings."""

    create_document_catalog_schema(connection)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(documents)")]
    template = {
        row[1]: (0 if row[2] in {"INTEGER", "REAL"} else "") if row[3] else None
        for row in connection.execute("PRAGMA table_info(documents)")
    }
    template.update(source_kind="docx", active=1, catalog_status="classified")
    placeholders = ",".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO documents({','.join(columns)}) VALUES({placeholders})",
        (
            tuple({**template, "file_key": f"{number:08d}", "path": str(root / f"{number}.docx")}[column] for column in columns)
            for number in range(count)
        ),
    )
    connection.commit()


def test_scope_paging_work_grows_with_rows_not_already_consumed_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    original_scope = catalog._catalog_path_in_scope
    work = []
    for count in (2048, 8192):
        seen: list[str] = []

        def observed_scope(path, scope, seen=seen):
            seen.append(path)
            return original_scope(path, scope)

        monkeypatch.setattr(catalog, "_catalog_path_in_scope", observed_scope)
        with closing(sqlite3.connect(tmp_path / f"catalog-{count}.sqlite3")) as connection:
            connection.row_factory = sqlite3.Row
            _catalog_rows(connection, root, count)
            steps = 0

            def progress() -> int:
                nonlocal steps
                steps += 100
                return 0

            connection.set_progress_handler(progress, 100)
            try:
                catalog._preserve_catalog_outside_scope(
                    connection, catalog.CatalogBuild(1, 1, "docx", None), root,
                )
            finally:
                connection.set_progress_handler(None, 0)
            work.append(steps)
            assert seen == [str(root / f"{number}.docx") for number in range(count)]
            assert connection.execute("SELECT COUNT(*) FROM catalog_generation_documents").fetchone()[0] == 0
    # Quadrupling a scan must not replay every earlier page. Allow ample
    # headroom for schema/query-planner variation; the old query exceeds 10x.
    assert 0 < work[1] < work[0] * 6, work


def test_scope_pages_preserve_exact_outside_rows_and_empty_first_key(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "other"
    with closing(sqlite3.connect(tmp_path / "catalog.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        _catalog_rows(connection, root, 600)
        connection.execute("UPDATE documents SET file_key='' WHERE file_key='00000000'")
        connection.execute("UPDATE documents SET path=? WHERE file_key=''", (str(outside / "empty.docx"),))
        connection.execute("UPDATE documents SET path=? WHERE file_key='00000256'", (str(outside / "é.docx"),))
        connection.execute("UPDATE documents SET path=? WHERE file_key='00000599'", (str(outside / "z.docx"),))
        connection.execute("UPDATE documents SET active=0 WHERE file_key='00000256'")
        connection.commit()
        columns = ",".join(catalog._CATALOG_DOCUMENT_COLUMNS)
        expected = tuple(map(tuple, connection.execute(
            f"SELECT {columns} FROM documents WHERE file_key IN ('','00000599') ORDER BY file_key",
        )))
        catalog._preserve_catalog_outside_scope(
            connection, catalog.CatalogBuild(1, 1, "docx", None), root,
        )
        actual = tuple(map(tuple, connection.execute(
            f"SELECT {columns} FROM catalog_generation_documents WHERE generation_id=1 ORDER BY file_key",
        )))
        assert actual == expected


def _code_rows(tmp_path: Path, count: int) -> Path:
    source = tmp_path / "first.py"
    source.write_text("print('owner fixture')\n", encoding="utf-8")
    path = tmp_path / "code.sqlite3"
    _make_code_owner(path, source, hex_identity=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO files SELECT ?,volume_id,physical_file_id,?,current_version_id,status "
            "FROM files WHERE file_id=1",
            ((number, str(tmp_path / f"{number}.py")) for number in range(2, count + 1)),
        )
    return path


def test_code_owner_declaration_is_read_once_per_source_observation(tmp_path: Path) -> None:
    path = _code_rows(tmp_path, 300)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        first = tuple(catalog._iter_source_documents(connection, "code"))
        connection.set_trace_callback(None)
        assert len(first) == 300
        owner_queries = [sql for sql in statements if "sqlite_master" in sql or "FROM metadata" in sql]
        assert len(owner_queries) == 2
        # A later observation must read the declaration anew, even on the
        # same connection. No per-connection or process cache owns this proof.
        connection.execute("UPDATE metadata SET value='unsupported' WHERE key='schema_version'")
        connection.commit()
        with pytest.raises(ResourceBindingError, match="schema is unsupported"):
            tuple(catalog._iter_source_documents(connection, "code"))


@pytest.mark.parametrize("schema", (None, "1", "6", "7", "8", "9", "unsupported", "duplicate"))
def test_code_observation_preserves_direct_identity_codec_validation(
    tmp_path: Path, schema: str | None,
) -> None:
    path = _code_rows(tmp_path, 1)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        if schema is None:
            connection.execute("DROP TABLE metadata")
        elif schema == "duplicate":
            connection.execute("DROP TABLE metadata")
            connection.execute("CREATE TABLE metadata(key TEXT,value TEXT)")
            connection.executemany("INSERT INTO metadata VALUES('schema_version',?)", (("7",), ("7",)))
        else:
            connection.execute("UPDATE metadata SET value=?", (schema,))
        connection.commit()
        row = connection.execute("SELECT * FROM files").fetchone()
        assert row is not None
        try:
            expected = catalog._code_source_identity(connection, row, verify_source_paths=True)
        except ResourceBindingError:
            with pytest.raises(ResourceBindingError):
                tuple(catalog._iter_source_documents(connection, "code", verify_source_paths=True))
        else:
            documents = tuple(catalog._iter_source_documents(connection, "code", verify_source_paths=True))
            assert len(documents) == 1
            assert (documents[0].volume_id, documents[0].file_id) == expected.decimal_components
