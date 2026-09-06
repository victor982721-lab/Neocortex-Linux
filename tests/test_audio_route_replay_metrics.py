from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

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
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.safety.route_filters import CandidateSelection


RUNTIME = WhisperRuntime("1.2.1", "4.8.1", 0, "cpu", "int8")
PROBE = MediaProbe(12.5, "ogg", "opus", 48_000, 1, 1, 0)


class _FrameworkState:
    def __init__(self, mime: str, snapshot: FileSnapshot) -> None:
        self.mime = mime
        self.snapshot = snapshot
        self.reviews: list[object] = []
        self.resolutions: list[object] = []

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        if mime != self.mime:
            return 0, 0
        if max_file_bytes is not None and self.snapshot.size > max_file_bytes:
            return 1, 0
        return 1, 1

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ):
        if mime == self.mime:
            yield self.snapshot

    def store_review_candidates(self, _run_id: int, candidates) -> None:
        self.reviews.extend(candidates)

    def reconcile_review_candidates_batch(self, _run_id: int, _route_name: str, reconciliations) -> None:
        self.resolutions.extend(reconciliations)


class _MemoryGate:
    peak_reserved_bytes = 0
    wait_count = 0

    @contextmanager
    def admit(self, estimated_bytes: int):
        self.peak_reserved_bytes = max(self.peak_reserved_bytes, estimated_bytes)
        yield


class _Transcriber:
    def __init__(self, result: TranscriptResult) -> None:
        self.result = result
        self.calls: list[Path] = []

    def transcribe(self, path: Path, *, cancellation) -> TranscriptResult:
        cancellation.checkpoint()
        self.calls.append(path)
        return self.result

    def close(self) -> None:
        pass


def _result(text: str) -> TranscriptResult:
    segments = (TranscriptSegment(0, 250, 2250, text, -0.2, 0.01),) if text else ()
    return TranscriptResult(
        text=text,
        language="es",
        language_probability=0.99,
        duration_seconds=12.5,
        speech_duration_seconds=2.0 if text else 0.0,
        segments=segments,
        model_name="small",
        backend_version="1.2.1",
        device="cpu",
        compute_type="int8",
    )


def _route(
    state_path: Path,
    source: Path,
    *,
    mime: str = "audio/ogg",
    result: TranscriptResult | None = None,
    media_probe=lambda *_args, **_kwargs: PROBE,
    include_video: bool = False,
) -> tuple[AudioRoute, _FrameworkState, _Transcriber, list[bool]]:
    framework = _FrameworkState(mime, snapshot_path(source))
    transcriber = _Transcriber(result or _result("Transcripción durable"))
    factory_calls: list[bool] = []

    def factory(_config, _runtime):
        factory_calls.append(True)
        return transcriber

    route = AudioRoute(
        AudioRouteConfig(
            state_path=state_path,
            include_video=include_video,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        runtime_resolver=lambda _device, _compute: RUNTIME,
        transcriber_factory=factory,
        media_probe=media_probe,
        memory_gate=_MemoryGate(),
    )
    return route, framework, transcriber, factory_calls


def _data_metrics(summary) -> tuple[object, ...]:
    return tuple(
        getattr(summary, name)
        for name in (
            "processed",
            "transcribed",
            "no_speech",
            "no_audio",
            "errors",
            "review_candidates",
            "deletion_candidates",
            "retryable_errors",
            "transcript_chars",
            "transcript_segments",
            "media_seconds",
            "speech_seconds",
        )
    )


def test_audio_cache_replay_restores_transcript_metrics(tmp_path: Path) -> None:
    source = tmp_path / "grabacion.opus"
    source.write_bytes(b"OggS deterministic fixture")
    transcript = "Pruebas eléctricas del transformador en la subestación"
    route, _framework, transcriber, factory_calls = _route(
        tmp_path / "audio.sqlite3",
        source,
        result=_result(transcript),
    )

    first = route.run()
    replay = route.run()

    assert _data_metrics(replay) == _data_metrics(first)
    assert replay.cache_hits == 1
    assert replay.cached_errors == 0
    assert replay.transcribed == 1
    assert replay.transcript_chars == len(transcript)
    assert replay.transcript_segments == 1
    assert replay.media_seconds == 12.5
    assert replay.speech_seconds == 2.0
    assert len(transcriber.calls) == 1
    assert len(factory_calls) == 1


@pytest.mark.parametrize("kind", ("no_speech", "no_audio"))
def test_audio_cache_replay_restores_benign_terminal_metrics(
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / f"{kind}.{'webm' if kind == 'no_audio' else 'mp3'}"
    source.write_bytes(b"deterministic media fixture")

    if kind == "no_audio":
        def media_probe(*_args, **_kwargs):
            raise AudioProcessingError(
                "media_without_audio_stream",
                "the media container has no audio stream",
                recommendation="manual_review",
                retryable=False,
                evidence={"duration_seconds": 12.5, "video_streams": 1},
            )

        route_args = {
            "mime": "video/webm",
            "media_probe": media_probe,
            "include_video": True,
        }
    else:
        route_args = {"mime": "audio/mpeg", "result": _result("")}

    route, _framework, transcriber, factory_calls = _route(
        tmp_path / "audio.sqlite3",
        source,
        **route_args,
    )

    first = route.run()
    replay = route.run()

    assert _data_metrics(replay) == _data_metrics(first)
    assert replay.cache_hits == 1
    assert replay.cached_errors == 0
    assert replay.no_speech == (kind == "no_speech")
    assert replay.no_audio == (kind == "no_audio")
    assert len(transcriber.calls) == (1 if kind == "no_speech" else 0)
    assert len(factory_calls) == (1 if kind == "no_speech" else 0)


def test_audio_cache_replay_restores_error_metrics(tmp_path: Path) -> None:
    source = tmp_path / "dañado.opus"
    source.write_bytes(b"not an audio container")

    def corrupt_probe(*_args, **_kwargs):
        raise AudioProcessingError(
            "audio_invalid_container",
            "invalid audio container",
            recommendation="deletion_candidate",
            retryable=True,
        )

    route, framework, transcriber, factory_calls = _route(
        tmp_path / "audio.sqlite3",
        source,
        media_probe=corrupt_probe,
    )

    first = route.run()
    replay = route.run()

    assert replay.processed == first.processed == 1
    assert replay.transcribed == first.transcribed == 0
    assert replay.no_speech == first.no_speech == 0
    assert replay.no_audio == first.no_audio == 0
    assert replay.transcript_chars == first.transcript_chars == 0
    assert replay.transcript_segments == first.transcript_segments == 0
    assert replay.media_seconds == first.media_seconds == 0.0
    assert replay.speech_seconds == first.speech_seconds == 0.0
    assert first.errors == 1
    assert first.review_candidates == 1
    assert first.deletion_candidates == 1
    assert first.retryable_errors == 1
    assert replay.cache_hits == 1
    assert replay.cached_errors == 1
    assert replay.errors == 0
    assert replay.review_candidates == 1
    assert replay.deletion_candidates == 1
    assert replay.retryable_errors == 1
    assert len(framework.reviews) == 2
    assert transcriber.calls == []
    assert factory_calls == []
