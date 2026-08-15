"""Pure, lightweight contracts for explainable capability selection.

The broker never imports an engine, opens workspace state, downloads a model or
executes a provider.  Runtime probes live in :mod:`neocortex.capabilities` and
feed this module explicit observations.  This separation keeps selection
deterministic, injectable and safe to use from ``--help``/doctor surfaces.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice

CAPABILITY_MANIFEST_SCHEMA = "neocortex.capability-manifest/v1"
CAPABILITY_REQUEST_SCHEMA = "neocortex.capability-request/v1"
CAPABILITY_SELECTION_SCHEMA = "neocortex.capability-selection/v1"

MAX_CAPABILITY_MANIFESTS = 128
MAX_CAPABILITY_CANDIDATES = 4
MAX_CAPABILITY_MANIFEST_BYTES = 64 * 1024
MAX_CAPABILITY_REQUEST_BYTES = 64 * 1024
MAX_CAPABILITY_POLICY_BYTES = 64 * 1024
MAX_CAPABILITY_SELECTION_BYTES = 1024 * 1024
MAX_CAPABILITY_VALUES = 64
MAX_CAPABILITY_EVIDENCE_VALUES = 16
MAX_CAPABILITY_BINARY_ALTERNATIVES = 4
MAX_CAPABILITY_METRICS = 32
MAX_CAPABILITY_TEXT = 512
MAX_CAPABILITY_INTEGER = (1 << 63) - 1
_REPRODUCIBILITY_CLASSES = frozenset(
    {
        "exact",
        "environment_bound",
        "seeded",
        "equivalent",
        "best_effort",
        "non_replayable",
    }
)
_MIME_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
_LANGUAGE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _contract_fingerprint(value: object) -> str:
    payload = _canonical_bytes(value)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _bounded_derived_text(prefix: str, detail: str) -> str:
    """Build a stable diagnostic without letting valid identifiers overflow it."""

    value = f"{prefix}:{detail}"
    if value.isascii() and len(value) <= MAX_CAPABILITY_TEXT:
        return value
    digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
    if not detail.isascii():
        return f"{prefix}:sha256:{digest}"
    suffix = f":sha256:{digest}"
    available = MAX_CAPABILITY_TEXT - len(prefix) - len(suffix) - 2
    return f"{prefix}:{detail[: max(0, available)]}:{suffix.removeprefix(':')}"


def _sha256_fingerprint(name: str, value: str) -> str:
    _required_text(name, value)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a canonical SHA-256 fingerprint")
    return value


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} cannot be blank")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    if len(value) > MAX_CAPABILITY_TEXT:
        raise ValueError(f"{name} cannot exceed {MAX_CAPABILITY_TEXT} characters")
    return value


def _optional_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _required_text(name, value)


def _exact_mime(name: str, value: str) -> str:
    _required_text(name, value)
    if _MIME_PATTERN.fullmatch(value) is None or "*" in value:
        raise ValueError(f"{name} must be one canonical exact MIME value")
    return value


def _language_tag(name: str, value: str) -> str:
    _required_text(name, value)
    if value != "unknown" and _LANGUAGE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a BCP-47 tag or unknown")
    return value.casefold()


def _bounded_values(
    name: str,
    values: tuple[str, ...],
    *,
    maximum: int = MAX_CAPABILITY_VALUES,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be a tuple")
    if len(values) > maximum:
        raise ValueError(f"{name} cannot contain more than {maximum} values")
    if not allow_empty and not values:
        raise ValueError(f"{name} cannot be empty")
    normalized = tuple(_required_text(name, item) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} cannot contain duplicates")
    return normalized


def _bounded_diagnostic_values(
    name: str,
    values: tuple[str, ...],
    *,
    maximum: int = MAX_CAPABILITY_EVIDENCE_VALUES,
) -> tuple[str, ...]:
    normalized = _bounded_values(name, values, maximum=maximum)
    if any(not item.isascii() for item in normalized):
        raise ValueError(f"{name} must contain ASCII diagnostic values")
    return normalized


def _positive_optional(name: str, value: int | float | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive when present")
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be finite")
    return value


def _positive_optional_integer(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_CAPABILITY_INTEGER
    ):
        raise ValueError(f"{name} must be a positive integer when present")
    return value


def _nonnegative_optional(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric when present")
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return float(value)


class CapabilityPrivacy(StrEnum):
    """Maximum data exposure inherent to an implementation."""

    LOCAL_ONLY = "local_only"
    NETWORK_OPTIONAL = "network_optional"
    NETWORK_REQUIRED = "network_required"


class CapabilityLanguageMode(StrEnum):
    """How an implementation handles the language dimension."""

    AGNOSTIC = "agnostic"
    DECLARED = "declared"
    DETECTS = "detects"


class CapabilityLifecycle(StrEnum):
    """Whether an implementation is eligible for ordinary production work."""

    PRODUCTION = "production"
    SHADOW = "shadow"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class CapabilityBinaryIdentity:
    """Verified executable artifact used by one readiness observation."""

    name: str
    command: str = field(repr=False, compare=False)
    command_sha256: str
    artifact_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _required_text("name", self.name)
        _required_text("command", self.command)
        for name, value in (
            ("command_sha256", self.command_sha256),
            ("artifact_sha256", self.artifact_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
        if not os.path.isabs(self.command) or os.path.normpath(self.command) != self.command:
            raise ValueError("command must be one absolute normalized path")
        expected_command_hash = hashlib.sha256(self.command.encode("utf-8")).hexdigest()
        if self.command_sha256 != expected_command_hash:
            raise ValueError("command_sha256 must fingerprint command exactly")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
            or self.size_bytes > MAX_CAPABILITY_INTEGER
        ):
            raise ValueError("size_bytes must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command_sha256": self.command_sha256,
            "artifact_sha256": self.artifact_sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class CapabilityQualityMetric:
    """One measured provider property with its evidence and direction."""

    metric_id: str
    value: float
    unit: str
    evidence: str
    higher_is_better: bool

    def __post_init__(self) -> None:
        _required_text("metric_id", self.metric_id)
        _required_text("unit", self.unit)
        _required_text("evidence", self.evidence)
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("quality metric value must be numeric")
        try:
            finite = math.isfinite(float(self.value))
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("quality metric value must be finite")
        object.__setattr__(self, "value", float(self.value))
        if not isinstance(self.higher_is_better, bool):
            raise ValueError("higher_is_better must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "value": float(self.value),
            "unit": self.unit,
            "evidence": self.evidence,
            "higher_is_better": self.higher_is_better,
        }


@dataclass(frozen=True, slots=True)
class CapabilityQualityRequirement:
    """A hard quality threshold and optional deterministic preference."""

    metric_id: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    evidence: str | None = None
    prefer_higher: bool | None = None

    def __post_init__(self) -> None:
        _required_text("metric_id", self.metric_id)
        _optional_text("unit", self.unit)
        _optional_text("evidence", self.evidence)
        for name, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"quality {name} must be finite when present")
            try:
                finite = math.isfinite(float(value))
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"quality {name} must be finite when present")
            object.__setattr__(self, name, float(value))
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("quality minimum cannot exceed maximum")
        if self.prefer_higher is not None and not isinstance(self.prefer_higher, bool):
            raise ValueError("prefer_higher must be a boolean when present")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"metric_id": self.metric_id}
        if self.minimum is not None:
            payload["minimum"] = float(self.minimum)
        if self.maximum is not None:
            payload["maximum"] = float(self.maximum)
        if self.unit is not None:
            payload["unit"] = self.unit
        if self.evidence is not None:
            payload["evidence"] = self.evidence
        if self.prefer_higher is not None:
            payload["prefer_higher"] = self.prefer_higher
        return payload


@dataclass(frozen=True, slots=True)
class CapabilityMimeBinaryAlternatives:
    """Executable alternatives that can serve one exact MIME type."""

    mime_type: str
    alternatives: tuple[str, ...]
    unavailable_reason: str

    def __post_init__(self) -> None:
        _exact_mime("mime_type", self.mime_type)
        _bounded_values(
            "alternatives",
            self.alternatives,
            maximum=MAX_CAPABILITY_BINARY_ALTERNATIVES,
            allow_empty=False,
        )
        _required_text("unavailable_reason", self.unavailable_reason)

    def to_dict(self) -> dict[str, object]:
        return {
            "mime_type": self.mime_type,
            "alternatives": list(self.alternatives),
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """Versioned facts declared by one replaceable implementation."""

    capability_id: str
    capability_version: str
    implementation_id: str
    provider: str
    provider_version: str | None
    lifecycle: CapabilityLifecycle
    supported_platforms: tuple[str, ...]
    modalities: tuple[str, ...]
    input_schemas: tuple[str, ...]
    output_schemas: tuple[str, ...]
    mime_types: tuple[str, ...]
    language_mode: CapabilityLanguageMode
    languages: tuple[str, ...]
    deterministic: bool | None
    reproducibility_classes: tuple[str, ...]
    incremental: bool
    cancellation: bool
    checkpointing: bool
    max_input_bytes: int | None
    default_timeout_seconds: float | None
    cpu_threads: int | None
    ram_bytes: int | None
    gpu_required: bool
    network_required: bool
    privacy: CapabilityPrivacy
    optional_extra: str | None
    required_components: tuple[str, ...]
    required_binaries: tuple[str, ...]
    mime_binary_alternatives: tuple[CapabilityMimeBinaryAlternatives, ...]
    required_models: tuple[str, ...]
    compatibility: tuple[str, ...]
    quality_metrics: tuple[CapabilityQualityMetric, ...]
    estimated_cost: float | None
    estimated_latency_ms: float | None

    def __post_init__(self) -> None:
        for name, value in (
            ("capability_id", self.capability_id),
            ("capability_version", self.capability_version),
            ("implementation_id", self.implementation_id),
            ("provider", self.provider),
        ):
            _required_text(name, value)
        _optional_text("provider_version", self.provider_version)
        _optional_text("optional_extra", self.optional_extra)
        if not isinstance(self.lifecycle, CapabilityLifecycle):
            raise ValueError("lifecycle must be a CapabilityLifecycle")
        if not isinstance(self.language_mode, CapabilityLanguageMode):
            raise ValueError("language_mode must be a CapabilityLanguageMode")
        if not isinstance(self.privacy, CapabilityPrivacy):
            raise ValueError("privacy must be a CapabilityPrivacy")
        _bounded_values(
            "supported_platforms",
            self.supported_platforms,
            allow_empty=False,
        )
        if "*" in self.supported_platforms:
            raise ValueError("supported_platforms must declare exact values")
        _bounded_values("modalities", self.modalities, allow_empty=False)
        _bounded_values("input_schemas", self.input_schemas, allow_empty=False)
        _bounded_values("output_schemas", self.output_schemas, allow_empty=False)
        _bounded_values("mime_types", self.mime_types, allow_empty=False)
        for mime in self.mime_types:
            _exact_mime("mime_types", mime)
        declared_languages = _bounded_values("languages", self.languages)
        normalized_languages = tuple(
            _language_tag("languages", language) for language in declared_languages
        )
        if len(normalized_languages) != len(set(normalized_languages)):
            raise ValueError("languages cannot contain case-insensitive duplicates")
        if "unknown" in normalized_languages:
            raise ValueError("manifest languages must not declare unknown")
        if self.language_mode is CapabilityLanguageMode.AGNOSTIC and normalized_languages:
            raise ValueError("agnostic language mode cannot declare languages")
        if self.language_mode is not CapabilityLanguageMode.AGNOSTIC and not normalized_languages:
            raise ValueError("declared or detecting language mode requires languages")
        classes = _bounded_values(
            "reproducibility_classes",
            self.reproducibility_classes,
            maximum=len(_REPRODUCIBILITY_CLASSES),
            allow_empty=False,
        )
        unknown_classes = set(classes) - _REPRODUCIBILITY_CLASSES
        if unknown_classes:
            raise ValueError(
                "unknown reproducibility classes: " + ",".join(sorted(unknown_classes))
            )
        for name, values in (
            ("required_components", self.required_components),
            ("required_binaries", self.required_binaries),
            ("required_models", self.required_models),
            ("compatibility", self.compatibility),
        ):
            _bounded_values(name, values)
        if not isinstance(self.mime_binary_alternatives, tuple) or any(
            not isinstance(item, CapabilityMimeBinaryAlternatives)
            for item in self.mime_binary_alternatives
        ):
            raise ValueError(
                "mime_binary_alternatives must be a tuple of CapabilityMimeBinaryAlternatives"
            )
        if len(self.mime_binary_alternatives) > MAX_CAPABILITY_VALUES:
            raise ValueError(
                f"mime_binary_alternatives cannot contain more than {MAX_CAPABILITY_VALUES} values"
            )
        binary_mimes = tuple(item.mime_type for item in self.mime_binary_alternatives)
        if len(binary_mimes) != len(set(binary_mimes)):
            raise ValueError("mime_binary_alternatives cannot contain duplicate MIME types")
        if set(binary_mimes) - set(self.mime_types):
            raise ValueError("mime_binary_alternatives must reference declared MIME types")
        alternative_binaries = {
            binary for item in self.mime_binary_alternatives for binary in item.alternatives
        }
        if alternative_binaries & set(self.required_binaries):
            raise ValueError("required_binaries cannot also be conditional MIME alternatives")
        if not isinstance(self.quality_metrics, tuple) or any(
            not isinstance(item, CapabilityQualityMetric) for item in self.quality_metrics
        ):
            raise ValueError("quality_metrics must be a tuple of CapabilityQualityMetric")
        if len(self.quality_metrics) > MAX_CAPABILITY_METRICS:
            raise ValueError(
                f"quality_metrics cannot contain more than {MAX_CAPABILITY_METRICS} values"
            )
        metric_ids = tuple(item.metric_id for item in self.quality_metrics)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("quality_metrics cannot contain duplicates")
        if self.privacy is CapabilityPrivacy.LOCAL_ONLY and self.network_required:
            raise ValueError("local_only capability cannot require network")
        if self.privacy is CapabilityPrivacy.NETWORK_REQUIRED and not self.network_required:
            raise ValueError("network_required privacy must require network")
        if "exact" in classes:
            raise ValueError(
                "exact reproducibility requires attested runtime contracts not available in v1"
            )
        if self.deterministic is not None and not isinstance(self.deterministic, bool):
            raise ValueError("deterministic must be a boolean when present")
        for boolean_name, boolean_value in (
            ("incremental", self.incremental),
            ("cancellation", self.cancellation),
            ("checkpointing", self.checkpointing),
            ("gpu_required", self.gpu_required),
            ("network_required", self.network_required),
        ):
            if not isinstance(boolean_value, bool):
                raise ValueError(f"{boolean_name} must be a boolean")
        _positive_optional_integer("max_input_bytes", self.max_input_bytes)
        timeout = _positive_optional("default_timeout_seconds", self.default_timeout_seconds)
        _positive_optional_integer("cpu_threads", self.cpu_threads)
        _positive_optional_integer("ram_bytes", self.ram_bytes)
        estimated_cost = _nonnegative_optional("estimated_cost", self.estimated_cost)
        estimated_latency = _nonnegative_optional(
            "estimated_latency_ms",
            self.estimated_latency_ms,
        )
        if timeout is not None:
            object.__setattr__(self, "default_timeout_seconds", float(timeout))
        object.__setattr__(self, "estimated_cost", estimated_cost)
        object.__setattr__(self, "estimated_latency_ms", estimated_latency)
        object.__setattr__(self, "languages", tuple(sorted(normalized_languages)))
        for name in (
            "supported_platforms",
            "modalities",
            "input_schemas",
            "output_schemas",
            "mime_types",
            "reproducibility_classes",
            "required_components",
            "required_binaries",
            "required_models",
            "compatibility",
        ):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name))))
        object.__setattr__(
            self,
            "mime_binary_alternatives",
            tuple(sorted(self.mime_binary_alternatives, key=lambda item: item.mime_type)),
        )
        object.__setattr__(
            self,
            "quality_metrics",
            tuple(sorted(self.quality_metrics, key=lambda item: item.metric_id)),
        )
        if len(_canonical_bytes(self.to_dict())) > MAX_CAPABILITY_MANIFEST_BYTES:
            raise ValueError(
                f"capability manifest cannot exceed {MAX_CAPABILITY_MANIFEST_BYTES} bytes"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_MANIFEST_SCHEMA,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "implementation_id": self.implementation_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "lifecycle": self.lifecycle.value,
            "supported_platforms": list(self.supported_platforms),
            "modalities": list(self.modalities),
            "input_schemas": list(self.input_schemas),
            "output_schemas": list(self.output_schemas),
            "mime_types": list(self.mime_types),
            "language_mode": self.language_mode.value,
            "languages": list(self.languages),
            "deterministic": self.deterministic,
            "reproducibility_classes": list(self.reproducibility_classes),
            "incremental": self.incremental,
            "cancellation": self.cancellation,
            "checkpointing": self.checkpointing,
            "max_input_bytes": self.max_input_bytes,
            "default_timeout_seconds": self.default_timeout_seconds,
            "cpu_threads": self.cpu_threads,
            "ram_bytes": self.ram_bytes,
            "gpu_required": self.gpu_required,
            "network_required": self.network_required,
            "privacy": self.privacy.value,
            "optional_extra": self.optional_extra,
            "components_required": list(self.required_components),
            "binaries_required": list(self.required_binaries),
            "mime_binary_alternatives": [item.to_dict() for item in self.mime_binary_alternatives],
            "models_required": list(self.required_models),
            "compatibility": list(self.compatibility),
            "quality_metrics": [item.to_dict() for item in self.quality_metrics],
            "estimated_cost": self.estimated_cost,
            "estimated_latency_ms": self.estimated_latency_ms,
        }

    @property
    def contract_fingerprint(self) -> str:
        return _contract_fingerprint(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityAvailability:
    """Lightweight runtime observation for exactly one implementation."""

    implementation_id: str
    manifest_fingerprint: str
    execution_request_fingerprint: str
    available: bool
    reasons: tuple[str, ...] = ()
    observed_components: tuple[str, ...] = ()
    binary_identities: tuple[CapabilityBinaryIdentity, ...] = ()

    def __post_init__(self) -> None:
        _required_text("implementation_id", self.implementation_id)
        _sha256_fingerprint("manifest_fingerprint", self.manifest_fingerprint)
        _sha256_fingerprint(
            "execution_request_fingerprint",
            self.execution_request_fingerprint,
        )
        if not isinstance(self.available, bool):
            raise ValueError("available must be a boolean")
        _bounded_diagnostic_values("reasons", self.reasons)
        _bounded_diagnostic_values("observed_components", self.observed_components)
        if not isinstance(self.binary_identities, tuple) or any(
            not isinstance(item, CapabilityBinaryIdentity) for item in self.binary_identities
        ):
            raise ValueError("binary_identities must be a tuple of CapabilityBinaryIdentity")
        if len(self.binary_identities) > MAX_CAPABILITY_EVIDENCE_VALUES:
            raise ValueError(
                "binary_identities cannot contain more than "
                f"{MAX_CAPABILITY_EVIDENCE_VALUES} values"
            )
        binary_names = tuple(item.name for item in self.binary_identities)
        if len(binary_names) != len(set(binary_names)):
            raise ValueError("binary_identities cannot contain duplicate names")
        object.__setattr__(self, "reasons", tuple(sorted(self.reasons)))
        object.__setattr__(
            self,
            "observed_components",
            tuple(sorted(self.observed_components)),
        )
        object.__setattr__(
            self,
            "binary_identities",
            tuple(
                sorted(
                    self.binary_identities,
                    key=lambda item: (item.name, item.artifact_sha256),
                )
            ),
        )
        if self.available and self.reasons:
            raise ValueError("available implementation cannot have unavailable reasons")
        if not self.available and not self.reasons:
            raise ValueError("unavailable implementation requires at least one reason")

    def to_dict(self) -> dict[str, object]:
        return {
            "implementation_id": self.implementation_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "execution_request_fingerprint": self.execution_request_fingerprint,
            "available": self.available,
            "reasons": list(self.reasons),
            "observed_components": list(self.observed_components),
            "binary_identities": [item.to_dict() for item in self.binary_identities],
        }


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """One bounded workload contract submitted to the broker."""

    capability_id: str
    modality: str
    input_schema: str
    output_schema: str
    platform: str | None = None
    mime_type: str | None = None
    language: str | None = None
    input_bytes: int | None = None
    workspace_id: str | None = None
    acceptable_reproducibility: tuple[str, ...] = ()
    require_deterministic: bool = False
    require_incremental: bool = False
    require_cancellation: bool = False
    require_checkpointing: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("capability_id", self.capability_id),
            ("modality", self.modality),
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
        ):
            _required_text(name, value)
        _optional_text("mime_type", self.mime_type)
        _optional_text("language", self.language)
        _optional_text("platform", self.platform)
        _optional_text("workspace_id", self.workspace_id)
        if self.mime_type is not None:
            _exact_mime("mime_type", self.mime_type)
        if self.language is not None:
            object.__setattr__(self, "language", _language_tag("language", self.language))
        if self.input_bytes is not None and (
            isinstance(self.input_bytes, bool)
            or not isinstance(self.input_bytes, int)
            or self.input_bytes < 0
            or self.input_bytes > MAX_CAPABILITY_INTEGER
        ):
            raise ValueError("input_bytes must be a non-negative integer when present")
        classes = _bounded_values(
            "acceptable_reproducibility",
            self.acceptable_reproducibility,
            maximum=len(_REPRODUCIBILITY_CLASSES),
        )
        unknown_classes = set(classes) - _REPRODUCIBILITY_CLASSES
        if unknown_classes:
            raise ValueError(
                "unknown reproducibility classes: " + ",".join(sorted(unknown_classes))
            )
        object.__setattr__(self, "acceptable_reproducibility", tuple(sorted(classes)))
        for boolean_name, boolean_value in (
            ("require_deterministic", self.require_deterministic),
            ("require_incremental", self.require_incremental),
            ("require_cancellation", self.require_cancellation),
            ("require_checkpointing", self.require_checkpointing),
        ):
            if not isinstance(boolean_value, bool):
                raise ValueError(f"{boolean_name} must be a boolean")
        if len(_canonical_bytes(self.to_dict())) > MAX_CAPABILITY_REQUEST_BYTES:
            raise ValueError(
                f"capability request cannot exceed {MAX_CAPABILITY_REQUEST_BYTES} bytes"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_REQUEST_SCHEMA,
            "capability_id": self.capability_id,
            "modality": self.modality,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "platform": self.platform,
            "mime_type": self.mime_type,
            "language": self.language,
            "input_bytes": self.input_bytes,
            "workspace_id": self.workspace_id,
            "acceptable_reproducibility": list(self.acceptable_reproducibility),
            "require_deterministic": self.require_deterministic,
            "require_incremental": self.require_incremental,
            "require_cancellation": self.require_cancellation,
            "require_checkpointing": self.require_checkpointing,
        }

    def execution_contract(self) -> dict[str, object]:
        """Behavioral request facts; excludes input identity and workspace label."""

        return {
            "capability_id": self.capability_id,
            "modality": self.modality,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "platform": self.platform,
            "mime_type": self.mime_type,
            "language": self.language,
            "acceptable_reproducibility": list(self.acceptable_reproducibility),
            "require_deterministic": self.require_deterministic,
            "require_incremental": self.require_incremental,
            "require_cancellation": self.require_cancellation,
            "require_checkpointing": self.require_checkpointing,
        }

    @property
    def execution_contract_fingerprint(self) -> str:
        return _contract_fingerprint(self.execution_contract())

    @property
    def contract_fingerprint(self) -> str:
        return _contract_fingerprint(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """Workspace/policy constraints applied before provider preference."""

    policy_id: str = "neocortex-local-default-v1"
    allow_network: bool = False
    allow_shadow: bool = False
    allowed_privacy: tuple[CapabilityPrivacy, ...] = (CapabilityPrivacy.LOCAL_ONLY,)
    allowed_providers: tuple[str, ...] = ()
    denied_providers: tuple[str, ...] = ()
    allowed_implementations: tuple[str, ...] = ()
    denied_implementations: tuple[str, ...] = ()
    max_cpu_threads: int | None = None
    max_ram_bytes: int | None = None
    gpu_available: bool = False
    max_estimated_cost: float | None = None
    max_estimated_latency_ms: float | None = None
    quality_requirements: tuple[CapabilityQualityRequirement, ...] = ()

    def __post_init__(self) -> None:
        _required_text("policy_id", self.policy_id)
        if not isinstance(self.allow_network, bool):
            raise ValueError("allow_network must be a boolean")
        if not isinstance(self.allow_shadow, bool):
            raise ValueError("allow_shadow must be a boolean")
        if not isinstance(self.gpu_available, bool):
            raise ValueError("gpu_available must be a boolean")
        if not isinstance(self.allowed_privacy, tuple) or any(
            not isinstance(item, CapabilityPrivacy) for item in self.allowed_privacy
        ):
            raise ValueError("allowed_privacy must contain CapabilityPrivacy values")
        if not self.allowed_privacy:
            raise ValueError("allowed_privacy cannot be empty")
        if len(self.allowed_privacy) != len(set(self.allowed_privacy)):
            raise ValueError("allowed_privacy cannot contain duplicates")
        for name, values in (
            ("allowed_providers", self.allowed_providers),
            ("denied_providers", self.denied_providers),
            ("allowed_implementations", self.allowed_implementations),
            ("denied_implementations", self.denied_implementations),
        ):
            _bounded_values(name, values)
        if set(self.allowed_providers) & set(self.denied_providers):
            raise ValueError("provider allowlist and denylist cannot overlap")
        if set(self.allowed_implementations) & set(self.denied_implementations):
            raise ValueError("implementation allowlist and denylist cannot overlap")
        _positive_optional_integer("max_cpu_threads", self.max_cpu_threads)
        _positive_optional_integer("max_ram_bytes", self.max_ram_bytes)
        max_estimated_cost = _nonnegative_optional(
            "max_estimated_cost",
            self.max_estimated_cost,
        )
        max_estimated_latency = _nonnegative_optional(
            "max_estimated_latency_ms",
            self.max_estimated_latency_ms,
        )
        object.__setattr__(self, "max_estimated_cost", max_estimated_cost)
        object.__setattr__(self, "max_estimated_latency_ms", max_estimated_latency)
        if not isinstance(self.quality_requirements, tuple) or any(
            not isinstance(item, CapabilityQualityRequirement) for item in self.quality_requirements
        ):
            raise ValueError("quality_requirements must be a tuple of CapabilityQualityRequirement")
        if len(self.quality_requirements) > MAX_CAPABILITY_METRICS:
            raise ValueError(
                f"quality_requirements cannot contain more than {MAX_CAPABILITY_METRICS} values"
            )
        metric_ids = tuple(item.metric_id for item in self.quality_requirements)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("quality_requirements cannot contain duplicates")
        object.__setattr__(
            self,
            "allowed_privacy",
            tuple(sorted(self.allowed_privacy, key=lambda item: item.value)),
        )
        for name in (
            "allowed_providers",
            "denied_providers",
            "allowed_implementations",
            "denied_implementations",
        ):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name))))
        if len(_canonical_bytes(self.to_dict())) > MAX_CAPABILITY_POLICY_BYTES:
            raise ValueError(f"capability policy cannot exceed {MAX_CAPABILITY_POLICY_BYTES} bytes")

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "allow_network": self.allow_network,
            "allow_shadow": self.allow_shadow,
            "allowed_privacy": [item.value for item in self.allowed_privacy],
            "allowed_providers": list(self.allowed_providers),
            "denied_providers": list(self.denied_providers),
            "allowed_implementations": list(self.allowed_implementations),
            "denied_implementations": list(self.denied_implementations),
            "max_cpu_threads": self.max_cpu_threads,
            "max_ram_bytes": self.max_ram_bytes,
            "gpu_available": self.gpu_available,
            "max_estimated_cost": self.max_estimated_cost,
            "max_estimated_latency_ms": self.max_estimated_latency_ms,
            "quality_requirements": [item.to_dict() for item in self.quality_requirements],
        }

    @property
    def contract_fingerprint(self) -> str:
        return _contract_fingerprint(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityCandidateEvaluation:
    """Hard-filter result and transparent preference facts for a candidate."""

    implementation_id: str
    manifest_fingerprint: str
    eligible: bool
    rejection_reasons: tuple[str, ...]
    preference_reasons: tuple[str, ...]
    availability: CapabilityAvailability | None

    def __post_init__(self) -> None:
        _required_text("implementation_id", self.implementation_id)
        _sha256_fingerprint("manifest_fingerprint", self.manifest_fingerprint)
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        _bounded_diagnostic_values("rejection_reasons", self.rejection_reasons)
        _bounded_diagnostic_values("preference_reasons", self.preference_reasons)
        if self.eligible and self.rejection_reasons:
            raise ValueError("eligible candidate cannot contain rejection reasons")

    def to_dict(self) -> dict[str, object]:
        return {
            "implementation_id": self.implementation_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "eligible": self.eligible,
            "rejection_reasons": list(self.rejection_reasons),
            "preference_reasons": list(self.preference_reasons),
            "availability": (None if self.availability is None else self.availability.to_dict()),
        }


@dataclass(frozen=True, slots=True)
class CapabilitySelection:
    """Explainable selection or explicit fail-closed abstention."""

    request: CapabilityRequest
    policy: CapabilityPolicy
    selected: CapabilityManifest | None
    candidates: tuple[CapabilityCandidateEvaluation, ...]
    explanation: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, CapabilityRequest):
            raise ValueError("request must be a CapabilityRequest")
        if not isinstance(self.policy, CapabilityPolicy):
            raise ValueError("policy must be a CapabilityPolicy")
        if self.selected is not None and not isinstance(
            self.selected,
            CapabilityManifest,
        ):
            raise ValueError("selected must be a CapabilityManifest when present")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, CapabilityCandidateEvaluation) for item in self.candidates
        ):
            raise ValueError("candidates must be a tuple of CapabilityCandidateEvaluation")
        if len(self.candidates) > MAX_CAPABILITY_CANDIDATES:
            raise ValueError(f"capability candidates cannot exceed {MAX_CAPABILITY_CANDIDATES}")
        candidate_ids = tuple(item.implementation_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("capability candidates cannot be duplicated")
        _bounded_diagnostic_values(
            "explanation",
            self.explanation,
            maximum=MAX_CAPABILITY_VALUES,
        )
        if self.selected is not None:
            selected_evaluation = next(
                (
                    item
                    for item in self.candidates
                    if item.implementation_id == self.selected.implementation_id
                ),
                None,
            )
            if selected_evaluation is None or not selected_evaluation.eligible:
                raise ValueError("selected capability must have an eligible evaluation")
            if selected_evaluation.manifest_fingerprint != self.selected.contract_fingerprint:
                raise ValueError("selected manifest does not match its candidate evaluation")
        if len(_canonical_bytes(self._payload())) > MAX_CAPABILITY_SELECTION_BYTES:
            raise ValueError(
                f"capability selection cannot exceed {MAX_CAPABILITY_SELECTION_BYTES} bytes"
            )

    @property
    def status(self) -> str:
        return "selected" if self.selected is not None else "unavailable"

    def _payload(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_SELECTION_SCHEMA,
            "status": self.status,
            "request": self.request.to_dict(),
            "policy": self.policy.to_dict(),
            "selected": None if self.selected is None else self.selected.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "explanation": list(self.explanation),
            "models_loaded": False,
            "models_downloaded": False,
        }

    @property
    def contract_fingerprint(self) -> str:
        return _contract_fingerprint(self._payload())

    @property
    def causal_fingerprint(self) -> str:
        selected_evaluation = (
            None
            if self.selected is None
            else next(
                item
                for item in self.candidates
                if item.implementation_id == self.selected.implementation_id
            )
        )
        return _contract_fingerprint(
            {
                "schema": CAPABILITY_SELECTION_SCHEMA,
                "request": self.request.to_dict(),
                "policy": self.policy.to_dict(),
                "selected": None if self.selected is None else self.selected.to_dict(),
                "evidence": (
                    [_execution_evidence(item) for item in self.candidates]
                    if selected_evaluation is None
                    else _execution_evidence(selected_evaluation)
                ),
                "explanation": list(self.explanation),
            }
        )

    @property
    def execution_fingerprint(self) -> str:
        """Fingerprint provider/policy/readiness facts that affect execution.

        Per-input facts such as byte size remain in the request and lineage input
        binding, but do not fragment a stage processing signature when they do
        not change the selected implementation.
        """

        selected_evaluation = (
            None
            if self.selected is None
            else next(
                item
                for item in self.candidates
                if item.implementation_id == self.selected.implementation_id
            )
        )
        return _contract_fingerprint(
            {
                "schema": CAPABILITY_SELECTION_SCHEMA,
                "execution_request": self.request.execution_contract(),
                "policy": self.policy.to_dict(),
                "selected": None if self.selected is None else self.selected.to_dict(),
                "evidence": (
                    [_execution_evidence(item) for item in self.candidates]
                    if selected_evaluation is None
                    else _execution_evidence(selected_evaluation)
                ),
                "explanation": list(self.explanation),
            }
        )

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload.update(
            {
                "request_fingerprint": self.request.contract_fingerprint,
                "execution_request_fingerprint": (self.request.execution_contract_fingerprint),
                "policy_fingerprint": self.policy.contract_fingerprint,
                "selected_manifest_fingerprint": (
                    None if self.selected is None else self.selected.contract_fingerprint
                ),
                "causal_fingerprint": self.causal_fingerprint,
                "execution_fingerprint": self.execution_fingerprint,
                "selection_fingerprint": self.contract_fingerprint,
            }
        )
        return payload


def _metric_map(manifest: CapabilityManifest) -> dict[str, CapabilityQualityMetric]:
    return {item.metric_id: item for item in manifest.quality_metrics}


def _availability_rejections(
    manifest: CapabilityManifest,
    request: CapabilityRequest,
    availability: CapabilityAvailability | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separate broker-owned availability failures from provider observations."""

    reasons: list[str] = []
    observed_reasons: tuple[str, ...] = ()
    if availability is None:
        reasons.append("runtime_availability_unknown")
    elif availability.execution_request_fingerprint != request.execution_contract_fingerprint:
        reasons.append("runtime_observation_request_mismatch")
    elif availability.manifest_fingerprint != manifest.contract_fingerprint:
        reasons.append("runtime_observation_manifest_mismatch")
    elif not availability.available:
        observed_reasons = availability.reasons
    return tuple(reasons), observed_reasons


