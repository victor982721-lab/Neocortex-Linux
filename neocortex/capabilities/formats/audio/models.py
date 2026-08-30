"""Stable contracts for media probing, Whisper transcription and route summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    build_processing_provenance,
    executable_component,
    python_runtime_component,
)
from neocortex.safety.route_filters import CandidateSelection


# region [01] Processing configuration


AUDIO_ROUTE_VERSION = "audio-route-v1"


@dataclass(frozen=True, slots=True)
class AudioRouteConfig:
    state_path: Path
    model_name: str = "small"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: str = "auto"
    language: str | None = None
    beam_size: int = 5
    vad_filter: bool = True
    include_video: bool = True
    max_file_bytes: int | None = None
    max_documents: int | None = None
    max_duration_seconds: float = 6 * 60 * 60
    max_transcript_chars: int = 5_000_000
    max_segments: int = 100_000
    file_timeout_seconds: float = 3600.0
    worker_startup_timeout_seconds: float = 1800.0
    worker_memory_bytes: int = 4 * 1024 * 1024 * 1024
    retry_errors: bool = False
    ffprobe_path: str | None = None
    model_cache_directory: Path | None = None
    local_models_only: bool = False
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    memory_budget_bytes: int = 2 * 1024 * 1024 * 1024
    min_free_memory_bytes: int = 2 * 1024 * 1024 * 1024
    min_free_commit_bytes: int = 2 * 1024 * 1024 * 1024
    memory_wait_timeout_seconds: float = 300.0

    def processing_signature(
        self,
        *,
        backend_version: str,
        resolved_device: str,
        resolved_compute_type: str,
        ctranslate2_version: str = "unknown",
    ) -> str:
        return self.processing_provenance(
            backend_version=backend_version,
            ctranslate2_version=ctranslate2_version,
            resolved_device=resolved_device,
            resolved_compute_type=resolved_compute_type,
        ).signature

    def processing_provenance(
        self,
        *,
        backend_version: str,
        ctranslate2_version: str,
        resolved_device: str,
        resolved_compute_type: str,
    ) -> ProcessingProvenance:
        return build_processing_provenance(
            "audio-route",
            AUDIO_ROUTE_VERSION,
            {
                "model_name": self.model_name,
                "device": resolved_device,
                "compute_type": resolved_compute_type,
                "language": self.language or "auto",
                "beam_size": self.beam_size,
                "vad_filter": self.vad_filter,
                "max_duration_seconds": self.max_duration_seconds,
                "max_transcript_chars": self.max_transcript_chars,
                "max_segments": self.max_segments,
                "local_models_only": self.local_models_only,
            },
            (
                python_runtime_component(),
                {
                    "name": "faster-whisper",
                    "kind": "python-distribution",
                    "version": backend_version,
                },
                {
                    "name": "ctranslate2",
                    "kind": "python-distribution",
                    "version": ctranslate2_version,
                },
                executable_component(
                    "ffprobe",
                    default_name="ffprobe",
                    explicit=self.ffprobe_path,
                    version_arguments=("-version",),
                ),
            ),
            compatibility_tag=AUDIO_ROUTE_VERSION,
        )


# endregion [01]


# region [02] Probe and transcript result schemas


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration_seconds: float
    format_name: str
    audio_codec: str
    sample_rate: int | None
    channels: int | None
    audio_streams: int
    video_streams: int


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    index: int
    start_ms: int
    end_ms: int
    text: str
    avg_logprob: float | None
    no_speech_probability: float | None


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    language: str | None
    language_probability: float | None
    duration_seconds: float
    speech_duration_seconds: float
    segments: tuple[TranscriptSegment, ...]
    model_name: str
    backend_version: str
    device: str
    compute_type: str


@dataclass(frozen=True, slots=True)
class WhisperRuntime:
    backend_version: str
    ctranslate2_version: str
    cuda_devices: int
    resolved_device: str
    resolved_compute_type: str


# endregion [02]


# region [03] Route summary and typed failures


@dataclass(frozen=True, slots=True)
class AudioRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    transcribed: int = 0
    no_speech: int = 0
    no_audio: int = 0
    errors: int = 0
    cache_documents_pruned: int = 0
    review_candidates: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    transcript_chars: int = 0
    transcript_segments: int = 0
    media_seconds: float = 0.0
    speech_seconds: float = 0.0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    catalog_candidates: int = 0
    catalog_classified: int = 0
    catalog_cache_hits: int = 0
    catalog_review_required: int = 0
    catalog_errors: int = 0
    catalog_source_stale: int = 0
    catalog_stale_marked: int = 0
    processing_signature: str | None = None
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA


class AudioProcessingError(RuntimeError):
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
        self.recommendation: Literal[
            "retry", "manual_review", "deletion_candidate"
        ] = recommendation
        self.retryable = retryable
        self.evidence = dict(evidence or {})


class WhisperRuntimeError(RuntimeError):
    """The shared Whisper runtime could not start or remain available."""


# endregion [03]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.audio.models"
del _defined_value
