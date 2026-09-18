"""Connection-local rowid lookup for Text's sole route writer.

FTS5 does not index equality on ``file_key``.  Build this temporary lookup once
under the route lock, preserve every FTS row (including duplicate keys), and
maintain it in the same transactions as the FTS writes.  Other connections keep
the original read queries; no durable schema or materialization identity changes.
"""

from __future__ import annotations

import sqlite3


def initialize_text_fts_lookup(connection: sqlite3.Connection) -> None:
    """Build an exact writer-local lookup and leave the connection idle."""

    if connection.in_transaction:
        raise ValueError("Text FTS lookup initialization requires an idle connection")
    connection.execute("BEGIN")
    try:
        connection.execute("DROP TABLE IF EXISTS temp._text_fts_row_lookup")
        # Preserve anomalous NULL/value types as well as duplicate file keys.
        # Only the physical FTS rowid is unique; this is not a repair operation.
        connection.execute(
            "CREATE TEMP TABLE _text_fts_row_lookup("
            "fts_rowid INTEGER PRIMARY KEY,file_key)"
        )
        connection.execute(
            "INSERT INTO temp._text_fts_row_lookup(fts_rowid,file_key) "
            "SELECT rowid,file_key FROM main.document_fts"
        )
        connection.execute(
            "CREATE INDEX temp._text_fts_row_lookup_file_key_idx "
            "ON _text_fts_row_lookup(file_key)"
        )
        _initialize_text_lineage_lookups(connection)
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _has_text_fts_lookup(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_temp_master "
            "WHERE type='table' AND name='_text_fts_row_lookup'"
        ).fetchone()
        is not None
    )


def text_fts_file_key_predicate(
    connection: sqlite3.Connection,
    file_keys: tuple[str, ...],
    *,
    lookup_available: bool | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return a bounded FTS predicate, with unchanged reader fallback."""

    if not file_keys:
        return "0", ()
    placeholders = ",".join("?" for _key in file_keys)
    if lookup_available is None:
        lookup_available = _has_text_fts_lookup(connection)
    if lookup_available:
        return (
            "rowid IN (SELECT fts_rowid FROM temp._text_fts_row_lookup "
            f"WHERE file_key IN ({placeholders})) "
            f"AND file_key IN ({placeholders})",
            file_keys + file_keys,
        )
    return f"file_key IN ({placeholders})", file_keys


def record_text_fts_row(
    connection: sqlite3.Connection,
    fts_rowid: int,
    file_key: str,
) -> None:
    """Record an inserted FTS row in its caller-owned transaction."""

    if _has_text_fts_lookup(connection):
        connection.execute(
            "INSERT INTO temp._text_fts_row_lookup(fts_rowid,file_key) VALUES(?,?)",
            (fts_rowid, file_key),
        )


def delete_text_fts_for_file_key(connection: sqlite3.Connection, file_key: str) -> None:
    """Delete all matching rows, including duplicates, with transactional lookup."""

    predicate, parameters = text_fts_file_key_predicate(connection, (file_key,))
    connection.execute(f"DELETE FROM document_fts WHERE {predicate}", parameters)
    if _has_text_fts_lookup(connection):
        connection.execute("DELETE FROM temp._text_fts_row_lookup WHERE file_key=?", (file_key,))


def prune_text_fts_for_run(connection: sqlite3.Connection, run_id: int) -> None:
    """Prune before the caller deletes stale documents, in the same transaction."""

    if _has_text_fts_lookup(connection):
        connection.execute(
            "DELETE FROM document_fts WHERE rowid IN ("
            "SELECT l.fts_rowid FROM temp._text_fts_row_lookup AS l "
            "JOIN main.documents AS d ON d.file_key=l.file_key "
            "WHERE d.last_seen_run_id<>?) "
            "AND file_key IN (SELECT file_key FROM main.documents WHERE last_seen_run_id<>?)",
            (run_id, run_id),
        )
        connection.execute(
            "DELETE FROM temp._text_fts_row_lookup WHERE file_key IN "
            "(SELECT file_key FROM main.documents WHERE last_seen_run_id<>?)",
            (run_id,),
        )
    else:
        connection.execute(
            "DELETE FROM document_fts WHERE file_key IN "
            "(SELECT file_key FROM main.documents WHERE last_seen_run_id<>?)",
            (run_id,),
        )


def _initialize_text_lineage_lookups(connection: sqlite3.Connection) -> None:
    """Index joins that use a different key from the durable table primary key."""

    available = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' "
            "AND name IN ('text_materialization_heads','text_materializations')"
        )
    }
    specifications = (
        (
            "text_materialization_heads",
            "_text_materialization_heads_lookup",
            (
                "resource_id", "materialization_kind", "materialization_owner",
                "materialization_id", "revision_id", "producer_receipt_id", "updated_ns",
            ),
            ("resource_id", "materialization_kind"),
            ("materialization_owner", "materialization_id"),
        ),
        (
            "text_materializations",
            "_text_materializations_lookup",
            ("owner", "materialization_id", "revision_id"),
            ("owner", "materialization_id"),
            ("revision_id",),
        ),
    )
    for source, lookup, columns, primary_key, lookup_key in specifications:
        if source not in available:
            continue
        for operation in ("insert", "delete", "update"):
            connection.execute(f"DROP TRIGGER IF EXISTS temp.{lookup}_{operation}")
        connection.execute(f"DROP TABLE IF EXISTS temp.{lookup}")
        projection = ",".join(columns)
        connection.execute(
            f"CREATE TEMP TABLE {lookup} AS SELECT {projection} FROM main.{source}"
        )
        connection.execute(
            f"CREATE UNIQUE INDEX temp.{lookup}_pk ON {lookup}({','.join(primary_key)})"
        )
        # This index is deliberately non-unique: multiple corrupt head aliases
        # must still reach the existing integrity checks, never collapse here.
        connection.execute(
            f"CREATE INDEX temp.{lookup}_key ON {lookup}({','.join(lookup_key)})"
        )
        insert = (
            f"INSERT INTO {lookup}({projection}) "
            f"VALUES({','.join('NEW.' + column for column in columns)});"
        )
        delete = (
            f"DELETE FROM {lookup} WHERE "
            + " AND ".join(f"{column}=OLD.{column}" for column in primary_key)
            + ";"
        )
        for operation, body in (("INSERT", insert), ("DELETE", delete), ("UPDATE", delete + insert)):
            connection.execute(
                f"CREATE TEMP TRIGGER {lookup}_{operation.lower()} "
                f"AFTER {operation} ON main.{source} BEGIN {body} END"
            )


def text_route_lookups_available(connection: sqlite3.Connection) -> bool:
    """Check once per validation operation, then pass the result through it."""

    row = connection.execute(
        "SELECT COUNT(*) FROM sqlite_temp_master WHERE type='table' AND name IN ("
        "'_text_fts_row_lookup','_text_materialization_heads_lookup',"
        "'_text_materializations_lookup')"
    ).fetchone()
    return row is not None and int(row[0]) == 3
