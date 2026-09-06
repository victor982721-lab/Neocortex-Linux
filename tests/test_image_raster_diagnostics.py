"""A document-like raster observation is not an automatic human finding."""

from dataclasses import asdict
from types import SimpleNamespace

from neocortex.capabilities.formats.image.diagnostics import project_raster_document_candidate
from neocortex.capabilities.formats.image.models import DocumentCandidate
from neocortex.deduplication import FileSnapshot
from neocortex.capabilities.formats.image.route import (
    _cached_success_review_candidates,
    _success_review_candidates,
    _successful_review_reconciliation,
)

TEST_CAPABILITIES = ("image",)


def test_raster_observation_separates_score_and_severity_without_manual_review() -> None:
    document = DocumentCandidate(True, 0.98, "baja", ("page",), ("geometry",), ("fixture",))
    payload = asdict(document)
    assert payload["score_semantics"] == "uncalibrated_heuristic"
    assert payload["severity"] == "informational" and not payload["human_review_required"]
    decision = SimpleNamespace(
        document_candidate=document, features=SimpleNamespace(decode_quality="complete")
    )
    snapshot = FileSnapshot("/fixture/page.png", 1, 2, 10, 1, -1)
    assert _success_review_candidates(snapshot, decision) == ()
    assert (
        _cached_success_review_candidates(
            {"decode_quality": "complete", "document_candidate": 1}, snapshot
        )
        == ()
    )
    reconciliation = _successful_review_reconciliation(snapshot, (), "detector observation policy")
    assert "image_raster_document_candidate" in reconciliation.evaluated_reason_codes
    assert "image_raster_document_candidate" not in reconciliation.active_reason_codes
    observation = project_raster_document_candidate(
        {"file_key": "fixture", "document_candidate": 1, "document_candidate_score": 0.98}
    )
    assert observation and not observation["human_review_required"]
    assert observation["logical_document_status"] == "unverified"
    assert observation["deletion_allowed"] is False
