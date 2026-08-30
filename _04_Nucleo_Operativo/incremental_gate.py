"""Compatibility alias for canonical incremental gate control."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.control.incremental_gate import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.runtime.control.incremental_gate")
