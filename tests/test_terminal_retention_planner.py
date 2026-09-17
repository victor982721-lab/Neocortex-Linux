"""Bounded terminal-activity retention contracts (C08).

These tests use projections shaped like the existing ScratchManager and
ArtifactRegistry records.  The planner is intentionally read-only: a
canonical owner must perform any later effect under its own lock and recovery
protocol.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from neocortex.workflow.retention.planner import (
    TerminalRetentionPolicy,
    TerminalRetentionRecord,
    plan_terminal_retention,
    terminal_retention_plan_payload,
)


NOW_NS = 10_000
IDENTITY = (7, 11, -1)


def _record(
    record_id: str,
    *,
    status: str = "completed",
    category: str = "workspace",
    terminal_ns: int = 1,
    apparent_bytes: int = 10,
    allocated_bytes: int = 16,
    physical_identity: tuple[int, int, int] | None = None,
    reconciled: bool = True,
    release_authorized: bool = True,
    **kwargs: Any,
) -> TerminalRetentionRecord:
    return TerminalRetentionRecord(
        record_id,
        status,
        category=cast(Any, category),
        terminal_ns=terminal_ns,
        apparent_bytes=apparent_bytes,
        allocated_bytes=allocated_bytes,
        physical_identity=physical_identity
        or (7, sum((index + 1) * ord(char) for index, char in enumerate(record_id)), -1),
        reconciled=reconciled,
        release_authorized=release_authorized,
        **kwargs,
    )


def test_reconciliation_and_explicit_release_are_required() -> None:
    interrupted = _record(
        "abandoned",
        status="abandoned",
        reconciled=False,
        release_authorized=True,
    )
    recovered = replace(interrupted, reconciled=True)
    active = _record("active", status="active", reconciled=False, release_authorized=False)
    recovery = _record(
        "recovery",
        status="recovery_required",
        recovery_required=True,
        reconciled=True,
        release_authorized=True,
    )
    old_success = _record("success", release_authorized=True)

    first = plan_terminal_retention(
        (interrupted, active, recovery, old_success),
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, workspace_count=0),
    )
    assert first.eligible_count == 1
    abandoned_item = next(item for item in first.items if item.record_id == "abandoned")
    assert abandoned_item.disposition == "protected"
    assert "reconciliation_required" in abandoned_item.reasons
    assert (
        next(item for item in first.items if item.record_id == "active").disposition
        == "protected"
    )
    assert (
        next(item for item in first.items if item.record_id == "recovery").disposition
        == "protected"
    )

    second = plan_terminal_retention(
        (recovered, active, recovery, old_success),
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, workspace_count=0),
    )
    assert (
        next(item for item in second.items if item.record_id == "abandoned").disposition
        == "eligible"
    )

    ttl_only = _record("ttl-only", release_authorized=False)
    ttl_plan = plan_terminal_retention(
        (ttl_only,),
        now_ns=NOW_NS + 1_000_000,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, workspace_count=0),
    )
    item = ttl_plan.items[0]
    assert item.disposition == "protected"
    assert "release_not_authorized" in item.reasons


def test_tombstone_quota_bounds_evidence_without_releasing_recovery() -> None:
    tombstones = tuple(
        _record(
            f"tombstone-{index}",
            status="retired",
            category="tombstone",
            terminal_ns=index,
            apparent_bytes=10,
            allocated_bytes=16,
            evidence_required=False,
        )
        for index in range(1, 5)
    )
    pending = _record(
        "tombstone-replay",
        status="retired",
        category="tombstone",
        terminal_ns=0,
        replay_required=True,
        evidence_required=True,
    )
    plan = plan_terminal_retention(
        (*tombstones, pending),
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(
            minimum_age_ns=0,
            tombstone_count=2,
            tombstone_bytes=100,
        ),
    )
    by_id = {item.record_id: item for item in plan.items}
    assert by_id["tombstone-4"].disposition == "protected"
    assert by_id["tombstone-3"].disposition == "protected"
    assert by_id["tombstone-1"].disposition == "eligible"
    assert by_id["tombstone-2"].disposition == "eligible"
    assert by_id["tombstone-replay"].disposition == "protected"
    assert "replay_required" in by_id["tombstone-replay"].reasons
    assert plan.tombstone_count == 5
    assert plan.recovery_pending_count == 1


def test_duplicate_owner_projections_are_counted_once_physically() -> None:
    records = (
        _record(
            "workspace-view",
            apparent_bytes=10,
            allocated_bytes=4_096,
            physical_identity=IDENTITY,
        ),
        _record(
            "registry-view",
            apparent_bytes=10,
            allocated_bytes=4_096,
            physical_identity=IDENTITY,
        ),
        _record("other", apparent_bytes=4, allocated_bytes=8, physical_identity=(7, 12, -1)),
    )
    plan = plan_terminal_retention(
        records,
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, workspace_count=0),
    )
    assert plan.logical_apparent_bytes == 24
    assert plan.observed_allocated_bytes == 8_200
    assert plan.unique_apparent_bytes == 14
    assert plan.unique_allocated_bytes == 4_104
    assert plan.physical_unique_count == 2
    assert plan.unique_workspace_count == 2
    assert plan.physical_accounting_complete


def test_observation_limits_fail_closed_and_report_omissions() -> None:
    records = tuple(_record(f"workspace-{index}") for index in range(3))
    plan = plan_terminal_retention(
        records,
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, max_records=2, workspace_count=0),
    )
    assert plan.status == "blocked"
    assert plan.truncated
    assert plan.truncation_reasons == ("record_limit",)
    assert plan.omitted_records == 1
    assert plan.eligible_count == 0
    assert all(item.disposition == "blocked" for item in plan.items)
    assert all("observation_incomplete" in item.reasons for item in plan.items)

    byte_limited = plan_terminal_retention(
        (_record("large", apparent_bytes=9, allocated_bytes=1),),
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, max_bytes=9),
    )
    assert byte_limited.status == "blocked"
    assert byte_limited.truncation_reasons == ("byte_limit",)
    assert byte_limited.observed_count == 0
    assert byte_limited.eligible_count == 0


def test_balance_exposes_new_records_apparent_allocated_and_free_separately() -> None:
    records = (
        _record("old", apparent_bytes=10, allocated_bytes=16),
        _record("new", apparent_bytes=3, allocated_bytes=4),
    )
    plan = plan_terminal_retention(
        records,
        now_ns=NOW_NS,
        baseline_record_ids=("old",),
        free_bytes=987_654,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, workspace_count=0),
    )
    assert plan.new_terminal_records == 1
    assert plan.bytes["logical_apparent"] == 13
    assert plan.bytes["observed_allocated"] == 20
    assert plan.bytes["free"] == 987_654
    assert plan.bytes["planned_release_apparent"] == 13
    assert plan.counts["unique_workspaces"] == 2
    payload = terminal_retention_plan_payload(plan)
    assert payload["read_only"] is True
    assert payload["physical_recovery_status"] == "not_verified"
    assert payload["bytes"]["free"] == 987_654  # type: ignore[index]


def test_terminal_fingerprint_is_order_independent_and_policy_bound() -> None:
    records = (_record("a"), _record("b", terminal_ns=2))
    policy = TerminalRetentionPolicy(minimum_age_ns=0, workspace_count=0)
    forward = plan_terminal_retention(records, now_ns=NOW_NS, policy=policy)
    reverse = plan_terminal_retention(tuple(reversed(records)), now_ns=NOW_NS, policy=policy)
    changed_policy = plan_terminal_retention(
        records,
        now_ns=NOW_NS,
        policy=replace(policy, workspace_count=1),
    )
    assert forward.fingerprint == reverse.fingerprint
    assert forward.fingerprint != changed_policy.fingerprint


def test_mapping_adapter_defaults_tombstone_evidence_conservatively() -> None:
    plan = plan_terminal_retention(
        (
            {
                "artifact_id": "retired-manifest",
                "state": "retired",
                "category": "registry_tombstone",
                "updated_ns": 1,
                "size_bytes": 10,
                "path_identity": [7, 13, -1],
                "reconciled": True,
                "release_authorized": True,
            },
        ),
        now_ns=NOW_NS,
        policy=TerminalRetentionPolicy(minimum_age_ns=0, tombstone_count=0),
    )
    item = plan.items[0]
    assert item.disposition == "protected"
    assert "evidence_required" in item.reasons
