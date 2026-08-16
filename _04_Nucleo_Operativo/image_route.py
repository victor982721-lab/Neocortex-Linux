"""Compatibility alias for the canonical Image route module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.image.route import *  # noqa: F403
    from .capabilities.formats.image.route import _same_snapshot as _same_snapshot
else:
    sys.modules[__name__] = import_module(
        "_04_Nucleo_Operativo.capabilities.formats.image.route"
    )
