"""Admission validation and queue waiting for the global coordinator.

The functions in this module are deliberately parameterized by the one
coordinator instance. They never create state or a lock; every mutable
transition remains behind ``coordinator._condition``.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager

from .cancellation import CancellationRequested, CancellationToken
from .memory_runtime import MemoryBudgetExceeded, MemoryHeadroomTimeout
from .resource_grants import ResourceGrant, _CURRENT_RESOURCE_GRANT
from .resource_models import _Request


class ResourceWaitTimeout(MemoryHeadroomTimeout):
    """A global admission wait expired; worker execution did not time out."""

    def __init__(self, message: str, *, reason: str = "resource") -> None:
        super().__init__(message)
        self.reason = reason


class NativeThreadBudgetExceeded(MemoryBudgetExceeded):
    """A request asks for more native threads than the configured cap."""


@contextmanager
def admission_scope(
    coordinator,
    route_name: str,
    memory_bytes: int | None = None,
    cpu_slots: int = 1,
    *,
    resident_bytes: int = 0,
    transient_bytes: int | None = None,
    temp_bytes: int = 0,
    native_threads: int = 0,
    phase: str | None = None,
    resident_key: str | None = None,
    cancellation: CancellationToken | None = None,
    io_slots: int = 0,
    io_device: str | None = None,
    gpu_bytes: int = 0,
    gpu_device: str | None = None,
    _draining_from: _Request | None = None,
    _adaptive_native_threads: bool = False,
    _native_thread_limit: int | None = None,
    _renewing_from: _Request | None = None,
):
    """Wait for and hold one bounded route admission.

    ``memory_bytes`` is the original aggregate reservation API.  When any
    resource components are supplied, it is an optional total check (not
    an additional charge); the components are charged exactly once as
    ``resident + transient``. Temporary disk bytes have their own budget;
    callers using tmpfs must also declare the corresponding RAM demand.
    A ``resident_key`` shares the
    resident component between concurrent phases of one route.
    """

    coordinator.checkpoint()
    if cancellation is not None:
        cancellation.checkpoint()
    if route_name not in coordinator._queues:
        raise ValueError(f"route is not coordinated: {route_name}")
    try:
        aggregate_memory = 0 if memory_bytes is None else int(memory_bytes)
        requested_resident = int(resident_bytes)
        requested_temp = int(temp_bytes)
        requested_native = int(native_threads)
        requested_io = int(io_slots)
        requested_gpu = int(gpu_bytes)
        requested_transient = None if transient_bytes is None else int(transient_bytes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("global resource reservations must be integers") from exc
    if aggregate_memory < 0:
        raise ValueError("global memory reservation cannot be negative")
    if min(requested_resident, requested_temp, requested_native, requested_io, requested_gpu) < 0:
        raise ValueError("global resource components cannot be negative")
    if (
        requested_gpu
        and coordinator._gpu_available_probes
        and not coordinator._gpu_monitor_running()
    ):
        # An unmonitored GPU request needs a current device observation;
        # an unrelated CPU/RAM request must not invoke driver telemetry.
        coordinator._sample_gpu_resources()
    if requested_transient is not None and requested_transient < 0:
        raise ValueError("global transient reservation cannot be negative")
    component_mode = (
        requested_resident > 0
        or requested_temp > 0
        or requested_native > 0
        or requested_transient is not None
        or phase is not None
        or resident_key is not None
    )
    if not component_mode:
        requested_transient = max(1, aggregate_memory)
        requested_resident = 0
        requested_temp = 0
    else:
        if requested_transient is None:
            requested_transient = max(0, aggregate_memory - requested_resident - requested_temp)
        component_total = requested_resident + requested_transient + requested_temp
        if aggregate_memory not in (0, component_total):
            raise ValueError(
                "memory_bytes is an aggregate alias when resource components "
                "are supplied; it must equal their sum"
            )
        aggregate_memory = component_total
    requested_memory = requested_resident + requested_transient
    if _renewing_from is not None and (
        requested_memory
        or requested_temp
        or requested_gpu
        or _draining_from is not None
        or _adaptive_native_threads
    ):
        raise ValueError("execution renewal cannot grow memory or change its native width")
    requested_cpu = int(cpu_slots)
    if requested_cpu < 0:
        raise ValueError("CPU slots cannot be negative")
    if coordinator._automatic_memory_budget or coordinator._automatic_cpu_slots:
        with coordinator._condition:
            coordinator._observe_live_resources()
    memory_ceiling = (
        coordinator._last_memory_budget
        if coordinator._automatic_memory_budget
        else coordinator.memory_budget_bytes
    )
    cpu_ceiling = (
        coordinator._last_cpu_capacity
        if coordinator._automatic_cpu_slots
        else coordinator.cpu_slots
    )
    if requested_memory > memory_ceiling:
        raise MemoryBudgetExceeded(
            f"{route_name} requires {requested_memory} bytes but the global "
            f"budget is {memory_ceiling} bytes"
        )
    route_budget = coordinator._route_memory_budgets.get(route_name)
    if route_budget is not None and requested_memory > route_budget:
        raise MemoryBudgetExceeded(
            f"{route_name} requires {requested_memory} bytes but its route "
            f"budget is {route_budget} bytes"
        )
    # A backend's already granted width remains real after a temporary
    # quota/affinity contraction. Renewal waits for it without allocating
    # new memory; explicit ceilings and _fits' live capacity still apply.
    if requested_cpu > cpu_ceiling and (
        _renewing_from is None or not coordinator._automatic_cpu_slots
    ):
        raise MemoryBudgetExceeded(
            f"{route_name} requires {requested_cpu} CPU slots but only {cpu_ceiling} are configured"
        )
    if requested_native > coordinator.native_thread_slots and (
        _renewing_from is None or not coordinator._automatic_native_thread_slots
    ):
        raise NativeThreadBudgetExceeded(
            f"{route_name} requires {requested_native} native threads but only "
            f"{coordinator.native_thread_slots} are configured"
        )
    if requested_temp and coordinator.limits.temp_budget_bytes is None:
        with coordinator._condition:
            materialized_temp = sum(
                active.temp_materialized_bytes
                for active in coordinator._active_reservations.values()
            )
            coordinator.temp_budget_bytes = (
                shutil.disk_usage(tempfile.gettempdir()).free + materialized_temp
            )
    if requested_temp > coordinator.temp_budget_bytes:
        raise MemoryBudgetExceeded(
            f"{route_name} requires {requested_temp} temporary bytes but only "
            f"{coordinator.temp_budget_bytes} are configured"
        )
    if requested_io > coordinator._io_capacity(str(io_device or "default")):
        raise MemoryBudgetExceeded("one request exceeds the device I/O concurrency limit")
    if requested_gpu > coordinator._gpu_capacity.get(str(gpu_device or "default"), 0):
        raise MemoryBudgetExceeded("GPU device is unregistered or its memory budget is exceeded")

    normalized_phase: str | None
    if phase is None:
        normalized_phase = None
    else:
        normalized_phase = str(phase).strip()
        if not normalized_phase:
            raise ValueError("global resource phase cannot be empty")
    normalized_resident_key: str | None
    if resident_key is None:
        normalized_resident_key = None
    else:
        normalized_resident_key = str(resident_key).strip()
        if not normalized_resident_key:
            raise ValueError("global resident key cannot be empty")

    started = coordinator._clock()
    request = _Request(
        route_name,
        requested_memory,
        requested_cpu,
        started,
        int(started * 1_000_000_000),
        resident_bytes=requested_resident,
        transient_bytes=requested_transient,
        temp_bytes=requested_temp,
        native_threads=requested_native,
        phase=normalized_phase,
        resident_key=normalized_resident_key,
        io_slots=requested_io,
        io_device=None if io_device is None else str(io_device),
        gpu_bytes=requested_gpu,
        gpu_device=None if gpu_device is None else str(gpu_device),
        draining_from=_draining_from,
        adaptive_native_threads=_adaptive_native_threads,
        native_thread_limit=_native_thread_limit,
    )
    route_index = coordinator.route_order.index(route_name)
    headroom_blocked_since: float | None = None
    try:
        with coordinator._condition:
            if _renewing_from is not None and (
                coordinator._active_reservations.get(id(_renewing_from)) is not _renewing_from
                or _renewing_from.route_name != route_name
            ):
                raise RuntimeError("execution renewal requires its original live lease")
            if _draining_from is not None:
                if (
                    coordinator._active_reservations.get(id(_draining_from)) is not _draining_from
                    or _draining_from.route_name != route_name
                    or requested_memory
                    or requested_temp
                    or requested_gpu
                    or max(requested_cpu, requested_native, requested_io) > 1
                ):
                    raise ValueError(
                        "drain admission must finalize a live lease without resource growth"
                    )
                coordinator._queues[route_name].appendleft(request)
            else:
                coordinator._queues[route_name].append(request)
            request.queued = True
            coordinator._condition.notify_all()
            while True:
                if coordinator._checkpoint_callback is not None:
                    coordinator._checkpoint_callback()
                if cancellation is not None:
                    cancellation.checkpoint()
                if coordinator.cancellation.is_cancelled or (
                    cancellation is not None and cancellation.is_cancelled
                ):
                    raise CancellationRequested(
                        f"{route_name} cancelled while waiting for global resources"
                    )
                if (
                    _renewing_from is not None
                    and coordinator._active_reservations.get(id(_renewing_from))
                    is not _renewing_from
                ):
                    raise RuntimeError("original lease was released while renewing execution")
                snapshot, effective_cpu_slots = coordinator._observe_live_resources()
                selected_route = coordinator._next_route(snapshot, effective_cpu_slots)
                if (
                    selected_route == route_name
                    and coordinator._queues[route_name]
                    and coordinator._queues[route_name][0] is request
                ):
                    coordinator.cancellation.checkpoint()
                    if cancellation is not None:
                        cancellation.checkpoint()
                    coordinator._grant_request_locked(request, route_index)
                    coordinator._condition.notify_all()
                    break

                if not request.waited:
                    request.waited = True
                    coordinator._metrics[route_name].waits += 1

                # Active bounded jobs own resources legitimately. Apply the
                # resource-wait timeout only while no work is active and no
                # queued request can start. Worker execution timeouts are
                # owned by their route and are never conflated here.
                remaining: float | None = None
                if (
                    selected_route is None
                    and coordinator._active_execution_requests == 0
                    and coordinator.limits.wait_timeout_seconds is not None
                ):
                    now = coordinator._clock()
                    if headroom_blocked_since is None:
                        headroom_blocked_since = now
                    remaining = coordinator.limits.wait_timeout_seconds - (
                        now - headroom_blocked_since
                    )
                    if remaining <= 0:
                        route_memory_blocked = route_budget is not None and (
                            coordinator._metrics[route_name].reserved_bytes
                            + coordinator._resident_charge_for_request_locked(request)
                            + request.transient_bytes
                            > route_budget
                        )
                        reason = (
                            "memory"
                            if coordinator._memory_pressure or route_memory_blocked
                            else "cpu"
                        )
                        raise ResourceWaitTimeout(
                            f"{route_name} timed out waiting for live system "
                            f"headroom; available_physical="
                            f"{snapshot.available_physical}, "
                            f"available_commit={snapshot.available_commit}, "
                            f"reserved={coordinator._reserved_bytes}, "
                            f"cpu_in_use={coordinator._cpu_in_use}, "
                            f"cpu_load={coordinator._last_cpu_load}, "
                            f"effective_cpu_slots="
                            f"{coordinator._last_effective_cpu_slots}, "
                            f"resident={coordinator._resident_bytes}, "
                            f"transient={coordinator._transient_bytes}, "
                            f"temp={coordinator._temp_bytes}, "
                            f"native_threads={coordinator._native_threads_in_use}",
                            reason=reason,
                        )
                else:
                    headroom_blocked_since = None
                wait_seconds = coordinator.limits.poll_interval_seconds
                if cancellation is not None:
                    wait_seconds = min(wait_seconds, 0.1)
                if remaining is not None:
                    wait_seconds = min(wait_seconds, remaining)
                coordinator._condition.wait(wait_seconds)
    except BaseException as admission_error:
        coordinator._cleanup_request(request, admission_error)
        raise

    primary_error: BaseException | None = None
    grant = ResourceGrant(coordinator, request, cancellation)
    grant_token = _CURRENT_RESOURCE_GRANT.set(grant)
    try:
        yield grant
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            try:
                grant.release_cpu()
            except BaseException as exc:
                cleanup_error = exc
            finally:
                if request.admitted and not request.released:
                    coordinator._cleanup_request(request, primary_error or cleanup_error)
        finally:
            _CURRENT_RESOURCE_GRANT.reset(grant_token)
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            primary_error.add_note(f"execution release failed: {cleanup_error}")
