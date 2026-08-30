from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from pathlib import Path

from neocortex.capabilities.formats.pdf.pdf_admin import doctor_pdf_runtime, verify_pdf_state
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state


# region [01] PDF administrative diagnostics
# Validate healthy state, corruption reporting and OCR-independent runtime checks.


class PdfAdminTests(unittest.TestCase):
    def test_verify_streams_page_payloads_and_reports_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            initialize_pdf_state(state)
            encoded = zlib.compress("Interruptor de potencia".encode("utf-8"))
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,processing_signature,status,page_count,
                    completed_pages,last_seen_run_id,updated_ns)
                    VALUES('key','one.pdf',1,1,'test','done',1,1,1,1)"""
                )
                connection.execute(
                    """INSERT INTO pages(
                    file_key,page_number,source,text_zlib,text_chars,profile_json)
                    VALUES('key',0,'native',?,23,NULL)""",
                    (encoded,),
                )
                connection.execute("INSERT INTO page_fts_state VALUES('key',0,'digest')")
                connection.commit()
            self.assertTrue(verify_pdf_state(state).ok)

            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    "UPDATE pages SET text_zlib=? WHERE file_key='key'",
                    (b"not-zlib",),
                )
                connection.commit()
            report = verify_pdf_state(state)
            self.assertFalse(report.ok)
            self.assertEqual(report.corrupt_page_payloads, 1)

    def test_doctor_without_ocr_checks_integrated_dependencies(self):
        report = doctor_pdf_runtime(ocr_mode="never")
        self.assertTrue(report.ok, report.checks)
        self.assertEqual(
            {check.name for check in report.checks},
            {"pymupdf", "pdfminer", "sqlite-fts5"},
        )


# endregion [01]


if __name__ == "__main__":
    unittest.main()
