"""Contract tests for the offline Fast Curation Semantic benchmark."""

from __future__ import annotations

from pathlib import Path

from neocortex.semantic.semantic_config import compact_multilingual_text_model
from neocortex.semantic.semantic_models import BackendEmbedding
from neocortex.semantic.fast_curation_prototypes import prototype_set_from_records
from tools.benchmarks.fast_curation_benchmark import (
    CalibrationPolicy,
    ScoreRecord,
    apply_policy,
    benchmark_cascade,
    calibrate_policy,
    document_representation,
    embed_records,
    load_fixture,
    metrics,
)


FIXTURE = Path(__file__).parent / "fixtures/curation_semantic_holdout.json"


class _FakeBackend:
    model = compact_multilingual_text_model()
    max_batch_size = 64

    def embed(self, requests):
        outputs = []
        for request in requests:
            value = float((sum(request.text.encode("utf-8")) % 17) + 1)
            outputs.append(
                BackendEmbedding(
                    request_id=request.request_id,
                    vector=(value,) + (0.0,) * (self.model.dimensions - 1),
                    provenance={"backend": "fixture"},
                )
            )
        return tuple(outputs)


def test_fixture_has_disjoint_prototype_validation_and_holdout_splits() -> None:
    fixture = load_fixture(FIXTURE)
    records = fixture["records"]
    assert len(records) > 100
    assert {record["split"] for record in records} == {
        "prototype",
        "validation",
        "heldout",
        "fresh_heldout",
    }
    assert sum(record["split"] == "prototype" for record in records) == 64
    assert sum(record["split"] == "validation" for record in records) == 36
    assert sum(record["split"] == "heldout" for record in records) == 36
    assert sum(record["split"] == "fresh_heldout" for record in records) == 120
    assert {record["case"] for record in records if record.get("case")} == {"A", "B", "C", "D"}
    ids_by_split = {
        split: {record["id"] for record in records if record["split"] == split}
        for split in ("prototype", "validation", "heldout", "fresh_heldout")
    }
    assert not ids_by_split["prototype"] & ids_by_split["validation"]
    assert not ids_by_split["prototype"] & ids_by_split["heldout"]
    assert any(record["language"] == "es" for record in records)
    assert any(record["language"] == "en" for record in records)
    prototype_set = prototype_set_from_records(fixture["prototype_manifest"])
    assert len(prototype_set.prototypes) == len(fixture["prototype_manifest"])
    assert {item.family for item in prototype_set.prototypes} == {
        "document_kind",
        "topic",
        "activity",
    }


def test_document_representation_is_bounded_and_keeps_structural_context() -> None:
    fixture = load_fixture(FIXTURE)
    record = fixture["records"][0]
    representation = document_representation(record, max_chars=256)
    assert len(representation) <= 256
    assert representation
    assert record["path_context"] not in representation
    assert "DOCUMENT_KIND:" not in representation
    assert "TOPIC:" not in representation
    assert "ACTIVITY:" not in representation


def test_fake_backend_uses_requested_batch_bound_and_one_vector_per_record() -> None:
    fixture = load_fixture(FIXTURE)
    records = fixture["records"][:17]
    run = embed_records(_FakeBackend(), records, batch_size=16)
    assert run.embedding_count == len(records)
    assert len(run.batch_seconds) == 2
    assert run.throughput > 0
    assert all(len(vector) == 384 for vector in run.vectors.values())


def test_calibration_uses_validation_rows_only_and_metrics_count_false_moves() -> None:
    validation = (
        ScoreRecord("v1", "a", "classify", "a", "technical", 0.90, 0.30),
        ScoreRecord("v2", "b", "classify", "a", "technical", 0.60, 0.02),
    )
    policy = calibrate_policy(validation, {"a": "technical", "b": "admin"})
    assert policy.calibration_count == 2
    assert policy.thresholds["technical"][1] >= 0.02
    heldout = (
        ScoreRecord("h1", None, "abstain", "a", "technical", 0.99, 0.99),
        ScoreRecord("h2", "a", "classify", "a", "technical", 0.91, 0.31),
    )
    decided = apply_policy(heldout, policy, {"a": "technical"})
    result = metrics(decided)
    assert result["false_automatic_moves"] == 1
    assert result["confusion_pairs"]["abstain->a"] == 1


def test_structural_contradiction_veto_abstains_without_a_second_classifier() -> None:
    row = ScoreRecord("invoice-1", "a", "classify", "a", "technical", 0.95, 0.40)
    policy = CalibrationPolicy({"technical": (0.80, 0.20)}, 0.99, 1)
    decided = apply_policy(
        (row,),
        policy,
        {"a": "technical"},
        records=({"id": "invoice-1", "route_structure": {"kind": "invoice"}},),
        label_metadata={"a": {"nature": "report"}},
        structural_veto=True,
    )
    assert decided[0].predicted is None
    assert decided[0].family == "structural_veto"


def test_cascade_uses_quality_model_only_after_compact_abstention() -> None:
    def row(record_id: str, truth: str | None, predicted: str | None) -> dict[str, object]:
        return {
            "record_id": record_id,
            "truth": truth,
            "expected_disposition": "classify" if truth else "abstain",
            "predicted": predicted,
            "family": "technical",
            "score": 0.9,
            "margin": 0.3,
        }

    minilm = {"status": "complete", "_heldout_rows": [row("1", "a", "a"), row("2", "b", None)]}
    jina = {"status": "complete", "_heldout_rows": [row("1", "a", "a"), row("2", "b", "b")]}
    result = benchmark_cascade(minilm, jina)
    assert result["status"] == "complete"
    assert result["stage_one_accepts"] == 1
    assert result["escalated_to_stage_two"] == 1
    assert result["stage_two_accepts"] == 1
    assert result["metrics"]["precision"] == 1.0
