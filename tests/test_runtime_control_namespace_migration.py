"""Compatibility contracts for canonical runtime control modules."""

from __future__ import annotations

import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "neocortex" / "runtime" / "control"

MODULES = (
    "bounded_subprocess",
    "cancellation",
    "console_cancellation",
    "cpu_runtime",
    "global_resources",
    "isolated_process",
    "locking",
    "memory_runtime",
    "retry_policy",
    "watcher",
    "watcher_life_lease",
)


def test_runtime_control_modules_are_owned_by_the_canonical_tree() -> None:
    for name in MODULES:
        product = __import__(f"neocortex.runtime.control.{name}", fromlist=[name])
        assert Path(product.__file__).resolve().is_relative_to(CONTROL_ROOT)


def test_runtime_control_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.runtime.control")
forbidden = {
    "neocortex.runtime.control.bounded_subprocess",
    "neocortex.runtime.control.cancellation",
    "neocortex.runtime.control.global_resources",
    "neocortex.runtime.control.watcher",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("runtime control loaded eagerly: " + ",".join(loaded))
print("RUNTIME_CONTROL_IMPORT_LIGHT")
"""
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "RUNTIME_CONTROL_IMPORT_LIGHT"


def test_runtime_control_symbols_keep_historical_pickle_fqns() -> None:
    cancellation = __import__("neocortex.runtime.control.cancellation", fromlist=["cancellation"])
    resources = __import__("neocortex.runtime.control.global_resources", fromlist=["resources"])
    memory = __import__("neocortex.runtime.control.memory_runtime", fromlist=["memory"])
    lease = __import__("neocortex.runtime.control.watcher_life_lease", fromlist=["lease"])

    values = (
        cancellation.CancellationRequested("fixture"),
        resources.GlobalResourceLimits(1, 2, 3, 4, 5.0, 6.0),
        memory.MemorySnapshot(1, 2, 3, 4),
        lease.WatcherLeaseIdentity("fixture", "fixture", "fixture"),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        if isinstance(value, BaseException):
            assert str(restored) == str(value)
        else:
            assert restored == value

    assert cancellation.CancellationRequested.__module__ == (
        "neocortex.runtime.control.cancellation"
    )
    assert resources.GlobalResourceLimits.__module__ == (
        "neocortex.runtime.control.global_resources"
    )
    assert memory.MemorySnapshot.__module__ == "neocortex.runtime.control.memory_runtime"
    assert lease.WatcherLeaseIdentity.__module__ == (
        "neocortex.runtime.control.watcher_life_lease"
    )
