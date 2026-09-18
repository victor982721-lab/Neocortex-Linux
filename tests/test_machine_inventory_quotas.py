"""Regression contracts for fair bounded machine-inventory root coverage."""

from __future__ import annotations

import json
import sys
import types
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.runtime.machine_inventory import (
    MACHINE_INVENTORY_STATUSES,
    MachineInventoryRoot,
    collect_machine_inventory,
)


def _root_with_files(tmp_path: Path, index: int, *, files: int = 3) -> Path:
    root = tmp_path / f"root-{index}"
    root.mkdir(mode=0o700)
    for ordinal in range(files):
        (root / f"entry-{ordinal}").write_bytes(f"root-{index}-{ordinal}".encode())
    return root


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _owner_module(function) -> types.ModuleType:
    module = types.ModuleType("neocortex.runtime.machine_inventory")
    module.__dict__["collect_machine_inventory"] = function
    return module


def _zero_filled_counts(values: Mapping[str, int]) -> dict[str, int]:
    return {
        status: int(values.get(status, 0))
        for status in MACHINE_INVENTORY_STATUSES
    }


def test_entry_budget_gives_each_nonempty_root_a_turn_before_extra_turns(
    tmp_path: Path,
) -> None:
    roots = tuple(
        MachineInventoryRoot(_root_with_files(tmp_path, index), category="tmp")
        for index in range(4)
    )

    report = collect_machine_inventory(
        roots,
        max_entries=5,
        max_depth=1,
        max_bytes=1_000_000,
    )

    # Four roots have data and five entry credits are available.  The extra
    # credit may go to any deterministic root according to the allocator, but
    # no later root may be starved while a credit remains.
    per_root = [root.records_scanned for root in report.roots]
    quotas = report.root_entry_quotas
    assert report.root_quota_policy == "equal_fair_share_v1"
    assert quotas == (2, 1, 1, 1)
    assert sum(quotas) == report.max_entries
    assert all(root.entry_quota == quotas[root.root_index] for root in report.roots)
    assert all(root.records_scanned <= root.entry_quota for root in report.roots)
    assert report.records_scanned == report.scanned == len(report.records) == 5
    assert sum(per_root) == report.records_scanned
    assert all(scanned >= 1 for scanned in per_root)
    assert all(root.root_exists for root in report.roots)
    assert all(
        root.reason_code != "entry_limit" or root.records_scanned > 0
        for root in report.roots
    )


def test_record_and_root_status_counters_have_disjoint_provenance(
    tmp_path: Path,
) -> None:
    # With two credits and four one-entry roots, two roots are necessarily
    # left without a record turn.  They must still be represented by the root
    # counters rather than being fabricated as record classifications.
    roots = tuple(
        MachineInventoryRoot(_root_with_files(tmp_path, index, files=1), category="tmp")
        for index in range(4)
    )

    report = collect_machine_inventory(
        roots,
        max_entries=2,
        max_depth=1,
        max_bytes=1_000_000,
    )

    record_counts = Counter(record.status for record in report.records)
    expected_record_counts = _zero_filled_counts(record_counts)
    assert dict(report.record_status_counts) == expected_record_counts
    assert sum(report.record_status_counts.values()) == len(report.records)
    assert sum(report.record_status_counts.values()) == report.records_scanned

    root_counts = Counter(root.status for root in report.roots)
    expected_root_counts = _zero_filled_counts(root_counts)
    assert dict(report.root_status_counts) == expected_root_counts
    assert sum(report.root_status_counts.values()) == report.root_count
    summary = report.to_summary_dict()
    assert summary["root_status_counts"] == dict(report.root_status_counts)
    assert summary["record_status_counts"] == dict(report.record_status_counts)

    unvisited = [root for root in report.roots if root.records_scanned == 0]
    assert len(unvisited) >= 2
    assert report.root_entry_quotas == (1, 1, 0, 0)
    assert all(
        root.entry_quota == report.root_entry_quotas[root.root_index]
        for root in report.roots
    )
    assert all(root.status == "unknown" for root in unvisited)
    assert report.root_status_counts["unknown"] >= len(unvisited)
    # The root-level unknowns do not leak into record_status_counts.
    assert report.record_status_counts["unknown"] == record_counts.get("unknown", 0)
    assert report.record_status_counts["unknown"] == 0


