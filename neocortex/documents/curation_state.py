"""Catalog-owned persistence for Fast Curation Semantic.

This module is intentionally small and owner-local.  Fast Curation stores a
content/representation keyed embedding cache and one current decision for each
Catalog document identity.  It does not open another SQLite owner, retain a
path as an identity, or make a Full Semantic chunk vector look equivalent to a
document representation.

The functions in this module receive one already-owned Catalog connection.
They validate the complete batch before writing and use one transaction (or a
savepoint when the caller already owns a transaction).  Reads use bounded
keyset pages and never open a connection per row or per page.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Callable, Iterator, TypeAlias


MAX_CURATION_PAGE_SIZE = 4096
MAX_CURATION_BATCH_SIZE = 4096
MAX_TOP_K = 5
MAX_METADATA_JSON_BYTES = 65_536
MAX_SOURCE_BINDING_JSON_BYTES = 32_768
MAX_INPUT_SIGNATURE_BYTES = 4096
MAX_EVIDENCE_JSON_BYTES = 65_536
MAX_CONTEXT_PROVENANCE_JSON_BYTES = 32_768
MAX_TEXT_BYTES = 4096
MAX_DIMENSIONS = 65_536

_FLOAT32_WIDTH = 4
_SAVEPOINT = "neocortex_curation_state_batch"

# These keys are part of the durable context envelope, not of the content
# embedding.  ``source_binding_json`` remains the current Catalog binding;
# the original binding/path below never follows a later physical rename.
ORIGINAL_PATH_KEY = "original_path"
ORIGINAL_SOURCE_BINDING_KEY = "original_source_binding"
SOURCE_ORIGINAL_BINDING_KEY = "source_original_binding"
ACTUAL_PATH_KEY = "actual_path"
CURRENT_PATH_KEY = "current_path"


def _bounded_text(name: str, value: object, *, maximum: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be non-empty trimmed text")
    if len(value.encode("utf-8", "strict")) > maximum:
        raise ValueError(f"{name} exceeds its byte bound")
    return value


def _bounded_optional_text(
    name: str,
    value: object | None,
    *,
    maximum: int = MAX_TEXT_BYTES,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(name, value, maximum=maximum)


def _sha256_text(name: str, value: object) -> str:
    selected = _bounded_text(name, value, maximum=64)
    if len(selected) != 64 or any(character not in "0123456789abcdef" for character in selected):
        raise ValueError(f"{name} must be a lowercase full SHA-256 hexadecimal digest")
    return selected


def _dimensions(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DIMENSIONS:
        raise ValueError(f"dimensions must be an integer between 1 and {MAX_DIMENSIONS}")
    return value


def _finite_score(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    selected = float(value)
    if not math.isfinite(selected):
        raise ValueError(f"{name} must be a finite number")
    return selected


def _canonical_json(
    name: str,
    value: Mapping[str, object],
    *,
    maximum: int,
) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} object keys must be strings")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{name} must contain bounded JSON values") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{name} exceeds its byte bound")
    return encoded.decode("utf-8")


def _canonical_json_array(name: str, value: Sequence[object], *, maximum: int) -> str:
    try:
        encoded = json.dumps(
            list(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{name} must contain bounded JSON values") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{name} exceeds its byte bound")
    return encoded.decode("utf-8")


def _timestamp(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer timestamp")
    return value


class CurationPhysicalIdentityMismatch(ValueError):
    """The Catalog key was reused for a different physical source identity."""

    code = "curation_physical_identity_mismatch"


class CurationRebindError(ValueError):
    """A Catalog curation rebind lacks an exact current owner/receipt fence."""

    code = "curation_rebind_invalid"


def _json_object(raw: object, *, name: str, maximum: int) -> dict[str, object]:
    """Decode one bounded object without consulting another owner."""

    if isinstance(raw, Mapping):
        value: object = dict(raw)
    else:
        try:
            value = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    _canonical_json(name, value, maximum=maximum)
    return value


def _path_from_value(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:MAX_TEXT_BYTES]


def _binding_current_path(binding: Mapping[str, object]) -> str | None:
    """Read a path locator as auxiliary context, never as identity."""

    for key in ("physical_anchor_path", CURRENT_PATH_KEY, ACTUAL_PATH_KEY, "path"):
        path = _path_from_value(binding.get(key))
        if path is not None:
            return path
    resource_ref = binding.get("resource_ref")
    if isinstance(resource_ref, Mapping):
        return _path_from_value(resource_ref.get(CURRENT_PATH_KEY))
    return None


def _physical_identity_token(binding: Mapping[str, object]) -> tuple[object, ...] | None:
    """Return a bounded identity token from a Catalog/resource binding.

    The current resource binding normally carries ``physical_identity`` as a
    packed key plus birth time.  Small test/adapter bindings may carry decimal
    components directly; accepting both keeps this owner independent of the
    binding codec while never deriving identity from a path.
    """

    candidates: list[Mapping[str, object]] = [binding]
    resource_ref = binding.get("resource_ref")
    if isinstance(resource_ref, Mapping):
        candidates.append(resource_ref)
    for candidate in candidates:
        physical = candidate.get("physical_identity")
        if isinstance(physical, Mapping):
            packed = physical.get("packed_key")
            birth = physical.get("birthtime_ns")
            if packed is not None and birth is not None:
                try:
                    return ("packed", str(packed), int(birth))
                except (TypeError, ValueError, OverflowError):
                    return None
            volume = physical.get("volume_id")
            file_id = physical.get("file_id")
            if volume is not None and file_id is not None and birth is not None:
                try:
                    return ("components", str(volume), str(file_id), int(birth))
                except (TypeError, ValueError, OverflowError):
                    return None
        volume = candidate.get("volume_id")
        file_id = candidate.get("file_id")
        birth = candidate.get("birthtime_ns")
        if volume is not None and file_id is not None and birth is not None:
            try:
                return ("components", str(volume), str(file_id), int(birth))
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def _physical_identity_components(
    binding: Mapping[str, object],
) -> tuple[str, str, int] | None:
    """Normalize packed or component identities for receipt comparison."""

    token = _physical_identity_token(binding)
    if token is None:
        return None
    if token[0] == "components":
        return str(token[1]), str(token[2]), int(token[3])
    if token[0] == "packed":
        try:
            from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding

            identity = FileIdentity.decode(
                str(token[1]), encoding=FileIdentityEncoding.PACKED_HEX_V1
            )
            return str(identity.volume_id), str(identity.file_id), int(token[2])
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _original_binding_from_context(
    context: Mapping[str, object],
) -> Mapping[str, object] | None:
    for key in (ORIGINAL_SOURCE_BINDING_KEY, SOURCE_ORIGINAL_BINDING_KEY):
        value = context.get(key)
        if isinstance(value, Mapping):
            return value
    return None


@dataclass(frozen=True, slots=True)
class CurationEmbeddingCacheKey:
    """The content-only identity of one reusable curation vector."""

    representation_sha256: str
    representation_version: str
    model_signature: str
    role: str
    vector_space: str
    dimensions: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "representation_sha256",
            _sha256_text("representation_sha256", self.representation_sha256),
        )
        for name in ("representation_version", "model_signature", "role", "vector_space"):
            object.__setattr__(self, name, _bounded_text(name, getattr(self, name)))
        object.__setattr__(self, "dimensions", _dimensions(self.dimensions))


@dataclass(frozen=True, slots=True)
class CurationEmbeddingCacheRecord:
    """One normalized float32 vector and bounded encoder metadata."""

    key: CurationEmbeddingCacheKey
    vector: tuple[float, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_ns: int = field(default_factory=time.time_ns)
    updated_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, CurationEmbeddingCacheKey):
            raise TypeError("key must be a CurationEmbeddingCacheKey")
        values = tuple(self.vector)
        if len(values) != self.key.dimensions:
            raise ValueError(
                f"vector has {len(values)} values; expected {self.key.dimensions}"
            )
        finite: list[float] = []
        for index, value in enumerate(values):
            finite.append(_finite_score(f"vector[{index}]", value))
        norm_squared = math.fsum(value * value for value in finite)
        if not math.isfinite(norm_squared) or norm_squared <= 0.0:
            raise ValueError("embedding vector must have a finite non-zero norm")
        norm = math.sqrt(norm_squared)
        normalized = tuple(value / norm for value in finite)
        try:
            payload = struct.pack(f"<{self.key.dimensions}f", *normalized)
            unpacked = struct.unpack(f"<{self.key.dimensions}f", payload)
        except (OverflowError, struct.error) as exc:
            raise ValueError("embedding vector cannot be represented as float32") from exc
        if any(not math.isfinite(value) for value in unpacked):
            raise ValueError("embedding vector float32 payload is not finite")
        payload_norm = math.sqrt(math.fsum(value * value for value in unpacked))
        if not math.isfinite(payload_norm) or not math.isclose(
            payload_norm, 1.0, rel_tol=2e-4, abs_tol=2e-4
        ):
            raise ValueError("embedding vector float32 payload is not normalized")
        object.__setattr__(self, "vector", tuple(float(value) for value in unpacked))
        _canonical_json("metadata", self.metadata, maximum=MAX_METADATA_JSON_BYTES)
        object.__setattr__(self, "created_ns", _timestamp("created_ns", self.created_ns))
        selected_updated = self.created_ns if self.updated_ns is None else self.updated_ns
        object.__setattr__(self, "updated_ns", _timestamp("updated_ns", selected_updated))

    @classmethod
    def from_values(
        cls,
        key: CurationEmbeddingCacheKey,
        values: Sequence[float],
        *,
        metadata: Mapping[str, object] | None = None,
        created_ns: int | None = None,
        updated_ns: int | None = None,
    ) -> "CurationEmbeddingCacheRecord":
        return cls(
            key=key,
            vector=tuple(values),
            metadata={} if metadata is None else metadata,
            created_ns=time.time_ns() if created_ns is None else created_ns,
            updated_ns=updated_ns,
        )

    @property
    def metadata_json(self) -> str:
        return _canonical_json("metadata", self.metadata, maximum=MAX_METADATA_JSON_BYTES)

    @property
    def provenance(self) -> Mapping[str, object]:
        return self.metadata

    @property
    def vector_bytes(self) -> bytes:
        return struct.pack(f"<{self.key.dimensions}f", *self.vector)

    @property
    def vector_blob(self) -> bytes:
        """Semantic-owner spelling for the exact float32 payload."""

        return self.vector_bytes


@dataclass(frozen=True, slots=True)
class CurationTopCandidate:
    """One bounded candidate retained in a decision's top-k evidence."""

    label: str
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _bounded_text("top_k label", self.label))
        object.__setattr__(self, "score", _finite_score("top_k score", self.score))


