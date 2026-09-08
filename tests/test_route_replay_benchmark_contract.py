"""Contracts for the temporary two-pass route replay benchmark."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import benchmarks.route_replay_benchmark as benchmark


def test_default_fixture_profile_is_heterogeneous_bounded_and_deterministic(
    tmp_path: Path,
) -> None:
    first = benchmark.build_fixture(tmp_path / "first")
    second = benchmark.build_fixture(tmp_path / "second")

    assert first == second
    assert 20 <= first.files <= 50
    assert first.files == 28
    assert first.groups["base"] == 20
    assert first.groups["documents"] == 3
    assert first.groups["archive"] == 1
    assert (tmp_path / "first" / "archive" / "fixture.zip").is_file()


def test_fixture_builder_rejects_a_non_temporary_destination(tmp_path: Path) -> None:
    outside = Path("/home") / f"neocortex-route-replay-{tmp_path.name}"

    with pytest.raises(benchmark.BenchmarkConfigurationError, match="temporary"):
        benchmark.build_fixture(outside)

    assert not outside.exists()


def test_route_metrics_normalizes_cache_hits_cached_errors_reuse_and_new_work() -> None:
    payload = {
        "status": "completed",
        "routes": [
            {
                "route_name": "docx",
                "status": "completed",
                "candidates": 5,
                "processed": 1,
                "cache_hits": 3,
                "cached_errors": 1,
                "new_work": 1,
                "replay_status": "mixed",
                "replayability": "safe_replay",
                "started_ns": 10,
                "completed_ns": 2_000_000_010,
            }
        ],
    }

    metrics = benchmark._route_metrics(payload, "docx")

    assert metrics["cache_hits"] == 3
    assert metrics["cached_errors"] == 1
    assert metrics["reuse"] == 4
    assert metrics["new_work"] == 1
    assert metrics["route_wall_seconds"] == 2.0


def test_route_metrics_rejects_a_status_payload_without_the_selected_route() -> None:
    with pytest.raises(benchmark.BenchmarkExecutionError, match="omitted selected route"):
        benchmark._route_metrics({"routes": []}, "image")


@pytest.mark.skipif(
    os.environ.get("NEOCORTEX_RUN_ROUTE_REPLAY_BENCHMARK") != "1",
    reason="opt-in: executes the installed product over the 28-file temporary profile",
)
def test_opt_in_route_replay_benchmark_is_two_pass_and_isolated() -> None:
    report = benchmark.run_benchmark(timeout_seconds=180)

    assert report["schema"] == benchmark.BENCHMARK_SCHEMA
    assert report["fixture"]["files"] == 28
    assert report["fixture"]["temporary"] is True
    assert report["state_is_temporary"] is True
    assert len(report["executions"]) == 2 * len(benchmark.DEFAULT_ROUTES)
    for route in benchmark.DEFAULT_ROUTES:
        for run in ("run_1", "run_2"):
            metrics = report["by_route"][route][run]
            assert metrics["wall_seconds"] >= 0
            assert metrics["cpu_seconds"] >= 0
            assert metrics["cache_hits"] >= 0
            assert metrics["reuse"] >= 0
            assert metrics["new_work"] >= 0
