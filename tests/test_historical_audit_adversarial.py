"""Adversarial boundaries for the explicit historical-audit phase.

These tests use only private temporary fixtures (apart from the read-only
``/tmp`` contract check) and deliberately exercise the claims that must fail
closed before historical retirement.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import neocortex.runtime.historical_audit as historical_audit
from neocortex.runtime.historical_audit import HistoricalAuditManager


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _identity(path: Path) -> list[int]:
    metadata = path.lstat()
    return [metadata.st_dev, metadata.st_ino, getattr(metadata, "st_birthtime_ns", -1)]


def _manifest(root: Path, entry: Path, *, approved: bool = True) -> dict[str, object]:
    adoption: dict[str, object] = {
        "approved": approved,
        "adoption_id": "adversarial-adoption-1",
    }
    payload: dict[str, object] = {
        "schema": "neocortex.scratch/v1",
        "owner": "neocortex-framework",
        "state": "completed",
        "disposable": True,
        "path": str(entry),
        "root": str(root),
        "path_identity": _identity(entry),
        "root_identity": _identity(root),
        "activity_uncertain": False,
        "historical_adoption": adoption,
    }
    digest = "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    payload["manifest_digest"] = digest
    adoption["digest"] = digest
    return payload


def _write_manifest(entry: Path, payload: dict[str, object]) -> Path:
    path = entry / "manifest.json"
    path.write_text(_canonical(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "historical-root"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def _adoptable_entry(root: Path, name: str = "neocortex-adversarial") -> Path:
    entry = root / name
    entry.mkdir(mode=0o700)
    os.chmod(entry, 0o700)
    payload = entry / "payload.bin"
    payload.write_bytes(b"payload")
    os.chmod(payload, 0o600)
    _write_manifest(entry, _manifest(root, entry))
    return entry


def test_tmp_sticky_root_is_readable_for_plan_but_apply_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared sticky root may be observed, never used as an apply owner."""

    tmp_root = Path("/tmp")
    metadata = tmp_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or not (metadata.st_mode & stat.S_ISVTX):
        pytest.skip("/tmp is not a sticky directory in this environment")
    if not (metadata.st_mode & 0o002):
        pytest.skip("/tmp is not a shared world-writable directory in this environment")

    # Keep this test focused on root ownership/permissions, not host mount
    # parser availability.  No candidate prefix is expected to exist.
    monkeypatch.setattr(
        historical_audit,
        "_mountinfo_snapshot",
        lambda: (frozenset(), "stable-mount-snapshot"),
    )
    manager = HistoricalAuditManager(
        tmp_root,
        prefix="__neocortex_adversarial_never_present__",
        max_entries=16,
        max_depth=0,
        max_bytes=4096,
    )

    plan = manager.plan()

    assert plan.read_only is True
    assert plan.root_blocked is None
    assert plan.applied == 0
    assert plan.scanned <= 16
    assert len(plan.records) + len(plan.unmanaged) <= 16

    applied = manager.apply(plan)

    assert applied.read_only is False
    assert applied.status == "blocked"
    assert applied.root_blocked is not None
    assert applied.applied == 0


def test_symlink_root_component_is_not_followed(tmp_path: Path) -> None:
    real_root = _root(tmp_path)
    entry = _adoptable_entry(real_root)
    alias = tmp_path / "historical-alias"
    os.symlink(real_root, alias)

    manager = HistoricalAuditManager(alias)

    plan = manager.plan()
    applied = manager.apply(plan)

    assert plan.status == applied.status == "blocked"
    assert plan.root_blocked == "historical root cannot contain symlink components"
    assert applied.root_blocked == plan.root_blocked
    assert plan.scanned == applied.scanned == 0
    assert alias.is_symlink()
    assert entry.exists()


def test_mountpoint_boundary_is_blocked_without_descending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    entry = _adoptable_entry(root)
    mountish = entry / "mounted-child"
    mountish.mkdir(mode=0o700)
    os.chmod(mountish, 0o700)
    (mountish / "opaque-payload").write_bytes(b"must not be treated as owned")
    os.chmod(mountish / "opaque-payload", 0o600)

    monkeypatch.setattr(
        historical_audit,
        "_mountinfo_snapshot",
        lambda: (frozenset({mountish}), "stable-mount-snapshot"),
    )

    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    record = plan.records[0]

    assert record.status == "blocked"
    assert record.adoptable is False
    assert record.identity_uncertain is True
    assert "mount_boundary" in (record.reason or "")
    assert entry.exists()

    applied = manager.apply(plan)
    assert applied.applied == 0
    assert applied.adoptable == 0
    assert entry.exists()


