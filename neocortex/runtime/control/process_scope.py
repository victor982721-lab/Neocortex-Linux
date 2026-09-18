"""An explicitly delegated cgroup v2 scope for one external activity.

No cgroup root is discovered or enabled here. Without a writable delegated
subtree callers retain the bounded POSIX-group contract and report incomplete
coverage for detached descendants. The barrier executes only our small trusted
launcher before the supervisor attaches its PID and releases the requested argv.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from neocortex.runtime.scratch_tree import _mount_id, _open_directory_path


class ProcessScopeError(RuntimeError):
    """The delegated process scope could not prove identity or quiescence."""


@dataclass(frozen=True, slots=True)
class ProcessScopeReceipt:
    schema: str
    activity_id: str
    boot_id: str
    delegated_root: str
    cgroup_path: str
    root_identity: tuple[int, int]
    cgroup_identity: tuple[int, int]
    mount_id: int
    phase: str
    process_pid: int | None = None
    process_start_ticks: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verified_process_quiescence(note: Mapping[str, Any], *, activity_id: str,
                               process_pid: object = None) -> bool:
    """Accept only an owner-persisted complete scope for an associated process.

    The caller supplies a note loaded from its private durable producer claim.
    Leader exit, a group drain, arbitrary evidence and an operator release flag
    cannot prove that detached descendants have stopped producing payload.
    """

    pid = note.get("process_pid")
    if pid is None:
        pid = process_pid
    if pid is None:
        return (note.get("process_status") is None
                and note.get("process_scope_coverage") is None)
    ticks = note.get("process_start_ticks")
    receipt = note.get("process_scope_receipt")
    return bool(
        type(pid) is int and pid > 0
        and type(ticks) is int and ticks >= 0
        and note.get("process_status") == "exited"
        and note.get("process_scope") == "cgroup-v2"
        and note.get("process_scope_coverage") == "cgroup-v2"
        and isinstance(receipt, Mapping)
        and receipt.get("schema") == "neocortex.process-scope/v1"
        and receipt.get("activity_id") == activity_id
        and receipt.get("phase") == "quiescent"
        and receipt.get("process_pid") == pid
        and receipt.get("process_start_ticks") == ticks
    )


def _read_fd(parent: int, name: str, limit: int = 8192) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ProcessScopeError("cgroup observation exceeds its byte budget")
        return data
    finally:
        os.close(fd)


def _write_fd(parent: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if os.write(fd, data) != len(data):
            raise ProcessScopeError("cgroup control write was incomplete")
    finally:
        os.close(fd)


class DelegatedProcessScope:
    def __init__(self, delegated_root: Path, activity_id: str, *,
                 receipt_writer: Callable[[Mapping[str, Any]], None]) -> None:
        self.root = Path(delegated_root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ProcessScopeError("delegated cgroup root must be exact and absolute")
        self.activity_id = activity_id
        self.receipt_writer = receipt_writer
        self.root_fd = _open_directory_path(self.root)
        self.fd = -1
        self.name = "neocortex-activity-" + uuid.uuid4().hex
        self.phase = "prepared"
        self.pid: int | None = None
        self.start_ticks: int | None = None
        self.quiescent = False
        try:
            metadata = os.fstat(self.root_fd)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise ProcessScopeError("cgroup delegation owner or permissions are unsafe")
            self.root_identity = (metadata.st_dev, metadata.st_ino)
            self.mount_id = _mount_id(self.root_fd)
            with Path("/proc/self/mountinfo").open("rb") as stream:
                mountinfo = stream.read(1024 * 1024 + 1)
            if len(mountinfo) > 1024 * 1024:
                raise ProcessScopeError("cgroup mount observation exceeds budget")
            mounts = [line for line in mountinfo.splitlines()
                      if line.split(b" ", 1)[0] == str(self.mount_id).encode()]
            if len(mounts) != 1 or mounts[0].split(b" - ", 1)[1].split(b" ", 1)[0] != b"cgroup2":
                raise ProcessScopeError("delegated root is not a verified cgroup v2 filesystem")
            self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if len(self.boot_id) != 36:
                raise ProcessScopeError("kernel boot identity is unavailable")
            os.mkdir(self.name, 0o700, dir_fd=self.root_fd)
            self.fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root_fd)
            metadata = os.fstat(self.fd)
            self.cgroup_identity = (metadata.st_dev, metadata.st_ino)
            if _mount_id(self.fd) != self.mount_id or self._populated():
                raise ProcessScopeError("new cgroup is not an empty owned scope")
            self._persist()
        except BaseException:
            if self.fd >= 0:
                os.close(self.fd)
                try:
                    os.rmdir(self.name, dir_fd=self.root_fd)
                except OSError:
                    pass
            os.close(self.root_fd)
            raise

    @property
    def receipt(self) -> ProcessScopeReceipt:
        return ProcessScopeReceipt("neocortex.process-scope/v1", self.activity_id, self.boot_id,
                    str(self.root), str(self.root / self.name), self.root_identity,
                    self.cgroup_identity, self.mount_id, self.phase, self.pid, self.start_ticks)

    def _persist(self) -> None:
        self.receipt_writer(self.receipt.to_dict())

    def _validate(self) -> None:
        root = os.fstat(self.root_fd)
        current = os.fstat(self.fd)
        named = os.stat(self.name, dir_fd=self.root_fd, follow_symlinks=False)
        if ((root.st_dev, root.st_ino) != self.root_identity
                or (current.st_dev, current.st_ino) != self.cgroup_identity
                or (named.st_dev, named.st_ino) != self.cgroup_identity
                or _mount_id(self.fd) != self.mount_id
                or Path("/proc/sys/kernel/random/boot_id").read_text().strip() != self.boot_id):
            raise ProcessScopeError("cgroup scope identity changed")

    def _populated(self) -> bool:
        values = dict(line.split(maxsplit=1) for line in _read_fd(self.fd, "cgroup.events").splitlines())
        if values.get(b"populated") not in {b"0", b"1"}:
            raise ProcessScopeError("cgroup population observation is unavailable")
        return values[b"populated"] == b"1"

    def prepare_command(self, command: Sequence[str]) -> tuple[str, ...]:
        return (sys.executable, "-c",
                "import os,signal,sys;os.kill(os.getpid(),signal.SIGSTOP);"
                "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)", *command)

    def attach(self, pid: int, start_ticks: int | None, *, deadline: float) -> None:
        if start_ticks is None:
            raise ProcessScopeError("producer start identity is unavailable")
        stopped = False
        stop_deadline = min(deadline, time.monotonic() + 5.0)
        while time.monotonic() < stop_deadline:
            data = Path(f"/proc/{pid}/stat").read_bytes()
            fields = data.rpartition(b")")[2].split()
            if int(fields[19]) != start_ticks:
                raise ProcessScopeError("producer identity changed before cgroup assignment")
            if fields[0] in {b"T", b"t"}:
                stopped = True
                break
            time.sleep(0.005)
        if not stopped:
            raise ProcessScopeError("producer did not reach the pre-exec barrier")
        self._validate()
        _write_fd(self.fd, "cgroup.procs", f"{pid}\n".encode())
        members = _read_fd(self.fd, "cgroup.procs").splitlines()
        if str(pid).encode() not in members:
            raise ProcessScopeError("producer cgroup membership was not verified")
        self.pid, self.start_ticks, self.phase = pid, start_ticks, "running"
        self._persist()
        os.kill(pid, signal.SIGCONT)

    def terminate(self, deadline: float | None = None) -> None:
        self._validate()
        if self._populated():
            _write_fd(self.fd, "cgroup.kill", b"1\n")

    def verify_quiescent(self, *, deadline: float) -> None:
        self._validate()
        if self._populated():
            self.terminate(deadline)
        while self._populated():
            if time.monotonic() >= deadline:
                self.phase = "cleanup_unverified"
                self._persist()
                raise ProcessScopeError("cgroup producers remain alive at cleanup deadline")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        self.quiescent, self.phase = True, "quiescent"
        self._persist()

    def close(self) -> None:
        try:
            if self.quiescent:
                self._validate()
                os.rmdir(self.name, dir_fd=self.root_fd)
        finally:
            os.close(self.fd)
            os.close(self.root_fd)


__all__ = ["DelegatedProcessScope", "ProcessScopeError", "ProcessScopeReceipt", "verified_process_quiescence"]