@dataclass(frozen=True, slots=True)
class CurationDecisionRecord:
    """The current calibrated decision for one Catalog document key.

    ``source_binding`` is the current Catalog binding and may carry the
    latest physical anchor path.  ``context_provenance`` separates the first
    observed origin (``original_path`` and ``original_source_binding``;
    ``source_original_binding`` is retained as a wire-compatible alias) from
    auxiliary current locator fields (``actual_path``/``current_path``).
    Origin fields are reconciled by the owner during an upsert and are never
    replaced merely because a Catalog rebind observed a rename.
    """

    source_kind: str
    file_key: str
    source_binding: Mapping[str, object]
    input_signature: str
    semantic_representation_fingerprint: str
    representation_version: str
    model_signature: str
    role: str
    vector_space: str
    dimensions: int
    ontology_version: str
    prototype_version: str
    policy_version: str
    calibration_version: str
    decision: str
    top1_label: str | None = None
    top1_score: float | None = None
    top2_label: str | None = None
    top2_score: float | None = None
    margin: float | None = None
    top_k: tuple[CurationTopCandidate, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)
    context_provenance: Mapping[str, object] = field(default_factory=dict)
    created_ns: int = field(default_factory=time.time_ns)
    updated_ns: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", _bounded_text("source_kind", self.source_kind))
        object.__setattr__(self, "file_key", _bounded_text("file_key", self.file_key))
        object.__setattr__(
            self,
            "input_signature",
            _bounded_text(
                "input_signature",
                self.input_signature,
                maximum=MAX_INPUT_SIGNATURE_BYTES,
            ),
        )
        object.__setattr__(
            self,
            "semantic_representation_fingerprint",
            _bounded_text(
                "semantic_representation_fingerprint",
                self.semantic_representation_fingerprint,
                maximum=MAX_INPUT_SIGNATURE_BYTES,
            ),
        )
        for name in (
            "representation_version",
            "model_signature",
            "role",
            "vector_space",
            "ontology_version",
            "prototype_version",
            "policy_version",
            "calibration_version",
        ):
            object.__setattr__(self, name, _bounded_text(name, getattr(self, name)))
        object.__setattr__(self, "dimensions", _dimensions(self.dimensions))
        if not isinstance(self.decision, str):
            raise ValueError("decision must be CLASSIFIED or ABSTAIN")
        normalized_decision = self.decision.upper()
        if normalized_decision not in {"CLASSIFIED", "ABSTAIN"}:
            raise ValueError("decision must be CLASSIFIED or ABSTAIN")
        object.__setattr__(self, "decision", normalized_decision)
        for name in ("top1_label", "top2_label"):
            object.__setattr__(self, name, _bounded_optional_text(name, getattr(self, name)))
        for name in ("top1_score", "top2_score", "margin"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _finite_score(name, value))
        if self.decision == "CLASSIFIED" and (
            self.top1_label is None or self.top1_score is None
        ):
            raise ValueError("CLASSIFIED decisions require top1 label and score")
        candidates = tuple(self.top_k)
        if len(candidates) > MAX_TOP_K:
            raise ValueError(f"top_k cannot contain more than {MAX_TOP_K} candidates")
        if any(not isinstance(item, CurationTopCandidate) for item in candidates):
            raise TypeError("top_k must contain CurationTopCandidate values")
        object.__setattr__(self, "top_k", candidates)
        if not isinstance(self.source_binding, Mapping) or not self.source_binding:
            raise ValueError("source_binding must be a non-empty JSON object")
        _canonical_json(
            "source_binding", self.source_binding, maximum=MAX_SOURCE_BINDING_JSON_BYTES
        )
        _canonical_json("evidence", self.evidence, maximum=MAX_EVIDENCE_JSON_BYTES)
        _canonical_json(
            "context_provenance",
            self.context_provenance,
            maximum=MAX_CONTEXT_PROVENANCE_JSON_BYTES,
        )
        if self.decision == "CLASSIFIED" and (
            not self.evidence or not self.context_provenance
        ):
            raise ValueError("CLASSIFIED decisions require evidence and context provenance")
        object.__setattr__(self, "created_ns", _timestamp("created_ns", self.created_ns))
        selected_updated = self.created_ns if self.updated_ns is None else self.updated_ns
        object.__setattr__(self, "updated_ns", _timestamp("updated_ns", selected_updated))

    @property
    def source_binding_json(self) -> str:
        return _canonical_json(
            "source_binding", self.source_binding, maximum=MAX_SOURCE_BINDING_JSON_BYTES
        )

    @property
    def top_k_json(self) -> str:
        return _canonical_json_array(
            "top_k",
            [{"label": item.label, "score": item.score} for item in self.top_k],
            maximum=MAX_EVIDENCE_JSON_BYTES // 2,
        )

    @property
    def evidence_json(self) -> str:
        return _canonical_json("evidence", self.evidence, maximum=MAX_EVIDENCE_JSON_BYTES)

    @property
    def context_provenance_json(self) -> str:
        return _canonical_json(
            "context_provenance",
            self.context_provenance,
            maximum=MAX_CONTEXT_PROVENANCE_JSON_BYTES,
        )

    @property
    def representation_fingerprint(self) -> str:
        """Compatibility spelling used by route-produced representations."""

        return self.semantic_representation_fingerprint

    @property
    def top_candidates(self) -> tuple[CurationTopCandidate, ...]:
        return self.top_k

    @property
    def confidence(self) -> float | None:
        return self.top1_score

    @property
    def reason(self) -> str:
        value = self.evidence.get("decision_reason", "current")
        return value if isinstance(value, str) and value.strip() else "current"

    @property
    def original_path(self) -> str | None:
        return _path_from_value(self.context_provenance.get(ORIGINAL_PATH_KEY))

    @property
    def actual_path(self) -> str | None:
        return _path_from_value(
            self.context_provenance.get(
                ACTUAL_PATH_KEY,
                self.context_provenance.get(CURRENT_PATH_KEY),
            )
        )

    @property
    def original_source_binding(self) -> Mapping[str, object] | None:
        return _original_binding_from_context(self.context_provenance)

    @property
    def source_original_binding(self) -> Mapping[str, object] | None:
        return self.original_source_binding

    @property
    def prototype_set_fingerprint(self) -> str:
        """Return the persisted prototype-set fingerprint without inference.

        The Catalog schema intentionally keeps this version in bounded
        ``evidence_json`` rather than adding another version column.  An
        absent value is exposed as ``""`` so the pure policy-bundle validator
        rejects it as ``stale_prototype_set_fingerprint`` instead of silently
        treating ``prototype_version`` as an equivalent fingerprint.
        """

        value = self.evidence.get("prototype_set_fingerprint")
        return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class CurationEmbeddingPage:
    items: tuple[CurationEmbeddingCacheRecord, ...]
    next_key: CurationEmbeddingCacheKey | None
    has_more: bool

    def __iter__(self) -> Iterator[CurationEmbeddingCacheRecord]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def next_cursor(self) -> CurationEmbeddingCacheKey | None:
        return self.next_key


