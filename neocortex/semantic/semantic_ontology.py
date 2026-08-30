"""Shared stable concepts for deterministic and embedding-based semantics.

The ontology does not assign confidence.  It gives every evidence producer a
stable concept identifier, bilingual aliases and prototype text while the
original producer remains responsible for scores and provenance.
"""

from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Literal, Mapping


# region [01] Stable ontology contract

ONTOLOGY_VERSION = "neocortex-industrial-ontology-v1"
ConceptFamily = Literal[
    "entity",
    "activity",
    "operational_context",
    "safety_condition",
    "document_kind",
    "topic",
    "equipment",
]


@dataclass(frozen=True, slots=True)
class ConceptSpec:
    """One stable semantic concept and its human/model-facing vocabulary."""

    concept_id: str
    family: ConceptFamily
    label_es: str
    label_en: str
    aliases_es: tuple[str, ...]
    aliases_en: tuple[str, ...]
    legacy_labels: tuple[str, ...] = ()
    parent_id: str | None = None

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (self.label_es, self.label_en, *self.aliases_es, *self.aliases_en)
            )
        )

    def prototype(self, *, modality: Literal["text", "image"] = "text") -> str:
        """Return a bounded bilingual prototype without claiming calibration."""

        if modality == "image":
            return (
                f"Fotografía industrial de {self.label_es}. "
                f"Industrial electrical photograph of {self.label_en}."
            )
        return (
            f"Contenido técnico sobre {self.label_es}. "
            f"Technical content about {self.label_en}."
        )


# endregion [01]


# region [02] Canonical industrial concepts

