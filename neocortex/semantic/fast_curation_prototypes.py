"""Controlled prototype sets for the Fast Curation Semantic plane.

The full Semantic service owns retrieval/index prototypes.  This module owns
only the small, versioned prototype contract used to make an organization
decision.  Prototypes are deliberately richer than a label: a description,
bounded aliases and (optionally) curated positive examples are included in the
text sent to the encoder.  Empirical examples are accepted only when their
label is explicit; automatic low-confidence output is never a source of
prototypes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .semantic_models import fingerprint_text


PROTOTYPE_SCHEMA_VERSION = "fast-curation-prototypes/v1"
DEFAULT_PROTOTYPE_VERSION = "fast-curation-prototype-set-v1"
DEFAULT_PROTOTYPE_DATA = Path(__file__).with_name("data") / "curation_prototypes.json"
DEFAULT_ONTOLOGY_VERSION = "neocortex-industrial-ontology-v1"
DEFAULT_ONTOLOGY_ID = "neocortex.document-taxonomy"
MAX_PROTOTYPE_TEXT = 6_000
MAX_TERMS = 24
MAX_EXAMPLES = 8
_SAFE_DIRECTORY_PARTS = frozenset({"", ".", ".."})


def _canonical_family(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("family must be a string")
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = {
        "documentkind": "document_kind",
        "document_kind": "document_kind",
        "topic": "topic",
        "activity": "activity",
    }.get(normalized, normalized)
    if normalized not in {"document_kind", "topic", "activity"}:
        raise ValueError("Fast Curation prototypes must use a controlled family")
    return normalized


def _text(value: object, *, name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = " ".join(value.split())
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be blank")
    return value


def _bounded_strings(values: Iterable[object] | None, *, limit: int) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        if not isinstance(value, str):
            raise ValueError("prototype vocabulary values must be strings")
        value = " ".join(value.split())
        if value and value not in result:
            result.append(value)
            if len(result) >= limit:
                break
    return tuple(result)


@dataclass(frozen=True, slots=True)
class FastCurationPrototype:
    """One controlled category prototype in one classification family.

    ``family`` is intentionally a stable axis name (``document_kind``,
    ``topic`` or ``activity``).  A category may have multiple instances with
    the same ``concept_id``; this is how multimodal categories retain several
    positive prototypes without turning the decision into clustering.
    """

    prototype_id: str
    concept_id: str
    family: str
    label: str
    description: str
    aliases: tuple[str, ...] = ()
    positive_examples: tuple[str, ...] = ()
    ontology_id: str = DEFAULT_ONTOLOGY_ID
    ontology_version: str = DEFAULT_ONTOLOGY_VERSION
    prototype_version: str = DEFAULT_PROTOTYPE_VERSION
    parent_id: str | None = None
    destination: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("prototype_id", self.prototype_id),
            ("concept_id", self.concept_id),
            ("family", self.family),
            ("label", self.label),
            ("description", self.description),
            ("ontology_id", self.ontology_id),
            ("ontology_version", self.ontology_version),
            ("prototype_version", self.prototype_version),
        ):
            _text(value, name=name)
        if self.parent_id is not None:
            _text(self.parent_id, name="parent_id")
        if self.destination is not None:
            _text(self.destination, name="destination")
        object.__setattr__(self, "family", _canonical_family(self.family))
        if len(self.aliases) > MAX_TERMS or len(self.positive_examples) > MAX_EXAMPLES:
            raise ValueError("prototype vocabulary exceeds bounded limits")
        if any(not isinstance(value, str) or not value.strip() for value in self.aliases):
            raise ValueError("aliases must be non-empty strings")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.positive_examples
        ):
            raise ValueError("positive examples must be non-empty strings")
        try:
            json.dumps(self.provenance, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("prototype provenance must be JSON compatible") from exc

    @property
    def text(self) -> str:
        """Return bounded rich text; a bare label is never sufficient."""

        parts = [
            f"Categoría de {self.family}: {self.label}.",
            f"Descripción: {self.description}.",
        ]
        if self.aliases:
            parts.append("Términos y alias: " + ", ".join(self.aliases) + ".")
        if self.positive_examples:
            parts.append(
                "Ejemplos positivos curados: "
                + " | ".join(self.positive_examples)
                + "."
            )
        if self.parent_id:
            parts.append(f"Categoría padre controlada: {self.parent_id}.")
        value = " ".join(parts)
        return value[:MAX_PROTOTYPE_TEXT].rstrip()

    @property
    def text_fingerprint(self) -> str:
        return fingerprint_text(self.text).xxh3_128

    @property
    def identity(self) -> str:
        return (
            f"{self.ontology_id}:{self.ontology_version}:"
            f"{self.prototype_version}:{self.prototype_id}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": PROTOTYPE_SCHEMA_VERSION,
            "prototype_id": self.prototype_id,
            "concept_id": self.concept_id,
            "family": self.family,
            "label": self.label,
            "description": self.description,
            "aliases": list(self.aliases),
            "positive_examples": list(self.positive_examples),
            "ontology_id": self.ontology_id,
            "ontology_version": self.ontology_version,
            "prototype_version": self.prototype_version,
            "parent_id": self.parent_id,
            "destination": self.destination,
            "text_fingerprint": self.text_fingerprint,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class EmpiricalPrototypeExample:
    """Explicitly labeled input allowed to seed an empirical prototype."""

    document_id: str
    family: str
    concept_id: str
    text: str
    source: str = "curated"
    explicit_label: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("document_id", self.document_id),
            ("family", self.family),
            ("concept_id", self.concept_id),
            ("text", self.text),
            ("source", self.source),
        ):
            _text(value, name=name)
        if self.family not in {"document_kind", "topic", "activity"}:
            raise ValueError("empirical prototype family is not controlled")
        if not self.explicit_label:
            raise ValueError("empirical prototypes require an explicit label")


@dataclass(frozen=True, slots=True)
class PrototypeSet:
    """Immutable, fingerprinted set used by a curation run."""

    prototypes: tuple[FastCurationPrototype, ...]
    ontology_id: str = DEFAULT_ONTOLOGY_ID
    ontology_version: str = DEFAULT_ONTOLOGY_VERSION
    prototype_version: str = DEFAULT_PROTOTYPE_VERSION
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.prototypes:
            raise ValueError("prototype set cannot be empty")
        ids = [prototype.prototype_id for prototype in self.prototypes]
        if len(ids) != len(set(ids)):
            raise ValueError("prototype ids must be unique")
        families = {prototype.family for prototype in self.prototypes}
        if not families <= {"document_kind", "topic", "activity"}:
            raise ValueError("prototype set contains an uncontrolled family")
        if {prototype.ontology_id for prototype in self.prototypes} != {self.ontology_id}:
            raise ValueError("prototype set ontology scope differs from its members")
        if {prototype.ontology_version for prototype in self.prototypes} != {self.ontology_version}:
            raise ValueError("prototype set ontology version differs from its members")
        if {prototype.prototype_version for prototype in self.prototypes} != {self.prototype_version}:
            raise ValueError("prototype set version differs from its members")
        expected = prototype_set_fingerprint(self.prototypes)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("prototype set fingerprint does not match its contents")
        object.__setattr__(self, "fingerprint", expected)

    def by_family(self, family: str) -> tuple[FastCurationPrototype, ...]:
        return tuple(value for value in self.prototypes if value.family == family)

    def by_id(self, prototype_id: str) -> FastCurationPrototype | None:
        return next(
            (value for value in self.prototypes if value.prototype_id == prototype_id),
            None,
        )


def prototype_set_fingerprint(
    prototypes: Sequence[FastCurationPrototype],
) -> str:
    payload = [
        {
            "prototype_id": value.prototype_id,
            "concept_id": value.concept_id,
            "family": value.family,
            "text_fingerprint": value.text_fingerprint,
            "ontology_version": value.ontology_version,
            "prototype_version": value.prototype_version,
        }
        for value in sorted(prototypes, key=lambda item: item.prototype_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def controlled_kind_directory(
    concept_id: str,
    directories: Mapping[str, str | Sequence[str]],
) -> str | None:
    """Resolve a document-kind ID through an explicit controlled map.

    The map is supplied by Organization's owner; this semantic module never
    turns model labels into directory names.  A sequence is accepted only to
    preserve the existing compact-directory contract, and its first entry is
    selected by that already-controlled map.
    """

    if not isinstance(concept_id, str) or not concept_id.strip():
        raise ValueError("concept_id must be non-empty")
    value = directories.get(concept_id)
    if value is None:
        return None
    selected = value[0] if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else value
    if not isinstance(selected, str) or not selected.strip():
        raise ValueError("controlled directory value must be non-empty")
    normalized = selected.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("controlled directory must be relative")
    parts = tuple(part for part in normalized.split("/") if part)
    if any(part in _SAFE_DIRECTORY_PARTS for part in parts) or any("\x00" in part for part in parts):
        raise ValueError("controlled directory contains an unsafe component")
    return "/".join(parts)


def controlled_kind_lookup(
    directories: Mapping[str, str | Sequence[str]],
) -> Mapping[str, str]:
    """Return a validated immutable ID-to-directory projection."""

    result = {
        concept_id: selected
        for concept_id in directories
        if (selected := controlled_kind_directory(concept_id, directories)) is not None
    }
    return MappingProxyType(result)


def _mapping_value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def prototypes_from_ontology(
    concepts: Iterable[object] | None = None,
    *,
    ontology_id: str = DEFAULT_ONTOLOGY_ID,
    ontology_version: str = DEFAULT_ONTOLOGY_VERSION,
    prototype_version: str = DEFAULT_PROTOTYPE_VERSION,
    empirical_examples: Iterable[EmpiricalPrototypeExample] = (),
) -> PrototypeSet:
    """Build rich controlled prototypes from the shared ontology.

    Importing the ontology is lazy so the full Semantic service remains
    import-light.  A caller may pass a bounded concept iterable in tests or
    for a calibrated local taxonomy.
    """

    if concepts is None:
        from .semantic_ontology import all_concepts

        concepts = all_concepts()
    empirical: dict[tuple[str, str], list[str]] = {}
    for example in empirical_examples:
        if not isinstance(example, EmpiricalPrototypeExample):
            raise TypeError("empirical_examples must contain explicit DTOs")
        empirical.setdefault((example.family, example.concept_id), []).append(example.text)

    values: list[FastCurationPrototype] = []
    for concept in concepts:
        family = _mapping_value(concept, "family")
        concept_id = _mapping_value(concept, "concept_id")
        label_es = _mapping_value(concept, "label_es")
        label_en = _mapping_value(concept, "label_en")
        aliases = _mapping_value(concept, "aliases", ())
        if family not in {"document_kind", "topic", "activity"}:
            continue
        if not isinstance(concept_id, str) or not concept_id:
            continue
        label = str(label_es or label_en or concept_id)
        description = str(label_en or label)
        prototype_method = getattr(concept, "prototype", None)
        if callable(prototype_method):
            try:
                description = str(prototype_method(modality="text"))
            except (TypeError, ValueError):
                pass
        # Dynamically bridged legacy taxonomy labels do not carry a rich
        # description.  Give them a bounded semantic context rather than
        # silently turning a label into the entire prototype.
        if len(description.strip()) <= len(label) + 24:
            description = {
                "document_kind": (
                    f"Documento cuyo tipo y propósito principal corresponden a {label}; "
                    "la decisión debe considerar su estructura y contenido, no sólo el nombre."
                ),
                "topic": (
                    f"Contenido técnico u operativo centrado en {label}, con evidencia "
                    "en el texto derivado y sus secciones representativas."
                ),
                "activity": (
                    f"Registro, procedimiento o resultado relacionado con la actividad {label}; "
                    "requiere evidencia documental suficiente."
                ),
            }[str(family)]
        alias_values = _bounded_strings(aliases, limit=MAX_TERMS)
        examples = tuple(empirical.get((str(family), concept_id), ()))[:MAX_EXAMPLES]
        values.append(
            FastCurationPrototype(
                prototype_id=f"{family}:{concept_id}:base",
                concept_id=concept_id,
                family=str(family),
                label=label,
                description=description,
                aliases=alias_values,
                positive_examples=examples,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
                prototype_version=prototype_version,
                parent_id=_mapping_value(concept, "parent_id"),
                provenance={"source": "shared-ontology", "authority": "controlled"},
            )
        )
    if not values:
        raise ValueError("ontology did not provide controlled curation prototypes")
    member_ontology_ids = {value.ontology_id for value in values}
    member_ontology_versions = {value.ontology_version for value in values}
    member_prototype_versions = {value.prototype_version for value in values}
    if len(member_ontology_ids) != 1 or len(member_ontology_versions) != 1 or len(member_prototype_versions) != 1:
        raise ValueError("prototype manifest mixes incompatible version scopes")
    return PrototypeSet(
        tuple(values),
        ontology_id=next(iter(member_ontology_ids)),
        ontology_version=next(iter(member_ontology_versions)),
        prototype_version=next(iter(member_prototype_versions)),
    )


def prototype_set_from_records(
    records: Iterable[Mapping[str, object] | FastCurationPrototype],
    *,
    ontology_id: str = DEFAULT_ONTOLOGY_ID,
    ontology_version: str = DEFAULT_ONTOLOGY_VERSION,
    prototype_version: str = DEFAULT_PROTOTYPE_VERSION,
) -> PrototypeSet:
    """Parse a bounded, explicit prototype manifest without accepting labels only."""

    values: list[FastCurationPrototype] = []
    for index, record in enumerate(records):
        if isinstance(record, FastCurationPrototype):
            values.append(record)
            continue
        if not isinstance(record, Mapping):
            raise TypeError("prototype manifest entries must be mappings or DTOs")
        label = record.get("label")
        description = record.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("prototype descriptions are required; labels alone are unsafe")
        concept_id = record.get("concept_id")
        family = record.get("family")
        if not isinstance(concept_id, str) or not isinstance(family, str):
            raise ValueError("prototype concept_id and family are required")
        values.append(
            FastCurationPrototype(
                prototype_id=str(record.get("prototype_id") or f"{family}:{concept_id}:{index}"),
                concept_id=concept_id,
                family=family,
                label=str(label or concept_id),
                description=description,
                aliases=_bounded_strings(record.get("aliases"), limit=MAX_TERMS),
                positive_examples=_bounded_strings(
                    record.get("positive_examples"), limit=MAX_EXAMPLES
                ),
                ontology_id=str(record.get("ontology_id") or ontology_id),
                ontology_version=str(record.get("ontology_version") or ontology_version),
                prototype_version=str(record.get("prototype_version") or prototype_version),
                parent_id=record.get("parent_id"),
                destination=record.get("destination"),
                provenance=record.get("provenance") or {},
            )
        )
    member_ontology_ids = {value.ontology_id for value in values}
    member_ontology_versions = {value.ontology_version for value in values}
    member_prototype_versions = {value.prototype_version for value in values}
    if len(member_ontology_ids) != 1 or len(member_ontology_versions) != 1 or len(member_prototype_versions) != 1:
        raise ValueError("prototype manifest mixes incompatible version scopes")
    return PrototypeSet(
        tuple(values),
        ontology_id=next(iter(member_ontology_ids)),
        ontology_version=next(iter(member_ontology_versions)),
        prototype_version=next(iter(member_prototype_versions)),
    )


def default_prototypes(
    source: object | None = None,
) -> PrototypeSet:
    """Load the packaged exact set, or use the current controlled ontology.

    A measured bundle may package an explicit manifest under ``data/``.  Until
    that artifact exists, the fallback is the repository's current controlled
    ontology—not a benchmark fixture and not an authority for autosafe
    organization without a calibrated policy.
    """

    selected: object = source
    if selected is None and DEFAULT_PROTOTYPE_DATA.is_file():
        selected = DEFAULT_PROTOTYPE_DATA
    if selected is None:
        return prototypes_from_ontology()
    if isinstance(selected, (str, Path)):
        raw = Path(selected).read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > 1_000_000:
            raise ValueError("prototype manifest exceeds bounded size")
        selected = json.loads(raw)
    if isinstance(selected, Mapping):
        records = selected.get("prototypes", selected.get("prototype_manifest"))
    else:
        records = selected
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
        raise ValueError("prototype source must contain an iterable manifest")
    return prototype_set_from_records(records)


__all__ = [
    "DEFAULT_ONTOLOGY_ID",
    "DEFAULT_ONTOLOGY_VERSION",
    "DEFAULT_PROTOTYPE_DATA",
    "DEFAULT_PROTOTYPE_VERSION",
    "PROTOTYPE_SCHEMA_VERSION",
    "EmpiricalPrototypeExample",
    "FastCurationPrototype",
    "PrototypeSet",
    "controlled_kind_directory",
    "controlled_kind_lookup",
    "default_prototypes",
    "prototype_set_fingerprint",
    "prototype_set_from_records",
    "prototypes_from_ontology",
]
