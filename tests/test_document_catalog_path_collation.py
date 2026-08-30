from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.platform_policy import sqlite_path_collation

from neocortex.documents import document_catalog as catalog_module
from neocortex.documents import document_catalog_schema as schema_module
from neocortex.documents import document_organization_application as application_module
from neocortex.documents import document_organization_planning as planning_module
from neocortex.documents.document_catalog import (
    CatalogBuild,
    SourceDocument,
    document_catalog_database,
    initialize_document_catalog,
)
from neocortex.sqlite_schema_contract import SQLiteSchemaContractError


_DOCUMENT_COLUMNS = (
    "source_kind",
    "file_key",
    "path",
    "volume_id",
    "file_id",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "source_status",
    "processing_signature",
    "text_fingerprint",
    "classifier_signature",
    "primary_kind",
    "confidence",
    "uncertainty",
    "standard_references_json",
    "organizations_json",
    "topics_json",
    "classification_json",
    "catalog_status",
    "active",
    "last_seen_catalog_run_id",
    "updated_ns",
)


def _create_schema(connection: sqlite3.Connection, path_collation: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in schema_module._document_catalog_schema_ddl(path_collation):
        connection.execute(statement)


def _insert_document(
    connection: sqlite3.Connection,
    *,
    table: str = "documents",
    generation_id: int | None = None,
    source_kind: str,
    file_key: str,
    path: str,
    classifier_signature: str = "classifier-v1",
) -> None:
    columns = _DOCUMENT_COLUMNS
    values: tuple[object, ...] = (
        source_kind,
        file_key,
        path,
        file_key.split(":", 1)[0],
        file_key.split(":", 1)[-1],
        100,
        200,
        -1,
        "done",
        "source-v1",
        "text-v1",
        classifier_signature,
        "otro",
        0.75,
        "media",
        "[]",
        "[]",
        "[]",
        "{}",
        "classified",
        1,
        1,
        300,
    )
    if table == "catalog_generation_documents":
        if generation_id is None:
            raise ValueError("generation_id is required for staged documents")
        columns = ("generation_id", *columns)
        values = (generation_id, *values)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        values,
    )


def _insert_plan(
    connection: sqlite3.Connection,
    *,
    plan_id: int,
    file_key: str,
    source_path: str,
    destination_path: str,
    organization_root: str = "/Organized",
) -> None:
    connection.execute(
        """INSERT INTO organization_plans(
        plan_id,catalog_run_id,source_kind,file_key,source_path,destination_path,
        organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
        classifier_signature,primary_kind,confidence,status,reason,evidence_json,
        planned_ns)
        VALUES(?,1,'pdf',?,?,?,?,'1',?,100,200,-1,'classifier-v1','otro',
        0.75,'planned','fixture','{}',300)""",
        (
            plan_id,
            file_key,
            source_path,
            destination_path,
            organization_root,
            file_key.split(":", 1)[-1],
        ),
    )


def _insert_generation(connection: sqlite3.Connection, generation_id: int = 1) -> None:
    connection.execute(
        """INSERT INTO catalog_runs(
        catalog_run_id,framework_run_id,source_kind,mode,status,started_ns,completed_ns)
        VALUES(1,7,'pdf','classify','completed',100,200)"""
    )
    connection.execute(
        """INSERT INTO catalog_generations(
        generation_id,catalog_run_id,source_kind,status,started_ns,completed_ns,
        published_ns)
        VALUES(?,1,'pdf','published',100,200,200)""",
        (generation_id,),
    )
    connection.execute(
        """INSERT INTO catalog_publications(source_kind,generation_id,published_ns)
        VALUES('pdf',?,200)""",
        (generation_id,),
    )


