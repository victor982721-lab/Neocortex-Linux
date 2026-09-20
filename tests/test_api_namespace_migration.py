"""Compatibility contracts for the canonical public API namespace."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "neocortex" / "api"
CLI_ROOT = API_ROOT / "cli"
CLI_MODULES = tuple(sorted(path.stem for path in CLI_ROOT.glob("cli_*.py")))


def test_canonical_cli_modules_are_owned_by_the_api_tree() -> None:
    assert len(CLI_MODULES) == 39
    assert "cli_agent_activity" in CLI_MODULES
    assert "cli_content_diagnostics" in CLI_MODULES
    assert "cli_dedup_keeper" in CLI_MODULES
    assert "cli_hygiene" in CLI_MODULES
    for name in CLI_MODULES:
        module = __import__(f"neocortex.api.cli.{name}", fromlist=[name])
        assert Path(module.__file__).resolve().is_relative_to(CLI_ROOT)


def test_canonical_read_api_port_is_owned_by_the_api_tree() -> None:
    module = __import__("neocortex.api.read_api_port", fromlist=["read_api_port"])
    assert Path(module.__file__).resolve().is_relative_to(API_ROOT)


def test_product_root_contains_only_package_metadata_and_module_entrypoint() -> None:
    """Prevent implementation modules from accumulating at the package root."""

    root = PROJECT_ROOT / "neocortex"
    assert sorted(path.name for path in root.glob("*.py")) == [
        "__init__.py",
        "__main__.py",
    ]
    assert not (PROJECT_ROOT / "_04_Nucleo_Operativo").exists()


def test_api_packages_remain_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.api")
importlib.import_module("neocortex.api.cli")
forbidden = {
    "neocortex.api.cli.cli_app",
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
