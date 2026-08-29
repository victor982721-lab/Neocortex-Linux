"""Compatibility alias for the canonical PDF route_models module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.pdf.pdf_route_models import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.pdf.pdf_route_models")
