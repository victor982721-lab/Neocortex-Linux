"""Optional writer-owned head receipts, bound to complete quiescent fences."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from neocortex.persistence.sqlite_immutable import capture_sqlite_immutable_fence
from .semantic_models import canonical_json
from .semantic_source_budget import source_read_checkpoint

_PREFIX = "source-head-receipt:v1:"
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_CACHE_BYTES = 2 * 1024 * 1024
_MAX_RECEIPTS = 64
REPLAY_RECEIPT_SUFFIX = ".replay-receipts.json"
_CACHE_SCHEMA = "neocortex.semantic-replay-receipts/v1"


@dataclass(slots=True)
class _HeadReceipts:
    database: Path
    records: dict[str, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)
    hits: int = 0
    projections: int = 0


_RECEIPTS: ContextVar[_HeadReceipts | None] = ContextVar("semantic_source_head_receipts", default=None)


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _receipt_path(database: Path) -> Path:
    return database.with_name(database.name + REPLAY_RECEIPT_SUFFIX)


def _load_records(database: Path) -> dict[str, str]:
    """An absent, replaced, oversized or malformed derivative is a miss."""
    path = _receipt_path(database)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CACHE_BYTES:
                return {}
            raw = stream.read(_MAX_CACHE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > _MAX_CACHE_BYTES or _file_identity(before) != _file_identity(after)
            or _file_identity(path.stat(follow_symlinks=False)) != _file_identity(after)):
            return {}
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "checksum"}:
            return {}
        payload = envelope["payload"]
        if not isinstance(payload, dict) or set(payload) != {"schema", "records"}:
            return {}
        if envelope["checksum"] != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
            return {}
        records = payload["records"]
        if payload["schema"] != _CACHE_SCHEMA or not isinstance(records, dict) or len(records) > _MAX_RECEIPTS:
            return {}
        if any(not isinstance(key, str) or len(key) > 128
               or not key.startswith((_PREFIX, "text-compatible-receipt:v1:"))
               or not isinstance(value, str)
               or len(value.encode()) > _MAX_RECEIPT_BYTES for key, value in records.items()):
            return {}
        return records
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {}


def _save_records(database: Path, records: dict[str, str]) -> None:
    """Publish only a bounded derivative; any I/O failure leaves a cache miss."""
    records = dict(list(records.items())[-_MAX_RECEIPTS:])
    payload = {"schema": _CACHE_SCHEMA, "records": records}
    raw = canonical_json({"payload": payload,
        "checksum": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}).encode()
    if len(raw) > _MAX_CACHE_BYTES:
        return
    path = _receipt_path(database)
    directory = None
    temporary = None
    try:
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            return
        temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
    except OSError:
        pass
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)


@contextmanager
def source_head_receipts(database: Path) -> Iterator[_HeadReceipts]:
    """Only an explicit indexing writer may persist these derived receipts."""
    current = _RECEIPTS.get()
    if current is not None and current.database == database:
        yield current
        return
    context = _HeadReceipts(database)
    if database.is_file():
        context.records = _load_records(database)
    token = _RECEIPTS.set(context)
    try:
        yield context
        source_read_checkpoint()
        if context.pending and database.is_file():
            _save_records(database, {**context.records, **context.pending})
    finally:
        _RECEIPTS.reset(token)


def _binding(paths: tuple[Path, ...], contract: object) -> dict[str, object]:
    owners = []
    for path in paths:
        source_read_checkpoint()
        try:
            fence = asdict(capture_sqlite_immutable_fence(path))
        except FileNotFoundError:
            fence = None
        owners.append({"path": str(path.absolute()), "fence": fence})
    return json.loads(canonical_json({"contract": contract, "owners": owners}))


def cached_source_head(
    source_kind: str, paths: tuple[Path, ...], contract: object,
    project: Callable[[], Any], decode: Callable[..., Any],
) -> Any:
    context = _RECEIPTS.get()
    if context is None:
        return project()
    try:
        binding = _binding(paths, contract)
    except (OSError, RuntimeError):
        # Active WAL or uncertain identity cannot authorize a cache hit.
        return project()
    key = _PREFIX + source_kind
    raw = context.pending.get(key, context.records.get(key))
    if raw is not None:
        try:
            receipt = json.loads(raw)
            if not isinstance(receipt, dict) or set(receipt) != {"payload", "checksum"}:
                raise ValueError("invalid source head receipt envelope")
            payload = receipt["payload"]
            if not isinstance(payload, dict) or set(payload) != {"binding", "head"}:
                raise ValueError("invalid source head receipt payload")
            checksum = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
            if receipt["checksum"] == checksum and payload["binding"] == binding:
                head = dict(payload["head"])
                head.pop("schema")
                decoded = decode(**head)
                if decoded.complete and decoded.source_kind == source_kind and _binding(paths, contract) == binding:
                    context.hits += 1
                    return decoded
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, RecursionError):
            pass
    source_read_checkpoint()
    head = project()
    context.projections += 1
    if not head.complete:
        return head
    try:
        stable = _binding(paths, contract) == binding
    except (OSError, RuntimeError):
        stable = False
    if stable:
        payload = {"binding": binding, "head": head.as_payload()}
        receipt = canonical_json({
            "payload": payload,
            "checksum": hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
        })
        if len(receipt.encode("utf-8")) <= _MAX_RECEIPT_BYTES and receipt != raw:
            context.pending[key] = receipt
    return head


def compatibility_receipt_key(model_signature: str, replay_scope: str) -> str:
    binding = canonical_json({"model": model_signature, "scope": replay_scope})
    return "text-compatible-receipt:v1:" + hashlib.sha256(binding.encode()).hexdigest()


def compatible_receipt_matches(database: Path, key: str, binding: Mapping[str, object]) -> bool:
    context = _RECEIPTS.get()
    if context is None or context.database != database:
        return False
    return context.pending.get(key, context.records.get(key)) == canonical_json(binding)


def store_compatible_receipt(database: Path, key: str, binding: Mapping[str, object]) -> None:
    context = _RECEIPTS.get()
    if context is None or context.database != database:
        return
    payload = canonical_json(binding)
    if len(payload.encode()) <= _MAX_RECEIPT_BYTES:
        context.pending[key] = payload
