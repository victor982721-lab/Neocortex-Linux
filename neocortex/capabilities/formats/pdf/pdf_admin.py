"""Explicit diagnostics and integrity verification for PDF framework state."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import json
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

from neocortex.safety.ocr_profiles import OcrProfileName, resolve_ocr_profile
from .pdf_state import connect_pdf_state
from neocortex.foundation.processing_provenance import resolve_tesseract_runtime


# region [01] Result models
# Return structured results so the CLI is only a presentation adapter.


@dataclass(frozen=True, slots=True)
class PdfCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PdfDoctorReport:
    checks: tuple[PdfCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


@dataclass(frozen=True, slots=True)
class PdfVerifyReport:
    quick_check: str
    foreign_key_errors: int
    page_count_mismatches: int
    page_error_mismatches: int
    missing_fts_pages: int
    orphan_fts_pages: int
    corrupt_page_payloads: int
    missing_layout_pages: int
    orphan_layout_pages: int
    corrupt_layout_payloads: int

    @property
    def ok(self) -> bool:
        return (
            self.quick_check == "ok"
            and self.foreign_key_errors == 0
            and self.page_count_mismatches == 0
            and self.page_error_mismatches == 0
            and self.missing_fts_pages == 0
            and self.orphan_fts_pages == 0
            and self.corrupt_page_payloads == 0
            and self.missing_layout_pages == 0
            and self.orphan_layout_pages == 0
            and self.corrupt_layout_payloads == 0
        )


# endregion [01]


# region [02] Runtime diagnostics
# Check only dependencies and capabilities actually used by the integrated route.


def doctor_pdf_runtime(
    *,
    ocr_mode: str = "auto",
    ocr_lang: str = "spa+eng",
    ocr_profile: OcrProfileName = "configured",
    tesseract_cmd: str | None = None,
    tessdata_dir: str | None = None,
) -> PdfDoctorReport:
    checks: list[PdfCheck] = []

    try:
        import fitz  # type: ignore[import-untyped]

        detail = getattr(fitz, "VersionBind", "available")
        checks.append(PdfCheck("pymupdf", True, str(detail)))
    except Exception as exc:
        checks.append(PdfCheck("pymupdf", False, f"{type(exc).__name__}: {exc}"))

    try:
        from pdfminer.high_level import extract_pages  # noqa: F401

        checks.append(PdfCheck("pdfminer", True, "available"))
    except Exception as exc:
        checks.append(PdfCheck("pdfminer", False, f"{type(exc).__name__}: {exc}"))

    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE probe_fts USING fts5(text)")
        finally:
            connection.close()
        checks.append(PdfCheck("sqlite-fts5", True, sqlite3.sqlite_version))
    except Exception as exc:
        checks.append(PdfCheck("sqlite-fts5", False, f"{type(exc).__name__}: {exc}"))

    if ocr_mode != "never":
        try:
            import pytesseract  # type: ignore[import-untyped]  # noqa: F401

            plan = resolve_ocr_profile(ocr_profile, ocr_lang)
            runtime = resolve_tesseract_runtime(
                command=tesseract_cmd,
                tessdata_dir=tessdata_dir,
                language=plan.required_language_spec,
                timeout_seconds=30.0,
            )
            if not runtime.available:
                raise RuntimeError(runtime.unavailable_reason or "Tesseract runtime unavailable")
            resolved_hashes = sum(
                digest is not None for _language, digest in runtime.traineddata_hashes
            )
            checks.append(
                PdfCheck(
                    "tesseract",
                    True,
                    f"profile={ocr_profile} languages={plan.required_language_spec} "
                    f"traineddata_hashes={resolved_hashes}",
                )
            )
        except Exception as exc:
            checks.append(PdfCheck("tesseract", False, f"{type(exc).__name__}: {exc}"))

    return PdfDoctorReport(tuple(checks))


# endregion [02]


# region [03] Persistent-state verification
# Stream compressed pages during verification instead of loading the corpus into memory.


def verify_pdf_state(path: Path) -> PdfVerifyReport:
    if not path.is_file():
        raise FileNotFoundError(f"PDF state database does not exist: {path}")
    connection = connect_pdf_state(path, readonly=True)
    try:
        quick_values: list[str] = []
        for row_number, row in enumerate(connection.execute("PRAGMA quick_check")):
            if row_number >= 100:
                quick_values.append("additional quick_check errors omitted")
                break
            quick_values.append(str(row[0]))
        quick_check = "ok" if quick_values == ["ok"] else "; ".join(quick_values)
        foreign_key_errors = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        page_count_mismatches = int(
            connection.execute(
                """SELECT COUNT(*) FROM documents d
                WHERE d.status IN ('done','partial') AND
                d.completed_pages<>(SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key)"""
            ).fetchone()[0]
        )
        page_error_mismatches = int(
            connection.execute(
                """SELECT COUNT(*) FROM documents d
                WHERE d.status IN ('done','partial') AND d.page_errors_count<>(
                    SELECT COUNT(*) FROM page_errors e WHERE e.file_key=d.file_key
                    AND e.processing_signature=d.processing_signature)"""
            ).fetchone()[0]
        )
        missing_fts_pages = int(
            connection.execute(
                """SELECT COUNT(*) FROM pages p JOIN documents d ON d.file_key=p.file_key
                WHERE d.status IN ('done','partial') AND NOT EXISTS(
                SELECT 1 FROM page_fts_state s
                WHERE s.file_key=p.file_key AND s.page_number=p.page_number)"""
            ).fetchone()[0]
        )
        orphan_fts_pages = int(
            connection.execute(
                """SELECT COUNT(*) FROM page_fts_state s WHERE NOT EXISTS(
                SELECT 1 FROM pages p WHERE p.file_key=s.file_key
                AND p.page_number=s.page_number)"""
            ).fetchone()[0]
        )
        corrupt_page_payloads = 0
        for row in connection.execute("SELECT text_zlib FROM pages"):
            try:
                zlib.decompress(row[0]).decode("utf-8")
            except (UnicodeDecodeError, zlib.error):
                corrupt_page_payloads += 1
        layout_schema_available = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_layouts'"
            ).fetchone()
            is not None
        )
        missing_layout_pages = orphan_layout_pages = corrupt_layout_payloads = 0
        if layout_schema_available:
            missing_layout_pages = int(
                connection.execute(
                    """SELECT COUNT(*) FROM pages p JOIN documents d USING(file_key)
                    WHERE d.status IN ('done','partial')
                    AND COALESCE(d.profile_version,0)>=2 AND NOT EXISTS(
                        SELECT 1 FROM page_layouts l WHERE l.file_key=p.file_key
                        AND l.page_number=p.page_number)"""
                ).fetchone()[0]
            )
            orphan_layout_pages = int(
                connection.execute(
                    """SELECT COUNT(*) FROM page_layouts l WHERE NOT EXISTS(
                        SELECT 1 FROM pages p WHERE p.file_key=l.file_key
                        AND p.page_number=l.page_number)"""
                ).fetchone()[0]
            )
            for row in connection.execute("SELECT layout_zlib FROM page_layouts"):
                try:
                    decoded = json.loads(zlib.decompress(row[0]).decode("utf-8"))
                    if not isinstance(decoded, dict) or "layout_simhash64" not in decoded:
                        corrupt_layout_payloads += 1
                except (UnicodeDecodeError, zlib.error, json.JSONDecodeError):
                    corrupt_layout_payloads += 1
        return PdfVerifyReport(
            quick_check,
            foreign_key_errors,
            page_count_mismatches,
            page_error_mismatches,
            missing_fts_pages,
            orphan_fts_pages,
            corrupt_page_payloads,
            missing_layout_pages,
            orphan_layout_pages,
            corrupt_layout_payloads,
        )
    finally:
        connection.close()


# endregion [03]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_admin")
