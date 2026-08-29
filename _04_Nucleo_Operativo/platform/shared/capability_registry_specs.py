"""Compatibility alias for the canonical capability registry specifications."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.platform.capability_registry_specs import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.platform.capability_registry_specs")
