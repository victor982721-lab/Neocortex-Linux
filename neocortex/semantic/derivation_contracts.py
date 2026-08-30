"""Immutable public contracts for reproducible owner-local derivations.

The contracts describe committed facts; they do not coordinate work or claim
cross-owner atomicity.  In particular, :class:`WorkReceipt` is terminal.  A
durable ``running`` attempt remains an owner-local persistence concern until it
can be reconciled to one of the terminal outcomes declared here.
"""

from __future__ import annotations
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from neocortex.knowledge.knowledge_contracts import (
    PhysicalIdentityRef,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from .semantic_models import canonical_json, fingerprint_text

# region [01] Versions, bounds and stable vocabulary


DERIVATION_CONTRACT_SCHEMA_VERSION = 1
MAX_BINDINGS_PER_RECEIPT = 4_096
MAX_CONFIGURATION_ITEMS = 128
MAX_CONFIGURATION_JSON_BYTES = 65_536
MAX_RUNTIME_ITEMS = 128
MAX_RUNTIME_JSON_BYTES = 16_384
MAX_FAILURE_DETAILS = 32
MAX_IDENTIFIER_CHARS = 512
MAX_SIGNATURE_CHARS = 4_096
MAX_VALUE_CHARS = 4_096
MAX_FAILURE_MESSAGE_CHARS = 8_192
MAX_ATTEMPT_NUMBER = 1_000_000
MAX_WORK_DURATION_NS = 366 * 24 * 60 * 60 * 1_000_000_000
MAX_WORK_RECEIPT_JSON_BYTES = 1_000_000

ConfigurationScalar = str | int | float | bool | None

_SENSITIVE_CONFIGURATION_KEY_SUFFIXES = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "secret_key",
    "token",
)
_REDACTED_CONFIGURATION_VALUES = {"[redacted]", "<redacted>"}


def _is_sensitive_key(value: str) -> bool:
    compact = "".join(character for character in value.casefold() if character.isalnum())
    return any(
        compact == "".join(character for character in suffix if character.isalnum())
        or compact.endswith("".join(character for character in suffix if character.isalnum()))
        for suffix in _SENSITIVE_CONFIGURATION_KEY_SUFFIXES
    )


def _is_redacted_value(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and value.casefold() in _REDACTED_CONFIGURATION_VALUES
    )


class ReproducibilityClass(StrEnum):
    """The strongest replay claim that a producer can actually substantiate."""

    EXACT = "exact"
    ENVIRONMENT_BOUND = "environment_bound"
    SEEDED = "seeded"
    EQUIVALENT = "equivalent"
    BEST_EFFORT = "best_effort"
    NON_REPLAYABLE = "non_replayable"


