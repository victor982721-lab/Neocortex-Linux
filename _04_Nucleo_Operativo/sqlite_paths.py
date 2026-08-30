"""Compatibility alias for canonical SQLite paths."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.persistence.sqlite_paths import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.persistence.sqlite_paths")
