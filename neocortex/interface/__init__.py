"""Canonical desktop-interface boundary.

The root remains import-light so non-GUI commands do not load PySide6.
"""

from __future__ import annotations

from collections.abc import Sequence


def main(arguments: Sequence[str] | None = None) -> int:
    """Load the optional Qt application only for an explicit UI launch."""

    from .application.app import main as run_application

    return run_application(arguments)


__all__ = ["main"]
