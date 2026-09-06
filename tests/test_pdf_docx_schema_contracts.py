"""Adversarial contracts and atomic migrations for PDF and DOCX state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.capabilities.formats.docx import schema as docx_schema, state as docx_state
from neocortex.capabilities.formats.pdf import pdf_schema, pdf_state
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.capabilities.formats.docx.route import DocxRoute
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.capabilities.formats.pdf.pdf_route_cache import PdfRouteCacheMixin
from neocortex.capabilities.formats.pdf.pdf_route_storage import PdfRouteStorageMixin
from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable
from neocortex.platform.policy import sqlite_path_collation


# region [01] Route fixtures


@dataclass(frozen=True, slots=True)
class _RouteSpec:
    label: str
    version: int
    state_module: ModuleType
    connect_name: str
    initialize: Callable[[Path], None]
    schema_module: ModuleType
    migration_version: int
    migrations_name: str
    migration_builder: Callable[[sqlite3.Connection], None]


_ROUTES = (
    _RouteSpec(
        "PDF",
        pdf_state.SCHEMA_VERSION,
        pdf_state,
        "connect_pdf_state",
        pdf_state.initialize_pdf_state,
        pdf_schema,
        12,
        "_PDF_MIGRATIONS",
        pdf_schema._build_pdf_v12_canonical_schema,
    ),
    _RouteSpec(
        "DOCX",
        docx_state.SCHEMA_VERSION,
        docx_state,
        "connect_docx_state",
        docx_state.initialize_docx_state,
        docx_schema,
        5,
        "_DOCX_MIGRATIONS",
        docx_schema._build_docx_v5_canonical_schema,
    ),
)


def _route_id(spec: _RouteSpec) -> str:
    return spec.label.lower()


def _observe_connections(
    monkeypatch: pytest.MonkeyPatch,
    spec: _RouteSpec,
) -> list[bool]:
    calls: list[bool] = []
    original = getattr(spec.state_module, spec.connect_name)

    def observed(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
        calls.append(readonly)
        return original(path, readonly=readonly)

    monkeypatch.setattr(spec.state_module, spec.connect_name, observed)
    return calls


def _object_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def _key_collations(connection: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    quoted_name = '"' + index_name.replace('"', '""') + '"'
    return tuple(
        str(row[4]).upper()
        for row in connection.execute(f"PRAGMA index_xinfo({quoted_name})")
        if bool(row[5])
    )


class _PdfStorageProbe(PdfRouteStorageMixin, PdfRouteCacheMixin):
    pass


# endregion [01]


# region [02] Shared structural precision


def _build_fixture_contract(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE fixture(
            identity TEXT PRIMARY KEY,
            score INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID;
        CREATE INDEX fixture_score_idx
            ON fixture(score COLLATE NOCASE DESC) WHERE score > 0;
        """
    )


