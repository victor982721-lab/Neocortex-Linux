"""Compatibility contracts for the canonical public API namespace."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "neocortex" / "api"
CLI_ROOT = API_ROOT / "cli"
CLI_MODULES = tuple(sorted(path.stem for path in CLI_ROOT.glob("cli_*.py")))


def test_legacy_cli_modules_are_exact_product_aliases() -> None:
    assert len(CLI_MODULES) == 31
    for name in CLI_MODULES:
        legacy = importlib.import_module(f"_04_Nucleo_Operativo.{name}")
        product = importlib.import_module(f"neocortex.api.cli.{name}")

        assert legacy is product
        assert sys.modules[f"_04_Nucleo_Operativo.{name}"] is product
        assert sys.modules[f"neocortex.api.cli.{name}"] is product
        assert Path(product.__file__).resolve().is_relative_to(CLI_ROOT)


def test_legacy_read_api_port_is_exact_product_alias() -> None:
    legacy = importlib.import_module("_04_Nucleo_Operativo.read_api_port")
    product = importlib.import_module("neocortex.api.read_api_port")

    assert legacy is product
    assert sys.modules["_04_Nucleo_Operativo.read_api_port"] is product
    assert Path(product.__file__).resolve().is_relative_to(API_ROOT)


def test_api_packages_remain_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.api")
importlib.import_module("neocortex.api.cli")
forbidden = {
    "neocortex.api.cli.cli_app",
    "neocortex.api.cli.cli_code",
    "neocortex.api.read_api_port",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("api loaded eagerly: " + ",".join(loaded))
print("API_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "API_IMPORT_LIGHT"
