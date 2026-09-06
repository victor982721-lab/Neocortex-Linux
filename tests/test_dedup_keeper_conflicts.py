"""Keeper decisions and reference receipts must remain valid at publication."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner, KeeperPolicy, snapshot_path
from neocortex.deduplication.domain.evidence import KeeperConflictError


def _files(root: Path, values: dict[str, bytes]) -> dict[str, Path]:
    root.mkdir()
    paths = {name: root / name for name in values}
    for name, path in paths.items():
        path.write_bytes(values[name])
    return paths


@pytest.mark.parametrize("exact", [False, True])
def test_multiple_explicit_keepers_abstain_with_preserved_evidence(tmp_path: Path, exact: bool) -> None:
    root = tmp_path / "corpus"
    paths = _files(root, {"safe-a": b"x", "safe-b": b"x", "chosen-a": b"same", "chosen-b": b"same"})
    selected = tuple(snapshot_path(paths[name]).identity for name in ("chosen-a", "chosen-b"))
    policy = KeeperPolicy(explicit_keep_identities=selected)
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with (
            patch("neocortex.deduplication.planning.pipeline.PLAN_GROUP_BATCH_SIZE", 1),
            patch("neocortex.deduplication.planning.pipeline.MAX_REDUNDANT_MEMBERS_PER_GROUP", 1),
            pytest.raises(KeeperConflictError, match="conflicting_explicit_keepers") as caught,
        ):
            DedupPlanner(index, keeper_policy=policy).plan(scan.scan_id, exact_compare=exact)
        error = caught.value
        assert error.code == "conflicting_explicit_keepers"
        assert error.policy is policy
        assert error.identities == tuple(sorted(selected))
        assert len(error.full_fingerprint) == 32
        assert error.proof.comparison_method == ("byte_for_byte" if exact else "full_xxh3")
        assert error.proof.comparison_result == ("equal" if exact else "fingerprint_match")
        assert "keeper_decision_conflict" in error.proof.missing_checks
        assert policy.explicit_keep_identities == selected
        assert index._connection.execute("SELECT COUNT(*) FROM duplicate_plan_summaries").fetchone() == (0,)
        # Completed earlier groups and hash facts are retained, but a partial
        # owner has no publication and cannot masquerade as a complete plan.
        assert index._connection.execute("SELECT COUNT(*) FROM planned_duplicate_groups").fetchone() == (1,)
        assert index._connection.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] >= 4
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == ()
    assert {name: path.read_bytes() for name, path in paths.items()} == {
        "safe-a": b"x", "safe-b": b"x", "chosen-a": b"same", "chosen-b": b"same",
    }


def test_exact_collision_partitions_do_not_confuse_distinct_explicit_classes(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    paths = _files(root, {"a": b"aaaa", "a-copy": b"aaaa", "b": b"bbbb", "b-copy": b"bbbb"})
    policy = KeeperPolicy(explicit_keep_identities=tuple(snapshot_path(paths[name]).identity for name in ("a", "b")))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        planner = DedupPlanner(index, keeper_policy=policy)
        with patch.object(planner, "_fingerprint", return_value=(bytes(16), True)):
            plan = planner.plan(scan.scan_id, exact_compare=True, preview_limit=20)
        assert plan.coverage == "complete" and plan.group_count == 2
        assert {group.keep.identity for group in plan.groups} == set(policy.explicit_keep_identities)
        with patch.object(planner, "_fingerprint", return_value=(bytes(16), True)), pytest.raises(KeeperConflictError) as caught:
            planner.plan(scan.scan_id, exact_compare=False)
        assert caught.value.proof.comparison_result == "fingerprint_match"
        assert "byte_for_byte_comparison" in caught.value.proof.missing_checks
        assert index._connection.execute("SELECT COUNT(*) FROM duplicate_plan_summaries").fetchone() == (0,)


def test_repeated_selection_of_one_hardlinked_object_is_not_conflict(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    paths = _files(root, {"keeper": b"same", "copy": b"same"})
    alias = root / "keeper-alias"
    alias.hardlink_to(paths["keeper"])
    identity = snapshot_path(paths["keeper"]).identity
    policy = KeeperPolicy(explicit_keep_identities=(identity, snapshot_path(alias).identity))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index, keeper_policy=policy).plan(scan.scan_id, preview_limit=20)
    assert plan.coverage == "complete" and plan.group_count == 1
    assert plan.groups[0].keep.identity == identity


@pytest.mark.parametrize("groups", [0, 1])
def test_keeper_validation_runs_immediately_before_each_publication(tmp_path: Path, groups: int) -> None:
    root = tmp_path / "corpus"
    _files(root, {"a": b"same", **({"b": b"same"} if groups else {})})
    events: list[str] = []
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        original = index.complete_duplicate_plan

        def validate():
            assert index._connection.execute("SELECT COUNT(*) FROM duplicate_plan_summaries").fetchone() == (0,)
            assert index._connection.execute("SELECT COUNT(*) FROM planned_duplicate_groups").fetchone() == (groups,)
            events.append("validate")

        def publish(*args, **kwargs):
            assert events[-1] == "validate"
            events.append("publish")
            return original(*args, **kwargs)

        planner = DedupPlanner(index, keeper_validation=validate)
        with patch.object(index, "complete_duplicate_plan", side_effect=publish):
            assert planner.plan(scan.scan_id).coverage == "complete"
            assert planner.plan(scan.scan_id).coverage == "complete"
    assert events == ["validate", "publish", "validate", "publish"]


@pytest.mark.parametrize("groups", [0, 1])
def test_failed_final_keeper_validation_leaves_no_complete_plan(tmp_path: Path, groups: int) -> None:
    root = tmp_path / "corpus"
    _files(root, {"a": b"same", **({"b": b"same"} if groups else {})})
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())

        def fail():
            raise RuntimeError("fixture_reference_changed")

        with pytest.raises(RuntimeError, match="fixture_reference_changed"):
            DedupPlanner(index, keeper_validation=fail).plan(scan.scan_id)
        assert index._connection.execute("SELECT COUNT(*) FROM duplicate_plan_summaries").fetchone() == (0,)
        assert index._connection.execute("SELECT COUNT(*) FROM planned_duplicate_groups").fetchone() == (groups,)
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == ()
        if groups:
            proof = json.loads(index._connection.execute("SELECT proof_json FROM planned_duplicate_groups").fetchone()[0])
            assert proof["comparison_result"] == "equal"


@pytest.mark.parametrize("explicit_other", [False, True])
def test_only_selected_referenced_keeper_persists_receipt_ids(tmp_path: Path, explicit_other: bool) -> None:
    root = tmp_path / "corpus"
    paths = _files(root, {"original": b"same", "reference-copy": b"same"})
    original = snapshot_path(paths["original"]).identity
    referenced = snapshot_path(paths["reference-copy"]).identity
    policy = KeeperPolicy(
        explicit_keep_identities=(original,) if explicit_other else (),
        verified_reference_identities=(referenced,),
        verified_reference_evidence=((referenced, ("catalog:7:11", "review:task:2")),),
    )
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        group = DedupPlanner(index, keeper_policy=policy).plan(scan.scan_id, preview_limit=20).groups[0]
        stored = json.loads(index._connection.execute("SELECT proof_json FROM planned_duplicate_groups").fetchone()[0])
    assert group.proof is not None
    factors = group.proof.keeper_factors
    assert tuple(stored["keeper_factors"]) == factors
    if explicit_other:
        assert group.keep.identity == original
        assert not any(factor.startswith("verified_reference_evidence:") for factor in factors)
    else:
        assert group.keep.identity == referenced
        assert "verified_reference_evidence:catalog:7:11" in factors
        assert "verified_reference_evidence:review:task:2" in factors


@pytest.mark.parametrize("invalid", [
    [],
    (((1, 2), ["ref:1"]),),
    (([1, 2], ("ref:1",)),),
    (((1, 3), ("ref:1",)),),
    (((True, 2), ("ref:1",)),),
    (((1, 2), ()),),
    (((1, 2), ("",)),),
    (((1, 2), ("\n",)),),
    (((1, 2), ("ref:\x00",)),),
    (((1, 2), ("ref:\u202e",)),),
    (((1, 2), ("ref:\ud800",)),),
    (((1, 2), ("x" * 513,)),),
    (((1, 2), (42,)),),
])
def test_reference_evidence_rejects_mutability_unverified_identity_and_unsafe_ids(invalid) -> None:
    with pytest.raises((TypeError, ValueError)):
        KeeperPolicy(verified_reference_identities=((1, 2),), verified_reference_evidence=invalid)


def test_reference_evidence_accepts_boundary_without_changing_input() -> None:
    supplied = (((1, 2), ("x" * 512, "catálogo:registro")),)
    policy = KeeperPolicy(verified_reference_identities=((1, 2),), verified_reference_evidence=supplied)
    assert policy.verified_reference_evidence is supplied
