"""Process-lifetime exclusion for one foreground watcher identity.

The operating-system byte lock is authoritative and is released when the
process closes the handle, including abnormal process termination.  JSON
metadata is diagnostic only: it is replaced only after the OS lock has been
acquired, so stale metadata can never grant ownership.
"""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

import xxhash

from neocortex import __version__
_METADATA_SCHEMA = "neocortex-watcher-life-lease-v1"
_MAX_METADATA_BYTES = 64 * 1024
_MAX_ARG_COUNT = 64
_MAX_ARG_CHARS = 512

OwnerStatus = Literal["live", "not-live", "unknown"]


# region [01] Public conflict and identity contracts


class WatcherLifeLeaseConflict(RuntimeError):
    """Another process owns the same root/state watcher identity."""

    def __init__(
        self,
        path: Path,
        owner: dict[str, object] | None,
        owner_status: OwnerStatus,
    ) -> None:
        self.path = path
        self.owner = owner
        self.owner_status = owner_status
        pid = owner.get("pid") if owner is not None else None
        detail = f"pid={pid}" if isinstance(pid, int) else "owner metadata unavailable"
        super().__init__(
            "another foreground watcher owns this root/state identity "
            f"({detail}, owner_status={owner_status}, lock={path})"
        )


@dataclass(frozen=True, slots=True)
class WatcherLeaseIdentity:
    """Canonical identity used to derive a bounded, non-cryptographic key."""

    root: str
    state_directory: str
    xxh3_128: str


def _canonical_path(path: str | Path) -> str:
    expanded = Path(path).expanduser()
    absolute = os.path.abspath(os.fspath(expanded))
    return os.path.normcase(os.path.realpath(absolute))


def watcher_lease_identity(
    root: str | Path,
    state_directory: str | Path,
) -> WatcherLeaseIdentity:
    """Return the canonical root/state identity and its explicit XXH3 key."""

    canonical_root = _canonical_path(root)
    canonical_state = _canonical_path(state_directory)
    encoded = f"{canonical_root}\0{canonical_state}".encode("utf-8")
    return WatcherLeaseIdentity(
        root=canonical_root,
        state_directory=canonical_state,
        xxh3_128=xxhash.xxh3_128_hexdigest(encoded),
    )


# endregion [01]


# region [02] Bounded owner metadata


def _process_creation_observation(pid: int) -> tuple[int | None, OwnerStatus]:
    if os.name != "nt":  # pragma: no cover - NeoCortex is Windows-first
        return None, "unknown"

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    close_handle = False
    if pid == os.getpid():
        handle = kernel32.GetCurrentProcess()
    else:
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        close_handle = bool(handle)
    if not handle:
        # ERROR_INVALID_PARAMETER is returned for a PID that does not exist.
        # Access-denied and other failures must remain diagnostically unknown.
        if ctypes.get_last_error() == 87:
            return None, "not-live"
        return None, "unknown"

    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None, "unknown"
    finally:
        if close_handle:
            kernel32.CloseHandle(handle)

    windows_ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    windows_to_unix_ticks = 116_444_736_000_000_000
    return (windows_ticks - windows_to_unix_ticks) * 100, "live"


def _process_creation_time_ns(pid: int) -> int | None:
    return _process_creation_observation(pid)[0]


def _bounded_argv(argv: list[str]) -> list[str]:
    return [str(value)[:_MAX_ARG_CHARS] for value in argv[:_MAX_ARG_COUNT]]


def _metadata(identity: WatcherLeaseIdentity) -> dict[str, object]:
    return {
        "schema": _METADATA_SCHEMA,
        "pid": os.getpid(),
        "process_creation_time_ns": _process_creation_time_ns(os.getpid()),
        "host": socket.gethostname()[:255],
        "version": __version__,
        "argv": _bounded_argv(sys.argv),
        "root": identity.root,
        "state_directory": identity.state_directory,
        "started_ns": time.time_ns(),
    }


def _read_metadata(stream: BinaryIO) -> dict[str, object] | None:
    stream.seek(1)
    raw = stream.read(_MAX_METADATA_BYTES + 1)
    if not raw or len(raw) > _MAX_METADATA_BYTES:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict) or decoded.get("schema") != _METADATA_SCHEMA:
        return None
    return {str(key): value for key, value in decoded.items()}


def _owner_status(owner: dict[str, object] | None) -> OwnerStatus:
    if owner is None:
        return "unknown"
    pid = owner.get("pid")
    expected_creation = owner.get("process_creation_time_ns")
    if not isinstance(pid, int) or not isinstance(expected_creation, int):
        return "unknown"
    actual_creation, observation = _process_creation_observation(pid)
    if observation != "live" or actual_creation is None:
        return observation
    return "live" if actual_creation == expected_creation else "not-live"


# endregion [02]


# region [03] Operating-system lock primitives


def _lock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - compatibility for development hosts
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


# endregion [03]


# region [04] Process-lifetime lease


class WatcherLifeLease:
    """Hold one root/state watcher lease until the context exits."""

    def __init__(self, root: str | Path, state_directory: str | Path) -> None:
        self.identity = watcher_lease_identity(root, state_directory)
        state = Path(self.identity.state_directory)
        self.path = state / (
            f"watcher-life-xxh3-128-{self.identity.xxh3_128}.lock"
        )
        self.owner: dict[str, object] | None = None
        self.previous_metadata: dict[str, object] | None = None
        self._stream: BinaryIO | None = None

    @property
    def replaced_stale_metadata(self) -> bool:
        return self.previous_metadata is not None

    def __enter__(self) -> "WatcherLifeLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b", buffering=0)
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
        stream.seek(0)
        try:
            _lock_stream(stream)
        except OSError as exc:
            owner = _read_metadata(stream)
            status = _owner_status(owner)
            stream.close()
            raise WatcherLifeLeaseConflict(self.path, owner, status) from exc

        try:
            previous = _read_metadata(stream)
            owner = _metadata(self.identity)
            encoded = json.dumps(
                owner,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(encoded) > _MAX_METADATA_BYTES:
                raise RuntimeError("watcher life-lease metadata exceeds its bound")
            stream.seek(0)
            stream.truncate()
            stream.write(b"\0" + encoded)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            try:
                _unlock_stream(stream)
            finally:
                stream.close()
            raise

        self.previous_metadata = previous
        self.owner = owner
        self._stream = stream
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        unlock_error: OSError | None = None
        try:
            _unlock_stream(stream)
        except OSError as caught:
            unlock_error = caught
        finally:
            stream.close()
        if unlock_error is not None and exc_type is None:
            raise unlock_error


# endregion [04]


__all__ = [
    "OwnerStatus",
    "WatcherLeaseIdentity",
    "WatcherLifeLease",
    "WatcherLifeLeaseConflict",
    "watcher_lease_identity",
]
