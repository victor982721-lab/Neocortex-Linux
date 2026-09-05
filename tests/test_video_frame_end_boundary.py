"""Low-rate media must not seek beyond the final constant-rate frame."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.video import route as video_route
from neocortex.capabilities.formats.video.frames import (
    VIDEO_FRAME_SAMPLING_POLICY,
    VideoFrameSamplingConfig,
    build_frame_plan,
    sampled_video_frames,
)
from neocortex.capabilities.formats.video.probe import probe_video
from neocortex.runtime.control.cancellation import CancellationToken


TEST_CAPABILITIES = ("base", "platform")


@pytest.mark.parametrize(
    ("duration", "rate", "expected"),
    (
        (2.0, 2.0, (0, 1500)),
        (2.0, 1.5, (0, 1000)),
        (2.0, 1.0, (0, 1000)),
        (2.0, 10.0, (0, 1800)),
        (2.0, None, (0, 1800)),
        (0.5, 2.0, (0,)),
        (0.125, 2.0, (0,)),
        (0.0, 2.0, (0,)),
        (2.0, 1e-300, (0,)),
    ),
)
def test_interval_end_respects_a_known_frame_period_without_replacing_sampling(
    duration: float, rate: float | None, expected: tuple[int, ...]
) -> None:
    plan = build_frame_plan(
        duration_seconds=duration,
        frame_rate=rate,
        max_frames=2,
        interval_seconds=1.0,
    )
    assert tuple(candidate.timestamp_ms for candidate in plan) == expected
    assert all(candidate.reasons == ("interval",) for candidate in plan)


@pytest.mark.parametrize("rate", (0.0, -1.0, float("nan"), float("inf"), -float("inf")))
def test_invalid_frame_rates_are_rejected_not_treated_as_observed_evidence(rate: float) -> None:
    with pytest.raises(ValueError, match="frame_rate must be finite and positive"):
        build_frame_plan(
            duration_seconds=2.0,
            frame_rate=rate,
            max_frames=2,
            interval_seconds=1.0,
        )


def test_rate_guard_does_not_rewrite_observed_scene_or_keyframe_timestamps() -> None:
    plan = build_frame_plan(
        duration_seconds=10.0,
        frame_rate=0.5,
        max_frames=6,
        interval_seconds=5.0,
        scene_timestamps_ms=(9500,),
        keyframe_timestamps_ms=(9000,),
    )
    # Averages do not establish the last PTS of VFR media; actual observations
    # remain authoritative and are not clamped to the interval endpoint.
    assert any(item.timestamp_ms == 9500 and "scene" in item.reasons for item in plan)
    assert any(item.timestamp_ms == 9000 and "keyframe" in item.reasons for item in plan)
    assert max(item.timestamp_ms for item in plan if "interval" in item.reasons) == 8000


def test_sampling_policy_changes_the_existing_processing_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        video_route,
        "executable_component",
        lambda name, **_kwargs: {"name": name, "kind": "executable", "version": "fixture"},
    )
    config = video_route.VideoRouteConfig(
        state_path=tmp_path / "video.sqlite", root=tmp_path / "corpus", ocr_mode="never"
    )
    no_ocr = SimpleNamespace(enabled=False, signature="disabled", processing_provenance_json=None)
    current = config.processing_provenance(no_ocr)
    assert current.manifest["configuration"]["frame_sampling_policy"] == VIDEO_FRAME_SAMPLING_POLICY
    assert config.processing_provenance(no_ocr).signature == current.signature
    monkeypatch.setattr(video_route, "VIDEO_FRAME_SAMPLING_POLICY", "previous-interval-end-guard")
    assert config.processing_provenance(no_ocr).signature != current.signature


@pytest.fixture
def low_rate_clip(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "platform video tests require a functional FFmpeg executable"
    assert shutil.which("ffprobe") is not None, "platform video tests require FFprobe"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "low-rate.mp4"
    result = subprocess.run(
        (
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=purple:size=128x96:rate=2:duration=2",
            "-an",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            "-y",
            str(source),
        ),
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return source


@pytest.mark.capability("platform")
def test_real_low_rate_ffmpeg_sampling_extracts_both_frames_and_cleans_scratch(
    low_rate_clip: Path,
) -> None:
    probe = probe_video(low_rate_clip)
    primary = probe.video[0]
    assert primary.frame_rate == 2.0
    with sampled_video_frames(
        low_rate_clip,
        corpus_root=low_rate_clip.parent,
        stream_index=primary.index,
        source_width=primary.width,
        source_height=primary.height,
        duration_seconds=probe.duration_seconds,
        frame_rate=primary.frame_rate,
        config=VideoFrameSamplingConfig(
            max_frames=2,
            interval_seconds=1.0,
            include_scenes=False,
            include_keyframes=False,
            file_timeout_seconds=15,
            frame_timeout_seconds=5,
        ),
        cancellation=CancellationToken(),
    ) as batch:
        paths = tuple(frame.path for frame in batch.frames)
        assert batch.warnings == ()
        assert tuple(frame.timestamp_ms for frame in batch.frames) == (0, 1500)
        assert all(frame.width == 128 and frame.height == 96 for frame in batch.frames)
        assert all(len(frame.content_xxh3_128) == 32 for frame in batch.frames)
        assert all(path.is_file() for path in paths)
        assert all(not path.is_relative_to(low_rate_clip.parent) for path in paths)
    assert all(not path.exists() for path in paths)


@pytest.mark.capability("platform")
def test_unknown_frame_rate_does_not_hide_a_real_extraction_failure(low_rate_clip: Path) -> None:
    probe = probe_video(low_rate_clip)
    primary = probe.video[0]
    with sampled_video_frames(
        low_rate_clip,
        corpus_root=low_rate_clip.parent,
        stream_index=primary.index,
        source_width=primary.width,
        source_height=primary.height,
        duration_seconds=probe.duration_seconds,
        frame_rate=None,
        config=VideoFrameSamplingConfig(
            max_frames=2,
            interval_seconds=1.0,
            include_scenes=False,
            include_keyframes=False,
            file_timeout_seconds=15,
            frame_timeout_seconds=5,
        ),
        cancellation=CancellationToken(),
    ) as batch:
        assert len(batch.frames) == 1
        assert batch.warnings == ("video_frame_extract_error",)
