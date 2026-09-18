"""Exact catalog observations that retain the original published generation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.control.cancellation import CancellationToken

RECEIPT_SCHEMA = "neocortex.catalog-observation/v1"
RECEIPT_KEY = "publication_observation"


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
    from .document_catalog_schema import _GENERATION_DIGEST_COLUMNS

    columns = ",".join(_GENERATION_DIGEST_COLUMNS)
    current = f"SELECT {columns} FROM documents WHERE source_kind=? AND active=1"
    published = f"SELECT {columns} FROM catalog_generation_documents WHERE generation_id=?"
    return (
        connection.execute(f"SELECT 1 FROM ({current} EXCEPT {published}) LIMIT 1", (source_kind, generation_id)).fetchone() is None
        and connection.execute(f"SELECT 1 FROM ({published} EXCEPT {current}) LIMIT 1", (generation_id, source_kind)).fetchone() is None
    )

