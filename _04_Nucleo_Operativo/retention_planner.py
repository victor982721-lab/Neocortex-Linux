"""Compatibility alias for canonical workflow retention planning."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.workflow.retention.planner import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.workflow.retention.planner")
