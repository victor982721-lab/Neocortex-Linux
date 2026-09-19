"""Proposed regressions for exact comparison and bounded inventory planning."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    FileSnapshot,
    InventoryError,
    KeeperPolicy,
    files_equal_exact,
    snapshot_path,
)
from neocortex.deduplication import fingerprinting
from neocortex.deduplication.inventory import repository_scans
from neocortex.deduplication.planning import pipeline
from neocortex.deduplication.planning.keeper import keeper_rank
from neocortex.progress import RecordingProgress


class _CappedReadStream:
    def __init__(self, stream, limits: tuple[int, ...]) -> None:
        self._stream = stream
        self._limits = limits
        self._reads = 0

    def __getattr__(self, name: str):
        return getattr(self._stream, name)

    def readinto(self, buffer: bytearray) -> int:
        limit = self._limits[self._reads % len(self._limits)]
        self._reads += 1
        return self._stream.readinto(memoryview(buffer)[:limit])


@pytest.mark.parametrize("limits", [(512,), (65536, 7, 32768, 1), (65536,)])
@pytest.mark.parametrize("difference", [None, 0, -1])
def test_exact_comparison_limits_work_to_new_bytes_and_preserves_tails(
    tmp_path: Path, limits: tuple[int, ...], difference: int | None,
) -> None:
    payload = b"a" * (2 * 65536 + 513)
    peer_payload = bytearray(payload)
    if difference is not None:
        peer_payload[difference] = ord("b")
    left, right = tmp_path / "left.bin", tmp_path / "right.bin"
    left.write_bytes(payload)
    right.write_bytes(peer_payload)
    snapshots = snapshot_path(left), snapshot_path(right)
    compared_capacities: list[int] = []
    read_bytes: list[int] = []
    checkpoints: list[None] = []

    class ObservedBuffer(bytearray):
        def __ne__(self, other):
            compared_capacities.append(len(self))
            return super().__ne__(other)

    real_fdopen = os.fdopen

    def fdopen(descriptor, mode="r", buffering=-1):
        return _CappedReadStream(real_fdopen(descriptor, mode, buffering), limits)

    with (
        patch.object(fingerprinting, "bytearray", ObservedBuffer, create=True),
        patch.object(os, "fdopen", fdopen),
    ):
        equal = files_equal_exact(
            *snapshots,
            chunk_size=65536,
            read_observer=read_bytes.append,
            checkpoint=lambda: checkpoints.append(None),
        )

    assert equal is (difference is None)
    if equal:
        assert sum(read_bytes) == 2 * len(payload)
    assert len(checkpoints) == len(read_bytes) + 1
    # Count logical comparison operands, not elapsed time or physical RAM I/O.
    # An unconditional whole-buffer implementation rescans stale short-read
    # tails and exceeds this work bound while still returning the right result.
    assert sum(compared_capacities) <= sum(read_bytes) // 2


def _partial_scan(index: DedupIndex, root: Path, count: int) -> int:
    with index._connection:
        cursor = index._connection.execute(
            """INSERT INTO scans(
            root,started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors,status,inventory_policy_signature)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (str(root), 1, 2, count, 1, count, 0, 0, 0, "partial", "fixture"),
        )
        assert cursor.lastrowid is not None
        scan_id = int(cursor.lastrowid)
        index._connection.executemany(
            "INSERT INTO files(scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                (scan_id, str(root / f"{position}.bin"), bytes(16),
                 position.to_bytes(16, "little"), 1, 1, -1)
                for position in range(count)
            ),
        )
    return scan_id


def test_prune_deletes_all_disposable_batches_and_preserves_explicit_holds(
    tmp_path: Path,
) -> None:
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        disposable = _partial_scan(index, tmp_path / "old", 21)
        held = _partial_scan(index, tmp_path / "held", 21)
        before = index._connection.execute(
            "SELECT * FROM files WHERE scan_id=? ORDER BY path", (held,),
        ).fetchall()
        statements: list[str] = []
        index._connection.set_trace_callback(statements.append)
        try:
            with patch.object(repository_scans, "PRUNE_BATCH_SIZE", 7):
                removed = index.prune_obsolete_state(protected_scan_ids=(held,))
        finally:
            index._connection.set_trace_callback(None)

        assert removed["files"] == 21
        assert index.file_count(disposable) == 0
        assert index._connection.execute(
            "SELECT * FROM files WHERE scan_id=? ORDER BY path", (held,),
        ).fetchall() == before
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone() == (2,)
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        selections = [statement for statement in statements
                      if statement.startswith("SELECT scan_id,path FROM files")]
        assert len(selections) == 4  # Three bounded deletes and the final empty selection.
        assert all(statement.endswith("LIMIT 7") for statement in selections)