@pytest.mark.parametrize(
    "paths",
    (
        ("/Corpus/Case.pdf", "/Corpus/case.pdf"),
        ("/Corpus/case.pdf", "/Corpus/Case.pdf"),
    ),
    ids=("upper-first", "lower-first"),
)
@pytest.mark.parametrize(
    ("platform_name", "expected_count"),
    (("posix", 2), ("nt", 1)),
    ids=("posix-binary", "windows-nocase"),
)
def test_document_path_identity_follows_platform_in_both_orders(
    paths: tuple[str, str],
    platform_name: str,
    expected_count: int,
) -> None:
    with sqlite3.connect(":memory:") as connection:
        _create_schema(
            connection,
            sqlite_path_collation(platform_name=platform_name),
        )
        _insert_document(
            connection,
            source_kind="pdf",
            file_key="1:1",
            path=paths[0],
        )
        if expected_count == 1:
            with pytest.raises(sqlite3.IntegrityError, match=r"documents\.path"):
                _insert_document(
                    connection,
                    source_kind="docx",
                    file_key="1:2",
                    path=paths[1],
                )
        else:
            _insert_document(
                connection,
                source_kind="docx",
                file_key="1:2",
                path=paths[1],
            )
        assert int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]) == (
            expected_count
        )


@pytest.mark.parametrize(
    ("platform_name", "allows_case_distinct"),
    (("posix", True), ("nt", False)),
    ids=("posix-binary", "windows-nocase"),
)
def test_generation_and_plan_unique_paths_follow_platform(
    platform_name: str,
    allows_case_distinct: bool,
) -> None:
    with sqlite3.connect(":memory:") as connection:
        _create_schema(
            connection,
            sqlite_path_collation(platform_name=platform_name),
        )
        _insert_generation(connection)
        _insert_document(
            connection,
            table="catalog_generation_documents",
            generation_id=1,
            source_kind="pdf",
            file_key="1:1",
            path="/Corpus/Case.pdf",
        )
        _insert_plan(
            connection,
            plan_id=1,
            file_key="1:1",
            source_path="/Corpus/Case.pdf",
            destination_path="/Organized/Case.pdf",
        )
        if allows_case_distinct:
            _insert_document(
                connection,
                table="catalog_generation_documents",
                generation_id=1,
                source_kind="docx",
                file_key="1:2",
                path="/Corpus/case.pdf",
            )
            _insert_plan(
                connection,
                plan_id=2,
                file_key="1:2",
                source_path="/Corpus/case.pdf",
                destination_path="/Organized/case.pdf",
            )
        else:
            with pytest.raises(sqlite3.IntegrityError, match=r"generation_id.*path"):
                _insert_document(
                    connection,
                    table="catalog_generation_documents",
                    generation_id=1,
                    source_kind="docx",
                    file_key="1:2",
                    path="/Corpus/case.pdf",
                )
            with pytest.raises(sqlite3.IntegrityError, match="destination_path"):
                _insert_plan(
                    connection,
                    plan_id=2,
                    file_key="1:2",
                    source_path="/Corpus/case.pdf",
                    destination_path="/Organized/case.pdf",
                )


