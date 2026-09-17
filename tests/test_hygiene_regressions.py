"""Regression cases for bounded artifact hygiene and lifecycle claims."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from neocortex.api.cli.cli_hygiene import _invoke_owner
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.hygiene import HygieneManager, plan_hygiene
from neocortex.runtime.scratch import ScratchManager, ScratchSecurityError, ScratchState


def _private_file(path: Path, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def test_cli_keeps_scan_budget_separate_from_json_budget(tmp_path: Path) -> None:
    roots = [tmp_path / "artifacts", tmp_path / "owned", tmp_path / "audit", tmp_path / "state"]
    result = _invoke_owner(
        plan_hygiene,
        roots,
        {"max_entries": 10_000, "max_depth": 2, "max_bytes": 1 << 40},
        preview=True,
    )
    assert result.json_max_bytes == 64 * 1024
    assert result.limits["scan_max_bytes"] == 1 << 40
    assert result.status == "blocked"


def test_split_artifact_root_is_a_valid_claim(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    artifact_root = tmp_path / "artifact-root"
    artifact_root.mkdir(mode=0o700)
    path = artifact_root / "output.bin"
    _private_file(path, b"payload")

    record = registry.register(
        "output",
        path=path,
        root=artifact_root,
        producer="fixture",
        purpose="split-root",
        state="completed",
        kind="cache",
        disposable=True,
        created_ns=1,
    )

    assert record.valid is True
    assert record.root == artifact_root
    assert registry.verify("output").verified is True


def test_selection_fingerprint_changes_when_claims_swap_with_equal_stats(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    paths = {}
    for name in "ABC":
        path = tmp_path / name
        _private_file(path)
        paths[name] = path
        registry.register(
            name,
            path=path,
            producer="fixture",
            purpose="selection",
            state="active" if name == "C" else "completed",
            kind="cache",
            disposable=True,
            created_ns=1,
        )

    manager = HygieneManager(artifact_registry=registry, now_ns=10)
    first = manager.plan()
    registry.update("B", state="active", updated_ns=3)
    registry.update("C", state="completed", updated_ns=3)
    second = manager.plan(now_ns=10)

    assert first.eligible == second.eligible == 2
    assert first.protected == second.protected == 1
    assert first.eligible_bytes == second.eligible_bytes
    assert first.fingerprint != second.fingerprint
    verified = manager.verify(first)
    assert verified.verification == "drift"
    assert verified.status == "blocked"
    assert verified.eligible == 0


def test_live_dependency_blocks_scratch_retirement_until_released(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    manager = ScratchManager(
        tmp_path / "scratch",
        owner="fixture",
        create_root=True,
        artifact_registry=registry,
    )
    source = manager.create(retain_on_success=True)
    source.complete(retain=True)
    consumer = manager.create(
        retain_on_success=True,
        metadata={"dependencies": [source.artifact_id]},
    )

    blocked = manager.apply(now_ns=10**30)
    assert blocked.applied == 0
    assert blocked.blocked >= 1
    assert source.path.exists()
    source_record = registry.plan(now_ns=10**30).records
    assert any(record.reason == "dependency_live" for record in source_record)

    assert consumer.artifact_id is not None
    registry.update(consumer.artifact_id, dependencies=[], updated_ns=10**30)
    released = manager.apply(now_ns=10**30)
    assert released.applied == 1
    assert not source.path.exists()


def test_dependency_writer_is_serialized_by_retirement_guard(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    path = tmp_path / "source"
    _private_file(path)
    registry.register(
        "source",
        path=path,
        producer="fixture",
        purpose="lock",
        state="completed",
        kind="cache",
        disposable=True,
        created_ns=1,
    )
    consumer_path = tmp_path / "consumer"
    _private_file(consumer_path)
    registry.register(
        "consumer",
        path=consumer_path,
        producer="fixture",
        purpose="lock",
        state="active",
        kind="cache",
        disposable=True,
        created_ns=1,
    )
    other = ArtifactRegistry(tmp_path / "registry", owner="fixture")
    started = threading.Event()
    finished = threading.Event()

    def add_dependency() -> None:
        started.set()
        other.update("consumer", dependencies=["source"], updated_ns=2)
        finished.set()

    thread = threading.Thread(target=add_dependency)
    with registry.retirement_guard("source"):
        thread.start()
        assert started.wait(1)
        time.sleep(0.05)
        assert not finished.is_set()
    thread.join(timeout=2)
    assert finished.is_set()


def test_scratch_scan_limits_are_fail_closed_and_bounded(tmp_path: Path) -> None:
    manager = ScratchManager(tmp_path / "scratch", create_root=True)
    for _ in range(3):
        workspace = manager.create(retain_on_success=True)
        nested = workspace.path / "nested"
        nested.mkdir(mode=0o700)
        _private_file(nested / "payload", b"x" * 32)
        workspace.complete(retain=True)

    plan = manager.plan(
        now_ns=10**30,
        max_entries=1,
        max_depth=0,
        max_bytes=1,
    )
    assert plan.truncated is True
    assert plan.status == "blocked"
    assert plan.planned == 0
    assert plan.blocked >= 1
    assert "depth_limit" in plan.truncation_reasons


def test_artifact_file_size_limit_is_not_presented_as_complete(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    _private_file(path, b"x" * 32)
    registry = ArtifactRegistry(
        tmp_path / "registry",
        owner="fixture",
        create_root=True,
        max_bytes=8,
    )
    registry.register(
        "payload",
        path=path,
        producer="fixture",
        purpose="bounded",
        state="completed",
        kind="cache",
        disposable=True,
        created_ns=1,
    )
    plan = registry.plan(now_ns=2)
    assert plan.eligible == 0
    assert plan.blocked == 1
    assert plan.truncated is True
    assert plan.records[0].issue == "size_truncated"


def test_completed_scratch_with_late_payload_is_preserved_for_recovery(tmp_path: Path) -> None:
    manager = ScratchManager(tmp_path / "scratch", create_root=True)
    workspace = manager.create(retain_on_success=True)
    _private_file(workspace.path / "published.txt", b"published")
    workspace.complete(retain=True)
    _private_file(workspace.path / "late.txt", b"late change")

    plan = manager.plan(now_ns=10**30)
    assert plan.planned == 0
    assert plan.blocked == 1
    assert plan.records[0].issue == "payload_changed_after_completion"
    assert workspace.path.exists()


def test_registry_policy_is_revalidated_before_scratch_effect(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    manager = ScratchManager(
        tmp_path / "scratch",
        owner="fixture",
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(retain_on_success=True)
    workspace.complete(retain=True)
    assert workspace.artifact_id is not None
    artifact_id = workspace.artifact_id

    registry.update(artifact_id, disposable=False)
    blocked = manager.apply(now_ns=10**30)
    assert blocked.applied == 0
    assert blocked.blocked == 1
    assert workspace.path.exists()
    assert registry.plan(now_ns=10**30).eligible == 0

    registry.update(artifact_id, disposable=True)
    released = manager.apply(now_ns=10**30)
    assert released.applied == 1
    assert not workspace.path.exists()
    retired = registry.verify(artifact_id)
    assert retired.state == "retired"


def test_corrupt_consumer_is_not_interpreted_as_no_dependency(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    manager = ScratchManager(
        tmp_path / "scratch",
        owner="fixture",
        create_root=True,
        artifact_registry=registry,
    )
    source = manager.create(retain_on_success=True)
    source.complete(retain=True)
    consumer = manager.create(
        retain_on_success=True,
        metadata={"dependencies": [source.artifact_id]},
    )
    assert consumer.artifact_id is not None
    consumer_manifest = registry.manifest_path(consumer.artifact_id)
    consumer_manifest.write_bytes(b"{\"schema\":")
    os.chmod(consumer_manifest, 0o600)

    applied = manager.apply(now_ns=10**30)
    assert applied.applied == 0
    assert source.path.exists()
    source_view = next(
        record for record in registry.plan(now_ns=10**30).records
        if record.artifact_id == source.artifact_id
    )
    assert source_view.classification == "blocked"
    assert source_view.reason == "dependency_observation_incomplete"


def test_post_effect_registry_failure_is_reconciled_without_repeating_unlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry_root = tmp_path / "registry"
    scratch_root = tmp_path / "scratch"
    registry = ArtifactRegistry(registry_root, owner="fixture", create_root=True)
    manager = ScratchManager(
        scratch_root,
        owner="fixture",
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(retain_on_success=True)
    workspace.complete(retain=True)
    artifact_id = workspace.artifact_id
    assert artifact_id is not None
    original_update = registry.update

    def fail_after_effect(*args, **kwargs):
        raise OSError("fixture registry confirmation failure")

    monkeypatch.setattr(registry, "update", fail_after_effect)
    first = manager.apply(now_ns=10**30)
    assert first.applied == 0
    assert first.blocked == 1
    assert not workspace.path.exists()

    monkeypatch.setattr(registry, "update", original_update)
    resumed_registry = ArtifactRegistry(registry_root, owner="fixture")
    resumed_manager = ScratchManager(
        scratch_root,
        owner="fixture",
        create_root=False,
        artifact_registry=resumed_registry,
    )
    replay = resumed_manager.apply(now_ns=10**30)
    assert replay.applied == 0
    assert replay.planned == 0
    retired = resumed_registry.verify(artifact_id)
    assert retired.state == "retired"
    assert retired.verified is True


def test_retirement_batch_reads_registry_linearly_at_scale(tmp_path: Path) -> None:
    """The apply path shares one dependency snapshot instead of rescanning N²."""

    reads_by_size: dict[int, int] = {}
    for count in (20, 40, 80, 160):
        root = tmp_path / f"case-{count}"
        registry = ArtifactRegistry(root / "registry", owner="fixture", create_root=True)
        manager = ScratchManager(
            root / "scratch",
            owner="fixture",
            create_root=True,
            artifact_registry=registry,
        )
        for _ in range(count):
            workspace = manager.create(retain_on_success=True)
            workspace.complete(retain=True)

        original_read = registry._read_manifest_payload
        reads = 0

        def counted(path: Path):
            nonlocal reads
            reads += 1
            return original_read(path)

        registry._read_manifest_payload = counted
        applied = manager.apply(now_ns=10**30)
        reads_by_size[count] = reads
        assert applied.applied == count

    # The current protocol uses a bounded constant number of reads per
    # target (including recovery and terminal confirmation); the assertion is
    # intentionally a generous linear ceiling, not a machine-time SLA.
    assert all(reads_by_size[count] <= count * 10 for count in reads_by_size)
    assert reads_by_size[160] < reads_by_size[20] * 10


def test_durable_seal_blocks_equal_size_late_change(tmp_path: Path) -> None:
    manager = ScratchManager(tmp_path / "scratch", create_root=True)
    workspace = manager.create(retain_on_success=True)
    payload = workspace.path / "payload.bin"
    _private_file(payload, b"original")
    sealed = manager.seal_workspace(workspace.record_id)
    assert sealed.seal is not None
    workspace.complete(retain=True)
    payload.write_bytes(b"changed!")
    os.chmod(payload, 0o600)

    plan = manager.plan(now_ns=10**30)
    assert plan.planned == 0
    assert plan.blocked == 1
    assert plan.records[0].issue == "workspace_seal_drift"
    assert manager.apply(now_ns=10**30).applied == 0
    assert payload.exists()


def test_failed_terminal_reconciliation_requires_evidence_and_retains_payload(
    tmp_path: Path,
) -> None:
    manager = ScratchManager(tmp_path / "scratch", create_root=True)
    workspace = manager.create(retain_on_success=True)
    _private_file(workspace.path / "partial.bin", b"partial")
    failed = workspace.fail("external process interrupted")
    with pytest.raises(ScratchSecurityError):
        manager.reconcile_terminal(
            failed.record_id,
            release_authorized=False,
            evidence={"operator": "fixture"},
        )
    reconciled = manager.reconcile_terminal(
        failed.record_id,
        release_authorized=True,
        evidence={"operator": "fixture", "outcome": "inspected"},
        now_ns=10**30,
    )
    assert reconciled.state is ScratchState.COMPLETED
    assert reconciled.retain_on_success is True
    assert workspace.path.exists()
    assert manager.plan(now_ns=10**30).planned == 1


def test_tombstone_retention_removes_only_manifest_and_replays(
    tmp_path: Path,
) -> None:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    manager = ScratchManager(
        tmp_path / "scratch",
        owner="fixture",
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(retain_on_success=True)
    workspace.complete(retain=True)
    artifact_id = workspace.artifact_id
    assert artifact_id is not None
    manager.apply(now_ns=10**30)
    manifest = registry.manifest_path(artifact_id)
    assert not workspace.path.exists()
    assert manifest.exists()
    result = registry.apply_tombstone_retention(
        [artifact_id],
        release_authorized=True,
        evidence={"reconciled": True, "operator": "fixture"},
    )
    assert result["status"] == "applied"
    assert not manifest.exists()
    assert list((registry.root / ".tombstone-retention").glob("receipt-*.json"))
    replay = registry.apply_tombstone_retention(
        [artifact_id],
        release_authorized=True,
        evidence={"reconciled": True, "operator": "fixture"},
        operation_id=str(result["operation_id"]),
    )
    assert replay["status"] == "applied"
