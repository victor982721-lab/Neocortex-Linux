"""Compatibility alias for canonical Video contracts."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities.formats.video.models import VIDEO_ROUTE_VERSION as VIDEO_ROUTE_VERSION
    from .capabilities.formats.video.models import SubtitleStreamProbe as SubtitleStreamProbe
    from .capabilities.formats.video.models import VideoMediaProbe as VideoMediaProbe
    from .capabilities.formats.video.models import VideoProcessingError as VideoProcessingError
    from .capabilities.formats.video.models import VideoRouteSummary as VideoRouteSummary
    from .capabilities.formats.video.models import VideoStreamProbe as VideoStreamProbe
else:
    sys.modules[__name__] = import_module("_04_Nucleo_Operativo.capabilities.formats.video.models")
