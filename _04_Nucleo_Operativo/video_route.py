"""Compatibility alias for the canonical Video route."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.video.route import VIDEO_MIME_TYPES as VIDEO_MIME_TYPES
    from neocortex.capabilities.formats.video.route import VideoRoute as VideoRoute
    from neocortex.capabilities.formats.video.route import VideoRouteConfig as VideoRouteConfig
    from neocortex.capabilities.formats.video.route import VideoRouteSummary as VideoRouteSummary
    from neocortex.capabilities.formats.video.route import search_video_state as search_video_state
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.video.route")
