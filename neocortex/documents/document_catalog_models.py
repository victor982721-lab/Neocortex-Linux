"""Pure cross-process input values shared by catalog owners and workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SourceKind = Literal[
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "odt",
    "text",
    "audio",
    "video",
    "image",
]
SourceCoverage = Literal["complete", "partial", "blocked"]


@dataclass(frozen=True, slots=True)
class CatalogDocumentView:
    """Bounded read-only projection returned by the published catalog."""

    source_kind: str
    path: str
    primary_kind: str
    primary_subtype: str | None
    primary_authority: str | None
    primary_organization: str | None
    primary_client: str | None
    primary_project: str | None
    primary_workstream: str | None
    standard_identifiers: tuple[str, ...]
    clients: tuple[str, ...]
    projects: tuple[str, ...]
    workstreams: tuple[str, ...]
    topics: tuple[str, ...]
    equipment: tuple[str, ...]
    activities: tuple[str, ...]
    confidence: float
    uncertainty: str
    catalog_status: str


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_kind: SourceKind
    file_key: str
    path: str
    volume_id: str
    file_id: str
    size: int
    mtime_ns: int
    birthtime_ns: int
    source_status: str
    processing_signature: str
    text_fingerprint: str | None
    title: str
    author: str
    metadata: str
    page_count: int | None = None
    coverage: SourceCoverage = "complete"
    text_truncated: bool = False
    resource_binding_json: str | None = None
