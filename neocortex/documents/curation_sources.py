"""Bounded, route-owned source adapters for Fast Curation.

Fast Curation consumes this module after content routes (and, when supplied,
the current catalog projection) have published derived text.  It never opens a
PDF, DOCX, image, audio file, corpus path, or Full Semantic index.  The route
owner remains responsible for opening its fenced SQLite connection; the
catalog/route adapter accepts that one connection and uses the existing
bounded ``_load_leading_text`` reader.

The public iterator is page-oriented so a caller can validate the current
catalog and route-owner fences before each page is sent to classification.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_PAGE_SIZE = 256
DEFAULT_SOURCE_SECTION_CHARS = 64_000
DEFAULT_SOURCE_SECTION_COUNT = 64
DEFAULT_CATALOG_TEXT_CHARS = 64_000
MAX_SOURCE_PAGE_SIZE = 10_000


class CurationSourceError(ValueError):
    """A route/catalog source cannot establish a bounded identity."""


class CurationSourceFenceError(CurationSourceError):
    """The source page does not match the caller's current owner fences."""


@dataclass(frozen=True, slots=True)
class PhysicalIdentity:
    """Physical identity copied from the route owner, not inferred from path."""

    volume_id: str | int
    file_id: str | int
    birthtime_ns: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.volume_id, bool) or not str(self.volume_id).strip():
            raise ValueError("physical volume_id cannot be blank")
        if isinstance(self.file_id, bool) or not str(self.file_id).strip():
            raise ValueError("physical file_id cannot be blank")
        if self.birthtime_ns is not None and (
            isinstance(self.birthtime_ns, bool) or not isinstance(self.birthtime_ns, int)
        ):
            raise ValueError("birthtime_ns must be an integer when present")

    def as_dict(self) -> dict[str, object]:
        return {
            "volume_id": str(self.volume_id),
            "file_id": str(self.file_id),
            "birthtime_ns": self.birthtime_ns,
        }


@dataclass(frozen=True, slots=True)
class DerivedContent:
    """One already-published route section; text is never read from a path."""

    section_kind: str
    section_id: str
    text: str
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.section_kind.strip() or not self.section_id.strip():
            raise ValueError("derived section kind/id cannot be blank")
        if not isinstance(self.text, str):
            raise ValueError("derived section text must be a string")
        if not isinstance(self.provenance, Mapping):
            raise ValueError("derived section provenance must be a mapping")


@dataclass(frozen=True, slots=True)
class CurationSourceFences:
    """Owner observations supplied by the caller for one source page."""

    route_owner: object | None = None
    catalog: object | None = None

    def matches(self, other: "CurationSourceFences") -> bool:
        return self.route_owner == other.route_owner and self.catalog == other.catalog

    def as_dict(self) -> dict[str, object]:
        return {"route_owner": self.route_owner, "catalog": self.catalog}


