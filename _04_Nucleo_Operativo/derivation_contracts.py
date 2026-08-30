"""Compatibility alias for canonical Semantic plane module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.semantic.derivation_contracts import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.semantic.derivation_contracts")
