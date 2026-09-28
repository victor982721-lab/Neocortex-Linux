"""C11 visual retrieval readiness lifecycle on private synthetic state."""

from __future__ import annotations

import json
from pathlib import Path

from neocortex.semantic import image_retrieval_calibration as calibration_module
from neocortex.semantic.image_retrieval_calibration import (
    ImageCalibrationEvidence,
    image_retrieval_readiness,
    persist_image_retrieval_calibration,
)
from neocortex.semantic.semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    clip_image_model,
    clip_text_model,
)
from neocortex.semantic.semantic_schema import initialize_semantic_state, semantic_database
from neocortex.semantic.semantic_service_contracts import ImageRetrievalCalibration


TEST_CAPABILITIES = ("base", "inference")


def _bound_calibration(sample_ids: tuple[str, ...]) -> ImageRetrievalCalibration:
    return ImageRetrievalCalibration(
        calibration_signature="image-retrieval-calibration-v1:sha256:synthetic",
        query_model_signature=clip_text_model().model_signature,
        indexed_model_signature=clip_image_model().model_signature,
        pipeline=SEMANTIC_PIPELINE_VERSION,
        backend="fastembed",
        minimum_score=0.5,
        positive_queries=3,
        negative_queries=3,
        sample_items=len(sample_ids),
        indexed_processing_signature="synthetic-image-processing-v1",
    )


def _evidence(sample_ids: tuple[str, ...]) -> ImageCalibrationEvidence:
    return ImageCalibrationEvidence(
        dataset_digest="synthetic-dataset-digest",
        sample_item_ids=sample_ids,
        positive_floor=0.8,
        negative_ceiling=0.2,
        generated_ns=10,
        generation_id=7,
    )


def test_factory_reset_and_generation_change_require_recalibration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    sample_ids = tuple(f"item:image:{index}" for index in range(20))
    monkeypatch.setattr(
        calibration_module,
        "_published_image_contract",
        lambda _database: (7, "synthetic-image-processing-v1"),
    )
    monkeypatch.setattr(
        calibration_module,
        "_active_image_item_ids",
        lambda _database: sample_ids,
    )

    before = image_retrieval_readiness(database)
    assert before.status == "requires_calibration"
    assert before.reason == "image_retrieval_not_calibrated"
    assert before.action == "run_semantic_image_calibrate_with_labelled_fixture"

    persist_image_retrieval_calibration(
        database,
        _bound_calibration(sample_ids),
        _evidence(sample_ids),
    )
    ready = image_retrieval_readiness(database)
    assert ready.status == "ready"
    assert ready.generation_id == 7
    assert isinstance(ready.calibration_signature, str)
    assert ready.calibration_signature.startswith("image-retrieval-calibration")

    monkeypatch.setattr(
        calibration_module,
        "_published_image_contract",
        lambda _database: (8, "synthetic-image-processing-v2"),
    )
    stale = image_retrieval_readiness(database)
    assert stale.status == "stale"
    assert stale.reason == "image_calibration_contract_mismatch"
    assert stale.action == "rerun_semantic_image_calibrate"

    # A factory reset removes the operational metadata; after a fresh image
    # publication, status is a clear recalibration requirement rather than a
    # silent reuse of an old floor.
    with semantic_database(database) as connection:
        connection.execute(
            "DELETE FROM metadata WHERE key=?",
            (calibration_module.CALIBRATION_METADATA_KEY,),
        )
    monkeypatch.setattr(
        calibration_module,
        "_published_image_contract",
        lambda _database: (8, "synthetic-image-processing-v2"),
    )
    reset = image_retrieval_readiness(database)
    assert reset.status == "requires_calibration"
    assert reset.reason == "image_retrieval_not_calibrated"


def test_unbound_legacy_record_is_not_reported_ready(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    sample_ids = tuple(f"item:image:{index}" for index in range(20))
    monkeypatch.setattr(
        calibration_module,
        "_published_image_contract",
        lambda _database: (7, "synthetic-image-processing-v1"),
    )
    monkeypatch.setattr(
        calibration_module,
        "_active_image_item_ids",
        lambda _database: sample_ids,
    )
    legacy = ImageRetrievalCalibration(
        calibration_signature="legacy",
        query_model_signature="legacy-query",
        indexed_model_signature="legacy-image",
        pipeline="legacy-pipeline",
        backend="legacy-backend",
        minimum_score=0.5,
        positive_queries=3,
        negative_queries=3,
        sample_items=20,
        indexed_processing_signature="synthetic-image-processing-v1",
    )
    persist_image_retrieval_calibration(database, legacy, _evidence(sample_ids))
    # Simulate a pre-readiness v1 record: it has no generation/scope binding.
    with semantic_database(database) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key=?",
            (calibration_module.CALIBRATION_METADATA_KEY,),
        ).fetchone()
        payload = json.loads(str(row[0]))
        payload.pop("indexed_generation_id", None)
        payload.pop("calibration_dataset_digest", None)
        payload.pop("calibration_sample_item_ids", None)
        connection.execute(
            "UPDATE metadata SET value=? WHERE key=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),
             calibration_module.CALIBRATION_METADATA_KEY),
        )
    readiness = image_retrieval_readiness(database)
    assert readiness.status == "requires_calibration"
    assert readiness.reason == "image_calibration_legacy_unbound"
    assert readiness.action == "rerun_semantic_image_calibrate"
