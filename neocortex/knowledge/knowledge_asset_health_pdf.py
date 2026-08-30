"""Bounded immutable PDF evidence for one physical Knowledge identity.

This reader deliberately projects only structural state.  It never opens the
corpus, decompresses page text, or returns raw metadata and diagnostic strings.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    read_application_schema_version,
)

from neocortex.foundation.file_identity import encode_file_identity
from .knowledge_asset_health_contracts import KnowledgeAssetIdentity
from neocortex.capabilities.formats.pdf.pdf_schema import PDF_SCHEMA_VERSION, validate_pdf_schema
from _04_Nucleo_Operativo.sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database


PDF_STRUCTURAL_RECOVERY_VERSION = "pdf-structural-recovery-v2"
PDF_STRUCTURAL_RECOVERY_ENGINES = frozenset({"pdfminer", "qpdf+pymupdf"})
_MAX_PDF_IDENTITY_ROWS = 3


class PdfHealthOwnerReadIssue(RuntimeError):
    """A PDF owner cannot supply current immutable evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PdfHealthRecord:
    """One bounded structural projection of a schema-13 PDF document."""

    file_key: str
    path: str
    size: int
    mtime_ns: int
    birthtime_ns: int
    processing_signature: str
    status: str
    page_count: int | None
    completed_pages: int
    native_pages: int
    ocr_pages: int
    normalized_text_xxh3_128: str | None
    normalized_text_chars: int
    page_start: int | None
    page_end: int | None
    is_partial: bool
    page_errors_count: int
    last_seen_run_id: int | None
    updated_ns: int
    persisted_pages: int
    first_page_number: int | None
    last_page_number: int | None
    staging_pages: int
    persisted_page_errors: int
    warning_rows: int
    warning_count: int
    fts_state_rows: int
    fts_rows: int
    staging_projection_exact: bool
    page_error_projection_exact: bool
    fts_projection_exact: bool
    metadata_valid: bool
    recovery_present: bool
    recovery_engine: str | None
    recovery_version: str | None
    recovery_recognized: bool

    @property
    def full_range(self) -> bool:
        """Whether the persisted page interval represents the complete PDF."""

        if self.page_count is None or self.page_count < 0:
            return False
        if self.page_count == 0:
            return (
                self.completed_pages == 0
                and self.persisted_pages == 0
                and (self.page_start, self.page_end) in {(None, None), (1, 0)}
            )
        return self.page_start == 1 and self.page_end == self.page_count

    @property
    def selected_range_is_coherent(self) -> bool:
        """Whether the selected range and structural page count agree."""

        if self.page_count is None or self.page_count < 0 or self.completed_pages < 0:
            return False
        if self.completed_pages == 0:
            return (
                self.persisted_pages == 0
                and self.first_page_number is None
                and self.last_page_number is None
                and (self.page_count == 0 or self.status in {"error", "processing", "protected"})
            )
        if self.page_start is None or self.page_end is None:
            return False
        if not 1 <= self.page_start <= self.page_end <= self.page_count:
            return False
        return (
            self.page_end - self.page_start + 1 == self.completed_pages
            and self.persisted_pages == self.completed_pages
            and self.first_page_number == self.page_start - 1
            and self.last_page_number == self.page_end - 1
        )


def _file_key_candidates(identity: KnowledgeAssetIdentity) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                encode_file_identity(identity.volume_id, identity.file_id),
                f"{identity.volume_id}:{identity.file_id}",
            }
        )
    )


def _sqlite_error_is_corrupt(exc: sqlite3.Error) -> bool:
    return getattr(exc, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }


