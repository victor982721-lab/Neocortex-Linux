"""Capability implementations grouped by product responsibility.

The package is intentionally import-light: selecting a format loads only that
format's route, state and worker modules.
"""

from __future__ import annotations

__all__ = ["formats"]
