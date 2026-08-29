"""Compatibility alias for the canonical PDF schema module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.pdf.pdf_schema import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.pdf.pdf_schema")
