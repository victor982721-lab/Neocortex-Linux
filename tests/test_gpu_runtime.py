"""The shared GPU observer must not guess ordinal identity or available VRAM."""

from types import SimpleNamespace

import pytest

from neocortex.runtime.control import gpu_runtime as gpu


@pytest.fixture
def observation(monkeypatch):
    monkeypatch.setattr(gpu, "_cached_at", None)
    monkeypatch.setattr(gpu, "_cached", ())
    monkeypatch.setattr(gpu, "_registered_processes", set())
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/fixture/nvidia-smi")
    calls = []
    rows = ["GPU-one, 00000000:01:00.0, 8192, 6144, Disabled\n"]

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=rows[0].encode(), stderr=b"")

    monkeypatch.setattr(gpu, "run_bounded_capture", capture)
    return rows, calls


def test_memory_probe_is_bounded_cached_and_refreshes_after_interval(observation, monkeypatch):
    rows, calls = observation
    now = [0.0]
    monkeypatch.setattr(gpu.time, "monotonic", lambda: now[0])
    assert gpu.cuda_memory_snapshot().available_bytes == 6144 * 1024**2
    rows[0] = "GPU-one, 00000000:01:00.0, 8192, 1024, Disabled\n"
    assert gpu.cuda_memory_snapshot().available_bytes == 6144 * 1024**2
    assert len(calls) == 1
    now[0] = 1.1
    assert gpu.cuda_memory_snapshot().available_bytes == 1024 * 1024**2
    assert len(calls) == 2
    assert calls[0][1] == {
        "timeout_seconds": 2.0, "stdout_limit_bytes": 65536, "stderr_limit_bytes": 8192,
    }


def test_multiple_default_ordinals_are_unknown_but_uuid_visibility_is_exact(observation, monkeypatch):
    rows, _calls = observation
    rows[0] += "GPU-two, 00000000:02:00.0, 4096, 2048, Disabled\n"
    assert gpu.cuda_memory_snapshot() is None
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-two,GPU-one")
    assert gpu.cuda_memory_snapshot(0).device_id == "GPU-two"
    assert gpu.cuda_memory_snapshot(1).device_id == "GPU-one"
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    assert gpu.cuda_memory_snapshot() is None
    monkeypatch.setenv("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    assert gpu.cuda_memory_snapshot().device_id == "GPU-two"


@pytest.mark.parametrize("row", (
    "GPU-one, 00000000:01:00.0, 8192, 6144, Enabled\n",
    "GPU-one, 00000000:01:00.0, 8192, 9000, Disabled\n",
    "GPU-one, 00000000:01:00.0, unknown, 100, Disabled\n",
))
def test_mig_or_invalid_capacity_remains_unknown(observation, row):
    rows, _calls = observation
    rows[0] = row
    assert gpu.cuda_memory_snapshot() is None


def test_whisper_auto_falls_back_but_explicit_cuda_requires_observation(monkeypatch):
    from neocortex.capabilities.formats.audio import whisper
    from neocortex.capabilities.formats.audio.models import AudioRuntimeUnavailableError

    monkeypatch.setattr(whisper, "_whisper_environment", lambda: ("fixture", "fixture", 1))
    monkeypatch.setattr(whisper, "cuda_memory_snapshot", lambda: None)
    assert whisper.resolve_whisper_runtime().resolved_device == "cpu"
    with pytest.raises(AudioRuntimeUnavailableError, match="memory/identity"):
        whisper.resolve_whisper_runtime("cuda")


@pytest.mark.parametrize("reused", (False, True))
def test_only_registered_identity_verified_around_query_gets_memory_credit(observation, monkeypatch, reused):
    _rows, _calls = observation
    monkeypatch.setattr(gpu, "_registered_processes", {(123, 456)})
    starts = iter((456, 999 if reused else 456))
    monkeypatch.setattr(gpu, "_process_start", lambda pid: next(starts))

    def capture(command, **kwargs):
        if "--query-compute-apps=pid,gpu_uuid,used_memory" in command:
            return SimpleNamespace(returncode=0, stdout=b"123, GPU-one, 2048\n999, GPU-one, 4096\n")
        return SimpleNamespace(returncode=0, stdout=b"GPU-one, 00000000:01:00.0, 8192, 6144, Disabled\n")

    monkeypatch.setattr(gpu, "run_bounded_capture", capture)
    assert gpu.cuda_memory_snapshot().process_memory_bytes == (
        {} if reused else {(123, 456): 2048 * 1024**2}
    )