def _request_rejections(
    manifest: CapabilityManifest,
    request: CapabilityRequest,
    policy: CapabilityPolicy,
) -> tuple[str, ...]:
    """Evaluate request/manifest compatibility without ranking preferences."""

    reasons: list[str] = []
    if request.modality not in manifest.modalities:
        reasons.append("modality_unsupported")
    if manifest.lifecycle is CapabilityLifecycle.DISABLED:
        reasons.append("implementation_disabled")
    elif manifest.lifecycle is CapabilityLifecycle.SHADOW and not policy.allow_shadow:
        reasons.append("shadow_not_allowed")
    if request.platform is None:
        reasons.append("platform_required")
    elif request.platform not in manifest.supported_platforms:
        reasons.append("platform_unsupported")
    if request.input_schema not in manifest.input_schemas:
        reasons.append("input_schema_unsupported")
    if request.output_schema not in manifest.output_schemas:
        reasons.append("output_schema_unsupported")
    if request.mime_type is None:
        reasons.append("mime_type_required")
    elif request.mime_type not in manifest.mime_types:
        reasons.append("mime_type_unsupported")
    if request.language is None:
        reasons.append("language_required")
    elif manifest.language_mode is CapabilityLanguageMode.DECLARED:
        if request.language not in manifest.languages:
            reasons.append("language_unsupported")
    elif manifest.language_mode is CapabilityLanguageMode.DETECTS:
        if request.language != "unknown" and request.language not in manifest.languages:
            reasons.append("language_unsupported")
    if manifest.max_input_bytes is not None:
        if request.input_bytes is None:
            reasons.append("input_size_unknown")
        elif request.input_bytes > manifest.max_input_bytes:
            reasons.append("input_limit_exceeded")
    if request.acceptable_reproducibility and not (
        set(request.acceptable_reproducibility) & set(manifest.reproducibility_classes)
    ):
        reasons.append("reproducibility_not_supported")
    if request.require_deterministic and manifest.deterministic is not True:
        reasons.append("determinism_not_guaranteed")
    if request.require_incremental and not manifest.incremental:
        reasons.append("incremental_not_supported")
    if request.require_cancellation and not manifest.cancellation:
        reasons.append("cancellation_not_supported")
    if request.require_checkpointing and not manifest.checkpointing:
        reasons.append("checkpointing_not_supported")
    return tuple(reasons)


