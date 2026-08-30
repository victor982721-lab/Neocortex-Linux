"""Incremental, bounded and resumable Whisper transcription for indexed media."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import zlib
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol

import xxhash

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)
from neocortex.platform_policy import sqlite_path_collation

from neocortex.workflow.actions.action_policy import same_snapshot
from .models import (
    AUDIO_ROUTE_VERSION,
    AudioProcessingError,
    AudioRouteConfig,
    AudioRouteSummary,
    MediaProbe,
    TranscriptResult,
    WhisperRuntime,
)
from .probe import probe_media
from .state import audio_database, initialize_audio_state
from .whisper import WhisperTranscriber, resolve_whisper_runtime
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot as _file_key
from neocortex.runtime.control.memory_runtime import MemoryResourceLimits, WeightedMemoryGate
from neocortex.workflow.review.review import ReviewCandidate
from neocortex.persistence.framework_route_state import (
    FrameworkRouteState,
    ReviewCandidateReconciliation,
)


# region [01] Media contracts and injectable transcription boundary


AUDIO_MIME_TYPES = frozenset(
    {
        "application/ogg",
        "audio/aac",
        "audio/amr",
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/webm",
        "audio/x-aiff",
        "audio/x-caf",
        "audio/x-ms-wma",
    }
)
VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
        "video/x-msvideo",
    }
)
AUDIO_COMMIT_BATCH = 8
_PATH_COLLATION = sqlite_path_collation()
AUDIO_REVIEW_REASON_CODES = frozenset(
    {
        "audio_duration_limit",
        "audio_duration_unknown",
        "audio_invalid_container",
        "audio_io_error",
        "audio_probe_error",
        "audio_probe_invalid_json",
        "audio_probe_output_limit",
        "audio_probe_schema",
        "audio_probe_timeout",
        "audio_segment_limit",
        "audio_source_changed",
        "audio_transcript_char_limit",
        "audio_transcription_error",
        "audio_transcription_timeout",
        "media_without_audio_stream",
    }
)


class Transcriber(Protocol):
    def transcribe(
        self,
        path: Path,
        *,
        cancellation: CancellationToken,
    ) -> TranscriptResult: ...

    def close(self) -> None: ...


RuntimeResolver = Callable[[str, str], WhisperRuntime]
TranscriberFactory = Callable[[AudioRouteConfig, WhisperRuntime], Transcriber]
ProbeFunction = Callable[..., MediaProbe]


def _default_transcriber_factory(
    config: AudioRouteConfig,
    runtime: WhisperRuntime,
) -> Transcriber:
    return WhisperTranscriber(config, runtime)


# endregion [01]


# region [02] Route coordinator


@dataclass(slots=True)
class _AudioRunMetrics:
    candidate_pool: int = 0
    eligible: int = 0
    selected: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    transcribed: int = 0
    no_speech: int = 0
    no_audio: int = 0
    errors: int = 0
    reviews: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    transcript_chars: int = 0
    transcript_segments: int = 0
    media_seconds: float = 0.0
    speech_seconds: float = 0.0
    pruned: int = 0

    def record_result(self, probe: MediaProbe, result: TranscriptResult) -> None:
        if result.text:
            self.transcribed += 1
        else:
            self.no_speech += 1
        self.transcript_chars += len(result.text)
        self.transcript_segments += len(result.segments)
        self.media_seconds += probe.duration_seconds
        self.speech_seconds += result.speech_duration_seconds

    def record_failure(self, failure: AudioProcessingError) -> None:
        self.errors += 1
        self.reviews += 1
        self.deletion_candidates += int(failure.recommendation == "deletion_candidate")
        self.retryable_errors += int(failure.retryable)


@dataclass(slots=True)
class _AudioReviewBuffer:
    framework_state: FrameworkRouteState
    run_id: int
    reconciliations: list[ReviewCandidateReconciliation] = field(default_factory=list)

    def queue_success(self, snapshot: FileSnapshot, note: str) -> None:
        self.reconciliations.append(
            ReviewCandidateReconciliation(
                snapshot=snapshot,
                resolution_note=note,
                evaluated_reason_codes=tuple(sorted(AUDIO_REVIEW_REASON_CODES)),
            )
        )

    def store_failure(
        self,
        snapshot: FileSnapshot,
        failure: AudioProcessingError,
    ) -> None:
        self.framework_state.store_review_candidates(
            self.run_id,
            (_review_candidate(snapshot, failure),),
        )

    def flush(self) -> None:
        if not self.reconciliations:
            return
        self.framework_state.reconcile_review_candidates_batch(
            self.run_id,
            "audio",
            tuple(self.reconciliations),
        )
        self.reconciliations.clear()


class _TranscriberLease:
    """Own the lazily admitted model and close it before its memory lease."""

    def __init__(self, route: AudioRoute, runtime: WhisperRuntime) -> None:
        self._route = route
        self._runtime = runtime
        self._resources = ExitStack()
        self._transcriber: Transcriber | None = None

    def acquire(self) -> Transcriber:
        if self._transcriber is None:
            self._resources.enter_context(
                self._route.memory_gate.admit(_estimated_audio_memory_bytes(self._route.config))
            )
            self._transcriber = self._route.transcriber_factory(
                self._route.config,
                self._runtime,
            )
        return self._transcriber

    def close(self) -> None:
        try:
            if self._transcriber is not None:
                self._transcriber.close()
        finally:
            self._resources.close()


class AudioRoute:
    def __init__(
        self,
        config: AudioRouteConfig,
        framework_state: FrameworkRouteState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
        runtime_resolver: RuntimeResolver = resolve_whisper_runtime,
        transcriber_factory: TranscriberFactory = _default_transcriber_factory,
        media_probe: ProbeFunction = probe_media,
    ) -> None:
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.cancellation = cancellation or CancellationToken()
        self.runtime_resolver = runtime_resolver
        self.transcriber_factory = transcriber_factory
        self.media_probe = media_probe
        self.memory_gate = (
            memory_gate
            if memory_gate is not None
            else WeightedMemoryGate(
                MemoryResourceLimits(
                    memory_budget_bytes=config.memory_budget_bytes,
                    min_free_memory_bytes=config.min_free_memory_bytes,
                    min_free_commit_bytes=config.min_free_commit_bytes,
                    wait_timeout_seconds=config.memory_wait_timeout_seconds,
                ),
                self.cancellation,
            )
        )

    def _validate(self) -> None:
        positive_values = {
            "max_duration_seconds": self.config.max_duration_seconds,
            "max_transcript_chars": self.config.max_transcript_chars,
            "max_segments": self.config.max_segments,
            "beam_size": self.config.beam_size,
            "file_timeout_seconds": self.config.file_timeout_seconds,
            "worker_startup_timeout_seconds": (self.config.worker_startup_timeout_seconds),
            "worker_memory_bytes": self.config.worker_memory_bytes,
        }
        invalid = tuple(name for name, value in positive_values.items() if value <= 0)
        if invalid:
            raise ValueError(f"audio values must be positive: {', '.join(invalid)}")
        if self.config.max_documents is not None and self.config.max_documents < 1:
            raise ValueError("audio max_documents must be positive")
        if not self.config.model_name.strip():
            raise ValueError("Whisper model name must be non-empty")
        if self.config.language is not None and not self.config.language.strip():
            raise ValueError("audio language must be non-empty or automatic")

    def run(self) -> AudioRouteSummary:
        self.cancellation.checkpoint()
        self._validate()
        initialize_audio_state(self.config.state_path)
        runtime = self.runtime_resolver(self.config.device, self.config.compute_type)
        processing = self.config.processing_provenance(
            backend_version=runtime.backend_version,
            ctranslate2_version=runtime.ctranslate2_version,
            resolved_device=runtime.resolved_device,
            resolved_compute_type=runtime.resolved_compute_type,
        )
        ordered_mimes = self._ordered_mimes()
        metrics = self._plan(ordered_mimes)
        reviews = _AudioReviewBuffer(self.framework_state, self.run_id)
        lease = _TranscriberLease(self, runtime)
        try:
            with audio_database(self.config.state_path, create=False) as connection:
                self._run_candidates(
                    connection,
                    ordered_mimes,
                    processing.signature,
                    lease,
                    metrics,
                    reviews,
                )
                self._finalize_database(connection, metrics, reviews)
        finally:
            lease.close()
        self._report(metrics, finished=True)
        return AudioRouteSummary(
            candidate_pool=metrics.candidate_pool,
            candidates=metrics.selected,
            skipped_by_size=metrics.candidate_pool - metrics.eligible,
            skipped_by_count=metrics.eligible - metrics.selected,
            processed=metrics.processed,
            cache_hits=metrics.cache_hits,
            cached_errors=metrics.cached_errors,
            transcribed=metrics.transcribed,
            no_speech=metrics.no_speech,
            no_audio=metrics.no_audio,
            errors=metrics.errors,
            cache_documents_pruned=metrics.pruned,
            review_candidates=metrics.reviews,
            deletion_candidates=metrics.deletion_candidates,
            retryable_errors=metrics.retryable_errors,
            transcript_chars=metrics.transcript_chars,
            transcript_segments=metrics.transcript_segments,
            media_seconds=metrics.media_seconds,
            speech_seconds=metrics.speech_seconds,
            peak_reserved_bytes=self.memory_gate.peak_reserved_bytes,
            memory_waits=self.memory_gate.wait_count,
            processing_signature=processing.signature,
            processing_provenance=processing.manifest,
        )

    def _ordered_mimes(self) -> tuple[str, ...]:
        mime_types = set(AUDIO_MIME_TYPES)
        if self.config.include_video:
            mime_types.update(VIDEO_MIME_TYPES)
        return tuple(sorted(mime_types))

    def _plan(self, ordered_mimes: tuple[str, ...]) -> _AudioRunMetrics:
        totals = [
            self.framework_state.selected_route_candidate_counts(
                self.run_id,
                mime,
                self.config.max_file_bytes,
                "audio",
                self.config.selection,
            )
            for mime in ordered_mimes
        ]
        candidate_pool = sum(item[0] for item in totals)
        eligible = sum(item[1] for item in totals)
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return _AudioRunMetrics(candidate_pool, eligible, selected)

    def _run_candidates(
        self,
        connection: sqlite3.Connection,
        ordered_mimes: tuple[str, ...],
        signature: str,
        lease: _TranscriberLease,
        metrics: _AudioRunMetrics,
        reviews: _AudioReviewBuffer,
    ) -> None:
        for mime in ordered_mimes:
            iterator = self.framework_state.iter_selected_route_candidates(
                self.run_id,
                mime,
                "audio",
                self.config.selection,
            )
            for snapshot in iterator:
                if metrics.processed >= metrics.selected:
                    break
                self.cancellation.checkpoint()
                if self._exceeds_file_limit(snapshot):
                    continue
                _store_inventory(connection, snapshot, mime, self.run_id)
                cached = _cached_document(connection, snapshot, signature)
                if self._consume_cached(connection, snapshot, mime, cached, metrics, reviews):
                    self._commit_batch(connection, metrics, reviews)
                    continue
                self._transcribe_candidate(
                    connection,
                    snapshot,
                    mime,
                    signature,
                    lease,
                    metrics,
                    reviews,
                )
                self._commit_batch(connection, metrics, reviews)
            if metrics.processed >= metrics.selected:
                break

    def _consume_cached(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        cached: sqlite3.Row | None,
        metrics: _AudioRunMetrics,
        reviews: _AudioReviewBuffer,
    ) -> bool:
        if cached is None:
            return False
        status = str(cached["status"])
        benign_statuses = {"complete", "no_speech", "no_audio"}
        if status not in benign_statuses and self.config.retry_errors:
            return False
        _refresh_cached_path(connection, snapshot, mime, self.run_id)
        metrics.cache_hits += 1
        if status in benign_statuses:
            reviews.queue_success(
                snapshot,
                "current media cache completed successfully",
            )
            metrics.no_audio += int(status == "no_audio")
        else:
            failure = _cached_failure(cached)
            reviews.store_failure(snapshot, failure)
            metrics.cached_errors += 1
            metrics.reviews += 1
            metrics.deletion_candidates += int(failure.recommendation == "deletion_candidate")
            metrics.retryable_errors += int(failure.retryable)
        metrics.processed += 1
        return True

    def _transcribe_candidate(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        signature: str,
        lease: _TranscriberLease,
        metrics: _AudioRunMetrics,
        reviews: _AudioReviewBuffer,
    ) -> None:
        try:
            probe, result = self._transcribe(snapshot, lease)
            _store_success(
                connection,
                snapshot,
                mime,
                signature,
                probe,
                result,
                self.run_id,
            )
            reviews.queue_success(
                snapshot,
                "Media probing and transcription completed",
            )
            metrics.record_result(probe, result)
        except AudioProcessingError as exc:
            video_streams = exc.evidence.get("video_streams")
            visual_only_video = (
                mime in VIDEO_MIME_TYPES
                and exc.code == "media_without_audio_stream"
                and not isinstance(video_streams, bool)
                and isinstance(video_streams, int)
                and video_streams > 0
            )
            if visual_only_video:
                _store_no_audio(
                    connection,
                    snapshot,
                    mime,
                    signature,
                    self.run_id,
                    exc,
                )
                reviews.queue_success(
                    snapshot,
                    "video has no audio stream; transcription abstained",
                )
                metrics.no_audio += 1
            else:
                self._store_failure(connection, snapshot, mime, signature, exc, metrics, reviews)
        except (OSError, sqlite3.Error) as exc:
            failure = AudioProcessingError(
                "audio_io_error",
                f"{type(exc).__name__}: {exc}",
                recommendation="retry",
                retryable=True,
            )
            self._store_failure(connection, snapshot, mime, signature, failure, metrics, reviews)
        metrics.processed += 1

    def _transcribe(
        self,
        snapshot: FileSnapshot,
        lease: _TranscriberLease,
    ) -> tuple[MediaProbe, TranscriptResult]:
        current = snapshot_path(snapshot.path)
        if not same_snapshot(snapshot, current):
            raise AudioProcessingError(
                "audio_source_changed",
                "media source changed after inventory",
                recommendation="retry",
                retryable=True,
            )
        probe = self.media_probe(
            Path(snapshot.path),
            ffprobe_path=self.config.ffprobe_path,
        )
        self._validate_duration(probe)
        result = lease.acquire().transcribe(
            Path(snapshot.path),
            cancellation=self.cancellation,
        )
        final = snapshot_path(snapshot.path)
        if not same_snapshot(snapshot, final):
            raise AudioProcessingError(
                "audio_source_changed",
                "media source changed during transcription",
                recommendation="retry",
                retryable=True,
            )
        return probe, result

    def _validate_duration(self, probe: MediaProbe) -> None:
        if probe.duration_seconds <= self.config.max_duration_seconds:
            return
        raise AudioProcessingError(
            "audio_duration_limit",
            "media duration exceeds configured limit: "
            f"{probe.duration_seconds:.3f} > "
            f"{self.config.max_duration_seconds:.3f} seconds",
            recommendation="manual_review",
            retryable=False,
            evidence={
                "duration_seconds": probe.duration_seconds,
                "limit_seconds": self.config.max_duration_seconds,
            },
        )

    def _store_failure(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        signature: str,
        failure: AudioProcessingError,
        metrics: _AudioRunMetrics,
        reviews: _AudioReviewBuffer,
    ) -> None:
        _store_error(
            connection,
            snapshot,
            mime,
            signature,
            self.run_id,
            failure,
        )
        reviews.store_failure(snapshot, failure)
        metrics.record_failure(failure)

    def _commit_batch(
        self,
        connection: sqlite3.Connection,
        metrics: _AudioRunMetrics,
        reviews: _AudioReviewBuffer,
    ) -> None:
        if metrics.processed % AUDIO_COMMIT_BATCH != 0:
            return
        connection.commit()
        reviews.flush()
        self._report(metrics)

    def _finalize_database(
        self,
        connection: sqlite3.Connection,
        metrics: _AudioRunMetrics,
        reviews: _AudioReviewBuffer,
    ) -> None:
        connection.commit()
        reviews.flush()
        if self._should_prune():
            metrics.pruned = _prune_stale_documents(connection, self.run_id)
            connection.commit()

    def _should_prune(self) -> bool:
        return (
            not self.config.selection.active
            and self.config.max_documents is None
            and self.config.max_file_bytes is None
        )

    def _exceeds_file_limit(self, snapshot: FileSnapshot) -> bool:
        return self.config.max_file_bytes is not None and snapshot.size > self.config.max_file_bytes

    def _report(self, metrics: _AudioRunMetrics, *, finished: bool = False) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                "audio",
                "transcribe",
                "Audio indexado" if finished else "Transcribiendo audio",
                metrics.processed,
                metrics.selected,
                "archivos",
                finished,
                (
                    ProgressMetric("cache_hits", metrics.cache_hits),
                    ProgressMetric("cached_errors", metrics.cached_errors),
                    ProgressMetric("errors", metrics.errors),
                    ProgressMetric("completed_work", metrics.transcribed + metrics.no_speech),
                    ProgressMetric("transcript_chars", metrics.transcript_chars),
                    ProgressMetric("memory_waits", self.memory_gate.wait_count),
                ),
            ),
        )


# endregion [02]


# region [03] Durable cache operations


def _estimated_audio_memory_bytes(config: AudioRouteConfig) -> int:
    normalized_model = config.model_name.casefold()
    if "large" in normalized_model:
        estimate_mib = 4096
    elif "medium" in normalized_model:
        estimate_mib = 2560
    elif "small" in normalized_model:
        estimate_mib = 1280
    elif "base" in normalized_model:
        estimate_mib = 768
    else:
        estimate_mib = 512
    return min(config.worker_memory_bytes, estimate_mib * 1024 * 1024)


def _store_inventory(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    run_id: int,
) -> None:
    key = _file_key(snapshot)
    connection.execute(
        f"DELETE FROM audio_inventory WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
        (snapshot.path, key),
    )
    connection.execute(
        """INSERT INTO audio_inventory(
        file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_key) DO UPDATE SET
        path=excluded.path,mime=excluded.mime,size=excluded.size,
        mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
        last_seen_run_id=excluded.last_seen_run_id""",
        (
            key,
            snapshot.path,
            mime,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            run_id,
        ),
    )


def _cached_document(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    processing_signature: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT status,error_type,error_message,retryable,review_disposition
        FROM documents WHERE file_key=? AND size=? AND mtime_ns=?
        AND birthtime_ns=? AND processing_signature=?""",
        (
            _file_key(snapshot),
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
        ),
    ).fetchone()