def _pdf_rows(
    connection: sqlite3.Connection,
    identity: KnowledgeAssetIdentity,
) -> tuple[sqlite3.Row, ...]:
    candidates = _file_key_candidates(identity)
    placeholders = ",".join("?" for _ in candidates)
    return tuple(
        connection.execute(
            f"""SELECT d.file_key,d.path,d.size,d.mtime_ns,d.birthtime_ns,
            d.processing_signature,d.status,d.page_count,d.completed_pages,
            d.native_pages,d.ocr_pages,d.normalized_text_xxh3_128,
            d.normalized_text_chars,d.page_start,
            d.page_end,d.is_partial,d.page_errors_count,d.last_seen_run_id,
            d.updated_ns,
            (SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key)
                AS persisted_pages,
            (SELECT MIN(p.page_number) FROM pages p WHERE p.file_key=d.file_key)
                AS first_page_number,
            (SELECT MAX(p.page_number) FROM pages p WHERE p.file_key=d.file_key)
                AS last_page_number,
            (SELECT COUNT(*) FROM page_staging s WHERE s.file_key=d.file_key
                AND s.processing_signature=d.processing_signature) AS staging_pages,
            (SELECT COUNT(*) FROM page_errors e WHERE e.file_key=d.file_key
                AND e.processing_signature=d.processing_signature)
                AS persisted_page_errors,
            (SELECT COUNT(*) FROM document_warnings w WHERE w.file_key=d.file_key
                AND w.processing_signature=d.processing_signature) AS warning_rows,
            (SELECT COALESCE(SUM(w.warning_count),0) FROM document_warnings w
                WHERE w.file_key=d.file_key
                AND w.processing_signature=d.processing_signature) AS warning_count,
            (SELECT COUNT(*) FROM page_fts_state f WHERE f.file_key=d.file_key)
                AS fts_state_rows,
            (SELECT COUNT(*) FROM page_fts f WHERE f.file_key=d.file_key)
                AS fts_rows,
            CASE WHEN NOT EXISTS(
                    SELECT p.page_number FROM pages p WHERE p.file_key=d.file_key
                    EXCEPT SELECT s.page_number FROM page_staging s
                    WHERE s.file_key=d.file_key
                    AND s.processing_signature=d.processing_signature)
                AND NOT EXISTS(
                    SELECT s.page_number FROM page_staging s
                    WHERE s.file_key=d.file_key
                    AND s.processing_signature=d.processing_signature
                    EXCEPT SELECT p.page_number FROM pages p WHERE p.file_key=d.file_key)
                THEN 1 ELSE 0 END AS staging_projection_exact,
            CASE WHEN NOT EXISTS(
                    SELECT e.page_number FROM page_errors e
                    WHERE e.file_key=d.file_key
                    AND e.processing_signature=d.processing_signature
                    EXCEPT SELECT p.page_number FROM pages p
                    WHERE p.file_key=d.file_key AND p.source='error')
                AND NOT EXISTS(
                    SELECT p.page_number FROM pages p
                    WHERE p.file_key=d.file_key AND p.source='error'
                    EXCEPT SELECT e.page_number FROM page_errors e
                    WHERE e.file_key=d.file_key
                    AND e.processing_signature=d.processing_signature)
                THEN 1 ELSE 0 END AS page_error_projection_exact,
            CASE WHEN NOT EXISTS(
                    SELECT p.page_number FROM pages p WHERE p.file_key=d.file_key
                    EXCEPT SELECT s.page_number FROM page_fts_state s
                    WHERE s.file_key=d.file_key)
                AND NOT EXISTS(
                    SELECT s.page_number FROM page_fts_state s
                    WHERE s.file_key=d.file_key
                    EXCEPT SELECT p.page_number FROM pages p WHERE p.file_key=d.file_key)
                AND NOT EXISTS(
                    SELECT p.page_number FROM pages p WHERE p.file_key=d.file_key
                    EXCEPT SELECT CAST(f.page_number AS INTEGER) FROM page_fts f
                    WHERE f.file_key=d.file_key)
                AND NOT EXISTS(
                    SELECT CAST(f.page_number AS INTEGER) FROM page_fts f
                    WHERE f.file_key=d.file_key
                    EXCEPT SELECT p.page_number FROM pages p WHERE p.file_key=d.file_key)
                AND (SELECT COUNT(*) FROM page_fts f WHERE f.file_key=d.file_key)
                    = (SELECT COUNT(DISTINCT CAST(f.page_number AS INTEGER))
                       FROM page_fts f WHERE f.file_key=d.file_key)
                THEN 1 ELSE 0 END AS fts_projection_exact,
            CASE WHEN d.metadata_json IS NULL THEN 1
                ELSE json_valid(d.metadata_json) END AS metadata_valid,
            CASE WHEN d.metadata_json IS NOT NULL AND json_valid(d.metadata_json)
                THEN json_type(d.metadata_json,'$.neocortex_recovery') IS NOT NULL
                ELSE 0 END AS recovery_present,
            CASE WHEN d.metadata_json IS NOT NULL AND json_valid(d.metadata_json)
                AND json_type(d.metadata_json,'$.neocortex_recovery.engine')='text'
                THEN json_extract(d.metadata_json,'$.neocortex_recovery.engine')
                ELSE NULL END AS recovery_engine,
            CASE WHEN d.metadata_json IS NOT NULL AND json_valid(d.metadata_json)
                AND json_type(d.metadata_json,'$.neocortex_recovery.recovery_version')='text'
                THEN json_extract(
                    d.metadata_json,'$.neocortex_recovery.recovery_version')
                ELSE NULL END AS recovery_version,
            CASE WHEN d.metadata_json IS NOT NULL AND json_valid(d.metadata_json)
                AND json_type(d.metadata_json,'$.neocortex_recovery')='object'
                AND json_type(d.metadata_json,'$.neocortex_recovery.engine')='text'
                AND json_extract(d.metadata_json,'$.neocortex_recovery.engine')
                    IN ('pdfminer','qpdf+pymupdf')
                AND json_type(
                    d.metadata_json,'$.neocortex_recovery.recovery_version')='text'
                AND json_extract(
                    d.metadata_json,'$.neocortex_recovery.recovery_version')=?
                THEN 1 ELSE 0 END AS recovery_recognized
            FROM documents d WHERE d.file_key IN ({placeholders})
            ORDER BY d.file_key LIMIT ?""",
            (
                PDF_STRUCTURAL_RECOVERY_VERSION,
                *candidates,
                _MAX_PDF_IDENTITY_ROWS,
            ),
        ).fetchall()
    )


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("PDF optional integer projection is invalid")
    return value


