"""Focused contracts for compact machine-inventory summaries.

These tests exercise only temporary fixture roots.  The owner keeps its full
``to_dict`` record projection for callers that explicitly need it, while the
new ``to_summary_dict`` projection is deliberately record-free and labels the
scanner and presentation facets separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from neocortex.runtime.machine_inventory import (
    MACHINE_INVENTORY_BYTE_SEMANTICS,
    MachineInventoryRoot,
    collect_machine_inventory,
)


def _root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_compact_summary_keeps_every_root_when_records_hit_a_scanner_bound(
    tmp_path: Path,
) -> None:
    first = _root(tmp_path, "first")
    (first / "nested").mkdir()
    (first / "nested" / "payload").write_bytes(b"payload")
    second = _root(tmp_path, "second")
    (second / "other").write_bytes(b"other")

    report = collect_machine_inventory(
        [
            MachineInventoryRoot(first, category="tmp"),
            MachineInventoryRoot(second, category="cache"),
        ],
        max_entries=1,
        max_depth=2,
        max_bytes=1_000_000,
    )

    summary = _mapping(report.to_summary_dict())
    root_summaries = summary["root_summaries"]
    assert isinstance(root_summaries, list)
    assert len(root_summaries) == 2
    assert {str(_mapping(item)["path"]) for item in root_summaries} == {
        str(first),
        str(second),
    }
    assert all("records" not in _mapping(item) for item in root_summaries)
    assert "records" not in summary

    scanner = _mapping(_mapping(summary["coverage_metadata"])["scanner"])
    presentation = _mapping(_mapping(summary["coverage_metadata"])["presentation"])
    assert scanner["truncated"] is True
    assert "entry_limit" in cast(list[object], scanner["truncation_reasons"])
    assert presentation["truncated"] is False
    presentation_omissions = _mapping(summary["omissions"])["presentation"]
    expected_presentation_omissions = {
        "truncated": False,
        "reasons": [],
        "records_omitted": 0,
        "records_omitted_known": True,
    }
    assert expected_presentation_omissions.items() <= _mapping(
        presentation_omissions
    ).items()

    assert summary["records_scanned"] == report.scanned
    assert summary["records_returned"] == len(report.records)
    assert summary["records_omitted"] is None
    # The full projection remains available and is not silently replaced by
    # the compact summary contract.
    assert report.to_dict()["records"]


def test_summary_labels_apparent_allocated_and_observed_bytes_separately(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "sized")
    payload = root / "payload"
    payload.write_bytes(b"x" * 17)

    report = collect_machine_inventory(
        [MachineInventoryRoot(root, category="external")],
        max_entries=10,
        max_depth=1,
        max_bytes=1_000_000,
    )
    summary = _mapping(report.to_summary_dict())

    byte_values = _mapping(summary["bytes"])
    assert set(byte_values) >= {"apparent", "allocated", "observed"}
    assert byte_values["observed"] == byte_values["apparent"] + byte_values["allocated"]
    assert summary["byte_semantics"] == dict(MACHINE_INVENTORY_BYTE_SEMANTICS)

    root_summaries = cast(list[object], summary["root_summaries"])
    root_summary = _mapping(root_summaries[0])
    root_bytes = _mapping(root_summary["bytes"])
    assert set(root_bytes) >= {"apparent", "allocated", "observed"}
    assert root_bytes["observed"] == root_bytes["apparent"] + root_bytes["allocated"]
    assert _mapping(root_summary["byte_semantics"]) == dict(
        MACHINE_INVENTORY_BYTE_SEMANTICS
    )


def test_complete_summary_reports_zero_scanner_and_presentation_omissions(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "complete")
    (root / "payload").write_bytes(b"complete")

    report = collect_machine_inventory(
        [MachineInventoryRoot(root, category="tmp")],
        max_entries=10,
        max_depth=1,
        max_bytes=1_000_000,
    )
    summary = _mapping(report.to_summary_dict())
    scanner = _mapping(_mapping(summary["coverage_metadata"])["scanner"])
    presentation = _mapping(_mapping(summary["coverage_metadata"])["presentation"])

    assert scanner["truncated"] is False
    assert scanner["records_omitted"] == 0
    assert presentation["truncated"] is False
    assert presentation["records_omitted"] == 0
    assert summary["records_omitted"] == 0


def test_compact_summary_marks_symlink_root_blocked_without_following(
    tmp_path: Path,
) -> None:
    target = _root(tmp_path, "target")
    marker = target / "outside"
    marker.write_bytes(b"must remain untouched")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)

    report = collect_machine_inventory(
        [MachineInventoryRoot(root_link, category="external")],
        max_entries=10,
        max_depth=2,
        max_bytes=1_000_000,
    )
    summary = _mapping(report.to_summary_dict())
    root_summaries = cast(list[object], summary["root_summaries"])
    root_summary = _mapping(root_summaries[0])

    assert root_summary["status"] == "blocked"
    assert root_summary["reason_code"] == "root_symlink"
    assert root_summary["root_exists"] is True
    assert root_summary["records_scanned"] == 0
    assert root_summary["records_omitted"] is None
    assert marker.read_bytes() == b"must remain untouched"
