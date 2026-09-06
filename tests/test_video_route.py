"""Vertical route contracts: visual-only, replay, warnings and hard limits."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.capabilities.formats.video.frames import ExtractedVideoFrame, VideoFrameBatch
from neocortex.capabilities.formats.video.models import VideoMediaProbe, VideoStreamProbe
from neocortex.capabilities.formats.video.route import VideoRoute, VideoRouteConfig
from neocortex.capabilities.formats.video.state import search_video_state, video_state_status


class _FrameworkState:
    def __init__(self, snapshot: FileSnapshot, mime: str = "video/webm") -> None:
        self.snapshot = snapshot
        self.mime = mime
        self.review_candidates: list[Any] = []
        self.reconciliations: list[Any] = []

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: Any,
    ) -> tuple[int, int]:
        if mime != self.mime:
            return (0, 0)
        eligible = int(max_file_bytes is None or self.snapshot.size <= max_file_bytes)
        return (1, eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: Any,
    ) -> Iterator[FileSnapshot]:
        if mime == self.mime:
            yield self.snapshot

    def store_review_candidates(self, _run_id: int, candidates: tuple[Any, ...]) -> None:
        self.review_candidates.extend(candidates)

    def reconcile_review_candidates_batch(
        self,
        _run_id: int,
        _route_name: str,
        reconciliations: tuple[Any, ...],
    ) -> None:
        self.reconciliations.extend(reconciliations)


class _MemoryGate:
    def __init__(self) -> None:
        self.admissions: list[int] = []
        self.peak_reserved_bytes = 0
        self.wait_count = 0

    @contextmanager
    def admit(self, estimated_bytes: int) -> Iterator[None]:
        self.admissions.append(estimated_bytes)
        self.peak_reserved_bytes = max(self.peak_reserved_bytes, estimated_bytes)
        yield


@dataclass(frozen=True)
class _Runtime:
    enabled: bool = True
    signature: str = "fixture-ocr-signature"
    provenance: str | None = "fixture-ocr"
    unavailable_reason: str | None = None
    processing_provenance_json: str | None = None


@dataclass(frozen=True)
class _Evidence:
    attempted: bool = True
    available: bool = True
    recognized_text: str = "presión interna transformador"
    mean_confidence: float = 93.0
    provenance: str | None = "fixture-ocr"
    error_type: str | None = None
    error_message: str | None = None


def _probe(*, duration: float = 12.0, audio_streams: int = 0) -> VideoMediaProbe:
    return VideoMediaProbe(
        duration,
        "matroska,webm",
        (VideoStreamProbe(0, "vp9", 640, 360, 30.0, duration, None),),
        audio_streams,
        (),
        0,
    )


def _snapshot(tmp_path: Path) -> FileSnapshot:
    source = tmp_path / "visual-only.webm"
    source.write_bytes(b"fixture-video-source")
    return snapshot_path(str(source))


def _frame(tmp_path: Path) -> ExtractedVideoFrame:
    return ExtractedVideoFrame(
        index=0,
        timestamp_ms=4321,
        reasons=("scene", "interval"),
        path=tmp_path / "ephemeral-frame.png",
        width=640,
        height=360,
        content_xxh3_128="a" * 32,
    )


def _config(tmp_path: Path) -> VideoRouteConfig:
    (tmp_path / "state").mkdir(exist_ok=True)
    return VideoRouteConfig(
        state_path=tmp_path / "state" / "video.sqlite3",
        root=tmp_path,
        audio_state_path=tmp_path / "state" / "audio.sqlite3",
        max_frames=4,
        ocr_mode="auto",
    )


def test_visual_only_video_is_searchable_and_second_run_is_a_true_cache_hit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    state = _FrameworkState(snapshot)
    memory_gate = _MemoryGate()
    calls = {"samples": 0, "ocr": 0}

    @contextmanager
    def sampler(*_args: Any, **_kwargs: Any):
        calls["samples"] += 1
        yield VideoFrameBatch((_frame(tmp_path),))

    def ocr(*_args: Any, **_kwargs: Any) -> _Evidence:
        calls["ocr"] += 1
        assert _args[2] is None
        return _Evidence()

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg", lambda _path: "ffmpeg"
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe", lambda _path: "ffprobe"
    )
    config = _config(tmp_path)
    first = VideoRoute(
        config,
        state,  # type: ignore[arg-type]
        1,
        memory_gate=memory_gate,
        media_probe=lambda *_args, **_kwargs: _probe(),
        frame_sampler=sampler,
        ocr_runtime_resolver=lambda _config: _Runtime(),
        frame_ocr=ocr,
    ).run()

    assert first.complete == 1
    assert first.visual_only == 1
    assert first.frames_sampled == 1
    assert first.scene_frames == first.interval_frames == 1
    assert first.ocr_positive == 1
    assert not first.errors
    assert calls == {"samples": 1, "ocr": 1}
    assert memory_gate.admissions == [config.worker_memory_bytes]
    assert first.processing_provenance is not None
    assert first.processing_provenance["configuration"]["ocr_profile"] == "configured"
    frame_ocr_component = next(
        component
        for component in first.processing_provenance["components"]
        if component["name"] == "frame-ocr"
    )
    assert frame_ocr_component["status"] == "available"
    assert search_video_state(config.state_path, "transformador")[0]["evidence"] == ("00:00:04.321")

    @contextmanager
    def forbidden_sampler(*_args: Any, **_kwargs: Any):
        raise AssertionError("cache replay attempted to decode video")
        yield  # type: ignore[unreachable]  # pragma: no cover

    second = VideoRoute(
        config,
        state,  # type: ignore[arg-type]
        2,
        memory_gate=memory_gate,
        media_probe=lambda *_args, **_kwargs: _probe(),
        frame_sampler=forbidden_sampler,
        ocr_runtime_resolver=lambda _config: _Runtime(),
        frame_ocr=ocr,
    ).run()

    assert second.cache_hits == 1
    assert second.complete == 1
    assert second.visual_only == 1
    assert second.frames_sampled == 1
    assert second.scene_frames == second.interval_frames == 1
    assert second.ocr_positive == 1
    assert calls == {"samples": 1, "ocr": 1}
    assert memory_gate.admissions == [config.worker_memory_bytes]
    assert state.reconciliations


def test_bounded_discovery_warning_publishes_partial_and_reviewable_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    state = _FrameworkState(snapshot)

    @contextmanager
    def sampler(*_args: Any, **_kwargs: Any):
        yield VideoFrameBatch((_frame(tmp_path),), ("video_frame_discovery_timeout",))

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg", lambda _path: "ffmpeg"
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe", lambda _path: "ffprobe"
    )
    summary = VideoRoute(
        _config(tmp_path),
        state,  # type: ignore[arg-type]
        1,
        media_probe=lambda *_args, **_kwargs: _probe(),
        frame_sampler=sampler,
        ocr_runtime_resolver=lambda _config: _Runtime(),
        frame_ocr=lambda *_args, **_kwargs: _Evidence(),
    ).run()

    assert summary.partial == 1
    assert summary.complete == 0
    assert summary.review_candidates == 1
    assert state.review_candidates[0].reason_code == "video_frame_discovery_timeout"
    assert state.review_candidates[0].source_status == "partial"
    assert state.review_candidates[0].retryable
    assert state.review_candidates[0].recommendation == "retry"
    assert video_state_status(_config(tmp_path).state_path)["statuses"][0]["status"] == ("partial")


def test_duration_limit_fails_before_any_frame_is_materialized(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    state = _FrameworkState(snapshot)

    @contextmanager
    def forbidden_sampler(*_args: Any, **_kwargs: Any):
        raise AssertionError("duration limit did not fail before sampling")
        yield  # type: ignore[unreachable]  # pragma: no cover

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg", lambda _path: "ffmpeg"
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe", lambda _path: "ffprobe"
    )
    (tmp_path / "state").mkdir(exist_ok=True)
    config = VideoRouteConfig(
        state_path=tmp_path / "state" / "video.sqlite3",
        root=tmp_path,
        max_duration_seconds=5,
    )
    summary = VideoRoute(
        config,
        state,  # type: ignore[arg-type]
        1,
        media_probe=lambda *_args, **_kwargs: _probe(duration=6),
        frame_sampler=forbidden_sampler,
        ocr_runtime_resolver=lambda _config: _Runtime(enabled=False),
    ).run()

    assert summary.errors == 1
    assert summary.review_candidates == 1
    assert state.review_candidates[0].reason_code == "video_duration_limit"
    assert state.review_candidates[0].source_status == "error"

    cached = VideoRoute(
        config,
        state,  # type: ignore[arg-type]
        2,
        media_probe=lambda *_args, **_kwargs: _probe(duration=6),
        frame_sampler=forbidden_sampler,
        ocr_runtime_resolver=lambda _config: _Runtime(enabled=False),
    ).run()

    assert cached.cache_hits == 1
    assert cached.cached_errors == 1
    assert cached.errors == 0
    assert cached.errors + cached.cached_errors == 1
