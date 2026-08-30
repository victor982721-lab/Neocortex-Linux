"""Compatibility contracts for canonical runtime orchestration modules."""

from __future__ import annotations

import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION_ROOT = PROJECT_ROOT / "neocortex" / "runtime" / "orchestration"

MODULES = (
    "orchestrator",
    "route_registry",
    "route_selection",
    "run_lifecycle",
    "run_status",
)


def _noop(_context: object) -> None:
    return None


def test_orchestration_modules_are_owned_by_the_canonical_tree() -> None:
    for name in MODULES:
        product = __import__(f"neocortex.runtime.orchestration.{name}", fromlist=[name])
        assert Path(product.__file__).resolve().is_relative_to(ORCHESTRATION_ROOT)


def test_runtime_orchestration_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.runtime.orchestration")
forbidden = {
    "neocortex.runtime.orchestration.orchestrator",
    "neocortex.runtime.orchestration.route_registry",
    "neocortex.runtime.orchestration.route_selection",
    "neocortex.runtime.orchestration.run_lifecycle",
    "neocortex.runtime.orchestration.run_status",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("runtime orchestration loaded eagerly: " + ",".join(loaded))
print("RUNTIME_ORCHESTRATION_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "RUNTIME_ORCHESTRATION_IMPORT_LIGHT"


def test_orchestration_status_models_keep_historical_pickle_fqns() -> None:
    registry = __import__("neocortex.runtime.orchestration.route_registry", fromlist=["registry"])
    status = __import__("neocortex.runtime.orchestration.run_status", fromlist=["status"])

    adapter = registry.RouteAdapter("fixture", _noop)
    phase = status.PhaseStatus("fixture", "phase", "complete", 1, 2, None)

    for value in (adapter, phase):
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        if isinstance(value, registry.RouteAdapter):
            assert restored.name == value.name
            assert restored.input_source == value.input_source
        else:
            assert restored == value

    assert registry.RouteAdapter.__module__ == "neocortex.runtime.orchestration.route_registry"
    assert status.PhaseStatus.__module__ == "neocortex.runtime.orchestration.run_status"
