"""Compatibility alias for canonical Video frame sampling."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.capabilities.formats.video.frames import MAX_VIDEO_FRAMES as MAX_VIDEO_FRAMES
    from neocortex.capabilities.formats.video.frames import ExtractedVideoFrame as ExtractedVideoFrame
    from neocortex.capabilities.formats.video.frames import VideoFrameBatch as VideoFrameBatch
    from neocortex.capabilities.formats.video.frames import VideoFrameCandidate as VideoFrameCandidate
    from neocortex.capabilities.formats.video.frames import (
        VideoFrameSamplingConfig as VideoFrameSamplingConfig,
    )
    from neocortex.capabilities.formats.video.frames import build_frame_plan as build_frame_plan
    from neocortex.capabilities.formats.video.frames import resolve_video_ffmpeg as resolve_video_ffmpeg
    from neocortex.capabilities.formats.video.frames import sampled_video_frames as sampled_video_frames
else:
    sys.modules[__name__] = import_module("neocortex.capabilities.formats.video.frames")
