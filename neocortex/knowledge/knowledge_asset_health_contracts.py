"""Typed read-only contracts for causal Knowledge asset health.

The report describes only evidence published by existing state owners.  It is
not a filesystem probe, a quality score, or authority to mutate the corpus.
"""

from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION = 1
KNOWLEDGE_ASSET_HEALTH_SCHEMA = "neocortex.knowledge-asset-health/v1"
MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES = 8

_MAX_FACTS = 4
_MAX_VALUES_PER_ITEM = 32
_MAX_CODES = 32
_MAX_TEXT_CHARS = 32_768
_MAX_TOTAL_VALUE_CHARS = 65_536
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")


class KnowledgeAssetHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    PROTECTED = "protected"
    UNKNOWN = "unknown"


class KnowledgeAssetHealthCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_EVIDENCE = "no_evidence"
    ABSTAINED = "abstained"


class KnowledgeAssetHealthStage(StrEnum):
    INVENTORY = "inventory"
    SOURCE_OWNER = "source_owner"
    CATALOG = "catalog"
    KNOWLEDGE_SEARCH = "knowledge_search"


_STAGE_ORDER = {stage: index for index, stage in enumerate(KnowledgeAssetHealthStage)}


def _required_text(name: str, value: object, *, maximum: int = _MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-blank text of at most {maximum} characters")
    return value


def _optional_text(name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _required_text(name, value)


def _required_code(name: str, value: object) -> str:
    selected = _required_text(name, value, maximum=128)
    if _CODE_PATTERN.fullmatch(selected) is None:
        raise ValueError(f"{name} must be a canonical lowercase code")
    return selected


def _canonical_unsigned(name: str, value: str, *, maximum: int) -> int:
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or (value != "0" and value.startswith("0"))
    ):
        raise ValueError(f"{name} must be canonical unsigned decimal text")
    decoded = int(value)
    if decoded > maximum:
        raise ValueError(f"{name} exceeds its supported range")
    return decoded


@dataclass(frozen=True, slots=True)
class KnowledgeAssetIdentity:
    volume_id: int
    file_id: int
    birthtime_ns: int

    def __post_init__(self) -> None:
        for name, value in (("volume_id", self.volume_id), ("file_id", self.file_id)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**128:
                raise ValueError(f"{name} must be an unsigned 128-bit integer")
        if (
            isinstance(self.birthtime_ns, bool)
            or not isinstance(self.birthtime_ns, int)
            or not -1 <= self.birthtime_ns < 2**63
        ):
            raise ValueError("birthtime_ns must be -1 or a non-negative signed 64-bit integer")

    @property
    def resource_id(self) -> str:
        return f"resource:file:{self.volume_id}:{self.file_id}:{self.birthtime_ns}"

    def to_dict(self) -> dict[str, object]:
        return {
            "birthtime_ns": self.birthtime_ns,
            "file_id": self.file_id,
            "resource_id": self.resource_id,
            "volume_id": self.volume_id,
        }


def parse_knowledge_asset_resource_id(resource_id: str) -> KnowledgeAssetIdentity:
    """Parse only the canonical physical resource identity used by Knowledge."""

    selected = _required_text("resource_id", resource_id, maximum=256)
    prefix = "resource:file:"
    if not selected.startswith(prefix):
        raise ValueError("resource_id must use the resource:file identity scheme")
    components = selected[len(prefix) :].split(":")
    if len(components) != 3:
        raise ValueError("resource_id must contain volume, file, and birthtime components")
    volume_id = _canonical_unsigned("volume_id", components[0], maximum=2**128 - 1)
    file_id = _canonical_unsigned("file_id", components[1], maximum=2**128 - 1)
    if components[2] == "-1":
        birthtime_ns = -1
    else:
        birthtime_ns = _canonical_unsigned(
            "birthtime_ns",
            components[2],
            maximum=2**63 - 1,
        )
    identity = KnowledgeAssetIdentity(volume_id, file_id, birthtime_ns)
    if identity.resource_id != selected:
        raise ValueError("resource_id is not canonical")
    return identity


@dataclass(frozen=True, slots=True)
class KnowledgeAssetHealthQuery:
    resource_id: str

    def __post_init__(self) -> None:
        parse_knowledge_asset_resource_id(self.resource_id)

    @property
    def identity(self) -> KnowledgeAssetIdentity:
        return parse_knowledge_asset_resource_id(self.resource_id)


@dataclass(frozen=True, slots=True)
class KnowledgeAssetHealthValue:
    name: str
    value: str

    def __post_init__(self) -> None:
        _required_code("value name", self.name)
        _required_text("value", self.value)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


def _validate_values(values: tuple[KnowledgeAssetHealthValue, ...]) -> None:
    if len(values) > _MAX_VALUES_PER_ITEM:
        raise ValueError(f"health evidence accepts at most {_MAX_VALUES_PER_ITEM} values")
    if any(not isinstance(value, KnowledgeAssetHealthValue) for value in values):
        raise ValueError("health evidence values must be KnowledgeAssetHealthValue instances")
    names = tuple(value.name for value in values)
    if names != tuple(sorted(names)) or len(set(names)) != len(names):
        raise ValueError("health evidence values must have unique sorted names")
    if sum(len(value.name) + len(value.value) for value in values) > _MAX_TOTAL_VALUE_CHARS:
        raise ValueError("health evidence values exceed the total character bound")


def _validate_digest(name: str, value: object) -> str:
    selected = _required_text(name, value, maximum=71)
    if _DIGEST_PATTERN.fullmatch(selected) is None:
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return selected


@dataclass(frozen=True, slots=True)
class KnowledgeAssetHealthFact:
    stage: KnowledgeAssetHealthStage
    owner: str
    schema_version: int
    record_id: str
    status: str
    projection_digest: str
    values: tuple[KnowledgeAssetHealthValue, ...]
    publication_id: str | None = None
    complete: bool = True
    bounded: bool = True
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.stage, KnowledgeAssetHealthStage):
            raise ValueError("stage must be a KnowledgeAssetHealthStage")
        _required_code("owner", self.owner)
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ValueError("schema_version must be a positive integer")
        _required_text("record_id", self.record_id, maximum=1_024)
        _required_code("status", self.status)
        _validate_digest("projection_digest", self.projection_digest)
        _validate_values(self.values)
        _optional_text("publication_id", self.publication_id)
        if any(
            not isinstance(flag, bool) for flag in (self.complete, self.bounded, self.truncated)
        ):
            raise ValueError("fact completeness flags must be boolean")
        if not self.bounded or self.truncated:
            raise ValueError("a selected causal fact must be bounded and untruncated")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "bounded": self.bounded,
            "complete": self.complete,
            "owner": self.owner,
            "projection_digest": self.projection_digest,
            "record_id": self.record_id,
            "schema_version": self.schema_version,
            "stage": self.stage.value,
            "status": self.status,
            "truncated": self.truncated,
            "values": [value.to_dict() for value in self.values],
        }
        if self.publication_id is not None:
            payload["publication_id"] = self.publication_id
        return payload


