"""Durable, bounded checkpoints for resumable Linux inventory scans.

The checkpoint is a small state manifest kept outside the scanned root.  It
contains only root identity, policy, counters, relative cursor and digests of
the committed prefix; file payloads and corpus instructions are never stored.
Writes are atomic and crash-safe, while resume validates the root, policy and
already committed prefix before accepting any new row.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


INVENTORY_RESUME_SCHEMA = "neocortex.inventory-resume/v1"
INVENTORY_RESUME_SCHEMA_VERSION = 1
MAX_RESUME_CHECKPOINT_BYTES = 64 * 1024
MAX_RESUME_CURSOR_BYTES = 4 * 1024
MAX_RESUME_POLICY_BYTES = 4 * 1024
MAX_SORTED_DIRECTORY_ENTRIES = 10_000

CheckpointState = Literal["building", "partial", "complete"]
_CHECKPOINT_KEYS = frozenset(
    {
        "batch_digest",
        "batch_files",
        "batch_index",
        "counters",
        "directory_digest",
        "last_relative_cursor",
        "policy_signature",
        "prefix_digest",
        "root",
        "scan_id",
        "schema",
        "schema_version",
        "status",
        "stop_reason",
    }
)
_COUNTER_KEYS = frozenset(
    {
        "bytes_seen",
        "directories_seen",
        "errors",
        "excluded_directories",
        "files_seen",
        "skipped_links",
    }
)
_ROOT_KEYS = frozenset({"birthtime_ns", "dev", "inode", "path"})
_PREFIX_DOMAIN = b"neocortex-inventory-prefix-v1\0"
_BATCH_DOMAIN = b"neocortex-inventory-batch-v1\0"
_DIRECTORY_DOMAIN = b"neocortex-inventory-directory-v1\0"
_OBSERVATION_DOMAIN = "neocortex-inventory-observation-v1"


class _Observation(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def birthtime_ns(self) -> int: ...

    @property
    def file_id(self) -> int: ...

    @property
    def mtime_ns(self) -> int: ...

    @property
    def size(self) -> int: ...


class InventoryResumeError(ValueError):
    """The requested checkpoint cannot be trusted or safely resumed."""


class InventoryResumeCorruptError(InventoryResumeError):
    """Checkpoint bytes are malformed, truncated or non-canonical."""


class InventoryResumeConflictError(InventoryResumeError):
    """The checkpoint conflicts with the current root or scan owner."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            # ``os.fsencode`` permits undecodable Linux filenames through the
            # cursor contract; ASCII escaping keeps those surrogate code
            # points valid UTF-8 checkpoint bytes.
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise InventoryResumeError("inventory resume checkpoint is not canonical JSON") from exc


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _validate_digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise InventoryResumeCorruptError(f"{label} is not a SHA-256 digest")
    return value


