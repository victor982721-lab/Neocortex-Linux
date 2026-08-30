"""Stable, immutable contracts for the read-only Knowledge Plane."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contracts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from neocortex.knowledge.knowledge_contracts import (
    MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS,
    MAX_EVIDENCE_IDENTIFIERS,
    MAX_EVIDENCE_SYMBOL_CHARS,
    ActiveModel,
    ContextBudget,
    ContextBundle,
    ContextContradictionRef,
    ContextEntityRef,
    ContextGraphBudget,
    ContextPlanRef,
    ContextPlanStepRef,
    ContextRelationRef,
    EvidenceMethod,
    EvidenceRef,
    KnowledgeCompleteness,
    KnowledgeHit,
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    PhysicalIdentityRef,
    PublicationHead,
    RankingSignal,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
    SnapshotConsistency,
)
# endregion [01]

# region [02] Implementación


def _context_plan(
    *,
    plan_id: str,
    normalized_query: str,
    intents: tuple[str, ...],
) -> ContextPlanRef:
    return ContextPlanRef(
        plan_id=plan_id,
        normalized_query=normalized_query,
        retrieval_mode="evidence",
        intents=intents,
        exact_terms=(),
        source_kinds=(),
        formats=(),
        project=None,
        date_from=None,
        date_to=None,
        include_history=False,
        limit=20,
        max_per_resource=3,
        min_section_distance=128,
        max_vectors=500_000,
        steps=(
            ContextPlanStepRef(
                channel="lexical",
                ranking_name="fixture",
                reason="fixture retrieval",
                candidate_limit=20,
                required=True,
            ),
        ),
    )


def _resource() -> ResourceRef:
    return ResourceRef(
        resource_id="resource:code:0001",
        source_kind="code",
        owner="code",
        physical_identity=PhysicalIdentityRef("windows_file_id", "aa:bb", 1),
        current_path=r"C:\Corpus\Área técnica #1\control.py",
        disposition=ResourceDisposition.CANONICAL,
    )


def _revision(resource: ResourceRef) -> RevisionRef:
    return RevisionRef(
        resource_id=resource.resource_id,
        revision_id="code-version:7",
        producer="code-route",
        processing_signature="code-v2:fixture",
        generation=7,
        state=RevisionState.CURRENT,
    )


def _evidence(revision: RevisionRef) -> EvidenceRef:
    return EvidenceRef(
        evidence_id="code-version:7:chunk:2",
        resource_id=revision.resource_id,
        revision_id=revision.revision_id,
        method=EvidenceMethod.STRUCTURAL,
        start_line=41,
        end_line=55,
        section_kind="code_function",
        section_id="2",
        symbol="control.validate",
        snippet="def validar_área(): ...",
        extractor="python-ast",
        extractor_version="3.13",
        generation=7,
    )


def _snapshot(*, generation: int = 3, captured_ns: int = 10) -> KnowledgeSnapshot:
    owner = OwnerSnapshot(
        owner="semantic",
        state=OwnerAvailability.AVAILABLE,
        expected_schema_version=6,
        observed_schema_version=6,
        publications=(
            PublicationHead(
                scope="model:fixture",
                publication_id=f"semantic:{generation}",
                generation=generation,
                model_signature="fixture-model",
            ),
        ),
        watermarks=(LogicalWatermark("member_limit", "bounded"),),
        data_version_before=2,
        data_version_after=2,
    )
    model = ActiveModel(
        signature="fixture-model",
        vector_space="fixture-space",
        modality="text",
        dimensions=8,
        generation=generation,
    )
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T01:02:03Z",
        captured_monotonic_ns=captured_ns,
        owners=(owner,),
        active_models=(model,),
    )


def test_contract_json_is_versioned_unicode_stable_and_omits_unknown_precision() -> None:
    resource = _resource()
    revision = _revision(resource)
    evidence = _evidence(revision)
    hit = KnowledgeHit(
        rank=1,
        resource=resource,
        revision=revision,
        evidence=evidence,
        signals=(
            RankingSignal(
                source="code_semantic",
                score_kind="cosine",
                raw_score=0.875,
                source_rank=1,
                model_signature="fixture-model",
                generation=7,
                query_model_signature="fixture-query-model",
            ),
        ),
        fused_score=1.0 / 61.0,
        reasons=("semantic code chunk matched",),
    )

    payload = hit.to_dict()
    evidence_payload = payload["evidence"]
    assert isinstance(evidence_payload, dict)
    assert payload["schema_version"] == 1
    assert "page" not in evidence_payload
    assert "bounding_box" not in evidence_payload
    signals_payload = payload["signals"]
    assert isinstance(signals_payload, list)
    signal_payload = signals_payload[0]
    assert isinstance(signal_payload, dict)
    assert signal_payload["model_signature"] == "fixture-model"
    assert signal_payload["query_model_signature"] == "fixture-query-model"
    assert "query_model_signature" not in RankingSignal(
        "lexical",
        "bm25",
        1.0,
        1,
    ).to_dict()
    assert "Área técnica" in hit.to_json()
    assert "\\u00c1" not in hit.to_json()

    with pytest.raises(FrozenInstanceError):
        resource.resource_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("values", "message"),
    (
        ({"start_line": 0, "end_line": 1}, "line"),
        ({"start_line": 3, "end_line": 2}, "line"),
        ({"start_ms": 50, "end_ms": 50}, "time"),
        ({"start_char": 4, "end_char": 4}, "character"),
        ({"bounding_box": (0.0, 0.0, float("nan"), 1.0)}, "bounding"),
    ),
)
def test_evidence_rejects_invented_or_invalid_precision(
    values: dict[str, object],
    message: str,
) -> None:
    revision = _revision(_resource())
    with pytest.raises(ValueError, match=message):
        EvidenceRef(
            evidence_id="invalid",
            resource_id=revision.resource_id,
            revision_id=revision.revision_id,
            method=EvidenceMethod.EXTRACTED,
            **values,  # type: ignore[arg-type]
        )


def test_evidence_graph_inputs_are_explicitly_bounded() -> None:
    evidence = _evidence(_revision(_resource()))
    accepted = replace(
        evidence,
        identifiers=tuple(
            ("serial", f"SN-{index}")
            for index in range(MAX_EVIDENCE_IDENTIFIERS)
        ),
    )
    assert len(accepted.identifiers) == MAX_EVIDENCE_IDENTIFIERS

    with pytest.raises(ValueError, match="more than"):
        replace(
            evidence,
            identifiers=tuple(
                ("serial", f"SN-{index}")
                for index in range(MAX_EVIDENCE_IDENTIFIERS + 1)
            ),
        )
    with pytest.raises(ValueError, match="identifier components"):
        replace(
            evidence,
            identifiers=(
                ("serial", "x" * (MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS + 1)),
            ),
        )
    with pytest.raises(ValueError, match="symbol"):
        replace(evidence, symbol="x" * (MAX_EVIDENCE_SYMBOL_CHARS + 1))


def test_snapshot_identity_excludes_capture_and_connection_local_data_version() -> None:
    first = _snapshot(captured_ns=10)
    second_owner = OwnerSnapshot(
        owner="semantic",
        state=OwnerAvailability.AVAILABLE,
        expected_schema_version=6,
        observed_schema_version=6,
        publications=first.owners[0].publications,
        watermarks=first.owners[0].watermarks,
        data_version_before=99,
        data_version_after=99,
    )
    second = KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2030-01-01T00:00:00Z",
        captured_monotonic_ns=999,
        owners=(second_owner,),
        active_models=first.active_models,
    )
    changed = _snapshot(generation=4)

    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_id != changed.snapshot_id
    assert first.to_json() == first.to_json()


def test_snapshot_consistency_and_active_models_require_observed_evidence() -> None:
    changed_owner = OwnerSnapshot(
        owner="code",
        state=OwnerAvailability.AVAILABLE,
        expected_schema_version=2,
        observed_schema_version=2,
        data_version_before=1,
        data_version_after=2,
    )
    stable_owner = replace(changed_owner, data_version_after=1)

    with pytest.raises(ValueError, match="stable snapshot"):
        KnowledgeSnapshot.create(
            source_version="0.7.0",
            captured_at_utc="2026-07-26T01:02:03Z",
            captured_monotonic_ns=1,
            owners=(changed_owner,),
        )
    with pytest.raises(ValueError, match="two attempts"):
        KnowledgeSnapshot.create(
            source_version="0.7.0",
            captured_at_utc="2026-07-26T01:02:03Z",
            captured_monotonic_ns=1,
            owners=(changed_owner,),
            consistency=SnapshotConsistency.SNAPSHOT_CHANGED,
            attempts=1,
        )
    with pytest.raises(ValueError, match="changed owner"):
        KnowledgeSnapshot.create(
            source_version="0.7.0",
            captured_at_utc="2026-07-26T01:02:03Z",
            captured_monotonic_ns=1,
            owners=(stable_owner,),
            consistency=SnapshotConsistency.SNAPSHOT_CHANGED,
            attempts=2,
        )

    changed = KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T01:02:03Z",
        captured_monotonic_ns=1,
        owners=(changed_owner,),
        consistency=SnapshotConsistency.SNAPSHOT_CHANGED,
        attempts=2,
    )
    assert changed.changed_owners == ("code",)
    explicit_identity_change = replace(stable_owner, identity_changed=True)
    explicit_marker = KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T01:02:03Z",
        captured_monotonic_ns=1,
        owners=(explicit_identity_change,),
        consistency=SnapshotConsistency.SNAPSHOT_CHANGED,
        attempts=2,
    )
    assert explicit_marker.changed_owners == ("code",)
    assert explicit_marker.owners[0].to_dict()["identity_changed"] is True

    valid = _snapshot()
    incompatible_model = replace(valid.active_models[0], generation=99)
    with pytest.raises(ValueError, match="semantic publication"):
        KnowledgeSnapshot.create(
            source_version="0.7.0",
            captured_at_utc="2026-07-26T01:02:03Z",
            captured_monotonic_ns=1,
            owners=valid.owners,
            active_models=(incompatible_model,),
        )


def test_context_bundle_validates_citations_and_budget() -> None:
    resource = _resource()
    revision = _revision(resource)
    evidence = _evidence(revision)
    hit = KnowledgeHit(
        rank=1,
        resource=resource,
        revision=revision,
        evidence=evidence,
        signals=(RankingSignal("code", "rank", 1.0, 1),),
        fused_score=1.0,
        reasons=("exact symbol",),
    )
    rendered = "[K1] def validar_área(): ..."
    bundle = ContextBundle(
        normalized_query="¿Dónde se valida?",
        intents=("structural",),
        plan_id="plan:fixture",
        plan=_context_plan(
            plan_id="plan:fixture",
            normalized_query="¿Dónde se valida?",
            intents=("structural",),
        ),
        snapshot=_snapshot(),
        selected_hits=(hit,),
        citation_ids=(("K1", evidence.evidence_id),),
        graph_budget=ContextGraphBudget(0, 0, 0),
        budget=ContextBudget(
            character_limit=2_000,
            characters_used=len(rendered),
            estimated_tokens=30,
            estimator_signature="unicode-codepoint-v1",
        ),
        rendered_context=rendered,
        completeness=KnowledgeCompleteness.COMPLETE,
    )
    assert bundle.to_dict()["citation_ids"] == [
        {"citation_id": "K1", "evidence_id": evidence.evidence_id}
    ]
    assert bundle.to_dict()["entities"] == []
    assert bundle.to_dict()["relations"] == []
    assert bundle.budget.to_dict()["measurement_scope"] == "rendered_context"
    assert bundle.to_dict()["plan"] == bundle.plan.to_dict()
    assert bundle.plan.to_dict()["steps"] == [
        {
            "schema_version": 1,
            "kind": "context_plan_step_ref",
            "channel": "lexical",
            "ranking_name": "fixture",
            "reason": "fixture retrieval",
            "candidate_limit": 20,
            "required": True,
        }
    ]
    assert bundle.graph_budget.to_dict()["measurement_scope"] == (
        "selected_evidence_graph"
    )

    with pytest.raises(ValueError, match="rendered_context"):
        replace(bundle.budget, measurement_scope="bundle_json")

    with pytest.raises(ValueError, match="exactly one citation"):
        replace(bundle, citation_ids=())
    with pytest.raises(ValueError, match="exactly one citation"):
        replace(
            bundle,
            citation_ids=(
                ("K1", evidence.evidence_id),
                ("K2", evidence.evidence_id),
            ),
        )
    with pytest.raises(ValueError, match="unique"):
        ContextContradictionRef.create(
            contradiction_kind="conflicting_structured_claim",
            topic="state",
            values=("closed", "open"),
            citation_ids=("K1", "K1"),
        )
    with pytest.raises(ValueError, match="plan_id"):
        replace(bundle, plan=replace(bundle.plan, plan_id="plan:other"))
    with pytest.raises(ValueError, match="query"):
        replace(bundle, plan=replace(bundle.plan, normalized_query="other query"))
    with pytest.raises(ValueError, match="intents"):
        replace(bundle, plan=replace(bundle.plan, intents=("other",)))

    with pytest.raises(ValueError, match="selected evidence"):
        ContextBundle(
            normalized_query="query",
            intents=(),
            plan_id="plan:fixture",
            plan=_context_plan(
                plan_id="plan:fixture",
                normalized_query="query",
                intents=(),
            ),
            snapshot=_snapshot(),
            selected_hits=(hit,),
            citation_ids=(("K1", "missing"),),
            graph_budget=ContextGraphBudget(0, 0, 0),
            budget=ContextBudget(100, 0, 0, "unicode-codepoint-v1"),
            rendered_context="",
            completeness=KnowledgeCompleteness.PARTIAL,
        )


def test_context_entity_and_relation_contracts_are_versioned_and_immutable() -> None:
    entity = ContextEntityRef(
        entity_id="entity:breaker:q52",
        entity_kind="electrical_breaker",
        label="Interruptor Q52",
        evidence_ids=("evidence:q52:1",),
        resource_ids=("resource:manual:1",),
    )
    relation = ContextRelationRef(
        relation_id="relation:q52:feeds:bus-a",
        source_entity_id=entity.entity_id,
        target_entity_id="entity:bus:a",
        relation_kind="feeds",
        method=EvidenceMethod.HUMAN_CONFIRMED,
        provenance=("human_review:fixture",),
        evidence_ids=("evidence:q52:1",),
        confidence=1.0,
    )

    assert entity.to_dict() == {
        "schema_version": 1,
        "kind": "context_entity_ref",
        "entity_id": "entity:breaker:q52",
        "entity_kind": "electrical_breaker",
        "label": "Interruptor Q52",
        "evidence_ids": ["evidence:q52:1"],
        "resource_ids": ["resource:manual:1"],
    }
    assert relation.to_dict()["kind"] == "context_relation_ref"
    assert relation.to_dict()["method"] == "human_confirmed"
    assert relation.to_dict()["provenance"] == ["human_review:fixture"]
    assert relation.to_dict()["confidence"] == 1.0
    assert "Interruptor Q52" in entity.to_json()
    assert entity.to_json() == entity.to_json()

    with pytest.raises(FrozenInstanceError):
        entity.label = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unique"):
        replace(entity, evidence_ids=("evidence:q52:1", "evidence:q52:1"))
    with pytest.raises(ValueError, match="at least one reference"):
        replace(entity, resource_ids=())
    with pytest.raises(ValueError, match="label"):
        replace(entity, label=" ")
    with pytest.raises(ValueError, match="different entities"):
        replace(relation, target_entity_id=relation.source_entity_id)
    with pytest.raises(ValueError, match="unique"):
        replace(
            relation,
            evidence_ids=("evidence:q52:1", "evidence:q52:1"),
        )
    with pytest.raises(ValueError, match="method"):
        replace(relation, method="invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provenance"):
        replace(relation, provenance=())
    with pytest.raises(ValueError, match="confidence"):
        replace(relation, confidence=float("nan"))
    with pytest.raises(ValueError, match="confidence"):
        replace(relation, confidence=1.01)


def test_context_bundle_validates_and_serializes_typed_graph_references() -> None:
    resource = _resource()
    revision = _revision(resource)
    evidence = _evidence(revision)
    hit = KnowledgeHit(
        rank=1,
        resource=resource,
        revision=revision,
        evidence=evidence,
        signals=(RankingSignal("code", "rank", 1.0, 1),),
        fused_score=1.0,
        reasons=("exact symbol",),
    )
    breaker = ContextEntityRef(
        entity_id="entity:breaker:q52",
        entity_kind="electrical_breaker",
        label="Q52",
        evidence_ids=(evidence.evidence_id,),
        resource_ids=(resource.resource_id,),
    )
    function = ContextEntityRef(
        entity_id="entity:function:validate",
        entity_kind="code_symbol",
        label="control.validate",
        evidence_ids=(evidence.evidence_id,),
        resource_ids=(resource.resource_id,),
    )
    relation = ContextRelationRef(
        relation_id="relation:validate:q52",
        source_entity_id=function.entity_id,
        target_entity_id=breaker.entity_id,
        relation_kind="validates",
        method=EvidenceMethod.STRUCTURAL,
        provenance=("code:fixture:1",),
        evidence_ids=(evidence.evidence_id,),
        confidence=0.95,
    )

    def make_bundle(
        *,
        entities: tuple[ContextEntityRef, ...],
        relations: tuple[ContextRelationRef, ...],
    ) -> ContextBundle:
        rendered_graph = "\n".join(
            (
                *(entity.to_json() for entity in entities),
                *(relation.to_json() for relation in relations),
            )
        )
        return ContextBundle(
            normalized_query="Q52 validation",
            intents=("structural",),
            plan_id="plan:graph-fixture",
            plan=_context_plan(
                plan_id="plan:graph-fixture",
                normalized_query="Q52 validation",
                intents=("structural",),
            ),
            snapshot=_snapshot(),
            selected_hits=(hit,),
            citation_ids=(("K1", evidence.evidence_id),),
            graph_budget=ContextGraphBudget(
                0,
                len(entities),
                len(relations),
            ),
            budget=ContextBudget(
                10_000,
                len(rendered_graph),
                0,
                "fixture-v1",
            ),
            rendered_context=rendered_graph,
            completeness=KnowledgeCompleteness.COMPLETE,
            entities=entities,
            relations=relations,
        )

    bundle = make_bundle(entities=(breaker, function), relations=(relation,))
    payload = bundle.to_dict()
    assert payload["entities"] == [breaker.to_dict(), function.to_dict()]
    assert payload["relations"] == [relation.to_dict()]
    assert bundle.to_json() == bundle.to_json()

    with pytest.raises(ValueError, match="rendered inside"):
        replace(
            bundle,
            rendered_context="",
            budget=replace(bundle.budget, characters_used=0),
        )
    with pytest.raises(ValueError, match="logical context relations"):
        make_bundle(
            entities=(breaker, function),
            relations=(
                relation,
                replace(relation, relation_id="relation:duplicate-logical-edge"),
            ),
        )

    with pytest.raises(ValueError, match="cited evidence"):
        make_bundle(
            entities=(replace(breaker, evidence_ids=("missing",)),),
            relations=(),
        )
    with pytest.raises(ValueError, match="grounded resource"):
        make_bundle(
            entities=(replace(breaker, resource_ids=("resource:external",)),),
            relations=(),
        )
    with pytest.raises(ValueError, match="existing entities"):
        make_bundle(
            entities=(breaker, function),
            relations=(replace(relation, target_entity_id="entity:missing"),),
        )
    with pytest.raises(ValueError, match="entity identifiers"):
        make_bundle(entities=(breaker, breaker), relations=())
    with pytest.raises(ValueError, match="cited evidence"):
        make_bundle(
            entities=(breaker, function),
            relations=(replace(relation, evidence_ids=("missing",)),),
        )

    other_resource = replace(
        resource,
        resource_id="resource:code:0002",
        current_path=r"C:\Corpus\other.py",
    )
    other_revision = replace(
        revision,
        resource_id=other_resource.resource_id,
        revision_id="code-version:8",
    )
    other_evidence = replace(
        evidence,
        evidence_id="code-version:8:chunk:1",
        resource_id=other_resource.resource_id,
        revision_id=other_revision.revision_id,
    )
    other_hit = KnowledgeHit(
        rank=2,
        resource=other_resource,
        revision=other_revision,
        evidence=other_evidence,
        signals=(RankingSignal("code", "rank", 0.5, 2),),
        fused_score=0.5,
        reasons=("second exact symbol",),
    )
    other_entity = ContextEntityRef(
        entity_id="entity:other",
        entity_kind="code_symbol",
        label="other.validate",
        evidence_ids=(other_evidence.evidence_id,),
        resource_ids=(other_resource.resource_id,),
    )

    def make_two_hit_bundle(
        *,
        entities: tuple[ContextEntityRef, ...],
        relations: tuple[ContextRelationRef, ...],
    ) -> ContextBundle:
        rendered_graph = "\n".join(
            (
                *(entity.to_json() for entity in entities),
                *(relation.to_json() for relation in relations),
            )
        )
        return ContextBundle(
            normalized_query="cross-resource relation",
            intents=("relational",),
            plan_id="plan:two-hit-fixture",
            plan=_context_plan(
                plan_id="plan:two-hit-fixture",
                normalized_query="cross-resource relation",
                intents=("relational",),
            ),
            snapshot=_snapshot(),
            selected_hits=(hit, other_hit),
            citation_ids=(
                ("K1", evidence.evidence_id),
                ("K2", other_evidence.evidence_id),
            ),
            graph_budget=ContextGraphBudget(
                0,
                len(entities),
                len(relations),
            ),
            budget=ContextBudget(
                10_000,
                len(rendered_graph),
                0,
                "fixture-v1",
            ),
            rendered_context=rendered_graph,
            completeness=KnowledgeCompleteness.COMPLETE,
            entities=entities,
            relations=relations,
        )

    with pytest.raises(ValueError, match="grounded by"):
        make_two_hit_bundle(
            entities=(
                replace(
                    breaker,
                    resource_ids=(
                        resource.resource_id,
                        other_resource.resource_id,
                    ),
                ),
                other_entity,
            ),
            relations=(),
        )
    with pytest.raises(ValueError, match="ground both endpoints"):
        make_two_hit_bundle(
            entities=(breaker, other_entity),
            relations=(
                ContextRelationRef(
                    relation_id="relation:ungrounded",
                    source_entity_id=breaker.entity_id,
                    target_entity_id=other_entity.entity_id,
                    relation_kind="mentions",
                    method=EvidenceMethod.INFERRED,
                    provenance=("fixture:ungrounded",),
                    evidence_ids=(evidence.evidence_id,),
                ),
            ),
        )

    two_hit_bundle = make_two_hit_bundle(entities=(), relations=())
    contradiction = ContextContradictionRef.create(
        contradiction_kind="conflicting_structured_claim",
        topic="fixture_state",
        values=("closed", "open"),
        citation_ids=("K1", "K2"),
    )
    assert contradiction.to_dict()["contradiction_kind"] == (
        "conflicting_structured_claim"
    )
    assert contradiction.to_dict()["summary"] == (
        'Structured claim "fixture_state" has conflicting values: '
        '"closed", "open".'
    )
    assert contradiction.to_json() == contradiction.to_json()
    with pytest.raises(ValueError, match="identity"):
        replace(contradiction, contradiction_id="context-contradiction-v1:wrong")
    with pytest.raises(ValueError, match="rendered inside"):
        replace(two_hit_bundle, contradictions=(contradiction,))
    rendered_contradiction = (
        f"{contradiction.summary} [{', '.join(contradiction.citation_ids)}]"
    )
    contradiction_bundle = replace(
        two_hit_bundle,
        rendered_context=rendered_contradiction,
        budget=replace(
            two_hit_bundle.budget,
            characters_used=len(rendered_contradiction),
        ),
    )
    with pytest.raises(ValueError, match="contradiction identifiers"):
        replace(
            contradiction_bundle,
            contradictions=(contradiction, contradiction),
        )
# endregion [02]
