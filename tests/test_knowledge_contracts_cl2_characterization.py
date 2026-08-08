"""Tests-first gates for the P2-5 Knowledge contract extraction."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_contracts_cl2_characterization.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import replace

import pytest

import _04_Nucleo_Operativo.knowledge_contracts as contracts
from tests.test_knowledge_contracts_characterization import (
    _contract_instances as make_contract_instances,
)
# endregion [01]

# region [02] Implementación


EXPECTED_TO_DICT_KEYS = {
    "KnowledgePhaseTiming": (
        "phase",
        "duration_ns",
        "service_attempt",
        "executed",
    ),
    "KnowledgeQueryTelemetry": (
        "schema_version",
        "kind",
        "operation",
        "clock_signature",
        "total_duration_ns",
        "phases",
    ),
    "PhysicalIdentityRef": ("scheme", "value", "identity_version"),
    "ResourceRef": (
        "schema_version",
        "kind",
        "resource_id",
        "source_kind",
        "owner",
        "physical_identity",
        "current_path",
        "disposition",
    ),
    "RevisionRef": (
        "schema_version",
        "kind",
        "resource_id",
        "revision_id",
        "producer",
        "processing_signature",
        "state",
        "generation",
        "observed_at_utc",
    ),
    "EvidenceRef": (
        "schema_version",
        "kind",
        "evidence_id",
        "resource_id",
        "revision_id",
        "method",
        "page",
        "start_line",
        "end_line",
        "sheet",
        "cell_range",
        "start_ms",
        "end_ms",
        "coordinate_space",
        "start_char",
        "end_char",
        "symbol",
        "section_kind",
        "section_id",
        "snippet",
        "extractor",
        "extractor_version",
        "generation",
        "bounding_box",
        "identifiers",
    ),
    "RankingSignal": (
        "source",
        "score_kind",
        "raw_score",
        "source_rank",
        "model_signature",
        "query_model_signature",
        "generation",
        "contribution",
    ),
    "KnowledgeHit": (
        "schema_version",
        "kind",
        "rank",
        "resource",
        "revision",
        "evidence",
        "signals",
        "fused_score",
        "reasons",
        "confidence",
        "warnings",
    ),
    "PublicationHead": (
        "scope",
        "publication_id",
        "generation",
        "model_signature",
    ),
    "LogicalWatermark": ("name", "value"),
    "ActiveModel": (
        "signature",
        "vector_space",
        "modality",
        "dimensions",
        "generation",
    ),
    "OwnerSnapshot": (
        "owner",
        "state",
        "expected_schema_version",
        "publications",
        "watermarks",
        "observed_schema_version",
        "data_version_before",
        "data_version_after",
    ),
    "KnowledgeSnapshot": (
        "schema_version",
        "kind",
        "source_version",
        "captured_at_utc",
        "captured_monotonic_ns",
        "owners",
        "active_models",
        "snapshot_id",
        "consistency",
        "attempts",
        "warnings",
    ),
    "ContextPlanStepRef": (
        "schema_version",
        "kind",
        "channel",
        "ranking_name",
        "reason",
        "candidate_limit",
        "required",
    ),
    "ContextPlanRef": (
        "schema_version",
        "kind",
        "plan_id",
        "normalized_query",
        "retrieval_mode",
        "intents",
        "exact_terms",
        "source_kinds",
        "formats",
        "include_history",
        "limit",
        "max_per_resource",
        "min_section_distance",
        "max_vectors",
        "steps",
        "project",
        "date_from",
        "date_to",
        "notices",
    ),
    "ContextGraphBudget": (
        "identifiers_considered",
        "entities_included",
        "relations_included",
        "omitted_identifiers",
        "omitted_entities",
        "omitted_relations",
        "identifier_limit_per_evidence",
        "measurement_scope",
    ),
    "ContextBudget": (
        "character_limit",
        "characters_used",
        "estimated_tokens",
        "estimator_signature",
        "omitted_candidates",
        "measurement_scope",
    ),
    "ContextEntityRef": (
        "schema_version",
        "kind",
        "entity_id",
        "entity_kind",
        "label",
        "evidence_ids",
        "resource_ids",
    ),
    "ContextContradictionRef": (
        "schema_version",
        "kind",
        "contradiction_id",
        "contradiction_kind",
        "topic",
        "values",
        "summary",
        "citation_ids",
    ),
    "ContextRelationRef": (
        "schema_version",
        "kind",
        "relation_id",
        "source_entity_id",
        "target_entity_id",
        "relation_kind",
        "method",
        "provenance",
        "evidence_ids",
        "confidence",
    ),
    "ContextBundle": (
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
    ),
}

EXPECTED_OWNER_IDENTITY_KEYS = (
    "owner",
    "state",
    "expected_schema_version",
    "publications",
    "watermarks",
    "observed_schema_version",
)


def test_all_twenty_one_payload_builders_preserve_insertion_order() -> None:
    instances = make_contract_instances()
    serializable_names = tuple(
        name for name, value in instances.items() if hasattr(value, "to_dict")
    )

    assert len(EXPECTED_TO_DICT_KEYS) == 21
    assert serializable_names == tuple(EXPECTED_TO_DICT_KEYS)
    for name, expected_keys in EXPECTED_TO_DICT_KEYS.items():
        payload = instances[name].to_dict()  # type: ignore[attr-defined]
        assert tuple(payload) == expected_keys, name

    owner = instances["OwnerSnapshot"]
    assert tuple(owner.identity_dict()) == EXPECTED_OWNER_IDENTITY_KEYS  # type: ignore[attr-defined]


OPTIONAL_KEY_UNIVERSES = {
    "KnowledgePhaseTiming": ("owner", "ranking_names", "snapshot_id"),
    "ResourceRef": (
        "physical_identity",
        "current_path",
        "disposition",
        "canonical_resource_id",
    ),
    "RevisionRef": ("generation", "observed_at_utc"),
    "EvidenceRef": (
        "page",
        "start_line",
        "end_line",
        "sheet",
        "cell_range",
        "start_ms",
        "end_ms",
        "coordinate_space",
        "start_char",
        "end_char",
        "symbol",
        "section_kind",
        "section_id",
        "snippet",
        "extractor",
        "extractor_version",
        "generation",
        "bounding_box",
        "identifiers",
    ),
    "RankingSignal": (
        "model_signature",
        "query_model_signature",
        "generation",
        "contribution",
    ),
    "KnowledgeHit": ("confidence", "warnings"),
    "PublicationHead": ("model_signature",),
    "OwnerSnapshot": (
        "observed_schema_version",
        "error_code",
        "identity_changed",
        "data_version_before",
        "data_version_after",
        "warning",
    ),
    "OwnerSnapshot.identity_dict": (
        "observed_schema_version",
        "error_code",
        "identity_changed",
    ),
    "KnowledgeSnapshot": ("changed_owners", "warnings"),
    "ContextPlanRef": ("project", "date_from", "date_to", "notices"),
    "ContextBudget": ("truncated_evidence_ids",),
    "ContextRelationRef": ("confidence",),
    "ContextBundle": (
        "contradictions",
        "missing_information",
        "warnings",
        "telemetry",
        "blocking_owners",
    ),
}

EXPECTED_OPTIONAL_PRESENCE = {
    "phase:minimal": ("KnowledgePhaseTiming", ()),
    "phase:owner": ("KnowledgePhaseTiming", ("owner", "ranking_names")),
    "phase:snapshot": ("KnowledgePhaseTiming", ("snapshot_id",)),
    "resource:minimal": ("ResourceRef", ()),
    "resource:full": (
        "ResourceRef",
        ("physical_identity", "current_path", "disposition"),
    ),
    "resource:duplicate": (
        "ResourceRef",
        ("disposition", "canonical_resource_id"),
    ),
    "revision:minimal": ("RevisionRef", ()),
    "revision:full": ("RevisionRef", ("generation", "observed_at_utc")),
    "evidence:minimal": ("EvidenceRef", ()),
    "evidence:full": ("EvidenceRef", OPTIONAL_KEY_UNIVERSES["EvidenceRef"]),
    "ranking:minimal": ("RankingSignal", ()),
    "ranking:full": ("RankingSignal", OPTIONAL_KEY_UNIVERSES["RankingSignal"]),
    "hit:minimal": ("KnowledgeHit", ()),
    "hit:full": ("KnowledgeHit", ("confidence", "warnings")),
    "publication:minimal": ("PublicationHead", ()),
    "publication:full": ("PublicationHead", ("model_signature",)),
    "owner:minimal": ("OwnerSnapshot", ()),
    "owner:full": (
        "OwnerSnapshot",
        (
            "observed_schema_version",
            "data_version_before",
            "data_version_after",
        ),
    ),
    "owner:enriched": (
        "OwnerSnapshot",
        OPTIONAL_KEY_UNIVERSES["OwnerSnapshot"],
    ),
    "owner-identity:minimal": ("OwnerSnapshot.identity_dict", ()),
    "owner-identity:full": (
        "OwnerSnapshot.identity_dict",
        ("observed_schema_version",),
    ),
    "owner-identity:enriched": (
        "OwnerSnapshot.identity_dict",
        OPTIONAL_KEY_UNIVERSES["OwnerSnapshot.identity_dict"],
    ),
    "snapshot:minimal": ("KnowledgeSnapshot", ()),
    "snapshot:full": ("KnowledgeSnapshot", ("warnings",)),
    "snapshot:changed": ("KnowledgeSnapshot", ("changed_owners",)),
    "plan:minimal": ("ContextPlanRef", ()),
    "plan:full": ("ContextPlanRef", OPTIONAL_KEY_UNIVERSES["ContextPlanRef"]),
    "budget:minimal": ("ContextBudget", ()),
    "budget:truncated": ("ContextBudget", ("truncated_evidence_ids",)),
    "relation:minimal": ("ContextRelationRef", ()),
    "relation:full": ("ContextRelationRef", ("confidence",)),
    "bundle:minimal": ("ContextBundle", ()),
    "bundle:full": ("ContextBundle", OPTIONAL_KEY_UNIVERSES["ContextBundle"]),
}


def _optional_payload_variants() -> dict[str, dict[str, object]]:
    values = make_contract_instances()
    phase = values["KnowledgePhaseTiming"]
    resource = values["ResourceRef"]
    revision = values["RevisionRef"]
    evidence = values["EvidenceRef"]
    ranking = values["RankingSignal"]
    hit = values["KnowledgeHit"]
    publication = values["PublicationHead"]
    owner = values["OwnerSnapshot"]
    snapshot = values["KnowledgeSnapshot"]
    plan = values["ContextPlanRef"]
    budget = values["ContextBudget"]
    relation = values["ContextRelationRef"]
    bundle = values["ContextBundle"]

    minimal_owner = contracts.OwnerSnapshot(
        "absent-owner",
        contracts.OwnerAvailability.ABSENT,
        1,
    )
    enriched_owner = replace(
        owner,
        error_code="fixture_error",
        identity_changed=True,
        warning="fixture warning",
    )
    changed_owner = replace(owner, identity_changed=True)
    changed_snapshot = replace(
        snapshot,
        owners=(changed_owner,),
        consistency=contracts.SnapshotConsistency.SNAPSHOT_CHANGED,
        attempts=2,
        warnings=(),
    )

    return {
        "phase:minimal": phase.to_dict(),  # type: ignore[attr-defined]
        "phase:owner": contracts.KnowledgePhaseTiming(
            contracts.KnowledgeTimingPhase.OWNER_RANKING,
            1,
            service_attempt=1,
            owner="semantic",
            ranking_names=("semantic_text",),
        ).to_dict(),
        "phase:snapshot": contracts.KnowledgePhaseTiming(
            contracts.KnowledgeTimingPhase.SNAPSHOT_BEFORE,
            1,
            service_attempt=1,
            snapshot_id="snapshot:fixture",
        ).to_dict(),
        "resource:minimal": contracts.ResourceRef(
            "resource:minimal",
            "document",
            "document_catalog",
        ).to_dict(),
        "resource:full": resource.to_dict(),  # type: ignore[attr-defined]
        "resource:duplicate": contracts.ResourceRef(
            "resource:duplicate",
            "document",
            "document_catalog",
            disposition=contracts.ResourceDisposition.DUPLICATE,
            canonical_resource_id="resource:canonical",
        ).to_dict(),
        "revision:minimal": replace(
            revision,
            generation=None,
            observed_at_utc=None,
        ).to_dict(),
        "revision:full": revision.to_dict(),  # type: ignore[attr-defined]
        "evidence:minimal": contracts.EvidenceRef(
            "evidence:minimal",
            "resource:minimal",
            "revision:minimal",
            contracts.EvidenceMethod.EXTRACTED,
        ).to_dict(),
        "evidence:full": evidence.to_dict(),  # type: ignore[attr-defined]
        "ranking:minimal": contracts.RankingSignal(
            "lexical",
            "bm25",
            1.0,
            1,
        ).to_dict(),
        "ranking:full": ranking.to_dict(),  # type: ignore[attr-defined]
        "hit:minimal": replace(hit, confidence=None, warnings=()).to_dict(),
        "hit:full": hit.to_dict(),  # type: ignore[attr-defined]
        "publication:minimal": replace(
            publication,
            model_signature=None,
        ).to_dict(),
        "publication:full": publication.to_dict(),  # type: ignore[attr-defined]
        "owner:minimal": minimal_owner.to_dict(),
        "owner:full": owner.to_dict(),  # type: ignore[attr-defined]
        "owner:enriched": enriched_owner.to_dict(),
        "owner-identity:minimal": minimal_owner.identity_dict(),
        "owner-identity:full": owner.identity_dict(),  # type: ignore[attr-defined]
        "owner-identity:enriched": enriched_owner.identity_dict(),
        "snapshot:minimal": replace(snapshot, warnings=()).to_dict(),
        "snapshot:full": snapshot.to_dict(),  # type: ignore[attr-defined]
        "snapshot:changed": changed_snapshot.to_dict(),
        "plan:minimal": replace(
            plan,
            project=None,
            date_from=None,
            date_to=None,
            notices=(),
        ).to_dict(),
        "plan:full": plan.to_dict(),  # type: ignore[attr-defined]
        "budget:minimal": budget.to_dict(),  # type: ignore[attr-defined]
        "budget:truncated": replace(
            budget,
            truncated_evidence_ids=("evidence:truncated",),
        ).to_dict(),
        "relation:minimal": replace(relation, confidence=None).to_dict(),
        "relation:full": relation.to_dict(),  # type: ignore[attr-defined]
        "bundle:minimal": replace(
            bundle,
            contradictions=(),
            missing_information=(),
            warnings=(),
            telemetry=None,
            blocking_owners=(),
        ).to_dict(),
        "bundle:full": replace(
            bundle,
            blocking_owners=("semantic",),
        ).to_dict(),
    }


def test_optional_payload_presence_and_omission_matrix_is_complete() -> None:
    payloads = _optional_payload_variants()
    coverage = {
        (contract_name, key): {"present": False, "absent": False}
        for contract_name, keys in OPTIONAL_KEY_UNIVERSES.items()
        for key in keys
    }

    assert tuple(payloads) == tuple(EXPECTED_OPTIONAL_PRESENCE)
    for case, (contract_name, expected_present) in EXPECTED_OPTIONAL_PRESENCE.items():
        payload = payloads[case]
        universe = OPTIONAL_KEY_UNIVERSES[contract_name]
        actual_present = tuple(key for key in payload if key in universe)
        assert actual_present == expected_present, case
        for key in universe:
            state = "present" if key in payload else "absent"
            coverage[(contract_name, key)][state] = True

    assert all(all(states.values()) for states in coverage.values())


MULTI_ERROR_PRECEDENCE = (
    ("phase:type", "Knowledge timing phase is invalid"),
    ("phase:duration", "Knowledge timing duration_ns cannot be negative"),
    ("phase:owner", "Knowledge timing owner cannot be blank when present"),
    ("phase:rankings", "Knowledge timing ranking names must be unique"),
    (
        "phase:scope",
        "owner_ranking timing requires owner and ranking names",
    ),
    ("evidence:required", "evidence_id cannot be blank"),
    ("evidence:optional", "sheet cannot be blank when present"),
    ("evidence:page", "page cannot be negative"),
    ("evidence:line", "line locator is invalid"),
    ("evidence:time", "time locator is invalid"),
    ("evidence:character", "character locator is invalid"),
    ("evidence:bounding", "bounding box is invalid"),
    ("evidence:snippet", "snippet cannot exceed 4096 characters"),
    (
        "evidence:identifier-count",
        "evidence cannot contain more than 64 identifiers",
    ),
    ("snapshot:required", "source_version cannot be blank"),
    (
        "snapshot:utc",
        "captured_at_utc must be an explicit UTC timestamp",
    ),
    (
        "snapshot:monotonic",
        "captured monotonic time cannot be negative",
    ),
    ("snapshot:attempts", "snapshot attempts must be one or two"),
    ("snapshot:owners", "snapshot owners must be unique"),
    (
        "snapshot:consistency",
        "a stable snapshot cannot contain a changed owner",
    ),
    ("plan:id", "context plan plan_id cannot be blank"),
    ("plan:query", "context plan normalized_query cannot be blank"),
    ("plan:mode", "context plan retrieval_mode is invalid"),
    ("plan:intents", "context plan intents must be a tuple"),
    (
        "plan:project",
        "context plan project cannot be blank when present",
    ),
    ("plan:limit", "context plan limit must be between 1 and 1000"),
    ("bundle:query", "normalized query cannot be blank"),
    ("bundle:id", "plan_id cannot be blank"),
    ("bundle:plan", "context plan_id must match the normalized plan"),
    (
        "bundle:evidence",
        "selected hits require unique evidence identifiers for citations",
    ),
    ("bundle:citation", "citation identifiers must be unique"),
    ("bundle:entity", "context entity identifiers must be unique"),
    ("bundle:relation", "logical context relations must be unique"),
    (
        "bundle:graph",
        "context graph identifier count must match selected evidence",
    ),
    (
        "bundle:rendered",
        "context entities must be rendered inside the character budget",
    ),
    (
        "bundle:contradiction",
        "context contradiction identifiers must be unique",
    ),
    ("bundle:notice", "context notice cannot be blank"),
    (
        "bundle:characters",
        "rendered context and budget character count disagree",
    ),
)


def _trigger_phase_precedence(case: str) -> None:
    values: dict[str, object] = {
        "phase": contracts.KnowledgeTimingPhase.PLANNER,
        "duration_ns": 1,
    }
    overrides = {
        "type": {"phase": "planner", "duration_ns": -1},
        "duration": {"duration_ns": -1, "service_attempt": 3},
        "owner": {"owner": " ", "ranking_names": ["rank"]},
        "rankings": {
            "ranking_names": ("rank", "rank"),
            "service_attempt": 1,
        },
        "scope": {
            "phase": contracts.KnowledgeTimingPhase.OWNER_RANKING,
            "service_attempt": 1,
            "snapshot_id": "snapshot:fixture",
        },
    }
    values.update(overrides[case])
    contracts.KnowledgePhaseTiming(**values)  # type: ignore[arg-type]


def _trigger_evidence_precedence(case: str) -> None:
    values: dict[str, object] = {
        "evidence_id": "evidence:fixture",
        "resource_id": "resource:fixture",
        "revision_id": "revision:fixture",
        "method": contracts.EvidenceMethod.EXTRACTED,
    }
    too_many_identifiers = tuple(("serial", str(index)) for index in range(65))
    overrides = {
        "required": {"evidence_id": " ", "sheet": " ", "page": -1},
        "optional": {"sheet": " ", "page": -1},
        "page": {"page": -1, "start_line": 1},
        "line": {
            "start_line": 2,
            "end_line": 1,
            "start_ms": 1,
        },
        "time": {
            "start_ms": 2,
            "end_ms": 1,
            "start_char": 1,
        },
        "character": {
            "start_char": 2,
            "end_char": 1,
            "bounding_box": (0.0, 0.0, float("nan"), 1.0),
        },
        "bounding": {
            "bounding_box": (0.0, 0.0, float("nan"), 1.0),
            "coordinate_space": "normalized-page-v1",
            "snippet": "x" * 4_097,
        },
        "snippet": {
            "snippet": "x" * 4_097,
            "identifiers": (("serial", "Q52"), ("serial", "Q52")),
        },
        "identifier-count": {
            "identifiers": (*too_many_identifiers, too_many_identifiers[-1]),
        },
    }
    values.update(overrides[case])
    contracts.EvidenceRef(**values)  # type: ignore[arg-type]


def _trigger_snapshot_precedence(case: str) -> None:
    snapshot = make_contract_instances()["KnowledgeSnapshot"]
    owner = snapshot.owners[0]  # type: ignore[attr-defined]
    model = snapshot.active_models[0]  # type: ignore[attr-defined]

    if case == "required":
        replace(snapshot, source_version=" ", captured_at_utc="invalid")
    elif case == "utc":
        replace(snapshot, captured_at_utc="invalid", captured_monotonic_ns=-1)
    elif case == "monotonic":
        replace(snapshot, captured_monotonic_ns=-1, attempts=3)
    elif case == "attempts":
        replace(snapshot, attempts=3, owners=(owner, owner))
    elif case == "owners":
        replace(
            snapshot,
            owners=(owner, owner),
            active_models=(model, model),
        )
    elif case == "consistency":
        replace(
            snapshot,
            owners=(replace(owner, identity_changed=True),),
            active_models=(replace(model, generation=99),),
        )
    else:
        raise AssertionError(f"unknown snapshot precedence case: {case}")


def _trigger_plan_precedence(case: str) -> None:
    plan = make_contract_instances()["ContextPlanRef"]
    if case == "id":
        replace(plan, plan_id=" ", normalized_query=" ")
    elif case == "query":
        replace(plan, normalized_query=" ", retrieval_mode="invalid")
    elif case == "mode":
        replace(plan, retrieval_mode="invalid", intents=["exact"])
    elif case == "intents":
        replace(plan, intents=["exact"], project=" ")
    elif case == "project":
        replace(plan, project=" ", limit=0)
    elif case == "limit":
        replace(plan, limit=0, steps=[])
    else:
        raise AssertionError(f"unknown plan precedence case: {case}")


def _remove_rendered_item(
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


def _trigger_bundle_precedence(case: str) -> None:
    bundle = make_contract_instances()["ContextBundle"]
    first_hit, second_hit = bundle.selected_hits  # type: ignore[attr-defined]
    first_entity, _second_entity = bundle.entities  # type: ignore[attr-defined]
    relation = bundle.relations[0]  # type: ignore[attr-defined]
    contradiction = bundle.contradictions[0]  # type: ignore[attr-defined]

    if case == "query":
        replace(bundle, normalized_query=" ", plan_id=" ")
    elif case == "id":
        replace(bundle, plan_id=" ", selected_hits=(first_hit, first_hit))
    elif case == "plan":
        replace(
            bundle,
            plan_id="knowledge-plan-v2:other",
            selected_hits=(first_hit, replace(first_hit, rank=2)),
        )
    elif case == "evidence":
        replace(
            bundle,
            selected_hits=(first_hit, replace(first_hit, rank=2)),
            citation_ids=(
                ("K1", first_hit.evidence.evidence_id),
                ("K1", second_hit.evidence.evidence_id),
            ),
        )
    elif case == "citation":
        replace(
            bundle,
            citation_ids=(
                ("K1", first_hit.evidence.evidence_id),
                ("K1", second_hit.evidence.evidence_id),
            ),
            entities=(first_entity, first_entity),
        )
    elif case == "entity":
        replace(
            bundle,
            entities=(first_entity, first_entity),
            relations=(relation, relation),
        )
    elif case == "relation":
        replace(
            bundle,
            relations=(relation, replace(relation, relation_id="relation:other")),
            graph_budget=replace(bundle.graph_budget, relations_included=99),
        )
    elif case == "graph":
        replace(
            bundle,
            graph_budget=replace(bundle.graph_budget, identifiers_considered=99),
            rendered_context="",
            budget=replace(bundle.budget, characters_used=0),
        )
    elif case == "rendered":
        candidate = _remove_rendered_item(bundle, first_entity.to_json())
        replace(
            candidate,
            contradictions=(contradiction, contradiction),
        )
    elif case == "contradiction":
        replace(
            bundle,
            contradictions=(contradiction, contradiction),
            missing_information=(" ",),
        )
    elif case == "notice":
        replace(
            bundle,
            missing_information=(" ",),
            budget=replace(
                bundle.budget,
                characters_used=bundle.budget.characters_used + 1,
            ),
        )
    elif case == "characters":
        replace(
            bundle,
            budget=replace(
                bundle.budget,
                characters_used=bundle.budget.characters_used + 1,
            ),
            telemetry="invalid",
        )
    else:
        raise AssertionError(f"unknown bundle precedence case: {case}")


def _trigger_multi_error(case: str) -> None:
    domain, _, detail = case.partition(":")
    if domain == "phase":
        _trigger_phase_precedence(detail)
    elif domain == "evidence":
        _trigger_evidence_precedence(detail)
    elif domain == "snapshot":
        _trigger_snapshot_precedence(detail)
    elif domain == "plan":
        _trigger_plan_precedence(detail)
    elif domain == "bundle":
        _trigger_bundle_precedence(detail)
    else:
        raise AssertionError(f"unknown precedence domain: {domain}")


@pytest.mark.parametrize(("case", "message"), MULTI_ERROR_PRECEDENCE)
def test_multi_error_validation_precedence_is_exact(
    case: str,
    message: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        _trigger_multi_error(case)
    assert str(captured.value) == message


def test_required_text_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contracts._required_text
    calls: list[tuple[str, str]] = []

    def spy(name: str, value: str) -> str:
        calls.append((name, value))
        return original(name, value)

    monkeypatch.setattr(contracts, "_required_text", spy)
    contracts.PhysicalIdentityRef("windows_file_id", "volume:file", 1)

    assert calls == [
        ("physical identity scheme", "windows_file_id"),
        ("physical identity value", "volume:file"),
    ]


def test_optional_text_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contracts._optional_text
    calls: list[tuple[str, str | None]] = []

    def spy(name: str, value: str | None) -> str | None:
        calls.append((name, value))
        return original(name, value)

    monkeypatch.setattr(contracts, "_optional_text", spy)
    contracts.ResourceRef("resource:fixture", "document", "document_catalog")

    assert calls == [
        ("current_path", None),
        ("canonical_resource_id", None),
    ]


def test_base_payload_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contracts._base_payload
    calls: list[str] = []

    def spy(kind: str) -> dict[str, object]:
        calls.append(kind)
        payload = original(kind)
        payload["late_binding_marker"] = True
        return payload

    monkeypatch.setattr(contracts, "_base_payload", spy)
    payload = contracts.ResourceRef(
        "resource:fixture",
        "document",
        "document_catalog",
    ).to_dict()

    assert calls == ["resource_ref"]
    assert payload["late_binding_marker"] is True


def test_canonical_output_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def spy(payload: dict[str, object]) -> str:
        calls.append(payload)
        return f"late:{payload['kind']}"

    monkeypatch.setattr(contracts, "_canonical_output", spy)
    entity = contracts.ContextEntityRef(
        "entity:fixture",
        "fixture",
        "Fixture",
        ("evidence:fixture",),
        ("resource:fixture",),
    )

    assert entity.to_json() == "late:context_entity_ref"
    assert calls == [entity.to_dict()]


def test_context_plan_values_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_contract_instances()["ContextPlanRef"]
    original = contracts._validate_context_plan_values
    calls: list[tuple[str, tuple[str, ...]]] = []

    def spy(name: str, values: tuple[str, ...]) -> None:
        calls.append((name, values))
        original(name, values)

    monkeypatch.setattr(contracts, "_validate_context_plan_values", spy)
    replace(plan, notices=plan.notices)  # type: ignore[attr-defined]

    assert tuple(name for name, _values in calls) == (
        "intents",
        "exact_terms",
        "source_kinds",
        "formats",
        "notices",
    )


def test_context_references_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contracts._validate_context_references
    calls: list[tuple[str, tuple[str, ...]]] = []

    def spy(name: str, references: tuple[str, ...]) -> None:
        calls.append((name, references))
        original(name, references)

    monkeypatch.setattr(contracts, "_validate_context_references", spy)
    contracts.ContextEntityRef(
        "entity:fixture",
        "fixture",
        "Fixture",
        ("evidence:fixture",),
        ("resource:fixture",),
    )

    assert calls == [
        ("entity evidence", ("evidence:fixture",)),
        ("entity resource", ("resource:fixture",)),
    ]


def test_stable_id_seam_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_type = contracts.ContextContradictionRef
    original = contract_type._stable_id
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def spy(
        contradiction_kind: str,
        topic: str,
        values: tuple[str, ...],
    ) -> str:
        calls.append((contradiction_kind, topic, values))
        return original(contradiction_kind, topic, values)

    monkeypatch.setattr(contract_type, "_stable_id", staticmethod(spy))
    created = contract_type.create(
        contradiction_kind="conflicting_structured_claim",
        topic="breaker_state",
        values=("open", "closed"),
        citation_ids=("K1", "K2"),
    )

    expected_call = (
        "conflicting_structured_claim",
        "breaker_state",
        ("closed", "open"),
    )
    assert calls == [expected_call, expected_call]
    assert created.contradiction_id == original(*expected_call)


# endregion [02]
