"""Small, filesystem-backed publication epochs for cross-owner operations.

SQLite databases are deliberately kept as independent owners in NeoCortex.  A
single transaction cannot publish several owners at once, so operations which
touch more than one owner use this module for a bounded, append-only marker.
The marker is not a replacement for owner transactions: it records whether a
set of owner-local commits completed and gives readers a stable epoch to
compare.  Reads never create this state.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast


STATE_PUBLICATION_SCHEMA = "neocortex.state-publication/v1"
STATE_CONTENT_PUBLICATION_MANIFEST_SCHEMA = "neocortex.content-publication-manifest/v1"
STATE_EPOCH_FILENAME = "state-epoch.json"
STATE_PUBLICATION_JOURNAL_FILENAME = "state-publication-journal.jsonl"
STATE_PUBLICATION_LOCK_FILENAME = "state-publication.lock"
STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME = "content-publication-manifest.json"
STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX = "content-publication-manifest."
MAX_PUBLICATION_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_PUBLICATION_RECORD_BYTES = 256 * 1024
MAX_OWNER_HEADS = 1024

PublicationStatus = Literal["complete", "partial", "failed"]
PublicationViewStatus = Literal["absent", "complete", "blocked", "inconsistent"]


class StatePublicationError(RuntimeError):
    """Base class for invalid or unavailable publication metadata."""


class StatePublicationConflictError(StatePublicationError):
    """The caller's expected epoch no longer matches the durable epoch."""


class StatePublicationRecoveryRequired(StatePublicationError):
    """An interrupted publication needs owner-aware reconciliation before writes."""

    error_code = "recovery_required"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"state publication recovery_required: {reason}")


class StatePublicationCommitError(StatePublicationError):
    """An append may be visible, so owner effects must not be rolled back.

    ``durable`` is true only after the journal file fsync succeeded, with its
    directory entry already synchronized.  False means recovery is required,
    not that the append did not happen.  In particular an epoch-pointer error
    cannot undo a durable complete event in the append-only journal.
    """

    def __init__(self, message: str, publication: StatePublication, *, durable: bool) -> None:
        super().__init__(message)
        self.publication = publication
        self.durable = durable


@dataclass(frozen=True, slots=True)
class StateOwnerHead:
    """A bounded, immutable identity for one owner-local published head.

    The digest is supplied by the owner producer after its own transaction has
    committed.  This module deliberately does not open SQLite or infer the
    digest from a path, which keeps the publication gate independent from the
    owner implementation and safe to use while a writer is active.
    """

    owner: str
    revision: int
    digest_sha256: str
    schema_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _required_text(self.owner, label="owner", maximum=128))
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("owner head revision must be a non-negative integer")
        object.__setattr__(
            self,
            "digest_sha256",
            _required_sha256(self.digest_sha256, label="owner head digest"),
        )
        if self.schema_version is not None and (
            type(self.schema_version) is not int or self.schema_version < 1
        ):
            raise ValueError("owner head schema version must be a positive integer")

    def as_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "revision": self.revision,
            "digest_sha256": self.digest_sha256,
            "schema_version": self.schema_version,
        }

    @property
    def digest(self) -> str:
        """Short compatibility alias for callers that use ``digest``."""

        return self.digest_sha256


@dataclass(frozen=True, slots=True)
class StateEpoch:
    """The last complete cross-owner publication observed for a state root."""

    epoch: int
    event_id: str | None
    operation: str | None
    owners: tuple[str, ...]
    manifest_sha256: str | None
    source: Literal["absent", "pointer", "journal"]
    owner_heads: tuple[StateOwnerHead, ...] = ()
    content_manifest_sha256: str | None = None
    content_manifest_name: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": STATE_PUBLICATION_SCHEMA,
            "epoch": self.epoch,
            "event_id": self.event_id,
            "operation": self.operation,
            "owners": list(self.owners),
            "manifest_sha256": self.manifest_sha256,
            "source": self.source,
            "owner_heads": [item.as_payload() for item in self.owner_heads],
            "content_manifest_sha256": self.content_manifest_sha256,
            "content_manifest_name": self.content_manifest_name,
        }


@dataclass(frozen=True, slots=True)
class StatePublication:
    """One append-only publication event."""

    event_id: str
    epoch: int
    operation: str
    owners: tuple[str, ...]
    status: PublicationStatus
    created_ns: int
    idempotency_key: str
    manifest_sha256: str | None = None
    detail: str | None = None
    owner_heads: tuple[StateOwnerHead, ...] = ()
    content_manifest_sha256: str | None = None
    content_manifest_name: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": STATE_PUBLICATION_SCHEMA,
            "event_id": self.event_id,
            "epoch": self.epoch,
            "operation": self.operation,
            "owners": list(self.owners),
            "status": self.status,
            "created_ns": self.created_ns,
            "idempotency_key": self.idempotency_key,
            "manifest_sha256": self.manifest_sha256,
            "detail": self.detail,
            "owner_heads": [item.as_payload() for item in self.owner_heads],
            "content_manifest_sha256": self.content_manifest_sha256,
            "content_manifest_name": self.content_manifest_name,
        }


@dataclass(frozen=True, slots=True)
class StatePublicationView:
    """Fail-closed view of the logical cross-owner publication boundary.

    ``read_state_epoch`` remains the compatibility accessor for the last
    complete epoch.  Consumers that combine more than one owner must use this
    view (or ``require_complete_state_epoch``): a durable ``partial`` record
    means an owner swap may have been interrupted, so the view is ``blocked``
    until a producer commits or safely aborts that transaction.
    """

    epoch: StateEpoch
    status: PublicationViewStatus
    publication: StatePublication | None
    pending: tuple[StatePublication, ...] = ()
    reason: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": STATE_PUBLICATION_SCHEMA,
            "status": self.status,
            "epoch": self.epoch.as_payload(),
            "publication": None if self.publication is None else self.publication.as_payload(),
            "pending": [item.as_payload() for item in self.pending],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StatePublicationTransaction:
    """Handle for a two-phase logical publication.

    Owner-local transactions still belong to the caller.  The handle records
    their baseline and commits only after the caller supplies the final heads;
    an interrupted handle leaves a durable pending record that blocks readers.
    """

    state_directory: Path
    prepared: StatePublication
    operation: str
    owners: tuple[str, ...]
    idempotency_key: str
    expected_epoch: int
    manifest_sha256: str | None = None

    def commit(
        self,
        owner_heads: Sequence[StateOwnerHead],
        *,
        manifest_sha256: str | None = None,
        detail: str | None = None,
        verify_owner_heads: Callable[[], Sequence[StateOwnerHead]] | None = None,
    ) -> StatePublication:
        return record_state_publication(
            self.state_directory,
            operation=self.operation,
            owners=self.owners,
            status="complete",
            idempotency_key=self.idempotency_key,
            expected_epoch=self.expected_epoch,
            manifest_sha256=self.manifest_sha256 if manifest_sha256 is None else manifest_sha256,
            detail=detail,
            owner_heads=owner_heads,
            verify_owner_heads=verify_owner_heads,
            expected_pending_event_id=(
                self.prepared.event_id if self.prepared.status == "partial" else None
            ),
        )

    def abort(
        self,
        observed_owner_heads: Sequence[StateOwnerHead],
        *,
        detail: str = "owner-local publication was rolled back",
    ) -> StatePublication:
        return abort_state_publication(
            self.state_directory,
            event_id=self.prepared.event_id,
            observed_owner_heads=observed_owner_heads,
            expected_epoch=self.expected_epoch,
            detail=detail,
        )


