"""Inventory retention keeps durable plans, checkpoints, and successors reachable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
    InventoryError,
    InventoryExclusionPolicy,
    snapshot_path,
)


def _policy() -> InventoryExclusionPolicy:
    return InventoryExclusionPolicy.compile(())


def _checkpoint(root: Path, scan_id: int) -> InventoryCheckpoint:
    policy = _policy()
    return InventoryCheckpoint(
        str(root),
        scan_id,
        True,
        policy.signature,
    )


def test_prune_retains_old_published_plan_payload_and_its_inventory_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "first.bin").write_bytes(b"same")
    (root / "second.bin").write_bytes(b"same")
    database = tmp_path / "inventory.sqlite3"
    policy = _policy()

    with DedupIndex(database) as index:
        first = index.scan(root, exclusion_policy=policy)
        plan = DedupPlanner(index).plan(first.scan_id, preview_limit=None)
        assert plan.group_count == 1
        group_id = int(
            index._connection.execute(
                "SELECT group_id FROM planned_duplicate_groups WHERE scan_id=?",
                (first.scan_id,),
            ).fetchone()[0]
        )

        # Create enough newer publications for the first scan to be outside
        # the ordinary current/previous payload window.
        for payload in (b"ten", b"twenty", b"thirty"):
            (root / "first.bin").write_bytes(payload)
            newer = index.scan(root, exclusion_policy=policy)
            index.bind_inventory_checkpoint(_checkpoint(root, newer.scan_id))

        removed = index.prune_obsolete_state(protected_scan_ids=())

        assert removed["files"] == 2
        assert removed["plan_groups"] == 0
        assert removed["plan_members"] == 0
        assert index.file_count(first.scan_id) == 2
        assert index._connection.execute(
            "SELECT COUNT(*) FROM duplicate_plan_summaries WHERE scan_id=?",
            (first.scan_id,),
        ).fetchone() == (1,)
        assert index._connection.execute(
            "SELECT COUNT(*) FROM planned_duplicate_groups WHERE group_id=?",
            (group_id,),
        ).fetchone() == (1,)
        assert index._connection.execute(
            "SELECT COUNT(*) FROM planned_duplicate_members WHERE group_id=?",
            (group_id,),
        ).fetchone() == (2,)
        assert index._connection.execute(
            "SELECT COUNT(*) FROM duplicate_plan_heads WHERE scan_id=?",
            (first.scan_id,),
        ).fetchone() == (1,)
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prune_retains_complete_successor_component_for_published_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "item.bin"
    source.write_bytes(b"one")
    database = tmp_path / "inventory.sqlite3"
    policy = _policy()

    with DedupIndex(database) as index:
        original = index.scan(root, exclusion_policy=policy)
        index.bind_inventory_checkpoint(_checkpoint(root, original.scan_id))

        source.write_bytes(b"two")
        second_snapshot = snapshot_path(source)
        index.apply_reconciliation(
            original.scan_id,
            upserts=(second_snapshot,),
            checkpoint=_checkpoint(root, original.scan_id),
        )
        second = index.current_scan_id(original.scan_id)

        source.write_bytes(b"three")
        third_snapshot = snapshot_path(source)
        index.apply_reconciliation(
            original.scan_id,
            upserts=(third_snapshot,),
            checkpoint=_checkpoint(root, original.scan_id),
        )
        third = index.current_scan_id(original.scan_id)
        assert (original.scan_id, second, third) == (1, 2, 3)

        index.prune_obsolete_state(protected_scan_ids=())

        assert [
            int(row[0])
            for row in index._connection.execute(
                "SELECT scan_id FROM files WHERE scan_id IN (?,?,?) ORDER BY scan_id",
                (original.scan_id, second, third),
            )
        ] == [original.scan_id, second, third]
        assert index._connection.execute(
            "SELECT predecessor_scan_id,successor_scan_id "
            "FROM inventory_scan_successors ORDER BY predecessor_scan_id"
        ).fetchall() == [(original.scan_id, second), (second, third)]
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prune_retains_payload_referenced_by_invalid_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    database = tmp_path / "inventory.sqlite3"
    policy = _policy()

    with DedupIndex(database) as index:
        with index._connection:
            cursor = index._connection.execute(
                """INSERT INTO scans(
                root,started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
                skipped_links,excluded_directories,errors,status,inventory_policy_signature)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (str(root), 1, 2, 1, 1, 4, 0, 0, 0, "partial", policy.signature),
            )
            assert cursor.lastrowid is not None
            scan_id = int(cursor.lastrowid)
            index._connection.execute(
                """INSERT INTO files(
                scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                VALUES(?,?,?,?,?,?,?)""",
                (scan_id, str(root / "partial.bin"), bytes(16), (1).to_bytes(16, "little"), 4, 1, -1),
            )
            index._connection.execute(
                """INSERT INTO inventory_checkpoints(
                root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
                VALUES(?,?,?,?,?,?,?)""",
                (str(root), scan_id, None, None, None, 0, 3),
            )

        removed = index.prune_obsolete_state(protected_scan_ids=())

        assert removed["files"] == 0
        assert index.file_count(scan_id) == 1
        assert index.inventory_checkpoint(root) is not None


def test_prune_abstains_without_mutating_an_existing_foreign_key_violation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "corpus"
    root.mkdir()

    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        before = index._connection.execute("SELECT COUNT(*) FROM files").fetchone()

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(?,?,?,?,?,?,?)""",
            (999, str(root / "orphan.bin"), bytes(16), (999).to_bytes(16, "little"), 1, 1, -1),
        )
        connection.commit()

    with DedupIndex(database) as index:
        with pytest.raises(InventoryError, match="foreign-key"):
            index.prune_obsolete_state(protected_scan_ids=())
        assert index._connection.execute("SELECT COUNT(*) FROM files").fetchone() == (
            before[0] + 1,
        )
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall()
        assert index._connection.execute(
            "SELECT COUNT(*) FROM scans WHERE scan_id=?", (scan.scan_id,)
        ).fetchone() == (1,)


def test_prune_abstains_on_unresolved_legacy_plan_reference(tmp_path: Path) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "corpus"
    root.mkdir()

    with DedupIndex(database) as index:
        index.scan(root, excluded_paths=())
        with index._connection:
            index._connection.execute(
                """INSERT INTO duplicate_plan_summaries(
                scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns)
                VALUES(999,0,0,0,1)"""
            )
        with pytest.raises(InventoryError, match=r"unresolved.*plan"):
            index.prune_obsolete_state(protected_scan_ids=())
