"""Canonical content-capability manifest.

This module is deliberately data-only.  It is the small, import-safe contract
that joins the physical route, its durable owner, catalog projection, full
text-search projection, Semantic channel and public surfaces.  Route
implementations remain responsible for execution; this manifest only makes
their relationship explicit and gives development tools one source of truth.

Keeping the declaration here (rather than importing every route module) is
important for ``--help`` and for the Linux runtime: optional media libraries
must not be imported merely to discover which content kinds exist.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal


CONTENT_CAPABILITY_MANIFEST_SCHEMA: Final = "neocortex.content-capability-manifest/v1"
CONTENT_CAPABILITY_MANIFEST_FINGERPRINT_PREFIX: Final = (
    "content-capability-manifest-v1:sha256:"
)

SemanticChannel = Literal["text", "image"]
InputSource = Literal["route_candidates", "inventory_snapshot"]
CoveragePolicy = Literal["complete_only", "complete_or_partial"]

_ID = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_MIME = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)
_MIME_PREFIX = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/$")


def _text(label: str, value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} is outside its bounds")
    return value


def _id(label: str, value: object) -> str:
    selected = _text(label, value)
    if _ID.fullmatch(selected) is None:
        raise ValueError(f"{label} is invalid")
    return selected


def _tuple_texts(
    label: str,
    values: Iterable[object],
    *,
    ordered: bool = False,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    result = tuple(_text(label, value) for value in values)
    if not allow_empty and not result:
        raise ValueError(f"{label} cannot be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot contain duplicates")
    if ordered and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be canonically ordered")
    return result


@dataclass(frozen=True, slots=True)
class CapabilityDependency:
    """One route dependency, explicit about whether it gates the producer."""

    capability_id: str
    required: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        _id("capability dependency", self.capability_id)
        if not isinstance(self.required, bool):
            raise ValueError("capability dependency required must be boolean")
        if self.required and not self.reason:
            raise ValueError("required capability dependencies need a reason")
        if self.reason:
            _text("capability dependency reason", self.reason)

    def as_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "required": self.required,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContentCapability:
    """One canonical producer-to-consumer content capability declaration."""

    capability_id: str
    route_name: str
    input_source: InputSource
    mime_types: tuple[str, ...]
    state_owner_id: str
    state_database: str
    state_schema_version: int
    catalog_source_kinds: tuple[str, ...]
    fts_tables: tuple[str, ...]
    semantic_source_kinds: tuple[str, ...]
    semantic_channel: SemanticChannel
    route_dependencies: tuple[CapabilityDependency, ...] = ()
    locators: tuple[str, ...] = ()
    coverage_policy: CoveragePolicy = "complete_or_partial"
    public_surfaces: tuple[str, ...] = ("cli", "human", "mcp", "ui")

    def __post_init__(self) -> None:
        _id("content capability id", self.capability_id)
        _id("content route name", self.route_name)
        if self.input_source not in {"route_candidates", "inventory_snapshot"}:
            raise ValueError("content capability input source is invalid")
        mimes = _tuple_texts("content MIME", self.mime_types, ordered=True)
        for mime in mimes:
            if _MIME.fullmatch(mime) is None and _MIME_PREFIX.fullmatch(mime) is None:
                raise ValueError("content MIME must be an exact value or lowercase prefix")
        _id("content state owner", self.state_owner_id)
        database = _text("content state database", self.state_database)
        if not database.endswith(".sqlite3"):
            raise ValueError("content state database must be SQLite")
        if (
            isinstance(self.state_schema_version, bool)
            or not isinstance(self.state_schema_version, int)
            or self.state_schema_version < 1
        ):
            raise ValueError("content state schema version must be positive")
        catalog = _tuple_texts(
            "content catalog source kind",
            self.catalog_source_kinds,
            ordered=True,
        )
        semantic = _tuple_texts(
            "content Semantic source kind",
            self.semantic_source_kinds,
            ordered=True,
            allow_empty=False,
        )
        if self.state_owner_id == "office" and not catalog:
            raise ValueError("office capability must declare catalog source kinds")
        _tuple_texts("content FTS table", self.fts_tables, ordered=True)
        # Locator order is semantic (the primary locator first), not lexical;
        # retain it in the manifest so clients can render a useful hierarchy.
        _tuple_texts("content locator", self.locators, allow_empty=False)
        if self.semantic_channel not in {"text", "image"}:
            raise ValueError("content Semantic channel is invalid")
        if self.coverage_policy not in {"complete_only", "complete_or_partial"}:
            raise ValueError("content coverage policy is invalid")
        surfaces = _tuple_texts("content public surface", self.public_surfaces, ordered=True)
        if not surfaces:
            raise ValueError("content capability must expose one public surface")
        dependencies = tuple(self.route_dependencies)
        if any(not isinstance(item, CapabilityDependency) for item in dependencies):
            raise ValueError("content route dependencies must be typed")
        dependency_ids = tuple(item.capability_id for item in dependencies)
        if len(set(dependency_ids)) != len(dependency_ids):
            raise ValueError("content route dependencies cannot repeat")
        if self.capability_id in dependency_ids:
            raise ValueError("content capability cannot depend on itself")
        # Keep the two projections joined: a catalog source must have a
        # corresponding Semantic source, except for a capability that is
        # intentionally catalog-free (none of the built-ins is one today).
        if not set(catalog).issubset(set(semantic)):
            raise ValueError("catalog source kinds must be Semantic source kinds")

    def as_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "route_name": self.route_name,
            "input_source": self.input_source,
            "mime_types": list(self.mime_types),
            "state_owner_id": self.state_owner_id,
            "state_database": self.state_database,
            "state_schema_version": self.state_schema_version,
            "catalog_source_kinds": list(self.catalog_source_kinds),
            "fts_tables": list(self.fts_tables),
            "semantic_source_kinds": list(self.semantic_source_kinds),
            "semantic_channel": self.semantic_channel,
            "route_dependencies": [item.as_payload() for item in self.route_dependencies],
            "locators": list(self.locators),
            "coverage_policy": self.coverage_policy,
            "public_surfaces": list(self.public_surfaces),
        }


def _capability(
    capability_id: str,
    route_name: str,
    *,
    input_source: InputSource,
    mime_types: tuple[str, ...],
    state_owner_id: str,
    state_database: str,
    state_schema_version: int,
    catalog_source_kinds: tuple[str, ...],
    fts_tables: tuple[str, ...],
    semantic_source_kinds: tuple[str, ...],
    semantic_channel: SemanticChannel,
    route_dependencies: tuple[CapabilityDependency, ...] = (),
    locators: tuple[str, ...],
    coverage_policy: CoveragePolicy = "complete_or_partial",
) -> ContentCapability:
    return ContentCapability(
        capability_id=capability_id,
        route_name=route_name,
        input_source=input_source,
        mime_types=mime_types,
        state_owner_id=state_owner_id,
        state_database=state_database,
        state_schema_version=state_schema_version,
        catalog_source_kinds=catalog_source_kinds,
        fts_tables=fts_tables,
        semantic_source_kinds=semantic_source_kinds,
        semantic_channel=semantic_channel,
        route_dependencies=route_dependencies,
        locators=locators,
        coverage_policy=coverage_policy,
    )


CONTENT_CAPABILITIES: Final[tuple[ContentCapability, ...]] = (
    _capability(
        "archive",
        "archive",
        input_source="route_candidates",
        mime_types=("application/zip",),
        state_owner_id="archive",
        state_database="archive.sqlite3",
        state_schema_version=1,
        catalog_source_kinds=("archive",),
        fts_tables=("document_fts",),
        semantic_source_kinds=("archive",),
        semantic_channel="text",
        locators=("archive_member", "character"),
    ),
    _capability(
        "audio",
        "audio",
        input_source="route_candidates",
        mime_types=(
            "application/ogg",
            "audio/aac",
            "audio/amr",
            "audio/flac",
            "audio/mp4",
            "audio/mpeg",
            "audio/ogg",
            "audio/opus",
            "audio/wav",
            "audio/webm",
            "audio/x-aiff",
            "audio/x-caf",
            "audio/x-ms-wma",
        ),
        state_owner_id="audio",
        state_database="audio.sqlite3",
        state_schema_version=2,
        catalog_source_kinds=("audio",),
        fts_tables=("transcript_fts",),
        semantic_source_kinds=("audio",),
        semantic_channel="text",
        locators=("audio_segment", "time_ms", "character"),
    ),
    _capability(
        "docx",
        "docx",
        input_source="route_candidates",
        mime_types=("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
        state_owner_id="docx",
        state_database="docx.sqlite3",
        state_schema_version=6,
        catalog_source_kinds=("docx",),
        fts_tables=("document_fts",),
        semantic_source_kinds=("docx",),
        semantic_channel="text",
        locators=("docx_part", "paragraph", "table", "character"),
    ),
    _capability(
        "image",
        "image",
        input_source="route_candidates",
        mime_types=("image/",),
        state_owner_id="image",
        state_database="image.sqlite3",
        state_schema_version=6,
        catalog_source_kinds=("image",),
        fts_tables=(),
        semantic_source_kinds=("image", "image_ocr"),
        semantic_channel="image",
        locators=("pixel_region", "ocr_character"),
    ),
    _capability(
        "office",
        "office",
        input_source="route_candidates",
        mime_types=(
            "application/vnd.oasis.opendocument.text",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        state_owner_id="office",
        state_database="office.sqlite3",
        state_schema_version=3,
        catalog_source_kinds=("odt", "pptx", "xlsx"),
        fts_tables=("document_fts",),
        semantic_source_kinds=("odt", "pptx", "xlsx"),
        semantic_channel="text",
        locators=("sheet", "cell", "slide", "paragraph", "character"),
    ),
    _capability(
        "pdf",
        "pdf",
        input_source="route_candidates",
        mime_types=("application/pdf",),
        state_owner_id="pdf",
        state_database="pdf.sqlite3",
        state_schema_version=13,
        catalog_source_kinds=("pdf",),
        fts_tables=("page_fts",),
        semantic_source_kinds=("pdf",),
        semantic_channel="text",
        locators=("pdf_page", "character"),
    ),
    _capability(
        "text",
        "text",
        input_source="route_candidates",
        mime_types=(
            "application/json",
            "application/xml",
            "message/rfc822",
            "text/csv",
            "text/html",
            "text/markdown",
            "text/plain",
            "text/tab-separated-values",
        ),
        state_owner_id="text",
        state_database="text.sqlite3",
        state_schema_version=2,
        catalog_source_kinds=("text",),
        fts_tables=("document_fts",),
        semantic_source_kinds=("text",),
        semantic_channel="text",
        locators=("document", "character"),
    ),
    _capability(
        "video",
        "video",
        input_source="route_candidates",
        mime_types=(
            "video/mp4",
            "video/quicktime",
            "video/webm",
            "video/x-matroska",
            "video/x-msvideo",
        ),
        state_owner_id="video",
        state_database="video.sqlite3",
        state_schema_version=2,
        catalog_source_kinds=("video",),
        fts_tables=("frame_fts",),
        semantic_source_kinds=("video",),
        semantic_channel="text",
        route_dependencies=(
            CapabilityDependency(
                "audio",
                required=False,
                reason="reuse complete transcript when a video has an audio stream",
            ),
        ),
        locators=("video_frame", "timestamp_ms", "ocr_character"),
    ),
)


def _validate_manifest(values: tuple[ContentCapability, ...]) -> None:
    if not values:
        raise ValueError("content capability manifest cannot be empty")
    ids = tuple(item.capability_id for item in values)
    if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
        raise ValueError("content capability ids must be unique and ordered")
    routes = tuple(item.route_name for item in values)
    if len(set(routes)) != len(routes):
        raise ValueError("content route names must be unique")
    owners = tuple(item.state_owner_id for item in values)
    if len(set(owners)) != len(owners):
        raise ValueError("content state owners must be unique")
    source_kinds = tuple(
        source_kind for item in values for source_kind in item.semantic_source_kinds
    )
    if len(set(source_kinds)) != len(source_kinds):
        raise ValueError("Semantic source kinds must have one canonical owner")
    known = set(ids)
    for item in values:
        if any(dependency.capability_id not in known for dependency in item.route_dependencies):
            raise ValueError("content dependency references unknown capability")


_validate_manifest(CONTENT_CAPABILITIES)


def content_capability_manifest() -> tuple[ContentCapability, ...]:
    """Return the immutable canonical content capability declarations."""

    return CONTENT_CAPABILITIES


def content_capability_by_id(capability_id: str) -> ContentCapability:
    """Resolve one capability without importing route implementations."""

    selected = _id("content capability id", capability_id)
    for capability in CONTENT_CAPABILITIES:
        if capability.capability_id == selected:
            return capability
    raise ValueError(f"unknown content capability: {selected}")


def content_capability_for_source(source_kind: str) -> ContentCapability:
    """Resolve a durable source kind to its single producing capability."""

    selected = _id("content source kind", source_kind)
    for capability in CONTENT_CAPABILITIES:
        if selected in capability.semantic_source_kinds:
            return capability
    raise ValueError(f"unknown content source kind: {selected}")


def content_capabilities_for_mime(mime: str) -> tuple[ContentCapability, ...]:
    """Return deterministic MIME matches, rejecting ambiguous exact matches."""

    selected = _text("content MIME", mime)
    if selected != selected.casefold():
        raise ValueError("content MIME must be an exact lowercase MIME value")
    if _MIME.fullmatch(selected) is None:
        raise ValueError("content MIME must be an exact lowercase MIME value")
    matches = tuple(
        capability
        for capability in CONTENT_CAPABILITIES
        if selected in capability.mime_types
        or any(prefix.endswith("/") and selected.startswith(prefix) for prefix in capability.mime_types)
    )
    return matches


def content_capability_manifest_payload() -> dict[str, object]:
    return {
        "schema": CONTENT_CAPABILITY_MANIFEST_SCHEMA,
        "capabilities": [item.as_payload() for item in CONTENT_CAPABILITIES],
    }


def content_capability_manifest_json() -> str:
    return json.dumps(
        content_capability_manifest_payload(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_capability_manifest_fingerprint() -> str:
    digest = hashlib.sha256(content_capability_manifest_json().encode("utf-8")).hexdigest()
    return CONTENT_CAPABILITY_MANIFEST_FINGERPRINT_PREFIX + digest


__all__ = (
    "CONTENT_CAPABILITIES",
    "CONTENT_CAPABILITY_MANIFEST_FINGERPRINT_PREFIX",
    "CONTENT_CAPABILITY_MANIFEST_SCHEMA",
    "CapabilityDependency",
    "ContentCapability",
    "content_capabilities_for_mime",
    "content_capability_by_id",
    "content_capability_for_source",
    "content_capability_manifest",
    "content_capability_manifest_fingerprint",
    "content_capability_manifest_json",
    "content_capability_manifest_payload",
)
