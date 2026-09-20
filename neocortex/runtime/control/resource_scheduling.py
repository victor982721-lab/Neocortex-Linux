"""Lock-scoped admission scheduling predicates.

These functions do not own a queue or condition. They are called by the one
coordinator while its condition is held, and only mutate the queued request's
adaptive width as part of the same selection decision.
"""

from __future__ import annotations

import shutil
import tempfile

from .resource_models import _Request


def fits(coordinator, request: _Request, snapshot, effective_cpu_slots: int) -> bool:
    draining = request.draining_from is not None
    if (
        draining
        and coordinator._active_reservations.get(id(request.draining_from))
        is not request.draining_from
    ):
        return False
    resident_charge = coordinator._resident_charge_for_request_locked(request)
    future_memory = coordinator._reserved_bytes + resident_charge + request.transient_bytes
    if not draining and future_memory > coordinator._last_memory_budget:
        return False
    route_budget = coordinator._route_memory_budgets.get(request.route_name)
    if (
        not draining
        and route_budget is not None
        and (
            coordinator._metrics[request.route_name].reserved_bytes
            + resident_charge
            + request.transient_bytes
            > route_budget
        )
    ):
        return False
    execution = request.cpu_slots > 0 or request.native_threads > 0 or request.io_slots > 0
    if not draining and (coordinator._memory_pressure or (coordinator._cpu_pressure and execution)):
        return False
    pressure = coordinator._memory_pressure or coordinator._cpu_pressure or coordinator._io_pressure
    if draining and pressure:
        # One bounded finalizer may make progress to persist/discard
        # already admitted results and return their memory. It reserves
        # no new memory, GPU or temporary space. New work remains paused.
        if any(
            active.draining_from is not None and active.execution_active
            for active in coordinator._active_reservations.values()
        ):
            return False
        effective_cpu_slots = max(1, effective_cpu_slots)
    if coordinator._cpu_in_use + request.cpu_slots > effective_cpu_slots:
        return False
    native_capacity = min(coordinator.native_thread_slots, effective_cpu_slots)
    if native_capacity is not None and (
        coordinator._native_threads_in_use + request.native_threads > native_capacity
    ):
        return False
    if request.temp_bytes + coordinator._temp_bytes > coordinator.temp_budget_bytes:
        return False
    if request.temp_bytes:
        pending = sum(
            max(0, active.temp_bytes - active.temp_materialized_bytes)
            for active in coordinator._active_reservations.values()
        )
        if pending + request.temp_bytes > shutil.disk_usage(tempfile.gettempdir()).free:
            return False
    io_device = request.io_device or "default"
    if request.io_slots:
        if coordinator._io_pressure and not draining:
            return False
        io_capacity = coordinator._io_capacity(io_device)
        if coordinator._io_in_use.get(io_device, 0) + request.io_slots > io_capacity:
            return False
    if request.gpu_bytes:
        gpu_device = request.gpu_device or "default"
        gpu_capacity = coordinator._gpu_capacity.get(gpu_device, 0)
        if coordinator._gpu_in_use.get(gpu_device, 0) + request.gpu_bytes > gpu_capacity:
            return False
        probe = coordinator._gpu_available_probes.get(gpu_device)
        if probe is not None:
            available_gpu = coordinator._gpu_available_locked(gpu_device)
            pending = coordinator._gpu_in_use.get(
                gpu_device, 0
            ) - coordinator._gpu_materialized_credit_locked(gpu_device)
            if available_gpu is None or pending + request.gpu_bytes > available_gpu:
                return False
    if draining:
        return True
    incremental_memory = max(0, future_memory - coordinator._materialized_credit_locked())
    physical_ok = (
        snapshot.available_physical is None
        or snapshot.available_physical >= coordinator.min_free_memory_bytes + incremental_memory
    )
    commit_ok = (
        snapshot.available_commit is None
        or snapshot.available_commit >= coordinator.min_free_commit_bytes + incremental_memory
    )
    return physical_ok and commit_ok


def next_route(coordinator, snapshot, effective_cpu_slots: int) -> str | None:
    route_count = len(coordinator.route_order)
    ordered_routes = tuple(
        coordinator.route_order[(coordinator._last_granted_index + offset) % route_count]
        for offset in range(1, route_count + 1)
    )
    for route_name in ordered_routes:
        if not coordinator._queues[route_name]:
            continue
        request = coordinator._queues[route_name][0]
        if request.adaptive_native_threads:
            # A native batch shares its memory among threads. Choose its
            # execution width from capacity free *now*, including work
            # already held by this same route. A stale pre-queue width
            # must not block useful work after external competition grows.
            available = min(
                effective_cpu_slots - coordinator._cpu_in_use,
                min(coordinator.native_thread_slots, effective_cpu_slots)
                - coordinator._native_threads_in_use,
            )
            if request.native_thread_limit is not None:
                available = min(available, request.native_thread_limit)
            request.cpu_slots = request.native_threads = max(1, available)
    fitting_routes = tuple(
        route_name
        for route_name in ordered_routes
        if coordinator._queues[route_name]
        and coordinator._fits(coordinator._queues[route_name][0], snapshot, effective_cpu_slots)
    )
    for route_name in fitting_routes:
        if coordinator._queues[route_name][0].draining_from is not None:
            return route_name
    # A route that already owns resources must not reacquire the last
    # available capacity ahead of a fitting route that has received none.
    # This prevents a stream of large PDF jobs from starving DOCX/images.
    for route_name in fitting_routes:
        if coordinator._metrics[route_name].cpu_slots == 0:
            return route_name
    if fitting_routes:
        return fitting_routes[0]
    return None
