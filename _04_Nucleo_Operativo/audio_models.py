"""Compatibility alias for canonical Audio contracts."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.audio.models import AUDIO_ROUTE_VERSION as AUDIO_ROUTE_VERSION
    from .capabilities.formats.audio.models import AudioProcessingError as AudioProcessingError
    from .capabilities.formats.audio.models import AudioRouteConfig as AudioRouteConfig
    from .capabilities.formats.audio.models import AudioRouteSummary as AudioRouteSummary
    from .capabilities.formats.audio.models import MediaProbe as MediaProbe
    from .capabilities.formats.audio.models import TranscriptResult as TranscriptResult
    from .capabilities.formats.audio.models import TranscriptSegment as TranscriptSegment
    from .capabilities.formats.audio.models import WhisperRuntime as WhisperRuntime
    from .capabilities.formats.audio.models import WhisperRuntimeError as WhisperRuntimeError
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.audio.models")
