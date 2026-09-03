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
import json
import os
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


STATE_PUBLICATION_SCHEMA = "neocortex.state-publication/v1"
STATE_EPOCH_FILENAME = "state-epoch.json"
STATE_PUBLICATION_JOURNAL_FILENAME = "state-publication-journal.jsonl"
STATE_PUBLICATION_LOCK_FILENAME = "state-publication.lock"
MAX_PUBLICATION_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_PUBLICATION_RECORD_BYTES = 256 * 1024

PublicationStatus = Literal["complete", "partial", "failed"]


class StatePublicationError(RuntimeError):
    """Base class for invalid or unavailable publication metadata."""


class StatePublicationConflictError(StatePublicationError):
    """The caller's expected epoch no longer matches the durable epoch."""


@dataclass(frozen=True, slots=True)
class StateEpoch:
    """The last complete cross-owner publication observed for a state root."""

    epoch: int
    event_id: str | None
    operation: str | None
    owners: tuple[str, ...]
    manifest_sha256: str | None
    source: Literal["absent", "pointer", "journal"]

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": STATE_PUBLICATION_SCHEMA,
            "epoch": self.epoch,
            "event_id": self.event_id,
            "operation": self.operation,
            "owners": list(self.owners),
            "manifest_sha256": self.manifest_sha256,
            "source": self.source,
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
        }


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


def _owners(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_required_text(value, label="owner", maximum=128) for value in values)
    if len(set(result)) != len(result):
        raise ValueError("owners cannot repeat")
    return result


def _optional_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    selected = _required_text(value, label="manifest_sha256", maximum=128)
    if len(selected) != 64 or any(character not in "0123456789abcdefABCDEF" for character in selected):
        raise ValueError("manifest_sha256 must be a SHA-256 hexadecimal digest")
    return selected.lower()


def _epoch_path(state_directory: Path) -> Path:
    return state_directory / STATE_EPOCH_FILENAME


def _journal_path(state_directory: Path) -> Path:
    return state_directory / STATE_PUBLICATION_JOURNAL_FILENAME


def _lock_path(state_directory: Path) -> Path:
    return state_directory / STATE_PUBLICATION_LOCK_FILENAME


def _read_json_file(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StatePublicationError(f"publication metadata cannot be inspected: {path.name}") from exc
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


def _parse_epoch(value: Mapping[str, object], *, source: Literal["pointer", "journal"]) -> StateEpoch:
    if value.get("schema") != STATE_PUBLICATION_SCHEMA:
        raise StatePublicationError("publication epoch schema is incompatible")
    raw_epoch = value.get("epoch")
    if type(raw_epoch) is not int or raw_epoch < 0:
        raise StatePublicationError("publication epoch is invalid")
    raw_event = value.get("event_id")
    event_id = None if raw_event is None else _required_text(raw_event, label="event_id", maximum=256)
    raw_operation = value.get("operation")
    operation = None if raw_operation is None else _required_text(raw_operation, label="operation")
    raw_owners = value.get("owners", ())
    if not isinstance(raw_owners, list) or any(not isinstance(item, str) for item in raw_owners):
        raise StatePublicationError("publication epoch owners are invalid")
    owners = _owners(tuple(raw_owners))
    manifest = value.get("manifest_sha256")
    if manifest is not None and not isinstance(manifest, str):
        raise StatePublicationError("publication epoch manifest digest is invalid")
    return StateEpoch(
        epoch=raw_epoch,
        event_id=event_id,
        operation=operation,
        owners=owners,
        manifest_sha256=_optional_sha256(manifest),
        source=source,
    )


def _parse_publication(value: Mapping[str, object]) -> StatePublication:
    if value.get("schema") != STATE_PUBLICATION_SCHEMA:
        raise StatePublicationError("publication journal schema is incompatible")
    event_id = _required_text(value.get("event_id"), label="event_id", maximum=256)
    operation = _required_text(value.get("operation"), label="operation")
    idempotency_key = _required_text(value.get("idempotency_key"), label="idempotency_key", maximum=256)
    raw_epoch = value.get("epoch")
    raw_created = value.get("created_ns")
    if type(raw_epoch) is not int or raw_epoch < 0:
        raise StatePublicationError("publication journal epoch is invalid")
    if type(raw_created) is not int or raw_created <= 0:
        raise StatePublicationError("publication journal timestamp is invalid")
    raw_owners = value.get("owners")
    if not isinstance(raw_owners, list) or any(not isinstance(item, str) for item in raw_owners):
        raise StatePublicationError("publication journal owners are invalid")
    status = value.get("status")
    if status not in {"complete", "partial", "failed"}:
        raise StatePublicationError("publication journal status is invalid")
    manifest = value.get("manifest_sha256")
    if manifest is not None and not isinstance(manifest, str):
        raise StatePublicationError("publication journal manifest digest is invalid")
    detail = value.get("detail")
    if detail is not None:
        detail = _required_text(detail, label="detail", maximum=4096)
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
        )
    if pointer_epoch is None and journal_epoch is None:
        return StateEpoch(0, None, None, (), None, "absent")
    if pointer_epoch is None:
        return journal_epoch  # type: ignore[return-value]
    if journal_epoch is None or pointer_epoch.epoch >= journal_epoch.epoch:
        return pointer_epoch
    return journal_epoch


