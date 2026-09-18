"""Owner-local Text derivation attempts, receipts, lineage and impact queries.

``begin_text_derivation_attempt`` commits a durable ``running`` fact before
expensive work starts.  Terminal writers deliberately accept a caller-owned
connection and never commit: the Text route must publish its document/FTS
changes, terminal receipt and outbox event in one SQLite transaction.
"""

from __future__ import annotations
import json
import math
import sqlite3
import time
import zlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from neocortex.semantic.derivation_contracts import (
    DERIVATION_CONTRACT_SCHEMA_VERSION,
    MAX_IDENTIFIER_CHARS,
    CapabilityFailure,
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
    WorkOutcome,
    WorkReceipt,
)
from neocortex.knowledge.knowledge_contracts import (
    PhysicalIdentityRef,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.foundation.file_identity import FileIdentityEncoding, decode_file_identity
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text
from .text_state import TEXT_SCHEMA_VERSION, _validate_reader, text_database
from .text_fts_lookup import text_fts_file_key_predicate, text_route_lookups_available


_TEXT_OWNER = "text"
MAX_TEXT_LINEAGE_ROWS = 1_000
_MAX_ABANDONED_DURATION_NS = 366 * 24 * 60 * 60 * 1_000_000_000
_SENSITIVE_CONFIGURATION_SUFFIXES = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
)
_REDACTED_CONFIGURATION_VALUES = {"[redacted]", "<redacted>"}
_TEXT_REPRESENTATION_KIND = "text_representation"
_TEXT_FTS_KIND = "text_fts"
_TEXT_FTS_SIGNATURE = "sqlite-fts5-unicode61-remove-diacritics-2-v1"
_TEXT_PUBLICATION_VALIDATION_BATCH = 16
_TEXT_ABANDONMENT_BATCH = 250
_TEXT_OUTBOX_PAGE_BYTES = 8 * 1024 * 1024


def compute_text_representation_fingerprint(
    *,
    text: str,
    content_kind: str,
    media_type: str,
    title: str | None,
    author: str | None,
    metadata: Mapping[str, object],
    truncated: bool,
    detail: str | None,
) -> str:
    text_fingerprint = fingerprint_text(text)
    return fingerprint_text(
        canonical_json(
            {
                "author": author,
                "content_kind": content_kind,
                "detail": detail,
                "media_type": media_type,
                "metadata": dict(metadata),
                "text_bytes": text_fingerprint.byte_count,
                "text_chars": len(text),
                "text_truncated": truncated,
                "text_xxh3_128": text_fingerprint.xxh3_128,
                "title": title,
            }
        )
    ).xxh3_128


def compute_text_fts_fingerprint(
    file_key: str,
    *,
    text: str,
    content_kind: str,
    title: str | None,
    author: str | None,
) -> str:
    return fingerprint_text(
        canonical_json(
            {
                "author": author or "",
                "body_chars": len(text),
                "body_xxh3_128": fingerprint_text(text).xxh3_128,
                "content_kind": content_kind,
                "file_key": file_key,
                "title": title or "",
                "tokenizer": _TEXT_FTS_SIGNATURE,
            }
        )
    ).xxh3_128


class TextDerivationIntegrityError(RuntimeError):
    """Persisted Text receipt/outbox facts contradict their owner contract."""


@dataclass(frozen=True, slots=True)
class TextDerivationAttemptStart:
    """All immutable facts known before a Text stage begins work."""

    attempt_id: str
    stage: StageDescriptor
    inputs: tuple[InputBinding, ...]
    effective_configuration: tuple[tuple[str, str | int | float | bool | None], ...]
    runtime: tuple[tuple[str, str], ...]
    started_at_utc: str
    started_monotonic_ns: int
    attempt: int
    run_id: str
    correlation_id: str
    recorded_ns: int
    causation_id: str | None = None

    def __post_init__(self) -> None:
        _required_text("attempt_id", self.attempt_id)
        if len(self.attempt_id) > MAX_IDENTIFIER_CHARS:
            raise ValueError(f"attempt_id cannot exceed {MAX_IDENTIFIER_CHARS} characters")
        _nonnegative_integer("started_monotonic_ns", self.started_monotonic_ns)
        _nonnegative_integer("recorded_ns", self.recorded_ns)
        terminal_probe = WorkReceipt(
            receipt_id="text-attempt-contract-validation",
            owner=_TEXT_OWNER,
            stage=self.stage,
            inputs=self.inputs,
            outputs=(),
            effective_configuration=self.effective_configuration,
            runtime=self.runtime,
            started_at_utc=self.started_at_utc,
            finished_at_utc=self.started_at_utc,
            duration_ns=0,
            attempt=self.attempt,
            outcome=WorkOutcome.ABANDONED,
            execution_mode=WorkExecutionMode.UNKNOWN,
            reproducibility=ReproducibilityClass.BEST_EFFORT,
            run_id=self.run_id,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            failure=CapabilityFailure(
                self.stage.stage_id,
                "attempt_contract_validation",
                "Attempt contract validation probe.",
                False,
            ),
        )
        object.__setattr__(
            self,
            "effective_configuration",
            terminal_probe.effective_configuration,
        )
        object.__setattr__(self, "runtime", terminal_probe.runtime)


@dataclass(frozen=True, slots=True)
class TextMaterializationLineage:
    name: str
    materialization: MaterializationRef
    fingerprint_algorithm: str
    fingerprint: str
    producer_receipt_id: str
    current_head: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "materialization": self.materialization.to_dict(),
            "fingerprint_algorithm": self.fingerprint_algorithm,
            "fingerprint": self.fingerprint,
            "producer_receipt_id": self.producer_receipt_id,
            "current_head": self.current_head,
        }


@dataclass(frozen=True, slots=True)
class TextDocumentLineage:
    file_key: str | None
    path: str | None
    document_status: str
    attribution: str
    revision: RevisionRef | None
    receipts: tuple[str, ...]
    materializations: tuple[TextMaterializationLineage, ...]
    receipt_count: int
    materialization_count: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["revision"] = self.revision.to_dict() if self.revision is not None else None
        payload["materializations"] = [item.to_dict() for item in self.materializations]
        payload["receipt_window_truncated"] = self.receipt_count > len(self.receipts)
        payload["materialization_window_truncated"] = self.materialization_count > len(
            self.materializations
        )
        return payload


@dataclass(frozen=True, slots=True)
class TextCacheObservation:
    """A committed publication observation scoped to exactly one connection."""

    connection: sqlite3.Connection = field(repr=False, compare=False)
    data_version: int
    total_changes: int


def _text_cache_observation(connection: sqlite3.Connection) -> TextCacheObservation | None:
    # A later rollback could invalidate uncommitted source rows without another
    # total_changes increment.  Such reads never grant a replay observation.
    if connection.in_transaction:
        return None
    return TextCacheObservation(
        connection, int(connection.execute("PRAGMA data_version").fetchone()[0]),
        connection.total_changes,
    )


def validate_text_cache_observation(
    connection: sqlite3.Connection, observation: TextCacheObservation,
) -> None:
    if (
        observation.connection is not connection
        or observation.data_version != int(connection.execute("PRAGMA data_version").fetchone()[0])
        or observation.total_changes != connection.total_changes
    ):
        raise TextDerivationIntegrityError("Text publication changed after its cache observation")


@dataclass(frozen=True, slots=True)
class TextValidatedRepresentation:
    file_key: str
    revision_id: str
    resource_id: str
    processing_signature: str
    path: str
    text: str
    content_kind: str
    media_type: str
    title: str | None
    author: str | None
    metadata_json: str
    truncated: bool
    detail: str | None
    representation_fingerprint: str
    fts_fingerprint: str


@dataclass(frozen=True, slots=True)
class TextReusableDerivation:
    producer_receipt_id: str
    revision: RevisionRef
    outputs: tuple[OutputBinding, ...]
    representation: TextValidatedRepresentation | None = field(default=None, kw_only=True, compare=False)
    observation: TextCacheObservation | None = field(default=None, kw_only=True, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class TextDerivationDependency:
    receipt_id: str
    stage_id: str
    stage_version: str
    processing_signature: str
    outcome: WorkOutcome
    outputs: tuple[TextMaterializationLineage, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "stage_id": self.stage_id,
            "stage_version": self.stage_version,
            "processing_signature": self.processing_signature,
            "outcome": self.outcome.value,
            "outputs": [item.to_dict() for item in self.outputs],
        }


@dataclass(frozen=True, slots=True)
class TextDerivationDependencyPage:
    items: tuple[TextDerivationDependency, ...]
    total_count: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class TextDerivationImpact:
    stage_id: str
    expected_processing_signature: str
    stale: tuple[TextMaterializationLineage, ...]
    total_count: int
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "stage_id": self.stage_id,
            "expected_processing_signature": self.expected_processing_signature,
            "stale_count": self.total_count,
            "stale_window_count": len(self.stale),
            "truncated": self.truncated,
            "stale": [item.to_dict() for item in self.stale],
        }


@dataclass(frozen=True, slots=True)
class TextDerivationOutboxEvent:
    sequence: int
    event_id: str
    event_type: str
    attempt_id: str
    receipt_id: str
    occurred_ns: int
    payload_json: str


@dataclass(frozen=True, slots=True)
class TextWorkReceiptRecord:
    receipt_id: str
    attempt_id: str
    receipt_fingerprint: str
    outcome: WorkOutcome
    recorded_ns: int
    payload_json: str


