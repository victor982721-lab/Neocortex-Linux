"""Linux descriptor boundaries used by the registered scratch owner.

This module grants no deletion authority. Callers must retain their existing
owner, lifecycle, dependency and journal guards. The strict payload profile
remains regular files/directories; special fixture profiles are not enabled.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import struct
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .path_identity import PathIdentity


class PayloadProfile(StrEnum):
    STRICT = "strict"
    FIXTURE_POSIX_V1 = "fixture_posix_v1"


def policy_revision(profile: PayloadProfile | str) -> str:
    return f"{PayloadProfile(profile).value}/v1"


def payload_issue(metadata: os.stat_result, profile: PayloadProfile | str) -> str | None:
    """One type policy for observation, sealing and effect; no authority here."""
    profile = PayloadProfile(profile)
    if metadata.st_uid != os.geteuid():
        return "payload_owner_drift"
    if stat.S_ISDIR(metadata.st_mode):
        return None
    if stat.S_ISREG(metadata.st_mode):
        return "hardlink_payload" if metadata.st_nlink != 1 and profile is PayloadProfile.STRICT else None
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink_payload" if profile is PayloadProfile.STRICT else None
    if stat.S_ISFIFO(metadata.st_mode):
        return "payload_type_drift" if profile is PayloadProfile.STRICT else None
    if stat.S_ISSOCK(metadata.st_mode):
        return "socket_payload"
    if stat.S_ISCHR(metadata.st_mode) or stat.S_ISBLK(metadata.st_mode):
        return "device_payload"
    return "payload_type_drift"


class ScratchTreeError(RuntimeError):
    """A claimed tree cannot be inspected or removed with its boundaries intact."""

    def __init__(self, message: str, *, issue: str | None = None,
                 effects: int = 0, receipt: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.issue = issue or message
        self.effects = effects
        self.receipt = dict(receipt or {})


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _open_directory_path(
    path: Path, *,
    observed_ancestors: list[tuple[int, int, int, int, int]] | None = None,
    expected_ancestors: tuple[tuple[int, int, int, int, int], ...] | None = None,
) -> int:
    """Open each absolute ancestor, refusing symlinks in every component."""

    if not path.is_absolute() or ".." in path.parts:
        raise ScratchTreeError("scratch path is not a canonical absolute path")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    position = 0
    def check_ancestor(fd: int) -> None:
        nonlocal position
        if observed_ancestors is not None or expected_ancestors is not None:
            metadata = os.fstat(fd)
            claim = (*_identity(metadata), _mount_id(fd), metadata.st_uid,
                     stat.S_IMODE(metadata.st_mode))
            if expected_ancestors is not None and (
                position >= len(expected_ancestors) or claim != expected_ancestors[position]
            ):
                raise ScratchTreeError("scratch absolute ancestor identity changed",
                                       issue="ancestor_identity_changed")
            if observed_ancestors is not None:
                observed_ancestors.append(claim)
        position += 1
    try:
        check_ancestor(current)
        for component in path.parts[1:]:
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            os.close(current)
            current = next_fd
            check_ancestor(current)
        if expected_ancestors is not None and position != len(expected_ancestors):
            raise ScratchTreeError("scratch absolute ancestor chain changed",
                                   issue="ancestor_identity_changed")
        return current
    except BaseException:
        os.close(current)
        raise


def _mount_ids() -> frozenset[int]:
    """Capture a bounded kernel mount namespace observation, or abstain."""

    try:
        with open("/proc/self/mountinfo", "rb") as stream:
            raw = stream.read((1 << 20) + 1)
    except OSError as exc:
        raise ScratchTreeError("scratch mount topology is unavailable") from exc
    if not raw or len(raw) > (1 << 20):
        raise ScratchTreeError("scratch mount topology exceeds its observation limit")
    try:
        rows = [line.split() for line in raw.splitlines()]
        if any(len(row) < 10 or b"-" not in row for row in rows):
            raise ValueError("invalid mountinfo row")
        result = frozenset(int(row[0]) for row in rows)
    except (ValueError, IndexError) as exc:
        raise ScratchTreeError("scratch mount topology is invalid") from exc
    return result


def _mount_id(directory_fd: int) -> int:
    """mnt_id distinguishes a bind mount even when st_dev is unchanged."""

    try:
        with open(f"/proc/self/fdinfo/{directory_fd}", "rb") as stream:
            raw = stream.read(8193)
        values = [line.split(b":", 1)[1].strip() for line in raw.splitlines()
                  if line.startswith(b"mnt_id:")]
        if len(raw) > 8192 or len(values) != 1:
            raise ValueError("missing or ambiguous mount id")
        return int(values[0])
    except (OSError, ValueError) as exc:
        raise ScratchTreeError("scratch descriptor mount identity is unavailable") from exc


def read_private_manifest(path: Path, *, limit: int) -> bytes:
    """Read at most limit+1 bytes, with no-follow and before/after identity."""

    parent_fd = _open_directory_path(path.parent)
    fd = -1
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o077):
            raise ScratchTreeError("scratch manifest protection drifted")
        if before.st_size > limit:
            raise ScratchTreeError("scratch manifest is too large")
        # NONBLOCK prevents a raced FIFO replacement from blocking open.
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ScratchTreeError("scratch manifest identity changed")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        final = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if len(raw) > limit:
            raise ScratchTreeError("scratch manifest is too large")
        def claims(value: os.stat_result) -> tuple[Any, ...]:
            return (_identity(value), value.st_size, value.st_mtime_ns,
                    value.st_ctime_ns, value.st_uid, value.st_mode, value.st_nlink)
        if claims(before) != claims(after) or claims(after) != claims(final):
            raise ScratchTreeError("scratch manifest changed while reading")
        return raw
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _entries(directory_fd: int, *, limit: int) -> list[str]:
    names: list[str] = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            if len(names) >= limit:
                raise ScratchTreeError("scratch retirement member limit exceeded")
            names.append(entry.name)
    return names


def _checked_child(parent_fd: int, name: str | bytes, mount_id: int, *,
                   profile: PayloadProfile | str = PayloadProfile.STRICT,
                   readable_directory: bool = True) -> tuple[int, os.stat_result]:
    raw_name = os.fsencode(name)
    if raw_name in {b"", b".", b".."} or b"/" in raw_name or b"\0" in raw_name:
        raise ScratchTreeError("scratch child name is not a single POSIX component")
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    issue = payload_issue(before, profile)
    if issue is not None:
        raise ScratchTreeError(f"scratch strict payload profile rejected: {issue}", issue=issue)
    flags = os.O_NOFOLLOW | (os.O_RDONLY | os.O_DIRECTORY
                            if stat.S_ISDIR(before.st_mode) and readable_directory else os.O_PATH)
    child_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(child_fd)
        if _identity(opened) != _identity(before) or opened.st_mode != before.st_mode:
            raise ScratchTreeError("scratch child identity changed")
        if _mount_id(child_fd) != mount_id:
            raise ScratchTreeError("scratch child crosses a mount boundary", issue="mount_boundary")
        return child_fd, before
    except BaseException:
        os.close(child_fd)
        raise


def _fchmod_path_fd(descriptor: int, mode: int) -> None:
    """Linux fchmodat2(AT_EMPTY_PATH); no mutable-path chmod fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "fchmodat2", None)
    if function is not None:
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_int]
        function.restype = ctypes.c_int
        result = function(descriptor, b"", mode, 0x1000)
    else:
        # These Linux ABIs share __NR_fchmodat2. Other ABIs abstain until a
        # provider with an explicit syscall contract is available.
        if os.uname().machine not in {"x86_64", "aarch64", "riscv64"}:
            raise ScratchTreeError("permission_repair_unavailable")
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        result = syscall(ctypes.c_long(452), ctypes.c_int(descriptor),
                         ctypes.c_char_p(b""), ctypes.c_uint(mode), ctypes.c_int(0x1000))
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL}:
            raise ScratchTreeError("permission_repair_unavailable")
        raise OSError(error, os.strerror(error))


