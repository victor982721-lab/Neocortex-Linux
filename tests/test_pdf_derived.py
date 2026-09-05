from __future__ import annotations


import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from neocortex.capabilities.formats.pdf.pdf_derived import (
    PdfDerivedIndexer,
    initialize_derived_schema,
)
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.platform.policy import sqlite_path_collation


TEST_CAPABILITIES = ('documents',)


# region [01] Linear FTS/state reconciliation


class PdfDerivedRepairTests(unittest.TestCase):
    def test_finalizes_from_persisted_pages_without_reprofiling_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pdf.sqlite3"
            initialize_pdf_state(database)
            profile = {
                "width": 600.0,
                "height": 800.0,
                "rotation": 0,
                "font_names": ["Arial"],
                "font_count": 1,
                "image_count": 0,
                "drawing_count": 0,
                "text_block_count": 1,
            }
            layout = {
                "source_kind": "text",
                "geometry_simhash64": "0000000000000001",
                "visual_simhash64": "0000000000000002",
                "header_simhash64": "0000000000000003",
                "footer_simhash64": "0000000000000004",
                "layout_simhash64": "0000000000000005",
                "visual_error": False,
                "header_ink": 0,
                "footer_ink": 0,
            }
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,page_count,completed_pages,last_seen_run_id,updated_ns)
                    VALUES('doc','doc.pdf',10,1,1,'sig','done',1,1,7,1)"""
                )
                connection.execute(
                    """INSERT INTO pages(
                    file_key,page_number,source,text_zlib,text_chars,profile_json)
                    VALUES('doc',0,'native',?,4,?)""",
                    (
                        zlib.compress(b"text"),
                        json.dumps(profile, separators=(",", ":")),
                    ),
                )
                connection.execute(
                    """INSERT INTO page_layouts(
                    file_key,page_number,algorithm_version,source_kind,
                    geometry_simhash64,visual_simhash64,header_simhash64,
                    footer_simhash64,layout_simhash64,layout_zlib,updated_ns)
                    VALUES('doc',0,1,'text',?,?,?,?,?,?,1)""",
                    (
                        layout["geometry_simhash64"],
                        layout["visual_simhash64"],
                        layout["header_simhash64"],
                        layout["footer_simhash64"],
                        layout["layout_simhash64"],
                        zlib.compress(json.dumps(layout, separators=(",", ":")).encode()),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            indexer = PdfDerivedIndexer(
                database,
                7,
                workers=1,
                similarity_threshold=0.8,
            )
            self.assertTrue(indexer._store_profiles("doc", ()))

            connection = sqlite3.connect(database)
            try:
                document = connection.execute(
                    "SELECT profile_version,template_simhash64 FROM documents"
                ).fetchone()
                layout_row = connection.execute(
                    "SELECT mapped_pages FROM document_layouts"
                ).fetchone()
                page_layout_count = connection.execute(
                    "SELECT COUNT(*) FROM page_layouts"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(document[0], 2)
            self.assertIsNotNone(document[1])
            self.assertEqual(layout_row, (1,))
            self.assertEqual(page_layout_count, (1,))

    def test_profile_candidates_defer_incomplete_extraction_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pdf.sqlite3"
            initialize_pdf_state(database)
            connection = sqlite3.connect(database)
            try:
                connection.executemany(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,page_count,completed_pages,error_type,last_seen_run_id,updated_ns)
                    VALUES(?,?,?,?,?,'sig',?,?,?,?,7,1)""",
                    (
                        ("small", "small.pdf", 10, 1, 1, "done", 1, 1, None),
                        (
                            "timeout",
                            "timeout.pdf",
                            1,
                            1,
                            1,
                            "partial",
                            5000,
                            3000,
                            "PdfDocumentTimeout",
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            indexer = PdfDerivedIndexer(
                database,
                7,
                workers=1,
                similarity_threshold=0.8,
            )
            self.assertEqual(
                list(indexer._profile_candidates()),
                [("small", "small.pdf", 10)],
            )
            self.assertEqual(indexer._profile_candidate_count(), 1)

    @unittest.skipUnless(
        sqlite_path_collation() == "BINARY",
        "case-distinct path pagination is the POSIX contract",
    )
    def test_profile_candidate_keyset_keeps_case_distinct_boundary_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pdf.sqlite3"
            initialize_pdf_state(database)
            rows = [
                (
                    f"prefix-{ordinal:04d}",
                    f"/fixture/A{ordinal:04d}.pdf",
                    10,
                    1,
                    1,
                    "sig",
                    "done",
                    7,
                    1,
                )
                for ordinal in range(999)
            ]
            rows.extend(
                (
                    ("upper", "/fixture/Case.pdf", 10, 1, 1, "sig", "done", 7, 1),
                    ("lower", "/fixture/case.pdf", 10, 1, 1, "sig", "done", 7, 1),
                )
            )
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,last_seen_run_id,updated_ns)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    rows,
                )

            indexer = PdfDerivedIndexer(
                database,
                7,
                workers=1,
                similarity_threshold=0.8,
            )
            candidates = list(indexer._profile_candidates())

            self.assertEqual(len(candidates), 1001)
            self.assertEqual(
                {candidate[1] for candidate in candidates[-2:]},
                {"/fixture/Case.pdf", "/fixture/case.pdf"},
            )

    def test_layout_groups_stream_relations_in_two_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pdf.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE documents(file_key TEXT PRIMARY KEY);
                CREATE TABLE pages(
                    file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
                    PRIMARY KEY(file_key,page_number)
                ) WITHOUT ROWID;
                """
            )
            initialize_derived_schema(connection)
            connection.execute("INSERT INTO similarity_state VALUES('layout','active',0.8,2,7,3)")
            connection.executemany(
                "INSERT INTO similarity_relations VALUES(?,?,?,?,?,?,?)",
                (
                    (7, "a", "b", "layout_similar", 0.9, 2, "{}"),
                    (7, "b", "c", "layout_similar", 0.8, 2, "{}"),
                    (7, "d", "e", "layout_similar", 0.95, 2, "{}"),
                ),
            )
            connection.commit()
            connection.close()

            indexer = PdfDerivedIndexer(
                database,
                7,
                workers=1,
                similarity_threshold=0.8,
            )
            self.assertEqual(indexer._build_layout_groups(), 2)

            connection = sqlite3.connect(database)
            try:
                groups = connection.execute(
                    "SELECT representative_file_key,member_count,minimum_edge_score "
                    "FROM layout_groups ORDER BY member_count DESC"
                ).fetchall()
                member_count = connection.execute(
                    "SELECT COUNT(*) FROM layout_group_members"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(groups, [("b", 3, 0.8), ("d", 2, 0.95)])
            self.assertEqual(member_count, 5)

    def test_repairs_fts_and_state_without_losing_consistent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "pdf.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE pages(
                    file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
                    PRIMARY KEY(file_key,page_number)
                ) WITHOUT ROWID;
                CREATE VIRTUAL TABLE page_fts USING fts5(
                    file_key UNINDEXED,path UNINDEXED,page_number UNINDEXED,text
                );
                CREATE TABLE page_fts_state(
                    file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
                    text_xxh3_128 TEXT NOT NULL,
                    PRIMARY KEY(file_key,page_number)
                ) WITHOUT ROWID;
                INSERT INTO pages VALUES('complete',0),('missing-fts',0),('missing-state',0);
                INSERT INTO page_fts(file_key,path,page_number,text) VALUES
                    ('complete','a.pdf',0,'a'),
                    ('missing-page','b.pdf',0,'b'),
                    ('missing-state','c.pdf',0,'c');
                INSERT INTO page_fts_state VALUES
                    ('complete',0,'a'),
                    ('missing-page',0,'b'),
                    ('missing-fts',0,'c');
                """
            )
            connection.commit()
            connection.close()

            indexer = object.__new__(PdfDerivedIndexer)
            indexer.state_path = database
            self.assertEqual(indexer._repair_fts_state(), 4)

            connection = sqlite3.connect(database)
            try:
                fts_keys = connection.execute(
                    "SELECT file_key,page_number FROM page_fts"
                ).fetchall()
                state_keys = connection.execute(
                    "SELECT file_key,page_number FROM page_fts_state"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(fts_keys, [("complete", 0)])
            self.assertEqual(state_keys, [("complete", 0)])


# endregion [01]


if __name__ == "__main__":
    unittest.main()
