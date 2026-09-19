"""Ephemeral, bounded FFmpeg frame sampling for visual video evidence.

The sampler never materializes frames beside the source or inside the corpus.
It discovers a bounded set of scene/keyframe timestamps, fills the remaining
budget with deterministic interval samples, and removes every raster on a
successful context-manager exit.  A registered workspace retains a failed
attempt for the runtime maintenance owner to inspect.
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
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from neocortex.foundation.hash_compat import xxhash

from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)
from neocortex.runtime.control.cancellation import CancellationToken
from ..media_resources import checkpoint_before_deadline, native_subprocess_arguments
from neocortex.runtime.control.elastic_workers import current_worker_cancellation
from neocortex.runtime.control.global_resources import current_resource_grant
from neocortex.capabilities.formats.image.png import probe_png_structure
from .models import VideoProcessingError
from .limits import (
    DEFAULT_VIDEO_WORKER_MEMORY_BYTES,
    MAX_VIDEO_FRAME_PIXELS,
    MAX_VIDEO_FRAMES,
)


MAX_VIDEO_FRAME_BYTES = 64 * 1024 * 1024
MAX_VIDEO_FRAME_BATCH_BYTES = 512 * 1024 * 1024
MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES = 2 * 1024 * 1024
VIDEO_FRAME_SAMPLING_POLICY = "frame-sampling-v3-bounded-batch"
REGISTERED_SCRATCH_OWNER = "video-frame-sampler"
VIDEO_FRAME_SCRATCH_SCOPE = "video-frames"
_TIMESTAMP_TOLERANCE_MS = 250
_SHOWINFO_TIMESTAMP = re.compile(rb"\bpts_time:([0-9]+(?:\.[0-9]+)?)")


def frame_batch_output_limit(pixels: int, count: int) -> int:
    return min(MAX_VIDEO_FRAME_BATCH_BYTES, count * min(MAX_VIDEO_FRAME_BYTES, pixels * 8 + 65536))


def video_worker_memory_reservation(config) -> int:
    """Native address-space limit plus capture bytearray/copy and Python owner."""
    capture = frame_batch_output_limit(config.max_frame_pixels, config.max_frames)
    return config.worker_memory_bytes + 2 * capture + 64 * 1024 * 1024


def _producer_remaining_timeout(deadline: float, timeout_error: VideoProcessingError) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise timeout_error
    return remaining


def _native_resources(cancellation=None, *, deadline=None, timeout_error=None):
    token = cancellation or current_worker_cancellation()
    if token is not None:
        token.checkpoint()
    grant = current_resource_grant()
    if grant is None:
        return "1", native_subprocess_arguments(None, token)
    if deadline is None:
        grant.checkpoint()
    else:
        checkpoint_before_deadline(grant, deadline, token, timeout_error)
    return str(max(1, grant.native_threads)), native_subprocess_arguments(grant, token)


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
    worker_memory_bytes: int = DEFAULT_VIDEO_WORKER_MEMORY_BYTES
    ffmpeg_path: str | None = None
    # A route supplies the state-owned registered scratch root.  ``None`` is
    # retained for direct callers that only need the historical ephemeral API
    # and do not have a state owner to bind to.
    scratch_directory: Path | None = None
    run_id: int | str | None = None

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


def _interval_timestamps(
    duration_seconds: float,
    interval_seconds: float,
    *,
    frame_rate: float | None = None,
) -> tuple[int, ...]:
    duration_ms = max(0, math.floor(duration_seconds * 1000))
    # Container duration commonly points just beyond the final decodable frame.
    # Stay a small bounded distance inside the media instead of manufacturing a
    # systematic end-of-stream extraction warning.
    end_guard_ms = min(250, max(1, duration_ms // 10)) if duration_ms else 0
    if frame_rate is not None:
        # A low-rate stream can hold its final frame for longer than the
        # historical 250 ms guard.  Respect at least one observed frame period
        # without changing scene/keyframe evidence or the interval policy.
        # An average rate is not proof of the last PTS for variable-rate media;
        # extraction errors remain explicit rather than being treated as success.
        frame_guard_ms = (
            duration_ms if frame_rate * duration_seconds <= 1.0 else math.ceil(1000 / frame_rate)
        )
        end_guard_ms = max(end_guard_ms, frame_guard_ms)
    final = max(0, duration_ms - end_guard_ms)
    if frame_rate is not None:
        # A container duration can include an audio tail or muxing/padding
        # beyond the final video frame.  Keep the interval endpoint below the
        # last frame-rate-aligned slot that can start before that duration.
        # This is a seek upper bound, not an invented PTS; scene/keyframe
        # observations remain untouched and extraction failures stay warnings.
        frame_slots = math.floor(duration_seconds * frame_rate + 0.5)
        if frame_slots > 0:
            aligned_end = math.floor((frame_slots - 1) * 1000 / frame_rate)
            final = min(final, max(0, aligned_end))
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
    frame_rate: float | None = None,
) -> tuple[VideoFrameCandidate, ...]:
    """Fuse bounded scene, keyframe and uniform coverage without score mixing."""

    _validate_frame_plan_inputs(duration_seconds, max_frames, frame_rate=frame_rate)
    interval = _interval_timestamps(duration_seconds, interval_seconds, frame_rate=frame_rate)
    selected = _initial_frame_selections(
        interval,
        scene_timestamps_ms,
        keyframe_timestamps_ms,
        max_frames,
    )
    _fill_interval_budget(selected, interval, max_frames)
    return _fuse_frame_candidates(selected, max_frames)


def _validate_frame_plan_inputs(
    duration_seconds: float, max_frames: int, *, frame_rate: float | None = None
) -> None:
    if duration_seconds < 0 or not math.isfinite(duration_seconds):
        raise ValueError("video duration must be finite and non-negative")
    if not 1 <= max_frames <= MAX_VIDEO_FRAMES:
        raise ValueError(f"video max_frames must be between 1 and {MAX_VIDEO_FRAMES}")
    if frame_rate is not None and (frame_rate <= 0 or not math.isfinite(frame_rate)):
        raise ValueError("video frame_rate must be finite and positive when known")


def _initial_frame_selections(
    interval: tuple[int, ...],
    scenes: tuple[int, ...],
    keyframes: tuple[int, ...],
    max_frames: int,
) -> list[tuple[int, FrameReason]]:
    scene_budget = max_frames // 3
    keyframe_budget = max_frames // 3
    selected: list[tuple[int, FrameReason]] = []
    selected.extend((value, "scene") for value in _even_subset(scenes, scene_budget))
    selected.extend((value, "keyframe") for value in _even_subset(keyframes, keyframe_budget))
    remaining = max_frames - len(selected)
    selected.extend((value, "interval") for value in _even_subset(interval, remaining))
    return selected


def _fill_interval_budget(
    selected: list[tuple[int, FrameReason]],
    interval: tuple[int, ...],
    max_frames: int,
) -> None:
    # Empty discovery sources donate their reserved budget back to uniform coverage.
    if len(selected) >= max_frames:
        return
    already = {timestamp for timestamp, _reason in selected}
    unused = tuple(value for value in interval if value not in already)
    selected.extend(
        (value, "interval") for value in _even_subset(unused, max_frames - len(selected))
    )


def _fuse_frame_candidates(
    selected: list[tuple[int, FrameReason]],
    max_frames: int,
) -> tuple[VideoFrameCandidate, ...]:
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
    cancellation: CancellationToken | None = None,
) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout_seconds
    timeout_error = VideoProcessingError(
        "video_frame_discovery_timeout",
        "FFmpeg timestamp discovery exceeded its bounded timeout",
        recommendation="retry", retryable=True,
    )
    threads, resources = _native_resources(
        cancellation, deadline=deadline, timeout_error=timeout_error,
    )
    command = (
        executable,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-threads",
        threads,
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
            timeout_seconds=_producer_remaining_timeout(deadline, timeout_error),
            stdout_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            stderr_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            creationflags=creation_flags,
            memory_limit_bytes=memory_limit_bytes,
            **resources,
        )
    except subprocess.TimeoutExpired as exc:
        raise timeout_error from exc
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
    threads, _resources = _native_resources()
    command = (
        executable,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-threads",
        threads,
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
    result = _run_frame_extraction(
        command,
        timestamp_ms=timestamp_ms,
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
    )
    frame_width, frame_height = _validate_extracted_frame(result, destination, timestamp_ms)
    return ExtractedVideoFrame(
        index=-1,
        timestamp_ms=timestamp_ms,
        reasons=(),
        path=destination,
        width=frame_width,
        height=frame_height,
        content_xxh3_128=_frame_content_digest(destination),
    )


def _run_frame_extraction(
    command: tuple[str, ...],
    *,
    timestamp_ms: int,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> subprocess.CompletedProcess[bytes]:
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    _threads, resources = _native_resources()
    try:
        return run_bounded_capture(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            stderr_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            creationflags=creation_flags,
            memory_limit_bytes=memory_limit_bytes,
            **resources,
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


def _validate_extracted_frame(
    result: subprocess.CompletedProcess[bytes],
    destination: Path,
    timestamp_ms: int,
) -> tuple[int, int]:
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
    assert png.width is not None and png.height is not None
    return png.width, png.height


def _frame_content_digest(path: Path) -> str:
    digest = xxhash.xxh3_128()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _png_payloads(payload: bytes) -> Iterator[memoryview]:
    """Walk a bounded image2pipe stream without decompressing or copying rasters."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        start = offset
        if view[offset:offset + 8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("invalid PNG batch signature")
        offset += 8
        while True:
            if offset + 12 > len(view):
                raise ValueError("truncated PNG batch chunk")
            length = int.from_bytes(view[offset:offset + 4], "big")
            kind = view[offset + 4:offset + 8]
            offset += 12 + length
            if offset > len(view) or offset - start > MAX_VIDEO_FRAME_BYTES:
                raise ValueError("PNG batch frame exceeds its byte bound")
            if kind == b"IEND":
                if length:
                    raise ValueError("invalid PNG batch terminator")
                yield view[start:offset]
                break


def _extract_frames(
    source: Path, scratch: Path, *, plan: tuple[VideoFrameCandidate, ...],
    executable: str, stream_index: int, width: int, height: int,
    timeout_seconds: float, memory_limit_bytes: int,
    cancellation: CancellationToken | None = None,
) -> tuple[ExtractedVideoFrame, ...]:
    """Decode once, retaining only the first raster at or after each planned time.

    Several requested times can identify the same low-rate source frame. They
    share its exact raster while retaining their own timestamp and typed reasons,
    matching the previous seek-per-candidate contract.
    """
    deadline = time.monotonic() + timeout_seconds
    timeout_error = VideoProcessingError(
        "video_frame_timeout", "FFmpeg batch extraction exceeded its bounded timeout",
        recommendation="retry", retryable=True,
    )
    threads, resources = _native_resources(
        cancellation, deadline=deadline, timeout_error=timeout_error,
    )
    first = plan[0].timestamp_ms / 1000
    selections = [f"isnan(prev_selected_t)*gte(t\\,{first:.3f})"]
    selections.extend(
        f"gte(t\\,{item.timestamp_ms / 1000:.3f})*lt(prev_selected_t\\,{item.timestamp_ms / 1000:.3f})"
        for item in plan
    )
    command = (
        executable, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-threads", threads, "-filter_threads", "1", "-i", str(source),
        "-map", f"0:{stream_index}", "-an", "-sn", "-vf",
        f"select={'+'.join(selections)},scale={width}:{height}:force_original_aspect_ratio=decrease,showinfo",
        "-frames:v", str(len(plan)), "-fps_mode", "passthrough", "-threads", "1",
        "-map_metadata", "-1", "-pix_fmt", "rgb24", "-c:v", "png", "-f", "image2pipe", "pipe:1",
    )
    # The captured PNG stream and materialized files both have explicit bounds.
    output_limit = frame_batch_output_limit(width * height, len(plan))
    grant = current_resource_grant()
    if grant is not None:
        # Admit the entire possible scratch peak before decoding: incremental
        # upgrades while several videos retain earlier rasters can deadlock.
        grant.resize_temp_bytes(output_limit, directory=scratch)
    try:
        result = run_bounded_capture(
            command, timeout_seconds=_producer_remaining_timeout(deadline, timeout_error),
            stdout_limit_bytes=output_limit,
            stderr_limit_bytes=MAX_VIDEO_FFMPEG_DIAGNOSTIC_BYTES,
            memory_limit_bytes=memory_limit_bytes, **resources,
        )
    except subprocess.TimeoutExpired as exc:
        raise timeout_error from exc
    except SubprocessOutputLimitError as exc:
        raise VideoProcessingError(
            "video_frame_batch_byte_limit", f"FFmpeg batch {exc.stream} exceeded its output bound",
            recommendation="manual_review", retryable=False,
        ) from exc
    if result.returncode:
        raise VideoProcessingError(
            "video_frame_extract_error", result.stderr.decode("utf-8", "replace")[-1000:],
            recommendation="retry", retryable=True,
        )
    timestamps = [round(float(value) * 1000) for value in _SHOWINFO_TIMESTAMP.findall(result.stderr)]
    rasters = []
    try:
        for index, png in enumerate(_png_payloads(result.stdout)):
            if (token := resources.get("cancellation")) is not None:
                token.checkpoint()
            _producer_remaining_timeout(deadline, timeout_error)
            if index >= len(timestamps) or index >= len(plan):
                raise ValueError("PNG batch and timestamp counts disagree")
            destination = scratch / f"frame-{index:04d}-{timestamps[index]}.png"
            destination.write_bytes(png)
            frame_width, frame_height = _validate_extracted_frame(result, destination, timestamps[index])
            if frame_width > width or frame_height > height:
                raise ValueError("PNG batch dimensions exceed the requested bounds")
            rasters.append((timestamps[index], destination, frame_width, frame_height,
                            xxhash.xxh3_128(png).hexdigest()))
        if len(rasters) != len(timestamps):
            raise ValueError("PNG batch and timestamp counts disagree")
    except ValueError as exc:
        raise VideoProcessingError(
            "video_frame_invalid_png", str(exc), recommendation="retry", retryable=True,
        ) from exc
    extracted: list[ExtractedVideoFrame] = []
    for candidate in plan:
        raster = next((item for item in rasters if item[0] + 1 >= candidate.timestamp_ms), None)
        if raster is not None:
            _actual_time, path, frame_width, frame_height, digest = raster
            extracted.append(ExtractedVideoFrame(
                len(extracted), candidate.timestamp_ms, candidate.reasons,
                path, frame_width, frame_height, digest,
            ))
    if grant is not None:
        grant.resize_temp_bytes(sum(item[1].stat().st_size for item in rasters), directory=scratch)
    return tuple(extracted)


def video_frame_scratch_root(state_path: Path) -> Path:
    """Return the canonical state-owned root for video frame scratch."""

    state = Path(state_path)
    if not state.is_absolute():
        state = state.absolute()
    return state.parent / "scratch" / VIDEO_FRAME_SCRATCH_SCOPE


def _temporary_directory_outside_corpus(corpus_root: Path) -> tempfile.TemporaryDirectory[str]:
    temporary = tempfile.TemporaryDirectory(prefix="neocortex-video-frames-")
    scratch = Path(temporary.name).resolve()
    try:
        _validate_video_scratch_disjoint(scratch, corpus_root)
    except BaseException:
        temporary.cleanup()
        raise
    return temporary


def _validate_video_scratch_disjoint(scratch: Path, corpus_root: Path) -> None:
    """Reject any scratch root that can contain or be contained by corpus."""

    scratch_resolved = scratch.resolve(strict=False)
    corpus_resolved = corpus_root.resolve(strict=False)
    if (
        scratch_resolved == corpus_resolved
        or scratch_resolved.is_relative_to(corpus_resolved)
        or corpus_resolved.is_relative_to(scratch_resolved)
    ):
        raise VideoProcessingError(
            "video_scratch_intersects_corpus",
            "video frame scratch directory intersects the corpus",
            recommendation="manual_review",
            retryable=False,
        )


def _video_artifact_registry_root(scratch_directory: Path) -> Path | None:
    """Derive the registry only for the canonical state-owned video root."""

    scratch = Path(scratch_directory)
    if (
        not scratch.is_absolute()
        or scratch.name != VIDEO_FRAME_SCRATCH_SCOPE
        or scratch.parent.name != "scratch"
    ):
        # Direct callers may provide an unrelated private scratch root.  Do
        # not guess a state directory (or a HOME/corpus-relative registry).
        return None
    return scratch.parent.parent / "artifacts"


@contextmanager
def _registered_video_scratch_workspace(
    scratch_directory: Path,
    *,
    corpus_root: Path,
    run_id: int | str | None = None,
) -> Iterator[Path]:
    """Yield one runtime-registered workspace for sampled frame rasters.

    The runtime scratch owner is deliberately the only cleanup authority for
    this path.  Successful sampling closes the workspace through its normal
    completion transition; a producer failure is retained as
    ``failed-retained`` so maintenance can inspect it later.  There is no
    filesystem cleanup fallback for an explicitly configured registered root.
    """

    _validate_video_scratch_disjoint(scratch_directory, corpus_root)
    try:
        from neocortex.runtime.scratch import ScratchManager
    except (ImportError, ModuleNotFoundError) as exc:
        raise VideoProcessingError(
            "video_scratch_service_unavailable",
            "registered video frame scratch service is unavailable",
            recommendation="manual_review",
            retryable=False,
        ) from exc

    try:
        artifact_registry_root = _video_artifact_registry_root(scratch_directory)
        manager_kwargs: dict[str, Path] = {}
        if artifact_registry_root is not None:
            manager_kwargs["artifact_registry_root"] = artifact_registry_root
        manager = ScratchManager(
            scratch_directory,
            owner=REGISTERED_SCRATCH_OWNER,
            create_root=True,
            **manager_kwargs,
        )
        create = getattr(manager, "create", None)
        if not callable(create):
            create = getattr(manager, "create_workspace", None)
        if not callable(create):
            raise TypeError("registered video frame scratch service has no workspace creator")
        workspace = create(
            run_id=run_id,
            retain_on_success=False,
            metadata={
                "component": REGISTERED_SCRATCH_OWNER,
                "operation": "sampled_video_frames",
            },
        )
    except Exception as exc:
        raise VideoProcessingError(
            "video_scratch_setup_error",
            f"could not create registered video frame scratch workspace: {exc}",
            recommendation="manual_review",
            retryable=False,
            evidence={"error_type": type(exc).__name__},
        ) from exc

    path = getattr(workspace, "path", None)
    if not isinstance(path, Path):
        # Do not attempt an unregistered fallback when the service contract is
        # malformed; the caller must receive a typed, reviewable failure.
        raise VideoProcessingError(
            "video_scratch_setup_error",
            "registered video frame scratch workspace has no Path path",
            recommendation="manual_review",
            retryable=False,
        )
    try:
        yield path
    except BaseException as error:
        fail = getattr(workspace, "fail", None)
        if callable(fail):
            try:
                fail(_video_scratch_failure_reason(error))
            except BaseException:
                # Preserve the producer failure.  The runtime owner will
                # surface recovery on maintenance if retention itself failed.
                pass
        raise
    else:
        complete = getattr(workspace, "complete", None)
        if not callable(complete):
            raise VideoProcessingError(
                "video_scratch_setup_error",
                "registered video frame scratch workspace has no complete() transition",
                recommendation="manual_review",
                retryable=False,
            )
        try:
            complete()
        except VideoProcessingError:
            raise
        except Exception as exc:
            raise VideoProcessingError(
                "video_scratch_cleanup_error",
                f"registered video frame scratch workspace could not close: {exc}",
                recommendation="manual_review",
                retryable=False,
                evidence={"error_type": type(exc).__name__},
            ) from exc


def _video_scratch_failure_reason(error: BaseException) -> str:
    """Return a bounded manifest reason without masking the primary failure."""

    reason = f"{type(error).__name__}: {error}".replace("\x00", "\\0")
    encoded = reason.encode("utf-8")
    if len(encoded) <= 8 * 1024:
        return reason
    return encoded[: 8 * 1024 - 3].decode("utf-8", "ignore") + "..."


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
    frame_rate: float | None = None,
) -> Iterator[VideoFrameBatch]:
    """Yield ephemeral sampled frames and guarantee recursive cleanup on exit."""

    config.validate()
    _validate_frame_plan_inputs(duration_seconds, config.max_frames, frame_rate=frame_rate)
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
                cancellation=cancellation,
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
                cancellation=cancellation,
            )
        except VideoProcessingError as exc:
            warnings.append(exc.code)
    plan = build_frame_plan(
        duration_seconds=duration_seconds,
        max_frames=config.max_frames,
        interval_seconds=config.interval_seconds,
        scene_timestamps_ms=scenes,
        keyframe_timestamps_ms=keyframes,
        frame_rate=frame_rate,
    )
    if not plan:
        raise VideoProcessingError(
            "video_frame_plan_empty",
            "bounded frame sampling produced no timestamps",
            recommendation="manual_review",
            retryable=False,
        )

    scratch_context: AbstractContextManager[str | Path]
    if config.scratch_directory is None:
        scratch_context = _temporary_directory_outside_corpus(corpus_root)
    else:
        scratch_context = _registered_video_scratch_workspace(
            config.scratch_directory,
            corpus_root=corpus_root,
            run_id=config.run_id,
        )
    with scratch_context as scratch_value:
        scratch = Path(scratch_value)
        cancellation.checkpoint()
        try:
            extracted = _extract_frames(
                source, scratch, plan=plan, executable=executable, stream_index=stream_index,
                width=target_width, height=target_height,
                timeout_seconds=_remaining_timeout(
                    deadline, config.frame_timeout_seconds * len(plan),
                ),
                memory_limit_bytes=config.worker_memory_bytes,
                cancellation=cancellation,
            )
        except VideoProcessingError as exc:
            warnings.append(exc.code)
            extracted = ()
        cancellation.checkpoint()
        if extracted and len(extracted) < len(plan):
            warnings.append("video_frame_extract_error")
        if not extracted:
            raise VideoProcessingError(
                "video_frames_unavailable",
                "FFmpeg could not materialize any bounded visual frame",
                recommendation="retry",
                retryable=True,
                evidence={"planned_frames": len(plan), "warnings": sorted(set(warnings))},
            )
        yield VideoFrameBatch(tuple(extracted), tuple(sorted(set(warnings))))
    grant = current_resource_grant()
    if grant is not None:
        # The scratch owner has completed cleanup before capacity is returned.
        grant.resize_temp_bytes(0)


__all__ = (
    "MAX_VIDEO_FRAMES",
    "MAX_VIDEO_FRAME_BATCH_BYTES",
    "MAX_VIDEO_FRAME_BYTES",
    "MAX_VIDEO_FRAME_PIXELS",
    "REGISTERED_SCRATCH_OWNER",
    "VIDEO_FRAME_SAMPLING_POLICY",
    "VIDEO_FRAME_SCRATCH_SCOPE",
    "ExtractedVideoFrame",
    "VideoFrameBatch",
    "VideoFrameCandidate",
    "VideoFrameSamplingConfig",
    "bounded_frame_dimensions",
    "build_frame_plan",
    "parse_showinfo_timestamps",
    "resolve_video_ffmpeg",
    "sampled_video_frames",
    "video_frame_scratch_root",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.video.frames"
del _defined_value
