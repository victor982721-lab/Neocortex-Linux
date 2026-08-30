"""Validation for Knowledge resource, evidence, ranking and hit contracts.
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_contract_references.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The public dataclasses remain in ``knowledge_contracts`` for stable type and
pickle identity. This helper has no runtime dependency on that facade.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from .knowledge_contract_protocols import (
        EvidenceRef,
        KnowledgeHit,
        PhysicalIdentityRef,
        RankingSignal,
        ResourceRef,
        RevisionRef,
    )

RequiredText = Callable[[str, str], str]
OptionalText = Callable[[str, str | None], str | None]


def validate_physical_identity_ref(
    contract: PhysicalIdentityRef,
    *,
    required_text_fn: RequiredText,
) -> None:
    required_text_fn("physical identity scheme", contract.scheme)
    required_text_fn("physical identity value", contract.value)
    if isinstance(contract.identity_version, bool) or contract.identity_version < 1:
        raise ValueError("physical identity version must be positive")


def validate_resource_ref(
    contract: ResourceRef,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
    resource_disposition_type: Any,
) -> None:
    required_text_fn("resource_id", contract.resource_id)
    required_text_fn("source_kind", contract.source_kind)
    required_text_fn("owner", contract.owner)
    optional_text_fn("current_path", contract.current_path)
    optional_text_fn("canonical_resource_id", contract.canonical_resource_id)
    if contract.disposition is resource_disposition_type.DUPLICATE:
        required_text_fn(
            "canonical_resource_id",
            "" if contract.canonical_resource_id is None else contract.canonical_resource_id,
        )
    if contract.canonical_resource_id == contract.resource_id:
        raise ValueError("a resource cannot name itself as its canonical resource")


def validate_revision_ref(
    contract: RevisionRef,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
) -> None:
    required_text_fn("resource_id", contract.resource_id)
    required_text_fn("revision_id", contract.revision_id)
    required_text_fn("producer", contract.producer)
    required_text_fn("processing_signature", contract.processing_signature)
    optional_text_fn("observed_at_utc", contract.observed_at_utc)
    if contract.generation is not None and (
        isinstance(contract.generation, bool) or contract.generation < 0
    ):
        raise ValueError("revision generation cannot be negative")


def _validate_evidence_optional_text(
    contract: EvidenceRef,
    *,
    optional_text_fn: OptionalText,
) -> None:
    for name, value in (
        ("sheet", contract.sheet),
        ("cell_range", contract.cell_range),
        ("coordinate_space", contract.coordinate_space),
        ("symbol", contract.symbol),
        ("section_kind", contract.section_kind),
        ("section_id", contract.section_id),
        ("extractor", contract.extractor),
        ("extractor_version", contract.extractor_version),
    ):
        optional_text_fn(name, value)


def _validate_page_locator(contract: EvidenceRef) -> None:
    if contract.page is not None and (isinstance(contract.page, bool) or contract.page < 0):
        raise ValueError("page cannot be negative")


def _validate_line_locator(contract: EvidenceRef) -> None:
    if (contract.start_line is None) != (contract.end_line is None):
        raise ValueError("line locator requires both start and end")
    if contract.start_line is not None and (
        isinstance(contract.start_line, bool)
        or isinstance(contract.end_line, bool)
        or contract.start_line < 1
        or contract.end_line is None
        or contract.end_line < contract.start_line
    ):
        raise ValueError("line locator is invalid")


def _validate_time_locator(contract: EvidenceRef) -> None:
    if (contract.start_ms is None) != (contract.end_ms is None):
        raise ValueError("time locator requires both start and end")
    if contract.start_ms is not None and (
        isinstance(contract.start_ms, bool)
        or isinstance(contract.end_ms, bool)
        or contract.start_ms < 0
        or contract.end_ms is None
        or contract.end_ms <= contract.start_ms
    ):
        raise ValueError("time locator is invalid")


def _validate_character_locator(contract: EvidenceRef) -> None:
    if (contract.start_char is None) != (contract.end_char is None):
        raise ValueError("character locator requires both start and end")
    if contract.start_char is not None and (
        isinstance(contract.start_char, bool)
        or isinstance(contract.end_char, bool)
        or contract.start_char < 0
        or contract.end_char is None
        or contract.end_char <= contract.start_char
    ):
        raise ValueError("character locator is invalid")


def _validate_bounding_box(contract: EvidenceRef) -> None:
    if contract.bounding_box is not None:
        left, top, right, bottom = contract.bounding_box
        if not all(math.isfinite(value) for value in contract.bounding_box) or (
            right <= left or bottom <= top
        ):
            raise ValueError("bounding box is invalid")
        if contract.coordinate_space is None:
            raise ValueError("bounding box requires a coordinate space")
    elif contract.coordinate_space is not None:
        raise ValueError("coordinate space requires a bounding box")


def _validate_evidence_lengths(
    contract: EvidenceRef,
    *,
    max_snippet_chars: int,
    max_symbol_chars: int,
) -> None:
    if contract.snippet is not None and len(contract.snippet) > max_snippet_chars:
        raise ValueError(f"snippet cannot exceed {max_snippet_chars} characters")
    if contract.symbol is not None and len(contract.symbol) > max_symbol_chars:
        raise ValueError(f"symbol cannot exceed {max_symbol_chars} characters")


def _validate_evidence_generation(contract: EvidenceRef) -> None:
    if contract.generation is not None and (
        isinstance(contract.generation, bool) or contract.generation < 0
    ):
        raise ValueError("evidence generation cannot be negative")


def _validate_evidence_identifiers(
    contract: EvidenceRef,
    *,
    required_text_fn: RequiredText,
    max_identifiers: int,
    max_component_chars: int,
) -> None:
    if len(contract.identifiers) > max_identifiers:
        raise ValueError(f"evidence cannot contain more than {max_identifiers} identifiers")
    if len(set(contract.identifiers)) != len(contract.identifiers):
        raise ValueError("evidence identifiers must be unique")
    for namespace, value in contract.identifiers:
        if not isinstance(namespace, str) or not isinstance(value, str):
            raise ValueError("evidence identifiers must contain strings")
        required_text_fn("identifier namespace", namespace)
        required_text_fn("identifier value", value)
        if len(namespace) > max_component_chars or len(value) > max_component_chars:
            raise ValueError(
                f"evidence identifier components cannot exceed {max_component_chars} characters"
            )


def validate_evidence_ref(
    contract: EvidenceRef,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
    max_snippet_chars: int,
    max_symbol_chars: int,
    max_identifiers: int,
    max_identifier_component_chars: int,
) -> None:
    required_text_fn("evidence_id", contract.evidence_id)
    required_text_fn("resource_id", contract.resource_id)
    required_text_fn("revision_id", contract.revision_id)
    _validate_evidence_optional_text(contract, optional_text_fn=optional_text_fn)
    _validate_page_locator(contract)
    _validate_line_locator(contract)
    _validate_time_locator(contract)
    _validate_character_locator(contract)
    _validate_bounding_box(contract)
    _validate_evidence_lengths(
        contract,
        max_snippet_chars=max_snippet_chars,
        max_symbol_chars=max_symbol_chars,
    )
    _validate_evidence_generation(contract)
    _validate_evidence_identifiers(
        contract,
        required_text_fn=required_text_fn,
        max_identifiers=max_identifiers,
        max_component_chars=max_identifier_component_chars,
    )


def validate_ranking_signal(
    contract: RankingSignal,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
) -> None:
    required_text_fn("ranking source", contract.source)
    required_text_fn("score_kind", contract.score_kind)
    optional_text_fn("model_signature", contract.model_signature)
    optional_text_fn("query_model_signature", contract.query_model_signature)
    if not math.isfinite(contract.raw_score):
        raise ValueError("ranking raw score must be finite")
    if isinstance(contract.source_rank, bool) or contract.source_rank < 1:
        raise ValueError("ranking source rank must be positive")
    if contract.generation is not None and (
        isinstance(contract.generation, bool) or contract.generation < 0
    ):
        raise ValueError("ranking generation cannot be negative")
    if contract.contribution is not None and not math.isfinite(contract.contribution):
        raise ValueError("ranking contribution must be finite")


def validate_knowledge_hit(
    contract: KnowledgeHit,
    *,
    required_text_fn: RequiredText,
) -> None:
    if isinstance(contract.rank, bool) or contract.rank < 1:
        raise ValueError("knowledge hit rank must be positive")
    if not math.isfinite(contract.fused_score):
        raise ValueError("knowledge fused score must be finite")
    if contract.revision.resource_id != contract.resource.resource_id:
        raise ValueError("revision does not belong to hit resource")
    if (
        contract.evidence.resource_id != contract.resource.resource_id
        or contract.evidence.revision_id != contract.revision.revision_id
    ):
        raise ValueError("evidence does not belong to hit revision")
    if not contract.signals:
        raise ValueError("knowledge hit requires at least one ranking signal")
    if not contract.reasons:
        raise ValueError("knowledge hit requires at least one retrieval reason")
    for reason in contract.reasons:
        required_text_fn("retrieval reason", reason)
    for warning in contract.warnings:
        required_text_fn("hit warning", warning)
    if contract.confidence is not None and (
        not math.isfinite(contract.confidence) or not 0.0 <= contract.confidence <= 1.0
    ):
        raise ValueError("knowledge confidence must be between 0 and 1")


__all__ = [
    "validate_evidence_ref",
    "validate_knowledge_hit",
    "validate_physical_identity_ref",
    "validate_ranking_signal",
    "validate_resource_ref",
    "validate_revision_ref",
]
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.knowledge_contract_references")
