"""FFmpeg resource renewals consume the existing producer deadline."""

import io
import subprocess
import time

import pytest
from PIL import Image

from neocortex.capabilities.formats.video import frames
from neocortex.capabilities.formats.video.models import VideoProcessingError
from neocortex.foundation.hash_compat import sha256
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits, ResourceSample,
)


TEST_CAPABILITIES = ("image",)
GIB = 1024 ** 3


@pytest.mark.parametrize("operation", ["discovery", "batch"])
def test_pressure_wait_expires_before_starting_decoder(tmp_path, monkeypatch, operation):
    pressure = [False]
    coordinator = GlobalResourceCoordinator(
        ("video",), GlobalResourceLimits(
            memory_budget_bytes=GIB, min_free_memory_bytes=0, min_free_commit_bytes=0,
            cpu_slots=4, native_thread_slots=4, sample_interval_seconds=0.001,
            wait_timeout_seconds=0.4,
        ),
        effective_cpu_probe=lambda: 4,
        resource_probe=lambda: ResourceSample(
            available_physical=8 * GIB, total_physical=8 * GIB,
            available_commit=8 * GIB, total_commit=8 * GIB,
            external_cpu_cores=4 if pressure[0] else 0, own_cpu_cores=0,
            effective_cpu_capacity=4,
        ),
    )
    gate = CoordinatedMemoryGate(coordinator, "video")
    monkeypatch.setattr(frames, "run_bounded_capture",
                        lambda *_args, **_kwargs: pytest.fail("decoder started after deadline"))
    with gate.admit(128):
        pressure[0] = True
        time.sleep(0.002)
        started = time.monotonic()
        with pytest.raises(VideoProcessingError) as raised:
            if operation == "discovery":
                frames._discover_timestamps(
                    tmp_path / "input.mkv", executable="ffmpeg", stream_index=0,
                    duration_seconds=1, selection="eq(pict_type\\,I)", max_frames=1,
                    timeout_seconds=0.05, memory_limit_bytes=GIB,
                    cancellation=CancellationToken(),
                )
            else:
                frames._extract_frames(
                    tmp_path / "input.mkv", tmp_path,
                    plan=(frames.VideoFrameCandidate(0, ("interval",)),),
                    executable="ffmpeg", stream_index=0, width=2, height=2,
                    timeout_seconds=0.05, memory_limit_bytes=GIB,
                    cancellation=CancellationToken(),
                )
        assert raised.value.code == (
            "video_frame_discovery_timeout" if operation == "discovery" else "video_frame_timeout"
        )
        assert time.monotonic() - started < 0.3
    assert coordinator.summary().cpu_slots_in_use == 0
    assert coordinator.summary().transient_bytes == 0
    assert not tuple(tmp_path.iterdir())


def test_batch_hashes_validated_capture_without_rereading_each_raster(tmp_path, monkeypatch):
    with Image.new("RGB", (2, 2), "navy") as raster, io.BytesIO() as stream:
        raster.save(stream, format="PNG")
        payload = stream.getvalue()
    monkeypatch.setattr(frames, "run_bounded_capture", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, payload, b"pts_time:0\n"))
    monkeypatch.setattr(frames, "_frame_content_digest", lambda *_args:
                        pytest.fail("captured raster was reopened solely for its digest"))
    result = frames._extract_frames(
        tmp_path / "input.mkv", tmp_path,
        plan=(frames.VideoFrameCandidate(0, ("interval",)),),
        executable="ffmpeg", stream_index=0, width=2, height=2,
        timeout_seconds=2, memory_limit_bytes=GIB,
    )
    assert len(result) == 1
    assert result[0].path.read_bytes() == payload
    assert result[0].content_xxh3_128 == sha256.sha256_128(payload).hexdigest()
