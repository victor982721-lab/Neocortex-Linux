"""Compatibility alias for canonical framework state writer."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.persistence.framework_state_writer import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.persistence.framework_state_writer")
