"""Transactional revision of graph inputs and immutable publication rows."""

from __future__ import annotations

import sqlite3


GRAPH_REVISION_KEY = "code_graph_revision_v1"
_TABLES = (
    "files", "file_versions", "symbols", "code_references", "dependencies",
    "diagnostics", "metrics", "code_chunks", "projects", "project_memberships",
    "project_edges", "version_relations", "graph_input_snapshots",
    "graph_snapshot_inputs", "graph_generations", "graph_batches",
    "graph_memberships", "graph_checkpoints", "graph_heads",
    "graph_generation_metadata", "graph_generation_migrations", "schema_migrations",
)
_OBSERVATION_COLUMNS = {
    "files": frozenset({"last_seen_run_id"}),
    "file_versions": frozenset({"last_observed_run_id"}),
}


def install_graph_revision_guards(connection: sqlite3.Connection) -> None:
    """Install v8's guards under the owner's schema-migration transaction."""

    connection.execute("INSERT INTO metadata(key,value) VALUES(?,'0')", (GRAPH_REVISION_KEY,))
    body = (
        "UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
        f"WHERE key='{GRAPH_REVISION_KEY}' AND length(value) BETWEEN 1 AND 18 "
        "AND value NOT GLOB '*[^0-9]*'; "
        "SELECT CASE WHEN changes()<>1 THEN RAISE(ABORT,'Code graph revision is invalid') END;"
    )
    for table in _TABLES:
        ignored = _OBSERVATION_COLUMNS.get(table, frozenset())
        columns = tuple(
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            if str(row[1]) not in ignored
        )
        if not columns:
            raise RuntimeError(f"Code graph revision source is missing: {table}")
        for operation in ("INSERT", "DELETE", "UPDATE"):
            when = ""
            if operation == "UPDATE":
                quoted = tuple('"' + column.replace('"', '""') + '"' for column in columns)
                when = " WHEN " + " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in quoted)
            connection.execute(
                f"CREATE TRIGGER code_graph_revision_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table}{when} BEGIN {body} END"
            )


def graph_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (GRAPH_REVISION_KEY,)).fetchone()
    value = None if row is None else row[0]
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 18:
        raise RuntimeError("Code graph revision is missing or invalid")
    return int(value)
