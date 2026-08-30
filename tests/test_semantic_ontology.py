from __future__ import annotations

from neocortex.documents.document_taxonomy import semantic_label_inventory
from neocortex.semantic.semantic_ontology import (
    all_concepts,
    resolve_legacy_label,
)


# region [01] Equipment producer compatibility

_FAMILY_INDEPENDENT_EQUIPMENT_IDS = {
    "interruptores_potencia": "industrial.equipment.circuit_breaker",
    "cuchillas_desconectadores": "industrial.equipment.disconnector",
    "switchgear_media_tension": "industrial.equipment.switchgear",
    "barras_bus": "industrial.equipment.busbar",
    "aisladores": "industrial.equipment.insulator",
    "proteccion_control": "industrial.system.protection_control",
    "subestaciones": "industrial.site.substation",
}


def test_every_equipment_label_resolves_to_one_canonical_concept_id() -> None:
    labels = semantic_label_inventory()["equipment"]
    concepts = all_concepts()
    by_id = {concept.concept_id: concept for concept in concepts}
    resolved = {label: resolve_legacy_label("equipment", label) for label in labels}

    assert len(by_id) == len(concepts)
    assert len(set(resolved.values())) == len(labels)
    assert set(resolved.values()) <= set(by_id)
    for label, concept_id in resolved.items():
        assert label in by_id[concept_id].legacy_labels


def test_family_independent_equipment_labels_reuse_core_concept_ids() -> None:
    resolved = {
        label: resolve_legacy_label("equipment", label)
        for label in _FAMILY_INDEPENDENT_EQUIPMENT_IDS
    }

    assert resolved == _FAMILY_INDEPENDENT_EQUIPMENT_IDS


# endregion [01]
