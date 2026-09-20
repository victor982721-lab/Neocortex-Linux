"""Regression coverage for immutable inventory and plan content identities."""

from __future__ import annotations

import os
from pathlib import Path

from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
    InventoryExclusionPolicy,
    snapshot_path,
)
from neocortex.deduplication.fingerprinting import FULL_ALGORITHM, full_fingerprint


def _policy() -> InventoryExclusionPolicy:
    return InventoryExclusionPolicy.compile(())


def test_same_size_same_mtime_reconciliation_creates_successor_and_hides_old_plan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    policy = _policy()

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        original = index.scan(root, exclusion_policy=policy)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(root), original.scan_id, True, policy.signature)
        )
        plan = DedupPlanner(index).plan(original.scan_id, preview_limit=None)
        assert plan.group_count == 1
        old_second = snapshot_path(second)
        old_digest = index._connection.execute(
            "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
            (original.scan_id,),
        ).fetchone()[0]

        second.write_bytes(b"diff")
        os.utime(second, ns=(old_second.mtime_ns, old_second.mtime_ns))
        rewritten = snapshot_path(second)
        assert rewritten == old_second
        index.apply_reconciliation(
            original.scan_id,
            upserts=(rewritten,),
            checkpoint=InventoryCheckpoint(
                str(root), original.scan_id, True, policy.signature
            ),
        )

        successor = index.current_scan_id(original.scan_id)
        assert successor != original.scan_id
        checkpoint = index.inventory_checkpoint(root)
        assert checkpoint is not None
        assert checkpoint.scan_id == successor
        assert tuple(index.iter_duplicate_groups(original.scan_id)) == ()
        assert index._connection.execute(
            "SELECT successor_scan_id FROM inventory_scan_successors "
            "WHERE predecessor_scan_id=?",
            (original.scan_id,),
        ).fetchone() == (successor,)
        assert index._connection.execute(
            "SELECT status FROM duplicate_plan_heads WHERE scan_id=?",
            (original.scan_id,),
        ).fetchone() == ("superseded",)
        new_digest = index._connection.execute(
            "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
            (successor,),
        ).fetchone()[0]
        # The inventory digest covers the durable file observation.  The
        # successor and superseded plan are the authoritative drift signal
        # when a same-stat rewrite is detected by reconciliation.
        assert bytes(new_digest) == bytes(old_digest)
        assert index._connection.execute(
            "SELECT size FROM files WHERE scan_id=? AND path=?",
            (original.scan_id, str(second)),
        ).fetchone() == (4,)
        assert index._connection.execute(
            "SELECT size FROM files WHERE scan_id=? AND path=?",
            (successor, str(second)),
        ).fetchone() == (4,)


def test_stat_only_fingerprint_cache_is_not_usable_for_disposition_after_rewrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"aaaa")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        recorded = snapshot_path(source)
        digest = full_fingerprint(recorded)
        index.store_fingerprint(recorded, FULL_ALGORITHM, digest)
        source.write_bytes(b"bbbb")
        os.utime(source, ns=(recorded.mtime_ns, recorded.mtime_ns))
        assert snapshot_path(source) == recorded
        # The compatibility cache remains readable by non-disposition routes,
        # while the strict planning seam rejects this stat-only hit.
        assert index.cached_fingerprint(recorded, FULL_ALGORITHM) == digest
        assert index.validated_cached_fingerprint(recorded, FULL_ALGORITHM) is None


def test_duplicate_plan_digest_is_replayed_for_same_immutable_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"content")
    (root / "b").write_bytes(b"content")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        first = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
        first_head = index._connection.execute(
            "SELECT inventory_content_digest,plan_digest,status "
            "FROM duplicate_plan_heads WHERE scan_id=?",
            (scan.scan_id,),
        ).fetchone()
        second = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
        second_head = index._connection.execute(
            "SELECT inventory_content_digest,plan_digest,status "
            "FROM duplicate_plan_heads WHERE scan_id=?",
            (scan.scan_id,),
        ).fetchone()
        assert first.groups[0].keep == second.groups[0].keep
        assert first.groups[0].redundant == second.groups[0].redundant
        assert first_head[:2] == second_head[:2]
        assert first_head[2] == second_head[2] == "published"
