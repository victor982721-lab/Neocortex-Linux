"""Adversarial contracts and atomic migrations for PDF and DOCX state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from _04_Nucleo_Operativo import docx_schema, docx_state, pdf_schema, pdf_state
from _04_Nucleo_Operativo.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


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


_ROUTES = (
    _RouteSpec(
        "PDF",
        pdf_state.SCHEMA_VERSION,
        pdf_state,
        "connect_pdf_state",
        pdf_state.initialize_pdf_state,
        pdf_schema,
        10,
        "_PDF_MIGRATIONS",
    ),
    _RouteSpec(
        "DOCX",
        docx_state.SCHEMA_VERSION,
        docx_state,
        "connect_docx_state",
        docx_state.initialize_docx_state,
        docx_schema,
        3,
        "_DOCX_MIGRATIONS",
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
            connection.executescript(
                """
                DROP INDEX docx_documents_path_idx;
                CREATE UNIQUE INDEX docx_documents_path_idx
                    ON documents(path COLLATE BINARY);
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
    before = database.read_bytes()

    with pytest.raises(SQLiteSchemaContractError, match="document_fts_data"):
        docx_state.initialize_docx_state(database)

    assert database.read_bytes() == before


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
    spec.initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
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


def test_pdf_legacy_migration_preserves_documents_pages_and_fts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    pdf_state.initialize_pdf_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,updated_ns
            ) VALUES('key','legacy.pdf',1,2,3,'sig','done',4)"""
        )
        connection.execute(
            """INSERT INTO pages(
            file_key,page_number,source,text_zlib,text_chars,profile_json)
            VALUES('key',0,'native',X'78',1,NULL)"""
        )
        connection.execute("INSERT INTO page_fts VALUES('key','legacy.pdf',0,'transformador')")
        connection.execute("INSERT INTO page_fts_state VALUES('key',0,'digest')")
        connection.execute("UPDATE metadata SET value='10' WHERE key='schema_version'")

    pdf_state.initialize_pdf_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM page_fts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM page_fts_state").fetchone() == (1,)
        assert connection.execute(
            "SELECT ocr_provenance_json FROM pages WHERE file_key='key'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(pdf_state.SCHEMA_VERSION),)


def test_docx_legacy_migration_preserves_parts_diagnostics_and_fts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "docx.sqlite3"
    docx_state.initialize_docx_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,last_seen_run_id,updated_ns
            ) VALUES('key','legacy.docx',1,2,3,'sig','complete',4,5)"""
        )
        connection.execute(
            "INSERT INTO document_parts VALUES('key','word/document.xml','body',0,X'78',1)"
        )
        connection.execute(
            """INSERT INTO document_diagnostics(
                file_key,ordinal,stage,code,message,required,retryable,disposition
            ) VALUES('key',0,'zip','warning','detail',0,0,'keep')"""
        )
        connection.execute(
            """INSERT INTO document_fts
            VALUES('key','legacy.docx','title','author','transformador')"""
        )
        connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

    docx_state.initialize_docx_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM document_parts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM document_diagnostics").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone() == (1,)


# endregion [04]