def _policy_rejections(
    manifest: CapabilityManifest,
    policy: CapabilityPolicy,
) -> tuple[str, ...]:
    """Evaluate provider, privacy and resource policy hard filters."""

    reasons: list[str] = []
    if policy.allowed_providers and manifest.provider not in policy.allowed_providers:
        reasons.append("provider_not_allowed")
    if manifest.provider in policy.denied_providers:
        reasons.append("provider_denied")
    if (
        policy.allowed_implementations
        and manifest.implementation_id not in policy.allowed_implementations
    ):
        reasons.append("implementation_not_allowed")
    if manifest.implementation_id in policy.denied_implementations:
        reasons.append("implementation_denied")
    if not policy.allow_network and (
        manifest.network_required or manifest.privacy is not CapabilityPrivacy.LOCAL_ONLY
    ):
        reasons.append("network_forbidden")
    if manifest.privacy not in policy.allowed_privacy:
        reasons.append(f"privacy_not_allowed:{manifest.privacy.value}")
    if manifest.gpu_required and not policy.gpu_available:
        reasons.append("gpu_unavailable")
    if policy.max_cpu_threads is not None:
        if manifest.cpu_threads is None:
            reasons.append("cpu_requirement_unknown")
        elif manifest.cpu_threads > policy.max_cpu_threads:
            reasons.append("cpu_budget_exceeded")
    if policy.max_ram_bytes is not None:
        if manifest.ram_bytes is None:
            reasons.append("ram_requirement_unknown")
        elif manifest.ram_bytes > policy.max_ram_bytes:
            reasons.append("ram_budget_exceeded")
    if policy.max_estimated_cost is not None:
        if manifest.estimated_cost is None:
            reasons.append("estimated_cost_unknown")
        elif manifest.estimated_cost > policy.max_estimated_cost:
            reasons.append("estimated_cost_budget_exceeded")
    if policy.max_estimated_latency_ms is not None:
        if manifest.estimated_latency_ms is None:
            reasons.append("estimated_latency_unknown")
        elif manifest.estimated_latency_ms > policy.max_estimated_latency_ms:
            reasons.append("estimated_latency_budget_exceeded")
    return tuple(reasons)


