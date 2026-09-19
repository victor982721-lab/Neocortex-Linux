"""Audio replicas obey shared CPU/RAM/VRAM and processing cache identities."""

from dataclasses import replace

from neocortex.capabilities.formats.audio import route as audio
from neocortex.capabilities.formats.audio.models import AudioRouteConfig, WhisperRuntime
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits, ResourceSample,
    current_resource_grant,
)
from neocortex.runtime.control.gpu_runtime import GpuMemorySnapshot
from tests.test_audio_route import FakeFrameworkRouteState, FakeTranscriber, PROBE, _result


GIB = 1024**3


def test_two_gib_model_three_gib_gpu_uses_one_hot_replica_and_replays_across_cpu_capacity(tmp_path, monkeypatch):
    snapshots = []
    for index in range(4):
        path = tmp_path / f"sound-{index}.ogg"
        path.write_bytes(f"OggS fixture {index}".encode())
        snapshots.append(snapshot_path(path))
    framework = FakeFrameworkRouteState({"audio/ogg": tuple(snapshots)})
    effective = [4]
    coordinator = GlobalResourceCoordinator(
        ("Audio",), GlobalResourceLimits(memory_budget_bytes=16 * GIB,
                                         min_free_memory_bytes=0, min_free_commit_bytes=0,
                                         cpu_slots=8, native_thread_slots=8,
                                         wait_timeout_seconds=2, sample_interval_seconds=0.001),
        effective_cpu_probe=lambda: effective[0],
        resource_probe=lambda: ResourceSample(
            available_physical=32 * GIB, total_physical=32 * GIB,
            available_commit=32 * GIB, total_commit=32 * GIB,
            cpu_load_percent=0, external_cpu_cores=0, own_cpu_cores=0,
            effective_cpu_capacity=effective[0],
        ),
    )
    gate = CoordinatedMemoryGate(coordinator, "Audio")
    monkeypatch.setattr(audio, "cuda_memory_snapshot", lambda: GpuMemorySnapshot("GPU-fixture", 3 * GIB, 3 * GIB, 0))
    models, threads = [], []

    class Transcriber(FakeTranscriber):
        def transcribe(self, path, *, cancellation):
            grant = current_resource_grant()
            assert grant is not None
            threads.append(grant.native_threads)
            assert coordinator.summary().gpu_bytes == (2 * GIB if self.cuda else 0)
            return super().transcribe(path, cancellation=cancellation)

    def factory(_config, _runtime):
        model = Transcriber(_result("technical audio"))
        model.cuda = _runtime.resolved_device == "cuda"
        models.append(model)
        return model

    config = AudioRouteConfig(state_path=tmp_path / "audio.sqlite3", model_name="medium",
                              model_cache_directory=tmp_path / "models", worker_memory_bytes=2 * GIB,
                              min_free_memory_bytes=0, min_free_commit_bytes=0)
    runtime = WhisperRuntime("fixture", "fixture", 1, "cuda", "float16")
    kwargs = {"memory_gate": gate, "runtime_resolver": lambda *_: runtime,
              "media_probe": lambda *_args, **_kwargs: PROBE, "transcriber_factory": factory}
    first = audio.AudioRoute(config, framework, 1, **kwargs).run()
    assert first.transcribed == 4
    assert len(models) == 1 and models[0].closed
    assert threads == [4] * 4
    assert coordinator.summary().gpu_bytes == 0
    effective[0] = 8
    replay = audio.AudioRoute(replace(config, workers=16), framework, 1, **kwargs).run()
    assert replay.cache_hits == 4
    assert replay.processing_signature == first.processing_signature
    assert len(models) == 1
    cpu = replace(runtime, resolved_device="cpu", resolved_compute_type="int8")
    changed = audio.AudioRoute(config, framework, 1, **{**kwargs, "runtime_resolver": lambda *_: cpu}).run()
    assert changed.transcribed == 4 and changed.cache_hits == 0
    assert changed.processing_signature != first.processing_signature