def read_state_publications(state_directory: str | Path) -> tuple[StatePublication, ...]:
    """Return the bounded publication journal without changing it."""

    return _read_journal(_required_state_directory(state_directory))


@contextmanager
def _publication_lock(state_directory: Path):
    lock_path = _lock_path(state_directory)
    try:
        metadata = lock_path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise StatePublicationError("publication lock cannot be inspected") from exc
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
    ):
        raise StatePublicationError("publication lock is not a regular file")
    try:
        stream = open(lock_path, "a+b", buffering=0)
    except OSError as exc:
        raise StatePublicationError("publication lock cannot be opened") from exc
    try:
        os.fchmod(stream.fileno(), 0o600)
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


def _append_journal(path: Path, publication: StatePublication) -> None:
    encoded = json.dumps(
        publication.as_payload(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\n"
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
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except OSError as exc:
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
) -> StatePublication:
    """Append one publication event and advance the epoch only on success.

    Complete events are idempotent by ``idempotency_key``.  Partial/failed
    events remain evidence but do not block a later retry of the same logical
    operation.  The journal is written before the pointer, so a pointer write
    failure remains recoverable from the append-only evidence.
    """

    selected = _required_state_directory(state_directory)
    operation = _required_text(operation, label="operation")
    selected_owners = _owners(tuple(owners))
    idempotency_key = _required_text(idempotency_key, label="idempotency_key", maximum=256)
    manifest_sha256 = _optional_sha256(manifest_sha256)
    if status not in {"complete", "partial", "failed"}:
        raise ValueError("publication status is invalid")
    if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch < 0):
        raise ValueError("expected_epoch must be a non-negative integer")
    if detail is not None:
        detail = _required_text(detail, label="detail", maximum=4096)
    digest = _idempotency_digest(operation, selected_owners, idempotency_key)
    with _publication_lock(selected):
        current = read_state_epoch(selected)
        if expected_epoch is not None and current.epoch != expected_epoch:
            raise StatePublicationConflictError(
                f"publication epoch changed: expected {expected_epoch}, observed {current.epoch}"
            )
        journal = _read_journal(selected)
        for prior in reversed(journal):
            if prior.status == "complete" and prior.idempotency_key == digest:
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
        )
        _append_journal(_journal_path(selected), publication)
        if status == "complete":
            _atomic_write_json(
                _epoch_path(selected),
                {
                    "schema": STATE_PUBLICATION_SCHEMA,
                    "epoch": epoch,
                    "event_id": event_id,
                    "operation": operation,
                    "owners": list(selected_owners),
                    "manifest_sha256": manifest_sha256,
                },
            )
        return publication


def publication_idempotency_key(*parts: object) -> str:
    """Build a deterministic bounded key for one cross-owner operation."""

    serialized = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 32_768:
        raise ValueError("publication idempotency input exceeds its bound")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_PUBLICATION_JOURNAL_BYTES",
    "MAX_PUBLICATION_RECORD_BYTES",
    "STATE_EPOCH_FILENAME",
    "STATE_PUBLICATION_JOURNAL_FILENAME",
    "STATE_PUBLICATION_LOCK_FILENAME",
    "STATE_PUBLICATION_SCHEMA",
    "PublicationStatus",
    "StateEpoch",
    "StatePublication",
    "StatePublicationConflictError",
    "StatePublicationError",
    "publication_idempotency_key",
    "read_state_epoch",
    "read_state_publications",
    "record_state_publication",
]
