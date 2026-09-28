"""Independent C08-C11 probes; known readiness defects are explicit xfails."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from neocortex.semantic import image_retrieval_calibration as calibration
from neocortex.semantic.semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    clip_image_model,
    clip_text_model,
)
from neocortex.semantic.semantic_query_evidence import structured_query_support
from neocortex.semantic.semantic_quality import assess_semantic_text


def _bound_payload() -> dict[str, object]:
    sample = [f"item:image:{index}" for index in range(20)]
    return {
        "schema": calibration.CALIBRATION_SCHEMA,
        "calibration_signature": "synthetic-calibration",
        "query_model_signature": clip_text_model().model_signature,
        "indexed_model_signature": clip_image_model().model_signature,
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "backend": "fastembed",
        "minimum_score": 0.5,
        "positive_queries": 3,
        "negative_queries": 3,
        "sample_items": len(sample),
        "indexed_processing_signature": "synthetic-processing-v1",
        "indexed_generation_id": 7,
        "calibration_dataset_digest": "synthetic-dataset",
        "calibration_sample_item_ids": sample,
    }


def test_calibration_loader_must_fail_closed_when_publication_contract_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "semantic.sqlite3"
    database.write_bytes(b"synthetic")
    monkeypatch.setattr(calibration, "_read_calibration_payload", lambda _path: _bound_payload())
    monkeypatch.setattr(calibration, "_published_image_contract", lambda _path: None)
    monkeypatch.setattr(calibration, "_published_image_head_exists", lambda _path: True)
    monkeypatch.setattr(
        calibration,
        "_active_image_item_ids",
        lambda _path: tuple(f"item:image:{index}" for index in range(20)),
    )

    assert calibration.load_image_retrieval_calibration(database) is None


def test_active_image_item_lookup_is_sample_bounded(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "semantic.sqlite3"
    database.write_bytes(b"synthetic")
    captured: list[str] = []

    class Cursor:
        def __init__(self, size: int) -> None:
            self.size = size

        def fetchall(self):
            return [(f"item:image:{index}",) for index in range(self.size)]

    class Connection:
        def execute(self, query, _parameters):
            captured.append(str(query))
            return Cursor(50 if "LIMIT" in str(query).upper() else 100_000)

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(calibration, "semantic_database", lambda *_args, **_kwargs: Context())
    monkeypatch.setattr(
        calibration,
        "clip_image_model",
        lambda: SimpleNamespace(model_signature="synthetic-image-model"),
    )

    values = calibration._active_image_item_ids(database)
    assert "LIMIT" in captured[0].upper()
    assert len(values) <= calibration.MAX_CALIBRATION_SAMPLE_ITEMS


def test_explicit_sample_ids_use_targeted_membership_outside_default_page(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "semantic.sqlite3"
    database.write_bytes(b"synthetic")
    captured: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def fetchall(self):
            return [("item:image:999",)]

    class Connection:
        def execute(self, query, parameters):
            captured.append((str(query), tuple(parameters)))
            return Cursor()

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(calibration, "semantic_database", lambda *_args, **_kwargs: Context())
    monkeypatch.setattr(
        calibration,
        "clip_image_model",
        lambda: SimpleNamespace(model_signature="synthetic-image-model"),
    )
    selected = calibration._active_image_item_ids(
        database,
        sample_item_ids=tuple([f"item:image:{index}" for index in range(20)] + ["item:image:999"]),
    )
    assert selected == ("item:image:999",)
    assert " IN (" in captured[0][0]
    assert "LIMIT" not in captured[0][0].upper()


def test_loader_rejects_incompatible_or_racing_publication_heads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "semantic.sqlite3"
    database.write_bytes(b"synthetic")
    payload = _bound_payload()
    monkeypatch.setattr(calibration, "_read_calibration_payload", lambda _path: payload)
    monkeypatch.setattr(
        calibration,
        "_active_image_item_ids",
        lambda _path, **_kwargs: tuple(payload["calibration_sample_item_ids"]),
    )
    monkeypatch.setattr(
        calibration,
        "_published_image_contract",
        lambda _path: (8, "other-processing-v1"),
    )
    assert calibration.load_image_retrieval_calibration(database) is None

    contracts = iter(((7, "synthetic-processing-v1"), (8, "new-processing-v2")))
    monkeypatch.setattr(calibration, "_published_image_contract", lambda _path: next(contracts))
    assert calibration.load_image_retrieval_calibration(database) is None


def test_structured_support_rejects_piecemeal_unit_date_identity_witnesses() -> None:
    query = "MALPASO-HCN-05-001 2026-01-01 5 kV"
    text = "MALPASO-HCN-05-001\n" + ("contexto irrelevante " * 180) + "2026-01-01 5 kV"
    support = structured_query_support(query, text)
    assert support["coherent"] is False
    assert support["status"] == "mismatch"


def test_formula_gate_keeps_engineering_pdf_identifiers() -> None:
    text = "Diagrama de control -U9 -TC22 X1 X2 X3 X4 X5 X6 X7 X8"
    assessment = assess_semantic_text(text, section_kind="pdf_page", source_kind="pdf")
    assert assessment.eligible is True
