"""Owned compute yields scheduling priority without changing its caller."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path

import pytest

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    resource_grant_scope,
)
from neocortex.runtime.control.isolated_process import (
    isolated_spawn_process,
    terminate_isolated_process,
)
from neocortex.runtime.control.memory_runtime import MemorySnapshot


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux worker scheduling")


def _priority_report(connection) -> None:
    from neocortex.runtime.control.worker_priority import current_worker_priority

    status = current_worker_priority()
    connection.send((os.getpid(), os.getpriority(os.PRIO_PROCESS, 0), status.applied))
    connection.close()


def _coordinator() -> GlobalResourceCoordinator:
    capacity = 64 * 1024 * 1024
    return GlobalResourceCoordinator(
        ("native",),
        GlobalResourceLimits(
            cpu_slots=1, memory_budget_bytes=capacity,
            min_free_memory_bytes=0, min_free_commit_bytes=0,
        ),
        cpu_load_probe=lambda: 0.0,
        resource_probe=lambda: MemorySnapshot(capacity, capacity, capacity, capacity),
    )


def test_coordinated_native_priority_preserves_cwd_environment_and_parent(tmp_path: Path) -> None:
    before = os.getpriority(os.PRIO_PROCESS, 0)
    environment = {"NEOCORTEX_PRIORITY_FIXTURE": "controlled"}
    command = (
        sys.executable, "-c",
        "import json,os; print(json.dumps({'nice':os.getpriority(os.PRIO_PROCESS,0),"
        "'cwd':os.getcwd(),'marker':os.environ['NEOCORTEX_PRIORITY_FIXTURE'],"
        "'has_pythonpath':'PYTHONPATH' in os.environ}))",
    )
    coordinator = _coordinator()
    with coordinator.admit("native", 1024, cpu_slots=1, native_threads=1) as grant:
        with resource_grant_scope(grant):
            completed = run_bounded_capture(
                command, cwd=tmp_path, environment=environment,
                timeout_seconds=5, stdout_limit_bytes=2048, stderr_limit_bytes=2048,
            )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "nice": max(before, 10), "cwd": str(tmp_path),
        "marker": "controlled", "has_pythonpath": False,
    }
    assert os.getpriority(os.PRIO_PROCESS, 0) == before
    assert environment == {"NEOCORTEX_PRIORITY_FIXTURE": "controlled"}
    assert coordinator.summary().cpu_slots_in_use == 0


def test_coordinated_missing_executable_preserves_popen_error(tmp_path: Path) -> None:
    coordinator = _coordinator()
    with coordinator.admit("native", 1, cpu_slots=1, native_threads=1) as grant:
        with resource_grant_scope(grant), pytest.raises(FileNotFoundError):
            run_bounded_capture(
                (str(tmp_path / "missing-native-tool"),), timeout_seconds=5,
                stdout_limit_bytes=1024, stderr_limit_bytes=1024,
            )
    assert coordinator.summary().cpu_slots_in_use == 0


def test_isolated_worker_lowers_its_own_priority_and_reports_actual_status() -> None:
    before = os.getpriority(os.PRIO_PROCESS, 0)
    reader, writer = multiprocessing.get_context("spawn").Pipe(duplex=False)
    process = isolated_spawn_process(target=_priority_report, args=(writer,))
    try:
        process.start()
        writer.close()
        assert reader.poll(5), "isolated worker did not report its scheduling state"
        pid, nice, applied = reader.recv()
        assert pid != os.getpid()
        assert nice == max(before, 10) and applied
        process.join(5)
        assert process.exitcode == 0
    finally:
        reader.close()
        writer.close()
        terminate_isolated_process(process)
        process.close()
    assert os.getpriority(os.PRIO_PROCESS, 0) == before
