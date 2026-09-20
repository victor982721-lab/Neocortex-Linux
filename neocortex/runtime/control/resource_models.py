"""Data contracts owned by the global resource coordinator.

The coordinator deliberately remains the sole mutable owner of these values.
This module only contains immutable public observations and the small mutable
records that are manipulated while ``GlobalResourceCoordinator._condition`` is
held.  Keeping the records separate from the admission algorithms makes the
lock boundary explicit without introducing a second coordinator.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True, slots=True)
class GlobalResourceLimits:
    memory_budget_bytes: int | None = None
    min_free_memory_bytes: int | None = None
    min_free_commit_bytes: int | None = None
    cpu_slots: int | None = None
    max_cpu_load_percent: float = 90.0
    wait_timeout_seconds: float | None = None
    poll_interval_seconds: float = 0.25
    # These fields are intentionally additive.  The first six fields are the
    # long-standing positional contract consumed by application projections.
    native_thread_slots: int | None = None
    cpu_hysteresis_percent: float = 5.0
    memory_hysteresis_bytes: int | None = None
    sample_interval_seconds: float = 0.25
    temp_budget_bytes: int | None = None
    memory_pressure_high_percent: float = 10.0
    memory_pressure_recovery_percent: float = 5.0
    io_slots: int | None = None
    io_device_slots: Mapping[str, int] | None = None
    gpu_memory_bytes: Mapping[str, int] | None = None
    io_pressure_high_percent: float = 20.0
    io_pressure_recovery_percent: float = 10.0


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Optional deterministic sample for the coordinator's live probes.

    ``memory_snapshot`` and ``cpu_load_probe`` remain the compatibility
    defaults.  A caller that already owns a bounded system sampler can inject
    this value instead; all fields are observations, never control commands.
    The coordinator computes deltas between successive samples and never
    treats a missing field as zero.
    """

    available_physical: int | None = None
    available_commit: int | None = None
    total_physical: int | None = None
    total_commit: int | None = None
    cpu_load_percent: float | None = None
    resident_bytes: int | None = None
    transient_bytes: int | None = None
    temp_bytes: int | None = None
    native_threads: int | None = None
    memory_pressure_some_percent: float | None = None
    memory_pressure_full_percent: float | None = None
    memory_pressure_some_total_us: int | None = None
    memory_pressure_full_total_us: int | None = None
    external_cpu_cores: float | None = None
    own_cpu_cores: float | None = None
    effective_cpu_capacity: float | None = None
    owned_materialized_bytes: int | None = None
    io_pressure_some_percent: float | None = None
    io_pressure_full_percent: float | None = None
    gpu_available_bytes: Mapping[str, int] | None = None

    @property
    def available_physical_bytes(self) -> int | None:
        return self.available_physical

    @property
    def available_commit_bytes(self) -> int | None:
        return self.available_commit


# Descriptive aliases keep the small injected-sample API discoverable without
# creating another coordinator or another resource owner.
GlobalResourceSample = ResourceSample
ResourcePressureSample = ResourceSample
ResourceUsage = ResourceSample
GlobalResourceUsage = ResourceSample


@dataclass(frozen=True, slots=True)
class RouteResourceSummary:
    admissions: int
    waits: int
    wait_seconds: float
    peak_reserved_bytes: int
    peak_cpu_slots: int
    wait_ns: int = 0
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    peak_resident_bytes: int = 0
    peak_transient_bytes: int = 0
    peak_temp_bytes: int = 0
    peak_native_threads: int = 0
    phases: dict[str, int] = field(default_factory=dict)
    io_slots: int = 0
    peak_io_slots: int = 0
    gpu_bytes: int = 0
    peak_gpu_bytes: int = 0
    active_execution_requests: int = 0
    cpu_slots_in_use: int = 0


@dataclass(frozen=True, slots=True)
class GlobalResourceSummary:
    memory_budget_bytes: int
    min_free_memory_bytes: int
    min_free_commit_bytes: int
    cpu_slots: int
    max_cpu_load_percent: float
    peak_reserved_bytes: int
    peak_cpu_slots: int
    peak_active_requests: int
    min_observed_available_memory_bytes: int | None
    min_observed_available_commit_bytes: int | None
    max_observed_cpu_load_percent: float | None
    min_effective_cpu_slots: int
    routes: dict[str, RouteResourceSummary]
    native_thread_slots: int = 0
    temp_budget_bytes: int | None = None
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    peak_resident_bytes: int = 0
    peak_transient_bytes: int = 0
    peak_temp_bytes: int = 0
    peak_native_threads: int = 0
    admission_paused: bool = False
    pressure_events: int = 0
    sample_count: int = 0
    last_available_memory_delta_bytes: int | None = None
    last_available_commit_delta_bytes: int | None = None
    memory_pressure_some_percent: float | None = None
    memory_pressure_full_percent: float | None = None
    last_memory_pressure_delta_percent: float | None = None
    last_memory_pressure_total_delta_us: int | None = None
    effective_cpu_slots: int = 0
    effective_memory_budget_bytes: int = 0
    own_cpu_cores: float | None = None
    external_cpu_cores: float | None = None
    materialized_credit_bytes: int = 0
    io_slots: int = 0
    peak_io_slots: int = 0
    gpu_bytes: int = 0
    peak_gpu_bytes: int = 0
    active_execution_requests: int = 0
    cpu_slots_in_use: int = 0


@dataclass(slots=True)
class _MutableRouteMetrics:
    """Per-route counters; mutate only while the coordinator condition is held."""

    admissions: int = 0
    waits: int = 0
    wait_seconds: float = 0.0
    wait_ns: int = 0
    reserved_bytes: int = 0
    cpu_slots: int = 0
    active_requests: int = 0
    peak_reserved_bytes: int = 0
    peak_cpu_slots: int = 0
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    peak_resident_bytes: int = 0
    peak_transient_bytes: int = 0
    peak_temp_bytes: int = 0
    peak_native_threads: int = 0
    phase_admissions: dict[str, int] = field(default_factory=dict)
    io_slots: int = 0
    peak_io_slots: int = 0
    gpu_bytes: int = 0
    peak_gpu_bytes: int = 0
    active_execution_requests: int = 0


@dataclass(slots=True)
class _Request:
    """One queued/admitted lease, owned by exactly one coordinator."""

    route_name: str
    memory_bytes: int
    cpu_slots: int
    enqueued_at: float
    enqueued_at_ns: int
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    phase: str | None = None
    resident_key: str | None = None
    resident_charge_bytes: int = 0
    waited: bool = False
    queued: bool = False
    admitted: bool = False
    released: bool = False
    wait_accounted: bool = False
    io_slots: int = 0
    io_device: str | None = None
    gpu_bytes: int = 0
    gpu_device: str | None = None
    execution_active: bool = False
    process_identities: set[tuple[int, int]] = field(default_factory=set)
    temp_materialized_bytes: int = 0
    temp_file_identity: tuple[int, int] | None = None
    draining_from: _Request | None = None
    adaptive_native_threads: bool = False
    native_thread_limit: int | None = None
