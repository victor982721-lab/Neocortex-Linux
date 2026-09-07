"""Bounded read-only inspection of semantic state."""
# region [00] Contexto del módulo
# Módulo: neocortex/semantic_status_service.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations
import sqlite3
from pathlib import Path

from . import semantic_schema
from .semantic_service_contracts import SEMANTIC_DATABASE_NAME, SemanticStatus
from .semantic_state import _generation_summary_rows, semantic_database
# endregion [01]

# region [02] Implementación


def _generation_timing_rows(
    connection: sqlite3.Connection,
    generation_ids: tuple[int, ...],
) -> dict[int, dict[str, int]]:
    """Aggregate receipt timing for selected generations on one read view."""

    if not generation_ids:
        return {}
    placeholders = ",".join("?" for _ in generation_ids)
    rows = connection.execute(
        f"""SELECT g.generation_id,g.started_ns,g.completed_ns,
            r.execution_mode,COUNT(r.receipt_id) AS receipts,
            COALESCE(SUM(r.duration_ns),0) AS duration_ns
        FROM embedding_generations g
        LEFT JOIN semantic_work_receipts r
          ON r.generation_id=g.generation_id
         AND r.stage_id='semantic.embedding'
         AND r.status='succeeded'
        WHERE g.generation_id IN ({placeholders})
        GROUP BY g.generation_id,r.execution_mode""",
        generation_ids,
    ).fetchall()
    timings: dict[int, dict[str, int]] = {}
    for row in rows:
        generation_id = int(row["generation_id"])
        timing = timings.setdefault(
            generation_id,
            {
                "generation_duration_ns": max(
                    0,
                    0 if row["completed_ns"] is None else int(row["completed_ns"])
                    - int(row["started_ns"]),
                ),
                "executed_ns": 0,
                "cache_hit_ns": 0,
                "replay_ns": 0,
                "receipts": 0,
            },
        )
        mode = row["execution_mode"]
        if mode in {"executed", "cache_hit", "replay"}:
            timing[f"{mode}_ns"] += int(row["duration_ns"])
        timing["receipts"] += int(row["receipts"])
    return timings


def _validate_status_schema(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    """Reject incompatible table shapes without running the full validator.

    The operator status path is intentionally cheaper than the full health
    validator, but it must not treat a database with an extra or missing
    column as a usable Semantic owner.  The exact contract is already
    materialized by the schema module and these bounded metadata probes avoid
    opening a second database or running one query per index.
    """

    expected = semantic_schema._canonical_contract(version)
    actual: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT m.name,p.name FROM sqlite_master AS m "
        "JOIN pragma_table_xinfo(m.name) AS p "
        "WHERE m.name NOT LIKE 'sqlite_%' AND m.type='table'"
    ):
        actual.setdefault(str(row[0]), set()).add(str(row[1]))
    expected_tables = set(expected.tables)
    if set(actual) != expected_tables:
        raise semantic_schema.SemanticStateError("semantic schema tables are incompatible")
    for table, contract in expected.tables.items():
        if actual[table] != set(contract.columns):
            raise semantic_schema.SemanticStateError(
                f"semantic schema incompatible columns for table {table!r}"
            )


def semantic_status(
    state_directory: Path,
    *,
    generation_limit: int,
) -> SemanticStatus:
    """Return bounded state counts without creating or migrating the database."""

    if not 1 <= generation_limit <= 1_000:
        raise ValueError("generation_limit must be between 1 and 1000")
    database = state_directory / SEMANTIC_DATABASE_NAME
    if not database.is_file():
        return SemanticStatus(False)
    with semantic_database(database, readonly=True) as connection:
        # Keep counts, selected generation identifiers, and their summaries on
        # one fenced read view.  A quiescent owner uses the zero-copy immutable
        # path, while a live WAL still falls back to one bounded temporary
        # snapshot; forcing the latter made a large, healthy semantic owner
        # impossible to inspect once its vector payload exceeded the default
        # scratch budget.
        connection.execute("BEGIN")
        schema_version = semantic_schema._read_schema_version(connection)
        if schema_version is None:
            raise ValueError("semantic schema is absent")
        _validate_status_schema(connection, schema_version)
        table_names = (
            "semantic_items",
            "text_channel_revisions",
            "text_chunks",
            "text_embeddings",
            "image_embeddings",
            "vector_payloads",
            "embedding_jobs",
            "label_prototypes",
            "semantic_evidence",
        )
        available_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: (
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if table in available_tables
                else 0
            )
            for table in table_names
        }
        generation_ids = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT generation_id FROM embedding_generations "
                "ORDER BY generation_id DESC LIMIT ?",
                (generation_limit,),
            )
        )
        generation_summaries = _generation_summary_rows(connection, generation_ids)
        generation_timings = (
            _generation_timing_rows(connection, generation_ids)
            if schema_version >= 7
            else {}
        )
    return SemanticStatus(
        True,
        schema_version,
        counts,
        generation_summaries,
        generation_timings,
    )
# endregion [02]
