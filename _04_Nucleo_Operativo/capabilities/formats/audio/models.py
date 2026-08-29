"""Compatibility alias for the canonical Audio model module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.audio.models import (
        AUDIO_ROUTE_VERSION as AUDIO_ROUTE_VERSION,
        AudioProcessingError as AudioProcessingError,
        AudioRouteConfig as AudioRouteConfig,
        AudioRouteSummary as AudioRouteSummary,
        MediaProbe as MediaProbe,
        TranscriptResult as TranscriptResult,
        TranscriptSegment as TranscriptSegment,
        WhisperRuntime as WhisperRuntime,
        WhisperRuntimeError as WhisperRuntimeError,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.audio.models")
