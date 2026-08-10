"""Focused contracts for bounded video timestamp and raster sampling."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.video_frames import (
    MAX_VIDEO_FRAME_BATCH_BYTES,
    ExtractedVideoFrame,
    VideoFrameSamplingConfig,
    bounded_frame_dimensions,
    build_frame_plan,
    parse_showinfo_timestamps,
    sampled_video_frames,
)
from _04_Nucleo_Operativo.video_probe import probe_video


@pytest.mark.parametrize(
    ("width", "height", "max_pixels", "max_side"),
    (
        (1920, 1080, 2_073_600, 1920),
        (3840, 2160, 2_073_600, 1920),
        (2160, 3840, 2_073_600, 1920),
        (640, 480, 100_000, 1000),
        (1, 1, 1, 1),
        (10_000, 100, 250_000, 800),
        (100, 10_000, 250_000, 800),
        (1280, 720, 40_000_000, 640),
    ),
)
def test_bounded_dimensions_never_exceed_pixel_or_side_limits(
    width: int,
    height: int,
    max_pixels: int,
    max_side: int,
) -> None:
    actual_width, actual_height = bounded_frame_dimensions(
        width,
        height,
        max_pixels=max_pixels,
        max_side=max_side,
    )

    assert 1 <= actual_width <= max_side
    assert 1 <= actual_height <= max_side
    assert actual_width * actual_height <= max_pixels
    assert abs((actual_width / actual_height) - (width / height)) <= max(
        1 / actual_height, 1 / height
    )


@pytest.mark.parametrize(
    ("duration", "maximum", "interval", "scenes", "keyframes"),
    (
        (0.0, 1, 30.0, (), ()),
        (1.0, 2, 30.0, (), ()),
        (60.0, 4, 30.0, (), ()),
        (61.0, 4, 30.0, (), ()),
        (3600.0, 8, 30.0, (), ()),
        (120.0, 6, 30.0, (10_000, 90_000), (0, 30_000, 60_000)),
        (120.0, 3, 30.0, (10_000, 20_000, 90_000), (0, 60_000, 100_000)),
        (5.0, 8, 0.5, (1000, 2000), (0, 3000)),
        (10.0, 1, 1.0, (5000,), (0,)),
        (10.0, 2, 1.0, (5000,), (0,)),
        (10.0, 3, 1.0, (5000,), (0,)),
        (10.0, 4, 1.0, (5000,), (0,)),
    ),
)
def test_frame_plan_is_sorted_bounded_and_preserves_typed_reasons(
    duration: float,
    maximum: int,
    interval: float,
    scenes: tuple[int, ...],
    keyframes: tuple[int, ...],
) -> None:
    plan = build_frame_plan(
        duration_seconds=duration,
        max_frames=maximum,
        interval_seconds=interval,
        scene_timestamps_ms=scenes,
        keyframe_timestamps_ms=keyframes,
    )

    assert 1 <= len(plan) <= maximum
    assert [frame.timestamp_ms for frame in plan] == sorted(frame.timestamp_ms for frame in plan)
    assert len({frame.timestamp_ms for frame in plan}) == len(plan)
    assert all(
        set(frame.reasons) <= {"interval", "scene", "keyframe"} and frame.reasons for frame in plan
    )
    assert all(0 <= frame.timestamp_ms <= max(0, round(duration * 1000)) for frame in plan)


def test_showinfo_parser_rejects_negative_nonfinite_and_out_of_range_values() -> None:
    payload = b"pts_time:0.000 pts_time:1.250 pts_time:1.250 pts_time:-1 pts_time:nan pts_time:99.0"

    assert parse_showinfo_timestamps(payload, duration_seconds=2.0) == (0, 1250)


def test_sampler_stops_before_aggregate_ephemeral_disk_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "fixture.mkv"
    source.write_bytes(b"not-decoded-by-this-bounded-fixture")
    frame_bytes = 63 * 1024 * 1024
    retained: tuple[Path, ...]

    def fake_extract(
        _source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> ExtractedVideoFrame:
        with destination.open("wb") as stream:
            stream.truncate(frame_bytes)
        return ExtractedVideoFrame(
            -1,
            kwargs["timestamp_ms"],
            (),
            destination,
            1,
            1,
            "a" * 32,
        )

    monkeypatch.setattr(
        "_04_Nucleo_Operativo.video_frames.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.video_frames._extract_frame",
        fake_extract,
    )
    with sampled_video_frames(
        source,
        corpus_root=corpus,
        stream_index=0,
        source_width=1,
        source_height=1,
        duration_seconds=12,
        config=VideoFrameSamplingConfig(
            max_frames=10,
            interval_seconds=1,
            include_scenes=False,
            include_keyframes=False,
        ),
        cancellation=CancellationToken(),
    ) as batch:
        retained = tuple(frame.path for frame in batch.frames)
        assert len(batch.frames) == MAX_VIDEO_FRAME_BATCH_BYTES // frame_bytes
        assert batch.warnings == ("video_frame_batch_byte_limit",)
        assert sum(path.stat().st_size for path in retained) <= MAX_VIDEO_FRAME_BATCH_BYTES

    assert retained
    assert all(not path.exists() for path in retained)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_small_ffmpeg_pilot_extracts_ephemeral_frames_and_cleans_them(
    tmp_path: Path,
) -> None:
    if os.name != "nt" and not Path("/usr/bin/prlimit").is_file():
        pytest.skip("POSIX subprocess memory containment is unavailable")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "visual-only.mkv"
    created = subprocess.run(
        (
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=2",
            "-an",
            "-c:v",
            "ffv1",
            "-y",
            str(source),
        ),
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    probe = probe_video(source)
    assert probe.audio_streams == 0

    retained_paths: tuple[Path, ...]
    with sampled_video_frames(
        source,
        corpus_root=corpus,
        stream_index=probe.video[0].index,
        source_width=probe.video[0].width,
        source_height=probe.video[0].height,
        duration_seconds=probe.duration_seconds,
        config=VideoFrameSamplingConfig(
            max_frames=4,
            interval_seconds=0.75,
            discovery_timeout_seconds=10,
            frame_timeout_seconds=10,
            file_timeout_seconds=30,
            worker_memory_bytes=2 * 1024 * 1024 * 1024,
        ),
        cancellation=CancellationToken(),
    ) as batch:
        retained_paths = tuple(frame.path for frame in batch.frames)
        assert 1 <= len(batch.frames) <= 4
        assert all(path.is_file() for path in retained_paths)
        assert all(not path.is_relative_to(corpus) for path in retained_paths)
        assert all(frame.width * frame.height <= 2_073_600 for frame in batch.frames)

    assert retained_paths
    assert all(not path.exists() for path in retained_paths)
