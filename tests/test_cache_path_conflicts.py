"""Regressions for cache-hit path ownership and escaped read-only SQLite URIs."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
import zlib
from contextlib import closing
from pathlib import Path

from neocortex.deduplication import FileSnapshot
from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.docx_route import DocxRoute, _file_key
from _04_Nucleo_Operativo.docx_state import (
    UNKNOWN_BIRTHTIME_NS as DOCX_UNKNOWN_BIRTHTIME_NS,
)
from _04_Nucleo_Operativo.docx_state import (
    connect_docx_state,
    docx_database,
    initialize_docx_state,
)
from _04_Nucleo_Operativo.pdf_route import PdfRoute, PdfRouteConfig
from _04_Nucleo_Operativo.pdf_route_cache import file_key
from _04_Nucleo_Operativo.pdf_state import (
    UNKNOWN_BIRTHTIME_NS as PDF_UNKNOWN_BIRTHTIME_NS,
)
from _04_Nucleo_Operativo.pdf_state import (
    connect_pdf_state,
    initialize_pdf_state,
    pdf_database,
)
from _04_Nucleo_Operativo.state import FrameworkRouteState


# region [01] Fixture helpers


def _snapshot(path: Path, file_id: int, *, birthtime_ns: int) -> FileSnapshot:
    return FileSnapshot(str(path), 1, file_id, 100, 200, birthtime_ns)


def _insert_pdf_document(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    *,
    path: str | None = None,
    birthtime_ns: int | None = None,
) -> None:
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
        status,last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,'protected',0,?)""",
        (
            file_key(snapshot),
            snapshot.path if path is None else path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns if birthtime_ns is None else birthtime_ns,
            "cache-path-test",
            time.time_ns(),
        ),
    )


def _insert_pdf_inventory(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    run_id: int,
) -> None:
    connection.execute(
        "INSERT INTO pdf_inventory VALUES(?,?,?,?,?,?)",
        (
            file_key(snapshot),
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            run_id,
        ),
    )


def _pdf_route(run_id: int) -> PdfRoute:
    route = object.__new__(PdfRoute)
    route.run_id = run_id
    route.cancellation = CancellationToken()
    return route


