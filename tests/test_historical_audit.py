"""Bounded historical-audit and explicit-adoption regressions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from neocortex.runtime.historical_audit import (
    HISTORICAL_AUDIT_SCHEMA,
    HistoricalAuditManager,
    HistoricalRootError,
)
import neocortex.runtime.historical_audit as historical_audit


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(path: Path) -> list[int]:
    metadata = path.lstat()
    return [metadata.st_dev, metadata.st_ino, getattr(metadata, "st_birthtime_ns", -1)]


def _manifest(
    root: Path,
    entry: Path,
    *,
    approved: bool = True,
    adoption_id: str = "adopt-fixture-1",
    activity_uncertain: bool = False,
    schema: str = "neocortex.scratch/v1",
    state: str = "completed",
) -> dict[str, object]:
    adoption: dict[str, object] = {
        "approved": approved,
        "adoption_id": adoption_id,
    }
    payload: dict[str, object] = {
        "schema": schema,
        "owner": "neocortex-framework",
        "state": state,
        "disposable": True,
        "path": str(entry),
        "root": str(root),
        "path_identity": _identity(entry),
        "root_identity": _identity(root),
        "activity_uncertain": activity_uncertain,
        "historical_adoption": adoption,
    }
    digest = "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    payload["manifest_digest"] = digest
    adoption["digest"] = digest
    return payload


def _write_manifest(entry: Path, payload: dict[str, object], name: str = "manifest.json") -> Path:
    path = entry / name
    path.write_text(_canonical(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "historical-root"
    root.mkdir(mode=0o700)
    return root


def test_explicit_root_is_required_and_missing_root_is_not_created(tmp_path: Path) -> None:
    with pytest.raises(HistoricalRootError):
        HistoricalAuditManager(Path("relative-root"))

    root = tmp_path / "not-created"
    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    assert plan.root_blocked == "historical root is absent"
    assert plan.scanned == plan.adoptable == plan.planned == 0
    assert not root.exists()


def test_root_scan_failure_is_blocked_not_an_empty_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    original_scandir = historical_audit.os.scandir

    def failing_scandir(path):
        if Path(path) == root:
            raise OSError("synthetic scan failure")
        return original_scandir(path)

    monkeypatch.setattr(historical_audit.os, "scandir", failing_scandir)
    manager = HistoricalAuditManager(root)

    plan = manager.plan()
    applied = manager.apply(plan)

    assert plan.status == "blocked"
    assert plan.root_blocked == "historical root scan failed: synthetic scan failure"
    assert applied.status == "blocked"
    assert applied.root_blocked == plan.root_blocked


def test_only_prefixed_direct_children_are_scanned_and_neighbors_are_unmanaged(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    managed = root / "neocortex-managed"
    managed.mkdir(mode=0o700)
    (managed / "payload").write_bytes(b"managed")
    os.chmod(managed / "payload", 0o600)
    neighbor = root / "neocortex_neighbor"
    neighbor.mkdir(mode=0o700)
    marker = neighbor / "must-remain"
    marker.write_bytes(b"outside-scope")
    os.chmod(marker, 0o600)
    ordinary = root / "ordinary\nñ"
    ordinary.mkdir(mode=0o700)
    (ordinary / "also-remains").write_bytes(b"ordinary")

    plan = HistoricalAuditManager(root).plan()

    assert tuple(record.path for record in plan.records) == (managed,)
    assert plan.records[0].status == "unknown"
    assert plan.unmanaged == (neighbor, ordinary)
    assert marker.read_bytes() == b"outside-scope"
    assert (ordinary / "also-remains").exists()


def test_invalid_and_other_application_manifests_are_blocked(tmp_path: Path) -> None:
    root = _root(tmp_path)
    invalid = root / "neocortex-invalid"
    invalid.mkdir(mode=0o700)
    (invalid / "manifest.json").write_text("{not-json", encoding="utf-8")
    os.chmod(invalid / "manifest.json", 0o600)

    other = root / "neocortex-other"
    other.mkdir(mode=0o700)
    payload = _manifest(root, other, schema="other-app/v9")
    _write_manifest(other, payload)

    plan = HistoricalAuditManager(root).plan()

    assert plan.blocked + plan.unknown == 2
    assert plan.adoptable == 0
    assert plan.records[0].status == "unknown"
    assert plan.records[1].status == "blocked"
    assert all(record.proposed_bytes == 0 for record in plan.records)


def test_valid_but_non_adopted_manifest_is_kept(tmp_path: Path) -> None:
    root = _root(tmp_path)
    entry = root / "neocortex-kept"
    entry.mkdir(mode=0o700)
    data = entry / "resultado [ñ]\n.txt"
    data.write_bytes("resultado".encode("utf-8"))
    os.chmod(data, 0o600)
    _write_manifest(entry, _manifest(root, entry, approved=False))

    plan = HistoricalAuditManager(root).plan()

    assert plan.kept == 1
    assert plan.adoptable == plan.planned == 0
    assert plan.records[0].status == "kept"
    assert entry.exists()
    assert HistoricalAuditManager(root).apply(plan).blocked == 1
    assert entry.exists()


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [("active", "active"), ("failed-retained", "recovery_required")],
)
def test_lifecycle_states_are_not_treated_as_unknown_or_adoptable(
    tmp_path: Path,
    state: str,
    expected_status: str,
) -> None:
    root = _root(tmp_path)
    entry = root / f"neocortex-{state}"
    entry.mkdir(mode=0o700)
    payload = entry / "payload"
    payload.write_bytes(b"keep")
    os.chmod(payload, 0o600)
    _write_manifest(entry, _manifest(root, entry, state=state))

    plan = HistoricalAuditManager(root).plan()

    assert plan.records[0].status == expected_status
    assert plan.adoptable == 0
    assert entry.exists()


def test_adoptable_fixture_apply_is_descriptor_relative_and_replay_is_noop(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    entry = root / "neocortex-adoptable"
    entry.mkdir(mode=0o700)
    data = entry / "payload [日本語].bin"
    data.write_bytes(b"payload")
    os.chmod(data, 0o600)
    _write_manifest(entry, _manifest(root, entry))

    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    assert plan.to_dict()["schema"] == HISTORICAL_AUDIT_SCHEMA
    assert plan.status == "planned"
    assert plan.adoptable == plan.planned == 1
    assert plan.proposed_bytes > 0
    assert plan.records[0].adoption_id == "adopt-fixture-1"

    applied = manager.apply(plan)
    assert applied.status == "applied"
    assert applied.planned == applied.applied == 1
    assert applied.applied_bytes == plan.proposed_bytes
    assert not entry.exists()

    receipts = root / ".neocortex-historical-audit"
    receipt_files = tuple(receipts.glob("*.json"))
    assert len(receipt_files) == 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["schema"] == "neocortex.historical-audit-receipt/v1"
    assert receipt["state"] == "applied"
    assert receipt["postcondition"] == "entry_absent"
    assert receipt["path"] == str(entry)
    assert plan.records[0].path_identity is not None
    assert receipt["path_identity"] == list(plan.records[0].path_identity)

    replay = manager.apply(plan)
    assert replay.planned == replay.applied == 0
    assert replay.scanned == 0
    assert replay.recovery_required == replay.failed == 0


def test_symlink_and_hardlink_entries_are_never_candidates(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do-not-delete")
    os.chmod(outside, 0o600)
    symlink = root / "neocortex-link"
    os.symlink(outside, symlink)
    hardlink = root / "neocortex-hardlink"
    os.link(outside, hardlink)

    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    applied = manager.apply(plan)

    assert plan.blocked == 2
    assert plan.adoptable == 0
    assert applied.applied == 0
    assert symlink.is_symlink()
    assert hardlink.exists()
    assert outside.read_bytes() == b"do-not-delete"


def test_identity_drift_between_plan_and_apply_is_not_deleted(tmp_path: Path) -> None:
    root = _root(tmp_path)
    entry = root / "neocortex-drift"
    entry.mkdir(mode=0o700)
    (entry / "payload").write_bytes(b"original")
    os.chmod(entry / "payload", 0o600)
    _write_manifest(entry, _manifest(root, entry))
    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    assert plan.adoptable == 1

    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    (replacement / "keep").write_bytes(b"replacement")
    os.chmod(replacement / "keep", 0o600)
    entry.rename(tmp_path / "old-entry")
    replacement.rename(entry)

    applied = manager.apply(plan)

    assert applied.applied == 0
    assert entry.exists()
    assert (entry / "keep").read_bytes() == b"replacement"
    assert (tmp_path / "old-entry").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_depth": 0},
        {"max_bytes": 1},
        {"max_entries": 1},
    ],
)
def test_bounds_are_visible_and_fail_closed(tmp_path: Path, kwargs: dict[str, int]) -> None:
    root = _root(tmp_path)
    entry = root / "neocortex-bounded"
    entry.mkdir(mode=0o700)
    (entry / "payload").write_bytes(b"payload")
    os.chmod(entry / "payload", 0o600)
    _write_manifest(entry, _manifest(root, entry))

    manager_kwargs: dict[str, object] = dict(kwargs)
    manager_type: Any = HistoricalAuditManager
    plan = manager_type(root, **manager_kwargs).plan()

    assert plan.truncated is True
    assert plan.adoptable == plan.planned == 0
    assert plan.blocked >= 1 or plan.unknown >= 1
    assert entry.exists()