@dataclass(frozen=True, slots=True)
class CurationDecisionPage:
    items: tuple[CurationDecisionRecord, ...]
    next_cursor: tuple[str, str] | None
    has_more: bool

    def __iter__(self) -> Iterator[CurationDecisionRecord]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def next_key(self) -> tuple[str, str] | None:
        return self.next_cursor


EmbeddingCacheKey: TypeAlias = CurationEmbeddingCacheKey
EmbeddingCacheRecord: TypeAlias = CurationEmbeddingCacheRecord
CurationDecision: TypeAlias = CurationDecisionRecord


def _page_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CURATION_PAGE_SIZE
    ):
        raise ValueError(f"limit must be an integer between 1 and {MAX_CURATION_PAGE_SIZE}")
    return value


def _batch_values(
    values: Iterable[object], expected: type[object], *, name: str
) -> tuple[object, ...]:
    selected = tuple(islice(iter(values), MAX_CURATION_BATCH_SIZE + 1))
    if len(selected) > MAX_CURATION_BATCH_SIZE:
        raise ValueError(f"{name} exceeds the batch bound of {MAX_CURATION_BATCH_SIZE}")
    if any(not isinstance(value, expected) for value in selected):
        raise TypeError(f"{name} contains an invalid record")
    return selected


def _run_batch(connection: sqlite3.Connection, writer: Callable[[], None]) -> None:
    """Run one owner-local atomic write, preserving an outer transaction."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        writer()
    except BaseException:
        if own_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            connection.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        raise
    else:
        if own_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")


def _embedding_parameters(record: CurationEmbeddingCacheRecord) -> tuple[object, ...]:
    return (
        record.key.representation_sha256,
        record.key.representation_version,
        record.key.model_signature,
        record.key.role,
        record.key.vector_space,
        record.key.dimensions,
        "float32",
        1,
        record.vector_bytes,
        record.metadata_json,
        record.created_ns,
        record.updated_ns,
    )


_EMBEDDING_UPSERT = """INSERT INTO curator_embedding_cache(
    representation_sha256,representation_version,model_signature,role,
    vector_space,dimensions,vector_dtype,normalized,vector_blob,metadata_json,
    created_ns,updated_ns)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(representation_sha256,representation_version,model_signature,
            role,vector_space,dimensions) DO UPDATE SET
    vector_dtype=excluded.vector_dtype,
    normalized=excluded.normalized,
    vector_blob=excluded.vector_blob,
    metadata_json=excluded.metadata_json,
    updated_ns=excluded.updated_ns"""


def _decision_parameters(record: CurationDecisionRecord) -> tuple[object, ...]:
    return (
        record.source_kind,
        record.file_key,
        record.source_binding_json,
        record.input_signature,
        record.semantic_representation_fingerprint,
        record.representation_version,
        record.model_signature,
        record.role,
        record.vector_space,
        record.dimensions,
        record.ontology_version,
        record.prototype_version,
        record.policy_version,
        record.calibration_version,
        record.decision,
        record.top1_label,
        record.top1_score,
        record.top2_label,
        record.top2_score,
        record.margin,
        record.top_k_json,
        record.evidence_json,
        record.context_provenance_json,
        record.created_ns,
        record.updated_ns,
    )


_DECISION_UPSERT = """INSERT INTO curator_decisions(
    source_kind,file_key,source_binding_json,input_signature,
    semantic_representation_fingerprint,representation_version,model_signature,
    role,vector_space,dimensions,ontology_version,prototype_version,
    policy_version,calibration_version,decision,top1_label,top1_score,
    top2_label,top2_score,margin,top_k_json,evidence_json,
    context_provenance_json,created_ns,updated_ns)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(source_kind,file_key) DO UPDATE SET
    source_binding_json=excluded.source_binding_json,
    input_signature=excluded.input_signature,
    semantic_representation_fingerprint=excluded.semantic_representation_fingerprint,
    representation_version=excluded.representation_version,
    model_signature=excluded.model_signature,
    role=excluded.role,
    vector_space=excluded.vector_space,
    dimensions=excluded.dimensions,
    ontology_version=excluded.ontology_version,
    prototype_version=excluded.prototype_version,
    policy_version=excluded.policy_version,
    calibration_version=excluded.calibration_version,
    decision=excluded.decision,
    top1_label=excluded.top1_label,
    top1_score=excluded.top1_score,
    top2_label=excluded.top2_label,
    top2_score=excluded.top2_score,
    margin=excluded.margin,
    top_k_json=excluded.top_k_json,
    evidence_json=excluded.evidence_json,
    context_provenance_json=excluded.context_provenance_json,
    updated_ns=excluded.updated_ns"""


def _row_value(row: sqlite3.Row | tuple[object, ...], name: str, index: int) -> object:
    try:
        return row[name]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _existing_decision_context(
    connection: sqlite3.Connection,
    record: CurationDecisionRecord,
) -> tuple[dict[str, object], dict[str, object]] | None:
    row = connection.execute(
        """SELECT source_binding_json,context_provenance_json
        FROM curator_decisions WHERE source_kind=? AND file_key=?""",
        (record.source_kind, record.file_key),
    ).fetchone()
    if row is None:
        return None
    return (
        _json_object(
            _row_value(row, "source_binding_json", 0),
            name="existing source_binding_json",
            maximum=MAX_SOURCE_BINDING_JSON_BYTES,
        ),
        _json_object(
            _row_value(row, "context_provenance_json", 1),
            name="existing context_provenance_json",
            maximum=MAX_CONTEXT_PROVENANCE_JSON_BYTES,
        ),
    )


def _identity_or_raise(
    existing_binding: Mapping[str, object],
    existing_context: Mapping[str, object],
    incoming_binding: Mapping[str, object],
) -> None:
    existing_identity = _physical_identity_token(existing_binding)
    original_binding = _original_binding_from_context(existing_context)
    original_identity = (
        None if original_binding is None else _physical_identity_token(original_binding)
    )
    if existing_identity is not None and original_identity is not None:
        if existing_identity != original_identity:
            raise CurationPhysicalIdentityMismatch(
                "existing current and original Catalog bindings disagree"
            )
    expected_identity = existing_identity or original_identity
    incoming_identity = _physical_identity_token(incoming_binding)
    if expected_identity is None or incoming_identity is None:
        raise CurationPhysicalIdentityMismatch(
            "physical identity is required to rebind an existing curation decision"
        )
    if expected_identity != incoming_identity:
        raise CurationPhysicalIdentityMismatch(
            "Catalog source key was reused for a different physical identity"
        )


def _reconciled_decision(
    record: CurationDecisionRecord,
    existing: tuple[Mapping[str, object], Mapping[str, object]] | None,
) -> CurationDecisionRecord:
    """Keep first origin fields while refreshing only current-path context."""

    incoming_context = dict(record.context_provenance)
    incoming_binding = dict(record.source_binding)
    incoming_original_binding = _original_binding_from_context(incoming_context)
    incoming_identity = _physical_identity_token(incoming_binding)
    if incoming_original_binding is not None:
        original_identity = _physical_identity_token(incoming_original_binding)
        if (
            incoming_identity is not None
            and original_identity is not None
            and incoming_identity != original_identity
        ):
            raise CurationPhysicalIdentityMismatch(
                "incoming original and current Catalog bindings disagree"
            )

    incoming_binding_path = _binding_current_path(incoming_binding)
    explicit_actual = _path_from_value(incoming_context.get(ACTUAL_PATH_KEY))
    explicit_current = _path_from_value(incoming_context.get(CURRENT_PATH_KEY))
    if (
        explicit_actual is not None
        and incoming_binding_path is not None
        and explicit_actual != incoming_binding_path
    ):
        raise CurationPhysicalIdentityMismatch(
            "incoming actual path disagrees with the current Catalog binding"
        )
    if (
        explicit_current is not None
        and incoming_binding_path is not None
        and explicit_current != incoming_binding_path
    ):
        raise CurationPhysicalIdentityMismatch(
            "incoming current path disagrees with the current Catalog binding"
        )
    actual_path = explicit_actual or explicit_current or incoming_binding_path

    if existing is None:
        original_binding = incoming_original_binding or incoming_binding
        original_path = _path_from_value(incoming_context.get(ORIGINAL_PATH_KEY))
        if original_path is None:
            original_path = actual_path
        merged = dict(incoming_context)
        if original_path is not None:
            merged[ORIGINAL_PATH_KEY] = original_path
        merged[ORIGINAL_SOURCE_BINDING_KEY] = dict(original_binding)
        merged[SOURCE_ORIGINAL_BINDING_KEY] = dict(original_binding)
        if actual_path is not None:
            merged[ACTUAL_PATH_KEY] = actual_path
            merged[CURRENT_PATH_KEY] = actual_path
        return replace(record, context_provenance=merged)

    existing_binding, existing_context = existing
    _identity_or_raise(existing_binding, existing_context, incoming_binding)
    existing_original_binding = _original_binding_from_context(existing_context)
    original_binding = existing_original_binding or existing_binding
    original_path = _path_from_value(existing_context.get(ORIGINAL_PATH_KEY))
    if original_path is None:
        original_path = _path_from_value(existing_context.get(ACTUAL_PATH_KEY))
    if original_path is None:
        original_path = _path_from_value(existing_context.get(CURRENT_PATH_KEY))
    if original_path is None:
        original_path = _binding_current_path(existing_binding)
    if original_path is None:
        original_path = _path_from_value(incoming_context.get(ORIGINAL_PATH_KEY))
    if original_path is None:
        original_path = actual_path

    merged = dict(existing_context)
    # Context fields other than origin/current locator are refreshed from the
    # latest Catalog observation.  The original fields are reset below so an
    # untrusted second-run path cannot overwrite them.
    for key, value in incoming_context.items():
        if key not in {
            ORIGINAL_PATH_KEY,
            ORIGINAL_SOURCE_BINDING_KEY,
            SOURCE_ORIGINAL_BINDING_KEY,
        }:
            merged[key] = value
    if original_path is not None:
        merged[ORIGINAL_PATH_KEY] = original_path
    merged[ORIGINAL_SOURCE_BINDING_KEY] = dict(original_binding)
    merged[SOURCE_ORIGINAL_BINDING_KEY] = dict(original_binding)
    if actual_path is not None:
        merged[ACTUAL_PATH_KEY] = actual_path
        merged[CURRENT_PATH_KEY] = actual_path
    return replace(record, context_provenance=merged)


def _prepare_decision_batch(
    connection: sqlite3.Connection,
    selected: tuple[CurationDecisionRecord, ...],
) -> tuple[CurationDecisionRecord, ...]:
    """Reconcile origin/current context once, bounded to the decision batch."""

    existing_by_key: dict[
        tuple[str, str], tuple[Mapping[str, object], Mapping[str, object]] | None
    ] = {}
    prepared: list[CurationDecisionRecord] = []
    for record in selected:
        key = (record.source_kind, record.file_key)
        if key not in existing_by_key:
            existing_by_key[key] = _existing_decision_context(connection, record)
        effective = _reconciled_decision(record, existing_by_key[key])
        prepared.append(effective)
        existing_by_key[key] = (
            dict(effective.source_binding),
            dict(effective.context_provenance),
        )
    return tuple(prepared)


def upsert_embedding_cache_batch(
    connection: sqlite3.Connection,
    records: Iterable[CurationEmbeddingCacheRecord],
) -> int:
    """Atomically upsert one bounded embedding batch on the supplied owner."""

    selected = _batch_values(records, CurationEmbeddingCacheRecord, name="embedding batch")
    if not selected:
        return 0

    def write() -> None:
        connection.executemany(
            _EMBEDDING_UPSERT,
            (_embedding_parameters(item) for item in selected),
        )

    _run_batch(connection, write)
    return len(selected)


def upsert_curation_decision_batch(
    connection: sqlite3.Connection,
    records: Iterable[CurationDecisionRecord],
) -> int:
    """Atomically upsert current decisions, preserving document-key rebinding."""

    selected = _batch_values(records, CurationDecisionRecord, name="decision batch")
    if not selected:
        return 0

    def write() -> None:
        prepared = _prepare_decision_batch(connection, selected)
        connection.executemany(
            _DECISION_UPSERT,
            (_decision_parameters(item) for item in prepared),
        )

    _run_batch(connection, write)
    return len(selected)


def upsert_curation_batch(
    connection: sqlite3.Connection,
    *,
    embeddings: Iterable[CurationEmbeddingCacheRecord] = (),
    decisions: Iterable[CurationDecisionRecord] = (),
) -> tuple[int, int]:
    """Atomically persist embeddings and decisions in one Catalog transaction."""

    selected_embeddings = _batch_values(
        embeddings, CurationEmbeddingCacheRecord, name="embedding batch"
    )
    selected_decisions = _batch_values(
        decisions, CurationDecisionRecord, name="decision batch"
    )
    if not selected_embeddings and not selected_decisions:
        return 0, 0

    def write() -> None:
        if selected_embeddings:
            connection.executemany(
                _EMBEDDING_UPSERT,
                (_embedding_parameters(item) for item in selected_embeddings),
            )
        if selected_decisions:
            prepared_decisions = _prepare_decision_batch(connection, selected_decisions)
            connection.executemany(
                _DECISION_UPSERT,
                (_decision_parameters(item) for item in prepared_decisions),
            )

    _run_batch(connection, write)
    return len(selected_embeddings), len(selected_decisions)


def _embedding_from_row(row: sqlite3.Row | tuple[object, ...]) -> CurationEmbeddingCacheRecord:
    def value(name: str, index: int) -> object:
        try:
            return row[name]  # type: ignore[index]
        except (IndexError, KeyError, TypeError):
            return row[index]

    dimensions = _dimensions(value("dimensions", 5))
    if value("vector_dtype", 6) != "float32" or int(value("normalized", 7)) != 1:
        raise ValueError("curation embedding cache row has an unsupported vector encoding")
    try:
        payload = bytes(value("vector_blob", 8))
    except (TypeError, ValueError) as exc:
        raise ValueError("curation embedding cache row has no byte vector") from exc
    if len(payload) != dimensions * _FLOAT32_WIDTH:
        raise ValueError("curation embedding cache has an invalid vector length")
    try:
        vector = struct.unpack(f"<{dimensions}f", payload)
        metadata = json.loads(str(value("metadata_json", 9)))
    except (TypeError, ValueError, json.JSONDecodeError, struct.error) as exc:
        raise ValueError("curation embedding cache row is not valid") from exc
    payload_norm = math.sqrt(math.fsum(item * item for item in vector))
    if any(not math.isfinite(item) for item in vector) or not math.isfinite(payload_norm):
        raise ValueError("curation embedding cache row contains a non-finite vector")
    if not math.isclose(payload_norm, 1.0, rel_tol=2e-4, abs_tol=2e-4):
        raise ValueError("curation embedding cache row is not normalized")
    if not isinstance(metadata, dict):
        raise ValueError("curation embedding cache metadata is not an object")
    return CurationEmbeddingCacheRecord(
        key=CurationEmbeddingCacheKey(
            representation_sha256=str(value("representation_sha256", 0)),
            representation_version=str(value("representation_version", 1)),
            model_signature=str(value("model_signature", 2)),
            role=str(value("role", 3)),
            vector_space=str(value("vector_space", 4)),
            dimensions=dimensions,
        ),
        vector=vector,
        metadata=metadata,
        created_ns=int(value("created_ns", 10)),
        updated_ns=int(value("updated_ns", 11)),
    )


def read_embedding_cache(
    connection: sqlite3.Connection,
    key: CurationEmbeddingCacheKey | None = None,
    *,
    representation_sha256: str | None = None,
    representation_version: str | None = None,
    model_signature: str | None = None,
    role: str | None = None,
    vector_space: str | None = None,
    dimensions: int | None = None,
) -> CurationEmbeddingCacheRecord | None:
    """Read one exact content/representation/model cache entry."""

    if key is None:
        if any(
            value is None
            for value in (
                representation_sha256,
                representation_version,
                model_signature,
                role,
                vector_space,
                dimensions,
            )
        ):
            raise TypeError("an exact CurationEmbeddingCacheKey or all key fields is required")
        key = CurationEmbeddingCacheKey(
            representation_sha256=representation_sha256,  # type: ignore[arg-type]
            representation_version=representation_version,  # type: ignore[arg-type]
            model_signature=model_signature,  # type: ignore[arg-type]
            role=role,  # type: ignore[arg-type]
            vector_space=vector_space,  # type: ignore[arg-type]
            dimensions=dimensions,  # type: ignore[arg-type]
        )
    if not isinstance(key, CurationEmbeddingCacheKey):
        raise TypeError("key must be a CurationEmbeddingCacheKey")
    row = connection.execute(
        """SELECT representation_sha256,representation_version,model_signature,
        role,vector_space,dimensions,vector_dtype,normalized,vector_blob,
        metadata_json,created_ns,updated_ns
        FROM curator_embedding_cache
        WHERE representation_sha256=? AND representation_version=?
          AND model_signature=? AND role=? AND vector_space=? AND dimensions=?""",
        (
            key.representation_sha256,
            key.representation_version,
            key.model_signature,
            key.role,
            key.vector_space,
            key.dimensions,
        ),
    ).fetchone()
    return None if row is None else _embedding_from_row(row)


lookup_embedding_cache = read_embedding_cache
read_exact_embedding_cache = read_embedding_cache


def read_embedding_cache_page(
    connection: sqlite3.Connection,
    *,
    limit: int = 256,
    after: CurationEmbeddingCacheKey | None = None,
) -> CurationEmbeddingPage:
    """Read a bounded keyset page from the content-only embedding cache."""

    selected_limit = _page_limit(limit)
    if after is not None and not isinstance(after, CurationEmbeddingCacheKey):
        raise TypeError("after must be a CurationEmbeddingCacheKey or None")
    where = ""
    parameters: tuple[object, ...] = ()
    if after is not None:
        where = """WHERE (representation_sha256 > ?)
            OR (representation_sha256=? AND representation_version > ?)
            OR (representation_sha256=? AND representation_version=?
                AND model_signature > ?)
            OR (representation_sha256=? AND representation_version=?
                AND model_signature=? AND role > ?)
            OR (representation_sha256=? AND representation_version=?
                AND model_signature=? AND role=? AND vector_space > ?)
            OR (representation_sha256=? AND representation_version=?
                AND model_signature=? AND role=? AND vector_space=?
                AND dimensions > ?)"""
        parameters = (
            after.representation_sha256,
            after.representation_sha256,
            after.representation_version,
            after.representation_sha256,
            after.representation_version,
            after.model_signature,
            after.representation_sha256,
            after.representation_version,
            after.model_signature,
            after.role,
            after.representation_sha256,
            after.representation_version,
            after.model_signature,
            after.role,
            after.vector_space,
            after.representation_sha256,
            after.representation_version,
            after.model_signature,
            after.role,
            after.vector_space,
            after.dimensions,
        )
    rows = connection.execute(
        f"""SELECT representation_sha256,representation_version,model_signature,
    role,vector_space,dimensions,vector_dtype,normalized,vector_blob,
        metadata_json,created_ns,updated_ns
        FROM curator_embedding_cache {where}
        ORDER BY representation_sha256,representation_version,model_signature,
                 role,vector_space,dimensions LIMIT ?""",
        (*parameters, selected_limit + 1),
    ).fetchall()
    has_more = len(rows) > selected_limit
    bounded_rows = rows[:selected_limit]
    items = tuple(_embedding_from_row(row) for row in bounded_rows)
    return CurationEmbeddingPage(
        items=items,
        next_key=items[-1].key if has_more and items else None,
        has_more=has_more,
    )


def _decision_from_row(row: sqlite3.Row | tuple[object, ...]) -> CurationDecisionRecord:
    def value(name: str, index: int) -> object:
        try:
            return row[name]  # type: ignore[index]
        except (IndexError, KeyError, TypeError):
            return row[index]

    def object_json(name: str, index: int) -> Mapping[str, object]:
        try:
            decoded = json.loads(str(value(name, index)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"curation decision {name} is invalid") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"curation decision {name} is not an object")
        return decoded

    try:
        decoded_top_k = json.loads(str(value("top_k_json", 20)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("curation decision top_k_json is invalid") from exc
    if not isinstance(decoded_top_k, list) or len(decoded_top_k) > MAX_TOP_K:
        raise ValueError("curation decision top_k_json exceeds its bound")
    candidates: list[CurationTopCandidate] = []
    for item in decoded_top_k:
        if not isinstance(item, dict) or set(item) != {"label", "score"}:
            raise ValueError("curation decision top_k_json has an invalid candidate")
        candidates.append(CurationTopCandidate(label=item["label"], score=item["score"]))
    return CurationDecisionRecord(
        source_kind=str(value("source_kind", 0)),
        file_key=str(value("file_key", 1)),
        source_binding=object_json("source_binding_json", 2),
        input_signature=str(value("input_signature", 3)),
        semantic_representation_fingerprint=str(value("semantic_representation_fingerprint", 4)),
        representation_version=str(value("representation_version", 5)),
        model_signature=str(value("model_signature", 6)),
        role=str(value("role", 7)),
        vector_space=str(value("vector_space", 8)),
        dimensions=int(value("dimensions", 9)),
        ontology_version=str(value("ontology_version", 10)),
        prototype_version=str(value("prototype_version", 11)),
        policy_version=str(value("policy_version", 12)),
        calibration_version=str(value("calibration_version", 13)),
        decision=str(value("decision", 14)),
        top1_label=None if value("top1_label", 15) is None else str(value("top1_label", 15)),
        top1_score=None if value("top1_score", 16) is None else float(value("top1_score", 16)),
        top2_label=None if value("top2_label", 17) is None else str(value("top2_label", 17)),
        top2_score=None if value("top2_score", 18) is None else float(value("top2_score", 18)),
        margin=None if value("margin", 19) is None else float(value("margin", 19)),
        top_k=tuple(candidates),
        evidence=object_json("evidence_json", 21),
        context_provenance=object_json("context_provenance_json", 22),
        created_ns=int(value("created_ns", 23)),
        updated_ns=int(value("updated_ns", 24)),
    )


_DECISION_SELECT = """SELECT source_kind,file_key,source_binding_json,input_signature,
    semantic_representation_fingerprint,representation_version,model_signature,
    role,vector_space,dimensions,ontology_version,prototype_version,
    policy_version,calibration_version,decision,top1_label,top1_score,
    top2_label,top2_score,margin,top_k_json,evidence_json,
    context_provenance_json,created_ns,updated_ns
    FROM curator_decisions"""


def _receipt_object(receipt: Mapping[str, object] | str) -> dict[str, object]:
    payload = _json_object(
        receipt,
        name="organization move receipt",
        maximum=MAX_EVIDENCE_JSON_BYTES,
    )
    target_identity = payload.get("target_identity")
    source_path = payload.get("source_path")
    target_path = payload.get("target_path")
    if (
        payload.get("organization_receipt_schema")
        != "neocortex.organization-move-receipt/v1"
        or payload.get("source_absent") is not True
        or not isinstance(source_path, str)
        or not isinstance(target_path, str)
        or not source_path.startswith("/")
        or not target_path.startswith("/")
        or not isinstance(target_identity, Mapping)
        or not target_identity
        or not all(
            key in target_identity
            for key in ("path", "size", "mtime_ns", "birthtime_ns", "volume_id", "file_id")
        )
        or (not payload.get("source_identity")
        and not payload.get("source_digest"))
    ):
        raise CurationRebindError("organization receipt lacks the exact move fence")
    return payload


def rebind_curation_decision(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    file_key: str,
    source_path: str,
    target_path: str,
    receipt: Mapping[str, object] | str,
    current_source_binding: Mapping[str, object] | str | None = None,
    source_binding: Mapping[str, object] | str | None = None,
    updated_ns: int | None = None,
) -> CurationDecisionRecord:
    """Rebind one current decision after an owner-verified physical move.

    The caller must be inside the Catalog owner boundary (or pass its sole
    owner connection).  The current ``documents`` row is checked against the
    supplied binding and target path, while the stored decision and receipt
    must prove the same physical identity.  Only the current binding and
    auxiliary actual/current path are changed; origin context and all content,
    model, and policy versions remain untouched.
    """

    source_kind = _bounded_text("source_kind", source_kind)
    file_key = _bounded_text("file_key", file_key)
    source_path = _bounded_text("source_path", source_path)
    target_path = _bounded_text("target_path", target_path)
    if not source_path.startswith("/") or not target_path.startswith("/"):
        raise CurationRebindError("Catalog rebind paths must be absolute")
    if current_source_binding is not None and source_binding is not None:
        left = _json_object(
            current_source_binding,
            name="current source binding",
            maximum=MAX_SOURCE_BINDING_JSON_BYTES,
        )
        right = _json_object(
            source_binding,
            name="source binding",
            maximum=MAX_SOURCE_BINDING_JSON_BYTES,
        )
        if left != right:
            raise CurationRebindError("duplicate current bindings disagree")
        selected_binding = left
    elif current_source_binding is not None:
        selected_binding = _json_object(
            current_source_binding,
            name="current source binding",
            maximum=MAX_SOURCE_BINDING_JSON_BYTES,
        )
    elif source_binding is not None:
        selected_binding = _json_object(
            source_binding,
            name="source binding",
            maximum=MAX_SOURCE_BINDING_JSON_BYTES,
        )
    else:
        selected_binding = None
    selected_receipt = _receipt_object(receipt)
    selected_updated = (
        time.time_ns() if updated_ns is None else _timestamp("updated_ns", updated_ns)
    )
    result: list[CurationDecisionRecord] = []

    def write() -> None:
        document = connection.execute(
            """SELECT path,resource_binding_json FROM documents
            WHERE source_kind=? AND file_key=? AND active=1""",
            (source_kind, file_key),
        ).fetchone()
        if document is None:
            raise CurationRebindError("Catalog current document is missing")
        document_path = _path_from_value(_row_value(document, "path", 0))
        raw_document_binding = _row_value(document, "resource_binding_json", 1)
        document_binding = _json_object(
            raw_document_binding,
            name="current Catalog source binding",
            maximum=MAX_SOURCE_BINDING_JSON_BYTES,
        )
        if selected_binding is not None and selected_binding != document_binding:
            raise CurationRebindError("supplied binding differs from current Catalog binding")
        binding = document_binding if selected_binding is None else selected_binding
        if document_path != target_path or _binding_current_path(binding) != target_path:
            raise CurationRebindError("Catalog current binding is not at the receipt target")
        if binding.get("source_kind") not in (None, source_kind) or binding.get("file_key") not in (
            None,
            file_key,
        ):
            raise CurationRebindError("current Catalog binding owner differs from decision")

        receipt_source = str(selected_receipt["source_path"])
        receipt_target = str(selected_receipt["target_path"])
        if receipt_source != source_path or receipt_target != target_path:
            raise CurationRebindError("move receipt paths differ from the requested rebind")
        target_identity = selected_receipt["target_identity"]
        assert isinstance(target_identity, Mapping)
        try:
            receipt_identity = (
                str(target_identity["volume_id"]),
                str(target_identity["file_id"]),
                int(target_identity["birthtime_ns"]),
            )
            target_size = int(target_identity["size"])
            target_mtime = int(target_identity["mtime_ns"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CurationRebindError("move receipt target identity is invalid") from exc
        if str(target_identity["path"]) != target_path:
            raise CurationRebindError("move receipt target identity path differs")
        binding_identity = _physical_identity_components(binding)
        if binding_identity != receipt_identity:
            raise CurationPhysicalIdentityMismatch(
                "Catalog current binding differs from move receipt target identity"
            )
        revision = binding.get("physical_anchor_revision")
        if isinstance(revision, Mapping):
            try:
                if (
                    int(revision["size"]) != target_size
                    or int(revision["mtime_ns"]) != target_mtime
                ):
                    raise CurationRebindError("Catalog binding revision differs from move receipt")
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CurationRebindError("Catalog binding revision is invalid") from exc

        row = connection.execute(
            _DECISION_SELECT + " WHERE source_kind=? AND file_key=?",
            (source_kind, file_key),
        ).fetchone()
        if row is None:
            raise CurationRebindError("current curation decision is missing")
        existing_record = _decision_from_row(row)
        existing_path = _binding_current_path(existing_record.source_binding)
        if existing_path is None:
            existing_path = existing_record.actual_path or existing_record.original_path
        if existing_path is not None and existing_path != source_path:
            raise CurationRebindError("curation decision is not bound to the move source")
        _identity_or_raise(
            existing_record.source_binding,
            existing_record.context_provenance,
            binding,
        )
        incoming_context = dict(existing_record.context_provenance)
        incoming_context[ACTUAL_PATH_KEY] = target_path
        incoming_context[CURRENT_PATH_KEY] = target_path
        incoming = replace(
            existing_record,
            source_binding=binding,
            context_provenance=incoming_context,
            updated_ns=selected_updated,
        )
        effective = _reconciled_decision(
            incoming,
            (dict(existing_record.source_binding), dict(existing_record.context_provenance)),
        )
        connection.execute(_DECISION_UPSERT, _decision_parameters(effective))
        result.append(effective)

    _run_batch(connection, write)
    if not result:  # pragma: no cover - write either appends or raises
        raise CurationRebindError("curation rebind did not produce a decision")
    return result[0]


def read_current_curation_decision(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    file_key: str,
) -> CurationDecisionRecord | None:
    """Read the current decision by the Catalog's stable document key."""

    source_kind = _bounded_text("source_kind", source_kind)
    file_key = _bounded_text("file_key", file_key)
    row = connection.execute(
        _DECISION_SELECT + " WHERE source_kind=? AND file_key=?",
        (source_kind, file_key),
    ).fetchone()
    return None if row is None else _decision_from_row(row)


