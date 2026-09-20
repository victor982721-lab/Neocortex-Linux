"""Requested policy, content receipts and path topology are separate facts."""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import (
    DedupIndex, DedupPlanner, FileChangedError, InventoryError, KeeperPolicy, snapshot_path,
)
from neocortex.deduplication.domain.evidence import PROOF_VERSION
from neocortex.deduplication.inventory.plan_evidence import decode_member_proof


def _files(root: Path, names: tuple[str, ...], content: bytes = b"duplicate-content") -> list[Path]:
    paths = []
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        paths.append(path)
    return paths


@pytest.mark.parametrize("exact", [False, True])
def test_policy_and_actual_proof_round_trip_without_authority(tmp_path: Path, exact: bool) -> None:
    root = tmp_path / "corpus"
    _files(root, ("Report.pdf", "Report (1).pdf"))
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=exact, preview_limit=20)
        replay = DedupPlanner(index).plan(scan.scan_id, exact_compare=exact, preview_limit=20)
    assert plan.requested_policy == ("exact" if exact else "fast")
    assert plan.coverage == "complete"
    assert plan.statistics.exact_compare_files == int(exact)
    assert replay.statistics.exact_compare_files == int(exact)
    assert replay.statistics.full_hash_files == 2
    assert replay.statistics.cache_validation_reads == 2
    assert replay.statistics.fingerprint_cache_hits == 2
    group = plan.groups[0]
    assert group.proof is not None
    assert group.proof.proof_version == PROOF_VERSION
    assert group.proof.requested_policy == plan.requested_policy
    assert group.proof.actionability == "review_required"
    assert "path_disposability_not_verified" in group.proof.missing_checks
    assert group.nominal_redundant_bytes == plan.nominal_redundant_bytes == len(b"duplicate-content")
    assert group.physical_reclaimable_bytes is plan.physical_reclaimable_bytes is None
    keep, member = group.member_proofs
    assert keep.comparison_result == "reference"
    assert keep.compared_to_identity is None
    assert member.comparison_result == ("equal" if exact else "fingerprint_match")
    assert member.comparison_method == ("byte_for_byte" if exact else "sha256_full")
    assert member.compared_to_identity == group.keep.identity
    assert member.comparison_bytes == (group.size if exact else None)
    assert ("byte_for_byte_comparison" in member.missing_checks) is not exact
    assert all(proof.fingerprint_source == "computed" for proof in replay.groups[0].member_proofs)
    with DedupIndex(database) as index:
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == replay.groups
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT requested_policy,coverage,exact_comparisons,changed_or_unreadable_files "
            "FROM duplicate_plan_summaries"
        ).fetchone()
        assert row == (plan.requested_policy, "complete", int(exact), 0)
        persisted = json.loads(connection.execute(
            "SELECT proof_json FROM planned_duplicate_members WHERE role='redundant'"
        ).fetchone()[0])
    assert persisted["comparison_result"] == member.comparison_result


def test_failed_other_candidate_does_not_downgrade_successful_group_proof(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _files(root, ("a.bin", "b.bin"), b"aaaa")
    _files(root, ("c.bin", "d.bin"), b"bbbbb")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        planner = DedupPlanner(index)
        original = planner._fingerprint

        def fail_one(snapshot):
            if Path(snapshot.path).name == "d.bin":
                raise FileChangedError("fixture candidate changed")
            return original(snapshot)

        with patch.object(planner, "_fingerprint", side_effect=fail_one):
            plan = planner.plan(scan.scan_id, exact_compare=True, preview_limit=20)
    assert plan.coverage == plan.verification_mode == "partial"
    assert plan.requested_policy == "exact"
    assert plan.statistics.changed_or_unreadable_files == 1
    assert plan.groups[0].verification_mode == "full_hash"
    assert plan.groups[0].member_proofs[1].comparison_result == "equal"


@pytest.mark.parametrize(
    ("explicit", "preferred", "referenced", "expected", "reason"),
    [
        (False, False, False, "Report.pdf", "clean_name_tiebreak"),
        (False, False, True, "references/Report (2).pdf", "verified_reference"),
        (False, True, True, "preferred/Report (1).pdf", "preferred_location"),
        (True, True, True, "chosen/Report (3).pdf", "explicit_user_decision"),
    ],
)
def test_keeper_priority_uses_verified_inputs_not_mtime(
    tmp_path: Path, explicit: bool, preferred: bool, referenced: bool, expected: str, reason: str,
) -> None:
    root = tmp_path / "corpus"
    paths = _files(root, (
        "chosen/Report (3).pdf", "preferred/Report (1).pdf", "references/Report (2).pdf", "Report.pdf",
    ))
    for ordinal, path in enumerate(reversed(paths)):
        timestamp = 1_700_000_000_000_000_000 + ordinal * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))
    policy = KeeperPolicy(
        explicit_keep_identities=(snapshot_path(paths[0]).identity,) if explicit else (),
        preferred_roots=(str(root / "preferred"),) if preferred else (),
        verified_reference_identities=(snapshot_path(paths[2]).identity,) if referenced else (),
    )
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        group = DedupPlanner(index, keeper_policy=policy).plan(scan.scan_id, preview_limit=20).groups[0]
    assert group.keep.path == str(root / expected)
    assert group.proof is not None
    assert group.proof.keeper_reason == reason
    assert group.proof.actionability == "review_required"


