"""Generated navigation metadata is a resource prior, never original-row evidence."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge import knowledge_search_content as content
from neocortex.knowledge.knowledge_planner import RetrievalStep
from neocortex.semantic.semantic_service_contracts import SemanticRanking
from neocortex.semantic.semantic_tabular_projection import TABULAR_METADATA_POLICY
from tests.test_knowledge_search_content_extraction_contract import _resolved


def _navigation():
    return replace(
        _resolved(source_kind="text"),
        section_kind="text_metadata_navigation", section_id=TABULAR_METADATA_POLICY,
        snippet="Resumen de navegación; muestras no exhaustivas.",
        section_provenance={
            "policy_signature": TABULAR_METADATA_POLICY,
            "basis": "parsed_identifier_heavy_table", "advisory_only": True,
        },
    )


def test_navigation_cannot_be_materialized_as_evidence() -> None:
    with pytest.raises(ValueError, match="advisory"):
        knowledge_search._candidate_from_resolved(
            _navigation(), ranking_name="semantic_text", source_rank=1, producer="semantic-v6",
        )


def test_navigation_has_typed_resource_discovery_signal() -> None:
    signal = knowledge_search._resource_discovery_signal_from_resolved(
        _navigation(), ranking_name="semantic_navigation", source_rank=1,
        producer="semantic-v6", fusion_weight=0.5,
    )
    assert "advisory_metadata_only" in signal.warnings
    assert "original rows" in signal.reason
    assert not hasattr(signal, "evidence")
    with pytest.raises(ValueError, match="provenance"):
        knowledge_search._resource_discovery_signal_from_resolved(
            replace(_navigation(), section_provenance={"advisory_only": True}),
            ranking_name="semantic_navigation", source_rank=1, producer="semantic-v6", fusion_weight=0.5,
        )


def test_semantic_body_conversion_filters_navigation_without_querying_again() -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("navigation was promoted into substantive evidence")

    hit = _navigation()
    ranking = SemanticRanking(name="semantic_text", hits=(hit.hit,), resolved=(hit,), scanned=1, complete=True)
    step = RetrievalStep("semantic", "semantic_text", "fixture", 5, True)
    assert content._materialize_semantic_candidates(SimpleNamespace(materialize_candidate=forbidden), step, ranking) == ()