def _quality_rejections(
    manifest: CapabilityManifest,
    policy: CapabilityPolicy,
) -> tuple[str, ...]:
    """Evaluate measured quality requirements and their exact evidence."""

    reasons: list[str] = []
    metrics = _metric_map(manifest)
    for requirement in policy.quality_requirements:
        metric = metrics.get(requirement.metric_id)
        if metric is None:
            reasons.append(
                _bounded_derived_text("quality_metric_unavailable", requirement.metric_id)
            )
            continue
        if requirement.unit is not None and metric.unit != requirement.unit:
            reasons.append(
                _bounded_derived_text("quality_unit_mismatch", requirement.metric_id)
            )
            continue
        if requirement.evidence is None:
            reasons.append(
                _bounded_derived_text("quality_evidence_required", requirement.metric_id)
            )
            continue
        if metric.evidence != requirement.evidence:
            reasons.append(
                _bounded_derived_text("quality_evidence_mismatch", requirement.metric_id)
            )
            continue
        if (
            requirement.prefer_higher is not None
            and metric.higher_is_better is not requirement.prefer_higher
        ):
            reasons.append(
                _bounded_derived_text("quality_direction_mismatch", requirement.metric_id)
            )
            continue
        if requirement.minimum is not None and metric.value < requirement.minimum:
            reasons.append(
                _bounded_derived_text("quality_below_minimum", requirement.metric_id)
            )
        if requirement.maximum is not None and metric.value > requirement.maximum:
            reasons.append(
                _bounded_derived_text("quality_above_maximum", requirement.metric_id)
            )
    return tuple(reasons)


