"""Validation and deterministic helpers for Knowledge context envelopes.
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_contract_context.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The public dataclasses and compatibility seams remain in ``knowledge_contracts``.
This module has no runtime dependency on that facade.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from .knowledge_contract_protocols import (
        ContextBudget,
        ContextBundle,
        ContextContradictionRef,
        ContextEntityRef,
        ContextGraphBudget,
        ContextPlanRef,
        ContextPlanStepRef,
        ContextRelationRef,
    )

RequiredText = Callable[[str, str], str]
OptionalText = Callable[[str, str | None], str | None]
ValidateValues = Callable[[str, tuple[str, ...]], None]
ValidateReferences = Callable[[str, tuple[str, ...]], None]
CanonicalJson = Callable[[Mapping[str, object]], str]
FingerprintText = Callable[[str], Any]
ContextContradictionT = TypeVar("ContextContradictionT")


def validate_context_plan_values(
    name: str,
    values: tuple[str, ...],
    *,
    required_text_fn: RequiredText,
    max_values: int,
    max_value_chars: int,
    max_total_value_chars: int,
) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"context plan {name} must be a tuple")
    if len(values) > max_values:
        raise ValueError(
            f"context plan {name} cannot contain more than {max_values} values"
        )
    if len(set(values)) != len(values):
        raise ValueError(f"context plan {name} must be unique")
    total_characters = 0
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"context plan {name} must contain strings")
        required_text_fn(f"context plan {name} value", value)
        if len(value) > max_value_chars:
            raise ValueError(
                f"context plan {name} values cannot exceed {max_value_chars} characters"
            )
        total_characters += len(value)
    if total_characters > max_total_value_chars:
        raise ValueError(
            f"context plan {name} cannot exceed "
            f"{max_total_value_chars} total characters"
        )


def validate_context_plan_step_ref(
    contract: ContextPlanStepRef,
    *,
    required_text_fn: RequiredText,
    max_value_chars: int,
) -> None:
    for name, value in (
        ("channel", contract.channel),
        ("ranking_name", contract.ranking_name),
        ("reason", contract.reason),
    ):
        if not isinstance(value, str):
            raise ValueError(f"context plan step {name} must be a string")
        required_text_fn(f"context plan step {name}", value)
        if len(value) > max_value_chars:
            raise ValueError(
                f"context plan step {name} cannot exceed {max_value_chars} characters"
            )
    if (
        isinstance(contract.candidate_limit, bool)
        or not isinstance(contract.candidate_limit, int)
        or not 1 <= contract.candidate_limit <= 1_000
    ):
        raise ValueError("context plan step candidate_limit must be between 1 and 1000")
    if not isinstance(contract.required, bool):
        raise ValueError("context plan step required must be a bool")


def _validate_context_plan_identity(
    contract: ContextPlanRef,
    *,
    required_text_fn: RequiredText,
    max_value_chars: int,
) -> None:
    for name, value in (
        ("plan_id", contract.plan_id),
        ("normalized_query", contract.normalized_query),
    ):
        if not isinstance(value, str):
            raise ValueError(f"context plan {name} must be a string")
        required_text_fn(f"context plan {name}", value)
        if len(value) > max_value_chars:
            raise ValueError(
                f"context plan {name} cannot exceed {max_value_chars} characters"
            )
    if contract.retrieval_mode not in {"discovery", "evidence"}:
        raise ValueError("context plan retrieval_mode is invalid")


def _validate_context_plan_options(
    contract: ContextPlanRef,
    *,
    optional_text_fn: OptionalText,
    max_value_chars: int,
) -> None:
    for name, option_value in (
        ("project", contract.project),
        ("date_from", contract.date_from),
        ("date_to", contract.date_to),
    ):
        if option_value is not None and not isinstance(option_value, str):
            raise ValueError(f"context plan {name} must be a string when present")
        optional_text_fn(f"context plan {name}", option_value)
        if option_value is not None and len(option_value) > max_value_chars:
            raise ValueError(
                f"context plan {name} cannot exceed {max_value_chars} characters"
            )


def _validate_context_plan_numbers(contract: ContextPlanRef) -> None:
    if not isinstance(contract.include_history, bool):
        raise ValueError("context plan include_history must be a bool")
    for name, numeric_value, minimum, maximum in (
        ("limit", contract.limit, 1, 1_000),
        ("max_per_resource", contract.max_per_resource, 1, 100),
        ("min_section_distance", contract.min_section_distance, 0, 1_000_000),
        ("max_vectors", contract.max_vectors, 1, 10_000_000),
    ):
        if (
            isinstance(numeric_value, bool)
            or not isinstance(numeric_value, int)
            or not minimum <= numeric_value <= maximum
        ):
            raise ValueError(
                f"context plan {name} must be between {minimum} and {maximum}"
            )


def _validate_context_plan_steps(
    contract: ContextPlanRef,
    *,
    max_steps: int,
    plan_step_type: type,
) -> None:
    if not isinstance(contract.steps, tuple):
        raise ValueError("context plan steps must be a tuple")
    if len(contract.steps) > max_steps:
        raise ValueError(f"context plan cannot contain more than {max_steps} steps")
    if not all(isinstance(step, plan_step_type) for step in contract.steps):
        raise ValueError("context plan steps are invalid")


def validate_context_plan_ref(
    contract: ContextPlanRef,
    *,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
    validate_values_fn: ValidateValues,
    max_value_chars: int,
    max_steps: int,
    plan_step_type: type,
) -> None:
    _validate_context_plan_identity(
        contract,
        required_text_fn=required_text_fn,
        max_value_chars=max_value_chars,
    )
    for name, values in (
        ("intents", contract.intents),
        ("exact_terms", contract.exact_terms),
        ("source_kinds", contract.source_kinds),
        ("formats", contract.formats),
        ("notices", contract.notices),
    ):
        validate_values_fn(name, values)
    _validate_context_plan_options(
        contract,
        optional_text_fn=optional_text_fn,
        max_value_chars=max_value_chars,
    )
    _validate_context_plan_numbers(contract)
    _validate_context_plan_steps(
        contract,
        max_steps=max_steps,
        plan_step_type=plan_step_type,
    )


def validate_context_graph_budget(
    contract: ContextGraphBudget,
    *,
    max_evidence_identifiers: int,
) -> None:
    for name, value in (
        ("identifiers_considered", contract.identifiers_considered),
        ("entities_included", contract.entities_included),
        ("relations_included", contract.relations_included),
        ("omitted_identifiers", contract.omitted_identifiers),
        ("omitted_entities", contract.omitted_entities),
        ("omitted_relations", contract.omitted_relations),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"context graph {name} cannot be negative")
    if contract.identifier_limit_per_evidence != max_evidence_identifiers:
        raise ValueError("context graph identifier limit is invalid")
    if contract.measurement_scope != "selected_evidence_graph":
        raise ValueError("context graph measurement_scope is invalid")


def context_graph_omitted_total(contract: ContextGraphBudget) -> int:
    return (
        contract.omitted_identifiers
        + contract.omitted_entities
        + contract.omitted_relations
    )


def validate_context_budget(
    contract: ContextBudget,
    *,
    required_text_fn: RequiredText,
) -> None:
    if isinstance(contract.character_limit, bool) or contract.character_limit < 1:
        raise ValueError("context character limit must be positive")
    if not 0 <= contract.characters_used <= contract.character_limit:
        raise ValueError("context characters used exceed the limit")
    if contract.estimated_tokens < 0:
        raise ValueError("estimated token count cannot be negative")
    if contract.omitted_candidates < 0:
        raise ValueError("omitted candidate count cannot be negative")
    required_text_fn("estimator signature", contract.estimator_signature)
    if contract.measurement_scope != "rendered_context":
        raise ValueError("context budget measurement_scope must be rendered_context")
    if len(set(contract.truncated_evidence_ids)) != len(
        contract.truncated_evidence_ids
    ):
        raise ValueError("truncated evidence identifiers must be unique")


def validate_context_references(
    name: str,
    references: tuple[str, ...],
    *,
    required_text_fn: RequiredText,
) -> None:
    if not references:
        raise ValueError(f"context {name} requires at least one reference")
    if len(set(references)) != len(references):
        raise ValueError(f"context {name} references must be unique")
    for reference in references:
        required_text_fn(f"context {name} reference", reference)


def validate_context_entity_ref(
    contract: ContextEntityRef,
    *,
    required_text_fn: RequiredText,
    validate_references_fn: ValidateReferences,
) -> None:
    required_text_fn("context entity_id", contract.entity_id)
    required_text_fn("context entity kind", contract.entity_kind)
    required_text_fn("context entity label", contract.label)
    validate_references_fn("entity evidence", contract.evidence_ids)
    validate_references_fn("entity resource", contract.resource_ids)


def context_contradiction_stable_id(
    contradiction_kind: str,
    topic: str,
    values: tuple[str, ...],
    *,
    canonical_json_fn: CanonicalJson,
    fingerprint_text_fn: FingerprintText,
) -> str:
    identity = {
        "contradiction_kind": contradiction_kind,
        "topic": topic.casefold(),
        "values": [value.casefold() for value in values],
    }
    fingerprint = fingerprint_text_fn(canonical_json_fn(identity))
    return (
        "context-contradiction-v1:"
        f"{fingerprint.xxh3_128}:{fingerprint.byte_count}:"
        f"{fingerprint.xxh3_64_guard}"
    )


def create_context_contradiction(
    cls: Callable[..., ContextContradictionT],
    *,
    contradiction_kind: str,
    topic: str,
    values: tuple[str, ...],
    citation_ids: tuple[str, ...],
) -> ContextContradictionT:
    ordered_values = tuple(sorted(values, key=str.casefold))
    stable_id_fn: Callable[[str, str, tuple[str, ...]], str] = cast(Any, cls)._stable_id
    return cls(
        contradiction_id=stable_id_fn(
            contradiction_kind,
            topic,
            ordered_values,
        ),
        contradiction_kind=contradiction_kind,
        topic=topic,
        values=ordered_values,
        citation_ids=citation_ids,
    )


def validate_context_contradiction(
    contract: ContextContradictionRef,
    *,
    required_text_fn: RequiredText,
    validate_references_fn: ValidateReferences,
) -> None:
    required_text_fn("context contradiction_id", contract.contradiction_id)
    required_text_fn("context contradiction kind", contract.contradiction_kind)
    required_text_fn("context contradiction topic", contract.topic)
    validate_references_fn("contradiction value", contract.values)
    if len({value.casefold() for value in contract.values}) < 2:
        raise ValueError("context contradictions require at least two distinct values")
    if contract.values != tuple(sorted(contract.values, key=str.casefold)):
        raise ValueError("context contradiction values must be canonically ordered")
    validate_references_fn("contradiction citation", contract.citation_ids)
    if len(contract.citation_ids) < 2:
        raise ValueError(
            "context contradictions require at least two distinct citations"
        )
    expected_id = contract._stable_id(
        contract.contradiction_kind,
        contract.topic,
        contract.values,
    )
    if contract.contradiction_id != expected_id:
        raise ValueError("context contradiction_id does not match its identity")


def context_contradiction_summary(contract: ContextContradictionRef) -> str:
    return (
        "Structured claim "
        f"{json.dumps(contract.topic, ensure_ascii=False, allow_nan=False)} "
        "has conflicting values: "
        f"{', '.join(json.dumps(value, ensure_ascii=False, allow_nan=False) for value in contract.values)}."
    )


def validate_context_relation_ref(
    contract: ContextRelationRef,
    *,
    required_text_fn: RequiredText,
    validate_references_fn: ValidateReferences,
    evidence_method_type: type,
    max_provenance_items: int,
    max_provenance_chars: int,
) -> None:
    required_text_fn("context relation_id", contract.relation_id)
    required_text_fn("context relation source", contract.source_entity_id)
    required_text_fn("context relation target", contract.target_entity_id)
    required_text_fn("context relation kind", contract.relation_kind)
    if not isinstance(contract.method, evidence_method_type):
        raise ValueError("context relation method is invalid")
    validate_references_fn("relation provenance", contract.provenance)
    if len(contract.provenance) > max_provenance_items:
        raise ValueError(
            "context relation provenance cannot contain more than "
            f"{max_provenance_items} items"
        )
    if sum(len(item) for item in contract.provenance) > max_provenance_chars:
        raise ValueError(
            "context relation provenance cannot exceed "
            f"{max_provenance_chars} total characters"
        )
    if contract.confidence is not None and (
        isinstance(contract.confidence, bool)
        or not isinstance(contract.confidence, (int, float))
        or not math.isfinite(contract.confidence)
        or not 0.0 <= contract.confidence <= 1.0
    ):
        raise ValueError("context relation confidence must be between 0 and 1")
    if contract.source_entity_id == contract.target_entity_id:
        raise ValueError("context relations require different entities")
    validate_references_fn("relation evidence", contract.evidence_ids)


def _validate_bundle_plan(
    contract: ContextBundle,
    *,
    required_text_fn: RequiredText,
) -> None:
    required_text_fn("normalized query", contract.normalized_query)
    required_text_fn("plan_id", contract.plan_id)
    for intent in contract.intents:
        required_text_fn("intent", intent)
    if contract.plan.plan_id != contract.plan_id:
        raise ValueError("context plan_id must match the normalized plan")
    if contract.plan.normalized_query != contract.normalized_query:
        raise ValueError("context query must match the normalized plan")
    if contract.plan.intents != contract.intents:
        raise ValueError("context intents must match the normalized plan")


def _bundle_evidence_grounding(
    contract: ContextBundle,
) -> tuple[tuple[str, ...], set[str], dict[str, set[str]], set[str]]:
    selected_evidence_ids = tuple(
        hit.evidence.evidence_id for hit in contract.selected_hits
    )
    evidence_ids = set(selected_evidence_ids)
    evidence_resources: dict[str, set[str]] = {}
    for hit in contract.selected_hits:
        grounded_resources = evidence_resources.setdefault(
            hit.evidence.evidence_id,
            set(),
        )
        grounded_resources.add(hit.resource.resource_id)
        for namespace, value in hit.evidence.identifiers:
            if namespace.casefold() in {
                "planned_duplicate_of",
                "code_relation_source_resource",
                "code_relation_target_resource",
            }:
                grounded_resources.add(value)
    resource_ids = {
        resource_id
        for grounded_resources in evidence_resources.values()
        for resource_id in grounded_resources
    }
    if len(evidence_ids) != len(selected_evidence_ids):
        raise ValueError(
            "selected hits require unique evidence identifiers for citations"
        )
    return selected_evidence_ids, evidence_ids, evidence_resources, resource_ids


def _validate_bundle_citations(
    contract: ContextBundle,
    *,
    selected_evidence_ids: tuple[str, ...],
    evidence_ids: set[str],
    required_text_fn: RequiredText,
) -> tuple[set[str], set[str]]:
    citation_names: set[str] = set()
    cited_evidence_ids: set[str] = set()
    for citation_id, evidence_id in contract.citation_ids:
        required_text_fn("citation_id", citation_id)
        if citation_id in citation_names:
            raise ValueError("citation identifiers must be unique")
        citation_names.add(citation_id)
        if evidence_id not in evidence_ids:
            raise ValueError("citation must reference selected evidence")
        cited_evidence_ids.add(evidence_id)
    if (
        len(contract.citation_ids) != len(selected_evidence_ids)
        or cited_evidence_ids != evidence_ids
    ):
        raise ValueError(
            "each selected hit must have exactly one citation by evidence_id"
        )
    return citation_names, cited_evidence_ids


def _validate_bundle_entities(
    contract: ContextBundle,
    *,
    cited_evidence_ids: set[str],
    resource_ids: set[str],
    evidence_resources: dict[str, set[str]],
) -> tuple[list[str], dict[str, ContextEntityRef]]:
    entity_ids = [entity.entity_id for entity in contract.entities]
    if len(set(entity_ids)) != len(entity_ids):
        raise ValueError("context entity identifiers must be unique")
    for entity in contract.entities:
        if not set(entity.evidence_ids).issubset(cited_evidence_ids):
            raise ValueError("context entities must reference cited evidence")
        if not set(entity.resource_ids).issubset(resource_ids):
            raise ValueError("context entities must reference a grounded resource")
        grounded_resources = {
            resource_id
            for evidence_id in entity.evidence_ids
            for resource_id in evidence_resources.get(evidence_id, set())
        }
        if not set(entity.resource_ids).issubset(grounded_resources):
            raise ValueError(
                "context entity resources must be grounded by its evidence references"
            )
    return entity_ids, {entity.entity_id: entity for entity in contract.entities}


def _validate_bundle_relations(
    contract: ContextBundle,
    *,
    entity_ids: list[str],
    entities_by_id: dict[str, ContextEntityRef],
    cited_evidence_ids: set[str],
) -> None:
    relation_ids = [relation.relation_id for relation in contract.relations]
    if len(set(relation_ids)) != len(relation_ids):
        raise ValueError("context relation identifiers must be unique")
    logical_relations = [
        (
            relation.source_entity_id,
            relation.target_entity_id,
            relation.relation_kind,
            relation.method,
            relation.provenance,
            relation.evidence_ids,
            relation.confidence,
        )
        for relation in contract.relations
    ]
    if len(set(logical_relations)) != len(logical_relations):
        raise ValueError("logical context relations must be unique")
    known_entity_ids = set(entity_ids)
    for relation in contract.relations:
        if not {
            relation.source_entity_id,
            relation.target_entity_id,
        }.issubset(known_entity_ids):
            raise ValueError("context relations must reference existing entities")
        if not set(relation.evidence_ids).issubset(cited_evidence_ids):
            raise ValueError("context relations must reference cited evidence")
        relation_evidence = set(relation.evidence_ids)
        source_evidence = set(entities_by_id[relation.source_entity_id].evidence_ids)
        target_evidence = set(entities_by_id[relation.target_entity_id].evidence_ids)
        if not relation_evidence.issubset(
            source_evidence.intersection(target_evidence)
        ):
            raise ValueError("context relation evidence must ground both endpoints")


def _validate_bundle_graph_budget(
    contract: ContextBundle,
    *,
    knowledge_completeness_type: Any,
) -> None:
    identifiers_considered = sum(
        len(hit.evidence.identifiers) for hit in contract.selected_hits
    )
    if contract.graph_budget.identifiers_considered != identifiers_considered:
        raise ValueError("context graph identifier count must match selected evidence")
    if contract.graph_budget.entities_included != len(contract.entities):
        raise ValueError("context graph entity count must match entities")
    if contract.graph_budget.relations_included != len(contract.relations):
        raise ValueError("context graph relation count must match relations")
    if (
        contract.graph_budget.omitted_total
        and contract.completeness is knowledge_completeness_type.COMPLETE
    ):
        raise ValueError("omitted context graph data requires partial completeness")


def _validate_bundle_rendered_graph(contract: ContextBundle) -> None:
    for entity in contract.entities:
        if entity.to_json() not in contract.rendered_context:
            raise ValueError(
                "context entities must be rendered inside the character budget"
            )
    for relation in contract.relations:
        if relation.to_json() not in contract.rendered_context:
            raise ValueError(
                "context relations must be rendered inside the character budget"
            )


def _validate_bundle_contradictions(
    contract: ContextBundle,
    *,
    citation_names: set[str],
) -> None:
    contradiction_ids: set[str] = set()
    logical_contradictions: set[tuple[str, str, tuple[str, ...]]] = set()
    for contradiction in contract.contradictions:
        if contradiction.contradiction_id in contradiction_ids:
            raise ValueError("context contradiction identifiers must be unique")
        contradiction_ids.add(contradiction.contradiction_id)
        if not set(contradiction.citation_ids).issubset(citation_names):
            raise ValueError("contradictions require at least two existing citations")
        rendered_citations = f"[{', '.join(contradiction.citation_ids)}]"
        if (
            contradiction.summary not in contract.rendered_context
            or rendered_citations not in contract.rendered_context
        ):
            raise ValueError(
                "context contradictions must be rendered inside the character budget"
            )
        logical_contradiction = (
            contradiction.contradiction_kind,
            contradiction.topic.casefold(),
            tuple(value.casefold() for value in contradiction.values),
        )
        if logical_contradiction in logical_contradictions:
            raise ValueError("logical context contradictions must be unique")
        logical_contradictions.add(logical_contradiction)


def _validate_bundle_notices(
    contract: ContextBundle,
    *,
    required_text_fn: RequiredText,
) -> None:
    for item in (*contract.missing_information, *contract.warnings):
        required_text_fn("context notice", item)
    if contract.graph_budget.omitted_total:
        graph_notices = tuple(
            item
            for item in (*contract.missing_information, *contract.warnings)
            if "graph" in item.casefold() and "omit" in item.casefold()
        )
        if not graph_notices or not any(
            notice in contract.rendered_context for notice in graph_notices
        ):
            raise ValueError(
                "omitted context graph data requires a rendered visible notice"
            )


def _validate_bundle_blocking_owners(
    contract: ContextBundle,
    *,
    required_text_fn: RequiredText,
) -> None:
    if not isinstance(contract.blocking_owners, tuple):
        raise ValueError("context blocking owners must be a tuple")
    for owner in contract.blocking_owners:
        if not isinstance(owner, str):
            raise ValueError("context blocking owners must contain strings")
        required_text_fn("context blocking owner", owner)
    if contract.blocking_owners != tuple(sorted(set(contract.blocking_owners))):
        raise ValueError("context blocking owners must be unique and ordered")


def _validate_bundle_character_budget(contract: ContextBundle) -> None:
    if len(contract.rendered_context) != contract.budget.characters_used:
        raise ValueError("rendered context and budget character count disagree")


def _validate_bundle_telemetry(
    contract: ContextBundle,
    *,
    telemetry_type: type,
    telemetry_operation_type: Any,
) -> None:
    if contract.telemetry is not None and not isinstance(
        contract.telemetry,
        telemetry_type,
    ):
        raise ValueError("context telemetry is invalid")
    if (
        contract.telemetry is not None
        and contract.telemetry.operation is not telemetry_operation_type.CONTEXT
    ):
        raise ValueError("ContextBundle telemetry must describe a context operation")


def validate_context_bundle(
    contract: ContextBundle,
    *,
    required_text_fn: RequiredText,
    knowledge_completeness_type: Any,
    telemetry_type: type,
    telemetry_operation_type: Any,
) -> None:
    _validate_bundle_plan(contract, required_text_fn=required_text_fn)
    (
        selected_evidence_ids,
        evidence_ids,
        evidence_resources,
        resource_ids,
    ) = _bundle_evidence_grounding(contract)
    citation_names, cited_evidence_ids = _validate_bundle_citations(
        contract,
        selected_evidence_ids=selected_evidence_ids,
        evidence_ids=evidence_ids,
        required_text_fn=required_text_fn,
    )
    entity_ids, entities_by_id = _validate_bundle_entities(
        contract,
        cited_evidence_ids=cited_evidence_ids,
        resource_ids=resource_ids,
        evidence_resources=evidence_resources,
    )
    _validate_bundle_relations(
        contract,
        entity_ids=entity_ids,
        entities_by_id=entities_by_id,
        cited_evidence_ids=cited_evidence_ids,
    )
    _validate_bundle_graph_budget(
        contract,
        knowledge_completeness_type=knowledge_completeness_type,
    )
    _validate_bundle_rendered_graph(contract)
    _validate_bundle_contradictions(contract, citation_names=citation_names)
    _validate_bundle_notices(contract, required_text_fn=required_text_fn)
    _validate_bundle_blocking_owners(
        contract,
        required_text_fn=required_text_fn,
    )
    _validate_bundle_character_budget(contract)
    _validate_bundle_telemetry(
        contract,
        telemetry_type=telemetry_type,
        telemetry_operation_type=telemetry_operation_type,
    )


__all__ = [
    "context_contradiction_stable_id",
    "context_contradiction_summary",
    "context_graph_omitted_total",
    "create_context_contradiction",
    "validate_context_budget",
    "validate_context_bundle",
    "validate_context_contradiction",
    "validate_context_entity_ref",
    "validate_context_graph_budget",
    "validate_context_plan_ref",
    "validate_context_plan_step_ref",
    "validate_context_plan_values",
    "validate_context_references",
    "validate_context_relation_ref",
]
# endregion [02]
