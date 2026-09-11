from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import zipfile
import zlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.capabilities.formats.docx import schema as docx_schema
from neocortex.capabilities.formats.docx.route import (
    DOCX_MIME,
    PDF_MIME,
    DocxRoute,
    DocxRouteConfig,
    list_docx_layout_groups,
    list_missing_pdf_counterparts,
    search_docx_state,
    extract_docx,
)
from neocortex.capabilities.formats.docx.state import (
    SCHEMA_VERSION,
    UNKNOWN_BIRTHTIME_NS,
    initialize_docx_state,
)
from neocortex.platform.policy import sqlite_path_collation


# region [01] Test fixtures


class _State:
    def __init__(self, candidates):
        self.candidates = candidates
        self.review_candidates = []
        self.review_resolutions = []

    def iter_route_candidates(self, run_id, mime):
        yield from self.candidates.get(mime, ())

    def store_review_candidates(self, run_id, candidates):
        self.review_candidates.extend(candidates)

    def resolve_review_candidates(self, run_id, route_name, snapshot, resolution_note):
        self.review_resolutions.append((run_id, route_name, snapshot, resolution_note))
        return 1

    def reconcile_review_candidates(
        self,
        run_id,
        route_name,
        snapshot,
        resolution_note,
        *,
        evaluated_reason_codes,
        active_reason_codes,
    ):
        self.review_resolutions.append(
            (
                run_id,
                route_name,
                snapshot,
                resolution_note,
                frozenset(evaluated_reason_codes),
                frozenset(active_reason_codes),
            )
        )
        return 1

    def reconcile_review_candidates_batch(
        self,
        run_id,
        route_name,
        reconciliations,
    ):
        for reconciliation in reconciliations:
            self.review_resolutions.append(
                (
                    run_id,
                    route_name,
                    reconciliation.snapshot,
                    reconciliation.resolution_note,
                    frozenset(reconciliation.evaluated_reason_codes),
                    frozenset(reconciliation.active_reason_codes),
                )
            )
        return len(tuple(reconciliations))


def _make_docx(
    path: Path,
    text: str = "Subestación transformador",
    *,
    compression: int = zipfile.ZIP_STORED,
    main_content_type: str = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    ),
) -> None:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Title"/><w:jc w:val="center"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Breaker 52</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
      </w:body>
    </w:document>"""
    core = """<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Informe eléctrico</dc:title><dc:creator>Victor</dc:creator></cp:coreProperties>"""
    header = """<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>NeoCortex</w:t></w:r></w:p></w:hdr>"""
    content_types = f"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="{main_content_type}"/></Types>"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"""
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/header1.xml", header)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("word/media/image1.png", b"not-rendered")


def _member_payload_offset(path: Path, member_name: str) -> int:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member_name)
    with path.open("rb") as source:
        source.seek(info.header_offset)
        header = source.read(30)
    name_length = int.from_bytes(header[26:28], "little")
    extra_length = int.from_bytes(header[28:30], "little")
    return info.header_offset + 30 + name_length + extra_length


def _break_deflate_stream(path: Path, member_name: str) -> None:
    payload = bytearray(path.read_bytes())
    payload[_member_payload_offset(path, member_name)] = 0x07
    path.write_bytes(payload)


def _corrupt_central_crc(path: Path, member_name: str) -> None:
    payload = bytearray(path.read_bytes())
    expected_name = member_name.encode("utf-8")
    offset = 0
    while True:
        offset = payload.find(b"PK\x01\x02", offset)
        if offset < 0:
            raise AssertionError(f"central member not found: {member_name}")
        name_length = int.from_bytes(payload[offset + 28 : offset + 30], "little")
        extra_length = int.from_bytes(payload[offset + 30 : offset + 32], "little")
        comment_length = int.from_bytes(payload[offset + 32 : offset + 34], "little")
        name = bytes(payload[offset + 46 : offset + 46 + name_length])
        if name == expected_name:
            crc = int.from_bytes(payload[offset + 16 : offset + 20], "little")
            payload[offset + 16 : offset + 20] = (crc ^ 1).to_bytes(4, "little")
            path.write_bytes(payload)
            return
        offset += 46 + name_length + extra_length + comment_length


# endregion [01]


# region [02] Migration and route regressions


