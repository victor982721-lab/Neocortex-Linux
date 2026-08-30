"""Incremental dedicated visual-video route with ephemeral frame OCR."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from neocortex.workflow.actions.action_policy import same_snapshot
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.safety.ocr_profiles import OCR_PROFILE_CHOICES, OcrProfileName
from neocortex.foundation.processing_provenance import (
    ProcessingProvenance,
    build_processing_provenance,
    executable_component,
    python_runtime_component,
)
from neocortex.workflow.review.review import ReviewCandidate
from neocortex.safety.route_filters import CandidateSelection
from neocortex.persistence.framework_route_state import (
    FrameworkRouteState,
    ReviewCandidateReconciliation,
)
from .frames import (
    ExtractedVideoFrame,
    VideoFrameBatch,
    VideoFrameSamplingConfig,
    resolve_video_ffmpeg,
    sampled_video_frames,
)
from .models import (
    VIDEO_ROUTE_VERSION,
    VideoMediaProbe,
    VideoProcessingError,
    VideoRouteSummary,
)
from .probe import probe_video, resolve_video_ffprobe
from .state import (
    VideoFrameEvidence,
    cached_video_document,
    find_published_audio_link,
    initialize_video_state,
    prune_stale_video_documents,
    refresh_cached_video,
    search_video_state,
    store_video_error,
    store_video_inventory,
    store_video_success,
    video_database,
)

if TYPE_CHECKING:
    from ..image.document import DocumentVerifierRuntime


VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
        "video/x-msvideo",
    }
)
VIDEO_COMMIT_BATCH = 4
VIDEO_REVIEW_REASON_CODES = frozenset(
    {
        "media_without_video_stream",
        "video_duration_limit",
        "video_duration_unknown",
        "video_file_timeout",
        "video_frame_byte_limit",
        "video_frame_batch_byte_limit",
        "video_frame_discovery_error",
        "video_frame_discovery_output_limit",
        "video_frame_discovery_timeout",
        "video_frame_extract_error",
        "video_frame_invalid_png",
        "video_frame_ocr_error",
        "video_frame_output_limit",
        "video_frame_plan_empty",
        "video_frame_timeout",
        "video_frames_unavailable",
        "video_invalid_container",
        "video_io_error",
        "video_probe_error",
        "video_probe_invalid_json",
        "video_probe_output_limit",
        "video_probe_schema",
        "video_probe_timeout",
        "video_scratch_intersects_corpus",
        "video_source_changed",
    }
)
VIDEO_RETRYABLE_WARNING_CODES = frozenset(
    {
        "video_file_timeout",
        "video_frame_discovery_error",
        "video_frame_discovery_timeout",
        "video_frame_extract_error",
        "video_frame_invalid_png",
        "video_frame_ocr_error",
        "video_frame_timeout",
    }
)


class _OcrRuntime(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def signature(self) -> str: ...

    @property
    def provenance(self) -> str | None: ...

    @property
    def unavailable_reason(self) -> str | None: ...

    @property
    def processing_provenance_json(self) -> str | None: ...


class _OcrEvidence(Protocol):
    @property
    def attempted(self) -> bool: ...

    @property
    def available(self) -> bool: ...

    @property
    def recognized_text(self) -> str: ...

    @property
    def mean_confidence(self) -> float: ...

    @property
    def provenance(self) -> str | None: ...

    @property
    def error_type(self) -> str | None: ...

    @property
    def error_message(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class _UnavailableOcrRuntime:
    enabled: bool = False
    signature: str = "video-frame-ocr-unavailable-v1"
    provenance: str | None = None
    unavailable_reason: str | None = "image_document_runtime_unavailable"
    processing_provenance_json: str | None = None


def _default_ocr_runtime(config: "VideoRouteConfig") -> _OcrRuntime:
    try:
        from ..image.document import (
            DocumentVerifierConfig,
            resolve_document_verifier,
        )
    except (ImportError, ModuleNotFoundError):
        return _UnavailableOcrRuntime()
    return resolve_document_verifier(
        DocumentVerifierConfig(
            mode=config.ocr_mode,
            lang=config.ocr_lang,
            profile=config.ocr_profile,
            timeout_seconds=config.ocr_timeout_seconds,
            tesseract_cmd=config.tesseract_cmd,
            tessdata_dir=config.tessdata_dir,
        )
    )


def _default_frame_ocr(path: Path, runtime: _OcrRuntime, memory_gate=None) -> _OcrEvidence:
    from ..image.document import verify_document_text

    return verify_document_text(
        path,
        cast("DocumentVerifierRuntime", runtime),
        memory_gate,
    )


@dataclass(frozen=True, slots=True)
class VideoRouteConfig:
    state_path: Path
    root: Path
    audio_state_path: Path | None = None
    max_file_bytes: int | None = None
    max_documents: int | None = None
    max_duration_seconds: float = 6 * 60 * 60
    max_frames: int = 48
    interval_seconds: float = 30.0
    scene_threshold: float = 0.35
    include_scenes: bool = True
    include_keyframes: bool = True
    max_frame_pixels: int = 2_073_600
    max_frame_side: int = 1920
    probe_timeout_seconds: float = 30.0
    discovery_timeout_seconds: float = 60.0
    frame_timeout_seconds: float = 20.0
    file_timeout_seconds: float = 300.0
    worker_memory_bytes: int = 2 * 1024 * 1024 * 1024
    retry_errors: bool = False
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    ocr_mode: Literal["auto", "never"] = "auto"
    ocr_lang: str = "spa+eng"
    ocr_profile: OcrProfileName = "configured"
    ocr_timeout_seconds: float = 12.0
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None
    selection: CandidateSelection = field(default_factory=CandidateSelection)

    def frame_sampling_config(self) -> VideoFrameSamplingConfig:
        return VideoFrameSamplingConfig(
            max_frames=self.max_frames,
            interval_seconds=self.interval_seconds,
            scene_threshold=self.scene_threshold,
            include_scenes=self.include_scenes,
            include_keyframes=self.include_keyframes,
            max_frame_pixels=self.max_frame_pixels,
            max_frame_side=self.max_frame_side,
            discovery_timeout_seconds=self.discovery_timeout_seconds,
            frame_timeout_seconds=self.frame_timeout_seconds,
            file_timeout_seconds=self.file_timeout_seconds,
            worker_memory_bytes=self.worker_memory_bytes,
            ffmpeg_path=self.ffmpeg_path,
        )

    def processing_provenance(self, ocr_runtime: _OcrRuntime) -> ProcessingProvenance:
        try:
            ocr_manifest = (
                json.loads(ocr_runtime.processing_provenance_json)
                if ocr_runtime.processing_provenance_json
                else None
            )
        except (TypeError, ValueError):
            ocr_manifest = None
        ocr_component: dict[str, Any] = {
            "name": "frame-ocr",
            "kind": "processing-pipeline",
            "status": (
                "disabled"
                if self.ocr_mode == "never"
                else "available"
                if ocr_runtime.enabled
                else "unavailable"
            ),
            "signature": ocr_runtime.signature,
        }
        if ocr_manifest is not None:
            ocr_component["manifest"] = ocr_manifest
        return build_processing_provenance(
            "video-route",
            VIDEO_ROUTE_VERSION,
            {
                "max_duration_seconds": self.max_duration_seconds,
                "max_frames": self.max_frames,
                "interval_seconds": self.interval_seconds,
                "scene_threshold": self.scene_threshold,
                "include_scenes": self.include_scenes,
                "include_keyframes": self.include_keyframes,
                "max_frame_pixels": self.max_frame_pixels,
                "max_frame_side": self.max_frame_side,
                "worker_memory_bytes": self.worker_memory_bytes,
                "ocr_mode": self.ocr_mode,
                "ocr_lang": self.ocr_lang,
                "ocr_profile": self.ocr_profile,
            },
            (
                python_runtime_component(),
                executable_component(
                    "ffmpeg",
                    default_name="ffmpeg",
                    explicit=self.ffmpeg_path,
                    version_arguments=("-version",),
                ),
                executable_component(
                    "ffprobe",
                    default_name="ffprobe",
                    explicit=self.ffprobe_path,
                    version_arguments=("-version",),
                ),
                ocr_component,
            ),
            compatibility_tag=VIDEO_ROUTE_VERSION,
        )


@dataclass(slots=True)
class _VideoMetrics:
    candidate_pool: int = 0
    eligible: int = 0
    selected: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    complete: int = 0
    partial: int = 0
    errors: int = 0
    visual_only: int = 0
    frames: int = 0
    scene_frames: int = 0
    keyframes: int = 0
    interval_frames: int = 0
    ocr_attempts: int = 0
    ocr_positive: int = 0
    ocr_failures: int = 0
    ocr_chars: int = 0
    audio_links: int = 0
    replay_links: int = 0
    reviews: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    pruned: int = 0


ProbeFunction = Callable[..., VideoMediaProbe]
OcrRuntimeResolver = Callable[[VideoRouteConfig], _OcrRuntime]
OcrFunction = Callable[[Path, _OcrRuntime, object], _OcrEvidence]
FrameSampler = Callable[..., AbstractContextManager[VideoFrameBatch]]


def _require_positive(value: float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"video {name} must be positive")


def _require_positive_optional(value: int | None, name: str) -> None:
    if value is not None and value < 1:
        raise ValueError(f"video {name} must be positive")


class VideoRoute:
    def __init__(
        self,
        config: VideoRouteConfig,
        framework_state: FrameworkRouteState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
        media_probe: ProbeFunction = probe_video,
        frame_sampler: FrameSampler = sampled_video_frames,
        ocr_runtime_resolver: OcrRuntimeResolver = _default_ocr_runtime,
        frame_ocr: OcrFunction = _default_frame_ocr,
    ) -> None:
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.memory_gate = memory_gate
        self.cancellation = cancellation or CancellationToken()
        self.media_probe = media_probe
        self.frame_sampler = frame_sampler
        self.ocr_runtime_resolver = ocr_runtime_resolver
        self.frame_ocr = frame_ocr

    def _validate(self) -> None:
        self.config.frame_sampling_config().validate()
        _require_positive_optional(self.config.max_documents, "max_documents")
        _require_positive_optional(self.config.max_file_bytes, "max_file_bytes")
        _require_positive(self.config.max_duration_seconds, "max_duration_seconds")
        _require_positive(self.config.probe_timeout_seconds, "probe_timeout_seconds")
        _require_positive(self.config.ocr_timeout_seconds, "ocr_timeout_seconds")
        self._validate_ocr_config()

    def _validate_ocr_config(self) -> None:
        if self.config.ocr_mode not in {"auto", "never"}:
            raise ValueError(f"unsupported video OCR mode: {self.config.ocr_mode}")
        if self.config.ocr_profile not in OCR_PROFILE_CHOICES:
            raise ValueError(f"unsupported video OCR profile: {self.config.ocr_profile}")
        if not self.config.ocr_lang.strip("+"):
            raise ValueError("video OCR language must be non-empty")

    def _plan(self) -> _VideoMetrics:
        totals = [
            self.framework_state.selected_route_candidate_counts(
                self.run_id,
                mime,
                self.config.max_file_bytes,
                "video",
                self.config.selection,
            )
            for mime in sorted(VIDEO_MIME_TYPES)
        ]
        candidate_pool = sum(total for total, _eligible in totals)
        eligible = sum(eligible for _total, eligible in totals)
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return _VideoMetrics(candidate_pool=candidate_pool, eligible=eligible, selected=selected)

    def run(self) -> VideoRouteSummary:
        self.cancellation.checkpoint()
        self._validate()
        # Resolve required native tools before creating owner state.
        resolve_video_ffmpeg(self.config.ffmpeg_path)
        resolve_video_ffprobe(self.config.ffprobe_path)
        ocr_runtime = self.ocr_runtime_resolver(self.config)
        processing = self.config.processing_provenance(ocr_runtime)
        initialize_video_state(self.config.state_path)
        metrics = self._plan()
        with video_database(self.config.state_path, create=False) as connection:
            self._run_candidates(connection, processing.signature, ocr_runtime, metrics)
            connection.commit()
            if self._should_prune():
                metrics.pruned = prune_stale_video_documents(connection, self.run_id)
                connection.commit()
        self._report(metrics, finished=True)
        return VideoRouteSummary(
            candidate_pool=metrics.candidate_pool,
            candidates=metrics.selected,
            skipped_by_size=metrics.candidate_pool - metrics.eligible,
            skipped_by_count=metrics.eligible - metrics.selected,
            processed=metrics.processed,
            cache_hits=metrics.cache_hits,
            cached_errors=metrics.cached_errors,
            complete=metrics.complete,
            partial=metrics.partial,
            errors=metrics.errors,
            visual_only=metrics.visual_only,
            frames_sampled=metrics.frames,
            scene_frames=metrics.scene_frames,
            keyframes=metrics.keyframes,
            interval_frames=metrics.interval_frames,
            ocr_attempts=metrics.ocr_attempts,
            ocr_positive=metrics.ocr_positive,
            ocr_failures=metrics.ocr_failures,
            ocr_text_chars=metrics.ocr_chars,
            audio_links=metrics.audio_links,
            audio_links_added_on_replay=metrics.replay_links,
            review_candidates=metrics.reviews,
            deletion_candidates=metrics.deletion_candidates,
            retryable_errors=metrics.retryable_errors,
            cache_documents_pruned=metrics.pruned,
            peak_reserved_bytes=(
                0 if self.memory_gate is None else self.memory_gate.peak_reserved_bytes
            ),
            memory_waits=0 if self.memory_gate is None else self.memory_gate.wait_count,
            ocr_available=ocr_runtime.enabled,
            processing_signature=processing.signature,
            processing_provenance=processing.manifest,
        )

    def _run_candidates(
        self,
        connection: sqlite3.Connection,
        signature: str,
        ocr_runtime: _OcrRuntime,
        metrics: _VideoMetrics,
    ) -> None:
        for mime in sorted(VIDEO_MIME_TYPES):
            self._run_mime_candidates(
                connection,
                mime,
                signature,
                ocr_runtime,
                metrics,
            )
            if metrics.processed >= metrics.selected:
                return

    def _run_mime_candidates(
        self,
        connection: sqlite3.Connection,
        mime: str,
        signature: str,
        ocr_runtime: _OcrRuntime,
        metrics: _VideoMetrics,
    ) -> None:
        iterator = self.framework_state.iter_selected_route_candidates(
            self.run_id,
            mime,
            "video",
            self.config.selection,
        )
        for snapshot in iterator:
            if metrics.processed >= metrics.selected:
                return
            self.cancellation.checkpoint()
            if self._exceeds_file_limit(snapshot):
                continue
            self._handle_candidate(
                connection,
                snapshot,
                mime,
                signature,
                ocr_runtime,
                metrics,
            )
            metrics.processed += 1
            self._commit_batch(connection, metrics)

    def _exceeds_file_limit(self, snapshot: FileSnapshot) -> bool:
        limit = self.config.max_file_bytes
        return limit is not None and snapshot.size > limit

    def _handle_candidate(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        signature: str,
        ocr_runtime: _OcrRuntime,
        metrics: _VideoMetrics,
    ) -> None:
        store_video_inventory(connection, snapshot, mime, self.run_id)
        cached = cached_video_document(connection, snapshot, signature)
        if self._can_reuse_cached(cached):
            assert cached is not None
            self._consume_cached(connection, snapshot, mime, cached, metrics)
            return
        self._process_candidate(
            connection,
            snapshot,
            mime,
            signature,
            ocr_runtime,
            metrics,
        )

    def _can_reuse_cached(self, cached: sqlite3.Row | None) -> bool:
        if cached is None:
            return False
        status = str(cached["status"])
        return status in {"complete", "partial"} or (
            status == "error" and not self.config.retry_errors
        )

    def _commit_batch(
        self,
        connection: sqlite3.Connection,
        metrics: _VideoMetrics,
    ) -> None:
        if metrics.processed % VIDEO_COMMIT_BATCH:
            return
        connection.commit()
        self._report(metrics)

    def _consume_cached(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        cached: sqlite3.Row,
        metrics: _VideoMetrics,
    ) -> None:
        prior_audio = cached["audio_status"]
        link = find_published_audio_link(self.config.audio_state_path, snapshot)
        refresh_cached_video(connection, snapshot, mime, self.run_id, link)
        metrics.cache_hits += 1
        status = str(cached["status"])
        if status == "error":
            failure = _cached_failure(cached)
            self._store_review(snapshot, failure)
            metrics.cached_errors += 1
            metrics.errors += 1
            metrics.reviews += 1
            metrics.deletion_candidates += int(failure.recommendation == "deletion_candidate")
            metrics.retryable_errors += int(failure.retryable)
            return
        metrics.complete += int(status == "complete")
        metrics.partial += int(status == "partial")
        metrics.frames += int(cached["frame_count"])
        metrics.ocr_positive += int(cached["ocr_frame_count"])
        metrics.ocr_chars += int(cached["ocr_text_chars"])
        metrics.visual_only += int(int(cached["audio_streams"]) == 0)
        reason_rows = connection.execute(
            "SELECT sampling_reasons_json FROM frames WHERE file_key=? ORDER BY frame_index",
            (file_key_from_snapshot(snapshot),),
        )
        for row in reason_rows:
            reasons = frozenset(json.loads(str(row[0])))
            metrics.scene_frames += int("scene" in reasons)
            metrics.keyframes += int("keyframe" in reasons)
            metrics.interval_frames += int("interval" in reasons)
        metrics.audio_links += int(link is not None)
        metrics.replay_links += int(link is not None and prior_audio is None)
        warnings = tuple(json.loads(str(cached["warnings_json"])))
        metrics.reviews += sum(warning in VIDEO_REVIEW_REASON_CODES for warning in warnings)
        self._reconcile_success(snapshot, warnings)

    def _process_candidate(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        signature: str,
        ocr_runtime: _OcrRuntime,
        metrics: _VideoMetrics,
    ) -> None:
        try:
            probe, frames, warnings = self._inspect(snapshot, ocr_runtime, metrics)
            link = find_published_audio_link(self.config.audio_state_path, snapshot)
            store_video_success(
                connection,
                snapshot,
                mime,
                signature,
                probe,
                frames,
                warnings,
                link,
                self.run_id,
            )
            metrics.complete += int(not warnings)
            metrics.partial += int(bool(warnings))
            metrics.visual_only += int(probe.audio_streams == 0)
            metrics.audio_links += int(link is not None)
            metrics.frames += len(frames)
            metrics.scene_frames += sum("scene" in frame.sampling_reasons for frame in frames)
            metrics.keyframes += sum("keyframe" in frame.sampling_reasons for frame in frames)
            metrics.interval_frames += sum("interval" in frame.sampling_reasons for frame in frames)
            metrics.reviews += sum(warning in VIDEO_REVIEW_REASON_CODES for warning in warnings)
            self._reconcile_success(snapshot, warnings)
        except VideoProcessingError as exc:
            store_video_error(connection, snapshot, mime, signature, self.run_id, exc)
            self._store_review(snapshot, exc)
            metrics.errors += 1
            metrics.reviews += 1
            metrics.deletion_candidates += int(exc.recommendation == "deletion_candidate")
            metrics.retryable_errors += int(exc.retryable)
        except (OSError, sqlite3.Error) as exc:
            failure = VideoProcessingError(
                "video_io_error",
                f"{type(exc).__name__}: {exc}",
                recommendation="retry",
                retryable=True,
            )
            store_video_error(connection, snapshot, mime, signature, self.run_id, failure)
            self._store_review(snapshot, failure)
            metrics.errors += 1
            metrics.reviews += 1
            metrics.retryable_errors += 1

    def _inspect(
        self,
        snapshot: FileSnapshot,
        ocr_runtime: _OcrRuntime,
        metrics: _VideoMetrics,
    ) -> tuple[
        VideoMediaProbe,
        tuple[VideoFrameEvidence, ...],
        tuple[str, ...],
    ]:
        before = snapshot_path(snapshot.path)
        if not same_snapshot(snapshot, before):
            raise _source_changed("after inventory")
        admission = (
            self.memory_gate.admit(self.config.worker_memory_bytes)
            if self.memory_gate is not None
            else nullcontext()
        )
        with admission:
            probe = self.media_probe(
                Path(snapshot.path),
                ffprobe_path=self.config.ffprobe_path,
                timeout_seconds=self.config.probe_timeout_seconds,
            )
            if probe.duration_seconds > self.config.max_duration_seconds:
                raise VideoProcessingError(
                    "video_duration_limit",
                    "video duration exceeds configured limit: "
                    f"{probe.duration_seconds:.3f} > "
                    f"{self.config.max_duration_seconds:.3f} seconds",
                    recommendation="manual_review",
                    retryable=False,
                    evidence={
                        "duration_seconds": probe.duration_seconds,
                        "limit_seconds": self.config.max_duration_seconds,
                    },
                )
            primary = max(
                probe.video,
                key=lambda stream: (stream.width * stream.height, -stream.index),
            )
            warnings: list[str] = []
            with self.frame_sampler(
                Path(snapshot.path),
                corpus_root=self.config.root,
                stream_index=primary.index,
                source_width=primary.width,
                source_height=primary.height,
                duration_seconds=probe.duration_seconds,
                config=self.config.frame_sampling_config(),
                cancellation=self.cancellation,
            ) as batch:
                warnings.extend(batch.warnings)
                frames = tuple(
                    self._inspect_frame(frame, ocr_runtime, metrics, warnings)
                    for frame in batch.frames
                )
                after = snapshot_path(snapshot.path)
                if not same_snapshot(snapshot, after):
                    raise _source_changed("during frame inspection")
                return probe, frames, tuple(sorted(set(warnings)))

    def _inspect_frame(
        self,
        frame: ExtractedVideoFrame,
        ocr_runtime: _OcrRuntime,
        metrics: _VideoMetrics,
        warnings: list[str],
    ) -> VideoFrameEvidence:
        # Sampling and frame OCR share the outer per-file reservation. Passing
        # the same coordinated gate into the existing image OCR path would
        # reacquire it recursively and double-count (or deadlock at the exact
        # configured budget) even though FFmpeg is no longer running here.
        evidence = self.frame_ocr(frame.path, ocr_runtime, None)
        metrics.ocr_attempts += int(evidence.attempted)
        positive = evidence.available and bool(evidence.recognized_text)
        metrics.ocr_positive += int(positive)
        metrics.ocr_chars += len(evidence.recognized_text)
        if evidence.attempted and not evidence.available:
            metrics.ocr_failures += 1
            warnings.append("video_frame_ocr_error")
        return VideoFrameEvidence(
            frame_index=frame.index,
            timestamp_ms=frame.timestamp_ms,
            sampling_reasons=frame.reasons,
            width=frame.width,
            height=frame.height,
            content_xxh3_128=frame.content_xxh3_128,
            ocr_available=evidence.available,
            ocr_text=evidence.recognized_text,
            ocr_mean_confidence=evidence.mean_confidence if evidence.available else None,
            ocr_provenance=evidence.provenance,
            ocr_error_type=evidence.error_type,
            ocr_error_message=evidence.error_message,
        )

    def _store_review(
        self,
        snapshot: FileSnapshot,
        failure: VideoProcessingError,
        *,
        source_status: Literal["partial", "error"] = "error",
    ) -> None:
        self.framework_state.store_review_candidates(
            self.run_id,
            (_review_candidate(snapshot, failure, source_status=source_status),),
        )

    def _reconcile_success(self, snapshot: FileSnapshot, warnings: tuple[str, ...]) -> None:
        known_warnings = tuple(reason for reason in warnings if reason in VIDEO_REVIEW_REASON_CODES)
        for reason in known_warnings:
            retryable = reason in VIDEO_RETRYABLE_WARNING_CODES
            self._store_review(
                snapshot,
                VideoProcessingError(
                    reason,
                    "bounded video inspection completed with a retained warning",
                    recommendation="retry" if retryable else "manual_review",
                    retryable=retryable,
                ),
                source_status="partial",
            )
        self.framework_state.reconcile_review_candidates_batch(
            self.run_id,
            "video",
            (
                ReviewCandidateReconciliation(
                    snapshot=snapshot,
                    resolution_note=(
                        "bounded video inspection completed"
                        if not known_warnings
                        else "bounded video inspection retained explicit warnings"
                    ),
                    evaluated_reason_codes=tuple(sorted(VIDEO_REVIEW_REASON_CODES)),
                    active_reason_codes=known_warnings,
                ),
            ),
        )

    def _should_prune(self) -> bool:
        return (
            not self.config.selection.active
            and self.config.max_documents is None
            and self.config.max_file_bytes is None
        )

    def _report(self, metrics: _VideoMetrics, *, finished: bool = False) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                "video",
                "inspect",
                "Video visual indexado" if finished else "Inspeccionando video",
                metrics.processed,
                metrics.selected,
                "archivos",
                finished,
                (
                    ProgressMetric("cache_hits", metrics.cache_hits),
                    ProgressMetric("frames", metrics.frames),
                    ProgressMetric("ocr_positive", metrics.ocr_positive),
                    ProgressMetric("partial", metrics.partial),
                    ProgressMetric("errors", metrics.errors),
                ),
            ),
        )


def _source_changed(phase: str) -> VideoProcessingError:
    return VideoProcessingError(
        "video_source_changed",
        f"video source changed {phase}",
        recommendation="retry",
        retryable=True,
    )


def _cached_failure(row: sqlite3.Row) -> VideoProcessingError:
    stored = str(row["review_disposition"])
    recommendation: Literal["retry", "manual_review", "deletion_candidate"]
    if stored == "retry":
        recommendation = "retry"
    elif stored == "deletion_candidate":
        recommendation = "deletion_candidate"
    else:
        recommendation = "manual_review"
    return VideoProcessingError(
        str(row["error_type"] or "video_cached_error"),
        str(row["error_message"] or "cached video error"),
        recommendation=recommendation,
        retryable=bool(row["retryable"]),
    )


def _review_candidate(
    snapshot: FileSnapshot,
    failure: VideoProcessingError,
    *,
    source_status: Literal["partial", "error"],
) -> ReviewCandidate:
    evidence: dict[str, object] = {
        "message": str(failure)[:512],
        "route_version": VIDEO_ROUTE_VERSION,
    }
    evidence.update(failure.evidence)
    return ReviewCandidate(
        route_name="video",
        snapshot=snapshot,
        reason_code=failure.code,
        source_status=source_status,
        recommendation=failure.recommendation,
        retryable=failure.retryable,
        confidence=0.98 if failure.recommendation == "deletion_candidate" else 0.85,
        evidence=evidence,
        detector_version=VIDEO_ROUTE_VERSION,
    )


__all__ = (
    "VIDEO_MIME_TYPES",
    "VIDEO_RETRYABLE_WARNING_CODES",
    "VIDEO_REVIEW_REASON_CODES",
    "VideoRoute",
    "VideoRouteConfig",
    "VideoRouteSummary",
    "search_video_state",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.video.route"
del _defined_value