def _record(row: sqlite3.Row) -> PdfHealthRecord:
    record = PdfHealthRecord(
        file_key=str(row["file_key"]),
        path=str(row["path"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
        processing_signature=str(row["processing_signature"]),
        status=str(row["status"]).casefold(),
        page_count=_optional_int(row["page_count"]),
        completed_pages=int(row["completed_pages"]),
        native_pages=int(row["native_pages"]),
        ocr_pages=int(row["ocr_pages"]),
        normalized_text_xxh3_128=(
            None
            if row["normalized_text_xxh3_128"] is None
            else str(row["normalized_text_xxh3_128"])
        ),
        normalized_text_chars=int(row["normalized_text_chars"]),
        page_start=_optional_int(row["page_start"]),
        page_end=_optional_int(row["page_end"]),
        is_partial=bool(int(row["is_partial"])),
        page_errors_count=int(row["page_errors_count"]),
        last_seen_run_id=_optional_int(row["last_seen_run_id"]),
        updated_ns=int(row["updated_ns"]),
        persisted_pages=int(row["persisted_pages"]),
        first_page_number=_optional_int(row["first_page_number"]),
        last_page_number=_optional_int(row["last_page_number"]),
        staging_pages=int(row["staging_pages"]),
        persisted_page_errors=int(row["persisted_page_errors"]),
        warning_rows=int(row["warning_rows"]),
        warning_count=int(row["warning_count"]),
        fts_state_rows=int(row["fts_state_rows"]),
        fts_rows=int(row["fts_rows"]),
        staging_projection_exact=bool(int(row["staging_projection_exact"])),
        page_error_projection_exact=bool(int(row["page_error_projection_exact"])),
        fts_projection_exact=bool(int(row["fts_projection_exact"])),
        metadata_valid=bool(int(row["metadata_valid"])),
        recovery_present=bool(int(row["recovery_present"])),
        recovery_engine=(None if row["recovery_engine"] is None else str(row["recovery_engine"])),
        recovery_version=(
            None if row["recovery_version"] is None else str(row["recovery_version"])
        ),
        recovery_recognized=bool(int(row["recovery_recognized"])),
    )
    integer_counts = (
        record.size,
        record.completed_pages,
        record.native_pages,
        record.ocr_pages,
        record.normalized_text_chars,
        record.page_errors_count,
        record.persisted_pages,
        record.staging_pages,
        record.persisted_page_errors,
        record.warning_rows,
        record.warning_count,
        record.fts_state_rows,
        record.fts_rows,
    )
    if any(value < 0 for value in integer_counts):
        raise ValueError("PDF structural counts cannot be negative")
    return record


def read_pdf_health_records(
    path: Path,
    identity: KnowledgeAssetIdentity,
) -> tuple[PdfHealthRecord, ...]:
    """Read at most two packed/legacy identity rows from immutable schema-13 state."""

    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    if not isinstance(identity, KnowledgeAssetIdentity):
        raise TypeError("identity must be KnowledgeAssetIdentity")
    try:
        with immutable_sqlite_database(path) as connection:
            observed = read_application_schema_version(connection, label="pdf")
            if observed is None:
                raise PdfHealthOwnerReadIssue("schema_version_absent")
            if observed > PDF_SCHEMA_VERSION:
                raise PdfHealthOwnerReadIssue("future")
            if observed < PDF_SCHEMA_VERSION:
                raise PdfHealthOwnerReadIssue("incompatible")
            validate_pdf_schema(connection)
            rows = _pdf_rows(connection, identity)
            return tuple(_record(row) for row in rows)
    except FileNotFoundError as exc:
        raise PdfHealthOwnerReadIssue("absent") from exc
    except ImmutableSQLiteUnavailable as exc:
        raise PdfHealthOwnerReadIssue("not_quiescent") from exc
    except PdfHealthOwnerReadIssue:
        raise
    except SQLiteSchemaContractError as exc:
        raise PdfHealthOwnerReadIssue("schema_invalid") from exc
    except sqlite3.Error as exc:
        code = "corrupt" if _sqlite_error_is_corrupt(exc) else "read_failed"
        raise PdfHealthOwnerReadIssue(code) from exc
    except (RuntimeError, TypeError, ValueError) as exc:
        raise PdfHealthOwnerReadIssue("schema_invalid") from exc


__all__ = [
    "PDF_STRUCTURAL_RECOVERY_ENGINES",
    "PDF_STRUCTURAL_RECOVERY_VERSION",
    "PdfHealthOwnerReadIssue",
    "PdfHealthRecord",
    "read_pdf_health_records",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.knowledge_asset_health_pdf")
