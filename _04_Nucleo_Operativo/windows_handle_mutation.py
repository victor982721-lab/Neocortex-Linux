"""Identity-bound, no-replace file renames for supported Windows volumes.

The public operation deliberately abstains unless both the source object and the
destination directory can remain open across the native rename. This closes the
path substitution window left by ``Path.rename``/``MoveFileW`` without adding a
runtime dependency. Recycle Bin APIs remain path-bound and are not implemented
here.
"""

from __future__ import annotations

import ctypes
import errno
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast
from ctypes import wintypes

from neocortex.deduplication.io import native_io_path


# region [01] Public contract


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
    """A rename could not satisfy its identity-bound safety contract."""


class UnsupportedIdentityBoundMutation(IdentityBoundMutationError):
    """The current platform or volume cannot provide the required guarantee."""


class MutationEffectUncertainError(IdentityBoundMutationError):
    """The native effect may have succeeded but was not durably confirmed."""

    def __init__(self, source: Path, destination: Path, cause: BaseException):
        super().__init__(
            "identity-bound rename may have succeeded but confirmation failed: "
            f"{type(cause).__name__}: {cause}"
        )
        self.source = source
        self.destination = destination
        self.cause = cause


@dataclass(frozen=True, slots=True)
class IdentityBoundRenameReceipt:
    source_path: str
    destination_path: str
    volume_id: int
    file_id: int
    file_system: str
    link_count: int


def rename_no_replace_by_identity(
    source: Path,
    destination: Path,
    expected: ExpectedFileSnapshot,
    *,
    before_native_call: Callable[[], None],
    cancellation_checkpoint: Callable[[], None] | None = None,
    _before_native_call: Callable[[], None] | None = None,
    _after_native_call: Callable[[], None] | None = None,
) -> IdentityBoundRenameReceipt:
    """Rename the expected file by retained handles, never replacing a target.

    Only local NTFS files with one link and non-reparse source/destination-parent
    entries are admitted. Unsupported cases fail closed before the native call.
    The mandatory ``before_native_call`` lets the caller revalidate policy and
    durably record its mutation frontier before the syscall. The underscored
    callbacks exist only for deterministic boundary fault injection.
    """

    if os.name != "nt":
        raise UnsupportedIdentityBoundMutation(
            "identity-bound rename is currently implemented only for Windows"
        )
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    if source == destination:
        raise IdentityBoundMutationError("source and destination must differ")
    if destination.name in {"", ".", ".."}:
        raise IdentityBoundMutationError("destination must name one file entry")

    with (
        _open_source(source) as source_handle,
        _open_directory(destination.parent) as parent_handle,
    ):
        source_info = _handle_identity(source_handle.value)
        parent_info = _handle_identity(parent_handle.value)
        source_legacy = _legacy_handle_info(source_handle.value)
        parent_legacy = _legacy_handle_info(parent_handle.value)
        file_system = _volume_file_system(source_handle.value)
        parent_file_system = _volume_file_system(parent_handle.value)
        if file_system != "NTFS" or parent_file_system != "NTFS":
            raise UnsupportedIdentityBoundMutation(
                "identity-bound rename requires local NTFS source and destination"
            )
        if source_info.volume_id != parent_info.volume_id:
            raise UnsupportedIdentityBoundMutation(
                "identity-bound rename does not support cross-volume moves"
            )
        _require_safe_legacy_entries(source_legacy, parent_legacy)
        _validate_expected_snapshot(source, expected, source_info)
        _validate_retained_path_binding(
            destination.parent,
            parent_info,
            role="destination parent",
        )

        if cancellation_checkpoint is not None:
            cancellation_checkpoint()
        if _before_native_call is not None:
            _before_native_call()
        before_native_call()
        _validate_expected_snapshot(source, expected, source_info)
        _validate_retained_path_binding(
            destination.parent,
            parent_info,
            role="destination parent",
        )
        final_source_legacy = _legacy_handle_info(source_handle.value)
        final_parent_legacy = _legacy_handle_info(parent_handle.value)
        _require_safe_legacy_entries(final_source_legacy, final_parent_legacy)
        try:
            _nt_rename_relative_no_replace(
                source_handle.value,
                parent_handle.value,
                destination.name,
                destination,
            )
        except OSError:
            raise
        except BaseException as exc:
            raise MutationEffectUncertainError(source, destination, exc) from exc
        try:
            if _after_native_call is not None:
                _after_native_call()
            _validate_rename_postcondition(
                source,
                destination,
                source_handle.value,
                source_info,
            )
        except BaseException as exc:
            raise MutationEffectUncertainError(source, destination, exc) from exc
        return IdentityBoundRenameReceipt(
            source_path=str(source),
            destination_path=str(destination),
            volume_id=source_info.volume_id,
            file_id=source_info.file_id,
            file_system=file_system,
            link_count=final_source_legacy.link_count,
        )


