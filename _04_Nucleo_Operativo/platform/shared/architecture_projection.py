"""Compatibility alias for the canonical platform architecture projection."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.platform.architecture_projection import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.platform.architecture_projection")