def _validate_text_publication_rows(
    file_key: str,
    revision_id: str,
    document: sqlite3.Row | None,
    fts_rows: tuple[sqlite3.Row, ...],
    revision: sqlite3.Row | None,
    rows: tuple[sqlite3.Row, ...],
) -> tuple[tuple[str, ...], TextValidatedRepresentation]:
    if (
        document is None
        or str(document["status"]) != "complete"
        or document["revision_id"] is None
        or str(document["revision_id"]) != revision_id
        or document["text_zlib"] is None
        or document["text_xxh3_128"] is None
    ):
        raise TextDerivationIntegrityError(f"current Text publication is incomplete: {file_key}")
    try:
        text = zlib.decompress(bytes(document["text_zlib"])).decode("utf-8", "strict")
        metadata = json.loads(str(document["metadata_json"]))
    except (TypeError, UnicodeError, ValueError, zlib.error) as exc:
        raise TextDerivationIntegrityError(
            f"current Text representation is unreadable: {file_key}"
        ) from exc
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise TextDerivationIntegrityError(f"current Text metadata is malformed: {file_key}")
    text_fingerprint = fingerprint_text(text)
    physical_match = (
        len(fts_rows) == 1
        and int(document["text_chars"]) == len(text)
        and str(document["text_xxh3_128"]) == text_fingerprint.xxh3_128
    )
    if physical_match:
        fts = fts_rows[0]
        physical_match = (
            str(fts["file_key"]) == file_key
            and str(fts["path"]) == str(document["path"])
            and str(fts["content_kind"]) == str(document["content_kind"])
            and str(fts["title"]) == ("" if document["title"] is None else str(document["title"]))
            and str(fts["author"])
            == ("" if document["author"] is None else str(document["author"]))
            and str(fts["body"]) == text
        )
    if not physical_match:
        raise TextDerivationIntegrityError(
            f"current Text physical outputs contradict their document: {file_key}"
        )
    if revision is None:
        raise TextDerivationIntegrityError(f"current Text revision is missing: {revision_id}")
    resource_id = str(revision["resource_id"])
    expected = {
        "text_representation": (
            _TEXT_REPRESENTATION_KIND,
            compute_text_representation_fingerprint(
                text=text,
                content_kind=str(document["content_kind"]),
                media_type=str(document["media_type"]),
                title=None if document["title"] is None else str(document["title"]),
                author=None if document["author"] is None else str(document["author"]),
                metadata=metadata,
                truncated=bool(document["text_truncated"]),
                detail=None if document["detail"] is None else str(document["detail"]),
            ),
        ),
        "text_fts": (
            _TEXT_FTS_KIND,
            compute_text_fts_fingerprint(
                file_key,
                text=text,
                content_kind=str(document["content_kind"]),
                title=None if document["title"] is None else str(document["title"]),
                author=None if document["author"] is None else str(document["author"]),
            ),
        ),
    }
    if len(rows) != len(expected) or {str(row["binding_name"]) for row in rows} != set(expected):
        raise TextDerivationIntegrityError(
            f"current Text publication requires exactly two output heads: {file_key}"
        )
    for row in rows:
        output_name = str(row["binding_name"])
        expected_kind, expected_fingerprint = expected[output_name]
        if (
            str(row["resource_id"]) != resource_id
            or str(row["materialization_kind"]) != expected_kind
            or str(row["materialization_owner"]) != _TEXT_OWNER
            or str(row["revision_id"]) != revision_id
            or str(row["producer_receipt_id"]) != str(row["output_receipt_id"])
            or str(row["kind"]) != expected_kind
            or int(row["schema_version"]) != TEXT_SCHEMA_VERSION
            or str(row["output_resource_id"]) != resource_id
            or str(row["output_revision_id"]) != revision_id
            or str(row["fingerprint_algorithm"]) != HASH_ALGORITHM_128
            or str(row["binding_algorithm"]) != HASH_ALGORITHM_128
            or str(row["fingerprint"]) != expected_fingerprint
            or str(row["binding_fingerprint"]) != expected_fingerprint
            or str(row["processing_signature"]) != str(document["processing_signature"])
        ):
            raise TextDerivationIntegrityError(
                f"current Text output head contradicts its publication: {file_key}"
            )
    representation = TextValidatedRepresentation(
        file_key=file_key,
        revision_id=revision_id,
        resource_id=resource_id,
        processing_signature=str(document["processing_signature"]),
        path=str(document["path"]),
        text=text,
        content_kind=str(document["content_kind"]),
        media_type=str(document["media_type"]),
        title=None if document["title"] is None else str(document["title"]),
        author=None if document["author"] is None else str(document["author"]),
        metadata_json=str(document["metadata_json"]),
        truncated=bool(document["text_truncated"]),
        detail=None if document["detail"] is None else str(document["detail"]),
        representation_fingerprint=expected["text_representation"][1],
        fts_fingerprint=expected["text_fts"][1],
    )
    return tuple({str(row["producer_receipt_id"]) for row in rows}), representation


def validate_text_publications_from_connection(
    connection: sqlite3.Connection,
    publications: tuple[tuple[str, str], ...],
    *,
    lookup_available: bool | None = None,
    _representations: dict[str, TextValidatedRepresentation] | None = None,
) -> None:
    """Validate a bounded Text publication window with set-based owner queries."""

    if not isinstance(publications, tuple):
        raise ValueError("publications must be an immutable tuple")
    if len(publications) > 1_000:
        raise ValueError("publications cannot contain more than 1000 values")
    normalized: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    by_file_key: dict[str, str] = {}
    for index, publication in enumerate(publications):
        if not isinstance(publication, tuple) or len(publication) != 2:
            raise ValueError(f"publications[{index}] must be a file/revision pair")
        file_key = _required_text(f"publications[{index}].file_key", publication[0])
        revision_id = _required_text(f"publications[{index}].revision_id", publication[1])
        prior = by_file_key.setdefault(file_key, revision_id)
        if prior != revision_id:
            raise ValueError(f"file_key {file_key!r} cannot identify two revisions")
        if (file_key, revision_id) not in seen_pairs:
            normalized.append((file_key, revision_id))
            seen_pairs.add((file_key, revision_id))

    if lookup_available is None:
        lookup_available = bool(normalized) and text_route_lookups_available(connection)
    for offset in range(0, len(normalized), _TEXT_PUBLICATION_VALIDATION_BATCH):
        batch = normalized[offset : offset + _TEXT_PUBLICATION_VALIDATION_BATCH]
        file_keys = tuple(file_key for file_key, _revision_id in batch)
        revision_ids = tuple(dict.fromkeys(revision_id for _file_key, revision_id in batch))
        file_placeholders = ",".join("?" for _item in file_keys)
        revision_placeholders = ",".join("?" for _item in revision_ids)
        documents = connection.execute(
            f"SELECT * FROM documents WHERE file_key IN ({file_placeholders})",
            file_keys,
        ).fetchall()
        documents_by_key = {str(row["file_key"]): row for row in documents}
        fts_predicate, fts_parameters = text_fts_file_key_predicate(
            connection, file_keys, lookup_available=lookup_available
        )
        fts_rows = connection.execute(
            f"""SELECT file_key,path,content_kind,title,author,body
            FROM document_fts WHERE {fts_predicate}
            ORDER BY file_key""",
            fts_parameters,
        ).fetchall()
        fts_by_key: dict[str, list[sqlite3.Row]] = {}
        for row in fts_rows:
            fts_by_key.setdefault(str(row["file_key"]), []).append(row)
        revisions = connection.execute(
            f"""SELECT revision_id,resource_id FROM text_input_revisions
            WHERE revision_id IN ({revision_placeholders})""",
            revision_ids,
        ).fetchall()
        revisions_by_id = {str(row["revision_id"]): row for row in revisions}
        resource_ids = tuple(dict.fromkeys(str(row["resource_id"]) for row in revisions))
        heads: list[sqlite3.Row] = []
        if resource_ids:
            resource_placeholders = ",".join("?" for _item in resource_ids)
            heads = connection.execute(
                f"""SELECT h.resource_id,h.materialization_kind,
                h.materialization_owner,h.materialization_id,h.revision_id,
                h.producer_receipt_id,m.kind,m.schema_version,
                m.resource_id AS output_resource_id,
                m.revision_id AS output_revision_id,
                m.producer_receipt_id AS output_receipt_id,
                m.fingerprint_algorithm,m.fingerprint,ob.binding_name,
                ob.fingerprint_algorithm AS binding_algorithm,
                ob.fingerprint AS binding_fingerprint,a.processing_signature
                FROM text_materialization_heads h
                JOIN text_materializations m
                  ON m.owner=h.materialization_owner
                 AND m.materialization_id=h.materialization_id
                JOIN text_work_receipts wr ON wr.receipt_id=m.producer_receipt_id
                JOIN text_derivation_attempts a ON a.attempt_id=wr.attempt_id
                JOIN text_derivation_output_bindings ob
                  ON ob.attempt_id=wr.attempt_id
                 AND ob.materialization_owner=m.owner
                 AND ob.materialization_id=m.materialization_id
                WHERE h.resource_id IN ({resource_placeholders})
                ORDER BY h.resource_id,h.materialization_kind""",
                resource_ids,
            ).fetchall()
        heads_by_resource: dict[str, list[sqlite3.Row]] = {}
        for row in heads:
            heads_by_resource.setdefault(str(row["resource_id"]), []).append(row)
        receipt_ids: list[str] = []
        representations: dict[str, TextValidatedRepresentation] = {}
        for file_key, revision_id in batch:
            revision = revisions_by_id.get(revision_id)
            resource_id = None if revision is None else str(revision["resource_id"])
            publication_heads = (
                () if resource_id is None else tuple(heads_by_resource.get(resource_id, ()))
            )
            validated_receipts, representation = _validate_text_publication_rows(
                    file_key,
                    revision_id,
                    documents_by_key.get(file_key),
                    tuple(fts_by_key.get(file_key, ())),
                    revision,
                    publication_heads,
                )
            receipt_ids.extend(validated_receipts)
            if _representations is not None:
                representations[file_key] = representation
        _validated_terminal_receipts(
            connection, tuple(receipt_ids), lookup_available=lookup_available
        )
        if _representations is not None:
            _representations.update(representations)


def validate_text_publication_from_connection(
    connection: sqlite3.Connection,
    file_key: str,
    revision_id: str,
    *,
    lookup_available: bool | None = None,
) -> None:
    """Validate one current Text publication through the bounded batch contract."""

    validate_text_publications_from_connection(
        connection, ((file_key, revision_id),), lookup_available=lookup_available
    )


def validate_text_failure_from_connection(
    connection: sqlite3.Connection,
    file_key: str,
    revision_id: str,
) -> None:
    """Reconcile one current error document with its exact failed receipt."""

    document = connection.execute(
        """SELECT status,processing_signature,birthtime_ns,text_zlib,text_chars,
        text_xxh3_128
        FROM documents WHERE file_key=? AND revision_id=?""",
        (file_key, revision_id),
    ).fetchone()
    revision = connection.execute(
        "SELECT resource_id FROM text_input_revisions WHERE revision_id=?",
        (revision_id,),
    ).fetchone()
    try:
        identity = decode_file_identity(
            file_key,
            encoding=FileIdentityEncoding.PACKED_HEX_V1,
        )
        expected_resource_id = (
            f"resource:file:{identity.volume_id}:{identity.file_id}:"
            f"{int(document['birthtime_ns']) if document is not None else 0}"
        )
    except (TypeError, ValueError) as exc:
        raise TextDerivationIntegrityError(
            f"current Text failure has malformed physical identity: {file_key}"
        ) from exc
    if (
        document is None
        or str(document["status"]) != "error"
        or document["text_zlib"] is not None
        or int(document["text_chars"]) != 0
        or document["text_xxh3_128"] is not None
        or revision is None
        or str(revision["resource_id"]) != expected_resource_id
    ):
        raise TextDerivationIntegrityError(
            f"current Text failure contradicts its source identity: {file_key}"
        )
    fts_predicate, fts_parameters = text_fts_file_key_predicate(connection, (file_key,))
    if (
        connection.execute(
            f"SELECT 1 FROM document_fts WHERE {fts_predicate} LIMIT 1",
            fts_parameters,
        ).fetchone()
        is not None
    ):
        raise TextDerivationIntegrityError(
            f"current Text failure unexpectedly has an FTS output: {file_key}"
        )
    if (
        connection.execute(
            """SELECT 1 FROM text_materialization_heads
        WHERE resource_id=? AND materialization_owner=? LIMIT 1""",
            (expected_resource_id, _TEXT_OWNER),
        ).fetchone()
        is not None
    ):
        raise TextDerivationIntegrityError(
            f"current Text failure unexpectedly has a materialization head: {file_key}"
        )
    rows = connection.execute(
        """SELECT DISTINCT a.receipt_id,a.terminal_ns
        FROM text_derivation_attempts a
        JOIN text_derivation_input_bindings ib ON ib.attempt_id=a.attempt_id
        WHERE a.stage_id='text.extract' AND a.processing_signature=?
          AND a.status='failed' AND ib.revision_id=? AND a.receipt_id IS NOT NULL
        ORDER BY a.terminal_ns DESC,a.receipt_id DESC LIMIT 1""",
        (str(document["processing_signature"]), revision_id),
    ).fetchall()
    if not rows:
        raise TextDerivationIntegrityError(
            f"current Text failure has no compatible failed receipt: {file_key}"
        )
    receipts = _validated_terminal_receipts(connection, (str(rows[0]["receipt_id"]),))
    if any(
        receipt.outcome is not WorkOutcome.FAILED or receipt.outputs
        for receipt in receipts.values()
    ):
        raise TextDerivationIntegrityError(
            f"current Text failure receipt is not terminally failed: {file_key}"
        )


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value