def test_table_column_order_is_not_part_of_the_contract() -> None:
    expected = schema_contract_from_builder(_build_fixture_contract)
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE fixture(
                score INTEGER NOT NULL DEFAULT 0,
                identity TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE INDEX fixture_score_idx
                ON fixture(score COLLATE NOCASE DESC) WHERE score > 0;
            """
        )
        validate_sqlite_schema_contract(connection, expected, label="fixture")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "actual_ddl",
    (
        "CREATE TABLE fixture(identity TEXT PRIMARY KEY) WITHOUT ROWID",
        """CREATE TABLE fixture(
            identity TEXT PRIMARY KEY,score INTEGER NOT NULL DEFAULT 0,
            extra TEXT) WITHOUT ROWID""",
        """CREATE TABLE fixture(
            identity TEXT PRIMARY KEY,score TEXT NOT NULL DEFAULT 0
        ) WITHOUT ROWID""",
        """CREATE TABLE fixture(
            identity TEXT PRIMARY KEY,score INTEGER NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE fixture(
            identity TEXT NOT NULL,score INTEGER PRIMARY KEY DEFAULT 0
        ) WITHOUT ROWID""",
    ),
)
def test_column_names_and_attributes_remain_exact(actual_ddl: str) -> None:
    expected = schema_contract_from_builder(_build_fixture_contract)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(actual_ddl)
        with pytest.raises(SQLiteSchemaContractError, match="incompatible columns"):
            validate_sqlite_schema_contract(connection, expected, label="fixture")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "index_ddl",
    (
        """CREATE INDEX fixture_score_idx
            ON fixture(score COLLATE NOCASE ASC) WHERE score > 0""",
        """CREATE INDEX fixture_score_idx
            ON fixture(score COLLATE BINARY DESC) WHERE score > 0""",
        """CREATE INDEX fixture_score_idx
            ON fixture(score COLLATE NOCASE DESC) WHERE score >= 0""",
    ),
)
def test_index_direction_collation_and_predicate_are_contractual(
    index_ddl: str,
) -> None:
    expected = schema_contract_from_builder(_build_fixture_contract)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            """CREATE TABLE fixture(
                identity TEXT PRIMARY KEY,
                score INTEGER NOT NULL DEFAULT 0
            ) WITHOUT ROWID"""
        )
        connection.execute(index_ddl)
        with pytest.raises(SQLiteSchemaContractError, match="incompatible indexes"):
            validate_sqlite_schema_contract(connection, expected, label="fixture")
    finally:
        connection.close()


def test_windows_pdf_and_docx_path_schema_contract_is_nocase() -> None:
    collation = sqlite_path_collation(platform_name="nt")
    assert collation == "NOCASE"
    connection = sqlite3.connect(":memory:")
    try:
        for statement in pdf_schema._pdf_table_ddl(collation):
            connection.execute(statement)
        for statement in pdf_schema._pdf_index_ddl(collation):
            connection.execute(statement)
        table_sql = {
            str(row[0]): str(row[1]).upper()
            for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
        }
        assert "PATH TEXT NOT NULL COLLATE NOCASE" in table_sql["documents"]
        assert "PATH TEXT NOT NULL COLLATE NOCASE" in table_sql["pdf_inventory"]
        assert _key_collations(connection, "documents_path_idx") == ("NOCASE",)
    finally:
        connection.close()

    connection = sqlite3.connect(":memory:")
    try:
        for statement in docx_schema._docx_table_ddl(collation):
            connection.execute(statement)
        for statement in docx_schema._docx_path_index_ddl(collation):
            connection.execute(statement)
        table_sql = {
            str(row[0]): str(row[1]).upper()
            for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
        }
        assert "PATH TEXT NOT NULL COLLATE NOCASE" in table_sql["documents"]
        assert "PATH TEXT NOT NULL COLLATE NOCASE" in table_sql["docx_inventory"]
        assert "PDF_PATH TEXT COLLATE NOCASE" in table_sql["pdf_counterparts"]
        assert _key_collations(connection, "docx_documents_path_idx") == ("NOCASE",)
        assert _key_collations(connection, "docx_documents_review_idx") == (
            "BINARY",
            "BINARY",
            "NOCASE",
        )
        counterpart_pk = next(
            str(row[1])
            for row in connection.execute("PRAGMA index_list(pdf_counterparts)")
            if str(row[3]) == "pk"
        )
        assert _key_collations(connection, counterpart_pk) == ("BINARY", "NOCASE")
    finally:
        connection.close()


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY",
    reason="case-distinct path identity is the POSIX contract",
)
@pytest.mark.parametrize("upper_first", (True, False), ids=("upper-first", "lower-first"))
@pytest.mark.parametrize("route_label", ("pdf", "docx"))
def test_linux_routes_preserve_case_distinct_paths_in_both_orders(
    tmp_path: Path,
    route_label: str,
    upper_first: bool,
) -> None:
    suffix = route_label
    upper_path = f"/fixture/Case.{suffix}"
    lower_path = f"/fixture/case.{suffix}"
    paths = (upper_path, lower_path) if upper_first else (lower_path, upper_path)
    database = tmp_path / f"{route_label}.sqlite3"

    if route_label == "pdf":
        pdf_state.initialize_pdf_state(database)
        route = _PdfStorageProbe()
        route.config = SimpleNamespace(processing_signature="test-signature")
        route.run_id = 1
        route.cancellation = CancellationToken()
        with pdf_state.pdf_database(database) as connection:
            snapshots: list[FileSnapshot] = []
            for ordinal, path in enumerate(paths, 1):
                snapshot = FileSnapshot(path, 1, ordinal, 10, 20, 30)
                assert route._stale_path_owner_keys(connection, [snapshot]) == ()
                route._prepare_document(
                    connection,
                    snapshot,
                    1,
                    {},
                )
                snapshots.append(snapshot)
                connection.execute(
                    "INSERT INTO pdf_inventory VALUES(?,?,?,?,?,?)",
                    (
                        file_key_from_snapshot(snapshot),
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        route.run_id,
                    ),
                )
            assert route._stale_path_owner_keys(connection, snapshots) == ()
            stored_paths = {str(row[0]) for row in connection.execute("SELECT path FROM documents")}
            assert _key_collations(connection, "documents_path_idx") == ("BINARY",)
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    else:
        docx_state.initialize_docx_state(database)
        route = object.__new__(DocxRoute)
        route.config = SimpleNamespace(processing_signature="test-signature")
        route.run_id = 1
        with docx_state.docx_database(database) as connection:
            snapshots = []
            for ordinal, path in enumerate(paths, 1):
                snapshot = FileSnapshot(path, 1, ordinal, 10, 20, 30)
                route._store_error(
                    connection,
                    snapshot,
                    ValueError("synthetic DOCX failure"),
                )
                snapshots.append(snapshot)
            route._write_inventory_batch(
                connection,
                [
                    (
                        file_key_from_snapshot(snapshot),
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        route.run_id,
                    )
                    for snapshot in snapshots
                ],
            )
            for snapshot in snapshots:
                route._touch_cache_hit(connection, snapshot, "cached_error")
            stored_paths = {str(row[0]) for row in connection.execute("SELECT path FROM documents")}
            assert _key_collations(connection, "docx_documents_path_idx") == ("BINARY",)
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert stored_paths == {upper_path, lower_path}


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY",
    reason="case-only path reconciliation is the POSIX contract",
)
@pytest.mark.parametrize(
    ("cached_path", "inventory_path"),
    (("/fixture/Case.pdf", "/fixture/case.pdf"), ("/fixture/case.pdf", "/fixture/Case.pdf")),
    ids=("upper-to-lower", "lower-to-upper"),
)
def test_linux_pdf_cache_reconciles_case_only_path_changes(
    tmp_path: Path,
    cached_path: str,
    inventory_path: str,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    pdf_state.initialize_pdf_state(database)
    with pdf_state.pdf_database(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
            status,last_seen_run_id,updated_ns)
            VALUES('key',?,10,20,30,'sig','done',1,1)""",
            (cached_path,),
        )
        connection.execute(
            "INSERT INTO pdf_inventory VALUES('key',?,10,20,30,1)",
            (inventory_path,),
        )
        connection.execute(
            "INSERT INTO page_fts VALUES('key',?,0,'fixture')",
            (cached_path,),
        )

    route = _PdfStorageProbe()
    route.config = SimpleNamespace(state_path=database)
    route.run_id = 1
    route.cancellation = CancellationToken()
    route._prune_pdf_cache()

    with pdf_state.pdf_database(database, readonly=True) as connection:
        assert (
            connection.execute("SELECT path FROM documents WHERE file_key='key'").fetchone()[0]
            == inventory_path
        )
        assert (
            connection.execute("SELECT path FROM page_fts WHERE file_key='key'").fetchone()[0]
            == inventory_path
        )


# endregion [02]


# region [03] Read-only rejection and byte preservation


@pytest.mark.parametrize("spec", _ROUTES, ids=_route_id)
def test_current_valid_schema_is_only_opened_readonly_and_is_byte_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: _RouteSpec,
) -> None:
    database = tmp_path / f"{spec.label.lower()}.sqlite3"
    spec.initialize(database)
    before = database.read_bytes()
    calls = _observe_connections(monkeypatch, spec)

    spec.initialize(database)

    assert calls == [True]
    assert database.read_bytes() == before