def _execution_evidence(
    evaluation: CapabilityCandidateEvaluation,
) -> dict[str, object]:
    availability = evaluation.availability
    return {
        "implementation_id": evaluation.implementation_id,
        "eligible": evaluation.eligible,
        "rejection_reasons": list(evaluation.rejection_reasons),
        "preference_reasons": list(evaluation.preference_reasons),
        "availability": (
            None
            if availability is None
            else {
                "manifest_fingerprint": availability.manifest_fingerprint,
                "available": availability.available,
                "reasons": list(availability.reasons),
                "observed_components": list(availability.observed_components),
                "binary_identities": [item.to_dict() for item in availability.binary_identities],
            }
        ),
    }


class CapabilityBroker:
    """Select one implementation through hard filters and stable preferences."""

    def __init__(
        self,
        manifests: Iterable[CapabilityManifest],
        availability: Iterable[CapabilityAvailability],
    ) -> None:
        selected_manifests = tuple(islice(manifests, MAX_CAPABILITY_MANIFESTS + 1))
        if len(selected_manifests) > MAX_CAPABILITY_MANIFESTS:
            raise ValueError(f"capability manifests cannot exceed {MAX_CAPABILITY_MANIFESTS}")
        if any(not isinstance(item, CapabilityManifest) for item in selected_manifests):
            raise ValueError("capability manifests must contain CapabilityManifest values")
        manifest_ids = tuple(item.implementation_id for item in selected_manifests)
        if len(manifest_ids) != len(set(manifest_ids)):
            raise ValueError("capability implementation IDs cannot be duplicated")
        observed = tuple(islice(availability, MAX_CAPABILITY_MANIFESTS + 1))
        if len(observed) > MAX_CAPABILITY_MANIFESTS:
            raise ValueError(f"capability availability cannot exceed {MAX_CAPABILITY_MANIFESTS}")
        if any(not isinstance(item, CapabilityAvailability) for item in observed):
            raise ValueError("capability availability must contain CapabilityAvailability values")
        observed_ids = tuple(item.implementation_id for item in observed)
        if len(observed_ids) != len(set(observed_ids)):
            raise ValueError("capability availability IDs cannot be duplicated")
        unknown = set(observed_ids) - set(manifest_ids)
        if unknown:
            raise ValueError(
                "capability availability references unknown implementations: "
                + ",".join(sorted(unknown))
            )
        self._manifests = tuple(sorted(selected_manifests, key=lambda item: item.implementation_id))
        self._availability = {
            item.implementation_id: item
            for item in sorted(observed, key=lambda item: item.implementation_id)
        }

    @property
    def manifests(self) -> tuple[CapabilityManifest, ...]:
        return self._manifests

    def _evaluate(
        self,
        manifest: CapabilityManifest,
        request: CapabilityRequest,
        policy: CapabilityPolicy,
    ) -> CapabilityCandidateEvaluation:
        availability = self._availability.get(manifest.implementation_id)
        availability_reasons, observed_reasons = _availability_rejections(
            manifest,
            request,
            availability,
        )
        reasons = (
            *availability_reasons,
            *_request_rejections(manifest, request, policy),
            *_policy_rejections(manifest, policy),
            *_quality_rejections(manifest, policy),
        )

        # Runtime observations and hard filters may report the same cause.  An
        # abstention must remain data, not become an exception because two
        # independent layers agreed on one reason.
        rejection_reasons = tuple(dict.fromkeys((*reasons, *observed_reasons)))
        if len(rejection_reasons) > MAX_CAPABILITY_EVIDENCE_VALUES:
            rejection_reasons = tuple(
                item for item in rejection_reasons if item != "additional_rejections_truncated"
            )
            rejection_reasons = (
                *rejection_reasons[: MAX_CAPABILITY_EVIDENCE_VALUES - 1],
                "additional_rejections_truncated",
            )
        preferences = self._preference_reasons(manifest, request, policy)
        if len(preferences) > MAX_CAPABILITY_EVIDENCE_VALUES:
            preferences = (
                *preferences[: MAX_CAPABILITY_EVIDENCE_VALUES - 1],
                "additional_preferences_truncated",
            )
        return CapabilityCandidateEvaluation(
            implementation_id=manifest.implementation_id,
            manifest_fingerprint=manifest.contract_fingerprint,
            eligible=not rejection_reasons,
            rejection_reasons=rejection_reasons,
            preference_reasons=preferences,
            availability=availability,
        )

    @staticmethod
    def _preference_reasons(
        manifest: CapabilityManifest,
        request: CapabilityRequest,
        policy: CapabilityPolicy,
    ) -> tuple[str, ...]:
        reasons = [f"privacy:{manifest.privacy.value}"]
        if request.mime_type is not None:
            reasons.append(
                "mime:exact" if request.mime_type in manifest.mime_types else "mime:unsupported"
            )
        if request.language is not None:
            if manifest.language_mode is CapabilityLanguageMode.AGNOSTIC:
                reasons.append("language:agnostic")
            elif request.language == "unknown":
                reasons.append("language:detects")
            else:
                reasons.append("language:exact")
        metrics = _metric_map(manifest)
        for requirement in policy.quality_requirements:
            metric = metrics.get(requirement.metric_id)
            if metric is not None:
                reasons.append(
                    _bounded_derived_text(
                        "quality",
                        f"{metric.metric_id}={metric.value:g}{metric.unit}",
                    )
                )
        if manifest.estimated_cost is not None:
            reasons.append(f"estimated_cost:{manifest.estimated_cost:g}")
        if manifest.estimated_latency_ms is not None:
            reasons.append(f"estimated_latency_ms:{manifest.estimated_latency_ms:g}")
        if manifest.ram_bytes is not None:
            reasons.append(_bounded_derived_text("ram_bytes", str(manifest.ram_bytes)))
        return tuple(reasons)

    @staticmethod
    def _rank(
        manifest: CapabilityManifest,
        request: CapabilityRequest,
        policy: CapabilityPolicy,
    ) -> tuple[object, ...]:
        metrics = _metric_map(manifest)
        quality: list[float] = []
        for requirement in policy.quality_requirements:
            metric = metrics[requirement.metric_id]
            if requirement.prefer_higher is True:
                quality.append(-float(metric.value))
            elif requirement.prefer_higher is False:
                quality.append(float(metric.value))
        return (
            0 if request.mime_type in manifest.mime_types else 1,
            (
                0
                if request.language in manifest.languages
                else 1
                if manifest.language_mode is CapabilityLanguageMode.DETECTS
                else 2
            ),
            0 if manifest.privacy is CapabilityPrivacy.LOCAL_ONLY else 1,
            0 if manifest.deterministic is True else 1 if manifest.deterministic is None else 2,
            tuple(quality),
            manifest.estimated_cost is None,
            math.inf if manifest.estimated_cost is None else manifest.estimated_cost,
            manifest.estimated_latency_ms is None,
            (math.inf if manifest.estimated_latency_ms is None else manifest.estimated_latency_ms),
            manifest.ram_bytes is None,
            math.inf if manifest.ram_bytes is None else manifest.ram_bytes,
        )

    def select(
        self,
        request: CapabilityRequest,
        policy: CapabilityPolicy | None = None,
    ) -> CapabilitySelection:
        """Return one deterministic selection or an explicit abstention."""

        if not isinstance(request, CapabilityRequest):
            raise ValueError("request must be a CapabilityRequest")
        if policy is not None and not isinstance(policy, CapabilityPolicy):
            raise ValueError("policy must be a CapabilityPolicy when present")
        effective_policy = CapabilityPolicy() if policy is None else policy
        manifests = tuple(
            item for item in self._manifests if item.capability_id == request.capability_id
        )
        if len(manifests) > MAX_CAPABILITY_CANDIDATES:
            registry_fingerprint = _contract_fingerprint(
                [item.contract_fingerprint for item in manifests]
            )
            return CapabilitySelection(
                request,
                effective_policy,
                None,
                (),
                (
                    _bounded_derived_text("unavailable", request.capability_id),
                    _bounded_derived_text(
                        "candidate_limit_exceeded",
                        f"{len(manifests)}:{registry_fingerprint}",
                    ),
                ),
            )
        evaluations = tuple(self._evaluate(item, request, effective_policy) for item in manifests)
        by_id = {item.implementation_id: item for item in manifests}
        eligible = tuple(by_id[item.implementation_id] for item in evaluations if item.eligible)
        if not eligible:
            explanation = (
                _bounded_derived_text("unavailable", request.capability_id),
                (
                    "capability_not_declared"
                    if not manifests
                    else f"rejected_candidates:{len(evaluations)}"
                ),
            )
            return CapabilitySelection(
                request,
                effective_policy,
                None,
                evaluations,
                explanation,
            )
        ranked = tuple(
            sorted(
                ((self._rank(item, request, effective_policy), item) for item in eligible),
                key=lambda pair: (pair[0], pair[1].implementation_id),
            )
        )
        if not ranked:  # pragma: no cover - guarded by the eligible branch above
            raise RuntimeError("eligible capability ranking unexpectedly became empty")
        best_rank = ranked[0][0]
        tied = tuple(item for rank, item in ranked if rank == best_rank)
        if len(tied) > 1:
            identifiers = ",".join(item.implementation_id for item in tied)
            return CapabilitySelection(
                request,
                effective_policy,
                None,
                evaluations,
                (
                    _bounded_derived_text("unavailable", request.capability_id),
                    _bounded_derived_text("ambiguous_capability_selection", identifiers),
                ),
            )
        selected = ranked[0][1]
        selected_evaluation = next(
            item for item in evaluations if item.implementation_id == selected.implementation_id
        )
        return CapabilitySelection(
            request,
            effective_policy,
            selected,
            evaluations,
            (
                _bounded_derived_text("selected", selected.implementation_id),
                f"eligible_candidates:{len(eligible)}",
                *selected_evaluation.preference_reasons,
            ),
        )


__all__ = (
    "CAPABILITY_MANIFEST_SCHEMA",
    "CAPABILITY_REQUEST_SCHEMA",
    "CAPABILITY_SELECTION_SCHEMA",
    "MAX_CAPABILITY_MANIFESTS",
    "CapabilityAvailability",
    "CapabilityBinaryIdentity",
    "CapabilityBroker",
    "CapabilityCandidateEvaluation",
    "CapabilityLanguageMode",
    "CapabilityLifecycle",
    "CapabilityManifest",
    "CapabilityMimeBinaryAlternatives",
    "CapabilityPolicy",
    "CapabilityPrivacy",
    "CapabilityQualityMetric",
    "CapabilityQualityRequirement",
    "CapabilityRequest",
    "CapabilitySelection",
)
