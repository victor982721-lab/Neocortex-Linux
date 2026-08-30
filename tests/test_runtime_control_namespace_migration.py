"""Compatibility contracts for canonical runtime control modules."""

from __future__ import annotations

import importlib
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
    "incremental_gate",
    "isolated_process",
    "locking",
    "memory_runtime",
    "retry_policy",
    "watcher",
    "watcher_life_lease",
)


def test_legacy_runtime_control_modules_are_exact_product_aliases() -> None:
    for name in MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.runtime.control.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert sys.modules[f"neocortex.runtime.control.{name}"] is product
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
    cancellation = importlib.import_module("neocortex.runtime.control.cancellation")
    resources = importlib.import_module("neocortex.runtime.control.global_resources")
    memory = importlib.import_module("neocortex.runtime.control.memory_runtime")
    lease = importlib.import_module("neocortex.runtime.control.watcher_life_lease")

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
        "_04_Nucleo_Operativo.cancellation"
    )
    assert resources.GlobalResourceLimits.__module__ == (
        "_04_Nucleo_Operativo.global_resources"
    )
    assert memory.MemorySnapshot.__module__ == "_04_Nucleo_Operativo.memory_runtime"
    assert lease.WatcherLeaseIdentity.__module__ == (
        "_04_Nucleo_Operativo.watcher_life_lease"
    )
