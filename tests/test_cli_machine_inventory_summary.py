"""CLI regressions for the bounded machine-inventory presentation layer."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Mapping
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main


def _owner_module(function) -> types.ModuleType:
    module = types.ModuleType("neocortex.runtime.machine_inventory")
    module.__dict__["collect_machine_inventory"] = function
    return module


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _owner_report(first: Path, second: Path) -> dict[str, object]:
    records = [
        {"path": str(first / "one"), "status": "preserved"},
        {"path": str(first / "two"), "status": "preserved"},
        {"path": str(second / "one"), "status": "observed"},
        {"path": str(second / "two"), "status": "observed"},
    ]
    roots = [
        {
            "root": str(first),
            "path": str(first),
            "root_index": 0,
            "category": "tmp",
            "status": "unknown",
            "coverage": "partial",
            "reason_code": "entry_limit",
            "records": records[:2],
            "records_scanned": 2,
            "records_returned": 2,
            "records_omitted": None,
            "scanner_truncated": True,
            "truncation_reasons": ["entry_limit"],
        },
        {
            "root": str(second),
            "path": str(second),
            "root_index": 1,
            "category": "cache",
            "status": "unknown",
            "coverage": "partial",
            "reason_code": "entry_limit",
            "records": records[2:],
            "records_scanned": 2,
            "records_returned": 2,
            "records_omitted": None,
            "scanner_truncated": True,
            "truncation_reasons": ["entry_limit"],
        },
    ]
    return {
        "schema": "neocortex.machine-inventory/v1",
        "operation": "machine-inventory",
        "status": "unknown",
        "coverage": "partial",
        "truncated": True,
        "scanner_truncated": True,
        "scanner_truncation_reasons": ["entry_limit"],
        "scanned": 4,
        "records_scanned": 4,
        "returned": 4,
        "records_returned": 4,
        "records_omitted": None,
        "root_count": 2,
        "status_counts": {
            "absent": 0,
            "blocked": 0,
            "observed": 2,
            "out_of_profile": 0,
            "preserved": 2,
            "unknown": 4,
        },
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


def test_machine_json_defaults_to_compact_root_and_record_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    def collect_machine_inventory(**_kwargs):
        return _owner_report(first, second)

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
                "--machine-json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["root_count"] == 2
    root_summaries = payload["root_summaries"]
    assert isinstance(root_summaries, list)
    assert {str(_mapping(item)["path"]) for item in root_summaries} == {
        str(first),
        str(second),
    }
    assert all("records" not in _mapping(item) for item in root_summaries)
    assert "records" not in payload
    assert "records" not in _mapping(payload["result"])

    assert payload["scanner_truncated"] is True
    assert payload["presentation_truncated"] is False
    assert payload["records_scanned"] == 4
    assert payload["records_returned"] == 4
    # Scanner omission remains unknown; it must not be confused with the
    # four records intentionally withheld by the compact renderer.
    assert payload["records_omitted"] is None
    assert payload["status_counts"]["preserved"] == 2
    presentation = _mapping(_mapping(payload["coverage_metadata"])["presentation"])
    assert presentation["mode"] == "compact"
    assert presentation["records_available"] == 4
    assert presentation["records_included"] == 0
    assert presentation["records_omitted"] == 4
    assert presentation["truncated"] is False


def test_machine_json_records_mode_is_explicit_and_keeps_scanner_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    def collect_machine_inventory(**_kwargs):
        return _owner_report(first, second)

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
                "--machine-json=records",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    result = _mapping(payload["result"])
    records = result["records"]
    assert isinstance(records, list)
    assert len(records) == 4
    assert payload["scanner_truncated"] is True
    assert payload["presentation_truncated"] is False
    presentation = _mapping(_mapping(payload["coverage_metadata"])["presentation"])
    assert presentation["mode"] == "records"
    assert presentation["records_available"] == 4
    assert presentation["records_included"] == 4
    assert presentation["records_omitted"] == 0
