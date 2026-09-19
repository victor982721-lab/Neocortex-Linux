"""A retained inventory projection owns resources without lending its context."""

from contextlib import nullcontext
from contextvars import Context, copy_context
from functools import wraps
from types import SimpleNamespace
from typing import Any

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    current_resource_grant,
    resource_scope,
)
from neocortex.runtime.orchestration import inventory_projection


MIB = 1024**2


def _fresh_context(function):
    """Prevent the deliberately leaking baseline from contaminating another case."""
    @wraps(function)
    def invoke(*args, **kwargs):
        return Context().run(function, *args, **kwargs)
    return invoke


def _coordinator():
    return GlobalResourceCoordinator(
        ("preparation", "other", "parent"),
        GlobalResourceLimits(
            memory_budget_bytes=64 * MIB,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=1,
            native_thread_slots=1,
            io_slots=1,
            temp_budget_bytes=4 * MIB,
            poll_interval_seconds=0.01,
            wait_timeout_seconds=0.2,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=8 * 1024**3,
            total_physical=8 * 1024**3,
            effective_cpu_capacity=1,
            external_cpu_cores=0,
        ),
        effective_cpu_probe=lambda: 1,
    )


def _snapshot(number=1):
    return FileSnapshot(f"/synthetic/project/unit_{number}.py", 1, number, 100, 1, -1)


def _assert_released(coordinator):
    summary = coordinator.summary()
    assert summary.resident_bytes == summary.transient_bytes == summary.temp_bytes == 0
    assert summary.cpu_slots_in_use == summary.native_threads == summary.io_slots == 0
    assert summary.active_execution_requests == 0


@pytest.mark.parametrize("nested", (False, True))
@_fresh_context
def test_projection_preserves_ambient_grant_and_leaves_one_cpu_available(nested):
    coordinator = _coordinator()
    with resource_scope(coordinator):
        parent_scope = (
            coordinator.admit("parent", MIB, cpu_slots=0, native_threads=0, io_slots=0)
            if nested else nullcontext()
        )
        with parent_scope as parent:
            projection = inventory_projection.build_code_inventory_projection(
                SimpleNamespace(snapshots=lambda _scan_id: (_snapshot(),)), 1,
            )
            try:
                assert current_resource_grant() is parent
                assert tuple(projection.snapshots(1)) == (_snapshot(),)
                summary = coordinator.summary()
                assert summary.transient_bytes == (9 if nested else 8) * MIB
                assert summary.cpu_slots_in_use == summary.native_threads == summary.io_slots == 0
                # A downstream PDF checkpoint must not renew the retained
                # projection before trying to use the sole execution slot.
                ambient = current_resource_grant()
                if ambient is not None:
                    ambient.checkpoint()
                with coordinator.admit("other", MIB) as other:
                    assert current_resource_grant() is other
                    assert coordinator.summary().cpu_slots_in_use == 1
                assert current_resource_grant() is parent
            finally:
                projection.close()
            assert current_resource_grant() is parent
            assert coordinator.summary().transient_bytes == (MIB if nested else 0)
    _assert_released(coordinator)


@_fresh_context
def test_spooled_projection_retains_resources_until_close_in_another_context(monkeypatch):
    monkeypatch.setattr(inventory_projection, "_MEMORY_RECORDS", 0)
    coordinator = _coordinator()
    with resource_scope(coordinator):
        projection = inventory_projection.build_code_inventory_projection(
            SimpleNamespace(snapshots=lambda _scan_id: (_snapshot(), _snapshot(2))), 1,
        )
        spool = projection._spool
        try:
            assert spool is not None
            assert not bool(spool.closed)
            assert tuple(projection.snapshots(1)) == (_snapshot(), _snapshot(2))
            summary = coordinator.summary()
            assert summary.transient_bytes == 8 * MIB and summary.temp_bytes > 0
            assert summary.cpu_slots_in_use == summary.native_threads == summary.io_slots == 0
            copy_context().run(projection.close)
            assert current_resource_grant() is None
            assert spool.closed
            _assert_released(coordinator)
            projection.close()
            copy_context().run(projection.close)
            _assert_released(coordinator)
        finally:
            projection.close()


@pytest.mark.parametrize("failure_kind", ("source", "cancel"))
@_fresh_context
def test_projection_failure_preserves_error_and_releases_memory_and_spool(monkeypatch, failure_kind):
    monkeypatch.setattr(inventory_projection, "_MEMORY_RECORDS", 0)
    temporary_file = inventory_projection.tempfile.TemporaryFile
    opened: list[Any] = []

    def observed_temporary(*args, **kwargs):
        spool = temporary_file(*args, **kwargs)
        opened.append(spool)
        return spool

    monkeypatch.setattr(inventory_projection.tempfile, "TemporaryFile", observed_temporary)
    cancellation = CancellationToken()
    failure = RuntimeError("inventory source failed")

    def snapshots(_scan_id):
        yield _snapshot()
        if failure_kind == "source":
            raise failure
        cancellation.cancel()
        yield _snapshot(2)

    coordinator = _coordinator()
    with resource_scope(coordinator):
        with pytest.raises(RuntimeError if failure_kind == "source" else CancellationRequested) as raised:
            inventory_projection.build_code_inventory_projection(
                SimpleNamespace(snapshots=snapshots), 1, cancellation=cancellation,
            )
        if failure_kind == "source":
            assert raised.value is failure
        assert "different Context" not in str(raised.value)
        assert current_resource_grant() is None
        assert opened and all(spool.closed for spool in opened)
        _assert_released(coordinator)
