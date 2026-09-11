"""Focused real-FFmpeg regression for the C6 video frame endpoint."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from neocortex.capabilities.formats.video.frames import (
    ExtractedVideoFrame,
    VideoFrameBatch,
    sampled_video_frames,
)
from neocortex.capabilities.formats.video.models import VideoMediaProbe, VideoStreamProbe
from neocortex.capabilities.formats.video.probe import probe_video
from neocortex.capabilities.formats.video.route import VideoRoute, VideoRouteConfig
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken


C6_VIDEO_ROOT = Path("/tmp/neocortex-functional-yNEGlQ/c6-fixtures/corpus/video")
C6_VIDEOS = (
    C6_VIDEO_ROOT / "c6_inspection.mp4",
    C6_VIDEO_ROOT / "c6_visual_audio.mkv",
)


@dataclass(frozen=True)
class _DisabledOcrRuntime:
    enabled: bool = False
    signature: str = "c6-test-ocr-disabled"
    provenance: str | None = None
    unavailable_reason: str | None = "test_disabled"
    processing_provenance_json: str | None = None


@dataclass(frozen=True)
class _NoOcrEvidence:
    attempted: bool = False
    available: bool = False
    recognized_text: str = ""
    mean_confidence: float = 0.0
    provenance: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class _SingleVideoCandidateState:
    def __init__(self, snapshot: FileSnapshot, mime: str) -> None:
        self.snapshot = snapshot
        self.mime = mime

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
        return 1, int(max_file_bytes is None or self.snapshot.size <= max_file_bytes)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: object,
    ) -> Iterator[FileSnapshot]:
        if mime == self.mime:
            yield self.snapshot

    def store_review_candidates(self, _run_id: int, _candidates: tuple[object, ...]) -> None:
        return None

    def reconcile_review_candidates_batch(
        self,
        _run_id: int,
        _route_name: str,
        _reconciliations: tuple[object, ...],
    ) -> None:
        return None


def _available_c6_video(path: Path) -> bool:
    return path.is_file()


@pytest.mark.parametrize("source", C6_VIDEOS, ids=("c6-mp4", "c6-mkv"))
@pytest.mark.skipif(
    not all(_available_c6_video(path) for path in C6_VIDEOS),
    reason="private C6 video fixtures are not present",
)
def test_c6_video_sampler_keeps_last_real_frame_without_partial_warning(
    tmp_path: Path,
    source: Path,
) -> None:
    before = source.read_bytes()
    probe = probe_video(source)
    primary = max(probe.video, key=lambda stream: (stream.width * stream.height, -stream.index))
    assert primary.duration_seconds is not None
    sampling_duration = min(probe.duration_seconds, primary.duration_seconds)
    with sampled_video_frames(
        source,
        corpus_root=C6_VIDEO_ROOT.parent,
        stream_index=primary.index,
        source_width=primary.width,
        source_height=primary.height,
        duration_seconds=sampling_duration,
        frame_rate=primary.frame_rate,
        config=VideoRouteConfig(
            state_path=tmp_path / "unused.sqlite3",
            root=C6_VIDEO_ROOT.parent,
            max_frames=8,
            interval_seconds=30.0,
            include_scenes=True,
            include_keyframes=True,
            ocr_mode="never",
        ).frame_sampling_config(),
        cancellation=CancellationToken(),
    ) as batch:
        retained = tuple(frame.path for frame in batch.frames)
        assert batch.warnings == ()
        assert tuple(frame.timestamp_ms for frame in batch.frames) == (0, 1000)
        assert all(frame.width == primary.width for frame in batch.frames)
        assert all(frame.height == primary.height for frame in batch.frames)
    assert all(not path.exists() for path in retained)
    assert source.read_bytes() == before


@pytest.mark.parametrize("source", C6_VIDEOS, ids=("c6-route-mp4", "c6-route-mkv"))
@pytest.mark.skipif(
    not all(_available_c6_video(path) for path in C6_VIDEOS),
    reason="private C6 video fixtures are not present",
)
def test_c6_video_route_passes_primary_duration_to_real_sampler(
    tmp_path: Path,
    source: Path,
) -> None:
    snapshot = snapshot_path(source)
    mime = "video/mp4" if source.suffix == ".mp4" else "video/x-matroska"
    state = _SingleVideoCandidateState(snapshot, mime)
    config = VideoRouteConfig(
        state_path=tmp_path / "video.sqlite3",
        root=C6_VIDEO_ROOT.parent,
        max_frames=8,
        interval_seconds=30.0,
        include_scenes=True,
        include_keyframes=True,
        ocr_mode="never",
    )
    summary = VideoRoute(
        config,
        state,  # type: ignore[arg-type]
        1,
        media_probe=probe_video,
        frame_sampler=sampled_video_frames,
        ocr_runtime_resolver=lambda _config: _DisabledOcrRuntime(),
        frame_ocr=lambda *_args, **_kwargs: _NoOcrEvidence(),
    ).run()

    assert summary.errors == 0
    assert summary.partial == 0
    assert summary.frames_sampled == 2
    assert summary.ocr_attempts == 0


def _fake_video_route(
    state: _SingleVideoCandidateState,
    state_path: Path,
    root: Path,
    *,
    run_id: int,
    sampler,
    retry_recoverable_errors: bool,
    frame_ocr=None,
) -> VideoRoute:
    if frame_ocr is None:
        def frame_ocr(*_args: object, **_kwargs: object) -> _NoOcrEvidence:
            return _NoOcrEvidence()
    return VideoRoute(
        VideoRouteConfig(
            state_path=state_path,
            root=root,
            max_frames=4,
            interval_seconds=1.0,
            include_scenes=False,
            include_keyframes=False,
            ocr_mode="never",
            retry_recoverable_errors=retry_recoverable_errors,
        ),
        state,  # type: ignore[arg-type]
        run_id,
        media_probe=lambda *_args, **_kwargs: VideoMediaProbe(
            2.0,
            "matroska,webm",
            (VideoStreamProbe(0, "vp9", 16, 16, 1.0, 2.0, None),),
            0,
            (),
            0,
        ),
        frame_sampler=sampler,
        ocr_runtime_resolver=lambda _config: _DisabledOcrRuntime(),
        frame_ocr=frame_ocr,
    )


def test_retryable_partial_cache_replays_once_and_becomes_complete(
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
    source = tmp_path / "partial.mp4"
    source.write_bytes(b"partial fixture")
    snapshot = snapshot_path(source)
    state = _SingleVideoCandidateState(snapshot, "video/mp4")
    state_path = tmp_path / "video.sqlite3"
    frame = ExtractedVideoFrame(0, 0, ("interval",), tmp_path / "frame.png", 16, 16, "a" * 32)

    @contextmanager
    def old_sampler(*_args: Any, **_kwargs: Any):
        yield VideoFrameBatch((frame,), ("video_frame_extract_error",))

    first = _fake_video_route(
        state,
        state_path,
        tmp_path,
        run_id=1,
        sampler=old_sampler,
        retry_recoverable_errors=False,
    ).run()
    assert first.partial == 1
    assert first.complete == 0

    calls: list[int] = []

    @contextmanager
    def new_sampler(*_args: Any, **_kwargs: Any):
        calls.append(1)
        yield VideoFrameBatch((frame,))

    replay = _fake_video_route(
        state,
        state_path,
        tmp_path,
        run_id=2,
        sampler=new_sampler,
        retry_recoverable_errors=True,
    ).run()
    assert replay.cache_hits == 0
    assert replay.complete == 1
    assert replay.partial == 0
    assert calls == [1]

    def forbidden_sampler(*_args: Any, **_kwargs: Any):
        raise AssertionError("complete cache was resampled")

    intact = _fake_video_route(
        state,
        state_path,
        tmp_path,
        run_id=3,
        sampler=forbidden_sampler,
        retry_recoverable_errors=True,
        frame_ocr=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete cache invoked OCR")
        ),
    ).run()
    assert intact.cache_hits == 1
    assert intact.complete == 1
    assert intact.partial == 0


def test_permanent_partial_frame_limit_is_not_auto_retried(
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
    source = tmp_path / "limited.mp4"
    source.write_bytes(b"permanent partial fixture")
    snapshot = snapshot_path(source)
    state = _SingleVideoCandidateState(snapshot, "video/mp4")
    state_path = tmp_path / "video.sqlite3"
    frame = ExtractedVideoFrame(0, 0, ("interval",), tmp_path / "frame.png", 16, 16, "a" * 32)

    @contextmanager
    def limiting_sampler(*_args: Any, **_kwargs: Any):
        yield VideoFrameBatch((frame,), ("video_frame_batch_byte_limit",))

    first = _fake_video_route(
        state,
        state_path,
        tmp_path,
        run_id=1,
        sampler=limiting_sampler,
        retry_recoverable_errors=False,
    ).run()
    assert first.partial == 1

    def forbidden_sampler(*_args: Any, **_kwargs: Any):
        raise AssertionError("permanent partial was auto-retried")

    replay = _fake_video_route(
        state,
        state_path,
        tmp_path,
        run_id=2,
        sampler=forbidden_sampler,
        retry_recoverable_errors=True,
    ).run()
    assert replay.cache_hits == 1
    assert replay.partial == 1
    assert replay.complete == 0
