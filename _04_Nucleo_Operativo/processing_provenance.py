"""Compatibility alias for the canonical processing provenance contract."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.foundation.processing_provenance import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.foundation.processing_provenance")