# endregion [01]


# region [02] Native declarations and retained handles


DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_LIST_DIRECTORY = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
FILE_ID_INFO_CLASS = 18
FILE_RENAME_INFORMATION_CLASS = 10


class _FileId128(ctypes.Structure):
    _fields_ = (("identifier", ctypes.c_ubyte * 16),)


class _FileIdInfo(ctypes.Structure):
    _fields_ = (("volume_serial_number", ctypes.c_ulonglong), ("file_id", _FileId128))


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    )


class _IoStatusBlock(ctypes.Structure):
    _fields_ = (("status_or_pointer", ctypes.c_void_p), ("information", ctypes.c_size_t))


@dataclass(frozen=True, slots=True)
class _HandleIdentity:
    volume_id: int
    file_id: int


@dataclass(frozen=True, slots=True)
class _LegacyHandleInfo:
    attributes: int
    link_count: int


if os.name == "nt":
    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll: Any = ctypes.WinDLL("ntdll")
    _kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetVolumeInformationByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    _kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    _ntdll.NtSetInformationFile.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    _ntdll.NtSetInformationFile.restype = ctypes.c_long
    _ntdll.RtlNtStatusToDosError.argtypes = (ctypes.c_long,)
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
else:  # pragma: no cover - imports remain safe for tooling on other platforms
    _kernel32 = None
    _ntdll = None


class _OwnedHandle(AbstractContextManager["_OwnedHandle"]):
    def __init__(self, value: int):
        self.value = value

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        assert _kernel32 is not None
        if self.value != INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(self.value)
            self.value = int(INVALID_HANDLE_VALUE)