class _PermissionRepairs:
    def __init__(self, mount_id: int, writer: Callable[[Mapping[str, Any]], None],
                 max_fds: int) -> None:
        self.mount_id = mount_id
        self.writer = writer
        self.max_fds = max_fds
        self.claims: list[dict[str, Any]] = []
        self.descriptors: list[int] = []
        self.effects = 0

    def publish(self, status: str) -> None:
        self.writer({"schema": "neocortex.scratch-permissions/v1", "status": status,
                     "mount_id": self.mount_id, "effects": self.effects,
                     "directories": self.claims})

    def readable(self, descriptor: int, relative: bytes) -> int:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (metadata.st_uid != os.geteuid() or not stat.S_ISDIR(metadata.st_mode)
                or _mount_id(descriptor) != self.mount_id):
            raise ScratchTreeError("permission_repair_identity_drift")
        opened = -1
        try:
            try:
                opened = os.open(f"/proc/self/fd/{descriptor}", os.O_RDONLY | os.O_DIRECTORY)
            except PermissionError:
                pass
            if opened >= 0 and (_identity(os.fstat(opened)) != _identity(metadata)
                                or _mount_id(opened) != self.mount_id):
                raise ScratchTreeError("permission_repair_identity_drift")
            if mode & 0o700 != 0o700:
                if len(self.descriptors) >= self.max_fds:
                    raise ScratchTreeError("fd_limit")
                retained = os.dup(opened if opened >= 0 else descriptor)
                self.descriptors.append(retained)
                self.claims.append({"path": PathIdentity.from_path(relative).as_dict(),
                                    "identity": list(_identity(metadata)),
                                    "original_mode": mode, "adjusted_mode": mode | 0o700,
                                    "state": "prepared"})
                self.publish("prepared")
                if opened >= 0:
                    os.fchmod(opened, mode | 0o700)
                else:
                    _fchmod_path_fd(descriptor, mode | 0o700)
                self.claims[-1]["state"] = "adjusted"
                self.publish("adjusted")
            if opened < 0:
                opened = os.open(f"/proc/self/fd/{descriptor}", os.O_RDONLY | os.O_DIRECTORY)
            if (_identity(os.fstat(opened)) != _identity(metadata)
                    or _mount_id(opened) != self.mount_id):
                raise ScratchTreeError("permission_repair_identity_drift")
            result = opened
            opened = -1
            return result
        finally:
            if opened >= 0:
                os.close(opened)

    def restore(self) -> None:
        failures = []
        try:
            for descriptor, claim in reversed(list(zip(self.descriptors, self.claims, strict=True))):
                metadata = os.fstat(descriptor)
                if metadata.st_nlink == 0:
                    claim["state"] = "removed"
                    continue
                try:
                    if (list(_identity(metadata)) != claim["identity"]
                            or _mount_id(descriptor) != self.mount_id
                            or metadata.st_uid != os.geteuid()):
                        raise ScratchTreeError("permission_restore_identity_drift")
                    if stat.S_IMODE(metadata.st_mode) != claim["original_mode"]:
                        try:
                            os.fchmod(descriptor, claim["original_mode"])
                        except OSError as exc:
                            if exc.errno != errno.EBADF:
                                raise
                            _fchmod_path_fd(descriptor, claim["original_mode"])
                    claim["state"] = "restored"
                except (OSError, ScratchTreeError) as exc:
                    claim["state"] = "restore_failed"
                    claim["issue"] = str(exc)
                    failures.append(str(exc))
            self.publish("recovery_required" if failures else "restored")
        finally:
            for descriptor in self.descriptors:
                os.close(descriptor)
            self.descriptors.clear()
        if failures:
            raise ScratchTreeError("permission_restore_failed", effects=self.effects)


