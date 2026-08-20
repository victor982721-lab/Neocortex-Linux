"""Compatibility alias for the canonical Audio route."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.audio.route import AUDIO_MIME_TYPES as AUDIO_MIME_TYPES
    from .capabilities.formats.audio.route import VIDEO_MIME_TYPES as VIDEO_MIME_TYPES
    from .capabilities.formats.audio.route import AudioRoute as AudioRoute
    from .capabilities.formats.audio.route import AudioRouteConfig as AudioRouteConfig
    from .capabilities.formats.audio.route import AudioRouteSummary as AudioRouteSummary
    from .capabilities.formats.audio.route import _file_key as _file_key
    from .capabilities.formats.audio.route import search_audio_state as search_audio_state
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.audio.route")
