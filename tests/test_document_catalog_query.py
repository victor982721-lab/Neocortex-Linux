"""Read-only query contracts for the published document catalog."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

import neocortex.documents.document_catalog as catalog_module
from neocortex.documents.document_catalog import (
    CatalogDocumentView,
    list_catalog_documents,
)


def _create_query_catalog(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                active INTEGER NOT NULL,
                source_kind TEXT NOT NULL,
                path TEXT NOT NULL,
                primary_kind TEXT NOT NULL,
                primary_subtype TEXT,
                primary_authority TEXT,
                primary_organization TEXT,
                primary_client TEXT,
                primary_project TEXT,
                primary_workstream TEXT,
                standard_references_json TEXT NOT NULL,
                clients_json TEXT NOT NULL,
                projects_json TEXT NOT NULL,
                workstreams_json TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                equipment_json TEXT NOT NULL,
                activities_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                uncertainty TEXT NOT NULL,
                catalog_status TEXT NOT NULL
            );
            """
        )
        rows = (
            (
                1,
                "pdf",
                "C:/z-standard.pdf",
                "normativa",
                "norma",
                "IEEE",
                "ANDRITZ",
                "Beta",
                "Project-2",
                "engineering",
                '[{"identifier":"IEEE C37"},{"identifier":2},{"other":"x"}]',
                '[{"label":"Beta"}]',
                '[{"label":"Project-2"}]',
                '[{"label":"engineering"}]',
                "not-json",
                '{}',
                '[{"label":"review"},null,{"label":3}]',
                0.91,
                "baja",
                "classified",
            ),
            (
                1,
                "docx",
                "C:/z-checklist.docx",
                "lista_verificacion",
                "formato",
                "IEC",
                "SERINTRA",
                "Zulu",
                "Project-1",
                "quality",
                "[]",
                '[{"label":"Zulu"}]',
                '[{"label":"Project-1"}]',
                '[{"label":"quality"}]',
                '[{"label":"inspection"}]',
                '[{"label":"transformer"}]',
                "[]",
                0.72,
                "media",
                "review",
            ),
            (
                1,
                "xlsx",
                "C:/a-checklist.xlsx",
                "lista_verificacion",
                "formato",
                "IEC",
                "SERINTRA",
                "Alpha",
                "Project-1",
                "quality",
                "[]",
                '[{"label":"Alpha"}]',
                '[{"label":"Project-1"}]',
                '[{"label":"quality"}]',
                "[]",
                "[]",
                "[]",
                0.81,
                "baja",
                "classified",
            ),
            (
                0,
                "pdf",
                "C:/inactive.pdf",
                "a_first",
                None,
                None,
                None,
                None,
                None,
                None,
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
                1.0,
                "baja",
                "classified",
            ),
        )
        connection.executemany(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def test_list_catalog_documents_signature_order_and_limit(tmp_path: Path) -> None:
    assert str(inspect.signature(list_catalog_documents)) == (
        "(catalog_path: 'Path', *, limit: 'int', "
        "primary_kind: 'str | None' = None, authority: 'str | None' = None, "
        "organization: 'str | None' = None, client: 'str | None' = None, "
        "project: 'str | None' = None, workstream: 'str | None' = None) "
        "-> 'tuple[CatalogDocumentView, ...]'"
    )
    catalog = tmp_path / "catalog.sqlite3"
    _create_query_catalog(catalog)

    documents = list_catalog_documents(catalog, limit=2)

    assert tuple(document.path for document in documents) == (
        "C:/a-checklist.xlsx",
        "C:/z-checklist.docx",
    )
    assert all(isinstance(document, CatalogDocumentView) for document in documents)


def test_list_catalog_documents_filters_case_insensitively_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _create_query_catalog(catalog)

    documents = list_catalog_documents(
        catalog,
        limit=10,
        primary_kind="NORMATIVA",
        authority="ieee",
        organization="andritz",
        client="beta",
        project="project-2",
        workstream="ENGINEERING",
    )

    assert documents == (
        CatalogDocumentView(
            source_kind="pdf",
            path="C:/z-standard.pdf",
            primary_kind="normativa",
            primary_subtype="norma",
            primary_authority="IEEE",
            primary_organization="ANDRITZ",
            primary_client="Beta",
            primary_project="Project-2",
            primary_workstream="engineering",
            standard_identifiers=("IEEE C37",),
            clients=("Beta",),
            projects=("Project-2",),
            workstreams=("engineering",),
            topics=(),
            equipment=(),
            activities=("review",),
            confidence=0.91,
            uncertainty="baja",
            catalog_status="classified",
        ),
    )


@pytest.mark.parametrize("limit", (0, -1, 10_001))
def test_list_catalog_documents_rejects_unbounded_limits(
    tmp_path: Path,
    limit: int,
) -> None:
    with pytest.raises(ValueError, match="limit must be between 1 and 10000"):
        list_catalog_documents(tmp_path / "absent.sqlite3", limit=limit)

    assert not (tmp_path / "absent.sqlite3").exists()


def test_list_catalog_documents_uses_readonly_connection_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _create_query_catalog(catalog)
    connection = sqlite3.connect(catalog)
    connection.row_factory = sqlite3.Row
    observed: list[tuple[Path, bool]] = []

    def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
        observed.append((path, readonly))
        return connection

    monkeypatch.setattr(catalog_module, "connect_document_catalog", connect)

    documents = list_catalog_documents(catalog, limit=1)

    assert len(documents) == 1
    assert observed == [(catalog, True)]
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_missing_legacy_filter_column_abstains_and_still_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(catalog) as writer:
        writer.executescript(
            """
            CREATE TABLE documents(
                active INTEGER NOT NULL,
                source_kind TEXT NOT NULL,
                path TEXT NOT NULL,
                primary_kind TEXT NOT NULL,
                primary_authority TEXT,
                primary_organization TEXT,
                standard_references_json TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                uncertainty TEXT NOT NULL,
                catalog_status TEXT NOT NULL
            );
            INSERT INTO documents VALUES(
                1,'pdf','C:/legacy.pdf','normativa','IEC',NULL,'[]','[]',
                0.8,'media','classified'
            );
            """
        )
    connection = sqlite3.connect(catalog)
    connection.row_factory = sqlite3.Row
    closed = False

    class ObservedConnection:
        def execute(self, *args, **kwargs):
            return connection.execute(*args, **kwargs)

        def close(self) -> None:
            nonlocal closed
            closed = True
            connection.close()

    monkeypatch.setattr(
        catalog_module,
        "connect_document_catalog",
        lambda _path, *, readonly=False: ObservedConnection(),
    )

    assert list_catalog_documents(catalog, limit=10, project="missing") == ()
    assert closed
