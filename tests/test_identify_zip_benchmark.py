"""Contract tests for the isolated Identify/ZIP benchmark harness."""

from __future__ import annotations

from pathlib import Path

from tools.benchmark_identify_zip import (
    BenchmarkConfig,
    FIXTURE_SCHEMA,
    generate_fixture,
    run_benchmark,
)


def test_fixture_manifest_is_deterministic_and_contains_required_mix(tmp_path: Path) -> None:
    config = BenchmarkConfig(
        txt_count=4,
        image_count=2,
        pdf_count=1,
        generic_zip_count=2,
        atomic_zip_count=3,
        wrong_extension_zip_count=2,
        large_one_mb_bytes=128,
        large_ten_mb_bytes=256,
    )
    first = generate_fixture(tmp_path / "first", config)
    second = generate_fixture(tmp_path / "second", config)

    assert first.to_dict()["schema"] == FIXTURE_SCHEMA
    assert first.files == second.files
    assert first.bytes == second.bytes
    assert first.digest_sha256 == second.digest_sha256
    assert first.categories == second.categories
    assert first.categories["txt"] == 4
    assert first.categories["slow_candidate"] == 1
    assert first.categories["generic_zip"] == 2
    assert first.categories["atomic_zip"] == 3
    assert first.categories["wrong_extension_zip"] == 2
    assert first.categories["over_1mb"] == 1
    assert first.categories["over_10mb"] == 1


def test_benchmark_reports_comparable_fresh_state_modes_without_mutation() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            txt_count=8,
            image_count=1,
            pdf_count=1,
            generic_zip_count=2,
            atomic_zip_count=3,
            wrong_extension_zip_count=2,
            large_one_mb_bytes=128,
            large_ten_mb_bytes=256,
            slow_delay_seconds=0.001,
            timeout_seconds=30,
            modes=("unlimited", "s10", "s1"),
        )
    )

    assert report["status"] == "complete"
    assert report["safe_scope"] == {
        "synthetic_temporary_corpus": True,
        "personal_corpus_selected": False,
        "apply_executed": False,
        "network_used": False,
        "state_cold_per_mode": True,
    }
    fixture = report["fixture"]
    assert isinstance(fixture, dict)
    digest = fixture["digest_sha256"]
    rows = report["modes"]
    assert isinstance(rows, list)
    assert [row["mode"] for row in rows] == ["unlimited", "s10", "s1"]
    for row in rows:
        assert row["cold_state"]["state_directory_fresh"] is True
        assert row["cold_state"]["same_fixture_digest"] == digest
        assert row["comparability"]["speedup_claim"] is False
        assert row["io"]["detector_calls"] > 0
        assert row["io"]["zip_candidate_hits"] > 0
        assert row["scheduler"]["max_inflight_futures"] > 0
        assert row["progress"]["events"] > 0
        assert row["zip_intake"]["apply"] is False
