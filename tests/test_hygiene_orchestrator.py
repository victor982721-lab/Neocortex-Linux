"""Focused contracts for the read-only hygiene orchestrator."""

from __future__ import annotations

import json
import os
from pathlib import Path

from neocortex.runtime.hygiene import (
    HYGIENE_SCHEMA,
    HygieneManager,
    plan_hygiene,
)
from neocortex.runtime.scratch import ScratchManager


class _Registry:
    def __init__(self, payload: dict[str, object], *, verify_result: object = True) -> None:
        self.payload = payload
        self.verify_result = verify_result
        self.plan_calls = 0
        self.verify_calls = 0

    def plan(self) -> dict[str, object]:
        self.plan_calls += 1
        return self.payload

    def verify(self, plan: object) -> object:
        del plan
        self.verify_calls += 1
        return self.verify_result


class _BoundedRegistry(_Registry):
    def __init__(self) -> None:
        super().__init__({"status": "ready"})
        self.plan_limits: tuple[int | None, int | None] | None = None

    def plan(
        self,
        *,
        now_ns: int | None = None,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, object]:
        del now_ns
        self.plan_limits = (max_entries, max_bytes)
        return self.payload


class _Retention:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def plan(self) -> dict[str, object]:
        return self.payload


def _registry(*, unmanaged: object = ()) -> _Registry:
    return _Registry(
        {
            "status": "ready",
            "observed": 2,
            "protected": 1,
            "eligible": 1,
            "unmanaged": unmanaged,
            "reasons": ("registry preview only",),
            "limits": {"max_entries": 8},
        }
    )


def _retention() -> _Retention:
    return _Retention(
        {
            "status": "ready",
            "stores": [
                {
                    "status": "ready",
                    "items": [
                        {"disposition": "protected"},
                        {"disposition": "eligible"},
                    ],
                }
            ],
        }
    )


def test_plan_federates_explicit_owners_and_is_preview_only(tmp_path: Path) -> None:
    owned = tmp_path / "owned-temp"
    audit = tmp_path / "audit-work"
    owned.mkdir(mode=0o700)
    audit.mkdir(mode=0o700)
    owned_manager = ScratchManager(owned)
    audit_manager = ScratchManager(audit)
    registry = _registry(unmanaged=(tmp_path / "foreign",))

    plan = HygieneManager(
        artifact_registry=registry,
        scratch_managers={"owned-temp": owned_manager, "audit-work": audit_manager},
        retention=_retention(),
        now_ns=1,
    ).plan()

    assert plan.schema == HYGIENE_SCHEMA
    assert plan.read_only is True
    assert plan.effects_enabled is False
    assert plan.preview_only is True
    assert plan.deletion_performed == 0
    assert plan.eligible == 2
    assert plan.protected == 2
    assert plan.blocked == 0
    assert set(plan.coverage) == {"artifact_registry", "owned-temp", "audit-work", "retention"}
    assert str(tmp_path / "foreign") in {str(path) for path in plan.unmanaged}


def test_scan_limits_are_forwarded_only_to_supporting_owner(tmp_path: Path) -> None:
    registry = _BoundedRegistry()

    HygieneManager(
        artifact_registry=registry,
        scan_max_entries=17,
        scan_max_bytes=4096,
        scan_max_depth=3,
        now_ns=1,
    ).plan()

    assert registry.plan_limits == (17, 4096)


def test_nested_owner_counts_are_preserved() -> None:
    registry = {
        "status": "planned",
        "counts": {"scanned": 3, "protected": 1, "eligible": 2, "blocked": 0, "unknown": 0},
        "bytes": {"protected": 7, "eligible": 11, "blocked": 0},
        "unmanaged": [],
    }

    plan = HygieneManager(artifact_registry=registry, now_ns=1).plan()

    assert plan.observed == 3
    assert plan.eligible == 2
    assert plan.protected == 1
    assert plan.coverage["artifact_registry"]["observed"] == 3
    assert plan.coverage["artifact_registry"]["eligible_bytes"] == 11


def test_missing_roots_are_deferred_and_blocked_without_creation(tmp_path: Path) -> None:
    before = tuple(tmp_path.iterdir())

    plan = plan_hygiene(now_ns=1)

    assert tuple(tmp_path.iterdir()) == before
    assert plan.status == "blocked"
    assert plan.blocked >= 4
    assert all(item["status"] == "deferred" for item in plan.coverage.values())
    assert all("omitted" in reason for reason in plan.reasons)


def test_verify_calls_registry_and_degrades_on_drift(tmp_path: Path) -> None:
    registry = _registry()
    plan = HygieneManager(artifact_registry=registry, now_ns=1).plan()
    registry.verify_result = False

    verified = HygieneManager(artifact_registry=registry, now_ns=1).verify(plan)

    assert registry.verify_calls == 1
    assert verified.verification == "drift"
    assert verified.status == "blocked"
    assert verified.eligible == 0
    assert any("verification rejected" in reason for reason in verified.reasons)


def test_verify_detects_filesystem_drift_without_mutating_scope(tmp_path: Path) -> None:
    root = tmp_path / "owned-temp"
    root.mkdir(mode=0o700)
    manager = HygieneManager(
        artifact_registry=_registry(),
        scratch_managers={"owned-temp": ScratchManager(root)},
        now_ns=1,
    )
    plan = manager.plan()
    root_stat = os.stat(root)
    (root / "neocortex-drift").write_text("foreign", encoding="utf-8")
    snapshot = (root / "neocortex-drift").read_bytes()

    verified = manager.verify(plan)

    assert verified.verification == "drift"
    assert verified.status == "blocked"
    assert (root / "neocortex-drift").read_bytes() == snapshot
    after_stat = os.stat(root)
    assert (root_stat.st_dev, root_stat.st_ino) == (after_stat.st_dev, after_stat.st_ino)


def test_json_is_bounded_and_json_ready(tmp_path: Path) -> None:
    huge = tuple(tmp_path / f"unmanaged-{index}" for index in range(10_000))
    plan = HygieneManager(
        artifact_registry=_registry(unmanaged=huge),
        now_ns=1,
    ).plan()
    encoded = plan.to_json().encode("utf-8")

    assert len(encoded) <= 64 * 1024
    payload = json.loads(encoded)
    assert payload["schema"] == HYGIENE_SCHEMA
    assert payload["read_only"] is True
    assert payload["effects_enabled"] is False
    assert payload["deletion_performed"] == 0