def _nonnegative_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _lineage_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("lineage limit must be an integer")
    if not 1 <= limit <= MAX_TEXT_LINEAGE_ROWS:
        raise ValueError(f"lineage limit must be between 1 and {MAX_TEXT_LINEAGE_ROWS}")
    return limit


def _validate_utc(name: str, value: str) -> None:
    _required_text(name, value)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must identify UTC explicitly")


def _canonical_pairs(
    name: str,
    values: tuple[tuple[str, object], ...],
    *,
    strings_only: bool = False,
) -> str:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be an immutable tuple of pairs")
    normalized: dict[str, object] = {}
    for index, pair in enumerate(values):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError(f"{name}[{index}] must be a pair")
        key, value = pair
        _required_text(f"{name}[{index}].key", key)
        if key in normalized:
            raise ValueError(f"{name} cannot contain duplicate key {key!r}")
        if strings_only:
            _required_text(f"{name}[{index}].value", value)
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError(f"{name}[{index}].value must be a JSON scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name}[{index}].value must be finite")
        normalized_key = key.casefold().replace("-", "_").replace(".", "_")
        sensitive = any(
            normalized_key == suffix or normalized_key.endswith("_" + suffix)
            for suffix in _SENSITIVE_CONFIGURATION_SUFFIXES
        )
        redacted = value is None or (
            isinstance(value, str) and value.casefold() in _REDACTED_CONFIGURATION_VALUES
        )
        if not strings_only and sensitive and not redacted:
            raise ValueError(f"{name}[{index}].value must be redacted for sensitive key {key!r}")
        normalized[key] = value
    return canonical_json(normalized)


def _revision_values(binding: InputBinding, recorded_ns: int) -> tuple[object, ...]:
    revision = binding.revision
    return (
        revision.revision_id,
        revision.resource_id,
        revision.producer,
        revision.processing_signature,
        revision.generation,
        revision.state.value,
        revision.observed_at_utc,
        binding.fingerprint_algorithm,
        binding.fingerprint,
        recorded_ns,
    )


def _persist_input_revision(
    connection: sqlite3.Connection,
    binding: InputBinding,
    recorded_ns: int,
) -> None:
    values = _revision_values(binding, recorded_ns)
    connection.execute(
        """INSERT OR IGNORE INTO text_input_revisions(
        revision_id,resource_id,producer,processing_signature,generation,
        revision_state,observed_at_utc,fingerprint_algorithm,fingerprint,recorded_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    row = connection.execute(
        """SELECT revision_id,resource_id,producer,processing_signature,generation,
        revision_state,observed_at_utc,fingerprint_algorithm,fingerprint
        FROM text_input_revisions WHERE revision_id=?""",
        (binding.revision.revision_id,),
    ).fetchone()
    if row is None or tuple(row) != values[:-1]:
        raise ValueError(
            f"immutable Text input revision conflicts with {binding.revision.revision_id!r}"
        )


def _materialization_columns(
    materialization: MaterializationRef | None,
) -> tuple[object | None, ...]:
    if materialization is None:
        return (None, None, None, None, None, None)
    return (
        materialization.owner,
        materialization.kind,
        materialization.materialization_id,
        materialization.schema_version,
        materialization.generation,
        materialization.to_json(),
    )


def begin_text_derivation_attempt_from_connection(
    connection: sqlite3.Connection,
    start: TextDerivationAttemptStart,
    *,
    cache_observation: TextCacheObservation | None = None,
) -> TextCacheObservation | None:
    """Commit a running attempt through one caller-owned, currently idle connection."""

    if not isinstance(start, TextDerivationAttemptStart):
        raise TypeError("start must be a TextDerivationAttemptStart")
    if connection.in_transaction:
        raise ValueError("Text attempt begin requires an idle caller connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if cache_observation is not None:
            validate_text_cache_observation(connection, cache_observation)
        for binding in start.inputs:
            _persist_input_revision(connection, binding, start.recorded_ns)
        stage = start.stage
        connection.execute(
            """INSERT INTO text_derivation_attempts(
            attempt_id,stage_id,stage_version,processing_signature,
            implementation_digest,provider,provider_version,model,model_version,
            model_digest,effective_configuration_json,runtime_json,started_at_utc,
            started_monotonic_ns,attempt_number,run_id,correlation_id,causation_id,
            status,recorded_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?)""",
            (
                start.attempt_id,
                stage.stage_id,
                stage.stage_version,
                stage.processing_signature,
                stage.implementation_digest,
                stage.provider,
                stage.provider_version,
                stage.model,
                stage.model_version,
                stage.model_digest,
                _canonical_pairs("effective_configuration", start.effective_configuration),
                _canonical_pairs("runtime", start.runtime, strings_only=True),
                start.started_at_utc,
                start.started_monotonic_ns,
                start.attempt,
                start.run_id,
                start.correlation_id,
                start.causation_id,
                start.recorded_ns,
            ),
        )
        for binding in start.inputs:
            connection.execute(
                """INSERT INTO text_derivation_input_bindings(
                attempt_id,binding_name,revision_id,fingerprint_algorithm,fingerprint,
                materialization_owner,materialization_kind,materialization_id,
                materialization_schema_version,materialization_generation,
                materialization_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    start.attempt_id,
                    binding.name,
                    binding.revision.revision_id,
                    binding.fingerprint_algorithm,
                    binding.fingerprint,
                    *_materialization_columns(binding.materialization),
                ),
            )
    except BaseException:
        connection.rollback()
        raise
    else:
        refreshed_observation = None if cache_observation is None else TextCacheObservation(
            connection, cache_observation.data_version, connection.total_changes,
        )
        connection.commit()
        return refreshed_observation


def begin_text_derivation_attempt(path: Path, start: TextDerivationAttemptStart) -> None:
    """Commit one running attempt before work; never claims an output."""

    with text_database(path, create=False) as connection:
        _validate_reader(connection)
        begin_text_derivation_attempt_from_connection(connection, start)


def _stage_from_row(row: sqlite3.Row) -> StageDescriptor:
    return StageDescriptor(
        stage_id=str(row["stage_id"]),
        stage_version=str(row["stage_version"]),
        processing_signature=str(row["processing_signature"]),
        implementation_digest=_optional_string(row["implementation_digest"]),
        provider=_optional_string(row["provider"]),
        provider_version=_optional_string(row["provider_version"]),
        model=_optional_string(row["model"]),
        model_version=_optional_string(row["model_version"]),
        model_digest=_optional_string(row["model_digest"]),
    )


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _revision_from_row(row: sqlite3.Row) -> RevisionRef:
    generation = row["generation"]
    return RevisionRef(
        resource_id=str(row["resource_id"]),
        revision_id=str(row["revision_id"]),
        producer=str(row["producer"]),
        processing_signature=str(row["processing_signature"]),
        generation=None if generation is None else int(generation),
        state=RevisionState(str(row["revision_state"])),
        observed_at_utc=_optional_string(row["observed_at_utc"]),
    )


def _physical_identity_from_payload(payload: object) -> PhysicalIdentityRef | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("persisted physical identity is malformed")
    return PhysicalIdentityRef(
        scheme=str(payload["scheme"]),
        value=str(payload["value"]),
        identity_version=int(payload["identity_version"]),
    )


def _resource_from_payload(payload: object) -> ResourceRef | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("persisted resource reference is malformed")
    disposition = payload.get("disposition")
    return ResourceRef(
        resource_id=str(payload["resource_id"]),
        source_kind=str(payload["source_kind"]),
        owner=str(payload["owner"]),
        physical_identity=_physical_identity_from_payload(payload.get("physical_identity")),
        current_path=_optional_string(payload.get("current_path")),
        disposition=(None if disposition is None else ResourceDisposition(str(disposition))),
        canonical_resource_id=_optional_string(payload.get("canonical_resource_id")),
    )


def _revision_from_payload(payload: object) -> RevisionRef | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("persisted revision reference is malformed")
    generation = payload.get("generation")
    return RevisionRef(
        resource_id=str(payload["resource_id"]),
        revision_id=str(payload["revision_id"]),
        producer=str(payload["producer"]),
        processing_signature=str(payload["processing_signature"]),
        generation=None if generation is None else int(generation),
        state=RevisionState(str(payload["state"])),
        observed_at_utc=_optional_string(payload.get("observed_at_utc")),
    )


def _materialization_from_json(value: str) -> MaterializationRef:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("persisted materialization reference is malformed")
    generation = payload.get("generation")
    return MaterializationRef(
        owner=str(payload["owner"]),
        kind=str(payload["materialization_kind"]),
        materialization_id=str(payload["materialization_id"]),
        schema_version=int(payload["owner_schema_version"]),
        resource=_resource_from_payload(payload.get("resource")),
        revision=_revision_from_payload(payload.get("revision")),
        generation=None if generation is None else int(generation),
    )


def _load_inputs(connection: sqlite3.Connection, attempt_id: str) -> tuple[InputBinding, ...]:
    rows = connection.execute(
        """SELECT b.binding_name,b.fingerprint_algorithm,b.fingerprint,
        b.materialization_json,r.revision_id,r.resource_id,r.producer,
        r.processing_signature,r.generation,r.revision_state,r.observed_at_utc
        FROM text_derivation_input_bindings b
        JOIN text_input_revisions r ON r.revision_id=b.revision_id
        WHERE b.attempt_id=? ORDER BY b.binding_name""",
        (attempt_id,),
    ).fetchall()
    return tuple(
        InputBinding(
            name=str(row["binding_name"]),
            revision=_revision_from_row(row),
            fingerprint=str(row["fingerprint"]),
            fingerprint_algorithm=str(row["fingerprint_algorithm"]),
            materialization=(
                None
                if row["materialization_json"] is None
                else _materialization_from_json(str(row["materialization_json"]))
            ),
        )
        for row in rows
    )


def _validated_terminal_input_bindings(
    rows: tuple[sqlite3.Row, ...],
) -> tuple[InputBinding, ...]:
    inputs = tuple(
        InputBinding(
            name=str(row["binding_name"]),
            revision=_revision_from_row(row),
            fingerprint=str(row["binding_fingerprint"]),
            fingerprint_algorithm=str(row["binding_fingerprint_algorithm"]),
            materialization=(
                None
                if row["materialization_json"] is None
                else _materialization_from_json(str(row["materialization_json"]))
            ),
        )
        for row in rows
    )
    for row, binding in zip(rows, inputs, strict=True):
        if str(row["binding_fingerprint_algorithm"]) != str(
            row["revision_fingerprint_algorithm"]
        ) or str(row["binding_fingerprint"]) != str(row["revision_fingerprint"]):
            raise ValueError("input revision fingerprint columns disagree")
        materialization = binding.materialization
        if materialization is not None and (
            str(row["materialization_owner"]) != materialization.owner
            or str(row["materialization_kind"]) != materialization.kind
            or str(row["materialization_id"]) != materialization.materialization_id
            or int(row["materialization_schema_version"]) != materialization.schema_version
            or row["materialization_generation"] != materialization.generation
        ):
            raise ValueError("input materialization columns disagree")
    return inputs


