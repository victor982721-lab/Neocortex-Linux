# region [00] Contexto del módulo
# Módulo: tests/test_audio_probe_bounded.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from neocortex.capabilities.formats.audio import probe as audio_probe
from neocortex.capabilities.formats.audio.models import AudioProcessingError
from neocortex.runtime.control.bounded_subprocess import SubprocessOutputLimitError
# endregion [01]

# region [02] Implementación


def test_audio_probe_uses_hard_output_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def bounded_capture(command, **options):
        observed["command"] = command
        observed.update(options)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                b'{"format":{"duration":"12.5","format_name":"wav"},'
                b'"streams":[{"codec_type":"audio","codec_name":"pcm_s16le",'
                b'"sample_rate":"48000","channels":2,"duration":"12.5"}]}'
            ),
            stderr=b"",
        )

    monkeypatch.setattr(audio_probe, "resolve_ffprobe", lambda _value: "ffprobe")
    monkeypatch.setattr(audio_probe, "run_bounded_capture", bounded_capture)

    result = audio_probe.probe_media(
        tmp_path / "sample.wav",
        timeout_seconds=7,
    )

    assert result.duration_seconds == 12.5
    assert observed["timeout_seconds"] == 7
    assert observed["stdout_limit_bytes"] == audio_probe.MAX_FFPROBE_OUTPUT_BYTES
    assert observed["stderr_limit_bytes"] == audio_probe.MAX_FFPROBE_OUTPUT_BYTES


def test_audio_probe_maps_output_overflow_to_non_retryable_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(audio_probe, "resolve_ffprobe", lambda _value: "ffprobe")
    monkeypatch.setattr(
        audio_probe,
        "run_bounded_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SubprocessOutputLimitError("stdout", 128)
        ),
    )

    with pytest.raises(AudioProcessingError) as captured:
        audio_probe.probe_media(tmp_path / "oversized.wav")

    assert captured.value.code == "audio_probe_output_limit"
    assert captured.value.recommendation == "manual_review"
    assert captured.value.retryable is False


def test_audio_probe_maps_timeout_to_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(audio_probe, "resolve_ffprobe", lambda _value: "ffprobe")
    monkeypatch.setattr(
        audio_probe,
        "run_bounded_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(("ffprobe",), 3)
        ),
    )

    with pytest.raises(AudioProcessingError) as captured:
        audio_probe.probe_media(tmp_path / "slow.wav", timeout_seconds=3)

    assert captured.value.code == "audio_probe_timeout"
    assert captured.value.recommendation == "retry"
    assert captured.value.retryable is True
# endregion [02]