def _remove_path_conflict(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
) -> None:
    conflict = connection.execute(
        f"SELECT file_key FROM documents WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
        (snapshot.path, _file_key(snapshot)),
    ).fetchone()
    if conflict is not None:
        key = str(conflict[0])
        connection.execute("DELETE FROM transcript_fts WHERE file_key=?", (key,))
        connection.execute("DELETE FROM documents WHERE file_key=?", (key,))


def _refresh_cached_path(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    run_id: int,
) -> None:
    _remove_path_conflict(connection, snapshot)
    key = _file_key(snapshot)
    connection.execute(
        """UPDATE documents SET mime=?,path=?,last_seen_run_id=?,updated_ns=?
        WHERE file_key=?""",
        (mime, snapshot.path, run_id, time.time_ns(), key),
    )
    connection.execute(
        "UPDATE transcript_fts SET path=? WHERE file_key=?",
        (snapshot.path, key),
    )


def _probe_metadata(probe: MediaProbe) -> dict[str, object]:
    return {
        "format_name": probe.format_name,
        "audio_codec": probe.audio_codec,
        "sample_rate": probe.sample_rate,
        "channels": probe.channels,
        "audio_streams": probe.audio_streams,
        "video_streams": probe.video_streams,
    }


def _store_success(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    processing_signature: str,
    probe: MediaProbe,
    result: TranscriptResult,
    run_id: int,
) -> None:
    _remove_path_conflict(connection, snapshot)
    key = _file_key(snapshot)
    title = Path(snapshot.path).stem
    text_bytes = result.text.encode("utf-8")
    fingerprint = xxhash.xxh3_128_hexdigest(text_bytes) if text_bytes else None
    status = "complete" if result.text else "no_speech"
    metadata = _probe_metadata(probe)
    metadata["transcription_duration_seconds"] = result.duration_seconds
    connection.execute(
        """INSERT INTO documents(
        file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
        title,duration_seconds,speech_duration_seconds,language,
        language_probability,model_name,backend_version,device,compute_type,
        media_metadata_json,text_zlib,text_chars,text_xxh3_128,segment_count,
        error_type,error_message,retryable,review_disposition,last_seen_run_id,
        updated_ns)
        VALUES(?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,'none',?,?)
        ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,mime=excluded.mime,
        size=excluded.size,mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns,
        processing_signature=excluded.processing_signature,status=excluded.status,
        title=excluded.title,duration_seconds=excluded.duration_seconds,
        speech_duration_seconds=excluded.speech_duration_seconds,
        language=excluded.language,
        language_probability=excluded.language_probability,
        model_name=excluded.model_name,backend_version=excluded.backend_version,
        device=excluded.device,compute_type=excluded.compute_type,
        media_metadata_json=excluded.media_metadata_json,
        text_zlib=excluded.text_zlib,text_chars=excluded.text_chars,
        text_xxh3_128=excluded.text_xxh3_128,
        segment_count=excluded.segment_count,error_type=NULL,error_message=NULL,
        retryable=0,review_disposition='none',
        last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
        (
            key,
            snapshot.path,
            mime,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
            status,
            title,
            probe.duration_seconds,
            result.speech_duration_seconds,
            result.language,
            result.language_probability,
            result.model_name,
            result.backend_version,
            result.device,
            result.compute_type,
            json.dumps(metadata, ensure_ascii=False, allow_nan=False),
            zlib.compress(text_bytes, 6) if text_bytes else None,
            len(result.text),
            fingerprint,
            len(result.segments),
            run_id,
            time.time_ns(),
        ),
    )
    connection.execute("DELETE FROM segments WHERE file_key=?", (key,))
    connection.executemany(
        """INSERT INTO segments(file_key,segment_index,start_ms,end_ms,text,
        avg_logprob,no_speech_probability) VALUES(?,?,?,?,?,?,?)""",
        (
            (
                key,
                segment.index,
                segment.start_ms,
                segment.end_ms,
                segment.text,
                segment.avg_logprob,
                segment.no_speech_probability,
            )
            for segment in result.segments
        ),
    )
    connection.execute("DELETE FROM transcript_fts WHERE file_key=?", (key,))
    if result.text:
        connection.execute(
            """INSERT INTO transcript_fts(file_key,path,title,body)
            VALUES(?,?,?,?)""",
            (key, snapshot.path, title, result.text),
        )


def _store_error(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    processing_signature: str,
    run_id: int,
    error: AudioProcessingError,
) -> None:
    _remove_path_conflict(connection, snapshot)
    key = _file_key(snapshot)
    metadata = {"evidence": error.evidence}
    connection.execute(
        """INSERT INTO documents(
        file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
        title,duration_seconds,speech_duration_seconds,language,
        language_probability,model_name,backend_version,device,compute_type,
        media_metadata_json,text_zlib,text_chars,text_xxh3_128,segment_count,
        error_type,error_message,retryable,review_disposition,last_seen_run_id,
        updated_ns)
        VALUES(?,?,?,?,?,?,?,'error',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
        ?,NULL,0,NULL,0,?,?,?,?,?,?)
        ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,mime=excluded.mime,
        size=excluded.size,mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns,
        processing_signature=excluded.processing_signature,status='error',
        title=NULL,duration_seconds=NULL,speech_duration_seconds=NULL,
        language=NULL,language_probability=NULL,model_name=NULL,
        backend_version=NULL,device=NULL,compute_type=NULL,
        media_metadata_json=excluded.media_metadata_json,text_zlib=NULL,
        text_chars=0,text_xxh3_128=NULL,segment_count=0,
        error_type=excluded.error_type,error_message=excluded.error_message,
        retryable=excluded.retryable,
        review_disposition=excluded.review_disposition,
        last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
        (
            key,
            snapshot.path,
            mime,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
            json.dumps(metadata, ensure_ascii=False, allow_nan=False),
            error.code,
            str(error)[:2000],
            int(error.retryable),
            error.recommendation,
            run_id,
            time.time_ns(),
        ),
    )
    connection.execute("DELETE FROM segments WHERE file_key=?", (key,))
    connection.execute("DELETE FROM transcript_fts WHERE file_key=?", (key,))


