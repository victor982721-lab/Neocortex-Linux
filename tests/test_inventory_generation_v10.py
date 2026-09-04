"""Identity-query indexes and migration contracts for inventory schema v11.

Every database is a bounded synthetic ``tmp_path`` fixture.  The regression
executes the production Knowledge inventory query and inspects SQLite's query
plan; it does not rely on machine-dependent wall-clock thresholds.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import persistence as inventory_schema_module
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.deduplication.persistence import (
    inventory_schema_contract as persistent_inventory_schema_contract,
)
from neocortex.deduplication.persistence.ddl import V10_DDL, V9_DDL
from neocortex.deduplication.persistence.migrations import MIGRATIONS
from neocortex.knowledge import knowledge_search_inventory


_NEW_INDEXES = {
    "files_identity_birth_scan_idx": (
        "CREATE INDEX files_identity_birth_scan_idx "
        "ON files(volume_id, file_id, birthtime_ns, scan_id)"
    ),
    "planned_members_identity_idx": (
        "CREATE INDEX planned_members_identity_idx "
        "ON planned_duplicate_members(volume_id, file_id, birthtime_ns)"
    ),
}


def test_schema_persistence_api_and_versioned_migration_registry_are_explicit() -> None:
    assert {
        "SCHEMA_VERSION",
        "configure_inventory_connection",
        "initialize_inventory_schema",
        "inventory_schema_contract",
        "validate_inventory_schema",
    } <= set(inventory_schema_module.__all__)
    assert inventory_schema_module.inventory_schema_contract() is (
        persistent_inventory_schema_contract()
    )
    assert {version: migration.__module__ for version, migration in MIGRATIONS.items()} == {
        version: f"neocortex.deduplication.persistence.migrations.v{version}_to_v{version + 1}"
        for version in range(1, 11)
    }


def _blob(value: int) -> bytes:
    return value.to_bytes(16, "little", signed=False)


def _create_populated_v9(
    database: Path,
    root: Path,
    *,
    unexpected_index: bool = False,
    ddl: tuple[str, ...] = V9_DDL,
    schema_version: int = 9,
) -> None:
    keep_path = str(root / "keep.bin")
    redundant_path = str(root / "redundant.bin")
    with sqlite3.connect(database) as connection:
        for statement in ddl:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
            (str(schema_version),),
        )
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,root_volume_id,root_file_id,root_birthtime_ns,
            started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(9,?,?,?,?,?,?,?,?,?,?,?,?,'complete',?)""",
            (
                str(root),
                _blob(1),
                _blob(2),
                3,
                10,
                20,
                2,
                1,
                20,
                0,
                0,
                0,
                "inventory-exclusion-policy-v2:xxh3_128:" + ("1" * 32),
            ),
        )
        connection.executemany(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(9,?,?,?,?,?,?)""",
            (
                (keep_path, _blob(11), _blob(21), 10, 30, 101),
                (redundant_path, _blob(12), _blob(22), 10, 31, 102),
            ),
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES(?,9,NULL,NULL,NULL,1,40)""",
            (str(root),),
        )
        connection.execute(
            """INSERT INTO duplicate_plan_summaries(
            scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns)
            VALUES(9,1,1,10,20)"""
        )
        connection.execute(
            """INSERT INTO planned_duplicate_groups(
            group_id,scan_id,size,keep_path,redundant_count,
            reclaimable_bytes,full_fingerprint)
            VALUES(90,9,10,?,1,10,?)""",
            (keep_path, "a" * 64),
        )
        connection.executemany(
            """INSERT INTO planned_duplicate_members(
            group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns) VALUES(90,?,?,?,?,?,?,?,?)""",
            (
                (0, "keep", keep_path, _blob(11), _blob(21), 10, 30, 101),
                (
                    1,
                    "redundant",
                    redundant_path,
                    _blob(12),
                    _blob(22),
                    10,
                    31,
                    102,
                ),
            ),
        )
        if unexpected_index:
            connection.execute("CREATE INDEX unexpected_v9_idx ON files(mtime_ns)")


def _indexes(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): " ".join(str(sql).split())
        for name, sql in connection.execute(
            """SELECT name,sql FROM sqlite_master
            WHERE type='index' AND name IN (?,?) ORDER BY name""",
            tuple(sorted(_NEW_INDEXES)),
        )
    }


