"""Bounded MIME attachment inventory and physical materialization.

The Text route owns the visible body of an ``.eml``.  This module is the
explicit integration seam for callers that also want attachment files in the
normal filesystem route.  It never executes an attachment, replaces a
destination, or removes the parent message.  The parent identity and a
content digest are fenced before and after parsing/effect, and a manifest
records the parent/child relationship for replay.
"""

from __future__ import annotations

import hashlib
import ctypes
import errno
import json
import os
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field, replace
from email import policy
from email.message import Message
from email.parser import BytesFeedParser
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Iterator

from neocortex.persistence.framework_state_types import RunBudgetExceeded
from neocortex.runtime.control.cancellation import CancellationRequested


EMAIL_ATTACHMENT_SCHEMA = "neocortex.email-attachments/v1"
DEFAULT_MAX_EMAIL_PARTS = 4_096
DEFAULT_MAX_EMAIL_DEPTH = 64
DEFAULT_MAX_EMAIL_PART_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_EMAIL_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_EMAIL_SOURCE_BYTES = 256 * 1024 * 1024
_MAX_FILENAME_CHARS = 180
_MAX_FILENAME_BYTES = 255
_EMAIL_READ_CHUNK_BYTES = 1024 * 1024
_CONTROL_TRANSLATION = dict.fromkeys(range(32), "_")


class EmailAttachmentError(ValueError):
    """A bounded MIME or child-materialization operation failed closed."""

    def __init__(self, status: str, reason: str, detail: str | None = None):
        super().__init__(detail or reason)
        self.status = status
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True, slots=True)
class EmailAttachmentResolution:
    """Trusted resolver result; content-equivalent reuse is opt-in."""

    path: str | os.PathLike[str] | None
    reuse_kind: str = "identity"
    provenance: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PreparedEmailAttachments:
    """One bounded parent read/parse reusable by body-only and apply stages."""

    source_path: Path
    parent_identity: EmailSourceIdentity
    parent_sha256: str
    attachments: tuple[EmailAttachmentDescriptor, ...]
    limits: EmailAttachmentLimits

    def assert_current(self) -> EmailSourceIdentity:
        current = EmailSourceIdentity.capture(self.source_path)
        if current != self.parent_identity:
            raise EmailAttachmentError("source_changed", "parent_identity_changed_after_prepare")
        return current


@dataclass(frozen=True, slots=True)
class EmailAttachmentLimits:
    """Independent MIME traversal and byte budgets."""

    max_parts: int = DEFAULT_MAX_EMAIL_PARTS
    max_depth: int = DEFAULT_MAX_EMAIL_DEPTH
    max_part_bytes: int = DEFAULT_MAX_EMAIL_PART_BYTES
    max_total_bytes: int = DEFAULT_MAX_EMAIL_TOTAL_BYTES
    max_source_bytes: int = DEFAULT_MAX_EMAIL_SOURCE_BYTES

    def validate(self) -> None:
        for name in (
            "max_parts",
            "max_depth",
            "max_part_bytes",
            "max_total_bytes",
            "max_source_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_part_bytes > self.max_total_bytes:
            raise ValueError("max_part_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True, slots=True)
class EmailSourceIdentity:
    """Path-bound identity used by the child publication fence."""

    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int

    @classmethod
    def from_stat(cls, path: Path, metadata: os.stat_result) -> "EmailSourceIdentity":
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise EmailAttachmentError("unsafe", "parent_not_regular")
        return cls(
            path=os.fspath(path),
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            size=int(metadata.st_size),
            mtime_ns=int(metadata.st_mtime_ns),
            ctime_ns=int(metadata.st_ctime_ns),
            nlink=int(metadata.st_nlink),
        )

    @classmethod
    def capture(cls, path: Path) -> "EmailSourceIdentity":
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise EmailAttachmentError("blocked", "parent_unavailable", str(exc)) from exc
        return cls.from_stat(path, metadata)

    def matches(self, path: Path) -> bool:
        try:
            current = self.from_stat(path, path.lstat())
        except (OSError, EmailAttachmentError):
            return False
        return current == self

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "nlink": self.nlink,
        }

    def stable_key(self, sha256: str) -> str:
        return f"{self.device}:{self.inode}:{self.size}:{self.mtime_ns}:{sha256}"

    def same_physical_content(self, other: "EmailSourceIdentity") -> bool:
        """Compare fields stable across an authorized own move."""

        return (
            self.device == other.device
            and self.inode == other.inode
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
        )


@dataclass(frozen=True, slots=True)
class EmailAttachmentDescriptor:
    """A safe, content-bound attachment observation before publication."""

    ordinal: int
    part_path: str
    filename: str
    media_type: str
    content_disposition: str | None
    content_id: str | None
    payload: bytes = field(repr=False, compare=False)
    sha256: str

    @property
    def size(self) -> int:
        return len(self.payload)

    def to_dict(self, *, include_payload: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "ordinal": self.ordinal,
            "part_path": self.part_path,
            "filename": self.filename,
            "media_type": self.media_type,
            "content_disposition": self.content_disposition,
            "content_id": self.content_id,
            "size": self.size,
            "sha256": self.sha256,
        }
        if include_payload:
            value["payload"] = self.payload
        return value


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """One child file and its parent lineage evidence."""

    ordinal: int
    part_path: str
    filename: str
    media_type: str
    content_disposition: str | None
    content_id: str | None
    size: int
    sha256: str
    child_path: str | None
    status: str
    child_device: int | None = None
    child_inode: int | None = None
    child_mtime_ns: int | None = None
    child_reuse_kind: str = "materialized"
    previous_child_device: int | None = None
    previous_child_inode: int | None = None
    previous_child_mtime_ns: int | None = None
    # Ephemeral owner-verified history; deliberately not accepted from a manifest.
    historical_successor_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "part_path": self.part_path,
            "filename": self.filename,
            "media_type": self.media_type,
            "content_disposition": self.content_disposition,
            "content_id": self.content_id,
            "size": self.size,
            "sha256": self.sha256,
            "child_path": self.child_path,
            "status": self.status,
            "child_device": self.child_device,
            "child_inode": self.child_inode,
            "child_mtime_ns": self.child_mtime_ns,
            "child_reuse_kind": self.child_reuse_kind,
            "previous_child_device": self.previous_child_device,
            "previous_child_inode": self.previous_child_inode,
            "previous_child_mtime_ns": self.previous_child_mtime_ns,
        }


