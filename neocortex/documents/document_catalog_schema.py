"""Versioned SQLite schema and migrations for the document catalog."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import sqlite3
import time
from collections.abc import Callable
from functools import lru_cache

from neocortex.platform_policy import sqlite_path_collation

from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    capture_sqlite_schema_contract,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


# region [01] Canonical schema


CATALOG_SCHEMA_VERSION = 7
_PATH_COLLATION = sqlite_path_collation()


_V5_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS catalog_runs(
        catalog_run_id INTEGER PRIMARY KEY,
        framework_run_id INTEGER,
        source_kind TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS documents(
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        source_status TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        text_fingerprint TEXT,
        classifier_signature TEXT NOT NULL,
        primary_kind TEXT NOT NULL,
        primary_subtype TEXT,
        primary_authority TEXT,
        primary_organization TEXT,
        primary_client TEXT,
        primary_project TEXT,
        primary_workstream TEXT,
        confidence REAL NOT NULL,
        uncertainty TEXT NOT NULL,
        standard_references_json TEXT NOT NULL,
        organizations_json TEXT NOT NULL,
        clients_json TEXT NOT NULL DEFAULT '[]',
        projects_json TEXT NOT NULL DEFAULT '[]',
        workstreams_json TEXT NOT NULL DEFAULT '[]',
        topics_json TEXT NOT NULL,
        equipment_json TEXT NOT NULL DEFAULT '[]',
        activities_json TEXT NOT NULL DEFAULT '[]',
        classification_json TEXT NOT NULL,
        catalog_status TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        last_seen_catalog_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(source_kind,file_key)
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS catalog_documents_path_idx
        ON documents(path) WHERE active=1""",
    """CREATE INDEX IF NOT EXISTS catalog_documents_kind_idx
        ON documents(active,primary_kind,primary_subtype,primary_authority,
        primary_organization,primary_client,primary_project,path)""",
    """CREATE INDEX IF NOT EXISTS catalog_documents_review_idx
        ON documents(active,catalog_status,confidence,path)""",
    """CREATE TABLE IF NOT EXISTS classification_history(
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        text_fingerprint TEXT NOT NULL,
        classifier_signature TEXT NOT NULL,
        path TEXT NOT NULL,
        classification_json TEXT NOT NULL,
        classified_ns INTEGER NOT NULL,
        PRIMARY KEY(
            source_kind,file_key,processing_signature,
            text_fingerprint,classifier_signature,path
        )
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS organization_plans(
        plan_id INTEGER PRIMARY KEY,
        catalog_run_id INTEGER,
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        source_path TEXT NOT NULL COLLATE NOCASE,
        destination_path TEXT COLLATE NOCASE,
        organization_root TEXT NOT NULL COLLATE NOCASE,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        classifier_signature TEXT NOT NULL,
        primary_kind TEXT NOT NULL,
        confidence REAL NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        planned_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        detail TEXT,
        move_completed_ns INTEGER,
        cache_sync_status TEXT NOT NULL DEFAULT 'pending',
        cache_sync_json TEXT,
        cache_sync_error TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS organization_plans_status_idx
        ON organization_plans(organization_root,status,plan_id)""",
    """CREATE INDEX IF NOT EXISTS organization_plans_source_idx
        ON organization_plans(source_kind,file_key,plan_id)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS organization_plans_destination_idx
        ON organization_plans(destination_path)
        WHERE status IN ('planned','applying','moved_cache_pending')
        AND destination_path IS NOT NULL""",
)

