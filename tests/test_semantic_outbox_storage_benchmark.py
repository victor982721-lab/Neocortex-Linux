"""Admission, isolation and comparison contracts for the synthetic outbox probe."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmarks.semantic_outbox_storage_benchmark as benchmark
from neocortex.semantic import semantic_state


def test_normal_and_large_point_admission_is_bounded() -> None:
    args = benchmark._parse_args([])
    assert tuple(args.counts) == (100, 1_000, 5_000)
    assert not args.sql_diagnostics
    assert benchmark._validate_point_admission((100, 1_000, 5_000), 4_096) is None
    assert benchmark._validate_point_admission((10,), 900_000) is None


@pytest.mark.parametrize("arguments", (
    ["--counts", "0"],
    ["--counts", "5001"],
    ["--counts", "100", "100"],
    ["--counts", "11", "--payload-bytes", "900000"],
    ["--counts", "101", "--sql-diagnostics"],
    ["--payload-bytes", "900001"],
))
def test_invalid_points_fail_before_any_fixture(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        benchmark._parse_args(arguments)
    assert error.value.code == 2


def _comparison_receipt() -> dict:
    return {
        "kind": "work_receipt",
        "owner": "semantic",
        "runtime": {"semantic_schema": "9", "python": "9"},
        "effective_configuration": {"semantic_schema": "9", "owner_schema_version": 9},
        "inputs": [{"materialization": {
            "kind": "materialization_ref", "owner": "semantic", "owner_schema_version": 9,
        }}],
        "outputs": [{"materialization": {
            "kind": "materialization_ref", "owner": "text", "owner_schema_version": 9,
        }}],
    }


def test_comparison_only_normalizes_typed_semantic_provenance_without_mutation() -> None:
    receipt = _comparison_receipt()
    before = deepcopy(receipt)
    result = benchmark._normalize_work_receipt(receipt)
    assert receipt == before
    expected = deepcopy(receipt)
    expected["runtime"]["semantic_schema"] = "8"
    expected["inputs"][0]["materialization"]["owner_schema_version"] = 8
    assert result == expected


@pytest.mark.parametrize("owner", ("text", "other"))
def test_comparison_does_not_normalize_other_owners(owner: str) -> None:
    receipt = _comparison_receipt()
    receipt["owner"] = owner
    assert benchmark._normalize_work_receipt(receipt) == receipt


def test_future_schema_values_are_not_hidden_by_comparison() -> None:
    receipt = _comparison_receipt()
    receipt["runtime"]["semantic_schema"] = "999"
    receipt["inputs"][0]["materialization"]["owner_schema_version"] = 999
    assert benchmark._normalize_work_receipt(receipt) == receipt


def test_existing_and_symlink_outputs_are_never_replaced(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    work = tmp_path / "work"
    output = tmp_path / "report.json"
    output.write_bytes(b"original")
    with pytest.raises(benchmark.BenchmarkConfigurationError):
        benchmark._output_path(output, repository_root=repo, temp_root=work)
    assert output.read_bytes() == b"original"
    link = tmp_path / "alias.json"
    missing = tmp_path / "missing.json"
    link.symlink_to(missing)
    with pytest.raises(benchmark.BenchmarkConfigurationError):
        benchmark._output_path(link, repository_root=repo, temp_root=work)
    assert link.is_symlink()
    assert not missing.exists()


def test_inherited_boundary_rejected_before_creating_private_state(tmp_path: Path) -> None:
    boundary = tmp_path / "lab"
    boundary.mkdir()
    outside = tmp_path / "outside"
    with pytest.raises(benchmark.BenchmarkConfigurationError):
        benchmark._private_environment(outside, inherited_boundary=boundary)
    assert not outside.exists()


def test_inherited_boundary_can_contain_the_callers_private_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = tmp_path / "lab"
    home = boundary / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(boundary))
    assert benchmark._inherited_audit_boundary(repository_root=tmp_path / "source") == boundary


def test_private_environment_preserves_marker_and_checks_real_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = tmp_path / "lab"
    boundary.mkdir()
    environment = dict(os.environ, NEOCORTEX_AUDIT_LAB_ROOT=str(boundary))
    monkeypatch.setattr(os, "environ", environment)
    result = benchmark._private_environment(boundary / "run", inherited_boundary=boundary)
    assert os.environ["NEOCORTEX_AUDIT_LAB_ROOT"] == str(boundary)
    assert result["marker_preserved"]
    for detail in result["variables"].values():
        path = Path(detail["path"])
        assert path.is_relative_to(boundary)
        assert path.is_dir()
        assert path.stat().st_mode & 0o077 == 0
    assert os.environ["HF_HUB_OFFLINE"] == "1"


@pytest.mark.parametrize("tampering", ("fence", "same_size_receipt_change"))
def test_size_equality_is_insufficient_to_claim_reader_preservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tampering: str,
) -> None:
    stable = {
        "files": {"database": 4_096}, "fences": {"database": {"sha256": "a" * 64}},
        "pragmas": {"page_size": 4_096}, "tables": {"receipts": 1},
        "receipts": {"rows": 1, "raw_sha256": "a" * 64},
        "outbox": {
            "rows": 1, "orphan_rows": 0, "raw_sha256": "b" * 64,
            "wire_schema_counts": {benchmark.WIRE_V2: 1},
        },
    }
    altered = deepcopy(stable)
    if tampering == "fence":
        altered["fences"]["database"]["sha256"] = "c" * 64
    else:
        altered["receipts"]["raw_sha256"] = "c" * 64
    snapshots = iter((deepcopy(stable), deepcopy(stable), altered))
    reader = {
        "events_read": 1, "cursor_exhausted": True, "projection_replay_equal": True,
        "projection": {"status": "complete", "comparison_replay_equal": True},
        "wire_schema_counts": {benchmark.WIRE_V1: 1},
    }
    monkeypatch.setattr(semantic_state, "initialize_semantic_state", lambda _path: None)
    monkeypatch.setattr(benchmark, "_schema_version", lambda *_args: 9)
    monkeypatch.setattr(benchmark, "_storage_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(benchmark, "_write_receipts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(benchmark, "_read_and_project", lambda *_args, **_kwargs: reader)
    result = benchmark._run_point(
        count=1, payload_bytes=0, run_root=tmp_path, semantic_schema=None,
        semantic_lineage=SimpleNamespace(_record_work_receipt=lambda *_args, **_kwargs: 1),
        projection_module=None, sql_diagnostics=False,
    )
    assert result["status"] == "failed"
    assert not result["reader_storage_preserved"]
    assert result["failure_reasons"]
