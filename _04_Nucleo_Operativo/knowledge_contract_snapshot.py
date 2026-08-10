"""Validation and deterministic construction for logical Knowledge snapshots.
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_contract_snapshot.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The public dataclasses remain in ``knowledge_contracts`` for stable type and
pickle identity. This helper has no runtime dependency on that facade.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from .knowledge_contract_protocols import (
        ActiveModel,
        KnowledgeSnapshot,
        LogicalWatermark,
        OwnerSnapshot,
        PublicationHead,
    )

SnapshotT = TypeVar("SnapshotT")
RequiredText = Callable[[str, str], str]
OptionalText = Callable[[str, str | None], str | None]
CanonicalJson = Callable[[Mapping[str, object]], str]
FingerprintText = Callable[[str], Any]


def validate_publication_head(
    contract: PublicationHead,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
) -> None:
    required_text_fn("publication scope", contract.scope)
    required_text_fn("publication_id", contract.publication_id)
    optional_text_fn("model_signature", contract.model_signature)
    if isinstance(contract.generation, bool) or contract.generation < 0:
        raise ValueError("publication generation cannot be negative")


def validate_logical_watermark(
    contract: LogicalWatermark,
    *,
    required_text_fn: RequiredText,
) -> None:
    required_text_fn("watermark name", contract.name)
    required_text_fn("watermark value", contract.value)


def validate_active_model(
    contract: ActiveModel,
    *,
    required_text_fn: RequiredText,
) -> None:
    required_text_fn("model signature", contract.signature)
    required_text_fn("vector_space", contract.vector_space)
    required_text_fn("modality", contract.modality)
    if isinstance(contract.dimensions, bool) or contract.dimensions < 1:
        raise ValueError("model dimensions must be positive")
    if isinstance(contract.generation, bool) or contract.generation < 0:
        raise ValueError("model generation cannot be negative")


def validate_owner_snapshot(
    contract: OwnerSnapshot,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
    owner_availability_type: Any,
) -> None:
    required_text_fn("owner", contract.owner)
    if contract.expected_schema_version < 1:
        raise ValueError("expected schema version must be positive")
    if (
        contract.observed_schema_version is not None
        and contract.observed_schema_version < 0
    ):
        raise ValueError("observed schema version cannot be negative")
    if contract.state is owner_availability_type.AVAILABLE and (
        contract.observed_schema_version is None
        or contract.observed_schema_version > contract.expected_schema_version
    ):
        raise ValueError("available owner must expose a supported schema")
    if contract.data_version_before is not None and contract.data_version_before < 0:
        raise ValueError("data_version cannot be negative")
    if contract.data_version_after is not None and contract.data_version_after < 0:
        raise ValueError("data_version cannot be negative")
    if not isinstance(contract.identity_changed, bool):
        raise ValueError("identity_changed must be boolean")
    optional_text_fn("owner warning", contract.warning)
    optional_text_fn("owner error code", contract.error_code)
    if len({head.scope for head in contract.publications}) != len(
        contract.publications
    ):
        raise ValueError("publication scopes must be unique per owner")
    if len({item.name for item in contract.watermarks}) != len(contract.watermarks):
        raise ValueError("watermark names must be unique per owner")


def owner_snapshot_changed(contract: OwnerSnapshot) -> bool:
    return contract.identity_changed or (
        contract.data_version_before is not None
        and contract.data_version_after is not None
        and contract.data_version_before != contract.data_version_after
    )


def _validate_snapshot_header(
    contract: KnowledgeSnapshot,
    *,
    required_text_fn: RequiredText,
) -> None:
    required_text_fn("source_version", contract.source_version)
    required_text_fn("captured_at_utc", contract.captured_at_utc)
    required_text_fn("snapshot_id", contract.snapshot_id)
    if not contract.captured_at_utc.endswith("Z"):
        raise ValueError("captured_at_utc must be an explicit UTC timestamp")
    if contract.captured_monotonic_ns < 0:
        raise ValueError("captured monotonic time cannot be negative")
    if isinstance(contract.attempts, bool) or not 1 <= contract.attempts <= 2:
        raise ValueError("snapshot attempts must be one or two")


def _validate_snapshot_members(contract: KnowledgeSnapshot) -> None:
    if len({owner.owner for owner in contract.owners}) != len(contract.owners):
        raise ValueError("snapshot owners must be unique")
    if len({model.signature for model in contract.active_models}) != len(
        contract.active_models
    ):
        raise ValueError("active model signatures must be unique")


def _validate_snapshot_consistency(
    contract: KnowledgeSnapshot,
    *,
    snapshot_consistency_type: Any,
    changed_owners: tuple[OwnerSnapshot, ...],
) -> None:
    if contract.consistency is snapshot_consistency_type.STABLE and changed_owners:
        raise ValueError("a stable snapshot cannot contain a changed owner")
    if contract.consistency is snapshot_consistency_type.SNAPSHOT_CHANGED:
        if contract.attempts != 2:
            raise ValueError("snapshot_changed requires exactly two attempts")
        if not changed_owners:
            raise ValueError("snapshot_changed requires at least one changed owner")


def _validate_active_model_publications(
    contract: KnowledgeSnapshot,
    *,
    owner_availability_type: Any,
) -> None:
    if not contract.active_models:
        return
    semantic_owner = next(
        (owner for owner in contract.owners if owner.owner == "semantic"),
        None,
    )
    compatible_publications: set[tuple[str, int]] = set()
    if (
        semantic_owner is not None
        and semantic_owner.state is owner_availability_type.AVAILABLE
    ):
        compatible_publications = {
            (head.model_signature, head.generation)
            for head in semantic_owner.publications
            if head.model_signature is not None
        }
    if any(
        (model.signature, model.generation) not in compatible_publications
        for model in contract.active_models
    ):
        raise ValueError(
            "active model must correspond to a compatible semantic publication"
        )


def validate_knowledge_snapshot(
    contract: KnowledgeSnapshot,
    *,
    required_text_fn: RequiredText,
    snapshot_consistency_type: Any,
    owner_availability_type: Any,
) -> None:
    _validate_snapshot_header(contract, required_text_fn=required_text_fn)
    _validate_snapshot_members(contract)
    changed_owners = tuple(owner for owner in contract.owners if owner.changed)
    _validate_snapshot_consistency(
        contract,
        snapshot_consistency_type=snapshot_consistency_type,
        changed_owners=changed_owners,
    )
    _validate_active_model_publications(
        contract,
        owner_availability_type=owner_availability_type,
    )
    for warning in contract.warnings:
        required_text_fn("snapshot warning", warning)


def create_knowledge_snapshot(
    cls: Callable[..., SnapshotT],
    *,
    source_version: str,
    captured_at_utc: str,
    captured_monotonic_ns: int,
    owners: tuple[OwnerSnapshot, ...],
    active_models: tuple[ActiveModel, ...],
    consistency: Any,
    attempts: int,
    warnings: tuple[str, ...],
    schema_version: int,
    canonical_json_fn: CanonicalJson,
    fingerprint_text_fn: FingerprintText,
) -> SnapshotT:
    ordered_owners = tuple(sorted(owners, key=lambda owner: owner.owner))
    ordered_models = tuple(sorted(active_models, key=lambda model: model.signature))
    identity_payload: dict[str, object] = {
        "schema_version": schema_version,
        "source_version": source_version,
        "owners": [owner.identity_dict() for owner in ordered_owners],
        "active_models": [model.to_dict() for model in ordered_models],
        "consistency": consistency.value,
    }
    fingerprint = fingerprint_text_fn(canonical_json_fn(identity_payload))
    snapshot_id = (
        "knowledge-snapshot-v1:"
        f"{fingerprint.xxh3_128}:{fingerprint.byte_count}:"
        f"{fingerprint.xxh3_64_guard}"
    )
    return cls(
        source_version=source_version,
        captured_at_utc=captured_at_utc,
        captured_monotonic_ns=captured_monotonic_ns,
        owners=ordered_owners,
        active_models=ordered_models,
        snapshot_id=snapshot_id,
        consistency=consistency,
        attempts=attempts,
        warnings=warnings,
    )


def knowledge_snapshot_changed_owners(
    contract: KnowledgeSnapshot,
) -> tuple[str, ...]:
    return tuple(owner.owner for owner in contract.owners if owner.changed)


__all__ = [
    "create_knowledge_snapshot",
    "knowledge_snapshot_changed_owners",
    "owner_snapshot_changed",
    "validate_active_model",
    "validate_knowledge_snapshot",
    "validate_logical_watermark",
    "validate_owner_snapshot",
    "validate_publication_head",
]
# endregion [02]
