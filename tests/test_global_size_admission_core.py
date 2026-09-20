"""Focused coverage for the run-wide size admission contract."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from neocortex.deduplication.admission import size_is_admitted, validate_max_file_bytes
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator_pipeline import (
    collect_size_admission_metrics,
)
from neocortex.runtime.orchestration.route_registry import (
    effective_route_config,
    effective_route_max_file_bytes,
)


def test_size_admission_is_inclusive_and_uses_decimal_bytes() -> None:
    assert validate_max_file_bytes(10_000_000) == 10_000_000
    assert size_is_admitted(10_000_000, 10_000_000)
    assert not size_is_admitted(10_000_001, 10_000_000)


def test_unlimited_mode_keeps_all_inventory_members_eligible() -> None:
    snapshots = (SimpleNamespace(size=1), SimpleNamespace(size=50_000_000))
    assert collect_size_admission_metrics(snapshots, None, total_files=2) == {
        "total_files": 2,
        "eligible_files": 2,
        "size_skipped_files": 0,
        "size_skipped_bytes": 0,
        "max_file_bytes": None,
    }


def test_inventory_metrics_observe_oversize_without_filtering_the_inventory() -> None:
    snapshots = tuple(
        SimpleNamespace(size=size)
        for size in (1_000, 10_000_000, 10_000_001, 50_000_000)
    )
    assert collect_size_admission_metrics(snapshots, 10_000_000, total_files=4) == {
        "total_files": 4,
        "eligible_files": 2,
        "size_skipped_files": 2,
        "size_skipped_bytes": 60_000_001,
        "max_file_bytes": 10_000_000,
    }


@pytest.mark.parametrize("invalid", (0, -1, True, 1.5, float("inf")))
def test_invalid_byte_limits_fail_closed(invalid: object) -> None:
    with pytest.raises(ValueError):
        validate_max_file_bytes(invalid)  # type: ignore[arg-type]


def test_route_specific_limit_cannot_widen_the_global_ceiling() -> None:
    config = replace(
        FrameworkConfig(),
        max_file_bytes=10_000_000,
        pdf_max_file_bytes=20_000_000,
    )
    assert effective_route_max_file_bytes(config, "pdf") == 10_000_000
    projected = effective_route_config(config, "pdf")
    assert projected.pdf_max_file_bytes == 10_000_000


def test_smaller_route_limit_wins_and_global_none_preserves_it() -> None:
    local = replace(
        FrameworkConfig(),
        max_file_bytes=100_000_000,
        image_max_file_bytes=20_000_000,
    )
    assert effective_route_max_file_bytes(local, "image") == 20_000_000

    no_global = replace(FrameworkConfig(), image_max_file_bytes=20_000_000)
    assert effective_route_max_file_bytes(no_global, "image") == 20_000_000


def test_metrics_require_the_complete_inventory_count() -> None:
    with pytest.raises(RuntimeError, match="count mismatch"):
        collect_size_admission_metrics(
            (SimpleNamespace(size=1),),
            10,
            total_files=2,
        )