@dataclass(frozen=True, slots=True)
class EmailAttachmentManifest:
    """Result of plan/apply/replay, serializable as the integration receipt."""

    schema: str
    status: str
    parent_path: str
    parent_identity: EmailSourceIdentity
    parent_sha256: str
    destination: str
    manifest_path: str
    attachments: tuple[EmailAttachment, ...]
    reason: str | None = None
    detail: str | None = None
    parent_physical_key: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "parent_path": self.parent_path,
            "parent_identity": self.parent_identity.to_dict(),
            "parent_sha256": self.parent_sha256,
            "destination": self.destination,
            "manifest_path": self.manifest_path,
            "attachments": [item.to_dict() for item in self.attachments],
            "reason": self.reason,
            "detail": self.detail,
            "parent_physical_key": self.parent_physical_key
            or self.parent_identity.stable_key(self.parent_sha256),
        }


def _sanitize_filename(raw: str | None, media_type: str, digest: str) -> str:
    value = unicodedata.normalize("NFKC", str(raw or "")).replace("\\", "/")
    value = value.rsplit("/", 1)[-1].translate(_CONTROL_TRANSLATION)
    value = " ".join(value.split()).strip(" .")
    if not value or value in {".", ".."}:
        extension = {
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "message/rfc822": ".eml",
        }.get(media_type.casefold(), ".bin")
        value = f"attachment-{digest[:12]}{extension}"
    if len(value) > _MAX_FILENAME_CHARS:
        suffix = Path(value).suffix[:20]
        value = value[: _MAX_FILENAME_CHARS - len(suffix)] + suffix
    return value


def _child_name(ordinal: int, filename: str) -> str:
    """Bound the final UTF-8 basename, including the deterministic ordinal."""

    prefix = f"{ordinal:04d}--"
    budget = _MAX_FILENAME_BYTES - len(prefix.encode("utf-8"))
    encoded = filename.encode("utf-8")
    if len(encoded) <= budget:
        return prefix + filename
    suffix = Path(filename).suffix
    suffix_bytes = suffix.encode("utf-8")
    keep = max(1, budget - len(suffix_bytes))
    base_bytes = filename.encode("utf-8")[:keep]
    base = base_bytes.decode("utf-8", errors="ignore").rstrip(" .")
    candidate = prefix + base + suffix
    while len(candidate.encode("utf-8")) > _MAX_FILENAME_BYTES and base:
        base = base[:-1]
        candidate = prefix + base + suffix
    if not base:
        candidate = prefix + "attachment" + suffix
    return candidate


def _attachment_payload(part: Message, *, limits: EmailAttachmentLimits) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            payload = raw.encode(part.get_content_charset() or "utf-8", "strict")
        else:
            raise EmailAttachmentError("corrupt", "attachment_payload_unavailable")
    if not isinstance(payload, bytes):
        raise EmailAttachmentError("corrupt", "attachment_payload_not_bytes")
    if len(payload) > limits.max_part_bytes:
        raise EmailAttachmentError("budget", "attachment_part_budget")
    return payload


class _BoundedFeedParser(BytesFeedParser):
    """Feed parser that rejects MIME node/depth growth before attaching nodes."""

    def __init__(self, *, limits: EmailAttachmentLimits) -> None:
        self._email_limits = limits
        self._email_nodes = 0
        super().__init__(policy=policy.default)

    def _new_message(self) -> object:
        if self._email_nodes >= self._email_limits.max_parts:
            raise EmailAttachmentError("budget", "attachment_part_count_budget")
        # ``_msgstack`` contains the current ancestors before the new node is
        # allocated.  The root is depth zero; a child at max_depth is allowed.
        ancestors = getattr(self, "_msgstack", ())
        if not isinstance(ancestors, list):
            raise EmailAttachmentError("dependency", "email_parser_stack_unavailable")
        if len(ancestors) > self._email_limits.max_depth:
            raise EmailAttachmentError("budget", "attachment_depth_budget")
        self._email_nodes += 1
        new_message = getattr(super(), "_new_message", None)
        if not callable(new_message):
            raise EmailAttachmentError("dependency", "email_parser_factory_unavailable")
        return new_message()


def _parse_email_stream(
    chunks: Iterator[bytes],
    *,
    limits: EmailAttachmentLimits,
    checkpoint: Callable[[], None] | None = None,
) -> Message:
    parser = _BoundedFeedParser(limits=limits)
    for chunk in chunks:
        if checkpoint is not None:
            checkpoint()
        parser.feed(chunk)
        if checkpoint is not None:
            checkpoint()
    if checkpoint is not None:
        checkpoint()
    return parser.close()