class DocxRouteTests(unittest.TestCase):
    def test_accepts_word_template_and_retries_legacy_mismatch_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "Formato ANDRITZ.docx"
            _make_docx(
                template,
                "Formato de pruebas ANDRITZ",
                main_content_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.template.main+xml"
                ),
            )
            state = _State({DOCX_MIME: [snapshot_path(template)], PDF_MIME: []})
            database = root / "docx.sqlite3"

            first = DocxRoute(DocxRouteConfig(database), state, 1).run()
            self.assertEqual((first.extracted, first.errors), (1, 0))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """UPDATE documents SET status='error',
                    failure_code='ooxml_content_type_mismatch',
                    error_type='DocxProcessingError',
                    error_message='legacy unsupported template',
                    integrity_status='invalid',retryable=0,
                    review_disposition='manual_review'"""
                )
                connection.commit()

            migrated = DocxRoute(DocxRouteConfig(database), state, 2).run()

            self.assertEqual(migrated.retried_documents, 1)
            self.assertEqual((migrated.extracted, migrated.errors), (1, 0))
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute("SELECT status,failure_code FROM documents").fetchone()
            self.assertEqual(stored, ("complete", None))

    def test_migrates_schema_one_without_discarding_document_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "docx.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(
                        key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version','1');
                    CREATE TABLE documents(
                        file_key TEXT PRIMARY KEY,path TEXT NOT NULL,
                        status TEXT NOT NULL) WITHOUT ROWID;
                    INSERT INTO documents VALUES('old','old.docx','complete');
                    """
                )
                connection.commit()

            initialize_docx_state(database)

            with closing(sqlite3.connect(database)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
                inventory_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(docx_inventory)")
                }
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT file_key,processing_signature,birthtime_ns FROM documents"
                ).fetchone()
            self.assertEqual(version, str(SCHEMA_VERSION))
            self.assertIn("layout_signature", columns)
            self.assertIn("last_seen_run_id", columns)
            self.assertIn("birthtime_ns", columns)
            self.assertIn("integrity_status", columns)
            self.assertIn("review_disposition", columns)
            self.assertIn("birthtime_ns", inventory_columns)
            self.assertEqual(row, ("old", "", UNKNOWN_BIRTHTIME_NS))

    def test_migrates_schema_two_birthtime_as_a_cache_miss_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "docx.sqlite3"
            snapshot = FileSnapshot("old.docx", 1, 2, 10, 20, 30)
            file_key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
            config = DocxRouteConfig(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(
                        key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version','2');
                    CREATE TABLE documents(
                        file_key TEXT PRIMARY KEY,path TEXT NOT NULL,
                        size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                        processing_signature TEXT NOT NULL,status TEXT NOT NULL,
                        last_seen_run_id INTEGER NOT NULL,updated_ns INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    CREATE TABLE docx_inventory(
                        file_key TEXT PRIMARY KEY,path TEXT NOT NULL,
                        size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                        last_seen_run_id INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    """
                )
                connection.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,'complete',1,1)",
                    (
                        file_key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        config.processing_signature,
                    ),
                )
                connection.execute(
                    "INSERT INTO docx_inventory VALUES(?,?,?,?,1)",
                    (file_key, snapshot.path, snapshot.size, snapshot.mtime_ns),
                )
                connection.commit()

            initialize_docx_state(database)

            route = object.__new__(DocxRoute)
            route.config = config
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                document = connection.execute(
                    "SELECT status,birthtime_ns FROM documents WHERE file_key=?",
                    (file_key,),
                ).fetchone()
                inventory_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM docx_inventory WHERE file_key=?",
                    (file_key,),
                ).fetchone()[0]
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                cache_status = route._cache_status(connection, snapshot)
                path_collations = tuple(
                    str(row[4]).upper()
                    for row in connection.execute("PRAGMA index_xinfo(docx_documents_path_idx)")
                    if row[5]
                )
            self.assertEqual(version, str(SCHEMA_VERSION))
            self.assertEqual(
                (document["status"], document["birthtime_ns"]),
                ("complete", UNKNOWN_BIRTHTIME_NS),
            )
            self.assertEqual(inventory_birthtime, UNKNOWN_BIRTHTIME_NS)
            self.assertEqual(cache_status, "miss")
            self.assertEqual(path_collations, (sqlite_path_collation(),))

    def test_migrates_schema_four_to_explicit_path_collations_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "docx.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                docx_schema._build_docx_v5_canonical_schema(connection)
                connection.executescript(
                    """
                    DROP INDEX docx_documents_path_idx;
                    DROP INDEX docx_documents_review_idx;
                    CREATE UNIQUE INDEX docx_documents_path_idx
                        ON documents(path);
                    CREATE INDEX docx_documents_review_idx
                        ON documents(review_disposition,status,path);
                    INSERT INTO metadata VALUES('schema_version','4');
                    """
                )
                connection.execute(
                    """INSERT INTO documents(
                        file_key,path,size,mtime_ns,processing_signature,status,
                        last_seen_run_id,updated_ns
                    ) VALUES('preserved','preserved.docx',1,2,'signature',
                        'complete',3,4)"""
                )
                connection.commit()

            initialize_docx_state(database)

            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                preserved = connection.execute(
                    "SELECT path,status FROM documents WHERE file_key='preserved'"
                ).fetchone()
                index_sql = {
                    str(row[0]): str(row[1]).upper()
                    for row in connection.execute(
                        """SELECT name,sql FROM sqlite_master
                        WHERE type='index' AND name IN (
                            'docx_documents_path_idx',
                            'docx_documents_review_idx'
                        )"""
                    )
                }
            self.assertEqual(version, str(SCHEMA_VERSION))
            self.assertEqual(preserved, ("preserved.docx", "complete"))
            expected = f"PATH COLLATE {sqlite_path_collation()}"
            self.assertIn(expected, index_sql["docx_documents_path_idx"])
            self.assertIn(expected, index_sql["docx_documents_review_idx"])

    def test_extracts_searches_classifies_pairs_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Informe.docx"
            pdf = root / "Informe.pdf"
            unrelated_pdf = root / "Otro.pdf"
            _make_docx(docx)
            pdf.write_bytes(b"%PDF-1.4\n")
            unrelated_pdf.write_bytes(b"%PDF-1.4\n")
            state = _State(
                {
                    DOCX_MIME: [snapshot_path(docx)],
                    PDF_MIME: [snapshot_path(pdf), snapshot_path(unrelated_pdf)],
                }
            )
            database = root / "state" / "docx.sqlite3"

            first = DocxRoute(DocxRouteConfig(database), state, 1).run()
            self.assertEqual(first.extracted, 1)
            self.assertEqual(first.new_documents, 1)
            self.assertEqual(first.retried_documents, 0)
            self.assertEqual(first.pdf_matched, 1)
            self.assertEqual(first.pdf_missing, 0)
            self.assertEqual(first.layouts_classified, 1)
            results = search_docx_state(database, "transformador")
            self.assertEqual(results[0]["path"], str(docx))
            groups = list_docx_layout_groups(database)
            self.assertEqual(groups[0]["layout_class"], "a4_portrait:letterhead")

            def validate_pdf_only(path):
                self.assertEqual(str(path), str(pdf))
                return snapshot_path(path)

            with patch(
                "neocortex.capabilities.formats.docx.route.snapshot_path",
                side_effect=validate_pdf_only,
            ) as live_snapshot:
                second = DocxRoute(DocxRouteConfig(database), state, 2).run()
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(second.new_documents, 0)
            self.assertEqual(second.retried_documents, 0)
            self.assertEqual(second.extracted, 0)
            live_snapshot.assert_called_once_with(str(pdf))

    @unittest.skipUnless(os.name == "nt", "birth-time replacement is Windows-only")
    def test_changed_birthtime_reprocesses_same_identity_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Informe.docx"
            _make_docx(docx)
            initial_snapshot = snapshot_path(docx)
            database = root / "docx.sqlite3"
            initial_state = _State({DOCX_MIME: [initial_snapshot], PDF_MIME: []})
            first = DocxRoute(DocxRouteConfig(database), initial_state, 1).run()
            self.assertEqual(first.extracted, 1)

            changed_snapshot = replace(
                initial_snapshot,
                birthtime_ns=initial_snapshot.birthtime_ns + 1,
            )
            changed_state = _State({DOCX_MIME: [changed_snapshot], PDF_MIME: []})
            # Simulate the live NTFS stat for a reused file reference.  The
            # real filesystem fixture has not been replaced, so its creation
            # time cannot otherwise match the replacement snapshot.
            with patch(
                "neocortex.capabilities.formats.docx.route.stat_matches_snapshot",
                return_value=True,
            ) as live_identity:
                second = DocxRoute(DocxRouteConfig(database), changed_state, 2).run()

            self.assertEqual(second.cache_hits, 0)
            self.assertEqual(second.new_documents, 1)
            self.assertEqual(second.extracted, 1)
            self.assertEqual(second.cache_documents_pruned, 0)
            self.assertEqual(live_identity.call_count, 2)
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    """SELECT d.birthtime_ns,i.birthtime_ns,d.last_seen_run_id
                    FROM documents d JOIN docx_inventory i USING(file_key)"""
                ).fetchone()
            self.assertEqual(
                stored,
                (changed_snapshot.birthtime_ns, changed_snapshot.birthtime_ns, 2),
            )

    def test_records_missing_pdf_and_corrupt_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "Sin contraparte.docx"
            bad = root / "Roto.docx"
            _make_docx(good, "Mantenimiento industrial")
            bad.write_bytes(b"PK\x03\x04broken")
            state = _State({DOCX_MIME: [snapshot_path(good), snapshot_path(bad)], PDF_MIME: []})
            database = root / "docx.sqlite3"
            result = DocxRoute(DocxRouteConfig(database), state, 7).run()
            self.assertEqual(result.errors, 1)
            self.assertEqual(result.pdf_missing, 1)
            self.assertEqual(list_missing_pdf_counterparts(database), [str(good)])
            connection = sqlite3.connect(database)
            try:
                status = connection.execute(
                    "SELECT status FROM documents WHERE path=?", (str(bad),)
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(status, "error")

            cached = DocxRoute(DocxRouteConfig(database), state, 8).run()
            self.assertEqual(cached.cached_errors, 1)
            self.assertEqual(cached.errors, 0)
            self.assertEqual(cached.review_candidates, 1)
            self.assertEqual(cached.deletion_candidates, 0)

    def test_discards_a_stale_pdf_counterpart_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Informe.docx"
            pdf = root / "Informe.pdf"
            _make_docx(docx)
            pdf.write_bytes(b"%PDF-1.4\n")
            pdf_snapshot = snapshot_path(pdf)
            pdf.unlink()
            state = _State(
                {
                    DOCX_MIME: [snapshot_path(docx)],
                    PDF_MIME: [pdf_snapshot],
                }
            )
            result = DocxRoute(DocxRouteConfig(root / "docx.sqlite3"), state, 9).run()
            self.assertEqual(result.pdf_matched, 0)
            self.assertEqual(result.pdf_missing, 1)
            self.assertEqual(result.pdf_stale_candidates, 1)

    def test_records_corrupt_compressed_member_without_aborting_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Compresion corrupta.docx"
            _make_docx(docx)
            state = _State({DOCX_MIME: [snapshot_path(docx)], PDF_MIME: []})
            database = root / "docx.sqlite3"

            with patch(
                "neocortex.capabilities.formats.docx.route.extract_docx",
                side_effect=zlib.error("invalid distance too far back"),
            ):
                result = DocxRoute(DocxRouteConfig(database), state, 1).run()

            self.assertEqual(result.errors, 1)
            connection = sqlite3.connect(database)
            try:
                outcome = connection.execute(
                    """SELECT error_type,failure_code,retryable,
                    review_disposition FROM documents WHERE path=?""",
                    (str(docx),),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                outcome,
                ("error", "zip_member_deflate_corrupt", 0, "deletion_candidate"),
            )
            self.assertEqual(result.deletion_candidates, 1)
            self.assertEqual(
                state.review_candidates[0].reason_code,
                "zip_member_deflate_corrupt",
            )

            cached = DocxRoute(DocxRouteConfig(database), state, 2).run()
            self.assertEqual(cached.cached_errors, 1)
            self.assertEqual(cached.review_candidates, 1)
            self.assertEqual(cached.deletion_candidates, 1)

    def test_rejects_a_document_that_cannot_fit_the_memory_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Grande.docx"
            _make_docx(docx)
            state = _State({DOCX_MIME: [snapshot_path(docx)], PDF_MIME: []})
            result = DocxRoute(
                DocxRouteConfig(
                    root / "docx.sqlite3",
                    memory_budget_bytes=1024 * 1024,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                ),
                state,
                1,
            ).run()
            self.assertEqual(result.errors, 1)
            connection = sqlite3.connect(root / "docx.sqlite3")
            try:
                outcome = connection.execute(
                    "SELECT error_type,failure_code,retryable FROM documents"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                outcome,
                ("MemoryBudgetExceeded", "resource_budget_exceeded", 1),
            )
            self.assertEqual(result.retryable_errors, 1)

    def test_indexes_body_when_an_optional_header_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Cabecera corrupta.docx"
            _make_docx(docx, compression=zipfile.ZIP_DEFLATED)
            _break_deflate_stream(docx, "word/header1.xml")
            state = _State({DOCX_MIME: [snapshot_path(docx)], PDF_MIME: []})
            database = root / "docx.sqlite3"

            result = DocxRoute(DocxRouteConfig(database), state, 1).run()

            self.assertEqual(result.errors, 0)
            self.assertEqual(result.partial_documents, 1)
            self.assertEqual(search_docx_state(database, "transformador")[0]["path"], str(docx))
            with closing(sqlite3.connect(database)) as connection:
                outcome = connection.execute(
                    """SELECT status,integrity_status,failure_code,
                    review_disposition,recovery_mode FROM documents"""
                ).fetchone()
                diagnostic = connection.execute(
                    "SELECT part_name,code,required FROM document_diagnostics"
                ).fetchone()
            self.assertEqual(
                outcome,
                (
                    "partial",
                    "degraded",
                    "zip_member_deflate_corrupt",
                    "manual_review",
                    "optional_parts_skipped",
                ),
            )
            self.assertEqual(
                diagnostic,
                ("word/header1.xml", "zip_member_deflate_corrupt", 0),
            )
            self.assertEqual(state.review_candidates[0].recommendation, "manual_review")

            cached = DocxRoute(DocxRouteConfig(database), state, 2).run()
            self.assertEqual(cached.cached_partial_documents, 1)
            self.assertEqual(cached.extracted, 0)

    def test_recovers_well_formed_required_xml_with_bad_central_crc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "CRC recuperable.docx"
            _make_docx(docx, compression=zipfile.ZIP_DEFLATED)
            _corrupt_central_crc(docx, "word/document.xml")
            state = _State({DOCX_MIME: [snapshot_path(docx)], PDF_MIME: []})
            database = root / "docx.sqlite3"

            result = DocxRoute(DocxRouteConfig(database), state, 1).run()

            self.assertEqual(result.errors, 0)
            self.assertEqual(result.partial_documents, 1)
            with closing(sqlite3.connect(database)) as connection:
                outcome = connection.execute(
                    "SELECT status,failure_code,recovery_mode FROM documents"
                ).fetchone()
                evidence = connection.execute(
                    """SELECT expected_size,actual_size,expected_crc32,actual_crc32
                    FROM document_diagnostics"""
                ).fetchone()
            self.assertEqual(
                outcome,
                (
                    "partial",
                    "zip_member_crc_mismatch_recovered",
                    "raw_deflate_validated_xml",
                ),
            )
            self.assertEqual(evidence[0], evidence[1])
            self.assertNotEqual(evidence[2], evidence[3])
            self.assertEqual(len(search_docx_state(database, "transformador")), 1)

    def test_marks_required_deflate_corruption_as_deletion_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Cuerpo corrupto.docx"
            _make_docx(docx, compression=zipfile.ZIP_DEFLATED)
            _break_deflate_stream(docx, "word/document.xml")
            state = _State({DOCX_MIME: [snapshot_path(docx)], PDF_MIME: []})
            database = root / "docx.sqlite3"

            result = DocxRoute(DocxRouteConfig(database), state, 1).run()

            self.assertEqual(result.errors, 1)
            self.assertEqual(result.deletion_candidates, 1)
            with closing(sqlite3.connect(database)) as connection:
                outcome = connection.execute(
                    """SELECT status,integrity_status,failure_code,
                    retryable,review_disposition FROM documents"""
                ).fetchone()
            self.assertEqual(
                outcome,
                (
                    "error",
                    "corrupt",
                    "zip_member_deflate_corrupt",
                    0,
                    "deletion_candidate",
                ),
            )
            self.assertEqual(
                state.review_candidates[0].recommendation,
                "deletion_candidate",
            )

    def test_retries_transient_errors_without_the_manual_retry_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx = root / "Temporal.docx"
            _make_docx(docx)
            state = _State({DOCX_MIME: [snapshot_path(docx)], PDF_MIME: []})
            database = root / "docx.sqlite3"

            with patch(
                "neocortex.capabilities.formats.docx.route.extract_docx",
                side_effect=OSError("temporarily unavailable"),
            ):
                failed = DocxRoute(DocxRouteConfig(database), state, 1).run()
            recovered = DocxRoute(
                DocxRouteConfig(database, retry_recoverable_errors=True),
                state,
                2,
            ).run()

            self.assertEqual(failed.retryable_errors, 1)
            self.assertEqual(recovered.retried_documents, 1)
            self.assertEqual(recovered.extracted, 1)
            self.assertEqual(len(state.review_resolutions), 1)
            reconciliation = state.review_resolutions[0]
            self.assertEqual(reconciliation[0], 2)
            self.assertIn("source_unavailable", reconciliation[4])
            self.assertEqual(reconciliation[5], frozenset())

    def test_commits_bounded_batches_before_an_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(9):
                path = root / f"{index:02d}.docx"
                _make_docx(path, f"Documento {index}")
                paths.append(path)
            state = _State({DOCX_MIME: [snapshot_path(path) for path in paths], PDF_MIME: []})
            database = root / "docx.sqlite3"
            calls = 0

            def interrupt_ninth(path, max_text_chars, memory_gate, cancellation=None):
                nonlocal calls
                calls += 1
                if calls == 9:
                    raise KeyboardInterrupt
                return extract_docx(
                    path,
                    max_text_chars,
                    memory_gate,
                    cancellation,
                )

            with patch(
                "neocortex.capabilities.formats.docx.route.extract_docx",
                side_effect=interrupt_ninth,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    DocxRoute(DocxRouteConfig(database), state, 1).run()
            connection = sqlite3.connect(database)
            try:
                persisted = connection.execute(
                    "SELECT COUNT(*) FROM documents WHERE status='complete'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(persisted, 8)

    def test_reconcile_prunes_unprocessed_cache_with_changed_birthtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "A.docx"
            second_path = root / "B.docx"
            _make_docx(first_path, "Documento A")
            _make_docx(second_path, "Documento B")
            first_snapshot = snapshot_path(first_path)
            second_snapshot = replace(
                snapshot_path(second_path),
                birthtime_ns=second_path.stat().st_ctime_ns,
            )
            database = root / "docx.sqlite3"
            initial = _State(
                {
                    DOCX_MIME: [first_snapshot, second_snapshot],
                    PDF_MIME: [],
                }
            )
            DocxRoute(DocxRouteConfig(database), initial, 1).run()

            replaced_second = replace(
                second_snapshot,
                birthtime_ns=second_snapshot.birthtime_ns + 1,
            )
            changed = _State(
                {
                    DOCX_MIME: [first_snapshot, replaced_second],
                    PDF_MIME: [],
                }
            )
            limited = DocxRoute(DocxRouteConfig(database, max_documents=1), changed, 2).run()

            self.assertEqual(limited.skipped_by_count, 1)
            self.assertEqual(limited.cache_hits, 1)
            self.assertEqual(limited.cache_documents_pruned, 1)
            with closing(sqlite3.connect(database)) as connection:
                cached_paths = connection.execute(
                    "SELECT path FROM documents ORDER BY path"
                ).fetchall()
                staged_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM docx_inventory WHERE path=?",
                    (str(second_path),),
                ).fetchone()[0]
            self.assertEqual(cached_paths, [(str(first_path),)])
            self.assertEqual(staged_birthtime, replaced_second.birthtime_ns)

    def test_reconcile_preserves_legacy_birthtime_until_candidate_is_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "A.docx"
            second_path = root / "B.docx"
            _make_docx(first_path, "Documento A")
            _make_docx(second_path, "Documento B")
            first_snapshot = snapshot_path(first_path)
            second_snapshot = replace(
                snapshot_path(second_path),
                birthtime_ns=second_path.stat().st_ctime_ns,
            )
            database = root / "docx.sqlite3"
            both = _State(
                {
                    DOCX_MIME: [first_snapshot, second_snapshot],
                    PDF_MIME: [],
                }
            )
            DocxRoute(DocxRouteConfig(database), both, 1).run()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE documents SET birthtime_ns=? WHERE path=?",
                    (UNKNOWN_BIRTHTIME_NS, str(second_path)),
                )
                connection.commit()

            limited = DocxRoute(DocxRouteConfig(database, max_documents=1), both, 2).run()

            self.assertEqual(limited.skipped_by_count, 1)
            self.assertEqual(limited.cache_hits, 1)
            self.assertEqual(limited.cache_documents_pruned, 0)
            with closing(sqlite3.connect(database)) as connection:
                preserved = connection.execute(
                    """SELECT d.birthtime_ns,d.last_seen_run_id,i.birthtime_ns
                    FROM documents d JOIN docx_inventory i USING(file_key)
                    WHERE d.path=?""",
                    (str(second_path),),
                ).fetchone()
                document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            self.assertEqual(document_count, 2)
            self.assertEqual(
                preserved,
                (UNKNOWN_BIRTHTIME_NS, 1, second_snapshot.birthtime_ns),
            )

            refreshed = DocxRoute(DocxRouteConfig(database), both, 3).run()

            self.assertEqual(refreshed.cache_hits, 1)
            self.assertEqual(refreshed.new_documents, 1)
            self.assertEqual(refreshed.extracted, 1)
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    "SELECT birthtime_ns,last_seen_run_id FROM documents WHERE path=?",
                    (str(second_path),),
                ).fetchone()
            self.assertEqual(stored, (second_snapshot.birthtime_ns, 3))

    def test_count_limit_preserves_live_cache_and_prunes_only_disappeared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "A.docx"
            second_path = root / "B.docx"
            _make_docx(first_path, "Documento A")
            _make_docx(second_path, "Documento B")
            database = root / "docx.sqlite3"
            both = _State(
                {
                    DOCX_MIME: [snapshot_path(first_path), snapshot_path(second_path)],
                    PDF_MIME: [],
                }
            )
            DocxRoute(DocxRouteConfig(database), both, 1).run()

            limited = DocxRoute(DocxRouteConfig(database, max_documents=1), both, 2).run()
            self.assertEqual(limited.skipped_by_count, 1)
            self.assertEqual(limited.cache_documents_pruned, 0)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    2,
                )
            finally:
                connection.close()

            only_first = _State({DOCX_MIME: [snapshot_path(first_path)], PDF_MIME: []})
            pruned = DocxRoute(DocxRouteConfig(database), only_first, 3).run()
            self.assertEqual(pruned.cache_documents_pruned, 1)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_count_limit_prioritizes_errors_before_old_complete_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete_path = root / "A-completo.docx"
            error_path = root / "Z-error.docx"
            _make_docx(complete_path, "Completo")
            _make_docx(error_path, "Reintentar")
            state = _State(
                {
                    DOCX_MIME: [
                        snapshot_path(complete_path),
                        snapshot_path(error_path),
                    ],
                    PDF_MIME: [],
                }
            )
            database = root / "docx.sqlite3"
            config = DocxRouteConfig(database)
            DocxRoute(config, state, 1).run()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE documents SET processing_signature='old' WHERE path=?",
                    (str(complete_path),),
                )
                connection.execute(
                    """UPDATE documents SET status='error',failure_code='prior_error',
                    review_disposition='deletion_candidate' WHERE path=?""",
                    (str(error_path),),
                )
                connection.commit()

            processed_paths = []

            def recording_extract(path, *args, **kwargs):
                processed_paths.append(str(path))
                return extract_docx(path, *args, **kwargs)

            with patch(
                "neocortex.capabilities.formats.docx.route.extract_docx",
                side_effect=recording_extract,
            ):
                limited = DocxRoute(
                    DocxRouteConfig(
                        database,
                        max_documents=1,
                        retry_errors=True,
                    ),
                    state,
                    2,
                ).run()

            self.assertEqual(processed_paths, [str(error_path)])
            self.assertEqual(limited.retried_documents, 1)
            self.assertEqual(limited.skipped_by_count, 1)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
