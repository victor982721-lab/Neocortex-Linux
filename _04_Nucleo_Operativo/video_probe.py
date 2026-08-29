"""Compatibility alias for canonical bounded Video probing."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.video.probe import decode_video_probe as decode_video_probe
    from neocortex.capabilities.formats.video.probe import probe_video as probe_video
    from neocortex.capabilities.formats.video.probe import resolve_video_ffprobe as resolve_video_ffprobe
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.video.probe")
