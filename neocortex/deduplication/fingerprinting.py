"""Stable file snapshots and exact or sampled content fingerprints.

The native XXH3 backend is preferred; :mod:`neocortex.foundation.hash_compat`
uses deterministic SHA-256-derived values when its optional wheel is absent.
"""

from __future__ import annotations

import os
import stat
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, cast

from neocortex.foundation.hash_compat import HAS_NATIVE_XXHASH, HASH_BACKEND, xxhash
from neocortex.platform.policy import stat_birthtime_ns

from .domain.errors import FileChangedError
from .domain.models import FileSnapshot
from .io import absolute_display_path, native_io_path

# Native installations retain the historical labels. A fallback gets its own
# cache namespace so XXH3 and SHA-256-derived fingerprints are never mixed.
_ALGORITHM_PREFIX = "xxh3_128" if HAS_NATIVE_XXHASH else "sha256_128_fallback"
FULL_ALGORITHM = f"{_ALGORITHM_PREFIX}_full_v1"
PARTIAL_ALGORITHM = f"{_ALGORITHM_PREFIX}_first_middle_last_v1_sample_262144"
FINGERPRINT_BACKEND = HASH_BACKEND
DEFAULT_IO_CHUNK_SIZE = 16 * 1024 * 1024
DEFAULT_SAMPLE_SIZE = 256 * 1024
_MIN_IO_CHUNK_SIZE = 64 * 1024


