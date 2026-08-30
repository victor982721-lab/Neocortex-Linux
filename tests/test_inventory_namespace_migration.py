"""Compatibility contracts for canonical inventory integrations."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_ROOT = PROJECT_ROOT / "neocortex" / "integrations" / "inventory"
MODULES = ("inventory_boundary", "inventory_coordinator", "reconcile")


def test_legacy_inventory_modules_are_exact_product_aliases() -> None:
    for name in MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.integrations.inventory.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert Path(product.__file__).resolve().is_relative_to(INVENTORY_ROOT)


def test_inventory_packages_remain_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.integrations")
importlib.import_module("neocortex.integrations.inventory")
forbidden = {
    "neocortex.integrations.inventory.inventory_boundary",
    "neocortex.integrations.inventory.inventory_coordinator",
    "neocortex.integrations.inventory.reconcile",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("inventory loaded eagerly: " + ",".join(loaded))
print("INVENTORY_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "INVENTORY_IMPORT_LIGHT"
