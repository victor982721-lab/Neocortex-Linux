"""Compatibility alias for canonical Code architecture contracts."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.code.contracts.target_registry import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.code.contracts.target_registry")