@dataclass(frozen=True, slots=True)
class KnowledgeAssetHealthExample:
    stage: KnowledgeAssetHealthStage
    code: str
    record_id: str
    projection_digest: str
    values: tuple[KnowledgeAssetHealthValue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stage, KnowledgeAssetHealthStage):
            raise ValueError("stage must be a KnowledgeAssetHealthStage")
        _required_code("example code", self.code)
        _required_text("example record_id", self.record_id, maximum=1_024)
        _validate_digest("example projection_digest", self.projection_digest)
        _validate_values(self.values)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "projection_digest": self.projection_digest,
            "record_id": self.record_id,
            "stage": self.stage.value,
            "values": [value.to_dict() for value in self.values],
        }


def _validate_codes(name: str, values: tuple[str, ...]) -> None:
    if len(values) > _MAX_CODES:
        raise ValueError(f"{name} accepts at most {_MAX_CODES} codes")
    for value in values:
        _required_code(name, value)
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise ValueError(f"{name} must contain unique sorted codes")


def _facts_payload(
    *,
    resource_id: str,
    knowledge_snapshot_id: str,
    facts: tuple[KnowledgeAssetHealthFact, ...],
    gaps: tuple[str, ...],
    counterevidence: tuple[str, ...],
    examples: tuple[KnowledgeAssetHealthExample, ...],
    examples_truncated: bool,
) -> dict[str, object]:
    return {
        "counterevidence": list(counterevidence),
        "examples": [example.to_dict() for example in examples],
        "examples_truncated": examples_truncated,
        "facts": [fact.to_dict() for fact in facts],
        "gaps": list(gaps),
        "knowledge_snapshot_id": knowledge_snapshot_id,
        "resource_id": resource_id,
        "schema": "neocortex.knowledge-asset-facts/v1",
    }


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class KnowledgeAssetFactSnapshot:
    resource_id: str
    knowledge_snapshot_id: str
    fact_snapshot_id: str
    facts: tuple[KnowledgeAssetHealthFact, ...]
    gaps: tuple[str, ...] = ()
    counterevidence: tuple[str, ...] = ()
    examples: tuple[KnowledgeAssetHealthExample, ...] = ()
    examples_truncated: bool = False

    def __post_init__(self) -> None:
        parse_knowledge_asset_resource_id(self.resource_id)
        _required_text("knowledge_snapshot_id", self.knowledge_snapshot_id, maximum=512)
        _validate_digest("fact_snapshot_id", self.fact_snapshot_id)
        if len(self.facts) > _MAX_FACTS or any(
            not isinstance(fact, KnowledgeAssetHealthFact) for fact in self.facts
        ):
            raise ValueError(f"fact snapshot accepts at most {_MAX_FACTS} typed facts")
        stages = tuple(fact.stage for fact in self.facts)
        if stages != tuple(sorted(stages, key=_STAGE_ORDER.__getitem__)) or len(set(stages)) != len(
            stages
        ):
            raise ValueError("fact snapshot stages must be unique and ordered")
        _validate_codes("gaps", self.gaps)
        _validate_codes("counterevidence", self.counterevidence)
        if len(self.examples) > MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES or any(
            not isinstance(example, KnowledgeAssetHealthExample) for example in self.examples
        ):
            raise ValueError("fact snapshot examples exceed the documented bound")
        expected_examples = tuple(
            sorted(
                self.examples,
                key=lambda value: (
                    _STAGE_ORDER[value.stage],
                    value.code,
                    value.record_id,
                    value.projection_digest,
                ),
            )
        )
        if self.examples != expected_examples:
            raise ValueError("fact snapshot examples must be deterministically ordered")
        if not isinstance(self.examples_truncated, bool):
            raise ValueError("examples_truncated must be boolean")
        expected = _sha256_payload(
            _facts_payload(
                resource_id=self.resource_id,
                knowledge_snapshot_id=self.knowledge_snapshot_id,
                facts=self.facts,
                gaps=self.gaps,
                counterevidence=self.counterevidence,
                examples=self.examples,
                examples_truncated=self.examples_truncated,
            )
        )
        if self.fact_snapshot_id != expected:
            raise ValueError("fact_snapshot_id does not match the canonical fact payload")

    @classmethod
    def create(
        cls,
        *,
        resource_id: str,
        knowledge_snapshot_id: str,
        facts: tuple[KnowledgeAssetHealthFact, ...],
        gaps: tuple[str, ...] = (),
        counterevidence: tuple[str, ...] = (),
        examples: tuple[KnowledgeAssetHealthExample, ...] = (),
        examples_truncated: bool = False,
    ) -> KnowledgeAssetFactSnapshot:
        payload = _facts_payload(
            resource_id=resource_id,
            knowledge_snapshot_id=knowledge_snapshot_id,
            facts=facts,
            gaps=gaps,
            counterevidence=counterevidence,
            examples=examples,
            examples_truncated=examples_truncated,
        )
        return cls(
            resource_id=resource_id,
            knowledge_snapshot_id=knowledge_snapshot_id,
            fact_snapshot_id=_sha256_payload(payload),
            facts=facts,
            gaps=gaps,
            counterevidence=counterevidence,
            examples=examples,
            examples_truncated=examples_truncated,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeAssetHealthReport:
    resource_id: str
    health: KnowledgeAssetHealthState
    completeness: KnowledgeAssetHealthCompleteness
    reason_code: str
    knowledge_snapshot_id: str | None
    fact_snapshot_id: str | None
    snapshot_consistency: str
    attempts: int
    facts: tuple[KnowledgeAssetHealthFact, ...] = ()
    gaps: tuple[str, ...] = ()
    counterevidence: tuple[str, ...] = ()
    examples: tuple[KnowledgeAssetHealthExample, ...] = ()
    examples_truncated: bool = False
    contract_version: int = KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION
    operation: str = "knowledge-health"
    read_only: bool = True
    advisory_only: bool = True
    mutation_authorized: bool = False

    def __post_init__(self) -> None:
        parse_knowledge_asset_resource_id(self.resource_id)
        if not isinstance(self.health, KnowledgeAssetHealthState):
            raise ValueError("health must be a KnowledgeAssetHealthState")
        if not isinstance(self.completeness, KnowledgeAssetHealthCompleteness):
            raise ValueError("completeness must be a KnowledgeAssetHealthCompleteness")
        _required_code("reason_code", self.reason_code)
        _optional_text("knowledge_snapshot_id", self.knowledge_snapshot_id)
        if self.fact_snapshot_id is not None:
            _validate_digest("fact_snapshot_id", self.fact_snapshot_id)
        if self.snapshot_consistency not in {"stable", "snapshot_changed", "unavailable"}:
            raise ValueError("snapshot_consistency is not a supported state")
        if isinstance(self.attempts, bool) or self.attempts not in {1, 2}:
            raise ValueError("attempts must be one or two")
        if self.contract_version != KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION:
            raise ValueError("unsupported Knowledge asset health contract version")
        if self.operation != "knowledge-health":
            raise ValueError("operation must remain knowledge-health")
        if self.read_only is not True or self.advisory_only is not True:
            raise ValueError("Knowledge asset health must remain read-only and advisory")
        if self.mutation_authorized is not False:
            raise ValueError("Knowledge asset health cannot authorize mutation")
        snapshot = KnowledgeAssetFactSnapshot.create(
            resource_id=self.resource_id,
            knowledge_snapshot_id=self.knowledge_snapshot_id or "unavailable",
            facts=self.facts,
            gaps=self.gaps,
            counterevidence=self.counterevidence,
            examples=self.examples,
            examples_truncated=self.examples_truncated,
        )
        if self.fact_snapshot_id is not None and self.fact_snapshot_id != snapshot.fact_snapshot_id:
            raise ValueError("report fact_snapshot_id does not match its evidence payload")
        if self.fact_snapshot_id is None and (self.facts or self.examples):
            raise ValueError("a report with fact evidence requires fact_snapshot_id")
        if self.health is KnowledgeAssetHealthState.HEALTHY and (
            self.completeness is not KnowledgeAssetHealthCompleteness.COMPLETE
            or self.snapshot_consistency != "stable"
            or self.gaps
            or self.counterevidence
            or len(self.facts) != _MAX_FACTS
        ):
            raise ValueError("healthy requires one complete stable four-stage causal trace")

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisory_only": self.advisory_only,
            "attempts": self.attempts,
            "completeness": self.completeness.value,
            "contract_version": self.contract_version,
            "counterevidence": list(self.counterevidence),
            "examples": [example.to_dict() for example in self.examples],
            "examples_truncated": self.examples_truncated,
            "fact_snapshot_id": self.fact_snapshot_id,
            "facts": [fact.to_dict() for fact in self.facts],
            "gaps": list(self.gaps),
            "health": self.health.value,
            "knowledge_snapshot_id": self.knowledge_snapshot_id,
            "mutation_authorized": self.mutation_authorized,
            "operation": self.operation,
            "read_only": self.read_only,
            "reason_code": self.reason_code,
            "resource_id": self.resource_id,
            "schema": KNOWLEDGE_ASSET_HEALTH_SCHEMA,
            "snapshot_consistency": self.snapshot_consistency,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


__all__ = [
    "KNOWLEDGE_ASSET_HEALTH_CONTRACT_VERSION",
    "KNOWLEDGE_ASSET_HEALTH_SCHEMA",
    "MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES",
    "KnowledgeAssetFactSnapshot",
    "KnowledgeAssetHealthCompleteness",
    "KnowledgeAssetHealthExample",
    "KnowledgeAssetHealthFact",
    "KnowledgeAssetHealthQuery",
    "KnowledgeAssetHealthReport",
    "KnowledgeAssetHealthStage",
    "KnowledgeAssetHealthState",
    "KnowledgeAssetHealthValue",
    "KnowledgeAssetIdentity",
    "parse_knowledge_asset_resource_id",
]
