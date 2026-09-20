"""Exact catalog observations that retain the original published generation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.control.cancellation import CancellationToken

RECEIPT_SCHEMA = "neocortex.catalog-observation/v1"
RECEIPT_KEY = "publication_observation"


def _catalog_database_stamp(connection: sqlite3.Connection) -> tuple[str, tuple[int, ...] | None]:
    for row in connection.execute("PRAGMA database_list"):
        if row[1] != "main":
            continue
        path = str(row[2])
        if not path:
            return path, None
        metadata = Path(path).stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("catalog observation requires a regular database owner")
        return path, (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )
    raise ValueError("catalog observation has no main database owner")


@dataclass(frozen=True, slots=True)
class CatalogReadFence:
    """Bind completed reads to the same connection's next writer transaction.

    Capture before BEGIN DEFERRED, then close that snapshot before comparing
    under BEGIN IMMEDIATE. data_version detects other connections' commits;
    total_changes also rejects local writes, including rolled-back writes.
    The physical stamp rejects replacement of the file behind this connection.
    This is short-lived evidence, never a durable substitute for validation.
    """

    data_version: int
    total_changes: int
    database_stamp: tuple[str, tuple[int, ...] | None]

    @classmethod
    def capture(cls, connection: sqlite3.Connection) -> CatalogReadFence:
        if connection.in_transaction:
            raise ValueError("catalog observation requires a fresh read transaction")
        return cls(
            data_version=int(connection.execute("PRAGMA main.data_version").fetchone()[0]),
            total_changes=connection.total_changes,
            database_stamp=_catalog_database_stamp(connection),
        )

    def matches(self, connection: sqlite3.Connection) -> bool:
        try:
            return (
                connection.total_changes == self.total_changes
                and int(connection.execute("PRAGMA main.data_version").fetchone()[0]) == self.data_version
                and _catalog_database_stamp(connection) == self.database_stamp
            )
        except (OSError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class CatalogPublicationFence:
    """Connection-local evidence for a prepared catalog publication.

    ``PRAGMA data_version`` is intentionally absent here.  It is a database-
    wide counter: a commit for an unrelated ``source_kind`` changes it on
    every sibling connection and used to make an otherwise valid publication
    fail after the bounded retry loop.  Publication callers separately
    revalidate the source-scoped generation, manifest and correction
    evidence, while this fence retains the two local/physical checks that do
    not depend on another owner making progress.
    """

    total_changes: int
    database_identity: tuple[str, tuple[int, int] | None]

    @classmethod
    def capture(cls, connection: sqlite3.Connection) -> "CatalogPublicationFence":
        if connection.in_transaction:
            raise ValueError("catalog publication requires a fresh read transaction")
        path, stamp = _catalog_database_stamp(connection)
        identity = None if stamp is None else (stamp[0], stamp[1])
        return cls(
            total_changes=connection.total_changes,
            database_identity=(path, identity),
        )

    def matches(self, connection: sqlite3.Connection) -> bool:
        try:
            path, stamp = _catalog_database_stamp(connection)
            identity = None if stamp is None else (stamp[0], stamp[1])
            return (
                connection.total_changes == self.total_changes
                and (path, identity) == self.database_identity
            )
        except (OSError, ValueError):
            return False


def begin_catalog_write(connection: sqlite3.Connection, cancellation: CancellationToken | None) -> None:
    """Admit cancellation between bounded SQLite busy waits.

    The caller has closed its read snapshot and removed its SQL progress
    callback. SQLite does not call that callback while its busy handler waits.
    Keep the original total wait allowance and restore it on every exit.
    """

    if cancellation is None:
        connection.execute("BEGIN IMMEDIATE")
        return
    original_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    started = time.monotonic()
    try:
        while True:
            cancellation.checkpoint()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            remaining_ms = max(0, original_timeout - elapsed_ms)
            connection.execute(f"PRAGMA busy_timeout={min(100, remaining_ms)}")
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                cancellation.checkpoint()
                error_code = getattr(exc, "sqlite_errorcode", None)
                if (
                    error_code is None
                    or error_code & 0xFF != sqlite3.SQLITE_BUSY
                    or (time.monotonic() - started) * 1000 >= original_timeout
                ):
                    raise
    finally:
        connection.execute(f"PRAGMA busy_timeout={original_timeout}")


@dataclass(frozen=True, slots=True)
class CatalogClassificationEvidence:
    input_digest: str
    input_count: int
    classifier_signature: str
    max_text_chars: int
    corrections_digest: str


class CatalogInputDigest:
    """Bounded, ordered digest of every classification input field."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256(b"NEOCORTEX_CATALOG_INPUT_V1\0")
        self.count = 0

    def add(self, document: object) -> None:
        if not is_dataclass(document) or isinstance(document, type):
            raise TypeError("catalog input must be a dataclass instance")
        payload = json.dumps(asdict(document), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        self._digest.update(len(payload).to_bytes(8, "big"))
        self._digest.update(payload)
        self.count += 1

    @property
    def digest(self) -> str:
        result = self._digest.copy()
        result.update(self.count.to_bytes(8, "big"))
        return result.hexdigest()


def document_input_marker(document: object, classifier_signature: str, max_text_chars: int) -> dict[str, object]:
    inputs = CatalogInputDigest()
    inputs.add(document)
    return {
        "schema": "neocortex.catalog-document-input/v1",
        "document_digest": inputs.digest,
        "classifier_signature": classifier_signature,
        "max_text_chars": max_text_chars,
    }


@contextmanager
def catalog_sql_cancellation(connection: sqlite3.Connection, cancellation: CancellationToken | None):
    """Install control only on the fresh, exclusively owned catalog/read connection.

    SQLiteReadSession has already removed its preparation handler before
    yielding the source. This scope never takes an arbitrary borrowed writer.
    """
    if cancellation is None:
        yield
        return
    failure: BaseException | None = None

    def checkpoint() -> int:
        nonlocal failure
        try:
            cancellation.checkpoint()
        except BaseException as exc:
            failure = exc
            return 1
        return 0

    cancellation.checkpoint()
    connection.set_progress_handler(checkpoint, 1000)
    try:
        yield
    except sqlite3.DatabaseError as exc:
        if failure is not None:
            raise failure from exc
        raise
    finally:
        connection.set_progress_handler(None, 0)


@dataclass(frozen=True, slots=True)
class CatalogReplayReceipt:
    observation_catalog_run_id: int
    generation_id: int
    producer_catalog_run_id: int
    source_kind: str
    source_path: str
    source_fence_json: str
    source_root: str | None
    source_root_identity_json: str | None
    input_policy_signature: str | None
    classifier_signature: str
    max_text_chars: int
    corrections_digest: str
    input_digest: str
    input_count: int
    generation_digest: str
    input_manifest_digest: str

    def payload(self) -> dict[str, object]:
        values = {"schema": RECEIPT_SCHEMA, **asdict(self)}
        raw = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return {**values, "receipt_digest": hashlib.sha256(raw).hexdigest()}

    @classmethod
    def from_payload(cls, value: object) -> CatalogReplayReceipt:
        if not isinstance(value, dict):
            raise ValueError("catalog observation must be a mapping")
        fields = set(cls.__dataclass_fields__)
        if set(value) != fields | {"schema", "receipt_digest"} or value["schema"] != RECEIPT_SCHEMA:
            raise ValueError("catalog observation schema is invalid")
        values = {key: value[key] for key in fields}
        for name in ("observation_catalog_run_id", "generation_id", "producer_catalog_run_id", "max_text_chars", "input_count"):
            minimum = 0 if name == "input_count" else 1
            if type(values[name]) is not int or values[name] < minimum:
                raise ValueError(f"catalog observation {name} is invalid")
        nullable = {"source_root", "source_root_identity_json", "input_policy_signature"}
        numeric = {"observation_catalog_run_id", "generation_id", "producer_catalog_run_id", "max_text_chars", "input_count"}
        for name in fields - numeric:
            if values[name] is None and name in nullable:
                continue
            if not isinstance(values[name], str) or not values[name]:
                raise ValueError(f"catalog observation {name} is invalid")
        receipt = cls(**values)
        if receipt.payload()["receipt_digest"] != value["receipt_digest"]:
            raise ValueError("catalog observation digest changed")
        return receipt


def latest_receipt(connection: sqlite3.Connection, source_kind: str) -> CatalogReplayReceipt | None:
    row = connection.execute(
        "SELECT catalog_run_id,summary_json FROM catalog_runs "
        "WHERE source_kind=? AND mode='classify' AND status='completed' "
        "ORDER BY catalog_run_id DESC LIMIT 1", (source_kind,),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    raw = str(row[1])
    if len(raw.encode("utf-8")) > 65_536:
        raise ValueError("catalog observation exceeds its metadata limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or RECEIPT_KEY not in payload:
        return None
    receipt = CatalogReplayReceipt.from_payload(payload[RECEIPT_KEY])
    if receipt.observation_catalog_run_id != int(row[0]) or receipt.source_kind != source_kind:
        raise ValueError("catalog observation is detached from its run")
    return receipt


def corrections_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256(b"NEOCORTEX_CATALOG_CORRECTIONS_V1\0")
    for row in connection.execute(
        "SELECT * FROM classification_corrections ORDER BY correction_id"
    ):
        payload = repr(tuple(row)).encode("utf-8", "surrogatepass")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def current_projection_matches(connection: sqlite3.Connection, generation_id: int, source_kind: str) -> bool:
    """Compare every publication field once through the unique document keys.

    The reverse lookup also rejects missing current rows or foreign source
    kinds in the generation. Unary plus removes column affinity, preserving
    EXCEPT's storage-value comparison; IS retains its NULL equality. No accepted
    result or database identity is cached between observations.
    """
    from .document_catalog_schema import _GENERATION_DIGEST_COLUMNS

    current = ",".join(f"+current.{column}" for column in _GENERATION_DIGEST_COLUMNS)
    published = ",".join(f"+published.{column}" for column in _GENERATION_DIGEST_COLUMNS)
    return (
        connection.execute(
            f"""SELECT 1 FROM documents AS current
            LEFT JOIN catalog_generation_documents AS published
            ON published.generation_id=? AND published.source_kind=current.source_kind
            AND published.file_key=current.file_key
            WHERE current.source_kind=? AND current.active=1
            AND (published.generation_id IS NULL OR ({current}) IS NOT ({published}))
            LIMIT 1""", (generation_id, source_kind),
        ).fetchone() is None
        and connection.execute(
            """SELECT 1 FROM catalog_generation_documents AS published
            WHERE published.generation_id=? AND NOT EXISTS(
                SELECT 1 FROM documents AS current
                WHERE current.source_kind=published.source_kind
                AND current.file_key=published.file_key
                AND current.source_kind=? AND current.active=1)
            LIMIT 1""", (generation_id, source_kind),
        ).fetchone() is None
    )