read_curation_decision = read_current_curation_decision
read_current_decision = read_current_curation_decision


def read_current_curation_decisions_page(
    connection: sqlite3.Connection,
    *,
    limit: int = 256,
    after: tuple[str, str] | None = None,
) -> CurationDecisionPage:
    """Read bounded current decisions using source-kind/file-key keyset pages."""

    selected_limit = _page_limit(limit)
    if after is not None:
        if (
            not isinstance(after, tuple)
            or len(after) != 2
            or not all(isinstance(item, str) for item in after)
        ):
            raise TypeError("after must be a (source_kind, file_key) tuple or None")
        after = (
            _bounded_text("after source_kind", after[0]),
            _bounded_text("after file_key", after[1]),
        )
    where = ""
    parameters: tuple[object, ...] = ()
    if after is not None:
        where = " WHERE (source_kind > ?) OR (source_kind=? AND file_key>?)"
        parameters = (after[0], after[0], after[1])
    rows = connection.execute(
        _DECISION_SELECT
        + where
        + " ORDER BY source_kind,file_key LIMIT ?",
        (*parameters, selected_limit + 1),
    ).fetchall()
    has_more = len(rows) > selected_limit
    bounded_rows = rows[:selected_limit]
    items = tuple(_decision_from_row(row) for row in bounded_rows)
    next_cursor = (items[-1].source_kind, items[-1].file_key) if has_more and items else None
    return CurationDecisionPage(items=items, next_cursor=next_cursor, has_more=has_more)


