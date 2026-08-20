"""Compatibility alias for canonical bounded Audio probing."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.audio.probe import (
        MAX_FFPROBE_OUTPUT_BYTES as MAX_FFPROBE_OUTPUT_BYTES,
    )
    from .capabilities.formats.audio.probe import probe_media as probe_media
    from .capabilities.formats.audio.probe import resolve_ffprobe as resolve_ffprobe
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.audio.probe")
