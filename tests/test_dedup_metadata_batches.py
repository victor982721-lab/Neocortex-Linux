"""Planning metadata stays complete, bounded and local to its live observations."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from neocortex.deduplication import (
    DedupIndex, DedupPlanner, FileSnapshot, InventoryError, KeeperPolicy,
)
from neocortex.deduplication.inventory.repository_plans import PlanningMemberMetadata
from neocortex.deduplication.planning.keeper import keeper_rank
from neocortex.deduplication.planning import pipeline
from neocortex.progress import ProgressEvent, RecordingProgress


def _legacy_reader(index: DedupIndex):
    def read(
        snapshots: Iterable[FileSnapshot],
        *,
        alias_limit: int = 128,
        checkpoint: Callable[[], None] | None = None,
    ) -> Iterator[PlanningMemberMetadata]:
        for snapshot in snapshots:
            if checkpoint is not None:
                checkpoint()
            yield index.planning_member_metadata(snapshot, alias_limit=alias_limit)
    return read


def _metadata_queries(statements: list[str]) -> list[str]:
    return [statement for statement in statements if statement.startswith((
        "WITH requested(",
        "SELECT 0,COUNT(*),MAX(link_count),(",
        "SELECT 0 AS ordinal,path,priority FROM (",
        "SELECT COUNT(*),MAX(link_count) FROM planning_observations",
        "SELECT path FROM planning_observations WHERE volume_id",
        "SELECT computed FROM planning_fingerprints WHERE stage='full'",
    ))]


def _public_runs(
    database: Path, root: Path, *, legacy: bool, exact: bool, policy: KeeperPolicy,
):
    results = []
    reads = []
    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        reader = _legacy_reader(index) if legacy else index.iter_planning_member_metadata
        with patch.object(index, "iter_planning_member_metadata", side_effect=reader):
            for _ in range(2):
                statements: list[str] = []
                progress = RecordingProgress()
                index._connection.set_trace_callback(statements.append)
                plan = DedupPlanner(index, keeper_policy=policy).plan(
                    scan.scan_id, exact_compare=exact, preview_limit=None, progress=progress,
                )
                index._connection.set_trace_callback(None)
                persisted = tuple(index.iter_duplicate_groups(scan.scan_id))
                head = index._connection.execute(
                    "SELECT inventory_content_digest,plan_digest,status FROM duplicate_plan_heads "
                    "WHERE scan_id=?", (scan.scan_id,),
                ).fetchone()
                assert persisted == plan.groups
                assert head is not None and head[2] == "published"
                assert sum(event.finished for event in progress.events) == 1
                assert progress.events[-1].completed == progress.events[-1].total
                results.append((plan, head))
                reads.append(len(_metadata_queries(statements)))
    return results, reads


@pytest.mark.parametrize("exact", [False, True])
def test_public_plan_and_replay_preserve_alias_proofs_digests_and_size_boundaries(
    tmp_path: Path, exact: bool,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(200):
        (root / f"{number:04d}.bin").write_bytes(b"identical bytes")
    for number in range(3):
        (root / f"other-{number}.bin").write_bytes(b"other identical bytes")
    first = root / "0000.bin"
    aliases = root / "aliases"
    aliases.mkdir()
    for number in range(129):
        os.link(first, aliases / f"alias-{number:04d}.bin")
    preferred = root / "preferred" / "z.bin"
    preferred.parent.mkdir()
    os.link(first, preferred)
    os.link(first, tmp_path / "outside-inventory.bin")
    policy = KeeperPolicy(preferred_roots=(str(preferred.parent),))

    expected, legacy_reads = _public_runs(
        tmp_path / "legacy.sqlite3", root, legacy=True, exact=exact, policy=policy,
    )
    actual, batched_reads = _public_runs(
        tmp_path / "batched.sqlite3", root, legacy=False, exact=exact, policy=policy,
    )

    assert actual == expected
    assert legacy_reads == [609, 609]
    assert all(0 < count <= 4 for count in batched_reads)
    for plan, _head in actual:
        assert plan.group_count == 2 and plan.redundant_files == 201
        proof = next(
            proof for group in plan.groups for proof in group.member_proofs
            if proof.alias_count == 131
        )
        assert proof.observed_link_count == 132
        assert len(proof.aliases) == 128 and proof.aliases_truncated
        assert proof.aliases[0] == str(preferred)
        assert proof.aliases[1:] == tuple(sorted(proof.aliases[1:]))
        assert "aliases_outside_inventory" in proof.missing_checks


def test_large_collision_groups_keep_repeated_keeper_proofs_and_public_digest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(1050):
        (root / f"{number:04d}.bin").write_bytes(b"identical bytes")
    policy = KeeperPolicy()
    expected, legacy_reads = _public_runs(
        tmp_path / "legacy.sqlite3", root, legacy=True, exact=False, policy=policy,
    )
    actual, batched_reads = _public_runs(
        tmp_path / "batched.sqlite3", root, legacy=False, exact=False, policy=policy,
    )
    assert actual == expected
    assert legacy_reads == [3153, 3153]
    assert all(0 < count <= 12 for count in batched_reads)
    for plan, _head in actual:
        assert plan.group_count == 2 and plan.redundant_files == 1049
        assert sorted(len(group.redundant) for group in plan.groups) == [25, 1024]
        assert len({group.keep.identity for group in plan.groups}) == 1
        assert plan.groups[0].member_proofs[0] == plan.groups[1].member_proofs[0]


def test_batch_preserves_each_requested_alias_identity_and_live_observation(tmp_path: Path) -> None:
    first = FileSnapshot("/fixture/a", 7, 11, 4, 1, -1)
    alias = replace(first, path="/fixture/z")
    other_volume = replace(first, path="/fixture/other", volume_id=8)
    observations = ((first, 2), (alias, 5), (other_volume, 1))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        index.begin_planning_fingerprints()
        index.store_planning_observations(
            (snapshot, keeper_rank(snapshot, KeeperPolicy()), links)
            for snapshot, links in observations
        )
        index.store_planning_fingerprints(
            "full", ((first, b"digest-a"), (other_volume, b"digest-b")),
            computed_identities=frozenset((first.identity,)),
        )
        requests = (alias, other_volume, first, alias, replace(first, path="/fixture/missing"))
        expected = tuple(index.planning_member_metadata(snapshot) for snapshot in requests)
        assert tuple(index.iter_planning_member_metadata(requests)) == expected
        assert expected[0] == ((alias.path, first.path), 2, 5, True)
        assert expected[1] == ((other_volume.path,), 1, 1, False)
        assert expected[2][0] == (first.path, alias.path)
        assert expected[4][0] == (first.path, alias.path)
        assert tuple(index.iter_planning_member_metadata(requests, alias_limit=1)) == tuple(
            index.planning_member_metadata(snapshot, alias_limit=1) for snapshot in requests
        )

        index.clear_planning_fingerprints()
        index.store_planning_observations(((first, keeper_rank(first, KeeperPolicy()), 1),))
        index.store_planning_fingerprints("full", ((first, b"new-digest"),))
        assert tuple(index.iter_planning_member_metadata((first,))) == (
            ((first.path,), 1, 1, False),
        )


def test_metadata_iterator_consumes_bounded_input_before_its_first_result(tmp_path: Path) -> None:
    snapshot = FileSnapshot("/fixture/a", 7, 11, 4, 1, -1)
    consumed = 0

    def requests() -> Iterator[FileSnapshot]:
        nonlocal consumed
        for _ in range(1_000_000):
            consumed += 1
            yield snapshot

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        index.begin_planning_fingerprints()
        index.store_planning_observations(((snapshot, keeper_rank(snapshot, KeeperPolicy()), 1),))
        index.store_planning_fingerprints("full", ((snapshot, b"digest"),))
        results = index.iter_planning_member_metadata(requests())
        assert next(results) == ((snapshot.path,), 1, 1, False)
        assert 0 < consumed <= 128
        results.close()
        assert consumed <= 128


@pytest.mark.parametrize("alias_limit", [1, 2, 127, 128])
def test_alias_transport_preserves_special_path_characters(tmp_path: Path, alias_limit: int) -> None:
    names = ['plain', 'quote"', "apostrophe'", 'slash\\', 'line\nbreak', 'tab\tname',
             'emoji-🧠', 'é', 'e\u0301', "'); DROP TABLE planning_observations;--"]
    aliases = tuple(FileSnapshot(f"/fixture/{name}", 7, 11, 4, 1, -1) for name in names)
    requests = (aliases[0], aliases[-1], replace(aliases[0], path="/fixture/not-observed"))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        index.begin_planning_fingerprints()
        index.store_planning_observations(
            (snapshot, keeper_rank(snapshot, KeeperPolicy()), len(aliases)) for snapshot in aliases
        )
        index.store_planning_fingerprints("full", ((aliases[0], b"digest"),))
        assert tuple(index.iter_planning_member_metadata(requests, alias_limit=alias_limit)) == tuple(
            index.planning_member_metadata(snapshot, alias_limit=alias_limit) for snapshot in requests
        )
        assert index._connection.execute("SELECT COUNT(*) FROM planning_observations").fetchone() == (
            len(aliases),
        )


@pytest.mark.parametrize("missing", ["planning_observations", "planning_fingerprints"])
def test_incomplete_batch_abstains_without_publishing_and_can_retry(
    tmp_path: Path, missing: str,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(3):
        (root / f"{number}.bin").write_bytes(b"same")
    progress = RecordingProgress()
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        reader = index.iter_planning_member_metadata

        def discard_observation(snapshots, **options):
            member = snapshots[-1]
            index._connection.execute(
                f"DELETE FROM {missing} WHERE volume_id=? AND file_id=?",
                (member.volume_id.to_bytes(16, "little"), member.file_id.to_bytes(16, "little")),
            )
            return reader(snapshots, **options)

        with patch.object(index, "iter_planning_member_metadata", side_effect=discard_observation):
            with pytest.raises(InventoryError, match="lacks complete planning observations"):
                DedupPlanner(index).plan(scan.scan_id, progress=progress, preview_limit=None)
        assert not any(event.finished for event in progress.events)
        assert index._connection.execute(
            "SELECT COUNT(*) FROM duplicate_plan_heads WHERE status='published'",
        ).fetchone() == (0,)
        assert DedupPlanner(index).plan(scan.scan_id).coverage == "complete"


def test_metadata_progress_has_time_cadence_when_completed_does_not_change(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for size in range(1, 261):
        for copy in range(2):
            (root / f"{size:04d}-{copy}.bin").write_bytes(b"x" * size)
    progress = RecordingProgress()
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with patch.object(pipeline, "time", SimpleNamespace(monotonic=lambda: 1.0)):
            plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False, progress=progress)
    assert plan.group_count == 260 and plan.coverage == "complete"
    assert not any(event.description == "Documentando evidencia de duplicados" for event in progress.events)
    assert len(progress.events) <= 40
    assert progress.events[-1].finished
    assert progress.events[-1].completed == progress.events[-1].total == 1040


def test_progress_exception_during_metadata_aborts_without_terminal_success(tmp_path: Path) -> None:
    class StopPlan(RuntimeError):
        pass

    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(200):
        (root / f"{number:04d}.bin").write_bytes(b"same")
    events: list[ProgressEvent] = []
    statements: list[str] = []
    stopped = StopPlan("cancel requested during metadata")
    clock = 1.0

    def trace(statement: str) -> None:
        nonlocal clock
        statements.append(statement)
        if _metadata_queries([statement]):
            clock += 0.11

    def cancel(event: ProgressEvent) -> None:
        events.append(event)
        if _metadata_queries(statements):
            raise stopped

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        index._connection.set_trace_callback(trace)
        with patch.object(pipeline, "time", SimpleNamespace(monotonic=lambda: clock)):
            with pytest.raises(StopPlan) as caught:
                DedupPlanner(index).plan(scan.scan_id, progress=cancel, preview_limit=None)
        index._connection.set_trace_callback(None)
        assert caught.value is stopped
        assert len(_metadata_queries(statements)) == 1
        assert not any(event.finished for event in events)
        assert index._connection.execute(
            "SELECT COUNT(*) FROM duplicate_plan_heads WHERE status='published'",
        ).fetchone() == (0,)
        assert DedupPlanner(index).plan(scan.scan_id).coverage == "complete"
