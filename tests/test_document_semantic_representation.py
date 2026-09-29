from __future__ import annotations

from dataclasses import replace

import pytest

from neocortex.documents.curation_sources import CurationSource, DerivedContent, PhysicalIdentity
from neocortex.documents.document_semantic_representation import (
    DocumentSemanticRepresentation,
    RepresentationBudgets,
    RepresentationInputError,
    build_document_representation,
)


def _source(*, path: str = "/corpus/old/report.txt", signature: str = "content-v1", kind: str = "text") -> CurationSource:
    return CurationSource(
        source_kind=kind,
        file_key="file-key-1",
        path=path,
        physical_identity=PhysicalIdentity("volume-1", "inode-1", 7),
        content_signature=signature,
        metadata={
            "title": "Informe técnico de pruebas",
            "author": "Ana López",
            "subject": "Resistencia de aislamiento",
            "logical_filename": "reporte.txt",
            "original_path": "/must-not-be-embedded.txt",
            "headings": ["Objetivo", "Resultados"],
        },
        sections=(
            DerivedContent(
                "text_document",
                "body",
                "# Objetivo\nSe midió la resistencia de aislamiento en el transformador U5.\n\n"
                "Resultados: los valores cumplen el criterio.\n\nConclusión: operación estable.\n"
                "Árbol UTF-8: áéíóú ñ; emoji 🙂.",
            ),
        ),
    )


def test_representation_is_bounded_utf8_and_excludes_path_context() -> None:
    budgets = RepresentationBudgets(
        max_chars=420,
        max_tokens=55,
        max_metadata_chars=100,
        max_headings=2,
        max_fragments=4,
        max_fragment_chars=120,
        max_views=4,
    )
    result = build_document_representation(_source(), budgets=budgets)

    assert isinstance(result, DocumentSemanticRepresentation)
    assert len(result.text) <= budgets.max_chars
    assert result.token_count <= budgets.max_tokens
    assert len(result.headings) <= 2
    assert len(result.representative_fragments) <= 4
    assert len(result.views) <= 4
    assert "á" in result.text or "é" in result.text
    assert "/corpus/old/report.txt" not in result.text
    assert "/must-not-be-embedded.txt" not in result.text
    assert "/corpus/old/report.txt" in result.context_text
    assert result.embedding_text == result.content_text
    assert len(result.fingerprint) == 64
    assert result.fingerprint_with_algorithm == "sha256:" + result.fingerprint
    assert result.provenance["content_embedding_excludes_path"] is True


def test_rename_changes_context_but_not_content_fingerprint() -> None:
    first = build_document_representation(_source(path="/corpus/old/report.txt"))
    renamed = build_document_representation(_source(path="/corpus/new/renamed.txt"))

    assert first.fingerprint == renamed.fingerprint
    assert first.content_text == renamed.content_text
    assert first.context_text != renamed.context_text


def test_changed_content_signature_invalidates_representation() -> None:
    first = build_document_representation(_source(signature="content-v1"))
    changed = build_document_representation(
        replace(_source(signature="content-v2"), sections=(DerivedContent("text", "body", "Contenido nuevo"),))
    )

    assert first.fingerprint != changed.fingerprint
    assert changed.content_signature == "content-v2"


def test_independent_budget_aliases_and_hard_four_limits() -> None:
    with pytest.raises(ValueError):
        RepresentationBudgets(max_fragments=5)
    with pytest.raises(ValueError):
        RepresentationBudgets(max_views=5)

    budgets = RepresentationBudgets(char_budget=120, token_budget=20, fragment_budget=4)
    result = build_document_representation(_source(), budgets=budgets)
    assert budgets.max_chars == 120
    assert budgets.max_tokens == 20
    assert len(result.representative_fragments) <= 4


def test_metadata_is_bounded_separately_from_content() -> None:
    source = replace(_source(), metadata={"z": "x" * 10_000, "title": "Título"})
    result = build_document_representation(
        source,
        budgets=RepresentationBudgets(max_metadata_chars=64, max_chars=512, max_tokens=128),
    )
    assert sum(len(str(key)) + len(str(value)) for key, value in result.metadata.items()) <= 128


def test_source_without_derived_text_fails_closed() -> None:
    source = replace(_source(), sections=(), metadata={"logical_filename": "file.txt"})
    with pytest.raises(RepresentationInputError):
        build_document_representation(source)


def test_audio_and_tabular_route_samples_use_same_compact_contract() -> None:
    audio = replace(
        _source(kind="audio"),
        sections=(DerivedContent("audio_segment", "0", "Transcripción: se verificó el equipo."),),
        metadata={"title": "Entrevista de inspección", "sheet_names": ["Resumen"]},
    )
    table = replace(
        _source(kind="xlsx"),
        sections=(DerivedContent("xlsx_document", "body", "Hoja: Resumen\nEquipo | Estado\nU5 | estable"),),
        metadata={"title": "Resumen de equipos", "sheet_names": ["Resumen", "Mediciones"]},
    )

    audio_result = build_document_representation(audio)
    table_result = build_document_representation(table)
    assert audio_result.text and table_result.text
    assert audio_result.provenance["source_kind"] == "audio"
    assert table_result.provenance["source_kind"] == "xlsx"
