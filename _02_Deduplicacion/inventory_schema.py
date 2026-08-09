"""Transactional schema lifecycle for the deduplication inventory."""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path

from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContract,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)
from neocortex.sqlite_schema_lifecycle import (
    existing_sqlite_uri,
    initialize_versioned_sqlite_schema,
    readonly_sqlite_uri,
)

from .errors import InventoryError


SCHEMA_VERSION = 9
_SCHEMA_LABEL = "dedup inventory"
_PATH_COLLATION = "NOCASE" if os.name == "nt" else "BINARY"
_METADATA_DDL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID
"""
_V8_CHECKPOINT_DDL = """
CREATE TABLE inventory_checkpoints (
    root TEXT PRIMARY KEY COLLATE NOCASE,
    scan_id INTEGER NOT NULL,
    volume TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    next_usn INTEGER NOT NULL,
    valid INTEGER NOT NULL CHECK(valid IN (0,1)),
    updated_ns INTEGER NOT NULL,
    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE RESTRICT
) WITHOUT ROWID
"""
_CURRENT_CHECKPOINT_DDL = f"""
CREATE TABLE inventory_checkpoints (
    root TEXT PRIMARY KEY COLLATE {_PATH_COLLATION},
    scan_id INTEGER NOT NULL,
    volume TEXT,
    journal_id TEXT,
    next_usn INTEGER,
    valid INTEGER NOT NULL CHECK(valid IN (0,1)),
    updated_ns INTEGER NOT NULL,
    FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE RESTRICT,
    CHECK(
        (volume IS NULL AND journal_id IS NULL AND next_usn IS NULL)
        OR
        (volume IS NOT NULL AND journal_id IS NOT NULL AND next_usn IS NOT NULL)
    )
) WITHOUT ROWID
"""
_CURRENT_DDL = (
    _METADATA_DDL,
    """
    CREATE TABLE scans (
        scan_id INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        root_volume_id BLOB,
        root_file_id BLOB,
        root_birthtime_ns INTEGER,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        files_seen INTEGER,
        directories_seen INTEGER,
        bytes_seen INTEGER,
        skipped_links INTEGER,
        excluded_directories INTEGER,
        errors INTEGER,
        status TEXT NOT NULL DEFAULT 'building'
            CHECK(status IN ('building','complete','partial')),
        inventory_policy_signature TEXT
    )
    """,
    _CURRENT_CHECKPOINT_DDL,
    f"""
    CREATE TABLE files (
        scan_id INTEGER NOT NULL,
        path TEXT NOT NULL COLLATE {_PATH_COLLATION},
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        PRIMARY KEY(scan_id, path),
        FOREIGN KEY(scan_id) REFERENCES scans(scan_id) ON DELETE RESTRICT
    ) WITHOUT ROWID
    """,
    "CREATE INDEX files_scan_size_idx ON files(scan_id, size)",
    "CREATE INDEX files_identity_idx ON files(volume_id, file_id)",
    f"CREATE INDEX files_path_scan_idx ON files(path COLLATE {_PATH_COLLATION}, scan_id)",
    """
    CREATE TABLE fingerprints (
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL DEFAULT -1,
        algorithm TEXT NOT NULL,
        digest BLOB NOT NULL,
        PRIMARY KEY(volume_id, file_id, size, mtime_ns, algorithm)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE duplicate_plan_summaries (
        scan_id INTEGER PRIMARY KEY,
        group_count INTEGER NOT NULL,
        redundant_files INTEGER NOT NULL,
        reclaimable_bytes INTEGER NOT NULL,
        completed_ns INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE planned_duplicate_groups (
        group_id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL,
        size INTEGER NOT NULL,
        keep_path TEXT NOT NULL,
        redundant_count INTEGER NOT NULL,
        reclaimable_bytes INTEGER NOT NULL,
        full_fingerprint TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX planned_groups_scan_order_idx
    ON planned_duplicate_groups(scan_id, reclaimable_bytes DESC, keep_path)
    """,
    """
    CREATE TABLE planned_duplicate_members (
        group_id INTEGER NOT NULL,
        member_order INTEGER NOT NULL,
        role TEXT NOT NULL,
        path TEXT NOT NULL,
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        PRIMARY KEY(group_id, member_order)
    ) WITHOUT ROWID
    """,
    f"""
    CREATE INDEX planned_members_path_idx
    ON planned_duplicate_members(path COLLATE {_PATH_COLLATION}, role)
    """,
)

# The first seven v9 statements own generation publication; later statements
# are unchanged cache/plan objects shared with v6 and v7. Explicit legacy
# builders let migrations abstain on unknown source structures.
_CURRENT_SHARED_DDL_START = 7
# Schemas v1-v8 were Windows-only and therefore always used NOCASE for the
# persisted plan-path index.  Keep that historical contract independent from
# the host performing a migration; fresh v9 Linux state uses BINARY instead.
_LEGACY_SHARED_DDL = tuple(
    statement.replace(f"COLLATE {_PATH_COLLATION}", "COLLATE NOCASE")
    for statement in _CURRENT_DDL[_CURRENT_SHARED_DDL_START:]
)
_V8_GENERATIONAL_DDL = (
    _METADATA_DDL,
    _CURRENT_DDL[1],
    _V8_CHECKPOINT_DDL,
    *_CURRENT_DDL[3:_CURRENT_SHARED_DDL_START],
)
_V7_GENERATIONAL_DDL = (
    _METADATA_DDL,
    """
    CREATE TABLE scans (
        scan_id INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        root_volume_id BLOB,
        root_file_id BLOB,
        root_birthtime_ns INTEGER,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        files_seen INTEGER,
        directories_seen INTEGER,
        bytes_seen INTEGER,
        skipped_links INTEGER,
        excluded_directories INTEGER,
        errors INTEGER,
        status TEXT NOT NULL DEFAULT 'building'
            CHECK(status IN ('building','complete','partial'))
    )
    """,
    _V8_CHECKPOINT_DDL,
    *_CURRENT_DDL[3:_CURRENT_SHARED_DDL_START],
)
_V6_GENERATIONAL_DDL = (
    _METADATA_DDL,
    """
    CREATE TABLE scans (
        scan_id INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        root_volume_id BLOB,
        root_file_id BLOB,
        root_birthtime_ns INTEGER,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        files_seen INTEGER,
        directories_seen INTEGER,
        bytes_seen INTEGER,
        skipped_links INTEGER,
        excluded_directories INTEGER,
        errors INTEGER
    )
    """,
    """
    CREATE TABLE inventory_checkpoints (
        root TEXT PRIMARY KEY COLLATE NOCASE,
        scan_id INTEGER NOT NULL,
        volume TEXT NOT NULL,
        journal_id TEXT NOT NULL,
        next_usn INTEGER NOT NULL,
        valid INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE files (
        path TEXT PRIMARY KEY COLLATE NOCASE,
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        scan_id INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
    "CREATE INDEX files_scan_size_idx ON files(scan_id, size)",
    "CREATE INDEX files_identity_idx ON files(volume_id, file_id)",
)
_V2_OBJECT_DDL = (
    """
    CREATE TABLE IF NOT EXISTS scans (
        scan_id INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_checkpoints (
        root TEXT PRIMARY KEY COLLATE NOCASE,
        scan_id INTEGER NOT NULL,
        volume TEXT NOT NULL,
        journal_id TEXT NOT NULL,
        next_usn INTEGER NOT NULL,
        valid INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY COLLATE NOCASE,
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        scan_id INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS files_scan_size_idx ON files(scan_id, size)",
    "CREATE INDEX IF NOT EXISTS files_identity_idx ON files(volume_id, file_id)",
    """
    CREATE TABLE IF NOT EXISTS fingerprints (
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        algorithm TEXT NOT NULL,
        digest BLOB NOT NULL,
        PRIMARY KEY(volume_id, file_id, size, mtime_ns, algorithm)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS duplicate_plan_summaries (
        scan_id INTEGER PRIMARY KEY,
        group_count INTEGER NOT NULL,
        redundant_files INTEGER NOT NULL,
        reclaimable_bytes INTEGER NOT NULL,
        completed_ns INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS planned_duplicate_groups (
        group_id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL,
        size INTEGER NOT NULL,
        keep_path TEXT NOT NULL,
        redundant_count INTEGER NOT NULL,
        reclaimable_bytes INTEGER NOT NULL,
        full_fingerprint TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS planned_groups_scan_order_idx
    ON planned_duplicate_groups(scan_id, reclaimable_bytes DESC, keep_path)
    """,
    """
    CREATE TABLE IF NOT EXISTS planned_duplicate_members (
        group_id INTEGER NOT NULL,
        member_order INTEGER NOT NULL,
        role TEXT NOT NULL,
        path TEXT NOT NULL,
        volume_id BLOB NOT NULL,
        file_id BLOB NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        PRIMARY KEY(group_id, member_order)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS planned_members_path_idx
    ON planned_duplicate_members(path COLLATE NOCASE, role)
    """,
)
_SCAN_COUNTER_COLUMNS = (
    ("files_seen", "INTEGER"),
    ("directories_seen", "INTEGER"),
    ("bytes_seen", "INTEGER"),
    ("skipped_links", "INTEGER"),
    ("excluded_directories", "INTEGER"),
    ("errors", "INTEGER"),
)
_SCAN_ROOT_COLUMNS = (
    ("root_volume_id", "BLOB"),
    ("root_file_id", "BLOB"),
    ("root_birthtime_ns", "INTEGER"),
)


# region [01] Contract construction and validation


def _configure_owner_connection(
    connection: sqlite3.Connection,
    *,
    readonly: bool,
) -> sqlite3.Connection:
    try:
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise InventoryError("dedup inventory could not enable foreign keys")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise InventoryError("dedup inventory connection is not query-only")
    except BaseException:
        connection.close()
        raise
    return connection


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(readonly_sqlite_uri(path), uri=True, timeout=60.0)
    else:
        connection = sqlite3.connect(path, timeout=60.0)
    return _configure_owner_connection(connection, readonly=readonly)


def _connect_existing(path: Path) -> sqlite3.Connection:
    """Open an accepted inventory database without recreating missing state."""

    connection = sqlite3.connect(existing_sqlite_uri(path), uri=True, timeout=60.0)
    return _configure_owner_connection(connection, readonly=False)


def _execute_ddl(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _build_metadata_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_METADATA_DDL)


def _build_current_schema(connection: sqlite3.Connection) -> None:
    _execute_ddl(connection, _CURRENT_DDL)


def _build_v6_schema(connection: sqlite3.Connection) -> None:
    _execute_ddl(connection, _V6_GENERATIONAL_DDL)
    _execute_ddl(connection, _LEGACY_SHARED_DDL)


def _build_v7_schema(connection: sqlite3.Connection) -> None:
    _execute_ddl(connection, _V7_GENERATIONAL_DDL)
    _execute_ddl(connection, _LEGACY_SHARED_DDL)


def _build_v8_schema(connection: sqlite3.Connection) -> None:
    _execute_ddl(connection, _V8_GENERATIONAL_DDL)
    _execute_ddl(connection, _LEGACY_SHARED_DDL)


@lru_cache(maxsize=1)
def _metadata_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_metadata_schema)


@lru_cache(maxsize=1)
def inventory_schema_contract() -> SQLiteSchemaContract:
    """Return the exact structural contract for inventory schema v9."""

    return schema_contract_from_builder(_build_current_schema)


@lru_cache(maxsize=1)
def _inventory_v6_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v6_schema)


@lru_cache(maxsize=1)
def _inventory_v7_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v7_schema)


@lru_cache(maxsize=1)
def _inventory_v8_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v8_schema)


def _validate_metadata(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        _metadata_contract(),
        label=f"{_SCHEMA_LABEL} metadata",
    )


def validate_inventory_schema(connection: sqlite3.Connection) -> None:
    """Validate every persistent v9 table and index without changing state."""

    validate_sqlite_schema_contract(
        connection,
        inventory_schema_contract(),
        label=_SCHEMA_LABEL,
        exact=True,
    )


# endregion [01]


# region [02] Sequential migrations


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    definitions: tuple[tuple[str, str], ...],
) -> None:
    present = _column_names(connection, table)
    for name, definition in definitions:
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _ensure_v2_objects(connection: sqlite3.Connection) -> None:
    _execute_ddl(connection, _V2_OBJECT_DDL)


def _add_scan_counters(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "scans", _SCAN_COUNTER_COLUMNS)


def _add_fingerprint_birthtime(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "fingerprints",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )


def _add_scan_root_identity(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "scans", _SCAN_ROOT_COLUMNS)


def _invalidate_checkpoints(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE inventory_checkpoints SET valid=0 WHERE valid<>0")


def _advance_version(connection: sqlite3.Connection, version: int) -> None:
    cursor = connection.execute(
        "UPDATE metadata SET value=? WHERE key='schema_version'",
        (str(version),),
    )
    if cursor.rowcount != 1:
        raise InventoryError("dedup inventory metadata lost its schema version")


def _migrate_one_to_two(connection: sqlite3.Connection) -> None:
    _ensure_v2_objects(connection)


def _migrate_two_to_three(connection: sqlite3.Connection) -> None:
    _ensure_v2_objects(connection)
    _add_scan_counters(connection)


def _migrate_three_to_four(connection: sqlite3.Connection) -> None:
    _ensure_v2_objects(connection)
    _add_scan_counters(connection)
    _invalidate_checkpoints(connection)


def _migrate_four_to_five(connection: sqlite3.Connection) -> None:
    _ensure_v2_objects(connection)
    _add_scan_counters(connection)
    _add_fingerprint_birthtime(connection)


def _migrate_five_to_six(connection: sqlite3.Connection) -> None:
    _ensure_v2_objects(connection)
    _add_scan_counters(connection)
    _add_fingerprint_birthtime(connection)
    _add_scan_root_identity(connection)
    _invalidate_checkpoints(connection)


def _migrate_six_to_seven(connection: sqlite3.Connection) -> None:
    """Rebuild path rows as isolated generations after an exact v6 preflight."""

    validate_sqlite_schema_contract(
        connection,
        _inventory_v6_schema_contract(),
        label=f"{_SCHEMA_LABEL} v6 migration source",
        exact=True,
    )
    invalid_flag = connection.execute(
        "SELECT 1 FROM inventory_checkpoints WHERE valid NOT IN (0,1) LIMIT 1"
    ).fetchone()
    if invalid_flag is not None:
        raise InventoryError("dedup inventory v6 has a non-boolean checkpoint flag")
    orphan_checkpoint = connection.execute(
        """SELECT 1 FROM inventory_checkpoints c
        WHERE NOT EXISTS(SELECT 1 FROM scans s WHERE s.scan_id=c.scan_id)
        LIMIT 1"""
    ).fetchone()
    orphan_file = connection.execute(
        """SELECT 1 FROM files f
        WHERE NOT EXISTS(SELECT 1 FROM scans s WHERE s.scan_id=f.scan_id)
        LIMIT 1"""
    ).fetchone()
    if orphan_checkpoint is not None or orphan_file is not None:
        raise InventoryError("dedup inventory v6 contains orphan generation references")

    file_count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0]
    )
    connection.execute("ALTER TABLE files RENAME TO files_v6")
    connection.execute("ALTER TABLE inventory_checkpoints RENAME TO inventory_checkpoints_v6")
    connection.execute(
        """ALTER TABLE scans ADD COLUMN status TEXT NOT NULL DEFAULT 'building'
        CHECK(status IN ('building','complete','partial'))"""
    )
    connection.execute(
        """UPDATE scans SET status=CASE
        WHEN completed_ns IS NULL THEN 'building'
        WHEN errors=0
          AND files_seen IS NOT NULL AND files_seen>=0
          AND directories_seen IS NOT NULL AND directories_seen>=0
          AND bytes_seen IS NOT NULL AND bytes_seen>=0
          AND skipped_links IS NOT NULL AND skipped_links>=0
          AND excluded_directories IS NOT NULL AND excluded_directories>=0
          AND files_seen=(SELECT COUNT(*) FROM files_v6 f
                          WHERE f.scan_id=scans.scan_id)
          AND bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files_v6 f
                          WHERE f.scan_id=scans.scan_id)
        THEN 'complete' ELSE 'partial' END"""
    )
    connection.execute(_V8_CHECKPOINT_DDL)
    connection.execute(_CURRENT_DDL[3])
    connection.execute(
        """INSERT INTO files(
        scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
        SELECT scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files_v6"""
    )
    connection.execute(
        """INSERT INTO inventory_checkpoints(
        root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
        SELECT c.root,c.scan_id,c.volume,c.journal_id,c.next_usn,
               CASE WHEN c.valid=1 AND s.status='complete' THEN 1 ELSE 0 END,
               c.updated_ns
        FROM inventory_checkpoints_v6 c
        JOIN scans s ON s.scan_id=c.scan_id"""
    )
    if int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]) != file_count:
        raise InventoryError("dedup inventory v7 file count changed during migration")
    if (
        int(connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0])
        != checkpoint_count
    ):
        raise InventoryError("dedup inventory v7 checkpoint count changed during migration")
    connection.execute("DROP TABLE inventory_checkpoints_v6")
    connection.execute("DROP TABLE files_v6")
    for statement in _CURRENT_DDL[4:_CURRENT_SHARED_DDL_START]:
        connection.execute(statement)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v7 foreign-key validation failed")


def _migrate_seven_to_eight(connection: sqlite3.Connection) -> None:
    """Bind future scans to policy signatures without inventing legacy evidence."""

    validate_sqlite_schema_contract(
        connection,
        _inventory_v7_schema_contract(),
        label=f"{_SCHEMA_LABEL} v7 migration source",
        exact=True,
    )
    scan_count = int(connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0]
    )
    file_count, total_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
    ).fetchone()

    _add_columns(
        connection,
        "scans",
        (("inventory_policy_signature", "TEXT"),),
    )
    _invalidate_checkpoints(connection)

    if int(connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0]) != scan_count:
        raise InventoryError("dedup inventory v8 scan count changed during migration")
    if (
        int(connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0])
        != checkpoint_count
    ):
        raise InventoryError("dedup inventory v8 checkpoint count changed during migration")
    migrated_file_count, migrated_total_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
    ).fetchone()
    if (int(migrated_file_count), int(migrated_total_bytes)) != (
        int(file_count),
        int(total_bytes),
    ):
        raise InventoryError("dedup inventory v8 file evidence changed during migration")
    if (
        connection.execute("SELECT 1 FROM inventory_checkpoints WHERE valid<>0 LIMIT 1").fetchone()
        is not None
    ):
        raise InventoryError("dedup inventory v8 retained an unbound checkpoint")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v8 foreign-key validation failed")


def _migrate_eight_to_nine(connection: sqlite3.Connection) -> None:
    """Decouple snapshot publication from the optional USN acceleration cursor."""

    validate_sqlite_schema_contract(
        connection,
        _inventory_v8_schema_contract(),
        label=f"{_SCHEMA_LABEL} v8 migration source",
        exact=True,
    )
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0]
    )
    connection.execute("ALTER TABLE inventory_checkpoints RENAME TO inventory_checkpoints_v8")
    connection.execute(_CURRENT_CHECKPOINT_DDL)
    connection.execute(
        """INSERT INTO inventory_checkpoints(
        root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
        SELECT root,scan_id,volume,journal_id,next_usn,valid,updated_ns
        FROM inventory_checkpoints_v8"""
    )
    if (
        int(connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0])
        != checkpoint_count
    ):
        raise InventoryError("dedup inventory v9 publication count changed during migration")
    connection.execute("DROP TABLE inventory_checkpoints_v8")
    if _PATH_COLLATION != "NOCASE":
        connection.execute("DROP INDEX planned_members_path_idx")
        connection.execute(_CURRENT_DDL[-1])
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v9 foreign-key validation failed")


_MIGRATIONS = {
    1: _migrate_one_to_two,
    2: _migrate_two_to_three,
    3: _migrate_three_to_four,
    4: _migrate_four_to_five,
    5: _migrate_five_to_six,
    6: _migrate_six_to_seven,
    7: _migrate_seven_to_eight,
    8: _migrate_eight_to_nine,
}


def _migrate(connection: sqlite3.Connection, version: int) -> None:
    while version < SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise InventoryError(f"no inventory migration exists for schema {version}")
        migration(connection)
        version += 1
        _advance_version(connection, version)


# endregion [02]


# region [03] Public lifecycle


def _create_fresh(connection: sqlite3.Connection) -> None:
    _build_current_schema(connection)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )


def initialize_inventory_schema(database: str | Path) -> None:
    """Create, migrate, or read-only validate one inventory database."""

    try:
        initialize_versioned_sqlite_schema(
            Path(database),
            label=_SCHEMA_LABEL,
            current_version=SCHEMA_VERSION,
            connect=_connect,
            validate_metadata=_validate_metadata,
            validate_current=validate_inventory_schema,
            create_fresh=_create_fresh,
            migrate=_migrate,
        )
    except InventoryError:
        raise
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        raise InventoryError(str(exc)) from exc


def configure_inventory_connection(connection: sqlite3.Connection) -> None:
    """Apply bounded operational settings after the schema is accepted."""

    for statement in (
        "PRAGMA busy_timeout=60000",
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA cache_size=-32768",
        "PRAGMA wal_autocheckpoint=4096",
        "PRAGMA journal_size_limit=268435456",
    ):
        connection.execute(statement)
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise InventoryError("dedup inventory connection has foreign keys disabled")


__all__ = [
    "SCHEMA_VERSION",
    "configure_inventory_connection",
    "initialize_inventory_schema",
    "inventory_schema_contract",
    "validate_inventory_schema",
]


# endregion [03]
