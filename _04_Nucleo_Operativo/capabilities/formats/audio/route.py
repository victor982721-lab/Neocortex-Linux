"""Compatibility alias for the canonical Audio route module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.audio.route import (
        AUDIO_MIME_TYPES as AUDIO_MIME_TYPES,
        VIDEO_MIME_TYPES as VIDEO_MIME_TYPES,
        AudioRoute as AudioRoute,
        AudioRouteConfig as AudioRouteConfig,
        AudioRouteSummary as AudioRouteSummary,
        _file_key as _file_key,
        search_audio_state as search_audio_state,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.audio.route")
