"""Compatibility contracts for canonical workflow review modules."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVIEW_ROOT = PROJECT_ROOT / "neocortex" / "workflow" / "review"

MODULES = (
    "review",
    "review_evidence",
    "review_task_contracts",
    "review_task_repository",
    "value_review",
    "value_review_contracts",
    "value_review_port",
    "value_review_repository",
    "value_review_tasks",
)


def test_legacy_review_modules_are_exact_product_aliases() -> None:
    for name in MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.workflow.review.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert sys.modules[f"neocortex.workflow.review.{name}"] is product
        assert Path(product.__file__).resolve().is_relative_to(REVIEW_ROOT)


def test_workflow_review_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.workflow.review")
forbidden = {
    "neocortex.workflow.review.review",
    "neocortex.workflow.review.review_task_repository",
    "neocortex.workflow.review.value_review_repository",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("workflow review loaded eagerly: " + ",".join(loaded))
print("WORKFLOW_REVIEW_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "WORKFLOW_REVIEW_IMPORT_LIGHT"


def test_review_models_keep_historical_pickle_fqns() -> None:
    evidence = importlib.import_module("neocortex.workflow.review.review_evidence")
    value_contracts = importlib.import_module(
        "neocortex.workflow.review.value_review_contracts"
    )

    values = (
        evidence.ReviewEvidenceSyncResult(1, 2, 3, True),
        value_contracts.ValueEvidenceFact("fixture", "value"),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        assert restored == value

    assert evidence.ReviewEvidenceSyncResult.__module__ == (
        "_04_Nucleo_Operativo.review_evidence"
    )
    assert value_contracts.ValueEvidenceFact.__module__ == (
        "_04_Nucleo_Operativo.value_review_contracts"
    )
