"""Compatibility contracts for canonical workflow retention planning."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETENTION_ROOT = PROJECT_ROOT / "neocortex" / "workflow" / "retention"


def test_retention_module_is_owned_by_the_canonical_tree() -> None:
    product = importlib.import_module("neocortex.workflow.retention.planner")
    assert Path(product.__file__).resolve().is_relative_to(RETENTION_ROOT)


def test_retention_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.workflow.retention")
if "neocortex.workflow.retention.planner" in sys.modules:
    raise SystemExit("retention planner loaded eagerly")
print("RETENTION_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "RETENTION_IMPORT_LIGHT"


def test_retention_models_keep_historical_pickle_fqns() -> None:
    module = importlib.import_module("neocortex.workflow.retention.planner")
    policy = module.RetentionPolicy()

    restored = pickle.loads(pickle.dumps(policy, protocol=5))
    assert type(restored) is type(policy)
    assert restored == policy
    assert module.RetentionPolicy.__module__ == "neocortex.workflow.retention.planner"
