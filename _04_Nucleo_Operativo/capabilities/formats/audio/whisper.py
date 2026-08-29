"""Compatibility alias for the canonical Audio transcription module."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.audio.whisper import (
        WhisperTranscriber as WhisperTranscriber,
        _whisper_worker as _whisper_worker,
        audio_runtime_doctor as audio_runtime_doctor,
        resolve_whisper_runtime as resolve_whisper_runtime,
    )
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.audio.whisper")