def _nonnegative(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InventoryResumeCorruptError(f"{label} is not a non-negative integer")
    return value


def _root_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise InventoryResumeCorruptError(f"{label} must be an absolute path")
    normalized = os.path.normpath(value)
    if normalized != value:
        raise InventoryResumeCorruptError(f"{label} is not canonical")
    return value


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise InventoryResumeCorruptError("last_relative_cursor is invalid")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_RESUME_CURSOR_BYTES:
        raise InventoryResumeCorruptError("last_relative_cursor exceeds its bound")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise InventoryResumeCorruptError("last_relative_cursor is not normalized")
    return value


def dfs_order_key(root: str | Path, path: str | Path) -> bytes:
    """Return a bytewise key whose order is the deterministic DFS order.

    A NUL terminator is safe for Linux path components and sorts descendants
    of a directory before the next sibling (``a/z`` precedes ``a.txt``), while
    preserving the byte ordering used by the directory iterator.
    """

    relative = relative_cursor(root, path)
    return b"".join(os.fsencode(part) + b"\0" for part in relative.split("/"))


def empty_batch_digest() -> str:
    return _digest(_BATCH_DOMAIN)


def _policy(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > MAX_RESUME_POLICY_BYTES
    ):
        raise InventoryResumeCorruptError("policy_signature is invalid")
    return value


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise InventoryResumeCorruptError(f"{label} must be an object")
    return value


def _exact(mapping: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    if set(mapping) != expected:
        raise InventoryResumeCorruptError(f"{label} has unknown or missing fields")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryResumeCorruptError(f"duplicate checkpoint key: {key}")
        result[key] = value
    return result


def _validate_root(value: object) -> tuple[str, int, int, int]:
    mapping = _mapping(value, label="root")
    _exact(mapping, _ROOT_KEYS, label="root")
    birth = mapping["birthtime_ns"]
    if isinstance(birth, bool) or not isinstance(birth, int) or birth < -1:
        raise InventoryResumeCorruptError("root.birthtime_ns is invalid")
    return (
        _root_path(mapping["path"], label="root.path"),
        _nonnegative(mapping["dev"], label="root.dev"),
        _nonnegative(mapping["inode"], label="root.inode"),
        birth,
    )


def _validate_counters(value: object) -> tuple[int, int, int, int, int, int]:
    mapping = _mapping(value, label="counters")
    _exact(mapping, _COUNTER_KEYS, label="counters")
    return tuple(
        _nonnegative(mapping[key], label=f"counters.{key}")
        for key in (
            "files_seen",
            "directories_seen",
            "bytes_seen",
            "skipped_links",
            "excluded_directories",
            "errors",
        )
    )  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class InventoryResumeCheckpoint:
    """One immutable snapshot of the last committed DFS prefix."""

    root_path: str
    root_dev: int
    root_inode: int
    root_birthtime_ns: int
    policy_signature: str
    scan_id: int
    last_relative_cursor: str | None
    batch_digest: str
    batch_files: int
    prefix_digest: str
    directory_digest: str
    batch_index: int
    files_seen: int
    directories_seen: int
    bytes_seen: int
    skipped_links: int
    excluded_directories: int
    errors: int
    status: CheckpointState
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        _root_path(self.root_path, label="root_path")
        for label, value in (
            ("root_dev", self.root_dev),
            ("root_inode", self.root_inode),
            ("scan_id", self.scan_id),
            ("batch_index", self.batch_index),
            ("batch_files", self.batch_files),
            ("files_seen", self.files_seen),
            ("directories_seen", self.directories_seen),
            ("bytes_seen", self.bytes_seen),
            ("skipped_links", self.skipped_links),
            ("excluded_directories", self.excluded_directories),
            ("errors", self.errors),
        ):
            _nonnegative(value, label=label)
        if (
            isinstance(self.root_birthtime_ns, bool)
            or not isinstance(self.root_birthtime_ns, int)
            or self.root_birthtime_ns < -1
        ):
            raise InventoryResumeError("root_birthtime_ns is invalid")
        if self.scan_id < 1:
            raise InventoryResumeError("scan_id must be positive")
        _policy(self.policy_signature)
        _cursor(self.last_relative_cursor)
        _validate_digest(self.batch_digest, label="batch_digest")
        _validate_digest(self.prefix_digest, label="prefix_digest")
        _validate_digest(self.directory_digest, label="directory_digest")
        if self.batch_files == 0 and self.batch_digest != empty_batch_digest():
            raise InventoryResumeError("empty batch has a non-empty batch digest")
        if self.batch_files > 0 and self.last_relative_cursor is None:
            raise InventoryResumeError("a non-empty batch has no cursor")
        if not isinstance(self.status, str) or self.status not in {
            "building",
            "partial",
            "complete",
        }:
            raise InventoryResumeError("checkpoint status is invalid")
        if self.stop_reason is not None:
            if not isinstance(self.stop_reason, str) or not self.stop_reason.strip():
                raise InventoryResumeError("stop_reason is invalid")
            if len(self.stop_reason.encode("utf-8")) > 256:
                raise InventoryResumeError("stop_reason exceeds its bound")

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_digest": self.batch_digest,
            "batch_files": self.batch_files,
            "batch_index": self.batch_index,
            "counters": {
                "bytes_seen": self.bytes_seen,
                "directories_seen": self.directories_seen,
                "errors": self.errors,
                "excluded_directories": self.excluded_directories,
                "files_seen": self.files_seen,
                "skipped_links": self.skipped_links,
            },
            "directory_digest": self.directory_digest,
            "last_relative_cursor": self.last_relative_cursor,
            "policy_signature": self.policy_signature,
            "prefix_digest": self.prefix_digest,
            "root": {
                "birthtime_ns": self.root_birthtime_ns,
                "dev": self.root_dev,
                "inode": self.root_inode,
                "path": self.root_path,
            },
            "scan_id": self.scan_id,
            "schema": INVENTORY_RESUME_SCHEMA,
            "schema_version": INVENTORY_RESUME_SCHEMA_VERSION,
            "status": self.status,
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "InventoryResumeCheckpoint":
        mapping = _mapping(value, label="checkpoint")
        _exact(mapping, _CHECKPOINT_KEYS, label="checkpoint")
        if (
            mapping["schema"] != INVENTORY_RESUME_SCHEMA
            or isinstance(mapping["schema_version"], bool)
            or mapping["schema_version"] != 1
        ):
            raise InventoryResumeCorruptError("checkpoint schema is unsupported")
        root_path, root_dev, root_inode, birth = _validate_root(mapping["root"])
        counters = _validate_counters(mapping["counters"])
        status = mapping["status"]
        if status not in {"building", "partial", "complete"}:
            raise InventoryResumeCorruptError("checkpoint status is invalid")
        stop_reason = mapping["stop_reason"]
        if stop_reason is not None and not isinstance(stop_reason, str):
            raise InventoryResumeCorruptError("checkpoint stop_reason is invalid")
        return cls(
            root_path=root_path,
            root_dev=root_dev,
            root_inode=root_inode,
            root_birthtime_ns=birth,
            policy_signature=_policy(mapping["policy_signature"]),
            scan_id=_nonnegative(mapping["scan_id"], label="scan_id"),
            last_relative_cursor=_cursor(mapping["last_relative_cursor"]),
            batch_digest=_validate_digest(mapping["batch_digest"], label="batch_digest"),
            batch_files=_nonnegative(mapping["batch_files"], label="batch_files"),
            prefix_digest=_validate_digest(mapping["prefix_digest"], label="prefix_digest"),
            directory_digest=_validate_digest(
                mapping["directory_digest"], label="directory_digest"
            ),
            batch_index=_nonnegative(mapping["batch_index"], label="batch_index"),
            files_seen=counters[0],
            directories_seen=counters[1],
            bytes_seen=counters[2],
            skipped_links=counters[3],
            excluded_directories=counters[4],
            errors=counters[5],
            status=status,  # type: ignore[arg-type]
            stop_reason=stop_reason,
        )


def relative_cursor(root: str | Path, path: str | Path) -> str:
    """Return one normalized relative path suitable for a resume cursor."""

    root_path = Path(root)
    candidate = Path(path)
    try:
        relative = candidate.relative_to(root_path).as_posix()
    except ValueError as exc:
        raise InventoryResumeConflictError("inventory cursor escapes its root") from exc
    if _cursor(relative) is None:
        raise InventoryResumeConflictError("inventory cursor is empty")
    return relative


def _observation_bytes(root: str | Path, observation: _Observation) -> bytes:
    relative = relative_cursor(root, observation.path)
    payload = {
        "birthtime_ns": int(observation.birthtime_ns),
        "file_id": int(observation.file_id),
        "mtime_ns": int(observation.mtime_ns),
        "path": relative,
        "size": int(observation.size),
        "type": _OBSERVATION_DOMAIN,
    }
    return _canonical(payload)


def empty_prefix_digest() -> str:
    return _digest(_PREFIX_DOMAIN)


@dataclass(slots=True)
class InventoryPrefixDigest:
    """Incremental digest of committed observations in traversal order."""

    root: str
    value: str = ""

    def __post_init__(self) -> None:
        self.root = _root_path(self.root, label="prefix.root")
        if not self.value:
            self.value = empty_prefix_digest()
        else:
            _validate_digest(self.value, label="prefix_digest")

    def observe(self, observation: _Observation) -> None:
        payload = _observation_bytes(self.root, observation)
        digest = hashlib.sha256()
        digest.update(_PREFIX_DOMAIN)
        digest.update(self.value.encode("ascii"))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        self.value = _digest(digest.digest())


@dataclass(slots=True)
class InventoryDirectoryDigest:
    """Incremental identity digest for directories opened by deterministic DFS."""

    root: str
    value: str = ""

    def __post_init__(self) -> None:
        self.root = _root_path(self.root, label="directory.root")
        if not self.value:
            self.value = _digest(_DIRECTORY_DOMAIN)
        else:
            _validate_digest(self.value, label="directory_digest")

    def observe(self, path: str | Path, *, dev: int, inode: int, birthtime_ns: int) -> None:
        absolute = os.path.abspath(os.fspath(path))
        relative = "" if absolute == self.root else relative_cursor(self.root, absolute)
        payload = _canonical(
            {
                "birthtime_ns": int(birthtime_ns),
                "dev": int(dev),
                "inode": int(inode),
                "path": relative,
                "type": "neocortex-inventory-directory-observation-v1",
            }
        )
        digest = hashlib.sha256()
        digest.update(_DIRECTORY_DOMAIN)
        digest.update(self.value.encode("ascii"))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        self.value = _digest(digest.digest())


def empty_directory_digest() -> str:
    return _digest(_DIRECTORY_DOMAIN)


def batch_digest(root: str | Path, observations: Iterable[_Observation]) -> str:
    digest = hashlib.sha256()
    digest.update(_BATCH_DOMAIN)
    for observation in observations:
        payload = _observation_bytes(root, observation)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return _digest(digest.digest())


def _storage_parent(path: Path, *, create: bool) -> None:
    parent = path.parent
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = parent
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise InventoryResumeError("checkpoint parent is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise InventoryResumeError("checkpoint parent contains an unsafe component")
        if current == parent and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise InventoryResumeError("checkpoint parent has unsafe permissions")
        if current == current.parent:
            break
        current = current.parent


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class InventoryResumeCheckpointStore:
    """Atomic mutable owner for one resumable inventory checkpoint."""

    def __init__(self, path: str | Path, *, root: str | Path) -> None:
        raw = Path(os.path.abspath(os.fspath(path)))
        canonical_root = Path(os.path.realpath(os.path.abspath(os.fspath(root))))
        canonical_path = Path(os.path.realpath(raw))
        try:
            canonical_path.relative_to(canonical_root)
        except ValueError:
            pass
        else:
            raise InventoryResumeError("checkpoint must be outside the scanned root")
        self.path = raw
        self.root = str(canonical_root)

    def exists(self) -> bool:
        return os.path.lexists(self.path)

    @contextmanager
    def owner_lock(self):
        """Serialize checkpoint writers with a private, non-blocking lock."""

        _storage_parent(self.path, create=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        if os.path.lexists(lock_path):
            lock_metadata = lock_path.lstat()
            if (
                stat.S_ISLNK(lock_metadata.st_mode)
                or not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_nlink != 1
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            ):
                raise InventoryResumeConflictError("checkpoint lock has unsafe identity")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            if opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
                raise InventoryResumeConflictError("checkpoint lock has unsafe identity")
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InventoryResumeConflictError("checkpoint owner is active") from exc
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def read(self) -> InventoryResumeCheckpoint:
        _storage_parent(self.path, create=False)
        try:
            metadata = self.path.lstat()
        except FileNotFoundError as exc:
            raise InventoryResumeError("inventory resume checkpoint is missing") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise InventoryResumeCorruptError("inventory resume checkpoint has unsafe identity")
        descriptor = os.open(
            self.path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise InventoryResumeConflictError("checkpoint changed while opening")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(MAX_RESUME_CHECKPOINT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > MAX_RESUME_CHECKPOINT_BYTES or len(raw) != metadata.st_size:
            raise InventoryResumeCorruptError("inventory resume checkpoint exceeds its bound")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_without_duplicates)
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise InventoryResumeCorruptError("inventory resume checkpoint is invalid JSON") from exc
        checkpoint = InventoryResumeCheckpoint.from_mapping(value)
        if checkpoint.root_path != self.root:
            raise InventoryResumeConflictError("checkpoint root does not match its store")
        if _canonical(checkpoint.to_dict()) != raw:
            raise InventoryResumeCorruptError("inventory resume checkpoint is not canonical")
        return checkpoint

    def write(self, checkpoint: InventoryResumeCheckpoint) -> None:
        if checkpoint.root_path != self.root:
            raise InventoryResumeConflictError("checkpoint root does not match its store")
        encoded = _canonical(checkpoint.to_dict())
        if len(encoded) > MAX_RESUME_CHECKPOINT_BYTES:
            raise InventoryResumeError("inventory resume checkpoint exceeds its bound")
        with self.owner_lock():
            current = self.read() if self.exists() else None
            if current is not None:
                if checkpoint.scan_id != current.scan_id:
                    raise InventoryResumeConflictError("checkpoint owner changed")
                if current.status == "complete" and checkpoint != current:
                    raise InventoryResumeConflictError("checkpoint owner is terminal")
                if checkpoint.batch_index < current.batch_index:
                    raise InventoryResumeConflictError("checkpoint progress moved backwards")
                # Finishing the DFS can observe directories after the last
                # file batch (or an entirely empty tree).  Only terminal
                # publication may extend that directory evidence without
                # moving the committed file cursor to another batch.
                terminal_directory_extension = (
                    checkpoint.status == "complete"
                    and current.status != "complete"
                    and checkpoint.directories_seen >= current.directories_seen
                    and checkpoint.skipped_links >= current.skipped_links
                    and checkpoint.excluded_directories >= current.excluded_directories
                    and checkpoint.errors == current.errors == 0
                )
                if checkpoint.batch_index == current.batch_index and (
                    checkpoint.last_relative_cursor != current.last_relative_cursor
                    or checkpoint.prefix_digest != current.prefix_digest
                    or (
                        checkpoint.directory_digest != current.directory_digest
                        and not terminal_directory_extension
                    )
                    or checkpoint.batch_digest != current.batch_digest
                    or checkpoint.batch_files != current.batch_files
                    or checkpoint.files_seen != current.files_seen
                    or checkpoint.bytes_seen != current.bytes_seen
                ):
                    raise InventoryResumeConflictError("checkpoint position changed concurrently")
                if checkpoint.batch_index > current.batch_index + 1:
                    raise InventoryResumeConflictError("checkpoint batch index skipped")
            if os.path.lexists(self.path) and self.path.is_symlink():
                raise InventoryResumeConflictError("checkpoint path is a symlink")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    descriptor = -1
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                _fsync_directory(self.path.parent)
            except OSError as exc:
                raise InventoryResumeError(
                    "inventory resume checkpoint cannot be published"
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


__all__ = (
    "INVENTORY_RESUME_SCHEMA",
    "INVENTORY_RESUME_SCHEMA_VERSION",
    "MAX_RESUME_CHECKPOINT_BYTES",
    "MAX_RESUME_CURSOR_BYTES",
    "MAX_RESUME_POLICY_BYTES",
    "MAX_SORTED_DIRECTORY_ENTRIES",
    "CheckpointState",
    "InventoryDirectoryDigest",
    "InventoryPrefixDigest",
    "InventoryResumeCheckpoint",
    "InventoryResumeCheckpointStore",
    "InventoryResumeConflictError",
    "InventoryResumeCorruptError",
    "InventoryResumeError",
    "batch_digest",
    "dfs_order_key",
    "empty_batch_digest",
    "empty_directory_digest",
    "empty_prefix_digest",
    "relative_cursor",
)
