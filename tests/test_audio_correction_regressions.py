"""Regressions for audio cache provenance, retries and bounded replay.

These tests use private temporary state and deterministic transcriber/probe
doubles.  They exercise ``AudioRoute.run`` and inspect only the route owner's
SQLite state; no model or corpus is loaded.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from neocortex.capabilities.formats.audio.models import (
    AudioProcessingError,
    AudioRouteConfig,
    MediaProbe,
    TranscriptResult,
    TranscriptSegment,
    WhisperRuntime,
)
from neocortex.capabilities.formats.audio.route import AudioRoute
from neocortex.capabilities.formats.audio.state import audio_database
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.progress import ProgressEvent
from neocortex.runtime.control.cancellation import (
    CancellationRequested,
    CancellationToken,
)


RUNTIME = WhisperRuntime("fixture-backend", "fixture-ct2", 0, "cpu", "int8")
PROBE = MediaProbe(4.0, "ogg", "opus", 48_000, 1, 1, 0)


class _Framework:
    def __init__(
        self,
        candidates: dict[str, tuple[FileSnapshot, ...]],
        *,
        cancellation: CancellationToken | None = None,
        cancel_before_index: int | None = None,
    ) -> None:
        self.candidates = candidates
        self.cancellation = cancellation
        self.cancel_before_index = cancel_before_index
        self.reviews: list[object] = []
        self.reconciliations: list[object] = []

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: object,
    ) -> tuple[int, int]:
        values = self.candidates.get(mime, ())
        eligible = sum(max_file_bytes is None or item.size <= max_file_bytes for item in values)
        return len(values), eligible

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: object,
    ) -> Iterator[FileSnapshot]:
        for index, snapshot in enumerate(self.candidates.get(mime, ())):
            if (
                self.cancellation is not None
                and self.cancel_before_index is not None
                and index == self.cancel_before_index
            ):
                self.cancellation.cancel()
            yield snapshot

    def store_review_candidates(self, _run_id: int, candidates) -> None:
        self.reviews.extend(candidates)

    def reconcile_review_candidates_batch(
        self,
        _run_id: int,
        _route_name: str,
        reconciliations,
    ) -> None:
        self.reconciliations.extend(reconciliations)


class _MemoryGate:
    peak_reserved_bytes = 0
    wait_count = 0

    @contextmanager
    def admit(self, estimated_bytes: int):
        self.peak_reserved_bytes = max(self.peak_reserved_bytes, estimated_bytes)
        yield


class _Transcriber:
    def __init__(self, result: TranscriptResult | None = None, failure=None) -> None:
        self.result = result
        self.failure = failure
        self.calls: list[Path] = []

    def transcribe(self, path: Path, *, cancellation) -> TranscriptResult:
        cancellation.checkpoint()
        self.calls.append(path)
        if self.failure is not None:
            raise self.failure
        assert self.result is not None
        return self.result

    def close(self) -> None:
        return None


def _result(text: str = "Recovered transcript") -> TranscriptResult:
    return TranscriptResult(
        text=text,
        language="en",
        language_probability=0.99,
        duration_seconds=4.0,
        speech_duration_seconds=2.0 if text else 0.0,
        segments=(TranscriptSegment(0, 0, 2000, text, -0.1, 0.01),) if text else (),
        model_name="small",
        backend_version=RUNTIME.backend_version,
        device=RUNTIME.resolved_device,
        compute_type=RUNTIME.resolved_compute_type,
    )


def _route(
    database: Path,
    framework: _Framework,
    *,
    config_overrides: dict[str, object] | None = None,
    transcriber: _Transcriber | None = None,
    media_probe=None,
    cancellation: CancellationToken | None = None,
    progress: list[ProgressEvent] | None = None,
) -> AudioRoute:
    values: dict[str, Any] = {
        "state_path": database,
        "min_free_memory_bytes": 0,
        "min_free_commit_bytes": 0,
    }
    values.update(config_overrides or {})
    worker = transcriber or _Transcriber(_result())
    return AudioRoute(
        AudioRouteConfig(**values),
        framework,  # type: ignore[arg-type]
        1,
        memory_gate=_MemoryGate(),
        cancellation=cancellation,
        runtime_resolver=lambda _device, _compute: RUNTIME,
        transcriber_factory=lambda _config, _runtime: worker,
        media_probe=media_probe or (lambda *_args, **_kwargs: PROBE),
        progress=None if progress is None else progress.append,
    )


def test_audio_transcription_error_uses_effective_signature_and_invalidates_selectively(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "failure.opus"
    video = tmp_path / "visual-only.webm"
    audio.write_bytes(b"audio fixture")
    video.write_bytes(b"video fixture")
    database = tmp_path / "audio.sqlite3"
    framework = _Framework(
        {
            "audio/ogg": (snapshot_path(audio),),
            "video/webm": (snapshot_path(video),),
        }
    )

    def probe(path: Path, **_kwargs: object) -> MediaProbe:
        if path == video:
            raise AudioProcessingError(
                "media_without_audio_stream",
                "visual-only fixture",
                recommendation="manual_review",
                retryable=False,
                evidence={"duration_seconds": 4.0, "video_streams": 1},
            )
        return PROBE

    first_worker = _Transcriber(
        failure=AudioProcessingError(
            "audio_transcript_char_limit",
            "fixture transcript limit",
            recommendation="manual_review",
            retryable=False,
        )
    )
    first = _route(
        database,
        framework,
        config_overrides={"include_video": True, "max_transcript_chars": 10},
        transcriber=first_worker,
        media_probe=probe,
    ).run()
    assert first.errors == first.no_audio == 1
    assert len(first_worker.calls) == 1

    with audio_database(database, readonly=True) as connection:
        rows = {
            row["path"]: row
            for row in connection.execute(
                "SELECT path,processing_signature,status FROM documents ORDER BY path"
            )
        }
    assert rows[str(audio)]["status"] == "error"
    assert rows[str(audio)]["processing_signature"] == first.processing_signature
    assert rows[str(video)]["status"] == "no_audio"
    probe_signature = AudioRouteConfig(
        database, include_video=True, max_transcript_chars=10,
    ).probe_processing_provenance().signature
    assert rows[str(audio)]["processing_signature"] != probe_signature

    second_worker = _Transcriber(_result("changed limit works"))
    second_framework = _Framework(
        {
            "audio/ogg": (snapshot_path(audio),),
            "video/webm": (snapshot_path(video),),
        }
    )
    second = _route(
        database,
        second_framework,
        config_overrides={"include_video": True, "max_transcript_chars": 1000},
        transcriber=second_worker,
        media_probe=probe,
    ).run()

    # The transcription error is selective to the changed effective
    # signature, while the probe-only no-audio result remains a cache hit.
    assert second.transcribed == 1
    assert second.cache_hits == 1
    assert second.no_audio == 1
    assert second.cached_errors == 0
    assert second_worker.calls == [audio]
    with audio_database(database, readonly=True) as connection:
        statuses = {
            row["path"]: row["status"]
            for row in connection.execute("SELECT path,status FROM documents")
        }
    assert statuses == {str(audio): "complete", str(video): "no_audio"}


def test_ambiguous_legacy_error_is_not_deleted_during_selective_lookup(tmp_path: Path) -> None:
    source = tmp_path / "legacy.opus"
    source.write_bytes(b"legacy fixture")
    framework = _Framework({"audio/ogg": (snapshot_path(source),)})
    route = _route(tmp_path / "audio.sqlite3", framework)
    route.run()

    with audio_database(route.config.state_path, create=False) as connection:
        connection.execute(
            "UPDATE documents SET processing_signature='legacy-audio-unknown'"
        )
        connection.commit()
        before = connection.execute(
            "SELECT status,processing_signature FROM documents"
        ).fetchone()
    assert tuple(before) == ("complete", "legacy-audio-unknown")

    # The current signature misses the ambiguous legacy row, but cache lookup
    # is non-destructive: migration/invalidation must not erase evidence.
    current_signature = route.config.processing_provenance(
        backend_version=RUNTIME.backend_version,
        ctranslate2_version=RUNTIME.ctranslate2_version,
        resolved_device=RUNTIME.resolved_device,
        resolved_compute_type=RUNTIME.resolved_compute_type,
    ).signature
    with audio_database(route.config.state_path, readonly=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM documents WHERE processing_signature=?", (current_signature,)
        ).fetchone() is None
        after = connection.execute(
            "SELECT status,processing_signature FROM documents"
        ).fetchone()
    assert tuple(after) == tuple(before)


def test_audio_retry_success_replaces_error_and_next_run_is_a_hit(tmp_path: Path) -> None:
    source = tmp_path / "retry.opus"
    source.write_bytes(b"retry fixture")
    database = tmp_path / "audio.sqlite3"
    snapshot = snapshot_path(source)
    failing = _Transcriber(
        failure=AudioProcessingError(
            "audio_transcription_error",
            "temporary fixture failure",
            recommendation="retry",
            retryable=True,
        )
    )
    first = _route(
        database,
        _Framework({"audio/ogg": (snapshot,)}),
        transcriber=failing,
    ).run()
    assert first.errors == 1

    recovered = _Transcriber(_result("recovered"))
    retry = _route(
        database,
        _Framework({"audio/ogg": (snapshot,)}),
        config_overrides={"retry_recoverable_errors": True},
        transcriber=recovered,
    ).run()
    assert retry.transcribed == 1
    assert retry.cache_hits == retry.cached_errors == 0
    assert recovered.calls == [source]
    with audio_database(database, readonly=True) as connection:
        assert connection.execute(
            "SELECT status,error_type,retryable,review_disposition FROM documents"
        ).fetchone()[:4] == ("complete", None, 0, "none")

    forbidden = _Transcriber(failure=AssertionError("a valid retry must hit cache"))
    replay = _route(
        database,
        _Framework({"audio/ogg": (snapshot,)}),
        transcriber=forbidden,
    ).run()
    assert replay.cache_hits == replay.transcribed == 1
    assert forbidden.calls == []


def test_audio_effective_error_without_retry_is_reused_after_probe(tmp_path: Path) -> None:
    source = tmp_path / "stable-error.opus"
    source.write_bytes(b"stable error fixture")
    database = tmp_path / "audio.sqlite3"
    framework = _Framework({"audio/ogg": (snapshot_path(source),)})
    failed = _Transcriber(
        failure=AudioProcessingError(
            "audio_transcription_error",
            "non-retryable fixture failure",
            recommendation="manual_review",
            retryable=False,
        )
    )
    first = _route(database, framework, transcriber=failed).run()
    assert first.errors == 1

    forbidden = _Transcriber(failure=AssertionError("cached error must not transcribe"))
    replay = _route(
        database,
        _Framework({"audio/ogg": (snapshot_path(source),)}),
        transcriber=forbidden,
    ).run()

    assert replay.cache_hits == 1
    assert replay.cached_errors == 1
    assert replay.transcribed == replay.errors == 0
    assert forbidden.calls == []


def _seed_no_audio_state(
    database: Path,
    source_root: Path,
    count: int,
) -> tuple[FileSnapshot, ...]:
    sources = tuple(source_root / f"silent-{index}.webm" for index in range(count))
    for path in sources:
        path.write_bytes(b"video without audio")
    snapshots = tuple(snapshot_path(path) for path in sources)
    state = _Framework({"video/webm": snapshots})

    def no_audio_probe(*_args: object, **_kwargs: object) -> MediaProbe:
        raise AudioProcessingError(
            "media_without_audio_stream",
            "synthetic visual-only media",
            recommendation="manual_review",
            retryable=False,
            evidence={"duration_seconds": 1.0, "video_streams": 1},
        )

    summary = _route(
        database,
        state,
        config_overrides={"include_video": True},
        media_probe=no_audio_probe,
    ).run()
    assert summary.no_audio == count
    return snapshots


def test_audio_all_hit_replay_reports_and_flushes_bounded_batches(tmp_path: Path) -> None:
    database = tmp_path / "audio.sqlite3"
    snapshots = _seed_no_audio_state(database, tmp_path, 20)
    events: list[ProgressEvent] = []
    replay_state = _Framework({"video/webm": snapshots})
    replay = _route(
        database,
        replay_state,
        config_overrides={"include_video": True},
        progress=events,
    ).run()

    assert replay.cache_hits == replay.no_audio == 20
    assert len(replay_state.reconciliations) == 20
    assert [event.completed for event in events if not event.finished] == [8, 16]
    assert events[-1].finished and events[-1].completed == 20


def test_audio_no_audio_path_never_constructs_transcriber_pool(tmp_path: Path) -> None:
    database = tmp_path / "audio.sqlite3"
    snapshots = _seed_no_audio_state(database, tmp_path, 3)
    forbidden = _Transcriber(failure=AssertionError("no_audio must not load Whisper"))
    replay = _route(
        database,
        _Framework({"video/webm": snapshots}),
        config_overrides={"include_video": True},
        transcriber=forbidden,
    ).run()

    assert replay.cache_hits == replay.no_audio == 3
    assert forbidden.calls == []


def test_audio_all_hit_replay_checkpoints_cancellation_and_flushes_prefix(tmp_path: Path) -> None:
    database = tmp_path / "audio.sqlite3"
    snapshots = _seed_no_audio_state(database, tmp_path, 40)
    token = CancellationToken()
    replay_state = _Framework(
        {"video/webm": snapshots},
        cancellation=token,
        cancel_before_index=1,
    )
    events: list[ProgressEvent] = []
    route = _route(
        database,
        replay_state,
        config_overrides={"include_video": True},
        cancellation=token,
        progress=events,
    )

    with pytest.raises(CancellationRequested):
        route.run()

    # Only the first hit is observed before the iterator's next checkpoint;
    # the owner still commits and reconciles that prefix before propagating.
    assert len(replay_state.reconciliations) == 1
    assert events and events[-1].completed == 1 and not events[-1].finished
    with audio_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 40


def test_actual_legacy_probe_signed_transcription_error_is_recomputed_once(tmp_path: Path) -> None:
    """Seed the *real* pre-fix signature, not an unknown signature that always misses."""
    import json
    source = tmp_path / "legacy-error.opus"
    source.write_bytes(b"fixture")
    database = tmp_path / "audio.sqlite3"
    framework = _Framework({"audio/ogg": (snapshot_path(source),)})
    first = _route(database, framework, transcriber=_Transcriber(failure=AudioProcessingError(
        "audio_transcript_char_limit", "old limit", recommendation="manual_review", retryable=False)))
    first.run()
    with audio_database(database, create=False) as c:
        c.execute("UPDATE documents SET processing_signature=?,media_metadata_json=?", (
            first.config.probe_processing_provenance().signature, json.dumps({"evidence": {"limit": 10}})))
        c.commit()
    worker = _Transcriber(_result())
    repaired = _route(database, framework, transcriber=worker).run()
    replay = _route(database, framework, transcriber=worker).run()
    assert repaired.transcribed == 1 and repaired.cached_errors == 0
    assert replay.cache_hits == 1
    assert len(worker.calls) == 1
