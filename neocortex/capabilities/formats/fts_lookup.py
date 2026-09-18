"""Exact connection-local identity lookups for a format's sole FTS writer.

FTS5 indexes words, not equality on ``file_key``.  A temporary rowid map keeps
replay proportional to a document's own rows.  It preserves duplicate and
malformed keys and participates in the caller's transactions.  Readers and
standalone helper callers retain the original SQL fallback.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence


def _lookup_name(table: str) -> str:
    if table not in {"document_fts", "transcript_fts", "frame_fts"}:
        raise ValueError(f"unsupported format FTS table: {table}")
    return f"_format_{table}_row_lookup"


def _has_lookup(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name=?",
        (_lookup_name(table),),
    ).fetchone() is not None


def initialize_format_fts_lookup(
    connection: sqlite3.Connection,
    table: str,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Build once under route ownership, without changing durable schema."""

    if connection.in_transaction:
        raise ValueError("format FTS lookup initialization requires an idle connection")
    lookup = _lookup_name(table)
    connection.execute("BEGIN")
    try:
        connection.execute(f"DROP TABLE IF EXISTS temp.{lookup}")
        connection.execute(
            f"CREATE TEMP TABLE {lookup}(fts_rowid INTEGER PRIMARY KEY,file_key)"
        )
        cursor = connection.execute(f"SELECT rowid,file_key FROM main.{table}")
        while rows := cursor.fetchmany(1_024):
            if checkpoint is not None:
                checkpoint()
            connection.executemany(
                f"INSERT INTO temp.{lookup}(fts_rowid,file_key) VALUES(?,?)", rows
            )
        interrupted: BaseException | None = None

        def check_index_progress() -> int:
            nonlocal interrupted
            try:
                if checkpoint is not None:
                    checkpoint()
            except BaseException as exc:
                interrupted = exc
                return 1
            return 0

        if checkpoint is not None:
            connection.set_progress_handler(check_index_progress, 1_000)
        try:
            connection.execute(
                f"CREATE INDEX temp.{lookup}_key_idx ON {lookup}(file_key)"
            )
        except sqlite3.Error:
            if interrupted is not None:
                raise interrupted from None
            raise
        finally:
            if checkpoint is not None:
                connection.set_progress_handler(None, 0)
        if checkpoint is not None:
            checkpoint()
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def format_fts_key_predicate(
    connection: sqlite3.Connection,
    table: str,
    file_keys: Sequence[object],
) -> tuple[str, tuple[object, ...]]:
    """Find every physical row for these identities, including duplicates."""

    lookup = _lookup_name(table)
    keys = tuple(file_keys)
    if not keys:
        return "0", ()
    placeholders = ",".join("?" for _ in keys)
    if _has_lookup(connection, table):
        return (
            f"rowid IN (SELECT fts_rowid FROM temp.{lookup} "
            f"WHERE file_key IN ({placeholders})) "
            f"AND file_key IN ({placeholders})",
            keys + keys,
        )
    return f"file_key IN ({placeholders})", keys


def delete_format_fts_keys(
    connection: sqlite3.Connection,
    table: str,
    file_keys: Sequence[object],
) -> None:
    lookup = _lookup_name(table)
    keys = tuple(dict.fromkeys(file_keys))
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset + 500]
        predicate, parameters = format_fts_key_predicate(connection, table, batch)
        connection.execute(f"DELETE FROM {table} WHERE {predicate}", parameters)
        if _has_lookup(connection, table):
            placeholders = ",".join("?" for _ in batch)
            connection.execute(
                f"DELETE FROM temp.{lookup} WHERE file_key IN ({placeholders})", batch
            )


def insert_format_fts_row(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    values: Sequence[object],
) -> None:
    lookup = _lookup_name(table)
    if not columns or any(not column.isidentifier() for column in columns):
        raise ValueError("invalid format FTS columns")
    if "file_key" not in columns or len(columns) != len(values):
        raise ValueError("format FTS insertion requires its exact file_key")
    cursor = connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(values),
    )
    if _has_lookup(connection, table):
        connection.execute(
            f"INSERT INTO temp.{lookup}(fts_rowid,file_key) VALUES(?,?)",
            (cursor.lastrowid, values[columns.index("file_key")]),
        )


def insert_format_fts_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> None:
    for row in rows:
        insert_format_fts_row(connection, table, columns, row)


def refresh_format_fts_path(
    connection: sqlite3.Connection,
    table: str,
    file_key: str,
    path: str,
) -> None:
    """Avoid reindexing unchanged text when only a run marker was refreshed."""

    predicate, parameters = format_fts_key_predicate(connection, table, (file_key,))
    for row in connection.execute(
        f"SELECT rowid,path FROM {table} WHERE {predicate}", parameters
    ).fetchall():
        if row[1] != path:
            connection.execute(
                f"UPDATE {table} SET path=? WHERE rowid=? AND file_key=?",
                (path, row[0], file_key),
            )