def test_machine_json_projects_separate_record_and_root_status_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    for root in (first, second, third):
        root.mkdir(mode=0o700)

    records = [
        {"path": str(first / "entry"), "status": "observed"},
        {"path": str(second / "entry"), "status": "observed"},
    ]
    roots = [
        {
            "path": str(first),
            "root": str(first),
            "root_index": 0,
            "status": "observed",
            "coverage": "complete",
            "entry_quota": 1,
            "records_scanned": 1,
            "records_returned": 1,
            "records_omitted": 0,
            "records": records[:1],
        },
        {
            "path": str(second),
            "root": str(second),
            "root_index": 1,
            "status": "observed",
            "coverage": "complete",
            "entry_quota": 1,
            "records_scanned": 1,
            "records_returned": 1,
            "records_omitted": 0,
            "records": records[1:],
        },
        {
            "path": str(third),
            "root": str(third),
            "root_index": 2,
            "status": "unknown",
            "coverage": "partial",
            "reason_code": "entry_limit",
            "entry_quota": 0,
            "records_scanned": 0,
            "records_returned": 0,
            "records_omitted": None,
            "scanner_truncated": True,
            "scanner_truncation_reasons": ["entry_limit"],
            "records": [],
        },
    ]
    owner_report = {
        "schema": "neocortex.machine-inventory/v1",
        "operation": "machine-inventory",
        "status": "unknown",
        "coverage": "partial",
        "scanner_truncated": True,
        "scanner_truncation_reasons": ["entry_limit"],
        "records_scanned": 2,
        "records_returned": 2,
        "records_omitted": None,
        "root_count": 3,
        "root_quota_policy": "equal_fair_share_v1",
        "root_entry_quotas": [1, 1, 0],
        # The legacy aggregate intentionally includes root-level unknowns;
        # the new pair below makes their provenance explicit.
        "status_counts": {"observed": 2, "unknown": 1},
        "record_status_counts": {"observed": 2, "unknown": 0},
        "root_status_counts": {"observed": 2, "unknown": 1},
        "roots": roots,
        "records": records,
        "read_only": True,
        "diagnostic_only": True,
        "metadata_only": True,
        "content_read": False,
        "sqlite_read": False,
        "network_used": False,
        "kio_used": False,
        "mutated": False,
    }

    def collect_machine_inventory(**_kwargs):
        return owner_report

    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.machine_inventory",
        _owner_module(collect_machine_inventory),
    )

    assert (
        main(
            [
                "machine-inventory",
                "--machine-root",
                str(first),
                "--machine-root",
                str(second),
                "--machine-root",
                str(third),
                "--machine-json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["status_counts"]["unknown"] == 1
    assert payload["root_quota_policy"] == "equal_fair_share_v1"
    assert payload["root_entry_quotas"] == [1, 1, 0]
    assert payload["record_status_counts"]["observed"] == 2
    assert payload["record_status_counts"]["unknown"] == 0
    assert payload["root_status_counts"]["observed"] == 2
    assert payload["root_status_counts"]["unknown"] == 1
    assert sum(payload["record_status_counts"].values()) == payload["records_scanned"]
    assert sum(payload["root_status_counts"].values()) == payload["root_count"]

    result = _mapping(payload["result"])
    assert result["root_quota_policy"] == "equal_fair_share_v1"
    assert result["root_entry_quotas"] == [1, 1, 0]
    assert result["record_status_counts"] == payload["record_status_counts"]
    assert result["root_status_counts"] == payload["root_status_counts"]


def test_root_boundary_reason_summary_preserves_multiple_limits(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for ordinal in range(3):
        nested = root / f"{ordinal:02d}-dir"
        nested.mkdir(parents=True, mode=0o700)
        (nested / "child").write_bytes(b"child")

    report = collect_machine_inventory(
        (MachineInventoryRoot(root, category="tmp"),),
        max_entries=2,
        max_depth=1,
        max_bytes=1_000_000,
    )

    # Every bounded native readdir prefix encounters a directory with a child.
    # Preserve both observed boundaries without depending on readdir order.
    assert report.records_scanned == report.scanned == 2
    assert report.root_entry_quotas == (2,)
    assert set(report.roots[0].truncation_reasons) == {"entry_limit", "depth_limit"}
    assert report.reason_summary["entry_limit"] == 1
    assert report.reason_summary["depth_limit"] == 1
    assert report.root_marker_reason_counts == {
        "entry_limit": 1,
        "depth_limit": 1,
    }
