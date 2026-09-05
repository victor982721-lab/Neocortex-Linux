"""Durable local image retrieval calibration contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from neocortex.semantic.image_retrieval_calibration import (
    CALIBRATION_SCHEMA,
    ImageCalibrationError,
    ImageCalibrationEvidence,
    load_calibration_dataset,
    load_image_retrieval_calibration,
    measure_image_retrieval_calibration,
    persist_image_retrieval_calibration,
)
from neocortex.semantic.semantic_schema import initialize_semantic_state
from neocortex.semantic.semantic_service_contracts import ImageRetrievalCalibration


def _calibration() -> ImageRetrievalCalibration:
    return ImageRetrievalCalibration(
        calibration_signature="image-retrieval-calibration-v1:sha256=fixture",
        query_model_signature="query-model",
        indexed_model_signature="image-model",
        pipeline="pipeline-v1",
        backend="fastembed",
        minimum_score=0.52,
        positive_queries=4,
        negative_queries=4,
        sample_items=24,
        indexed_processing_signature="image-processing-v1",
    )


def _evidence() -> ImageCalibrationEvidence:
    return ImageCalibrationEvidence(
        dataset_digest="fixture-digest",
        sample_item_ids=tuple(f"item:image:{index}" for index in range(24)),
        positive_floor=0.81,
        negative_ceiling=0.23,
        generated_ns=123,
        generation_id=7,
    )


def test_persist_and_load_image_calibration_is_exact(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    calibration = _calibration()
    persist_image_retrieval_calibration(database, calibration, _evidence())

    loaded = load_image_retrieval_calibration(database)
    assert loaded == calibration
    assert database.read_bytes().startswith(b"SQLite format 3")


def test_loader_abstains_on_missing_or_malformed_metadata(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    assert load_image_retrieval_calibration(database) is None
    from neocortex.semantic.semantic_schema import semantic_database

    with semantic_database(database) as connection:
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            ("image_retrieval_calibration.v1", "not-json"),
        )
    assert load_image_retrieval_calibration(database) is None


def test_persist_rejects_non_durable_calibration(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    calibration = ImageRetrievalCalibration(
        calibration_signature="fixture",
        query_model_signature="query-model",
        indexed_model_signature="image-model",
        pipeline="pipeline-v1",
        backend="fastembed",
        minimum_score=0.5,
        positive_queries=1,
        negative_queries=1,
        sample_items=1,
    )
    with pytest.raises(ImageCalibrationError, match="processing signature"):
        persist_image_retrieval_calibration(database, calibration, _evidence())


def test_calibration_dataset_requires_labelled_positive_queries(tmp_path: Path) -> None:
    dataset = tmp_path / "calibration.json"
    dataset.write_text(
        json.dumps(
            {
                "schema": CALIBRATION_SCHEMA,
                "positive_queries": ["flower"],
                "negative_queries": ["transformer", "document", "cable"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ImageCalibrationError, match="at least 3 queries"):
        load_calibration_dataset(dataset)


def test_measurement_binds_generation_and_separates_labelled_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neocortex.semantic.image_retrieval_calibration as calibration_module
    import neocortex.semantic.semantic_search_service as search_module

    sample_ids = tuple(f"item:image:{index}" for index in range(24))
    dataset = tmp_path / "calibration.json"
    dataset.write_text(
        json.dumps(
            {
                "schema": CALIBRATION_SCHEMA,
                "sample_item_ids": list(sample_ids),
                "positive_queries": [
                    {"query": "positive one", "expected_item_ids": [sample_ids[0]]},
                    {"query": "positive two", "expected_item_ids": [sample_ids[0]]},
                    {"query": "positive three", "expected_item_ids": [sample_ids[0]]},
                ],
                "negative_queries": ["negative one", "negative two", "negative three"],
            }
        ),
        encoding="utf-8",
    )
    scores = {
        "positive one": 0.80,
        "positive two": 0.75,
        "positive three": 0.78,
        "negative one": 0.20,
        "negative two": 0.25,
        "negative three": 0.18,
    }
    monkeypatch.setattr(
        calibration_module,
        "_published_image_contract",
        lambda _database: (7, "image-processing-v1"),
    )
    monkeypatch.setattr(calibration_module, "_active_image_item_ids", lambda _database: sample_ids)
    monkeypatch.setattr(
        search_module,
        "query_vector",
        lambda _model, query, **_kwargs: (scores[next(key for key in scores if key in query)],),
    )

    def fake_ranking(_database, *, vector, **_kwargs):
        score = vector[0]
        query_hit = SearchHit(
            ref_id=1,
            entity_id="image:0",
            item_id=sample_ids[0],
            indexed_model_signature="unused",
            vector_space="unused",
            modality=EmbeddingModality.IMAGE,
            score=score,
            generation_id=7,
            provenance={},
        )
        return SimpleNamespace(hits=(query_hit,))

    from neocortex.semantic.semantic_models import EmbeddingModality, SearchHit

    monkeypatch.setattr(search_module, "semantic_ranking", fake_ranking)
    calibration, evidence = measure_image_retrieval_calibration(
        tmp_path / "semantic.sqlite3",
        dataset,
        cache=tmp_path / "models",
        local_files_only=True,
        threads=1,
        backend_factory=lambda *_args, **_kwargs: None,
    )

    assert calibration.minimum_score == pytest.approx((0.25 + 0.75) / 2)
    assert calibration.indexed_processing_signature == "image-processing-v1"
    assert evidence.generation_id == 7
