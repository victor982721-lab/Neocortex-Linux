"""Renewed execution follows a live lease across its sequential owners."""

from __future__ import annotations

from contextlib import nullcontext
from contextvars import ContextVar

import pytest

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.elastic_workers import elastic_map
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    current_resource_grant,
)


def _coordinator(*, checkpoint=None):
    return GlobalResourceCoordinator(
        ("pdf", "outer"),
        GlobalResourceLimits(
            memory_budget_bytes=1000, min_free_memory_bytes=0, min_free_commit_bytes=0,
            cpu_slots=4, native_thread_slots=4, poll_interval_seconds=0.01,
            wait_timeout_seconds=0.1,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=10000, total_physical=20000,
            effective_cpu_capacity=4, external_cpu_cores=0,
        ),
        checkpoint=checkpoint,
    )


@pytest.mark.parametrize("nested", [False, True])
def test_prepare_worker_and_consumer_renew_one_lease_and_restore_owner_context(nested):
    coordinator = _coordinator()
    gate = CoordinatedMemoryGate(coordinator, "pdf")
    inherited: ContextVar[str | None] = ContextVar("inherited_operation", default=None)
    token = inherited.set("owner operation")
    seen = []

    def checkpoint(phase, value):
        grant = current_resource_grant()
        assert grant is not None
        assert inherited.get() == "owner operation"
        grant.checkpoint()
        assert current_resource_grant() is grant
        seen.append((phase, value))
        return value

    try:
        with (coordinator.admit("outer", 10) if nested else nullcontext()) as outer:
            assert current_resource_grant() is outer
            with elastic_map(
                lambda value: checkpoint("worker", value), [1, 2],
                gate=gate, estimated_bytes=20, max_workers=1,
                prepare=lambda value: checkpoint("prepare", value), poll_interval=0.01,
            ) as results:
                for value in results:
                    checkpoint("consumer", value)
            assert current_resource_grant() is outer
            assert coordinator.summary().transient_bytes == (10 if nested else 0)
    finally:
        inherited.reset(token)
    assert current_resource_grant() is None
    assert seen == [
        ("prepare", 1), ("worker", 1), ("consumer", 1),
        ("prepare", 2), ("worker", 2), ("consumer", 2),
    ]
    assert coordinator.summary().active_execution_requests == 0
    assert coordinator.summary().transient_bytes == 0


@pytest.mark.parametrize("failure_kind", ["cancel", "deadline"])
def test_failed_renewal_preserves_owner_context_and_original_error(failure_kind):
    failure = RuntimeError("deadline reached during renewal")
    expired = False

    def deadline():
        if expired:
            raise failure

    coordinator = _coordinator(checkpoint=deadline)
    cancellation = CancellationToken()
    gate = CoordinatedMemoryGate(coordinator, "pdf", cancellation=cancellation)

    def prepare(value):
        nonlocal expired
        grant = current_resource_grant()
        assert grant is not None
        grant.checkpoint()
        if failure_kind == "cancel":
            cancellation.cancel()
        else:
            expired = True
        grant.checkpoint()
        return value

    with pytest.raises(CancellationRequested if failure_kind == "cancel" else RuntimeError) as raised:
        with elastic_map(
            lambda value: value, [1], gate=gate, estimated_bytes=20,
            max_workers=1, prepare=prepare, poll_interval=0.01,
        ) as results:
            list(results)
    if failure_kind == "deadline":
        assert raised.value is failure
    assert not any("different Context" in note for note in getattr(raised.value, "__notes__", ()))
    assert current_resource_grant() is None
    assert coordinator.summary().active_execution_requests == 0
    assert coordinator.summary().transient_bytes == 0


def test_worker_failure_after_preparation_renewal_preserves_primary_error():
    coordinator = _coordinator()
    gate = CoordinatedMemoryGate(coordinator, "pdf")
    failure = ValueError("parser failed after preparation")

    def prepare(value):
        grant = current_resource_grant()
        assert grant is not None
        grant.checkpoint()
        return value

    def worker(_value):
        raise failure

    with pytest.raises(ValueError) as raised:
        with elastic_map(
            worker, [1], gate=gate, estimated_bytes=20, max_workers=1,
            prepare=prepare, poll_interval=0.01,
        ) as results:
            list(results)
    assert raised.value is failure
    assert current_resource_grant() is None
    assert coordinator.summary().active_execution_requests == 0
    assert coordinator.summary().transient_bytes == 0