def _terminal_output_materialization_matches(
    row: sqlite3.Row,
    materialization: MaterializationRef,
) -> bool:
    return (
        str(row["materialization_owner"]) == materialization.owner
        and str(row["materialization_id"]) == materialization.materialization_id
        and str(row["materialization_kind"]) == materialization.kind
        and int(row["materialization_schema_version"]) == materialization.schema_version
        and row["materialization_generation"] == materialization.generation
        and row["materialization_resource_id"]
        == (None if materialization.resource is None else materialization.resource.resource_id)
        and row["materialization_revision_id"]
        == (None if materialization.revision is None else materialization.revision.revision_id)
        and str(row["binding_fingerprint_algorithm"])
        == str(row["materialization_fingerprint_algorithm"])
        and str(row["binding_fingerprint"]) == str(row["materialization_fingerprint"])
    )


def _terminal_output_head_matches(row: sqlite3.Row) -> bool:
    return (
        row["head_resource_id"] is None
        or (
            str(row["head_resource_id"]) == str(row["materialization_resource_id"])
            and str(row["head_materialization_kind"]) == str(row["materialization_kind"])
            and str(row["head_materialization_owner"]) == str(row["materialization_owner"])
            and str(row["head_materialization_id"]) == str(row["materialization_id"])
            and str(row["head_revision_id"]) == str(row["materialization_revision_id"])
            and str(row["head_producer_receipt_id"]) == str(row["producer_receipt_id"])
        )
    )


def _validated_terminal_output_bindings(
    rows: tuple[sqlite3.Row, ...],
) -> tuple[tuple[OutputBinding, ...], tuple[str, ...]]:
    outputs = tuple(
        OutputBinding(
            name=str(row["binding_name"]),
            materialization=_materialization_from_json(str(row["materialization_json"])),
            fingerprint=str(row["binding_fingerprint"]),
            fingerprint_algorithm=str(row["binding_fingerprint_algorithm"]),
        )
        for row in rows
    )
    for row, output in zip(rows, outputs, strict=True):
        if not _terminal_output_materialization_matches(row, output.materialization):
            raise ValueError("output materialization columns disagree")
        if not _terminal_output_head_matches(row):
            raise ValueError("materialization head columns disagree")
    return outputs, tuple(str(row["producer_receipt_id"]) for row in rows)


def _validate_terminal_outbox_row(
    rows: tuple[sqlite3.Row, ...],
    *,
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
    attempt_row: sqlite3.Row,
    payload_json: str,
) -> None:
    if len(rows) != 1:
        raise ValueError("terminal receipt must have exactly one outbox event")
    outbox = rows[0]
    receipt_id = str(receipt_row["receipt_id"])
    if (
        str(outbox["event_id"]) != f"text-outbox:{receipt_id}"
        or str(outbox["event_type"]) != f"text.work_{receipt.outcome.value}.v1"
        or str(outbox["attempt_id"]) != str(receipt_row["attempt_id"])
        or str(outbox["receipt_id"]) != receipt_id
        or int(outbox["occurred_ns"]) != int(attempt_row["terminal_ns"])
        or int(receipt_row["recorded_ns"]) != int(attempt_row["terminal_ns"])
        or str(outbox["payload_json"]) != payload_json
    ):
        raise ValueError("outbox/terminal timestamp columns disagree")


def _terminal_receipt_header_matches(
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
    attempt_row: sqlite3.Row,
) -> bool:
    return (
        receipt.receipt_id == str(receipt_row["receipt_id"])
        and receipt.owner == _TEXT_OWNER
        and receipt.contract_fingerprint == str(receipt_row["receipt_fingerprint"])
        and receipt.outcome.value == str(receipt_row["outcome"])
        and receipt.stage == _stage_from_row(attempt_row)
    )


def _terminal_receipt_bindings_match(
    receipt: WorkReceipt,
    inputs: tuple[InputBinding, ...],
    outputs: tuple[OutputBinding, ...],
) -> bool:
    return (
        len(receipt.inputs) == len(inputs)
        and {item.name: item for item in receipt.inputs} == {item.name: item for item in inputs}
        and len(receipt.outputs) == len(outputs)
        and {item.name: item for item in receipt.outputs}
        == {item.name: item for item in outputs}
    )


def _terminal_receipt_attempt_contract_matches(
    receipt: WorkReceipt,
    attempt_row: sqlite3.Row,
    configuration: Mapping[str, object],
    runtime: Mapping[str, object],
) -> bool:
    return (
        receipt.effective_configuration == tuple(configuration.items())
        and receipt.runtime == tuple((str(key), str(value)) for key, value in runtime.items())
        and receipt.run_id == str(attempt_row["run_id"])
        and receipt.correlation_id == str(attempt_row["correlation_id"])
        and receipt.causation_id == _optional_string(attempt_row["causation_id"])
    )


def _terminal_receipt_lifecycle_matches(
    receipt: WorkReceipt,
    receipt_id: str,
    attempt_row: sqlite3.Row,
) -> bool:
    failure_json = None if receipt.failure is None else receipt.failure.to_json()
    return (
        receipt.started_at_utc == str(attempt_row["started_at_utc"])
        and receipt.finished_at_utc == str(attempt_row["finished_at_utc"])
        and receipt.duration_ns == int(attempt_row["duration_ns"])
        and receipt.attempt == int(attempt_row["attempt_number"])
        and receipt.outcome.value == str(attempt_row["status"])
        and receipt.execution_mode.value == str(attempt_row["execution_mode"])
        and receipt.reproducibility.value == str(attempt_row["reproducibility_class"])
        and failure_json == _optional_string(attempt_row["failure_json"])
        and str(attempt_row["receipt_id"]) == receipt_id
    )


def _terminal_receipt_producers_match(
    receipt: WorkReceipt,
    receipt_id: str,
    producer_receipt_ids: tuple[str, ...],
) -> bool:
    if receipt.execution_mode is WorkExecutionMode.EXECUTED:
        return all(producer == receipt_id for producer in producer_receipt_ids)
    if receipt.outcome is WorkOutcome.SUCCEEDED:
        return receipt.causation_id is not None and all(
            producer == receipt.causation_id for producer in producer_receipt_ids
        )
    return True


def _validate_terminal_receipt_rows(
    receipt_row: sqlite3.Row,
    attempt_row: sqlite3.Row,
    input_rows: tuple[sqlite3.Row, ...],
    output_rows: tuple[sqlite3.Row, ...],
    outbox_rows: tuple[sqlite3.Row, ...],
) -> WorkReceipt:
    receipt_id = str(receipt_row["receipt_id"])
    payload_json = str(receipt_row["receipt_json"])
    try:
        receipt = WorkReceipt.from_json(payload_json)
        inputs = _validated_terminal_input_bindings(input_rows)
        outputs, producer_receipt_ids = _validated_terminal_output_bindings(output_rows)
        configuration = json.loads(str(attempt_row["effective_configuration_json"]))
        runtime = json.loads(str(attempt_row["runtime_json"]))
        if not isinstance(configuration, dict) or not isinstance(runtime, dict):
            raise ValueError("configuration/runtime must be JSON objects")
        _validate_terminal_outbox_row(
            outbox_rows,
            receipt=receipt,
            receipt_row=receipt_row,
            attempt_row=attempt_row,
            payload_json=payload_json,
        )
        normalized_match = (
            _terminal_receipt_header_matches(receipt, receipt_row, attempt_row)
            and _terminal_receipt_bindings_match(receipt, inputs, outputs)
            and _terminal_receipt_attempt_contract_matches(
                receipt,
                attempt_row,
                configuration,
                runtime,
            )
            and _terminal_receipt_lifecycle_matches(receipt, receipt_id, attempt_row)
            and _terminal_receipt_producers_match(receipt, receipt_id, producer_receipt_ids)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TextDerivationIntegrityError(
            f"Text WorkReceipt normalized facts are invalid: {receipt_id}"
        ) from exc
    if not normalized_match:
        raise TextDerivationIntegrityError(
            f"Text WorkReceipt contradicts normalized owner facts: {receipt_id}"
        )
    return receipt


def _validated_terminal_receipts(
    connection: sqlite3.Connection,
    receipt_ids: tuple[str, ...],
    *,
    lookup_available: bool | None = False,
) -> dict[str, WorkReceipt]:
    """Validate up to one lineage window in fixed-size, set-based SQL batches."""

    unique_ids = tuple(dict.fromkeys(receipt_ids))
    if lookup_available is None:
        lookup_available = bool(unique_ids) and text_route_lookups_available(connection)
    head_table = (
        "temp._text_materialization_heads_lookup"
        if lookup_available else "text_materialization_heads"
    )
    validated: dict[str, WorkReceipt] = {}
    for offset in range(0, len(unique_ids), 250):
        batch = unique_ids[offset : offset + 250]
        placeholders = ",".join("?" for _item in batch)
        receipt_rows = connection.execute(
            f"""SELECT receipt_id,attempt_id,receipt_json,receipt_fingerprint,outcome,
            recorded_ns
            FROM text_work_receipts WHERE receipt_id IN ({placeholders})""",
            batch,
        ).fetchall()
        receipts_by_id = {str(row["receipt_id"]): row for row in receipt_rows}
        missing = set(batch).difference(receipts_by_id)
        if missing:
            raise TextDerivationIntegrityError(f"Text WorkReceipt is missing: {min(missing)}")
        attempt_ids = tuple(str(row["attempt_id"]) for row in receipt_rows)
        attempt_placeholders = ",".join("?" for _item in attempt_ids)
        attempt_rows = connection.execute(
            f"SELECT * FROM text_derivation_attempts WHERE attempt_id IN ({attempt_placeholders})",
            attempt_ids,
        ).fetchall()
        attempts_by_id = {str(row["attempt_id"]): row for row in attempt_rows}
        input_rows = connection.execute(
            f"""SELECT b.attempt_id,b.binding_name,
            b.fingerprint_algorithm AS binding_fingerprint_algorithm,
            b.fingerprint AS binding_fingerprint,b.materialization_owner,
            b.materialization_kind,b.materialization_id,b.materialization_schema_version,
            b.materialization_generation,b.materialization_json,r.revision_id,
            r.resource_id,r.producer,r.processing_signature,r.generation,
            r.revision_state,r.observed_at_utc,
            r.fingerprint_algorithm AS revision_fingerprint_algorithm,
            r.fingerprint AS revision_fingerprint
            FROM text_derivation_input_bindings b
            JOIN text_input_revisions r ON r.revision_id=b.revision_id
            WHERE b.attempt_id IN ({attempt_placeholders})
            ORDER BY b.attempt_id,b.binding_name""",
            attempt_ids,
        ).fetchall()
        output_rows = connection.execute(
            f"""SELECT b.attempt_id,b.binding_name,
            b.fingerprint_algorithm AS binding_fingerprint_algorithm,
            b.fingerprint AS binding_fingerprint,b.materialization_owner,
            b.materialization_id,m.kind AS materialization_kind,
            m.schema_version AS materialization_schema_version,
            m.resource_id AS materialization_resource_id,
            m.revision_id AS materialization_revision_id,
            m.generation AS materialization_generation,m.materialization_json,
            m.producer_receipt_id,
            m.fingerprint_algorithm AS materialization_fingerprint_algorithm,
            m.fingerprint AS materialization_fingerprint,
            h.resource_id AS head_resource_id,
            h.materialization_kind AS head_materialization_kind,
            h.materialization_owner AS head_materialization_owner,
            h.materialization_id AS head_materialization_id,
            h.revision_id AS head_revision_id,
            h.producer_receipt_id AS head_producer_receipt_id
            FROM text_derivation_output_bindings b
            JOIN text_materializations m
              ON m.owner=b.materialization_owner
             AND m.materialization_id=b.materialization_id
            LEFT JOIN {head_table} h
              ON h.materialization_owner=m.owner
             AND h.materialization_id=m.materialization_id
            WHERE b.attempt_id IN ({attempt_placeholders})
            ORDER BY b.attempt_id,b.binding_name""",
            attempt_ids,
        ).fetchall()
        outbox_rows = connection.execute(
            f"""SELECT event_id,event_type,attempt_id,receipt_id,occurred_ns,payload_json
            FROM text_derivation_outbox
            WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id""",
            batch,
        ).fetchall()
        inputs_by_attempt: dict[str, list[sqlite3.Row]] = {}
        outputs_by_attempt: dict[str, list[sqlite3.Row]] = {}
        outbox_by_receipt: dict[str, list[sqlite3.Row]] = {}
        for row in input_rows:
            inputs_by_attempt.setdefault(str(row["attempt_id"]), []).append(row)
        for row in output_rows:
            outputs_by_attempt.setdefault(str(row["attempt_id"]), []).append(row)
        for row in outbox_rows:
            outbox_by_receipt.setdefault(str(row["receipt_id"]), []).append(row)
        for receipt_id in batch:
            receipt_row = receipts_by_id[receipt_id]
            attempt_id = str(receipt_row["attempt_id"])
            attempt_row = attempts_by_id.get(attempt_id)
            if attempt_row is None:
                raise TextDerivationIntegrityError(
                    f"Text WorkReceipt attempt is missing: {receipt_id}"
                )
            validated[receipt_id] = _validate_terminal_receipt_rows(
                receipt_row,
                attempt_row,
                tuple(inputs_by_attempt.get(attempt_id, ())),
                tuple(outputs_by_attempt.get(attempt_id, ())),
                tuple(outbox_by_receipt.get(receipt_id, ())),
            )
    return validated


def _validated_terminal_receipt(
    connection: sqlite3.Connection,
    receipt_id: str,
    *,
    lookup_available: bool | None = False,
) -> WorkReceipt:
    return _validated_terminal_receipts(
        connection, (receipt_id,), lookup_available=lookup_available
    )[receipt_id]


def _load_running_attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM text_derivation_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Text derivation attempt does not exist: {attempt_id}")
    if str(row["status"]) != "running":
        raise RuntimeError(f"Text derivation attempt {attempt_id!r} is already {row['status']}")
    return row


def _receipt_from_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    receipt_id: str,
    outputs: tuple[OutputBinding, ...],
    finished_at_utc: str,
    duration_ns: int,
    outcome: WorkOutcome,
    execution_mode: WorkExecutionMode,
    reproducibility: ReproducibilityClass,
    failure: CapabilityFailure | None,
) -> WorkReceipt:
    row = _load_running_attempt(connection, attempt_id)
    configuration = json.loads(str(row["effective_configuration_json"]))
    runtime = json.loads(str(row["runtime_json"]))
    if not isinstance(configuration, dict) or not isinstance(runtime, dict):
        raise ValueError("persisted Text derivation configuration is malformed")
    return WorkReceipt(
        receipt_id=receipt_id,
        owner=_TEXT_OWNER,
        stage=_stage_from_row(row),
        inputs=_load_inputs(connection, attempt_id),
        outputs=outputs,
        effective_configuration=tuple(configuration.items()),
        runtime=tuple((str(key), str(value)) for key, value in runtime.items()),
        started_at_utc=str(row["started_at_utc"]),
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        attempt=int(row["attempt_number"]),
        outcome=outcome,
        execution_mode=execution_mode,
        reproducibility=reproducibility,
        run_id=str(row["run_id"]),
        correlation_id=str(row["correlation_id"]),
        causation_id=_optional_string(row["causation_id"]),
        failure=failure,
    )


