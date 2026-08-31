"""Canonical interface ownership after numbered-root retirement."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import neocortex.interface as interface

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_root_is_import_light() -> None:
    script = """
import sys
import neocortex.interface as interface

assert interface.__all__ == ["main"]
assert not any(name == "PySide6" or name.startswith("PySide6.") for name in sys.modules)
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


def test_responsibility_owned_leaf_modules_are_importable() -> None:
    modules = (
        "neocortex.interface.application.app",
        "neocortex.interface.application.controller",
        "neocortex.interface.application.request",
        "neocortex.interface.presentation.assets",
        "neocortex.interface.presentation.theme",
        "neocortex.interface.presentation.widgets",
        "neocortex.interface.presentation.windows.main",
        "neocortex.interface.presentation.windows.pages",
        "neocortex.interface.protocol.messages",
        "neocortex.interface.protocol.worker",
        "neocortex.interface.read.client",
        "neocortex.interface.read.issues",
        "neocortex.interface.read.models",
        "neocortex.interface.read.presentation",
        "neocortex.interface.read.status",
        "neocortex.interface.read.tasks",
    )

    assert all(importlib.import_module(name).__name__ == name for name in modules)


def test_public_objects_are_owned_by_canonical_modules() -> None:
    from neocortex.interface.application import RunRequest, WorkerController
    from neocortex.interface.presentation import MainWindow
    from neocortex.interface.read import ReadRequest, SharedReadClient

    assert RunRequest.__module__ == "neocortex.interface.application.request"
    assert WorkerController.__module__ == "neocortex.interface.application.controller"
    assert MainWindow.__module__ == "neocortex.interface.presentation.windows.main"
    assert ReadRequest.__module__ == "neocortex.interface.read.models"
    assert SharedReadClient.__module__ == "neocortex.interface.read.client"


def test_assets_are_owned_by_the_canonical_presentation_package() -> None:
    from neocortex.interface.presentation.assets import application_icon_path, asset_directory

    directory = asset_directory()
    assert directory == REPOSITORY_ROOT / "neocortex/interface/presentation/assets"
    assert application_icon_path() == directory / "neocortex-app-icon.ico"
    assert {path.suffix for path in directory.iterdir()} == {".ico", ".png", ".svg"}


def test_numbered_interface_root_is_extinct() -> None:
    legacy = "_05" + "_Interfaz"

    assert not (REPOSITORY_ROOT / legacy).exists()
    for relative in ("neocortex", "neocortex", "tools"):
        for path in (REPOSITORY_ROOT / relative).rglob("*.py"):
            assert legacy not in path.read_text(encoding="utf-8"), path


def test_canonical_run_request_patch_seam_is_live(tmp_path: Path) -> None:
    from neocortex.interface.application.request import RunRequest

    request = RunRequest(tmp_path, ("pdf",), apply=True).validated()

    assert request.apply is True
    assert request.__class__.__module__ == "neocortex.interface.application.request"
    assert interface.__all__ == ["main"]
