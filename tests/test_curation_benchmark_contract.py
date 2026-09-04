"""Contract tests for the opt-in synthetic 0.12 scale benchmark."""

from __future__ import annotations

import json
import os
import stat
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

import tools.benchmark_curation_scale as benchmark_module
from tools.benchmark_curation_scale import (
    BENCHMARK_SCHEMA,
    REPOSITORY_ROOT,
    BenchmarkConfigurationError,
    iter_fixture_entries,
    main,
    run_benchmark,
    validate_config,
)


def test_fixture_generator_is_deterministic_and_streamable() -> None:
    first = tuple(iter_fixture_entries(5, payload_bytes=17))
    second = tuple(iter_fixture_entries(5, payload_bytes=17))

    assert first == second
    assert [entry.index for entry in first] == list(range(5))
    assert [entry.relative_path for entry in first] == [
        "shard-0000/item-0000000.bin",
        "shard-0000/item-0000001.bin",
        "shard-0000/item-0000002.bin",
        "shard-0000/item-0000003.bin",
        "shard-0000/item-0000004.bin",
    ]
    assert all(len(entry.payload) == 17 for entry in first)


def test_large_fixture_requires_explicit_opt_in_before_file_creation() -> None:
    with pytest.raises(BenchmarkConfigurationError, match="allow-large"):
        validate_config(count=100_001)

    configuration = validate_config(count=100_001, allow_large=True, timeout_seconds=1)
    assert configuration.count == 100_001
    assert configuration.allow_large is True


def test_hard_total_byte_cap_is_checked_before_generation() -> None:
    with pytest.raises(BenchmarkConfigurationError, match="hard cap"):
        validate_config(count=1_000_000, payload_bytes=1_024, allow_large=True)


def test_quick_benchmark_reports_bounded_metrics_without_receipt() -> None:
    report = run_benchmark(
        count=32,
        payload_bytes=11,
        batch_size=7,
        timeout_seconds=30,
    )

    assert report["schema"] == BENCHMARK_SCHEMA
    assert report["status"] == "complete"
    assert report["receipt_written"] is False
    fixture = report["fixture"]
    assert isinstance(fixture, dict)
    assert fixture["count"] == 32
    assert fixture["bytes"] == 32 * 11
    scan = report["scan"]
    assert isinstance(scan, dict)
    assert scan["files"] == 32
    assert scan["bytes"] == 32 * 11
    assert scan["errors"] == 0
    assert scan["batches"] == scan["commits"] == 5
    assert scan["max_pending_rows"] <= 7
    assert scan["eta_seconds"] == 0.0
    generation = report["generation"]
    assert isinstance(generation, dict)
    assert generation["files_per_second"] > 0
    assert generation["bytes_per_second"] > 0
    memory = report["memory"]
    assert isinstance(memory, dict)
    assert isinstance(memory["available"], bool)


def test_explicit_receipt_is_written_only_to_external_path(tmp_path: Path) -> None:
    receipt = tmp_path / "benchmark-receipt.json"
    report = run_benchmark(
        count=8,
        payload_bytes=9,
        batch_size=4,
        timeout_seconds=30,
        receipt_path=receipt,
    )

    assert receipt.is_file()
    assert report["receipt_written"] is True
    metadata = receipt.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    assert not tuple(tmp_path.glob(f".{receipt.name}.*.tmp"))
    assert json.loads(receipt.read_text(encoding="utf-8")) == report


def test_receipt_inside_repository_is_rejected_without_writing() -> None:
    receipt = REPOSITORY_ROOT / ".codex-benchmark-receipt.json"
    if receipt.exists():
        pytest.fail("test receipt sentinel unexpectedly exists in the repository")
    with pytest.raises(BenchmarkConfigurationError, match="outside the repository"):
        run_benchmark(count=1, receipt_path=receipt)
    assert not receipt.exists()


@pytest.mark.parametrize("kind", ("existing", "symlink", "hardlink"))
def test_existing_receipt_alias_is_rejected_without_following_or_overwriting(
    tmp_path: Path,
    kind: str,
) -> None:
    target = tmp_path / "receipt-target.json"
    target.write_text("sentinel\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    if kind == "existing":
        receipt.write_text("existing\n", encoding="utf-8")
    elif kind == "symlink":
        receipt.symlink_to(target)
    else:
        os.link(target, receipt)

    with pytest.raises(BenchmarkConfigurationError, match="already exists"):
        run_benchmark(count=1, receipt_path=receipt)
    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_receipt_parent_symlink_is_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(BenchmarkConfigurationError, match="parent directory"):
        run_benchmark(count=1, receipt_path=alias_parent / "receipt.json")
    assert not (real_parent / "receipt.json").exists()


def test_free_space_budget_is_checked_before_fixture_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    with pytest.raises(BenchmarkConfigurationError, match="free space"):
        run_benchmark(count=1, payload_bytes=8)


def test_fixture_deadline_is_checked_before_the_first_file(tmp_path: Path) -> None:
    with pytest.raises(benchmark_module.BenchmarkTimeout):
        benchmark_module.generate_fixture(
            tmp_path / "fixture",
            1,
            deadline=time.monotonic() - 1,
        )


def test_cli_small_run_is_explicit_and_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        (
            "--count",
            "4",
            "--payload-bytes",
            "8",
            "--batch-size",
            "2",
            "--timeout-seconds",
            "30",
        )
    )
    output = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(output.out)
    assert payload["schema"] == BENCHMARK_SCHEMA
    assert payload["status"] == "complete"
    assert output.err == ""


@pytest.mark.skipif(
    os.environ.get("NEOCORTEX_RUN_LARGE_BENCHMARK") != "1",
    reason="opt-in only: set NEOCORTEX_RUN_LARGE_BENCHMARK=1",
)
def test_opt_in_large_benchmark_is_bounded_and_complete() -> None:
    report = run_benchmark(
        count=100_001,
        payload_bytes=8,
        batch_size=1_024,
        timeout_seconds=900,
        allow_large=True,
    )

    assert report["status"] == "complete"
    fixture = report["fixture"]
    scan = report["scan"]
    assert isinstance(fixture, dict)
    assert isinstance(scan, dict)
    assert fixture["count"] == scan["files"] == 100_001
    assert fixture["bytes"] == scan["bytes"] == 100_001 * 8
    assert scan["max_pending_rows"] <= 1_024
    assert scan["errors"] == 0
