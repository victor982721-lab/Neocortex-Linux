"""Adversarial and legacy fixtures for additive inventory evidence migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, InventoryError
from neocortex.deduplication import persistence as inventory_schema_module
from neocortex.deduplication.persistence.ddl import build_v11_schema
from neocortex.deduplication.persistence.lifecycle import initialize_inventory_schema
from neocortex.deduplication.persistence.migrations import v11_to_v12
from neocortex.deduplication.persistence.validation import validate_inventory_schema


def _legacy(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        build_v11_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','11')")
        connection.execute("INSERT INTO duplicate_plan_summaries VALUES(7,1,1,4,99,'full_hash')")
        connection.execute("INSERT INTO planned_duplicate_groups VALUES(1,7,4,'/fixture/a',1,4,?)", ("ab"*16,))
        connection.executemany(
            "INSERT INTO planned_duplicate_members VALUES(?,?,?,?,?,?,?,?,?)",
            [(1, order, role, f"/fixture/{name}", b"\1"*16, file_id, 4, 17, -1)
             for order, role, name, file_id in ((0,"keep","a",b"\2"*16),(1,"redundant","b",b"\3"*16))],
        )


def test_v11_migration_preserves_legacy_evidence_without_inference(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    _legacy(database)
    with sqlite3.connect(database) as connection:
        before = v11_to_v12._legacy_plan_digest(connection)
    with DedupIndex(database) as index:
        group = next(index.iter_duplicate_groups(7))
        assert group.verification_mode == "legacy_unknown"
        assert group.proof is None
        assert all(member.proof_version == "legacy_unknown" for member in group.member_proofs)
        assert all(member.alias_count is None for member in group.member_proofs)
    with sqlite3.connect(database) as connection:
        assert v11_to_v12._legacy_plan_digest(connection) == before
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (
            str(inventory_schema_module.SCHEMA_VERSION),
        )
        assert connection.execute(
            "SELECT verification_mode,requested_policy,coverage,exact_comparisons,"
            "changed_or_unreadable_files FROM duplicate_plan_summaries"
        ).fetchone() == ("full_hash", "legacy_unknown", "legacy_unknown", None, None)
        validate_inventory_schema(connection)
    fresh = tmp_path / "fresh.sqlite3"
    initialize_inventory_schema(fresh)
    with sqlite3.connect(fresh) as connection:
        validate_inventory_schema(connection)


def test_v12_migration_failure_rolls_back_added_columns(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "legacy.sqlite3"
    _legacy(database)
    original = v11_to_v12.execute_ddl

    def interrupt(connection, statements):
        original(connection, statements[:1])
        raise InventoryError("fixture interruption")

    monkeypatch.setattr(v11_to_v12, "execute_ddl", interrupt)
    with pytest.raises(InventoryError, match="fixture interruption"):
        initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("11",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(duplicate_plan_summaries)")}
        assert "requested_policy" not in columns
        assert connection.execute("SELECT COUNT(*) FROM planned_duplicate_members").fetchone() == (2,)


def test_v12_migration_rejects_malformed_source_without_repair(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    _legacy(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE planned_duplicate_members ADD COLUMN unexpected TEXT")
    with pytest.raises(InventoryError):
        initialize_inventory_schema(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("11",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(planned_duplicate_groups)")}
        assert "proof_json" not in columns
