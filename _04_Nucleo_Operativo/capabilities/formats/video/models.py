"""Stable contracts for bounded visual-video inspection and indexing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from ....processing_provenance import ROUTE_SUMMARY_SCHEMA


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
        self.recommendation: Literal["retry", "manual_review", "deletion_candidate"] = (
            recommendation
        )
        self.retryable = retryable
        self.evidence = dict(evidence or {})


@dataclass(frozen=True, slots=True)
class VideoRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    complete: int = 0
    partial: int = 0
    errors: int = 0
    visual_only: int = 0
    frames_sampled: int = 0
    scene_frames: int = 0
    keyframes: int = 0
    interval_frames: int = 0
    ocr_attempts: int = 0
    ocr_positive: int = 0
    ocr_failures: int = 0
    ocr_text_chars: int = 0
    audio_links: int = 0
    audio_links_added_on_replay: int = 0
    review_candidates: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    cache_documents_pruned: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    ocr_available: bool = False
    processing_signature: str | None = None
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA


__all__ = (
    "VIDEO_ROUTE_VERSION",
    "SubtitleStreamProbe",
    "VideoMediaProbe",
    "VideoProcessingError",
    "VideoRouteSummary",
    "VideoStreamProbe",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "_04_Nucleo_Operativo.video_models"
del _defined_value
