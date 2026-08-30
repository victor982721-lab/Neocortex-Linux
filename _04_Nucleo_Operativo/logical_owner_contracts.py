"""Compatibility alias for canonical Code logical ownership contracts."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.code.logical_owner_contracts import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.code.logical_owner_contracts")
