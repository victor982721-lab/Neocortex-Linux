"""Contract tests for the isolated, synthetic exact-search cost benchmark."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import benchmarks.semantic_exact_search_benchmark as benchmark
from neocortex.semantic.semantic_config import multilingual_text_model


def _measurement() -> dict:
    observation = {
        "complete": True,
        "scanned": 100,
        "hits": 20,
        "next_cursor": None,
        "topk_fingerprint_sha256": "a" * 64,
    }
    round_value = {"observations": [observation]}
    return {
        "owner_fences": {"unchanged": True},
        "read_only_file_delta": {"database": 0, "wal": 0, "shm": 0, "journal": 0},
        "explain_query_plan": ["SEARCH fixture"],
        "readers": {
            "1": {
                "cold": deepcopy(round_value),
                "warm": [deepcopy(round_value) for _ in range(5)],
                "warm_latency_seconds": {"p95": 0.5, "samples": 5},
            },
        },
    }


def _failures(measured: dict) -> list[str]:
    return benchmark._measurement_failure_reasons(
        measured, extent=100, limit=20, max_vectors=100, readers=(1,),
    )


def test_normal_and_canary_scale_contracts_are_separate() -> None:
    normal = benchmark._parse_args([])
    assert tuple(normal.scales) == (100_000, 500_000)
    assert not normal.canary
    canary = benchmark._parse_args(["--canary"])
    assert tuple(canary.scales) == (100, 1_000)
    assert canary.canary


@pytest.mark.parametrize("arguments", [
    ["--scales", "100"],
    ["--canary", "--scales", "100000"],
    ["--canary", "--scales", "100", "100"],
])
def test_invalid_or_duplicate_scales_fail_before_running(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        benchmark._parse_args(arguments)
    assert error.value.code == 2


def test_fixture_metadata_matches_canonical_dto_without_loading_model() -> None:
    assert benchmark._fixture_model(benchmark.MODEL_SIGNATURE) == multilingual_text_model()


def test_measurement_accepts_five_actual_warm_round_lists() -> None:
    assert not _failures(_measurement())


@pytest.mark.parametrize("field,value,reason", [
    ("complete", False, "incomplete"),
    ("scanned", 99, "scan_mismatch"),
    ("hits", 19, "topk_count_mismatch"),
    ("next_cursor", 99, "cursor_remaining"),
])
def test_any_incomplete_warm_observation_invalidates_point(
    field: str, value: object, reason: str,
) -> None:
    measured = _measurement()
    measured["readers"]["1"]["warm"][3]["observations"][0][field] = value
    assert any(reason in failure for failure in _failures(measured))


@pytest.mark.parametrize("mutation,reason", [
    ("fence", "owner_fence_changed_or_missing"),
    ("bytes", "read_only_file_delta_nonzero_or_missing"),
    ("round", "warm_round_count"),
    ("reader", "warm_0_observation_count"),
    ("samples", "warm_sample_count"),
])
def test_changed_owner_or_incomplete_measurement_is_not_accepted(
    mutation: str, reason: str,
) -> None:
    measured = _measurement()
    reader = measured["readers"]["1"]
    if mutation == "fence":
        measured["owner_fences"]["unchanged"] = False
    elif mutation == "bytes":
        measured["read_only_file_delta"]["wal"] = 4096
    elif mutation == "round":
        reader["warm"].pop()
    elif mutation == "reader":
        reader["warm"][0]["observations"].append({})
    else:
        reader["warm_latency_seconds"]["samples"] = 4
    assert any(reason in failure for failure in _failures(measured))


def test_private_environment_preserves_inherited_audit_lab_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    environment = {
        "NEOCORTEX_AUDIT_LAB_ROOT": str(tmp_path),
        "NEOCORTEX_CORPUS_ROOT": "/not-a-benchmark-input",
    }
    monkeypatch.setattr(benchmark.os, "environ", environment)
    private = tmp_path / "private-run"
    benchmark._private_environment(private)
    assert environment["NEOCORTEX_AUDIT_LAB_ROOT"] == str(tmp_path)
    assert "NEOCORTEX_CORPUS_ROOT" not in environment
    for name in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR", "HF_HOME"):
        selected = Path(environment[name])
        assert selected.is_relative_to(private)
        assert selected.is_dir()
