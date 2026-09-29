"""Small Linux read-lease capability for bounded artifact proofs.

The lease is an in-process capability, not durable evidence.  It is held on
the same ``O_RDONLY|O_NOFOLLOW`` open file description used by a bounded proof
and survives an identity-preserving rename.  No process-global signal handler
or signal mask is installed: Linux is configured to target SIGURG at the
current native thread, and the helper accepts only the default/ignored
SIGURG disposition.  A lease break is observed through ``F_GETLEASE`` and
fails closed before a caller may cross a destructive frontier.
"""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from neocortex.platform.policy import stat_birthtime_ns


class ArtifactReadLeaseError(RuntimeError):
    """The kernel cannot provide or preserve the bounded read lease."""


class ArtifactReadLeaseChanged(ArtifactReadLeaseError):
    """A writer/truncator requested a lease break or the object drifted."""


def _fcntl_constant(name: str) -> int:
    value = getattr(fcntl, name, None)
    if type(value) is not int:
        raise ArtifactReadLeaseError(f"Linux fcntl constant {name} is unavailable")
    return value


def _validate_regular(metadata: os.stat_result) -> tuple[int, int, int]:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ArtifactReadLeaseError("artifact read lease requires a unique regular file")
    birthtime_ns = int(stat_birthtime_ns(metadata))
    return int(metadata.st_dev), int(metadata.st_ino), birthtime_ns


@dataclass(slots=True)
class ArtifactReadLease:
    """Live, typed capability held on one leased regular-file description."""

    _fd: int
    _identity: tuple[int, int, int]
    _closed: bool = False
    _broken: bool = False

    @property
    def fd(self) -> int:
        if self._closed:
            raise ArtifactReadLeaseError("artifact read lease is closed")
        return self._fd

    @property
    def identity(self) -> tuple[int, int, int]:
        return self._identity

    @property
    def closed(self) -> bool:
        return self._closed

    def check(self) -> os.stat_result:
        """Return the current descriptor metadata only while the lease is live."""

        if self._closed:
            raise ArtifactReadLeaseError("artifact read lease is closed")
        if self._broken:
            raise ArtifactReadLeaseChanged("artifact read lease break was observed")
        try:
            lease_type = fcntl.fcntl(self._fd, _fcntl_constant("F_GETLEASE"))
        except OSError as exc:
            self._broken = True
            raise ArtifactReadLeaseChanged("artifact read lease cannot be queried") from exc
        if lease_type != _fcntl_constant("F_RDLCK"):
            self._broken = True
            raise ArtifactReadLeaseChanged("artifact read lease was broken")
        try:
            metadata = os.fstat(self._fd)
        except OSError as exc:
            self._broken = True
            raise ArtifactReadLeaseChanged("artifact read lease descriptor is unavailable") from exc
        try:
            identity = _validate_regular(metadata)
        except ArtifactReadLeaseError:
            self._broken = True
            raise
        if identity != self._identity:
            self._broken = True
            raise ArtifactReadLeaseChanged("artifact read lease identity changed")
        return metadata

    def close(self) -> None:
        """Release the lease and descriptor; cleanup never raises to callers."""

        if self._closed:
            return
        self._closed = True
        try:
            fcntl.fcntl(self._fd, _fcntl_constant("F_SETLEASE"), _fcntl_constant("F_UNLCK"))
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self) -> "ArtifactReadLease":
        self.check()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter finalization timing
        try:
            self.close()
        except BaseException:
            pass


def acquire_artifact_read_lease(path: str | os.PathLike[str]) -> ArtifactReadLease:
    """Acquire a fail-closed Linux read lease without changing signal state."""

    if os.name != "posix":
        raise ArtifactReadLeaseError("artifact read leases require Linux POSIX")
    selected = Path(os.fspath(path))
    if not selected.is_absolute() or "\x00" in os.fspath(selected):
        raise ArtifactReadLeaseError("artifact read lease path must be absolute")
    disposition = signal.getsignal(signal.SIGURG)
    if disposition not in (signal.SIG_DFL, signal.SIG_IGN):
        raise ArtifactReadLeaseError("SIGURG has a process handler; lease capability is unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(selected, flags)
        metadata = os.fstat(descriptor)
        identity = _validate_regular(metadata)
        setlease = _fcntl_constant("F_SETLEASE")
        getlease = _fcntl_constant("F_GETLEASE")
        rdlock = _fcntl_constant("F_RDLCK")
        setsig = _fcntl_constant("F_SETSIG")
        setown_ex = _fcntl_constant("F_SETOWN_EX")
        owner_tid = _fcntl_constant("F_OWNER_TID")
        # Target the notification at this native thread; do not install or
        # modify a handler/mask shared by unrelated threads.
        fcntl.fcntl(
            descriptor,
            setown_ex,
            struct.pack("ii", owner_tid, threading.get_native_id()),
        )
        fcntl.fcntl(descriptor, setsig, signal.SIGURG)
        fcntl.fcntl(descriptor, setlease, rdlock)
        if fcntl.fcntl(descriptor, getlease) != rdlock:
            raise ArtifactReadLeaseChanged("artifact read lease was not established")
        return ArtifactReadLease(descriptor, identity)
    except ArtifactReadLeaseError:
        if descriptor is not None:
            try:
                fcntl.fcntl(descriptor, _fcntl_constant("F_SETLEASE"), _fcntl_constant("F_UNLCK"))
            except (OSError, ArtifactReadLeaseError):
                pass
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            try:
                fcntl.fcntl(descriptor, _fcntl_constant("F_SETLEASE"), _fcntl_constant("F_UNLCK"))
            except (OSError, ArtifactReadLeaseError):
                pass
            os.close(descriptor)
        raise ArtifactReadLeaseError("artifact read lease is unsupported or conflicted") from exc


__all__ = [
    "ArtifactReadLease",
    "ArtifactReadLeaseChanged",
    "ArtifactReadLeaseError",
    "acquire_artifact_read_lease",
]
