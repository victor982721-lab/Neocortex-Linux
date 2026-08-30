"""Compatibility alias for canonical action recovery."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.workflow.actions.file_action_recovery import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.workflow.actions.file_action_recovery")