def _store_no_audio(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    processing_signature: str,
    run_id: int,
    error: AudioProcessingError,
) -> None:
    """Cache a benign abstention for a video container with no audio track."""

    _store_error(
        connection,
        snapshot,
        mime,
        processing_signature,
        run_id,
        error,
    )
    duration = error.evidence.get("duration_seconds")
    stored_duration = (
        float(duration)
        if not isinstance(duration, bool)
        and isinstance(duration, (int, float))
        and math.isfinite(duration)
        and duration >= 0
        else None
    )
    connection.execute(
        """UPDATE documents SET status='no_audio',title=?,duration_seconds=?,
        error_type=NULL,error_message=NULL,retryable=0,review_disposition='none'
        WHERE file_key=?""",
        (
            Path(snapshot.path).stem,
            stored_duration,
            _file_key(snapshot),
        ),
    )


def _cached_failure(row: sqlite3.Row) -> AudioProcessingError:
    stored_recommendation = str(row["review_disposition"])
    recommendation: Literal["retry", "manual_review", "deletion_candidate"]
    if stored_recommendation == "retry":
        recommendation = "retry"
    elif stored_recommendation == "deletion_candidate":
        recommendation = "deletion_candidate"
    else:
        recommendation = "manual_review"
    return AudioProcessingError(
        str(row["error_type"] or "audio_cached_error"),
        str(row["error_message"] or "cached audio error"),
        recommendation=recommendation,
        retryable=bool(row["retryable"]),
    )


