"""Stable contracts for bounded visual-video inspection and indexing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping


VIDEO_ROUTE_VERSION = "video-route-v1"


@dataclass(frozen=True, slots=True)
class VideoStreamProbe:
    index: int
    codec_name: str
    width: int
    height: int
    frame_rate: float | None
    duration_seconds: float | None
    rotation_degrees: int | None


@dataclass(frozen=True, slots=True)
class SubtitleStreamProbe:
    index: int
    codec_name: str
    language: str | None


@dataclass(frozen=True, slots=True)
class VideoMediaProbe:
    duration_seconds: float
    format_name: str
    video: tuple[VideoStreamProbe, ...]
    audio_streams: int
    subtitles: tuple[SubtitleStreamProbe, ...]
    chapters: int

    @property
    def video_streams(self) -> int:
        return len(self.video)


class VideoProcessingError(RuntimeError):
    """One typed, reviewable video failure; it never authorizes mutation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recommendation: Literal["retry", "manual_review", "deletion_candidate"],
        retryable: bool,
        evidence: Mapping[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.recommendation = recommendation
        self.retryable = retryable
        self.evidence = dict(evidence or {})


__all__ = (
    "VIDEO_ROUTE_VERSION",
    "SubtitleStreamProbe",
    "VideoMediaProbe",
    "VideoProcessingError",
    "VideoStreamProbe",
)
