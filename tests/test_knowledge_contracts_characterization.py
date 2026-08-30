"""P2-5 characterization gates for the Knowledge contract facade."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contracts_characterization.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import hashlib
import inspect
import os
import pickle
import subprocess
import sys
import textwrap
import time
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import cast

import pytest

import _04_Nucleo_Operativo.knowledge_contracts as contracts
# endregion [01]

# region [02] Implementación

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_MODULE = "_04_Nucleo_Operativo.knowledge_contracts"

EXPECTED_ALL = (
    "KNOWLEDGE_CONTRACT_SCHEMA_VERSION",
    "KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE",
    "KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE",
    "KNOWLEDGE_TELEMETRY_SCHEMA_VERSION",
    "MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS",
    "MAX_EVIDENCE_IDENTIFIERS",
    "MAX_EVIDENCE_SYMBOL_CHARS",
    "ActiveModel",
    "ContextBudget",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "EvidenceMethod",
    "EvidenceRef",
    "KnowledgeCompleteness",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgeQueryTelemetry",
    "KnowledgeSnapshot",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "LogicalWatermark",
    "OwnerAvailability",
    "OwnerSnapshot",
    "PhysicalIdentityRef",
    "PublicationHead",
    "RankingSignal",
    "ResourceDisposition",
    "ResourceRef",
    "RevisionRef",
    "RevisionState",
    "SnapshotConsistency",
)

EXPECTED_ENUM_MEMBERS = {
    "ResourceDisposition": (
        ("CANONICAL", "canonical"),
        ("DUPLICATE", "duplicate"),
        ("SUPERSEDED", "superseded"),
        ("DERIVED", "derived"),
    ),
    "RevisionState": (
        ("CURRENT", "current"),
        ("HISTORICAL", "historical"),
        ("SUPERSEDED", "superseded"),
        ("PARTIAL", "partial"),
        ("AMBIGUOUS", "ambiguous"),
    ),
    "EvidenceMethod": (
        ("STRUCTURAL", "structural"),
        ("EXTRACTED", "extracted"),
        ("INFERRED", "inferred"),
        ("HUMAN_CONFIRMED", "human_confirmed"),
        ("AMBIGUOUS", "ambiguous"),
    ),
    "OwnerAvailability": (
        ("AVAILABLE", "available"),
        ("ABSENT", "absent"),
        ("FUTURE", "future"),
        ("CORRUPT", "corrupt"),
        ("INCOMPATIBLE", "incompatible"),
    ),
    "SnapshotConsistency": (
        ("STABLE", "stable"),
        ("SNAPSHOT_CHANGED", "snapshot_changed"),
    ),
    "KnowledgeCompleteness": (
        ("COMPLETE", "complete"),
        ("PARTIAL", "partial"),
        ("NO_EVIDENCE", "no_evidence"),
        ("UNSUPPORTED", "unsupported"),
    ),
    "KnowledgeTimingPhase": (
        ("PLANNER", "planner"),
        ("SNAPSHOT_BEFORE", "snapshot_before"),
        ("OWNER_RANKING", "owner_ranking"),
        ("FUSION", "fusion"),
        ("BROKER", "broker"),
        ("SNAPSHOT_AFTER", "snapshot_after"),
        ("CONTEXT_COMPILE", "context_compile"),
    ),
    "KnowledgeTelemetryOperation": (
        ("SEARCH", "search"),
        ("CONTEXT", "context"),
    ),
}

EXPECTED_DATACLASS_SIGNATURES = {
    "KnowledgeTelemetryClock": (
        "read_ns:Callable[[], int]=<perf_counter_ns>",
        "signature:str='python-perf-counter-ns-v1'",
    ),
    "KnowledgePhaseTiming": (
        "phase:KnowledgeTimingPhase=<required>",
        "duration_ns:int=<required>",
        "service_attempt:int=0",
        "owner:str | None=None",
        "ranking_names:tuple[str, ...]=()",
        "snapshot_id:str | None=None",
        "executed:bool=True",
    ),
    "KnowledgeQueryTelemetry": (
        "operation:KnowledgeTelemetryOperation=<required>",
        "total_duration_ns:int=<required>",
        "phases:tuple[KnowledgePhaseTiming, ...]=<required>",
        "clock_signature:str='python-perf-counter-ns-v1'",
    ),
    "PhysicalIdentityRef": (
        "scheme:str=<required>",
        "value:str=<required>",
        "identity_version:int=<required>",
    ),
    "ResourceRef": (
        "resource_id:str=<required>",
        "source_kind:str=<required>",
        "owner:str=<required>",
        "physical_identity:PhysicalIdentityRef | None=None",
        "current_path:str | None=None",
        "disposition:ResourceDisposition | None=None",
        "canonical_resource_id:str | None=None",
    ),
    "RevisionRef": (
        "resource_id:str=<required>",
        "revision_id:str=<required>",
        "producer:str=<required>",
        "processing_signature:str=<required>",
        "generation:int | None=<required>",
        "state:RevisionState=<required>",
        "observed_at_utc:str | None=None",
    ),
    "EvidenceRef": (
        "evidence_id:str=<required>",
        "resource_id:str=<required>",
        "revision_id:str=<required>",
        "method:EvidenceMethod=<required>",
        "page:int | None=None",
        "start_line:int | None=None",
        "end_line:int | None=None",
        "sheet:str | None=None",
        "cell_range:str | None=None",
        "start_ms:int | None=None",
        "end_ms:int | None=None",
        "bounding_box:tuple[float, float, float, float] | None=None",
        "coordinate_space:str | None=None",
        "start_char:int | None=None",
        "end_char:int | None=None",
        "symbol:str | None=None",
        "section_kind:str | None=None",
        "section_id:str | None=None",
        "snippet:str | None=None",
        "extractor:str | None=None",
        "extractor_version:str | None=None",
        "generation:int | None=None",
        "identifiers:tuple[tuple[str, str], ...]=()",
    ),
    "RankingSignal": (
        "source:str=<required>",
        "score_kind:str=<required>",
        "raw_score:float=<required>",
        "source_rank:int=<required>",
        "model_signature:str | None=None",
        "generation:int | None=None",
        "contribution:float | None=None",
        "query_model_signature:str | None=None",
    ),
    "KnowledgeHit": (
        "rank:int=<required>",
        "resource:ResourceRef=<required>",
        "revision:RevisionRef=<required>",
        "evidence:EvidenceRef=<required>",
        "signals:tuple[RankingSignal, ...]=<required>",
        "fused_score:float=<required>",
        "reasons:tuple[str, ...]=<required>",
        "confidence:float | None=None",
        "warnings:tuple[str, ...]=()",
    ),
    "PublicationHead": (
        "scope:str=<required>",
        "publication_id:str=<required>",
        "generation:int=<required>",
        "model_signature:str | None=None",
    ),
    "LogicalWatermark": ("name:str=<required>", "value:str=<required>"),
    "ActiveModel": (
        "signature:str=<required>",
        "vector_space:str=<required>",
        "modality:str=<required>",
        "dimensions:int=<required>",
        "generation:int=<required>",
    ),
    "OwnerSnapshot": (
        "owner:str=<required>",
        "state:OwnerAvailability=<required>",
        "expected_schema_version:int=<required>",
        "observed_schema_version:int | None=None",
        "publications:tuple[PublicationHead, ...]=()",
        "watermarks:tuple[LogicalWatermark, ...]=()",
        "data_version_before:int | None=None",
        "data_version_after:int | None=None",
        "warning:str | None=None",
        "error_code:str | None=None",
        "identity_changed:bool=False",
    ),
    "KnowledgeSnapshot": (
        "source_version:str=<required>",
        "captured_at_utc:str=<required>",
        "captured_monotonic_ns:int=<required>",
        "owners:tuple[OwnerSnapshot, ...]=<required>",
        "active_models:tuple[ActiveModel, ...]=<required>",
        "snapshot_id:str=<required>",
        "consistency:SnapshotConsistency=<SnapshotConsistency.STABLE>",
        "attempts:int=1",
        "warnings:tuple[str, ...]=()",
    ),
    "ContextPlanStepRef": (
        "channel:str=<required>",
        "ranking_name:str=<required>",
        "reason:str=<required>",
        "candidate_limit:int=<required>",
        "required:bool=<required>",
    ),
    "ContextPlanRef": (
        "plan_id:str=<required>",
        "normalized_query:str=<required>",
        "retrieval_mode:str=<required>",
        "intents:tuple[str, ...]=<required>",
        "exact_terms:tuple[str, ...]=<required>",
        "source_kinds:tuple[str, ...]=<required>",
        "formats:tuple[str, ...]=<required>",
        "project:str | None=<required>",
        "date_from:str | None=<required>",
        "date_to:str | None=<required>",
        "include_history:bool=<required>",
        "limit:int=<required>",
        "max_per_resource:int=<required>",
        "min_section_distance:int=<required>",
        "max_vectors:int=<required>",
        "steps:tuple[ContextPlanStepRef, ...]=<required>",
        "notices:tuple[str, ...]=()",
    ),
    "ContextGraphBudget": (
        "identifiers_considered:int=<required>",
        "entities_included:int=<required>",
        "relations_included:int=<required>",
        "omitted_identifiers:int=0",
        "omitted_entities:int=0",
        "omitted_relations:int=0",
        "identifier_limit_per_evidence:int=64",
        "measurement_scope:str='selected_evidence_graph'",
    ),
    "ContextBudget": (
        "character_limit:int=<required>",
        "characters_used:int=<required>",
        "estimated_tokens:int=<required>",
        "estimator_signature:str=<required>",
        "omitted_candidates:int=0",
        "truncated_evidence_ids:tuple[str, ...]=()",
        "measurement_scope:str='rendered_context'",
    ),
    "ContextEntityRef": (
        "entity_id:str=<required>",
        "entity_kind:str=<required>",
        "label:str=<required>",
        "evidence_ids:tuple[str, ...]=<required>",
        "resource_ids:tuple[str, ...]=<required>",
    ),
    "ContextContradictionRef": (
        "contradiction_id:str=<required>",
        "contradiction_kind:str=<required>",
        "topic:str=<required>",
        "values:tuple[str, ...]=<required>",
        "citation_ids:tuple[str, ...]=<required>",
    ),
    "ContextRelationRef": (
        "relation_id:str=<required>",
        "source_entity_id:str=<required>",
        "target_entity_id:str=<required>",
        "relation_kind:str=<required>",
        "method:EvidenceMethod=<required>",
        "provenance:tuple[str, ...]=<required>",
        "evidence_ids:tuple[str, ...]=<required>",
        "confidence:float | None=None",
    ),
    "ContextBundle": (
        "normalized_query:str=<required>",
        "intents:tuple[str, ...]=<required>",
        "plan_id:str=<required>",
        "plan:ContextPlanRef=<required>",
        "snapshot:KnowledgeSnapshot=<required>",
        "selected_hits:tuple[KnowledgeHit, ...]=<required>",
        "citation_ids:tuple[tuple[str, str], ...]=<required>",
        "graph_budget:ContextGraphBudget=<required>",
        "budget:ContextBudget=<required>",
        "rendered_context:str=<required>",
        "completeness:KnowledgeCompleteness=<required>",
        "entities:tuple[ContextEntityRef, ...]=()",
        "relations:tuple[ContextRelationRef, ...]=()",
        "contradictions:tuple[ContextContradictionRef, ...]=()",
        "missing_information:tuple[str, ...]=()",
        "warnings:tuple[str, ...]=()",
        "telemetry:KnowledgeQueryTelemetry | None=None",
        "blocking_owners:tuple[str, ...]=()",
    ),
}

PRIVATE_SIGNATURES = {
    "_required_text": "(name: 'str', value: 'str') -> 'str'",
    "_optional_text": "(name: 'str', value: 'str | None') -> 'str | None'",
    "_base_payload": "(kind: 'str') -> 'dict[str, object]'",
    "_canonical_output": "(payload: 'Mapping[str, object]') -> 'str'",
    "_validate_context_plan_values": (
        "(name: 'str', values: 'tuple[str, ...]') -> 'None'"
    ),
    "_validate_context_references": (
        "(name: 'str', references: 'tuple[str, ...]') -> 'None'"
    ),
}

LEGACY_AND_SDK_EXPORTS = (
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "EvidenceRef",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgeQueryTelemetry",
    "KnowledgeSnapshot",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "ResourceRef",
    "RevisionRef",
)

EXPECTED_BUNDLE_KEYS = (
    "schema_version",
    "kind",
    "normalized_query",
    "intents",
    "plan_id",
    "plan",
    "snapshot",
    "selected_hits",
    "citation_ids",
    "entities",
    "relations",
    "graph_budget",
    "budget",
    "rendered_context",
    "completeness",
    "contradictions",
    "missing_information",
    "warnings",
    "telemetry",
)


def _stable_signature(contract_type: type[object]) -> tuple[str, ...]:
    signature = inspect.signature(contract_type)
    assert signature.return_annotation is None
    output: list[str] = []
    for parameter in signature.parameters.values():
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        annotation = cast(str, parameter.annotation)
        if parameter.default is inspect.Parameter.empty:
            default = "<required>"
        elif parameter.default is time.perf_counter_ns:
            default = "<perf_counter_ns>"
        elif isinstance(parameter.default, Enum):
            default = f"<{type(parameter.default).__name__}.{parameter.default.name}>"
        else:
            default = repr(parameter.default)
        output.append(f"{parameter.name}:{annotation}={default}")
    return tuple(output)


def _contract_instances() -> dict[str, object]:
    clock = contracts.KnowledgeTelemetryClock()
    planner_timing = contracts.KnowledgePhaseTiming(
        contracts.KnowledgeTimingPhase.PLANNER,
        7,
    )
    context_timing = contracts.KnowledgePhaseTiming(
        contracts.KnowledgeTimingPhase.CONTEXT_COMPILE,
        11,
    )
    telemetry = contracts.KnowledgeQueryTelemetry(
        contracts.KnowledgeTelemetryOperation.CONTEXT,
        18,
        (planner_timing, context_timing),
    )
    physical_identity = contracts.PhysicalIdentityRef(
        "windows_file_id",
        "volume:fixture:file:0001",
        1,
    )
    resource = contracts.ResourceRef(
        "resource:document:0001",
        "document",
        "document_catalog",
        physical_identity,
        r"C:\Corpus\Área técnica\manual.pdf",
        contracts.ResourceDisposition.CANONICAL,
    )
    other_resource = contracts.ResourceRef(
        "resource:code:0002",
        "code",
        "code",
        current_path=r"C:\Corpus\control.py",
        disposition=contracts.ResourceDisposition.CANONICAL,
    )
    revision = contracts.RevisionRef(
        resource.resource_id,
        "revision:document:0001:7",
        "document-route",
        "document-v7:fixture",
        7,
        contracts.RevisionState.CURRENT,
        "2026-07-30T12:00:00Z",
    )
    other_revision = contracts.RevisionRef(
        other_resource.resource_id,
        "revision:code:0002:3",
        "code-route",
        "code-v3:fixture",
        3,
        contracts.RevisionState.CURRENT,
        "2026-07-30T12:00:01Z",
    )
    evidence = contracts.EvidenceRef(
        "evidence:document:0001:page:2",
        resource.resource_id,
        revision.revision_id,
        contracts.EvidenceMethod.EXTRACTED,
        page=2,
        start_line=10,
        end_line=12,
        sheet="Datos",
        cell_range="A1:B2",
        start_ms=1_000,
        end_ms=2_000,
        bounding_box=(0.1, 0.2, 0.8, 0.9),
        coordinate_space="normalized-page-v1",
        start_char=20,
        end_char=42,
        symbol="interruptor.Q52",
        section_kind="equipment_record",
        section_id="section:2",
        snippet="Interruptor Q52 cerrado en Área técnica.",
        extractor="fixture-extractor",
        extractor_version="1.2.3",
        generation=7,
        identifiers=(("serial", "Q52"), ("asset", "breaker")),
    )
    other_evidence = contracts.EvidenceRef(
        "evidence:code:0002:line:41",
        other_resource.resource_id,
        other_revision.revision_id,
        contracts.EvidenceMethod.STRUCTURAL,
        start_line=41,
        end_line=55,
        symbol="control.validate_q52",
        snippet="def validate_q52(): ...",
        extractor="python-ast",
        extractor_version="3.13",
        generation=3,
        identifiers=(("serial", "Q52-A"),),
    )
    ranking_signal = contracts.RankingSignal(
        "document_lexical",
        "bm25",
        8.5,
        1,
        model_signature="model:fixture:text",
        generation=7,
        contribution=0.75,
        query_model_signature="model:fixture:query",
    )
    hit = contracts.KnowledgeHit(
        1,
        resource,
        revision,
        evidence,
        (ranking_signal,),
        0.95,
        ("exact asset identifier",),
        confidence=0.9,
        warnings=("advisory ranking",),
    )
    other_hit = contracts.KnowledgeHit(
        2,
        other_resource,
        other_revision,
        other_evidence,
        (contracts.RankingSignal("code_exact", "rank", 1.0, 1),),
        0.8,
        ("exact symbol",),
        confidence=0.8,
    )
    publication = contracts.PublicationHead(
        "model:text",
        "semantic-publication:7",
        7,
        "model:fixture:text",
    )
    watermark = contracts.LogicalWatermark("member_limit", "bounded:500000")
    active_model = contracts.ActiveModel(
        "model:fixture:text",
        "fixture-space",
        "text",
        8,
        7,
    )
    owner = contracts.OwnerSnapshot(
        "semantic",
        contracts.OwnerAvailability.AVAILABLE,
        6,
        observed_schema_version=6,
        publications=(publication,),
        watermarks=(watermark,),
        data_version_before=12,
        data_version_after=12,
    )
    snapshot = contracts.KnowledgeSnapshot.create(
        source_version="0.7.2",
        captured_at_utc="2026-07-30T12:00:02Z",
        captured_monotonic_ns=123_456,
        owners=(owner,),
        active_models=(active_model,),
        warnings=("fixture snapshot",),
    )
    plan_step = contracts.ContextPlanStepRef(
        "lexical",
        "document_lexical",
        "exact asset lookup",
        20,
        True,
    )
    plan = contracts.ContextPlanRef(
        "knowledge-plan-v2:fixture",
        "interruptor q52",
        "evidence",
        ("exact", "relational"),
        ("Q52",),
        ("document", "code"),
        ("pdf", "py"),
        "substation-alpha",
        "2026-01-01",
        "2026-07-30",
        False,
        20,
        3,
        128,
        500_000,
        (plan_step,),
        ("fixture plan",),
    )
    entity = contracts.ContextEntityRef(
        "entity:breaker:q52",
        "electrical_breaker",
        "Interruptor Q52",
        (evidence.evidence_id, other_evidence.evidence_id),
        (resource.resource_id, other_resource.resource_id),
    )
    other_entity = contracts.ContextEntityRef(
        "entity:function:validate-q52",
        "code_symbol",
        "control.validate_q52",
        (evidence.evidence_id, other_evidence.evidence_id),
        (resource.resource_id, other_resource.resource_id),
    )
    relation = contracts.ContextRelationRef(
        "relation:validate:q52",
        other_entity.entity_id,
        entity.entity_id,
        "validates",
        contracts.EvidenceMethod.STRUCTURAL,
        ("code:fixture:line:41",),
        (evidence.evidence_id, other_evidence.evidence_id),
        0.95,
    )
    contradiction = contracts.ContextContradictionRef.create(
        contradiction_kind="conflicting_structured_claim",
        topic="breaker_state",
        values=("open", "closed"),
        citation_ids=("K1", "K2"),
    )
    rendered_context = "\n".join(
        (
            entity.to_json(),
            other_entity.to_json(),
            relation.to_json(),
            f"{contradiction.summary} [K1, K2]",
        )
    )
    graph_budget = contracts.ContextGraphBudget(3, 2, 1)
    budget = contracts.ContextBudget(
        12_000,
        len(rendered_context),
        240,
        "unicode-codepoint-v1",
    )
    bundle = contracts.ContextBundle(
        "interruptor q52",
        ("exact", "relational"),
        plan.plan_id,
        plan,
        snapshot,
        (hit, other_hit),
        (("K1", evidence.evidence_id), ("K2", other_evidence.evidence_id)),
        graph_budget,
        budget,
        rendered_context,
        contracts.KnowledgeCompleteness.COMPLETE,
        entities=(entity, other_entity),
        relations=(relation,),
        contradictions=(contradiction,),
        missing_information=("No maintenance date was found.",),
        warnings=("Ranks are advisory.",),
        telemetry=telemetry,
    )
    return {
        "KnowledgeTelemetryClock": clock,
        "KnowledgePhaseTiming": planner_timing,
        "KnowledgeQueryTelemetry": telemetry,
        "PhysicalIdentityRef": physical_identity,
        "ResourceRef": resource,
        "RevisionRef": revision,
        "EvidenceRef": evidence,
        "RankingSignal": ranking_signal,
        "KnowledgeHit": hit,
        "PublicationHead": publication,
        "LogicalWatermark": watermark,
        "ActiveModel": active_model,
        "OwnerSnapshot": owner,
        "KnowledgeSnapshot": snapshot,
        "ContextPlanStepRef": plan_step,
        "ContextPlanRef": plan,
        "ContextGraphBudget": graph_budget,
        "ContextBudget": budget,
        "ContextEntityRef": entity,
        "ContextContradictionRef": contradiction,
        "ContextRelationRef": relation,
        "ContextBundle": bundle,
    }


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_exact_public_surface_enum_vocabulary_and_versions() -> None:
    assert contracts.__all__ == EXPECTED_ALL
    assert len(contracts.__all__) == 37
    assert contracts.KNOWLEDGE_CONTRACT_SCHEMA_VERSION == 1
    assert contracts.KNOWLEDGE_TELEMETRY_SCHEMA_VERSION == 1
    assert contracts.KNOWLEDGE_TELEMETRY_CLOCK_SIGNATURE == "python-perf-counter-ns-v1"
    assert (
        contracts.KNOWLEDGE_TELEMETRY_UNIDENTIFIED_CLOCK_SIGNATURE
        == "python-callable-unidentified-ns-v1"
    )
    assert contracts.MAX_EVIDENCE_IDENTIFIERS == 64
    assert contracts.MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS == 512
    assert contracts.MAX_EVIDENCE_SYMBOL_CHARS == 1_024

    public_enum_names = {
        name
        for name in contracts.__all__
        if isinstance((value := getattr(contracts, name)), type)
        and issubclass(value, StrEnum)
    }
    assert public_enum_names == set(EXPECTED_ENUM_MEMBERS)
    assert len(public_enum_names) == 8
    enum_types = {
        name: cast(type[StrEnum], getattr(contracts, name))
        for name in EXPECTED_ENUM_MEMBERS
    }
    for name, contract_type in enum_types.items():
        assert contract_type.__module__ == CONTRACT_MODULE
        assert (
            tuple((member.name, member.value) for member in contract_type)
            == (EXPECTED_ENUM_MEMBERS[name])
        )
        assert pickle.loads(pickle.dumps(contract_type, protocol=5)) is contract_type
        for member in contract_type:
            assert pickle.loads(pickle.dumps(member, protocol=5)) is member


def test_exact_dataclass_signatures_shape_defaults_and_pickle_identity() -> None:
    instances = _contract_instances()
    assert tuple(instances) == tuple(EXPECTED_DATACLASS_SIGNATURES)
    assert len(instances) == 22

    for name, expected_signature in EXPECTED_DATACLASS_SIGNATURES.items():
        contract_type = cast(type[object], getattr(contracts, name))
        instance = instances[name]
        parameters = contract_type.__dataclass_params__

        assert is_dataclass(contract_type)
        assert contract_type.__module__ == CONTRACT_MODULE
        assert contract_type.__qualname__ == name
        assert parameters.init is True
        assert parameters.repr is True
        assert parameters.eq is True
        assert parameters.order is False
        assert parameters.unsafe_hash is False
        assert parameters.frozen is True
        assert parameters.match_args is True
        assert parameters.kw_only is False
        assert parameters.slots is True
        assert parameters.weakref_slot is False
        assert contract_type.__match_args__ == tuple(
            field.name for field in fields(contract_type)
        )
        assert contract_type.__slots__ == contract_type.__match_args__
        assert "__dict__" not in contract_type.__dict__
        assert _stable_signature(contract_type) == expected_signature

        assert pickle.loads(pickle.dumps(contract_type, protocol=5)) is contract_type
        restored = pickle.loads(pickle.dumps(instance, protocol=5))
        assert type(restored) is contract_type
        assert restored == instance

        first_field = fields(contract_type)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(instance, first_field, getattr(instance, first_field))

    special_fields = {
        (name, field.name): (field.compare, field.repr)
        for name in EXPECTED_DATACLASS_SIGNATURES
        for field in fields(getattr(contracts, name))
        if not field.compare or not field.repr
    }
    assert special_fields == {
        ("KnowledgeTelemetryClock", "read_ns"): (False, False),
        ("ContextBundle", "telemetry"): (False, False),
    }


def test_private_validation_and_serialization_seams_remain_local() -> None:
    for name, expected_signature in PRIVATE_SIGNATURES.items():
        function = getattr(contracts, name)
        assert function.__module__ == CONTRACT_MODULE
        assert function.__qualname__ == name
        assert str(inspect.signature(function)) == expected_signature
        assert pickle.loads(pickle.dumps(function, protocol=5)) is function

    stable_id_descriptor = vars(contracts.ContextContradictionRef)["_stable_id"]
    stable_id = contracts.ContextContradictionRef._stable_id
    assert isinstance(stable_id_descriptor, staticmethod)
    assert stable_id.__module__ == CONTRACT_MODULE
    assert stable_id.__qualname__ == "ContextContradictionRef._stable_id"
    assert str(inspect.signature(stable_id)) == (
        "(contradiction_kind: 'str', topic: 'str', values: 'tuple[str, ...]') -> 'str'"
    )
    assert pickle.loads(pickle.dumps(stable_id, protocol=5)) is stable_id


def test_legacy_and_sdk_exports_preserve_exact_type_identity() -> None:
    import _04_Nucleo_Operativo as legacy
    import neocortex.sdk as sdk

    for name in LEGACY_AND_SDK_EXPORTS:
        source = getattr(contracts, name)
        assert getattr(legacy, name) is source
        assert getattr(sdk, name) is source


def test_contract_module_cold_import_has_a_minimal_dag() -> None:
    completed = _run_isolated(
        """
        import sys

        before = set(sys.modules)
        import _04_Nucleo_Operativo.knowledge_contracts
        loaded = {
            name
            for name in set(sys.modules) - before
            if name.startswith("_04_Nucleo_Operativo")
            or name.startswith("neocortex")
            or name == "xxhash"
        }
        required = {
            "_04_Nucleo_Operativo",
            "_04_Nucleo_Operativo.knowledge_contracts",
            "neocortex",
            "neocortex.knowledge",
            "neocortex.knowledge.knowledge_contracts",
            "_04_Nucleo_Operativo.semantic_models",
            "neocortex.platform",
            "xxhash",
        }
        future_extraction_modules = {
            "neocortex.knowledge.knowledge_contract_context",
            "neocortex.knowledge.knowledge_contract_payloads",
            "neocortex.knowledge.knowledge_contract_references",
            "neocortex.knowledge.knowledge_contract_snapshot",
            "neocortex.knowledge.knowledge_contract_telemetry",
            "neocortex.knowledge.knowledge_contract_validation",
        }
        missing = sorted(required - loaded)
        unexpected = sorted(loaded - required - future_extraction_modules)
        if missing or unexpected:
            raise SystemExit(
                f"unexpected contract import DAG: missing={missing!r}, "
                f"unexpected={unexpected!r}"
            )
        print("KNOWLEDGE_CONTRACT_DAG_OK")
        """
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "KNOWLEDGE_CONTRACT_DAG_OK"


def test_sdk_resolves_context_bundle_lazily_without_search_owners() -> None:
    completed = _run_isolated(
        """
        import sys

        import neocortex.sdk as sdk
        if any(name.startswith("_04_Nucleo_Operativo") for name in sys.modules):
            raise SystemExit("SDK imported the legacy facade eagerly")

        resolved = sdk.ContextBundle
        import _04_Nucleo_Operativo as legacy
        import _04_Nucleo_Operativo.knowledge_contracts as contracts

        if resolved is not legacy.ContextBundle or resolved is not contracts.ContextBundle:
            raise SystemExit("ContextBundle identity changed across facades")
        forbidden = {
            "_04_Nucleo_Operativo.knowledge_context",
            "_04_Nucleo_Operativo.knowledge_planner",
            "_04_Nucleo_Operativo.knowledge_search",
            "_04_Nucleo_Operativo.knowledge_service",
            "_04_Nucleo_Operativo.knowledge_snapshot",
        }
        unexpected = sorted(forbidden.intersection(sys.modules))
        if unexpected:
            raise SystemExit(f"ContextBundle loaded owners: {unexpected!r}")
        print("KNOWLEDGE_CONTRACT_LAZY_IDENTITY_OK")
        """
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "KNOWLEDGE_CONTRACT_LAZY_IDENTITY_OK"


def test_context_bundle_payload_and_canonical_serialization_are_golden() -> None:
    bundle = cast(contracts.ContextBundle, _contract_instances()["ContextBundle"])
    payload = bundle.to_dict()
    encoded = bundle.to_json()

    assert tuple(payload) == EXPECTED_BUNDLE_KEYS
    assert payload["schema_version"] == 1
    assert payload["kind"] == "context_bundle"
    assert payload["plan"] == bundle.plan.to_dict()
    assert payload["snapshot"] == bundle.snapshot.to_dict()
    assert payload["telemetry"] == bundle.telemetry.to_dict()  # type: ignore[union-attr]
    assert encoded == contracts._canonical_output(payload)
    assert "Área técnica" in encoded
    assert "\\u00c1" not in encoded
    assert len(encoded.encode("utf-8")) == 7_940
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == (
        "388cf6147a9f3b2aea0f7b7678aca812aacafabb18230ce707d22378f03ca7ff"
    )


@pytest.mark.parametrize(
    "namespace",
    (
        "planned_duplicate_of",
        "code_relation_source_resource",
        "code_relation_target_resource",
    ),
)
def test_context_bundle_accepts_explicit_relationship_grounding(
    namespace: str,
) -> None:
    bundle = cast(contracts.ContextBundle, _contract_instances()["ContextBundle"])
    first_hit, second_hit = bundle.selected_hits
    linked_resource_id = "resource:relationship:linked"
    changed_evidence = replace(
        first_hit.evidence,
        identifiers=(
            *first_hit.evidence.identifiers,
            (namespace, linked_resource_id),
        ),
    )
    changed_hit = replace(first_hit, evidence=changed_evidence)
    linked_entity = replace(
        bundle.entities[0],
        evidence_ids=(changed_evidence.evidence_id,),
        resource_ids=(linked_resource_id,),
    )
    contradiction = bundle.contradictions[0]
    rendered_context = "\n".join(
        (
            linked_entity.to_json(),
            f"{contradiction.summary} [K1, K2]",
        )
    )

    accepted = replace(
        bundle,
        selected_hits=(changed_hit, second_hit),
        entities=(linked_entity,),
        relations=(),
        graph_budget=contracts.ContextGraphBudget(4, 1, 0),
        rendered_context=rendered_context,
        budget=replace(bundle.budget, characters_used=len(rendered_context)),
    )

    assert accepted.entities[0].resource_ids == (linked_resource_id,)
    assert accepted.graph_budget.identifiers_considered == 4


def test_context_bundle_accepts_partial_graph_with_visible_omission_notice() -> None:
    bundle = cast(contracts.ContextBundle, _contract_instances()["ContextBundle"])
    notice = "Graph entity omitted by bounded context compilation."
    rendered_context = f"{bundle.rendered_context}\n{notice}"

    accepted = replace(
        bundle,
        graph_budget=replace(bundle.graph_budget, omitted_entities=1),
        completeness=contracts.KnowledgeCompleteness.PARTIAL,
        rendered_context=rendered_context,
        budget=replace(bundle.budget, characters_used=len(rendered_context)),
        warnings=(*bundle.warnings, notice),
    )

    assert accepted.graph_budget.omitted_total == 1
    assert accepted.to_dict()["completeness"] == "partial"
    assert notice in accepted.rendered_context


PHASE_INVALID_CASES = (
    ({"phase": "planner"}, "Knowledge timing phase is invalid"),
    ({"duration_ns": True}, "Knowledge timing duration_ns cannot be negative"),
    ({"duration_ns": -1}, "Knowledge timing duration_ns cannot be negative"),
    (
        {"service_attempt": True},
        "Knowledge timing service_attempt must be zero, one or two",
    ),
    (
        {"service_attempt": 3},
        "Knowledge timing service_attempt must be zero, one or two",
    ),
    ({"executed": 1}, "Knowledge timing executed must be boolean"),
    ({"owner": 1}, "Knowledge timing owner must be text when present"),
    (
        {"snapshot_id": 1},
        "Knowledge timing snapshot_id must be text when present",
    ),
    ({"owner": " "}, "Knowledge timing owner cannot be blank when present"),
    (
        {"snapshot_id": " "},
        "Knowledge timing snapshot_id cannot be blank when present",
    ),
    (
        {"owner": "x" * 257},
        "Knowledge timing owner is too long",
    ),
    (
        {"snapshot_id": "x" * 513},
        "Knowledge timing snapshot_id is too long",
    ),
    (
        {"ranking_names": ["rank"]},
        "Knowledge timing ranking_names must be a tuple",
    ),
    (
        {"ranking_names": tuple(str(index) for index in range(65))},
        "Knowledge timing has too many ranking names",
    ),
    (
        {"ranking_names": (1,)},
        "Knowledge timing ranking name must be text",
    ),
    (
        {"ranking_names": (" ",)},
        "Knowledge timing ranking name cannot be blank",
    ),
    (
        {"ranking_names": ("x" * 257,)},
        "Knowledge timing ranking name is too long",
    ),
    (
        {"ranking_names": tuple(f"{index:02d}" + "x" * 254 for index in range(17))},
        "Knowledge timing ranking names are too large",
    ),
    (
        {"ranking_names": ("rank", "rank")},
        "Knowledge timing ranking names must be unique",
    ),
    (
        {"phase": contracts.KnowledgeTimingPhase.FUSION},
        "attempt-scoped Knowledge timing requires attempt one or two",
    ),
    (
        {"service_attempt": 1},
        "operation-scoped Knowledge timing must use attempt zero",
    ),
    (
        {
            "phase": contracts.KnowledgeTimingPhase.OWNER_RANKING,
            "service_attempt": 1,
        },
        "owner_ranking timing requires owner and ranking names",
    ),
    (
        {"owner": "semantic"},
        "only owner_ranking timing may identify owners or rankings",
    ),
    (
        {
            "phase": contracts.KnowledgeTimingPhase.SNAPSHOT_BEFORE,
            "service_attempt": 1,
        },
        "snapshot timing requires snapshot_id",
    ),
    (
        {"snapshot_id": "snapshot:1"},
        "only snapshot timing may identify a snapshot",
    ),
)


@pytest.mark.parametrize(("overrides", "message"), PHASE_INVALID_CASES)
def test_phase_timing_validation_branches_are_exact(
    overrides: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "phase": contracts.KnowledgeTimingPhase.PLANNER,
        "duration_ns": 1,
    }
    values.update(overrides)

    with pytest.raises(ValueError) as captured:
        contracts.KnowledgePhaseTiming(**values)  # type: ignore[arg-type]
    assert str(captured.value) == message


EVIDENCE_INVALID_CASES = (
    ({"evidence_id": " "}, "evidence_id cannot be blank"),
    ({"sheet": " "}, "sheet cannot be blank when present"),
    ({"page": True}, "page cannot be negative"),
    ({"start_line": 1}, "line locator requires both start and end"),
    ({"end_line": 1}, "line locator requires both start and end"),
    ({"start_line": 0, "end_line": 1}, "line locator is invalid"),
    ({"start_ms": 1}, "time locator requires both start and end"),
    ({"end_ms": 2}, "time locator requires both start and end"),
    ({"start_ms": 2, "end_ms": 2}, "time locator is invalid"),
    ({"start_char": 1}, "character locator requires both start and end"),
    ({"end_char": 2}, "character locator requires both start and end"),
    ({"start_char": 2, "end_char": 2}, "character locator is invalid"),
    (
        {"bounding_box": (0.0, 0.0, float("nan"), 1.0)},
        "bounding box is invalid",
    ),
    (
        {"bounding_box": (0.0, 0.0, 1.0, 1.0)},
        "bounding box requires a coordinate space",
    ),
    (
        {"coordinate_space": "normalized-page-v1"},
        "coordinate space requires a bounding box",
    ),
    (
        {"snippet": "x" * 4_097},
        "snippet cannot exceed 4096 characters",
    ),
    ({"symbol": "x" * 1_025}, "symbol cannot exceed 1024 characters"),
    ({"generation": True}, "evidence generation cannot be negative"),
    (
        {"identifiers": tuple(("serial", str(index)) for index in range(65))},
        "evidence cannot contain more than 64 identifiers",
    ),
    (
        {"identifiers": (("serial", "Q52"), ("serial", "Q52"))},
        "evidence identifiers must be unique",
    ),
    (
        {"identifiers": ((1, "Q52"),)},
        "evidence identifiers must contain strings",
    ),
    (
        {"identifiers": ((" ", "Q52"),)},
        "identifier namespace cannot be blank",
    ),
    (
        {"identifiers": (("serial", " "),)},
        "identifier value cannot be blank",
    ),
    (
        {"identifiers": (("serial", "x" * 513),)},
        "evidence identifier components cannot exceed 512 characters",
    ),
)


@pytest.mark.parametrize(("overrides", "message"), EVIDENCE_INVALID_CASES)
def test_evidence_validation_branches_are_exact(
    overrides: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "evidence_id": "evidence:fixture",
        "resource_id": "resource:fixture",
        "revision_id": "revision:fixture",
        "method": contracts.EvidenceMethod.EXTRACTED,
    }
    values.update(overrides)

    with pytest.raises(ValueError) as captured:
        contracts.EvidenceRef(**values)  # type: ignore[arg-type]
    assert str(captured.value) == message


BUNDLE_INVALID_CASES = (
    ("plan_id", "context plan_id must match the normalized plan"),
    ("query", "context query must match the normalized plan"),
    ("intents", "context intents must match the normalized plan"),
    (
        "duplicate_evidence",
        "selected hits require unique evidence identifiers for citations",
    ),
    ("duplicate_citation", "citation identifiers must be unique"),
    ("unknown_citation_evidence", "citation must reference selected evidence"),
    (
        "citation_coverage",
        "each selected hit must have exactly one citation by evidence_id",
    ),
    ("duplicate_entity", "context entity identifiers must be unique"),
    ("entity_uncited", "context entities must reference cited evidence"),
    (
        "entity_unknown_resource",
        "context entities must reference a grounded resource",
    ),
    (
        "entity_ungrounded_by_evidence",
        "context entity resources must be grounded by its evidence references",
    ),
    ("duplicate_relation_id", "context relation identifiers must be unique"),
    ("duplicate_logical_relation", "logical context relations must be unique"),
    (
        "relation_missing_entity",
        "context relations must reference existing entities",
    ),
    ("relation_uncited", "context relations must reference cited evidence"),
    (
        "relation_not_grounding_endpoints",
        "context relation evidence must ground both endpoints",
    ),
    (
        "identifier_count",
        "context graph identifier count must match selected evidence",
    ),
    ("entity_count", "context graph entity count must match entities"),
    ("relation_count", "context graph relation count must match relations"),
    (
        "omitted_complete",
        "omitted context graph data requires partial completeness",
    ),
    (
        "entity_not_rendered",
        "context entities must be rendered inside the character budget",
    ),
    (
        "relation_not_rendered",
        "context relations must be rendered inside the character budget",
    ),
    (
        "duplicate_contradiction",
        "context contradiction identifiers must be unique",
    ),
    (
        "unknown_contradiction_citation",
        "contradictions require at least two existing citations",
    ),
    (
        "contradiction_not_rendered",
        "context contradictions must be rendered inside the character budget",
    ),
    ("blank_notice", "context notice cannot be blank"),
    (
        "omitted_without_visible_notice",
        "omitted context graph data requires a rendered visible notice",
    ),
    (
        "character_count",
        "rendered context and budget character count disagree",
    ),
    ("invalid_telemetry", "context telemetry is invalid"),
    (
        "search_telemetry",
        "ContextBundle telemetry must describe a context operation",
    ),
)


def _without_rendered_item(
    bundle: contracts.ContextBundle,
    item: str,
) -> contracts.ContextBundle:
    rendered = "\n".join(
        line for line in bundle.rendered_context.splitlines() if line != item
    )
    return replace(
        bundle,
        rendered_context=rendered,
        budget=replace(bundle.budget, characters_used=len(rendered)),
    )


def _make_invalid_bundle(case: str) -> contracts.ContextBundle:
    bundle = cast(contracts.ContextBundle, _contract_instances()["ContextBundle"])
    first_hit, second_hit = bundle.selected_hits
    first_entity, second_entity = bundle.entities
    relation = bundle.relations[0]
    contradiction = bundle.contradictions[0]

    if case == "plan_id":
        return replace(bundle, plan_id="knowledge-plan-v2:other")
    if case == "query":
        return replace(bundle, normalized_query="different query")
    if case == "intents":
        return replace(bundle, intents=("different",))
    if case == "duplicate_evidence":
        return replace(bundle, selected_hits=(first_hit, replace(first_hit, rank=2)))
    if case == "duplicate_citation":
        return replace(
            bundle,
            citation_ids=(
                ("K1", first_hit.evidence.evidence_id),
                ("K1", second_hit.evidence.evidence_id),
            ),
        )
    if case == "unknown_citation_evidence":
        return replace(
            bundle,
            citation_ids=(
                ("K1", first_hit.evidence.evidence_id),
                ("K2", "evidence:missing"),
            ),
        )
    if case == "citation_coverage":
        return replace(bundle, citation_ids=bundle.citation_ids[:1])
    if case == "duplicate_entity":
        return replace(
            bundle,
            entities=(
                first_entity,
                replace(second_entity, entity_id=first_entity.entity_id),
            ),
        )
    if case == "entity_uncited":
        return replace(
            bundle,
            entities=(
                replace(first_entity, evidence_ids=("evidence:missing",)),
                second_entity,
            ),
        )
    if case == "entity_unknown_resource":
        return replace(
            bundle,
            entities=(
                replace(first_entity, resource_ids=("resource:missing",)),
                second_entity,
            ),
        )
    if case == "entity_ungrounded_by_evidence":
        return replace(
            bundle,
            entities=(
                replace(
                    first_entity,
                    evidence_ids=(first_hit.evidence.evidence_id,),
                ),
                second_entity,
            ),
        )
    if case == "duplicate_relation_id":
        return replace(bundle, relations=(relation, relation))
    if case == "duplicate_logical_relation":
        return replace(
            bundle,
            relations=(relation, replace(relation, relation_id="relation:other")),
        )
    if case == "relation_missing_entity":
        return replace(
            bundle,
            relations=(replace(relation, target_entity_id="entity:missing"),),
        )
    if case == "relation_uncited":
        return replace(
            bundle,
            relations=(replace(relation, evidence_ids=("evidence:missing",)),),
        )
    if case == "relation_not_grounding_endpoints":
        changed_second = replace(
            second_entity,
            evidence_ids=(second_hit.evidence.evidence_id,),
            resource_ids=(second_hit.resource.resource_id,),
        )
        return replace(
            bundle,
            entities=(first_entity, changed_second),
            relations=(
                replace(
                    relation,
                    evidence_ids=(first_hit.evidence.evidence_id,),
                ),
            ),
        )
    if case == "identifier_count":
        return replace(
            bundle,
            graph_budget=replace(bundle.graph_budget, identifiers_considered=99),
        )
    if case == "entity_count":
        return replace(
            bundle,
            graph_budget=replace(bundle.graph_budget, entities_included=99),
        )
    if case == "relation_count":
        return replace(
            bundle,
            graph_budget=replace(bundle.graph_budget, relations_included=99),
        )
    if case == "omitted_complete":
        return replace(
            bundle,
            graph_budget=replace(bundle.graph_budget, omitted_entities=1),
        )
    if case == "entity_not_rendered":
        return _without_rendered_item(bundle, first_entity.to_json())
    if case == "relation_not_rendered":
        return _without_rendered_item(bundle, relation.to_json())
    if case == "duplicate_contradiction":
        return replace(bundle, contradictions=(contradiction, contradiction))
    if case == "unknown_contradiction_citation":
        return replace(
            bundle,
            contradictions=(replace(contradiction, citation_ids=("K1", "K-missing")),),
        )
    if case == "contradiction_not_rendered":
        rendered = f"{contradiction.summary} [{', '.join(contradiction.citation_ids)}]"
        return _without_rendered_item(bundle, rendered)
    if case == "blank_notice":
        return replace(bundle, missing_information=(" ",))
    if case == "omitted_without_visible_notice":
        return replace(
            bundle,
            graph_budget=replace(bundle.graph_budget, omitted_entities=1),
            completeness=contracts.KnowledgeCompleteness.PARTIAL,
        )
    if case == "character_count":
        return replace(
            bundle,
            budget=replace(
                bundle.budget,
                characters_used=bundle.budget.characters_used + 1,
            ),
        )
    if case == "invalid_telemetry":
        return replace(bundle, telemetry="invalid")  # type: ignore[arg-type]
    if case == "search_telemetry":
        telemetry = contracts.KnowledgeQueryTelemetry(
            contracts.KnowledgeTelemetryOperation.SEARCH,
            1,
            (
                contracts.KnowledgePhaseTiming(
                    contracts.KnowledgeTimingPhase.PLANNER,
                    1,
                ),
            ),
        )
        return replace(bundle, telemetry=telemetry)
    raise AssertionError(f"unknown characterization case: {case}")


@pytest.mark.parametrize(("case", "message"), BUNDLE_INVALID_CASES)
def test_context_bundle_validation_branches_are_exact(
    case: str,
    message: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        _make_invalid_bundle(case)
    assert str(captured.value) == message


# endregion [02]