@pytest.mark.parametrize("partial_threshold", [0, 1_000_000])
@pytest.mark.parametrize("remove_alias", [False, True])
@pytest.mark.parametrize("provider_method", ["_fingerprint", "_fingerprint_batch"])
def test_one_observed_physical_identity_never_enters_hashing(
    tmp_path: Path, partial_threshold: int, remove_alias: bool, provider_method: str,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"same physical object")
    aliases = (root / "alias-a.bin", root / "alias-b.bin")
    for alias in aliases:
        os.link(source, alias)
    progress = RecordingProgress()
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        if remove_alias:
            aliases[-1].unlink()
        planner = DedupPlanner(index, partial_threshold=partial_threshold)
        with patch.object(
            planner, provider_method, side_effect=AssertionError("singleton identity was hashed"),
        ):
            plan = planner.plan(scan.scan_id, progress=progress)

    assert plan.group_count == plan.redundant_files == 0
    assert plan.statistics.size_candidate_files == 0
    assert plan.statistics.full_hash_files == plan.statistics.partial_hash_files == 0
    assert plan.statistics.hash_read_bytes == plan.statistics.cache_validation_bytes == 0
    assert plan.statistics.changed_or_unreadable_files == int(remove_alias)
    assert plan.coverage == ("partial" if remove_alias else "complete")
    assert progress.events[-1].finished
    assert progress.events[-1].completed == progress.events[-1].total == 3


@pytest.mark.parametrize("other_identity", [(7, 0), (7, 256), (7, 65536), (256, 1)])
def test_second_identity_probe_preserves_blob_order_and_resets_between_sizes(
    tmp_path: Path, other_identity: tuple[int, int],
) -> None:
    first = FileSnapshot("/fixture/first", 7, 1, 8, 1, -1)
    alias = replace(first, path="/fixture/alias")
    other = replace(first, path="/fixture/other", volume_id=other_identity[0], file_id=other_identity[1])
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        index.begin_planning_fingerprints()
        assert not index.planning_has_multiple_identities()
        index.store_planning_observations(
            (snapshot, keeper_rank(snapshot, KeeperPolicy()), 2)
            for snapshot in (first, alias)
        )
        assert not index.planning_has_multiple_identities()
        index.store_planning_observations(((other, keeper_rank(other, KeeperPolicy()), 1),))
        assert index.planning_has_multiple_identities()
        index.clear_planning_fingerprints()
        assert not index.planning_has_multiple_identities()


def _duplicate_fixture(root: Path) -> None:
    root.mkdir()
    for size in (7, 11, 15):
        for member in range(5):
            source = root / f"{size:02d}-{member}.bin"
            source.write_bytes(b"x" * size)
            for alias in range(2):
                os.link(source, root / f"{size:02d}-{member}-alias-{alias}.bin")


def _run_batched_plan(database: Path, root: Path, members: int, aliases: int):
    batches: list[tuple[int, int, int]] = []
    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        store = index.store_duplicate_groups

        def observe(scan_id, groups):
            groups = tuple(groups)
            batches.append((
                len(groups),
                sum(1 + len(group.redundant) for group in groups),
                sum(len(proof.aliases) for group in groups for proof in group.member_proofs),
            ))
            store(scan_id, groups)

        with (
            patch.object(pipeline, "PLAN_MEMBER_BATCH_SIZE", members),
            patch.object(pipeline, "PLAN_ALIAS_BATCH_SIZE", aliases),
            patch.object(index, "store_duplicate_groups", side_effect=observe),
        ):
            plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False, preview_limit=None)
        persisted = tuple(index.iter_duplicate_groups(scan.scan_id))
        head = index._connection.execute(
            "SELECT inventory_content_digest,plan_digest,status FROM duplicate_plan_heads "
            "WHERE scan_id=?", (scan.scan_id,),
        ).fetchone()
        assert persisted == plan.groups
        assert head is not None and head[2] == "published"
    return plan, head, batches


@pytest.mark.parametrize("members,aliases", [(6, 10_000), (10_000, 10)])
def test_member_and_alias_budgets_preserve_complete_groups_and_public_digest(
    tmp_path: Path, members: int, aliases: int,
) -> None:
    root = tmp_path / "corpus"
    _duplicate_fixture(root)
    expected_plan, expected_head, expected_batches = _run_batched_plan(
        tmp_path / "wide.sqlite3", root, 10_000, 10_000,
    )
    plan, head, batches = _run_batched_plan(
        tmp_path / "bounded.sqlite3", root, members, aliases,
    )
    assert plan == expected_plan
    assert head == expected_head
    assert expected_batches == [(3, 15, 45)]
    assert batches == [(1, 5, 15)] * 3
    assert plan.group_count == 3 and plan.redundant_files == 12
    assert all(proof.alias_count == len(proof.aliases) == 3
               for group in plan.groups for proof in group.member_proofs)


def test_failure_after_an_early_batch_does_not_publish_partial_evidence(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _duplicate_fixture(root)
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        read = index.iter_planning_member_metadata

        def fail_second_group(snapshots, **options):
            snapshots = tuple(snapshots)
            if snapshots[0].size == 11:
                raise InventoryError("fixture evidence became unavailable")
            return read(snapshots, **options)

        with (
            patch.object(pipeline, "PLAN_MEMBER_BATCH_SIZE", 6),
            patch.object(index, "iter_planning_member_metadata", side_effect=fail_second_group),
            pytest.raises(InventoryError, match="fixture evidence"),
        ):
            DedupPlanner(index).plan(scan.scan_id, exact_compare=False)
        assert index._connection.execute("SELECT COUNT(*) FROM planned_duplicate_groups").fetchone() == (1,)
        assert index._connection.execute("SELECT COUNT(*) FROM duplicate_plan_summaries").fetchone() == (0,)
        assert index._connection.execute(
            "SELECT COUNT(*) FROM duplicate_plan_heads WHERE status='published'",
        ).fetchone() == (0,)
        assert DedupPlanner(index).plan(scan.scan_id, exact_compare=False).coverage == "complete"
