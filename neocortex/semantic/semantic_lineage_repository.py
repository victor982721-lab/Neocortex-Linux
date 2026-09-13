"""Owner-local semantic work receipts and read-only lineage explanations."""

from __future__ import annotations
import hashlib
import json
import math
import platform
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import xxhash

from .derivation_contracts import (
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
from neocortex.knowledge.knowledge_contracts import RevisionRef, RevisionState
from .semantic_models import (
    ContentFingerprint,
    VectorDType,
    canonical_json,
    decode_vector,
    fingerprint_bytes,
)
from .semantic_repository_common import StaleEmbeddingJobError, _same_fingerprint
from .semantic_schema import (
    SemanticStateError,
    _read_schema_version,
    _validate_version_contract,
    semantic_database,
)


WORK_RECEIPT_CONTRACT = "neocortex.work-receipt/v1"
SEMANTIC_CHUNK_STAGE = "semantic.text.chunk.materialize"
SEMANTIC_CHUNK_MANIFEST_STAGE = "semantic.text.chunk.manifest"
SEMANTIC_CHUNK_PUBLICATION_STAGE = "semantic.text.chunk.publish"
SEMANTIC_EMBEDDING_STAGE = "semantic.embedding"
SEMANTIC_EMBEDDING_CLONE_STAGE = "semantic.embedding.clone"
SEMANTIC_EMBEDDING_DISCARD_STAGE = "semantic.embedding.provider_execution_discard"
SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE = "semantic.vector_payload.legacy_attest"
SEMANTIC_EMBEDDING_MANIFEST_STAGE = "semantic.embedding.manifest"
SEMANTIC_GENERATION_PUBLICATION_STAGE = "semantic.embedding.publish"
_EFFECTIVE_CONFIG_KEYS_BY_STAGE = {
    SEMANTIC_CHUNK_STAGE: frozenset({"chunking_signature"}),
    SEMANTIC_CHUNK_MANIFEST_STAGE: frozenset(
        {"manifest_level", "manifest_ordinal", "member_count"}
    ),
    SEMANTIC_CHUNK_PUBLICATION_STAGE: frozenset({"chunking_signature"}),
    SEMANTIC_EMBEDDING_STAGE: frozenset(),
    SEMANTIC_EMBEDDING_CLONE_STAGE: frozenset({"base_generation_id", "member_count"}),
    SEMANTIC_EMBEDDING_DISCARD_STAGE: frozenset(
        {
            "disposition",
            "incumbent_payload_id",
            "observation_contract_xxh3_128",
        }
    ),
    SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE: frozenset({"payload_id"}),
    SEMANTIC_EMBEDDING_MANIFEST_STAGE: frozenset(
        {"manifest_level", "manifest_ordinal", "member_count"}
    ),
    SEMANTIC_GENERATION_PUBLICATION_STAGE: frozenset(),
}
_SENSITIVE_CONFIGURATION_SUFFIXES = (
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "secret_key",
    "token",
)
_MAX_CONFIGURATION_CAPTURE_NODES = 4_096
_MAX_CONFIGURATION_VALUE_CHARS = 4_000
MAX_LINEAGE_ROWS = 1_000
_MAX_MANIFEST_INPUTS = 256
_MAX_OUTBOX_PAGE_BYTES = 8 * 1024 * 1024
_RECEIPT_LOOKUP_BATCH = 250
_RECEIPT_LOOKUP_TABLE = "_semantic_receipt_output_lookup"
_RECEIPT_LOOKUP_STATE_TABLE = "_semantic_receipt_output_lookup_state"
_RECEIPT_LOOKUP_INDEX = "_semantic_receipt_output_lookup_materialization_idx"
_RECEIPT_LOOKUP_SAVEPOINT = "semantic_receipt_output_lookup_refresh"
_SAFE_FAILURE_REASON_CODES = frozenset(
    {
        "deadline_cancelled",
        "lease_expired",
        "provider_failed",
        "provider_timeout",
        "source_changed",
    }
)

# Work receipts were introduced by v7.  v8 keeps their tables and payload
# contract unchanged; v9/v10 use the referenced event representation. Keep
# these sets explicit rather than accepting future versions by comparison (or
# by a ``>=`` check), so a newer schema cannot silently acquire old semantics.
_RECEIPT_SCHEMA_VERSIONS = frozenset({7, 8, 9, 10})
_LINEAGE_SCHEMA_VERSIONS = frozenset({6, 7, 8, 9, 10})
_RECEIPT_LINEAGE_SCHEMA_VERSIONS = frozenset({7, 8, 9, 10})
_FORWARD_RECEIPT_SCHEMA_TRANSITIONS = frozenset(
    {(7, 8), (7, 9), (7, 10), (8, 9), (8, 10), (9, 10)}
)
# Stable digest encoding for pre-existing manifest/clone/attestation IDs.  It
# is not the schema advertised by a new receipt or locator.
_SEMANTIC_IDENTITY_SCHEMA_VERSION = 7
_SEMANTIC_DERIVATION_EVENT_V1 = "neocortex.semantic-derivation-event/v1"
_SEMANTIC_DERIVATION_EVENT_V2 = "neocortex.semantic-derivation-event/v2"


@dataclass(frozen=True, slots=True)
class SemanticChunkOrigin:
    """One observed item revision from which a chunk was materialized."""

    item_revision_id: int
    source_kind: str
    source_identity: str
    identity_version: str
    source_revision: Mapping[str, object]
    materialization_receipt_id: int | None
    publication_receipt_id: int | None
    refresh_token: str
    published: bool
    lineage_status: str


@dataclass(frozen=True, slots=True)
class SemanticEmbeddingDerivation:
    """One immutable embedding member and its exact published generation."""

    member_id: int
    generation_id: int
    generation_status: str
    published: bool
    processing_signature: str
    model_signature: str
    model_id: str
    model_version: str
    provider: str
    vector_space: str
    payload_id: int
    item_revision_id: int
    chunk_revision_id: int
    stage_id: str
    execution_mode: str
    receipt_id: int | None
    lineage_status: str


@dataclass(frozen=True, slots=True)
class SemanticTextChunkLineage:
    """Causal explanation for one semantic text chunk."""

    chunk_id: str
    item_id: str
    chunk_revision_id: int | None
    chunking_signature: str
    chunk_stage_id: str
    chunk_stage_version: str
    published: bool
    lineage_status: str
    origins: tuple[SemanticChunkOrigin, ...]
    origin_count: int
    origins_truncated: bool
    embeddings: tuple[SemanticEmbeddingDerivation, ...]
    embedding_count: int
    embeddings_truncated: bool


@dataclass(frozen=True, slots=True)
class SemanticRevisionChunkPage:
    """One bounded dependency page for a route-owned source revision."""

    revision_id: str
    chunk_ids: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SemanticDerivationEvent:
    """One owner-local outbox fact available to rebuild a projection."""

    event_id: int
    receipt_id: int
    event_kind: str
    aggregate_kind: str
    aggregate_id: str
    payload: Mapping[str, object]
    receipt: Mapping[str, object]
    committed_ns: int


def _json_object(raw: object, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(str(raw))
    except (RecursionError, TypeError, ValueError) as exc:
        raise SemanticStateError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SemanticStateError(f"{label} is not a JSON object")
    return value


def _stable_key(stage_id: str, parts: Sequence[object]) -> str:
    identity = canonical_json(
        {
            "schema": WORK_RECEIPT_CONTRACT,
            "stage_id": stage_id,
            "parts": list(parts),
        }
    )
    return "semantic-work-xxh3-128:" + xxhash.xxh3_128_hexdigest(identity.encode("utf-8"))


def _require_current_receipt_schema(connection: sqlite3.Connection) -> int:
    version = _read_schema_version(connection)
    if version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            "semantic work receipts require schema 7, 8, 9 or 10; "
            f"observed {version!r}"
        )
    return int(version)


def _receipt_schema_version(receipt: WorkReceipt) -> int:
    """Return the explicit owner schema advertised by one stored receipt."""

    raw_version = dict(receipt.runtime).get("semantic_schema")
    if raw_version not in {str(version) for version in _RECEIPT_SCHEMA_VERSIONS}:
        raise ValueError(
            "semantic WorkReceipt has an unsupported owner schema version: "
            f"{raw_version!r}"
        )
    return int(str(raw_version))


def _validate_receipt_semantic_locators(receipt: WorkReceipt) -> None:
    """Reject unsupported Semantic owner locators without touching upstream refs."""

    receipt_schema = _receipt_schema_version(receipt)
    bindings: tuple[InputBinding | OutputBinding, ...] = (*receipt.inputs, *receipt.outputs)
    for binding in bindings:
        materialization = binding.materialization
        if materialization is None or materialization.owner != "semantic":
            continue
        if (
            isinstance(materialization.schema_version, bool)
            or materialization.schema_version not in _RECEIPT_SCHEMA_VERSIONS
            or materialization.schema_version > receipt_schema
        ):
            raise ValueError(
                "semantic WorkReceipt has an unsupported Semantic locator schema: "
                f"{materialization.schema_version!r}"
            )


def _semantic_materialization_for_schema(
    materialization: MaterializationRef,
    *,
    schema_version: int,
) -> MaterializationRef:
    """Bind a newly derived Semantic locator to the observed owner schema.

    Materializations from another owner are provenance from that owner and are
    intentionally left untouched.  This matters for source revisions carried
    through a Semantic receipt: their historical owner schema is not a claim
    about the current Semantic SQLite owner.
    """

    if materialization.owner != "semantic":
        return materialization
    if (
        isinstance(materialization.schema_version, bool)
        or materialization.schema_version not in _RECEIPT_SCHEMA_VERSIONS
    ):
        raise SemanticStateError(
            "semantic materialization owner schema metadata is not 7, 8, 9 or 10"
        )
    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic materialization schema {schema_version!r} is unsupported"
        )
    if materialization.schema_version == schema_version:
        return materialization
    if materialization.schema_version > schema_version:
        raise SemanticStateError(
            "semantic materialization owner schema cannot be downgraded"
        )
    return replace(materialization, schema_version=schema_version)


def _normalize_receipt_semantic_schema_metadata(value: object) -> object:
    """Normalize only bounded owner-schema metadata for forward writes.

    This helper is intentionally strict: the caller must separately prove that
    the stored receipt/new owner pair is one of the supported forward
    transitions.  A missing, boolean, malformed, or future owner schema is not
    a compatibility case.
    """

    if isinstance(value, Mapping):
        normalized = {
            key: _normalize_receipt_semantic_schema_metadata(item)
            for key, item in value.items()
        }
        if (
            normalized.get("kind") == "materialization_ref"
            and normalized.get("owner") == "semantic"
        ):
            owner_schema = normalized.get("owner_schema_version")
            if (
                isinstance(owner_schema, bool)
                or not isinstance(owner_schema, int)
                or owner_schema not in _RECEIPT_SCHEMA_VERSIONS
            ):
                raise SemanticStateError(
                    "semantic materialization owner schema metadata is not 7, 8, 9 or 10"
                )
            normalized["owner_schema_version"] = "<semantic-owner-schema>"
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_receipt_semantic_schema_metadata(item) for item in value]
    return value


def _utc_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def _binding_identity(binding: Mapping[str, object]) -> tuple[str, str, int | None]:
    kind = str(binding.get("kind") or "semantic_input")
    identifier = str(
        binding.get("materialization_id")
        or binding.get("item_id")
        or binding.get("chunk_id")
        or binding.get("entity_id")
        or binding.get("generation_id")
        or binding.get("payload_id")
        or "unknown"
    )
    generation_value = binding.get("revision_id") or binding.get("generation_id")
    generation = (
        int(generation_value)
        if isinstance(generation_value, int) and not isinstance(generation_value, bool)
        else None
    )
    return kind, identifier, generation


def _binding_fingerprint(binding: Mapping[str, object]) -> tuple[str, str]:
    raw = binding.get("fingerprint")
    if isinstance(raw, Mapping):
        primary = raw.get("value") or raw.get("xxh3_128")
        byte_count = raw.get("byte_count")
        guard = raw.get("xxh3_64_guard")
        if isinstance(primary, str) and primary:
            value = primary
            algorithm = str(raw.get("algorithm") or "xxh3-128")
            if byte_count is not None:
                value += f";bytes={byte_count}"
            if guard is not None:
                value += f";xxh3-64-guard={guard}"
            return value, algorithm
    identity = canonical_json({"binding": dict(binding)})
    return xxhash.xxh3_128_hexdigest(identity.encode("utf-8")), "xxh3-128"


def _input_contracts(
    inputs: Sequence[Mapping[str, object]],
    *,
    stage_id: str,
    processing_signature: str,
    observed_ns: int,
) -> tuple[InputBinding, ...]:
    contracts: list[InputBinding] = []
    for index, binding in enumerate(inputs):
        kind, identifier, generation = _binding_identity(binding)
        fingerprint, algorithm = _binding_fingerprint(binding)
        selected_revision = binding.get("revision_ref")
        if selected_revision is None:
            resource_digest = xxhash.xxh3_128_hexdigest(f"{kind}\0{identifier}".encode("utf-8"))
            revision_value = binding.get("revision_id")
            selected_revision = RevisionRef(
                resource_id=f"resource:semantic:{kind}:{resource_digest}",
                revision_id=(
                    f"revision:semantic:{kind}:{revision_value}"
                    if revision_value is not None
                    else f"revision:semantic:{kind}:fingerprint:{fingerprint[:128]}"
                ),
                producer="semantic.owner.snapshot",
                processing_signature=processing_signature,
                generation=generation,
                state=RevisionState.CURRENT,
                observed_at_utc=_utc_from_ns(observed_ns),
            )
        if not isinstance(selected_revision, RevisionRef):
            raise SemanticStateError("semantic input revision contract is invalid")
        materialization = binding.get("materialization_ref")
        if materialization is not None and not isinstance(materialization, MaterializationRef):
            raise SemanticStateError("semantic input materialization contract is invalid")
        contracts.append(
            InputBinding(
                name=str(binding.get("binding_name") or f"{kind}:{index}"),
                revision=selected_revision,
                fingerprint=fingerprint,
                fingerprint_algorithm=algorithm,
                materialization=materialization,
            )
        )
    return tuple(contracts)


def _output_contracts(
    outputs: Sequence[Mapping[str, object]],
    *,
    generation_id: int | None,
    schema_version: int,
) -> tuple[OutputBinding, ...]:
    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic output schema {schema_version!r} is unsupported"
        )
    contracts: list[OutputBinding] = []
    for index, binding in enumerate(outputs):
        kind, identifier, binding_generation = _binding_identity(binding)
        fingerprint, algorithm = _binding_fingerprint(binding)
        materialization = binding.get("materialization_ref")
        if materialization is None:
            materialization = MaterializationRef(
                owner="semantic",
                kind=kind,
                materialization_id=(
                    identifier
                    if identifier.startswith("materialization:")
                    else f"materialization:semantic:{kind}:{identifier}"
                ),
                schema_version=schema_version,
                generation=(generation_id if generation_id is not None else binding_generation),
            )
        if not isinstance(materialization, MaterializationRef):
            raise SemanticStateError("semantic output materialization contract is invalid")
        materialization = _semantic_materialization_for_schema(
            materialization,
            schema_version=schema_version,
        )
        contracts.append(
            OutputBinding(
                name=f"{kind}:{index}",
                materialization=materialization,
                fingerprint=fingerprint,
                fingerprint_algorithm=algorithm,
            )
        )
    return tuple(contracts)


def _sensitive_configuration_key(key: object) -> bool:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    normalized = snake_case.casefold().replace("-", "_").replace(".", "_")
    sensitive_tokens = {
        "authorization",
        "cookie",
        "credential",
        "password",
        "passwd",
        "secret",
        "token",
    }
    return any(
        normalized == suffix or normalized.endswith("_" + suffix)
        for suffix in _SENSITIVE_CONFIGURATION_SUFFIXES
    ) or bool(sensitive_tokens.intersection(normalized.split("_")))


def _sanitize_configuration_value(
    value: object,
    *,
    remaining_nodes: list[int],
) -> object:
    if remaining_nodes[0] <= 0:
        return "[truncated]"
    remaining_nodes[0] -= 1
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            key = str(raw_key)
            if _sensitive_configuration_key(key):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_configuration_value(
                    value[raw_key],
                    remaining_nodes=remaining_nodes,
                )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_configuration_value(item, remaining_nodes=remaining_nodes)
            for item in value
            if remaining_nodes[0] > 0
        ]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return "[unsupported]"


def _configuration_contract(
    effective_config: Mapping[str, object],
    *,
    stage_id: str,
) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    selected: list[tuple[str, str | int | float | bool | None]] = []
    remaining_nodes = [_MAX_CONFIGURATION_CAPTURE_NODES]
    allowed_keys = _EFFECTIVE_CONFIG_KEYS_BY_STAGE.get(stage_id, frozenset())
    for key, value in sorted(effective_config.items()):
        if key not in allowed_keys:
            continue
        if _sensitive_configuration_key(key):
            selected.append((key, "[redacted]"))
        elif isinstance(value, (str, int, float, bool, type(None))):
            selected.append(
                (
                    key,
                    (value[:_MAX_CONFIGURATION_VALUE_CHARS] if isinstance(value, str) else value),
                )
            )
        else:
            sanitized = _sanitize_configuration_value(
                value,
                remaining_nodes=remaining_nodes,
            )
            encoded = canonical_json({"value": sanitized})
            if len(encoded) > _MAX_CONFIGURATION_VALUE_CHARS:
                safe_fingerprint = xxhash.xxh3_128_hexdigest(encoded.encode("utf-8"))
                encoded = canonical_json(
                    {
                        "capture": "truncated",
                        "sanitized_xxh3_128": safe_fingerprint,
                    }
                )
            selected.append((key, encoded))
    return tuple(selected)


def _capability_failure(
    *,
    stage_id: str,
    error: Mapping[str, object] | None,
    status: str,
    provider_name: object,
) -> CapabilityFailure:
    raw_reason = str((error or {}).get("type") or status)
    reason_code = raw_reason if raw_reason in _SAFE_FAILURE_REASON_CODES else "provider_error"
    return CapabilityFailure(
        capability_id=stage_id,
        reason_code=reason_code,
        message=f"{stage_id} ended with {reason_code}",
        retryable=bool((error or {}).get("retryable", False)),
        provider=None if provider_name is None else str(provider_name),
        details=(
            ("reason_capture", "allowlisted" if reason_code == raw_reason else "redacted"),
            ("message_capture", "redacted"),
        ),
    )


def _receipt_material_contract(
    receipt: Mapping[str, object],
    *,
    normalize_semantic_schema_metadata: bool = False,
) -> str:
    contract: object = {
        "owner": receipt.get("owner"),
        "stage": receipt.get("stage"),
        "inputs": receipt.get("inputs"),
        "outputs": receipt.get("outputs"),
        "effective_configuration": receipt.get("effective_configuration"),
        "attempt": receipt.get("attempt"),
        "outcome": receipt.get("outcome"),
        "execution_mode": receipt.get("execution_mode"),
        "reproducibility": receipt.get("reproducibility"),
        "causation_id": receipt.get("causation_id"),
        "failure": receipt.get("failure"),
    }
    if normalize_semantic_schema_metadata:
        contract = _normalize_receipt_semantic_schema_metadata(contract)
    return canonical_json(contract)


def _semantic_receipt_sha256(receipt_json: str) -> str:
    """Hash the exact stored UTF-8 receipt body, including canonical bytes."""

    if not isinstance(receipt_json, str):
        raise SemanticStateError("semantic receipt body must be text")
    return "sha256:" + hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()


