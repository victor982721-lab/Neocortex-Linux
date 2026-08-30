"""Compatibility alias for canonical runtime models and results."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.models import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.runtime.models")
