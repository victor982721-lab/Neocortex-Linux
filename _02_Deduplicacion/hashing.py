"""Fast non-cryptographic fingerprints with mutation detection."""
# region [00] Contexto del módulo
# Módulo: _02_Deduplicacion/hashing.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import struct
from pathlib import Path

from neocortex.platform_policy import stat_birthtime_ns

from .errors import FileChangedError, MissingDependencyError
from .models import FileSnapshot
from .path_io import absolute_display_path, native_io_path
# endregion [01]

# region [02] Implementación

try:
    import xxhash
except ImportError as exc:  # pragma: no cover - exercised only without dependency
    raise MissingDependencyError(
        "_02_Deduplicacion requires the native 'xxhash' package (pip install xxhash)"
    ) from exc


FULL_ALGORITHM = "xxh3_128_full_v1"
PARTIAL_ALGORITHM = "xxh3_128_first_middle_last_v1_sample_262144"
DEFAULT_IO_CHUNK_SIZE = 16 * 1024 * 1024
DEFAULT_SAMPLE_SIZE = 256 * 1024


def stat_matches_snapshot(snapshot: FileSnapshot, stat: os.stat_result) -> bool:
    """Match one captured stat result to the durable mutation invariant."""

    birthtime_ns = stat_birthtime_ns(stat)
    return (
        stat.st_dev == snapshot.volume_id
        and stat.st_ino == snapshot.file_id
        and stat.st_size == snapshot.size
        and stat.st_mtime_ns == snapshot.mtime_ns
        and birthtime_ns == snapshot.birthtime_ns
    )


def _assert_unchanged(snapshot: FileSnapshot, stat: os.stat_result) -> None:
    if not stat_matches_snapshot(snapshot, stat):
        raise FileChangedError(f"file changed while processing: {snapshot.path}")


def full_fingerprint(snapshot: FileSnapshot, *, chunk_size: int = DEFAULT_IO_CHUNK_SIZE) -> bytes:
    """Return an XXH3-128 digest after streaming the entire file once."""

    if chunk_size < 64 * 1024:
        raise ValueError("chunk_size must be at least 64 KiB")
    hasher = xxhash.xxh3_128()
    buffer = bytearray(chunk_size)
    view = memoryview(buffer)
    try:
        with open(native_io_path(snapshot.path), "rb", buffering=0) as stream:
            _assert_unchanged(snapshot, os.fstat(stream.fileno()))
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

    if left.size != right.size:
        return False
    if left.identity == right.identity:
        return True
    left_buffer = bytearray(chunk_size)
    right_buffer = bytearray(chunk_size)
    try:
        with (
            open(native_io_path(left.path), "rb", buffering=0) as left_stream,
            open(native_io_path(right.path), "rb", buffering=0) as right_stream,
        ):
            _assert_unchanged(left, os.fstat(left_stream.fileno()))
            _assert_unchanged(right, os.fstat(right_stream.fileno()))
            while True:
                left_count = left_stream.readinto(left_buffer)
                right_count = right_stream.readinto(right_buffer)
                if left_count != right_count:
                    return False
                if left_count == 0:
                    break
                if memoryview(left_buffer)[:left_count] != memoryview(right_buffer)[:right_count]:
                    return False
            _assert_unchanged(left, os.fstat(left_stream.fileno()))
            _assert_unchanged(right, os.fstat(right_stream.fileno()))
            return True
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


# endregion [02]
