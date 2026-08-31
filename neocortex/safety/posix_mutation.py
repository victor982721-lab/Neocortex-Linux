"""Fail-closed Linux file moves using ``renameat2(RENAME_NOREPLACE)``.

The operation is deliberately narrow: it moves one regular file on one POSIX
filesystem, never replaces an existing destination, and verifies the observed
identity and metadata immediately before and after the syscall.  A platform
without ``renameat2`` is unsupported instead of silently falling back to a
weaker path operation.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from neocortex.deduplication.fingerprinting import stat_matches_snapshot
from neocortex.deduplication.io import absolute_display_path


RENAME_NOREPLACE = 1
POSIX_RENAME_RECEIPT_SCHEMA = 1


class ExpectedFileSnapshot(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def volume_id(self) -> int: ...

    @property
    def file_id(self) -> int: ...

    @property
    def size(self) -> int: ...

    @property
    def mtime_ns(self) -> int: ...

    @property
    def birthtime_ns(self) -> int: ...


class IdentityBoundMutationError(RuntimeError):
    """A POSIX move could not satisfy its checked no-replace contract."""


class UnsupportedIdentityBoundMutation(IdentityBoundMutationError):
    """The Linux host does not expose the required syscall or filesystem."""


class MutationEffectUncertainError(IdentityBoundMutationError):
    """The syscall may have succeeded but its postcondition was not confirmed."""

    def __init__(self, source: Path, destination: Path, cause: BaseException):
        super().__init__(
            "POSIX rename may have succeeded but confirmation failed: "
            f"{type(cause).__name__}: {cause}"
        )
        self.source = source
        self.destination = destination
        self.cause = cause


@dataclass(frozen=True, slots=True)
class PosixRenameReceipt:
    """Receipt for one verified ``renameat2`` operation."""

    source_path: str
    destination_path: str
    volume_id: int
    file_id: int
    file_system: str = "POSIX"
    link_count: int = 1
    backend: str = "renameat2"
    guarantee: str = "verified_no_replace"
    schema_version: int = POSIX_RENAME_RECEIPT_SCHEMA


# The action layer historically used this name.  It is now a neutral receipt,
# not a Windows contract.
IdentityBoundRenameReceipt = PosixRenameReceipt


def _renameat2_function() -> Any:
    if os.name != "posix" or not sys_platform_linux():
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except (AttributeError, OSError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


def sys_platform_linux() -> bool:
    return os.uname().sysname.casefold() == "linux"


def _open_nofollow(path: Path, *, directory: bool = False) -> int:
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY | os.O_RDONLY
    else:
        flags |= getattr(os, "O_PATH", os.O_RDONLY)
    try:
        return os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise UnsupportedIdentityBoundMutation(
                "POSIX no-follow handles are unavailable on this host"
            ) from exc
        raise


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return int(left.st_dev) == int(right.st_dev) and int(left.st_ino) == int(right.st_ino)


def _require_expected(source: Path, expected: ExpectedFileSnapshot, metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsupportedIdentityBoundMutation("source is not a regular file")
    if int(metadata.st_nlink) != 1:
        raise UnsupportedIdentityBoundMutation(
            "POSIX no-replace move abstains for files with multiple hard links"
        )
    if not stat_matches_snapshot(expected, metadata):
        raise IdentityBoundMutationError("source identity or metadata changed")


def _path_stat(path: Path, *, role: str, allow_missing: bool = False) -> os.stat_result | None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise IdentityBoundMutationError(f"{role} is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise IdentityBoundMutationError(f"{role} is a symbolic link")
    return metadata


def _require_parent_identity(path: Path, retained: os.stat_result, *, role: str) -> None:
    current = _path_stat(path, role=role)
    if current is None or not stat.S_ISDIR(current.st_mode) or not _same_identity(current, retained):
        raise IdentityBoundMutationError(f"{role} identity changed")


def _invoke_renameat2(
    function: Any,
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    destination: Path,
) -> None:
    result = int(
        function(
            source_parent_fd,
            os.fsencode(source_name),
            destination_parent_fd,
            os.fsencode(destination_name),
            RENAME_NOREPLACE,
        )
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), os.fspath(destination))
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
        raise UnsupportedIdentityBoundMutation("renameat2(RENAME_NOREPLACE) is unavailable")
    raise OSError(error_number, os.strerror(error_number), os.fspath(destination))


def rename_no_replace_by_identity(
    source: Path,
    destination: Path,
    expected: ExpectedFileSnapshot,
    *,
    before_native_call: Callable[[], None],
    cancellation_checkpoint: Callable[[], None] | None = None,
    _before_native_call: Callable[[], None] | None = None,
    _after_native_call: Callable[[], None] | None = None,
) -> PosixRenameReceipt:
    """Move one expected file without replacing a destination.

    The caller must persist its mutation frontier in ``before_native_call``.
    The source and both parent directories are opened without following links,
    then revalidated immediately before the native call.  The contract is
    ``verified_no_replace`` rather than an impossible claim of kernel-level
    path identity binding.
    """

    if os.name != "posix" or not sys_platform_linux():
        raise UnsupportedIdentityBoundMutation("POSIX mutation requires Linux")
    function = _renameat2_function()
    if function is None:
        raise UnsupportedIdentityBoundMutation("renameat2(RENAME_NOREPLACE) is unavailable")
    source = Path(absolute_display_path(source))
    destination = Path(absolute_display_path(destination))
    if source == destination:
        raise IdentityBoundMutationError("source and destination must differ")
    if destination.name in {"", ".", ".."} or "/" in destination.name:
        raise IdentityBoundMutationError("destination must name one file entry")

    with ExitStack() as stack:
        source_parent_fd = stack.enter_context(_fd_context(_open_nofollow(source.parent, directory=True)))
        destination_parent_fd = stack.enter_context(
            _fd_context(_open_nofollow(destination.parent, directory=True))
        )
        source_fd = stack.enter_context(_fd_context(_open_nofollow(source)))
        source_metadata = os.fstat(source_fd)
        source_parent_metadata = os.fstat(source_parent_fd)
        destination_parent_metadata = os.fstat(destination_parent_fd)
        _require_expected(source, expected, source_metadata)
        if int(source_metadata.st_dev) != int(destination_parent_metadata.st_dev):
            raise UnsupportedIdentityBoundMutation("cross-filesystem moves are unsupported")
        source_parent_path = _path_stat(source.parent, role="source parent")
        if source_parent_path is None or not _same_identity(source_parent_metadata, source_parent_path):
            raise IdentityBoundMutationError("source parent identity changed")
        _require_parent_identity(destination.parent, destination_parent_metadata, role="destination parent")
        if _path_stat(destination, role="destination", allow_missing=True) is not None:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), os.fspath(destination))
        if cancellation_checkpoint is not None:
            cancellation_checkpoint()
        if _before_native_call is not None:
            _before_native_call()
        before_native_call()

        current_source = _path_stat(source, role="source")
        if current_source is None:
            raise IdentityBoundMutationError("source disappeared before mutation")
        _require_expected(source, expected, current_source)
        if not _same_identity(current_source, source_metadata):
            raise IdentityBoundMutationError("source path no longer names the retained identity")
        _require_parent_identity(source.parent, source_parent_metadata, role="source parent")
        _require_parent_identity(destination.parent, destination_parent_metadata, role="destination parent")
        if _path_stat(destination, role="destination", allow_missing=True) is not None:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), os.fspath(destination))

        try:
            _invoke_renameat2(
                function,
                source_parent_fd,
                source.name,
                destination_parent_fd,
                destination.name,
                destination,
            )
        except (OSError, UnsupportedIdentityBoundMutation):
            raise
        except BaseException as exc:
            raise MutationEffectUncertainError(source, destination, exc) from exc
        try:
            if _after_native_call is not None:
                _after_native_call()
            if os.path.lexists(source):
                raise IdentityBoundMutationError("source name still exists after native success")
            destination_metadata = _path_stat(destination, role="destination")
            if destination_metadata is None or not _same_identity(destination_metadata, source_metadata):
                raise IdentityBoundMutationError("destination identity does not match the source")
            if not stat_matches_snapshot(expected, destination_metadata):
                raise IdentityBoundMutationError("destination metadata does not match the source")
            destination_parent_path = _path_stat(destination.parent, role="destination parent")
            if destination_parent_path is None or not _same_identity(
                destination_parent_metadata,
                destination_parent_path,
            ):
                raise IdentityBoundMutationError("destination parent changed after native success")
        except BaseException as exc:
            raise MutationEffectUncertainError(source, destination, exc) from exc
        return PosixRenameReceipt(
            source_path=os.fspath(source),
            destination_path=os.fspath(destination),
            volume_id=int(source_metadata.st_dev),
            file_id=int(source_metadata.st_ino),
            link_count=int(source_metadata.st_nlink),
        )


class _fd_context:
    def __init__(self, fd: int):
        self.fd = fd

    def __enter__(self) -> int:
        return self.fd

    def __exit__(self, *_args: object) -> None:
        os.close(self.fd)


__all__ = [
    "POSIX_RENAME_RECEIPT_SCHEMA",
    "RENAME_NOREPLACE",
    "IdentityBoundMutationError",
    "IdentityBoundRenameReceipt",
    "MutationEffectUncertainError",
    "PosixRenameReceipt",
    "UnsupportedIdentityBoundMutation",
    "rename_no_replace_by_identity",
]
