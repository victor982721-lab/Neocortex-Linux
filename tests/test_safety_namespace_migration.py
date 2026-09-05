"""Compatibility contracts for canonical safety modules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


TEST_CAPABILITIES = ("base", "image")


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


@pytest.mark.parametrize("name", [
    pytest.param(name, marks=pytest.mark.capability("image"))
    if name == "ocr_image_preprocess" else name for name in MODULES
])
def test_safety_modules_are_owned_by_the_canonical_tree(name: str) -> None:
    product = __import__(f"neocortex.safety.{name}", fromlist=[name])
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
