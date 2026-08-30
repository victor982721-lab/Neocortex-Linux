"""Compatibility alias for canonical global resource coordination."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.control.global_resources import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.runtime.control.global_resources")
