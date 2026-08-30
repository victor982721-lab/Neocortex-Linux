"""Compatibility alias for canonical inventory integration."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.integrations.inventory.inventory_coordinator import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.integrations.inventory.inventory_coordinator")
