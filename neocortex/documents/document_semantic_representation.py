"""Small, deterministic document representations for Fast Curation.

This module is deliberately not a semantic-index implementation.  It turns
already-derived route text into one bounded, versioned content view (and, when
requested, at most three auxiliary views).  The content view never contains a
filesystem path or filename.  Path and physical identity are carried as
separate context/provenance so a physical rename does not invalidate content
inference.

The source contract is intentionally duck-typed at the boundary: callers may
pass route-owned section objects, but this module never opens an original
file.  A source's ``content_signature`` is the route/catalog owner's content
identity and is required to make changed content invalidate a representation.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field


# Match the Fast Curation service DTO and measured policy-bundle namespace.
REPRESENTATION_VERSION = "fast-curation-representation/v1"
MAX_REPRESENTATION_VIEWS = 4
MAX_REPRESENTATION_FRAGMENTS = 4

# Context keys must never leak into ``content_text``.  They remain available to
# a caller that explicitly wants structural evidence, but are not content
# embedding input by default.
_CONTEXT_KEYS = frozenset(
    {
        "path",
        "source_path",
        "original_path",
        "filename",
        "file_name",
        "logical_filename",
        "logical_name",
        "physical_identity",
        "volume_id",
        "file_id",
        "birthtime_ns",
        "source_fence",
        "catalog_fence",
    }
)
_TITLE_KEYS = ("title", "document_title", "source_title")
_AUTHOR_KEYS = ("author", "creator", "producer")
_HEADING_KEYS = ("headings", "heading", "outline", "sections")
_HEADING_LINE = re.compile(
    r"^\s*(?:(?:#{1,6})\s+|(?:\d+(?:\.\d+)*|[A-Z])(?:[.)]|\s+)\s+)\S+"
)


class RepresentationInputError(ValueError):
    """The source did not provide a bounded, identity-bound route input."""


@dataclass(frozen=True, slots=True)
class RepresentationBudgets:
    """Independent ceilings used while constructing one representation.

    ``max_chars`` and ``max_tokens`` apply to the complete content view;
    metadata, headings, fragments, source reads, context, and view count have
    independent limits.  The defaults are deliberately small compared with a
    full semantic index and no setting can produce more than four views or
    four representative fragments.
    """

    max_chars: int = 12_000
    max_tokens: int = 2_048
    max_metadata_chars: int = 2_000
    max_headings: int = 24
    max_fragments: int = MAX_REPRESENTATION_FRAGMENTS
    max_fragment_chars: int = 2_000
    max_context_chars: int = 2_000
    max_source_chars: int = 64_000
    max_sections: int = 64
    max_views: int = 1
    # Friendly aliases used by callers that phrase these as budgets.  They do
    # not create a second policy; when present they replace the corresponding
    # max_* value during validation.
    char_budget: int | None = None
    token_budget: int | None = None
    metadata_budget: int | None = None
    heading_budget: int | None = None
    fragment_budget: int | None = None

    def __post_init__(self) -> None:
        values = {
            "max_chars": self.char_budget if self.char_budget is not None else self.max_chars,
            "max_tokens": self.token_budget if self.token_budget is not None else self.max_tokens,
            "max_metadata_chars": (
                self.metadata_budget
                if self.metadata_budget is not None
                else self.max_metadata_chars
            ),
            "max_headings": self.heading_budget if self.heading_budget is not None else self.max_headings,
            "max_fragments": (
                self.fragment_budget if self.fragment_budget is not None else self.max_fragments
            ),
            "max_fragment_chars": self.max_fragment_chars,
            "max_context_chars": self.max_context_chars,
            "max_source_chars": self.max_source_chars,
            "max_sections": self.max_sections,
            "max_views": self.max_views,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if values["max_fragments"] > MAX_REPRESENTATION_FRAGMENTS:
            raise ValueError("max_fragments cannot exceed four")
        if values["max_views"] > MAX_REPRESENTATION_VIEWS:
            raise ValueError("max_views cannot exceed four")
        # Store aliases as the effective values so serialization and equality
        # cannot depend on which spelling the caller chose.
        object.__setattr__(self, "max_chars", values["max_chars"])
        object.__setattr__(self, "max_tokens", values["max_tokens"])
        object.__setattr__(self, "max_metadata_chars", values["max_metadata_chars"])
        object.__setattr__(self, "max_headings", values["max_headings"])
        object.__setattr__(self, "max_fragments", values["max_fragments"])
        for name in ("char_budget", "token_budget", "metadata_budget", "heading_budget", "fragment_budget"):
            object.__setattr__(self, name, None)

    def as_dict(self) -> dict[str, int]:
        return {
            "max_chars": self.max_chars,
            "max_tokens": self.max_tokens,
            "max_metadata_chars": self.max_metadata_chars,
            "max_headings": self.max_headings,
            "max_fragments": self.max_fragments,
            "max_fragment_chars": self.max_fragment_chars,
            "max_context_chars": self.max_context_chars,
            "max_source_chars": self.max_source_chars,
            "max_sections": self.max_sections,
            "max_views": self.max_views,
        }


@dataclass(frozen=True, slots=True)
class RepresentationView:
    """One bounded encoder view with an explicit role."""

    name: str
    text: str
    role: str = "content"

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.text.strip():
            raise ValueError("representation views need a name and non-empty text")
        if self.role not in {"content", "body", "tail", "context"}:
            raise ValueError("unsupported representation view role")

    @property
    def token_count(self) -> int:
        return _token_count(self.text)


@dataclass(frozen=True, slots=True)
class DocumentSemanticRepresentation:
    """Versioned, bounded semantic evidence consumed by Fast Curation.

    ``text`` is an alias-compatible name for the primary content view.  It is
    safe to send to a content encoder: it excludes path/filename context.
    ``context_text`` is advisory structural evidence and is never included in
    ``fingerprint``.  The fingerprint does include the route-owned
    ``content_signature`` so a changed source invalidates even when extraction
    happens to produce the same bounded prefix.
    """

    text: str
    fingerprint: str
    version: str
    provenance: Mapping[str, object]
    content_text: str = ""
    context_text: str = ""
    # The fast service consumes plain bounded strings.  ``view_records``
    # reconstructs role labels for callers that need them without making the
    # service know this module's DTO class.
    views: tuple[str, ...] = ()
    content_signature: str = ""
    headings: tuple[str, ...] = ()
    representative_fragments: tuple[str, ...] = ()
    document_id: str = ""
    source_kind: str = ""
    file_key: str = ""
    input_signature: str = ""
    path_context: str = ""
    title: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    opening: str = ""
    representative_body: str = ""
    conclusion: str = ""
    derived_sources: tuple[str, ...] = ()
    deterministic_evidence: Mapping[str, object] = field(default_factory=dict)
    structural_evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.content_text == "":
            object.__setattr__(self, "content_text", self.text)
        if self.text != self.content_text:
            raise ValueError("text and content_text must identify the primary content view")
        if not self.text.strip():
            raise ValueError("representation content cannot be empty")
        if (
            not self.version.strip()
            or len(self.fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.fingerprint)
        ):
            raise ValueError("representation version/fingerprint is invalid")
        if len(self.views) > MAX_REPRESENTATION_VIEWS:
            raise ValueError("representation cannot contain more than four views")
        if self.views and self.views[0] != self.text:
            raise ValueError("the first representation view must be the primary content view")
        if any(not isinstance(view, str) or not view.strip() for view in self.views):
            raise ValueError("representation views must contain non-empty strings")
        if len(self.representative_fragments) > MAX_REPRESENTATION_FRAGMENTS:
            raise ValueError("representation cannot contain more than four fragments")
        if len(set(self.headings)) != len(self.headings):
            raise ValueError("representation headings must be unique")
        if not isinstance(self.provenance, Mapping):
            raise ValueError("representation provenance must be a mapping")
        if not isinstance(self.deterministic_evidence, Mapping):
            raise ValueError("deterministic_evidence must be a mapping")
        if not isinstance(self.structural_evidence, Mapping):
            raise ValueError("structural_evidence must be a mapping")

    @property
    def view_records(self) -> tuple[RepresentationView, ...]:
        names = ("content", "body", "tail", "context")
        roles = ("content", "body", "tail", "context")
        return tuple(
            RepresentationView(name, text, role)
            for name, role, text in zip(names, roles, self.views, strict=False)
        )

    @property
    def content_fingerprint(self) -> str:
        """Stable fingerprint used by content-embedding caches."""

        return self.fingerprint

    @property
    def fingerprint_with_algorithm(self) -> str:
        """Diagnostic spelling when an algorithm-qualified digest is needed."""

        return "sha256:" + self.fingerprint

    @property
    def representation_text(self) -> str:
        return self.text

    @property
    def representation_version(self) -> str:
        """Compatibility spelling expected by the Fast Curation service."""

        return self.version

    @property
    def embedding_text(self) -> str:
        """Primary content-only encoder input; path context is separate."""

        return self.content_text

    @property
    def content_embedding_text(self) -> str:
        return self.content_text

    @property
    def token_count(self) -> int:
        return _token_count(self.text)

    @property
    def view_texts(self) -> tuple[str, ...]:
        return tuple(self.views)

    @property
    def representation_fingerprint(self) -> str:
        """Compatibility spelling used by Fast Curation evidence."""

        return self.fingerprint

    def as_dict(self) -> dict[str, object]:
        """Return a bounded structural payload without raw source bytes."""

        return {
            "schema": "neocortex.document-semantic-representation/v1",
            "document_id": self.document_id,
            "source_kind": self.source_kind,
            "file_key": self.file_key,
            "input_signature": self.input_signature or self.content_signature,
            "content_fingerprint": self.fingerprint,
            "representation_version": self.version,
            "path_context": self.path_context or self.context_text,
            "text_chars": len(self.text),
            "view_count": len(self.views),
            "heading_count": len(self.headings),
        }


@dataclass(frozen=True, slots=True)
class _SourceLike:
    """Protocol-shaped documentation helper for static readers."""

    source_kind: str
    file_key: str
    path: str
    content_signature: str
    sections: tuple[object, ...]
    metadata: Mapping[str, object]


def _source_value(source: object, name: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _normalise_text(value: object, *, max_chars: int | None = None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = unicodedata.normalize("NFKC", value)
    clean: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("C") and character not in {"\n", "\t", "\r"}:
            continue
        clean.append(character)
    text = "".join(clean).replace("\r\n", "\n").replace("\r", "\n")
    # Preserve paragraph boundaries while collapsing accidental route noise.
    text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    text = "\n".join(line for line in text.split("\n") if line.strip())
    text = text.strip()
    if max_chars is not None:
        return text[:max_chars]
    return text


def _token_count(text: str) -> int:
    return len(re.findall(r"\S+", text, flags=re.UNICODE))


def _fit_tokens(text: str, maximum: int) -> str:
    if _token_count(text) <= maximum:
        return text
    pieces = re.findall(r"\S+", text, flags=re.UNICODE)
    return " ".join(pieces[:maximum])


def _fit_text(text: str, *, max_chars: int, max_tokens: int) -> str:
    text = _normalise_text(text)
    text = text[:max_chars]
    text = _fit_tokens(text, max_tokens)
    return text[:max_chars].rstrip()


def _mapping_value(mapping: Mapping[str, object], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return _normalise_text(value)
    return ""


def _section_parts(source: object, budgets: RepresentationBudgets) -> tuple[tuple[str, str, Mapping[str, object]], ...]:
    raw_sections = _source_value(source, "sections", ())
    if raw_sections is None:
        return ()
    result: list[tuple[str, str, Mapping[str, object]]] = []
    for ordinal, section in enumerate(raw_sections):
        if ordinal >= budgets.max_sections:
            break
        if isinstance(section, str):
            kind, identifier, value, provenance = "derived", str(ordinal), section, {}
        elif isinstance(section, Mapping):
            kind = str(section.get("section_kind", section.get("kind", "derived")))
            identifier = str(section.get("section_id", section.get("id", ordinal)))
            value = section.get("text", "")
            provenance = section.get("provenance", {})
        else:
            kind = str(getattr(section, "section_kind", "derived"))
            identifier = str(getattr(section, "section_id", ordinal))
            value = getattr(section, "text", "")
            provenance = getattr(section, "provenance", {})
        text = _normalise_text(value, max_chars=budgets.max_source_chars)
        if not text:
            continue
        if not isinstance(provenance, Mapping):
            provenance = {}
        result.append((kind, identifier, dict(provenance)))
        # Replace the last tuple with text retained separately below.  Keeping
        # this local representation explicit avoids depending on a route DTO.
        result[-1] = (kind, identifier, {"text": text, **dict(provenance)})
    return tuple(result)


def _extract_headings(sections: Sequence[tuple[str, str, Mapping[str, object]]], metadata: Mapping[str, object], budgets: RepresentationBudgets) -> tuple[str, ...]:
    candidates: list[str] = []
    for key in _HEADING_KEYS:
        value = metadata.get(key)
        if isinstance(value, str):
            values: Iterable[object] = value.splitlines()
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            values = value
        else:
            values = ()
        for item in values:
            heading = _normalise_text(item, max_chars=256)
            if heading:
                candidates.append(heading)
    for _kind, _identifier, provenance in sections:
        explicit = provenance.get("headings")
        if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes, bytearray)):
            candidates.extend(_normalise_text(item, max_chars=256) for item in explicit)
    for _kind, _identifier, provenance in sections:
        text = str(provenance.get("text", ""))
        for line in text.splitlines():
            line = _normalise_text(line, max_chars=256)
            if line and _HEADING_LINE.match(line):
                candidates.append(line.lstrip("# "))
    result: list[str] = []
    seen: set[str] = set()
    for heading in candidates:
        if heading and heading not in seen:
            seen.add(heading)
            result.append(heading)
            if len(result) >= budgets.max_headings:
                break
    return tuple(result)


def _metadata_fields(metadata: Mapping[str, object], budgets: RepresentationBudgets) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, str]] = []
    remaining = budgets.max_metadata_chars
    for label, keys in (("TITLE", _TITLE_KEYS), ("AUTHOR", _AUTHOR_KEYS), ("SUBJECT", ("subject",))):
        value = _mapping_value(metadata, keys)
        if not value or remaining <= 0:
            continue
        value = value[:remaining]
        fields.append((label, value))
        remaining -= len(value)
    # Other metadata is structural unless explicitly classified as semantic
    # document metadata.  Never promote arbitrary path-like keys into content.
    return tuple(fields)


def _bounded_metadata(metadata: Mapping[str, object], budgets: RepresentationBudgets) -> dict[str, object]:
    """Retain structural metadata under the independent metadata ceiling."""

    result: dict[str, object] = {}
    remaining = budgets.max_metadata_chars
    for original_key, value in sorted(metadata.items(), key=lambda item: str(item[0])):
        if len(result) >= 32 or remaining <= 0:
            break
        key = str(original_key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            selected: object = value
            encoded_size = len(str(value).encode("utf-8"))
        elif isinstance(value, Mapping):
            selected = {
                str(child_key): str(child_value)[:256]
                for child_key, child_value in list(value.items())[:16]
            }
            encoded_size = len(json.dumps(selected, ensure_ascii=False, sort_keys=True))
        elif isinstance(value, (list, tuple)):
            selected = [str(item)[:256] for item in value[:16]]
            encoded_size = len(json.dumps(selected, ensure_ascii=False))
        else:
            selected = str(value)[:512]
            encoded_size = len(str(selected).encode("utf-8"))
        if encoded_size > remaining:
            if isinstance(selected, str):
                selected = selected[:remaining]
            else:
                break
        result[key[:256]] = selected
        remaining -= min(remaining, encoded_size)
    return result


def _text_from_sections(sections: Sequence[tuple[str, str, Mapping[str, object]]]) -> tuple[str, ...]:
    return tuple(str(provenance.get("text", "")) for _kind, _identifier, provenance in sections)


def _paragraphs(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for paragraph in re.split(r"\n{2,}|(?<=\.)\s{2,}", text):
        normalized = _normalise_text(paragraph)
        if normalized:
            values.append(normalized)
    return tuple(values)


def _select_fragments(section_texts: Sequence[str], budgets: RepresentationBudgets) -> tuple[str, ...]:
    paragraphs: list[str] = []
    for text in section_texts:
        paragraphs.extend(_paragraphs(text))
    if not paragraphs:
        return ()
    selected: list[str] = []
    # Deterministic spread: opening, then evenly spaced body paragraphs, then
    # tail.  The count is capped at four by RepresentationBudgets.
    count = min(budgets.max_fragments, len(paragraphs))
    if count == 1:
        indexes = (0,)
    elif count == 2:
        indexes = (0, len(paragraphs) - 1)
    else:
        indexes = tuple(round(index * (len(paragraphs) - 1) / (count - 1)) for index in range(count))
    seen: set[int] = set()
    for index in indexes:
        if index in seen:
            continue
        seen.add(index)
        selected.append(_fit_text(paragraphs[index], max_chars=budgets.max_fragment_chars, max_tokens=budgets.max_tokens))
    return tuple(fragment for fragment in selected if fragment)


def _context_text(source: object, metadata: Mapping[str, object], budgets: RepresentationBudgets) -> str:
    path = _normalise_text(_source_value(source, "path", ""), max_chars=512)
    logical = _mapping_value(metadata, ("logical_filename", "logical_name", "filename", "file_name"))
    source_kind = _normalise_text(_source_value(source, "source_kind", ""), max_chars=64)
    values: list[str] = []
    if source_kind:
        values.append(f"[SOURCE_KIND] {source_kind}")
    if logical:
        values.append(f"[LOGICAL_FILENAME] {logical}")
    if path:
        values.append(f"[ORIGINAL_PATH] {path}")
    physical = _source_value(source, "physical_identity", None)
    if physical:
        if isinstance(physical, Mapping):
            physical_value = json.dumps(
                dict(physical), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        else:
            physical_value = str(physical)
        values.append(f"[PHYSICAL_IDENTITY] {_normalise_text(physical_value, max_chars=256)}")
    return _fit_text("\n".join(values), max_chars=budgets.max_context_chars, max_tokens=max(1, budgets.max_context_chars // 2))


def _source_content_signature(source: object) -> str:
    value = _source_value(source, "content_signature", None)
    if value is None:
        value = _source_value(source, "input_signature", None)
    if not isinstance(value, str) or not value.strip():
        raise RepresentationInputError("route source requires a non-empty content_signature")
    return value.strip()


def _fingerprint_payload(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    # Cache owners store the canonical lowercase digest without a scheme
    # prefix; the representation exposes ``fingerprint_with_algorithm`` for
    # receipts that need an explicit algorithm label.
    return hashlib.sha256(encoded).hexdigest()


def build_document_semantic_representation(
    source: object,
    *,
    budgets: RepresentationBudgets | None = None,
    version: str = REPRESENTATION_VERSION,
) -> DocumentSemanticRepresentation:
    """Build one bounded representation from route-derived source content.

    The caller is responsible for validating route/catalog fences immediately
    before using the result.  This pure function performs no filesystem,
    SQLite, model, network, or corpus access.
    """

    if not isinstance(version, str) or not version.strip():
        raise ValueError("representation version cannot be blank")
    budgets = budgets or RepresentationBudgets()
    source_kind = _normalise_text(_source_value(source, "source_kind", ""), max_chars=64)
    file_key = _normalise_text(_source_value(source, "file_key", ""), max_chars=512)
    path = _normalise_text(_source_value(source, "path", ""), max_chars=4_096)
    if not source_kind or not file_key or not path:
        raise RepresentationInputError("source_kind, file_key and path are required")
    content_signature = _source_content_signature(source)
    raw_metadata = _source_value(source, "metadata", {})
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    sections = _section_parts(source, budgets)
    if not sections and not _metadata_fields(metadata, budgets):
        raise RepresentationInputError("route source has no bounded derived content")

    metadata_fields = _metadata_fields(metadata, budgets)
    headings = _extract_headings(sections, metadata, budgets)
    section_texts = _text_from_sections(sections)
    fragments = _select_fragments(section_texts, budgets)
    opening = _fit_text(section_texts[0] if section_texts else "", max_chars=budgets.max_fragment_chars, max_tokens=budgets.max_tokens)
    tail = _fit_text(section_texts[-1][-budgets.max_fragment_chars :] if section_texts else "", max_chars=budgets.max_fragment_chars, max_tokens=budgets.max_tokens)

    parts: list[str] = []
    for label, value in metadata_fields:
        parts.append(f"[{label}]\n{value}")
    if headings:
        parts.append("[HEADINGS]\n" + "\n".join(headings))
    if opening:
        parts.append("[OPENING]\n" + opening)
    if fragments:
        parts.append("[REPRESENTATIVE_CONTENT]\n" + "\n".join(fragments))
    if tail and tail != opening:
        parts.append("[CONCLUSION]\n" + tail)
    content_text = _fit_text("\n\n".join(parts), max_chars=budgets.max_chars, max_tokens=budgets.max_tokens)
    if not content_text:
        raise RepresentationInputError("route source produced an empty bounded representation")
    context_text = _context_text(source, metadata, budgets)
    bounded_metadata = _bounded_metadata(metadata, budgets)

    view_records: list[RepresentationView] = [RepresentationView("content", content_text, "content")]
    if budgets.max_views >= 2 and opening:
        view_records.append(RepresentationView("body", _fit_text("\n".join(fragments or (opening,)), max_chars=budgets.max_chars, max_tokens=budgets.max_tokens), "body"))
    if budgets.max_views >= 3 and tail and tail != opening:
        view_records.append(RepresentationView("tail", tail, "tail"))
    if budgets.max_views >= 4 and context_text:
        view_records.append(RepresentationView("context", context_text, "context"))

    provenance = {
        "schema": "neocortex.document-semantic-representation/v1",
        "version": version,
        "source_kind": source_kind,
        "file_key": file_key,
        "path": path,
        "content_signature": content_signature,
        "content_embedding_excludes_path": True,
        "context_separate": True,
        "source_fence_required": True,
        "budgets": budgets.as_dict(),
        "selected": {
            "metadata_fields": tuple(label for label, _value in metadata_fields),
            "heading_count": len(headings),
            "fragment_count": len(fragments),
            "view_names": tuple(view.name for view in view_records),
        },
    }
    fingerprint = _fingerprint_payload(
        {
            "version": version,
            "content_signature": content_signature,
            "text": content_text,
            "views": tuple((view.name, view.text) for view in view_records if view.role != "context"),
        }
    )
    return DocumentSemanticRepresentation(
        text=content_text,
        content_text=content_text,
        context_text=context_text,
        fingerprint=fingerprint,
        version=version,
        provenance=provenance,
        views=tuple(view.text for view in view_records),
        content_signature=content_signature,
        headings=headings,
        representative_fragments=fragments,
        document_id=file_key,
        source_kind=source_kind,
        file_key=file_key,
        input_signature=content_signature,
        path_context=path,
        title=next((value for label, value in metadata_fields if label == "TITLE"), ""),
        metadata=bounded_metadata,
        opening=opening,
        representative_body="\n".join(fragments),
        conclusion=tail,
        derived_sources=tuple(kind for kind, _identifier, _provenance in sections),
    )


# Short names are intentionally aliases, not separate implementations.  The
# fast curation service can use the descriptive name while callers migrating
# from a design document can use the compact one.
build_document_representation = build_document_semantic_representation
make_document_semantic_representation = build_document_semantic_representation


__all__ = [
    "MAX_REPRESENTATION_FRAGMENTS",
    "MAX_REPRESENTATION_VIEWS",
    "REPRESENTATION_VERSION",
    "DocumentSemanticRepresentation",
    "RepresentationBudgets",
    "RepresentationInputError",
    "RepresentationView",
    "build_document_representation",
    "build_document_semantic_representation",
    "make_document_semantic_representation",
]
