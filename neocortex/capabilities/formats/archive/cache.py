"""Durable Archive cache and projection repair helpers.

The cache layer never opens the source ZIP.  It validates the durable member
representation before rebuilding the FTS projection and keeps path refreshes
set-based for replay performance.
"""

from __future__ import annotations

import sqlite3
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from neocortex.foundation.hash_compat import sha256
from neocortex.deduplication import FileSnapshot
from neocortex.foundation.file_identity import file_key_from_snapshot
from .contracts import ArchiveCacheInvalid as _ArchiveCacheInvalid
from .contracts import DEFAULT_MAX_TEXT_CHARS
from ..fts_lookup import (
    delete_format_fts_keys,
    format_fts_key_predicate,
    insert_format_fts_rows,
)

def _member_key(container_key: str, member_chain: str) -> str:
    digest = sha256.sha256_128_hexdigest(
        f"{container_key}\x00{member_chain}".encode("utf-8", "surrogatepass")
    )
    return f"archive:{digest}"


def _virtual_path(container_path: str, member_chain: str) -> str:
    return f"{container_path}!/{member_chain}" if member_chain else container_path


def _delete_container(connection: sqlite3.Connection, container_key: str) -> int:
    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM documents WHERE container_key=?", (container_key,)
        ).fetchone()[0]
    )
    keys = tuple(row[0] for row in connection.execute(
        "SELECT file_key FROM documents WHERE container_key=?", (container_key,)
    ))
    delete_format_fts_keys(connection, "document_fts", keys)
    connection.execute("DELETE FROM containers WHERE container_key=?", (container_key,))
    return count


def _cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
) -> sqlite3.Row | None:
    cached = connection.execute(
        """SELECT path,status,member_count,indexed_count,metadata_only_count,
        nested_archive_count,issue_count,text_chars,max_depth,retryable
        FROM containers WHERE container_key=? AND size=? AND mtime_ns=?
        AND birthtime_ns=? AND processing_signature=?""",
        (
            file_key_from_snapshot(snapshot),
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
        ),
    ).fetchone()
    if cached is not None and str(cached["status"]) not in {"complete", "partial", "error"}:
        return None
    if (
        cached is not None
        and Path(cached["path"]).suffix.casefold() != Path(snapshot.path).suffix.casefold()
        and connection.execute(
            """SELECT 1 FROM archive_logical_documents
            WHERE container_key=? AND member_chain=''""",
            (file_key_from_snapshot(snapshot),),
        ).fetchone()
        is not None
    ):
        # A user rename may resolve (or introduce) the root extension mismatch,
        # even though content identity and the extraction signature stayed equal.
        return None
    return cached


def _cached_archive_text(row: sqlite3.Row, *, max_text_chars: int) -> str:
    """Load one persisted member representation without opening its ZIP source."""

    compressed = row["text_zlib"]
    try:
        text_chars = int(row["text_chars"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text length is malformed"
        ) from exc
    if text_chars < 0 or text_chars > max_text_chars:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text length exceeds the cache bound"
        )
    digest = row["text_xxh3_128"]
    if compressed is None:
        if str(row["status"]) == "indexed" or text_chars != 0 or digest is not None:
            raise _ArchiveCacheInvalid(
                f"Archive member {row['file_key']} has an incomplete text representation"
            )
        return ""
    try:
        decoder = zlib.decompressobj()
        output_limit = text_chars * 4 + 1
        encoded = decoder.decompress(bytes(compressed), output_limit)
        if decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
            raise ValueError("compressed representation exceeded its recorded bound")
        text = encoded.decode("utf-8")
    except (TypeError, UnicodeError, ValueError, OverflowError, zlib.error) as exc:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text representation is unreadable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if len(text) != text_chars or digest is None:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text representation metadata is inconsistent"
        )
    if str(digest) != sha256.sha256_128_hexdigest(encoded):
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text representation fingerprint changed"
        )
    return text


