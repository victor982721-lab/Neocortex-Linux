"""Compatibility alias for canonical Code plane module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.code.external_deep_coverage_worker import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.code.external_deep_coverage_worker")
