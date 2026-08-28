"""Compatibility alias for the canonical Archive SQLite owner."""

from __future__ import annotations

import sys
from importlib import import_module

sys.modules[__name__] = import_module("neocortex.capabilities.formats.archive.state")
