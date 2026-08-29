"""Bounded PDF staging, promotion, warning and failure persistence."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import json
import sqlite3
import time
import unicodedata
import zlib
from typing import Literal, Protocol, cast

import xxhash

from neocortex.deduplication import FileSnapshot
from neocortex.platform_policy import sqlite_path_collation

from .pdf_route_cache import RETRYABLE_PAGE_ERROR_SQL, file_key
from .pdf_route_models import PdfRouteConfig
from .pdf_runtime import ensure_free_space
from .pdf_state import pdf_database
from .pdf_writer import serialized_pdf_write
from _04_Nucleo_Operativo.retry_policy import retry_delay_seconds


# region [01] Persistence bounds and host contract

PROMOTION_BATCH_PAGES = 16
PROMOTION_BATCH_BYTES = 8 * 1024 * 1024
OCR_PROVENANCE_MAX_UTF8_BYTES = 64 * 1024
_PATH_COLLATION = sqlite_path_collation()


class _DocumentCacheDeleter(Protocol):
    def _delete_document_cache(
        self,
        connection: sqlite3.Connection,
        cache_key: str,
    ) -> int: ...


def normalize_pdf_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class PdfRouteStorageMixin:
    """SQLite repository responsibilities required by the PDF route."""

    config: PdfRouteConfig
    run_id: int

    def _check_disk(self) -> None:
        ensure_free_space(self.config.state_path.parent, self.config.min_free_bytes)

    # endregion [01]

    # region [02] Staging and page errors

    def _prepare_document(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        page_count: int,
        metadata: dict,
    ) -> None:
        key = file_key(snapshot)
        signature = self.config.processing_signature
        old = connection.execute(
            "SELECT size,mtime_ns,birthtime_ns,processing_signature,status,error_type "
            "FROM documents "
            "WHERE file_key=?",
            (key,),
        ).fetchone()
        source_changed = old is None or (
            int(old["size"]) != snapshot.size
            or int(old["mtime_ns"]) != snapshot.mtime_ns
            or int(old["birthtime_ns"]) != snapshot.birthtime_ns
            or old["processing_signature"] != signature
        )
        if source_changed:
            connection.execute("DELETE FROM page_staging WHERE file_key=?", (key,))
            connection.execute("DELETE FROM page_errors WHERE file_key=?", (key,))
            if old is not None:
                connection.execute(
                    "UPDATE documents SET transient_retry_count=0,next_retry_ns=NULL "
                    "WHERE file_key=?",
                    (key,),
                )
        elif old is not None and old["status"] in {"partial", "error"} and (
            old["status"] == "error"
            or old["error_type"]
            in {
                "PdfDocumentTimeout",
                "InterruptedPdfProcessing",
                "PdfPageSequenceAborted",
            }
            or self.config.retry_errors
            or bool(
                connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM page_errors WHERE file_key=? "
                    "AND processing_signature=? AND " + RETRYABLE_PAGE_ERROR_SQL + ")",
                    (key, signature),
                ).fetchone()[0]
            )
        ):
            connection.execute(
                "DELETE FROM page_staging WHERE file_key=? AND source='error'",
                (key,),
            )
            connection.execute(
                "DELETE FROM page_errors WHERE file_key=? AND processing_signature=?",
                (key, signature),
            )
            connection.execute(
                """UPDATE documents SET page_errors_count=0,
                completed_pages=(SELECT COUNT(*) FROM page_staging
                    WHERE file_key=? AND processing_signature=?),
                updated_ns=? WHERE file_key=?""",
                (key, signature, time.time_ns(), key),
            )
        stale_path_owners = connection.execute(
            "SELECT file_key FROM documents "
            f"WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>? ORDER BY file_key",
            (snapshot.path, key),
        ).fetchall()
        cache_deleter = cast(_DocumentCacheDeleter, self)
        for stale_owner in stale_path_owners:
            stale_key = str(stale_owner["file_key"])
            cache_deleter._delete_document_cache(connection, stale_key)
            connection.execute(
                "DELETE FROM documents WHERE file_key=?",
                (stale_key,),
            )
            # The shared cleanup is intentionally bounded by commits.  Publish
            # removal of the stale owner as the matching recoverable checkpoint
            # before attempting to create the replacement row.
            connection.commit()
        connection.execute(
            """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,page_count,metadata_json,last_seen_run_id,updated_ns)
                    VALUES(?,?,?,?,?,?,'processing',?,?,?,?)
                    ON CONFLICT(file_key) DO UPDATE SET
                    path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,
                    birthtime_ns=excluded.birthtime_ns,
                    processing_signature=excluded.processing_signature,status='processing',
                    page_count=excluded.page_count,metadata_json=excluded.metadata_json,
                    last_seen_run_id=excluded.last_seen_run_id,error_type=NULL,
                    error_message=NULL,updated_ns=excluded.updated_ns""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                page_count,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                self.run_id,
                time.time_ns(),
            ),
        )

    def _store_staging_page(
        self,
        connection: sqlite3.Connection,
        key: str,
        signature: str,
        page_number: int,
        source: str,
        text: str,
        ocr_provenance: dict[str, object] | None = None,
    ) -> None:
        encoded = zlib.compress(text.encode("utf-8"), level=3)
        provenance_json = (
            None
            if ocr_provenance is None
            else json.dumps(
                ocr_provenance,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        if (
            provenance_json is not None
            and len(provenance_json.encode("utf-8")) > OCR_PROVENANCE_MAX_UTF8_BYTES
        ):
            raise ValueError(
                f"PDF OCR provenance exceeds {OCR_PROVENANCE_MAX_UTF8_BYTES} UTF-8 bytes"
            )
        connection.execute(
            """INSERT OR REPLACE INTO page_staging(
                    file_key,processing_signature,page_number,source,text_zlib,text_chars,
                    ocr_provenance_json) VALUES(?,?,?,?,?,?,?)""",
            (
                key,
                signature,
                page_number,
                source,
                encoded,
                len(text),
                provenance_json,
            ),
        )
        connection.execute(
            "UPDATE documents SET completed_pages=(SELECT COUNT(*) FROM page_staging "
            "WHERE file_key=? AND processing_signature=?),updated_ns=? WHERE file_key=?",
            (key, signature, time.time_ns(), key),
        )

    def _restart_structural_recovery_attempt(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
    ) -> None:
        """Discard failed-page placeholders before the next recovery engine."""

        key = file_key(snapshot)
        signature = self.config.processing_signature
        connection.execute(
            "DELETE FROM page_errors WHERE file_key=? AND processing_signature=?",
            (key, signature),
        )
        connection.execute(
            "DELETE FROM page_staging WHERE file_key=? AND processing_signature=? "
            "AND source='error'",
            (key, signature),
        )
        connection.execute(
            """UPDATE documents SET page_errors_count=0,
            completed_pages=(SELECT COUNT(*) FROM page_staging
                WHERE file_key=? AND processing_signature=?),updated_ns=?
            WHERE file_key=?""",
            (key, signature, time.time_ns(), key),
        )

    def _store_page_failure(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        page_number: int,
        error_type: str,
        error_message: str,
    ) -> None:
        key = file_key(snapshot)
        signature = self.config.processing_signature
        connection.execute(
            """INSERT OR REPLACE INTO page_errors(
            file_key,processing_signature,page_number,error_type,error_message,updated_ns)
            VALUES(?,?,?,?,?,?)""",
            (
                key,
                signature,
                page_number,
                error_type,
                error_message,
                time.time_ns(),
            ),
        )
        self._store_staging_page(
            connection,
            key,
            signature,
            page_number,
            "error",
            "",
        )

    # endregion [02]

    # region [03] Bounded promotion

    def _promote_document(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        page_count: int,
        completed_pages: int,
        metadata: dict,
        binary_digest: str | None,
        *,
        status: Literal["done", "partial"],
        page_start: int,
        page_end: int,
        is_partial: bool,
        page_errors: int,
        document_error_type: str | None = None,
        document_error_message: str | None = None,
    ) -> None:
        key = file_key(snapshot)
        signature = self.config.processing_signature
        digest = xxhash.xxh3_128()
        normalized_chars = native_pages = ocr_pages = native_chars = ocr_chars = 0
        has_normalized_text = False
        rows = connection.execute(
            "SELECT page_number,source,text_zlib,text_chars FROM page_staging "
            "WHERE file_key=? AND processing_signature=? ORDER BY page_number",
            (key, signature),
        )
        for row in rows:
            text = zlib.decompress(row["text_zlib"]).decode("utf-8")
            normalized = normalize_pdf_text(text)
            if normalized:
                if has_normalized_text:
                    digest.update(b" ")
                    normalized_chars += 1
                digest.update(normalized.encode("utf-8"))
                normalized_chars += len(normalized)
                has_normalized_text = True
            if row["source"] == "ocr":
                ocr_pages += 1
                ocr_chars += int(row["text_chars"])
            elif row["source"] != "error":
                native_pages += 1
                native_chars += int(row["text_chars"])

        while connection.execute(
            "SELECT 1 FROM page_fts WHERE file_key=? LIMIT 1",
            (key,),
        ).fetchone():
            self._check_disk()
            connection.execute(
                "DELETE FROM page_fts WHERE rowid IN "
                "(SELECT rowid FROM page_fts WHERE file_key=? LIMIT 256)",
                (key,),
            )
            connection.commit()
        for table in ("page_fts_state", "pages"):
            while connection.execute(
                f"SELECT 1 FROM {table} WHERE file_key=? LIMIT 1",
                (key,),
            ).fetchone():
                self._check_disk()
                connection.execute(
                    f"DELETE FROM {table} WHERE file_key=? AND page_number IN "
                    f"(SELECT page_number FROM {table} WHERE file_key=? LIMIT 256)",
                    (key, key),
                )
                connection.commit()
        connection.execute("DELETE FROM text_signatures WHERE file_key=?", (key,))
        connection.commit()

        page_batch: list[tuple[str, int, str, bytes, int, str | None]] = []
        batch_bytes = 0
        staging_rows = connection.execute(
            "SELECT page_number,source,text_zlib,text_chars,ocr_provenance_json "
            "FROM page_staging "
            "WHERE file_key=? AND processing_signature=? ORDER BY page_number",
            (key, signature),
        )
        for row in staging_rows:
            blob = bytes(row["text_zlib"])
            page_batch.append(
                (
                    key,
                    int(row["page_number"]),
                    str(row["source"]),
                    blob,
                    int(row["text_chars"]),
                    (
                        str(row["ocr_provenance_json"])
                        if row["ocr_provenance_json"] is not None
                        else None
                    ),
                )
            )
            batch_bytes += len(blob)
            if len(page_batch) >= PROMOTION_BATCH_PAGES or batch_bytes >= PROMOTION_BATCH_BYTES:
                self._check_disk()
                connection.executemany(
                    "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars,"
                    "ocr_provenance_json) VALUES(?,?,?,?,?,?)",
                    page_batch,
                )
                connection.commit()
                page_batch.clear()
                batch_bytes = 0
        if page_batch:
            self._check_disk()
            connection.executemany(
                "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars,"
                "ocr_provenance_json) VALUES(?,?,?,?,?,?)",
                page_batch,
            )
            connection.commit()

        prior_retry = connection.execute(
            "SELECT transient_retry_count FROM documents WHERE file_key=?",
            (key,),
        ).fetchone()
        has_retryable_page_error = status == "partial" and bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM page_errors WHERE file_key=? "
                "AND processing_signature=? AND " + RETRYABLE_PAGE_ERROR_SQL + ")",
                (key, signature),
            ).fetchone()[0]
        )
        retry_count = (
            (0 if prior_retry is None else int(prior_retry[0])) + 1
            if has_retryable_page_error
            else 0
        )
        next_retry_ns = (
            time.time_ns() + retry_delay_seconds(retry_count) * 1_000_000_000
            if has_retryable_page_error
            else None
        )

        with connection:
            connection.execute(
                """UPDATE documents SET path=?,size=?,mtime_ns=?,birthtime_ns=?,status=?,
                    page_count=?,completed_pages=?,native_pages=?,ocr_pages=?,native_chars=?,
                    ocr_chars=?,normalized_text_xxh3_128=?,normalized_text_chars=?,
                    binary_xxh3_128=?,page_start=?,page_end=?,is_partial=?,
                    page_errors_count=?,metadata_json=?,profile_version=NULL,
                    template_simhash64=NULL,
                    error_type=?,error_message=?,transient_retry_count=?,
                    next_retry_ns=?,updated_ns=? WHERE file_key=?""",
                (
                    snapshot.path,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    status,
                    page_count,
                    completed_pages,
                    native_pages,
                    ocr_pages,
                    native_chars,
                    ocr_chars,
                    digest.hexdigest() if has_normalized_text else None,
                    normalized_chars,
                    binary_digest,
                    page_start,
                    page_end,
                    int(is_partial),
                    page_errors,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    document_error_type,
                    document_error_message,
                    retry_count,
                    next_retry_ns,
                    time.time_ns(),
                    key,
                ),
            )
        if status == "done":
            while connection.execute(
                "SELECT 1 FROM page_staging WHERE file_key=? LIMIT 1",
                (key,),
            ).fetchone():
                self._check_disk()
                connection.execute(
                    "DELETE FROM page_staging WHERE file_key=? AND page_number IN "
                    "(SELECT page_number FROM page_staging WHERE file_key=? LIMIT 256)",
                    (key, key),
                )
                connection.commit()

    # endregion [03]

    # region [04] Warning and failure evidence

    def _store_document_warnings(
        self,
        snapshot: FileSnapshot,
        stage: str,
        warning_count: int,
        samples: tuple[str, ...],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Replace bounded native-warning evidence for one processing stage."""

        def write(target: sqlite3.Connection) -> None:
            key = file_key(snapshot)
            signature = self.config.processing_signature
            if warning_count <= 0:
                target.execute(
                    "DELETE FROM document_warnings WHERE file_key=? "
                    "AND processing_signature=? AND stage=?",
                    (key, signature, stage),
                )
                return
            target.execute(
                """INSERT OR REPLACE INTO document_warnings(
                file_key,processing_signature,stage,warning_count,samples_json,updated_ns)
                VALUES(?,?,?,?,?,?)""",
                (
                    key,
                    signature,
                    stage,
                    warning_count,
                    json.dumps(samples, ensure_ascii=True, separators=(",", ":")),
                    time.time_ns(),
                ),
            )

        if connection is not None:
            write(connection)
            return
        with serialized_pdf_write(), pdf_database(self.config.state_path) as target:
            write(target)

    def _store_failure(
        self,
        snapshot: FileSnapshot,
        status: str,
        error_type: str,
        error_message: str,
        *,
        transient: bool = False,
        reset_retry_count: bool = False,
    ) -> None:
        with serialized_pdf_write(), pdf_database(self.config.state_path) as connection:
            key = file_key(snapshot)
            signature = self.config.processing_signature
            old = connection.execute(
                "SELECT size,mtime_ns,birthtime_ns,processing_signature,"
                "transient_retry_count FROM documents WHERE file_key=?",
                (key,),
            ).fetchone()
            source_changed = old is not None and (
                int(old["size"]) != snapshot.size
                or int(old["mtime_ns"]) != snapshot.mtime_ns
                or int(old["birthtime_ns"]) != snapshot.birthtime_ns
                or old["processing_signature"] != signature
            )
            if source_changed:
                connection.execute("DELETE FROM page_staging WHERE file_key=?", (key,))
                connection.execute("DELETE FROM page_errors WHERE file_key=?", (key,))
                connection.execute(
                    "DELETE FROM document_warnings WHERE file_key=?",
                    (key,),
                )
            prior_retry_count = (
                0 if old is None or source_changed else int(old["transient_retry_count"])
            )
            retry_count = (
                0 if transient and reset_retry_count else prior_retry_count + 1 if transient else 0
            )
            if not transient or reset_retry_count:
                next_retry_ns = None
            else:
                next_retry_ns = time.time_ns() + retry_delay_seconds(retry_count) * 1_000_000_000
            connection.execute(
                """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,error_type,
                    error_message,transient_retry_count,next_retry_ns,last_seen_run_id,
                    updated_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,size=excluded.size,
                    mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
                    processing_signature=excluded.processing_signature,
                    status=excluded.status,error_type=excluded.error_type,
                    error_message=excluded.error_message,
                    transient_retry_count=excluded.transient_retry_count,
                    next_retry_ns=excluded.next_retry_ns,
                    last_seen_run_id=excluded.last_seen_run_id,
                    updated_ns=excluded.updated_ns""",
                (
                    key,
                    snapshot.path,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    signature,
                    status,
                    error_type,
                    error_message,
                    retry_count,
                    next_retry_ns,
                    self.run_id,
                    time.time_ns(),
                ),
            )


# endregion [04]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_route_storage")