def _existing_materialization_matches(
    connection: sqlite3.Connection,
    output: OutputBinding,
) -> bool:
    materialization = output.materialization
    row = connection.execute(
        """SELECT kind,schema_version,resource_id,revision_id,generation,
        fingerprint_algorithm,fingerprint,materialization_json
        FROM text_materializations WHERE owner=? AND materialization_id=?""",
        (materialization.owner, materialization.materialization_id),
    ).fetchone()
    if row is None:
        return False
    expected = (
        materialization.kind,
        materialization.schema_version,
        None if materialization.resource is None else materialization.resource.resource_id,
        None if materialization.revision is None else materialization.revision.revision_id,
        materialization.generation,
        output.fingerprint_algorithm,
        output.fingerprint,
        materialization.to_json(),
    )
    return tuple(row) == expected


def _persist_output(
    connection: sqlite3.Connection,
    receipt: WorkReceipt,
    output: OutputBinding,
    terminal_ns: int,
) -> None:
    materialization = output.materialization
    if materialization.owner != _TEXT_OWNER:
        raise ValueError("Text owner cannot publish another owner's materialization")
    reused = receipt.execution_mode in (
        WorkExecutionMode.CACHE_HIT,
        WorkExecutionMode.REPLAY,
    )
    if reused:
        if not _existing_materialization_matches(connection, output):
            raise ValueError(
                "reused output does not identify an existing exact Text materialization"
            )
    else:
        connection.execute(
            """INSERT INTO text_materializations(
            owner,materialization_id,kind,schema_version,resource_id,revision_id,
            generation,producer_receipt_id,fingerprint_algorithm,fingerprint,
            materialization_json,recorded_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                materialization.owner,
                materialization.materialization_id,
                materialization.kind,
                materialization.schema_version,
                None if materialization.resource is None else materialization.resource.resource_id,
                None if materialization.revision is None else materialization.revision.revision_id,
                materialization.generation,
                receipt.receipt_id,
                output.fingerprint_algorithm,
                output.fingerprint,
                materialization.to_json(),
                terminal_ns,
            ),
        )
    connection.execute(
        """INSERT INTO text_derivation_output_bindings(
        attempt_id,binding_name,materialization_owner,materialization_id,
        fingerprint_algorithm,fingerprint) VALUES(?,?,?,?,?,?)""",
        (
            _attempt_id_for_receipt(connection, receipt.receipt_id),
            output.name,
            materialization.owner,
            materialization.materialization_id,
            output.fingerprint_algorithm,
            output.fingerprint,
        ),
    )
    if not reused:
        if materialization.resource is None or materialization.revision is None:
            raise ValueError("published Text outputs require resource and revision references")
        connection.execute(
            """INSERT INTO text_materialization_heads(
            resource_id,materialization_kind,materialization_owner,materialization_id,
            revision_id,producer_receipt_id,updated_ns) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(resource_id,materialization_kind) DO UPDATE SET
            materialization_owner=excluded.materialization_owner,
            materialization_id=excluded.materialization_id,
            revision_id=excluded.revision_id,
            producer_receipt_id=excluded.producer_receipt_id,
            updated_ns=excluded.updated_ns""",
            (
                materialization.resource.resource_id,
                materialization.kind,
                materialization.owner,
                materialization.materialization_id,
                materialization.revision.revision_id,
                receipt.receipt_id,
                terminal_ns,
            ),
        )


def _attempt_id_for_receipt(connection: sqlite3.Connection, receipt_id: str) -> str:
    row = connection.execute(
        "SELECT attempt_id FROM text_work_receipts WHERE receipt_id=?", (receipt_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - local transaction invariant
        raise RuntimeError("terminal receipt disappeared during persistence")
    return str(row["attempt_id"])


def _persist_terminal_receipt(
    connection: sqlite3.Connection,
    attempt_id: str,
    receipt: WorkReceipt,
    *,
    terminal_ns: int,
    document_file_key: str | None,
) -> None:
    if not connection.in_transaction:
        raise RuntimeError(
            "terminal Text receipt requires a caller-owned transaction with owner output"
        )
    _load_running_attempt(connection, attempt_id)
    input_revisions = {
        (binding.revision.resource_id, binding.revision.revision_id): binding.revision
        for binding in receipt.inputs
    }
    for output in receipt.outputs:
        materialization = output.materialization
        if materialization.resource is None or materialization.revision is None:
            raise ValueError("Text outputs require a resource and an input revision")
        output_revision = (
            materialization.resource.resource_id,
            materialization.revision.revision_id,
        )
        expected_revision = input_revisions.get(output_revision)
        if expected_revision is None or materialization.revision != expected_revision:
            raise ValueError("Text output revision facts must match one receipt input exactly")
    if receipt.execution_mode in {WorkExecutionMode.CACHE_HIT, WorkExecutionMode.REPLAY}:
        producer_receipt_ids: set[str] = set()
        for output in receipt.outputs:
            row = connection.execute(
                """SELECT producer_receipt_id FROM text_materializations
                WHERE owner=? AND materialization_id=?""",
                (
                    output.materialization.owner,
                    output.materialization.materialization_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("reused Text output materialization does not exist")
            producer_receipt_ids.add(str(row["producer_receipt_id"]))
        if producer_receipt_ids != {receipt.causation_id}:
            raise ValueError(
                "Text cache_hit/replay causation must identify the exact output producer"
            )
    receipt_json = receipt.to_json()
    connection.execute(
        """INSERT INTO text_work_receipts(
        receipt_id,attempt_id,receipt_json,receipt_fingerprint,outcome,recorded_ns)
        VALUES(?,?,?,?,?,?)""",
        (
            receipt.receipt_id,
            attempt_id,
            receipt_json,
            receipt.contract_fingerprint,
            receipt.outcome.value,
            terminal_ns,
        ),
    )
    for output in receipt.outputs:
        _persist_output(connection, receipt, output, terminal_ns)
    if document_file_key is not None:
        revisions = {item.revision.revision_id for item in receipt.inputs}
        if len(revisions) != 1:
            raise ValueError("Text document publication requires exactly one input revision")
        updated = connection.execute(
            "UPDATE documents SET revision_id=? WHERE file_key=?",
            (next(iter(revisions)), document_file_key),
        )
        if updated.rowcount != 1:
            raise ValueError(f"Text document does not exist for file_key {document_file_key!r}")
    failure_json = None if receipt.failure is None else receipt.failure.to_json()
    updated = connection.execute(
        """UPDATE text_derivation_attempts SET status=?,receipt_id=?,finished_at_utc=?,
        duration_ns=?,execution_mode=?,reproducibility_class=?,failure_json=?,terminal_ns=?
        WHERE attempt_id=? AND status='running'""",
        (
            receipt.outcome.value,
            receipt.receipt_id,
            receipt.finished_at_utc,
            receipt.duration_ns,
            receipt.execution_mode.value,
            receipt.reproducibility.value,
            failure_json,
            terminal_ns,
            attempt_id,
        ),
    )
    if updated.rowcount != 1:  # pragma: no cover - guarded above
        raise RuntimeError("Text derivation attempt changed during terminalization")
    event_type = f"text.work_{receipt.outcome.value}.v1"
    connection.execute(
        """INSERT INTO text_derivation_outbox(
        event_id,event_type,attempt_id,receipt_id,occurred_ns,payload_json)
        VALUES(?,?,?,?,?,?)""",
        (
            f"text-outbox:{receipt.receipt_id}",
            event_type,
            attempt_id,
            receipt.receipt_id,
            terminal_ns,
            receipt_json,
        ),
    )


def succeed_text_derivation_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    receipt_id: str,
    outputs: tuple[OutputBinding, ...],
    finished_at_utc: str,
    duration_ns: int,
    execution_mode: WorkExecutionMode,
    reproducibility: ReproducibilityClass,
    terminal_ns: int | None = None,
    document_file_key: str | None = None,
) -> WorkReceipt:
    """Publish a success/cache-hit receipt without committing caller state."""

    if execution_mode not in (
        WorkExecutionMode.EXECUTED,
        WorkExecutionMode.CACHE_HIT,
        WorkExecutionMode.REPLAY,
    ):
        raise ValueError("invalid successful execution mode")
    receipt = _receipt_from_attempt(
        connection,
        attempt_id,
        receipt_id=receipt_id,
        outputs=outputs,
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        outcome=WorkOutcome.SUCCEEDED,
        execution_mode=execution_mode,
        reproducibility=reproducibility,
        failure=None,
    )
    _persist_terminal_receipt(
        connection,
        attempt_id,
        receipt,
        terminal_ns=time.time_ns() if terminal_ns is None else terminal_ns,
        document_file_key=document_file_key,
    )
    return receipt


def _unsuccessful_text_derivation_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    receipt_id: str,
    finished_at_utc: str,
    duration_ns: int,
    outcome: WorkOutcome,
    reproducibility: ReproducibilityClass,
    failure: CapabilityFailure,
    terminal_ns: int | None,
    document_file_key: str | None,
) -> WorkReceipt:
    receipt = _receipt_from_attempt(
        connection,
        attempt_id,
        receipt_id=receipt_id,
        outputs=(),
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        outcome=outcome,
        execution_mode=WorkExecutionMode.ATTEMPTED,
        reproducibility=reproducibility,
        failure=failure,
    )
    _persist_terminal_receipt(
        connection,
        attempt_id,
        receipt,
        terminal_ns=time.time_ns() if terminal_ns is None else terminal_ns,
        document_file_key=document_file_key,
    )
    return receipt


def fail_text_derivation_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    receipt_id: str,
    finished_at_utc: str,
    duration_ns: int,
    reproducibility: ReproducibilityClass,
    failure: CapabilityFailure,
    terminal_ns: int | None = None,
    document_file_key: str | None = None,
) -> WorkReceipt:
    return _unsuccessful_text_derivation_attempt(
        connection,
        attempt_id,
        receipt_id=receipt_id,
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        outcome=WorkOutcome.FAILED,
        reproducibility=reproducibility,
        failure=failure,
        terminal_ns=terminal_ns,
        document_file_key=document_file_key,
    )


def cancel_text_derivation_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    *,
    receipt_id: str,
    finished_at_utc: str,
    duration_ns: int,
    reproducibility: ReproducibilityClass,
    failure: CapabilityFailure,
    terminal_ns: int | None = None,
    document_file_key: str | None = None,
) -> WorkReceipt:
    return _unsuccessful_text_derivation_attempt(
        connection,
        attempt_id,
        receipt_id=receipt_id,
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        outcome=WorkOutcome.CANCELLED,
        reproducibility=reproducibility,
        failure=failure,
        terminal_ns=terminal_ns,
        document_file_key=document_file_key,
    )


def _abandoned_receipt_id(attempt_id: str, terminal_ns: int) -> str:
    digest = fingerprint_text(
        canonical_json({"attempt_id": attempt_id, "terminal_ns": terminal_ns})
    )
    return f"receipt:text:abandoned:{digest.xxh3_128}"


def abandon_running_text_derivations(
    path: Path,
    *,
    finished_at_utc: str,
    terminal_ns: int,
    correlation_id: str | None = None,
    limit: int = _TEXT_ABANDONMENT_BATCH,
) -> tuple[WorkReceipt, ...]:
    """Reconcile one bounded page of running attempts in an owner transaction."""

    _validate_utc("finished_at_utc", finished_at_utc)
    _nonnegative_integer("terminal_ns", terminal_ns)
    limit = _lineage_limit(limit)
    with text_database(path, create=False) as connection:
        _validate_reader(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            if correlation_id is None:
                rows = connection.execute(
                    """SELECT attempt_id,recorded_ns FROM text_derivation_attempts
                    WHERE status='running' ORDER BY recorded_ns,attempt_id LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT attempt_id,recorded_ns FROM text_derivation_attempts
                    WHERE status='running' AND correlation_id=?
                    ORDER BY recorded_ns,attempt_id LIMIT ?""",
                    (correlation_id, limit),
                ).fetchall()
            receipts: list[WorkReceipt] = []
            for row in rows:
                attempt_id = str(row["attempt_id"])
                duration_ns = min(
                    max(0, terminal_ns - int(row["recorded_ns"])),
                    _MAX_ABANDONED_DURATION_NS,
                )
                receipt = _receipt_from_attempt(
                    connection,
                    attempt_id,
                    receipt_id=_abandoned_receipt_id(attempt_id, terminal_ns),
                    outputs=(),
                    finished_at_utc=finished_at_utc,
                    duration_ns=duration_ns,
                    outcome=WorkOutcome.ABANDONED,
                    execution_mode=WorkExecutionMode.UNKNOWN,
                    reproducibility=ReproducibilityClass.BEST_EFFORT,
                    failure=CapabilityFailure(
                        capability_id="text.derivation",
                        reason_code="attempt_abandoned",
                        message="A prior running Text attempt had no committed terminal receipt.",
                        retryable=True,
                    ),
                )
                _persist_terminal_receipt(
                    connection,
                    attempt_id,
                    receipt,
                    terminal_ns=terminal_ns,
                    document_file_key=None,
                )
                receipts.append(receipt)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    return tuple(receipts)


def _materialization_lineage_from_row(row: sqlite3.Row) -> TextMaterializationLineage:
    return TextMaterializationLineage(
        name=str(row["binding_name"]),
        materialization=_materialization_from_json(str(row["materialization_json"])),
        fingerprint_algorithm=str(row["fingerprint_algorithm"]),
        fingerprint=str(row["fingerprint"]),
        producer_receipt_id=str(row["producer_receipt_id"]),
        current_head=bool(row["current_head"]),
    )


_MATERIALIZATION_LINEAGE_SELECT = """SELECT ob.binding_name,m.materialization_json,
ob.fingerprint_algorithm,ob.fingerprint,m.producer_receipt_id,
EXISTS(SELECT 1 FROM text_materialization_heads h
    WHERE h.materialization_owner=m.owner
    AND h.materialization_id=m.materialization_id) AS current_head
FROM text_materializations m
JOIN text_work_receipts wr ON wr.receipt_id=m.producer_receipt_id
JOIN text_derivation_output_bindings ob ON ob.attempt_id=wr.attempt_id
    AND ob.materialization_owner=m.owner
    AND ob.materialization_id=m.materialization_id"""


def _read_revision_receipts(
    connection: sqlite3.Connection,
    revision_id: str,
    *,
    limit: int,
) -> tuple[tuple[str, ...], int]:
    total = int(
        connection.execute(
            """SELECT COUNT(DISTINCT wr.receipt_id)
            FROM text_derivation_input_bindings ib
            JOIN text_work_receipts wr ON wr.attempt_id=ib.attempt_id
            WHERE ib.revision_id=?""",
            (revision_id,),
        ).fetchone()[0]
    )
    receipts = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT DISTINCT wr.receipt_id,wr.recorded_ns
            FROM text_derivation_input_bindings ib
            JOIN text_work_receipts wr ON wr.attempt_id=ib.attempt_id
            WHERE ib.revision_id=?
            ORDER BY wr.recorded_ns,wr.receipt_id LIMIT ?""",
            (revision_id, limit),
        )
    )
    _validated_terminal_receipts(connection, receipts)
    return receipts, total


