"""Packager-safe access to immutable desktop application assets."""

from __future__ import annotations

import sys
from pathlib import Path


# region [01] Asset resolution


def asset_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        return Path(frozen_root) / "neocortex" / "interface" / "presentation" / "assets"
    return Path(__file__).resolve().parent / "assets"


def application_icon_path() -> Path:
    return asset_directory() / "neocortex-app-icon.ico"


# endregion [01]
