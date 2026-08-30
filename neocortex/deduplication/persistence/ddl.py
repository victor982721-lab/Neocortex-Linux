"""Versioned DDL and builders for the deduplication inventory."""

from __future__ import annotations

import sqlite3

from neocortex.platform.policy import sqlite_path_collation


SCHEMA_VERSION = 10
SCHEMA_LABEL = "dedup inventory"
PATH_COLLATION = sqlite_path_collation()
METADATA_DDL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID
"""
V8_CHECKPOINT_DDL = """
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
V9_CHECKPOINT_DDL = f"""
CREATE TABLE inventory_checkpoints (
    root TEXT PRIMARY KEY COLLATE {PATH_COLLATION},
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
V9_PLANNED_MEMBERS_PATH_INDEX_DDL = f"""
CREATE INDEX planned_members_path_idx
ON planned_duplicate_members(path COLLATE {PATH_COLLATION}, role)
"""
V9_DDL = (
    METADATA_DDL,
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
    V9_CHECKPOINT_DDL,
    f"""
    CREATE TABLE files (
        scan_id INTEGER NOT NULL,
        path TEXT NOT NULL COLLATE {PATH_COLLATION},
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
    f"CREATE INDEX files_path_scan_idx ON files(path COLLATE {PATH_COLLATION}, scan_id)",
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
    V9_PLANNED_MEMBERS_PATH_INDEX_DDL,
)
V10_INDEX_DDL = (
    """
    CREATE INDEX files_identity_birth_scan_idx
    ON files(volume_id, file_id, birthtime_ns, scan_id)
    """,
    """
    CREATE INDEX planned_members_identity_idx
    ON planned_duplicate_members(volume_id, file_id, birthtime_ns)
    """,
)
CURRENT_DDL = (*V9_DDL, *V10_INDEX_DDL)

# The first seven v9 statements own generation publication; later statements
# are unchanged cache/plan objects shared with v6 and v7. Explicit legacy
# builders let migrations abstain on unknown source structures.
CURRENT_SHARED_DDL_START = 7
# Schemas v1-v8 were Windows-only and therefore always used NOCASE for the
# persisted plan-path index.  Keep that historical contract independent from
# the host performing a migration; v9 and later Linux state uses BINARY instead.
LEGACY_SHARED_DDL = tuple(
    statement.replace(f"COLLATE {PATH_COLLATION}", "COLLATE NOCASE")
    for statement in V9_DDL[CURRENT_SHARED_DDL_START:]
)
V8_GENERATIONAL_DDL = (
    METADATA_DDL,
    V9_DDL[1],
    V8_CHECKPOINT_DDL,
    *V9_DDL[3:CURRENT_SHARED_DDL_START],
)
V7_GENERATIONAL_DDL = (
    METADATA_DDL,
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
    V8_CHECKPOINT_DDL,
    *V9_DDL[3:CURRENT_SHARED_DDL_START],
)
V6_GENERATIONAL_DDL = (
    METADATA_DDL,
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
V2_OBJECT_DDL = (
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
SCAN_COUNTER_COLUMNS = (
    ("files_seen", "INTEGER"),
    ("directories_seen", "INTEGER"),
    ("bytes_seen", "INTEGER"),
    ("skipped_links", "INTEGER"),
    ("excluded_directories", "INTEGER"),
    ("errors", "INTEGER"),
)
SCAN_ROOT_COLUMNS = (
    ("root_volume_id", "BLOB"),
    ("root_file_id", "BLOB"),
    ("root_birthtime_ns", "INTEGER"),
)


def execute_ddl(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """Execute an ordered group of schema statements."""

    for statement in statements:
        connection.execute(statement)


def build_metadata_schema(connection: sqlite3.Connection) -> None:
    connection.execute(METADATA_DDL)


def build_current_schema(connection: sqlite3.Connection) -> None:
    execute_ddl(connection, CURRENT_DDL)


def build_v6_schema(connection: sqlite3.Connection) -> None:
    execute_ddl(connection, V6_GENERATIONAL_DDL)
    execute_ddl(connection, LEGACY_SHARED_DDL)


def build_v7_schema(connection: sqlite3.Connection) -> None:
    execute_ddl(connection, V7_GENERATIONAL_DDL)
    execute_ddl(connection, LEGACY_SHARED_DDL)


def build_v8_schema(connection: sqlite3.Connection) -> None:
    execute_ddl(connection, V8_GENERATIONAL_DDL)
    execute_ddl(connection, LEGACY_SHARED_DDL)


def build_v9_schema(connection: sqlite3.Connection) -> None:
    execute_ddl(connection, V9_DDL)
