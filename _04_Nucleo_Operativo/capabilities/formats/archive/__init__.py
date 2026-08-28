"""Compatibility namespace for the relocated canonical Archive capability."""

from __future__ import annotations

import sys
from importlib import import_module

sys.modules[__name__] = import_module("neocortex.capabilities.formats.archive")
