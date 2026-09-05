"""Control cases for publication-only classification and historical evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.semantic.semantic_classification_service import classify_embedding_model
from neocortex.semantic.semantic_models import EmbeddingModelSpec, SemanticEvidence
from neocortex.semantic.semantic_ontology import CONCEPTS, ONTOLOGY_VERSION
from neocortex.semantic.semantic_service_contracts import SEMANTIC_ONTOLOGY_ID
from neocortex.semantic.semantic_state import (
    claim_embedding_jobs,
    enqueue_text_chunk_jobs,
    fail_embedding_job,
    finalize_embedding_generation,
    list_semantic_evidence,
    record_semantic_evidence,
    start_embedding_generation,
)
from tests.test_semantic_service import _ConstantBackend
from tests.test_semantic_state import (
    _complete_text_job,
    _initialize,
    _stage_text_item,
    _text_model,
)

TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


def _classify(database: Path, model: EmbeddingModelSpec) -> tuple[SemanticEvidence, ...]:
    result = classify_embedding_model(
        database,
        indexed_model=model,
        query_backend=_ConstantBackend(model),
        max_evidence_per_entity=1,
        page_size=1,
        concepts_provider=lambda _modality: (CONCEPTS["industrial.equipment.transformer"],),
    )
    assert result.entities_scored == result.evidence_staged == 1
    return list_semantic_evidence(
        database,
        item_id="published",
        ontology_id=SEMANTIC_ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
    )


@pytest.mark.parametrize("partial", (False, True))
def test_classification_reads_only_the_published_generation(
    tmp_path: Path, partial: bool
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "published", "power transformer maintenance")
    published = _complete_text_job(database, model, chunk, processing_signature="published")
    _, candidate_chunk = _stage_text_item(database, "candidate", "unpublished transformer record")
    candidate = start_embedding_generation(
        database, model_signature=model.model_signature, processing_signature="candidate"
    )
    enqueue_text_chunk_jobs(database, candidate, (candidate_chunk.chunk_id,), now_ns=100)
    if partial:
        lease = claim_embedding_jobs(database, candidate, worker_id="fixture", now_ns=101)[0]
        fail_embedding_job(
            database,
            lease.job_id,
            worker_id="fixture",
            error_type="fixture_failure",
            error_message="synthetic model failure",
            retryable=False,
            now_ns=102,
        )
        assert finalize_embedding_generation(database, candidate, allow_partial=True).status == (
            "ready_partial"
        )

    evidence = _classify(database, model)
    assert len(evidence) == 1
    assert evidence[0].generation_id == published


def test_valid_historical_evidence_survives_a_successor_head(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "published", "power transformer maintenance")
    original = _complete_text_job(database, model, chunk, processing_signature="original")
    evidence = _classify(database, model)
    assert len(evidence) == 1
    assert evidence[0].generation_id == original
    successor = start_embedding_generation(
        database, model_signature=model.model_signature, processing_signature="successor"
    )
    assert finalize_embedding_generation(database, successor).status == "ready"
    assert successor != original

    # Provenance identifies the generation actually scored, not today's head.
    record_semantic_evidence(database, evidence[0], refresh_token="historical-score")
    assert list_semantic_evidence(
        database,
        item_id="published",
        ontology_id=evidence[0].ontology_id,
        ontology_version=evidence[0].ontology_version,
    ) == evidence