def _create_populated_v6_catalog(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        schema_module._create_v6_schema(connection)
        schema_module._set_schema_version(connection, 6)
        connection.execute("INSERT INTO metadata VALUES('sentinel','preserved')")
        _insert_generation(connection)
        _insert_document(
            connection,
            source_kind="pdf",
            file_key="1:1",
            path="/Corpus/Legacy.pdf",
        )
        _insert_document(
            connection,
            table="catalog_generation_documents",
            generation_id=1,
            source_kind="pdf",
            file_key="1:1",
            path="/Corpus/Legacy.pdf",
        )
        _insert_plan(
            connection,
            plan_id=9,
            file_key="1:1",
            source_path="/Corpus/Legacy.pdf",
            destination_path="/Organized/Legacy.pdf",
        )
        connection.execute(
            """INSERT INTO classification_history(
            source_kind,file_key,processing_signature,text_fingerprint,
            classifier_signature,path,classification_json,classified_ns)
            VALUES('pdf','1:1','source-v1','text-v1','classifier-v1',
            '/Corpus/Legacy.pdf','{}',300)"""
        )
        connection.commit()


def test_populated_v6_path_migration_preserves_all_catalog_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog-v6.sqlite3"
    _create_populated_v6_catalog(database)

    initialize_document_catalog(database)

    with document_catalog_database(database, readonly=True) as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "documents",
                "organization_plans",
                "catalog_generation_documents",
                "classification_history",
                "catalog_generations",
                "catalog_publications",
            )
        }
        paths = tuple(
            connection.execute(
                """SELECT
                (SELECT path FROM documents),
                (SELECT path FROM catalog_generation_documents),
                (SELECT source_path FROM organization_plans),
                (SELECT destination_path FROM organization_plans),
                (SELECT path FROM classification_history)"""
            ).fetchone()
        )
        plan_id = int(connection.execute("SELECT plan_id FROM organization_plans").fetchone()[0])
        version = str(
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
        )
        sentinel = str(
            connection.execute("SELECT value FROM metadata WHERE key='sentinel'").fetchone()[0]
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert counts == {
        "documents": 1,
        "organization_plans": 1,
        "catalog_generation_documents": 1,
        "classification_history": 1,
        "catalog_generations": 1,
        "catalog_publications": 1,
    }
    assert paths == (
        "/Corpus/Legacy.pdf",
        "/Corpus/Legacy.pdf",
        "/Corpus/Legacy.pdf",
        "/Organized/Legacy.pdf",
        "/Corpus/Legacy.pdf",
    )
    assert plan_id == 9
    assert version == str(schema_module.CATALOG_SCHEMA_VERSION)
    assert sentinel == "preserved"
    assert integrity == "ok"
    assert foreign_keys == []


@pytest.mark.parametrize(
    "injected",
    (RuntimeError("injected path migration failure"), KeyboardInterrupt("interrupted")),
    ids=("exception", "base-exception"),
)
def test_v6_path_migration_rolls_back_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected: BaseException,
) -> None:
    database = tmp_path / "catalog-v6-rollback.sqlite3"
    _create_populated_v6_catalog(database)
    # Exercise the rebuild branch deterministically on every CI platform.
    # Windows already uses the v6 NOCASE layout and therefore has no physical
    # path-table migration to interrupt.
    monkeypatch.setattr(schema_module, "_PATH_COLLATION", "BINARY")
    monkeypatch.setattr(
        schema_module,
        "_CURRENT_SCHEMA_DDL",
        schema_module._document_catalog_schema_ddl("BINARY"),
    )
    original = schema_module._copy_catalog_table_exact

    def fail_after_copy(
        connection: sqlite3.Connection,
        *,
        source: str,
        target: str,
    ) -> None:
        original(connection, source=source, target=target)
        if target == "organization_plans":
            raise injected

    monkeypatch.setattr(schema_module, "_copy_catalog_table_exact", fail_after_copy)

    with pytest.raises(type(injected), match=r"injected|interrupted"):
        initialize_document_catalog(database)

    with sqlite3.connect(database) as connection:
        version = str(
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
        )
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()[0]
        )
        counts = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "documents",
                "organization_plans",
                "catalog_generation_documents",
                "classification_history",
            )
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert version == "6"
    assert "path TEXT NOT NULL COLLATE NOCASE" in table_sql
    assert counts == (1, 1, 1, 1)
    assert integrity == "ok"
    assert foreign_keys == []


