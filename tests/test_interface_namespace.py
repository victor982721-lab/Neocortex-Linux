"""The installed interface boundary is CLI-only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_interface_root_is_import_light_and_cli_owned() -> None:
    script = """
import sys
from neocortex import interface
import neocortex.interface.entrypoint as entrypoint_module

assert interface.__all__ == []
assert interface.entrypoint is entrypoint_module
assert callable(entrypoint_module.entrypoint)
assert not any(name.startswith("neocortex.interface.presentation") for name in sys.modules)
assert not any(name.startswith("neocortex.interface.") and name != "neocortex.interface.entrypoint"
               for name in sys.modules)
"""
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPOSITORY_ROOT),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_cli_entrypoint_runs_without_desktop_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    from neocortex.api.cli import cli_app
    from neocortex.interface.entrypoint import entrypoint

    monkeypatch.setattr(
        cli_app,
        "main",
        lambda arguments: 17 if arguments == ["--version"] else 0,
    )
    assert entrypoint(("--version",)) == 17


def test_desktop_subsystems_and_assets_are_extinct() -> None:
    for relative in (
        "neocortex/interface/application",
        "neocortex/interface/presentation",
        "neocortex/interface/protocol",
        "neocortex/interface/read",
    ):
        assert not (REPOSITORY_ROOT / relative).exists()
    assert not any(
        path.name.startswith("neocortex-app-icon")
        for path in (REPOSITORY_ROOT / "neocortex").rglob("*")
        if path.is_file()
    )


def test_numbered_interface_root_is_extinct() -> None:
    legacy = "_05" + "_Interfaz"

    assert not (REPOSITORY_ROOT / legacy).exists()
    for relative in ("neocortex", "tools"):
        for path in (REPOSITORY_ROOT / relative).rglob("*.py"):
            assert legacy not in path.read_text(encoding="utf-8"), path
