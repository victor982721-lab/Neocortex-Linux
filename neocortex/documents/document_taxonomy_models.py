"""Stable value objects shared by document-taxonomy components."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_taxonomy_models.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from dataclasses import dataclass
# endregion [01]

# region [02] Implementación


@dataclass(frozen=True, slots=True)
class DocumentSignals:
    """Bounded text and metadata already extracted by a content route."""

    source_kind: str
    path: str
    source_status: str
    title: str = ""
    author: str = ""
    metadata: str = ""
    leading_text: str = ""
    page_count: int | None = None


@dataclass(frozen=True, slots=True)
class ScoredLabel:
    label: str
    score: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StandardReference:
    authority: str
    identifier: str
    evidence: str


@dataclass(frozen=True, slots=True)
class DocumentClassification:
    classifier_signature: str
    primary_kind: str
    kind_candidates: tuple[ScoredLabel, ...]
    authorities: tuple[ScoredLabel, ...]
    standard_references: tuple[StandardReference, ...]
    organizations: tuple[ScoredLabel, ...]
    clients: tuple[ScoredLabel, ...]
    projects: tuple[ScoredLabel, ...]
    workstreams: tuple[ScoredLabel, ...]
    topics: tuple[ScoredLabel, ...]
    document_subtypes: tuple[ScoredLabel, ...]
    equipment: tuple[ScoredLabel, ...]
    activities: tuple[ScoredLabel, ...]
    confidence: float
    uncertainty: str
    evidence: tuple[str, ...]
    suggested_stem: str
    naming_signature: str
    naming_evidence: tuple[str, ...]

    @property
    def primary_authority(self) -> str | None:
        return self.authorities[0].label if self.authorities else None

    @property
    def primary_organization(self) -> str | None:
        return self.organizations[0].label if self.organizations else None

    @property
    def primary_client(self) -> str | None:
        return self.clients[0].label if self.clients else None

    @property
    def primary_project(self) -> str | None:
        return self.projects[0].label if self.projects else None

    @property
    def primary_workstream(self) -> str | None:
        return self.workstreams[0].label if self.workstreams else None

    @property
    def primary_subtype(self) -> str | None:
        return self.document_subtypes[0].label if self.document_subtypes else None

    @property
    def primary_equipment(self) -> str | None:
        return self.equipment[0].label if self.equipment else None

    @property
    def primary_activity(self) -> str | None:
        return self.activities[0].label if self.activities else None


@dataclass(frozen=True, slots=True)
class AuthoritySpec:
    code: str
    aliases: tuple[str, ...]
    identifier_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrganizationSpec:
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClientSpec:
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectSpec:
    name: str
    client: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TechnicalTaxonomy:
    signature: str
    authorities: tuple[AuthoritySpec, ...]
    organizations: tuple[OrganizationSpec, ...]
    clients: tuple[ClientSpec, ...]
    projects: tuple[ProjectSpec, ...]
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_taxonomy_models")
