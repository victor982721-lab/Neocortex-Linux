"""Coordinated IMAGE budgets retain per-image errors and automatic capacity."""

from pathlib import Path
from typing import cast

import pytest

from neocortex.capabilities.formats.image.route import ImageRoute, ImageRouteConfig, ImageRouteState
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)
from tests.test_image_admission_shutdown import _images, _State


TEST_CAPABILITIES = ("image",)
MIB = 1024 * 1024


@pytest.mark.parametrize(
    ("route_budget_mib", "global_budget_mib", "expected_classified"),
    [(64, 1024, 0), (192, 1024, 1), (None, 1024, 1), (256, 128, 0)],
)
def test_effective_budget_rejects_oversized_candidate_without_aborting_route(
    tmp_path: Path,
    route_budget_mib: int | None,
    global_budget_mib: int,
    expected_classified: int,
) -> None:
    path, = _images(tmp_path, "tiny.png")
    coordinator = GlobalResourceCoordinator(
        ("image",),
        GlobalResourceLimits(
            memory_budget_bytes=global_budget_mib * MIB,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=4,
            sample_interval_seconds=0.001,
        ),
        route_memory_budgets=(
            {} if route_budget_mib is None else {"image": route_budget_mib * MIB}
        ),
        effective_cpu_probe=lambda: 4,
        resource_probe=lambda: ResourceSample(
            total_physical=8192 * MIB,
            available_physical=8192 * MIB,
            total_commit=8192 * MIB,
            available_commit=8192 * MIB,
            external_cpu_cores=0,
            own_cpu_cores=0,
            effective_cpu_capacity=4,
        ),
    )
    state = _State([path])
    route = ImageRoute(
        ImageRouteConfig(
            state_path=tmp_path / "image.sqlite3",
            root=tmp_path,
            workers=1,
            # This standalone fallback is deliberately smaller than the image.
            # Only the coordinator knows whether a Framework cap was explicit.
            memory_budget_bytes=64 * MIB,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            document_ocr_mode="never",
            isolate_decoders=False,
        ),
        # Empty selection uses only the three methods implemented by this fixture.
        cast(ImageRouteState, state),
        1,
        memory_gate=CoordinatedMemoryGate(coordinator, "image"),
    )

    result = route.run()

    assert result.classified == expected_classified
    assert result.errors == 1 - expected_classified
    assert not route.cancellation.is_cancelled
    assert result.peak_reserved_bytes == expected_classified * 192 * MIB
    if not expected_classified:
        assert state.review_candidates
        assert "image estimate exceeds the configured memory budget" in str(state.review_candidates)
    summary = coordinator.summary()
    assert summary.resident_bytes == summary.transient_bytes == 0
    assert summary.cpu_slots_in_use == summary.native_threads == 0
