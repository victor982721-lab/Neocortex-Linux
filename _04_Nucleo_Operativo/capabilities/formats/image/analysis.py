"""Compatibility alias for the canonical Image analysis module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.image.analysis import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.image.analysis")