_CORE_CONCEPTS = (
    ConceptSpec(
        "industrial.site.substation",
        "entity",
        "subestación eléctrica",
        "electrical substation",
        ("subestación", "patio eléctrico"),
        ("substation", "switchyard"),
        ("subestacion", "subestaciones"),
    ),
    ConceptSpec(
        "industrial.equipment.transformer",
        "entity",
        "transformador",
        "transformer",
        ("autotransformador", "transformador de potencia", "trafo", "tranfo"),
        ("power transformer", "autotransformer"),
        ("transformador", "transformadores"),
    ),
    ConceptSpec(
        "industrial.equipment.switchgear",
        "entity",
        "celdas de media tensión",
        "medium-voltage switchgear",
        ("switchgear", "celda eléctrica", "celdas eléctricas", "tablero MT"),
        ("electrical switchgear", "MV switchgear"),
        ("switchgear", "switchgear_media_tension"),
    ),
    ConceptSpec(
        "industrial.equipment.circuit_breaker",
        "entity",
        "interruptor de potencia",
        "power circuit breaker",
        ("interruptor", "disyuntor"),
        ("breaker", "circuit breaker"),
        ("interruptor_potencia", "interruptores_potencia"),
    ),
    ConceptSpec(
        "industrial.equipment.disconnector",
        "entity",
        "seccionador",
        "disconnector",
        ("seccionador", "cuchilla desconectadora"),
        ("disconnect switch", "isolator switch"),
        ("seccionador", "cuchillas_desconectadores"),
    ),
    ConceptSpec(
        "industrial.equipment.busbar",
        "entity",
        "barra eléctrica",
        "electrical busbar",
        ("barras eléctricas", "barra colectora"),
        ("busbar", "busbars"),
        ("barras", "barras_bus"),
    ),
    ConceptSpec(
        "industrial.equipment.insulator",
        "entity",
        "aislador eléctrico",
        "electrical insulator",
        ("aislador", "aisladores"),
        ("insulator", "insulators"),
        ("aislador", "aisladores"),
    ),
    ConceptSpec(
        "industrial.system.protection_control",
        "entity",
        "protección y control",
        "protection and control system",
        ("relé de protección", "relevador", "tablero de control"),
        ("protection relay", "relay panel", "control panel"),
        ("proteccion_control",),
    ),
    ConceptSpec(
        "industrial.equipment.instrumentation",
        "entity",
        "instrumentación eléctrica",
        "electrical instrumentation",
        (
            "instrumentación",
            "medidor",
            "multímetro",
            "osciloscopio",
            "analizador",
            "megger",
        ),
        ("instrumentation", "meter", "multimeter", "oscilloscope", "analyzer"),
        ("instrumentacion", "medicion_instrumentacion"),
    ),
    ConceptSpec(
        "industrial.equipment.machinery",
        "entity",
        "maquinaria industrial",
        "industrial machinery",
        ("maquinaria", "motor eléctrico", "generador", "compresor"),
        ("industrial machinery", "electric motor", "generator", "compressor"),
        ("maquinaria_industrial",),
    ),
    ConceptSpec(
        "industrial.activity.maintenance",
        "activity",
        "mantenimiento",
        "maintenance",
        ("reparación",),
        ("repair",),
        ("mantenimiento", "reparacion"),
    ),
    ConceptSpec(
        "industrial.activity.inspection",
        "activity",
        "inspección",
        "inspection",
        ("recorrido",),
        ("walkdown",),
        ("inspeccion",),
    ),
    ConceptSpec(
        "industrial.activity.testing",
        "activity",
        "pruebas y mediciones",
        "testing and measurement",
        ("prueba", "pruebas", "medición", "mediciones", "puesta en servicio"),
        ("testing", "measurement", "commissioning"),
        ("pruebas_medicion", "pruebas_campo", "pruebas_fabrica"),
    ),
    ConceptSpec(
        "industrial.activity.installation",
        "activity",
        "instalación y montaje",
        "installation and erection",
        ("instalación", "montaje"),
        ("installation", "erection"),
        ("instalacion_montaje", "construccion_montaje"),
    ),
    ConceptSpec(
        "industrial.activity.field_work",
        "activity",
        "trabajo de campo",
        "field work",
        ("obra", "trabajo en sitio"),
        ("site work",),
        ("trabajo_campo",),
    ),
    ConceptSpec(
        "industrial.context.substation_yard",
        "operational_context",
        "patio de subestación",
        "substation switchyard",
        ("patio eléctrico",),
        ("switchyard",),
        ("patio_subestacion",),
    ),
    ConceptSpec(
        "industrial.context.control_room",
        "operational_context",
        "sala de control",
        "control room",
        ("cuarto de control",),
        ("control room",),
        ("sala_control",),
    ),
    ConceptSpec(
        "industrial.context.workshop",
        "operational_context",
        "taller",
        "workshop",
        (),
        (),
        ("taller",),
    ),
    ConceptSpec(
        "industrial.context.plant",
        "operational_context",
        "planta industrial",
        "industrial plant",
        ("fábrica",),
        ("factory",),
        ("planta_industrial",),
    ),
    ConceptSpec(
        "industrial.safety.ppe",
        "safety_condition",
        "equipo de protección personal",
        "personal protective equipment",
        ("EPP", "casco", "arnés"),
        ("PPE", "helmet", "harness"),
        ("epp",),
    ),
    ConceptSpec(
        "industrial.safety.work_at_height",
        "safety_condition",
        "trabajo en altura",
        "working at height",
        ("andamio",),
        ("scaffold",),
        ("trabajo_altura",),
    ),
    ConceptSpec(
        "industrial.safety.hazard_signage",
        "safety_condition",
        "señalización de riesgo",
        "hazard signage",
        ("peligro", "riesgo eléctrico"),
        ("danger", "warning"),
        ("senalizacion_riesgo",),
    ),
    ConceptSpec(
        "industrial.safety.lockout_tagout",
        "safety_condition",
        "bloqueo y etiquetado",
        "lockout tagout",
        ("LOTO",),
        ("lockout tagout",),
        ("bloqueo_etiquetado",),
    ),
)

CONCEPTS: Mapping[str, ConceptSpec] = MappingProxyType(
    {concept.concept_id: concept for concept in _CORE_CONCEPTS}
)


# endregion [02]


