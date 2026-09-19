"""Bounded text-prefix reads from an already selected SQLite source view."""

from __future__ import annotations

import codecs
import sqlite3
from typing import TYPE_CHECKING
import zlib

from .document_catalog_models import SourceDocument

if TYPE_CHECKING:
    from neocortex.runtime.control.cancellation import CancellationToken


class _TextPrefix:
    """Retain an exact joined prefix, charging separators to the same limit."""

    def __init__(self, maximum: int) -> None:
        self.remaining = maximum
        self.chunks: list[str] = []

    @property
    def text_capacity(self) -> int:
        return max(0, self.remaining - bool(self.chunks))

    def append(self, text: str) -> None:
        if self.chunks:
            self.remaining -= 1
        part = text[:self.remaining]
        self.chunks.append(part)
        self.remaining -= len(part)

    def finish(self) -> str:
        return "\n".join(self.chunks)


def _sqlite_text_encoding(connection: sqlite3.Connection) -> str:
    # CAST(TEXT AS BLOB) uses the database encoding, which need not be UTF-8.
    # A normal TEXT substr stops at NUL and silently drops later evidence.
    encoding = str(connection.execute("PRAGMA encoding").fetchone()[0]).lower()
    return {"utf-8": "utf-8", "utf-16le": "utf-16-le", "utf-16be": "utf-16-be"}[encoding]


def _decode_sqlite_text_prefix(value: bytes | None, encoding: str, maximum: int) -> str:
    if value is None:
        return ""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    # Only a bounded read may end partway through a valid Unicode unit. A
    # shorter result is complete and must reject an incomplete stored tail.
    return decoder.decode(value, final=len(value) < maximum * 4 + 4)[:maximum]


def _load_leading_text(
    connection: sqlite3.Connection,
    document: SourceDocument,
    *,
    max_text_chars: int,
    cancellation: CancellationToken | None = None,
) -> str:
    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    if cancellation is None:
        cancellation = current_worker_cancellation()
    if cancellation is not None:
        cancellation.checkpoint()
    if max_text_chars <= 0:
        return ""
    if document.source_kind == "video":
        # Video OCR is stored in FTS rows rather than a document blob.  Read a
        # bounded prefix in timestamp order so a long recording cannot turn a
        # catalog pass into an unbounded memory operation.
        video_prefix = _TextPrefix(max_text_chars)
        encoding = _sqlite_text_encoding(connection)
        rows = connection.execute(
            """SELECT substr(CAST(body AS BLOB),1,?) FROM frame_fts WHERE file_key=?
            ORDER BY timestamp_ms,rowid""",
            (max_text_chars * 4 + 4, document.file_key),
        )
        for row in rows:
            if cancellation is not None:
                cancellation.checkpoint()
            text = _decode_sqlite_text_prefix(row[0], encoding, max_text_chars)
            if text:
                video_prefix.append(text)
                if video_prefix.remaining == 0:
                    break
        return video_prefix.finish()
    if document.source_kind == "code":
        # Code keeps the current file version and may retain either the
        # bounded source blob or chunk rows, depending on the analyzer.  The
        # fallback preserves useful path/symbol evidence without inventing a
        # complete source when the producer only published text-only output.
        raw_file_id = document.file_key.removeprefix("code:")
        try:
            file_id = int(raw_file_id)
        except ValueError as exc:
            raise RuntimeError("code catalog identity is malformed") from exc
        row = connection.execute(
            """SELECT v.version_id,v.text_zlib IS NOT NULL AS has_text FROM files AS f
            JOIN file_versions AS v ON v.version_id=f.current_version_id
            WHERE f.file_id=? AND f.status='current'""",
            (file_id,),
        ).fetchone()
        if row is None:
            return ""
        if row["has_text"]:
            return _read_compressed_text_prefix(
                connection, "file_versions", "text_zlib", "version_id=?", (int(row["version_id"]),), max_text_chars,
                cancellation=cancellation,
            )
        code_prefix = _TextPrefix(max_text_chars)
        encoding = _sqlite_text_encoding(connection)
        for chunk in connection.execute(
            """SELECT substr(CAST(text AS BLOB),1,?) FROM code_chunks WHERE version_id=?
            ORDER BY chunk_index""",
            (max_text_chars * 4 + 4, int(row["version_id"])),
        ):
            if cancellation is not None:
                cancellation.checkpoint()
            text = _decode_sqlite_text_prefix(chunk[0], encoding, max_text_chars)
            if text:
                code_prefix.append(text)
                if code_prefix.remaining == 0:
                    break
        return code_prefix.finish()
    if document.source_kind == "image":
        return _read_compressed_text_prefix(
            connection, "images", "ocr_text_zlib", "file_key=?", (document.file_key,), max_text_chars,
            cancellation=cancellation,
        )
    if document.source_kind != "pdf":
        return _read_compressed_text_prefix(
            connection, "documents", "text_zlib", "file_key=?", (document.file_key,), max_text_chars,
            cancellation=cancellation,
        )
    pdf_prefix = _TextPrefix(max_text_chars)
    rows = connection.execute(
        """SELECT page_number FROM pages WHERE file_key=?
        ORDER BY page_number""",
        (document.file_key,),
    )
    for row in rows:
        if cancellation is not None:
            cancellation.checkpoint()
        text = "" if pdf_prefix.text_capacity == 0 else _read_compressed_text_prefix(
            connection, "pages", "text_zlib", "file_key=? AND page_number=?",
            (document.file_key, int(row[0])), pdf_prefix.text_capacity,
            cancellation=cancellation,
        )
        pdf_prefix.append(text)
        if pdf_prefix.remaining == 0:
            break
    return pdf_prefix.finish()


def _read_compressed_text_prefix(
    connection: sqlite3.Connection, table: str, column: str,
    predicate: str, parameters: tuple[object, ...], max_chars: int,
    *, cancellation: CancellationToken | None = None,
) -> str:
    """Bound both compressed input and decoded prefix in the reader process.

    SQL identifiers come only from the fixed owner adapters above. Repeated
    bounded substr reads also handle streams with many empty deflate blocks;
    a fixed compressed-prefix guess would silently truncate those inputs.
    """

    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    if cancellation is None:
        cancellation = current_worker_cancellation()
    decoder = zlib.decompressobj()
    decoded = bytearray()
    remaining = max_chars * 4 + 4
    offset = 1
    while remaining > 0 and not decoder.eof:
        if cancellation is not None:
            cancellation.checkpoint()
        row = connection.execute(
            f"SELECT substr({column},?,?) FROM {table} WHERE {predicate}",
            (offset, 64 * 1024, *parameters),
        ).fetchone()
        if row is None or row[0] is None or not row[0]:
            break
        compressed = bytes(row[0])
        offset += len(compressed)
        chunk = decoder.decompress(compressed, remaining)
        decoded.extend(chunk)
        remaining -= len(chunk)
    utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    return utf8_decoder.decode(decoded, final=False)[:max_chars]


def _decompress_prefix(blob: bytes, max_chars: int) -> str:
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(blob, max_chars * 4 + 4)
    utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    text = utf8_decoder.decode(decoded, final=False)
    return text[:max_chars]
