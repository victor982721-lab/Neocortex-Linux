"""Controlled Fast Curation prototype and directory-boundary tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.semantic.fast_curation_prototypes import (
    FastCurationPrototype,
    controlled_kind_directory,
    controlled_kind_lookup,
    default_prototypes,
    prototype_set_from_records,
)


FIXTURE = Path(__file__).parent / "fixtures" / "curation_semantic_holdout.json"


def test_label_only_manifest_is_rejected() -> None:
    with pytest.raises(ValueError, match="descriptions"):
        prototype_set_from_records(
            [{"concept_id": "report", "family": "document_kind", "label": "Report"}]
        )


def test_rich_prototype_text_and_explicit_fixture_scope_are_versioned() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prototype_set = prototype_set_from_records(fixture["prototype_manifest"])
    assert len(prototype_set.prototypes) == 23
    assert prototype_set.ontology_id == "neocortex.synthetic-fast-curation"
    assert prototype_set.ontology_version == "fixture-v1"
    assert len(prototype_set.fingerprint) == 64
    sample = prototype_set.prototypes[0]
    assert len(sample.text) > len(sample.label)
    assert "Descripción:" in sample.text
    assert sample.text_fingerprint != sample.prototype_id


def test_default_ontology_has_only_controlled_axes_and_rich_fallbacks() -> None:
    prototype_set = default_prototypes()
    assert prototype_set.prototypes
    assert {value.family for value in prototype_set.prototypes} <= {
        "document_kind",
        "topic",
        "activity",
    }
    assert all(len(value.text) > len(value.label) for value in prototype_set.prototypes)


def test_explicit_manifest_becomes_the_default_compatible_exact_set() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prototype_set = default_prototypes(fixture["prototype_manifest"])
    assert len(prototype_set.prototypes) == 23
    assert prototype_set.ontology_id == "neocortex.synthetic-fast-curation"


def test_controlled_kind_lookup_uses_explicit_map_and_rejects_path_injection() -> None:
    mapping = {
        "report": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
        "invoice": "Gestion_y_administracion",
    }
    assert controlled_kind_directory("report", mapping) == "Pruebas_y_calidad"
    assert dict(controlled_kind_lookup(mapping)) == {
        "report": "Pruebas_y_calidad",
        "invoice": "Gestion_y_administracion",
    }
    assert controlled_kind_directory("unknown", mapping) is None
    with pytest.raises(ValueError, match=r"unsafe|relative"):
        controlled_kind_directory("report", {"report": "../outside"})


def test_controlled_lookup_accepts_existing_production_kind_map() -> None:
    from neocortex.documents.document_organization_planning import _COMPACT_KIND_DIRECTORIES

    lookup = controlled_kind_lookup(_COMPACT_KIND_DIRECTORIES)
    assert lookup["factura_comprobante"] == "Gestion_y_administracion"
    assert lookup["reporte_resultados_pruebas"] == "Pruebas_y_calidad"


def test_member_scope_cannot_be_mixed() -> None:
    first = FastCurationPrototype(
        "a", "a", "document_kind", "A", "A description", ontology_id="scope-a"
    )
    second = FastCurationPrototype(
        "b", "b", "document_kind", "B", "B description", ontology_id="scope-b"
    )
    with pytest.raises(ValueError, match="scope"):
        prototype_set_from_records([first, second])
