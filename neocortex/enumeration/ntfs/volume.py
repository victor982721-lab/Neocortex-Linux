"""Minimal, dependency-free Win32 bindings used by the public API."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/ntfs/volume.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import ctypes
import os
import re
import struct
import sys
from ctypes import wintypes
from typing import Any

from ..errors import InvalidVolumeError, UnsupportedPlatformError, VolumeAccessError
from ..models import UsnJournalInfo
# endregion [01]

# region [02] Implementación


FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_QUERY_USN_JOURNAL = 0x000900F4

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
ERROR_HANDLE_EOF = 38
ERROR_NO_MORE_FILES = 18

_DRIVE_RE = re.compile(r"^(?:\\\\\.\\)?([A-Za-z]):?$")

# ``ctypes`` exposes these symbols only on Windows.  Define the module seams on
# every platform so Linux type checking can prove that the guarded backend is
# importable without pretending that Win32 calls are available.
_CreateFileW: Any = None
_CloseHandle: Any = None
_DeviceIoControl: Any = None
_GetVolumeInformationW: Any = None
_OpenFileById: Any = None
_GetFinalPathNameByHandleW: Any = None
_INVALID_HANDLE_VALUE: int | None = None
_GET_LAST_ERROR: Any = getattr(ctypes, "get_last_error", lambda: 0)
_FORMAT_ERROR: Any = getattr(ctypes, "FormatError", lambda code: f"Windows error {code}")


class MftEnumDataV0(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn", ctypes.c_longlong),
        ("HighUsn", ctypes.c_longlong),
    ]


class ReadUsnJournalDataV1(ctypes.Structure):
    _fields_ = [
        ("StartUsn", ctypes.c_longlong),
        ("ReasonMask", wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout", ctypes.c_ulonglong),
        ("BytesToWaitFor", ctypes.c_ulonglong),
        ("UsnJournalID", ctypes.c_ulonglong),
        ("MinMajorVersion", wintypes.WORD),
        ("MaxMajorVersion", wintypes.WORD),
    ]


class FileIdDescriptorUnion(ctypes.Union):
    _fields_ = [
        ("FileId", ctypes.c_longlong),
        ("ObjectId", ctypes.c_ubyte * 16),
        ("ExtendedFileId", ctypes.c_ubyte * 16),
    ]


class FileIdDescriptor(ctypes.Structure):
    _anonymous_ = ("Identifier",)
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Type", ctypes.c_int),
        ("Identifier", FileIdDescriptorUnion),
    ]


def normalize_volume(volume: str | os.PathLike[str]) -> tuple[str, str, str]:
    """Return ``(display, device_path, root_path)`` for a drive designator."""

    raw = os.fspath(volume).strip()
    match = _DRIVE_RE.fullmatch(raw)
    if not match:
        raise InvalidVolumeError(f"expected a local drive designator such as 'C:'; got {raw!r}")
    drive = match.group(1).upper()
    return f"{drive}:", rf"\\.\{drive}:", f"{drive}:\\"


if sys.platform == "win32":
    _kernel32 = ctypes.__dict__["WinDLL"]("kernel32", use_last_error=True)

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _DeviceIoControl = _kernel32.DeviceIoControl
    _DeviceIoControl.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _DeviceIoControl.restype = wintypes.BOOL

    _GetVolumeInformationW = _kernel32.GetVolumeInformationW
    _GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    _GetVolumeInformationW.restype = wintypes.BOOL

    _OpenFileById = _kernel32.OpenFileById
    _OpenFileById.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileIdDescriptor),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _OpenFileById.restype = wintypes.HANDLE

    _GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _GetFinalPathNameByHandleW.restype = wintypes.DWORD

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _require_windows() -> None:
    if sys.platform != "win32":
        raise UnsupportedPlatformError("neocortex.enumeration requires Windows")


def _error(operation: str, volume: str) -> VolumeAccessError:
    code = int(_GET_LAST_ERROR())
    return VolumeAccessError(operation, volume, code, str(_FORMAT_ERROR(code)).strip())


def verify_ntfs(root_path: str, display_volume: str) -> None:
    _require_windows()
    fs_name = ctypes.create_unicode_buffer(64)
    ok = _GetVolumeInformationW(root_path, None, 0, None, None, None, fs_name, len(fs_name))
    if not ok:
        raise _error("GetVolumeInformationW", display_volume)
    if fs_name.value.upper() != "NTFS":
        raise InvalidVolumeError(
            f"{display_volume!r} uses {fs_name.value or 'an unknown filesystem'}, not NTFS"
        )


class VolumeHandle:
    """Owned Win32 volume handle."""

    def __init__(self, volume: str | os.PathLike[str]):
        self.display, self.device_path, self.root_path = normalize_volume(volume)
        self._handle: int | None = None

    def open(self) -> "VolumeHandle":
        _require_windows()
        if self._handle is not None:
            return self
        verify_ntfs(self.root_path, self.display)
        handle = _CreateFileW(
            self.device_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise _error("CreateFileW", self.display)
        self._handle = handle
        return self

    def close(self) -> None:
        if self._handle is not None:
            _CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "VolumeHandle":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _ioctl(self, code: int, input_value, output_size: int) -> bytes | None:
        self.open()
        output = ctypes.create_string_buffer(output_size)
        returned = wintypes.DWORD()
        input_pointer = ctypes.byref(input_value) if input_value is not None else None
        input_size = ctypes.sizeof(input_value) if input_value is not None else 0
        ok = _DeviceIoControl(
            self._handle,
            code,
            input_pointer,
            input_size,
            output,
            output_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            error_code = int(_GET_LAST_ERROR())
            if code == FSCTL_ENUM_USN_DATA and error_code in (
                ERROR_HANDLE_EOF,
                ERROR_NO_MORE_FILES,
            ):
                return None
            raise _error(f"DeviceIoControl(0x{code:08X})", self.display)
        return output.raw[: returned.value]

    def query_journal(self) -> UsnJournalInfo:
        raw = self._ioctl(FSCTL_QUERY_USN_JOURNAL, None, 80)
        if raw is None or len(raw) < 56:
            raise VolumeAccessError("FSCTL_QUERY_USN_JOURNAL", self.display, 0, "short response")
        journal_id, first, next_, lowest, maximum, size, delta = struct.unpack_from("<QqqqqQQ", raw)
        return UsnJournalInfo(
            journal_id=journal_id,
            first_usn=first,
            next_usn=next_,
            lowest_valid_usn=lowest,
            max_usn=maximum,
            maximum_size=size,
            allocation_delta=delta,
        )

    def enum_mft(
        self, start_frn: int, low_usn: int, high_usn: int, buffer_size: int
    ) -> bytes | None:
        request = MftEnumDataV0(start_frn, low_usn, high_usn)
        return self._ioctl(FSCTL_ENUM_USN_DATA, request, buffer_size)

    def read_journal(
        self,
        *,
        start_usn: int,
        journal_id: int,
        reason_mask: int,
        timeout_seconds: int,
        bytes_to_wait_for: int,
        buffer_size: int,
    ) -> bytes:
        request = ReadUsnJournalDataV1(
            start_usn,
            reason_mask,
            0,
            timeout_seconds,
            bytes_to_wait_for,
            journal_id,
            2,
            3,
        )
        raw = self._ioctl(FSCTL_READ_USN_JOURNAL, request, buffer_size)
        # READ_USN_JOURNAL does not use EOF as its normal completion signal.
        assert raw is not None
        return raw

    def resolve_path(self, file_reference_number: int) -> str:
        """Resolve a currently live 64/128-bit file ID to a DOS path."""

        self.open()
        if file_reference_number < 0 or file_reference_number.bit_length() > 128:
            raise ValueError("file_reference_number must be unsigned and at most 128 bits")
        descriptor = FileIdDescriptor()
        descriptor.dwSize = ctypes.sizeof(FileIdDescriptor)
        if file_reference_number.bit_length() <= 64:
            descriptor.Type = 0  # FileIdType
            descriptor.FileId = ctypes.c_longlong(file_reference_number).value
        else:
            descriptor.Type = 2  # ExtendedFileIdType
            raw_id = file_reference_number.to_bytes(16, "little")
            descriptor.ExtendedFileId[:] = raw_id
        file_handle = _OpenFileById(
            self._handle,
            ctypes.byref(descriptor),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            FILE_FLAG_BACKUP_SEMANTICS,
        )
        if file_handle == _INVALID_HANDLE_VALUE:
            raise _error("OpenFileById", self.display)
        try:
            capacity = 32768
            buffer = ctypes.create_unicode_buffer(capacity)
            length = _GetFinalPathNameByHandleW(file_handle, buffer, capacity, 0)
            if length == 0:
                raise _error("GetFinalPathNameByHandleW", self.display)
            if length >= capacity:
                buffer = ctypes.create_unicode_buffer(length + 1)
                length = _GetFinalPathNameByHandleW(file_handle, buffer, len(buffer), 0)
                if length == 0 or length >= len(buffer):
                    raise _error("GetFinalPathNameByHandleW", self.display)
            path = buffer.value
            if path.startswith("\\\\?\\"):
                path = path[4:]
            return path
        finally:
            _CloseHandle(file_handle)


# endregion [02]
