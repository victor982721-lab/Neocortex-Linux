"""Compatibility contracts for canonical safety modules."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFETY_ROOT = PROJECT_ROOT / "neocortex" / "safety"
MODULES = (
    "corpus_access",
    "internal_paths",
    "ocr_image_preprocess",
    "ocr_profiles",
    "protected_content",
    "route_filters",
    "state_topology_contracts",
    "windows_handle_mutation",
)


def test_legacy_safety_modules_are_exact_product_aliases() -> None:
    for name in MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.safety.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert sys.modules[f"neocortex.safety.{name}"] is product
        assert Path(product.__file__).resolve().is_relative_to(SAFETY_ROOT)


def test_safety_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.safety")
forbidden = {
    "neocortex.safety.corpus_access",
    "neocortex.safety.internal_paths",
    "neocortex.safety.state_topology_contracts",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("safety loaded eagerly: " + ",".join(loaded))
print("SAFETY_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "SAFETY_IMPORT_LIGHT"
