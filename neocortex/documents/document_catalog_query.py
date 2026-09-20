"""Bounded read-only queries over a published document catalog.

This module owns only the projection/query side of the catalog.  Publication,
classification corrections, migrations, and source fences remain in
``document_catalog`` and retain their existing SQLite owner.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import ContextManager

from .document_catalog_models import CatalogDocumentView


def catalog_query_predicates(
    columns: set[str],
    *,
    primary_kind: str | None,
    authority: str | None,
    organization: str | None,
    client: str | None,
    project: str | None,
    workstream: str | None,
) -> tuple[list[str], list[object]] | None:
    """Build bounded filters, abstaining when a legacy column is absent."""

    clauses = ["active=1"]
    parameters: list[object] = []
    for column, value in (
        ("primary_kind", primary_kind),
        ("primary_authority", authority),
        ("primary_organization", organization),
    ):
        if value is not None:
            clauses.append(f"{column}=? COLLATE NOCASE")
            parameters.append(value)
    for column, value in (
        ("primary_client", client),
        ("primary_project", project),
        ("primary_workstream", workstream),
    ):
        if value is None:
            continue
        if column not in columns:
            return None
        clauses.append(f"{column}=? COLLATE NOCASE")
        parameters.append(value)
    return clauses, parameters


def catalog_projection_column(columns: set[str], column: str, fallback: str) -> str:
    """Select a current column or a bounded legacy-compatible fallback."""

    return column if column in columns else f"{fallback} AS {column}"


def catalog_document_rows(
    connection: sqlite3.Connection,
    columns: set[str],
    clauses: list[str],
    parameters: list[object],
    limit: int,
) -> list[sqlite3.Row]:
    """Read one bounded, deterministic page from the catalog projection."""

    subtype_column = catalog_projection_column(columns, "primary_subtype", "NULL")
    equipment_column = catalog_projection_column(columns, "equipment_json", "'[]'")
    activities_column = catalog_projection_column(columns, "activities_json", "'[]'")
    client_column = catalog_projection_column(columns, "primary_client", "NULL")
    project_column = catalog_projection_column(columns, "primary_project", "NULL")
    workstream_column = catalog_projection_column(columns, "primary_workstream", "NULL")
    clients_column = catalog_projection_column(columns, "clients_json", "'[]'")
    projects_column = catalog_projection_column(columns, "projects_json", "'[]'")
    workstreams_column = catalog_projection_column(columns, "workstreams_json", "'[]'")
    return connection.execute(
        f"""SELECT source_kind,path,primary_kind,{subtype_column},
        primary_authority,primary_organization,{client_column},{project_column},
        {workstream_column},standard_references_json,{clients_column},
        {projects_column},{workstreams_column},
        topics_json,{equipment_column},{activities_column},
        confidence,uncertainty,catalog_status FROM documents
        WHERE {" AND ".join(clauses)}
        ORDER BY primary_kind,primary_client,primary_project,
        primary_authority,primary_organization,path
        LIMIT ?""",
        (*parameters, limit),
    ).fetchall()


def _optional_text(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    return None if value is None else str(value)


def _json_labels(value: object, key: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        str(item[key])
        for item in decoded
        if isinstance(item, dict) and isinstance(item.get(key), str)
    )


def catalog_document_view(row: sqlite3.Row) -> CatalogDocumentView:
    """Convert one SQLite row to the stable public catalog projection."""

    return CatalogDocumentView(
        source_kind=str(row["source_kind"]),
        path=str(row["path"]),
        primary_kind=str(row["primary_kind"]),
        primary_subtype=_optional_text(row, "primary_subtype"),
        primary_authority=_optional_text(row, "primary_authority"),
        primary_organization=_optional_text(row, "primary_organization"),
        primary_client=_optional_text(row, "primary_client"),
        primary_project=_optional_text(row, "primary_project"),
        primary_workstream=_optional_text(row, "primary_workstream"),
        standard_identifiers=_json_labels(row["standard_references_json"], "identifier"),
        clients=_json_labels(row["clients_json"], "label"),
        projects=_json_labels(row["projects_json"], "label"),
        workstreams=_json_labels(row["workstreams_json"], "label"),
        topics=_json_labels(row["topics_json"], "label"),
        equipment=_json_labels(row["equipment_json"], "label"),
        activities=_json_labels(row["activities_json"], "label"),
        confidence=float(row["confidence"]),
        uncertainty=str(row["uncertainty"]),
        catalog_status=str(row["catalog_status"]),
    )


def read_catalog_documents(
    catalog_path: Path,
    *,
    limit: int,
    primary_kind: str | None = None,
    authority: str | None = None,
    organization: str | None = None,
    client: str | None = None,
    project: str | None = None,
    workstream: str | None = None,
    open_catalog: Callable[..., ContextManager[sqlite3.Connection]],
) -> tuple[CatalogDocumentView, ...]:
    """Read a bounded page while leaving connection ownership with the caller."""

    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with open_catalog(catalog_path, readonly=True) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")}
        predicates = catalog_query_predicates(
            columns,
            primary_kind=primary_kind,
            authority=authority,
            organization=organization,
            client=client,
            project=project,
            workstream=workstream,
        )
        if predicates is None:
            return ()
        clauses, parameters = predicates
        rows = catalog_document_rows(connection, columns, clauses, parameters, limit)
        return tuple(catalog_document_view(row) for row in rows)


__all__ = [
    "catalog_document_rows",
    "catalog_document_view",
    "catalog_projection_column",
    "catalog_query_predicates",
    "read_catalog_documents",
]
