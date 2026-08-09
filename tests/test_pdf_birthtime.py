"""Regression tests for PDF cache birth-time invariance and migration."""


# region [01] Imports and test adapter

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
import zlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from _02_Deduplicacion import (
    FULL_ALGORITHM,
    DedupIndex,
    FileSnapshot,
    full_fingerprint,
    snapshot_path,
)
from _04_Nucleo_Operativo.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from _04_Nucleo_Operativo.pdf_cache import binary_fingerprint
from _04_Nucleo_Operativo.pdf_route import PdfRoute, PdfRouteConfig
from _04_Nucleo_Operativo.pdf_route_cache import file_key
from _04_Nucleo_Operativo.pdf_state import (
    SCHEMA_VERSION,
    UNKNOWN_BIRTHTIME_NS,
    initialize_pdf_state,
    pdf_database,
)


class _PdfCandidates:
    def __init__(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        self.snapshots = snapshots

    def iter_route_candidates(self, run_id: int, mime: str):
        if mime == "application/pdf":
            yield from self.snapshots

    def iter_selected_route_candidates(
        self,
        run_id: int,
        mime: str,
        route_name: str,
        selection: object,
    ):
        if mime == "application/pdf":
            yield from self.snapshots

    def begin_file_actions(self, run_id: int, actions: object) -> list[int]:
        raise AssertionError("birthtime tests do not create file actions")

    def finish_file_actions(
        self,
        action_ids: object,
        status: str,
        detail: str | None = None,
    ) -> None:
        raise AssertionError("birthtime tests do not finish file actions")


# endregion [01]


# region [02] Migration and cache invariants


class PdfBirthtimeInvariantTests(unittest.TestCase):
    def test_schema_eleven_reclassifies_durable_timeouts_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            snapshot = FileSnapshot("large.pdf", 1, 9, 100, 11, 12)
            config = PdfRouteConfig(state)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute("UPDATE metadata SET value='10' WHERE key='schema_version'")
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,page_count,completed_pages,error_type,error_message,updated_ns)
                    VALUES(?,?,?,?,?,?,'error',10,1,'PdfDocumentTimeout','timeout',?)""",
                    (
                        file_key(snapshot),
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        config.processing_signature,
                        time.time_ns(),
                    ),
                )
                connection.execute(
                    """INSERT INTO page_staging VALUES(?,?,?,?,?,?)""",
                    (
                        file_key(snapshot),
                        config.processing_signature,
                        0,
                        "native",
                        zlib.compress(b"text"),
                        4,
                    ),
                )
                connection.commit()

            initialize_pdf_state(state)

            with closing(sqlite3.connect(state)) as connection:
                stored = connection.execute("SELECT status,is_partial FROM documents").fetchone()
                migrated = connection.execute(
                    "SELECT value FROM metadata WHERE key='durable_timeout_rows_migrated'"
                ).fetchone()[0]
            self.assertEqual(stored, ("partial", 1))
            self.assertEqual(migrated, "1")

    def test_reconciles_abandoned_processing_without_discarding_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            complete = FileSnapshot("complete.pdf", 1, 1, 100, 11, 12)
            partial = FileSnapshot("partial.pdf", 1, 2, 100, 11, 13)
            config = PdfRouteConfig(state)
            with closing(sqlite3.connect(state)) as connection:
                for snapshot in (complete, partial):
                    connection.execute(
                        "INSERT INTO pdf_inventory VALUES(?,?,?,?,?,2)",
                        (
                            file_key(snapshot),
                            snapshot.path,
                            snapshot.size,
                            snapshot.mtime_ns,
                            snapshot.birthtime_ns,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO documents(
                        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                        status,page_count,completed_pages,last_seen_run_id,updated_ns)
                        VALUES(?,?,?,?,?,?,'processing',2,2,1,?)""",
                        (
                            file_key(snapshot),
                            snapshot.path,
                            snapshot.size,
                            snapshot.mtime_ns,
                            snapshot.birthtime_ns,
                            config.processing_signature,
                            time.time_ns(),
                        ),
                    )
                connection.executemany(
                    """INSERT INTO pages(
                    file_key,page_number,source,text_zlib,text_chars)
                    VALUES(?,?,?,?,?)""",
                    (
                        (file_key(complete), 0, "native", zlib.compress(b"a"), 1),
                        (file_key(complete), 1, "native", zlib.compress(b"b"), 1),
                    ),
                )
                connection.execute(
                    "INSERT INTO page_staging VALUES(?,?,?,?,?,?)",
                    (
                        file_key(partial),
                        config.processing_signature,
                        0,
                        "native",
                        zlib.compress(b"a"),
                        1,
                    ),
                )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            route.run_id = 2
            route.cancellation = CancellationToken()
            self.assertEqual(route._reconcile_interrupted_documents(), (1, 1))

            with closing(sqlite3.connect(state)) as connection:
                rows = connection.execute(
                    "SELECT path,status,completed_pages,error_type FROM documents ORDER BY path"
                ).fetchall()
            self.assertEqual(rows[0], ("complete.pdf", "done", 2, None))
            self.assertEqual(
                rows[1],
                ("partial.pdf", "partial", 1, "InterruptedPdfProcessing"),
            )

    def test_cancelled_token_can_flush_already_validated_cache_touches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            snapshot = FileSnapshot("cached.pdf", 1, 1, 100, 11, 12)
            config = PdfRouteConfig(state)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    "INSERT INTO pdf_inventory VALUES(?,?,?,?,?,9)",
                    (
                        file_key(snapshot),
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                    ),
                )
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,last_seen_run_id,updated_ns)
                    VALUES(?,?,?,?,?,?,'done',1,?)""",
                    (
                        file_key(snapshot),
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        config.processing_signature,
                        time.time_ns(),
                    ),
                )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            route.run_id = 9
            route.cancellation = CancellationToken()
            route.cancellation.cancel()
            with pdf_database(state) as connection:
                route._touch_cache_hits(
                    connection,
                    [snapshot],
                    cancellable=False,
                )

            with closing(sqlite3.connect(state)) as connection:
                last_seen = connection.execute("SELECT last_seen_run_id FROM documents").fetchone()[
                    0
                ]
            self.assertEqual(last_seen, 9)

    def test_schema_eight_migrates_additively_with_unknown_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            with closing(sqlite3.connect(state)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(
                        key TEXT PRIMARY KEY,value TEXT NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version','8');
                    CREATE TABLE documents(
                        file_key TEXT PRIMARY KEY,
                        path TEXT NOT NULL COLLATE NOCASE,
                        size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        processing_signature TEXT NOT NULL,
                        status TEXT NOT NULL,
                        updated_ns INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO documents VALUES(
                        'volume:file','legacy.pdf',100,200,'legacy-signature',
                        'protected',300
                    );
                    CREATE TABLE pdf_inventory(
                        file_key TEXT PRIMARY KEY,
                        path TEXT NOT NULL COLLATE NOCASE,
                        size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        last_seen_run_id INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO pdf_inventory VALUES(
                        'volume:file','legacy.pdf',100,200,7
                    );
                    """
                )

            initialize_pdf_state(state)

            with closing(sqlite3.connect(state)) as connection:
                version = int(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()[0]
                )
                document = connection.execute(
                    "SELECT path,status,birthtime_ns FROM documents WHERE file_key='volume:file'"
                ).fetchone()
                inventory = connection.execute(
                    "SELECT path,last_seen_run_id,birthtime_ns FROM pdf_inventory "
                    "WHERE file_key='volume:file'"
                ).fetchone()

            self.assertEqual(version, SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 9)
            self.assertEqual(UNKNOWN_BIRTHTIME_NS, -1)
            self.assertEqual(document, ("legacy.pdf", "protected", -1))
            self.assertEqual(inventory, ("legacy.pdf", 7, -1))

    def test_matching_legacy_digest_promotes_once_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.pdf"
            source.write_bytes(b"legacy PDF cache evidence")
            snapshot = replace(
                snapshot_path(source),
                birthtime_ns=source.stat().st_ctime_ns,
            )
            expected_digest = full_fingerprint(snapshot).hex()
            state = root / "pdf.sqlite3"
            initialize_pdf_state(state)
            config = PdfRouteConfig(state, ocr_mode="never")
            key = file_key(snapshot)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,binary_xxh3_128,updated_ns)
                    VALUES(?,?,?,?,?,?,'protected',?,?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        UNKNOWN_BIRTHTIME_NS,
                        config.processing_signature,
                        expected_digest,
                        time.time_ns(),
                    ),
                )
                connection.commit()

            with DedupIndex(root / "dedup.sqlite3") as index:
                route = object.__new__(PdfRoute)
                route.config = config
                route.index = index
                route.cancellation = CancellationToken()
                with patch(
                    "_04_Nucleo_Operativo.pdf_route_cache.binary_fingerprint",
                    wraps=binary_fingerprint,
                ) as fingerprint:
                    self.assertTrue(route._is_cache_hit(snapshot, touch=False))
                    self.assertTrue(route._is_cache_hit(snapshot, touch=False))
                self.assertEqual(fingerprint.call_count, 1)
                self.assertEqual(
                    index.cached_fingerprint(snapshot, FULL_ALGORITHM),
                    bytes.fromhex(expected_digest),
                )

            with closing(sqlite3.connect(state)) as connection:
                promoted_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM documents WHERE file_key=?",
                    (key,),
                ).fetchone()[0]
            self.assertEqual(promoted_birthtime, snapshot.birthtime_ns)

    def test_current_dedup_digest_promotes_without_rereading_and_batches_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.pdf"
            source.write_bytes(b"legacy PDF cache evidence")
            snapshot = snapshot_path(source)
            digest = full_fingerprint(snapshot)
            state = root / "pdf.sqlite3"
            initialize_pdf_state(state)
            config = PdfRouteConfig(state, ocr_mode="never")
            key = file_key(snapshot)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,binary_xxh3_128,updated_ns)
                    VALUES(?,?,?,?,?,?,'protected',?,?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        UNKNOWN_BIRTHTIME_NS,
                        config.processing_signature,
                        digest.hex(),
                        time.time_ns(),
                    ),
                )
                connection.commit()

            with DedupIndex(root / "dedup.sqlite3") as index:
                index.store_fingerprint(snapshot, FULL_ALGORITHM, digest)
                route = object.__new__(PdfRoute)
                route.config = config
                route.index = index
                route.cancellation = CancellationToken()
                route.run_id = 1
                with (
                    patch(
                        "_04_Nucleo_Operativo.pdf_cache.full_fingerprint",
                        side_effect=AssertionError("source was read again"),
                    ),
                    pdf_database(state) as cache,
                ):
                    self.assertTrue(
                        route._is_cache_hit(
                            snapshot,
                            connection=cache,
                            touch=False,
                        )
                    )
                    self.assertFalse(cache.in_transaction)
                    with closing(sqlite3.connect(state, timeout=0)) as writer:
                        writer.execute("BEGIN IMMEDIATE")
                        writer.rollback()
                    legacy = cache.execute(
                        "SELECT birthtime_ns FROM documents WHERE file_key=?",
                        (key,),
                    ).fetchone()[0]
                    self.assertEqual(legacy, UNKNOWN_BIRTHTIME_NS)
                    route._touch_cache_hits(cache, [snapshot])
                    self.assertFalse(cache.in_transaction)

            with closing(sqlite3.connect(state)) as connection:
                promoted = connection.execute(
                    "SELECT birthtime_ns FROM documents WHERE file_key=?",
                    (key,),
                ).fetchone()[0]
            self.assertEqual(promoted, snapshot.birthtime_ns)

    def test_mismatched_legacy_digest_stays_untrusted_and_is_cached_in_dedup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.pdf"
            source.write_bytes(b"current bytes differ from legacy evidence")
            snapshot = replace(
                snapshot_path(source),
                birthtime_ns=source.stat().st_ctime_ns,
            )
            state = root / "pdf.sqlite3"
            initialize_pdf_state(state)
            config = PdfRouteConfig(state, ocr_mode="never")
            key = file_key(snapshot)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,binary_xxh3_128,updated_ns)
                    VALUES(?,?,?,?,?,?,'protected',?,?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        UNKNOWN_BIRTHTIME_NS,
                        config.processing_signature,
                        "00" * 16,
                        time.time_ns(),
                    ),
                )
                connection.commit()

            with DedupIndex(root / "dedup.sqlite3") as index:
                route = object.__new__(PdfRoute)
                route.config = config
                route.index = index
                route.cancellation = CancellationToken()
                self.assertFalse(route._is_cache_hit(snapshot, touch=False))
                current_digest = index.cached_fingerprint(snapshot, FULL_ALGORITHM)

            self.assertEqual(current_digest, full_fingerprint(snapshot))
            with closing(sqlite3.connect(state)) as connection:
                retained_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM documents WHERE file_key=?",
                    (key,),
                ).fetchone()[0]
            self.assertEqual(retained_birthtime, UNKNOWN_BIRTHTIME_NS)

    def test_legacy_verification_honors_cancellation_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.pdf"
            source.write_bytes(b"legacy evidence")
            snapshot = replace(
                snapshot_path(source),
                birthtime_ns=source.stat().st_ctime_ns,
            )
            state = root / "pdf.sqlite3"
            initialize_pdf_state(state)
            config = PdfRouteConfig(state, ocr_mode="never")
            key = file_key(snapshot)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,binary_xxh3_128,updated_ns)
                    VALUES(?,?,?,?,?,?,'protected',?,?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        UNKNOWN_BIRTHTIME_NS,
                        config.processing_signature,
                        full_fingerprint(snapshot).hex(),
                        time.time_ns(),
                    ),
                )
                connection.commit()

            with DedupIndex(root / "dedup.sqlite3") as index:
                route = object.__new__(PdfRoute)
                route.config = config
                route.index = index
                route.cancellation = CancellationToken()
                route.cancellation.cancel()
                with self.assertRaises(CancellationRequested):
                    route._is_cache_hit(snapshot, touch=False)

            with closing(sqlite3.connect(state)) as connection:
                retained_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM documents WHERE file_key=?",
                    (key,),
                ).fetchone()[0]
            self.assertEqual(retained_birthtime, UNKNOWN_BIRTHTIME_NS)

    def test_cache_and_resumption_require_matching_birthtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            config = PdfRouteConfig(state, ocr_mode="never")
            snapshot = FileSnapshot("document.pdf", 1, 2, 100, 11, 12)
            key = file_key(snapshot)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,updated_ns) VALUES(?,?,?,?,?,?,'protected',?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        UNKNOWN_BIRTHTIME_NS,
                        config.processing_signature,
                        time.time_ns(),
                    ),
                )
                connection.execute(
                    """INSERT INTO page_staging(
                    file_key,processing_signature,page_number,source,text_zlib,text_chars)
                    VALUES(?,?,0,'native',?,4)""",
                    (key, config.processing_signature, zlib.compress(b"text")),
                )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            self.assertFalse(route._is_cache_hit(snapshot, touch=False))
            with closing(sqlite3.connect(state)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT birthtime_ns FROM documents WHERE file_key=?",
                        (key,),
                    ).fetchone()[0],
                    UNKNOWN_BIRTHTIME_NS,
                )

            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    "UPDATE documents SET birthtime_ns=? WHERE file_key=?",
                    (snapshot.birthtime_ns, key),
                )
                connection.commit()

            self.assertTrue(route._is_cache_hit(snapshot, touch=False))
            self.assertEqual(
                route._resumable_pages(snapshot),
                (1, frozenset(), 0),
            )

            replaced = FileSnapshot("document.pdf", 1, 2, 100, 11, 13)
            self.assertFalse(route._is_cache_hit(replaced, touch=False))
            self.assertEqual(
                route._resumable_pages(replaced),
                (0, frozenset(), 0),
            )

    def test_inventory_and_failure_upserts_persist_birthtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            config = PdfRouteConfig(state, ocr_mode="never")
            snapshot = FileSnapshot("document.pdf", 1, 2, 100, 11, 12)
            route = object.__new__(PdfRoute)
            route.config = config
            route.framework_state = _PdfCandidates((snapshot,))
            route.run_id = 7
            route.cancellation = CancellationToken()

            route._stage_pdf_inventory()
            route._store_failure(snapshot, "error", "Sentinel", "expected")

            with closing(sqlite3.connect(state)) as connection:
                inventory_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM pdf_inventory WHERE file_key=?",
                    (file_key(snapshot),),
                ).fetchone()[0]
                document_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM documents WHERE file_key=?",
                    (file_key(snapshot),),
                ).fetchone()[0]

            self.assertEqual(inventory_birthtime, snapshot.birthtime_ns)
            self.assertEqual(document_birthtime, snapshot.birthtime_ns)

            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    "UPDATE documents SET transient_retry_count=3 WHERE file_key=?",
                    (file_key(snapshot),),
                )
                connection.execute(
                    """INSERT INTO page_staging(
                    file_key,processing_signature,page_number,source,text_zlib,text_chars)
                    VALUES(?,?,0,'native',?,4)""",
                    (
                        file_key(snapshot),
                        config.processing_signature,
                        zlib.compress(b"old!"),
                    ),
                )
                connection.execute(
                    """INSERT INTO page_errors(
                    file_key,processing_signature,page_number,error_type,
                    error_message,updated_ns) VALUES(?,?,0,'OldError','old',?)""",
                    (file_key(snapshot), config.processing_signature, time.time_ns()),
                )
                connection.execute(
                    """INSERT INTO document_warnings(
                    file_key,processing_signature,stage,warning_count,
                    samples_json,updated_ns) VALUES(?,?,'extract',1,'[]',?)""",
                    (file_key(snapshot), config.processing_signature, time.time_ns()),
                )
                connection.commit()

            replaced = FileSnapshot("document.pdf", 1, 2, 100, 11, 13)
            route.framework_state = _PdfCandidates((replaced,))
            route.run_id = 8
            route._stage_pdf_inventory()
            route._store_failure(
                replaced,
                "error",
                "Sentinel",
                "expected",
                transient=True,
            )

            with closing(sqlite3.connect(state)) as connection:
                values = connection.execute(
                    """SELECT d.birthtime_ns,i.birthtime_ns,
                    d.transient_retry_count
                    FROM documents d JOIN pdf_inventory i USING(file_key)
                    WHERE d.file_key=?""",
                    (file_key(replaced),),
                ).fetchone()
                stale_rows = sum(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE file_key=?",
                        (file_key(replaced),),
                    ).fetchone()[0]
                    for table in (
                        "page_staging",
                        "page_errors",
                        "document_warnings",
                    )
                )
            self.assertEqual(
                values,
                (replaced.birthtime_ns, replaced.birthtime_ns, 1),
            )
            self.assertEqual(stale_rows, 0)

    def test_pruning_retains_legacy_cache_omitted_by_count_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            selected = FileSnapshot("selected.pdf", 1, 1, 100, 11, 12)
            legacy = FileSnapshot("legacy.pdf", 1, 2, 100, 11, 13)
            replaced = FileSnapshot("replaced.pdf", 1, 3, 100, 11, 15)
            config = PdfRouteConfig(
                state,
                ocr_mode="never",
                max_documents=1,
            )
            with closing(sqlite3.connect(state)) as connection:
                connection.executemany(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,updated_ns) VALUES(?,?,?,?,?,?,'protected',?)""",
                    (
                        (
                            file_key(legacy),
                            legacy.path,
                            legacy.size,
                            legacy.mtime_ns,
                            UNKNOWN_BIRTHTIME_NS,
                            config.processing_signature,
                            time.time_ns(),
                        ),
                        (
                            file_key(replaced),
                            replaced.path,
                            replaced.size,
                            replaced.mtime_ns,
                            replaced.birthtime_ns - 1,
                            config.processing_signature,
                            time.time_ns(),
                        ),
                    ),
                )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            route.framework_state = _PdfCandidates((selected, legacy, replaced))
            route.run_id = 7
            route.cancellation = CancellationToken()
            route._stage_pdf_inventory()
            self.assertEqual(
                [item.path for item in route._candidate_snapshots()],
                [selected.path],
            )

            documents_pruned, _ = route._prune_pdf_cache()

            with closing(sqlite3.connect(state)) as connection:
                surviving = {row[0] for row in connection.execute("SELECT file_key FROM documents")}
            self.assertEqual(documents_pruned, 1)
            self.assertIn(file_key(legacy), surviving)
            self.assertNotIn(file_key(replaced), surviving)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
