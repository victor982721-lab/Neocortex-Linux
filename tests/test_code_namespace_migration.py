"""Compatibility contracts for the canonical Code and evidence namespace."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_ROOT = PROJECT_ROOT / "neocortex" / "code"


def test_code_and_evidence_modules_are_exposed_under_neocortex() -> None:
    modules = (
        ("code_contracts", "neocortex.code.code_contracts"),
        ("code_route", "neocortex.code.code_route"),
        ("code_schema", "neocortex.code.code_schema"),
        ("code_search", "neocortex.code.code_search"),
        ("code_change_validation", "neocortex.code.code_change_validation"),
        ("external_evidence_models", "neocortex.code.external_evidence_models"),
        (
            "external_evidence_providers",
            "neocortex.code.external_evidence_providers",
        ),
        ("external_evidence_store", "neocortex.code.external_evidence_store"),
        ("validation_supply", "neocortex.code.validation_supply"),
    )
    for _name, canonical_name in modules:
        product = importlib.import_module(canonical_name)
        assert sys.modules[canonical_name] is product
        assert Path(product.__file__).resolve().is_relative_to(CODE_ROOT)

    for name in ("target_projection", "target_registry"):
        product = importlib.import_module(f"neocortex.code.contracts.{name}")
        assert Path(product.__file__).resolve().is_relative_to(CODE_ROOT)


def test_code_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.code")
forbidden = {
    "neocortex.code.code_contracts",
    "neocortex.code.code_route",
    "neocortex.code.external_evidence_providers",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("code loaded eagerly: " + ",".join(loaded))
print("CODE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "CODE_IMPORT_LIGHT"
