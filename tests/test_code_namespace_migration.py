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
        ("code_contracts", "_04_Nucleo_Operativo.code_contracts"),
        ("code_route", "_04_Nucleo_Operativo.code_route"),
        ("code_schema", "_04_Nucleo_Operativo.code_schema"),
        ("code_search", "_04_Nucleo_Operativo.code_search"),
        ("code_change_validation", "_04_Nucleo_Operativo.code_change_validation"),
        ("external_evidence_models", "_04_Nucleo_Operativo.external_evidence_models"),
        (
            "external_evidence_providers",
            "_04_Nucleo_Operativo.external_evidence_providers",
        ),
        ("external_evidence_store", "_04_Nucleo_Operativo.external_evidence_store"),
        ("validation_supply", "_04_Nucleo_Operativo.code.validation_supply"),
    )
    for name, legacy_name in modules:
        legacy = importlib.import_module(legacy_name)
        product = importlib.import_module(f"neocortex.code.{name}")
        assert legacy is product
        assert sys.modules[legacy_name] is product
        assert Path(product.__file__).resolve().is_relative_to(CODE_ROOT)

    for name in ("target_projection", "target_registry"):
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.code.contracts.{name}")
        product = importlib.import_module(f"neocortex.code.contracts.{name}")
        assert legacy is product
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