def test_unknown_v6_catalog_is_rejected_before_writable_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "catalog-v6-unknown.sqlite3"
    _create_populated_v6_catalog(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE INDEX unknown_catalog_index ON documents(updated_ns)")
        connection.commit()
    original_bytes = database.read_bytes()
    modes: list[bool] = []
    original_connect = catalog_module.connect_document_catalog

    def recording_connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
        modes.append(readonly)
        return original_connect(path, readonly=readonly)

    monkeypatch.setattr(catalog_module, "connect_document_catalog", recording_connect)

    with pytest.raises(SQLiteSchemaContractError, match=r"unexpected|incompatible"):
        initialize_document_catalog(database)

    assert modes == [True]
    assert database.read_bytes() == original_bytes


def test_current_catalog_reader_is_query_only_and_preserves_bytes(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_document_catalog(database)
    before = database.read_bytes()

    with document_catalog_database(database, readonly=True) as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TEMP TABLE forbidden(value INTEGER)")

    assert database.read_bytes() == before


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY",
    reason="POSIX path identity contract",
)
def test_case_distinct_path_is_not_a_cache_hit(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_document_catalog(database)
    taxonomy = catalog_module.load_taxonomy(None)
    signature = catalog_module.document_classifier_signature(taxonomy)
    with document_catalog_database(database) as connection:
        _insert_document(
            connection,
            source_kind="pdf",
            file_key="1:1",
            path="/Corpus/Case.pdf",
            classifier_signature=signature,
        )
        document = SourceDocument(
            source_kind="pdf",
            file_key="1:1",
            path="/Corpus/case.pdf",
            volume_id="1",
            file_id="1",
            size=100,
            mtime_ns=200,
            birthtime_ns=-1,
            source_status="done",
            processing_signature="source-v1",
            text_fingerprint="text-v1",
            title="",
            author="",
            metadata="",
        )
        assert not catalog_module._catalog_cache_hit(connection, document, taxonomy)


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY",
    reason="POSIX path identity contract",
)
def test_case_distinct_publication_does_not_cleanup_other_path(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_document_catalog(database)
    with document_catalog_database(database) as connection:
        _insert_document(
            connection,
            source_kind="docx",
            file_key="1:1",
            path="/Corpus/Case.pdf",
        )
        _insert_generation(connection)
        _insert_document(
            connection,
            table="catalog_generation_documents",
            generation_id=1,
            source_kind="pdf",
            file_key="1:2",
            path="/Corpus/case.pdf",
        )
        catalog_module._replace_catalog_projection(
            connection,
            CatalogBuild(1, 1, "pdf", None),
            now=400,
        )
        rows = connection.execute(
            "SELECT source_kind,path,active FROM documents ORDER BY source_kind"
        ).fetchall()
    assert tuple(map(tuple, rows)) == (
        ("docx", "/Corpus/Case.pdf", 1),
        ("pdf", "/Corpus/case.pdf", 1),
    )


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY",
    reason="POSIX path identity contract",
)
def test_case_distinct_catalog_and_plan_paths_are_available(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_document_catalog(database)
    with document_catalog_database(database) as connection:
        _insert_document(
            connection,
            source_kind="pdf",
            file_key="1:1",
            path=str(tmp_path / "Case.pdf"),
        )
        _insert_document(
            connection,
            source_kind="docx",
            file_key="1:2",
            path=str(tmp_path / "owner.docx"),
        )
        _insert_plan(
            connection,
            plan_id=1,
            file_key="1:1",
            source_path=str(tmp_path / "Case.pdf"),
            destination_path=str(tmp_path / "Organized" / "Case.pdf"),
            organization_root=str(tmp_path / "Organized"),
        )
        owner = connection.execute("SELECT * FROM documents WHERE source_kind='docx'").fetchone()
        assert planning_module._plan_destination_available(
            connection,
            owner,
            tmp_path / "case.pdf",
        )
        assert planning_module._plan_destination_available(
            connection,
            owner,
            tmp_path / "Organized" / "case.pdf",
        )
        _insert_plan(
            connection,
            plan_id=2,
            file_key="1:2",
            source_path=str(tmp_path / "owner.docx"),
            destination_path=str(tmp_path / "case.pdf"),
            organization_root=str(tmp_path / "Organized"),
        )
        apply_row = connection.execute(
            "SELECT * FROM organization_plans WHERE plan_id=2"
        ).fetchone()
        assert not application_module._catalog_destination_conflict(
            connection,
            apply_row,
        )
