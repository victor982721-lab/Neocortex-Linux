"""Independent acceptance for Audio's producer/admission boundary."""

from __future__ import annotations

from neocortex.runtime.control.elastic_workers import elastic_map
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)


def _tiny_coordinator() -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("audio",),
        GlobalResourceLimits(
            memory_budget_bytes=2,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=2,
            native_thread_slots=2,
            io_slots=1,
            wait_timeout_seconds=0.5,
            poll_interval_seconds=0.005,
        ),
        effective_cpu_probe=lambda: 2,
        resource_probe=lambda: ResourceSample(
            available_physical=2,
            available_commit=2,
            total_physical=4,
            total_commit=4,
            cpu_load_percent=0,
        ),
        cpu_load_probe=lambda: 0,
    )


def test_producer_probe_can_wait_for_memory_without_result_cycle() -> None:
    """A 2-byte probe must not wait forever behind a retained 1-byte result."""

    coordinator = _tiny_coordinator()
    gate = CoordinatedMemoryGate(coordinator, "audio")

    def source():
        yield 1
        # This models an Audio probe performed while the prior transcription
        # result still owns its bounded result bytes.
        with gate.admit(2, native_threads=0, io_slots=1, phase="audio-probe"):
            pass
        yield 2

    try:
        with elastic_map(
            lambda item: item,
            source(),
            gate=gate,
            estimated_bytes=1,
            max_workers=2,
            poll_interval=0.005,
        ) as results:
            assert list(results) == [1, 2]
    finally:
        coordinator.close()
