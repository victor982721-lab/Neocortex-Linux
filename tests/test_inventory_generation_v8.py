"""Policy-bound dedup inventory schema v8 regression contracts.
# region [00] Contexto del módulo
# Módulo: tests/test_inventory_generation_v8.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


Every database and corpus in this module is a bounded ``tmp_path`` fixture.
Legacy migrations preserve evidence but deliberately invalidate checkpoints
whose producing exclusion policy cannot be proven.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import (
    DedupIndex,
    InventoryCheckpoint,
    InventoryExclusionPolicy,
)
from neocortex.deduplication import persistence as inventory_schema_module
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.deduplication.persistence.ddl import (
    LEGACY_SHARED_DDL,
    V7_GENERATIONAL_DDL,
)
# endregion [01]

# region [02] Implementación


def _create_populated_v7(
    database: Path,
    root: Path,
    *,
    unexpected_scan_column: bool = False,
) -> None:
    identity = bytes(16)
    with sqlite3.connect(database) as connection:
        for statement in V7_GENERATIONAL_DDL:
            connection.execute(statement)
        for statement in LEGACY_SHARED_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','7')")
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,root_volume_id,root_file_id,root_birthtime_ns,
            started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors,status)
            VALUES(7,?,?,?,?,?,?,?,?,?,?,?,?,'complete')""",
            (str(root), identity, identity, 1, 10, 20, 2, 1, 7, 0, 0, 0),
        )
        connection.executemany(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(7,?,?,?,?,?,?)""",
            (
                (str(root / "a.bin"), identity, (1).to_bytes(16, "little"), 3, 4, 5),
                (str(root / "b.bin"), identity, (2).to_bytes(16, "little"), 4, 5, 6),
            ),
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES(?,7,'fixture:','9',123,1,777)""",
            (str(root),),
        )
        if unexpected_scan_column:
            connection.execute("ALTER TABLE scans ADD COLUMN unexpected TEXT")
            connection.execute("UPDATE scans SET unexpected='preserve-me' WHERE scan_id=7")


def test_fresh_v8_persists_policy_signature_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy = InventoryExclusionPolicy.compile(directory_names=("internal",))

    with DedupIndex(database) as index:
        scan = index.scan(root, exclusion_policy=policy)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(root),
                scan.scan_id,
                "fixture:",
                9,
                123,
            )
        )
        checkpoint = index.inventory_checkpoint(root)

    inventory_schema_module.initialize_inventory_schema(database)
    inventory_schema_module.initialize_inventory_schema(database)

    assert checkpoint is not None
    assert checkpoint.inventory_policy_signature == policy.signature
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("10",)
        assert connection.execute(
            "SELECT inventory_policy_signature FROM scans WHERE scan_id=?",
            (scan.scan_id,),
        ).fetchone() == (policy.signature,)
        assert connection.execute("SELECT scan_id,valid FROM inventory_checkpoints").fetchone() == (
            scan.scan_id,
            1,
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_v7_to_v8_preserves_rows_and_bytes_but_invalidates_checkpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v7.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v7(database, root)

    inventory_schema_module.initialize_inventory_schema(database)
    inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("10",)
        assert connection.execute(
            """SELECT scan_id,status,files_seen,bytes_seen,
            inventory_policy_signature FROM scans"""
        ).fetchone() == (7, "complete", 2, 7, None)
        assert connection.execute(
            "SELECT path,size,scan_id FROM files ORDER BY path"
        ).fetchall() == [
            (str(root / "a.bin"), 3, 7),
            (str(root / "b.bin"), 4, 7),
        ]
        assert connection.execute(
            """SELECT root,scan_id,volume,journal_id,next_usn,valid,updated_ns
            FROM inventory_checkpoints"""
        ).fetchone() == (str(root), 7, "fixture:", "9", 123, 0, 777)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_v7_unknown_structure_abstains_without_changing_legacy_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v7-extended.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v7(database, root, unexpected_scan_column=True)

    with pytest.raises(InventoryError, match="v7 migration source"):
        inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("7",)
        assert connection.execute("SELECT unexpected FROM scans WHERE scan_id=7").fetchone() == (
            "preserve-me",
        )
        assert connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
        ).fetchone() == (2, 7)
        assert connection.execute("SELECT valid FROM inventory_checkpoints").fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_checkpoint_rejects_a_signature_that_differs_from_its_scan(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy = InventoryExclusionPolicy.compile()

    with DedupIndex(database) as index:
        scan = index.scan(root, exclusion_policy=policy)
        with pytest.raises(InventoryError, match="does not match its scan"):
            index.bind_inventory_checkpoint(
                InventoryCheckpoint(
                    str(root),
                    scan.scan_id,
                    "fixture:",
                    9,
                    123,
                    True,
                    "inventory-exclusion-policy-v1:xxh3_128:wrong",
                )
            )
        assert index.inventory_checkpoint(root) is None


# endregion [02]
