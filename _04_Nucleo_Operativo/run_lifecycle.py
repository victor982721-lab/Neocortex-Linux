"""Compatibility alias for canonical runtime execution lifecycle."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.orchestration.run_lifecycle import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.runtime.orchestration.run_lifecycle")
