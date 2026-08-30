"""Compatibility alias for canonical CLI module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.api.cli.cli_audio_surface import *  # noqa: F403
else:
    sys.modules[__name__] = import_module("neocortex.api.cli.cli_audio_surface")
