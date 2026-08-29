"""Compatibility alias for the canonical Audio probe module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.audio.probe import (
        MAX_FFPROBE_OUTPUT_BYTES as MAX_FFPROBE_OUTPUT_BYTES,
        probe_media as probe_media,
        resolve_ffprobe as resolve_ffprobe,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.audio.probe")
