"""Compatibility alias for canonical safety module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.safety.state_topology_contracts import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.safety.state_topology_contracts")