@contextmanager
def opened_claimed_tree(path: Path, *, profile: PayloadProfile | str = PayloadProfile.STRICT):
    """Open a workspace and its parent without following any ancestor link."""
    parent_fd = _open_directory_path(path.parent)
    root_fd = -1
    try:
        mount_id = _mount_id(parent_fd)
        if mount_id not in _mount_ids():
            raise ScratchTreeError("mount_observation_unavailable")
        root_fd, metadata = _checked_child(parent_fd, path.name, mount_id, profile=profile)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ScratchTreeError("scratch workspace is not a directory")
        yield root_fd, mount_id
        final = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(final) != _identity(metadata) or _mount_id(root_fd) != mount_id:
            raise ScratchTreeError("scratch workspace identity changed during observation")
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


@dataclass(frozen=True, slots=True)
class TreeObservation:
    members: int = 0
    apparent_bytes: int = 0
    complete: bool = False
    issue: str | None = None
    mount_id: int | None = None
    allocated_bytes: int | None = None
    exclusive_bytes: int | None = None


def observe_claimed_tree(path: Path, *, limit: int = 100_000, max_depth: int = 2048,
                         max_bytes: int = 1 << 50, max_fds: int = 2048,
                         profile: PayloadProfile | str = PayloadProfile.STRICT,
                         include_control_manifest: bool = False) -> TreeObservation:
    """Bounded read-only descriptor observation; partial is never measured zero."""
    members = apparent = 0
    allocated: int | None = 0
    observed_inodes: set[tuple[int, int]] = set()
    mount_id = None
    stack: list[tuple[int, Any, int]] = []
    try:
        with opened_claimed_tree(path, profile=profile) as (root_fd, mount_id):
            root_metadata = os.fstat(root_fd)
            root_blocks = getattr(root_metadata, "st_blocks", None)
            allocated = None if root_blocks is None else max(0, root_blocks) * 512
            observed_inodes.add(_identity(root_metadata))
            stack.append((os.dup(root_fd), None, 0))
            while stack:
                fd, iterator, depth = stack[-1]
                if iterator is None:
                    iterator = os.scandir(fd)
                    stack[-1] = fd, iterator, depth
                entry = next(iterator, None)
                if entry is None:
                    iterator.close()
                    os.close(fd)
                    stack.pop()
                    continue
                if depth == 0 and entry.name == "manifest.json" and not include_control_manifest:
                    continue
                if members >= limit:
                    raise ScratchTreeError("entry_limit")
                members += 1
                child, metadata = _checked_child(fd, entry.name, mount_id, profile=profile)
                first_inode = _identity(metadata) not in observed_inodes
                if first_inode:
                    observed_inodes.add(_identity(metadata))
                    blocks = getattr(metadata, "st_blocks", None)
                    allocated = None if allocated is None or blocks is None else allocated + max(0, blocks) * 512
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= max_depth or len(stack) + 3 >= max_fds:
                        os.close(child)
                        raise ScratchTreeError("depth_limit" if depth >= max_depth else "fd_limit")
                    stack.append((child, None, depth + 1))
                else:
                    os.close(child)
                    if stat.S_ISREG(metadata.st_mode) and first_inode:
                        if metadata.st_size > max_bytes - apparent:
                            apparent = max_bytes
                            raise ScratchTreeError("byte_limit")
                        apparent += max(0, metadata.st_size)
        return TreeObservation(members, apparent, True, mount_id=mount_id, allocated_bytes=allocated)
    except (OSError, ScratchTreeError) as exc:
        issue = exc.issue if isinstance(exc, ScratchTreeError) else (
            "permission_repair_required" if isinstance(exc, PermissionError)
            else "payload_identity_unavailable")
        return TreeObservation(members, apparent, False, issue, mount_id, allocated)
    finally:
        for fd, iterator, _depth in reversed(stack):
            if iterator is not None:
                iterator.close()
            os.close(fd)