read_curation_decisions_page = read_current_curation_decisions_page
upsert_decisions_batch = upsert_curation_decision_batch


__all__ = [
    "ACTUAL_PATH_KEY",
    "CURRENT_PATH_KEY",
    "MAX_CURATION_BATCH_SIZE",
    "MAX_CURATION_PAGE_SIZE",
    "MAX_TOP_K",
    "ORIGINAL_PATH_KEY",
    "ORIGINAL_SOURCE_BINDING_KEY",
    "SOURCE_ORIGINAL_BINDING_KEY",
    "CurationDecision",
    "CurationDecisionPage",
    "CurationDecisionRecord",
    "CurationEmbeddingCacheKey",
    "CurationEmbeddingCacheRecord",
    "CurationEmbeddingPage",
    "CurationPhysicalIdentityMismatch",
    "CurationRebindError",
    "CurationTopCandidate",
    "EmbeddingCacheKey",
    "EmbeddingCacheRecord",
    "lookup_embedding_cache",
    "read_curation_decision",
    "read_curation_decisions_page",
    "read_current_curation_decision",
    "read_current_curation_decisions_page",
    "read_current_decision",
    "read_embedding_cache",
    "read_embedding_cache_page",
    "read_exact_embedding_cache",
    "rebind_curation_decision",
    "upsert_curation_batch",
    "upsert_curation_decision_batch",
    "upsert_decisions_batch",
    "upsert_embedding_cache_batch",
]