def _insert_docx_document(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    *,
    path: str | None = None,
    birthtime_ns: int | None = None,
) -> None:
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,'complete',0,?)""",
        (
            _file_key(snapshot),
            snapshot.path if path is None else path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns if birthtime_ns is None else birthtime_ns,
            "cache-path-test",
            time.time_ns(),
        ),
    )


def _insert_docx_inventory(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    run_id: int,
) -> None:
    connection.execute(
        "INSERT INTO docx_inventory VALUES(?,?,?,?,?,?)",
        (
            _file_key(snapshot),
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            run_id,
        ),
    )


def _docx_route(run_id: int) -> DocxRoute:
    route = object.__new__(DocxRoute)
    route.run_id = run_id
    return route


# endregion [01]


# region [02] PDF path ownership


class PdfCachePathConflictTests(unittest.TestCase):
    def test_full_touch_batch_classifies_conflicts_in_one_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "pdf.sqlite3"
            initialize_pdf_state(state)
            snapshots = [
                _snapshot(
                    root / f"target-{index:03d}.pdf",
                    1000 + index,
                    birthtime_ns=3000 + index,
                )
                for index in range(256)
            ]
            owners = [
                _snapshot(Path(snapshot.path), 2000 + index, birthtime_ns=4000 + index)
                for index, snapshot in enumerate(snapshots)
            ]
            statements: list[str] = []
            with pdf_database(state) as connection:
                for owner in owners:
                    _insert_pdf_document(connection, owner)
                connection.set_trace_callback(statements.append)
                stale_keys = _pdf_route(7)._stale_path_owner_keys(
                    connection,
                    snapshots,
                )
                connection.set_trace_callback(None)

            classification_queries = [
                statement
                for statement in statements
                if statement.lstrip().startswith("WITH incoming(path,file_key)")
            ]
            self.assertEqual(len(classification_queries), 1)
            self.assertEqual(set(stale_keys), {file_key(owner) for owner in owners})

    def test_stale_owner_is_fully_removed_and_cache_hit_rename_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            old_path = Path(temporary) / "old.pdf"
            target_path = Path(temporary) / "target.pdf"
            moved = _snapshot(target_path, 1, birthtime_ns=301)
            old = _snapshot(old_path, 1, birthtime_ns=301)
            stale = _snapshot(target_path, 2, birthtime_ns=302)
            run_id = 7
            with pdf_database(state) as connection:
                _insert_pdf_document(connection, old)
                _insert_pdf_document(connection, stale)
                _insert_pdf_inventory(connection, moved, run_id)
                connection.execute(
                    """INSERT INTO pages(
                    file_key,page_number,source,text_zlib,text_chars,profile_json)
                    VALUES(?,0,'native',?,4,NULL)""",
                    (file_key(stale), zlib.compress(b"text")),
                )
                connection.execute(
                    "INSERT INTO page_fts VALUES(?,?,0,'stale text')",
                    (file_key(stale), stale.path),
                )
                connection.execute(
                    "INSERT INTO page_fts_state VALUES(?,0,'digest')",
                    (file_key(stale),),
                )

            with pdf_database(state) as connection:
                _pdf_route(run_id)._touch_cache_hits(connection, [moved])

            with closing(sqlite3.connect(state)) as connection:
                rows = connection.execute(
                    "SELECT file_key,path FROM documents ORDER BY file_key"
                ).fetchall()
                dependent_rows = sum(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE file_key=?",
                        (file_key(stale),),
                    ).fetchone()[0]
                    for table in ("pages", "page_fts", "page_fts_state")
                )
            self.assertEqual(rows, [(file_key(moved), moved.path)])
            self.assertEqual(dependent_rows, 0)

    def test_prepare_document_removes_all_stale_path_owner_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            path = Path(temporary) / "replaced.pdf"
            stale = _snapshot(path, 1, birthtime_ns=301)
            replacement = _snapshot(path, 2, birthtime_ns=302)
            stale_key = file_key(stale)
            route = _pdf_route(8)
            route.config = PdfRouteConfig(state)
            with pdf_database(state) as connection:
                _insert_pdf_document(connection, stale)
                connection.execute(
                    """INSERT INTO pages(
                    file_key,page_number,source,text_zlib,text_chars,profile_json)
                    VALUES(?,0,'native',?,4,NULL)""",
                    (stale_key, zlib.compress(b"text")),
                )
                connection.execute(
                    "INSERT INTO page_fts VALUES(?,?,0,'stale text')",
                    (stale_key, stale.path),
                )
                connection.execute(
                    "INSERT INTO page_fts_state VALUES(?,0,'digest')",
                    (stale_key,),
                )
                connection.execute(
                    """INSERT INTO page_staging(
                    file_key,processing_signature,page_number,source,text_zlib,text_chars)
                    VALUES(?,?,0,'native',?,4)""",
                    (stale_key, "old-signature", zlib.compress(b"text")),
                )
                connection.execute(
                    "INSERT INTO page_errors VALUES(?,?,0,'OldError','old',?)",
                    (stale_key, "old-signature", time.time_ns()),
                )
                connection.execute(
                    "INSERT INTO document_warnings VALUES(?,?,'extract',1,'[]',?)",
                    (stale_key, "old-signature", time.time_ns()),
                )
                connection.execute(
                    "INSERT INTO text_signatures VALUES(?,1,'1',1,?)",
                    (stale_key, time.time_ns()),
                )

                route._prepare_document(connection, replacement, 1, {})

            with pdf_database(state, readonly=True) as connection:
                documents = connection.execute(
                    "SELECT file_key,path,status FROM documents"
                ).fetchall()
                stale_rows = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE file_key=?",
                        (stale_key,),
                    ).fetchone()[0]
                    for table in (
                        "pages",
                        "page_fts",
                        "page_fts_state",
                        "page_staging",
                        "page_errors",
                        "document_warnings",
                        "text_signatures",
                    )
                }
            self.assertEqual(
                [tuple(row) for row in documents],
                [(file_key(replacement), replacement.path, "processing")],
            )
            self.assertEqual(set(stale_rows.values()), {0})

    def test_live_owner_is_preserved_and_rename_fails_conservatively(self):
        self._assert_live_owner_is_preserved(owner_birthtime_ns=302)

    def test_live_legacy_sentinel_omitted_by_limit_is_preserved(self):
        self._assert_live_owner_is_preserved(owner_birthtime_ns=PDF_UNKNOWN_BIRTHTIME_NS)

    def _assert_live_owner_is_preserved(self, *, owner_birthtime_ns: int) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            old = _snapshot(Path(temporary) / "old.pdf", 1, birthtime_ns=301)
            moved = _snapshot(Path(temporary) / "target.pdf", 1, birthtime_ns=301)
            owner = _snapshot(Path(temporary) / "target.pdf", 2, birthtime_ns=302)
            owner_live = _snapshot(
                Path(temporary) / "owner-new-path.pdf",
                2,
                birthtime_ns=302,
            )
            run_id = 8
            with pdf_database(state) as connection:
                _insert_pdf_document(connection, old)
                _insert_pdf_document(
                    connection,
                    owner,
                    birthtime_ns=owner_birthtime_ns,
                )
                _insert_pdf_inventory(connection, moved, run_id)
                _insert_pdf_inventory(connection, owner_live, run_id)

            with pdf_database(state) as connection:
                with self.assertRaisesRegex(RuntimeError, "live inventory identity"):
                    _pdf_route(run_id)._touch_cache_hits(connection, [moved])

            with closing(sqlite3.connect(state)) as connection:
                rows = connection.execute(
                    "SELECT file_key,path,birthtime_ns FROM documents ORDER BY file_key"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (file_key(old), old.path, old.birthtime_ns),
                    (file_key(owner), owner.path, owner_birthtime_ns),
                ],
            )


# endregion [02]


# region [03] DOCX path ownership


class DocxCachePathConflictTests(unittest.TestCase):
    def test_stale_owner_and_cascades_are_removed_before_cache_hit_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "docx.sqlite3"
            initialize_docx_state(state)
            old = _snapshot(Path(temporary) / "old.docx", 1, birthtime_ns=401)
            moved = _snapshot(Path(temporary) / "target.docx", 1, birthtime_ns=401)
            stale = _snapshot(Path(temporary) / "target.docx", 2, birthtime_ns=402)
            run_id = 9
            with docx_database(state) as connection:
                _insert_docx_document(connection, old)
                _insert_docx_document(connection, stale)
                _insert_docx_inventory(connection, moved, run_id)
                connection.execute(
                    "INSERT INTO document_fts VALUES(?,?,?,?,?)",
                    (_file_key(stale), stale.path, "", "", "stale text"),
                )
                connection.execute(
                    "INSERT INTO document_parts VALUES(?,?,?,?,?,?)",
                    (_file_key(stale), "word/document.xml", "body", 0, b"x", 1),
                )
                connection.execute(
                    "INSERT INTO pdf_counterparts VALUES(?,?,?,?,?,?,?)",
                    (_file_key(stale), "", "missing", "test", 0, run_id, 1),
                )

            with docx_database(state) as connection:
                _docx_route(run_id)._touch_cache_hit(connection, moved, "complete")

            with closing(sqlite3.connect(state)) as connection:
                rows = connection.execute(
                    "SELECT file_key,path FROM documents ORDER BY file_key"
                ).fetchall()
                dependent_rows = sum(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                        (_file_key(stale),),
                    ).fetchone()[0]
                    for table, column in (
                        ("document_fts", "file_key"),
                        ("document_parts", "file_key"),
                        ("pdf_counterparts", "docx_file_key"),
                    )
                )
            self.assertEqual(rows, [(_file_key(moved), moved.path)])
            self.assertEqual(dependent_rows, 0)

    def test_live_owner_is_preserved_and_rename_fails_conservatively(self):
        self._assert_live_owner_is_preserved(owner_birthtime_ns=402)

    def test_live_legacy_sentinel_omitted_by_limit_is_preserved(self):
        self._assert_live_owner_is_preserved(owner_birthtime_ns=DOCX_UNKNOWN_BIRTHTIME_NS)

    def _assert_live_owner_is_preserved(self, *, owner_birthtime_ns: int) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "docx.sqlite3"
            initialize_docx_state(state)
            old = _snapshot(Path(temporary) / "old.docx", 1, birthtime_ns=401)
            moved = _snapshot(Path(temporary) / "target.docx", 1, birthtime_ns=401)
            owner = _snapshot(Path(temporary) / "target.docx", 2, birthtime_ns=402)
            owner_live = _snapshot(
                Path(temporary) / "owner-new-path.docx",
                2,
                birthtime_ns=402,
            )
            run_id = 10
            with docx_database(state) as connection:
                _insert_docx_document(connection, old)
                _insert_docx_document(
                    connection,
                    owner,
                    birthtime_ns=owner_birthtime_ns,
                )
                _insert_docx_inventory(connection, moved, run_id)
                _insert_docx_inventory(connection, owner_live, run_id)

            with docx_database(state) as connection:
                with self.assertRaisesRegex(RuntimeError, "live inventory identity"):
                    _docx_route(run_id)._touch_cache_hit(
                        connection,
                        moved,
                        "complete",
                    )

            with closing(sqlite3.connect(state)) as connection:
                rows = connection.execute(
                    "SELECT file_key,path,birthtime_ns FROM documents ORDER BY file_key"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (_file_key(old), old.path, old.birthtime_ns),
                    (_file_key(owner), owner.path, owner_birthtime_ns),
                ],
            )


# endregion [03]


# region [04] Escaped read-only SQLite paths


class ReadOnlySqliteUriTests(unittest.TestCase):
    def test_percent_and_fragment_characters_open_the_exact_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (root / "pdf%20state.sqlite3", connect_pdf_state),
                (root / "docx#state.sqlite3", connect_docx_state),
            )
            for path, connector in cases:
                with self.subTest(path=path.name):
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute("CREATE TABLE marker(value TEXT)")
                        connection.execute("INSERT INTO marker VALUES('exact')")
                        connection.commit()
                    with closing(connector(path, readonly=True)) as connection:
                        value = connection.execute("SELECT value FROM marker").fetchone()[0]
                    self.assertEqual(value, "exact")

    def test_framework_route_state_escapes_readonly_database_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "framework%23#state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE marker(value TEXT)")
                connection.execute("INSERT INTO marker VALUES('exact')")
                connection.commit()
            with closing(FrameworkRouteState(path)._connect(readonly=True)) as connection:
                value = connection.execute("SELECT value FROM marker").fetchone()[0]
            self.assertEqual(value, "exact")


# endregion [04]


if __name__ == "__main__":
    unittest.main()
