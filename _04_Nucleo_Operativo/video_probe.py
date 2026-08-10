"""Bounded FFprobe metadata inspection for dedicated visual-video routing."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture
from .video_models import (
    SubtitleStreamProbe,
    VideoMediaProbe,
    VideoProcessingError,
    VideoStreamProbe,
)


MAX_VIDEO_FFPROBE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_VIDEO_STREAMS = 256
MAX_VIDEO_CHAPTERS = 10_000
MAX_VIDEO_DIMENSION = 131_072


def resolve_video_ffprobe(explicit: str | None = None) -> str:
    candidate = explicit or "ffprobe"
    resolved = shutil.which(candidate)
    if resolved is None:
        raise FileNotFoundError(f"FFprobe executable was not found: {candidate}")
    return resolved


def probe_video(
    path: Path,
    *,
    ffprobe_path: str | None = None,
    timeout_seconds: float = 30.0,
) -> VideoMediaProbe:
    """Return bounded visual metadata, accepting media without an audio stream."""

    if timeout_seconds <= 0:
        raise ValueError("FFprobe timeout must be positive")
    executable = resolve_video_ffprobe(ffprobe_path)
    command = (
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:"
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration:"
        "stream_tags=language,rotate:stream_side_data=rotation:"
        "chapter=id,start_time,end_time",
        "-of",
        "json",
        str(path),
    )
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    try:
        completed = run_bounded_capture(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=MAX_VIDEO_FFPROBE_OUTPUT_BYTES,
            stderr_limit_bytes=MAX_VIDEO_FFPROBE_OUTPUT_BYTES,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProcessingError(
            "video_probe_timeout",
            f"FFprobe exceeded {timeout_seconds:g} seconds",
            recommendation="retry",
            retryable=True,
        ) from exc
    except SubprocessOutputLimitError as exc:
        raise VideoProcessingError(
            "video_probe_output_limit",
            f"FFprobe {exc.stream} exceeded its {exc.limit_bytes}-byte safety bound",
            recommendation="manual_review",
            retryable=False,
        ) from exc
    if completed.returncode != 0:
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
        raise VideoProcessingError(
            "video_invalid_container" if structural else "video_probe_error",
            detail or f"FFprobe exited with code {completed.returncode}",
            recommendation="deletion_candidate" if structural else "retry",
            retryable=not structural,
        )
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VideoProcessingError(
            "video_probe_invalid_json",
            f"FFprobe returned invalid JSON: {exc}",
            recommendation="retry",
            retryable=True,
        ) from exc
    return decode_video_probe(payload)


def decode_video_probe(payload: Any) -> VideoMediaProbe:
    """Decode the bounded FFprobe projection and reject ambiguous media."""

    if not isinstance(payload, dict):
        raise _schema_error("FFprobe root is not an object")
    raw_streams = payload.get("streams", ())
    if not isinstance(raw_streams, list):
        raise _schema_error("FFprobe streams is not an array")
    if len(raw_streams) > MAX_VIDEO_STREAMS:
        raise _schema_error(f"media declares more than {MAX_VIDEO_STREAMS} streams")
    streams = tuple(item for item in raw_streams if isinstance(item, dict))
    raw_video = tuple(item for item in streams if item.get("codec_type") == "video")
    if not raw_video:
        raise VideoProcessingError(
            "media_without_video_stream",
            "the media container has no visual video stream",
            recommendation="manual_review",
            retryable=False,
        )
    video = tuple(_decode_video_stream(item) for item in raw_video)
    subtitles = tuple(
        _decode_subtitle_stream(item) for item in streams if item.get("codec_type") == "subtitle"
    )
    raw_chapters = payload.get("chapters", ())
    if not isinstance(raw_chapters, list):
        raise _schema_error("FFprobe chapters is not an array")
    if len(raw_chapters) > MAX_VIDEO_CHAPTERS:
        raise _schema_error(f"media declares more than {MAX_VIDEO_CHAPTERS} chapters")

    raw_format = payload.get("format")
    format_values = raw_format if isinstance(raw_format, dict) else {}
    durations = [
        value
        for value in (
            _finite_nonnegative_float(format_values.get("duration")),
            *(_finite_nonnegative_float(item.get("duration")) for item in raw_video),
        )
        if value is not None
    ]
    if not durations:
        raise VideoProcessingError(
            "video_duration_unknown",
            "FFprobe could not determine a finite media duration",
            recommendation="manual_review",
            retryable=False,
        )
    return VideoMediaProbe(
        duration_seconds=max(durations),
        format_name=str(format_values.get("format_name") or "unknown")[:200],
        video=video,
        audio_streams=sum(item.get("codec_type") == "audio" for item in streams),
        subtitles=subtitles,
        chapters=len(raw_chapters),
    )


def _decode_video_stream(value: Mapping[str, object]) -> VideoStreamProbe:
    width = _bounded_positive_int(value.get("width"), field_name="width")
    height = _bounded_positive_int(value.get("height"), field_name="height")
    frame_rate = _frame_rate(value.get("avg_frame_rate"))
    if frame_rate is None:
        frame_rate = _frame_rate(value.get("r_frame_rate"))
    return VideoStreamProbe(
        index=_nonnegative_int(value.get("index"), field_name="stream index"),
        codec_name=str(value.get("codec_name") or "unknown")[:100],
        width=width,
        height=height,
        frame_rate=frame_rate,
        duration_seconds=_finite_nonnegative_float(value.get("duration")),
        rotation_degrees=_rotation(value),
    )


def _decode_subtitle_stream(value: Mapping[str, object]) -> SubtitleStreamProbe:
    tags = value.get("tags")
    tag_values = tags if isinstance(tags, dict) else {}
    language_value = tag_values.get("language")
    language = None if language_value is None else str(language_value).strip()[:32] or None
    return SubtitleStreamProbe(
        index=_nonnegative_int(value.get("index"), field_name="stream index"),
        codec_name=str(value.get("codec_name") or "unknown")[:100],
        language=language,
    )


def _rotation(value: Mapping[str, object]) -> int | None:
    tags = value.get("tags")
    candidates: list[object] = []
    if isinstance(tags, dict):
        candidates.append(tags.get("rotate"))
    side_data = value.get("side_data_list")
    if isinstance(side_data, list):
        candidates.extend(item.get("rotation") for item in side_data if isinstance(item, dict))
    for candidate in candidates:
        if isinstance(candidate, bool) or not isinstance(
            candidate, (int, float, str, bytes, bytearray)
        ):
            continue
        try:
            rotation = int(candidate)
        except (TypeError, ValueError):
            continue
        if -360 <= rotation <= 360:
            return rotation
    return None


def _frame_rate(value: object) -> float | None:
    result: float | None
    if isinstance(value, str) and "/" in value:
        numerator_text, denominator_text = value.split("/", 1)
        try:
            numerator = float(numerator_text)
            denominator = float(denominator_text)
        except ValueError:
            return None
        if denominator == 0:
            return None
        result = numerator / denominator
    else:
        result = _finite_nonnegative_float(value)
        if result is None:
            return None
    assert result is not None
    return result if math.isfinite(result) and 0 < result <= 100_000 else None


def _finite_nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _nonnegative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        raise _schema_error(f"video {field_name} is not an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _schema_error(f"video {field_name} is not an integer") from exc
    if result < 0:
        raise _schema_error(f"video {field_name} cannot be negative")
    return result


def _bounded_positive_int(value: object, *, field_name: str) -> int:
    result = _nonnegative_int(value, field_name=field_name)
    if not 1 <= result <= MAX_VIDEO_DIMENSION:
        raise _schema_error(f"video {field_name} must be between 1 and {MAX_VIDEO_DIMENSION}")
    return result


def _schema_error(message: str) -> VideoProcessingError:
    return VideoProcessingError(
        "video_probe_schema",
        message,
        recommendation="manual_review",
        retryable=False,
    )


__all__ = (
    "MAX_VIDEO_CHAPTERS",
    "MAX_VIDEO_DIMENSION",
    "MAX_VIDEO_FFPROBE_OUTPUT_BYTES",
    "MAX_VIDEO_STREAMS",
    "decode_video_probe",
    "probe_video",
    "resolve_video_ffprobe",
)