@pytest.mark.parametrize("mismatch", ["manifest", "adoption"])
def test_manifest_digest_or_adoption_binding_mismatch_is_not_adoptable(
    tmp_path: Path,
    mismatch: str,
) -> None:
    root = _root(tmp_path)
    entry = _adoptable_entry(root)
    manifest = _manifest(root, entry)
    adoption = manifest["historical_adoption"]
    assert isinstance(adoption, dict)
    if mismatch == "manifest":
        manifest["manifest_digest"] = "sha256:" + ("0" * 64)
    else:
        # The manifest digest intentionally remains valid because the
        # production digest excludes only this nested attestation field.
        adoption["digest"] = "sha256:" + ("1" * 64)
    _write_manifest(entry, manifest)

    plan = HistoricalAuditManager(root).plan()

    assert plan.adoptable == plan.planned == 0
    assert len(plan.records) == 1
    record = plan.records[0]
    assert record.status == "unknown"
    if mismatch == "manifest":
        assert "manifest digest mismatch" in (record.reason or "")
    else:
        assert "adoption id/digest binding" in (record.reason or "")

    applied = HistoricalAuditManager(root).apply(plan)
    assert applied.applied == 0
    assert entry.exists()


def test_apply_does_not_mutate_or_honor_a_stale_read_only_plan(tmp_path: Path) -> None:
    root = _root(tmp_path)
    entry = _adoptable_entry(root)
    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    before = plan.to_dict()
    assert plan.adoptable == 1

    # Revoke approval after planning.  A read-only plan is evidence, not an
    # effect authorization, and must remain an unchanged value object.
    _write_manifest(entry, _manifest(root, entry, approved=False))

    applied = manager.apply(plan)

    assert applied.applied == 0
    assert applied.planned == 0
    assert applied.status == "blocked"
    assert entry.exists()
    assert plan.to_dict() == before


def test_top_level_counts_and_records_are_bounded(tmp_path: Path) -> None:
    root = _root(tmp_path)
    limit = 3
    for index in range(9):
        name = (
            f"neocortex-adversarial-{index:02d}"
            if index % 2
            else f"unmanaged-adversarial-{index:02d}"
        )
        child = root / name
        child.mkdir(mode=0o700)
        os.chmod(child, 0o700)

    plan = HistoricalAuditManager(
        root,
        max_entries=limit,
        max_depth=0,
        max_bytes=1 << 20,
    ).plan()

    assert plan.truncated is True
    assert plan.scanned == len(plan.records)
    assert plan.scanned <= limit
    assert len(plan.unmanaged) <= limit
    assert len(plan.records) + len(plan.unmanaged) <= limit
    status_count = sum(
        plan.counts[name]
        for name in ("adoptable", "kept", "blocked", "unknown", "failed", "recovery_required")
    )
    assert status_count == plan.scanned
    assert all(record.path.parent == root for record in plan.records)


@pytest.mark.parametrize(
    ("payload_kind", "reason_fragment"),
    [
        ("symlink", "symlink_payload"),
        ("hardlink", "hardlink_payload"),
        ("fifo", "unsupported_payload_type"),
    ],
)
def test_nested_symlink_hardlink_and_fifo_payloads_are_blocked(
    tmp_path: Path,
    payload_kind: str,
    reason_fragment: str,
) -> None:
    root = _root(tmp_path)
    entry = _adoptable_entry(root)
    nested = entry / "nested"
    nested.mkdir(mode=0o700)
    os.chmod(nested, 0o700)
    outside = tmp_path / f"outside-{payload_kind}.bin"
    outside.write_bytes(b"outside target")
    os.chmod(outside, 0o600)
    payload = nested / "payload"
    if payload_kind == "symlink":
        os.symlink(outside, payload)
    elif payload_kind == "hardlink":
        os.link(outside, payload)
    else:
        os.mkfifo(payload, 0o600)
        os.chmod(payload, 0o600)
    _write_manifest(entry, _manifest(root, entry))

    manager = HistoricalAuditManager(root)
    plan = manager.plan()
    record = plan.records[0]

    assert record.status == "blocked"
    assert record.adoptable is False
    assert reason_fragment in (record.reason or "")
    assert entry.exists()

    applied = manager.apply(plan)

    assert applied.applied == 0
    assert entry.exists()
    assert outside.exists()
    if payload_kind == "fifo":
        assert stat.S_ISFIFO(payload.lstat().st_mode)
    else:
        assert payload.exists() or payload.is_symlink()