def _close_quietly(file_descriptor: int | None) -> None:
    if file_descriptor is None:
        return
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _open_regular_descriptor(snapshot: FileSnapshot) -> int:
    """Open the snapshot through descriptor-relative, no-following POSIX I/O.

    ``open(path, "rb")`` follows a final symlink and can block indefinitely
    when that path is replaced with a FIFO.  Keep the descriptor tied to the
    observed directory entries instead: every ancestor is opened with
    ``O_NOFOLLOW|O_DIRECTORY`` and the final component is opened with
    ``O_NOFOLLOW|O_NONBLOCK`` before its type and snapshot are checked.
    """

    path = Path(native_io_path(snapshot.path))
    if os.name != "posix":  # pragma: no cover - NeoCortex is Linux-only
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode):
                raise FileChangedError(f"file is not a regular file: {snapshot.path}")
            _assert_unchanged(snapshot, observed)
            return descriptor
        except BaseException:
            _close_quietly(descriptor)
            raise

    components = path.parts
    if not components or components[0] != os.sep or len(components) == 1:
        raise FileChangedError(f"file path is not a regular file: {snapshot.path}")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    common_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_flags = common_flags | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = os.open(os.sep, directory_flags)
        for component in components[1:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            _close_quietly(directory_descriptor)
            directory_descriptor = next_descriptor
        file_descriptor = os.open(
            components[-1],
            common_flags,
            dir_fd=directory_descriptor,
        )
        observed = os.fstat(file_descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise FileChangedError(f"file is not a regular file: {snapshot.path}")
        _assert_unchanged(snapshot, observed)
        _close_quietly(directory_descriptor)
        directory_descriptor = None
        return file_descriptor
    except BaseException:
        _close_quietly(file_descriptor)
        _close_quietly(directory_descriptor)
        raise


@contextmanager
def _open_regular_stream(snapshot: FileSnapshot) -> Iterator[BinaryIO]:
    """Yield an unbuffered stream bound to one validated regular-file inode."""

    descriptor = _open_regular_descriptor(snapshot)
    try:
        stream = os.fdopen(descriptor, "rb", buffering=0)
    except BaseException:
        _close_quietly(descriptor)
        raise
    try:
        yield stream
    finally:
        stream.close()


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


def fingerprint_change_version(snapshot: FileSnapshot) -> int:
    """Read a no-follow descriptor's change time without reading its content."""

    descriptor = _open_regular_descriptor(snapshot)
    try:
        return os.fstat(descriptor).st_ctime_ns
    finally:
        _close_quietly(descriptor)


def require_fingerprint_change_version(snapshot: FileSnapshot, ctime_ns: int) -> None:
    """Reject stale in-run content evidence, including restored-mtime rewrites."""

    if fingerprint_change_version(snapshot) != ctime_ns:
        raise FileChangedError(f"file content observation changed: {snapshot.path}")


def _validated_io_chunk_size(chunk_size: int) -> int:
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise TypeError("chunk_size must be an integer")
    if chunk_size < _MIN_IO_CHUNK_SIZE:
        raise ValueError("chunk_size must be at least 64 KiB")
    return chunk_size


def _adaptive_buffer_capacity(file_size: int, chunk_size: int) -> int:
    """Bound one reusable buffer to the observed file instead of the global limit."""

    return min(chunk_size, max(1, file_size))


def full_fingerprint(
    snapshot: FileSnapshot, *, chunk_size: int = DEFAULT_IO_CHUNK_SIZE,
    read_observer: Callable[[int], None] | None = None,
) -> bytes:
    """Return an XXH3-128 digest after streaming the entire file once."""

    chunk_size = _validated_io_chunk_size(chunk_size)
    hasher = xxhash.xxh3_128()
    try:
        with _open_regular_stream(snapshot) as stream:
            before_ctime_ns = os.fstat(stream.fileno()).st_ctime_ns
            buffer = bytearray(_adaptive_buffer_capacity(snapshot.size, chunk_size))
            view = memoryview(buffer)
            bytes_read = 0
            while count := cast(Any, stream).readinto(buffer):
                bytes_read += count
                if read_observer is not None:
                    read_observer(count)
                if bytes_read > snapshot.size:
                    raise FileChangedError(f"file grew while processing: {snapshot.path}")
                hasher.update(view[:count])
            if bytes_read != snapshot.size:
                raise FileChangedError(f"file was truncated while processing: {snapshot.path}")
            after_stat = os.fstat(stream.fileno())
            _assert_unchanged(snapshot, after_stat)
            if after_stat.st_ctime_ns != before_ctime_ns:
                raise FileChangedError(f"file changed while hashing: {snapshot.path}")
    except FileChangedError:
        raise
    except OSError as exc:
        raise FileChangedError(f"cannot read {snapshot.path}: {exc}") from exc
    return hasher.digest()


def partial_fingerprint(
    snapshot: FileSnapshot, *, sample_size: int = DEFAULT_SAMPLE_SIZE,
    read_observer: Callable[[int], None] | None = None,
) -> bytes:
    """Hash deterministic first/middle/last ranges, including their offsets."""

    if sample_size < 4096:
        raise ValueError("sample_size must be at least 4096 bytes")
    size = snapshot.size
    offsets = sorted({0, max(0, (size - sample_size) // 2), max(0, size - sample_size)})
    hasher = xxhash.xxh3_128()
    hasher.update(b"T_DEDUP_PARTIAL_V1\0")
    hasher.update(struct.pack("<QQ", size, sample_size))
    try:
        with _open_regular_stream(snapshot) as stream:
            before_ctime_ns = os.fstat(stream.fileno()).st_ctime_ns
            for offset in offsets:
                stream.seek(offset)
                expected = min(sample_size, size - offset)
                data = stream.read(expected)
                if read_observer is not None:
                    read_observer(len(data))
                if len(data) != expected:
                    raise FileChangedError(f"file was truncated while sampling: {snapshot.path}")
                hasher.update(struct.pack("<QQ", offset, len(data)))
                hasher.update(data)
            after_stat = os.fstat(stream.fileno())
            _assert_unchanged(snapshot, after_stat)
            if after_stat.st_ctime_ns != before_ctime_ns:
                raise FileChangedError(f"file changed while sampling: {snapshot.path}")
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
    read_observer: Callable[[int], None] | None = None,
) -> bool:
    """Perform the final byte comparison required before a destructive policy."""

    chunk_size = _validated_io_chunk_size(chunk_size)
    if left.size != right.size:
        return False
    try:
        with _open_regular_stream(left) as left_stream, _open_regular_stream(right) as right_stream:
            left_ctime_ns = os.fstat(left_stream.fileno()).st_ctime_ns
            right_ctime_ns = os.fstat(right_stream.fileno()).st_ctime_ns
            capacity = _adaptive_buffer_capacity(left.size, chunk_size)
            left_buffer = bytearray(capacity)
            right_buffer = bytearray(capacity)
            left_view = memoryview(left_buffer)
            right_view = memoryview(right_buffer)
            equal = True
            bytes_compared = 0
            while True:
                left_count = cast(Any, left_stream).readinto(left_buffer)
                right_count = cast(Any, right_stream).readinto(right_buffer)
                if read_observer is not None:
                    read_observer(left_count + right_count)
                if left_count != right_count:
                    equal = False
                    break
                if left_count == 0:
                    break
                bytes_compared += left_count
                if left_view[:left_count] != right_view[:right_count]:
                    equal = False
                    break
            if equal and bytes_compared != left.size:
                raise FileChangedError("file was truncated or grew during exact comparison")
            left_stat = os.fstat(left_stream.fileno())
            right_stat = os.fstat(right_stream.fileno())
            _assert_unchanged(left, left_stat)
            _assert_unchanged(right, right_stat)
            if left_stat.st_ctime_ns != left_ctime_ns or right_stat.st_ctime_ns != right_ctime_ns:
                raise FileChangedError("file changed during exact comparison")
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
    "FINGERPRINT_BACKEND",
    "FULL_ALGORITHM",
    "PARTIAL_ALGORITHM",
    "files_equal_exact",
    "fingerprint_change_version",
    "full_fingerprint",
    "partial_fingerprint",
    "require_fingerprint_change_version",
    "snapshot_path",
    "stat_matches_snapshot",
]
