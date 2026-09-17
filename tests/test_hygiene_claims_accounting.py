"""Adversarial selection identity and physical accounting contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.hygiene import HygieneManager


def _empty_owner() -> dict[str, object]:
    return {
        "status": "ready",
        "records": [],
        "observed": 0,
        "eligible": 0,
        "protected": 0,
        "blocked": 0,
        "unknown": 0,
    }


def _manager(registry: object) -> HygieneManager:
    return HygieneManager(
        artifact_registry=registry,
        scratch_managers={
            "owned-temp": _empty_owner(),
            "audit-work": _empty_owner(),
        },
        retention={"status": "ready", "stores": [], "observed": 0},
        now_ns=10**30,
    )


def _registry_with_records(tmp_path: Path, count: int) -> ArtifactRegistry:
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    for index in range(count):
        path = tmp_path / f"payload-{index:03d}.bin"
        path.write_bytes(b"x" * 10)
        path.chmod(0o600)
        registry.register(
            f"{index:03d}",
            path=path,
            producer="fixture",
            purpose="selection-claims",
            state="completed",
            kind="cache",
            disposable=True,
            created_ns=1,
        )
    return registry


@pytest.mark.parametrize("count", (63, 64, 65, 70, 100))
def test_selection_claims_cover_the_complete_owner_plan(
    tmp_path: Path,
    count: int,
) -> None:
    registry = _registry_with_records(tmp_path, count)

    plan = _manager(registry).plan()

    assert plan.selection_complete is True
    assert plan.selection_claims == count
    assert plan.coverage["artifact_registry"]["selection_claims"] == count
    assert plan.coverage["artifact_registry"]["selection_complete"] is True
    assert plan.eligible == count
    # These are apparent bytes of the unique fixture files, not allocated or
    # free filesystem blocks.
    assert plan.eligible_bytes == count * 10
    assert plan.logical_eligible_bytes == count * 10
    assert _manager(registry).verify(plan).verification == "verified"


def test_claim_exchange_after_the_first_display_page_is_detected(tmp_path: Path) -> None:
    registry = _registry_with_records(tmp_path, 70)
    manager = _manager(registry)

    registry.update("069", state="active", updated_ns=2)
    first = manager.plan()
    registry.update("068", state="active", updated_ns=3)
    registry.update("069", state="completed", updated_ns=3)
    second = manager.plan()

    assert first.eligible == second.eligible == 69
    assert first.protected == second.protected == 1
    assert first.eligible_bytes == second.eligible_bytes == 690
    assert first.selection_claims == second.selection_claims == 70
    assert first.fingerprint != second.fingerprint
    verified = manager.verify(first)
    assert verified.verification == "drift"
    assert verified.status == "blocked"
    assert verified.eligible == 0


def test_incomplete_claim_page_cannot_be_certified(tmp_path: Path) -> None:
    records = [
        {
            "record_id": str(index),
            "path": str(tmp_path / f"payload-{index}"),
            "path_identity": [1, index, -1],
            "classification": "eligible",
            "eligible": True,
            "size_bytes": 10,
        }
        for index in range(70)
    ]
    owner = {
        "status": "planned",
        "records": records[:64],
        "observed": 70,
        "eligible": 70,
        "truncated": True,
    }
    manager = _manager(owner)

    plan = manager.plan()

    assert plan.selection_complete is False
    assert plan.coverage["artifact_registry"]["selection_claims"] == 64
    assert plan.coverage["artifact_registry"]["selection_complete"] is False
    assert manager.verify(plan).verification == "drift"


def test_policy_and_provenance_claim_changes_are_selection_drift(tmp_path: Path) -> None:
    record = {
        "record_id": "a",
        "owner": "fixture",
        "producer": "producer-a",
        "purpose": "hygiene",
        "path": str(tmp_path / "payload"),
        "path_identity": [71, 9010, -1],
        "state": "completed",
        "classification": "eligible",
        "eligible": True,
        "disposable": True,
        "metadata": {"policy": "temporary"},
        "size_bytes": 10,
    }
    owner = {
        "status": "planned",
        "records": [record],
        "observed": 1,
        "eligible": 1,
        "eligible_bytes": 10,
    }
    manager = _manager(owner)

    first = manager.plan()
    record["metadata"] = {"policy": "retained"}
    second = manager.plan()

    assert first.fingerprint != second.fingerprint
    assert manager.verify(first).verification == "drift"


def test_physical_projection_is_counted_once_without_a_page_cutoff(tmp_path: Path) -> None:
    identity = [71, 9001, -1]
    first = {
        "status": "ready",
        "records": [
            {
                "artifact_id": "artifact-1",
                "path": str(tmp_path / "payload.bin"),
                "path_identity": identity,
                "classification": "eligible",
                "eligible": True,
                "size_bytes": 10,
            }
        ],
        "observed": 1,
        "eligible": 1,
        "eligible_bytes": 10,
    }
    second = {
        "status": "ready",
        "records": [
            {
                "record_id": "scratch-1",
                "path": str(tmp_path / "payload.bin"),
                "path_identity": identity,
                "classification": "eligible",
                "eligible": True,
                "size_bytes": 10,
            }
        ],
        "observed": 1,
        "eligible": 1,
        "eligible_bytes": 10,
    }
    manager = HygieneManager(
        artifact_registry=first,
        scratch_managers={"owned-temp": second, "audit-work": _empty_owner()},
        retention={"status": "ready", "stores": [], "observed": 0},
        now_ns=1,
    )

    plan = manager.plan()

    assert plan.logical_eligible_bytes == 20
    assert plan.eligible_bytes == 10
    assert plan.eligible_bytes != plan.logical_eligible_bytes


def test_missing_physical_identity_keeps_logical_bytes_and_explains_limit() -> None:
    owner = {
        "status": "ready",
        "records": [
            {
                "id": "without-identity",
                "classification": "eligible",
                "eligible": True,
                "size_bytes": 10,
            }
        ],
        "observed": 1,
        "eligible": 1,
        "eligible_bytes": 10,
    }

    plan = _manager(owner).plan()

    assert plan.eligible_bytes == plan.logical_eligible_bytes == 10
    assert any("physical accounting incomplete" in reason for reason in plan.reasons)