def _semantic_outbox_v1_payload(
    *,
    receipt_id: int,
    receipt_key: str,
    event_kind: str,
    aggregate_kind: str,
    aggregate_id: str,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Build the historical embedded envelope without changing its shape."""

    return {
        "schema": _SEMANTIC_DERIVATION_EVENT_V1,
        "receipt_id": receipt_id,
        "receipt_key": receipt_key,
        "event_kind": event_kind,
        "aggregate_kind": aggregate_kind,
        "aggregate_id": aggregate_id,
        "receipt": dict(receipt),
    }


def _semantic_outbox_v2_payload(
    *,
    receipt_id: int,
    receipt_key: str,
    event_kind: str,
    aggregate_kind: str,
    aggregate_id: str,
    committed_ns: int,
    receipt_json: str,
) -> dict[str, object]:
    """Build the compact referenced envelope for schema-9/10 writes."""

    return {
        "schema": _SEMANTIC_DERIVATION_EVENT_V2,
        "owner": "semantic",
        "receipt_ref": {
            "owner": "semantic",
            "receipt_id": receipt_id,
            "receipt_key": receipt_key,
            "receipt_sha256": _semantic_receipt_sha256(receipt_json),
            "contract": WORK_RECEIPT_CONTRACT,
        },
        "event_kind": event_kind,
        "aggregate_kind": aggregate_kind,
        "aggregate_id": aggregate_id,
        "committed_ns": committed_ns,
    }


def _json_object_no_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticStateError("semantic v2 outbox payload has duplicate keys")
        result[key] = value
    return result


def _strict_semantic_outbox_json(
    payload_raw: str,
    *,
    label: str,
) -> dict[str, object]:
    try:
        payload = json.loads(
            payload_raw,
            object_pairs_hook=_json_object_no_duplicate_pairs,
        )
    except SemanticStateError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise SemanticStateError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SemanticStateError(f"{label} is not a JSON object")
    try:
        canonical = canonical_json(payload)
    except (RecursionError, TypeError, ValueError) as exc:
        raise SemanticStateError(f"{label} is not canonical JSON") from exc
    if canonical != payload_raw:
        raise SemanticStateError(f"{label} is not canonical JSON")
    return payload


def _semantic_outbox_payload_schema(payload_raw: str) -> object:
    """Read only the wire discriminator for page sizing before full validation."""

    try:
        payload = json.loads(payload_raw)
    except (RecursionError, TypeError, ValueError):
        return None
    return payload.get("schema") if isinstance(payload, dict) else None


def _semantic_outbox_v1_payload_from_row(
    row: sqlite3.Row,
    *,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    raw_receipt_id = row["outbox_receipt_id"]
    receipt_id = (
        int(raw_receipt_id)
        if isinstance(raw_receipt_id, int) and not isinstance(raw_receipt_id, bool)
        else 0
    )
    return _semantic_outbox_v1_payload(
        receipt_id=receipt_id,
        receipt_key="" if row["receipt_key"] is None else str(row["receipt_key"]),
        event_kind="" if row["event_kind"] is None else str(row["event_kind"]),
        aggregate_kind=("" if row["aggregate_kind"] is None else str(row["aggregate_kind"])),
        aggregate_id="" if row["aggregate_id"] is None else str(row["aggregate_id"]),
        receipt=receipt,
    )


def _semantic_outbox_row_cost(
    row: sqlite3.Row,
    *,
    payload_raw: str,
    receipt_raw: str,
) -> int:
    """Charge v1 logical hydration bytes, not only the compact v2 wire."""

    receipt_bytes = len(receipt_raw.encode("utf-8"))
    if _semantic_outbox_payload_schema(payload_raw) == _SEMANTIC_DERIVATION_EVENT_V1:
        return len(payload_raw.encode("utf-8")) + receipt_bytes
    placeholder = _semantic_outbox_v1_payload_from_row(row, receipt={})
    placeholder_bytes = len(canonical_json(placeholder).encode("utf-8"))
    # ``{}`` is the exact canonical JSON for the receipt placeholder.  A valid
    # receipt_json body can therefore be substituted by byte length without
    # parsing/validating an event that may later fall outside this page.
    # Unknown or malformed compact wire cannot shrink this logical cost and
    # pull an otherwise excluded corrupt event into the current page.  Its
    # protocol error is reported only if the selected page reaches that row.
    logical_v1_bytes = placeholder_bytes - 2 + receipt_bytes
    return max(logical_v1_bytes, len(payload_raw.encode("utf-8"))) + receipt_bytes


def _validate_semantic_outbox_v1_payload(
    payload: Mapping[str, object],
    *,
    row: sqlite3.Row,
    receipt: Mapping[str, object],
) -> None:
    """Preserve the existing v1 checks; do not tighten historical wire data."""

    if (
        payload.get("schema") != _SEMANTIC_DERIVATION_EVENT_V1
        or row["outbox_receipt_id"] is None
        or row["receipt_id"] is None
        or int(row["outbox_receipt_id"]) != int(row["receipt_id"])
        or payload.get("receipt_id") != int(row["receipt_id"])
        or payload.get("receipt_key") != str(row["receipt_key"])
        or payload.get("event_kind") != str(row["event_kind"])
        or payload.get("aggregate_kind") != str(row["aggregate_kind"])
        or payload.get("aggregate_id") != str(row["aggregate_id"])
        or payload.get("receipt") != receipt
    ):
        raise SemanticStateError("semantic v1 outbox receipt does not match its owner receipt")


def _validate_semantic_outbox_commit_timestamp(row: sqlite3.Row) -> None:
    """Require the event and canonical receipt to share their commit fact."""

    if row["event_committed_ns"] is None or row["committed_ns"] is None:
        raise SemanticStateError("semantic derivation outbox commit timestamp is missing")
    if int(row["event_committed_ns"]) != int(row["committed_ns"]):
        raise SemanticStateError("semantic derivation outbox commit timestamp is inconsistent")


def _validate_semantic_outbox_v2_payload(
    payload_raw: str,
    *,
    row: sqlite3.Row,
    receipt_raw: str,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Strictly validate a compact v2 envelope against its joined receipt row."""

    payload = _strict_semantic_outbox_json(
        payload_raw,
        label="semantic v2 derivation outbox payload",
    )
    expected_payload_keys = frozenset(
        {
            "schema",
            "owner",
            "receipt_ref",
            "event_kind",
            "aggregate_kind",
            "aggregate_id",
            "committed_ns",
        }
    )
    if set(payload) != expected_payload_keys:
        raise SemanticStateError("semantic v2 outbox payload has unknown or missing fields")
    receipt_ref = payload["receipt_ref"]
    if not isinstance(receipt_ref, dict) or set(receipt_ref) != {
        "owner",
        "receipt_id",
        "receipt_key",
        "receipt_sha256",
        "contract",
    }:
        raise SemanticStateError("semantic v2 outbox receipt_ref has unknown or missing fields")
    receipt_id = receipt_ref["receipt_id"]
    if not isinstance(receipt_id, int) or isinstance(receipt_id, bool) or receipt_id < 1:
        raise SemanticStateError("semantic v2 outbox receipt_ref receipt_id is invalid")
    receipt_key = receipt_ref["receipt_key"]
    receipt_digest = receipt_ref["receipt_sha256"]
    if not isinstance(receipt_key, str) or not receipt_key.strip():
        raise SemanticStateError("semantic v2 outbox receipt_ref receipt_key is invalid")
    if not isinstance(receipt_digest, str) or re.fullmatch(
        r"sha256:[0-9a-f]{64}", receipt_digest
    ) is None:
        raise SemanticStateError("semantic v2 outbox receipt_ref receipt_sha256 is invalid")
    committed_ns = payload["committed_ns"]
    if not isinstance(committed_ns, int) or isinstance(committed_ns, bool) or committed_ns < 0:
        raise SemanticStateError("semantic v2 outbox committed_ns is invalid")
    if (
        payload["schema"] != _SEMANTIC_DERIVATION_EVENT_V2
        or payload["owner"] != "semantic"
        or receipt_ref["owner"] != "semantic"
        or receipt_ref["contract"] != WORK_RECEIPT_CONTRACT
        or payload["event_kind"] != str(row["event_kind"])
        or payload["aggregate_kind"] != str(row["aggregate_kind"])
        or payload["aggregate_id"] != str(row["aggregate_id"])
        or committed_ns != int(row["event_committed_ns"])
        or row["outbox_receipt_id"] is None
        or row["receipt_id"] is None
        or receipt_id != int(row["outbox_receipt_id"])
        or receipt_id != int(row["receipt_id"])
        or receipt_key != str(row["receipt_key"])
        or receipt_key != str(receipt["receipt_id"])
        or receipt_digest != _semantic_receipt_sha256(receipt_raw)
    ):
        raise SemanticStateError("semantic v2 outbox receipt_ref does not match its owner receipt")
    return payload


def _validate_semantic_outbox_event_row(
    row: sqlite3.Row,
    *,
    receipt_raw: str,
    receipt: Mapping[str, object],
    requested_event_kind: str,
    requested_aggregate_kind: str,
    requested_aggregate_id: str,
    owner_schema_version: int,
    candidate_payload_raw: str | None = None,
) -> None:
    """Validate a stored event while allowing its original commit timestamp."""

    if row["receipt_id"] is None or row["outbox_receipt_id"] is None:
        raise SemanticStateError("semantic derivation outbox receipt is missing")
    _validate_semantic_outbox_commit_timestamp(row)
    if (
        str(row["event_kind"]) != requested_event_kind
        or str(row["aggregate_kind"]) != requested_aggregate_kind
        or str(row["aggregate_id"]) != requested_aggregate_id
    ):
        raise SemanticStateError("semantic derivation outbox event is bound to different facts")
    payload_raw = str(row["payload_json"])
    if candidate_payload_raw is not None and payload_raw != candidate_payload_raw:
        raise SemanticStateError("semantic derivation outbox payload is not idempotent")
    payload_schema = _semantic_outbox_payload_schema(payload_raw)
    if payload_schema == _SEMANTIC_DERIVATION_EVENT_V1:
        payload = _json_object(payload_raw, label="semantic derivation outbox payload")
        _validate_semantic_outbox_v1_payload(payload, row=row, receipt=receipt)
    elif payload_schema == _SEMANTIC_DERIVATION_EVENT_V2:
        if owner_schema_version not in {9, 10}:
            raise SemanticStateError("semantic v2 outbox payload requires schema 9 or 10")
        _validate_semantic_outbox_v2_payload(
            payload_raw,
            row=row,
            receipt_raw=receipt_raw,
            receipt=receipt,
        )
    else:
        raise SemanticStateError("semantic derivation outbox payload schema is unsupported")


def _record_work_receipt(
    connection: sqlite3.Connection,
    *,
    receipt_key: str,
    stage_id: str,
    stage_version: str,
    processing_signature: str,
    status: str,
    execution_mode: str,
    reproducibility_class: str,
    entity_kind: str,
    entity_id: str,
    inputs: Sequence[Mapping[str, object]],
    outputs: Sequence[Mapping[str, object]],
    effective_config: Mapping[str, object],
    provider: Mapping[str, object] | None,
    item_revision_id: int | None,
    chunk_revision_id: int | None,
    generation_id: int | None,
    model_signature: str | None,
    payload_id: int | None,
    job_id: int | None,
    attempt: int | None,
    started_ns: int | None,
    finished_ns: int | None,
    error: Mapping[str, object] | None,
    causation_receipt_id: int | None,
    event_kind: str,
    aggregate_kind: str,
    aggregate_id: str,
    committed_ns: int,
) -> int:
    """Persist a receipt and its projection outbox fact in one transaction."""

    schema_version = _require_current_receipt_schema(connection)
    selected_finished_ns = committed_ns if finished_ns is None else finished_ns
    selected_started_ns = selected_finished_ns if started_ns is None else started_ns
    duration_ns = max(0, selected_finished_ns - selected_started_ns)
    selected_attempt = 1 if attempt is None or attempt < 1 else attempt
    causation_id = None
    if causation_receipt_id is not None:
        cause = connection.execute(
            "SELECT receipt_key FROM semantic_work_receipts WHERE receipt_id=?",
            (causation_receipt_id,),
        ).fetchone()
        if cause is not None:
            causation_id = str(cause[0])
    provider_name = None if provider is None else provider.get("provider")
    provider_version = None if provider is None else provider.get("provider_version")
    model_name = None if provider is None else provider.get("model_id")
    model_version = None if provider is None else provider.get("model_version")
    failure = None
    if status != WorkOutcome.SUCCEEDED.value:
        failure = _capability_failure(
            stage_id=stage_id,
            error=error,
            status=status,
            provider_name=provider_name,
        )
    work_receipt = WorkReceipt(
        receipt_id=receipt_key,
        owner="semantic",
        stage=StageDescriptor(
            stage_id=stage_id,
            stage_version=stage_version,
            processing_signature=processing_signature,
            provider=None if provider_name is None else str(provider_name),
            provider_version=(None if provider_version is None else str(provider_version)),
            model=None if model_name is None else str(model_name),
            model_version=None if model_version is None else str(model_version),
        ),
        inputs=_input_contracts(
            inputs,
            stage_id=stage_id,
            processing_signature=processing_signature,
            observed_ns=selected_started_ns,
        ),
        outputs=_output_contracts(
            outputs,
            generation_id=generation_id,
            schema_version=schema_version,
        ),
        effective_configuration=_configuration_contract(
            effective_config,
            stage_id=stage_id,
        ),
        runtime=(
            ("platform", platform.platform()),
            ("python", platform.python_version()),
            ("semantic_schema", str(schema_version)),
            ("timing_basis", "owner_observation"),
        ),
        started_at_utc=_utc_from_ns(selected_started_ns),
        finished_at_utc=_utc_from_ns(selected_finished_ns),
        duration_ns=duration_ns,
        attempt=selected_attempt,
        outcome=WorkOutcome(status),
        execution_mode=WorkExecutionMode(execution_mode),
        reproducibility=ReproducibilityClass(reproducibility_class),
        run_id=(
            f"semantic-generation:{generation_id}"
            if generation_id is not None
            else f"semantic-run:{receipt_key}"
        ),
        correlation_id=(
            f"semantic-generation:{generation_id}"
            if generation_id is not None
            else f"semantic-correlation:{receipt_key}"
        ),
        causation_id=causation_id,
        failure=failure,
    )
    _validate_receipt_semantic_locators(work_receipt)
    receipt_json = work_receipt.to_json()
    cursor = connection.execute(
        """INSERT INTO semantic_work_receipts(
            receipt_key,contract_version,stage_id,stage_version,
            processing_signature,status,execution_mode,reproducibility_class,
            entity_kind,entity_id,item_revision_id,chunk_revision_id,
            generation_id,model_signature,payload_id,job_id,attempt,started_ns,
            finished_ns,duration_ns,receipt_json,committed_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(receipt_key) DO NOTHING""",
        (
            receipt_key,
            WORK_RECEIPT_CONTRACT,
            stage_id,
            stage_version,
            processing_signature,
            status,
            execution_mode,
            reproducibility_class,
            entity_kind,
            entity_id,
            item_revision_id,
            chunk_revision_id,
            generation_id,
            model_signature,
            payload_id,
            job_id,
            selected_attempt,
            selected_started_ns,
            selected_finished_ns,
            duration_ns,
            receipt_json,
            committed_ns,
        ),
    )
    if cursor.lastrowid is not None and cursor.rowcount == 1:
        receipt_id = int(cursor.lastrowid)
        stored_receipt = work_receipt.to_dict()
        stored_receipt_json = receipt_json
    else:
        existing = connection.execute(
            "SELECT * FROM semantic_work_receipts WHERE receipt_key=?",
            (receipt_key,),
        ).fetchone()
        expected = (
            (stage_id, stage_version, processing_signature, status, execution_mode,
             reproducibility_class, entity_kind, entity_id, item_revision_id,
             chunk_revision_id, generation_id, model_signature, payload_id, job_id,
             selected_attempt)
        )
        observed = (
            None
            if existing is None
            else (
                str(existing["stage_id"]),
                str(existing["stage_version"]),
                str(existing["processing_signature"]),
                str(existing["status"]),
                str(existing["execution_mode"]),
                str(existing["reproducibility_class"]),
                str(existing["entity_kind"]),
                str(existing["entity_id"]),
                existing["item_revision_id"],
                existing["chunk_revision_id"],
                existing["generation_id"],
                existing["model_signature"],
                existing["payload_id"],
                existing["job_id"],
                existing["attempt"],
            )
        )
        if existing is None or observed != expected:
            raise SemanticStateError("semantic receipt key is bound to different work")
        receipt_id = int(existing["receipt_id"])
        stored_receipt_json = str(existing["receipt_json"])
        try:
            stored_work_receipt = _validated_semantic_receipt_row(
                existing,
                owner_schema_version=schema_version,
            )
            stored_schema_version = _receipt_schema_version(stored_work_receipt)
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticStateError(
                "semantic receipt key is bound to an invalid causal receipt"
            ) from exc
        stored_receipt = stored_work_receipt.to_dict()
        if stored_schema_version != schema_version and (
            stored_schema_version,
            schema_version,
        ) not in _FORWARD_RECEIPT_SCHEMA_TRANSITIONS:
            raise SemanticStateError("semantic receipt key is bound to different causal facts")
        if _receipt_material_contract(stored_receipt) != _receipt_material_contract(
            work_receipt.to_dict()
        ):
            # Only an explicitly forward owner-schema transition may compare
            # schema metadata as non-identity.  Same-version conflicts,
            # backward transitions, and future/malformed metadata remain hard
            # conflicts instead of falling through this compatibility path.
            if (
                stored_schema_version,
                schema_version,
            ) not in _FORWARD_RECEIPT_SCHEMA_TRANSITIONS:
                raise SemanticStateError("semantic receipt key is bound to different causal facts")
            if _receipt_material_contract(
                stored_receipt,
                normalize_semantic_schema_metadata=True,
            ) != _receipt_material_contract(
                work_receipt.to_dict(),
                normalize_semantic_schema_metadata=True,
            ):
                raise SemanticStateError("semantic receipt key is bound to different causal facts")
    candidate_payload = (
        _semantic_outbox_v2_payload(
            receipt_id=receipt_id,
            receipt_key=receipt_key,
            event_kind=event_kind,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            committed_ns=committed_ns,
            receipt_json=stored_receipt_json,
        )
        if schema_version in {9, 10}
        else _semantic_outbox_v1_payload(
            receipt_id=receipt_id,
            receipt_key=receipt_key,
            event_kind=event_kind,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            receipt=stored_receipt,
        )
    )
    candidate_payload_raw = canonical_json(candidate_payload)
    # Keep the original conflict/allocator semantics: even an ignored insert
    # advances SQLite's AUTOINCREMENT sequence.  A pre-read would change the
    # IDs of later events after replay, as well as add a query per new receipt.
    insert_cursor = connection.execute(
        """INSERT INTO semantic_derivation_outbox(
            receipt_id,event_kind,aggregate_kind,aggregate_id,payload_json,
            committed_ns)
        VALUES(?,?,?,?,?,?) ON CONFLICT(receipt_id) DO NOTHING""",
        (
            receipt_id,
            event_kind,
            aggregate_kind,
            aggregate_id,
            candidate_payload_raw,
            committed_ns,
        ),
    )
    inserted_event = insert_cursor.rowcount == 1
    stored_event = connection.execute(
        """SELECT event.event_id,event.receipt_id AS outbox_receipt_id,
            event.event_kind,event.aggregate_kind,event.aggregate_id,
            event.payload_json,event.committed_ns AS event_committed_ns,
            receipt.*
        FROM semantic_derivation_outbox event
        JOIN semantic_work_receipts receipt ON receipt.receipt_id=event.receipt_id
        WHERE event.receipt_id=?""",
        (receipt_id,),
    ).fetchone()
    if stored_event is None:
        raise SemanticStateError("semantic derivation outbox row disappeared")
    _validate_semantic_outbox_event_row(
        stored_event,
        receipt_raw=stored_receipt_json,
        receipt=stored_receipt,
        requested_event_kind=event_kind,
        requested_aggregate_kind=aggregate_kind,
        requested_aggregate_id=aggregate_id,
        owner_schema_version=schema_version,
        candidate_payload_raw=(candidate_payload_raw if inserted_event else None),
    )
    return receipt_id


def _validated_semantic_receipt_row(
    row: sqlite3.Row,
    *,
    owner_schema_version: int | None = None,
) -> WorkReceipt:
    receipt_id = int(row["receipt_id"])
    try:
        receipt = WorkReceipt.from_json(str(row["receipt_json"]))
        _validate_receipt_semantic_locators(receipt)
        receipt_schema_version = _receipt_schema_version(receipt)
        if (
            owner_schema_version is not None
            and receipt_schema_version > owner_schema_version
        ):
            raise ValueError(
                "semantic WorkReceipt is newer than its owner schema"
            )
        started_ns = int(row["started_ns"])
        finished_ns = int(row["finished_ns"])
        normalized = (
            str(row["contract_version"]) == WORK_RECEIPT_CONTRACT
            and receipt.to_json() == str(row["receipt_json"])
            and receipt.receipt_id == str(row["receipt_key"])
            and receipt.owner == "semantic"
            and receipt.stage.stage_id == str(row["stage_id"])
            and receipt.stage.stage_version == str(row["stage_version"])
            and receipt.stage.processing_signature == str(row["processing_signature"])
            and receipt.outcome.value == str(row["status"])
            and receipt.execution_mode.value == str(row["execution_mode"])
            and receipt.reproducibility.value == str(row["reproducibility_class"])
            and receipt.started_at_utc == _utc_from_ns(started_ns)
            and receipt.finished_at_utc == _utc_from_ns(finished_ns)
            and receipt.duration_ns == int(row["duration_ns"])
            and receipt.attempt == int(row["attempt"])
            and receipt.run_id
            == (
                f"semantic-generation:{int(row['generation_id'])}"
                if row["generation_id"] is not None
                else f"semantic-run:{row['receipt_key']}"
            )
            and receipt.correlation_id
            == (
                f"semantic-generation:{int(row['generation_id'])}"
                if row["generation_id"] is not None
                else f"semantic-correlation:{row['receipt_key']}"
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"semantic WorkReceipt is not canonical: {receipt_id}") from exc
    if not normalized:
        raise ValueError(f"semantic WorkReceipt contradicts normalized facts: {receipt_id}")
    return receipt


def _validated_semantic_receipts(
    connection: sqlite3.Connection,
    receipt_ids: Iterable[int],
) -> dict[int, tuple[WorkReceipt, sqlite3.Row]]:
    owner_schema_version = _require_current_receipt_schema(connection)
    unique_ids = tuple(dict.fromkeys(int(receipt_id) for receipt_id in receipt_ids))
    validated: dict[int, tuple[WorkReceipt, sqlite3.Row]] = {}
    for offset in range(0, len(unique_ids), 250):
        batch = unique_ids[offset : offset + 250]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT * FROM semantic_work_receipts
            WHERE receipt_id IN ({placeholders}) ORDER BY receipt_id""",
            batch,
        ).fetchall()
        rows_by_id = {int(row["receipt_id"]): row for row in rows}
        missing = set(batch).difference(rows_by_id)
        if missing:
            raise ValueError(f"semantic WorkReceipt is missing: {min(missing)}")
        for receipt_id in batch:
            row = rows_by_id[receipt_id]
            validated[receipt_id] = (
                _validated_semantic_receipt_row(
                    row,
                    owner_schema_version=owner_schema_version,
                ),
                row,
            )
    return validated


def _validated_semantic_receipt_by_key(
    connection: sqlite3.Connection,
    receipt_key: str,
) -> tuple[WorkReceipt, sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM semantic_work_receipts WHERE receipt_key=? LIMIT 2",
        (receipt_key,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"semantic causation receipt is missing or ambiguous: {receipt_key}")
    return (
        _validated_semantic_receipt_row(
            rows[0],
            owner_schema_version=_require_current_receipt_schema(connection),
        ),
        rows[0],
    )


def _output_matches_input(output: OutputBinding, input_binding: InputBinding) -> bool:
    return (
        output.materialization == input_binding.materialization
        and output.materialization.revision == input_binding.revision
        and output.fingerprint == input_binding.fingerprint
        and output.fingerprint_algorithm == input_binding.fingerprint_algorithm
    )


def _input_mapping_from_output(
    output: OutputBinding,
    *,
    name: str,
) -> dict[str, object]:
    revision = output.materialization.revision
    if revision is None:
        raise SemanticStateError("semantic causal output has no exact revision binding")
    return {
        "kind": output.materialization.kind,
        "materialization_id": output.materialization.materialization_id,
        "binding_name": name,
        "revision_ref": revision,
        "materialization_ref": output.materialization,
        "fingerprint": {
            "algorithm": output.fingerprint_algorithm,
            "value": output.fingerprint,
        },
    }


def _input_mapping_from_binding(
    binding: InputBinding,
    *,
    name: str,
) -> dict[str, object]:
    """Re-use an exact stored input without rewriting its owner schema."""

    materialization = binding.materialization
    if materialization is None:
        raise SemanticStateError("semantic stored input has no exact materialization")
    return {
        "kind": materialization.kind,
        "binding_name": name,
        "revision_ref": binding.revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": binding.fingerprint_algorithm,
            "value": binding.fingerprint,
        },
    }


def _materialization_numeric_identifier(
    materialization: MaterializationRef,
    *,
    prefix: str,
) -> int:
    identifier = materialization.materialization_id
    if not identifier.startswith(prefix):
        raise ValueError("semantic materialization identifier is not canonical")
    raw = identifier.removeprefix(prefix)
    if not raw.isdecimal() or int(raw) < 1:
        raise ValueError("semantic materialization identifier is not canonical")
    return int(raw)


def _validate_legacy_payload_attestation_receipt(
    connection: sqlite3.Connection,
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
) -> None:
    if receipt_row["payload_id"] is None:
        raise ValueError("semantic legacy payload attestation lacks a payload")
    receipt_schema_version = _receipt_schema_version(receipt)
    payload_id = int(receipt_row["payload_id"])
    payload, _payload_provider = _payload_binding(
        connection,
        payload_id,
        schema_version=receipt_schema_version,
    )
    expected_inputs = _input_contracts(
        (_renamed_binding(payload, "legacy_vector_payload"),),
        stage_id=SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE,
        processing_signature="semantic-legacy-payload-attestation-v1",
        observed_ns=int(receipt_row["started_ns"]),
    )
    expected_outputs = _output_contracts(
        (
            _legacy_payload_attestation_binding(
                payload_id=payload_id,
                payload_binding=payload,
                now_ns=int(receipt_row["finished_ns"]),
                schema_version=receipt_schema_version,
            ),
        ),
        generation_id=None,
        schema_version=receipt_schema_version,
    )
    if (
        not bool(
            connection.execute(
                "SELECT legacy_before_receipts FROM vector_payloads WHERE payload_id=?",
                (payload_id,),
            ).fetchone()[0]
        )
        or receipt.stage.stage_version != "semantic-legacy-payload-attestation-v1"
        or receipt.stage.processing_signature != "semantic-legacy-payload-attestation-v1"
        or receipt.stage.provider != "neocortex"
        or receipt.stage.model is not None
        or receipt.stage.model_version is not None
        or receipt.inputs != expected_inputs
        or receipt.outputs != expected_outputs
        or dict(receipt.effective_configuration) != {"payload_id": payload_id}
        or receipt.causation_id is not None
    ):
        raise ValueError("semantic legacy payload attestation contradicts physical facts")


def _embedding_member_row_for_receipt(
    connection: sqlite3.Connection,
    receipt_row: sqlite3.Row,
) -> sqlite3.Row:
    if receipt_row["generation_id"] is None or receipt_row["payload_id"] is None:
        raise ValueError("semantic embedding receipt lacks normalized member facts")
    normalized_entity_kind = str(receipt_row["entity_kind"])
    member_entity_kind = {
        "text_embedding": "text_chunk",
        "image_embedding": "image_item",
    }.get(normalized_entity_kind)
    if member_entity_kind is None:
        raise ValueError("semantic embedding receipt entity kind is invalid")
    rows = connection.execute(
        """SELECT member.*,g.processing_signature,
            payload.dimensions,payload.vector_dtype,payload.original_norm,
            payload.content_xxh3_128 AS payload_xxh3_128,
            payload.content_bytes AS payload_bytes,
            payload.content_xxh3_64_guard AS payload_xxh3_64_guard
        FROM embedding_generation_members member
        JOIN embedding_generations g ON g.generation_id=member.generation_id
        JOIN vector_payloads payload ON payload.payload_id=member.payload_id
        WHERE member.generation_id=? AND member.entity_kind=? AND member.entity_id=?
          AND member.payload_id=? LIMIT 2""",
        (
            int(receipt_row["generation_id"]),
            member_entity_kind,
            str(receipt_row["entity_id"]),
            int(receipt_row["payload_id"]),
        ),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("semantic embedding receipt member is missing or ambiguous")
    return rows[0]


def _validate_embedding_causation_receipt(
    connection: sqlite3.Connection,
    *,
    causation_id: str,
    expected_input: InputBinding,
    seen: set[str],
) -> tuple[WorkReceipt, sqlite3.Row]:
    if causation_id in seen:
        raise ValueError("semantic receipt causation contains a cycle")
    seen.add(causation_id)
    cause, cause_row = _validated_semantic_receipt_by_key(
        connection,
        causation_id,
    )
    if not any(_output_matches_input(output, expected_input) for output in cause.outputs):
        raise ValueError("semantic causation receipt does not produce the reused input")
    expected_materialization = expected_input.materialization
    if expected_materialization is None:
        raise ValueError("semantic reused input lacks an exact materialization")
    if cause.stage.stage_id == SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE:
        _validate_legacy_payload_attestation_receipt(connection, cause, cause_row)
    elif cause.stage.stage_id == SEMANTIC_EMBEDDING_STAGE:
        if expected_materialization.kind == "semantic_vector_payload":
            _validate_embedding_payload_producer_receipt(
                connection,
                cause,
                cause_row,
            )
        else:
            cause_member = _embedding_member_row_for_receipt(connection, cause_row)
            _validate_embedding_receipt_contract(
                connection,
                cause,
                cause_row,
                cause_member,
                seen=seen,
            )
    elif cause.stage.stage_id == SEMANTIC_EMBEDDING_CLONE_STAGE:
        if (
            cause.outcome is not WorkOutcome.SUCCEEDED
            or cause.execution_mode is not WorkExecutionMode.EXECUTED
        ):
            raise ValueError("semantic clone causation receipt is not successful")
    else:
        raise ValueError("semantic reused input has an invalid causation stage")
    return cause, cause_row


def _validate_embedding_payload_producer_receipt(
    connection: sqlite3.Connection,
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
) -> None:
    if receipt_row["payload_id"] is None or receipt_row["generation_id"] is None:
        raise SemanticStateError("semantic reused vector payload producer lacks normalized facts")
    receipt_schema_version = _receipt_schema_version(receipt)
    payload_id = int(receipt_row["payload_id"])
    generation = connection.execute(
        """SELECT processing_signature,provenance_json FROM embedding_generations
        WHERE generation_id=?""",
        (int(receipt_row["generation_id"]),),
    ).fetchone()
    if generation is None:
        raise SemanticStateError("semantic reused vector payload producer generation disappeared")
    payload, provider = _payload_binding(
        connection,
        payload_id,
        schema_version=receipt_schema_version,
    )
    expected_payload = _output_contracts(
        (payload,),
        generation_id=int(receipt_row["generation_id"]),
        schema_version=receipt_schema_version,
    )[0]
    item_revision_id = (
        None if receipt_row["item_revision_id"] is None else int(receipt_row["item_revision_id"])
    )
    chunk_revision_id = (
        None if receipt_row["chunk_revision_id"] is None else int(receipt_row["chunk_revision_id"])
    )
    raw_inputs: list[Mapping[str, object]] = []
    if item_revision_id is not None:
        raw_inputs.append(
            _item_revision_binding(
                connection,
                item_revision_id,
                schema_version=receipt_schema_version,
            )
        )
    if chunk_revision_id is not None:
        raw_inputs.append(
            _chunk_revision_binding(
                connection,
                chunk_revision_id,
                schema_version=receipt_schema_version,
            )
        )
    expected_inputs = _input_contracts(
        tuple(raw_inputs),
        stage_id=SEMANTIC_EMBEDDING_STAGE,
        processing_signature=str(generation["processing_signature"]),
        observed_ns=int(receipt_row["started_ns"]),
    )
    expected_config = _configuration_contract(
        _json_object(
            generation["provenance_json"],
            label="embedding generation provenance",
        ),
        stage_id=SEMANTIC_EMBEDDING_STAGE,
    )
    # A chunk revision can be observed by more than one refresh/generation.
    # The embedding receipt records the materialization that was current at
    # its own execution time, while a later refresh may append another
    # derivation for the same physical chunk revision.  Validate against the
    # complete immutable set of materialization receipts rather than only the
    # latest row, otherwise a later staging pass invalidates an older,
    # otherwise canonical producer receipt and blocks recovery/replay.
    expected_causations: frozenset[str] = frozenset()
    if chunk_revision_id is not None and item_revision_id is not None:
        cause = connection.execute(
            """SELECT receipt.receipt_key
            FROM semantic_chunk_derivations derivation
            JOIN semantic_work_receipts receipt
              ON receipt.receipt_id=derivation.materialization_receipt_id
            WHERE derivation.chunk_revision_id=? AND derivation.item_revision_id=?
            ORDER BY derivation.derivation_id""",
            (chunk_revision_id, item_revision_id),
        ).fetchall()
        expected_causations = frozenset(str(row["receipt_key"]) for row in cause)
    if (
        receipt.stage.stage_id != SEMANTIC_EMBEDDING_STAGE
        or receipt.stage.stage_version != "semantic-embedding-v1"
        or receipt.stage.processing_signature != str(generation["processing_signature"])
        or receipt.outcome is not WorkOutcome.SUCCEEDED
        or receipt.execution_mode is not WorkExecutionMode.EXECUTED
        or receipt.stage.provider != str(provider["provider"])
        or receipt.stage.model != str(provider["model_id"])
        or receipt.stage.model_version != str(provider["model_version"])
        or str(receipt_row["model_signature"]) != str(provider["model_signature"])
        or int(receipt_row["payload_id"]) != payload_id
        or receipt.inputs != expected_inputs
        or receipt.effective_configuration != expected_config
        or (
            receipt.causation_id is not None
            and receipt.causation_id not in expected_causations
        )
        or (receipt.causation_id is None and bool(expected_causations))
        or expected_payload not in receipt.outputs
    ):
        raise SemanticStateError("semantic reused vector payload producer is not canonical")


def _validate_embedding_receipt_contract(
    connection: sqlite3.Connection,
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
    member: sqlite3.Row,
    *,
    seen: set[str] | None = None,
) -> None:
    selected_seen = set() if seen is None else seen
    selected_seen.add(receipt.receipt_id)
    receipt_schema_version = _receipt_schema_version(receipt)
    generation_id = int(member["generation_id"])
    item_revision_id = int(member["item_revision_id"])
    chunk_revision_id = (
        None if member["chunk_revision_id"] is None else int(member["chunk_revision_id"])
    )
    payload_id = int(member["payload_id"])
    payload, provider = _payload_binding(
        connection,
        payload_id,
        schema_version=receipt_schema_version,
    )
    member_binding = _embedding_member_binding_from_row(
        member,
        schema_version=receipt_schema_version,
    )
    raw_inputs: list[Mapping[str, object]] = [
        _item_revision_binding(
            connection,
            item_revision_id,
            schema_version=receipt_schema_version,
        )
    ]
    if chunk_revision_id is not None:
        raw_inputs.append(
            _chunk_revision_binding(
                connection,
                chunk_revision_id,
                schema_version=receipt_schema_version,
            )
        )
    mode = receipt.execution_mode
    cause_expected_input: InputBinding | None = None
    reused_payload_input: InputBinding | None = None
    if mode in {WorkExecutionMode.CACHE_HIT, WorkExecutionMode.REPLAY}:
        reused_payloads = tuple(
            binding for binding in receipt.inputs if binding.name == "reused_vector_payload"
        )
        if len(reused_payloads) != 1 or reused_payloads[0].materialization is None:
            raise ValueError("semantic reused embedding lacks one exact payload input")
        reused_payload_input = reused_payloads[0]
        raw_inputs.append(
            _input_mapping_from_binding(
                reused_payload_input,
                name="reused_vector_payload",
            )
        )
    if mode is WorkExecutionMode.REPLAY:
        source_inputs = tuple(
            binding for binding in receipt.inputs if binding.name == "source_embedding_member"
        )
        if len(source_inputs) != 1 or source_inputs[0].materialization is None:
            raise ValueError("semantic replay receipt lacks one source member")
        source_input = source_inputs[0]
        raw_inputs.append(
            {
                "kind": "semantic_embedding_member",
                "binding_name": "source_embedding_member",
                "revision_ref": source_input.revision,
                "materialization_ref": source_input.materialization,
                "fingerprint": {
                    "algorithm": source_input.fingerprint_algorithm,
                    "value": source_input.fingerprint,
                },
            }
        )
        cause_expected_input = source_input
    elif mode is WorkExecutionMode.CACHE_HIT:
        if receipt.causation_id is None:
            raise ValueError("semantic cached embedding lacks exact causation")
        cause_receipt, cause_row = _validated_semantic_receipt_by_key(
            connection,
            receipt.causation_id,
        )
        if cause_receipt.stage.stage_id == SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE:
            _validate_legacy_payload_attestation_receipt(
                connection,
                cause_receipt,
                cause_row,
            )
            if len(cause_receipt.outputs) != 1:
                raise ValueError("semantic legacy payload attestation output is ambiguous")
            attestation = _input_mapping_from_output(
                cause_receipt.outputs[0],
                name="legacy_payload_attestation",
            )
            raw_inputs.append(attestation)
            cause_expected_input = _input_contracts(
                (attestation,),
                stage_id=SEMANTIC_EMBEDDING_STAGE,
                processing_signature=str(member["processing_signature"]),
                observed_ns=int(receipt_row["started_ns"]),
            )[0]
        else:
            if reused_payload_input is None:
                raise ValueError("semantic cached embedding lacks exact payload input")
            cause_expected_input = _input_contracts(
                (
                    _input_mapping_from_binding(
                        reused_payload_input,
                        name="reused_vector_payload",
                    ),
                ),
                stage_id=SEMANTIC_EMBEDDING_STAGE,
                processing_signature=str(member["processing_signature"]),
                observed_ns=int(receipt_row["started_ns"]),
            )[0]
    expected_inputs = _input_contracts(
        tuple(raw_inputs),
        stage_id=SEMANTIC_EMBEDDING_STAGE,
        processing_signature=str(member["processing_signature"]),
        observed_ns=int(receipt_row["started_ns"]),
    )
    expected_outputs = _output_contracts(
        ((payload, member_binding) if mode is WorkExecutionMode.EXECUTED else (member_binding,)),
        generation_id=generation_id,
        schema_version=receipt_schema_version,
    )
    if (
        receipt.stage.stage_version != "semantic-embedding-v1"
        or receipt.stage.processing_signature != str(member["processing_signature"])
        or receipt.stage.provider != str(provider["provider"])
        or receipt.stage.model != str(provider["model_id"])
        or receipt.stage.model_version != str(provider["model_version"])
        or receipt.inputs != expected_inputs
        or receipt.outputs != expected_outputs
        or dict(receipt.effective_configuration)
        or str(receipt_row["model_signature"]) != str(member["model_signature"])
        or str(receipt_row["entity_id"]) != str(member["entity_id"])
        or str(receipt_row["entity_kind"])
        != ("text_embedding" if str(member["entity_kind"]) == "text_chunk" else "image_embedding")
        or int(receipt_row["item_revision_id"]) != item_revision_id
        or (
            None
            if receipt_row["chunk_revision_id"] is None
            else int(receipt_row["chunk_revision_id"])
        )
        != chunk_revision_id
        or int(receipt_row["payload_id"]) != payload_id
    ):
        raise ValueError("semantic embedding receipt contradicts exact physical facts")
    causation_id = receipt.causation_id
    if mode in {WorkExecutionMode.CACHE_HIT, WorkExecutionMode.REPLAY}:
        if causation_id is None or cause_expected_input is None:
            raise ValueError("semantic reused embedding lacks exact causation")
        _validate_embedding_causation_receipt(
            connection,
            causation_id=causation_id,
            expected_input=cause_expected_input,
            seen=selected_seen,
        )
    elif mode is WorkExecutionMode.EXECUTED:
        if chunk_revision_id is None:
            if causation_id is not None:
                raise ValueError("semantic image execution has unexpected causation")
        else:
            if causation_id is None:
                raise ValueError("semantic text execution lacks exact causation")
            cause = connection.execute(
                """SELECT receipt.receipt_key
                FROM semantic_chunk_derivations derivation
                JOIN semantic_work_receipts receipt
                  ON receipt.receipt_id=derivation.materialization_receipt_id
                WHERE derivation.chunk_revision_id=?
                  AND derivation.item_revision_id=?
                  AND receipt.receipt_key=? LIMIT 1""",
                (chunk_revision_id, item_revision_id, causation_id),
            ).fetchone()
            if cause is None or causation_id != str(cause["receipt_key"]):
                raise ValueError("semantic text execution has invalid causation")
            _validated_semantic_receipt_by_key(connection, causation_id)
    else:
        raise ValueError("semantic successful member has an invalid execution mode")


def _validate_embedding_clone_receipt_contract(
    connection: sqlite3.Connection,
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
) -> None:
    if receipt_row["generation_id"] is None:
        raise ValueError("semantic clone receipt lacks a generation")
    receipt_schema_version = _receipt_schema_version(receipt)
    generation_id = int(receipt_row["generation_id"])
    output_member_ids = tuple(
        _materialization_numeric_identifier(
            output.materialization,
            prefix="materialization:semantic:embedding-member:",
        )
        for output in receipt.outputs
    )
    input_member_ids = tuple(
        _materialization_numeric_identifier(
            input_binding.materialization,
            prefix="materialization:semantic:embedding-member:",
        )
        for input_binding in receipt.inputs
        if input_binding.materialization is not None
    )
    if (
        not output_member_ids
        or len(output_member_ids) != len(set(output_member_ids))
        or len(input_member_ids) != len(output_member_ids)
        or len(input_member_ids) != len(set(input_member_ids))
        or len(output_member_ids) > _MAX_MANIFEST_INPUTS
    ):
        raise ValueError("semantic clone receipt member set is invalid")
    placeholders = ",".join("?" for _ in input_member_ids)
    base_generations = tuple(
        int(row[0])
        for row in connection.execute(
            f"""SELECT DISTINCT generation_id
            FROM embedding_generation_members
            WHERE member_id IN ({placeholders}) ORDER BY generation_id""",
            input_member_ids,
        )
    )
    if len(base_generations) != 1:
        raise ValueError("semantic clone receipt base generation is unavailable")
    base_generation_id = base_generations[0]
    base_by_id = {
        int(row["member_id"]): row
        for row in _embedding_member_rows(
            connection,
            generation_id=base_generation_id,
            member_ids=input_member_ids,
        )
    }
    output_by_id = {
        int(row["member_id"]): row
        for row in _embedding_member_rows(
            connection,
            generation_id=generation_id,
            member_ids=output_member_ids,
        )
    }
    if set(base_by_id) != set(input_member_ids) or set(output_by_id) != set(output_member_ids):
        raise ValueError("semantic clone receipt member facts disappeared")
    for input_member_id, output_member_id in zip(
        input_member_ids,
        output_member_ids,
        strict=True,
    ):
        base = base_by_id[input_member_id]
        output = output_by_id[output_member_id]
        if int(output["base_member_id"] or 0) != input_member_id or any(
            output[name] != base[name]
            for name in (
                "model_signature",
                "entity_kind",
                "entity_id",
                "item_id",
                "item_revision_id",
                "chunk_revision_id",
                "payload_id",
                "content_xxh3_128",
                "content_bytes",
                "content_xxh3_64_guard",
            )
        ):
            raise ValueError("semantic clone receipt does not preserve member causality")
    expected_inputs = _input_contracts(
        tuple(
            _renamed_binding(
                _embedding_member_binding_from_row(
                    base_by_id[member_id],
                    schema_version=receipt_schema_version,
                ),
                f"base_member:{index}",
            )
            for index, member_id in enumerate(input_member_ids)
        ),
        stage_id=SEMANTIC_EMBEDDING_CLONE_STAGE,
        processing_signature=str(receipt_row["processing_signature"]),
        observed_ns=int(receipt_row["started_ns"]),
    )
    expected_outputs = _output_contracts(
        tuple(
            _embedding_member_binding_from_row(
                output_by_id[member_id],
                schema_version=receipt_schema_version,
            )
            for member_id in output_member_ids
        ),
        generation_id=generation_id,
        schema_version=receipt_schema_version,
    )
    generation = connection.execute(
        """SELECT generation.processing_signature,generation.model_signature,
            model.provider,model.model_id,model.model_version
        FROM embedding_generations generation
        JOIN embedding_models model
          ON model.model_signature=generation.model_signature
        WHERE generation.generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise ValueError("semantic clone generation disappeared")
    if (
        receipt.stage.stage_version != "semantic-embedding-clone-v1"
        or receipt.stage.processing_signature != str(generation["processing_signature"])
        or receipt.stage.provider != str(generation["provider"])
        or receipt.stage.model != str(generation["model_id"])
        or receipt.stage.model_version != str(generation["model_version"])
        or receipt.inputs != expected_inputs
        or receipt.outputs != expected_outputs
        or dict(receipt.effective_configuration)
        != {
            "base_generation_id": base_generation_id,
            "member_count": len(output_member_ids),
        }
        or str(receipt_row["entity_kind"]) != "embedding_clone_page"
        or str(receipt_row["model_signature"]) != str(generation["model_signature"])
        or receipt.causation_id is not None
    ):
        raise ValueError("semantic clone receipt contradicts exact physical facts")


def _snapshot_item_revision(
    connection: sqlite3.Connection,
    item_id: str,
    now_ns: int,
) -> int:
    item = connection.execute(
        """SELECT item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,source_revision_json
        FROM semantic_items WHERE item_id=? AND active=1""",
        (item_id,),
    ).fetchone()
    if item is None:
        raise StaleEmbeddingJobError("semantic item became inactive before snapshot")
    existing = connection.execute(
        """SELECT item_revision_id FROM semantic_item_revisions
        WHERE item_id=? AND source_kind=? AND source_identity=?
          AND identity_version=? AND path IS ? AND content_xxh3_128=?
          AND content_bytes=? AND content_xxh3_64_guard=?
          AND provenance_json=? AND source_revision_json=?
        ORDER BY item_revision_id DESC LIMIT 1""",
        (
            str(item["item_id"]),
            str(item["source_kind"]),
            str(item["source_identity"]),
            str(item["identity_version"]),
            None if item["path"] is None else str(item["path"]),
            str(item["content_xxh3_128"]),
            int(item["content_bytes"]),
            str(item["content_xxh3_64_guard"]),
            str(item["provenance_json"]),
            str(item["source_revision_json"]),
        ),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    cursor = connection.execute(
        """INSERT INTO semantic_item_revisions(
            item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,source_revision_json,captured_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(item["item_id"]),
            str(item["source_kind"]),
            str(item["source_identity"]),
            str(item["identity_version"]),
            None if item["path"] is None else str(item["path"]),
            str(item["content_xxh3_128"]),
            int(item["content_bytes"]),
            str(item["content_xxh3_64_guard"]),
            str(item["provenance_json"]),
            str(item["source_revision_json"]),
            now_ns,
        ),
    )
    if cursor.lastrowid is None:
        raise SemanticStateError("item revision insert returned no identifier")
    return int(cursor.lastrowid)


def _snapshot_chunk_revision(
    connection: sqlite3.Connection,
    chunk_id: str,
    fingerprint: ContentFingerprint,
    now_ns: int,
    *,
    require_published: bool = False,
) -> int:
    chunk = connection.execute(
        """SELECT chunk_id,item_id,ordinal,section_kind,section_id,start_char,
            end_char,text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,refresh_token
        FROM text_chunks WHERE chunk_id=? AND active=1""",
        (chunk_id,),
    ).fetchone()
    if chunk is None or not _same_fingerprint(chunk, fingerprint):
        raise StaleEmbeddingJobError("text chunk changed before snapshot")
    existing = connection.execute(
        "SELECT * FROM semantic_chunk_revisions WHERE chunk_id=?",
        (chunk_id,),
    ).fetchone()
    values = (
        str(chunk["item_id"]),
        int(chunk["ordinal"]),
        str(chunk["section_kind"]),
        str(chunk["section_id"]),
        int(chunk["start_char"]),
        int(chunk["end_char"]),
        bytes(chunk["text_zlib"]),
        int(chunk["text_chars"]),
        str(chunk["content_xxh3_128"]),
        int(chunk["content_bytes"]),
        str(chunk["content_xxh3_64_guard"]),
        str(chunk["chunking_signature"]),
        str(chunk["provenance_json"]),
    )
    if existing is not None:
        persisted = (
            str(existing["item_id"]),
            int(existing["ordinal"]),
            str(existing["section_kind"]),
            str(existing["section_id"]),
            int(existing["start_char"]),
            int(existing["end_char"]),
            bytes(existing["text_zlib"]),
            int(existing["text_chars"]),
            str(existing["content_xxh3_128"]),
            int(existing["content_bytes"]),
            str(existing["content_xxh3_64_guard"]),
            str(existing["chunking_signature"]),
            str(existing["provenance_json"]),
        )
        if persisted != values:
            raise SemanticStateError(
                "content-addressed chunk id is bound to different snapshot data"
            )
        chunk_revision_id = int(existing["chunk_revision_id"])
        if require_published:
            _require_published_chunk_revision(
                connection,
                chunk_revision_id=chunk_revision_id,
                refresh_token=str(chunk["refresh_token"]),
            )
        return chunk_revision_id
    cursor = connection.execute(
        """INSERT INTO semantic_chunk_revisions(
            chunk_id,item_id,ordinal,section_kind,section_id,start_char,end_char,
            text_zlib,text_chars,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,chunking_signature,provenance_json,captured_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (chunk_id, *values, now_ns),
    )
    if cursor.lastrowid is None:
        raise SemanticStateError("chunk revision insert returned no identifier")
    chunk_revision_id = int(cursor.lastrowid)
    if require_published:
        _require_published_chunk_revision(
            connection,
            chunk_revision_id=chunk_revision_id,
            refresh_token=str(chunk["refresh_token"]),
        )
    return chunk_revision_id


def _require_published_chunk_revision(
    connection: sqlite3.Connection,
    *,
    chunk_revision_id: int,
    refresh_token: str,
) -> None:
    current = connection.execute(
        """SELECT 1 FROM semantic_chunk_derivations
        WHERE chunk_revision_id=? AND refresh_token=?
          AND publication_receipt_id IS NOT NULL LIMIT 1""",
        (chunk_revision_id, refresh_token),
    ).fetchone()
    if current is not None:
        return
    previously_published = connection.execute(
        """SELECT 1 FROM semantic_chunk_derivations
        WHERE chunk_revision_id=? AND publication_receipt_id IS NOT NULL
        LIMIT 1""",
        (chunk_revision_id,),
    ).fetchone()
    if previously_published is not None:
        return
    has_owner_derivation = connection.execute(
        """SELECT 1 FROM semantic_chunk_derivations
        WHERE chunk_revision_id=? LIMIT 1""",
        (chunk_revision_id,),
    ).fetchone()
    legacy_published = None
    if has_owner_derivation is None:
        legacy_published = connection.execute(
            """SELECT 1 FROM embedding_generation_members member
            JOIN published_embedding_heads head
              ON head.generation_id=member.generation_id
             AND head.model_signature=member.model_signature
            JOIN embedding_generations generation
              ON generation.generation_id=head.generation_id
             AND generation.model_signature=head.model_signature
             AND generation.status='ready'
            WHERE member.chunk_revision_id=? AND member.entity_kind='text_chunk'
            LIMIT 1""",
            (chunk_revision_id,),
        ).fetchone()
    if legacy_published is None:
        raise StaleEmbeddingJobError("text chunk refresh is materialized but not published")


def _item_revision_binding(
    connection: sqlite3.Connection,
    item_revision_id: int,
    *,
    schema_version: int | None = None,
) -> dict[str, object]:
    selected_schema_version = (
        _require_current_receipt_schema(connection)
        if schema_version is None
        else schema_version
    )
    if selected_schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic item materialization schema {selected_schema_version!r} is unsupported"
        )
    row = connection.execute(
        "SELECT * FROM semantic_item_revisions WHERE item_revision_id=?",
        (item_revision_id,),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic item revision disappeared")
    revision = RevisionRef(
        resource_id=(
            "semantic:item:" + xxhash.xxh3_128_hexdigest(str(row["item_id"]).encode("utf-8"))
        ),
        revision_id=f"revision:semantic:item:{item_revision_id}",
        producer="semantic.item.snapshot",
        processing_signature=str(row["identity_version"]),
        generation=item_revision_id,
        state=RevisionState.CURRENT,
        observed_at_utc=_utc_from_ns(int(row["captured_ns"])),
    )
    materialization = MaterializationRef(
        owner="semantic",
        kind="semantic_item_revision",
        materialization_id=f"materialization:semantic:item-revision:{item_revision_id}",
        schema_version=selected_schema_version,
        revision=revision,
        generation=item_revision_id,
    )
    return {
        "kind": "semantic_item_revision",
        "item_id": str(row["item_id"]),
        "revision_id": item_revision_id,
        "binding_name": "semantic_item_snapshot",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "source_kind": str(row["source_kind"]),
        "source_identity": str(row["source_identity"]),
        "fingerprint": {
            "algorithm": "xxh3-128+bytes+xxh3-64-guard",
            "xxh3_128": str(row["content_xxh3_128"]),
            "byte_count": int(row["content_bytes"]),
            "xxh3_64_guard": str(row["content_xxh3_64_guard"]),
        },
    }


def _native_source_revision_binding(
    connection: sqlite3.Connection,
    item_revision_id: int,
) -> dict[str, object] | None:
    row = connection.execute(
        """SELECT source_revision_json FROM semantic_item_revisions
        WHERE item_revision_id=?""",
        (item_revision_id,),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic item revision disappeared")
    source_revision = _json_object(
        row["source_revision_json"],
        label="semantic item source revision",
    )
    owner_revision = source_revision.get("owner_revision")
    if owner_revision is None:
        return None
    if not isinstance(owner_revision, Mapping):
        raise SemanticStateError("semantic owner revision contract is invalid")
    raw_revision = owner_revision.get("revision")
    if not isinstance(raw_revision, Mapping):
        raise SemanticStateError("semantic owner RevisionRef is missing")
    try:
        generation_value = raw_revision.get("generation")
        revision = RevisionRef(
            resource_id=str(raw_revision["resource_id"]),
            revision_id=str(raw_revision["revision_id"]),
            producer=str(raw_revision["producer"]),
            processing_signature=str(raw_revision["processing_signature"]),
            generation=(None if generation_value is None else int(generation_value)),
            state=RevisionState(str(raw_revision["state"])),
            observed_at_utc=(
                None
                if raw_revision.get("observed_at_utc") is None
                else str(raw_revision["observed_at_utc"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticStateError("semantic owner RevisionRef is invalid") from exc
    fingerprint_algorithm = owner_revision.get("fingerprint_algorithm")
    fingerprint = owner_revision.get("fingerprint")
    if (
        not isinstance(fingerprint_algorithm, str)
        or not fingerprint_algorithm.strip()
        or not isinstance(fingerprint, str)
        or not fingerprint.strip()
    ):
        raise SemanticStateError("semantic owner revision fingerprint is invalid")
    materialization = None
    consumed_materialization = source_revision.get("consumed_materialization")
    if consumed_materialization is not None:
        if not isinstance(consumed_materialization, Mapping):
            raise SemanticStateError("semantic consumed materialization contract is invalid")
        raw_materialization = consumed_materialization.get("materialization")
        try:
            materialization = MaterializationRef.from_dict(raw_materialization)
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticStateError("semantic consumed MaterializationRef is invalid") from exc
        selected_algorithm = consumed_materialization.get("fingerprint_algorithm")
        selected_fingerprint = consumed_materialization.get("fingerprint")
        if (
            not isinstance(selected_algorithm, str)
            or not selected_algorithm.strip()
            or not isinstance(selected_fingerprint, str)
            or not selected_fingerprint.strip()
        ):
            raise SemanticStateError("semantic consumed materialization fingerprint is invalid")
        fingerprint_algorithm = selected_algorithm
        fingerprint = selected_fingerprint
    return {
        "kind": "source_revision",
        "entity_id": revision.revision_id,
        "binding_name": "owner_source_revision",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": fingerprint_algorithm,
            "value": fingerprint,
        },
    }


def _chunk_revision_binding(
    connection: sqlite3.Connection,
    chunk_revision_id: int,
    *,
    schema_version: int | None = None,
) -> dict[str, object]:
    selected_schema_version = (
        _require_current_receipt_schema(connection)
        if schema_version is None
        else schema_version
    )
    row = connection.execute(
        "SELECT * FROM semantic_chunk_revisions WHERE chunk_revision_id=?",
        (chunk_revision_id,),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic chunk revision disappeared")
    return _chunk_revision_binding_from_row(
        row,
        schema_version=selected_schema_version,
    )


def _chunk_revision_binding_from_row(
    row: sqlite3.Row,
    *,
    schema_version: int,
) -> dict[str, object]:
    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic chunk materialization schema {schema_version!r} is unsupported"
        )
    chunk_revision_id = int(row["chunk_revision_id"])
    revision = RevisionRef(
        resource_id=(
            "semantic:text-chunk:" + xxhash.xxh3_128_hexdigest(str(row["chunk_id"]).encode("utf-8"))
        ),
        revision_id=f"revision:semantic:chunk:{chunk_revision_id}",
        producer=SEMANTIC_CHUNK_STAGE,
        processing_signature=str(row["chunking_signature"]),
        generation=chunk_revision_id,
        state=RevisionState.CURRENT,
        observed_at_utc=_utc_from_ns(int(row["captured_ns"])),
    )
    materialization = MaterializationRef(
        owner="semantic",
        kind="semantic_chunk_revision",
        materialization_id=(f"materialization:semantic:chunk-revision:{chunk_revision_id}"),
        schema_version=schema_version,
        revision=revision,
        generation=chunk_revision_id,
    )
    return {
        "kind": "semantic_chunk_revision",
        "chunk_id": str(row["chunk_id"]),
        "revision_id": chunk_revision_id,
        "binding_name": "semantic_chunk_snapshot",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": "xxh3-128+bytes+xxh3-64-guard",
            "xxh3_128": str(row["content_xxh3_128"]),
            "byte_count": int(row["content_bytes"]),
            "xxh3_64_guard": str(row["content_xxh3_64_guard"]),
        },
    }


def _manifest_binding_fact(binding: Mapping[str, object]) -> dict[str, object]:
    """Return stable causal facts for one already validated owner binding."""

    fingerprint, fingerprint_algorithm = _binding_fingerprint(binding)
    revision = binding.get("revision_ref")
    materialization = binding.get("materialization_ref")
    if not isinstance(revision, RevisionRef) or not isinstance(materialization, MaterializationRef):
        raise SemanticStateError(
            "semantic manifest inputs require exact revision and materialization refs"
        )
    materialization_payload = materialization.to_dict()
    if materialization.owner == "semantic":
        if (
            isinstance(materialization.schema_version, bool)
            or materialization.schema_version not in _RECEIPT_SCHEMA_VERSIONS
        ):
            raise SemanticStateError(
                "semantic manifest locator schema metadata is not 7, 8, 9 or 10"
            )
        # Owner-schema metadata is provenance, not content identity.  Pin the
        # digest representation to the last receipt schema so a v7 receipt
        # keeps its stable manifest/clone key after the v8 owner migration.
        materialization_payload["owner_schema_version"] = _SEMANTIC_IDENTITY_SCHEMA_VERSION
    return {
        "revision": revision.to_dict(),
        "materialization": materialization_payload,
        "fingerprint": fingerprint,
        "fingerprint_algorithm": fingerprint_algorithm,
    }


def _renamed_binding(
    binding: Mapping[str, object],
    name: str,
) -> dict[str, object]:
    renamed = dict(binding)
    renamed["binding_name"] = name
    return renamed


def _receipt_output_candidates_uncached(
    connection: sqlite3.Connection,
    materialization_ids: Sequence[str],
) -> Iterator[sqlite3.Row]:
    """Preserve the original global lookup for query-only connections."""

    selected_ids = tuple(dict.fromkeys(str(value) for value in materialization_ids))
    for offset in range(0, len(selected_ids), _RECEIPT_LOOKUP_BATCH):
        batch = selected_ids[offset : offset + _RECEIPT_LOOKUP_BATCH]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        yield from connection.execute(
            f"""SELECT receipt.receipt_id,receipt.status,
                output.id AS output_ordinal,
                json_extract(output.value,
                    '$.materialization.materialization_id') AS materialization_id,
                json_extract(output.value,'$.fingerprint') AS fingerprint,
                json_extract(output.value,'$.fingerprint_algorithm')
                    AS fingerprint_algorithm
            FROM semantic_work_receipts receipt
            JOIN json_each(receipt.receipt_json,'$.outputs') output
              ON TRUE
            WHERE json_extract(
                output.value,'$.materialization.materialization_id'
            ) IN ({placeholders})
            ORDER BY receipt.receipt_id,output.id""",
            batch,
        )


def _refresh_receipt_output_lookup(connection: sqlite3.Connection) -> None:
    """Refresh the connection-local output index through a receipt watermark.

    The TEMP rows and their watermark are updated inside the caller's
    transaction (and a nested savepoint for refresh failures); this helper
    never commits the owner database.  ``semantic_work_receipts`` is append-only
    and its integer receipt id is therefore a safe incremental boundary.
    """

    connection.execute(
        f"""CREATE TEMP TABLE IF NOT EXISTS {_RECEIPT_LOOKUP_TABLE}(
            receipt_id INTEGER NOT NULL,
            output_ordinal INTEGER NOT NULL,
            materialization_id TEXT,
            status TEXT NOT NULL,
            fingerprint TEXT,
            fingerprint_algorithm TEXT,
            PRIMARY KEY(receipt_id,output_ordinal)
        )"""
    )
    connection.execute(
        f"""CREATE INDEX IF NOT EXISTS {_RECEIPT_LOOKUP_INDEX}
        ON {_RECEIPT_LOOKUP_TABLE}(materialization_id,receipt_id,output_ordinal)"""
    )
    connection.execute(
        f"""CREATE TEMP TABLE IF NOT EXISTS {_RECEIPT_LOOKUP_STATE_TABLE}(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            watermark_receipt_id INTEGER NOT NULL CHECK(watermark_receipt_id>=0)
        )"""
    )
    connection.execute(
        f"""INSERT OR IGNORE INTO {_RECEIPT_LOOKUP_STATE_TABLE}(
            singleton,watermark_receipt_id) VALUES(1,0)"""
    )
    state = connection.execute(
        f"""SELECT watermark_receipt_id
        FROM {_RECEIPT_LOOKUP_STATE_TABLE} WHERE singleton=1"""
    ).fetchone()
    if state is None:
        raise SemanticStateError("semantic receipt lookup watermark is unavailable")
    watermark = int(state[0])
    high_watermark = int(
        connection.execute(
            "SELECT COALESCE(MAX(receipt_id),0) FROM semantic_work_receipts"
        ).fetchone()[0]
    )
    if high_watermark <= watermark:
        return
    connection.execute(f"SAVEPOINT {_RECEIPT_LOOKUP_SAVEPOINT}")
    try:
        connection.execute(
            f"""INSERT INTO {_RECEIPT_LOOKUP_TABLE}(
                receipt_id,output_ordinal,materialization_id,status,
                fingerprint,fingerprint_algorithm)
            SELECT receipt.receipt_id,output.id,
                json_extract(output.value,
                    '$.materialization.materialization_id'),
                receipt.status,
                json_extract(output.value,'$.fingerprint'),
                json_extract(output.value,'$.fingerprint_algorithm')
            FROM semantic_work_receipts receipt
            JOIN json_each(receipt.receipt_json,'$.outputs') output
              ON TRUE
            WHERE receipt.receipt_id>? AND receipt.receipt_id<=?
            ORDER BY receipt.receipt_id,output.id""",
            (watermark, high_watermark),
        )
        connection.execute(
            f"""UPDATE {_RECEIPT_LOOKUP_STATE_TABLE}
            SET watermark_receipt_id=? WHERE singleton=1""",
            (high_watermark,),
        )
        connection.execute(f"RELEASE SAVEPOINT {_RECEIPT_LOOKUP_SAVEPOINT}")
    except BaseException:
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {_RECEIPT_LOOKUP_SAVEPOINT}")
            connection.execute(f"RELEASE SAVEPOINT {_RECEIPT_LOOKUP_SAVEPOINT}")
        except sqlite3.Error:
            pass
        raise


def _receipt_output_candidates_for_materializations(
    connection: sqlite3.Connection,
    materialization_ids: Sequence[str],
) -> Iterator[sqlite3.Row]:
    """Return all-status output candidates using one connection-local index.

    Query-only consumers cannot create TEMP state and retain the original
    bounded global query.  Writer rebinds build the TEMP index once, then only
    scan receipt ids newer than its append-only watermark.
    """

    selected_ids = tuple(dict.fromkeys(str(value) for value in materialization_ids))
    if not selected_ids:
        return
    query_only = int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
    if query_only:
        yield from _receipt_output_candidates_uncached(connection, selected_ids)
        return
    _refresh_receipt_output_lookup(connection)
    for offset in range(0, len(selected_ids), _RECEIPT_LOOKUP_BATCH):
        batch = selected_ids[offset : offset + _RECEIPT_LOOKUP_BATCH]
        placeholders = ",".join("?" for _ in batch)
        yield from connection.execute(
            f"""SELECT receipt_id,status,output_ordinal,
                materialization_id,fingerprint,fingerprint_algorithm
            FROM {_RECEIPT_LOOKUP_TABLE}
            WHERE materialization_id IN ({placeholders})
            ORDER BY receipt_id,output_ordinal""",
            batch,
        )


def _producer_receipts_for_embedding_members(
    connection: sqlite3.Connection,
    member_ids: Sequence[int],
) -> dict[int, int]:
    schema_version = _require_current_receipt_schema(connection)
    producer_by_member: dict[int, int] = {}
    for offset in range(0, len(member_ids), 250):
        batch = tuple(dict.fromkeys(int(value) for value in member_ids[offset : offset + 250]))
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        physical_rows = connection.execute(
            f"""SELECT member.*,g.processing_signature,
                payload.dimensions,payload.vector_dtype,payload.original_norm,
                payload.content_xxh3_128 AS payload_xxh3_128,
                payload.content_bytes AS payload_bytes,
                payload.content_xxh3_64_guard AS payload_xxh3_64_guard
            FROM embedding_generation_members member
            JOIN embedding_generations g
              ON g.generation_id=member.generation_id
            JOIN vector_payloads payload ON payload.payload_id=member.payload_id
            WHERE member.member_id IN ({placeholders})
            ORDER BY member.member_id""",
            batch,
        ).fetchall()
        physical_by_id = {int(row["member_id"]): row for row in physical_rows}
        missing = set(batch).difference(physical_by_id)
        if missing:
            raise SemanticStateError(
                f"semantic source embedding member disappeared: {min(missing)}"
            )
        expected_by_materialization: dict[str, tuple[int, str, str]] = {}
        for member_id in batch:
            binding = _embedding_member_binding_from_row(
                physical_by_id[member_id],
                schema_version=schema_version,
            )
            materialization = binding.get("materialization_ref")
            if not isinstance(materialization, MaterializationRef):
                raise SemanticStateError(
                    "semantic source embedding member has no exact materialization"
                )
            fingerprint, fingerprint_algorithm = _binding_fingerprint(binding)
            expected_by_materialization[materialization.materialization_id] = (
                member_id,
                fingerprint,
                fingerprint_algorithm,
            )
        rows = _receipt_output_candidates_for_materializations(
            connection,
            tuple(expected_by_materialization),
        )
        matching_receipts: dict[int, set[int]] = {member_id: set() for member_id in batch}
        try:
            for row in rows:
                if str(row["status"]) != "succeeded":
                    continue
                materialization_id = str(row["materialization_id"])
                expected = expected_by_materialization.get(materialization_id)
                if expected is None:  # pragma: no cover - protected by the SQL predicate
                    continue
                member_id, fingerprint, fingerprint_algorithm = expected
                if (
                    str(row["fingerprint"]) != fingerprint
                    or str(row["fingerprint_algorithm"]) != fingerprint_algorithm
                ):
                    continue
                receipt_id = int(row["receipt_id"])
                exact_receipts = matching_receipts[member_id]
                exact_receipts.add(receipt_id)
                if len(exact_receipts) > 1:
                    raise SemanticStateError(
                        "semantic source embedding member has multiple exact producer receipts"
                    )
        finally:
            close = getattr(rows, "close", None)
            if close is not None:
                close()
        selected_receipt_ids = {
            next(iter(receipt_ids))
            for receipt_ids in matching_receipts.values()
            if receipt_ids
        }
        _validated_semantic_receipts(connection, selected_receipt_ids)
        for member_id in batch:
            exact_receipts = matching_receipts[member_id]
            if not exact_receipts:
                raise SemanticStateError(
                    "semantic source embedding member has no exact producer receipt"
                )
            if len(exact_receipts) != 1:
                raise SemanticStateError(
                    "semantic source embedding member has multiple exact producer receipts"
                )
            producer_by_member[member_id] = next(iter(exact_receipts))
    return producer_by_member


def _legacy_payload_attestation_binding(
    *,
    payload_id: int,
    payload_binding: Mapping[str, object],
    now_ns: int,
    schema_version: int,
) -> dict[str, object]:
    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic attestation schema {schema_version!r} is unsupported"
        )
    payload_fact = _manifest_binding_fact(payload_binding)
    contract_fingerprint = xxhash.xxh3_128_hexdigest(
        canonical_json(
            {
                "payload_id": payload_id,
                "payload_materialization": payload_fact,
            }
        ).encode("utf-8")
    )
    revision = RevisionRef(
        resource_id=f"resource:semantic:legacy-vector-payload-attestation:{payload_id}",
        revision_id=(
            "revision:semantic:legacy-vector-payload-attestation:"
            f"{payload_id}:{contract_fingerprint}"
        ),
        producer=SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE,
        processing_signature="semantic-legacy-payload-attestation-v1",
        generation=payload_id,
        state=RevisionState.CURRENT,
        observed_at_utc=_utc_from_ns(now_ns),
    )
    materialization = MaterializationRef(
        owner="semantic",
        kind="legacy_vector_payload_attestation",
        materialization_id=(
            "materialization:semantic:legacy-vector-payload-attestation:"
            f"{payload_id}:{contract_fingerprint}"
        ),
        schema_version=schema_version,
        revision=revision,
        generation=payload_id,
    )
    return {
        "kind": "legacy_vector_payload_attestation",
        "payload_id": payload_id,
        "binding_name": "legacy_vector_payload_attestation",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": "legacy-payload-attestation-xxh3-128-v1",
            "xxh3_128": contract_fingerprint,
        },
    }


def _record_legacy_payload_attestation(
    connection: sqlite3.Connection,
    payload_id: int,
    *,
    now_ns: int,
) -> int:
    row = connection.execute(
        """SELECT p.*,m.model_id,m.model_version,m.provider,m.vector_space
        FROM vector_payloads p JOIN embedding_models m
          ON m.model_signature=p.model_signature
        WHERE p.payload_id=?""",
        (payload_id,),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic legacy vector payload disappeared")
    if not bool(row["legacy_before_receipts"]):
        raise SemanticStateError("semantic vector payload without a producer receipt is not legacy")
    schema_version = _require_current_receipt_schema(connection)
    payload_binding, _payload_provider = _payload_binding(
        connection,
        payload_id,
        schema_version=schema_version,
    )
    output = _legacy_payload_attestation_binding(
        payload_id=payload_id,
        payload_binding=payload_binding,
        now_ns=now_ns,
        schema_version=schema_version,
    )
    return _record_work_receipt(
        connection,
        receipt_key=_stable_key(
            SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE,
            (payload_id, _binding_fingerprint(payload_binding)),
        ),
        stage_id=SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE,
        stage_version="semantic-legacy-payload-attestation-v1",
        processing_signature="semantic-legacy-payload-attestation-v1",
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="environment_bound",
        entity_kind="legacy_vector_payload_attestation",
        entity_id=str(payload_id),
        inputs=(_renamed_binding(payload_binding, "legacy_vector_payload"),),
        outputs=(output,),
        effective_config={"payload_id": payload_id},
        provider={"provider": "neocortex"},
        item_revision_id=None,
        chunk_revision_id=None,
        generation_id=None,
        model_signature=str(row["model_signature"]),
        payload_id=payload_id,
        job_id=None,
        attempt=1,
        started_ns=now_ns,
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=None,
        event_kind="semantic_legacy_vector_payload_attested",
        aggregate_kind="vector_payload",
        aggregate_id=str(payload_id),
        committed_ns=now_ns,
    )


def _payload_causation_receipt(
    connection: sqlite3.Connection,
    payload_id: int,
    *,
    now_ns: int | None,
) -> int:
    return _payload_causation_receipts(
        connection,
        (payload_id,),
        now_ns=now_ns,
    )[payload_id]


def _payload_causation_receipts(
    connection: sqlite3.Connection,
    payload_ids: Sequence[int],
    *,
    now_ns: int | None,
) -> dict[int, int]:
    selected_ids = tuple(dict.fromkeys(int(payload_id) for payload_id in payload_ids))
    if not selected_ids:
        return {}
    selected = set(selected_ids)
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(selected_ids), 250):
        batch = selected_ids[offset : offset + 250]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(
            connection.execute(
                f"""SELECT receipt_id,payload_id FROM semantic_work_receipts
                WHERE stage_id=? AND status='succeeded'
                  AND execution_mode='executed'
                  AND payload_id IN ({placeholders}) ORDER BY receipt_id""",
                (SEMANTIC_EMBEDDING_STAGE, *batch),
            ).fetchall()
        )
    producer_rows: dict[int, list[int]] = {payload_id: [] for payload_id in selected_ids}
    for row in rows:
        producer_rows[int(row["payload_id"])].append(int(row["receipt_id"]))
    multiple = tuple(
        payload_id for payload_id, receipt_ids in producer_rows.items() if len(receipt_ids) > 1
    )
    if multiple:
        raise SemanticStateError(
            "semantic reused vector payload has multiple executed producer receipts"
        )
    producer_ids = tuple(receipt_ids[0] for receipt_ids in producer_rows.values() if receipt_ids)
    validated = _validated_semantic_receipts(connection, producer_ids)
    resolved: dict[int, int] = {}
    for payload_id, receipt_ids in producer_rows.items():
        if not receipt_ids:
            continue
        receipt_id = receipt_ids[0]
        receipt, receipt_row = validated[receipt_id]
        _validate_embedding_payload_producer_receipt(
            connection,
            receipt,
            receipt_row,
        )
        resolved[payload_id] = receipt_id
    unresolved = tuple(payload_id for payload_id in selected_ids if payload_id not in resolved)
    attestation_rows: list[sqlite3.Row] = []
    for offset in range(0, len(unresolved), 250):
        batch = unresolved[offset : offset + 250]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        attestation_rows.extend(
            connection.execute(
                f"""SELECT receipt_id,payload_id FROM semantic_work_receipts
                WHERE stage_id=? AND status='succeeded'
                  AND execution_mode='executed'
                  AND payload_id IN ({placeholders}) ORDER BY receipt_id""",
                (SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE, *batch),
            ).fetchall()
        )
    attestations: dict[int, list[int]] = {payload_id: [] for payload_id in unresolved}
    for row in attestation_rows:
        payload_id = int(row["payload_id"])
        if payload_id in selected:
            attestations[payload_id].append(int(row["receipt_id"]))
    multiple = tuple(
        payload_id for payload_id, receipt_ids in attestations.items() if len(receipt_ids) > 1
    )
    if multiple:
        raise SemanticStateError("semantic legacy vector payload has multiple attestation receipts")
    attestation_ids = tuple(receipt_ids[0] for receipt_ids in attestations.values() if receipt_ids)
    validated_attestations = _validated_semantic_receipts(connection, attestation_ids)
    for payload_id, receipt_ids in attestations.items():
        if not receipt_ids:
            continue
        receipt_id = receipt_ids[0]
        receipt, receipt_row = validated_attestations[receipt_id]
        _validate_legacy_payload_attestation_receipt(
            connection,
            receipt,
            receipt_row,
        )
        resolved[payload_id] = receipt_id
    missing = tuple(payload_id for payload_id in selected_ids if payload_id not in resolved)
    if missing:
        if now_ns is None:
            raise SemanticStateError(
                "semantic reused vector payload has no producer or legacy attestation"
            )
        for payload_id in missing:
            resolved[payload_id] = _record_legacy_payload_attestation(
                connection,
                payload_id,
                now_ns=now_ns,
            )
    return resolved


def _manifest_output_binding(
    *,
    stage_id: str,
    processing_signature: str,
    digest: str,
    generation_id: int | None,
    schema_version: int,
) -> dict[str, object]:
    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic manifest schema {schema_version!r} is unsupported"
        )
    revision = RevisionRef(
        resource_id=f"resource:semantic:derivation-manifest:{digest}",
        revision_id=f"revision:semantic:derivation-manifest:{digest}",
        producer=stage_id,
        processing_signature=processing_signature,
        generation=generation_id,
        state=RevisionState.CURRENT,
        observed_at_utc=None,
    )
    materialization = MaterializationRef(
        owner="semantic",
        kind="derivation_manifest",
        materialization_id=f"materialization:semantic:derivation-manifest:{digest}",
        schema_version=schema_version,
        revision=revision,
        generation=generation_id,
    )
    return {
        "kind": "derivation_manifest",
        "materialization_id": materialization.materialization_id,
        "binding_name": "derivation_manifest",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": "ordered-causal-bindings-xxh3-128-v1",
            "xxh3_128": digest,
        },
    }


def _record_manifest_node(
    connection: sqlite3.Connection,
    *,
    stage_id: str,
    processing_signature: str,
    scope: Sequence[object],
    level: int,
    ordinal: int,
    inputs: Sequence[Mapping[str, object]],
    item_revision_id: int | None,
    generation_id: int | None,
    now_ns: int,
) -> dict[str, object]:
    encoded = canonical_json(
        {
            "schema": "neocortex.semantic-derivation-manifest/v1",
            "stage_id": stage_id,
            "processing_signature": processing_signature,
            "scope": list(scope),
            "level": level,
            "ordinal": ordinal,
            "inputs": [_manifest_binding_fact(binding) for binding in inputs],
        }
    )
    digest = xxhash.xxh3_128_hexdigest(encoded.encode("utf-8"))
    schema_version = _require_current_receipt_schema(connection)
    output = _manifest_output_binding(
        stage_id=stage_id,
        processing_signature=processing_signature,
        digest=digest,
        generation_id=generation_id,
        schema_version=schema_version,
    )
    _record_work_receipt(
        connection,
        receipt_key=_stable_key(
            stage_id,
            (*scope, level, ordinal, digest),
        ),
        stage_id=stage_id,
        stage_version="semantic-derivation-manifest-v1",
        processing_signature=processing_signature,
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="environment_bound",
        entity_kind="derivation_manifest",
        entity_id=digest,
        inputs=tuple(
            _renamed_binding(binding, f"member:{level}:{ordinal}:{index}")
            for index, binding in enumerate(inputs)
        ),
        outputs=(output,),
        effective_config={
            "manifest_level": level,
            "manifest_ordinal": ordinal,
            "member_count": len(inputs),
        },
        provider={"provider": "neocortex", "component": stage_id},
        item_revision_id=item_revision_id,
        chunk_revision_id=None,
        generation_id=generation_id,
        model_signature=None,
        payload_id=None,
        job_id=None,
        attempt=1,
        started_ns=None,
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=None,
        event_kind="semantic_derivation_manifest_materialized",
        aggregate_kind="derivation_manifest",
        aggregate_id=digest,
        committed_ns=now_ns,
    )
    return output


def _record_manifest_tree(
    connection: sqlite3.Connection,
    *,
    stage_id: str,
    processing_signature: str,
    scope: Sequence[object],
    bindings: Iterable[Mapping[str, object]],
    item_revision_id: int | None,
    generation_id: int | None,
    now_ns: int,
) -> tuple[dict[str, object] | None, int, str]:
    """Build a bounded fan-in tree and return its root and exact member digest."""

    member_digest = xxhash.xxh3_128()
    count = 0
    leaves: list[dict[str, object]] = []
    group: list[Mapping[str, object]] = []
    ordinal = 0
    for binding in bindings:
        fact = canonical_json(_manifest_binding_fact(binding)).encode("utf-8")
        member_digest.update(len(fact).to_bytes(8, "big"))
        member_digest.update(fact)
        count += 1
        group.append(binding)
        if len(group) == _MAX_MANIFEST_INPUTS:
            leaves.append(
                _record_manifest_node(
                    connection,
                    stage_id=stage_id,
                    processing_signature=processing_signature,
                    scope=scope,
                    level=0,
                    ordinal=ordinal,
                    inputs=tuple(group),
                    item_revision_id=item_revision_id,
                    generation_id=generation_id,
                    now_ns=now_ns,
                )
            )
            ordinal += 1
            group.clear()
    if group:
        leaves.append(
            _record_manifest_node(
                connection,
                stage_id=stage_id,
                processing_signature=processing_signature,
                scope=scope,
                level=0,
                ordinal=ordinal,
                inputs=tuple(group),
                item_revision_id=item_revision_id,
                generation_id=generation_id,
                now_ns=now_ns,
            )
        )
    if not leaves:
        return None, 0, member_digest.hexdigest()
    level = 1
    current = leaves
    while len(current) > 1:
        parents: list[dict[str, object]] = []
        for ordinal, offset in enumerate(range(0, len(current), _MAX_MANIFEST_INPUTS)):
            parents.append(
                _record_manifest_node(
                    connection,
                    stage_id=stage_id,
                    processing_signature=processing_signature,
                    scope=scope,
                    level=level,
                    ordinal=ordinal,
                    inputs=tuple(current[offset : offset + _MAX_MANIFEST_INPUTS]),
                    item_revision_id=item_revision_id,
                    generation_id=generation_id,
                    now_ns=now_ns,
                )
            )
        current = parents
        level += 1
    return (
        _renamed_binding(current[0], "derivation_manifest_root"),
        count,
        member_digest.hexdigest(),
    )


def _manifest_contract_tree(
    *,
    stage_id: str,
    processing_signature: str,
    scope: Sequence[object],
    bindings: Sequence[Mapping[str, object]],
    generation_id: int | None,
    schema_version: int,
) -> tuple[
    dict[str, object] | None,
    str,
    tuple[tuple[dict[str, object], tuple[Mapping[str, object], ...], int, int], ...],
]:
    """Recompute the bounded manifest contract without writing owner state."""

    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic manifest schema {schema_version!r} is unsupported"
        )

    member_digest = xxhash.xxh3_128()
    nodes: list[tuple[dict[str, object], tuple[Mapping[str, object], ...], int, int]] = []

    def node(
        inputs: tuple[Mapping[str, object], ...],
        *,
        level: int,
        ordinal: int,
    ) -> dict[str, object]:
        encoded = canonical_json(
            {
                "schema": "neocortex.semantic-derivation-manifest/v1",
                "stage_id": stage_id,
                "processing_signature": processing_signature,
                "scope": list(scope),
                "level": level,
                "ordinal": ordinal,
                "inputs": [_manifest_binding_fact(binding) for binding in inputs],
            }
        )
        digest = xxhash.xxh3_128_hexdigest(encoded.encode("utf-8"))
        output = _manifest_output_binding(
            stage_id=stage_id,
            processing_signature=processing_signature,
            digest=digest,
            generation_id=generation_id,
            schema_version=schema_version,
        )
        nodes.append((output, inputs, level, ordinal))
        return output

    leaves: list[dict[str, object]] = []
    for ordinal, offset in enumerate(range(0, len(bindings), _MAX_MANIFEST_INPUTS)):
        group = tuple(bindings[offset : offset + _MAX_MANIFEST_INPUTS])
        for binding in group:
            fact = canonical_json(_manifest_binding_fact(binding)).encode("utf-8")
            member_digest.update(len(fact).to_bytes(8, "big"))
            member_digest.update(fact)
        leaves.append(node(group, level=0, ordinal=ordinal))
    level = 1
    current = leaves
    while len(current) > 1:
        parents: list[dict[str, object]] = []
        for ordinal, offset in enumerate(range(0, len(current), _MAX_MANIFEST_INPUTS)):
            parents.append(
                node(
                    tuple(current[offset : offset + _MAX_MANIFEST_INPUTS]),
                    level=level,
                    ordinal=ordinal,
                )
            )
        current = parents
        level += 1
    root = None if not current else _renamed_binding(current[0], "derivation_manifest_root")
    return root, member_digest.hexdigest(), tuple(nodes)


def _chunk_set_output_binding(
    *,
    item_id: str,
    item_revision_id: int,
    chunking_signature: str,
    refresh_token: str,
    chunk_count: int,
    members_fingerprint: str,
) -> dict[str, object]:
    output_fingerprint = xxhash.xxh3_128_hexdigest(
        canonical_json(
            {
                "item_revision_id": item_revision_id,
                "chunking_signature": chunking_signature,
                "refresh_token": refresh_token,
                "chunk_count": chunk_count,
                "members_xxh3_128": members_fingerprint,
            }
        ).encode("utf-8")
    )
    return {
        "kind": "semantic_chunk_set",
        "materialization_id": (
            "materialization:semantic:chunk-set:"
            + xxhash.xxh3_128_hexdigest(
                f"{item_id}\0{chunking_signature}\0{refresh_token}".encode("utf-8")
            )
        ),
        "item_id": item_id,
        "chunking_signature": chunking_signature,
        "chunk_count": chunk_count,
        "fingerprint": {
            "algorithm": "semantic-chunk-set-contract-xxh3-128-v1",
            "xxh3_128": output_fingerprint,
        },
    }


def _validate_chunk_publication_receipt(
    connection: sqlite3.Connection,
    *,
    receipt: WorkReceipt,
    receipt_row: sqlite3.Row,
    item_id: str,
    item_revision_id: int,
    chunking_signature: str,
    refresh_token: str,
) -> bool:
    """Validate one bounded publication exactly; return False above the cap."""

    receipt_schema_version = _receipt_schema_version(receipt)
    rows = connection.execute(
        """SELECT revision.chunk_revision_id,revision.chunk_id,
            revision.chunking_signature,revision.captured_ns,
            revision.content_xxh3_128,revision.content_bytes,
            revision.content_xxh3_64_guard,COUNT(*) OVER() AS total_count
        FROM semantic_chunk_revisions revision
        JOIN semantic_chunk_derivations derivation
          ON derivation.chunk_revision_id=revision.chunk_revision_id
        WHERE derivation.item_revision_id=? AND derivation.refresh_token=?
          AND revision.item_id=? AND revision.chunking_signature=?
        ORDER BY revision.ordinal,revision.chunk_id LIMIT ?""",
        (
            item_revision_id,
            refresh_token,
            item_id,
            chunking_signature,
            MAX_LINEAGE_ROWS + 1,
        ),
    ).fetchall()
    total_count = 0 if not rows else int(rows[0]["total_count"])
    if total_count > MAX_LINEAGE_ROWS:
        return False
    bindings = tuple(
        _chunk_revision_binding_from_row(
            row,
            schema_version=receipt_schema_version,
        )
        for row in rows
    )
    root, members_fingerprint, nodes = _manifest_contract_tree(
        stage_id=SEMANTIC_CHUNK_MANIFEST_STAGE,
        processing_signature="semantic-chunk-manifest-v1",
        scope=(item_revision_id, chunking_signature, refresh_token),
        bindings=bindings,
        generation_id=None,
        schema_version=receipt_schema_version,
    )
    materialization_ids = tuple(
        str(node_output["materialization_id"]) for node_output, _inputs, _level, _ordinal in nodes
    )
    manifest_receipt_ids: dict[str, int] = {}
    if materialization_ids:
        placeholders = ",".join("?" for _ in materialization_ids)
        producer_rows = connection.execute(
            f"""SELECT receipt.receipt_id,
                json_extract(output.value,'$.materialization.materialization_id')
                  AS materialization_id
            FROM semantic_work_receipts receipt,
                 json_each(receipt.receipt_json,'$.outputs') output
            WHERE receipt.stage_id=? AND receipt.status='succeeded'
              AND json_extract(
                  output.value,'$.materialization.materialization_id'
              ) IN ({placeholders})
            ORDER BY receipt.receipt_id""",
            (SEMANTIC_CHUNK_MANIFEST_STAGE, *materialization_ids),
        ).fetchall()
        for producer in producer_rows:
            materialization_id = str(producer["materialization_id"])
            receipt_id = int(producer["receipt_id"])
            if materialization_id in manifest_receipt_ids:
                raise ValueError("semantic manifest node has multiple producers")
            manifest_receipt_ids[materialization_id] = receipt_id
    if set(manifest_receipt_ids) != set(materialization_ids):
        raise ValueError("semantic publication manifest is incomplete")
    validated_manifests = _validated_semantic_receipts(
        connection,
        manifest_receipt_ids.values(),
    )
    for node_output, node_inputs, level, ordinal in nodes:
        materialization_id = str(node_output["materialization_id"])
        node_receipt, node_row = validated_manifests[manifest_receipt_ids[materialization_id]]
        expected_inputs = _input_contracts(
            tuple(
                _renamed_binding(
                    binding,
                    f"member:{level}:{ordinal}:{index}",
                )
                for index, binding in enumerate(node_inputs)
            ),
            stage_id=SEMANTIC_CHUNK_MANIFEST_STAGE,
            processing_signature="semantic-chunk-manifest-v1",
            observed_ns=int(node_row["started_ns"]),
        )
        expected_outputs = _output_contracts(
            (node_output,),
            generation_id=None,
            schema_version=receipt_schema_version,
        )
        node_fingerprint = node_output.get("fingerprint")
        digest = node_fingerprint.get("xxh3_128") if isinstance(node_fingerprint, Mapping) else None
        if not isinstance(digest, str) or not digest:
            raise ValueError("semantic manifest node lacks an exact fingerprint")
        if (
            node_receipt.stage.stage_version != "semantic-derivation-manifest-v1"
            or node_receipt.stage.processing_signature != "semantic-chunk-manifest-v1"
            or node_receipt.stage.provider != "neocortex"
            or node_receipt.inputs != expected_inputs
            or node_receipt.outputs != expected_outputs
            or dict(node_receipt.effective_configuration)
            != {
                "manifest_level": level,
                "manifest_ordinal": ordinal,
                "member_count": len(node_inputs),
            }
            or node_receipt.causation_id is not None
            or str(node_row["entity_kind"]) != "derivation_manifest"
            or str(node_row["entity_id"]) != digest
            or int(node_row["item_revision_id"]) != item_revision_id
        ):
            raise ValueError("semantic publication manifest contradicts exact facts")
    raw_publication_inputs: tuple[Mapping[str, object], ...] = (
        (
            _item_revision_binding(
                connection,
                item_revision_id,
                schema_version=receipt_schema_version,
            ),
        )
        if root is None
        else (
            _item_revision_binding(
                connection,
                item_revision_id,
                schema_version=receipt_schema_version,
            ),
            root,
        )
    )
    expected_publication_inputs = _input_contracts(
        raw_publication_inputs,
        stage_id=SEMANTIC_CHUNK_PUBLICATION_STAGE,
        processing_signature=chunking_signature,
        observed_ns=int(receipt_row["started_ns"]),
    )
    expected_publication_outputs = _output_contracts(
        (
            _chunk_set_output_binding(
                item_id=item_id,
                item_revision_id=item_revision_id,
                chunking_signature=chunking_signature,
                refresh_token=refresh_token,
                chunk_count=total_count,
                members_fingerprint=members_fingerprint,
            ),
        ),
        generation_id=None,
        schema_version=receipt_schema_version,
    )
    if (
        receipt.stage.stage_version != "owner-refresh-v1"
        or receipt.stage.processing_signature != chunking_signature
        or receipt.stage.provider != "neocortex"
        or receipt.stage.model is not None
        or receipt.outcome is not WorkOutcome.SUCCEEDED
        or receipt.execution_mode is not WorkExecutionMode.EXECUTED
        or receipt.inputs != expected_publication_inputs
        or receipt.outputs != expected_publication_outputs
        or dict(receipt.effective_configuration) != {"chunking_signature": chunking_signature}
        or receipt.causation_id is not None
        or str(receipt_row["entity_kind"]) != "text_chunk_set"
        or str(receipt_row["entity_id"]) != f"{item_id}:{chunking_signature}"
        or int(receipt_row["item_revision_id"]) != item_revision_id
        or str(receipt_row["processing_signature"]) != chunking_signature
    ):
        raise ValueError("semantic chunk publication contradicts exact physical facts")
    return True


def _record_chunk_materialization(
    connection: sqlite3.Connection,
    *,
    chunk_id: str,
    item_id: str,
    fingerprint: ContentFingerprint,
    chunking_signature: str,
    refresh_token: str,
    now_ns: int,
) -> int:
    item_revision_id = _snapshot_item_revision(connection, item_id, now_ns)
    chunk_revision_id = _snapshot_chunk_revision(
        connection,
        chunk_id,
        fingerprint,
        now_ns,
    )
    publication_key = _stable_key(
        SEMANTIC_CHUNK_PUBLICATION_STAGE,
        (item_revision_id, chunking_signature, refresh_token),
    )
    already_published = connection.execute(
        """SELECT receipt_id FROM semantic_work_receipts
        WHERE receipt_key=? AND stage_id=? LIMIT 1""",
        (publication_key, SEMANTIC_CHUNK_PUBLICATION_STAGE),
    ).fetchone()
    if already_published is not None:
        existing_derivation = connection.execute(
            """SELECT materialization_receipt_id,publication_receipt_id
            FROM semantic_chunk_derivations
            WHERE chunk_revision_id=? AND item_revision_id=? AND refresh_token=?""",
            (chunk_revision_id, item_revision_id, refresh_token),
        ).fetchone()
        if (
            existing_derivation is not None
            and existing_derivation["publication_receipt_id"] is not None
            and int(existing_derivation["publication_receipt_id"])
            == int(already_published["receipt_id"])
        ):
            return int(existing_derivation["materialization_receipt_id"])
        raise SemanticStateError("semantic text chunk refresh is already published and immutable")
    stage_version = chunking_signature.partition("|")[0]
    receipt_key = _stable_key(
        SEMANTIC_CHUNK_STAGE,
        (chunk_revision_id, item_revision_id, refresh_token),
    )
    item_binding = _item_revision_binding(connection, item_revision_id)
    native_binding = _native_source_revision_binding(connection, item_revision_id)
    receipt_id = _record_work_receipt(
        connection,
        receipt_key=receipt_key,
        stage_id=SEMANTIC_CHUNK_STAGE,
        stage_version=stage_version,
        processing_signature=chunking_signature,
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="environment_bound",
        entity_kind="text_chunk",
        entity_id=chunk_id,
        inputs=((item_binding,) if native_binding is None else (native_binding, item_binding)),
        outputs=(_chunk_revision_binding(connection, chunk_revision_id),),
        effective_config={"chunking_signature": chunking_signature},
        provider={"provider": "neocortex", "component": SEMANTIC_CHUNK_STAGE},
        item_revision_id=item_revision_id,
        chunk_revision_id=chunk_revision_id,
        generation_id=None,
        model_signature=None,
        payload_id=None,
        job_id=None,
        attempt=1,
        started_ns=None,
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=None,
        event_kind="semantic_chunk_materialized",
        aggregate_kind="text_chunk",
        aggregate_id=chunk_id,
        committed_ns=now_ns,
    )
    connection.execute(
        """INSERT INTO semantic_chunk_derivations(
            chunk_revision_id,item_revision_id,refresh_token,
            materialization_receipt_id,created_ns)
        VALUES(?,?,?,?,?)
        ON CONFLICT(chunk_revision_id,item_revision_id,refresh_token) DO NOTHING""",
        (
            chunk_revision_id,
            item_revision_id,
            refresh_token,
            receipt_id,
            now_ns,
        ),
    )
    return receipt_id


def _record_chunk_refresh_publication(
    connection: sqlite3.Connection,
    *,
    item_id: str,
    chunking_signature: str,
    refresh_token: str,
    now_ns: int,
) -> int:
    schema_version = _require_current_receipt_schema(connection)
    item_revision_id = _snapshot_item_revision(connection, item_id, now_ns)
    duplicate = connection.execute(
        """SELECT revision.ordinal
        FROM semantic_chunk_revisions revision
        JOIN semantic_chunk_derivations derivation
          ON derivation.chunk_revision_id=revision.chunk_revision_id
        WHERE derivation.item_revision_id=? AND derivation.refresh_token=?
          AND revision.item_id=? AND revision.chunking_signature=?
        GROUP BY revision.ordinal HAVING COUNT(*)>1 LIMIT 1""",
        (item_revision_id, refresh_token, item_id, chunking_signature),
    ).fetchone()
    if duplicate is not None:
        raise ValueError(f"refresh contains duplicate chunk ordinal {int(duplicate['ordinal'])}")
    rows = connection.execute(
        """SELECT r.*,d.derivation_id
        FROM semantic_chunk_revisions r
        JOIN semantic_chunk_derivations d
          ON d.chunk_revision_id=r.chunk_revision_id
        WHERE d.item_revision_id=? AND d.refresh_token=?
          AND r.item_id=? AND r.chunking_signature=?
        ORDER BY r.ordinal,r.chunk_id""",
        (
            item_revision_id,
            refresh_token,
            item_id,
            chunking_signature,
        ),
    )
    manifest, chunk_count, members_fingerprint = _record_manifest_tree(
        connection,
        stage_id=SEMANTIC_CHUNK_MANIFEST_STAGE,
        processing_signature="semantic-chunk-manifest-v1",
        scope=(item_revision_id, chunking_signature, refresh_token),
        bindings=(
            _chunk_revision_binding_from_row(
                row,
                schema_version=schema_version,
            )
            for row in rows
        ),
        item_revision_id=item_revision_id,
        generation_id=None,
        now_ns=now_ns,
    )
    receipt_key = _stable_key(
        SEMANTIC_CHUNK_PUBLICATION_STAGE,
        (item_revision_id, chunking_signature, refresh_token),
    )
    receipt_id = _record_work_receipt(
        connection,
        receipt_key=receipt_key,
        stage_id=SEMANTIC_CHUNK_PUBLICATION_STAGE,
        stage_version="owner-refresh-v1",
        processing_signature=chunking_signature,
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="environment_bound",
        entity_kind="text_chunk_set",
        entity_id=f"{item_id}:{chunking_signature}",
        inputs=tuple(
            binding
            for binding in (
                _item_revision_binding(connection, item_revision_id),
                manifest,
            )
            if binding is not None
        ),
        outputs=(
            _chunk_set_output_binding(
                item_id=item_id,
                item_revision_id=item_revision_id,
                chunking_signature=chunking_signature,
                refresh_token=refresh_token,
                chunk_count=chunk_count,
                members_fingerprint=members_fingerprint,
            ),
        ),
        effective_config={"chunking_signature": chunking_signature},
        provider={"provider": "neocortex", "component": "owner-refresh"},
        item_revision_id=item_revision_id,
        chunk_revision_id=None,
        generation_id=None,
        model_signature=None,
        payload_id=None,
        job_id=None,
        attempt=1,
        started_ns=None,
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=None,
        event_kind="semantic_chunk_set_published",
        aggregate_kind="semantic_item",
        aggregate_id=item_id,
        committed_ns=now_ns,
    )
    conflicting_publication = connection.execute(
        """SELECT derivation_id FROM semantic_chunk_derivations
        WHERE item_revision_id=? AND refresh_token=?
          AND chunk_revision_id IN (
            SELECT r.chunk_revision_id FROM semantic_chunk_revisions r
            WHERE r.item_id=? AND r.chunking_signature=?)
          AND publication_receipt_id IS NOT NULL
          AND publication_receipt_id<>? LIMIT 1""",
        (
            item_revision_id,
            refresh_token,
            item_id,
            chunking_signature,
            receipt_id,
        ),
    ).fetchone()
    if conflicting_publication is not None:
        raise SemanticStateError(
            "semantic chunk derivation is bound to another publication receipt"
        )
    connection.execute(
        """UPDATE semantic_chunk_derivations SET publication_receipt_id=?
        WHERE item_revision_id=? AND refresh_token=? AND chunk_revision_id IN (
            SELECT r.chunk_revision_id FROM semantic_chunk_revisions r
            WHERE r.item_id=? AND r.chunking_signature=?)
          AND publication_receipt_id IS NULL""",
        (
            receipt_id,
            item_revision_id,
            refresh_token,
            item_id,
            chunking_signature,
        ),
    )
    return receipt_id


def _payload_binding(
    connection: sqlite3.Connection,
    payload_id: int,
    *,
    schema_version: int | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    selected_schema_version = (
        _require_current_receipt_schema(connection)
        if schema_version is None
        else schema_version
    )
    if selected_schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic payload materialization schema {selected_schema_version!r} is unsupported"
        )
    row = connection.execute(
        """SELECT p.*,m.model_id,m.model_version,m.provider,m.vector_space,
            m.dimensions AS model_dimensions,
            m.vector_dtype AS model_vector_dtype,
            m.normalization AS model_normalization,
            m.distance AS model_distance,
            space.dimensions AS space_dimensions,
            space.normalization AS space_normalization,
            space.distance AS space_distance
        FROM vector_payloads p JOIN embedding_models m
          ON m.model_signature=p.model_signature
        JOIN vector_spaces space ON space.vector_space=m.vector_space
        WHERE p.payload_id=?""",
        (payload_id,),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic vector payload disappeared")
    try:
        dtype = VectorDType(str(row["vector_dtype"]))
        decoded = decode_vector(
            bytes(row["vector_blob"]),
            int(row["dimensions"]),
            dtype,
        )
    except (TypeError, ValueError) as exc:
        raise SemanticStateError("semantic vector payload failed physical validation") from exc
    decoded_finite = bool(decoded) and all(math.isfinite(value) for value in decoded)
    decoded_norm = (
        math.sqrt(math.fsum(value * value for value in decoded)) if decoded_finite else math.inf
    )
    normalization_tolerance = 1e-2 if dtype is VectorDType.FLOAT16 else 1e-5
    if (
        int(row["dimensions"]) != int(row["model_dimensions"])
        or int(row["dimensions"]) != int(row["space_dimensions"])
        or str(row["vector_dtype"]) != str(row["model_vector_dtype"])
        or str(row["model_normalization"]) != str(row["space_normalization"])
        or str(row["model_distance"]) != str(row["space_distance"])
        or not decoded_finite
        or not math.isfinite(decoded_norm)
        or abs(decoded_norm - 1.0) > normalization_tolerance
        or not math.isfinite(float(row["original_norm"]))
        or float(row["original_norm"]) <= 0.0
    ):
        raise SemanticStateError("semantic vector payload contradicts its model or vector space")
    output_fingerprint = fingerprint_bytes(bytes(row["vector_blob"]))
    revision = RevisionRef(
        resource_id=(
            "resource:semantic:vector-payload:"
            + xxhash.xxh3_128_hexdigest(
                f"{row['model_signature']}\0{row['content_xxh3_128']}".encode("utf-8")
            )
        ),
        revision_id=f"revision:semantic:vector-payload:{payload_id}",
        producer="semantic.embedding.payload",
        processing_signature=str(row["model_signature"]),
        generation=payload_id,
        state=RevisionState.CURRENT,
        observed_at_utc=_utc_from_ns(int(row["created_ns"])),
    )
    materialization = MaterializationRef(
        owner="semantic",
        kind="semantic_vector_payload",
        materialization_id=f"materialization:semantic:vector-payload:{payload_id}",
        schema_version=selected_schema_version,
        revision=revision,
        generation=payload_id,
    )
    output: dict[str, object] = {
        "kind": "semantic_vector_payload",
        "payload_id": payload_id,
        "binding_name": "semantic_vector_payload",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": "xxh3-128+bytes+xxh3-64-guard",
            "xxh3_128": output_fingerprint.xxh3_128,
            "byte_count": output_fingerprint.byte_count,
            "xxh3_64_guard": output_fingerprint.xxh3_64_guard,
        },
        "dimensions": int(row["dimensions"]),
        "dtype": str(row["vector_dtype"]),
    }
    provider: dict[str, object] = {
        "provider": str(row["provider"]),
        "model_id": str(row["model_id"]),
        "model_version": str(row["model_version"]),
        "model_signature": str(row["model_signature"]),
        "vector_space": str(row["vector_space"]),
    }
    return output, provider


def _embedding_member_binding(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    entity_kind: str,
    entity_id: str,
    schema_version: int | None = None,
) -> dict[str, object]:
    selected_schema_version = (
        _require_current_receipt_schema(connection)
        if schema_version is None
        else schema_version
    )
    row = connection.execute(
        """SELECT member.*,g.processing_signature,
            payload.dimensions,payload.vector_dtype,payload.original_norm,
            payload.content_xxh3_128 AS payload_xxh3_128,
            payload.content_bytes AS payload_bytes,
            payload.content_xxh3_64_guard AS payload_xxh3_64_guard
        FROM embedding_generation_members member
        JOIN embedding_generations g ON g.generation_id=member.generation_id
        JOIN vector_payloads payload ON payload.payload_id=member.payload_id
        WHERE member.generation_id=? AND member.entity_kind=?
          AND member.entity_id=?""",
        (generation_id, entity_kind, entity_id),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic embedding generation member disappeared")
    return _embedding_member_binding_from_row(
        row,
        schema_version=selected_schema_version,
    )


def _embedding_member_binding_from_row(
    row: sqlite3.Row,
    *,
    schema_version: int,
) -> dict[str, object]:
    if schema_version not in _RECEIPT_SCHEMA_VERSIONS:
        raise SemanticStateError(
            f"semantic embedding member schema {schema_version!r} is unsupported"
        )
    member_id = int(row["member_id"])
    generation_id = int(row["generation_id"])
    member_contract = canonical_json(
        {
            "entity_kind": str(row["entity_kind"]),
            "entity_id": str(row["entity_id"]),
            "item_id": str(row["item_id"]),
            "item_revision_id": int(row["item_revision_id"]),
            "chunk_revision_id": (
                None if row["chunk_revision_id"] is None else int(row["chunk_revision_id"])
            ),
            "payload_id": int(row["payload_id"]),
            "model_signature": str(row["model_signature"]),
            "content_xxh3_128": str(row["content_xxh3_128"]),
            "content_bytes": int(row["content_bytes"]),
            "content_xxh3_64_guard": str(row["content_xxh3_64_guard"]),
            "provenance_json": str(row["provenance_json"]),
            "base_member_id": (
                None if row["base_member_id"] is None else int(row["base_member_id"])
            ),
            "payload_dimensions": int(row["dimensions"]),
            "payload_vector_dtype": str(row["vector_dtype"]),
            "payload_original_norm": float(row["original_norm"]),
            "payload_xxh3_128": str(row["payload_xxh3_128"]),
            "payload_bytes": int(row["payload_bytes"]),
            "payload_xxh3_64_guard": str(row["payload_xxh3_64_guard"]),
        }
    )
    fingerprint = xxhash.xxh3_128_hexdigest(member_contract.encode("utf-8"))
    revision = RevisionRef(
        resource_id=(
            "resource:semantic:embedding-member:"
            + xxhash.xxh3_128_hexdigest(
                f"{generation_id}\0{row['entity_kind']}\0{row['entity_id']}".encode("utf-8")
            )
        ),
        revision_id=f"revision:semantic:embedding-member:{member_id}",
        producer=SEMANTIC_EMBEDDING_STAGE,
        processing_signature=str(row["processing_signature"]),
        generation=generation_id,
        state=RevisionState.CURRENT,
        observed_at_utc=_utc_from_ns(int(row["updated_ns"])),
    )
    materialization = MaterializationRef(
        owner="semantic",
        kind="semantic_embedding_member",
        materialization_id=f"materialization:semantic:embedding-member:{member_id}",
        schema_version=schema_version,
        revision=revision,
        generation=generation_id,
    )
    return {
        "kind": "semantic_embedding_member",
        "generation_id": generation_id,
        "materialization_id": materialization.materialization_id,
        "binding_name": "semantic_embedding_member",
        "revision_ref": revision,
        "materialization_ref": materialization,
        "fingerprint": {
            "algorithm": "semantic-embedding-member-contract-xxh3-128-v1",
            "xxh3_128": fingerprint,
        },
    }


def _embedding_member_rows(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    member_ids: Sequence[int],
) -> tuple[sqlite3.Row, ...]:
    if not member_ids:
        return ()
    placeholders = ",".join("?" for _ in member_ids)
    return tuple(
        connection.execute(
            f"""SELECT member.*,g.processing_signature,
                payload.dimensions,payload.vector_dtype,payload.original_norm,
                payload.content_xxh3_128 AS payload_xxh3_128,
                payload.content_bytes AS payload_bytes,
                payload.content_xxh3_64_guard AS payload_xxh3_64_guard
            FROM embedding_generation_members member
            JOIN embedding_generations g ON g.generation_id=member.generation_id
            JOIN vector_payloads payload ON payload.payload_id=member.payload_id
            WHERE member.generation_id=?
              AND member.member_id IN ({placeholders})
            ORDER BY member.member_id""",
            (generation_id, *member_ids),
        ).fetchall()
    )


def _embedding_member_binding_by_id(
    connection: sqlite3.Connection,
    member_id: int,
    *,
    schema_version: int | None = None,
) -> dict[str, object]:
    selected_schema_version = (
        _require_current_receipt_schema(connection)
        if schema_version is None
        else schema_version
    )
    selected = connection.execute(
        """SELECT generation_id FROM embedding_generation_members
        WHERE member_id=?""",
        (member_id,),
    ).fetchone()
    if selected is None:
        raise SemanticStateError("semantic source embedding member disappeared")
    rows = _embedding_member_rows(
        connection,
        generation_id=int(selected["generation_id"]),
        member_ids=(member_id,),
    )
    if len(rows) != 1:
        raise SemanticStateError("semantic source embedding member is ambiguous")
    return _embedding_member_binding_from_row(
        rows[0],
        schema_version=selected_schema_version,
    )


def _record_embedding_clone_batch(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    base_generation_id: int,
    base_member_ids: Sequence[int],
    now_ns: int,
) -> int:
    """Record one bounded, set-loaded replay page instead of per-member events."""

    schema_version = _require_current_receipt_schema(connection)
    if not base_member_ids:
        raise ValueError("embedding clone receipt requires at least one base member")
    if len(base_member_ids) > _MAX_MANIFEST_INPUTS:
        last_receipt_id = 0
        for offset in range(0, len(base_member_ids), _MAX_MANIFEST_INPUTS):
            receipt_id = _record_embedding_clone_batch(
                connection,
                generation_id=generation_id,
                base_generation_id=base_generation_id,
                base_member_ids=base_member_ids[offset : offset + _MAX_MANIFEST_INPUTS],
                now_ns=now_ns,
            )
            if receipt_id:
                last_receipt_id = receipt_id
        return last_receipt_id
    placeholders = ",".join("?" for _ in base_member_ids)
    cloned_pairs = tuple(
        (int(row[0]), int(row[1]))
        for row in connection.execute(
            f"""SELECT member_id,base_member_id FROM embedding_generation_members
            WHERE generation_id=? AND base_member_id IN ({placeholders})
            ORDER BY member_id""",
            (generation_id, *base_member_ids),
        )
    )
    if not cloned_pairs:
        return 0
    cloned_ids = tuple(member_id for member_id, _base_member_id in cloned_pairs)
    selected_base_ids = tuple(base_member_id for _member_id, base_member_id in cloned_pairs)
    base_rows = _embedding_member_rows(
        connection,
        generation_id=base_generation_id,
        member_ids=selected_base_ids,
    )
    cloned_rows = _embedding_member_rows(
        connection,
        generation_id=generation_id,
        member_ids=cloned_ids,
    )
    if len(base_rows) != len(cloned_pairs) or len(cloned_rows) != len(cloned_pairs):
        raise SemanticStateError("embedding clone page changed before receipt publication")
    generation = connection.execute(
        """SELECT g.processing_signature,g.provenance_json,g.model_signature,
            m.model_id,m.model_version,m.provider,m.vector_space
        FROM embedding_generations g JOIN embedding_models m
          ON m.model_signature=g.model_signature
        WHERE g.generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise SemanticStateError("embedding clone generation disappeared")
    inputs = tuple(
        _renamed_binding(
            _embedding_member_binding_from_row(
                row,
                schema_version=schema_version,
            ),
            f"base_member:{index}",
        )
        for index, row in enumerate(base_rows)
    )
    outputs = tuple(
        _embedding_member_binding_from_row(
            row,
            schema_version=schema_version,
        )
        for row in cloned_rows
    )
    causal_digest = xxhash.xxh3_128_hexdigest(
        canonical_json(
            {
                "inputs": [_manifest_binding_fact(binding) for binding in inputs],
                "outputs": [_manifest_binding_fact(binding) for binding in outputs],
            }
        ).encode("utf-8")
    )
    return _record_work_receipt(
        connection,
        receipt_key=_stable_key(
            SEMANTIC_EMBEDDING_CLONE_STAGE,
            (generation_id, base_generation_id, causal_digest),
        ),
        stage_id=SEMANTIC_EMBEDDING_CLONE_STAGE,
        stage_version="semantic-embedding-clone-v1",
        processing_signature=str(generation["processing_signature"]),
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="environment_bound",
        entity_kind="embedding_clone_page",
        entity_id=f"{generation_id}:{causal_digest}",
        inputs=inputs,
        outputs=outputs,
        effective_config={
            "base_generation_id": base_generation_id,
            "member_count": len(outputs),
        },
        provider={
            "provider": str(generation["provider"]),
            "model_id": str(generation["model_id"]),
            "model_version": str(generation["model_version"]),
            "model_signature": str(generation["model_signature"]),
            "vector_space": str(generation["vector_space"]),
        },
        item_revision_id=None,
        chunk_revision_id=None,
        generation_id=generation_id,
        model_signature=str(generation["model_signature"]),
        payload_id=None,
        job_id=None,
        attempt=1,
        started_ns=None,
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=None,
        event_kind="semantic_embedding_clone_page_replayed",
        aggregate_kind="embedding_generation",
        aggregate_id=str(generation_id),
        committed_ns=now_ns,
    )


def _record_embedding_receipt(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    entity_kind: str,
    entity_id: str,
    item_revision_id: int,
    chunk_revision_id: int | None,
    model_signature: str,
    payload_id: int,
    job_id: int | None,
    attempt: int | None,
    execution_mode: str,
    now_ns: int,
    source_member_id: int | None = None,
    source_member_binding: Mapping[str, object] | None = None,
    source_causation_receipt_id: int | None = None,
    payload_causation_receipt_id: int | None = None,
) -> int:
    schema_version = _require_current_receipt_schema(connection)
    generation = connection.execute(
        """SELECT processing_signature,provenance_json
        FROM embedding_generations WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise SemanticStateError("embedding generation disappeared")
    output, provider = _payload_binding(
        connection,
        payload_id,
        schema_version=schema_version,
    )
    member_output = _embedding_member_binding(
        connection,
        generation_id=generation_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        schema_version=schema_version,
    )
    inputs = [
        _item_revision_binding(
            connection,
            item_revision_id,
            schema_version=schema_version,
        )
    ]
    if chunk_revision_id is not None:
        inputs.append(
            _chunk_revision_binding(
                connection,
                chunk_revision_id,
                schema_version=schema_version,
            )
        )
    causation = None
    causal_payload_binding: Mapping[str, object] = output
    if execution_mode in {"cache_hit", "replay"}:
        # A cache hit may reuse a payload whose producer receipt predates the
        # current owner schema.  Keep that exact causal locator rather than
        # manufacturing a v8 locator for a historical source.
        if execution_mode == "replay":
            inputs.append(_renamed_binding(output, "reused_vector_payload"))
    if execution_mode == "cache_hit":
        causation = (
            _payload_causation_receipt(
                connection,
                payload_id,
                now_ns=now_ns,
            )
            if payload_causation_receipt_id is None
            else payload_causation_receipt_id
        )
        cause_receipt, _cause_row = _validated_semantic_receipts(
            connection,
            (causation,),
        )[causation]
        payload_outputs = tuple(
            candidate
            for candidate in cause_receipt.outputs
            if candidate.materialization.kind == "semantic_vector_payload"
        )
        if len(payload_outputs) == 1:
            causal_payload_binding = _input_mapping_from_output(
                payload_outputs[0],
                name="reused_vector_payload",
            )
        else:
            payload_inputs = tuple(
                candidate
                for candidate in cause_receipt.inputs
                if candidate.name == "legacy_vector_payload"
            )
            if len(payload_inputs) != 1:
                raise SemanticStateError(
                    "semantic cached embedding lacks one exact payload causation"
                )
            causal_payload_binding = _input_mapping_from_binding(
                payload_inputs[0],
                name="reused_vector_payload",
            )
        inputs.append(_renamed_binding(causal_payload_binding, "reused_vector_payload"))
        if cause_receipt.stage.stage_id == SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE:
            if len(cause_receipt.outputs) != 1:
                raise SemanticStateError("semantic legacy payload attestation output is ambiguous")
            inputs.append(
                _input_mapping_from_output(
                    cause_receipt.outputs[0],
                    name="legacy_payload_attestation",
                )
            )
    selected_source_binding = source_member_binding
    if selected_source_binding is not None and source_member_id is None:
        raise SemanticStateError(
            "semantic source member binding requires its immutable member identifier"
        )
    if source_member_id is not None:
        if selected_source_binding is None:
            selected_source_binding = _embedding_member_binding_by_id(
                connection,
                source_member_id,
                schema_version=schema_version,
            )
        if execution_mode == "replay" and source_causation_receipt_id is not None:
            source_receipt, _source_receipt_row = _validated_semantic_receipts(
                connection,
                (source_causation_receipt_id,),
            )[source_causation_receipt_id]
            source_materialization = selected_source_binding.get("materialization_ref")
            if not isinstance(source_materialization, MaterializationRef):
                raise SemanticStateError(
                    "semantic replay source member has no exact materialization"
                )
            source_outputs = tuple(
                candidate
                for candidate in source_receipt.outputs
                if candidate.materialization.materialization_id
                == source_materialization.materialization_id
            )
            if len(source_outputs) != 1:
                raise SemanticStateError(
                    "semantic replay source member causation is not exact"
                )
            selected_source_binding = _input_mapping_from_output(
                source_outputs[0],
                name="source_embedding_member",
            )
        inputs.append(
            _renamed_binding(
                selected_source_binding,
                "source_embedding_member",
            )
        )
    if execution_mode == "replay":
        if source_member_id is None or selected_source_binding is None:
            raise SemanticStateError("semantic replay receipt is missing its source member")
        source_materialization = selected_source_binding.get("materialization_ref")
        if not isinstance(source_materialization, MaterializationRef):
            raise SemanticStateError("semantic replay source member has no exact materialization")
        if source_causation_receipt_id is None:
            raise SemanticStateError("semantic replay source member has no producer receipt")
        causation = source_causation_receipt_id
    elif execution_mode == "executed" and chunk_revision_id is not None:
        cause = connection.execute(
            """SELECT materialization_receipt_id
            FROM semantic_chunk_derivations
            WHERE chunk_revision_id=? AND item_revision_id=?
            ORDER BY derivation_id DESC LIMIT 1""",
            (chunk_revision_id, item_revision_id),
        ).fetchone()
        if cause is not None:
            causation = int(cause[0])
    receipt_identity: tuple[object, ...] = (
        generation_id,
        entity_kind,
        entity_id,
        execution_mode,
        job_id,
        attempt,
        source_member_id,
    )
    if execution_mode == "cache_hit":
        materialization = member_output.get("materialization_ref")
        if not isinstance(materialization, MaterializationRef):
            raise SemanticStateError("semantic cached member has no exact materialization")
        receipt_identity = (
            *receipt_identity,
            item_revision_id,
            chunk_revision_id,
            materialization.materialization_id,
        )
    receipt_key = _stable_key(SEMANTIC_EMBEDDING_STAGE, receipt_identity)
    started_ns = None
    if job_id is not None:
        job = connection.execute(
            "SELECT attempt_started_ns FROM embedding_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if job is not None and job["attempt_started_ns"] is not None:
            started_ns = int(job["attempt_started_ns"])
    return _record_work_receipt(
        connection,
        receipt_key=receipt_key,
        stage_id=SEMANTIC_EMBEDDING_STAGE,
        stage_version="semantic-embedding-v1",
        processing_signature=str(generation["processing_signature"]),
        status="succeeded",
        execution_mode=execution_mode,
        reproducibility_class="environment_bound",
        entity_kind=("text_embedding" if entity_kind == "text_chunk" else "image_embedding"),
        entity_id=entity_id,
        inputs=inputs,
        outputs=((output, member_output) if execution_mode == "executed" else (member_output,)),
        effective_config=_json_object(
            generation["provenance_json"],
            label="embedding generation provenance",
        ),
        provider=provider,
        item_revision_id=item_revision_id,
        chunk_revision_id=chunk_revision_id,
        generation_id=generation_id,
        model_signature=model_signature,
        payload_id=payload_id,
        job_id=job_id,
        attempt=attempt,
        started_ns=started_ns,
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=causation,
        event_kind="semantic_embedding_materialized",
        aggregate_kind=entity_kind,
        aggregate_id=entity_id,
        committed_ns=now_ns,
    )


def _record_discarded_embedding_execution(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    incumbent_payload_id: int,
    candidate_vector_blob: bytes,
    dimensions: int,
    vector_dtype: str,
    original_norm: float,
    now_ns: int,
) -> int:
    """Record a provider invocation whose duplicate result was not published."""

    generation_id = int(row["generation_id"])
    schema_version = _require_current_receipt_schema(connection)
    generation = connection.execute(
        """SELECT processing_signature FROM embedding_generations
        WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise SemanticStateError("embedding generation disappeared")
    incumbent, provider = _payload_binding(
        connection,
        incumbent_payload_id,
        schema_version=schema_version,
    )
    item_revision_id = (
        int(row["input_item_revision_id"])
        if row["input_item_revision_id"] is not None
        else _snapshot_item_revision(connection, str(row["item_id"]), now_ns)
    )
    chunk_revision_id = (
        None if row["input_chunk_revision_id"] is None else int(row["input_chunk_revision_id"])
    )
    inputs = [
        _item_revision_binding(
            connection,
            item_revision_id,
            schema_version=schema_version,
        )
    ]
    if chunk_revision_id is not None:
        inputs.append(
            _chunk_revision_binding(
                connection,
                chunk_revision_id,
                schema_version=schema_version,
            )
        )
    job_id = int(row["job_id"])
    attempt = max(1, int(row["attempt_sequence"]))
    candidate_fingerprint = fingerprint_bytes(candidate_vector_blob)
    observation_payload = canonical_json(
        {
            "attempt": attempt,
            "candidate_vector_bytes": candidate_fingerprint.byte_count,
            "candidate_vector_xxh3_128": candidate_fingerprint.xxh3_128,
            "candidate_vector_xxh3_64_guard": candidate_fingerprint.xxh3_64_guard,
            "dimensions": dimensions,
            "disposition": "discarded_duplicate_content_payload",
            "incumbent_payload_id": incumbent_payload_id,
            "job_id": job_id,
            "original_norm": original_norm,
            "vector_dtype": vector_dtype,
        }
    )
    observation_revision = RevisionRef(
        resource_id=f"resource:semantic:provider-execution:{job_id}:{attempt}",
        revision_id=f"revision:semantic:provider-execution:{job_id}:{attempt}",
        producer=SEMANTIC_EMBEDDING_DISCARD_STAGE,
        processing_signature=str(generation["processing_signature"]),
        generation=generation_id,
        state=RevisionState.CURRENT,
        observed_at_utc=_utc_from_ns(now_ns),
    )
    observation_materialization = MaterializationRef(
        owner="semantic",
        kind="provider_execution_observation",
        materialization_id=(f"materialization:semantic:provider-execution:{job_id}:{attempt}"),
        schema_version=schema_version,
        revision=observation_revision,
        generation=generation_id,
    )
    output = {
        "kind": "provider_execution_observation",
        "binding_name": "discarded_provider_execution",
        "materialization_ref": observation_materialization,
        "fingerprint": {
            "algorithm": "xxh3-128+bytes+xxh3-64-guard",
            "xxh3_128": candidate_fingerprint.xxh3_128,
            "byte_count": candidate_fingerprint.byte_count,
            "xxh3_64_guard": candidate_fingerprint.xxh3_64_guard,
        },
    }
    cause = _payload_causation_receipt(
        connection,
        incumbent_payload_id,
        now_ns=now_ns,
    )
    cause_receipt, _cause_row = _validated_semantic_receipts(
        connection,
        (cause,),
    )[cause]
    causal_incumbent_binding: Mapping[str, object] = incumbent
    payload_outputs = tuple(
        candidate
        for candidate in cause_receipt.outputs
        if candidate.materialization.kind == "semantic_vector_payload"
    )
    if len(payload_outputs) == 1:
        causal_incumbent_binding = _input_mapping_from_output(
            payload_outputs[0],
            name="incumbent_vector_payload",
        )
    else:
        payload_inputs = tuple(
            candidate
            for candidate in cause_receipt.inputs
            if candidate.name == "legacy_vector_payload"
        )
        if len(payload_inputs) != 1:
            raise SemanticStateError(
                "semantic discarded execution lacks one exact incumbent causation"
            )
        causal_incumbent_binding = _input_mapping_from_binding(
            payload_inputs[0],
            name="incumbent_vector_payload",
        )
    inputs.append(_renamed_binding(causal_incumbent_binding, "incumbent_vector_payload"))
    if cause_receipt.stage.stage_id == SEMANTIC_LEGACY_PAYLOAD_ATTESTATION_STAGE:
        if len(cause_receipt.outputs) != 1:
            raise SemanticStateError("semantic legacy payload attestation output is ambiguous")
        inputs.append(
            _input_mapping_from_output(
                cause_receipt.outputs[0],
                name="legacy_payload_attestation",
            )
        )
    return _record_work_receipt(
        connection,
        receipt_key=_stable_key(
            SEMANTIC_EMBEDDING_DISCARD_STAGE,
            (generation_id, job_id, attempt),
        ),
        stage_id=SEMANTIC_EMBEDDING_DISCARD_STAGE,
        stage_version="semantic-provider-execution-discard-v1",
        processing_signature=str(generation["processing_signature"]),
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="non_replayable",
        entity_kind="provider_execution_observation",
        entity_id=f"{job_id}:{attempt}",
        inputs=inputs,
        outputs=(output,),
        effective_config={
            "disposition": "discarded_duplicate_content_payload",
            "incumbent_payload_id": incumbent_payload_id,
            "observation_contract_xxh3_128": xxhash.xxh3_128_hexdigest(
                observation_payload.encode("utf-8")
            ),
        },
        provider=provider,
        item_revision_id=item_revision_id,
        chunk_revision_id=chunk_revision_id,
        generation_id=generation_id,
        model_signature=str(row["model_signature"]),
        payload_id=None,
        job_id=job_id,
        attempt=attempt,
        started_ns=(None if row["attempt_started_ns"] is None else int(row["attempt_started_ns"])),
        finished_ns=now_ns,
        error=None,
        causation_receipt_id=cause,
        event_kind="semantic_embedding_execution_discarded",
        aggregate_kind="embedding_job",
        aggregate_id=str(job_id),
        committed_ns=now_ns,
    )


def _record_embedding_attempt_failure(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    status: str,
    error_type: str,
    error_message: str,
    retryable: bool,
    now_ns: int,
) -> int:
    generation = connection.execute(
        """SELECT processing_signature,provenance_json
        FROM embedding_generations WHERE generation_id=?""",
        (int(row["generation_id"]),),
    ).fetchone()
    if generation is None:
        raise SemanticStateError("embedding generation disappeared")
    model = connection.execute(
        """SELECT model_id,model_version,provider,vector_space
        FROM embedding_models WHERE model_signature=?""",
        (str(row["model_signature"]),),
    ).fetchone()
    if model is None:
        raise SemanticStateError("embedding model disappeared")
    attempt = max(1, int(row["attempt_sequence"]))
    job_id = int(row["job_id"])
    inputs: list[dict[str, object]] = []
    if row["input_item_revision_id"] is not None:
        inputs.append(_item_revision_binding(connection, int(row["input_item_revision_id"])))
    if row["input_chunk_revision_id"] is not None:
        inputs.append(_chunk_revision_binding(connection, int(row["input_chunk_revision_id"])))
    if not inputs:
        fingerprint_value = (
            f"{row['content_xxh3_128']};bytes={int(row['content_bytes'])};"
            f"xxh3-64-guard={row['content_xxh3_64_guard']}"
        )
        entity_kind = str(row["entity_kind"])
        entity_id = str(row["entity_id"])
        resource_digest = xxhash.xxh3_128_hexdigest(f"{entity_kind}\0{entity_id}".encode("utf-8"))
        inputs.append(
            {
                "kind": f"{entity_kind}_job_input",
                "entity_id": entity_id,
                "binding_name": "immutable_job_input",
                "revision_ref": RevisionRef(
                    resource_id=(f"resource:semantic:{entity_kind}:{resource_digest}"),
                    revision_id=(
                        f"revision:semantic:{entity_kind}:fingerprint:{row['content_xxh3_128']}"
                    ),
                    producer="semantic.embedding.job",
                    processing_signature="semantic-embedding-job-input-v1",
                    generation=None,
                    state=RevisionState.CURRENT,
                    observed_at_utc=None,
                ),
                "fingerprint": {
                    "algorithm": "xxh3-128+bytes+xxh3-64-guard",
                    "value": fingerprint_value,
                },
            }
        )
    receipt_key = _stable_key(
        SEMANTIC_EMBEDDING_STAGE,
        (
            "attempt",
            job_id,
            attempt,
            status,
            row["attempt_started_ns"],
            row["lease_owner"],
        ),
    )
    return _record_work_receipt(
        connection,
        receipt_key=receipt_key,
        stage_id=SEMANTIC_EMBEDDING_STAGE,
        stage_version="semantic-embedding-v1",
        processing_signature=str(generation["processing_signature"]),
        status=status,
        execution_mode=("unknown" if status == "abandoned" else "attempted"),
        reproducibility_class="environment_bound",
        entity_kind=str(row["entity_kind"]),
        entity_id=str(row["entity_id"]),
        inputs=tuple(inputs),
        outputs=(),
        effective_config=_json_object(
            generation["provenance_json"],
            label="embedding generation provenance",
        ),
        provider={
            "provider": str(model["provider"]),
            "model_id": str(model["model_id"]),
            "model_version": str(model["model_version"]),
            "model_signature": str(row["model_signature"]),
            "vector_space": str(model["vector_space"]),
        },
        item_revision_id=None,
        chunk_revision_id=None,
        generation_id=int(row["generation_id"]),
        model_signature=str(row["model_signature"]),
        payload_id=None,
        job_id=job_id,
        attempt=attempt,
        started_ns=(None if row["attempt_started_ns"] is None else int(row["attempt_started_ns"])),
        finished_ns=now_ns,
        error={
            "type": error_type,
            "message": error_message,
            "retryable": retryable,
        },
        causation_receipt_id=None,
        event_kind=f"semantic_work_{status}",
        aggregate_kind=str(row["entity_kind"]),
        aggregate_id=str(row["entity_id"]),
        committed_ns=now_ns,
    )


def _iter_embedding_member_bindings(
    connection: sqlite3.Connection,
    generation_id: int,
) -> Iterable[dict[str, object]]:
    schema_version = _require_current_receipt_schema(connection)
    rows = connection.execute(
        """SELECT member.*,g.processing_signature,
            payload.dimensions,payload.vector_dtype,
            payload.original_norm,payload.content_xxh3_128 AS payload_xxh3_128,
            payload.content_bytes AS payload_bytes,
            payload.content_xxh3_64_guard AS payload_xxh3_64_guard
        FROM embedding_generation_members member
        JOIN embedding_generations g ON g.generation_id=member.generation_id
        JOIN vector_payloads payload ON payload.payload_id=member.payload_id
        WHERE member.generation_id=?
        ORDER BY member.entity_kind,member.entity_id,member.member_id""",
        (generation_id,),
    )
    for row in rows:
        yield _embedding_member_binding_from_row(
            row,
            schema_version=schema_version,
        )


def _generation_candidate_binding(row: sqlite3.Row, generation_id: int) -> dict[str, object]:
    fingerprint = xxhash.xxh3_128_hexdigest(
        canonical_json(
            {
                "generation_id": generation_id,
                "model_signature": str(row["model_signature"]),
                "processing_signature": str(row["processing_signature"]),
                "base_generation_id": (
                    None if row["base_generation_id"] is None else int(row["base_generation_id"])
                ),
            }
        ).encode("utf-8")
    )
    return {
        "kind": "embedding_generation_candidate",
        "generation_id": generation_id,
        "binding_name": "embedding_generation_candidate",
        "revision_ref": RevisionRef(
            resource_id=f"resource:semantic:embedding-generation:{generation_id}",
            revision_id=f"revision:semantic:embedding-generation:{generation_id}",
            producer="semantic.embedding.generation",
            processing_signature=str(row["processing_signature"]),
            generation=generation_id,
            state=RevisionState.CURRENT,
            observed_at_utc=None,
        ),
        "fingerprint": {
            "algorithm": "semantic-generation-candidate-xxh3-128-v1",
            "xxh3_128": fingerprint,
        },
    }


def _record_generation_publication_receipt(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    published_ns: int,
) -> int:
    row = connection.execute(
        """SELECT g.processing_signature,g.provenance_json,g.model_signature,
            g.base_generation_id,m.model_id,m.model_version,m.provider,m.vector_space
        FROM embedding_generations g
        JOIN embedding_models m ON m.model_signature=g.model_signature
        WHERE g.generation_id=?""",
        (generation_id,),
    ).fetchone()
    if row is None:
        raise SemanticStateError("published embedding generation disappeared")
    receipt_key = _stable_key(
        SEMANTIC_GENERATION_PUBLICATION_STAGE,
        (generation_id, str(row["model_signature"])),
    )
    manifest, member_count, members_fingerprint = _record_manifest_tree(
        connection,
        stage_id=SEMANTIC_EMBEDDING_MANIFEST_STAGE,
        processing_signature="semantic-embedding-manifest-v1",
        scope=(generation_id, str(row["model_signature"])),
        bindings=_iter_embedding_member_bindings(connection, generation_id),
        item_revision_id=None,
        generation_id=generation_id,
        now_ns=published_ns,
    )
    generation_fingerprint = xxhash.xxh3_128_hexdigest(
        canonical_json(
            {
                "generation_id": generation_id,
                "model_signature": str(row["model_signature"]),
                "processing_signature": str(row["processing_signature"]),
                "base_generation_id": (
                    None if row["base_generation_id"] is None else int(row["base_generation_id"])
                ),
                "member_count": member_count,
                "members_xxh3_128": members_fingerprint,
            }
        ).encode("utf-8")
    )
    return _record_work_receipt(
        connection,
        receipt_key=receipt_key,
        stage_id=SEMANTIC_GENERATION_PUBLICATION_STAGE,
        stage_version="cas-head-v1",
        processing_signature=str(row["processing_signature"]),
        status="succeeded",
        execution_mode="executed",
        reproducibility_class="environment_bound",
        entity_kind="embedding_generation",
        entity_id=str(generation_id),
        inputs=tuple(
            binding
            for binding in (
                _generation_candidate_binding(row, generation_id),
                manifest,
            )
            if binding is not None
        ),
        outputs=(
            {
                "kind": "published_embedding_generation",
                "generation_id": generation_id,
                "model_signature": str(row["model_signature"]),
                "member_count": member_count,
                "fingerprint": {
                    "algorithm": "generation-contract-xxh3-128-v1",
                    "xxh3_128": generation_fingerprint,
                },
            },
        ),
        effective_config=_json_object(
            row["provenance_json"],
            label="embedding generation provenance",
        ),
        provider={
            "provider": str(row["provider"]),
            "model_id": str(row["model_id"]),
            "model_version": str(row["model_version"]),
            "model_signature": str(row["model_signature"]),
            "vector_space": str(row["vector_space"]),
        },
        item_revision_id=None,
        chunk_revision_id=None,
        generation_id=generation_id,
        model_signature=str(row["model_signature"]),
        payload_id=None,
        job_id=None,
        attempt=1,
        started_ns=None,
        finished_ns=published_ns,
        error=None,
        causation_receipt_id=None,
        event_kind="semantic_generation_published",
        aggregate_kind="embedding_generation",
        aggregate_id=str(generation_id),
        committed_ns=published_ns,
    )


def _query_text_chunk_embedding_rows(
    connection: sqlite3.Connection,
    *,
    version: int,
    chunk_id: str,
    model_signature: str | None,
    published_only: bool,
    limit: int,
) -> tuple[sqlite3.Row, ...]:
    """Read the bounded normalized embedding rows for one text chunk."""

    publication_join_kind = "JOIN" if published_only else "LEFT JOIN"
    publication_join = (
        f"{publication_join_kind} published_embedding_heads h "
        "ON h.generation_id=member.generation_id "
        "AND h.model_signature=member.model_signature "
        "AND g.model_signature=h.model_signature AND g.status='ready'"
    )
    model_filter = "" if model_signature is None else "AND member.model_signature=?"
    if version in _RECEIPT_LINEAGE_SCHEMA_VERSIONS:
        parameters: tuple[object, ...] = (
            (
                SEMANTIC_EMBEDDING_STAGE,
                chunk_id,
                SEMANTIC_EMBEDDING_CLONE_STAGE,
                chunk_id,
                limit,
            )
            if model_signature is None
            else (
                SEMANTIC_EMBEDDING_STAGE,
                chunk_id,
                SEMANTIC_EMBEDDING_CLONE_STAGE,
                chunk_id,
                model_signature,
                limit,
            )
        )
        rows = connection.execute(
            f"""WITH latest_receipt AS (
                SELECT generation_id,entity_id,payload_id,
                    MAX(receipt_id) AS receipt_id
                FROM semantic_work_receipts
                WHERE stage_id=? AND status='succeeded' AND entity_id=?
                GROUP BY generation_id,entity_id,payload_id)
            SELECT member.*,g.processing_signature,
                g.status AS generation_status,
                m.model_id,m.model_version,m.provider,m.vector_space,
                payload.dimensions,payload.vector_dtype,payload.original_norm,
                payload.content_xxh3_128 AS payload_xxh3_128,
                payload.content_bytes AS payload_bytes,
                payload.content_xxh3_64_guard AS payload_xxh3_64_guard,
                h.generation_id AS published_generation_id,
                COALESCE(receipt.receipt_id,clone_receipt.receipt_id)
                  AS work_receipt_id,
                COALESCE(receipt.stage_id,clone_receipt.stage_id)
                  AS derivation_stage_id,
                COALESCE(receipt.execution_mode,clone_receipt.execution_mode)
                  AS execution_mode,COUNT(*) OVER() AS total_count
            FROM embedding_generation_members member
            JOIN embedding_generations g
              ON g.generation_id=member.generation_id
            JOIN embedding_models m
              ON m.model_signature=member.model_signature
            JOIN vector_payloads payload ON payload.payload_id=member.payload_id
            {publication_join}
            LEFT JOIN latest_receipt selected_receipt
              ON selected_receipt.generation_id=member.generation_id
             AND selected_receipt.entity_id=member.entity_id
             AND selected_receipt.payload_id=member.payload_id
            LEFT JOIN semantic_work_receipts receipt
              ON receipt.receipt_id=selected_receipt.receipt_id
            LEFT JOIN semantic_work_receipts clone_receipt
              ON clone_receipt.generation_id=member.generation_id
             AND clone_receipt.stage_id=?
             AND EXISTS(
                SELECT 1 FROM json_each(clone_receipt.receipt_json,'$.outputs') output
                WHERE json_extract(
                    output.value,
                    '$.materialization.materialization_id'
                )='materialization:semantic:embedding-member:' || member.member_id)
            WHERE member.entity_kind='text_chunk' AND member.entity_id=?
              {model_filter}
            ORDER BY member.generation_id,member.member_id LIMIT ?""",
            parameters,
        ).fetchall()
    else:
        legacy_parameters: tuple[object, ...] = (
            (chunk_id, limit)
            if model_signature is None
            else (chunk_id, model_signature, limit)
        )
        rows = connection.execute(
            f"""SELECT member.*,g.processing_signature,
                g.status AS generation_status,
                m.model_id,m.model_version,m.provider,m.vector_space,
                h.generation_id AS published_generation_id,
                NULL AS work_receipt_id,NULL AS derivation_stage_id,
                NULL AS execution_mode,
                COUNT(*) OVER() AS total_count
            FROM embedding_generation_members member
            JOIN embedding_generations g
              ON g.generation_id=member.generation_id
            JOIN embedding_models m
              ON m.model_signature=member.model_signature
            {publication_join}
            WHERE member.entity_kind='text_chunk' AND member.entity_id=?
              {model_filter}
            ORDER BY member.generation_id,member.member_id LIMIT ?""",
            legacy_parameters,
        ).fetchall()
    return tuple(rows)


def _materialize_text_chunk_embedding_derivation(
    connection: sqlite3.Connection,
    *,
    member: sqlite3.Row,
    member_receipts: dict[int, tuple[WorkReceipt, sqlite3.Row]],
) -> SemanticEmbeddingDerivation:
    """Verify one normalized member and expose its durable derivation."""

    receipt_id = (
        None if member["work_receipt_id"] is None else int(member["work_receipt_id"])
    )
    mode = (
        "legacy_unattributed"
        if member["execution_mode"] is None
        else str(member["execution_mode"])
    )
    stage_id = (
        "legacy_unattributed"
        if member["derivation_stage_id"] is None
        else str(member["derivation_stage_id"])
    )
    if receipt_id is not None:
        receipt, receipt_row = member_receipts[receipt_id]
        receipt_schema_version = _receipt_schema_version(receipt)
        expected_member = _output_contracts(
            (
                _embedding_member_binding_from_row(
                    member,
                    schema_version=receipt_schema_version,
                ),
            ),
            generation_id=int(member["generation_id"]),
            schema_version=receipt_schema_version,
        )[0]
        member_output_matches = any(
            output.materialization == expected_member.materialization
            and output.fingerprint == expected_member.fingerprint
            and output.fingerprint_algorithm == expected_member.fingerprint_algorithm
            for output in receipt.outputs
        )
        normalized_member = (
            receipt.stage.stage_id == stage_id
            and receipt.outcome is WorkOutcome.SUCCEEDED
            and receipt.execution_mode.value == mode
            and int(receipt_row["generation_id"]) == int(member["generation_id"])
            and member_output_matches
        )
        if stage_id == SEMANTIC_EMBEDDING_STAGE:
            normalized_member = (
                normalized_member
                and str(receipt_row["entity_id"]) == str(member["entity_id"])
                and int(receipt_row["item_revision_id"])
                == int(member["item_revision_id"])
                and int(receipt_row["chunk_revision_id"])
                == int(member["chunk_revision_id"])
                and int(receipt_row["payload_id"]) == int(member["payload_id"])
                and str(receipt_row["model_signature"])
                == str(member["model_signature"])
            )
            if normalized_member:
                try:
                    _validate_embedding_receipt_contract(
                        connection,
                        receipt,
                        receipt_row,
                        member,
                    )
                except SemanticStateError as exc:
                    raise ValueError(
                        "semantic embedding physical facts are corrupt"
                    ) from exc
        elif stage_id == SEMANTIC_EMBEDDING_CLONE_STAGE:
            if normalized_member:
                _validate_embedding_clone_receipt_contract(
                    connection,
                    receipt,
                    receipt_row,
                )
        else:
            normalized_member = False
        if not normalized_member:
            raise ValueError(
                "semantic embedding receipt contradicts normalized member facts"
            )
    return SemanticEmbeddingDerivation(
        member_id=int(member["member_id"]),
        generation_id=int(member["generation_id"]),
        generation_status=str(member["generation_status"]),
        published=member["published_generation_id"] is not None,
        processing_signature=str(member["processing_signature"]),
        model_signature=str(member["model_signature"]),
        model_id=str(member["model_id"]),
        model_version=str(member["model_version"]),
        provider=str(member["provider"]),
        vector_space=str(member["vector_space"]),
        payload_id=int(member["payload_id"]),
        item_revision_id=int(member["item_revision_id"]),
        chunk_revision_id=int(member["chunk_revision_id"]),
        stage_id=stage_id,
        execution_mode=mode,
        receipt_id=receipt_id,
        lineage_status=(
            "recorded" if receipt_id is not None else "legacy_unattributed"
        ),
    )


def _read_text_chunk_embedding_derivations(
    connection: sqlite3.Connection,
    *,
    version: int,
    chunk_id: str,
    model_signature: str | None,
    published_only: bool,
    limit: int,
) -> tuple[tuple[SemanticEmbeddingDerivation, ...], int]:
    """Read and verify the bounded embedding side of one chunk lineage."""

    member_rows = _query_text_chunk_embedding_rows(
        connection,
        version=version,
        chunk_id=chunk_id,
        model_signature=model_signature,
        published_only=published_only,
        limit=limit,
    )
    member_receipts = (
        {}
        if version not in _RECEIPT_LINEAGE_SCHEMA_VERSIONS
        else _validated_semantic_receipts(
            connection,
            (
                int(member["work_receipt_id"])
                for member in member_rows
                if member["work_receipt_id"] is not None
            ),
        )
    )
    embedding_count = 0 if not member_rows else int(member_rows[0]["total_count"])
    return (
        tuple(
            _materialize_text_chunk_embedding_derivation(
                connection,
                member=member,
                member_receipts=member_receipts,
            )
            for member in member_rows
        ),
        embedding_count,
    )


def _query_text_chunk_origin_rows(
    connection: sqlite3.Connection,
    *,
    chunk_revision_id: int,
    refresh_token: str,
    limit: int,
) -> tuple[sqlite3.Row, ...]:
    """Read the bounded normalized origin rows for one chunk revision."""

    return tuple(
        connection.execute(
            """SELECT d.materialization_receipt_id,d.publication_receipt_id,
                d.refresh_token,
                i.item_revision_id,i.source_kind,i.source_identity,
                i.identity_version,i.source_revision_json,
                COUNT(*) OVER() AS total_count
            FROM semantic_chunk_derivations d
            JOIN semantic_item_revisions i
              ON i.item_revision_id=d.item_revision_id
            WHERE d.chunk_revision_id=?
            ORDER BY CASE WHEN d.refresh_token=? THEN 0 ELSE 1 END,
                d.derivation_id DESC LIMIT ?""",
            (chunk_revision_id, refresh_token, limit),
        ).fetchall()
    )


def _materialize_text_chunk_origin(
    connection: sqlite3.Connection,
    *,
    chunk: sqlite3.Row,
    chunk_revision_id: int,
    row: sqlite3.Row,
    origin_receipts: dict[int, tuple[WorkReceipt, sqlite3.Row]],
) -> SemanticChunkOrigin:
    """Verify one normalized chunk origin and expose its attribution."""

    published = row["publication_receipt_id"] is not None
    materialization_id = int(row["materialization_receipt_id"])
    materialization_receipt, materialization_row = origin_receipts[materialization_id]
    receipt_schema_version = _receipt_schema_version(materialization_receipt)
    item_revision_id = int(row["item_revision_id"])
    try:
        item_binding = _item_revision_binding(
            connection,
            item_revision_id,
            schema_version=receipt_schema_version,
        )
        native_binding = _native_source_revision_binding(connection, item_revision_id)
        expected_item = _input_contracts(
            (item_binding,),
            stage_id=SEMANTIC_CHUNK_STAGE,
            processing_signature=str(materialization_row["processing_signature"]),
            observed_ns=int(materialization_row["started_ns"]),
        )[0]
        expected_chunk = _output_contracts(
            (
                _chunk_revision_binding(
                    connection,
                    chunk_revision_id,
                    schema_version=receipt_schema_version,
                ),
            ),
            generation_id=None,
            schema_version=receipt_schema_version,
        )[0]
    except SemanticStateError as exc:
        raise ValueError(
            "semantic chunk origin contradicts its immutable receipt"
        ) from exc
    expected_origin_inputs = (
        (expected_item,)
        if native_binding is None
        else _input_contracts(
            (native_binding, item_binding),
            stage_id=SEMANTIC_CHUNK_STAGE,
            processing_signature=str(materialization_row["processing_signature"]),
            observed_ns=int(materialization_row["started_ns"]),
        )
    )
    normalized_origin = (
        materialization_receipt.stage.stage_id == SEMANTIC_CHUNK_STAGE
        and materialization_receipt.stage.stage_version
        == str(chunk["chunking_signature"]).partition("|")[0]
        and materialization_receipt.stage.processing_signature
        == str(chunk["chunking_signature"])
        and materialization_receipt.stage.provider == "neocortex"
        and materialization_receipt.stage.model is None
        and materialization_receipt.outcome is WorkOutcome.SUCCEEDED
        and materialization_receipt.execution_mode is WorkExecutionMode.EXECUTED
        and str(materialization_row["processing_signature"])
        == str(chunk["chunking_signature"])
        and str(materialization_row["entity_kind"]) == "text_chunk"
        and int(materialization_row["item_revision_id"]) == item_revision_id
        and int(materialization_row["chunk_revision_id"]) == chunk_revision_id
        and str(materialization_row["entity_id"]) == str(chunk["chunk_id"])
        and materialization_receipt.inputs == expected_origin_inputs
        and materialization_receipt.outputs == (expected_chunk,)
        and dict(materialization_receipt.effective_configuration)
        == {"chunking_signature": str(materialization_row["processing_signature"])}
        and materialization_receipt.causation_id is None
    )
    source_attributed = native_binding is not None
    if native_binding is not None:
        expected_native = expected_origin_inputs[0]
        if (
            str(row["source_kind"]) == "text"
            and expected_native.materialization is None
        ):
            source_attributed = False
    if published:
        publication_receipt, publication_row = origin_receipts[
            int(row["publication_receipt_id"])
        ]
        try:
            publication_verified = _validate_chunk_publication_receipt(
                connection,
                receipt=publication_receipt,
                receipt_row=publication_row,
                item_id=str(chunk["item_id"]),
                item_revision_id=item_revision_id,
                chunking_signature=str(chunk["chunking_signature"]),
                refresh_token=str(row["refresh_token"]),
            )
        except SemanticStateError as exc:
            raise ValueError(
                "semantic chunk publication physical facts are corrupt"
            ) from exc
        source_attributed = source_attributed and publication_verified
        normalized_origin = (
            normalized_origin
            and publication_receipt.stage.stage_id == SEMANTIC_CHUNK_PUBLICATION_STAGE
            and publication_receipt.outcome is WorkOutcome.SUCCEEDED
            and int(publication_row["item_revision_id"]) == item_revision_id
        )
    if not normalized_origin:
        raise ValueError("semantic chunk origin receipt contradicts normalized facts")
    return SemanticChunkOrigin(
        item_revision_id=item_revision_id,
        source_kind=str(row["source_kind"]),
        source_identity=str(row["source_identity"]),
        identity_version=str(row["identity_version"]),
        source_revision=_json_object(
            row["source_revision_json"],
            label="semantic item source revision",
        ),
        materialization_receipt_id=materialization_id,
        publication_receipt_id=(
            None
            if row["publication_receipt_id"] is None
            else int(row["publication_receipt_id"])
        ),
        refresh_token=str(row["refresh_token"]),
        published=published,
        lineage_status=(
            ("published" if source_attributed else "published_partially_unattributed")
            if published
            else "materialized_unpublished"
        ),
    )


def _read_text_chunk_origins(
    connection: sqlite3.Connection,
    *,
    version: int,
    chunk: sqlite3.Row,
    chunk_revision_id: int | None,
    limit: int,
) -> tuple[tuple[SemanticChunkOrigin, ...], int]:
    """Read and verify the bounded owner-native origins of one chunk."""

    if version not in _RECEIPT_LINEAGE_SCHEMA_VERSIONS or chunk_revision_id is None:
        return (), 0
    origin_rows = _query_text_chunk_origin_rows(
        connection,
        chunk_revision_id=chunk_revision_id,
        refresh_token=str(chunk["refresh_token"]),
        limit=limit,
    )
    origin_receipts = _validated_semantic_receipts(
        connection,
        (
            receipt_id
            for row in origin_rows
            for receipt_id in (
                int(row["materialization_receipt_id"]),
                (
                    None
                    if row["publication_receipt_id"] is None
                    else int(row["publication_receipt_id"])
                ),
            )
            if receipt_id is not None
        ),
    )
    origin_count = 0 if not origin_rows else int(origin_rows[0]["total_count"])
    return (
        tuple(
            _materialize_text_chunk_origin(
                connection,
                chunk=chunk,
                chunk_revision_id=chunk_revision_id,
                row=row,
                origin_receipts=origin_receipts,
            )
            for row in origin_rows
        ),
        origin_count,
    )


def explain_text_chunk_lineage(
    path: Path,
    *,
    chunk_id: str,
    model_signature: str | None = None,
    published_only: bool = True,
    origin_limit: int = 100,
    embedding_limit: int = 100,
) -> SemanticTextChunkLineage:
    """Explain one chunk without creating or migrating semantic state."""

    if not chunk_id.strip():
        raise ValueError("chunk_id cannot be blank")
    if not 1 <= origin_limit <= MAX_LINEAGE_ROWS:
        raise ValueError(f"origin_limit must be between 1 and {MAX_LINEAGE_ROWS}")
    if not 1 <= embedding_limit <= MAX_LINEAGE_ROWS:
        raise ValueError(f"embedding_limit must be between 1 and {MAX_LINEAGE_ROWS}")
    with semantic_database(path, readonly=True) as connection:
        version = _read_schema_version(connection)
        if version not in _LINEAGE_SCHEMA_VERSIONS:
            raise SemanticStateError(
                f"semantic lineage requires schema 6, 7, 8, 9 or 10; observed {version!r}"
            )
        _validate_version_contract(connection, version)
        chunk = connection.execute(
            """SELECT c.chunk_id,c.item_id,c.chunking_signature,c.refresh_token,
                r.chunk_revision_id
            FROM text_chunks c LEFT JOIN semantic_chunk_revisions r
              ON r.chunk_id=c.chunk_id
            WHERE c.chunk_id=?""",
            (chunk_id,),
        ).fetchone()
        if chunk is None:
            raise KeyError(f"unknown semantic text chunk {chunk_id!r}")
        chunk_revision_id = (
            None if chunk["chunk_revision_id"] is None else int(chunk["chunk_revision_id"])
        )
        origins, origin_count = _read_text_chunk_origins(
            connection,
            version=version,
            chunk=chunk,
            chunk_revision_id=chunk_revision_id,
            limit=origin_limit,
        )
        embeddings, embedding_count = _read_text_chunk_embedding_derivations(
            connection,
            version=version,
            chunk_id=chunk_id,
            model_signature=model_signature,
            published_only=published_only,
            limit=embedding_limit,
        )
        signature = str(chunk["chunking_signature"])
        current_refresh = str(chunk["refresh_token"])
        published = any(
            origin.published and origin.refresh_token == current_refresh for origin in origins
        )
        current_origins = tuple(
            origin for origin in origins if origin.refresh_token == current_refresh
        )
        if not origins:
            lineage_status = "legacy_unattributed"
        elif not published:
            lineage_status = "materialized_unpublished"
        elif (
            current_origins
            and all(origin.lineage_status == "published" for origin in current_origins)
            and embeddings
            and all(embedding.receipt_id is not None for embedding in embeddings)
        ):
            lineage_status = "recorded"
        else:
            lineage_status = "partially_unattributed"
        return SemanticTextChunkLineage(
            chunk_id=str(chunk["chunk_id"]),
            item_id=str(chunk["item_id"]),
            chunk_revision_id=chunk_revision_id,
            chunking_signature=signature,
            chunk_stage_id=SEMANTIC_CHUNK_STAGE,
            chunk_stage_version=signature.partition("|")[0],
            published=published,
            lineage_status=lineage_status,
            origins=tuple(origins),
            origin_count=origin_count,
            origins_truncated=origin_count > len(origins),
            embeddings=tuple(embeddings),
            embedding_count=embedding_count,
            embeddings_truncated=embedding_count > len(embeddings),
        )


def find_text_chunks_for_source_revision(
    path: Path,
    *,
    revision_id: str,
    limit: int = 100,
) -> SemanticRevisionChunkPage:
    """Return a bounded set of chunks that depend on one source RevisionRef id."""

    if not revision_id.strip():
        raise ValueError("revision_id cannot be blank")
    if not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    with semantic_database(path, readonly=True) as connection:
        version = _read_schema_version(connection)
        if version not in _LINEAGE_SCHEMA_VERSIONS:
            raise SemanticStateError(
                f"semantic lineage requires schema 6, 7, 8, 9 or 10; observed {version!r}"
            )
        _validate_version_contract(connection, version)
        if version in _RECEIPT_LINEAGE_SCHEMA_VERSIONS:
            rows = connection.execute(
                """SELECT chunk_id FROM (
                    SELECT chunk.chunk_id AS chunk_id
                    FROM semantic_item_revisions item
                    JOIN semantic_chunk_derivations derivation
                      ON derivation.item_revision_id=item.item_revision_id
                    JOIN semantic_chunk_revisions chunk
                      ON chunk.chunk_revision_id=derivation.chunk_revision_id
                    WHERE json_extract(item.source_revision_json,'$.revision_id')=?
                    UNION
                    SELECT chunk.chunk_id AS chunk_id
                    FROM semantic_item_revisions item
                    JOIN embedding_generation_members member
                      ON member.item_revision_id=item.item_revision_id
                    JOIN semantic_chunk_revisions chunk
                      ON chunk.chunk_revision_id=member.chunk_revision_id
                    WHERE member.entity_kind='text_chunk'
                      AND json_extract(item.source_revision_json,'$.revision_id')=?
                ) ORDER BY chunk_id LIMIT ?""",
                (revision_id, revision_id, limit + 1),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT DISTINCT chunk.chunk_id
                FROM semantic_item_revisions item
                JOIN embedding_generation_members member
                  ON member.item_revision_id=item.item_revision_id
                JOIN semantic_chunk_revisions chunk
                  ON chunk.chunk_revision_id=member.chunk_revision_id
                WHERE member.entity_kind='text_chunk'
                  AND json_extract(item.source_revision_json,'$.revision_id')=?
                ORDER BY chunk.chunk_id LIMIT ?""",
                (revision_id, limit + 1),
            ).fetchall()
        return SemanticRevisionChunkPage(
            revision_id=revision_id,
            chunk_ids=tuple(str(row[0]) for row in rows[:limit]),
            truncated=len(rows) > limit,
        )


def read_semantic_derivation_outbox(
    path: Path,
    *,
    after_event_id: int = 0,
    limit: int = 100,
) -> tuple[SemanticDerivationEvent, ...]:
    """Read a bounded owner-local event page without marking it consumed."""

    if after_event_id < 0:
        raise ValueError("after_event_id cannot be negative")
    if not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    with semantic_database(path, readonly=True) as connection:
        version = _read_schema_version(connection)
        if version == 6:
            _validate_version_contract(connection, version)
            return ()
        if version not in _RECEIPT_LINEAGE_SCHEMA_VERSIONS:
            raise SemanticStateError(
                f"semantic derivation outbox requires schema 7, 8, 9 or 10; observed {version!r}"
            )
        _validate_version_contract(connection, version)
        rows = connection.execute(
            """SELECT event.event_id,event.receipt_id AS outbox_receipt_id,
            event.event_kind,event.aggregate_kind,event.aggregate_id,
            event.payload_json,event.committed_ns AS event_committed_ns,
            receipt.*
            FROM semantic_derivation_outbox event
            LEFT JOIN semantic_work_receipts receipt
              ON receipt.receipt_id=event.receipt_id
            WHERE event.event_id>? ORDER BY event.event_id LIMIT ?""",
            (after_event_id, limit),
        )
        events: list[SemanticDerivationEvent] = []
        captured_bytes = 0
        for row in rows:
            payload_raw = str(row["payload_json"])
            receipt_raw = "" if row["receipt_json"] is None else str(row["receipt_json"])
            row_bytes = _semantic_outbox_row_cost(
                row,
                payload_raw=payload_raw,
                receipt_raw=receipt_raw,
            )
            if events and captured_bytes + row_bytes > _MAX_OUTBOX_PAGE_BYTES:
                break
            captured_bytes += row_bytes
            if row["receipt_id"] is None:
                raise SemanticStateError("semantic derivation outbox receipt is missing")
            _validate_semantic_outbox_commit_timestamp(row)
            payload_raw = str(row["payload_json"])
            receipt_raw = str(row["receipt_json"])
            payload_schema = _semantic_outbox_payload_schema(payload_raw)
            receipt_contract = _validated_semantic_receipt_row(
                row,
                owner_schema_version=version,
            )
            receipt = receipt_contract.to_dict()
            if payload_schema == _SEMANTIC_DERIVATION_EVENT_V1:
                payload = _json_object(
                    payload_raw,
                    label="semantic derivation outbox payload",
                )
                _validate_semantic_outbox_v1_payload(
                    payload,
                    row=row,
                    receipt=receipt,
                )
            elif payload_schema == _SEMANTIC_DERIVATION_EVENT_V2:
                if version not in {9, 10}:
                    raise SemanticStateError(
                        "semantic v2 outbox payload requires schema 9 or 10"
                    )
                _validate_semantic_outbox_v2_payload(
                    payload_raw,
                    row=row,
                    receipt_raw=receipt_raw,
                    receipt=receipt,
                )
                # Public payload remains the historical logical v1 envelope;
                # only the stored wire body is compact and referenced.
                payload = _semantic_outbox_v1_payload_from_row(
                    row,
                    receipt=receipt,
                )
            else:
                raise SemanticStateError("semantic derivation outbox payload schema is unsupported")
            events.append(
                SemanticDerivationEvent(
                    event_id=int(row["event_id"]),
                    receipt_id=int(row["outbox_receipt_id"]),
                    event_kind=str(row["event_kind"]),
                    aggregate_kind=str(row["aggregate_kind"]),
                    aggregate_id=str(row["aggregate_id"]),
                    payload=payload,
                    receipt=receipt,
                    committed_ns=int(row["event_committed_ns"]),
                )
            )
        return tuple(events)


__all__ = (
    "MAX_LINEAGE_ROWS",
    "SEMANTIC_CHUNK_STAGE",
    "SemanticChunkOrigin",
    "SemanticDerivationEvent",
    "SemanticEmbeddingDerivation",
    "SemanticRevisionChunkPage",
    "SemanticTextChunkLineage",
    "_payload_causation_receipts",
    "_producer_receipts_for_embedding_members",
    "_record_chunk_materialization",
    "_record_chunk_refresh_publication",
    "_record_discarded_embedding_execution",
    "_record_embedding_attempt_failure",
    "_record_embedding_clone_batch",
    "_record_embedding_receipt",
    "_record_generation_publication_receipt",
    "_snapshot_chunk_revision",
    "_snapshot_item_revision",
    "explain_text_chunk_lineage",
    "find_text_chunks_for_source_revision",
    "read_semantic_derivation_outbox",
)
