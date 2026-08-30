"""Compatibility alias for canonical runtime model management."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.config.model_management import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.runtime.config.model_management")