class WorkOutcome(StrEnum):
    """Terminal outcome of one owner-local attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class WorkExecutionMode(StrEnum):
    """Whether work produced, reused, attempted or could not be observed."""

    EXECUTED = "executed"
    CACHE_HIT = "cache_hit"
    REPLAY = "replay"
    ATTEMPTED = "attempted"
    UNKNOWN = "unknown"


# endregion [01]


# region [02] Pure bounded validation and canonical payload helpers


def _required_text(name: str, value: object, *, limit: int = MAX_VALUE_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if len(value) > limit:
        raise ValueError(f"{name} cannot exceed {limit} characters")
    return value


def _optional_text(
    name: str,
    value: object | None,
    *,
    limit: int = MAX_VALUE_CHARS,
) -> str | None:
    if value is None:
        return None
    return _required_text(name, value, limit=limit)


def _positive_integer(name: str, value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _optional_nonnegative_integer(name: str, value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer when present")
    return value


def _enum_member(name: str, value: object, enum_type: type[StrEnum]) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{name} must be a {enum_type.__name__}")


def _fingerprint(name: str, value: object) -> str:
    return _required_text(name, value, limit=MAX_SIGNATURE_CHARS)


def _utc_datetime(name: str, value: object) -> datetime:
    text = _required_text(name, value, limit=64)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must identify UTC explicitly")
    return parsed


def _string_pairs(
    name: str,
    values: object,
    *,
    maximum_items: int,
    maximum_json_bytes: int,
    redact_sensitive: bool = False,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be an immutable tuple of pairs")
    if len(values) > maximum_items:
        raise ValueError(f"{name} cannot contain more than {maximum_items} items")

    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"{name}[{index}] must be a pair")
        key = _required_text(
            f"{name}[{index}].key",
            item[0],
            limit=MAX_IDENTIFIER_CHARS,
        )
        value = _required_text(f"{name}[{index}].value", item[1])
        if key in seen:
            raise ValueError(f"{name} cannot contain duplicate key {key!r}")
        if redact_sensitive and _is_sensitive_key(key) and not _is_redacted_value(value):
            raise ValueError(f"{name}[{index}].value must be redacted for sensitive key {key!r}")
        seen.add(key)
        normalized.append((key, value))

    result = tuple(sorted(normalized))
    encoded = canonical_json(dict(result)).encode("utf-8")
    if len(encoded) > maximum_json_bytes:
        raise ValueError(f"{name} exceeds its {maximum_json_bytes}-byte JSON bound")
    return result


def _configuration_pairs(
    values: object,
) -> tuple[tuple[str, ConfigurationScalar], ...]:
    name = "effective_configuration"
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be an immutable tuple of pairs")
    if len(values) > MAX_CONFIGURATION_ITEMS:
        raise ValueError(f"{name} cannot contain more than {MAX_CONFIGURATION_ITEMS} items")

    normalized: list[tuple[str, ConfigurationScalar]] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"{name}[{index}] must be a pair")
        key = _required_text(
            f"{name}[{index}].key",
            item[0],
            limit=MAX_IDENTIFIER_CHARS,
        )
        if key in seen:
            raise ValueError(f"{name} cannot contain duplicate key {key!r}")
        seen.add(key)

        value = item[1]
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError(f"{name}[{index}].value must be a JSON scalar")
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"{name}[{index}].value cannot exceed {MAX_VALUE_CHARS} characters")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name}[{index}].value must be finite")
        if _is_sensitive_key(key) and not _is_redacted_value(value):
            raise ValueError(f"{name}[{index}].value must be redacted for sensitive key {key!r}")
        normalized.append((key, value))

    result = tuple(sorted(normalized))
    encoded = canonical_json(dict(result)).encode("utf-8")
    if len(encoded) > MAX_CONFIGURATION_JSON_BYTES:
        raise ValueError(f"{name} exceeds its {MAX_CONFIGURATION_JSON_BYTES}-byte JSON bound")
    return result


class _NamedBinding(Protocol):
    @property
    def name(self) -> str: ...


def _bounded_bindings(
    name: str,
    values: object,
    binding_type: type[_NamedBinding],
) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be an immutable tuple")
    if len(values) > MAX_BINDINGS_PER_RECEIPT:
        raise ValueError(f"{name} cannot contain more than {MAX_BINDINGS_PER_RECEIPT} bindings")
    if any(not isinstance(value, binding_type) for value in values):
        raise ValueError(f"{name} must contain only {binding_type.__name__} values")
    names = tuple(value.name for value in values)
    if len(set(names)) != len(names):
        raise ValueError(f"{name} cannot contain duplicate binding names")


def _base_payload(kind: str) -> dict[str, object]:
    return {
        "schema_version": DERIVATION_CONTRACT_SCHEMA_VERSION,
        "kind": kind,
    }


def _canonical_output(payload: Mapping[str, object]) -> str:
    return canonical_json(payload)


def _contract_fingerprint(payload: Mapping[str, object]) -> str:
    digest = fingerprint_text(_canonical_output(payload)).xxh3_128
    return f"derivation-contract-v{DERIVATION_CONTRACT_SCHEMA_VERSION}:xxh3-128:{digest}"


def _payload_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _payload_sequence(value: object, *, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if len(value) > MAX_BINDINGS_PER_RECEIPT:
        raise ValueError(f"{label} exceeds its binding bound")
    return tuple(
        _payload_mapping(item, label=f"{label}[{index}]") for index, item in enumerate(value)
    )


def _physical_identity_from_payload(value: object) -> PhysicalIdentityRef | None:
    if value is None:
        return None
    payload = _payload_mapping(value, label="physical_identity")
    return PhysicalIdentityRef(
        scheme=cast(str, payload["scheme"]),
        value=cast(str, payload["value"]),
        identity_version=cast(int, payload["identity_version"]),
    )


def _resource_from_payload(value: object) -> ResourceRef | None:
    if value is None:
        return None
    payload = _payload_mapping(value, label="resource")
    disposition = payload.get("disposition")
    return ResourceRef(
        resource_id=cast(str, payload["resource_id"]),
        source_kind=cast(str, payload["source_kind"]),
        owner=cast(str, payload["owner"]),
        physical_identity=_physical_identity_from_payload(payload.get("physical_identity")),
        current_path=cast(str | None, payload.get("current_path")),
        disposition=(None if disposition is None else ResourceDisposition(str(disposition))),
        canonical_resource_id=cast(str | None, payload.get("canonical_resource_id")),
    )


def _revision_from_payload(value: object) -> RevisionRef:
    payload = _payload_mapping(value, label="revision")
    return RevisionRef(
        resource_id=cast(str, payload["resource_id"]),
        revision_id=cast(str, payload["revision_id"]),
        producer=cast(str, payload["producer"]),
        processing_signature=cast(str, payload["processing_signature"]),
        generation=cast(int | None, payload.get("generation")),
        state=RevisionState(str(payload["state"])),
        observed_at_utc=cast(str | None, payload.get("observed_at_utc")),
    )


# endregion [02]


# region [03] Stage, materialization and binding references


@dataclass(frozen=True, slots=True)
class StageDescriptor:
    """Versioned implementation identity for one independently replayable stage."""

    stage_id: str
    stage_version: str
    processing_signature: str
    implementation_digest: str | None = None
    provider: str | None = None
    provider_version: str | None = None
    model: str | None = None
    model_version: str | None = None
    model_digest: str | None = None

    def __post_init__(self) -> None:
        _required_text("stage_id", self.stage_id, limit=MAX_IDENTIFIER_CHARS)
        _required_text("stage_version", self.stage_version, limit=MAX_IDENTIFIER_CHARS)
        _required_text(
            "processing_signature",
            self.processing_signature,
            limit=MAX_SIGNATURE_CHARS,
        )
        _optional_text(
            "implementation_digest",
            self.implementation_digest,
            limit=MAX_SIGNATURE_CHARS,
        )
        _optional_text("provider", self.provider, limit=MAX_IDENTIFIER_CHARS)
        _optional_text(
            "provider_version",
            self.provider_version,
            limit=MAX_IDENTIFIER_CHARS,
        )
        _optional_text("model", self.model, limit=MAX_IDENTIFIER_CHARS)
        _optional_text(
            "model_version",
            self.model_version,
            limit=MAX_IDENTIFIER_CHARS,
        )
        _optional_text("model_digest", self.model_digest, limit=MAX_SIGNATURE_CHARS)
        if self.provider_version is not None and self.provider is None:
            raise ValueError("provider_version requires provider")
        if (self.model_version is not None or self.model_digest is not None) and self.model is None:
            raise ValueError("model_version and model_digest require model")

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("stage_descriptor")
        payload.update(
            {
                "stage_id": self.stage_id,
                "stage_version": self.stage_version,
                "processing_signature": self.processing_signature,
                "implementation_digest": self.implementation_digest,
                "provider": self.provider,
                "provider_version": self.provider_version,
                "model": self.model,
                "model_version": self.model_version,
                "model_digest": self.model_digest,
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())

    @property
    def contract_fingerprint(self) -> str:
        return _contract_fingerprint(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> StageDescriptor:
        payload = _payload_mapping(value, label="stage")
        return cls(
            stage_id=cast(str, payload["stage_id"]),
            stage_version=cast(str, payload["stage_version"]),
            processing_signature=cast(str, payload["processing_signature"]),
            implementation_digest=cast(str | None, payload.get("implementation_digest")),
            provider=cast(str | None, payload.get("provider")),
            provider_version=cast(str | None, payload.get("provider_version")),
            model=cast(str | None, payload.get("model")),
            model_version=cast(str | None, payload.get("model_version")),
            model_digest=cast(str | None, payload.get("model_digest")),
        )


@dataclass(frozen=True, slots=True)
class MaterializationRef:
    """Logical reference to derived state without a path-bound locator."""

    owner: str
    kind: str
    materialization_id: str
    schema_version: int
    resource: ResourceRef | None = None
    revision: RevisionRef | None = None
    generation: int | None = None

    def __post_init__(self) -> None:
        _required_text("owner", self.owner, limit=MAX_IDENTIFIER_CHARS)
        _required_text("kind", self.kind, limit=MAX_IDENTIFIER_CHARS)
        _required_text(
            "materialization_id",
            self.materialization_id,
            limit=MAX_IDENTIFIER_CHARS,
        )
        _positive_integer("schema_version", self.schema_version, maximum=2_147_483_647)
        if self.resource is not None and not isinstance(self.resource, ResourceRef):
            raise ValueError("resource must be a ResourceRef when present")
        if self.revision is not None and not isinstance(self.revision, RevisionRef):
            raise ValueError("revision must be a RevisionRef when present")
        _optional_nonnegative_integer("generation", self.generation)
        if (
            self.resource is not None
            and self.revision is not None
            and self.resource.resource_id != self.revision.resource_id
        ):
            raise ValueError("resource and revision must identify the same resource")

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("materialization_ref")
        payload.update(
            {
                "owner": self.owner,
                "materialization_kind": self.kind,
                "materialization_id": self.materialization_id,
                "owner_schema_version": self.schema_version,
                "resource": self.resource.to_dict() if self.resource is not None else None,
                "revision": self.revision.to_dict() if self.revision is not None else None,
                "generation": self.generation,
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> MaterializationRef:
        payload = _payload_mapping(value, label="materialization")
        revision = payload.get("revision")
        return cls(
            owner=cast(str, payload["owner"]),
            kind=cast(str, payload["materialization_kind"]),
            materialization_id=cast(str, payload["materialization_id"]),
            schema_version=cast(int, payload["owner_schema_version"]),
            resource=_resource_from_payload(payload.get("resource")),
            revision=None if revision is None else _revision_from_payload(revision),
            generation=cast(int | None, payload.get("generation")),
        )


@dataclass(frozen=True, slots=True)
class InputBinding:
    """Exact revision and fingerprint consumed under one stage input name."""

    name: str
    revision: RevisionRef
    fingerprint: str
    fingerprint_algorithm: str = "xxh3-128"
    materialization: MaterializationRef | None = None

    def __post_init__(self) -> None:
        _required_text("name", self.name, limit=MAX_IDENTIFIER_CHARS)
        if not isinstance(self.revision, RevisionRef):
            raise ValueError("revision must be a RevisionRef")
        _fingerprint("fingerprint", self.fingerprint)
        _required_text(
            "fingerprint_algorithm",
            self.fingerprint_algorithm,
            limit=MAX_IDENTIFIER_CHARS,
        )
        if self.materialization is not None:
            if not isinstance(self.materialization, MaterializationRef):
                raise ValueError("materialization must be a MaterializationRef when present")
            materialized_revision = self.materialization.revision
            if materialized_revision is not None and materialized_revision != self.revision:
                raise ValueError("materialization and revision facts must match exactly")

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("input_binding")
        payload.update(
            {
                "name": self.name,
                "revision": self.revision.to_dict(),
                "fingerprint": self.fingerprint,
                "fingerprint_algorithm": self.fingerprint_algorithm,
                "materialization": (
                    self.materialization.to_dict() if self.materialization is not None else None
                ),
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> InputBinding:
        payload = _payload_mapping(value, label="input binding")
        materialization = payload.get("materialization")
        return cls(
            name=cast(str, payload["name"]),
            revision=_revision_from_payload(payload["revision"]),
            fingerprint=cast(str, payload["fingerprint"]),
            fingerprint_algorithm=cast(str, payload["fingerprint_algorithm"]),
            materialization=(
                None if materialization is None else MaterializationRef.from_dict(materialization)
            ),
        )


@dataclass(frozen=True, slots=True)
class OutputBinding:
    """Exact fingerprint published for one logical materialization."""

    name: str
    materialization: MaterializationRef
    fingerprint: str
    fingerprint_algorithm: str = "xxh3-128"

    def __post_init__(self) -> None:
        _required_text("name", self.name, limit=MAX_IDENTIFIER_CHARS)
        if not isinstance(self.materialization, MaterializationRef):
            raise ValueError("materialization must be a MaterializationRef")
        _fingerprint("fingerprint", self.fingerprint)
        _required_text(
            "fingerprint_algorithm",
            self.fingerprint_algorithm,
            limit=MAX_IDENTIFIER_CHARS,
        )

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("output_binding")
        payload.update(
            {
                "name": self.name,
                "materialization": self.materialization.to_dict(),
                "fingerprint": self.fingerprint,
                "fingerprint_algorithm": self.fingerprint_algorithm,
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> OutputBinding:
        payload = _payload_mapping(value, label="output binding")
        return cls(
            name=cast(str, payload["name"]),
            materialization=MaterializationRef.from_dict(payload["materialization"]),
            fingerprint=cast(str, payload["fingerprint"]),
            fingerprint_algorithm=cast(str, payload["fingerprint_algorithm"]),
        )


# endregion [03]


# region [04] Failures, terminal receipts and derivation references


@dataclass(frozen=True, slots=True)
class CapabilityFailure:
    """Bounded, explainable reason why one capability could not complete work."""

    capability_id: str
    reason_code: str
    message: str
    retryable: bool
    provider: str | None = None
    details: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _required_text("capability_id", self.capability_id, limit=MAX_IDENTIFIER_CHARS)
        _required_text("reason_code", self.reason_code, limit=MAX_IDENTIFIER_CHARS)
        _required_text("message", self.message, limit=MAX_FAILURE_MESSAGE_CHARS)
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a bool")
        _optional_text("provider", self.provider, limit=MAX_IDENTIFIER_CHARS)
        normalized = _string_pairs(
            "details",
            self.details,
            maximum_items=MAX_FAILURE_DETAILS,
            maximum_json_bytes=MAX_RUNTIME_JSON_BYTES,
            redact_sensitive=True,
        )
        object.__setattr__(self, "details", normalized)

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("capability_failure")
        payload.update(
            {
                "capability_id": self.capability_id,
                "reason_code": self.reason_code,
                "message": self.message,
                "retryable": self.retryable,
                "provider": self.provider,
                "details": dict(self.details),
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> CapabilityFailure:
        payload = _payload_mapping(value, label="failure")
        details = _payload_mapping(payload["details"], label="failure details")
        return cls(
            capability_id=cast(str, payload["capability_id"]),
            reason_code=cast(str, payload["reason_code"]),
            message=cast(str, payload["message"]),
            retryable=cast(bool, payload["retryable"]),
            provider=cast(str | None, payload.get("provider")),
            details=tuple((key, cast(str, detail)) for key, detail in details.items()),
        )


@dataclass(frozen=True, slots=True)
class WorkReceipt:
    """One terminal, owner-local causal record for committed derivation work.

    ``effective_configuration`` contains only producer-allowlisted scalars.
    Secret-shaped keys are accepted only with an explicit redacted value.
    """

    receipt_id: str
    owner: str
    stage: StageDescriptor
    inputs: tuple[InputBinding, ...]
    outputs: tuple[OutputBinding, ...]
    effective_configuration: tuple[tuple[str, ConfigurationScalar], ...] = field(repr=False)
    runtime: tuple[tuple[str, str], ...]
    started_at_utc: str
    finished_at_utc: str
    duration_ns: int
    attempt: int
    outcome: WorkOutcome
    execution_mode: WorkExecutionMode
    reproducibility: ReproducibilityClass
    run_id: str
    correlation_id: str
    causation_id: str | None = None
    failure: CapabilityFailure | None = None

    def __post_init__(self) -> None:
        _required_text("receipt_id", self.receipt_id, limit=MAX_IDENTIFIER_CHARS)
        _required_text("owner", self.owner, limit=MAX_IDENTIFIER_CHARS)
        if not isinstance(self.stage, StageDescriptor):
            raise ValueError("stage must be a StageDescriptor")
        _bounded_bindings("inputs", self.inputs, InputBinding)
        _bounded_bindings("outputs", self.outputs, OutputBinding)
        if not self.inputs:
            raise ValueError("inputs must contain at least one exact input binding")

        configuration = _configuration_pairs(self.effective_configuration)
        runtime = _string_pairs(
            "runtime",
            self.runtime,
            maximum_items=MAX_RUNTIME_ITEMS,
            maximum_json_bytes=MAX_RUNTIME_JSON_BYTES,
            redact_sensitive=True,
        )
        object.__setattr__(self, "effective_configuration", configuration)
        object.__setattr__(self, "runtime", runtime)

        started = _utc_datetime("started_at_utc", self.started_at_utc)
        finished = _utc_datetime("finished_at_utc", self.finished_at_utc)
        if finished < started:
            raise ValueError("finished_at_utc cannot precede started_at_utc")
        if (
            isinstance(self.duration_ns, bool)
            or not isinstance(self.duration_ns, int)
            or not 0 <= self.duration_ns <= MAX_WORK_DURATION_NS
        ):
            raise ValueError(f"duration_ns must be between 0 and {MAX_WORK_DURATION_NS}")
        _positive_integer("attempt", self.attempt, maximum=MAX_ATTEMPT_NUMBER)
        _enum_member("outcome", self.outcome, WorkOutcome)
        _enum_member("execution_mode", self.execution_mode, WorkExecutionMode)
        _enum_member("reproducibility", self.reproducibility, ReproducibilityClass)
        _required_text("run_id", self.run_id, limit=MAX_IDENTIFIER_CHARS)
        _required_text("correlation_id", self.correlation_id, limit=MAX_IDENTIFIER_CHARS)
        _optional_text("causation_id", self.causation_id, limit=MAX_IDENTIFIER_CHARS)
        if self.failure is not None and not isinstance(self.failure, CapabilityFailure):
            raise ValueError("failure must be a CapabilityFailure when present")

        succeeded = self.outcome is WorkOutcome.SUCCEEDED
        if succeeded:
            if not self.outputs:
                raise ValueError("a succeeded receipt must bind at least one output")
            if self.failure is not None:
                raise ValueError("a succeeded receipt cannot include a failure")
            if self.execution_mode not in {
                WorkExecutionMode.EXECUTED,
                WorkExecutionMode.CACHE_HIT,
                WorkExecutionMode.REPLAY,
            }:
                raise ValueError("a succeeded receipt must use executed, cache_hit or replay mode")
            if (
                self.execution_mode
                in {
                    WorkExecutionMode.CACHE_HIT,
                    WorkExecutionMode.REPLAY,
                }
                and self.causation_id is None
            ):
                raise ValueError("cache_hit and replay receipts require a causation_id")
        elif self.outcome in {WorkOutcome.FAILED, WorkOutcome.CANCELLED}:
            if self.outputs:
                raise ValueError("an unsuccessful receipt cannot claim committed outputs")
            if self.failure is None:
                raise ValueError("an unsuccessful receipt must explain its failure")
            if self.execution_mode is not WorkExecutionMode.ATTEMPTED:
                raise ValueError("a failed or cancelled receipt must use attempted mode")
        else:
            if self.outputs:
                raise ValueError("an unsuccessful receipt cannot claim committed outputs")
            if self.failure is None:
                raise ValueError("an abandoned receipt must explain its unknown state")
            if self.execution_mode is not WorkExecutionMode.UNKNOWN:
                raise ValueError("an abandoned receipt must use unknown mode")

        if self.reproducibility is ReproducibilityClass.NON_REPLAYABLE and self.execution_mode in {
            WorkExecutionMode.CACHE_HIT,
            WorkExecutionMode.REPLAY,
        }:
            raise ValueError("non_replayable work cannot be a cache hit or replay")
        if self.reproducibility is ReproducibilityClass.EXACT:
            self._validate_exact_claim(runtime)
        if len(self.to_json().encode("utf-8")) > MAX_WORK_RECEIPT_JSON_BYTES:
            raise ValueError(f"WorkReceipt JSON cannot exceed {MAX_WORK_RECEIPT_JSON_BYTES} bytes")

    def _validate_exact_claim(self, runtime: tuple[tuple[str, str], ...]) -> None:
        if self.stage.implementation_digest is None:
            raise ValueError("exact reproducibility requires implementation_digest")
        if not runtime:
            raise ValueError("exact reproducibility requires an identified runtime")
        if self.stage.model is not None and (
            self.stage.model_version is None or self.stage.model_digest is None
        ):
            raise ValueError("exact model work requires model_version and model_digest")

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("work_receipt")
        payload.update(
            {
                "receipt_id": self.receipt_id,
                "owner": self.owner,
                "stage": self.stage.to_dict(),
                "inputs": [binding.to_dict() for binding in self.inputs],
                "outputs": [binding.to_dict() for binding in self.outputs],
                "effective_configuration": dict(self.effective_configuration),
                "runtime": dict(self.runtime),
                "started_at_utc": self.started_at_utc,
                "finished_at_utc": self.finished_at_utc,
                "duration_ns": self.duration_ns,
                "attempt": self.attempt,
                "outcome": self.outcome.value,
                "execution_mode": self.execution_mode.value,
                "reproducibility": self.reproducibility.value,
                "run_id": self.run_id,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
                "failure": self.failure.to_dict() if self.failure is not None else None,
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> WorkReceipt:
        """Reconstruct and strictly revalidate one canonical v1 receipt payload."""

        payload = _payload_mapping(value, label="WorkReceipt")
        if (
            payload.get("schema_version") != DERIVATION_CONTRACT_SCHEMA_VERSION
            or payload.get("kind") != "work_receipt"
        ):
            raise ValueError("payload is not a WorkReceipt v1")
        try:
            configuration = _payload_mapping(
                payload["effective_configuration"],
                label="effective_configuration",
            )
            runtime = _payload_mapping(payload["runtime"], label="runtime")
            failure = payload["failure"]
            receipt = cls(
                receipt_id=cast(str, payload["receipt_id"]),
                owner=cast(str, payload["owner"]),
                stage=StageDescriptor.from_dict(payload["stage"]),
                inputs=tuple(
                    InputBinding.from_dict(item)
                    for item in _payload_sequence(payload["inputs"], label="inputs")
                ),
                outputs=tuple(
                    OutputBinding.from_dict(item)
                    for item in _payload_sequence(payload["outputs"], label="outputs")
                ),
                effective_configuration=tuple(
                    (key, cast(ConfigurationScalar, item)) for key, item in configuration.items()
                ),
                runtime=tuple((key, cast(str, item)) for key, item in runtime.items()),
                started_at_utc=cast(str, payload["started_at_utc"]),
                finished_at_utc=cast(str, payload["finished_at_utc"]),
                duration_ns=cast(int, payload["duration_ns"]),
                attempt=cast(int, payload["attempt"]),
                outcome=WorkOutcome(cast(str, payload["outcome"])),
                execution_mode=WorkExecutionMode(cast(str, payload["execution_mode"])),
                reproducibility=ReproducibilityClass(cast(str, payload["reproducibility"])),
                run_id=cast(str, payload["run_id"]),
                correlation_id=cast(str, payload["correlation_id"]),
                causation_id=cast(str | None, payload["causation_id"]),
                failure=(None if failure is None else CapabilityFailure.from_dict(failure)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid WorkReceipt payload: {exc}") from exc
        if receipt.to_dict() != dict(payload):
            raise ValueError("WorkReceipt payload is not canonical or contains unknown fields")
        return receipt

    @classmethod
    def from_json(cls, value: str) -> WorkReceipt:
        if not isinstance(value, str):
            raise ValueError("WorkReceipt JSON must be a string")
        if len(value.encode("utf-8")) > MAX_WORK_RECEIPT_JSON_BYTES:
            raise ValueError("WorkReceipt JSON exceeds its byte bound")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("WorkReceipt JSON is malformed") from exc
        receipt = cls.from_dict(payload)
        if receipt.to_json() != value:
            raise ValueError("WorkReceipt JSON is not canonical")
        return receipt

    @property
    def contract_fingerprint(self) -> str:
        return _contract_fingerprint(self.to_dict())


@dataclass(frozen=True, slots=True)
class DerivationRef:
    """Stable edge from one materialization to its owner-local receipt."""

    derivation_id: str
    receipt_id: str
    materialization: MaterializationRef

    def __post_init__(self) -> None:
        _required_text("derivation_id", self.derivation_id, limit=MAX_IDENTIFIER_CHARS)
        _required_text("receipt_id", self.receipt_id, limit=MAX_IDENTIFIER_CHARS)
        if not isinstance(self.materialization, MaterializationRef):
            raise ValueError("materialization must be a MaterializationRef")

    def to_dict(self) -> dict[str, object]:
        payload = _base_payload("derivation_ref")
        payload.update(
            {
                "derivation_id": self.derivation_id,
                "receipt_id": self.receipt_id,
                "materialization": self.materialization.to_dict(),
            }
        )
        return payload

    def to_json(self) -> str:
        return _canonical_output(self.to_dict())


# endregion [04]


__all__ = [
    "DERIVATION_CONTRACT_SCHEMA_VERSION",
    "CapabilityFailure",
    "DerivationRef",
    "InputBinding",
    "MaterializationRef",
    "OutputBinding",
    "ReproducibilityClass",
    "StageDescriptor",
    "WorkExecutionMode",
    "WorkOutcome",
    "WorkReceipt",
]