def test_fresh_v11_has_exact_identity_indexes_and_verification_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v10.sqlite3"

    inventory_schema_module.initialize_inventory_schema(database)
    inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("11",)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(duplicate_plan_summaries)")
        }
        assert "verification_mode" in columns
        assert _indexes(connection) == _NEW_INDEXES
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_populated_v9_to_v11_preserves_rows_bytes_and_foreign_keys(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v9.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v9(database, root)
    with sqlite3.connect(database) as connection:
        assert _indexes(connection) == {}

    inventory_schema_module.initialize_inventory_schema(database)
    migrated = database.read_bytes()
    inventory_schema_module.initialize_inventory_schema(database)

    assert database.read_bytes() == migrated
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("11",)
        assert connection.execute(
            "SELECT verification_mode FROM duplicate_plan_summaries WHERE scan_id=9"
        ).fetchone() == ("legacy_unknown",)
        assert connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
        ).fetchone() == (2, 20)
        assert connection.execute(
            """SELECT COUNT(*),COALESCE(SUM(size),0)
            FROM planned_duplicate_members"""
        ).fetchone() == (2, 20)
        assert connection.execute(
            "SELECT root,scan_id,valid FROM inventory_checkpoints"
        ).fetchone() == (str(root), 9, 1)
        assert _indexes(connection) == _NEW_INDEXES
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_populated_v10_to_v11_adds_unknown_verification_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v10.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v9(database, root, ddl=V10_DDL, schema_version=10)

    inventory_schema_module.initialize_inventory_schema(database)
    migrated = database.read_bytes()
    inventory_schema_module.initialize_inventory_schema(database)

    assert database.read_bytes() == migrated
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("11",)
        assert connection.execute(
            "SELECT verification_mode FROM duplicate_plan_summaries WHERE scan_id=9"
        ).fetchone() == ("legacy_unknown",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_schema_one_migrates_sequentially_through_v11(tmp_path: Path) -> None:
    database = tmp_path / "inventory-v1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE metadata(
            key TEXT PRIMARY KEY,value TEXT NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO metadata VALUES('schema_version','1');"""
        )

    inventory_schema_module.initialize_inventory_schema(database)
    inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("11",)
        assert _indexes(connection) == _NEW_INDEXES
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_unknown_v9_object_abstains_without_partial_v10_ddl(tmp_path: Path) -> None:
    database = tmp_path / "inventory-v9-extended.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v9(database, root, unexpected_index=True)

    with pytest.raises(InventoryError, match="v9 migration source"):
        inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("9",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='unexpected_v9_idx'"
        ).fetchone() == (1,)
        assert _indexes(connection) == {}
        assert connection.execute("SELECT COUNT(*),SUM(size) FROM files").fetchone() == (
            2,
            20,
        )


def test_v9_to_v10_foreign_key_failure_rolls_back_indexes_and_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v9-orphan.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v9(database, root)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(99,?,?,?,?,?,?)""",
            (str(root / "orphan.bin"), _blob(90), _blob(91), 7, 92, 93),
        )

    with pytest.raises(InventoryError, match="v10 foreign-key validation failed"):
        inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("9",)
        assert _indexes(connection) == {}
        assert connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
        ).fetchone() == (3, 27)
        assert len(connection.execute("PRAGMA foreign_key_check").fetchall()) == 1


def test_real_knowledge_inventory_query_uses_both_v10_identity_indexes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v9.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v9(database, root)
    inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA automatic_index=OFF")
        query_plan: list[sqlite3.Row] = []

        class ExplainingConnection:
            def execute(
                self,
                statement: str,
                parameters: tuple[object, ...],
            ) -> sqlite3.Cursor:
                query_plan.extend(
                    connection.execute(
                        "EXPLAIN QUERY PLAN " + statement,
                        parameters,
                    ).fetchall()
                )
                return connection.execute(statement, parameters)

        rows = knowledge_search_inventory._inventory_rows(
            ExplainingConnection(),
            ((12, 22, 102),),
            ((9, 20, 1, 1, 10),),
            10,
            _blob,
        )

    details = tuple(str(row[3]) for row in query_plan)
    assert len(rows) == 1
    assert any("files_identity_birth_scan_idx" in detail for detail in details), details
    assert any("planned_members_identity_idx" in detail for detail in details), details
