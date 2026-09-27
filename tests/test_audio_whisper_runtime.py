"""Producer/worker Whisper runtime identity and startup protocol regressions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import pytest

from neocortex.capabilities.formats.audio import whisper
from neocortex.capabilities.formats.audio.models import (
    AudioRouteConfig,
    TranscriptResult,
    WhisperRuntime,
    WhisperRuntimeError,
)
from neocortex.runtime.control.cancellation import CancellationToken


_SOURCE_RUNTIME = WhisperRuntime("fixture-whisper", "fixture-ctranslate2", 1, "cpu", "int8")
_SOURCE = Path("fixture.wav")


class _ReadyWorker:
    """Pickleable isolated worker double with a controlled startup runtime."""

    def __init__(self, runtime: WhisperRuntime) -> None:
        self.runtime = runtime

    def __call__(self, task_channel, result_channel, _settings) -> None:
        result_channel.put(("ready", self.runtime))
        while True:
            task = task_channel.get()
            if task is None:
                return
            request_id, _path, config = task
            result_channel.put(
                (
                    "ok",
                    request_id,
                    TranscriptResult(
                        "fixture",
                        "en",
                        1.0,
                        1.0,
                        1.0,
                        (),
                        str(config["model_name"]),
                        self.runtime.backend_version,
                        self.runtime.resolved_device,
                        self.runtime.resolved_compute_type,
                    ),
                )
            )


def _transcriber(tmp_path: Path, runtime: WhisperRuntime, worker_runtime: object):
    return whisper.WhisperTranscriber(
        AudioRouteConfig(
            state_path=tmp_path / "unused.sqlite3",
            worker_startup_timeout_seconds=10,
            file_timeout_seconds=10,
            worker_memory_bytes=512 * 1024 * 1024,
        ),
        runtime,
        worker_target=_ReadyWorker(worker_runtime),  # type: ignore[arg-type]
    )


def test_cpu_worker_capability_probe_drift_does_not_change_processing_identity(
    tmp_path: Path,
) -> None:
    """A finite worker address-space limit may hide CUDA without changing CPU work."""

    child_runtime = replace(_SOURCE_RUNTIME, cuda_devices=0)
    assert whisper._whisper_runtime_processing_identity(_SOURCE_RUNTIME) == (
        whisper._whisper_runtime_processing_identity(child_runtime)
    )
    transcriber = _transcriber(tmp_path, _SOURCE_RUNTIME, child_runtime)
    try:
        result = transcriber.transcribe(_SOURCE, cancellation=CancellationToken())
    finally:
        transcriber.close()
    assert result.text == "fixture"
    assert result.device == "cpu"


def test_processing_signature_excludes_capability_probe_but_keeps_runtime_versions(
    tmp_path: Path,
) -> None:
    config = AudioRouteConfig(
        state_path=tmp_path / "unused.sqlite3",
        model_cache_directory=tmp_path / "models",
        device="cpu",
        compute_type="int8",
    )
    base = config.processing_signature(
        backend_version=_SOURCE_RUNTIME.backend_version,
        ctranslate2_version=_SOURCE_RUNTIME.ctranslate2_version,
        resolved_device=_SOURCE_RUNTIME.resolved_device,
        resolved_compute_type=_SOURCE_RUNTIME.resolved_compute_type,
    )
    capability_only = replace(_SOURCE_RUNTIME, cuda_devices=0)
    assert base == config.processing_signature(
        backend_version=capability_only.backend_version,
        ctranslate2_version=capability_only.ctranslate2_version,
        resolved_device=capability_only.resolved_device,
        resolved_compute_type=capability_only.resolved_compute_type,
    )
    assert base != config.processing_signature(
        backend_version="fixture-whisper-other",
        ctranslate2_version=_SOURCE_RUNTIME.ctranslate2_version,
        resolved_device=_SOURCE_RUNTIME.resolved_device,
        resolved_compute_type=_SOURCE_RUNTIME.resolved_compute_type,
    )


@pytest.mark.parametrize(
    "field, value",
    (
        ("backend_version", "fixture-whisper-other"),
        ("ctranslate2_version", "fixture-ctranslate2-other"),
        ("resolved_device", "cuda"),
        ("resolved_compute_type", "float16"),
    ),
)
def test_worker_rejects_processing_runtime_divergence(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    if field == "backend_version":
        child_runtime = replace(_SOURCE_RUNTIME, backend_version=value)
    elif field == "ctranslate2_version":
        child_runtime = replace(_SOURCE_RUNTIME, ctranslate2_version=value)
    elif field == "resolved_device":
        child_runtime = replace(
            _SOURCE_RUNTIME,
            resolved_device=cast(Literal["cpu", "cuda"], value),
        )
    else:
        assert field == "resolved_compute_type"
        child_runtime = replace(_SOURCE_RUNTIME, resolved_compute_type=value)
    assert whisper._whisper_runtime_processing_identity(_SOURCE_RUNTIME) != (
        whisper._whisper_runtime_processing_identity(child_runtime)
    )
    transcriber = _transcriber(tmp_path, _SOURCE_RUNTIME, child_runtime)
    try:
        with pytest.raises(WhisperRuntimeError, match="runtime differs"):
            transcriber.transcribe(_SOURCE, cancellation=CancellationToken())
        assert transcriber._process is None
    finally:
        transcriber.close()


def test_cuda_worker_with_no_observable_cuda_is_rejected_instead_of_downgraded(
    tmp_path: Path,
) -> None:
    parent_runtime = WhisperRuntime("fixture-whisper", "fixture-ctranslate2", 1, "cuda", "float16")
    child_runtime = replace(parent_runtime, cuda_devices=0)
    assert whisper._whisper_runtime_processing_identity(child_runtime) is None
    transcriber = _transcriber(tmp_path, parent_runtime, child_runtime)
    try:
        with pytest.raises(WhisperRuntimeError, match="runtime differs"):
            transcriber.transcribe(_SOURCE, cancellation=CancellationToken())
    finally:
        transcriber.close()


def test_malformed_worker_runtime_is_rejected(tmp_path: Path) -> None:
    transcriber = _transcriber(tmp_path, _SOURCE_RUNTIME, object())
    try:
        with pytest.raises(WhisperRuntimeError, match="runtime differs"):
            transcriber.transcribe(_SOURCE, cancellation=CancellationToken())
    finally:
        transcriber.close()


@pytest.mark.parametrize("device", ("invalid", []))
def test_malformed_parent_and_worker_runtime_are_not_equal_by_accident(
    tmp_path: Path, device: object,
) -> None:
    malformed = WhisperRuntime("fixture-whisper", "fixture-ctranslate2", 0, cast(str, device), "int8")
    assert whisper._whisper_runtime_processing_identity(malformed) is None
    transcriber = _transcriber(tmp_path, malformed, malformed)
    try:
        with pytest.raises(WhisperRuntimeError, match="runtime differs"):
            transcriber.transcribe(_SOURCE, cancellation=CancellationToken())
    finally:
        transcriber.close()
