"""Stable display paths and Win32 extended paths for filesystem I/O."""

from __future__ import annotations

import os
from pathlib import Path


WINDOWS_LEGACY_PATH_LIMIT = 260


def absolute_display_path(path: str | Path) -> str:
    """Return the canonical absolute spelling persisted in schemas and reports."""

    return os.path.abspath(os.fspath(path))


def native_io_path(path: str | Path) -> str:
    """Return an OS path suitable for opening long Windows paths.

    Extended prefixes are an I/O detail only. Persisted paths deliberately keep
    their normal drive or UNC spelling so caches and reports remain compatible.
    """

    absolute = absolute_display_path(path)
    if os.name != "nt" or len(absolute) < WINDOWS_LEGACY_PATH_LIMIT:
        return absolute
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


__all__ = ["WINDOWS_LEGACY_PATH_LIMIT", "absolute_display_path", "native_io_path"]
