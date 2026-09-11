"""Functional media defaults: durable cache repair and dependency boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from neocortex.capabilities.formats.audio.models import (
    AudioProcessingError,
    AudioRuntimeUnavailableError,
    AudioRouteConfig,
    MediaProbe,
    TranscriptResult,
    TranscriptSegment,
    WhisperRuntime,
    WhisperRuntimeError,
)
from neocortex.capabilities.formats.audio.route import AudioRoute, search_audio_state
from neocortex.capabilities.formats.audio.state import audio_database
from neocortex.capabilities.formats.video.frames import ExtractedVideoFrame, VideoFrameBatch
from neocortex.capabilities.formats.video.models import (
    VideoMediaProbe,
    VideoProcessingError,
    VideoRuntimeUnavailableError,
    VideoStreamProbe,
)
from neocortex.capabilities.formats.video.route import VideoRoute, VideoRouteConfig
from neocortex.capabilities.formats.video.state import search_video_state, video_database
from neocortex.deduplication import FileSnapshot, snapshot_path


RUNTIME = WhisperRuntime("fixture-backend", "fixture-ct2", 0, "cpu", "int8")
AUDIO_PROBE = MediaProbe(4.0, "ogg", "opus", 48_000, 1, 1, 0)


class _Framework:
    def __init__(
        self,
        mime: str,
        snapshot: FileSnapshot,
        *,
        snapshots: tuple[FileSnapshot, ...] | None = None,
    ) -> None:
        self.mime = mime
        self.snapshot = snapshot
        self.snapshots = snapshots or (snapshot,)
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
        if mime != self.mime:
            return 0, 0
        eligible = sum(
            max_file_bytes is None or item.size <= max_file_bytes for item in self.snapshots
        )
        return len(self.snapshots), eligible

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: object,
    ) -> Iterator[FileSnapshot]:
        if mime == self.mime:
            yield from self.snapshots

    def store_review_candidates(self, _run_id: int, candidates: tuple[object, ...]) -> None:
        self.reviews.extend(candidates)

    def reconcile_review_candidates_batch(
        self,
        _run_id: int,
        _route_name: str,
        reconciliations: tuple[object, ...],
    ) -> None:
        self.reconciliations.extend(reconciliations)


class _EmptyFramework:
    def selected_route_candidate_counts(
        self,
        _run_id: int,
        _mime: str,
        _max_file_bytes: int | None,
        _route_name: str,
        _selection: object,
    ) -> tuple[int, int]:
        return 0, 0

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        _mime: str,
        _route_name: str,
        _selection: object,
    ) -> Iterator[FileSnapshot]:
        yield from ()

    def store_review_candidates(self, _run_id: int, _candidates: tuple[object, ...]) -> None:
        return None

    def reconcile_review_candidates_batch(
        self,
        _run_id: int,
        _route_name: str,
        _reconciliations: tuple[object, ...],
    ) -> None:
        return None


class _MemoryGate:
    peak_reserved_bytes = 0
    wait_count = 0

    @contextmanager
    def admit(self, _amount: int):
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
        return None


def _audio_result(text: str) -> TranscriptResult:
    return TranscriptResult(
        text=text,
        language="es",
        language_probability=0.99,
        duration_seconds=4.0,
        speech_duration_seconds=2.0 if text else 0.0,
        segments=(TranscriptSegment(0, 500, 2500, text, -0.1, 0.01),) if text else (),
        model_name="small",
        backend_version=RUNTIME.backend_version,
        device=RUNTIME.resolved_device,
        compute_type=RUNTIME.resolved_compute_type,
    )


def _audio_result_with_segments(parts: tuple[str, ...]) -> TranscriptResult:
    return TranscriptResult(
        text=" ".join(parts),
        language="es",
        language_probability=0.99,
        duration_seconds=4.0,
        speech_duration_seconds=float(len(parts)),
        segments=tuple(
            TranscriptSegment(index, index * 1000, (index + 1) * 1000, text, -0.1, 0.01)
            for index, text in enumerate(parts)
        ),
        model_name="small",
        backend_version=RUNTIME.backend_version,
        device=RUNTIME.resolved_device,
        compute_type=RUNTIME.resolved_compute_type,
    )


def _audio_route(
    database: Path,
    framework: _Framework,
    transcriber: _Transcriber,
    *,
    run_id: int,
    media_probe=lambda *_args, **_kwargs: AUDIO_PROBE,
    factory=None,
    include_video: bool = False,
    runtime_resolver=None,
    retry_recoverable_errors: bool = False,
) -> AudioRoute:
    if factory is None:
        def factory(_config: object, _runtime: WhisperRuntime) -> _Transcriber:
            return transcriber
    return AudioRoute(
        AudioRouteConfig(
            state_path=database,
            include_video=include_video,
            retry_recoverable_errors=retry_recoverable_errors,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        run_id,
        memory_gate=_MemoryGate(),
        runtime_resolver=(runtime_resolver or (lambda _device, _compute: RUNTIME)),
        transcriber_factory=factory,
        media_probe=media_probe,
    )


def _video_probe() -> VideoMediaProbe:
    return VideoMediaProbe(
        4.0,
        "matroska,webm",
        (VideoStreamProbe(0, "vp9", 640, 360, 30.0, 4.0, None),),
        0,
        (),
        0,
    )


@dataclass(frozen=True)
class _OcrRuntime:
    enabled: bool = True
    signature: str = "fixture-ocr"
    provenance: str | None = "fixture-ocr"
    unavailable_reason: str | None = None
    processing_provenance_json: str | None = None


@dataclass(frozen=True)
class _OcrEvidence:
    attempted: bool = True
    available: bool = True
    recognized_text: str = "durable frame evidence"
    mean_confidence: float = 95.0
    provenance: str | None = "fixture-ocr"
    error_type: str | None = None
    error_message: str | None = None


def _video_route(
    database: Path,
    framework: _Framework,
    source_root: Path,
    *,
    run_id: int,
    sampler,
    frame_ocr=None,
    retry_recoverable_errors: bool = False,
) -> VideoRoute:
    if frame_ocr is None:
        def frame_ocr(*_args: object, **_kwargs: object) -> _OcrEvidence:
            return _OcrEvidence()
    config = VideoRouteConfig(
        state_path=database,
        root=source_root,
        max_frames=4,
        ocr_mode="auto",
        retry_recoverable_errors=retry_recoverable_errors,
    )
    return VideoRoute(
        config,
        framework,  # type: ignore[arg-type]
        run_id,
        media_probe=lambda *_args, **_kwargs: _video_probe(),
        frame_sampler=sampler,
        ocr_runtime_resolver=lambda _config: _OcrRuntime(),
        frame_ocr=frame_ocr,
    )


def test_audio_cache_replay_repairs_fts_and_document_derivatives_from_segments(
    tmp_path: Path,
) -> None:
    source = tmp_path / "grabacion.opus"
    source.write_bytes(b"deterministic audio fixture")
    database = tmp_path / "audio.sqlite3"
    framework = _Framework("audio/ogg", snapshot_path(source))
    parts = ("durable", "segment", "transcript")
    transcript = " ".join(parts)
    transcriber = _Transcriber(_audio_result_with_segments(parts))
    route = _audio_route(database, framework, transcriber, run_id=1)

    assert route.run().transcribed == 1
    with audio_database(database, create=False) as connection:
        connection.execute("DELETE FROM transcript_fts")
        connection.commit()

    forbidden = _Transcriber(_audio_result("must not run"))
    replay = _audio_route(
        database,
        framework,
        forbidden,
        run_id=2,
        factory=lambda *_args: (_ for _ in ()).throw(AssertionError("retranscribed")),
    ).run()

    assert replay.cache_hits == 1
    assert replay.transcribed == 1
    assert replay.transcript_chars == len(transcript)
    assert replay.transcript_segments == 3
    assert forbidden.calls == []
    assert search_audio_state(database, "durable transcript", 5)[0]["path"] == str(source)
    with audio_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT text_chars,segment_count,text_zlib,text_xxh3_128 FROM documents"
        ).fetchone()
        assert tuple(row[:2]) == (len(transcript), 3)
        assert row["text_zlib"] is not None
        assert row["text_xxh3_128"]
        assert connection.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0] == 1

        # The FTS-only replay above did not rewrite the durable segments.
    # Destroy the canonical validators as well as the FTS projection.  The
    # surviving segments cannot prove that they are the complete transcript.
    with audio_database(database, create=False) as connection:
        connection.execute(
            """UPDATE documents SET text_zlib=NULL,text_chars=0,
            text_xxh3_128=NULL,segment_count=0,speech_duration_seconds=0"""
        )
        connection.execute("DELETE FROM transcript_fts")
        connection.commit()
    second_transcriber = _Transcriber(_audio_result_with_segments(parts))
    second_replay = _audio_route(
        database,
        framework,
        second_transcriber,
        run_id=3,
    ).run()
    assert second_replay.cache_hits == 0
    assert second_replay.transcribed == 1
    assert second_transcriber.calls == [source]


@pytest.mark.parametrize("lost_index", (1, 2), ids=("intermediate", "final"))
def test_audio_segment_loss_does_not_reuse_a_shorter_complete_cache(
    tmp_path: Path,
    lost_index: int,
) -> None:
    source = tmp_path / "segments.opus"
    source.write_bytes(b"three segment audio fixture")
    database = tmp_path / "audio.sqlite3"
    parts = ("first", "intermediate", "final")
    snapshot = snapshot_path(source)
    framework = _Framework("audio/ogg", snapshot)
    first_transcriber = _Transcriber(_audio_result_with_segments(parts))
    assert _audio_route(database, framework, first_transcriber, run_id=1).run().transcribed == 1

    with audio_database(database, create=False) as connection:
        connection.execute("DELETE FROM segments WHERE segment_index=?", (lost_index,))
        connection.execute("DELETE FROM transcript_fts")
        connection.commit()

    replacement = _Transcriber(_audio_result_with_segments(parts))
    replay = _audio_route(database, framework, replacement, run_id=2).run()

    assert replay.cache_hits == 0
    assert replay.transcribed == 1
    assert replacement.calls == [source]
    with audio_database(database, readonly=True) as connection:
        assert connection.execute("SELECT segment_count FROM documents").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 3


def test_audio_segment_loss_with_lost_validators_is_not_repaired_from_remaining_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ambiguous.opus"
    source.write_bytes(b"ambiguous audio fixture")
    database = tmp_path / "audio.sqlite3"
    parts = ("first", "middle", "last")
    snapshot = snapshot_path(source)
    framework = _Framework("audio/ogg", snapshot)
    assert _audio_route(
        database,
        framework,
        _Transcriber(_audio_result_with_segments(parts)),
        run_id=1,
    ).run().transcribed == 1

    with audio_database(database, create=False) as connection:
        connection.execute("DELETE FROM segments WHERE segment_index=1")
        connection.execute(
            """UPDATE documents SET text_zlib=NULL,text_xxh3_128=NULL,
            text_chars=0,segment_count=2,media_metadata_json='{}'"""
        )
        connection.execute("DELETE FROM transcript_fts")
        connection.commit()

    replacement = _Transcriber(_audio_result_with_segments(parts))
    replay = _audio_route(database, framework, replacement, run_id=2).run()
    assert replay.cache_hits == 0
    assert replay.transcribed == 1
    assert replacement.calls == [source]


@pytest.mark.parametrize("status", ("no_speech", "no_audio"))
def test_audio_terminal_coverage_replay_does_not_create_empty_transcript(
    tmp_path: Path,
    status: str,
) -> None:
    source = tmp_path / f"{status}.{'mp3' if status == 'no_speech' else 'webm'}"
    source.write_bytes(b"deterministic terminal fixture")
    framework = _Framework(
        "audio/mpeg" if status == "no_speech" else "video/webm",
        snapshot_path(source),
    )
    database = tmp_path / "audio.sqlite3"
    if status == "no_audio":
        def probe(*_args: object, **_kwargs: object):
            raise AudioProcessingError(
                "media_without_audio_stream",
                "fixture has no audio",
                recommendation="manual_review",
                retryable=False,
                evidence={"duration_seconds": 4.0, "video_streams": 1},
            )

        route = _audio_route(
            database,
            framework,
            _Transcriber(_audio_result("must not run")),
            run_id=1,
            media_probe=probe,
            include_video=True,
        )
    else:
        route = _audio_route(
            database,
            framework,
            _Transcriber(_audio_result("")),
            run_id=1,
        )
    first = route.run()
    assert first.no_speech == (status == "no_speech")
    assert first.no_audio == (status == "no_audio")
    assert route.run().cache_hits == 1

    with audio_database(database, create=False) as connection:
        key = str(connection.execute("SELECT file_key FROM documents").fetchone()[0])
        connection.execute(
            "INSERT INTO transcript_fts(file_key,path,title,body) VALUES(?,?,?,?)",
            (key, str(source), source.stem, "false empty transcript"),
        )
        connection.commit()

    replay = route.run()
    assert replay.cache_hits == 1
    assert replay.no_speech == (status == "no_speech")
    assert replay.no_audio == (status == "no_audio")
    assert search_audio_state(database, "false", 5) == []
    with audio_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0] == 0


def test_video_cache_replay_repairs_fts_and_counters_from_durable_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe",
        lambda _path: "ffprobe",
    )
    source = tmp_path / "visual.webm"
    source.write_bytes(b"deterministic video fixture")
    snapshot = snapshot_path(source)
    framework = _Framework("video/webm", snapshot)
    database = tmp_path / "video.sqlite3"
    frames = tuple(
        ExtractedVideoFrame(
            index,
            1234 + index * 1000,
            ("interval",),
            tmp_path / f"ephemeral-{index}.png",
            640,
            360,
            f"{index + 1:032x}",
        )
        for index in range(3)
    )

    @contextmanager
    def sampler(*_args: Any, **_kwargs: Any):
        yield VideoFrameBatch(frames)

    assert _video_route(database, framework, tmp_path, run_id=1, sampler=sampler).run().complete == 1
    with video_database(database, create=False) as connection:
        connection.execute("DELETE FROM frame_fts")
        connection.commit()

    @contextmanager
    def forbidden_sampler(*_args: Any, **_kwargs: Any):
        raise AssertionError("decoded on cache replay")
        yield  # pragma: no cover

    replay = _video_route(
        database,
        framework,
        tmp_path,
        run_id=2,
        sampler=forbidden_sampler,
        frame_ocr=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OCR ran on cache replay")
        ),
    ).run()

    assert replay.cache_hits == 1
    assert replay.complete == 1
    assert replay.frames_sampled == 3
    assert replay.ocr_positive == 3
    assert search_video_state(database, "durable frame", 5)[0]["evidence"] == "00:00:01.234"
    with video_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT title,frame_count,ocr_frame_count,ocr_text_chars FROM documents"
        ).fetchone()
        assert tuple(row) == (source.stem, 3, 3, 3 * len("durable frame evidence"))
        assert connection.execute("SELECT COUNT(*) FROM frame_fts").fetchone()[0] == 3

    with video_database(database, create=False) as connection:
        connection.execute(
            "UPDATE documents SET title='wrong',frame_count=0,ocr_frame_count=0,ocr_text_chars=0"
        )
        connection.execute("DELETE FROM frame_fts")
        connection.commit()
    second_replay = _video_route(
        database,
        framework,
        tmp_path,
        run_id=3,
        sampler=sampler,
    ).run()
    assert second_replay.cache_hits == 0
    assert second_replay.complete == 1
    assert second_replay.frames_sampled == 3


@pytest.mark.parametrize("lost_index", (1, 2), ids=("intermediate", "final"))
def test_video_frame_loss_does_not_reuse_a_shorter_complete_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_index: int,
) -> None:
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe",
        lambda _path: "ffprobe",
    )
    source = tmp_path / "three-frames.webm"
    source.write_bytes(b"three frame video fixture")
    snapshot = snapshot_path(source)
    framework = _Framework("video/webm", snapshot)
    database = tmp_path / "video.sqlite3"
    frames = tuple(
        ExtractedVideoFrame(
            index,
            (index + 1) * 1000,
            ("interval",),
            tmp_path / f"ephemeral-{index}.png",
            640,
            360,
            f"{index + 1:032x}",
        )
        for index in range(3)
    )

    @contextmanager
    def sampler(*_args: Any, **_kwargs: Any):
        yield VideoFrameBatch(frames)

    assert _video_route(database, framework, tmp_path, run_id=1, sampler=sampler).run().complete == 1
    with video_database(database, create=False) as connection:
        connection.execute("DELETE FROM frames WHERE frame_index=?", (lost_index,))
        connection.execute("DELETE FROM frame_fts")
        connection.commit()

    replay = _video_route(database, framework, tmp_path, run_id=2, sampler=sampler).run()
    assert replay.cache_hits == 0
    assert replay.complete == 1
    assert replay.frames_sampled == 3
    with video_database(database, readonly=True) as connection:
        assert connection.execute("SELECT frame_count FROM documents").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3


@pytest.mark.parametrize(
    "damage",
    ("width", "digest", "probe_metadata"),
    ids=("zero_width", "invalid_digest", "invalid_probe_metadata"),
)
def test_video_invalid_durable_metadata_does_not_remain_cache_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe",
        lambda _path: "ffprobe",
    )
    source = tmp_path / "invalid-metadata.webm"
    source.write_bytes(b"invalid metadata video fixture")
    snapshot = snapshot_path(source)
    framework = _Framework("video/webm", snapshot)
    database = tmp_path / "video.sqlite3"
    frame = ExtractedVideoFrame(
        0,
        1000,
        ("interval",),
        tmp_path / "ephemeral.png",
        640,
        360,
        "a" * 32,
    )

    @contextmanager
    def sampler(*_args: Any, **_kwargs: Any):
        yield VideoFrameBatch((frame,))

    assert _video_route(database, framework, tmp_path, run_id=1, sampler=sampler).run().complete == 1
    with video_database(database, create=False) as connection:
        if damage == "width":
            connection.execute("UPDATE frames SET width=0")
        elif damage == "digest":
            connection.execute("UPDATE frames SET content_xxh3_128='invalid'")
        else:
            connection.execute("UPDATE documents SET probe_json='{}'")
        connection.commit()

    replay = _video_route(database, framework, tmp_path, run_id=2, sampler=sampler).run()
    assert replay.cache_hits == 0
    assert replay.complete == 1
    assert replay.frames_sampled == 1


def test_video_empty_selection_counts_before_missing_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def missing_ffmpeg(_path: str | None) -> str:
        calls.append("ffmpeg")
        raise FileNotFoundError("FFmpeg fixture missing")

    def missing_ffprobe(_path: str | None) -> str:
        calls.append("ffprobe")
        raise FileNotFoundError("FFprobe fixture missing")

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg", missing_ffmpeg
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe", missing_ffprobe
    )
    config = VideoRouteConfig(state_path=tmp_path / "video.sqlite3", root=tmp_path)
    summary = VideoRoute(config, _EmptyFramework(), 1).run()  # type: ignore[arg-type]

    assert summary.candidate_pool == summary.candidates == summary.processed == 0
    assert summary.errors == 0
    assert calls == []


def test_video_candidate_preserves_typed_missing_ffmpeg_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.mp4"
    source.write_bytes(b"candidate")
    framework = _Framework("video/mp4", snapshot_path(source))
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("FFmpeg fixture missing")),
    )

    with pytest.raises(VideoRuntimeUnavailableError, match="FFmpeg fixture missing"):
        VideoRoute(
            VideoRouteConfig(state_path=tmp_path / "video.sqlite3", root=tmp_path),
            framework,  # type: ignore[arg-type]
            1,
        ).run()


def test_video_candidate_preserves_typed_missing_ffprobe_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.mp4"
    source.write_bytes(b"candidate")
    framework = _Framework("video/mp4", snapshot_path(source))
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("FFprobe fixture missing")),
    )

    with pytest.raises(VideoRuntimeUnavailableError, match="FFprobe fixture missing") as raised:
        VideoRoute(
            VideoRouteConfig(state_path=tmp_path / "video.sqlite3", root=tmp_path),
            framework,  # type: ignore[arg-type]
            1,
        ).run()
    assert raised.value.capability_unavailable is True


def test_media_retry_configuration_defaults_and_opt_in() -> None:
    assert AudioRouteConfig(state_path=Path("audio.sqlite3")).retry_recoverable_errors is False
    assert AudioRouteConfig(
        state_path=Path("audio.sqlite3"), retry_recoverable_errors=True
    ).retry_recoverable_errors is True
    assert VideoRouteConfig(
        state_path=Path("video.sqlite3"), root=Path(".")
    ).retry_recoverable_errors is False
    assert VideoRouteConfig(
        state_path=Path("video.sqlite3"), root=Path("."), retry_recoverable_errors=True
    ).retry_recoverable_errors is True


def test_audio_retries_explicit_retryable_error_once_per_file_per_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "retry.opus"
    source.write_bytes(b"retryable audio fixture")
    snapshot = snapshot_path(source)
    database = tmp_path / "audio.sqlite3"
    initial = _Framework("audio/ogg", snapshot)
    attempts: list[Path] = []

    def failing_factory(_config: object, _runtime: WhisperRuntime):
        def transcribe(path: Path, *, cancellation) -> TranscriptResult:
            cancellation.checkpoint()
            attempts.append(path)
            raise AudioProcessingError(
                "audio_transcription_error",
                "retryable fixture failure",
                recommendation="retry",
                retryable=True,
            )

        return SimpleNamespace(transcribe=transcribe, close=lambda: None)

    first = _audio_route(
        database,
        initial,
        _Transcriber(_audio_result("unused")),
        run_id=1,
        factory=failing_factory,
    ).run()
    assert first.errors == 1
    assert len(attempts) == 1

    duplicate = _Framework("audio/ogg", snapshot, snapshots=(snapshot, snapshot))
    replay = _audio_route(
        database,
        duplicate,
        _Transcriber(_audio_result("unused")),
        run_id=2,
        factory=failing_factory,
        retry_recoverable_errors=True,
    ).run()

    assert replay.processed == 2
    assert replay.errors == 1
    assert replay.cached_errors == 1
    assert replay.cache_hits == 1
    assert len(attempts) == 2


@pytest.mark.parametrize("legacy_kind", ("manual", "unknown_retryability"))
def test_audio_auto_retry_requires_explicit_retry_metadata(
    tmp_path: Path,
    legacy_kind: str,
) -> None:
    source = tmp_path / f"{legacy_kind}.opus"
    source.write_bytes(b"retry metadata fixture")
    snapshot = snapshot_path(source)
    database = tmp_path / "audio.sqlite3"
    framework = _Framework("audio/ogg", snapshot)
    attempts: list[Path] = []

    def failing_factory(_config: object, _runtime: WhisperRuntime):
        def transcribe(path: Path, *, cancellation) -> TranscriptResult:
            cancellation.checkpoint()
            attempts.append(path)
            raise AudioProcessingError(
                "audio_transcription_error",
                "protected fixture failure",
                recommendation="manual_review" if legacy_kind == "manual" else "retry",
                retryable=True,
            )

        return SimpleNamespace(transcribe=transcribe, close=lambda: None)

    _audio_route(
        database,
        framework,
        _Transcriber(_audio_result("unused")),
        run_id=1,
        factory=failing_factory,
    ).run()
    if legacy_kind == "unknown_retryability":
        with audio_database(database, create=False) as connection:
            connection.execute("UPDATE documents SET retryable=2")
            connection.commit()

    def forbidden_factory(_config: object, _runtime: WhisperRuntime):
        raise AssertionError("manual or legacy error was retried")

    replay = _audio_route(
        database,
        framework,
        _Transcriber(_audio_result("unused")),
        run_id=2,
        factory=forbidden_factory,
        retry_recoverable_errors=True,
    ).run()
    assert replay.cache_hits == 1
    assert replay.cached_errors == 1
    assert replay.errors == 0
    assert len(attempts) == 1


def test_audio_runtime_absence_is_typed_but_protocol_errors_are_not_reclassified(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runtime.opus"
    source.write_bytes(b"runtime fixture")

    def missing_runtime(_device: str, _compute: str) -> WhisperRuntime:
        raise AudioRuntimeUnavailableError("local faster-whisper dependency is absent")

    with pytest.raises(AudioRuntimeUnavailableError, match="dependency is absent") as raised:
        _audio_route(
            tmp_path / "audio.sqlite3",
            _Framework("audio/ogg", snapshot_path(source)),
            _Transcriber(_audio_result("unused")),
            run_id=1,
            runtime_resolver=missing_runtime,
        ).run()
    assert raised.value.capability_unavailable is True
    assert isinstance(raised.value, WhisperRuntimeError)


def test_video_retries_explicit_retryable_error_once_per_file_per_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe",
        lambda _path: "ffprobe",
    )
    source = tmp_path / "retry.webm"
    source.write_bytes(b"retryable video fixture")
    snapshot = snapshot_path(source)
    database = tmp_path / "video.sqlite3"
    attempts: list[Path] = []

    @contextmanager
    def failing_sampler(path: Path, **_kwargs: object):
        attempts.append(path)
        raise VideoProcessingError(
            "video_frame_extract_error",
            "retryable fixture failure",
            recommendation="retry",
            retryable=True,
        )
        yield  # pragma: no cover

    first = _video_route(
        database,
        _Framework("video/webm", snapshot),
        tmp_path,
        run_id=1,
        sampler=failing_sampler,
    ).run()
    assert first.errors == 1
    assert len(attempts) == 1

    duplicate = _Framework("video/webm", snapshot, snapshots=(snapshot, snapshot))
    replay = _video_route(
        database,
        duplicate,
        tmp_path,
        run_id=2,
        sampler=failing_sampler,
        retry_recoverable_errors=True,
    ).run()
    assert replay.processed == 2
    assert replay.errors == 1
    assert replay.cached_errors == 1
    assert replay.cache_hits == 1
    assert len(attempts) == 2