@pytest.mark.parametrize("spec", _ROUTES, ids=_route_id)
def test_current_malformed_schema_is_rejected_readonly_without_byte_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: _RouteSpec,
) -> None:
    database = tmp_path / f"{spec.label.lower()}.sqlite3"
    spec.initialize(database)
    with sqlite3.connect(database) as connection:
        if spec.label == "PDF":
            connection.executescript(
                """
                DROP INDEX similarity_relations_score_idx;
                CREATE INDEX similarity_relations_score_idx
                    ON similarity_relations(run_id,kind,score ASC);
                """
            )
        else:
            incompatible_collation = "NOCASE" if sqlite_path_collation() == "BINARY" else "BINARY"
            connection.executescript(
                f"""
                DROP INDEX docx_documents_path_idx;
                CREATE UNIQUE INDEX docx_documents_path_idx
                    ON documents(path COLLATE {incompatible_collation});
                """
            )
    before = database.read_bytes()
    calls = _observe_connections(monkeypatch, spec)

    with pytest.raises(SQLiteSchemaContractError, match="schema contract"):
        spec.initialize(database)

    assert calls == [True]
    assert database.read_bytes() == before


def test_pdf_current_contract_requires_derived_layout_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    pdf_state.initialize_pdf_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE page_layouts")
    before = database.read_bytes()

    with pytest.raises(SQLiteSchemaContractError, match="page_layouts"):
        pdf_state.initialize_pdf_state(database)

    assert database.read_bytes() == before


