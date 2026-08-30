"""Bounded read-only inspection of semantic state."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/semantic_status_service.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from pathlib import Path

from .semantic_service_contracts import SEMANTIC_DATABASE_NAME, SemanticStatus
from .semantic_state import _generation_summary_rows, semantic_database
# endregion [01]

# region [02] Implementación


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
        # one WAL read snapshot.  Reopening per generation both produced N+1
        # connections and allowed a concurrent publication/prune to mix views.
        connection.execute("BEGIN")
        version_row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
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
    return SemanticStatus(
        True,
        None if version_row is None else int(version_row[0]),
        counts,
        generation_summaries,
    )
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.semantic_status_service")
