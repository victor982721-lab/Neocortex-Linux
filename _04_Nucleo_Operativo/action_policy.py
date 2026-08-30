"""Compatibility alias for canonical workflow action policy."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.workflow.actions.action_policy import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.workflow.actions.action_policy")