def _required_state_directory(path: str | Path) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise StatePublicationError("state directory must be absolute")
    absolute = Path(os.path.abspath(os.fspath(selected)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            component_metadata = current.lstat()
        except FileNotFoundError as exc:
            raise StatePublicationError("state directory does not exist") from exc
        except OSError as exc:
            raise StatePublicationError("state directory cannot be inspected") from exc
        if stat.S_ISLNK(component_metadata.st_mode):
            raise StatePublicationError("state directory cannot contain symlinks")
    try:
        value = selected.lstat()
    except FileNotFoundError as exc:
        raise StatePublicationError("state directory does not exist") from exc
    except OSError as exc:
        raise StatePublicationError("state directory cannot be inspected") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise StatePublicationError("state directory must be a real directory")
    return absolute


def _required_text(value: object, *, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _required_sha256(value: object, *, label: str) -> str:
    selected = _required_text(value, label=label, maximum=64)
    if len(selected) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in selected
    ):
        raise ValueError(f"{label} must be a SHA-256 hexadecimal digest")
    return selected.lower()


def _owners(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_required_text(value, label="owner", maximum=128) for value in values)
    if len(set(result)) != len(result):
        raise ValueError("owners cannot repeat")
    return result


def _optional_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_sha256(value, label="manifest_sha256")


def _owner_heads(values: Sequence[StateOwnerHead]) -> tuple[StateOwnerHead, ...]:
    if len(values) > MAX_OWNER_HEADS:
        raise ValueError("owner heads exceed their bound")
    result: list[StateOwnerHead] = []
    for value in values:
        if not isinstance(value, StateOwnerHead):
            raise ValueError("owner heads are invalid")
        result.append(value)
    if len({item.owner for item in result}) != len(result):
        raise ValueError("owner heads cannot repeat")
    return tuple(sorted(result, key=lambda item: item.owner))


def _parse_owner_heads(raw: object, *, label: str) -> tuple[StateOwnerHead, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise StatePublicationError(f"{label} are invalid")
    if len(raw) > MAX_OWNER_HEADS:
        raise StatePublicationError(f"{label} exceed their bound")
    values: list[StateOwnerHead] = []
    for item in raw:
        if not isinstance(item, dict):
            raise StatePublicationError(f"{label} contain an invalid entry")
        try:
            values.append(
                StateOwnerHead(
                    owner=cast(str, item.get("owner")),
                    revision=cast(int, item.get("revision")),
                    digest_sha256=cast(str, item.get("digest_sha256")),
                    schema_version=cast(int | None, item.get("schema_version")),
                )
            )
        except (TypeError, ValueError) as exc:
            raise StatePublicationError(f"{label} contain an invalid entry") from exc
    try:
        return _owner_heads(tuple(values))
    except ValueError as exc:
        raise StatePublicationError(f"{label} are invalid") from exc


def _epoch_path(state_directory: Path) -> Path:
    return state_directory / STATE_EPOCH_FILENAME


def _journal_path(state_directory: Path) -> Path:
    return state_directory / STATE_PUBLICATION_JOURNAL_FILENAME


def _lock_path(state_directory: Path) -> Path:
    return state_directory / STATE_PUBLICATION_LOCK_FILENAME


def _content_manifest_path(state_directory: Path, name: str) -> Path:
    # Names are generated from an event-id digest and are never accepted from
    # an external path.  Keep this check here as a second containment barrier
    # for readers of older or hand-edited journals.
    selected = Path(name)
    if (
        selected.name != name
        or not name.startswith(STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX)
        or not name.endswith(".json")
    ):
        raise StatePublicationError("content publication manifest name is invalid")
    return state_directory / name


def _content_manifest_event_name(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"{STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX}{digest}.json"


def _read_json_file(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StatePublicationError(
            f"publication metadata cannot be inspected: {path.name}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StatePublicationError(f"publication metadata is not regular: {path.name}")
    if metadata.st_size > MAX_PUBLICATION_RECORD_BYTES:
        raise StatePublicationError(f"publication metadata is too large: {path.name}")
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StatePublicationError(f"publication metadata is malformed: {path.name}") from exc
    if not isinstance(value, dict):
        raise StatePublicationError(f"publication metadata is not an object: {path.name}")
    return value


def _parse_epoch(
    value: Mapping[str, object], *, source: Literal["pointer", "journal"]
) -> StateEpoch:
    if value.get("schema") != STATE_PUBLICATION_SCHEMA:
        raise StatePublicationError("publication epoch schema is incompatible")
    raw_epoch = value.get("epoch")
    if type(raw_epoch) is not int or raw_epoch < 0:
        raise StatePublicationError("publication epoch is invalid")
    raw_event = value.get("event_id")
    event_id = (
        None if raw_event is None else _required_text(raw_event, label="event_id", maximum=256)
    )
    raw_operation = value.get("operation")
    operation = None if raw_operation is None else _required_text(raw_operation, label="operation")
    raw_owners = value.get("owners", ())
    if not isinstance(raw_owners, list) or any(not isinstance(item, str) for item in raw_owners):
        raise StatePublicationError("publication epoch owners are invalid")
    owners = _owners(tuple(raw_owners))
    manifest = value.get("manifest_sha256")
    if manifest is not None and not isinstance(manifest, str):
        raise StatePublicationError("publication epoch manifest digest is invalid")
    content_manifest = value.get("content_manifest_sha256")
    if content_manifest is not None and not isinstance(content_manifest, str):
        raise StatePublicationError("publication epoch content manifest digest is invalid")
    content_name = value.get("content_manifest_name")
    if content_name is not None:
        if not isinstance(content_name, str):
            raise StatePublicationError("publication epoch content manifest name is invalid")
        _content_manifest_path(Path("/"), content_name)
    return StateEpoch(
        epoch=raw_epoch,
        event_id=event_id,
        operation=operation,
        owners=owners,
        manifest_sha256=_optional_sha256(manifest),
        source=source,
        owner_heads=_parse_owner_heads(
            value.get("owner_heads"), label="publication epoch owner heads"
        ),
        content_manifest_sha256=_optional_sha256(content_manifest),
        content_manifest_name=content_name,
    )


def _parse_publication(value: Mapping[str, object]) -> StatePublication:
    if value.get("schema") != STATE_PUBLICATION_SCHEMA:
        raise StatePublicationError("publication journal schema is incompatible")
    event_id = _required_text(value.get("event_id"), label="event_id", maximum=256)
    operation = _required_text(value.get("operation"), label="operation")
    idempotency_key = _required_text(
        value.get("idempotency_key"), label="idempotency_key", maximum=256
    )
    raw_epoch = value.get("epoch")
    raw_created = value.get("created_ns")
    if type(raw_epoch) is not int or raw_epoch < 0:
        raise StatePublicationError("publication journal epoch is invalid")
    if type(raw_created) is not int or raw_created <= 0:
        raise StatePublicationError("publication journal timestamp is invalid")
    raw_owners = value.get("owners")
    if not isinstance(raw_owners, list) or any(not isinstance(item, str) for item in raw_owners):
        raise StatePublicationError("publication journal owners are invalid")
    raw_status = value.get("status")
    if raw_status not in {"complete", "partial", "failed"}:
        raise StatePublicationError("publication journal status is invalid")
    status = cast(PublicationStatus, raw_status)
    manifest = value.get("manifest_sha256")
    if manifest is not None and not isinstance(manifest, str):
        raise StatePublicationError("publication journal manifest digest is invalid")
    detail = value.get("detail")
    if detail is not None:
        detail = _required_text(detail, label="detail", maximum=4096)
    content_manifest = value.get("content_manifest_sha256")
    if content_manifest is not None and not isinstance(content_manifest, str):
        raise StatePublicationError("publication journal content manifest digest is invalid")
    content_name = value.get("content_manifest_name")
    if content_name is not None:
        if not isinstance(content_name, str):
            raise StatePublicationError("publication journal content manifest name is invalid")
        _content_manifest_path(Path("/"), content_name)
    return StatePublication(
        event_id=event_id,
        epoch=raw_epoch,
        operation=operation,
        owners=_owners(tuple(raw_owners)),
        status=status,
        created_ns=raw_created,
        idempotency_key=idempotency_key,
        manifest_sha256=_optional_sha256(manifest),
        detail=detail,
        owner_heads=_parse_owner_heads(
            value.get("owner_heads"), label="publication journal owner heads"
        ),
        content_manifest_sha256=_optional_sha256(content_manifest),
        content_manifest_name=content_name,
    )


def _read_journal(state_directory: Path) -> tuple[StatePublication, ...]:
    path = _journal_path(state_directory)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise StatePublicationError("publication journal cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StatePublicationError("publication journal is not a regular file")
    size = metadata.st_size
    if size > MAX_PUBLICATION_JOURNAL_BYTES:
        raise StatePublicationError("publication journal exceeds its bound")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StatePublicationError("publication journal cannot be read") from exc
    result: list[StatePublication] = []
    for line in lines:
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_PUBLICATION_RECORD_BYTES:
            raise StatePublicationError("publication journal record exceeds its bound")
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise StatePublicationError("publication journal contains malformed JSON") from exc
        if not isinstance(value, dict):
            raise StatePublicationError("publication journal record is not an object")
        result.append(_parse_publication(value))
    return tuple(result)


def read_state_epoch(state_directory: str | Path) -> StateEpoch:
    """Read the current publication epoch without creating any file.

    A missing pointer is a valid epoch zero.  If a journal contains a newer
    complete event than the pointer, the journal is returned as the source;
    this makes a pointer-write interruption observable without repairing it
    during a read.
    """

    selected = _required_state_directory(state_directory)
    pointer = _read_json_file(_epoch_path(selected))
    pointer_epoch = None if pointer is None else _parse_epoch(pointer, source="pointer")
    journal = _read_journal(selected)
    complete = tuple(item for item in journal if item.status == "complete")
    latest = max(complete, key=lambda item: (item.epoch, item.created_ns), default=None)
    journal_epoch = None
    if latest is not None:
        journal_epoch = StateEpoch(
            epoch=latest.epoch,
            event_id=latest.event_id,
            operation=latest.operation,
            owners=latest.owners,
            manifest_sha256=latest.manifest_sha256,
            source="journal",
            owner_heads=latest.owner_heads,
            content_manifest_sha256=latest.content_manifest_sha256,
            content_manifest_name=latest.content_manifest_name,
        )
    if pointer_epoch is None and journal_epoch is None:
        return StateEpoch(0, None, None, (), None, "absent")
    if pointer_epoch is None:
        return journal_epoch  # type: ignore[return-value]
    if journal_epoch is None:
        if pointer_epoch.epoch != 0 or pointer_epoch.event_id is not None:
            raise StatePublicationError("publication epoch pointer has no matching journal event")
        return pointer_epoch
    if pointer_epoch.epoch > journal_epoch.epoch:
        raise StatePublicationError("publication epoch pointer is ahead of the complete journal")
    if pointer_epoch.epoch == journal_epoch.epoch:
        if replace(pointer_epoch, source="journal") != journal_epoch:
            raise StatePublicationError(
                "publication epoch pointer disagrees with the complete journal"
            )
        return pointer_epoch
    return journal_epoch


def read_state_publications(state_directory: str | Path) -> tuple[StatePublication, ...]:
    """Return the bounded publication journal without changing it."""

    return _read_journal(_required_state_directory(state_directory))


def read_state_publication_state(state_directory: str | Path) -> StatePublicationView:
    """Read the logical publication gate without opening any owner database.

    A journal append is intentionally not treated as a commit.  The latest
    unresolved ``partial`` event blocks the whole cross-owner view, while a
    complete event with missing or tampered content evidence is reported as
    ``inconsistent``.  This is the strongest guarantee available for several
    independent SQLite files: it prevents a reader from claiming a coherent
    epoch, but it cannot make the underlying file renames physically
    atomic.
    """

    selected = _required_state_directory(state_directory)
    publications = _read_journal(selected)
    epoch = read_state_epoch(selected)
    latest_by_key: dict[str, StatePublication] = {}
    for publication in publications:
        latest_by_key[publication.idempotency_key] = publication
    pending = tuple(item for item in latest_by_key.values() if item.status == "partial")
    pending = tuple(sorted(pending, key=lambda item: (item.created_ns, item.event_id)))
    complete = tuple(item for item in publications if item.status == "complete")
    latest_complete = max(complete, key=lambda item: (item.epoch, item.created_ns), default=None)
    if pending:
        return StatePublicationView(
            epoch=epoch,
            status="blocked",
            publication=latest_complete,
            pending=pending,
            reason="unresolved cross-owner publication is pending recovery",
        )
    if latest_complete is None:
        return StatePublicationView(
            epoch=epoch,
            status="absent",
            publication=None,
        )
    try:
        _read_content_manifest_for_publication(selected, latest_complete)
    except StatePublicationError as exc:
        return StatePublicationView(
            epoch=epoch,
            status="inconsistent",
            publication=latest_complete,
            reason=str(exc),
        )
    return StatePublicationView(
        epoch=epoch,
        status="complete",
        publication=latest_complete,
    )


def require_complete_state_epoch(
    state_directory: str | Path,
    *,
    expected_epoch: int | None = None,
    owner_heads: Sequence[StateOwnerHead] | None = None,
) -> StateEpoch:
    """Return an epoch only if the cross-owner publication gate is complete.

    ``owner_heads`` lets a reader compare its freshly observed owner-local
    heads with the committed content-publication manifest.  Mismatches are
    conflicts, not partial successes.
    """

    view = read_state_publication_state(state_directory)
    if view.status == "absent":
        # Epoch zero with no publication is the valid initial state.  It is
        # complete only when the caller did not ask to compare owner heads.
        if view.epoch.epoch != 0:
            raise StatePublicationError("state publication epoch is absent but non-zero")
    elif view.status != "complete":
        reason = view.reason or "state publication is not complete"
        raise StatePublicationError(reason)
    if expected_epoch is not None:
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise ValueError("expected_epoch must be a non-negative integer")
        if view.epoch.epoch != expected_epoch:
            raise StatePublicationConflictError(
                f"publication epoch changed: expected {expected_epoch}, observed {view.epoch.epoch}"
            )
    if owner_heads is not None:
        observed = _owner_heads(tuple(owner_heads))
        if observed != view.epoch.owner_heads:
            raise StatePublicationConflictError("observed owner heads do not match published epoch")
    return view.epoch


@contextmanager
def _publication_lock(state_directory: Path):
    lock_path = _lock_path(state_directory)
    try:
        directory_metadata = state_directory.lstat()
    except OSError as exc:
        raise StatePublicationError("publication state directory cannot be inspected") from exc
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
        raise StatePublicationError("publication state directory is not a real directory")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_fd: int | None = None
    lock_fd: int | None = None
    try:
        directory_fd = os.open(state_directory, directory_flags)
        opened_directory = os.fstat(directory_fd)
        if (opened_directory.st_dev, opened_directory.st_ino) != (
            directory_metadata.st_dev,
            directory_metadata.st_ino,
        ):
            raise StatePublicationError("publication state directory identity changed")
        lock_fd = os.open(
            lock_path.name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(lock_fd, 0o600)
        stream = os.fdopen(lock_fd, "a+b", buffering=0)
        lock_fd = None
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StatePublicationError("publication lock is a symlink or non-directory") from exc
        raise StatePublicationError("publication lock cannot be opened") from exc
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    try:
        if os.name == "nt":
            raise StatePublicationError("publication epochs are Linux-only")
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise StatePublicationError("publication lock cannot be acquired") from exc
        yield
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()
        if directory_fd is not None:
            os.close(directory_fd)


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _content_manifest_payload(publication: StatePublication) -> dict[str, object]:
    return {
        "schema": STATE_CONTENT_PUBLICATION_MANIFEST_SCHEMA,
        "status": "complete",
        "event_id": publication.event_id,
        "epoch": publication.epoch,
        "operation": publication.operation,
        "owners": list(publication.owners),
        "owner_heads": [item.as_payload() for item in publication.owner_heads],
        "idempotency_key": publication.idempotency_key,
        "source_manifest_sha256": publication.manifest_sha256,
    }


def _write_content_manifest(
    state_directory: Path,
    publication: StatePublication,
) -> tuple[str, str]:
    """Write an immutable manifest before exposing a complete journal event.

    The event-specific file is never replaced.  A crash can leave an orphan
    file, which is harmless because readers authenticate it through the
    complete journal record; a complete record without its matching file is
    treated as inconsistent and therefore blocked.
    """

    if not publication.owner_heads:
        raise StatePublicationError("content publication requires owner heads")
    name = _content_manifest_event_name(publication.event_id)
    destination = _content_manifest_path(state_directory, name)
    payload = _content_manifest_payload(publication)
    encoded = _canonical_json_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise StatePublicationError("content publication manifest cannot be inspected") from exc
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StatePublicationError("content publication manifest is not regular")
        if (
            metadata.st_size != len(encoded)
            or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
        ):
            raise StatePublicationConflictError(
                "content publication manifest event already exists with different bytes"
            )
    else:
        # _atomic_write_json serializes with the same canonical representation
        # used for ``encoded`` and fsyncs both the file and its parent.
        _atomic_write_json(destination, payload)
    # This pointer is only a convenience for diagnostics.  The event-specific
    # immutable file and journal are authoritative, so a pointer-write failure
    # never turns a completed owner set into an apparent success.
    _atomic_write_json(
        state_directory / STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME,
        {
            "schema": STATE_CONTENT_PUBLICATION_MANIFEST_SCHEMA,
            "status": "pointer",
            "epoch": publication.epoch,
            "event_id": publication.event_id,
            "manifest_name": name,
            "manifest_sha256": digest,
        },
    )
    return name, digest


def _read_content_manifest_for_publication(
    state_directory: Path,
    publication: StatePublication,
) -> None:
    if not publication.owner_heads:
        if (
            publication.content_manifest_name is not None
            or publication.content_manifest_sha256 is not None
        ):
            raise StatePublicationError("publication content manifest metadata is incomplete")
        return
    if publication.content_manifest_name is None or publication.content_manifest_sha256 is None:
        raise StatePublicationError("complete publication lacks content manifest metadata")
    expected_name = _content_manifest_event_name(publication.event_id)
    if publication.content_manifest_name != expected_name:
        raise StatePublicationError("publication content manifest event does not match journal")
    path = _content_manifest_path(state_directory, publication.content_manifest_name)
    payload = _read_json_file(path)
    if payload is None:
        raise StatePublicationError("complete publication content manifest is missing")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StatePublicationError("complete publication content manifest cannot be read") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != publication.content_manifest_sha256:
        raise StatePublicationError("publication content manifest digest mismatch")
    if payload.get("schema") != STATE_CONTENT_PUBLICATION_MANIFEST_SCHEMA:
        raise StatePublicationError("publication content manifest schema is incompatible")
    if payload.get("status") != "complete":
        raise StatePublicationError("publication content manifest is not complete")
    if payload.get("event_id") != publication.event_id or payload.get("epoch") != publication.epoch:
        raise StatePublicationError("publication content manifest event is inconsistent")
    if payload.get("operation") != publication.operation or payload.get("owners") != list(
        publication.owners
    ):
        raise StatePublicationError("publication content manifest scope is inconsistent")
    if payload.get("idempotency_key") != publication.idempotency_key:
        raise StatePublicationError("publication content manifest idempotency is inconsistent")
    source_manifest = payload.get("source_manifest_sha256")
    if source_manifest != publication.manifest_sha256:
        raise StatePublicationError("publication content manifest source is inconsistent")
    parsed_heads = _parse_owner_heads(
        payload.get("owner_heads"),
        label="content publication manifest owner heads",
    )
    if parsed_heads != publication.owner_heads:
        raise StatePublicationError("publication content manifest owner heads are inconsistent")


def _fsync_directory(path: Path) -> None:
    """Durably commit an append that may have created a new journal file."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError as exc:
        raise StatePublicationError("publication directory cannot be synchronized") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise StatePublicationError("publication directory cannot be synchronized") from exc
    finally:
        os.close(descriptor)


def _append_journal(path: Path, publication: StatePublication) -> None:
    encoded = (
        json.dumps(
            publication.as_payload(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
    if len(encoded.encode("utf-8")) > MAX_PUBLICATION_RECORD_BYTES:
        raise StatePublicationError("publication record exceeds its bound")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise StatePublicationError("publication journal cannot be inspected") from exc
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
    ):
        raise StatePublicationError("publication journal is not a regular file")
    current_size = 0 if metadata is None else metadata.st_size
    if current_size + len(encoded.encode("utf-8")) > MAX_PUBLICATION_JOURNAL_BYTES:
        raise StatePublicationError("publication journal would exceed its bound")
    append_started = False
    durable = False
    try:
        with path.open("a", encoding="utf-8") as stream:
            # Complete all permission/directory setup before exposing bytes.
            # Once an append starts, an I/O error does not prove its absence.
            os.fchmod(stream.fileno(), 0o600)
            _fsync_directory(path.parent)
            append_started = True
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            durable = True
    except (OSError, StatePublicationError) as exc:
        if append_started:
            raise StatePublicationCommitError(
                "publication journal append requires reconciliation",
                publication,
                durable=durable,
            ) from exc
        raise StatePublicationError("publication journal cannot be appended") from exc


def _idempotency_digest(
    operation: str,
    owners: tuple[str, ...],
    idempotency_key: str,
) -> str:
    payload = json.dumps(
        [operation, owners, idempotency_key], ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def record_state_publication(
    state_directory: str | Path,
    *,
    operation: str,
    owners: Sequence[str],
    status: PublicationStatus,
    idempotency_key: str,
    expected_epoch: int | None = None,
    manifest_sha256: str | None = None,
    detail: str | None = None,
    owner_heads: Sequence[StateOwnerHead] | None = None,
    verify_owner_heads: Callable[[], Sequence[StateOwnerHead]] | None = None,
    expected_pending_event_id: str | None = None,
) -> StatePublication:
    """Append one publication event and advance the epoch only on success.

    Complete events are idempotent by ``idempotency_key``.  Partial/failed
    events remain evidence but do not advance the epoch.  A partial event is a
    durable prepare record: cross-owner readers must treat it as ``blocked``
    until a matching complete event or a verified abort is recorded.  When
    ``owner_heads`` is supplied for a complete event, an immutable
    content-publication manifest is written before the journal record and is
    authenticated by that record.  The journal is written before the epoch
    pointer, so a pointer-write interruption remains recoverable from the
    append-only evidence.
    """

    selected = _required_state_directory(state_directory)
    operation = _required_text(operation, label="operation")
    selected_owners = _owners(tuple(owners))
    idempotency_key = _required_text(idempotency_key, label="idempotency_key", maximum=256)
    manifest_sha256 = _optional_sha256(manifest_sha256)
    normalized_owner_heads = () if owner_heads is None else _owner_heads(tuple(owner_heads))
    if expected_pending_event_id is not None:
        expected_pending_event_id = _required_text(expected_pending_event_id, label="pending event")
        if status != "complete":
            raise ValueError("a pending-event commit guard requires complete status")
    if verify_owner_heads is not None and (status != "complete" or not callable(verify_owner_heads)):
        raise ValueError("owner revalidation belongs to a complete publication")
    if normalized_owner_heads and {item.owner for item in normalized_owner_heads} != set(
        selected_owners
    ):
        raise ValueError("owner heads must cover exactly the publication owners")
    if status not in {"complete", "partial", "failed"}:
        raise ValueError("publication status is invalid")
    if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch < 0):
        raise ValueError("expected_epoch must be a non-negative integer")
    if detail is not None:
        detail = _required_text(detail, label="detail", maximum=4096)
    digest = _idempotency_digest(operation, selected_owners, idempotency_key)
    append_completed: StatePublication | None = None
    try:
        with _publication_lock(selected):
            current = read_state_epoch(selected)
            if expected_epoch is not None and current.epoch != expected_epoch:
                raise StatePublicationConflictError(
                    f"publication epoch changed: expected {expected_epoch}, observed {current.epoch}"
                )
            journal = _read_journal(selected)
            latest_by_key = {item.idempotency_key: item for item in journal}
            if expected_pending_event_id is not None:
                active = latest_by_key.get(digest)
                if active is None or active.status != "partial" or active.event_id != expected_pending_event_id:
                    raise StatePublicationConflictError("prepared event changed before publication commit")
            if any(
                item.status == "partial" and item.idempotency_key != digest
                for item in latest_by_key.values()
            ):
                raise StatePublicationConflictError("another publication is pending recovery")
            if verify_owner_heads is not None:
                observed = _owner_heads(tuple(verify_owner_heads()))
                if observed != normalized_owner_heads:
                    raise StatePublicationConflictError("owner heads changed before publication commit")
            latest_complete = next(
                (item for item in reversed(journal) if item.status == "complete"), None
            )
            if latest_complete is not None:
                _read_content_manifest_for_publication(selected, latest_complete)
            prior = latest_by_key.get(digest)
            if prior is not None:
                if prior.manifest_sha256 != manifest_sha256:
                    raise StatePublicationConflictError(
                        "idempotency key is already bound to a different manifest"
                    )
                if prior.status == "partial":
                    if status == "failed":
                        raise StatePublicationConflictError(
                            "a prepared publication requires a verified abort"
                        )
                    if status == "partial" and prior.owner_heads != normalized_owner_heads:
                        raise StatePublicationConflictError(
                            "prepared owner heads cannot be replaced on retry"
                        )
                    if status == "complete" and prior.owner_heads and not normalized_owner_heads:
                        raise StatePublicationConflictError(
                            "prepared owner heads require final owner heads"
                        )
                # A complete replay is always safe only when the content heads are
                # identical.  A prepare may be completed with final heads that
                # differ from its baseline, but a repeated prepare itself must not
                # create an unbounded journal.
                if prior.status == "complete":
                    if prior.owner_heads != normalized_owner_heads:
                        raise StatePublicationConflictError(
                            "idempotency key is already bound to different owner heads"
                        )
                    return prior
                if prior.status == status and prior.owner_heads == normalized_owner_heads:
                    return prior
            epoch = current.epoch + (1 if status == "complete" else 0)
            created_ns = time.time_ns()
            event_id = f"epoch:{epoch}:event:{created_ns}"
            publication = StatePublication(
                event_id=event_id,
                epoch=epoch,
                operation=operation,
                owners=selected_owners,
                status=status,
                created_ns=created_ns,
                idempotency_key=digest,
                manifest_sha256=manifest_sha256,
                detail=detail,
                owner_heads=normalized_owner_heads,
            )
            content_manifest_name = None
            content_manifest_sha256 = None
            if status == "complete" and normalized_owner_heads:
                content_manifest_name, content_manifest_sha256 = _write_content_manifest(
                    selected,
                    publication,
                )
                publication = StatePublication(
                    event_id=publication.event_id,
                    epoch=publication.epoch,
                    operation=publication.operation,
                    owners=publication.owners,
                    status=publication.status,
                    created_ns=publication.created_ns,
                    idempotency_key=publication.idempotency_key,
                    manifest_sha256=publication.manifest_sha256,
                    detail=publication.detail,
                    owner_heads=publication.owner_heads,
                    content_manifest_sha256=content_manifest_sha256,
                    content_manifest_name=content_manifest_name,
                )
            _append_journal(_journal_path(selected), publication)
            append_completed = publication
            if status == "complete":
                try:
                    _atomic_write_json(
                        _epoch_path(selected),
                        {
                            "schema": STATE_PUBLICATION_SCHEMA,
                            "epoch": epoch,
                            "event_id": event_id,
                            "operation": operation,
                            "owners": list(selected_owners),
                            "manifest_sha256": manifest_sha256,
                            "owner_heads": [item.as_payload() for item in normalized_owner_heads],
                            "content_manifest_sha256": content_manifest_sha256,
                            "content_manifest_name": content_manifest_name,
                        },
                    )
                except (OSError, StatePublicationError) as exc:
                    raise StatePublicationCommitError(
                        "complete journal committed; epoch pointer update failed",
                        publication,
                        durable=True,
                    ) from exc
            return publication
    except (OSError, StatePublicationError) as exc:
        if isinstance(exc, StatePublicationCommitError):
            raise
        if append_completed is not None:
            raise StatePublicationCommitError(
                "publication journal committed; lock cleanup failed",
                append_completed,
                durable=True,
            ) from exc
        raise


def begin_state_publication(
    state_directory: str | Path,
    *,
    operation: str,
    owners: Sequence[str],
    idempotency_key: str,
    expected_epoch: int | None = None,
    owner_heads: Sequence[StateOwnerHead] | None = None,
    manifest_sha256: str | None = None,
    detail: str | None = None,
) -> StatePublicationTransaction:
    """Durably prepare a cross-owner publication.

    The caller owns the owner-local writes between ``begin`` and
    ``transaction.commit``.  If the process is interrupted after this call,
    ``read_state_publication_state`` reports ``blocked`` rather than treating
    a partially swapped set as a published epoch.
    """

    selected = _owners(tuple(owners))
    prepared = record_state_publication(
        state_directory,
        operation=operation,
        owners=selected,
        status="partial",
        idempotency_key=idempotency_key,
        expected_epoch=expected_epoch,
        manifest_sha256=manifest_sha256,
        detail=detail or "cross-owner publication prepared",
        owner_heads=owner_heads,
    )
    return StatePublicationTransaction(
        state_directory=_required_state_directory(state_directory),
        prepared=prepared,
        operation=operation,
        owners=selected,
        idempotency_key=idempotency_key,
        expected_epoch=prepared.epoch,
        manifest_sha256=manifest_sha256,
    )


def abort_state_publication(
    state_directory: str | Path,
    *,
    event_id: str,
    observed_owner_heads: Sequence[StateOwnerHead],
    expected_epoch: int | None = None,
    detail: str = "owner-local publication was rolled back",
) -> StatePublication:
    """Resolve a prepared publication only after a verified owner rollback.

    This function never restores files itself.  It requires the caller to
    provide owner heads that exactly match the baseline captured in the
    prepare record; otherwise the pending marker remains and readers stay
    blocked.  That explicit limitation is intentional because independent
    SQLite files cannot participate in one filesystem transaction.
    """

    selected = _required_state_directory(state_directory)
    event_id = _required_text(event_id, label="event_id", maximum=256)
    observed = _owner_heads(tuple(observed_owner_heads))
    detail = _required_text(detail, label="detail", maximum=4096)
    with _publication_lock(selected):
        current = read_state_epoch(selected)
        if expected_epoch is not None and current.epoch != expected_epoch:
            raise StatePublicationConflictError(
                f"publication epoch changed: expected {expected_epoch}, observed {current.epoch}"
            )
        journal = _read_journal(selected)
        pending = next(
            (
                item
                for item in reversed(journal)
                if item.event_id == event_id and item.status == "partial"
            ),
            None,
        )
        if pending is None:
            raise StatePublicationConflictError("publication prepare event is not pending")
        latest_for_key = next(
            (item for item in reversed(journal) if item.idempotency_key == pending.idempotency_key),
            None,
        )
        if latest_for_key is None or latest_for_key.event_id != pending.event_id:
            raise StatePublicationConflictError("publication prepare event was already resolved")
        if not pending.owner_heads:
            raise StatePublicationError("publication recovery requires baseline owner heads")
        if observed != pending.owner_heads:
            raise StatePublicationConflictError(
                "owner heads do not prove rollback to the prepared baseline"
            )
        created_ns = time.time_ns()
        failed = StatePublication(
            event_id=f"epoch:{current.epoch}:recovery:{created_ns}",
            epoch=current.epoch,
            operation=pending.operation,
            owners=pending.owners,
            status="failed",
            created_ns=created_ns,
            idempotency_key=pending.idempotency_key,
            manifest_sha256=pending.manifest_sha256,
            detail=detail,
            owner_heads=pending.owner_heads,
        )
        _append_journal(_journal_path(selected), failed)
        return failed


def resume_state_publication(
    state_directory: str | Path,
    *,
    event_id: str,
    operation: str,
    owners: Sequence[str],
    idempotency_key: str,
    manifest_sha256: str,
    expected_epoch: int,
) -> StatePublicationTransaction:
    """Rehydrate one exact pending producer transaction without changing it.

    The producer supplies the original (unhashed) transaction key and input
    manifest. A stored journal key is not interchangeable with that key.
    Resumption keeps readers blocked until the producer commits its verified
    final heads; it does not abort an ambiguous owner-local change.
    """

    selected = _required_state_directory(state_directory)
    selected_owners = _owners(tuple(owners))
    operation = _required_text(operation, label="operation")
    event_id = _required_text(event_id, label="event_id", maximum=256)
    manifest_sha256 = _required_sha256(manifest_sha256, label="manifest digest")
    idempotency_key = _required_text(idempotency_key, label="idempotency_key", maximum=256)
    if type(expected_epoch) is not int or expected_epoch < 0:
        raise ValueError("expected_epoch must be a non-negative integer")
    digest = _idempotency_digest(operation, selected_owners, idempotency_key)
    with _publication_lock(selected):
        view = read_state_publication_state(selected)
        if view.epoch.epoch != expected_epoch:
            raise StatePublicationConflictError("publication epoch changed before resume")
        if len(view.pending) != 1 or view.pending[0].event_id != event_id:
            raise StatePublicationConflictError("publication resume requires one exact pending event")
        prepared = view.pending[0]
        if (
            prepared.operation != operation
            or prepared.owners != selected_owners
            or prepared.idempotency_key != digest
            or prepared.manifest_sha256 != manifest_sha256
            or prepared.epoch != expected_epoch
        ):
            raise StatePublicationConflictError("publication resume input contract changed")
        if view.publication is not None:
            _read_content_manifest_for_publication(selected, view.publication)
        return StatePublicationTransaction(
            state_directory=selected,
            prepared=prepared,
            operation=operation,
            owners=selected_owners,
            idempotency_key=idempotency_key,
            expected_epoch=expected_epoch,
            manifest_sha256=manifest_sha256,
        )


def abort_unbound_state_publication(
    state_directory: str | Path,
    *,
    event_id: str,
    expected_epoch: int | None = None,
    detail: str = "unbound publication prepare invalidated without an owner baseline",
) -> StatePublication:
    """Invalidate a prepare that captured no owner baseline at all.

    A normal abort must prove rollback to ``owner_heads``.  Older integrated
    ``--all`` runs could create a partial marker before capturing those heads,
    leaving a permanently blocked epoch that has no rollback contract to check.
    This narrow recovery path only accepts that malformed shape while the
    publication epoch is still zero and no complete publication exists; it
    records a failed event and never claims that owner files were rolled back.
    """

    selected = _required_state_directory(state_directory)
    event_id = _required_text(event_id, label="event_id", maximum=256)
    detail = _required_text(detail, label="detail", maximum=4096)
    with _publication_lock(selected):
        current = read_state_epoch(selected)
        if expected_epoch is not None and current.epoch != expected_epoch:
            raise StatePublicationConflictError(
                f"publication epoch changed: expected {expected_epoch}, observed {current.epoch}"
            )
        if current.epoch != 0:
            raise StatePublicationConflictError(
                "unbound publication recovery requires the initial publication epoch"
            )
        journal = _read_journal(selected)
        pending = next(
            (item for item in reversed(journal) if item.event_id == event_id),
            None,
        )
        if pending is None or pending.status != "partial":
            raise StatePublicationConflictError("publication prepare event is not pending")
        if pending.owner_heads:
            raise StatePublicationConflictError(
                "publication prepare has an owner baseline; use verified abort"
            )
        if any(item.status == "complete" for item in journal):
            raise StatePublicationConflictError(
                "unbound publication recovery cannot follow a complete publication"
            )
        latest_for_key = next(
            (item for item in reversed(journal) if item.idempotency_key == pending.idempotency_key),
            None,
        )
        if latest_for_key is None or latest_for_key.event_id != pending.event_id:
            raise StatePublicationConflictError("publication prepare event was already resolved")
        failed = StatePublication(
            event_id=f"epoch:{current.epoch}:recovery:{time.time_ns()}",
            epoch=current.epoch,
            operation=pending.operation,
            owners=pending.owners,
            status="failed",
            created_ns=time.time_ns(),
            idempotency_key=pending.idempotency_key,
            manifest_sha256=pending.manifest_sha256,
            detail=detail,
            owner_heads=(),
        )
        _append_journal(_journal_path(selected), failed)
    return failed


def reconcile_unbound_state_publication(
    state_directory: str | Path,
    *,
    event_id: str,
    expected_epoch: int,
    expected_publication_event_id: str,
    verify_owner_heads: Callable[[], Sequence[StateOwnerHead]],
) -> StatePublication:
    """Resolve a legacy Semantic marker after an owner-aware verification.

    Unlike the epoch-zero invalidation, this operation has a previous complete
    publication to preserve. The producer must supply a fresh, legacy-aware
    observation while the publication lock is held. The callback must not
    simply return heads copied from the journal: only the owner can prove that
    its local publication still has that identity. No owner rollback is
    performed or claimed, and the previous complete epoch is never advanced.
    """

    selected = _required_state_directory(state_directory)
    event_id = _required_text(event_id, label="event_id", maximum=256)
    expected_publication_event_id = _required_text(
        expected_publication_event_id, label="publication event_id", maximum=256
    )
    if type(expected_epoch) is not int or expected_epoch < 1:
        raise ValueError("legacy reconciliation requires a positive expected epoch")
    if not callable(verify_owner_heads):
        raise TypeError("legacy reconciliation requires an owner verification callback")
    with _publication_lock(selected):
        view = read_state_publication_state(selected)
        if (
            view.epoch.epoch != expected_epoch
            or view.publication is None
            or view.publication.event_id != expected_publication_event_id
        ):
            raise StatePublicationConflictError("legacy publication epoch or event changed")
        # A blocked view deliberately delays content-manifest inspection. It
        # must be authenticated here before that blocked marker is removed.
        _read_content_manifest_for_publication(selected, view.publication)
        if len(view.pending) != 1 or view.pending[0].event_id != event_id:
            raise StatePublicationConflictError("legacy reconciliation requires one exact pending event")
        pending = view.pending[0]
        if (
            pending.operation != "framework-all-semantic"
            or pending.owner_heads
            or pending.epoch != expected_epoch
            or pending.owners != view.publication.owners
            or pending.manifest_sha256 is None
            or not view.epoch.owner_heads
        ):
            raise StatePublicationConflictError("legacy pending publication has no compatible scope")
        observed = _owner_heads(tuple(verify_owner_heads()))
        if observed != view.epoch.owner_heads:
            raise StatePublicationConflictError("fresh owner heads do not match the legacy publication")
        created_ns = time.time_ns()
        failed = StatePublication(
            event_id=f"epoch:{expected_epoch}:recovery:{created_ns}",
            epoch=expected_epoch,
            operation=pending.operation,
            owners=pending.owners,
            status="failed",
            created_ns=created_ns,
            idempotency_key=pending.idempotency_key,
            manifest_sha256=pending.manifest_sha256,
            detail=(
                "Legacy Semantic prepare reconciled against unchanged published owners; "
                f"publication_event={expected_publication_event_id}; rollback_claimed=false"
            ),
            owner_heads=(),
        )
        _append_journal(_journal_path(selected), failed)
        return failed


def restart_state_publication_checkpoint(
    state_directory: str | Path,
    *,
    event_id: str,
    expected_epoch: int,
    owner_heads: Sequence[StateOwnerHead],
    verify_owner_heads: Callable[[], Sequence[StateOwnerHead]],
) -> StatePublication:
    """Abandon interrupted Semantic work and checkpoint only published heads.

    A *new* full execution need not resume the old producer's work contract.
    Its coordinator must hold FrameworkRunLock and authenticate the old
    manifest/root first. This function does not open SQLite, promote building
    generations, claim a rollback, or declare the interrupted run successful.

    The two logical appends are exposed with one journal replacement. Readers
    therefore see either the old unresolved prepare or the new authenticated
    checkpoint, never the old epoch after its recovery fence was removed.
    The complete original journal prefix is preserved byte for byte.
    A revision-zero head is an explicitly verified empty/absent owner, not
    permission to omit that owner or to promote an unfinished generation.
    """

    selected = _required_state_directory(state_directory)
    event_id = _required_text(event_id, label="event_id", maximum=256)
    if type(expected_epoch) is not int or expected_epoch < 0:
        raise ValueError("expected_epoch must be a non-negative integer")
    heads = _owner_heads(owner_heads)
    owners = _owners(tuple(head.owner for head in heads))
    if "semantic" not in owners or not set(owners) <= {"semantic", "code"}:
        raise ValueError("restart checkpoint requires Semantic and optional Code heads")
    operation = "framework-all-restart-checkpoint"
    digest = _idempotency_digest(
        operation,
        owners,
        publication_idempotency_key(event_id, expected_epoch, [head.as_payload() for head in heads]),
    )
    with _publication_lock(selected):
        view = read_state_publication_state(selected)
        journal = _read_journal(selected)
        replay = next(
            (item for item in reversed(journal) if item.idempotency_key == digest), None
        )
        if not view.pending and replay is not None and replay.status == "complete":
            if (
                view.status != "complete"
                or view.publication != replay
                or view.epoch.epoch != replay.epoch
                or view.epoch.event_id != replay.event_id
            ):
                raise StatePublicationConflictError("restart replay is no longer the current complete checkpoint")
            _read_content_manifest_for_publication(selected, replay)
            return replay
        if (
            view.epoch.epoch != expected_epoch
            or len(view.pending) != 1
            or view.pending[0].event_id != event_id
            or view.pending[0].epoch != expected_epoch
            or view.pending[0].operation != "framework-all-semantic"
        ):
            raise StatePublicationConflictError("restart requires one exact pending Semantic publication")
        pending = view.pending[0]
        if pending.owner_heads and {head.owner for head in pending.owner_heads} != set(pending.owners):
            raise StatePublicationConflictError("restart pending baseline does not cover its owners exactly")
        required_owners = set(pending.owners)
        if view.publication is not None:
            required_owners.update(view.publication.owners)
            _read_content_manifest_for_publication(selected, view.publication)
            if (
                view.epoch.event_id != view.publication.event_id
                or view.epoch.owner_heads != view.publication.owner_heads
                or view.epoch.manifest_sha256 != view.publication.manifest_sha256
                or view.epoch.content_manifest_sha256 != view.publication.content_manifest_sha256
                or view.epoch.content_manifest_name != view.publication.content_manifest_name
            ):
                raise StatePublicationConflictError("restart publication pointer differs from its journal")
        elif expected_epoch != 0:
            raise StatePublicationConflictError("restart has no authenticated previous publication")
        if not required_owners <= set(owners):
            raise StatePublicationConflictError("restart cannot reduce the published owner scope")
        if _owner_heads(tuple(verify_owner_heads())) != heads:
            raise StatePublicationConflictError("owner heads changed before restart preparation")

        path = _journal_path(selected)
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > MAX_PUBLICATION_JOURNAL_BYTES
        ):
            raise StatePublicationError("restart journal is not a bounded regular file")

        def fingerprint(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns,
            )

        def captured_prefix() -> bytes:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
                    raise StatePublicationConflictError("restart journal identity changed")
                raw = stream.read(MAX_PUBLICATION_JOURNAL_BYTES + 1)
                if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
                    raise StatePublicationConflictError("restart journal changed during read")
                return raw

        prefix = captured_prefix()
        if len(prefix) != before.st_size or len(prefix) > MAX_PUBLICATION_JOURNAL_BYTES:
            raise StatePublicationConflictError("restart journal changed during capture")
        created_ns = time.time_ns()
        failed = StatePublication(
            event_id=f"epoch:{expected_epoch}:abandoned:{created_ns}",
            epoch=expected_epoch,
            operation=pending.operation,
            owners=pending.owners,
            status="failed",
            created_ns=created_ns,
            idempotency_key=pending.idempotency_key,
            manifest_sha256=pending.manifest_sha256,
            detail="interrupted work abandoned for a new full execution; rollback_claimed=false",
            owner_heads=tuple(head for head in heads if head.owner in pending.owners),
        )
        checkpoint = StatePublication(
            event_id=f"epoch:{expected_epoch + 1}:restart:{created_ns}",
            epoch=expected_epoch + 1,
            operation=operation,
            owners=owners,
            status="complete",
            created_ns=created_ns + 1,
            idempotency_key=digest,
            detail="checkpoint of currently published heads only; interrupted_work_completed=false; rollback_claimed=false",
            owner_heads=heads,
        )
        name, manifest_digest = _write_content_manifest(selected, checkpoint)
        checkpoint = replace(
            checkpoint, content_manifest_name=name, content_manifest_sha256=manifest_digest
        )
        records = (_canonical_json_bytes(failed.as_payload()), _canonical_json_bytes(checkpoint.as_payload()))
        if any(len(record) > MAX_PUBLICATION_RECORD_BYTES for record in records):
            raise StatePublicationError("restart publication record exceeds its bound")
        separator = b"" if not prefix or prefix.endswith(b"\n") else b"\n"
        encoded = prefix + separator + b"".join(records)
        if len(encoded) > MAX_PUBLICATION_JOURNAL_BYTES:
            raise StatePublicationError("restart journal would exceed its bound")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.restart-", dir=selected)
        temporary = Path(temporary_name)
        replace_started = False
        journal_durable = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if _owner_heads(tuple(verify_owner_heads())) != heads:
                raise StatePublicationConflictError("owner heads changed before restart checkpoint")
            after = path.lstat()
            if fingerprint(before) != fingerprint(after) or captured_prefix() != prefix:
                raise StatePublicationConflictError("restart journal changed before checkpoint")
            if read_state_epoch(selected) != view.epoch:
                raise StatePublicationConflictError("restart epoch changed before checkpoint")
            replace_started = True
            os.replace(temporary, path)
            _fsync_directory(selected)
            journal_durable = True
            # The already-durable journal is authoritative if this convenience
            # pointer write is interrupted; the ordinary reader falls forward.
            _atomic_write_json(
                _epoch_path(selected),
                {
                    "schema": STATE_PUBLICATION_SCHEMA,
                    "epoch": checkpoint.epoch,
                    "event_id": checkpoint.event_id,
                    "operation": checkpoint.operation,
                    "owners": list(checkpoint.owners),
                    "manifest_sha256": checkpoint.manifest_sha256,
                    "owner_heads": [head.as_payload() for head in checkpoint.owner_heads],
                    "content_manifest_sha256": checkpoint.content_manifest_sha256,
                    "content_manifest_name": checkpoint.content_manifest_name,
                },
            )
            return checkpoint
        except (OSError, StatePublicationError) as exc:
            if replace_started:
                raise StatePublicationCommitError(
                    "restart checkpoint replacement may be visible; reread the publication state",
                    checkpoint,
                    durable=journal_durable,
                ) from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)


def publication_idempotency_key(*parts: object) -> str:
    """Build a deterministic bounded key for one cross-owner operation."""

    serialized = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 32_768:
        raise ValueError("publication idempotency input exceeds its bound")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_OWNER_HEADS",
    "MAX_PUBLICATION_JOURNAL_BYTES",
    "MAX_PUBLICATION_RECORD_BYTES",
    "STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME",
    "STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX",
    "STATE_CONTENT_PUBLICATION_MANIFEST_SCHEMA",
    "STATE_EPOCH_FILENAME",
    "STATE_PUBLICATION_JOURNAL_FILENAME",
    "STATE_PUBLICATION_LOCK_FILENAME",
    "STATE_PUBLICATION_SCHEMA",
    "PublicationStatus",
    "PublicationViewStatus",
    "StateEpoch",
    "StateOwnerHead",
    "StatePublication",
    "StatePublicationCommitError",
    "StatePublicationConflictError",
    "StatePublicationError",
    "StatePublicationRecoveryRequired",
    "StatePublicationTransaction",
    "StatePublicationView",
    "abort_state_publication",
    "abort_unbound_state_publication",
    "begin_state_publication",
    "publication_idempotency_key",
    "read_state_epoch",
    "read_state_publication_state",
    "read_state_publications",
    "reconcile_unbound_state_publication",
    "record_state_publication",
    "require_complete_state_epoch",
    "restart_state_publication_checkpoint",
    "resume_state_publication",
]
