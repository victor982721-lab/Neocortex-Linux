"""Compatibility alias for canonical Code dependency validation."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.code.validation_supply import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.code.validation_supply")