def _read_revision_materializations(
    connection: sqlite3.Connection,
    revision_id: str,
    *,
    limit: int,
) -> tuple[tuple[TextMaterializationLineage, ...], int]:
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM text_materializations WHERE revision_id=?",
            (revision_id,),
        ).fetchone()[0]
    )
    materializations = tuple(
        _materialization_lineage_from_row(row)
        for row in connection.execute(
            _MATERIALIZATION_LINEAGE_SELECT
            + " WHERE m.revision_id=? ORDER BY m.kind,m.materialization_id LIMIT ?",
            (revision_id, limit),
        )
    )
    return materializations, total


def _historical_materialization_path(
    materializations: tuple[TextMaterializationLineage, ...],
) -> str | None:
    paths = {
        resource.current_path
        for item in materializations
        if (resource := item.materialization.resource) is not None
        and resource.current_path is not None
    }
    return next(iter(paths)) if len(paths) == 1 else None


def read_text_document_lineage(
    path: Path,
    file_key: str,
    *,
    limit: int = MAX_TEXT_LINEAGE_ROWS,
) -> TextDocumentLineage | None:
    """Read current attribution without creating or migrating Text state."""

    limit = _lineage_limit(limit)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        document = connection.execute(
            "SELECT file_key,path,status,revision_id FROM documents WHERE file_key=?",
            (file_key,),
        ).fetchone()
        if document is None:
            return None
        revision_id = document["revision_id"]
        if revision_id is None:
            return TextDocumentLineage(
                file_key=str(document["file_key"]),
                path=str(document["path"]),
                document_status=str(document["status"]),
                attribution="legacy_unattributed",
                revision=None,
                receipts=(),
                materializations=(),
                receipt_count=0,
                materialization_count=0,
            )
        if str(document["status"]) == "complete":
            validate_text_publication_from_connection(
                connection,
                str(document["file_key"]),
                str(revision_id),
            )
        elif str(document["status"]) == "error":
            validate_text_failure_from_connection(
                connection,
                str(document["file_key"]),
                str(revision_id),
            )
        else:
            raise TextDerivationIntegrityError(
                f"current Text document has unsupported status: {document['status']}"
            )
        revision_row = connection.execute(
            "SELECT * FROM text_input_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        receipts, receipt_count = _read_revision_receipts(
            connection,
            str(revision_id),
            limit=limit,
        )
        materializations, materialization_count = _read_revision_materializations(
            connection,
            str(revision_id),
            limit=limit,
        )
    if revision_row is None:
        attribution = "revision_missing"
        revision = None
    elif not receipts:
        attribution = "receipt_missing"
        revision = _revision_from_row(revision_row)
    else:
        attribution = "attributed"
        revision = _revision_from_row(revision_row)
    return TextDocumentLineage(
        file_key=str(document["file_key"]),
        path=str(document["path"]),
        document_status=str(document["status"]),
        attribution=attribution,
        revision=revision,
        receipts=receipts,
        materializations=materializations,
        receipt_count=receipt_count,
        materialization_count=materialization_count,
    )


