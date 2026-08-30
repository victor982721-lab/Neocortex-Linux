"""Compatibility contracts for canonical self-analysis workflow modules."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SELF_ANALYSIS_ROOT = PROJECT_ROOT / "neocortex" / "workflow" / "self_analysis"

MODULES = (
    "self_analysis",
    "self_analysis_finalization",
    "self_analysis_freshness",
    "self_analysis_manifest",
    "self_analysis_status",
)


def test_legacy_self_analysis_modules_are_exact_product_aliases() -> None:
    for name in MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.workflow.self_analysis.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert sys.modules[f"neocortex.workflow.self_analysis.{name}"] is product
        assert Path(product.__file__).resolve().is_relative_to(SELF_ANALYSIS_ROOT)


def test_self_analysis_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.workflow.self_analysis")
forbidden = {
    "neocortex.workflow.self_analysis.self_analysis",
    "neocortex.workflow.self_analysis.self_analysis_manifest",
    "neocortex.workflow.self_analysis.self_analysis_status",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("self-analysis loaded eagerly: " + ",".join(loaded))
print("SELF_ANALYSIS_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "SELF_ANALYSIS_IMPORT_LIGHT"


def test_self_analysis_status_exports_are_available_through_canonical_module() -> None:
    product = importlib.import_module("neocortex.workflow.self_analysis.self_analysis_status")
    legacy = importlib.import_module("_04_Nucleo_Operativo.self_analysis_status")

    for name in (
        "SelfAnalysisFreshness",
        "ManifestStatus",
        "read_self_analysis_status",
    ):
        assert getattr(product, name) is getattr(legacy, name)