def prepare_email_attachments(
    source: str | os.PathLike[str],
    *,
    limits: EmailAttachmentLimits | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> PreparedEmailAttachments:
    """Read/hash/parse one EML once and return a reusable prepared decision."""

    effective = EmailAttachmentLimits() if limits is None else limits
    effective.validate()
    source_path = Path(source)
    identity = EmailSourceIdentity.capture(source_path)
    if identity.size > effective.max_source_bytes:
        raise EmailAttachmentError("budget", "parent_source_budget")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(source_path, flags)
    except OSError as exc:
        raise EmailAttachmentError("blocked", "parent_open_failed", str(exc)) from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        opened_identity = EmailSourceIdentity.from_stat(source_path, opened)
        if opened_identity != identity:
            raise EmailAttachmentError("source_changed", "parent_identity_changed_before_read")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1

            def feed_chunks() -> Iterator[bytes]:
                remaining = identity.size
                while remaining:
                    if checkpoint is not None:
                        checkpoint()
                    chunk = stream.read(min(_EMAIL_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise EmailAttachmentError("source_changed", "parent_size_changed_while_reading")
                    digest.update(chunk)
                    remaining -= len(chunk)
                    yield chunk
                if stream.read(1):
                    raise EmailAttachmentError("source_changed", "parent_grew_while_reading")

            message = _parse_email_stream(
                feed_chunks(),
                limits=effective,
                checkpoint=checkpoint,
            )
    except EmailAttachmentError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise EmailAttachmentError("corrupt", "email_parse_failed", str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not identity.matches(source_path):
        raise EmailAttachmentError("source_changed", "parent_identity_changed_after_prepare")
    descriptors = tuple(
        iter_email_attachments(message, limits=effective, checkpoint=checkpoint)
    )
    if checkpoint is not None:
        checkpoint()
    return PreparedEmailAttachments(
        source_path=source_path,
        parent_identity=identity,
        parent_sha256=digest.hexdigest(),
        attachments=descriptors,
        limits=effective,
    )


def iter_email_attachments(
    message: Message,
    *,
    limits: EmailAttachmentLimits | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> Iterator[EmailAttachmentDescriptor]:
    """Yield attachment payloads in deterministic MIME traversal order."""

    effective = EmailAttachmentLimits() if limits is None else limits
    effective.validate()
    pending: list[tuple[Message, int, str]] = [(message, 0, "0")]
    visited = 0
    ordinal = 0
    total_bytes = 0
    while pending:
        if checkpoint is not None:
            checkpoint()
        part, depth, part_path = pending.pop()
        visited += 1
        if visited > effective.max_parts:
            raise EmailAttachmentError("budget", "attachment_part_count_budget")
        if depth > effective.max_depth:
            raise EmailAttachmentError("budget", "attachment_depth_budget")
        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                for index, child in reversed(tuple(enumerate(children))):
                    if isinstance(child, Message):
                        pending.append((child, depth + 1, f"{part_path}/{index}"))
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue
        payload = _attachment_payload(part, limits=effective)
        total_bytes += len(payload)
        if total_bytes > effective.max_total_bytes:
            raise EmailAttachmentError("budget", "attachment_total_budget")
        digest = hashlib.sha256(payload).hexdigest()
        if checkpoint is not None:
            checkpoint()
        ordinal += 1
        yield EmailAttachmentDescriptor(
            ordinal=ordinal,
            part_path=part_path,
            filename=_sanitize_filename(filename, part.get_content_type(), digest),
            media_type=part.get_content_type().casefold(),
            content_disposition=disposition,
            content_id=(str(part.get("Content-ID"))[:512] if part.get("Content-ID") else None),
            payload=payload,
            sha256=digest,
        )


def describe_email_attachments(
    payload: bytes,
    *,
    limits: EmailAttachmentLimits | None = None,
) -> tuple[dict[str, object], ...]:
    """Return bounded attachment metadata without writing physical children."""

    effective = EmailAttachmentLimits() if limits is None else limits
    effective.validate()
    if len(payload) > effective.max_source_bytes:
        raise EmailAttachmentError("budget", "parent_source_budget")
    message = _parse_email_stream(
        (payload[index : index + _EMAIL_READ_CHUNK_BYTES] for index in range(0, len(payload), _EMAIL_READ_CHUNK_BYTES)),
        limits=effective,
    )
    return tuple(
        item.to_dict()
        for item in iter_email_attachments(message, limits=effective)
    )


def stable_attachment_root(
    state_root: str | os.PathLike[str],
    identity: EmailSourceIdentity,
    parent_sha256: str,
) -> Path:
    """Return a filename-independent child root for integration callers."""

    root = Path(state_root)
    if not root.is_absolute():
        raise EmailAttachmentError("unsafe", "state_root_not_absolute")
    return root / f"email-{identity.device:x}-{identity.inode:x}-{parent_sha256[:24]}"



def _ensure_directory(path: Path, *, create: bool = True) -> None:
    if not path.is_absolute() or "\x00" in os.fspath(path):
        raise EmailAttachmentError("unsafe", "destination_invalid")
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise EmailAttachmentError("unsafe", "destination_not_directory")
        elif create:
            path.mkdir(parents=True, mode=0o700)
        else:
            return
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise EmailAttachmentError("unsafe", "destination_not_directory")
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("blocked", "destination_unavailable", str(exc)) from exc


def _open_pinned_directory(path: Path, *, create: bool) -> tuple[int, tuple[int, int]]:
    """Open a directory with O_NOFOLLOW and retain its physical identity."""

    _ensure_directory(path, create=create)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        current = path.lstat()
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        if identity != (int(current.st_dev), int(current.st_ino)):
            os.close(descriptor)
            raise EmailAttachmentError("source_changed", "destination_identity_changed")
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            os.close(descriptor)
            raise EmailAttachmentError("unsafe", "destination_not_directory")
        return descriptor, identity
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("blocked", "destination_open_failed", str(exc)) from exc


def _pinned_directory_matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(metadata.st_mode)
        and stat.S_ISDIR(metadata.st_mode)
        and (int(metadata.st_dev), int(metadata.st_ino)) == identity
    )


def _rename_directory_noreplace(
    stage: Path,
    destination: Path,
    *,
    parent_fd: int | None = None,
    parent_identity: tuple[int, int] | None = None,
) -> None:
    """Atomically publish a complete stage without replacing a destination."""

    if stage.parent != destination.parent:
        raise EmailAttachmentError("dependency", "stage_destination_parent_mismatch")
    owns_parent_fd = parent_fd is None
    if parent_fd is None:
        parent_fd, parent_identity = _open_pinned_directory(stage.parent, create=False)
    elif parent_identity is None:
        metadata = os.fstat(parent_fd)
        parent_identity = (int(metadata.st_dev), int(metadata.st_ino))
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise EmailAttachmentError("dependency", "rename_noreplace_unavailable")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if not _pinned_directory_matches(stage.parent, parent_identity):
            raise EmailAttachmentError("source_changed", "destination_parent_changed")
        result = renameat2(
            parent_fd,
            os.fsencode(stage.name),
            parent_fd,
            os.fsencode(destination.name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise EmailAttachmentError("collision", "destination_collision")
            raise EmailAttachmentError("dependency", "stage_publish_failed", os.strerror(error_number))
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("dependency", "stage_publish_failed", str(exc)) from exc
    finally:
        if owns_parent_fd:
            os.close(parent_fd)


def _write_child(
    path: Path,
    payload: bytes,
    expected_sha256: str,
    *,
    parent_fd: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    name = path.name
    if parent_fd is None:
        exists = os.path.lexists(path)
    else:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            exists = False
        except OSError as exc:
            raise EmailAttachmentError("blocked", "child_probe_failed", str(exc)) from exc
        else:
            exists = True
    if exists:
        try:
            metadata = path.lstat() if parent_fd is None else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise EmailAttachmentError("collision", "child_path_not_regular", os.fspath(path))
            if parent_fd is None:
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(_EMAIL_READ_CHUNK_BYTES):
                        if checkpoint is not None:
                            checkpoint()
                        hasher.update(chunk)
                digest = hasher.hexdigest()
            else:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
                with os.fdopen(descriptor, "rb", closefd=True) as stream:
                    hasher = hashlib.sha256()
                    while chunk := stream.read(_EMAIL_READ_CHUNK_BYTES):
                        if checkpoint is not None:
                            checkpoint()
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
        except EmailAttachmentError:
            raise
        except OSError as exc:
            raise EmailAttachmentError("blocked", "child_replay_read_failed", str(exc)) from exc
        if digest == expected_sha256 and len(payload) == metadata.st_size:
            return "replayed"
        raise EmailAttachmentError("collision", "child_destination_collision", os.fspath(path))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = (
            os.open(path, flags, 0o600)
            if parent_fd is None
            else os.open(name, flags, 0o600, dir_fd=parent_fd)
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                for offset in range(0, len(payload), _EMAIL_READ_CHUNK_BYTES):
                    if checkpoint is not None:
                        checkpoint()
                    stream.write(payload[offset : offset + _EMAIL_READ_CHUNK_BYTES])
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except FileExistsError as exc:
        raise EmailAttachmentError("collision", "child_destination_collision", os.fspath(path)) from exc
    except OSError as exc:
        raise EmailAttachmentError("blocked", "child_write_failed", str(exc)) from exc
    try:
        metadata = path.lstat() if parent_fd is None else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise EmailAttachmentError("unsafe", "child_not_regular")
        if parent_fd is None:
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(_EMAIL_READ_CHUNK_BYTES):
                    if checkpoint is not None:
                        checkpoint()
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        else:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                hasher = hashlib.sha256()
                while chunk := stream.read(_EMAIL_READ_CHUNK_BYTES):
                    if checkpoint is not None:
                        checkpoint()
                    hasher.update(chunk)
                digest = hasher.hexdigest()
        if metadata.st_size != len(payload) or digest != expected_sha256:
            raise EmailAttachmentError("corrupt", "child_integrity_failed")
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("blocked", "child_verify_failed", str(exc)) from exc
    return "materialized"


def _write_manifest_noreplace(path: Path, payload: bytes) -> None:
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_file():
            raise EmailAttachmentError("collision", "manifest_destination_collision")
        try:
            if path.read_bytes() == payload:
                return
        except OSError as exc:
            raise EmailAttachmentError("blocked", "manifest_replay_read_failed", str(exc)) from exc
        raise EmailAttachmentError("collision", "manifest_destination_collision")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".email-manifest-", dir=os.fspath(path.parent))
        temporary = Path(name)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard-link publication is no-replace on the same filesystem.
        os.link(temporary, path)
        temporary.unlink()
    except FileExistsError as exc:
        raise EmailAttachmentError("collision", "manifest_destination_collision") from exc
    except OSError as exc:
        raise EmailAttachmentError("blocked", "manifest_write_failed", str(exc)) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_manifest(path: Path, *, parent_fd: int | None = None) -> bytes:
    """Read a manifest through a pinned directory when it is route-local."""

    try:
        if parent_fd is None:
            return path.read_bytes()
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            return stream.read()
    except OSError as exc:
        raise EmailAttachmentError("blocked", "manifest_read_failed", str(exc)) from exc


def _replace_owned_manifest(path: Path, payload: bytes, expected_current: bytes) -> None:
    """Atomically advance a manifest previously written by this owner."""

    try:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected_current:
            raise EmailAttachmentError("collision", "manifest_owner_fence_failed")
        descriptor, name = tempfile.mkstemp(prefix=".email-manifest-", dir=os.fspath(path.parent))
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("blocked", "manifest_replace_failed", str(exc)) from exc


def _manifest_from_dict(value: object) -> EmailAttachmentManifest:
    if not isinstance(value, dict) or value.get("schema") != EMAIL_ATTACHMENT_SCHEMA:
        raise EmailAttachmentError("corrupt", "manifest_schema_invalid")
    identity = value.get("parent_identity")
    if not isinstance(identity, dict):
        raise EmailAttachmentError("corrupt", "manifest_parent_identity_missing")
    try:
        parent_identity = EmailSourceIdentity(**identity)
        attachments = tuple(
            EmailAttachment(**item)
            for item in value.get("attachments", ())
            if isinstance(item, dict)
        )
        return EmailAttachmentManifest(
            schema=str(value["schema"]),
            status=str(value["status"]),
            parent_path=str(value["parent_path"]),
            parent_identity=parent_identity,
            parent_sha256=str(value["parent_sha256"]),
            destination=str(value["destination"]),
            manifest_path=str(value["manifest_path"]),
            attachments=attachments,
            reason=None if value.get("reason") is None else str(value["reason"]),
            detail=None if value.get("detail") is None else str(value["detail"]),
            parent_physical_key=(
                None
                if value.get("parent_physical_key") is None
                else str(value["parent_physical_key"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EmailAttachmentError("corrupt", "manifest_shape_invalid") from exc


def _attachment_manifest_fingerprint(item: EmailAttachment) -> tuple[object, ...]:
    """Fields that must agree with a freshly parsed parent on replay."""

    return (
        item.ordinal,
        item.part_path,
        item.filename,
        item.media_type,
        item.content_disposition,
        item.content_id,
        item.size,
        item.sha256,
        item.child_path,
    )


def _attachment_lineage_fingerprint(item: EmailAttachment) -> tuple[object, ...]:
    return (
        item.ordinal,
        item.part_path,
        item.filename,
        item.media_type,
        item.content_disposition,
        item.content_id,
        item.size,
        item.sha256,
    )


def _resolve_relocated_children(
    existing: EmailAttachmentManifest,
    planned: EmailAttachmentManifest,
    *,
    resolver: Callable[
        [EmailAttachment],
        str | os.PathLike[str] | EmailAttachmentResolution | None,
    ] | None,
    allow_content_equivalent: bool,
    destination: Path,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[EmailAttachment, ...] | None:
    """Resolve moved children through a bounded, caller-owned inventory seam."""

    if len(existing.attachments) != len(planned.attachments):
        raise EmailAttachmentError("corrupt", "manifest_attachment_count_mismatch")
    if tuple(_attachment_lineage_fingerprint(item) for item in existing.attachments) != tuple(
        _attachment_lineage_fingerprint(item) for item in planned.attachments
    ):
        raise EmailAttachmentError("corrupt", "manifest_attachment_lineage_mismatch")
    if resolver is None:
        return None
    resolved: list[EmailAttachment] = []
    seen: set[tuple[int, int]] = set()
    for old, fresh in zip(existing.attachments, planned.attachments, strict=True):
        try:
            candidate_value = resolver(old)
        except (KeyboardInterrupt, SystemExit):
            raise
        except (CancellationRequested, RunBudgetExceeded):
            raise
        except Exception as exc:
            raise EmailAttachmentError("blocked", "child_resolver_failed", type(exc).__name__) from exc
        if candidate_value is None:
            raise EmailAttachmentError("corrupt", "child_resolver_missing")
        if isinstance(candidate_value, EmailAttachmentResolution):
            reuse_kind = candidate_value.reuse_kind
            candidate = None if candidate_value.path is None else Path(candidate_value.path)
        else:
            reuse_kind = "identity"
            candidate = Path(candidate_value)
        if reuse_kind == "consumed_archive":
            provenance = candidate_value.provenance if isinstance(candidate_value, EmailAttachmentResolution) else None
            if not isinstance(provenance, Mapping):
                raise EmailAttachmentError("recovery_required", "consumed_archive_receipt_missing")
            if (
                provenance.get("status") != "applied"
                or provenance.get("published") is not True
                or provenance.get("trashed") is not True
                or provenance.get("source_sha256") != fresh.sha256
            ):
                raise EmailAttachmentError("recovery_required", "consumed_archive_receipt_mismatch")
            source_identity = provenance.get("source_identity")
            if not isinstance(source_identity, Mapping):
                raise EmailAttachmentError("recovery_required", "consumed_archive_identity_missing")
            required_identity = ("path", "device", "inode", "size", "mtime_ns", "ctime_ns", "nlink")
            if (
                any(field not in source_identity for field in required_identity)
                or not isinstance(source_identity.get("path"), str)
                or not Path(source_identity["path"]).is_absolute()
                or source_identity.get("nlink") != 1
                or any(
                    type(source_identity.get(field)) is not int
                    or int(source_identity[field]) < 0
                    for field in required_identity[1:]
                )
            ):
                raise EmailAttachmentError("recovery_required", "consumed_archive_identity_invalid")
            if (
                old.child_device is None
                or old.child_inode is None
                or old.child_mtime_ns is None
                or source_identity.get("device") != old.child_device
                or source_identity.get("inode") != old.child_inode
                or source_identity.get("size") != old.size
                or source_identity.get("mtime_ns") != old.child_mtime_ns
            ):
                raise EmailAttachmentError("recovery_required", "consumed_archive_identity_mismatch")
            raw_successors = provenance.get("successor_paths", ())
            if not isinstance(raw_successors, (list, tuple)) or any(
                not isinstance(path, str) or not Path(path).is_absolute()
                for path in raw_successors
            ):
                raise EmailAttachmentError("recovery_required", "consumed_archive_successors_invalid")
            resolved.append(
                replace(
                    fresh,
                    child_path=old.child_path,
                    status="consumed_archive",
                    historical_successor_paths=tuple(raw_successors),
                    child_device=old.child_device,
                    child_inode=old.child_inode,
                    child_mtime_ns=old.child_mtime_ns,
                    child_reuse_kind="consumed_archive",
                    previous_child_device=old.child_device,
                    previous_child_inode=old.child_inode,
                    previous_child_mtime_ns=old.child_mtime_ns,
                )
            )
            continue
        if candidate is None:
            raise EmailAttachmentError("unsafe", "child_resolver_path_missing")
        if reuse_kind not in {"identity", "content_equivalent"}:
            raise EmailAttachmentError("unsafe", "child_resolver_reuse_kind_invalid")
        if reuse_kind == "content_equivalent" and not allow_content_equivalent:
            raise EmailAttachmentError("unsafe", "content_equivalent_reuse_not_authorized")
        if not candidate.is_absolute():
            raise EmailAttachmentError("unsafe", "child_resolver_path_not_absolute")
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise EmailAttachmentError("corrupt", "child_resolver_child_missing", str(exc)) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise EmailAttachmentError("unsafe", "child_resolver_child_not_regular")
        physical = (int(metadata.st_dev), int(metadata.st_ino))
        if physical in seen:
            raise EmailAttachmentError("corrupt", "child_resolver_duplicate_identity")
        seen.add(physical)
        if reuse_kind == "identity" and old.child_device is not None and (
            int(metadata.st_dev) != old.child_device
            or int(metadata.st_ino) != old.child_inode
            or int(metadata.st_size) != old.size
            or int(metadata.st_mtime_ns) != old.child_mtime_ns
        ):
            raise EmailAttachmentError("corrupt", "child_resolver_identity_mismatch")
        _verify_child(
            candidate,
            size=fresh.size,
            digest=fresh.sha256,
            checkpoint=checkpoint,
        )
        resolved.append(
            replace(
                fresh,
                child_path=os.fspath(candidate),
                status="replayed",
                child_device=int(metadata.st_dev),
                child_inode=int(metadata.st_ino),
                child_mtime_ns=int(metadata.st_mtime_ns),
                child_reuse_kind=reuse_kind,
                previous_child_device=old.child_device,
                previous_child_inode=old.child_inode,
                previous_child_mtime_ns=old.child_mtime_ns,
            )
        )
    return tuple(resolved)


def _validate_replay_manifest(
    existing: EmailAttachmentManifest,
    planned: EmailAttachmentManifest,
    destination: Path,
    manifest_file: Path,
    *,
    directory_fd: int | None = None,
    resolved_children: tuple[EmailAttachment, ...] | None = None,
) -> None:
    """Reject stale/edited manifests before opening any child path."""

    if existing.manifest_path != os.fspath(manifest_file):
        raise EmailAttachmentError("corrupt", "manifest_path_mismatch")
    if existing.destination != os.fspath(destination):
        raise EmailAttachmentError("collision", "manifest_parent_collision")
    if len(existing.attachments) != len(planned.attachments):
        raise EmailAttachmentError("corrupt", "manifest_attachment_count_mismatch")
    fingerprint = (
        _attachment_manifest_fingerprint
        if resolved_children is None else _attachment_lineage_fingerprint
    )
    expected = tuple(fingerprint(item) for item in planned.attachments)
    observed = tuple(fingerprint(item) for item in existing.attachments)
    if observed != expected:
        raise EmailAttachmentError("corrupt", "manifest_attachment_lineage_mismatch")
    if resolved_children is None:
        expected_names = {Path(item.child_path or "").name for item in planned.attachments}
    else:
        # The trusted resolver has already verified every child's identity and
        # bytes (or canonical historical consumption). Only its currently local
        # children belong in this directory; old basenames are not authority.
        expected_names = {
            Path(item.child_path or "").name for item in resolved_children
            if item.child_reuse_kind != "consumed_archive"
            and Path(item.child_path or "").parent == destination
        }
    # This directory is dedicated to one parent.  Ignore only the manifest;
    # any other entry is an unbound/foreign child and invalidates replay.
    try:
        if directory_fd is None:
            names = {
                child.name
                for child in destination.iterdir()
                if child.name != manifest_file.name
            }
        else:
            duplicate = os.dup(directory_fd)
            try:
                with os.scandir(duplicate) as entries:
                    names = {
                        child.name
                        for child in entries
                        if child.name != manifest_file.name
                    }
            finally:
                os.close(duplicate)
    except OSError as exc:
        raise EmailAttachmentError("blocked", "manifest_children_list_failed", str(exc)) from exc
    historical_names = set()
    for child in resolved_children or ():
        for value in child.historical_successor_paths:
            successor = Path(os.path.abspath(value))
            if successor.parent != destination or successor.name not in names:
                continue
            try:
                metadata = (
                    successor.lstat() if directory_fd is None
                    else os.stat(successor.name, dir_fd=directory_fd, follow_symlinks=False)
                )
            except OSError as exc:
                raise EmailAttachmentError("source_changed", "historical_successor_changed") from exc
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise EmailAttachmentError("unsafe", "historical_successor_not_directory")
            # This is only a known historical publication root, not evidence
            # that any of its current children have been verified or indexed.
            historical_names.add(successor.name)
    if names - historical_names != expected_names:
        raise EmailAttachmentError("corrupt", "manifest_extra_or_missing_child")


def _verify_child(
    path: Path,
    *,
    size: int,
    digest: str,
    parent_fd: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    name = path.name
    try:
        if parent_fd is None:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise EmailAttachmentError("unsafe", "manifest_child_not_regular")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        else:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise EmailAttachmentError("unsafe", "manifest_child_not_regular")
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            hasher = hashlib.sha256()
            while chunk := stream.read(_EMAIL_READ_CHUNK_BYTES):
                if checkpoint is not None:
                    checkpoint()
                hasher.update(chunk)
            actual_digest = hasher.hexdigest()
    except FileNotFoundError as exc:
        raise EmailAttachmentError("corrupt", "manifest_child_missing") from exc
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("blocked", "manifest_child_read_failed", str(exc)) from exc
    if int(metadata.st_size) != int(size) or actual_digest != digest:
        raise EmailAttachmentError("corrupt", "manifest_child_integrity_failed")


def _child_identity(path: Path, *, parent_fd: int | None = None) -> tuple[int, int, int, int]:
    try:
        metadata = (
            path.lstat()
            if parent_fd is None
            else os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        )
    except OSError as exc:
        raise EmailAttachmentError("blocked", "child_identity_unavailable", str(exc)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EmailAttachmentError("unsafe", "child_not_regular")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def materialize_email_attachments(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    apply: bool = False,
    manifest_path: str | os.PathLike[str] | None = None,
    limits: EmailAttachmentLimits | None = None,
    resolver: Callable[
        [EmailAttachment],
        str | os.PathLike[str] | EmailAttachmentResolution | None,
    ] | None = None,
    allow_content_equivalent: bool = False,
    prepared: PreparedEmailAttachments | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> EmailAttachmentManifest:
    """Plan or materialize attachment children without touching the parent.

    The same destination/manifest and unchanged parent are an idempotent
    replay.  Any pre-existing child with different bytes is a collision and is
    never overwritten.  Callers should pass ``apply=True`` only after their
    normal route gate has authorized physical child creation.
    """

    effective = EmailAttachmentLimits() if limits is None else limits
    effective.validate()
    source_path = Path(source)
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        raise EmailAttachmentError("unsafe", "destination_invalid")
    # Do not create the final directory until a complete staged tree is ready.
    destination_exists = destination_path.exists() or destination_path.is_symlink()
    if destination_exists:
        _ensure_directory(destination_path, create=False)
    manifest_file = (
        Path(manifest_path)
        if manifest_path is not None
        else destination_path / ".neocortex-email-attachments.json"
    )
    if not manifest_file.is_absolute():
        raise EmailAttachmentError("unsafe", "manifest_path_not_absolute")
    if manifest_file.parent != destination_path:
        _ensure_directory(manifest_file.parent, create=apply)
    if prepared is None:
        prepared = prepare_email_attachments(
            source_path,
            limits=effective,
            checkpoint=checkpoint,
        )
    elif prepared.source_path != source_path:
        raise EmailAttachmentError("source_changed", "prepared_parent_path_mismatch")
    else:
        if prepared.limits != effective:
            raise EmailAttachmentError("budget", "prepared_limits_mismatch")
        prepared.assert_current()
    identity = prepared.parent_identity
    parent_sha256 = prepared.parent_sha256
    descriptors = prepared.attachments
    planned_children: list[EmailAttachment] = []
    for item in descriptors:
        child_name = _child_name(item.ordinal, item.filename)
        planned_children.append(
            EmailAttachment(
                ordinal=item.ordinal,
                part_path=item.part_path,
                filename=item.filename,
                media_type=item.media_type,
                content_disposition=item.content_disposition,
                content_id=item.content_id,
                size=item.size,
                sha256=item.sha256,
                child_path=os.fspath(destination_path / child_name),
                status="planned",
            )
        )
    planned = EmailAttachmentManifest(
        EMAIL_ATTACHMENT_SCHEMA,
        "planned",
        os.fspath(source_path),
        identity,
        parent_sha256,
        os.fspath(destination_path),
        os.fspath(manifest_file),
        tuple(planned_children),
        reason="attachments_discovered",
    )
    if not apply:
        return planned
    if not identity.matches(source_path):
        raise EmailAttachmentError("source_changed", "parent_identity_changed_before_publish")

    # C02 may leave the attachment directory empty/absent after moving or
    # deduping children.  A state manifest plus a trusted inventory resolver is
    # sufficient to replay the content without creating a second copy.
    if not destination_exists and resolver is not None and manifest_file.exists():
        try:
            existing_manifest = _manifest_from_dict(
                json.loads(_read_manifest(manifest_file).decode("utf-8"))
            )
        except EmailAttachmentError:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            raise EmailAttachmentError("corrupt", "manifest_read_failed", str(exc)) from exc
        if (
            not existing_manifest.parent_identity.same_physical_content(identity)
            or existing_manifest.parent_sha256 != parent_sha256
            or existing_manifest.status not in {"applied", "replayed", "staging"}
        ):
            raise EmailAttachmentError("collision", "manifest_parent_collision")
        rebound = _resolve_relocated_children(
            existing_manifest,
            planned,
            resolver=resolver,
            destination=destination_path,
            allow_content_equivalent=allow_content_equivalent,
            checkpoint=checkpoint,
        )
        if rebound is not None:
            replayed = EmailAttachmentManifest(
                EMAIL_ATTACHMENT_SCHEMA,
                "replayed",
                os.fspath(source_path),
                identity,
                parent_sha256,
                os.fspath(destination_path),
                os.fspath(manifest_file),
                rebound,
                reason="replay_rebound_without_destination",
            )
            current = _read_manifest(manifest_file)
            encoded = (json.dumps(replayed.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            _replace_owned_manifest(manifest_file, encoded, current)
            return replayed

    # Existing destination + complete manifest is replay.  Pin the directory
    # before inspecting/reading children; mutable manifest paths cannot redirect
    # the verifier outside this directory.
    if destination_exists:
        destination_fd, destination_identity = _open_pinned_directory(destination_path, create=False)
        try:
            if not _pinned_directory_matches(destination_path, destination_identity):
                raise EmailAttachmentError("source_changed", "destination_identity_changed")
            if not manifest_file.exists() and not manifest_file.is_symlink():
                raise EmailAttachmentError("collision", "destination_requires_manifest")
            if manifest_file.is_symlink():
                raise EmailAttachmentError("unsafe", "manifest_symlink")
            try:
                existing_manifest = _manifest_from_dict(
                    json.loads(
                        _read_manifest(
                            manifest_file,
                            parent_fd=(destination_fd if manifest_file.parent == destination_path else None),
                        ).decode("utf-8")
                    )
                )
            except EmailAttachmentError:
                raise
            except (OSError, ValueError, UnicodeError) as exc:
                raise EmailAttachmentError("corrupt", "manifest_read_failed", str(exc)) from exc
            if (
                not existing_manifest.parent_identity.same_physical_content(identity)
                or existing_manifest.parent_sha256 != parent_sha256
            ):
                raise EmailAttachmentError("collision", "manifest_parent_collision")
            if existing_manifest.status not in {"applied", "replayed", "staging"}:
                raise EmailAttachmentError("corrupt", "manifest_not_terminal")
            rebound = _resolve_relocated_children(
                existing_manifest,
                planned,
                resolver=resolver,
                destination=destination_path,
                allow_content_equivalent=allow_content_equivalent,
                checkpoint=checkpoint,
            )
            if rebound is None:
                if existing_manifest.destination != os.fspath(destination_path):
                    raise EmailAttachmentError("collision", "manifest_parent_collision")
                _validate_replay_manifest(
                    existing_manifest,
                    planned,
                    destination_path,
                    manifest_file,
                    directory_fd=destination_fd,
                )
                for child in planned.attachments:
                    _verify_child(
                        Path(child.child_path or ""),
                        size=child.size,
                        digest=child.sha256,
                        parent_fd=destination_fd,
                        checkpoint=checkpoint,
                    )
                replay_children = existing_manifest.attachments
            else:
                _validate_replay_manifest(
                    existing_manifest,
                    planned,
                    destination_path,
                    manifest_file,
                    directory_fd=destination_fd,
                    resolved_children=rebound,
                )
                for child in rebound:
                    if child.child_reuse_kind == "consumed_archive":
                        continue
                    _verify_child(
                        Path(child.child_path or ""),
                        size=child.size,
                        digest=child.sha256,
                        parent_fd=(
                            destination_fd
                            if Path(child.child_path or "").parent == destination_path
                            else None
                        ),
                        checkpoint=checkpoint,
                    )
                replay_children = rebound
            if rebound is not None or not existing_manifest.parent_identity == identity:
                replayed = EmailAttachmentManifest(
                    EMAIL_ATTACHMENT_SCHEMA,
                    "replayed",
                    os.fspath(source_path),
                    identity,
                    parent_sha256,
                    os.fspath(destination_path),
                    os.fspath(manifest_file),
                    replay_children,
                    reason="replay_rebound",
                )
                current = _read_manifest(
                    manifest_file,
                    parent_fd=(destination_fd if manifest_file.parent == destination_path else None),
                )
                encoded = (json.dumps(replayed.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                _replace_owned_manifest(manifest_file, encoded, current)
                return replayed
            if existing_manifest.status == "staging":
                if manifest_file.parent == destination_path:
                    raise EmailAttachmentError("corrupt", "local_manifest_not_terminal")
                recovered_children = tuple(
                    replace(item, status="materialized")
                    for item in replay_children
                )
                recovered = EmailAttachmentManifest(
                    EMAIL_ATTACHMENT_SCHEMA,
                    "applied",
                    os.fspath(source_path),
                    identity,
                    parent_sha256,
                    os.fspath(destination_path),
                    os.fspath(manifest_file),
                    recovered_children,
                    reason="staging_intent_recovered",
                )
                current = _read_manifest(manifest_file)
                encoded = (json.dumps(recovered.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                _replace_owned_manifest(manifest_file, encoded, current)
                return recovered
            return EmailAttachmentManifest(
                existing_manifest.schema,
                "replayed",
                existing_manifest.parent_path,
                existing_manifest.parent_identity,
                existing_manifest.parent_sha256,
                existing_manifest.destination,
                existing_manifest.manifest_path,
                existing_manifest.attachments,
                reason="replay_verified",
            )
        finally:
            os.close(destination_fd)

    # First application is assembled in a private sibling and atomically
    # published as one directory.  A source drift or interruption therefore
    # cannot leave unrecorded children in the final route tree.
    stage: Path | None = None
    stage_fd: int | None = None
    destination_parent_fd: int | None = None
    external_intent: bytes | None = None
    try:
        destination_parent = destination_path.parent
        _ensure_directory(destination_parent, create=False)
        destination_parent_fd, destination_parent_identity = _open_pinned_directory(
            destination_parent,
            create=False,
        )
        if manifest_file.parent != destination_path:
            intent = EmailAttachmentManifest(
                EMAIL_ATTACHMENT_SCHEMA,
                "staging",
                os.fspath(source_path),
                identity,
                parent_sha256,
                os.fspath(destination_path),
                os.fspath(manifest_file),
                tuple(planned_children),
                reason="staging_intent",
            )
            external_intent = (json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            if manifest_file.exists() or manifest_file.is_symlink():
                if manifest_file.is_symlink():
                    raise EmailAttachmentError("unsafe", "manifest_symlink")
                try:
                    old = _manifest_from_dict(json.loads(_read_manifest(manifest_file).decode("utf-8")))
                except (OSError, ValueError, UnicodeError) as exc:
                    raise EmailAttachmentError("corrupt", "manifest_read_failed", str(exc)) from exc
                if old.status != "staging" or old.parent_sha256 != parent_sha256:
                    raise EmailAttachmentError("collision", "manifest_parent_collision")
                _replace_owned_manifest(manifest_file, external_intent, _read_manifest(manifest_file))
            else:
                _write_manifest_noreplace(manifest_file, external_intent)
        stage = Path(tempfile.mkdtemp(prefix=f".{destination_path.name}.stage-", dir=os.fspath(destination_parent)))
        os.chmod(stage, 0o700)
        stage_fd, stage_identity = _open_pinned_directory(stage, create=False)
        if not _pinned_directory_matches(stage, stage_identity):
            raise EmailAttachmentError("source_changed", "stage_identity_changed")
        materialized: list[EmailAttachment] = []
        for planned_child, descriptor in zip(planned_children, descriptors, strict=True):
            stage_child = stage / Path(planned_child.child_path or "").name
            status = _write_child(
                stage_child,
                descriptor.payload,
                descriptor.sha256,
                parent_fd=stage_fd,
                checkpoint=checkpoint,
            )
            child_device, child_inode, _child_size, child_mtime_ns = _child_identity(
                stage_child,
                parent_fd=stage_fd,
            )
            materialized.append(
                replace(
                    planned_child,
                    status=status,
                    child_device=child_device,
                    child_inode=child_inode,
                    child_mtime_ns=child_mtime_ns,
                )
            )
        if not identity.matches(source_path):
            raise EmailAttachmentError("source_changed", "parent_identity_changed_after_stage")
        applied = EmailAttachmentManifest(
            EMAIL_ATTACHMENT_SCHEMA,
            "applied",
            os.fspath(source_path),
            identity,
            parent_sha256,
            os.fspath(destination_path),
            os.fspath(manifest_file),
            tuple(materialized),
            reason="attachments_materialized",
        )
        encoded = (json.dumps(applied.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        # The default manifest travels with the atomically published tree.  A
        # caller-owned state manifest is written only after the tree is safely
        # visible; callers can recover from a durable destination without any
        # parent deletion or child overwrite.
        if manifest_file.parent == destination_path:
            _write_manifest_noreplace(stage / manifest_file.name, encoded)
        os.close(stage_fd)
        stage_fd = None
        _rename_directory_noreplace(
            stage,
            destination_path,
            parent_fd=destination_parent_fd,
            parent_identity=destination_parent_identity,
        )
        stage = None
        if manifest_file.parent != destination_path:
            if external_intent is None:
                raise EmailAttachmentError("dependency", "manifest_intent_missing")
            _replace_owned_manifest(manifest_file, encoded, external_intent)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)
            destination_parent_fd = None
        return applied
    except BaseException:
        if stage_fd is not None:
            try:
                os.close(stage_fd)
            except OSError:
                pass
        if destination_parent_fd is not None:
            try:
                os.close(destination_parent_fd)
            except OSError:
                pass
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise


__all__ = (
    "EMAIL_ATTACHMENT_SCHEMA",
    "EmailAttachment",
    "EmailAttachmentDescriptor",
    "EmailAttachmentError",
    "EmailAttachmentLimits",
    "EmailAttachmentManifest",
    "EmailAttachmentResolution",
    "EmailSourceIdentity",
    "PreparedEmailAttachments",
    "describe_email_attachments",
    "iter_email_attachments",
    "materialize_email_attachments",
    "prepare_email_attachments",
    "stable_attachment_root",
)
