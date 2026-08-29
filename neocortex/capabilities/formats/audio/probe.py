"""Bounded FFprobe metadata inspection for audio and video containers."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .models import AudioProcessingError, MediaProbe
from _04_Nucleo_Operativo.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)


# region [01] Executable discovery and bounded probe


MAX_FFPROBE_OUTPUT_BYTES = 2 * 1024 * 1024


def resolve_ffprobe(explicit: str | None = None) -> str:
    candidate = explicit or "ffprobe"
    resolved = shutil.which(candidate)
    if resolved is None:
        raise FileNotFoundError(f"FFprobe executable was not found: {candidate}")
    return resolved


def probe_media(
    path: Path,
    *,
    ffprobe_path: str | None = None,
    timeout_seconds: float = 30.0,
) -> MediaProbe:
    """Return only fields required for safe transcription decisions."""

    if timeout_seconds <= 0:
        raise ValueError("FFprobe timeout must be positive")
    executable = resolve_ffprobe(ffprobe_path)
    command = (
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels,duration",
        "-of",
        "json",
        str(path),
    )
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    completed = _run_ffprobe(command, timeout_seconds, creation_flags)
    return _decode_probe(_probe_payload(completed))


def _run_ffprobe(
    command: tuple[str, ...],
    timeout_seconds: float,
    creation_flags: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return run_bounded_capture(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=MAX_FFPROBE_OUTPUT_BYTES,
            stderr_limit_bytes=MAX_FFPROBE_OUTPUT_BYTES,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError(
            "audio_probe_timeout",
            f"FFprobe exceeded {timeout_seconds:g} seconds",
            recommendation="retry",
            retryable=True,
        ) from exc
    except SubprocessOutputLimitError as exc:
        raise AudioProcessingError(
            "audio_probe_output_limit",
            f"FFprobe {exc.stream} exceeded its {exc.limit_bytes}-byte safety bound",
            recommendation="manual_review",
            retryable=False,
        ) from exc


def _probe_payload(completed: subprocess.CompletedProcess[bytes]) -> object:
    if completed.returncode != 0:
        _raise_probe_exit(completed)
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AudioProcessingError(
            "audio_probe_invalid_json",
            f"FFprobe returned invalid JSON: {exc}",
            recommendation="retry",
            retryable=True,
        ) from exc


def _raise_probe_exit(completed: subprocess.CompletedProcess[bytes]) -> None:
    detail = completed.stderr.decode("utf-8", errors="replace").strip()[:1000]
    structural = any(
        token in detail.casefold()
        for token in (
            "invalid data",
            "moov atom not found",
            "end of file",
            "header missing",
        )
    )
    raise AudioProcessingError(
        "audio_invalid_container" if structural else "audio_probe_error",
        detail or f"FFprobe exited with code {completed.returncode}",
        recommendation="deletion_candidate" if structural else "retry",
        retryable=not structural,
    )


# endregion [01]


# region [02] Strict probe schema decoding


def _decode_probe(payload: Any) -> MediaProbe:
    if not isinstance(payload, dict):
        raise AudioProcessingError(
            "audio_probe_schema",
            "FFprobe root is not an object",
            recommendation="retry",
            retryable=True,
        )
    audio, video, format_values = _probe_streams(payload)
    if not audio:
        _raise_missing_audio(format_values, video)
    durations = _stream_durations(format_values, audio)
    if not durations:
        raise AudioProcessingError(
            "audio_duration_unknown",
            "FFprobe could not determine a finite media duration",
            recommendation="manual_review",
            retryable=False,
        )
    first = audio[0]
    return MediaProbe(
        duration_seconds=max(durations),
        format_name=str(format_values.get("format_name") or "unknown")[:200],
        audio_codec=str(first.get("codec_name") or "unknown")[:100],
        sample_rate=_positive_int(first.get("sample_rate")),
        channels=_positive_int(first.get("channels")),
        audio_streams=len(audio),
        video_streams=len(video),
    )


def _probe_streams(
    payload: Mapping[str, Any],
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    Mapping[str, Any],
]:
    raw_streams = payload.get("streams", ())
    streams = (
        tuple(item for item in raw_streams if isinstance(item, dict))
        if isinstance(raw_streams, list)
        else ()
    )
    raw_format = payload.get("format")
    return (
        tuple(item for item in streams if item.get("codec_type") == "audio"),
        tuple(item for item in streams if item.get("codec_type") == "video"),
        raw_format if isinstance(raw_format, dict) else {},
    )


def _stream_durations(
    format_values: Mapping[str, Any],
    streams: tuple[Mapping[str, Any], ...],
) -> list[float]:
    return [
        value
        for value in (
            _finite_float(format_values.get("duration")),
            *(_finite_float(item.get("duration")) for item in streams),
        )
        if value is not None and value >= 0
    ]


def _raise_missing_audio(
    format_values: Mapping[str, Any],
    video: tuple[Mapping[str, Any], ...],
) -> None:
    video_durations = _stream_durations(format_values, video)
    raise AudioProcessingError(
        "media_without_audio_stream",
        "the media container has no audio stream",
        recommendation="manual_review",
        retryable=False,
        evidence={
            "format_name": str(format_values.get("format_name") or "unknown")[:200],
            "duration_seconds": max(video_durations) if video_durations else None,
            "video_streams": len(video),
        },
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


# endregion [02]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "_04_Nucleo_Operativo.audio_probe"
del _defined_value