def test_docx_current_contract_requires_fts_shadow_tables(tmp_path: Path) -> None:
    database = tmp_path / "docx.sqlite3"
    docx_state.initialize_docx_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE document_fts_data")
    # sqlite3.Connection.__exit__ commits but does not close: make this the
    # cold-owner schema-contract test, not the live-WAL materialization case.
    connection.close()
    before = database.read_bytes()

    with pytest.raises(SQLiteSchemaContractError, match="document_fts_data"):
        docx_state.initialize_docx_state(database)

    assert database.read_bytes() == before


def test_docx_shadow_table_corruption_in_live_wal_rejects_snapshot_without_source_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "docx.sqlite3"
    docx_state.initialize_docx_state(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE document_fts_data")
        connection.commit()
        wal = Path(f"{database}-wal")
        assert wal.is_file(), "the fixture must exercise uncheckpointed WAL state"
        source_files = (database, wal, Path(f"{database}-shm"))
        before = {path: path.read_bytes() if path.exists() else None for path in source_files}

        # The immutable kernel now verifies a temporary WAL materialization
        # before the DOCX schema reader. Require the actual FTS corruption as
        # the cause, not merely a generic refusal or a changed exception class.
        with pytest.raises(ImmutableSQLiteUnavailable, match="temporary SQLite snapshot") as rejected:
            docx_state.initialize_docx_state(database)

        assert isinstance(rejected.value.__cause__, sqlite3.DatabaseError)
        assert "vtable constructor failed: document_fts" in str(rejected.value.__cause__)
        after = {path: path.read_bytes() if path.exists() else None for path in source_files}
        assert after == before
    finally:
        connection.close()


@pytest.mark.parametrize("spec", _ROUTES, ids=_route_id)
def test_future_schema_is_rejected_readonly_without_byte_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: _RouteSpec,
) -> None:
    database = tmp_path / f"future-{spec.label.lower()}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE metadata(
                key TEXT PRIMARY KEY,value TEXT NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO metadata VALUES(
                'schema_version','{spec.version + 1}'
            );
            CREATE TABLE sentinel(value TEXT);
            INSERT INTO sentinel VALUES('preserve');
            """
        )
    before = database.read_bytes()
    calls = _observe_connections(monkeypatch, spec)

    with pytest.raises(RuntimeError, match="unsupported"):
        spec.initialize(database)

    assert calls == [True]
    assert database.read_bytes() == before


@pytest.mark.parametrize("spec", _ROUTES, ids=_route_id)
@pytest.mark.parametrize(
    "metadata_ddl",
    (
        """CREATE TABLE metadata(
            key TEXT PRIMARY KEY,value TEXT NOT NULL
        ) WITHOUT ROWID;
        INSERT INTO metadata VALUES('schema_version','04')""",
        """CREATE TABLE metadata(key TEXT,value TEXT NOT NULL);
        INSERT INTO metadata VALUES('schema_version','1');
        INSERT INTO metadata VALUES('schema_version','1')""",
        """CREATE TABLE metadata(key TEXT PRIMARY KEY) WITHOUT ROWID;
        INSERT INTO metadata VALUES('schema_version')""",
    ),
    ids=("noncanonical", "duplicate", "missing-value"),
)
def test_malformed_metadata_is_rejected_readonly_without_byte_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: _RouteSpec,
    metadata_ddl: str,
) -> None:
    database = tmp_path / f"malformed-{spec.label.lower()}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(metadata_ddl)
    before = database.read_bytes()
    calls = _observe_connections(monkeypatch, spec)

    with pytest.raises(SQLiteSchemaContractError):
        spec.initialize(database)

    assert calls == [True]
    assert database.read_bytes() == before


# endregion [03]


# region [04] Atomic rollback and cache preservation


@pytest.mark.parametrize("spec", _ROUTES, ids=_route_id)
def test_failed_legacy_migration_rolls_back_ddl_version_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: _RouteSpec,
) -> None:
    database = tmp_path / f"rollback-{spec.label.lower()}.sqlite3"
    with sqlite3.connect(database) as connection:
        spec.migration_builder(connection)
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
            (str(spec.migration_version),),
        )
        connection.execute("INSERT INTO metadata VALUES('preserved','yes')")
    with sqlite3.connect(database) as connection:
        before_objects = _object_names(connection)

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE rollback_probe(value TEXT)")
        raise sqlite3.OperationalError("injected migration failure")

    migrations = getattr(spec.schema_module, spec.migrations_name)
    monkeypatch.setitem(migrations, spec.migration_version, fail_after_ddl)

    with pytest.raises(RuntimeError, match="initialization from version"):
        spec.initialize(database)

    with sqlite3.connect(database) as connection:
        assert _object_names(connection) == before_objects
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(spec.migration_version),)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='preserved'"
        ).fetchone() == ("yes",)


def test_pdf_v12_migration_preserves_documents_children_and_fts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        pdf_schema._build_pdf_v12_canonical_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','12')")
        connection.execute(
            """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,updated_ns
            ) VALUES('key','legacy.pdf',1,2,3,'sig','done',4)"""
        )
        connection.execute("INSERT INTO pdf_inventory VALUES('key','legacy.pdf',1,2,3,4)")
        connection.execute(
            """INSERT INTO pages(
            file_key,page_number,source,text_zlib,text_chars,
            ocr_provenance_json,profile_json)
            VALUES('key',0,'native',X'78',1,'{}','{}')"""
        )
        connection.execute(
            """INSERT INTO page_staging(
            file_key,processing_signature,page_number,source,text_zlib,
            text_chars,ocr_provenance_json)
            VALUES('key','sig',0,'native',X'78',1,'{}')"""
        )
        connection.execute(
            """INSERT INTO page_errors VALUES(
            'key','sig',1,'FixtureError','detail',5)"""
        )
        connection.execute(
            """INSERT INTO document_warnings VALUES(
            'key','sig','parser',1,'[]',6)"""
        )
        connection.execute(
            """INSERT INTO page_layouts VALUES(
            'key',0,1,'native','geometry','visual','header','footer',
            'layout',X'78',7)"""
        )
        connection.execute(
            """INSERT INTO document_layouts VALUES(
            'key',1,1,'layout','geometry','visual','header','footer',
            'sequence','{}',8)"""
        )
        connection.execute(
            """INSERT INTO page_fts(rowid,file_key,path,page_number,text)
            VALUES(41,'key','legacy.pdf',0,'transformador')"""
        )
        connection.execute("INSERT INTO page_fts_state VALUES('key',0,'digest')")

    pdf_state.initialize_pdf_state(database)

    with sqlite3.connect(database) as connection:
        expected_counts = {
            "documents": 1,
            "pdf_inventory": 1,
            "pages": 1,
            "page_staging": 1,
            "page_errors": 1,
            "document_warnings": 1,
            "page_layouts": 1,
            "document_layouts": 1,
            "page_fts": 1,
            "page_fts_state": 1,
        }
        for table, expected in expected_counts.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (expected,)
        assert connection.execute(
            "SELECT ocr_provenance_json FROM pages WHERE file_key='key'"
        ).fetchone() == ("{}",)
        assert connection.execute(
            "SELECT layout_simhash64 FROM document_layouts WHERE file_key='key'"
        ).fetchone() == ("layout",)
        assert connection.execute(
            "SELECT rowid,text FROM page_fts WHERE file_key='key'"
        ).fetchone() == (41, "transformador")
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(pdf_state.SCHEMA_VERSION),)
        assert _key_collations(connection, "documents_path_idx") == (sqlite_path_collation(),)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_docx_v5_migration_preserves_documents_children_and_fts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "docx.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        docx_schema._build_docx_v5_canonical_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','5')")
        connection.execute(
            """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,last_seen_run_id,updated_ns
            ) VALUES('key','legacy.docx',1,2,3,'sig','complete',4,5)"""
        )
        connection.execute("INSERT INTO docx_inventory VALUES('key','legacy.docx',1,2,3,4)")
        connection.execute(
            "INSERT INTO document_parts VALUES('key','word/document.xml','body',0,X'78',1)"
        )
        connection.execute(
            """INSERT INTO document_diagnostics(
                file_key,ordinal,stage,code,message,required,retryable,disposition
            ) VALUES('key',0,'zip','warning','detail',0,0,'keep')"""
        )
        connection.execute(
            """INSERT INTO document_fts(rowid,file_key,path,title,author,body)
            VALUES(43,'key','legacy.docx','title','author','transformador')"""
        )
        connection.execute(
            """INSERT INTO pdf_counterparts VALUES(
            'key','legacy.pdf','matched','same_stem',1,4,5)"""
        )

    docx_state.initialize_docx_state(database)

    with sqlite3.connect(database) as connection:
        expected_counts = {
            "documents": 1,
            "docx_inventory": 1,
            "document_parts": 1,
            "document_diagnostics": 1,
            "document_fts": 1,
            "pdf_counterparts": 1,
        }
        for table, expected in expected_counts.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (expected,)
        assert connection.execute(
            "SELECT rowid,body FROM document_fts WHERE file_key='key'"
        ).fetchone() == (43, "transformador")
        assert connection.execute(
            "SELECT pdf_path FROM pdf_counterparts WHERE docx_file_key='key'"
        ).fetchone() == ("legacy.pdf",)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(docx_state.SCHEMA_VERSION),)
        assert _key_collations(connection, "docx_documents_path_idx") == (sqlite_path_collation(),)
        counterpart_pk = next(
            str(row[1])
            for row in connection.execute("PRAGMA index_list(pdf_counterparts)")
            if str(row[3]) == "pk"
        )
        assert _key_collations(connection, counterpart_pk) == (
            "BINARY",
            sqlite_path_collation(),
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# endregion [04]