_GENERATION_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS catalog_generations(
        generation_id INTEGER PRIMARY KEY,
        catalog_run_id INTEGER UNIQUE,
        source_kind TEXT NOT NULL,
        base_generation_id INTEGER,
        status TEXT NOT NULL CHECK(status IN (
            'building','published','failed','cancelled','superseded','abandoned'
        )),
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        published_ns INTEGER,
        error_type TEXT,
        error_message TEXT,
        FOREIGN KEY(catalog_run_id) REFERENCES catalog_runs(catalog_run_id),
        FOREIGN KEY(base_generation_id)
            REFERENCES catalog_generations(generation_id)
    )""",
    """CREATE INDEX IF NOT EXISTS catalog_generations_status_idx
        ON catalog_generations(source_kind,status,generation_id)""",
    """CREATE TABLE IF NOT EXISTS catalog_publications(
        source_kind TEXT PRIMARY KEY,
        generation_id INTEGER NOT NULL UNIQUE,
        published_ns INTEGER NOT NULL,
        FOREIGN KEY(generation_id) REFERENCES catalog_generations(generation_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS catalog_generation_documents(
        generation_id INTEGER NOT NULL,
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        source_status TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        text_fingerprint TEXT,
        classifier_signature TEXT NOT NULL,
        primary_kind TEXT NOT NULL,
        primary_subtype TEXT,
        primary_authority TEXT,
        primary_organization TEXT,
        primary_client TEXT,
        primary_project TEXT,
        primary_workstream TEXT,
        confidence REAL NOT NULL,
        uncertainty TEXT NOT NULL,
        standard_references_json TEXT NOT NULL,
        organizations_json TEXT NOT NULL,
        clients_json TEXT NOT NULL DEFAULT '[]',
        projects_json TEXT NOT NULL DEFAULT '[]',
        workstreams_json TEXT NOT NULL DEFAULT '[]',
        topics_json TEXT NOT NULL,
        equipment_json TEXT NOT NULL DEFAULT '[]',
        activities_json TEXT NOT NULL DEFAULT '[]',
        classification_json TEXT NOT NULL,
        catalog_status TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        last_seen_catalog_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(generation_id,source_kind,file_key),
        FOREIGN KEY(generation_id)
            REFERENCES catalog_generations(generation_id)
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS catalog_generation_documents_path_idx
        ON catalog_generation_documents(generation_id,path) WHERE active=1""",
    """CREATE INDEX IF NOT EXISTS catalog_generation_documents_kind_idx
        ON catalog_generation_documents(
            generation_id,active,primary_kind,primary_client,primary_project,path
        )""",
)

_CURRENT_ORGANIZATION_DESTINATION_INDEX_DDL = """
    CREATE UNIQUE INDEX IF NOT EXISTS organization_plans_destination_idx
    ON organization_plans(destination_path)
    WHERE status IN (
        'planned','applying','moved_cache_pending','recovery_required'
    ) AND destination_path IS NOT NULL
"""

# The historical v5 contract must remain byte-for-byte structural evidence for
# migration validation. Its final statement is replaced only in the v6 schema.
_V6_SCHEMA_DDL = (
    *_V5_SCHEMA_DDL[:-1],
    _CURRENT_ORGANIZATION_DESTINATION_INDEX_DDL,
    *_GENERATION_SCHEMA_DDL,
)


def _document_catalog_schema_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported document catalog path collation: {path_collation}")
    # classification_history.path remains an exact archival observation.  Only
    # current filesystem identities, planning keys and publication members use
    # the platform path-equivalence policy.
    return tuple(
        statement.replace(
            "path TEXT NOT NULL COLLATE NOCASE",
            f"path TEXT NOT NULL COLLATE {path_collation}",
        )
        .replace(
            "source_path TEXT NOT NULL COLLATE NOCASE",
            f"source_path TEXT NOT NULL COLLATE {path_collation}",
        )
        .replace(
            "destination_path TEXT COLLATE NOCASE",
            f"destination_path TEXT COLLATE {path_collation}",
        )
        .replace(
            "organization_root TEXT NOT NULL COLLATE NOCASE",
            f"organization_root TEXT NOT NULL COLLATE {path_collation}",
        )
        for statement in _V6_SCHEMA_DDL
    )


_CURRENT_SCHEMA_DDL = _document_catalog_schema_ddl(_PATH_COLLATION)


def create_document_catalog_schema(connection: sqlite3.Connection) -> None:
    """Create every object required by the current catalog contract."""

    for statement in _CURRENT_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def document_catalog_schema_contract() -> SQLiteSchemaContract:
    """Return the immutable structural contract for schema v7."""

    return schema_contract_from_builder(create_document_catalog_schema)


# endregion [01]


# region [02] Historical schema steps


_V1_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS catalog_runs(
        catalog_run_id INTEGER PRIMARY KEY,
        framework_run_id INTEGER,
        source_kind TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS documents(
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        source_status TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        text_fingerprint TEXT,
        classifier_signature TEXT NOT NULL,
        primary_kind TEXT NOT NULL,
        primary_authority TEXT,
        primary_organization TEXT,
        confidence REAL NOT NULL,
        uncertainty TEXT NOT NULL,
        standard_references_json TEXT NOT NULL,
        organizations_json TEXT NOT NULL,
        topics_json TEXT NOT NULL,
        classification_json TEXT NOT NULL,
        catalog_status TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        last_seen_catalog_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(source_kind,file_key)
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS catalog_documents_path_idx
        ON documents(path) WHERE active=1""",
    """CREATE INDEX IF NOT EXISTS catalog_documents_kind_idx
        ON documents(active,primary_kind,primary_authority,
        primary_organization,path)""",
    """CREATE INDEX IF NOT EXISTS catalog_documents_review_idx
        ON documents(active,catalog_status,confidence,path)""",
    """CREATE TABLE IF NOT EXISTS classification_history(
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        text_fingerprint TEXT NOT NULL,
        classifier_signature TEXT NOT NULL,
        path TEXT NOT NULL,
        classification_json TEXT NOT NULL,
        classified_ns INTEGER NOT NULL,
        PRIMARY KEY(
            source_kind,file_key,processing_signature,
            text_fingerprint,classifier_signature
        )
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS organization_plans(
        plan_id INTEGER PRIMARY KEY,
        catalog_run_id INTEGER,
        source_kind TEXT NOT NULL,
        file_key TEXT NOT NULL,
        source_path TEXT NOT NULL COLLATE NOCASE,
        destination_path TEXT COLLATE NOCASE,
        organization_root TEXT NOT NULL COLLATE NOCASE,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        classifier_signature TEXT NOT NULL,
        primary_kind TEXT NOT NULL,
        confidence REAL NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        planned_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        detail TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS organization_plans_status_idx
        ON organization_plans(organization_root,status,plan_id)""",
    """CREATE INDEX IF NOT EXISTS organization_plans_source_idx
        ON organization_plans(source_kind,file_key,plan_id)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS organization_plans_destination_idx
        ON organization_plans(destination_path)
        WHERE status IN ('planned','applying')
        AND destination_path IS NOT NULL""",
)


def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    for statement in _V1_SCHEMA_DDL:
        connection.execute(statement)


def _history_primary_key(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute("PRAGMA table_info(classification_history)")
    return tuple(
        str(row[1])
        for row in sorted(rows, key=lambda row: int(row[5]) if int(row[5]) else 99)
        if int(row[5])
    )


def _create_v1_history_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE classification_history(
            source_kind TEXT NOT NULL,
            file_key TEXT NOT NULL,
            processing_signature TEXT NOT NULL,
            text_fingerprint TEXT NOT NULL,
            classifier_signature TEXT NOT NULL,
            path TEXT NOT NULL,
            classification_json TEXT NOT NULL,
            classified_ns INTEGER NOT NULL,
            PRIMARY KEY(
                source_kind,file_key,processing_signature,
                text_fingerprint,classifier_signature
            )
        ) WITHOUT ROWID"""
    )


@lru_cache(maxsize=1)
def _v1_history_table_contract() -> object:
    return schema_contract_from_builder(_create_v1_history_table).tables[0]


def _validate_v1_history_source(connection: sqlite3.Connection) -> None:
    actual = next(
        (
            table
            for table in capture_sqlite_schema_contract(connection).tables
            if table.name == "classification_history"
        ),
        None,
    )
    if actual != _v1_history_table_contract():
        raise SQLiteSchemaContractError(
            "document catalog v1 classification_history has unknown structure"
        )
    trigger = connection.execute(
        """SELECT name FROM sqlite_master WHERE type='trigger'
        AND tbl_name='classification_history' LIMIT 1"""
    ).fetchone()
    if trigger is not None:
        raise SQLiteSchemaContractError(
            "document catalog v1 classification_history has an unknown trigger"
        )


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    reserved = connection.execute(
        """SELECT type FROM sqlite_master
        WHERE name='classification_history_v2' LIMIT 1"""
    ).fetchone()
    if reserved is not None:
        raise SQLiteSchemaContractError(
            "document catalog migration reserved object 'classification_history_v2' already exists"
        )
    if _history_primary_key(connection)[-1:] == ("path",):
        return
    _validate_v1_history_source(connection)
    source_count = int(
        connection.execute("SELECT COUNT(*) FROM classification_history").fetchone()[0]
    )
    connection.execute(
        """CREATE TABLE classification_history_v2(
            source_kind TEXT NOT NULL,
            file_key TEXT NOT NULL,
            processing_signature TEXT NOT NULL,
            text_fingerprint TEXT NOT NULL,
            classifier_signature TEXT NOT NULL,
            path TEXT NOT NULL,
            classification_json TEXT NOT NULL,
            classified_ns INTEGER NOT NULL,
            PRIMARY KEY(
                source_kind,file_key,processing_signature,
                text_fingerprint,classifier_signature,path
            )
        ) WITHOUT ROWID"""
    )
    connection.execute(
        """INSERT INTO classification_history_v2
        SELECT source_kind,file_key,processing_signature,text_fingerprint,
        classifier_signature,path,classification_json,classified_ns
        FROM classification_history"""
    )
    target_count = int(
        connection.execute("SELECT COUNT(*) FROM classification_history_v2").fetchone()[0]
    )
    if target_count != source_count:
        raise RuntimeError("document catalog history row count changed during v1 to v2 migration")
    connection.execute("DROP TABLE classification_history")
    connection.execute("ALTER TABLE classification_history_v2 RENAME TO classification_history")


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def _migrate_to_v3(
    connection: sqlite3.Connection,
    identity_migrator: Callable[[sqlite3.Connection], None],
) -> None:
    identity_migrator(connection)


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    additions = (
        ("primary_subtype", "primary_subtype TEXT"),
        ("equipment_json", "equipment_json TEXT NOT NULL DEFAULT '[]'"),
        ("activities_json", "activities_json TEXT NOT NULL DEFAULT '[]'"),
    )
    for column, declaration in additions:
        _add_column_if_missing(connection, "documents", column, declaration)
    connection.execute("DROP INDEX IF EXISTS catalog_documents_kind_idx")
    connection.execute(
        """CREATE INDEX catalog_documents_kind_idx
        ON documents(active,primary_kind,primary_subtype,primary_authority,
        primary_organization,path)"""
    )


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    document_additions = (
        ("primary_client", "primary_client TEXT"),
        ("primary_project", "primary_project TEXT"),
        ("primary_workstream", "primary_workstream TEXT"),
        ("clients_json", "clients_json TEXT NOT NULL DEFAULT '[]'"),
        ("projects_json", "projects_json TEXT NOT NULL DEFAULT '[]'"),
        ("workstreams_json", "workstreams_json TEXT NOT NULL DEFAULT '[]'"),
    )
    plan_additions = (
        ("move_completed_ns", "move_completed_ns INTEGER"),
        (
            "cache_sync_status",
            "cache_sync_status TEXT NOT NULL DEFAULT 'pending'",
        ),
        ("cache_sync_json", "cache_sync_json TEXT"),
        ("cache_sync_error", "cache_sync_error TEXT"),
    )
    for column, declaration in document_additions:
        _add_column_if_missing(connection, "documents", column, declaration)
    for column, declaration in plan_additions:
        _add_column_if_missing(connection, "organization_plans", column, declaration)
    connection.execute("DROP INDEX IF EXISTS catalog_documents_kind_idx")
    connection.execute(
        """CREATE INDEX catalog_documents_kind_idx
        ON documents(active,primary_kind,primary_subtype,primary_authority,
        primary_organization,primary_client,primary_project,path)"""
    )
    connection.execute("DROP INDEX IF EXISTS organization_plans_destination_idx")
    connection.execute(
        """CREATE UNIQUE INDEX organization_plans_destination_idx
        ON organization_plans(destination_path)
        WHERE status IN ('planned','applying','moved_cache_pending')
        AND destination_path IS NOT NULL"""
    )


def _create_v5_schema(connection: sqlite3.Connection) -> None:
    for statement in _V5_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _v5_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_v5_schema)


def validate_v5_document_catalog_schema(connection: sqlite3.Connection) -> None:
    """Abstain before writable migration if a v5 object is not understood."""

    validate_sqlite_schema_contract(
        connection,
        _v5_schema_contract(),
        label="document catalog v5 migration source",
        exact=True,
    )


def _create_v6_schema(connection: sqlite3.Connection) -> None:
    for statement in _V6_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _v6_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_v6_schema)


def validate_v6_document_catalog_schema(connection: sqlite3.Connection) -> None:
    """Abstain before writable path migration if schema 6 is not exact."""

    validate_sqlite_schema_contract(
        connection,
        _v6_schema_contract(),
        label="document catalog v6 migration source",
        exact=True,
    )


def _migrate_to_v6(connection: sqlite3.Connection) -> None:
    """Create isolated generations only from the exact understood v5 contract."""

    validate_v5_document_catalog_schema(connection)
    connection.execute("DROP INDEX organization_plans_destination_idx")
    connection.execute(_CURRENT_ORGANIZATION_DESTINATION_INDEX_DDL)
    for statement in _GENERATION_SCHEMA_DDL:
        connection.execute(statement)
    now = time.time_ns()
    source_kinds = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT source_kind FROM documents ORDER BY source_kind"
        )
    )
    migrated_rows = 0
    for source_kind in source_kinds:
        cursor = connection.execute(
            """INSERT INTO catalog_generations(
            catalog_run_id,source_kind,base_generation_id,status,started_ns,
            completed_ns,published_ns)
            VALUES(NULL,?,NULL,'published',?,?,?)""",
            (source_kind, now, now, now),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("catalog v5 migration did not create a generation")
        generation_id = int(cursor.lastrowid)
        source_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM documents WHERE source_kind=?",
                (source_kind,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO catalog_generation_documents(
            generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns,source_status,processing_signature,text_fingerprint,
            classifier_signature,primary_kind,primary_subtype,primary_authority,
            primary_organization,primary_client,primary_project,primary_workstream,
            confidence,uncertainty,standard_references_json,organizations_json,
            clients_json,projects_json,workstreams_json,topics_json,equipment_json,
            activities_json,classification_json,catalog_status,error_type,
            error_message,active,last_seen_catalog_run_id,updated_ns)
            SELECT ?,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns,source_status,processing_signature,text_fingerprint,
            classifier_signature,primary_kind,primary_subtype,primary_authority,
            primary_organization,primary_client,primary_project,primary_workstream,
            confidence,uncertainty,standard_references_json,organizations_json,
            clients_json,projects_json,workstreams_json,topics_json,equipment_json,
            activities_json,classification_json,catalog_status,error_type,
            error_message,active,last_seen_catalog_run_id,updated_ns
            FROM documents WHERE source_kind=?""",
            (generation_id, source_kind),
        )
        staged_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM catalog_generation_documents
                WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()[0]
        )
        if staged_count != source_count:
            raise RuntimeError("document catalog row count changed during v5 to v6 migration")
        migrated_rows += staged_count
        connection.execute(
            """INSERT INTO catalog_publications(
            source_kind,generation_id,published_ns) VALUES(?,?,?)""",
            (source_kind, generation_id, now),
        )
    document_count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    if migrated_rows != document_count:
        raise RuntimeError("document catalog total row count changed during v5 to v6 migration")
    violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise RuntimeError("document catalog v5 to v6 migration violated foreign keys")


_PATH_REBUILD_TABLES = (
    "documents",
    "organization_plans",
    "catalog_generation_documents",
)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _ordered_columns(
    connection: sqlite3.Connection,
    table: str,
    *,
    schema: str = "main",
) -> tuple[str, ...]:
    if schema not in {"main", "temp"}:  # pragma: no cover - internal invariant
        raise ValueError(f"unsupported SQLite schema: {schema}")
    quoted = _quoted_identifier(table)
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA {schema}.table_info({quoted})"))


def _copy_catalog_table_exact(
    connection: sqlite3.Connection,
    *,
    source: str,
    target: str,
) -> None:
    source_columns = _ordered_columns(connection, source, schema="temp")
    target_columns = _ordered_columns(connection, target)
    if not source_columns or set(source_columns) != set(target_columns):
        raise RuntimeError(
            f"document catalog path migration columns changed for {target}: "
            f"source={source_columns!r} target={target_columns!r}"
        )
    columns = ",".join(_quoted_identifier(column) for column in target_columns)
    connection.execute(
        f"INSERT INTO main.{_quoted_identifier(target)}({columns}) "
        f"SELECT {columns} FROM temp.{_quoted_identifier(source)}"
    )


def _migrate_to_v7(connection: sqlite3.Connection) -> None:
    """Apply platform-native path identity without losing catalog evidence."""

    validate_v6_document_catalog_schema(connection)
    if _PATH_COLLATION == "NOCASE":
        return
    row_counts = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM main.{_quoted_identifier(table)}").fetchone()[
                0
            ]
        )
        for table in _PATH_REBUILD_TABLES
    }
    for table in _PATH_REBUILD_TABLES:
        backup = f"document_catalog_v6_{table}"
        connection.execute(
            f"CREATE TEMP TABLE {_quoted_identifier(backup)} AS "
            f"SELECT * FROM main.{_quoted_identifier(table)}"
        )

    connection.execute("DROP TABLE catalog_generation_documents")
    connection.execute("DROP TABLE organization_plans")
    connection.execute("DROP TABLE documents")
    create_document_catalog_schema(connection)

    for table in _PATH_REBUILD_TABLES:
        backup = f"document_catalog_v6_{table}"
        _copy_catalog_table_exact(connection, source=backup, target=table)
        migrated_count = int(
            connection.execute(f"SELECT COUNT(*) FROM main.{_quoted_identifier(table)}").fetchone()[
                0
            ]
        )
        if migrated_count != row_counts[table]:
            raise RuntimeError(f"document catalog path migration changed {table} row count")
        connection.execute(f"DROP TABLE temp.{_quoted_identifier(backup)}")
    violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise RuntimeError("document catalog path migration violated foreign keys")


def migrate_document_catalog_schema(
    connection: sqlite3.Connection,
    prior_version: int,
    *,
    identity_migrator: Callable[[sqlite3.Connection], None],
) -> None:
    """Apply every required historical step without committing the transaction."""

    migrations: dict[int, Callable[[], None]] = {
        1: lambda: _migrate_to_v1(connection),
        2: lambda: _migrate_to_v2(connection),
        3: lambda: _migrate_to_v3(connection, identity_migrator),
        4: lambda: _migrate_to_v4(connection),
        5: lambda: _migrate_to_v5(connection),
        6: lambda: _migrate_to_v6(connection),
        7: lambda: _migrate_to_v7(connection),
    }
    for target_version in range(prior_version + 1, CATALOG_SCHEMA_VERSION + 1):
        migrations[target_version]()
        _set_schema_version(connection, target_version)
    create_document_catalog_schema(connection)


# endregion [02]


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "create_document_catalog_schema",
    "document_catalog_schema_contract",
    "migrate_document_catalog_schema",
    "validate_v5_document_catalog_schema",
    "validate_v6_document_catalog_schema",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_catalog_schema")