# region [03] Compatibility mappings and document vocabulary

_FAMILY_LEGACY_INDEX: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        (concept.family, label): concept.concept_id
        for concept in _CORE_CONCEPTS
        for label in concept.legacy_labels
    }
)
_LEGACY_INDEX: Mapping[str, str] = MappingProxyType(
    {
        label: concept.concept_id
        for concept in _CORE_CONCEPTS
        for label in concept.legacy_labels
    }
)


def resolve_legacy_label(family: str, label: str) -> str:
    """Map a producer label to a stable ID, preserving unknown labels safely."""

    known = _FAMILY_LEGACY_INDEX.get((family, label)) or _LEGACY_INDEX.get(label)
    if known is not None:
        return known
    normalized = re.sub(r"[^a-z0-9_]+", "_", _fold(label)).strip("_") or "unknown"
    namespace = {
        "document_kind": "document.kind",
        "topic": "industrial.topic",
        "equipment": "industrial.equipment",
        "activity": "industrial.activity",
        "entity": "industrial.entity",
        "operational_context": "industrial.context",
        "safety_condition": "industrial.safety",
    }.get(family, "semantic.unmapped")
    return f"{namespace}.{normalized}"


def iter_document_concepts() -> Iterable[ConceptSpec]:
    """Expose every deterministic document label through the shared namespace."""

    from neocortex.documents.document_taxonomy import semantic_label_inventory

    family_map: tuple[tuple[str, ConceptFamily], ...] = (
        ("document_kind", "document_kind"),
        ("topic", "topic"),
        ("equipment", "equipment"),
        ("activity", "activity"),
    )
    for inventory_key, family in family_map:
        for label in semantic_label_inventory()[inventory_key]:
            concept_id = resolve_legacy_label(family, label)
            if concept_id in CONCEPTS:
                continue
            display = label.replace("_", " ")
            yield ConceptSpec(
                concept_id=concept_id,
                family=family,
                label_es=display,
                label_en=display,
                aliases_es=(),
                aliases_en=(),
                legacy_labels=(label,),
            )


def all_concepts() -> tuple[ConceptSpec, ...]:
    """Return a deterministic de-duplicated ontology snapshot."""

    combined = {concept.concept_id: concept for concept in _CORE_CONCEPTS}
    for concept in iter_document_concepts():
        combined.setdefault(concept.concept_id, concept)
    return tuple(combined[key] for key in sorted(combined))


# endregion [03]


# region [04] Legacy hint dictionaries and query expansion


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _legacy_hints(family: ConceptFamily) -> dict[str, tuple[str, ...]]:
    hints: dict[str, tuple[str, ...]] = {}
    for concept in _CORE_CONCEPTS:
        if concept.family != family or not concept.legacy_labels:
            continue
        primary = concept.legacy_labels[0]
        hints[primary] = tuple(dict.fromkeys(_fold(value) for value in concept.aliases))
    return hints


INDUSTRIAL_ENTITY_HINTS = _legacy_hints("entity")
INDUSTRIAL_ACTIVITY_HINTS = _legacy_hints("activity")
OPERATIONAL_CONTEXT_HINTS = _legacy_hints("operational_context")
SAFETY_CONDITION_HINTS = _legacy_hints("safety_condition")


def expand_domain_query(query: str, *, max_aliases: int = 8) -> str:
    """Add bounded bilingual aliases for known domain terms."""

    if max_aliases < 0:
        raise ValueError("max_aliases cannot be negative")
    folded = f" {_fold(query)} "
    additions: list[str] = []
    for concept in _CORE_CONCEPTS:
        if not any(f" {_fold(alias)} " in folded for alias in concept.aliases):
            continue
        for alias in (concept.label_es, concept.label_en):
            if _fold(alias) not in folded and alias not in additions:
                additions.append(alias)
                if len(additions) >= max_aliases:
                    break
        if len(additions) >= max_aliases:
            break
    return " | ".join((query, *additions)) if additions else query


# endregion [04]