@dataclass(frozen=True, slots=True)
class CurationSource:
    """Identity and derived content consumed by representation construction.

    ``content_signature``/``input_signature`` is deliberately content/route
    based, never a path or mtime-only value.  It is the invalidation key for
    changed content; a physical rename may update ``path`` while leaving this
    field unchanged.
    """

    source_kind: str
    file_key: str
    path: str
    physical_identity: PhysicalIdentity | Mapping[str, object] | str
    content_signature: str | None = None
    sections: tuple[DerivedContent, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    route_fence: object | None = None
    catalog_fence: object | None = None
    source_status: str = "complete"
    coverage: str = "complete"
    # Compatibility spelling used by catalog persistence.  It is normalized
    # to content_signature in __post_init__; both public properties agree.
    input_signature: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("source_kind", self.source_kind),
            ("file_key", self.file_key),
            ("path", self.path),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be blank")
        signature = self.content_signature or self.input_signature
        if not isinstance(signature, str) or not signature.strip():
            raise CurationSourceError("content_signature/input_signature is required")
        if self.content_signature is not None and self.input_signature is not None:
            if self.content_signature != self.input_signature:
                raise CurationSourceError("content_signature and input_signature disagree")
        object.__setattr__(self, "content_signature", signature.strip())
        object.__setattr__(self, "input_signature", signature.strip())
        if not isinstance(self.physical_identity, (PhysicalIdentity, Mapping, str)):
            raise ValueError("physical_identity must be an owner identity")
        if self.coverage not in {"complete", "partial", "blocked"}:
            raise ValueError("source coverage is invalid")
        sections: list[DerivedContent] = []
        for section in self.sections:
            if isinstance(section, DerivedContent):
                sections.append(section)
            elif isinstance(section, Mapping):
                sections.append(
                    DerivedContent(
                        str(section.get("section_kind", section.get("kind", "derived"))),
                        str(section.get("section_id", section.get("id", len(sections)))),
                        str(section.get("text", "")),
                        section.get("provenance", {}),
                    )
                )
            else:
                kind = str(getattr(section, "section_kind", "derived"))
                identifier = str(getattr(section, "section_id", len(sections)))
                text = getattr(section, "text", "")
                provenance = getattr(section, "provenance", {})
                sections.append(DerivedContent(kind, identifier, str(text), provenance))
        object.__setattr__(self, "sections", tuple(sections))
        object.__setattr__(self, "metadata", dict(self.metadata) if isinstance(self.metadata, Mapping) else {})

    @property
    def fences(self) -> CurationSourceFences:
        return CurationSourceFences(self.route_fence, self.catalog_fence)

    @property
    def physical_identity_json(self) -> str:
        value = self.physical_identity
        if isinstance(value, PhysicalIdentity):
            value = value.as_dict()
        elif isinstance(value, Mapping):
            value = dict(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CurationSourcePage:
    """One bounded page and the fences required before classification."""

    items: tuple[CurationSource, ...]
    page_index: int
    next_page: int | None
    fences: CurationSourceFences

    def __post_init__(self) -> None:
        if self.page_index < 0:
            raise ValueError("page_index cannot be negative")
        if self.next_page is not None and self.next_page <= self.page_index:
            raise ValueError("next_page must advance")
        if len(self.items) > MAX_SOURCE_PAGE_SIZE:
            raise ValueError("source page is too large")
        if self.items and any(item.fences != self.fences for item in self.items):
            raise CurationSourceFenceError("one page cannot mix owner fences")

    @property
    def sources(self) -> tuple[CurationSource, ...]:
        return self.items

    @property
    def records(self) -> tuple[CurationSource, ...]:
        return self.items

    @property
    def next_cursor(self) -> int | None:
        return self.next_page

    @property
    def complete(self) -> bool:
        return self.next_page is None


def _validate_page_fences(
    fences: CurationSourceFences,
    expected: CurationSourceFences | None,
    validator: Callable[[CurationSourceFences], object] | None,
) -> None:
    if expected is not None and not fences.matches(expected):
        raise CurationSourceFenceError("route/catalog source fence changed before page use")
    if validator is not None:
        result = validator(fences)
        if result is False:
            raise CurationSourceFenceError("caller rejected current route/catalog source fences")


def iter_curation_source_pages(
    sources: Iterable[CurationSource],
    *,
    page_size: int = DEFAULT_SOURCE_PAGE_SIZE,
    expected_fences: CurationSourceFences | None = None,
    validate_fences: Callable[[CurationSourceFences], object] | None = None,
) -> Iterator[CurationSourcePage]:
    """Yield deterministic bounded pages without materializing all sources.

    A one-item lookahead is used only to mark the final page; no corpus or
    SQLite access occurs here.  Fence validation runs after a page is formed
    and before it is yielded, leaving the caller a final opportunity to
    revalidate its live owner before encoding or organization.
    """

    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_SOURCE_PAGE_SIZE:
        raise ValueError("page_size must be between 1 and 10000")
    iterator = iter(sources)
    page_index = 0
    pending: CurationSource | None = None
    exhausted = False
    while not exhausted:
        page: list[CurationSource] = []
        page_fences: CurationSourceFences | None = None
        while len(page) < page_size:
            current = pending
            pending = None
            if current is None:
                try:
                    current = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
            if not isinstance(current, CurationSource):
                raise TypeError("iter_curation_source_pages expects CurationSource values")
            if page_fences is None:
                page_fences = current.fences
            elif current.fences != page_fences:
                # Do not mix catalog/route revisions; hold the new item for
                # the next page and let the caller validate the new fence.
                pending = current
                break
            page.append(current)
        if not page:
            break
        # A full page still needs one bounded lookahead so an exact multiple
        # of ``page_size`` is marked complete rather than advertising an
        # empty next page.  A fence change is retained for the next page.
        if not exhausted and pending is None and len(page) == page_size:
            try:
                lookahead = next(iterator)
            except StopIteration:
                exhausted = True
            else:
                pending = lookahead
        assert page_fences is not None
        _validate_page_fences(page_fences, expected_fences, validate_fences)
        has_more = pending is not None or not exhausted
        yield CurationSourcePage(tuple(page), page_index, page_index + 1 if has_more else None, page_fences)
        page_index += 1


def iter_curation_sources(
    sources: Iterable[CurationSource],
    *,
    page_size: int = DEFAULT_SOURCE_PAGE_SIZE,
    expected_fences: CurationSourceFences | None = None,
    validate_fences: Callable[[CurationSourceFences], object] | None = None,
) -> Iterator[CurationSource]:
    """Flatten the page adapter for callers that do their own batching."""

    for page in iter_curation_source_pages(
        sources,
        page_size=page_size,
        expected_fences=expected_fences,
        validate_fences=validate_fences,
    ):
        yield from page.items


class CurationSourceAdapter:
    """Reusable page iterator binding the fence policy once."""

    def __init__(
        self,
        sources: Iterable[CurationSource],
        *,
        page_size: int = DEFAULT_SOURCE_PAGE_SIZE,
        expected_fences: CurationSourceFences | None = None,
        validate_fences: Callable[[CurationSourceFences], object] | None = None,
    ) -> None:
        self._sources = sources
        self.page_size = page_size
        self.expected_fences = expected_fences
        self.validate_fences = validate_fences

    def pages(self) -> Iterator[CurationSourcePage]:
        yield from iter_curation_source_pages(
            self._sources,
            page_size=self.page_size,
            expected_fences=self.expected_fences,
            validate_fences=self.validate_fences,
        )

    def __iter__(self) -> Iterator[CurationSource]:
        for page in self.pages():
            yield from page.items


def _bounded_section(section: object, *, max_chars: int) -> DerivedContent | None:
    if isinstance(section, DerivedContent):
        kind, identifier, text, provenance = section.section_kind, section.section_id, section.text, section.provenance
    elif isinstance(section, Mapping):
        kind = str(section.get("section_kind", section.get("kind", "derived")))
        identifier = str(section.get("section_id", section.get("id", "0")))
        text = str(section.get("text", ""))
        provenance = section.get("provenance", {})
    else:
        kind = str(getattr(section, "section_kind", "derived"))
        identifier = str(getattr(section, "section_id", "0"))
        text = str(getattr(section, "text", ""))
        provenance = getattr(section, "provenance", {})
    if not text:
        return None
    if max_chars < 1:
        return None
    return DerivedContent(kind, identifier, text[:max_chars], provenance if isinstance(provenance, Mapping) else {})


def _semantic_item_signature(item: object) -> str:
    revision = getattr(item, "source_revision", {})
    if isinstance(revision, Mapping):
        for key in ("raw_content_xxh3_128", "fingerprint_digest", "fingerprint"):
            value = revision.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    fingerprint = getattr(item, "fingerprint", None)
    digest = getattr(fingerprint, "xxh3_128", None)
    count = getattr(fingerprint, "byte_count", None)
    if isinstance(digest, str) and digest.strip():
        return f"route-descriptor:{digest.strip()}:{count if isinstance(count, int) else ''}"
    raise CurationSourceError("semantic route item has no content signature")


def curation_source_from_semantic_record(
    record: object,
    *,
    metadata: Mapping[str, object] | None = None,
    route_fence: object | None = None,
    catalog_fence: object | None = None,
    max_section_chars: int = DEFAULT_SOURCE_SECTION_CHARS,
) -> CurationSource:
    """Adapt an existing Semantic route record without reading its path."""

    item = getattr(record, "item", None)
    if item is None:
        raise CurationSourceError("semantic route record has no item")
    source_kind = str(getattr(item, "source_kind", ""))
    file_key = str(getattr(item, "source_identity", ""))
    path = str(getattr(item, "path", ""))
    if not source_kind or not file_key or not path:
        raise CurationSourceError("semantic route item lacks source identity/path")
    item_metadata = dict(getattr(item, "provenance", {}) or {})
    if metadata:
        item_metadata.update(metadata)
    sections: list[DerivedContent] = []
    section = getattr(record, "section", None)
    ocr_section = getattr(record, "ocr_section", None)
    for candidate in ((section,) if section is not None else ()):
        bounded = _bounded_section(candidate, max_chars=max_section_chars)
        if bounded is not None:
            sections.append(bounded)
    if ocr_section is not None:
        bounded = _bounded_section(ocr_section, max_chars=max_section_chars)
        if bounded is not None:
            sections.append(bounded)
    revision = getattr(item, "source_revision", {})
    identity_values = revision if isinstance(revision, Mapping) else {}
    physical = {
        "source_identity": file_key,
        "volume_id": identity_values.get("volume_id", "route"),
        "file_id": identity_values.get("file_id", file_key),
        "birthtime_ns": identity_values.get("birthtime_ns"),
    }
    return CurationSource(
        source_kind=source_kind,
        file_key=file_key,
        path=path,
        physical_identity=physical,
        content_signature=_semantic_item_signature(item),
        sections=tuple(sections),
        metadata=item_metadata,
        route_fence=route_fence,
        catalog_fence=catalog_fence,
        source_status=str(item_metadata.get("source_status", "complete")),
        coverage=str(item_metadata.get("coverage", "complete")),
    )


def iter_semantic_record_pages(
    records: Iterable[object],
    *,
    page_size: int = DEFAULT_SOURCE_PAGE_SIZE,
    route_fence: object | None = None,
    catalog_fence: object | None = None,
    metadata_lookup: Callable[[CurationSource], Mapping[str, object] | None] | None = None,
    max_section_chars: int = DEFAULT_SOURCE_SECTION_CHARS,
    max_sections_per_source: int = DEFAULT_SOURCE_SECTION_COUNT,
) -> Iterator[CurationSourcePage]:
    """Group contiguous route records by file identity, then paginate."""

    if (
        isinstance(max_sections_per_source, bool)
        or not isinstance(max_sections_per_source, int)
        or not 1 <= max_sections_per_source <= 4096
    ):
        raise ValueError("max_sections_per_source must be between 1 and 4096")

    def grouped() -> Iterator[CurationSource]:
        current: CurationSource | None = None
        for record in records:
            candidate = curation_source_from_semantic_record(
                record,
                route_fence=route_fence,
                catalog_fence=catalog_fence,
                max_section_chars=max_section_chars,
            )
            if current is not None and (
                candidate.source_kind != current.source_kind or candidate.file_key != current.file_key
            ):
                if metadata_lookup is not None:
                    extra = metadata_lookup(current)
                    if extra:
                        current = _merge_source_metadata(current, extra)
                yield current
                current = None
            if current is None:
                current = candidate
            else:
                current = _merge_source_sections(
                    current, candidate, max_sections=max_sections_per_source
                )
        if current is not None:
            if metadata_lookup is not None:
                extra = metadata_lookup(current)
                if extra:
                    current = _merge_source_metadata(current, extra)
            yield current

    yield from iter_curation_source_pages(grouped(), page_size=page_size)


def _merge_source_sections(
    left: CurationSource,
    right: CurationSource,
    *,
    max_sections: int = DEFAULT_SOURCE_SECTION_COUNT,
) -> CurationSource:
    return CurationSource(
        source_kind=left.source_kind,
        file_key=left.file_key,
        path=left.path,
        physical_identity=left.physical_identity,
        content_signature=left.content_signature,
        sections=(left.sections + right.sections)[:max_sections],
        metadata={**left.metadata, **right.metadata},
        route_fence=left.route_fence,
        catalog_fence=left.catalog_fence,
        source_status=left.source_status,
        coverage=left.coverage,
    )


def _merge_source_metadata(source: CurationSource, extra: Mapping[str, object]) -> CurationSource:
    return CurationSource(
        source_kind=source.source_kind,
        file_key=source.file_key,
        path=source.path,
        physical_identity=source.physical_identity,
        content_signature=source.content_signature,
        sections=source.sections,
        metadata={**source.metadata, **dict(extra)},
        route_fence=source.route_fence,
        catalog_fence=source.catalog_fence,
        source_status=source.source_status,
        coverage=source.coverage,
    )


def iter_route_curation_source_pages(
    state_directory: Path,
    source_kind: str,
    *,
    connection: Any | None = None,
    page_size: int = DEFAULT_SOURCE_PAGE_SIZE,
    route_fence: object | None = None,
    catalog_fence: object | None = None,
    metadata_lookup: Callable[[CurationSource], Mapping[str, object] | None] | None = None,
    max_section_chars: int = DEFAULT_SOURCE_SECTION_CHARS,
    max_sections_per_source: int = DEFAULT_SOURCE_SECTION_COUNT,
) -> Iterator[CurationSourcePage]:
    """Read only existing route projections and yield bounded source pages.

    ``connection`` is passed through to the route adapter when supported.  If
    a caller owns a fenced connection, it remains the sole owner and remains
    responsible for closing/rechecking it.  This function does not require a
    published Full Semantic head.
    """

    from neocortex.semantic.semantic_sources import (
        iter_image_source_records,
        iter_text_source_records,
    )

    if source_kind == "image":
        records: Iterable[object] = iter_image_source_records(state_directory)
    else:
        records = iter_text_source_records(state_directory, source_kind, connection=connection)
    yield from iter_semantic_record_pages(
        records,
        page_size=page_size,
        route_fence=route_fence,
        catalog_fence=catalog_fence,
        metadata_lookup=metadata_lookup,
        max_section_chars=max_section_chars,
        max_sections_per_source=max_sections_per_source,
    )


def _document_value(document: object, name: str, default: object = None) -> object:
    if isinstance(document, Mapping):
        return document.get(name, default)
    return getattr(document, name, default)


def iter_catalog_route_source_pages(
    documents: Iterable[object],
    connection: Any,
    *,
    page_size: int = DEFAULT_SOURCE_PAGE_SIZE,
    route_fence: object | None = None,
    catalog_fence: object | None = None,
    metadata_lookup: Callable[[CurationSource], Mapping[str, object] | None] | None = None,
    require_catalog_match: bool = False,
    max_text_chars: int = DEFAULT_CATALOG_TEXT_CHARS,
    cancellation: object | None = None,
) -> Iterator[CurationSourcePage]:
    """Adapt current catalog-selected route documents using one owner connection.

    ``documents`` must already be the current catalog/root/run selection.  The
    adapter only calls the existing bounded ``_load_leading_text`` API on the
    route connection; it never opens original documents or a second route
    connection.  If ``require_catalog_match`` is true, a lookup returning
    ``None`` excludes that document rather than falling back to a stale alias.
    """

    if max_text_chars < 1:
        raise ValueError("max_text_chars must be positive")
    from neocortex.documents.document_catalog_text import _load_leading_text

    def source_iter() -> Iterator[CurationSource]:
        for document in documents:
            source_kind = str(_document_value(document, "source_kind", ""))
            file_key = str(_document_value(document, "file_key", ""))
            path = str(_document_value(document, "path", ""))
            if not source_kind or not file_key or not path:
                raise CurationSourceError("catalog source document lacks identity/path")
            text = _load_leading_text(
                connection,
                document,
                max_text_chars=max_text_chars,
                cancellation=cancellation,
            )
            metadata: dict[str, object] = {
                "title": str(_document_value(document, "title", "") or ""),
                "author": str(_document_value(document, "author", "") or ""),
                "processing_signature": str(_document_value(document, "processing_signature", "") or ""),
            }
            document_metadata = _document_value(document, "metadata", {})
            if isinstance(document_metadata, Mapping):
                # Current Catalog projections may carry owner-native binding
                # and classification fields.  Promote those bounded fields
                # without placing them in the semantic content view.
                metadata.update(dict(document_metadata))
            else:
                metadata["catalog_metadata"] = document_metadata
            signature = _document_value(document, "source_input_signature", None)
            if not isinstance(signature, str) or not signature.strip():
                signature = _document_value(document, "text_fingerprint", None)
            if not isinstance(signature, str) or not signature.strip():
                signature = _document_value(document, "content_signature", None)
            if not isinstance(signature, str) or not signature.strip():
                raise CurationSourceError("catalog route document lacks text/content signature")
            physical = PhysicalIdentity(
                str(_document_value(document, "volume_id", "catalog")),
                str(_document_value(document, "file_id", file_key)),
                _optional_int(_document_value(document, "birthtime_ns", None)),
            )
            source = CurationSource(
                source_kind=source_kind,
                file_key=file_key,
                path=path,
                physical_identity=physical,
                content_signature=signature,
                sections=(DerivedContent("route_leading_text", "leading", text),) if text else (),
                metadata=metadata,
                route_fence=route_fence,
                catalog_fence=catalog_fence,
                source_status=str(_document_value(document, "source_status", "complete")),
                coverage=str(_document_value(document, "coverage", "complete")),
            )
            if metadata_lookup is not None:
                extra = metadata_lookup(source)
                if extra is None and require_catalog_match:
                    continue
                if extra:
                    source = _merge_source_metadata(source, extra)
            elif require_catalog_match:
                raise CurationSourceError("require_catalog_match needs metadata_lookup")
            yield source

    yield from iter_curation_source_pages(source_iter(), page_size=page_size)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CurationSourceError("physical timestamp cannot be boolean")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CurationSourceError("physical timestamp is invalid") from exc


# Explicit names used by orchestration code; they all retain the same adapter.
iter_current_curation_sources = iter_curation_sources
iter_current_curation_source_pages = iter_curation_source_pages
iter_route_curation_sources = iter_route_curation_source_pages


__all__ = [
    "DEFAULT_CATALOG_TEXT_CHARS",
    "DEFAULT_SOURCE_PAGE_SIZE",
    "DEFAULT_SOURCE_SECTION_CHARS",
    "DEFAULT_SOURCE_SECTION_COUNT",
    "CurationSource",
    "CurationSourceAdapter",
    "CurationSourceError",
    "CurationSourceFenceError",
    "CurationSourceFences",
    "CurationSourcePage",
    "DerivedContent",
    "PhysicalIdentity",
    "curation_source_from_semantic_record",
    "iter_catalog_route_source_pages",
    "iter_curation_source_pages",
    "iter_curation_sources",
    "iter_current_curation_source_pages",
    "iter_current_curation_sources",
    "iter_route_curation_source_pages",
    "iter_route_curation_sources",
    "iter_semantic_record_pages",
]