def read_text_revision_lineage(
    path: Path,
    revision_id: str,
    *,
    limit: int = MAX_TEXT_LINEAGE_ROWS,
) -> TextDocumentLineage | None:
    """Read one exact current or historical Text revision without retargeting it."""

    _required_text("revision_id", revision_id)
    limit = _lineage_limit(limit)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        revision_row = connection.execute(
            "SELECT * FROM text_input_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if revision_row is None:
            return None
        document = connection.execute(
            "SELECT file_key,path,status FROM documents WHERE revision_id=? LIMIT 1",
            (revision_id,),
        ).fetchone()
        receipts, receipt_count = _read_revision_receipts(
            connection,
            revision_id,
            limit=limit,
        )
        materializations, materialization_count = _read_revision_materializations(
            connection,
            revision_id,
            limit=limit,
        )
    return TextDocumentLineage(
        file_key=None if document is None else str(document["file_key"]),
        path=(
            _historical_materialization_path(materializations)
            if document is None
            else str(document["path"])
        ),
        document_status="historical" if document is None else str(document["status"]),
        attribution=(
            "receipt_missing"
            if not receipts
            else "receipt_only"
            if document is None and not materializations
            else "attributed"
        ),
        revision=_revision_from_row(revision_row),
        receipts=receipts,
        materializations=materializations,
        receipt_count=receipt_count,
        materialization_count=materialization_count,
    )


def read_reusable_text_derivation(
    path: Path,
    file_key: str,
    *,
    stage_id: str,
    processing_signature: str,
) -> TextReusableDerivation | None:
    """Return exact published outputs usable by a cache-hit receipt."""

    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        return read_reusable_text_derivation_from_connection(
            connection,
            file_key,
            stage_id=stage_id,
            processing_signature=processing_signature,
        )


def read_reusable_text_derivation_from_connection(
    connection: sqlite3.Connection,
    file_key: str,
    *,
    stage_id: str,
    processing_signature: str,
) -> TextReusableDerivation | None:
    """Reuse a caller-validated connection without commit, close or migration."""

    _required_text("file_key", file_key)
    _required_text("stage_id", stage_id)
    _required_text("processing_signature", processing_signature)
    observation = _text_cache_observation(connection)
    document = connection.execute(
        """SELECT revision_id FROM documents
        WHERE file_key=? AND status='complete'""",
        (file_key,),
    ).fetchone()
    if document is None or document["revision_id"] is None:
        return None
    revision_id = str(document["revision_id"])
    lookup_available = text_route_lookups_available(connection)
    representations: dict[str, TextValidatedRepresentation] = {}
    validate_text_publications_from_connection(
        connection, ((file_key, revision_id),), lookup_available=lookup_available,
        _representations=representations,
    )
    revision_row = connection.execute(
        "SELECT * FROM text_input_revisions WHERE revision_id=?", (revision_id,)
    ).fetchone()
    if revision_row is None:
        return None
    if lookup_available:
        # Start at the exact revision lookup and retain the original table
        # predicates.  Every following join uses an existing primary/unique key.
        rows = connection.execute(
            """SELECT ob.binding_name,m.materialization_json,
            ob.fingerprint_algorithm,ob.fingerprint,m.producer_receipt_id,
            1 AS current_head
            FROM temp._text_materializations_lookup AS l
            CROSS JOIN text_materializations m
              ON m.owner=l.owner AND m.materialization_id=l.materialization_id
            CROSS JOIN text_work_receipts wr ON wr.receipt_id=m.producer_receipt_id
            CROSS JOIN text_derivation_output_bindings ob
              ON ob.attempt_id=wr.attempt_id AND ob.materialization_owner=m.owner
             AND ob.materialization_id=m.materialization_id
            CROSS JOIN text_derivation_attempts a ON a.receipt_id=m.producer_receipt_id
            WHERE l.revision_id=? AND m.revision_id=?
              AND a.stage_id=? AND a.processing_signature=? AND a.status='succeeded'
              AND EXISTS(SELECT 1 FROM temp._text_materialization_heads_lookup h
                WHERE h.materialization_owner=m.owner
                  AND h.materialization_id=m.materialization_id)
            ORDER BY ob.binding_name""",
            (revision_id, revision_id, stage_id, processing_signature),
        ).fetchall()
    else:
        rows = connection.execute(
            _MATERIALIZATION_LINEAGE_SELECT
            + """ JOIN text_derivation_attempts a ON a.receipt_id=m.producer_receipt_id
            WHERE m.revision_id=? AND a.stage_id=? AND a.processing_signature=?
            AND a.status='succeeded' AND EXISTS(
                SELECT 1 FROM text_materialization_heads h
                WHERE h.materialization_owner=m.owner
                AND h.materialization_id=m.materialization_id)
            ORDER BY ob.binding_name""",
            (revision_id, stage_id, processing_signature),
        ).fetchall()
    if not rows:
        return None
    producer_receipts = {str(row["producer_receipt_id"]) for row in rows}
    if len(producer_receipts) != 1:
        return None
    _validated_terminal_receipt(
        connection, next(iter(producer_receipts)), lookup_available=lookup_available
    )
    outputs = tuple(
        OutputBinding(
            name=str(row["binding_name"]),
            materialization=_materialization_from_json(str(row["materialization_json"])),
            fingerprint=str(row["fingerprint"]),
            fingerprint_algorithm=str(row["fingerprint_algorithm"]),
        )
        for row in rows
    )
    if observation is not None:
        validate_text_cache_observation(connection, observation)
    return TextReusableDerivation(
        producer_receipt_id=next(iter(producer_receipts)),
        revision=_revision_from_row(revision_row),
        outputs=outputs,
        representation=representations[file_key],
        observation=observation,
    )


def read_reusable_text_failure_from_connection(
    connection: sqlite3.Connection,
    file_key: str,
    *,
    stage_id: str,
    processing_signature: str,
    revision_id: str,
    size: int,
    mtime_ns: int,
    birthtime_ns: int,
) -> str | None:
    """Return the exact prior failed receipt used by retry_errors=False."""

    _required_text("file_key", file_key)
    _required_text("stage_id", stage_id)
    _required_text("processing_signature", processing_signature)
    _required_text("revision_id", revision_id)
    document = connection.execute(
        """SELECT revision_id FROM documents
        WHERE file_key=? AND status='error' AND processing_signature=? AND revision_id=?
          AND size=? AND mtime_ns=? AND birthtime_ns=?""",
        (file_key, processing_signature, revision_id, size, mtime_ns, birthtime_ns),
    ).fetchone()
    if document is None or document["revision_id"] is None:
        return None
    stored_revision_id = str(document["revision_id"])
    rows = connection.execute(
        """SELECT DISTINCT a.receipt_id,a.terminal_ns
        FROM text_derivation_attempts a
        JOIN text_derivation_input_bindings ib ON ib.attempt_id=a.attempt_id
        WHERE a.stage_id=? AND a.processing_signature=? AND a.status='failed'
          AND ib.revision_id=? AND a.receipt_id IS NOT NULL
        ORDER BY a.terminal_ns DESC,a.receipt_id DESC LIMIT 2""",
        (stage_id, processing_signature, stored_revision_id),
    ).fetchall()
    if not rows:
        return None
    receipt_id = str(rows[0]["receipt_id"])
    receipt = _validated_terminal_receipt(connection, receipt_id)
    if receipt.outcome is not WorkOutcome.FAILED or receipt.outputs:
        raise TextDerivationIntegrityError(
            f"cached Text failure contradicts its receipt: {receipt_id}"
        )
    resource = connection.execute(
        "SELECT resource_id FROM text_input_revisions WHERE revision_id=?",
        (stored_revision_id,),
    ).fetchone()
    if resource is None:
        raise TextDerivationIntegrityError(
            f"cached Text failure revision is missing: {stored_revision_id}"
        )
    head = connection.execute(
        """SELECT 1 FROM text_materialization_heads
        WHERE resource_id=? AND materialization_owner=? LIMIT 1""",
        (str(resource["resource_id"]), _TEXT_OWNER),
    ).fetchone()
    if head is not None:
        raise TextDerivationIntegrityError(
            f"cached Text failure unexpectedly has a published output: {receipt_id}"
        )
    return receipt_id


def read_text_derivation_dependents(
    path: Path,
    revision_id: str,
    *,
    limit: int = MAX_TEXT_LINEAGE_ROWS,
) -> tuple[TextDerivationDependency, ...]:
    """Compatibility wrapper for a bounded, set-based dependency page."""

    return read_text_derivation_dependents_page(
        path,
        revision_id,
        limit=limit,
    ).items


def read_text_derivation_dependents_page(
    path: Path,
    revision_id: str,
    *,
    limit: int = MAX_TEXT_LINEAGE_ROWS,
) -> TextDerivationDependencyPage:
    """Read terminal dependents with a fixed SQL window and no per-row queries."""

    _required_text("revision_id", revision_id)
    limit = _lineage_limit(limit)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        total_count = int(
            connection.execute(
                """SELECT COUNT(DISTINCT a.attempt_id)
                FROM text_derivation_attempts a
                JOIN text_derivation_input_bindings ib ON ib.attempt_id=a.attempt_id
                WHERE ib.revision_id=? AND a.status<>'running'""",
                (revision_id,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """WITH selected AS (
                SELECT a.attempt_id,a.receipt_id,a.stage_id,a.stage_version,
                    a.processing_signature,a.status,a.terminal_ns
                FROM text_derivation_attempts a
                JOIN text_derivation_input_bindings ib ON ib.attempt_id=a.attempt_id
                WHERE ib.revision_id=? AND a.status<>'running'
                GROUP BY a.attempt_id
                ORDER BY a.terminal_ns,a.attempt_id LIMIT ?
            )
            SELECT selected.attempt_id,selected.receipt_id,selected.stage_id,
                selected.stage_version,selected.processing_signature,selected.status,
                selected.terminal_ns,ob.binding_name,m.materialization_json,
                ob.fingerprint_algorithm,ob.fingerprint,m.producer_receipt_id,
                EXISTS(SELECT 1 FROM text_materialization_heads h
                    WHERE h.materialization_owner=m.owner
                    AND h.materialization_id=m.materialization_id) AS current_head
            FROM selected
            LEFT JOIN text_derivation_output_bindings ob
                ON ob.attempt_id=selected.attempt_id
            LEFT JOIN text_materializations m
                ON m.owner=ob.materialization_owner
                AND m.materialization_id=ob.materialization_id
            ORDER BY selected.terminal_ns,selected.attempt_id,ob.binding_name""",
            (revision_id, limit),
        ).fetchall()
        dependencies_by_attempt: dict[str, TextDerivationDependency] = {}
        outputs_by_attempt: dict[str, list[TextMaterializationLineage]] = {}
        for row in rows:
            attempt_id = str(row["attempt_id"])
            if attempt_id not in dependencies_by_attempt:
                dependencies_by_attempt[attempt_id] = TextDerivationDependency(
                    receipt_id=str(row["receipt_id"]),
                    stage_id=str(row["stage_id"]),
                    stage_version=str(row["stage_version"]),
                    processing_signature=str(row["processing_signature"]),
                    outcome=WorkOutcome(str(row["status"])),
                    outputs=(),
                )
                outputs_by_attempt[attempt_id] = []
            if row["binding_name"] is not None:
                outputs_by_attempt[attempt_id].append(_materialization_lineage_from_row(row))
        dependencies = tuple(
            TextDerivationDependency(
                receipt_id=item.receipt_id,
                stage_id=item.stage_id,
                stage_version=item.stage_version,
                processing_signature=item.processing_signature,
                outcome=item.outcome,
                outputs=tuple(outputs_by_attempt[attempt_id]),
            )
            for attempt_id, item in dependencies_by_attempt.items()
        )
        _validated_terminal_receipts(
            connection,
            tuple(dependency.receipt_id for dependency in dependencies),
        )
    return TextDerivationDependencyPage(
        items=dependencies,
        total_count=total_count,
        truncated=total_count > len(dependencies),
    )


def read_text_derivation_impact(
    path: Path,
    *,
    stage_id: str,
    processing_signature: str,
    limit: int = MAX_TEXT_LINEAGE_ROWS,
) -> TextDerivationImpact:
    """Find published heads stale under a changed stage processing signature."""

    _required_text("stage_id", stage_id)
    _required_text("processing_signature", processing_signature)
    limit = _lineage_limit(limit)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        total_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM text_materializations m
                JOIN text_derivation_attempts a
                    ON a.receipt_id=m.producer_receipt_id
                WHERE a.stage_id=? AND a.processing_signature<>?
                AND EXISTS(SELECT 1 FROM text_materialization_heads h
                    WHERE h.materialization_owner=m.owner
                    AND h.materialization_id=m.materialization_id)""",
                (stage_id, processing_signature),
            ).fetchone()[0]
        )
        rows = connection.execute(
            _MATERIALIZATION_LINEAGE_SELECT
            + """ JOIN text_derivation_attempts a ON a.receipt_id=m.producer_receipt_id
            WHERE a.stage_id=? AND a.processing_signature<>?
            AND EXISTS(SELECT 1 FROM text_materialization_heads h
                WHERE h.materialization_owner=m.owner
                AND h.materialization_id=m.materialization_id)
            ORDER BY m.kind,m.materialization_id LIMIT ?""",
            (stage_id, processing_signature, limit),
        ).fetchall()
        _validated_terminal_receipts(
            connection,
            tuple({str(row["producer_receipt_id"]) for row in rows}),
        )
    stale = tuple(_materialization_lineage_from_row(row) for row in rows)
    return TextDerivationImpact(
        stage_id=stage_id,
        expected_processing_signature=processing_signature,
        stale=stale,
        total_count=total_count,
        truncated=total_count > len(stale),
    )


def read_text_derivation_outbox(
    path: Path,
    *,
    after_sequence: int = 0,
    limit: int = 1_000,
) -> tuple[TextDerivationOutboxEvent, ...]:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        cursor = connection.execute(
            """SELECT o.sequence,o.event_id,o.event_type,o.attempt_id,o.receipt_id,
            o.occurred_ns,o.payload_json,wr.attempt_id AS receipt_attempt_id,
            wr.receipt_json,wr.receipt_fingerprint,wr.outcome
            FROM text_derivation_outbox o
            JOIN text_work_receipts wr ON wr.receipt_id=o.receipt_id
            WHERE o.sequence>? ORDER BY o.sequence LIMIT ?""",
            (after_sequence, limit),
        )
        rows: list[sqlite3.Row] = []
        page_bytes = 0
        for row in cursor:
            row_bytes = len(str(row["payload_json"]).encode("utf-8")) + len(
                str(row["receipt_json"]).encode("utf-8")
            )
            if rows and page_bytes + row_bytes > _TEXT_OUTBOX_PAGE_BYTES:
                break
            rows.append(row)
            page_bytes += row_bytes
        _validated_terminal_receipts(
            connection,
            tuple(str(row["receipt_id"]) for row in rows),
        )
    events: list[TextDerivationOutboxEvent] = []
    for row in rows:
        payload_json = str(row["payload_json"])
        receipt_json = str(row["receipt_json"])
        receipt_id = str(row["receipt_id"])
        if payload_json != receipt_json:
            raise TextDerivationIntegrityError(f"Text outbox payload mismatch: {receipt_id}")
        try:
            receipt = WorkReceipt.from_json(payload_json)
        except ValueError as exc:
            raise TextDerivationIntegrityError(
                f"Text outbox WorkReceipt is invalid: {receipt_id}"
            ) from exc
        expected_fingerprint = (
            f"derivation-contract-v{DERIVATION_CONTRACT_SCHEMA_VERSION}:{HASH_ALGORITHM_128}:"
            f"{fingerprint_text(payload_json).xxh3_128}"
        )
        if (
            receipt.receipt_id != receipt_id
            or receipt.outcome.value != str(row["outcome"])
            or str(row["attempt_id"]) != str(row["receipt_attempt_id"])
            or str(row["event_type"]) != f"text.work_{receipt.outcome.value}.v1"
            or str(row["receipt_fingerprint"]) != expected_fingerprint
        ):
            raise TextDerivationIntegrityError(
                f"Text outbox receipt columns mismatch: {receipt_id}"
            )
        events.append(
            TextDerivationOutboxEvent(
                sequence=int(row["sequence"]),
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                attempt_id=str(row["attempt_id"]),
                receipt_id=receipt_id,
                occurred_ns=int(row["occurred_ns"]),
                payload_json=payload_json,
            )
        )
    return tuple(events)


def read_text_work_receipts(
    path: Path,
    receipt_ids: tuple[str, ...],
) -> tuple[TextWorkReceiptRecord, ...]:
    """Read a bounded set of canonical receipt payloads without migration."""

    if len(receipt_ids) > 1_000:
        raise ValueError("receipt_ids cannot contain more than 1000 values")
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ValueError("receipt_ids cannot contain duplicates")
    if not receipt_ids:
        return ()
    for receipt_id in receipt_ids:
        _required_text("receipt_id", receipt_id)
    placeholders = ",".join("?" for _item in receipt_ids)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            f"""SELECT receipt_id,attempt_id,receipt_fingerprint,outcome,
            recorded_ns,receipt_json FROM text_work_receipts
            WHERE receipt_id IN ({placeholders})""",
            receipt_ids,
        ).fetchall()
        _validated_terminal_receipts(
            connection,
            tuple(str(row["receipt_id"]) for row in rows),
        )
    by_id: dict[str, TextWorkReceiptRecord] = {}
    for row in rows:
        payload_json = str(row["receipt_json"])
        try:
            receipt = WorkReceipt.from_json(payload_json)
        except ValueError as exc:
            raise TextDerivationIntegrityError(
                f"Text WorkReceipt is invalid: {row['receipt_id']}"
            ) from exc
        if receipt.receipt_id != str(row["receipt_id"]) or receipt.outcome.value != str(
            row["outcome"]
        ):
            raise TextDerivationIntegrityError(
                f"Text WorkReceipt columns mismatch: {row['receipt_id']}"
            )
        expected_fingerprint = (
            f"derivation-contract-v{DERIVATION_CONTRACT_SCHEMA_VERSION}:{HASH_ALGORITHM_128}:"
            f"{fingerprint_text(payload_json).xxh3_128}"
        )
        stored_fingerprint = str(row["receipt_fingerprint"])
        if stored_fingerprint != expected_fingerprint:
            raise TextDerivationIntegrityError(
                f"Text WorkReceipt fingerprint mismatch: {row['receipt_id']}"
            )
        by_id[str(row["receipt_id"])] = TextWorkReceiptRecord(
            receipt_id=str(row["receipt_id"]),
            attempt_id=str(row["attempt_id"]),
            receipt_fingerprint=stored_fingerprint,
            outcome=WorkOutcome(str(row["outcome"])),
            recorded_ns=int(row["recorded_ns"]),
            payload_json=payload_json,
        )
    return tuple(by_id[item] for item in receipt_ids if item in by_id)


def resolve_text_lineage_revision_id(path: Path, identifier: str) -> str | None:
    """Resolve an exact historical revision without retargeting to the current head."""

    _required_text("identifier", identifier)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        row = connection.execute(
            "SELECT revision_id FROM text_input_revisions WHERE revision_id=?",
            (identifier,),
        ).fetchone()
        if row is not None:
            return str(row["revision_id"])
        row = connection.execute(
            """SELECT revision_id FROM text_materializations
            WHERE owner=? AND materialization_id=? AND revision_id IS NOT NULL""",
            (_TEXT_OWNER, identifier),
        ).fetchone()
        if row is not None:
            return str(row["revision_id"])
        rows = connection.execute(
            """SELECT DISTINCT ib.revision_id
            FROM text_derivation_attempts a
            JOIN text_derivation_input_bindings ib ON ib.attempt_id=a.attempt_id
            WHERE a.receipt_id=? ORDER BY ib.revision_id LIMIT 2""",
            (identifier,),
        ).fetchall()
    return str(rows[0]["revision_id"]) if len(rows) == 1 else None


def resolve_text_lineage_identifier(path: Path, identifier: str) -> str | None:
    """Resolve current file key from file/revision/materialization/receipt/path."""

    _required_text("identifier", identifier)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        row = connection.execute(
            """SELECT file_key FROM documents WHERE file_key=? OR path=?
            ORDER BY file_key LIMIT 1""",
            (identifier, identifier),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """SELECT d.file_key FROM documents d
                JOIN text_materializations m ON m.revision_id=d.revision_id
                WHERE d.revision_id=? OR (m.owner=? AND m.materialization_id=?)
                ORDER BY d.file_key LIMIT 1""",
                (identifier, _TEXT_OWNER, identifier),
            ).fetchone()
        if row is None:
            row = connection.execute(
                """SELECT d.file_key FROM documents d
                JOIN text_materializations m ON m.revision_id=d.revision_id
                WHERE m.producer_receipt_id=? ORDER BY d.file_key LIMIT 1""",
                (identifier,),
            ).fetchone()
    return None if row is None else str(row["file_key"])


read_text_derivation_events = read_text_derivation_outbox


__all__ = (
    "MAX_TEXT_LINEAGE_ROWS",
    "TextDerivationAttemptStart",
    "TextDerivationDependency",
    "TextDerivationDependencyPage",
    "TextDerivationImpact",
    "TextDerivationIntegrityError",
    "TextDerivationOutboxEvent",
    "TextDocumentLineage",
    "TextMaterializationLineage",
    "TextReusableDerivation",
    "TextWorkReceiptRecord",
    "abandon_running_text_derivations",
    "begin_text_derivation_attempt",
    "begin_text_derivation_attempt_from_connection",
    "cancel_text_derivation_attempt",
    "compute_text_fts_fingerprint",
    "compute_text_representation_fingerprint",
    "fail_text_derivation_attempt",
    "read_reusable_text_derivation",
    "read_reusable_text_derivation_from_connection",
    "read_reusable_text_failure_from_connection",
    "read_text_derivation_dependents",
    "read_text_derivation_dependents_page",
    "read_text_derivation_events",
    "read_text_derivation_impact",
    "read_text_derivation_outbox",
    "read_text_document_lineage",
    "read_text_revision_lineage",
    "read_text_work_receipts",
    "resolve_text_lineage_identifier",
    "resolve_text_lineage_revision_id",
    "succeed_text_derivation_attempt",
    "validate_text_publication_from_connection",
    "validate_text_publications_from_connection",
)
