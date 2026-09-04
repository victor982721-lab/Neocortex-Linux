"""Stable file snapshots and exact or sampled XXH3 fingerprints."""

from __future__ import annotations

import os
import struct
from pathlib import Path

from neocortex.platform.policy import stat_birthtime_ns

from .domain.errors import FileChangedError, MissingDependencyError
from .domain.models import FileSnapshot
from .io import absolute_display_path, native_io_path

try:
    import xxhash
except ImportError as exc:  # pragma: no cover - exercised only without dependency
    raise MissingDependencyError(
        "deduplication requires the native 'xxhash' package (pip install xxhash)"
    ) from exc


FULL_ALGORITHM = "xxh3_128_full_v1"
PARTIAL_ALGORITHM = "xxh3_128_first_middle_last_v1_sample_262144"
DEFAULT_IO_CHUNK_SIZE = 16 * 1024 * 1024
DEFAULT_SAMPLE_SIZE = 256 * 1024
_MIN_IO_CHUNK_SIZE = 64 * 1024


def stat_matches_snapshot(snapshot: FileSnapshot, stat: os.stat_result) -> bool:
    """Match one captured stat result to the durable mutation invariant."""

    birthtime_ns = stat_birthtime_ns(stat)
    # Older callers (and durable observations written before the Linux
    # sentinel contract) used ``ctime`` when a platform did not expose a real
    # birth time.  Keep those observations verifiable without manufacturing a
    # birth time for new Linux snapshots: current captures still persist -1,
    # while an exact legacy ctime value is accepted only for this mutation
    # check.
    birthtime_matches = birthtime_ns == snapshot.birthtime_ns or (
        birthtime_ns == -1
        and snapshot.birthtime_ns >= 0
        and snapshot.birthtime_ns == stat.st_ctime_ns
    )
    return (
        stat.st_dev == snapshot.volume_id
        and stat.st_ino == snapshot.file_id
        and stat.st_size == snapshot.size
        and stat.st_mtime_ns == snapshot.mtime_ns
        and birthtime_matches
    )


def _assert_unchanged(snapshot: FileSnapshot, stat: os.stat_result) -> None:
    if not stat_matches_snapshot(snapshot, stat):
        raise FileChangedError(f"file changed while processing: {snapshot.path}")


def _validated_io_chunk_size(chunk_size: int) -> int:
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise TypeError("chunk_size must be an integer")
    if chunk_size < _MIN_IO_CHUNK_SIZE:
        raise ValueError("chunk_size must be at least 64 KiB")
    return chunk_size


def _adaptive_buffer_capacity(file_size: int, chunk_size: int) -> int:
    """Bound one reusable buffer to the observed file instead of the global limit."""

    return min(chunk_size, max(1, file_size))


def full_fingerprint(snapshot: FileSnapshot, *, chunk_size: int = DEFAULT_IO_CHUNK_SIZE) -> bytes:
    """Return an XXH3-128 digest after streaming the entire file once."""

    chunk_size = _validated_io_chunk_size(chunk_size)
    hasher = xxhash.xxh3_128()
    try:
        with open(native_io_path(snapshot.path), "rb", buffering=0) as stream:
            _assert_unchanged(snapshot, os.fstat(stream.fileno()))
            buffer = bytearray(_adaptive_buffer_capacity(snapshot.size, chunk_size))
            view = memoryview(buffer)
            while count := stream.readinto(buffer):
                hasher.update(view[:count])
            _assert_unchanged(snapshot, os.fstat(stream.fileno()))
    except FileChangedError:
        raise
    except OSError as exc:
        raise FileChangedError(f"cannot read {snapshot.path}: {exc}") from exc
    return hasher.digest()


def partial_fingerprint(snapshot: FileSnapshot, *, sample_size: int = DEFAULT_SAMPLE_SIZE) -> bytes:
    """Hash deterministic first/middle/last ranges, including their offsets."""

    if sample_size < 4096:
        raise ValueError("sample_size must be at least 4096 bytes")
    size = snapshot.size
    offsets = sorted({0, max(0, (size - sample_size) // 2), max(0, size - sample_size)})
    hasher = xxhash.xxh3_128()
    hasher.update(b"T_DEDUP_PARTIAL_V1\0")
    hasher.update(struct.pack("<QQ", size, sample_size))
    try:
        with open(native_io_path(snapshot.path), "rb", buffering=0) as stream:
            _assert_unchanged(snapshot, os.fstat(stream.fileno()))
            for offset in offsets:
                stream.seek(offset)
                data = stream.read(min(sample_size, size - offset))
                hasher.update(struct.pack("<QQ", offset, len(data)))
                hasher.update(data)
            _assert_unchanged(snapshot, os.fstat(stream.fileno()))
    except FileChangedError:
        raise
    except OSError as exc:
        raise FileChangedError(f"cannot sample {snapshot.path}: {exc}") from exc
    return hasher.digest()


def files_equal_exact(
    left: FileSnapshot,
    right: FileSnapshot,
    *,
    chunk_size: int = DEFAULT_IO_CHUNK_SIZE,
) -> bool:
    """Perform the final byte comparison required before a destructive policy."""

    chunk_size = _validated_io_chunk_size(chunk_size)
    if left.size != right.size:
        return False
    try:
        with (
            open(native_io_path(left.path), "rb", buffering=0) as left_stream,
            open(native_io_path(right.path), "rb", buffering=0) as right_stream,
        ):
            _assert_unchanged(left, os.fstat(left_stream.fileno()))
            _assert_unchanged(right, os.fstat(right_stream.fileno()))
            capacity = _adaptive_buffer_capacity(left.size, chunk_size)
            left_buffer = bytearray(capacity)
            right_buffer = bytearray(capacity)
            left_view = memoryview(left_buffer)
            right_view = memoryview(right_buffer)
            equal = True
            while True:
                left_count = left_stream.readinto(left_buffer)
                right_count = right_stream.readinto(right_buffer)
                if left_count != right_count:
                    equal = False
                    break
                if left_count == 0:
                    break
                if left_view[:left_count] != right_view[:right_count]:
                    equal = False
                    break
            _assert_unchanged(left, os.fstat(left_stream.fileno()))
            _assert_unchanged(right, os.fstat(right_stream.fileno()))
            return equal
    except FileChangedError:
        raise
    except OSError as exc:
        raise FileChangedError(f"cannot compare {left.path!r} and {right.path!r}: {exc}") from exc


def snapshot_path(path: str | Path) -> FileSnapshot:
    """Capture the identity and mutation fields used by all hash operations."""

    resolved = absolute_display_path(path)
    stat = os.stat(native_io_path(resolved), follow_symlinks=False)
    birthtime_ns = stat_birthtime_ns(stat)
    return FileSnapshot(
        path=resolved,
        volume_id=stat.st_dev,
        file_id=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        birthtime_ns=birthtime_ns,
    )


__all__ = [
    "DEFAULT_IO_CHUNK_SIZE",
    "DEFAULT_SAMPLE_SIZE",
    "FULL_ALGORITHM",
    "PARTIAL_ALGORITHM",
    "files_equal_exact",
    "full_fingerprint",
    "partial_fingerprint",
    "snapshot_path",
    "stat_matches_snapshot",
]
