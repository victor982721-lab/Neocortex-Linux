"""Batch decoding accounts for both captured copies and materialized scratch."""

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from neocortex.capabilities.formats.video import frames
from neocortex.capabilities.formats.video.route import VideoRoute, _VideoMetrics
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken

TEST_CAPABILITIES = ("image",)


def test_batch_claims_scratch_before_native_output_and_keeps_only_materialized_bytes(tmp_path, monkeypatch):
    with Image.new("RGB", (2, 2), "navy") as raster, io.BytesIO() as stream:
        raster.save(stream, format="PNG")
        payload = stream.getvalue()
    reservations = []
    grant = SimpleNamespace(
        native_threads=3, checkpoint=lambda **_kwargs: None,
        subprocess_env=lambda: {"OMP_THREAD_LIMIT": "3"}, register_process=lambda *args: None,
        resize_temp_bytes=lambda amount, **kwargs: reservations.append(amount),
    )
    monkeypatch.setattr(frames, "current_resource_grant", lambda: grant)

    def capture(command, **kwargs):
        assert reservations == [frames.frame_batch_output_limit(4, 2)]
        assert kwargs["stdout_limit_bytes"] == reservations[0]
        assert command[command.index("-threads") + 1] == "3"
        assert kwargs["environment"]["OMP_THREAD_LIMIT"] == "3"
        assert not list(tmp_path.glob("*.png"))
        return subprocess.CompletedProcess(command, 0, payload + payload, b"pts_time:0\npts_time:1\n")

    monkeypatch.setattr(frames, "run_bounded_capture", capture)
    result = frames._extract_frames(
        tmp_path / "input.mkv", tmp_path,
        plan=(frames.VideoFrameCandidate(0, ("scene",)), frames.VideoFrameCandidate(1000, ("interval",))),
        executable="ffmpeg", stream_index=0, width=2, height=2,
        timeout_seconds=2, memory_limit_bytes=1024**3,
    )
    assert [item.timestamp_ms for item in result] == [0, 1000]
    assert reservations[-1] == sum(path.stat().st_size for path in tmp_path.glob("*.png"))
    assert result[0].content_xxh3_128 == result[1].content_xxh3_128


def test_exhausted_temp_quota_stops_before_any_decoder_or_raster_write(tmp_path, monkeypatch):
    def deny(_amount, **_kwargs):
        raise MemoryBudgetExceeded("fixture temporary quota")

    grant = SimpleNamespace(native_threads=1, checkpoint=lambda **_kwargs: None,
                            subprocess_env=dict, register_process=lambda *args: None,
                            resize_temp_bytes=deny)
    monkeypatch.setattr(frames, "current_resource_grant", lambda: grant)
    monkeypatch.setattr(frames, "run_bounded_capture", lambda *args, **kwargs: pytest.fail("decoder started"))
    with pytest.raises(MemoryBudgetExceeded):
        frames._extract_frames(
            tmp_path / "input.mkv", tmp_path,
            plan=(frames.VideoFrameCandidate(0, ("interval",)),), executable="ffmpeg",
            stream_index=0, width=2, height=2, timeout_seconds=2, memory_limit_bytes=1024**3,
        )
    assert not tuple(tmp_path.iterdir())


def test_worker_reservation_includes_native_limit_capture_copy_and_parent_workspace():
    config = frames.VideoFrameSamplingConfig()
    output = frames.frame_batch_output_limit(config.max_frame_pixels, config.max_frames)
    assert frames.video_worker_memory_reservation(config) >= config.worker_memory_bytes + 2 * output


def test_exact_raster_memo_preserves_times_and_changes_with_ocr_provenance():
    from tests.test_video_route import _Evidence, _Runtime

    route = object.__new__(VideoRoute)
    calls = []
    route.frame_ocr = lambda path, *_args: calls.append(path) or _Evidence()
    metrics, memo = _VideoMetrics(), {}
    first = frames.ExtractedVideoFrame(0, 0, ("scene",), Path("first.png"), 2, 2, "same-raster")
    later = frames.ExtractedVideoFrame(1, 1000, ("interval",), Path("later.png"), 2, 2, "same-raster")
    a = route._inspect_frame(first, _Runtime(), metrics, [], memo=memo)
    b = route._inspect_frame(later, _Runtime(), metrics, [], memo=memo)
    assert [a.timestamp_ms, b.timestamp_ms] == [0, 1000]
    assert [a.sampling_reasons, b.sampling_reasons] == [("scene",), ("interval",)]
    assert metrics.ocr_attempts == len(calls) == 1
    route._inspect_frame(later, _Runtime(processing_provenance_json="changed"), metrics, [], memo=memo)
    assert metrics.ocr_attempts == len(calls) == 2


def test_inflight_decoder_cancellation_terminates_owned_child_before_return(tmp_path):
    producer = tmp_path / "slow-decoder"
    marker = producer.with_suffix(".pid")
    producer.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
        "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n"
    )
    producer.chmod(0o700)
    cancellation = CancellationToken()
    started = threading.Event()

    def cancel_started_producer():
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if marker.exists():
            started.set()
        cancellation.cancel()

    canceller = threading.Thread(target=cancel_started_producer)
    canceller.start()
    begin = time.monotonic()
    try:
        with pytest.raises(CancellationRequested):
            frames._extract_frames(
                tmp_path / "input.mkv", tmp_path,
                plan=(frames.VideoFrameCandidate(0, ("interval",)),), executable=str(producer),
                stream_index=0, width=2, height=2, timeout_seconds=20,
                memory_limit_bytes=1024**3, cancellation=cancellation,
            )
    finally:
        canceller.join()
    assert started.is_set()
    assert time.monotonic() - begin < 4
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert not tuple(tmp_path.glob("*.png"))