def directory_batch(directory_fd: int, *, cookie: int, limit: int) -> tuple[list[tuple[bytes, int]], bool]:
    """Read one bounded Linux getdents64 batch, retaining kernel resume cookies.

    A cookie is used only after the directory generation has been revalidated.
    Names are raw POSIX bytes, never display escapes or decoded cursor text.
    """
    if limit < 1 or cookie < 0:
        raise ValueError("invalid directory batch budget/cookie")
    machine = os.uname().machine
    syscall_number = 217 if machine == "x86_64" else 61 if machine in {"aarch64", "riscv64"} else None
    if syscall_number is None:
        raise ScratchTreeError("directory_cursor_unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    os.lseek(directory_fd, cookie, os.SEEK_SET)
    result: list[tuple[bytes, int]] = []
    buffer = ctypes.create_string_buffer(64 * 1024)
    while len(result) < limit:
        received = syscall(ctypes.c_long(syscall_number), ctypes.c_int(directory_fd),
                           ctypes.byref(buffer), ctypes.c_uint(len(buffer)))
        if received < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if received == 0:
            return result, True
        raw = buffer.raw[:received]
        offset = 0
        while offset < received:
            if received - offset < 20:
                raise ScratchTreeError("directory_cursor_invalid")
            _inode, following, record_length = struct.unpack_from("=QqH", raw, offset)
            if record_length < 20 or offset + record_length > received or following < 0:
                raise ScratchTreeError("directory_cursor_invalid")
            name = raw[offset + 19:offset + record_length].split(b"\0", 1)[0]
            offset += record_length
            if name in {b".", b".."}:
                continue
            result.append((name, following))
            if len(result) >= limit:
                return result, False
    return result, False


@dataclass(slots=True)
class _Frame:
    fd: int
    parent_fd: int | None
    name: str | None
    identity: tuple[int, int] | None
    names: list[str]


def _walk_strict(directory_fd: int, mount_id: int, *, limit: int, remove: bool,
                 profile: PayloadProfile | str = PayloadProfile.STRICT,
                 max_fds: int = 2048, max_depth: int = 2048,
                 effect_callback: Callable[[int], None] | None = None,
                 boundary_check: Callable[[], None] | None = None) -> None:
    """Iterative descriptor traversal with a complete validation pass first."""

    # Each stack frame retains the opened identity until its children finish.
    initial_entries = _entries(directory_fd, limit=limit)
    # pop() removes from the end: keep the exact root control manifest until
    # all payload is gone, so a partial failure retains its lifecycle claim.
    initial_entries.sort(key=lambda name: (name != "manifest.json", os.fsencode(name)))
    stack = [_Frame(os.dup(directory_fd), None, None, None, initial_entries)]
    observed = 0
    def check_containment() -> None:
        if boundary_check is None:
            raise ScratchTreeError("scratch retirement requires an absolute containment boundary")
        boundary_check()
        # A retained FD remains usable after its directory is moved outside
        # the workspace. Reopen every parent/name link, including upper
        # internal ancestors, instead of treating the FD itself as containment.
        for index, ancestor in enumerate(stack[1:], start=1):
            parent = stack[index - 1]
            if (ancestor.name is None or ancestor.identity is None
                    or ancestor.parent_fd != parent.fd):
                raise ScratchTreeError("scratch retained ancestor claim is invalid")
            reopened_fd, metadata = _checked_child(parent.fd, ancestor.name, mount_id, profile=profile)
            try:
                if (_identity(metadata) != ancestor.identity
                        or _identity(os.fstat(ancestor.fd)) != ancestor.identity):
                    raise ScratchTreeError("scratch internal ancestor identity changed",
                                           issue="ancestor_identity_changed")
            finally:
                os.close(reopened_fd)
    try:
        while stack:
            frame = stack[-1]
            fd = frame.fd
            if not frame.names:
                if remove and frame.parent_fd is not None:
                    if frame.name is None or frame.identity is None:
                        raise ScratchTreeError("scratch directory frame has no identity")
                    name, identity = frame.name, frame.identity
                    parent_fd = frame.parent_fd
                    check_fd, checked = _checked_child(parent_fd, name, mount_id, profile=profile)
                    try:
                        if _identity(checked) != identity or _identity(os.fstat(fd)) != identity:
                            raise ScratchTreeError("scratch directory changed before rmdir")
                        check_containment()
                        os.rmdir(name, dir_fd=parent_fd)
                        if effect_callback is not None:
                            effect_callback(1)
                    finally:
                        os.close(check_fd)
                os.close(fd)
                stack.pop()
                continue
            name = frame.names.pop()
            observed += 1
            if observed > limit:
                raise ScratchTreeError("scratch retirement member limit exceeded")
            if _mount_id(fd) != mount_id:
                raise ScratchTreeError("scratch directory mount identity changed")
            child_fd, metadata = _checked_child(fd, name, mount_id, profile=profile)
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    if len(stack) >= max_depth or len(stack) + 3 >= max_fds:
                        raise ScratchTreeError("depth_limit" if len(stack) >= max_depth else "fd_limit")
                    children = _entries(child_fd, limit=limit - observed)
                except BaseException:
                    os.close(child_fd)
                    raise
                stack.append(_Frame(child_fd, fd, name, _identity(metadata), children))
            else:
                try:
                    if remove:
                        check_containment()
                        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (_identity(current) != _identity(metadata)
                                or payload_issue(current, profile) is not None
                                or current.st_ctime_ns != metadata.st_ctime_ns):
                            raise ScratchTreeError("scratch file changed before unlink")
                        os.unlink(name, dir_fd=fd)
                        if effect_callback is not None:
                            effect_callback(1)
                finally:
                    os.close(child_fd)
    finally:
        for frame in reversed(stack):
            os.close(frame.fd)


@contextmanager
def prepared_fixture_tree(path: Path, *, receipt_writer: Callable[[Mapping[str, Any]], None],
                          limit: int = 100_001, max_fds: int = 2048,
                          max_depth: int = 2048):
    """Scoped permission preparation for an explicitly granted fixture owner.

    Modes are journaled before each change and restored on surviving
    descriptors on every exit. Regular files, links and FIFO are never chmoded.
    """
    parent_fd = _open_directory_path(path.parent)
    root_fd = -1
    repairs = None
    stack: list[tuple[int, bytes, Any, int]] = []
    try:
        mount_id = _mount_id(parent_fd)
        if mount_id not in _mount_ids():
            raise ScratchTreeError("mount_observation_unavailable")
        root_fd, _metadata = _checked_child(parent_fd, path.name, mount_id,
                    profile=PayloadProfile.FIXTURE_POSIX_V1, readable_directory=False)
        repairs = _PermissionRepairs(mount_id, receipt_writer, max_fds)
        readable = repairs.readable(root_fd, b"")
        stack.append((readable, b"", None, 0))
        members = 0
        while stack:
            fd, relative, iterator, depth = stack[-1]
            if iterator is None:
                iterator = os.scandir(fd)
                stack[-1] = fd, relative, iterator, depth
            entry = next(iterator, None)
            if entry is None:
                iterator.close()
                os.close(fd)
                stack.pop()
                continue
            members += 1
            if members > limit:
                raise ScratchTreeError("entry_limit")
            child_fd, metadata = _checked_child(fd, entry.name, mount_id,
                        profile=PayloadProfile.FIXTURE_POSIX_V1, readable_directory=False)
            try:
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= max_depth or len(stack) + len(repairs.descriptors) + 4 >= max_fds:
                        raise ScratchTreeError("depth_limit" if depth >= max_depth else "fd_limit")
                    child_relative = relative + b"/" + os.fsencode(entry.name)
                    readable = repairs.readable(child_fd, child_relative)
                    stack.append((readable, child_relative, None, depth + 1))
            finally:
                os.close(child_fd)
        yield repairs
    finally:
        for fd, _relative, iterator, _depth in reversed(stack):
            if iterator is not None:
                iterator.close()
            os.close(fd)
        try:
            if repairs is not None:
                repairs.restore()
        finally:
            if root_fd >= 0:
                os.close(root_fd)
            os.close(parent_fd)


def remove_claimed_directory_contents(directory_fd: int, *, limit: int,
                    profile: PayloadProfile | str = PayloadProfile.STRICT,
                    max_fds: int = 2048, max_depth: int = 2048,
                    effect_callback: Callable[[int], None] | None = None,
                    boundary_check: Callable[[], None] | None = None) -> None:
    if boundary_check is None:
        raise ScratchTreeError("scratch retirement requires an absolute containment boundary")
    known_mounts = _mount_ids()
    mount_id = _mount_id(directory_fd)
    if mount_id not in known_mounts:
        raise ScratchTreeError("scratch mount identity was not observed")
    _walk_strict(directory_fd, mount_id, limit=limit, remove=False, profile=profile,
                 max_fds=max_fds, max_depth=max_depth)
    if _mount_ids() != known_mounts:
        raise ScratchTreeError("mount_topology_changed")
    _walk_strict(directory_fd, mount_id, limit=limit, remove=True, profile=profile,
                 max_fds=max_fds, max_depth=max_depth, effect_callback=effect_callback,
                 boundary_check=boundary_check)


def remove_claimed_tree(path: Path, *, expected_identity: tuple[int, int, int] | None,
                        limit: int, profile: PayloadProfile | str = PayloadProfile.STRICT,
                        max_fds: int = 2048, max_depth: int = 2048,
                        effect_callback: Callable[[int], None] | None = None) -> None:
    ancestor_claims: list[tuple[int, int, int, int, int]] = []
    parent_fd = _open_directory_path(path.parent, observed_ancestors=ancestor_claims)
    expected_ancestors = tuple(ancestor_claims)
    child_fd = -1
    try:
        parent_mount = _mount_id(parent_fd)
        child_fd, metadata = _checked_child(parent_fd, path.name, parent_mount, profile=profile)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ScratchTreeError("scratch workspace is not a directory")
        birthtime = getattr(metadata, "st_birthtime_ns", None)
        identity = (*_identity(metadata), int(birthtime) if birthtime is not None else -1)
        if expected_identity is not None and identity != expected_identity:
            raise ScratchTreeError("scratch workspace identity changed before retirement")
        namespace_identity = _identity(os.stat("/proc/self/ns/mnt"))
        def boundary_check() -> None:
            if _identity(os.stat("/proc/self/ns/mnt")) != namespace_identity:
                raise ScratchTreeError("mount_namespace_changed")
            fresh_parent = -1
            current_fd = -1
            try:
                # Reopen from '/', not the retained parent FD: that FD can
                # point into a tree that has been renamed out of this scope.
                fresh_parent = _open_directory_path(path.parent, expected_ancestors=expected_ancestors)
                if _identity(os.fstat(fresh_parent)) != _identity(os.fstat(parent_fd)):
                    raise ScratchTreeError("scratch parent identity changed during retirement")
                current_fd, current = _checked_child(fresh_parent, path.name, parent_mount, profile=profile)
                if _identity(current) != _identity(metadata):
                    raise ScratchTreeError("scratch workspace changed during retirement")
            except OSError as exc:
                raise ScratchTreeError("scratch absolute ancestor is unavailable",
                                       issue="ancestor_identity_unavailable") from exc
            finally:
                if current_fd >= 0:
                    os.close(current_fd)
                if fresh_parent >= 0:
                    os.close(fresh_parent)
        remove_claimed_directory_contents(child_fd, limit=limit, profile=profile,
                    max_fds=max_fds, max_depth=max_depth, effect_callback=effect_callback,
                    boundary_check=boundary_check)
        final_fd, final = _checked_child(parent_fd, path.name, parent_mount, profile=profile)
        try:
            if _identity(final) != _identity(metadata):
                raise ScratchTreeError("scratch workspace changed before final rmdir")
            boundary_check()
            os.rmdir(path.name, dir_fd=parent_fd)
            if effect_callback is not None:
                effect_callback(1)
        finally:
            os.close(final_fd)
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(parent_fd)


__all__ = [
    "PayloadProfile",
    "ScratchTreeError",
    "TreeObservation",
    "observe_claimed_tree",
    "opened_claimed_tree",
    "payload_issue",
    "policy_revision",
    "prepared_fixture_tree",
    "read_private_manifest",
    "remove_claimed_directory_contents",
    "remove_claimed_tree",
]
