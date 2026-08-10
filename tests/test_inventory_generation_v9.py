"""Portable publication contracts introduced by dedup inventory schema v9."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _02_Deduplicacion import (
    DedupIndex,
    InventoryCheckpoint,
    InventoryExclusionPolicy,
)
from _02_Deduplicacion import inventory_schema as inventory_schema_module
from _02_Deduplicacion.errors import InventoryError


def _create_populated_v8(
    database: Path,
    root: Path,
    *,
    unexpected_checkpoint_column: bool = False,
) -> None:
    identity = bytes(16)
    policy_signature = "inventory-exclusion-policy-v2:xxh3_128:" + ("1" * 32)
    with sqlite3.connect(database) as connection:
        for statement in inventory_schema_module._V8_GENERATIONAL_DDL:
            connection.execute(statement)
        for statement in inventory_schema_module._LEGACY_SHARED_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','8')")
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,root_volume_id,root_file_id,root_birthtime_ns,
            started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors,status,
            inventory_policy_signature)
            VALUES(8,?,?,?,?,?,?,?,?,?,?,?,?,'complete',?)""",
            (
                str(root),
                identity,
                identity,
                1,
                10,
                20,
                1,
                1,
                7,
                0,
                0,
                0,
                policy_signature,
            ),
        )
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(8,?,?,?,?,?,?)""",
            (str(root / "a.bin"), identity, (1).to_bytes(16, "little"), 7, 4, 5),
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES(?,8,'fixture:','9',123,1,777)""",
            (str(root),),
        )
        if unexpected_checkpoint_column:
            connection.execute("ALTER TABLE inventory_checkpoints ADD COLUMN unexpected TEXT")
            connection.execute("UPDATE inventory_checkpoints SET unexpected='preserve-me'")


def test_fresh_current_schema_publishes_a_snapshot_without_inventing_usn(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    policy = InventoryExclusionPolicy.compile()

    with DedupIndex(database) as index:
        scan = index.scan(root, exclusion_policy=policy)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(root),
                scan.scan_id,
                None,
                None,
                None,
                True,
                policy.signature,
            )
        )
        checkpoint = index.inventory_checkpoint(root)
        published = list(index.published_snapshots(root))

    assert checkpoint is not None
    assert checkpoint.valid
    assert not checkpoint.journal_available
    assert (checkpoint.volume, checkpoint.journal_id, checkpoint.next_usn) == (
        None,
        None,
        None,
    )
    assert [Path(item.path).name for item in published] == ["source.py"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("10",)
        assert connection.execute(
            "SELECT volume,journal_id,next_usn,valid FROM inventory_checkpoints"
        ).fetchone() == (None, None, None, 1)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_current_schema_rejects_a_partial_optional_usn_cursor(tmp_path: Path) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy = InventoryExclusionPolicy.compile()

    with DedupIndex(database) as index:
        scan = index.scan(root, exclusion_policy=policy)
        with pytest.raises(InventoryError, match="partial USN cursor"):
            index.bind_inventory_checkpoint(
                InventoryCheckpoint(
                    str(root),
                    scan.scan_id,
                    None,
                    9,
                    None,
                    True,
                    policy.signature,
                )
            )


def test_v8_to_current_preserves_published_evidence_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v8.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v8(database, root)

    inventory_schema_module.initialize_inventory_schema(database)
    inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("10",)
        assert connection.execute(
            """SELECT root,scan_id,volume,journal_id,next_usn,valid,updated_ns
            FROM inventory_checkpoints"""
        ).fetchone() == (str(root), 8, "fixture:", "9", 123, 1, 777)
        assert connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
        ).fetchone() == (1, 7)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_unknown_v8_structure_abstains_without_changing_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v8-extended.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v8(database, root, unexpected_checkpoint_column=True)

    with pytest.raises(InventoryError, match="v8 migration source"):
        inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("8",)
        assert connection.execute("SELECT unexpected FROM inventory_checkpoints").fetchone() == (
            "preserve-me",
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
