"""Compatibility alias for canonical read API port."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.api.read_api_port import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.api.read_api_port")
