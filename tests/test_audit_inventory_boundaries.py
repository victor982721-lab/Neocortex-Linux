"""Adversarial inventory ownership and retention regressions."""

from __future__ import annotations

import os
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.deduplication import (
    FULL_ALGORITHM,
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
    InventoryError,
    InventoryExclusionPolicy,
    full_fingerprint,
    snapshot_path,
)
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.documents.document_catalog import update_document_catalog_source


def _policy() -> InventoryExclusionPolicy:
    return InventoryExclusionPolicy.compile(())


def _checkpoint(root: Path, scan_id: int, policy: InventoryExclusionPolicy) -> InventoryCheckpoint:
    return InventoryCheckpoint(str(root), scan_id, True, policy.signature)


def test_duplicate_publication_rejects_member_not_observed_by_scan(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "keep.bin").write_bytes(b"same-content")
    (root / "redundant.bin").write_bytes(b"same-content")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"same-content")
    outside_snapshot = snapshot_path(outside)

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
        index.begin_duplicate_plan(scan.scan_id)
        index.store_duplicate_groups(scan.scan_id, plan.groups)
        index._connection.execute(
            """UPDATE planned_duplicate_members
            SET path=?,volume_id=?,file_id=?,size=?,mtime_ns=?,birthtime_ns=?
            WHERE role='redundant'""",
            (
                outside_snapshot.path,
                outside_snapshot.volume_id.to_bytes(16, "little"),
                outside_snapshot.file_id.to_bytes(16, "little"),
                outside_snapshot.size,
                outside_snapshot.mtime_ns,
                outside_snapshot.birthtime_ns,
            ),
        )
        index._connection.commit()

        with pytest.raises(InventoryError, match="inconsistent group membership"):
            index.complete_duplicate_plan(
                scan.scan_id,
                group_count=1,
                redundant_files=1,
                reclaimable_bytes=plan.reclaimable_bytes,
                verification_mode="full_hash",
                requested_policy="exact",
                coverage="complete",
                exact_comparisons=1,
                changed_or_unreadable_files=0,
            )


def test_retention_removes_orphaned_content_evidence_with_fingerprint_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"aaaa")
    policy = _policy()

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        first = index.scan(root, exclusion_policy=policy)
        recorded = snapshot_path(source)
        index.store_fingerprint(recorded, FULL_ALGORITHM, full_fingerprint(recorded))
        index.bind_inventory_checkpoint(_checkpoint(root, first.scan_id, policy))

        for ordinal, payload in enumerate((b"bbbb", b"cccc", b"dddd"), start=1):
            source.write_bytes(payload)
            timestamp = 1_700_000_000_000_000_000 + ordinal * 1_000_000_000
            os.utime(source, ns=(timestamp, timestamp))
            current = index.scan(root, exclusion_policy=policy)
            index.bind_inventory_checkpoint(_checkpoint(root, current.scan_id, policy))

        assert index._connection.execute(
            "SELECT COUNT(*) FROM fingerprint_content_evidence"
        ).fetchone() == (1,)
        index.prune_obsolete_state(protected_scan_ids=())
        assert index._connection.execute(
            "SELECT COUNT(*) FROM fingerprint_content_evidence"
        ).fetchone() == (0,)


def test_catalog_scope_rejects_symlink_anchor_even_without_path_revalidation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside fixture")
    alias = root / "alias.docx"
    alias.symlink_to(outside)
    source = tmp_path / "docx.sqlite3"
    initialize_docx_state(source)
    outside_snapshot = snapshot_path(outside)
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,'fixture','complete','valid',?,14,'text',1,1,'fixture','')""",
            (
                f"{outside_snapshot.volume_id}:{outside_snapshot.file_id}",
                str(alias),
                outside_snapshot.size,
                outside_snapshot.mtime_ns,
                outside_snapshot.birthtime_ns,
                zlib.compress(b"outside fixture"),
            ),
        )

    result = update_document_catalog_source(
        tmp_path / "catalog.sqlite3",
        source,
        "docx",
        source_root=root,
        verify_source_paths=False,
    )
    assert result.candidates == 0
    assert result.classified == 0
    assert result.publication_state == "published"
