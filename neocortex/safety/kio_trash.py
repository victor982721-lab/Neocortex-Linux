"""Conservative KDE/KIO Recycle Bin backend for Linux.

KIO resolves a source by path, so this adapter is explicitly
``reversible_path_bound``.  It never pretends to provide identity binding and
requires a successful, observable command plus a changed ``trash:/`` listing
before returning a receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from neocortex.deduplication.fingerprinting import stat_matches_snapshot
from neocortex.deduplication.io import absolute_display_path


KIO_RECEIPT_SCHEMA = 1
KIO_SELF_TEST_SCHEMA = 1
_CLIENT_NAMES = ("kioclient6", "kioclient5", "kioclient")


class KioTrashError(RuntimeError):
    """KIO could not complete or confirm a reversible move."""


class UnsupportedKioTrash(KioTrashError):
    """No supported KIO client is available on this Linux host."""


class KioTrashEffectUncertain(KioTrashError):
    """KIO may have moved the source but the resulting trash entry is unclear."""


class ExpectedFileSnapshot(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def volume_id(self) -> int: ...

    @property
    def file_id(self) -> int: ...

    @property
    def size(self) -> int: ...

    @property
    def mtime_ns(self) -> int: ...

    @property
    def birthtime_ns(self) -> int: ...


@dataclass(frozen=True, slots=True)
class KioTrashReceipt:
    source_path: str
    trash_entry: str
    client: str
    client_version: str | None
    source_absent: bool = True
    backend: str = "kio"
    guarantee: str = "reversible_path_bound"
    operation: str = "trash"
    receipt_type: str = "successful_return_and_observation"
    schema_version: int = KIO_RECEIPT_SCHEMA

    @property
    def trash_url(self) -> str:
        return f"trash:/{self.trash_entry}"

    def as_json(self) -> str:
        return json.dumps(
            {
                "backend": self.backend,
                "client": self.client,
                "client_version": self.client_version,
                "guarantee": self.guarantee,
                "operation": self.operation,
                "receipt_type": self.receipt_type,
                "schema_version": self.schema_version,
                "source_absent": self.source_absent,
                "source_path": self.source_path,
                "target_path": None,
                "trash_entry": self.trash_entry,
                "trash_url": self.trash_url,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class KioSelfTestReceipt:
    backend: str
    client: str
    client_version: str | None
    filesystem: str
    fixture_sha256: str
    operations: tuple[str, ...]
    result: str
    timestamp_ns: int
    schema_version: int = KIO_SELF_TEST_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "client": self.client,
            "client_version": self.client_version,
            "filesystem": self.filesystem,
            "fixture_sha256": self.fixture_sha256,
            "operations": list(self.operations),
            "result": self.result,
            "schema_version": self.schema_version,
            "timestamp_ns": self.timestamp_ns,
        }


def discover_kio_client() -> tuple[str, str | None]:
    """Return the first supported client and its reported version."""

    for name in _CLIENT_NAMES:
        path = shutil.which(name)
        if path is None:
            continue
        version: str | None = None
        try:
            result = subprocess.run(
                (path, "--version"),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                version = lines[-1] if lines else None
        except (OSError, subprocess.SubprocessError):
            version = None
        return path, version
    raise UnsupportedKioTrash("no kioclient6, kioclient5 or kioclient binary is available")


def _run_kio(client: str, *arguments: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            (client, "--noninteractive", *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KioTrashError(f"KIO command failed: {type(exc).__name__}: {exc}") from exc


def list_trash_entries(client: str | None = None) -> tuple[str, ...]:
    """Enumerate names currently exposed by ``trash:/`` through KIO."""

    selected = discover_kio_client()[0] if client is None else client
    result = _run_kio(selected, "ls", "trash:/", timeout=30.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise KioTrashError(f"KIO trash enumeration failed: {detail}")
    entries = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and line.strip() not in {".", ".."}
    }
    return tuple(sorted(entries))


def _new_trash_entry(before: set[str], after: tuple[str, ...], source_name: str) -> str:
    added = sorted(set(after) - before)
    if len(added) == 1:
        return added[0]
    matching = [entry for entry in added if entry == source_name or entry.endswith(f"-{source_name}")]
    if len(matching) == 1:
        return matching[0]
    if not added:
        raise KioTrashEffectUncertain("source disappeared but no new trash entry was observable")
    raise KioTrashEffectUncertain("KIO produced multiple or ambiguous trash entries")


def move_to_trash(
    source: Path,
    expected: ExpectedFileSnapshot,
    *,
    before_native_call: Callable[[], None],
    client: str | None = None,
    client_version: str | None = None,
) -> KioTrashReceipt:
    """Move one expected source to ``trash:/`` and return a verified receipt."""

    if os.name != "posix" or os.uname().sysname.casefold() != "linux":
        raise UnsupportedKioTrash("KIO trash backend is available only on Linux")
    if client is None:
        client, client_version = discover_kio_client()
    source_path = Path(absolute_display_path(source))
    try:
        metadata = os.stat(source_path, follow_symlinks=False)
    except OSError as exc:
        raise KioTrashError(f"trash source cannot be inspected: {source_path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise KioTrashError("KIO trash only accepts one regular non-symlink file")
    if not stat_matches_snapshot(expected, metadata):
        raise KioTrashError("trash source identity or metadata changed")
    before = set(list_trash_entries(client))
    before_native_call()
    try:
        current = os.stat(source_path, follow_symlinks=False)
    except OSError as exc:
        raise KioTrashError("trash source disappeared before KIO") from exc
    if stat.S_ISLNK(current.st_mode) or not stat_matches_snapshot(expected, current):
        raise KioTrashError("trash source changed immediately before KIO")
    result = _run_kio(client, "move", os.fspath(source_path), "trash:/")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise KioTrashError(f"KIO move failed: {detail}")
    if os.path.lexists(source_path):
        raise KioTrashEffectUncertain("KIO reported success but the source still exists")
    after = list_trash_entries(client)
    entry = _new_trash_entry(before, after, source_path.name)
    return KioTrashReceipt(
        source_path=os.fspath(source_path),
        trash_entry=entry,
        client=os.path.basename(client),
        client_version=client_version,
    )


def _filesystem_label(path: Path) -> str:
    try:
        result = subprocess.run(
            ("stat", "-f", "-c", "%T", os.fspath(path)),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        label = result.stdout.strip()
        return label or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run_self_test(
    *,
    parent: Path | None = None,
    receipt_path: Path | None = None,
) -> KioSelfTestReceipt:
    """Exercise move, enumeration, restore and hash verification on one fixture."""

    client, version = discover_kio_client()
    base = Path(tempfile.mkdtemp(prefix="neocortex-kio-selftest-", dir=None if parent is None else parent))
    fixture = base / f"fixture-{os.getpid()}-{time.time_ns()}-kio-token.bin"
    restored = base / "restored.bin"
    payload = f"neocortex-kio-selftest:{time.time_ns()}".encode("utf-8")
    fixture.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    operations = (
        "create_fixture",
        "sha256",
        "move_to_trash",
        "enumerate_trash",
        "restore_fixture",
        "verify_sha256",
        "cleanup_fixture",
    )
    try:
        expected = _snapshot_for_self_test(fixture)
        moved = move_to_trash(fixture, expected, before_native_call=lambda: None, client=client, client_version=version)
        restore = _run_kio(client, "move", moved.trash_url, os.fspath(restored), timeout=120.0)
        if restore.returncode != 0 or not restored.is_file():
            raise KioTrashError("KIO self-test restore failed")
        if hashlib.sha256(restored.read_bytes()).hexdigest() != digest:
            raise KioTrashError("KIO self-test SHA-256 verification failed")
        result = KioSelfTestReceipt(
            backend="kio",
            client=os.path.basename(client),
            client_version=version,
            filesystem=_filesystem_label(base),
            fixture_sha256=digest,
            operations=operations,
            result="passed",
            timestamp_ns=time.time_ns(),
        )
    except BaseException:
        # The fixture is synthetic; best effort cleanup is limited to this
        # dedicated directory and never touches an unrelated trash entry.
        raise
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if receipt_path is not None:
        _write_receipt(receipt_path, result)
    return result


def _snapshot_for_self_test(path: Path):
    from neocortex.deduplication import snapshot_path

    return snapshot_path(path)


def _write_receipt(path: Path, receipt: KioSelfTestReceipt) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipt.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class KioTrashBackend:
    """Reusable KIO adapter with one self-test per process/run."""

    def __init__(self, client: str | None = None, client_version: str | None = None):
        self.client = client
        self.client_version = client_version
        self._self_test: KioSelfTestReceipt | None = None

    def ensure_self_test(self, *, parent: Path | None = None, receipt_path: Path | None = None) -> KioSelfTestReceipt:
        if self.client is None:
            self.client, self.client_version = discover_kio_client()
        if self._self_test is None:
            self._self_test = run_self_test(parent=parent, receipt_path=receipt_path)
        elif receipt_path is not None and not receipt_path.exists():
            _write_receipt(receipt_path, self._self_test)
        return self._self_test

    def move_to_trash(
        self,
        source: Path,
        expected: ExpectedFileSnapshot,
        *,
        before_native_call: Callable[[], None],
    ) -> KioTrashReceipt:
        if self.client is None:
            self.client, self.client_version = discover_kio_client()
        return move_to_trash(
            source,
            expected,
            before_native_call=before_native_call,
            client=self.client,
            client_version=self.client_version,
        )


__all__ = [
    "KIO_RECEIPT_SCHEMA",
    "KIO_SELF_TEST_SCHEMA",
    "KioSelfTestReceipt",
    "KioTrashBackend",
    "KioTrashEffectUncertain",
    "KioTrashError",
    "KioTrashReceipt",
    "UnsupportedKioTrash",
    "discover_kio_client",
    "list_trash_entries",
    "move_to_trash",
    "run_self_test",
]
