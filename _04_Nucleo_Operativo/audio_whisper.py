"""Compatibility alias for canonical isolated Audio transcription."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.audio.whisper import WhisperTranscriber as WhisperTranscriber
    from .capabilities.formats.audio.whisper import _whisper_worker as _whisper_worker
    from .capabilities.formats.audio.whisper import audio_runtime_doctor as audio_runtime_doctor
    from .capabilities.formats.audio.whisper import (
        resolve_whisper_runtime as resolve_whisper_runtime,
    )
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.audio.whisper")
