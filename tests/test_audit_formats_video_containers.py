from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.capabilities.formats.video.models import (
    VideoNotApplicable,
    VideoProcessingError,
)
from neocortex.capabilities.formats.video.probe import decode_video_probe, probe_video
from neocortex.capabilities.formats.video.route import VideoRoute, VideoRouteConfig, _VideoMetrics
from neocortex.capabilities.formats.video.state import (
    initialize_video_state,
    store_video_error,
    store_video_not_applicable,
    video_database,
    video_state_status,
)


class _FrameworkState:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.review_candidates: list[Any] = []

    def selected_route_candidate_counts(self, _run_id, mime, _max_file_bytes, _route, _selection):
        return (1, 1) if mime == "video/webm" else (0, 0)

    def iter_selected_route_candidates(self, _run_id, mime, _route, _selection):
        if mime == "video/webm":
            yield self.snapshot

    def store_review_candidates(self, _run_id, candidates) -> None:
        self.review_candidates.extend(candidates)

    def reconcile_review_candidates_batch(self, _run_id, _route, _reconciliations) -> None:
        return None


class _MemoryGate:
    peak_reserved_bytes = 0
    wait_count = 0

    @contextmanager
    def admit(self, _bytes: int) -> Iterator[None]:
        yield


@dataclass(frozen=True)
class _OcrRuntime:
    enabled: bool = False
    signature: str = "video-audit-ocr-disabled"
    provenance: str | None = None
    unavailable_reason: str | None = None
    processing_provenance_json: str | None = None


def _audio_only_payload() -> dict[str, object]:
    return {
        "format": {"duration": "0.258", "format_name": "matroska,webm"},
        "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "opus"},
        ],
        "chapters": [],
    }


def test_audio_only_probe_is_valid_non_visual_media() -> None:
    probe = decode_video_probe(_audio_only_payload())

    assert probe.video_streams == 0
    assert probe.audio_streams == 1
    assert probe.duration_seconds == pytest.approx(0.258)


def test_video_route_marks_audio_only_probe_not_applicable_before_sampling(
    tmp_path: Path,
) -> None:
    source = tmp_path / "audio-only.webm"
    source.write_bytes(b"fixture audio-only container")
    snapshot = snapshot_path(source)
    probe = decode_video_probe(_audio_only_payload())
    route = VideoRoute(
        VideoRouteConfig(
            state_path=tmp_path / "video.sqlite3",
            root=tmp_path,
            ocr_mode="never",
        ),
        object(),  # type: ignore[arg-type]
        1,
        media_probe=lambda *_args, **_kwargs: probe,
        frame_sampler=lambda *_args, **_kwargs: pytest.fail("audio-only media was sampled"),
    )

    with pytest.raises(VideoNotApplicable) as raised:
        route._inspect(snapshot, object(), _VideoMetrics(), admitted=True)
    assert raised.value.probe is probe


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg/FFprobe unavailable",
)
def test_real_audio_only_webm_probe_is_not_a_video_error(tmp_path: Path) -> None:
    source = tmp_path / "audio-only.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.25",
            "-c:a",
            "libopus",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    probe = probe_video(source)

    assert probe.video_streams == 0
    assert probe.audio_streams == 1
    assert probe.duration_seconds > 0


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg/FFprobe unavailable",
)
def test_video_route_reprobes_legacy_audio_only_error_and_replaces_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "audio-only.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.25",
            "-c:a",
            "libopus",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    snapshot = snapshot_path(source)
    state_path = tmp_path / "video.sqlite3"
    config = VideoRouteConfig(
        state_path=state_path,
        root=tmp_path,
        ffmpeg_path=shutil.which("ffmpeg"),
        ffprobe_path=shutil.which("ffprobe"),
        ocr_mode="never",
    )
    signature = config.processing_provenance(_OcrRuntime()).signature
    initialize_video_state(state_path)
    with video_database(state_path, create=False) as connection:
        store_video_error(
            connection,
            snapshot,
            "video/webm",
            signature,
            1,
            VideoProcessingError(
                "media_without_video_stream",
                "legacy audio-only error",
                recommendation="manual_review",
                retryable=False,
            ),
        )
        connection.commit()

    def forbidden_sampler(*_args, **_kwargs):
        raise AssertionError("audio-only media was sampled")

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg",
        lambda path: str(path or shutil.which("ffmpeg")),
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe",
        lambda path: str(path or shutil.which("ffprobe")),
    )
    summary = VideoRoute(
        config,
        _FrameworkState(snapshot),  # type: ignore[arg-type]
        2,
        memory_gate=_MemoryGate(),
        media_probe=probe_video,
        frame_sampler=forbidden_sampler,
        ocr_runtime_resolver=lambda _config: _OcrRuntime(),
    ).run()

    assert summary.errors == 0
    assert summary.not_applicable == 1
    assert summary.review_candidates == 0
    assert video_state_status(state_path)["statuses"] == [
        {"status": "not_applicable", "documents": 1, "frames": 0, "ocr_frames": 0, "ocr_chars": 0}
    ]


def test_audio_only_exclusion_replaces_prior_video_error(tmp_path: Path) -> None:
    state_path = tmp_path / "video.sqlite3"
    source = tmp_path / "audio-only.webm"
    source.write_bytes(b"fixture")
    snapshot = snapshot_path(source)
    probe = decode_video_probe(_audio_only_payload())
    signature = "video-route-fixture-v2"
    error = VideoProcessingError(
        "media_without_video_stream",
        "legacy video-only error",
        recommendation="manual_review",
        retryable=False,
    )
    initialize_video_state(state_path)

    with video_database(state_path, create=False) as connection:
        store_video_error(connection, snapshot, "video/webm", signature, 1, error)
        store_video_not_applicable(
            connection,
            snapshot,
            "video/webm",
            signature,
            probe,
            None,
            2,
        )
        row = connection.execute(
            "SELECT status,error_type,video_streams,audio_streams FROM documents"
        ).fetchone()

    assert tuple(row) == ("not_applicable", None, 0, 1)