def _cached_archive_fts_rows(
    connection: sqlite3.Connection,
    container_key: str,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    text_decoder: Callable[[sqlite3.Row], str] | None = None,
) -> tuple[tuple[str, str, str, str, str, str, str], ...]:
    """Materialize expected FTS rows from durable Archive member records."""

    container = connection.execute(
        """SELECT status,member_count,indexed_count,metadata_only_count
        FROM containers WHERE container_key=?""",
        (container_key,),
    ).fetchone()
    if container is None:
        raise _ArchiveCacheInvalid(f"Archive container {container_key} disappeared")
    rows = connection.execute(
        """SELECT file_key,path,container_path,member_chain,content_kind,status,
        text_zlib,text_chars,text_xxh3_128 FROM documents
        WHERE container_key=? ORDER BY member_chain COLLATE NOCASE,file_key""",
        (container_key,),
    ).fetchall()
    if str(container["status"]) in {"complete", "partial"}:
        try:
            member_count = int(container["member_count"])
            indexed_count = int(container["indexed_count"])
            metadata_only_count = int(container["metadata_only_count"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise _ArchiveCacheInvalid(
                f"Archive container {container_key} counters are malformed"
            ) from exc
        member_documents = sum(str(row["member_chain"]) != "" for row in rows)
        if (
            member_documents != member_count
            or indexed_count + metadata_only_count != member_count
        ):
            raise _ArchiveCacheInvalid(
                f"Archive container {container_key} durable member set is incomplete"
            )
    expected: list[tuple[str, str, str, str, str, str, str]] = []
    for row in rows:
        file_key = str(row["file_key"])
        container_path = str(row["container_path"])
        member_chain = str(row["member_chain"])
        path = str(row["path"])
        if path != _virtual_path(container_path, member_chain):
            raise _ArchiveCacheInvalid(
                f"Archive member {file_key} virtual path is inconsistent"
            )
        decoder = text_decoder or (lambda current: _cached_archive_text(
            current, max_text_chars=max_text_chars
        ))
        expected.append(
            (
                file_key,
                path,
                container_path,
                Path(container_path).name,
                member_chain,
                str(row["content_kind"]),
                decoder(row),
            )
        )
    return tuple(expected)


def _repair_cached_container_fts(
    connection: sqlite3.Connection,
    container_key: str,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    text_decoder: Callable[[sqlite3.Row], str] | None = None,
) -> int:
    """Repair only a damaged Archive FTS projection from durable member text."""

    expected = _cached_archive_fts_rows(
        connection,
        container_key,
        max_text_chars=max_text_chars,
        text_decoder=text_decoder,
    )
    expected_by_key = {row[0]: row for row in expected}
    actual_rows: list[sqlite3.Row] = []
    keys = tuple(row[0] for row in connection.execute(
        "SELECT file_key FROM documents WHERE container_key=?", (container_key,)
    ))
    for offset in range(0, len(keys), 500):
        predicate, parameters = format_fts_key_predicate(
            connection, "document_fts", keys[offset:offset + 500]
        )
        actual_rows.extend(connection.execute(
            f"""SELECT file_key,path,container_path,container_name,member_chain,
            content_kind,body FROM document_fts WHERE {predicate}""", parameters,
        ).fetchall())
    actual_by_key: dict[str, list[tuple[object, ...]]] = {}
    for row in actual_rows:
        actual_by_key.setdefault(str(row["file_key"]), []).append(
            (
                str(row["file_key"]),
                str(row["path"]),
                str(row["container_path"]),
                str(row["container_name"]),
                str(row["member_chain"]),
                str(row["content_kind"]),
                str(row["body"]),
            )
        )
    complete = len(actual_rows) == len(expected) and all(
        actual_by_key.get(file_key) == [expected_row]
        for file_key, expected_row in expected_by_key.items()
    )
    if complete:
        return 0

    delete_format_fts_keys(connection, "document_fts", keys)
    insert_format_fts_rows(
        connection, "document_fts",
        ("file_key", "path", "container_path", "container_name", "member_chain", "content_kind", "body"),
        expected,
    )
    return len(expected)


def _refresh_cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    run_id: int,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    text_decoder: Callable[[sqlite3.Row], str] | None = None,
) -> int:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    now = time.time_ns()
    connection.execute(
        "UPDATE containers SET path=?,last_seen_run_id=?,updated_ns=? WHERE container_key=?",
        (snapshot.path, run_id, now, container_key),
    )
    # A cache hit only changes the physical container path and run marker.  A
    # per-member Python loop used to issue two SQL statements for every member
    # (1,808 members on the current corpus), turning a no-work replay into the
    # dominant Archive route cost.  Keep the same path/FTS contract but let
    # SQLite update the whole container in two bounded set-based statements.
    connection.execute(
        """UPDATE documents SET
            path=? || CASE WHEN member_chain='' THEN '' ELSE '!/' || member_chain END,
            container_path=?,last_seen_run_id=?,updated_ns=?
        WHERE container_key=?""",
        (snapshot.path, snapshot.path, run_id, now, container_key),
    )
    return _repair_cached_container_fts(
        connection,
        container_key,
        max_text_chars=max_text_chars,
        text_decoder=text_decoder,
    )


def _prune_stale_containers(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[int, int]:
    rows = connection.execute(
        "SELECT container_key FROM containers WHERE last_seen_run_id<>?", (run_id,)
    ).fetchall()
    members = sum(_delete_container(connection, str(row[0])) for row in rows)
    return len(rows), members