def _review_candidate(
    snapshot: FileSnapshot,
    error: AudioProcessingError,
) -> ReviewCandidate:
    evidence: dict[str, object] = {
        "message": str(error)[:512],
        "route_version": AUDIO_ROUTE_VERSION,
    }
    evidence.update(error.evidence)
    return ReviewCandidate(
        route_name="audio",
        snapshot=snapshot,
        reason_code=error.code,
        source_status="error",
        recommendation=error.recommendation,
        retryable=error.retryable,
        confidence=0.98 if error.recommendation == "deletion_candidate" else 0.85,
        evidence=evidence,
        detector_version=AUDIO_ROUTE_VERSION,
    )


def _prune_stale_documents(connection: sqlite3.Connection, run_id: int) -> int:
    stale_keys = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT file_key FROM documents WHERE last_seen_run_id<>?", (run_id,)
        )
    )
    for offset in range(0, len(stale_keys), 256):
        batch = stale_keys[offset : offset + 256]
        placeholders = ",".join("?" for _ in batch)
        connection.execute(f"DELETE FROM transcript_fts WHERE file_key IN ({placeholders})", batch)
        connection.execute(f"DELETE FROM documents WHERE file_key IN ({placeholders})", batch)
    connection.execute("DELETE FROM audio_inventory WHERE last_seen_run_id<>?", (run_id,))
    return len(stale_keys)


# endregion [03]


# region [04] Read-only full-text search


def search_audio_state(path: Path, query: str, limit: int = 20) -> list[dict]:
    if not query.strip():
        raise ValueError("audio search query must be non-empty")
    if not 1 <= limit <= 1000:
        raise ValueError("audio search limit must be between 1 and 1000")
    with audio_database(path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT f.file_key,f.path,f.title,
            snippet(transcript_fts,3,'[',']',' ... ',24) AS snippet,
            d.language,d.duration_seconds,d.speech_duration_seconds,
            d.model_name,d.backend_version,d.device,d.compute_type
            FROM transcript_fts AS f
            JOIN documents AS d ON d.file_key=f.file_key
            WHERE transcript_fts MATCH ? AND d.status='complete'
            ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# endregion [04]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.audio.route"
del _defined_value
