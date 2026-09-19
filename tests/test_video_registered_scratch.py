"""Contracts for the video frame producer's registered scratch workspace."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from neocortex.capabilities.formats.video.frames import (
    ExtractedVideoFrame,
    VideoFrameSamplingConfig,
    _registered_video_scratch_workspace,
    sampled_video_frames,
    video_frame_scratch_root,
)
from neocortex.capabilities.formats.video.models import VideoProcessingError
from neocortex.capabilities.formats.video.route import VideoRouteConfig
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.scratch import ScratchManager, ScratchState


OWNER = "video-frame-sampler"


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _sampling_config(scratch: Path, *, run_id: int | str | None = None) -> VideoFrameSamplingConfig:
    return VideoFrameSamplingConfig(
        max_frames=1,
        interval_seconds=1.0,
        include_scenes=False,
        include_keyframes=False,
        scratch_directory=scratch,
        run_id=run_id,
    )


def test_video_sampler_registers_workspace_and_retires_it_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _private_directory(tmp_path / "corpus")
    source = corpus / "fixture.mp4"
    source.write_bytes(b"video fixture")
    scratch = _private_directory(tmp_path / "scratch")

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.frames.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )

    def fake_extract(_source: Path, scratch: Path, **kwargs: Any):
        candidate = kwargs["plan"][0]
        destination = scratch / "frame.png"
        destination.write_bytes(b"bounded frame")
        return (ExtractedVideoFrame(
            -1,
            candidate.timestamp_ms,
            (),
            destination,
            1,
            1,
            "a" * 32,
        ),)

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.frames._extract_frames",
        fake_extract,
    )

    manager = ScratchManager(scratch, owner=OWNER, create_root=False)
    source_before = source.read_bytes()
    with sampled_video_frames(
        source,
        corpus_root=corpus,
        stream_index=0,
        source_width=16,
        source_height=16,
        duration_seconds=1.0,
        config=_sampling_config(scratch, run_id=41),
        cancellation=CancellationToken(),
    ) as batch:
        assert len(batch.frames) == 1
        frame_path = batch.frames[0].path
        records = manager.records()
        assert len(records) == 1
        record = records[0]
        assert record.state is ScratchState.ACTIVE
        assert record.owner == OWNER
        assert record.run_id == 41
        assert record.metadata == {
            "component": OWNER,
            "operation": "sampled_video_frames",
        }
        assert frame_path.is_relative_to(record.path)
        assert (record.path / "manifest.json").is_file()

    assert manager.records() == ()
    scratch_entries = tuple(scratch.iterdir())
    assert {entry.name for entry in scratch_entries} == {".scratch-control"}
    assert len(scratch_entries) == 1
    scratch_control = scratch_entries[0]
    assert scratch_control.is_dir()
    assert not scratch_control.is_symlink()
    assert scratch_control.stat().st_mode & 0o077 == 0
    assert source.read_bytes() == source_before


def test_video_sampler_retains_registered_workspace_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _private_directory(tmp_path / "corpus")
    source = corpus / "fixture.webm"
    source.write_bytes(b"video fixture")
    scratch = _private_directory(tmp_path / "scratch")

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.frames.resolve_video_ffmpeg",
        lambda _path: "ffmpeg",
    )

    def failing_extract(_source: Path, _destination: Path, **_kwargs: Any) -> ExtractedVideoFrame:
        raise VideoProcessingError(
            "video_fixture_extract_failure",
            "fixture extraction failed",
            recommendation="retry",
            retryable=True,
        )

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.frames._extract_frames",
        failing_extract,
    )

    with pytest.raises(VideoProcessingError, match="could not materialize"):
        with sampled_video_frames(
            source,
            corpus_root=corpus,
            stream_index=0,
            source_width=16,
            source_height=16,
            duration_seconds=1.0,
            config=_sampling_config(scratch, run_id="video-run"),
            cancellation=CancellationToken(),
        ):
            pytest.fail("the failing sampler unexpectedly yielded frames")

    records = ScratchManager(scratch, owner=OWNER, create_root=False).records()
    assert len(records) == 1
    record = records[0]
    assert record.state is ScratchState.FAILED_RETAINED
    assert record.owner == OWNER
    assert record.run_id == "video-run"
    assert "VideoProcessingError" in (record.reason or "")
    assert record.path.is_dir()
    assert (record.path / "manifest.json").is_file()
    assert source.read_bytes() == b"video fixture"


def test_video_registered_scratch_rejects_corpus_intersection_without_creation(
    tmp_path: Path,
) -> None:
    corpus = _private_directory(tmp_path / "corpus")
    scratch = corpus / "scratch"

    with pytest.raises(VideoProcessingError, match="intersects the corpus"):
        with _registered_video_scratch_workspace(
            scratch,
            corpus_root=corpus,
        ):
            pytest.fail("a corpus-intersecting scratch workspace was yielded")

    assert not scratch.exists()


def test_video_route_derives_state_owned_registered_scratch_root(tmp_path: Path) -> None:
    state = _private_directory(tmp_path / "state")
    config = VideoRouteConfig(
        state_path=state / "video.sqlite3",
        root=tmp_path / "corpus",
    )

    sampling = config.frame_sampling_config(run_id=7)

    assert sampling.scratch_directory == video_frame_scratch_root(state / "video.sqlite3")
    assert sampling.run_id == 7
