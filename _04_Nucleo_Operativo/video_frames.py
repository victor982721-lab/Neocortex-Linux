"""Ephemeral, bounded FFmpeg frame sampling for visual video evidence.

The sampler never materializes frames beside the source or inside the corpus.
It discovers a bounded set of scene/keyframe timestamps, fills the remaining
budget with deterministic interval samples, and removes every raster when the
context manager exits.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import xxhash

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture
from .cancellation import CancellationToken
from .capabilities.formats.image.png import probe_png_structure
from .video_models import VideoProcessingError


MAX_VIDEO_FRAMES = 256
MAX_VIDEO_FRAME_PIXELS = 40_000_000
MAX_VIDEO_FRAME_BYTES = 64 * 1024 * 1024
MAX_VIDEO_FRAME_BATCH_BYTES = 512 * 1024 * 1024
MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES = 2 * 1024 * 1024
_TIMESTAMP_TOLERANCE_MS = 250
_SHOWINFO_TIMESTAMP = re.compile(rb"\bpts_time:([0-9]+(?:\.[0-9]+)?)")


@dataclass(frozen=True, slots=True)
class VideoFrameSamplingConfig:
    max_frames: int = 48
    interval_seconds: float = 30.0
    scene_threshold: float = 0.35
    include_scenes: bool = True
    include_keyframes: bool = True
    max_frame_pixels: int = 2_073_600
    max_frame_side: int = 1920
    discovery_timeout_seconds: float = 60.0
    frame_timeout_seconds: float = 20.0
    file_timeout_seconds: float = 300.0
    worker_memory_bytes: int = 2 * 1024 * 1024 * 1024
    ffmpeg_path: str | None = None

    def validate(self) -> None:
        if not 1 <= self.max_frames <= MAX_VIDEO_FRAMES:
            raise ValueError(f"video max_frames must be between 1 and {MAX_VIDEO_FRAMES}")
        if self.interval_seconds <= 0:
            raise ValueError("video interval_seconds must be positive")
        if not 0.0 < self.scene_threshold < 1.0:
            raise ValueError("video scene_threshold must be between 0 and 1")
        if not 1 <= self.max_frame_pixels <= MAX_VIDEO_FRAME_PIXELS:
            raise ValueError(
                f"video max_frame_pixels must be between 1 and {MAX_VIDEO_FRAME_PIXELS}"
            )
        if self.max_frame_side < 1:
            raise ValueError("video max_frame_side must be positive")
        for name, value in (
            ("discovery_timeout_seconds", self.discovery_timeout_seconds),
            ("frame_timeout_seconds", self.frame_timeout_seconds),
            ("file_timeout_seconds", self.file_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"video {name} must be positive")
        if self.worker_memory_bytes < 1:
            raise ValueError("video worker_memory_bytes must be positive")


FrameReason = Literal["interval", "scene", "keyframe"]


@dataclass(frozen=True, slots=True)
class VideoFrameCandidate:
    timestamp_ms: int
    reasons: tuple[FrameReason, ...]


@dataclass(frozen=True, slots=True)
class ExtractedVideoFrame:
    index: int
    timestamp_ms: int
    reasons: tuple[FrameReason, ...]
    path: Path
    width: int
    height: int
    content_xxh3_128: str


@dataclass(frozen=True, slots=True)
class VideoFrameBatch:
    frames: tuple[ExtractedVideoFrame, ...]
    warnings: tuple[str, ...] = ()


def resolve_video_ffmpeg(explicit: str | None = None) -> str:
    candidate = explicit or "ffmpeg"
    resolved = shutil.which(candidate)
    if resolved is None:
        raise FileNotFoundError(f"FFmpeg executable was not found: {candidate}")
    return resolved


def bounded_frame_dimensions(
    width: int,
    height: int,
    *,
    max_pixels: int,
    max_side: int,
) -> tuple[int, int]:
    """Return aspect-preserving integer dimensions within both hard limits."""

    if width < 1 or height < 1:
        raise ValueError("video frame dimensions must be positive")
    scale = min(
        1.0,
        max_side / max(width, height),
        math.sqrt(max_pixels / (width * height)),
    )
    bounded_width = max(1, math.floor(width * scale))
    bounded_height = max(1, math.floor(height * scale))
    while bounded_width * bounded_height > max_pixels:
        if bounded_width >= bounded_height:
            bounded_width -= 1
        else:
            bounded_height -= 1
    return bounded_width, bounded_height


def parse_showinfo_timestamps(payload: bytes, *, duration_seconds: float) -> tuple[int, ...]:
    """Parse finite, in-range FFmpeg ``showinfo`` timestamps deterministically."""

    duration_ms = max(0, math.floor(duration_seconds * 1000))
    values: set[int] = set()
    for match in _SHOWINFO_TIMESTAMP.finditer(payload):
        try:
            seconds = float(match.group(1))
        except ValueError:
            continue
        if not math.isfinite(seconds) or seconds < 0:
            continue
        timestamp_ms = round(seconds * 1000)
        if timestamp_ms <= duration_ms:
            values.add(timestamp_ms)
    return tuple(sorted(values))


def _even_subset(values: tuple[int, ...], limit: int) -> tuple[int, ...]:
    if limit <= 0 or not values:
        return ()
    if len(values) <= limit:
        return values
    if limit == 1:
        return (values[len(values) // 2],)
    indexes = {round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)}
    return tuple(values[index] for index in sorted(indexes))


def _interval_timestamps(duration_seconds: float, interval_seconds: float) -> tuple[int, ...]:
    duration_ms = max(0, math.floor(duration_seconds * 1000))
    # Container duration commonly points just beyond the final decodable frame.
    # Stay a small bounded distance inside the media instead of manufacturing a
    # systematic end-of-stream extraction warning.
    end_guard_ms = min(250, max(1, duration_ms // 10)) if duration_ms else 0
    final = max(0, duration_ms - end_guard_ms)
    interval_ms = max(1, round(interval_seconds * 1000))
    values = list(range(0, final + 1, interval_ms))
    if not values or final - values[-1] >= min(interval_ms // 2, 1000):
        values.append(final)
    return tuple(sorted(set(values)))


def build_frame_plan(
    *,
    duration_seconds: float,
    max_frames: int,
    interval_seconds: float,
    scene_timestamps_ms: tuple[int, ...] = (),
    keyframe_timestamps_ms: tuple[int, ...] = (),
) -> tuple[VideoFrameCandidate, ...]:
    """Fuse bounded scene, keyframe and uniform coverage without score mixing."""

    if duration_seconds < 0 or not math.isfinite(duration_seconds):
        raise ValueError("video duration must be finite and non-negative")
    if not 1 <= max_frames <= MAX_VIDEO_FRAMES:
        raise ValueError(f"video max_frames must be between 1 and {MAX_VIDEO_FRAMES}")
    interval = _interval_timestamps(duration_seconds, interval_seconds)
    scene_budget = max_frames // 3
    keyframe_budget = max_frames // 3
    selected: list[tuple[int, FrameReason]] = []
    selected.extend((value, "scene") for value in _even_subset(scene_timestamps_ms, scene_budget))
    selected.extend(
        (value, "keyframe") for value in _even_subset(keyframe_timestamps_ms, keyframe_budget)
    )
    remaining = max_frames - len(selected)
    selected.extend((value, "interval") for value in _even_subset(interval, remaining))

    # Empty discovery sources donate their reserved budget back to uniform coverage.
    if len(selected) < max_frames:
        already = {timestamp for timestamp, _reason in selected}
        unused = tuple(value for value in interval if value not in already)
        selected.extend(
            (value, "interval") for value in _even_subset(unused, max_frames - len(selected))
        )

    fused: list[tuple[int, set[FrameReason]]] = []
    for timestamp, reason in sorted(selected):
        if fused and abs(timestamp - fused[-1][0]) <= _TIMESTAMP_TOLERANCE_MS:
            fused[-1][1].add(reason)
        else:
            fused.append((timestamp, {reason}))
    return tuple(
        VideoFrameCandidate(timestamp, tuple(sorted(reasons)))
        for timestamp, reasons in fused[:max_frames]
    )


def _remaining_timeout(deadline: float, ceiling: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoProcessingError(
            "video_file_timeout",
            "video frame inspection exceeded its total file time budget",
            recommendation="retry",
            retryable=True,
        )
    return min(remaining, ceiling)


def _discover_timestamps(
    source: Path,
    *,
    executable: str,
    stream_index: int,
    duration_seconds: float,
    selection: str,
    max_frames: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> tuple[int, ...]:
    command = (
        executable,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-an",
        "-sn",
        "-vf",
        f"select={selection},showinfo",
        "-frames:v",
        str(max_frames),
        "-f",
        "null",
        "-",
    )
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    try:
        result = run_bounded_capture(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            stderr_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            creationflags=creation_flags,
            memory_limit_bytes=memory_limit_bytes,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProcessingError(
            "video_frame_discovery_timeout",
            "FFmpeg timestamp discovery exceeded its bounded timeout",
            recommendation="retry",
            retryable=True,
        ) from exc
    except SubprocessOutputLimitError as exc:
        raise VideoProcessingError(
            "video_frame_discovery_output_limit",
            f"FFmpeg {exc.stream} exceeded its diagnostic output bound",
            recommendation="manual_review",
            retryable=False,
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[-1000:]
        raise VideoProcessingError(
            "video_frame_discovery_error",
            detail or f"FFmpeg exited with code {result.returncode}",
            recommendation="retry",
            retryable=True,
        )
    return parse_showinfo_timestamps(result.stderr, duration_seconds=duration_seconds)


def _extract_frame(
    source: Path,
    destination: Path,
    *,
    executable: str,
    stream_index: int,
    timestamp_ms: int,
    width: int,
    height: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> ExtractedVideoFrame:
    command = (
        executable,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-ss",
        f"{timestamp_ms / 1000:.3f}",
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-an",
        "-sn",
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        "-threads",
        "1",
        "-f",
        "image2",
        "-y",
        str(destination),
    )
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    try:
        result = run_bounded_capture(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            stderr_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            creationflags=creation_flags,
            memory_limit_bytes=memory_limit_bytes,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProcessingError(
            "video_frame_timeout",
            f"FFmpeg timed out extracting frame at {timestamp_ms} ms",
            recommendation="retry",
            retryable=True,
            evidence={"timestamp_ms": timestamp_ms},
        ) from exc
    except SubprocessOutputLimitError as exc:
        raise VideoProcessingError(
            "video_frame_output_limit",
            f"FFmpeg {exc.stream} exceeded its diagnostic output bound",
            recommendation="manual_review",
            retryable=False,
            evidence={"timestamp_ms": timestamp_ms},
        ) from exc
    if result.returncode != 0 or not destination.is_file():
        detail = result.stderr.decode("utf-8", "replace").strip()[-1000:]
        raise VideoProcessingError(
            "video_frame_extract_error",
            detail or f"FFmpeg exited with code {result.returncode}",
            recommendation="retry",
            retryable=True,
            evidence={"timestamp_ms": timestamp_ms},
        )
    size = destination.stat().st_size
    if not 1 <= size <= MAX_VIDEO_FRAME_BYTES:
        raise VideoProcessingError(
            "video_frame_byte_limit",
            f"extracted frame size {size} is outside the safety bound",
            recommendation="manual_review",
            retryable=False,
            evidence={"timestamp_ms": timestamp_ms, "frame_bytes": size},
        )
    png = probe_png_structure(destination)
    if png.status != "valid" or png.width is None or png.height is None:
        raise VideoProcessingError(
            "video_frame_invalid_png",
            f"FFmpeg frame failed structural PNG validation: {png.reason_code}",
            recommendation="retry",
            retryable=True,
            evidence={"timestamp_ms": timestamp_ms, **png.evidence()},
        )
    digest = xxhash.xxh3_128()
    with destination.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return ExtractedVideoFrame(
        index=-1,
        timestamp_ms=timestamp_ms,
        reasons=(),
        path=destination,
        width=png.width,
        height=png.height,
        content_xxh3_128=digest.hexdigest(),
    )


def _temporary_directory_outside_corpus(corpus_root: Path) -> tempfile.TemporaryDirectory[str]:
    temporary = tempfile.TemporaryDirectory(prefix="neocortex-video-frames-")
    scratch = Path(temporary.name).resolve()
    try:
        root = corpus_root.resolve(strict=False)
        if scratch == root or scratch.is_relative_to(root) or root.is_relative_to(scratch):
            raise VideoProcessingError(
                "video_scratch_intersects_corpus",
                "ephemeral video frame directory intersects the corpus",
                recommendation="manual_review",
                retryable=False,
            )
    except BaseException:
        temporary.cleanup()
        raise
    return temporary


@contextmanager
def sampled_video_frames(
    source: Path,
    *,
    corpus_root: Path,
    stream_index: int,
    source_width: int,
    source_height: int,
    duration_seconds: float,
    config: VideoFrameSamplingConfig,
    cancellation: CancellationToken,
) -> Iterator[VideoFrameBatch]:
    """Yield ephemeral sampled frames and guarantee recursive cleanup on exit."""

    config.validate()
    executable = resolve_video_ffmpeg(config.ffmpeg_path)
    target_width, target_height = bounded_frame_dimensions(
        source_width,
        source_height,
        max_pixels=config.max_frame_pixels,
        max_side=config.max_frame_side,
    )
    deadline = time.monotonic() + config.file_timeout_seconds
    warnings: list[str] = []
    scenes: tuple[int, ...] = ()
    keyframes: tuple[int, ...] = ()
    if config.include_scenes:
        try:
            scenes = _discover_timestamps(
                source,
                executable=executable,
                stream_index=stream_index,
                duration_seconds=duration_seconds,
                selection=f"gt(scene\\,{config.scene_threshold:g})",
                max_frames=config.max_frames,
                timeout_seconds=_remaining_timeout(deadline, config.discovery_timeout_seconds),
                memory_limit_bytes=config.worker_memory_bytes,
            )
        except VideoProcessingError as exc:
            warnings.append(exc.code)
    if config.include_keyframes:
        try:
            keyframes = _discover_timestamps(
                source,
                executable=executable,
                stream_index=stream_index,
                duration_seconds=duration_seconds,
                selection="eq(pict_type\\,I)",
                max_frames=config.max_frames,
                timeout_seconds=_remaining_timeout(deadline, config.discovery_timeout_seconds),
                memory_limit_bytes=config.worker_memory_bytes,
            )
        except VideoProcessingError as exc:
            warnings.append(exc.code)
    plan = build_frame_plan(
        duration_seconds=duration_seconds,
        max_frames=config.max_frames,
        interval_seconds=config.interval_seconds,
        scene_timestamps_ms=scenes,
        keyframe_timestamps_ms=keyframes,
    )
    if not plan:
        raise VideoProcessingError(
            "video_frame_plan_empty",
            "bounded frame sampling produced no timestamps",
            recommendation="manual_review",
            retryable=False,
        )

    with _temporary_directory_outside_corpus(corpus_root) as scratch_text:
        scratch = Path(scratch_text)
        extracted: list[ExtractedVideoFrame] = []
        extracted_bytes = 0
        for candidate in plan:
            cancellation.checkpoint()
            destination = scratch / f"frame-{len(extracted):04d}-{candidate.timestamp_ms}.png"
            try:
                raw = _extract_frame(
                    source,
                    destination,
                    executable=executable,
                    stream_index=stream_index,
                    timestamp_ms=candidate.timestamp_ms,
                    width=target_width,
                    height=target_height,
                    timeout_seconds=_remaining_timeout(deadline, config.frame_timeout_seconds),
                    memory_limit_bytes=config.worker_memory_bytes,
                )
            except VideoProcessingError as exc:
                warnings.append(exc.code)
                continue
            frame_bytes = raw.path.stat().st_size
            if extracted_bytes + frame_bytes > MAX_VIDEO_FRAME_BATCH_BYTES:
                warnings.append("video_frame_batch_byte_limit")
                raw.path.unlink(missing_ok=True)
                break
            extracted_bytes += frame_bytes
            extracted.append(
                ExtractedVideoFrame(
                    index=len(extracted),
                    timestamp_ms=raw.timestamp_ms,
                    reasons=candidate.reasons,
                    path=raw.path,
                    width=raw.width,
                    height=raw.height,
                    content_xxh3_128=raw.content_xxh3_128,
                )
            )
        if not extracted:
            raise VideoProcessingError(
                "video_frames_unavailable",
                "FFmpeg could not materialize any bounded visual frame",
                recommendation="retry",
                retryable=True,
                evidence={"planned_frames": len(plan), "warnings": sorted(set(warnings))},
            )
        yield VideoFrameBatch(tuple(extracted), tuple(sorted(set(warnings))))


__all__ = (
    "MAX_VIDEO_FRAMES",
    "MAX_VIDEO_FRAME_BATCH_BYTES",
    "MAX_VIDEO_FRAME_BYTES",
    "MAX_VIDEO_FRAME_PIXELS",
    "ExtractedVideoFrame",
    "VideoFrameBatch",
    "VideoFrameCandidate",
    "VideoFrameSamplingConfig",
    "bounded_frame_dimensions",
    "build_frame_plan",
    "parse_showinfo_timestamps",
    "resolve_video_ffmpeg",
    "sampled_video_frames",
)
