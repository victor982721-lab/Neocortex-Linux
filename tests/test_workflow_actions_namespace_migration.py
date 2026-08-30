"""Compatibility contracts for canonical workflow action modules."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTIONS_ROOT = PROJECT_ROOT / "neocortex" / "workflow" / "actions"

MODULES = (
    "action_policy",
    "actions",
    "file_action_reconciliation_store",
    "file_action_recovery",
)


def test_legacy_workflow_action_modules_are_exact_product_aliases() -> None:
    for name in MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.workflow.actions.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert sys.modules[f"neocortex.workflow.actions.{name}"] is product
        assert Path(product.__file__).resolve().is_relative_to(ACTIONS_ROOT)


def test_workflow_action_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.workflow")
importlib.import_module("neocortex.workflow.actions")
forbidden = {
    "neocortex.workflow.actions.action_policy",
    "neocortex.workflow.actions.actions",
    "neocortex.workflow.actions.file_action_recovery",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("workflow actions loaded eagerly: " + ",".join(loaded))
print("WORKFLOW_ACTIONS_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "WORKFLOW_ACTIONS_IMPORT_LIGHT"


def test_action_policy_and_reconciliation_keep_historical_pickle_fqns() -> None:
    policy = importlib.import_module("neocortex.workflow.actions.action_policy")
    recovery = importlib.import_module("neocortex.workflow.actions.file_action_recovery")

    action = recovery.FileActionReconciliation(
        action_id=1,
        run_id=2,
        idempotency_key="fixture-key",
        action_type="rename",
        source_path="/tmp/source",
        target_path="/tmp/target",
        recorded_status="applying",
        reconciler_signature="fixture-signature",
        classification="stale",
        recommendation="manual_review",
        detail="fixture",
    )
    restored = pickle.loads(pickle.dumps(action, protocol=5))
    assert type(restored) is type(action)
    assert restored == action
    assert policy.__file__
    assert action.__class__.__module__ == "_04_Nucleo_Operativo.file_action_recovery"