def test_stable_identity_tiebreak_survives_mtime_inversion(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    paths = _files(root, ("alpha.bin", "omega.bin"))
    selected = []
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        for turn in range(2):
            for ordinal, path in enumerate(paths):
                stamp = 1_700_000_000_000_000_000 + (ordinal ^ turn) * 1_000_000_000
                os.utime(path, ns=(stamp, stamp))
            scan = index.scan(root, excluded_paths=())
            group = DedupPlanner(index).plan(scan.scan_id, preview_limit=20).groups[0]
            selected.append(group.keep.identity)
            assert group.proof is not None and group.proof.keeper_reason == "stable_identity"
    assert selected[0] == selected[1]


def test_aliases_visible_without_counting_same_object_as_redundant(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    a = _files(root, ("alias-a.bin",))[0]
    b = root / "preferred" / "alias-b.bin"
    b.parent.mkdir()
    os.link(a, b)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        assert DedupPlanner(index).plan(scan.scan_id).group_count == 0
        independent = _files(root, ("independent.bin",))[0]
        policy = KeeperPolicy(
            explicit_keep_identities=(snapshot_path(independent).identity,),
            preferred_roots=(str(b.parent),),
        )
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index, keeper_policy=policy).plan(scan.scan_id, preview_limit=20)
        group = plan.groups[0]
        assert group.keep.path == str(independent)
        assert group.redundant[0].path == str(b)
        aliases = group.member_proofs[1]
        assert aliases.alias_count == aliases.observed_link_count == 2
        assert aliases.aliases == (str(b), str(a))
        assert not aliases.aliases_truncated
        assert plan.redundant_files == 1
        assert plan.physical_reclaimable_bytes is None


def test_alias_sample_declares_truncation_and_external_links(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    a, _ = _files(root, ("a.bin", "copy.bin"))
    os.link(a, root / "b.bin")
    os.link(a, root / "c.bin")
    os.link(a, tmp_path / "outside-inventory.bin")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        metadata = index.iter_planning_member_metadata
        with patch.object(
            index, "iter_planning_member_metadata",
            side_effect=lambda s, **options: metadata(s, alias_limit=2, **options),
        ):
            group = DedupPlanner(index).plan(scan.scan_id, preview_limit=20).groups[0]
    alias_proof = next(proof for proof in group.member_proofs if proof.alias_count == 3)
    assert len(alias_proof.aliases) == 2 and alias_proof.aliases_truncated
    assert alias_proof.observed_link_count == 4
    assert "aliases_outside_inventory" in alias_proof.missing_checks


def test_virtual_zip_members_are_not_physical_duplicate_members(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    loose = _files(root, ("loose.txt",))[0]
    with zipfile.ZipFile(root / "container.zip", "w") as archive:
        archive.writestr("inner.txt", loose.read_bytes())
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=20)
        assert {Path(s.path).name for s in index.snapshots(scan.scan_id)} == {"loose.txt", "container.zip"}
    assert plan.group_count == 0 and plan.coverage == "complete"


def test_complete_publication_rejects_missing_member_and_proof(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _files(root, ("a.bin", "b.bin"))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=20)
        index.begin_duplicate_plan(scan.scan_id)
        with pytest.raises(InventoryError, match="cover its members"):
            index.store_duplicate_groups(scan.scan_id, (replace(plan.groups[0], member_proofs=()),))
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == ()
        index.store_duplicate_groups(scan.scan_id, plan.groups)
        index._connection.execute("DELETE FROM planned_duplicate_members WHERE role='redundant'")
        index._connection.commit()
        with pytest.raises(InventoryError, match="incomplete owner evidence"):
            index.complete_duplicate_plan(
                scan.scan_id, group_count=1, redundant_files=1,
                reclaimable_bytes=plan.reclaimable_bytes, verification_mode="full_hash",
                requested_policy="exact", coverage="complete", exact_comparisons=1,
                changed_or_unreadable_files=0,
            )
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == ()


def test_member_receipt_rejects_configured_method_without_comparison(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _files(root, ("a.bin", "b.bin"))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        group = DedupPlanner(index).plan(scan.scan_id, preview_limit=20, exact_compare=False).groups[0]
    payload = group.member_proofs[1].as_dict()
    payload["comparison_result"] = "equal"
    with pytest.raises(InventoryError, match="receipt is inconsistent"):
        decode_member_proof(json.dumps(payload))


def test_member_proof_rejects_impossible_alias_topology(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _files(root, ("a.bin", "b.bin"))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        group = DedupPlanner(index).plan(scan.scan_id, preview_limit=20).groups[0]

    payload = group.member_proofs[1].as_dict()
    payload["alias_count"] = 2
    payload["aliases_truncated"] = True
    payload["observed_link_count"] = 1
    with pytest.raises(InventoryError, match="alias topology is inconsistent"):
        decode_member_proof(json.dumps(payload))


def test_partial_inventory_cannot_replace_complete_duplicate_plan(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _files(root, ("a.bin", "b.bin"))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=20)
        index._connection.execute("UPDATE scans SET status='partial' WHERE scan_id=?", (scan.scan_id,))
        index._connection.commit()
        with pytest.raises(InventoryError, match="complete inventory scan"):
            DedupPlanner(index).plan(scan.scan_id)
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == plan.groups


def test_publish_rechecks_roles_not_just_global_counts(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _files(root, ("a.bin", "b.bin"))
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=20)
        index._connection.execute("DELETE FROM duplicate_plan_summaries")
        index._connection.execute("UPDATE planned_duplicate_members SET role='keep'")
        index._connection.commit()
        with pytest.raises(InventoryError, match="inconsistent group membership"):
            index.complete_duplicate_plan(
                scan.scan_id, group_count=1, redundant_files=1, reclaimable_bytes=plan.reclaimable_bytes,
                verification_mode="full_hash", requested_policy="exact", coverage="complete",
                exact_comparisons=1, changed_or_unreadable_files=0,
            )
        assert tuple(index.iter_duplicate_groups(scan.scan_id)) == ()