def _create_handle(
    path: Path,
    access: int,
    flags: int,
    *,
    share_mode: int,
) -> _OwnedHandle:
    assert _kernel32 is not None
    value = _kernel32.CreateFileW(
        native_io_path(path),
        access,
        share_mode,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if value == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return _OwnedHandle(int(value))


def _open_source(path: Path) -> _OwnedHandle:
    return _create_handle(
        path,
        DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_FLAG_OPEN_REPARSE_POINT,
        share_mode=FILE_SHARE_READ,
    )


def _open_directory(path: Path) -> _OwnedHandle:
    return _create_handle(
        path,
        FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
    )


# endregion [02]


# region [03] Validation and native rename


def _handle_identity(handle: int) -> _HandleIdentity:
    assert _kernel32 is not None
    information = _FileIdInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        FILE_ID_INFO_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return _HandleIdentity(
        volume_id=int(information.volume_serial_number),
        file_id=int.from_bytes(bytes(information.file_id.identifier), "little"),
    )


def _legacy_handle_info(handle: int) -> _LegacyHandleInfo:
    assert _kernel32 is not None
    information = _ByHandleFileInformation()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _LegacyHandleInfo(
        attributes=int(information.file_attributes),
        link_count=int(information.number_of_links),
    )


def _require_safe_legacy_entries(
    source: _LegacyHandleInfo,
    destination_parent: _LegacyHandleInfo,
) -> None:
    if source.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsupportedIdentityBoundMutation("source is a reparse point")
    if source.attributes & FILE_ATTRIBUTE_DIRECTORY:
        raise UnsupportedIdentityBoundMutation("directory renames are not supported")
    if destination_parent.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsupportedIdentityBoundMutation("destination parent is a reparse point")
    if not destination_parent.attributes & FILE_ATTRIBUTE_DIRECTORY:
        raise IdentityBoundMutationError("destination parent is not a directory")
    if source.link_count != 1:
        raise UnsupportedIdentityBoundMutation(
            "identity-bound rename abstains for files with multiple hard links"
        )


def _volume_file_system(handle: int) -> str:
    assert _kernel32 is not None
    file_system = ctypes.create_unicode_buffer(64)
    if not _kernel32.GetVolumeInformationByHandleW(
        handle,
        None,
        0,
        None,
        None,
        None,
        file_system,
        len(file_system),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return file_system.value.upper()


def _validate_retained_path_binding(
    path: Path,
    handle_identity: _HandleIdentity,
    *,
    role: str,
) -> os.stat_result:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise IdentityBoundMutationError(
            f"{role} path became unavailable after its handle was retained"
        ) from exc
    current_identity = (int(current.st_dev), int(current.st_ino))
    retained_identity = (handle_identity.volume_id, handle_identity.file_id)
    if current_identity != retained_identity:
        raise IdentityBoundMutationError(f"{role} path does not resolve to the retained handle")
    return current


def _validate_expected_snapshot(
    source: Path,
    expected: ExpectedFileSnapshot,
    handle_identity: _HandleIdentity,
) -> None:
    current = _validate_retained_path_binding(
        source,
        handle_identity,
        role="source",
    )
    expected_identity = (int(expected.volume_id), int(expected.file_id))
    retained_identity = (handle_identity.volume_id, handle_identity.file_id)
    if expected_identity != retained_identity:
        raise IdentityBoundMutationError(
            "source identity changed after the mutation was authorized"
        )
    birthtime_ns = int(getattr(current, "st_birthtime_ns", current.st_ctime_ns))
    if (
        int(current.st_size) != int(expected.size)
        or int(current.st_mtime_ns) != int(expected.mtime_ns)
        or birthtime_ns != int(expected.birthtime_ns)
    ):
        raise IdentityBoundMutationError(
            "source metadata changed after the mutation was authorized"
        )


def _nt_rename_relative_no_replace(
    source_handle: int,
    parent_handle: int,
    target_name: str,
    destination: Path,
) -> None:
    assert _ntdll is not None
    encoded = target_name.encode("utf-16-le")
    # FILE_RENAME_INFORMATION has a 20-byte field prefix and 4 bytes of
    # structure tail padding on 64-bit Windows. FileNameLength excludes padding.
    buffer = ctypes.create_string_buffer(24 + len(encoded))
    ctypes.c_ubyte.from_buffer(buffer, 0).value = 0
    ctypes.c_void_p.from_buffer(buffer, 8).value = parent_handle
    ctypes.c_ulong.from_buffer(buffer, 16).value = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + 20, encoded, len(encoded))
    io_status = _IoStatusBlock()
    status = int(
        _ntdll.NtSetInformationFile(
            source_handle,
            ctypes.byref(io_status),
            buffer,
            len(buffer),
            FILE_RENAME_INFORMATION_CLASS,
        )
    )
    if status >= 0:
        return
    error = int(_ntdll.RtlNtStatusToDosError(status))
    if error in {80, 183}:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination)
    raise ctypes.WinError(error)


def _validate_rename_postcondition(
    source: Path,
    destination: Path,
    source_handle: int,
    expected_identity: _HandleIdentity,
) -> None:
    if os.path.lexists(source):
        raise IdentityBoundMutationError("source name still exists after native success")
    if not os.path.lexists(destination):
        raise IdentityBoundMutationError("destination name is absent after native success")
    destination_stat = os.stat(destination, follow_symlinks=False)
    destination_identity = (int(destination_stat.st_dev), int(destination_stat.st_ino))
    retained = _handle_identity(source_handle)
    expected = (expected_identity.volume_id, expected_identity.file_id)
    if (
        destination_identity != expected
        or (
            retained.volume_id,
            retained.file_id,
        )
        != expected
    ):
        raise IdentityBoundMutationError(
            "destination identity does not match the retained source handle"
        )


# endregion [03]


__all__ = [
    "IdentityBoundMutationError",
    "IdentityBoundRenameReceipt",
    "MutationEffectUncertainError",
    "UnsupportedIdentityBoundMutation",
    "rename_no_replace_by_identity",
]
