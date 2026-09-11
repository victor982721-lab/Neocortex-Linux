"""Direct, revision-bound document evidence reads from published owners.

No query compilation, MATCH, ranking, embedding, arbitrary corpus path or new
database is involved. Unsupported owner locators abstain instead of rerunning
the user's search and silently rebinding a citation alias.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from neocortex.knowledge.knowledge_contracts import KnowledgeHit
from neocortex.knowledge.knowledge_search import _LEXICAL_OWNER_FORMATS, _candidate_from_resolved
from neocortex.knowledge.knowledge_snapshot import (
    _OWNER_VALIDATORS, _logical_observation, _owner_spec,
)
from neocortex.persistence.sqlite_immutable import preferred_sqlite_read_mode
from neocortex.semantic.semantic_models import EmbeddingModality, ResolvedSearchHit, SearchHit
from neocortex.semantic.semantic_schema import SemanticReadContext, semantic_database, semantic_read_context
from neocortex.semantic.semantic_search_repository import resolve_search_hits
from neocortex.semantic.semantic_sources import TEXT_SOURCE_KINDS

_SQLITE_MAX_INTEGER = (1 << 63) - 1


class EvidenceLookupError(ValueError):
    """A typed inability to prove a supplied immutable reference."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical_records(records: object) -> list[str]:
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise EvidenceLookupError("invalid_evidence_reference")
    return sorted(json.dumps(item, sort_keys=True, allow_nan=False) for item in records)


def _observe_owner(connection: sqlite3.Connection, owner: str) -> dict[str, Any]:
    validate, legacy = _OWNER_VALIDATORS[owner]
    validate(connection)
    observation = _logical_observation(connection, _owner_spec(owner, validate, legacy))
    return {
        "owner": owner,
        "publications": [item.to_dict() for item in observation.publications],
        "watermarks": [item.to_dict() for item in observation.watermarks],
    }


def _owner_revision(row: sqlite3.Row, owner: str) -> dict[str, object]:
    revision: dict[str, object] = {
        name: row[name]
        for name in ("size", "mtime_ns", "birthtime_ns", "processing_signature", "last_seen_run_id")
        if name in row.keys()
    }
    if owner == "text" and row["revision_id"] is not None:
        revision["revision_id"] = row["revision_id"]
    if owner == "pdf":
        revision["is_partial"] = bool(row["is_partial"])
    if owner == "video":
        for name in (
            "frame_count",
            "ocr_frame_count",
            "ocr_text_chars",
            "audio_file_key",
            "audio_processing_signature",
            "audio_status",
        ):
            if name in row.keys():
                revision[name] = row[name]
    if owner == "image":
        if "ocr_text_chars" in row.keys():
            revision["ocr_text_chars"] = row["ocr_text_chars"]
        if "ocr_text_truncated" in row.keys():
            revision["ocr_text_truncated"] = bool(row["ocr_text_truncated"])
        if "processing_signature" in row.keys():
            revision["processing_signature"] = row["processing_signature"] or "unprocessed"
    if owner == "code" and "version_id" in row.keys():
        revision["version_id"] = row["version_id"]
    return revision


def _owner_record(
    connection: sqlite3.Connection, owner: str, source_kind: str, file_key: str,
) -> sqlite3.Row:
    statuses = {
        "text": {"complete"}, "pdf": {"done", "partial"},
        "docx": {"complete", "partial"}, "office": {"complete"}, "archive": {"indexed"},
        "audio": {"complete", "no_speech"}, "video": {"complete", "partial"},
        "image": {"done", "partial"},
        "code": {"current"},
    }
    if owner == "archive":
        rows = connection.execute(
            """SELECT d.*,c.status AS container_status,c.path AS current_container_path,
                      c.mtime_ns AS container_mtime_ns,c.birthtime_ns AS container_birthtime_ns,
                      c.processing_signature AS container_processing_signature,
                      c.last_seen_run_id AS container_last_seen_run_id
               FROM documents d JOIN containers c ON c.container_key=d.container_key
               WHERE d.file_key=? LIMIT 2""", (file_key,),
        ).fetchall()
    elif owner == "image":
        rows = connection.execute(
            "SELECT * FROM images WHERE file_key=? LIMIT 2", (file_key,),
        ).fetchall()
    elif owner == "code":
        volume, separator, physical_file = file_key.partition(":")
        if not separator or not volume or not physical_file:
            raise EvidenceLookupError("invalid_evidence_reference")
        rows = connection.execute(
            """SELECT f.volume_id,f.physical_file_id,f.current_path AS path,
                      f.status,f.last_seen_run_id,v.version_id,v.size,v.mtime_ns,
                      v.birthtime_ns,v.processing_signature,v.analysis_status,
                      v.text_chars,v.language
               FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
               WHERE f.volume_id=? AND f.physical_file_id=? AND f.status='current'
                 AND v.invalidated_ns IS NULL LIMIT 2""",
            (volume, physical_file),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM documents WHERE file_key=? LIMIT 2", (file_key,),
        ).fetchall()
    if len(rows) != 1 or rows[0]["status"] not in statuses[owner]:
        raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
    row = rows[0]
    if owner == "office" and row["format"] != source_kind:
        raise EvidenceLookupError("owner_revision_changed")
    if owner == "audio" and source_kind != "audio":
        raise EvidenceLookupError("owner_revision_changed")
    if owner == "video" and source_kind != "video":
        raise EvidenceLookupError("owner_revision_changed")
    if owner == "image" and source_kind not in {"image", "image_ocr"}:
        raise EvidenceLookupError("owner_revision_changed")
    if owner == "code" and source_kind != "code":
        raise EvidenceLookupError("owner_revision_changed")
    if owner == "archive":
        if row["container_status"] not in {"complete", "partial"}:
            raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
        if row["container_path"] != row["current_container_path"] or any(
            row[name] != row[f"container_{name}"]
            for name in ("mtime_ns", "birthtime_ns", "processing_signature", "last_seen_run_id")
        ):
            raise EvidenceLookupError("owner_revision_changed")
    return row


def _row_has(row: sqlite3.Row, name: str) -> bool:
    return name in row.keys()


def _row_value(row: sqlite3.Row, name: str) -> object | None:
    return row[name] if _row_has(row, name) else None


def _archive_locator_value(
    resolved: ResolvedSearchHit,
    name: str,
) -> object | None:
    provenance = resolved.section_provenance
    value = provenance.get(name)
    if value is not None:
        return value
    nested = provenance.get("locator")
    return nested.get(name) if isinstance(nested, Mapping) else None


def _archive_inside_zip_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value in {"0", "1"}:
        return value == "1"
    raise EvidenceLookupError("evidence_locator_changed")


def _archive_row_is_physical_root(row: sqlite3.Row) -> bool:
    member_chain = _row_value(row, "member_chain")
    member_path = _row_value(row, "member_path")
    path = _row_value(row, "path")
    container_path = _row_value(row, "container_path")
    role = _row_value(row, "document_role")
    logical_chain = _row_value(row, "logical_document_chain")
    raw_depth = _row_value(row, "archive_depth")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (path, container_path)
    ):
        return False
    if isinstance(raw_depth, bool) or not isinstance(raw_depth, (int, str)):
        return False
    try:
        depth = int(raw_depth)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        member_chain == ""
        and member_path == ""
        and depth == 0
        and path == container_path
        and (not _row_has(row, "document_role") or role == "logical_document")
        and (
            not _row_has(row, "logical_document_chain")
            or logical_chain == ""
        )
    )


def _validate_archive_owner_locator(
    row: sqlite3.Row,
    resolved: ResolvedSearchHit,
) -> None:
    """Validate Archive root/member role, exact chain, depth and path."""

    physical_root = _archive_row_is_physical_root(row)
    if physical_root:
        if (resolved.section_kind, resolved.section_id) != ("archive_document", "body"):
            raise EvidenceLookupError("evidence_locator_changed")
        expected_path = _row_value(row, "path")
        if (
            not isinstance(expected_path, str)
            or not expected_path.strip()
            or resolved.path != expected_path
        ):
            raise EvidenceLookupError("evidence_locator_changed")
        supplied_inside_zip = _archive_locator_value(resolved, "inside_zip")
        if supplied_inside_zip is not None and _archive_inside_zip_value(supplied_inside_zip):
            raise EvidenceLookupError("evidence_locator_changed")
    else:
        if (
            resolved.section_kind != "archive_member"
            or resolved.section_id != _row_value(row, "member_chain")
        ):
            raise EvidenceLookupError("evidence_locator_changed")
        expected_path = _row_value(row, "path")
        if (
            not isinstance(expected_path, str)
            or not expected_path.strip()
            or resolved.path != expected_path
        ):
            raise EvidenceLookupError("evidence_locator_changed")
        supplied_inside_zip = _archive_locator_value(resolved, "inside_zip")
        if supplied_inside_zip is not None and not _archive_inside_zip_value(supplied_inside_zip):
            raise EvidenceLookupError("evidence_locator_changed")

    for name in (
        "container_key",
        "container_path",
        "member_chain",
        "member_path",
        "archive_depth",
        "content_kind",
        "media_type",
        "container_status",
        "document_role",
        "logical_document_chain",
    ):
        if not _row_has(row, name):
            continue
        expected = _row_value(row, name)
        supplied = _archive_locator_value(resolved, name)
        # Older member projections predate the role/chain columns and remain
        # valid when their owner still proves the member path and chain.  A
        # modern physical root must carry its explicit role and empty logical
        # chain, so those fields stay strict for ``archive_document``.
        if (
            not physical_root
            and name in {"document_role", "logical_document_chain"}
            and supplied is None
        ):
            continue
        if expected is None:
            if supplied is not None:
                raise EvidenceLookupError("evidence_locator_changed")
        elif supplied != expected:
            raise EvidenceLookupError("evidence_locator_changed")


def _validate_owner_locator(
    connection: sqlite3.Connection, owner: str, row: sqlite3.Row, resolved: ResolvedSearchHit,
) -> None:
    """Require the same route-owned section, without reconstructing content."""
    if owner == "office":
        if (resolved.section_kind, resolved.section_id) != (f"{resolved.source_kind}_document", "body"):
            raise EvidenceLookupError("unsupported_evidence_lookup")
    elif owner == "docx":
        if (resolved.section_kind, resolved.section_id) == ("docx_document", "body"):
            if connection.execute(
                "SELECT 1 FROM document_parts WHERE file_key=? LIMIT 1", (row["file_key"],),
            ).fetchone() is not None:
                raise EvidenceLookupError("evidence_locator_changed")
        else:
            parts = connection.execute(
                "SELECT part_kind FROM document_parts WHERE file_key=? AND part_name=? LIMIT 2",
                (row["file_key"], resolved.section_id),
            ).fetchall()
            if len(parts) != 1 or resolved.section_kind != f"docx_{parts[0]['part_kind']}":
                raise EvidenceLookupError("evidence_locator_changed")
    elif owner == "archive":
        _validate_archive_owner_locator(row, resolved)
    elif owner == "audio":
        if resolved.section_kind != "audio_segment" or resolved.section_id is None:
            raise EvidenceLookupError("unsupported_evidence_lookup")
        try:
            segment_index = int(resolved.section_id)
        except (TypeError, ValueError):
            raise EvidenceLookupError("invalid_evidence_reference") from None
        segment = connection.execute(
            "SELECT start_ms,end_ms,text FROM segments "
            "WHERE file_key=? AND segment_index=? LIMIT 2",
            (row["file_key"], segment_index),
        ).fetchall()
        if len(segment) != 1:
            raise EvidenceLookupError("evidence_locator_changed")
        actual = segment[0]
        provenance = resolved.section_provenance
        locator = provenance.get("locator") if isinstance(provenance, Mapping) else None
        locator = locator if isinstance(locator, Mapping) else {}
        for name in ("start_ms", "end_ms"):
            expected = actual[name]
            supplied = provenance.get(name, locator.get(name))
            if supplied is not None and supplied != expected:
                raise EvidenceLookupError("evidence_locator_changed")
    elif owner == "video":
        if resolved.section_kind != "video_frame_ocr" or resolved.section_id is None:
            # Linked audio transcripts are owned by the audio database and are
            # not replayable through a video owner reference.
            raise EvidenceLookupError("unsupported_evidence_lookup")
        try:
            frame_index = int(resolved.section_id)
        except (TypeError, ValueError):
            raise EvidenceLookupError("invalid_evidence_reference") from None
        rows = connection.execute(
            "SELECT timestamp_ms,content_xxh3_128,ocr_available,ocr_text "
            "FROM frames WHERE file_key=? AND frame_index=? LIMIT 2",
            (row["file_key"], frame_index),
        ).fetchall()
        if len(rows) != 1 or not bool(rows[0]["ocr_available"]):
            raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
        actual = rows[0]
        provenance = resolved.section_provenance
        locator = provenance.get("locator") if isinstance(provenance, Mapping) else None
        locator = locator if isinstance(locator, Mapping) else {}
        supplied_timestamp = provenance.get("timestamp_ms", locator.get("timestamp_ms"))
        if supplied_timestamp is not None and supplied_timestamp != actual["timestamp_ms"]:
            raise EvidenceLookupError("evidence_locator_changed")
        if provenance.get("content_xxh3_128") is not None and (
            provenance.get("content_xxh3_128") != actual["content_xxh3_128"]
        ):
            raise EvidenceLookupError("evidence_locator_changed")
    elif owner == "image":
        if resolved.section_kind != "image_ocr" or resolved.section_id != "ocr":
            raise EvidenceLookupError("unsupported_evidence_lookup")
        if row["ocr_text_zlib"] is None or row["ocr_text_chars"] is None:
            raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
    elif owner == "code":
        if not resolved.section_kind or not resolved.section_kind.startswith("code_"):
            raise EvidenceLookupError("unsupported_evidence_lookup")
        if resolved.section_id is None or not resolved.section_id.isdecimal():
            raise EvidenceLookupError("invalid_evidence_reference")
        rows = connection.execute(
            """SELECT start_line,end_line,symbol_id,text FROM code_chunks
               WHERE version_id=? AND chunk_index=? LIMIT 2""",
            (row["version_id"], int(resolved.section_id)),
        ).fetchall()
        if len(rows) != 1:
            raise EvidenceLookupError("evidence_locator_changed")
        actual = rows[0]
        provenance = resolved.section_provenance
        for name in ("start_line", "end_line"):
            if provenance.get(name) is not None and provenance.get(name) != actual[name]:
                raise EvidenceLookupError("evidence_locator_changed")


def _semantic_fragment(
    resolved: ResolvedSearchHit, locator: Mapping[str, Any], *, chunk_chars: int,
) -> tuple[ResolvedSearchHit, dict[str, object]]:
    """Bind only an explicit range within this immutable, resolved chunk."""
    chunk_start, chunk_end = resolved.start_char, resolved.end_char
    requested_start, requested_end = locator.get("start_char"), locator.get("end_char")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (
        chunk_start, chunk_end, requested_start, requested_end,
    )):
        raise EvidenceLookupError("invalid_evidence_reference")
    # Narrowing above is intentionally followed by assertions for static readers.
    assert isinstance(chunk_start, int) and isinstance(chunk_end, int)
    assert isinstance(requested_start, int) and isinstance(requested_end, int)
    if not chunk_start <= requested_start < requested_end <= chunk_end:
        raise EvidenceLookupError("evidence_range_unavailable")
    if not 0 < chunk_chars <= chunk_end - chunk_start or len(resolved.snippet or "") != min(chunk_chars, 4096):
        raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
    page = (
        int(resolved.section_id)
        if resolved.source_kind == "pdf" and resolved.section_id is not None
        and resolved.section_id.isascii() and resolved.section_id.isdecimal()
        else None
    )
    section_kind = "pdf_page" if page is not None else resolved.section_kind
    if any(locator.get(name) != actual for name, actual in (
        ("page", page), ("section_kind", section_kind), ("section_id", resolved.section_id),
    )):
        raise EvidenceLookupError("evidence_locator_changed")
    actual_provenance = resolved.section_provenance
    nested_provenance = actual_provenance.get("locator")
    nested_provenance = nested_provenance if isinstance(nested_provenance, Mapping) else {}
    for name in (
        "start_line",
        "end_line",
        "sheet",
        "cell_range",
        "start_ms",
        "end_ms",
        "symbol",
        "coordinate_space",
        "bounding_box",
    ):
        if name not in locator:
            continue
        actual = actual_provenance.get(name, nested_provenance.get(name))
        supplied = locator.get(name)
        if name == "bounding_box" and isinstance(actual, tuple):
            actual = list(actual)
        if supplied != actual:
            raise EvidenceLookupError("evidence_locator_changed")
    full_chunk_requested = (requested_start, requested_end) == (chunk_start, chunk_end)
    # Chunks collapse whitespace: their normalized text need not share the
    # source section's character coordinates. Never invent that missing map.
    if not full_chunk_requested and chunk_chars != chunk_end - chunk_start:
        raise EvidenceLookupError("evidence_range_unavailable")
    normalized_start = requested_start - chunk_start
    normalized_end = chunk_chars if full_chunk_requested else requested_end - chunk_start
    available_end = len(resolved.snippet or "")
    if normalized_start >= available_end or (normalized_end > available_end and not full_chunk_requested):
        raise EvidenceLookupError("evidence_range_unavailable")
    returned_end = min(normalized_end, available_end)
    snippet = (resolved.snippet or "")[normalized_start:returned_end]
    extent: dict[str, object] = {
        "units": "characters",
        "exact_reference_range": {"start_char": requested_start, "end_char": requested_end, "basis": "source_section"},
        "chunk_range": {"start_char": chunk_start, "end_char": chunk_end, "basis": "source_section"},
        "returned_range": {"start_char": normalized_start, "end_char": returned_end, "basis": "normalized_chunk"},
        "bounded": returned_end < normalized_end,
    }
    return replace(resolved, start_char=requested_start, end_char=requested_end, snippet=snippet), extent


def _lexical_range(
    owner: str,
    locator: Mapping[str, Any],
    *,
    page: int | None,
    total: int,
) -> tuple[int, int]:
    """Return a source-backed lexical range, never an arbitrary prefix."""
    expected_kind = (
        "pdf_page"
        if owner == "pdf"
        else "transcript"
        if owner == "audio"
        else "document"
    )
    expected_id = str(page) if owner == "pdf" else "fulltext"
    if locator.get("section_kind") != expected_kind or locator.get("section_id") != expected_id:
        raise EvidenceLookupError("evidence_locator_changed")
    if owner == "pdf":
        if locator.get("page") != page:
            raise EvidenceLookupError("evidence_locator_changed")
    elif locator.get("page") is not None:
        raise EvidenceLookupError("evidence_locator_changed")
    if any(name in locator and locator.get(name) is not None for name in (
        "start_line", "end_line", "sheet", "cell_range", "start_ms", "end_ms",
        "bounding_box", "coordinate_space", "symbol",
    )):
        raise EvidenceLookupError("evidence_locator_changed")
    present = [name for name in ("start_char", "end_char") if name in locator]
    if present and len(present) != 2:
        raise EvidenceLookupError("invalid_evidence_reference")
    if not present or (
        locator.get("start_char") is None and locator.get("end_char") is None
    ):
        if total <= 0 or total > 4096:
            raise EvidenceLookupError("evidence_range_unavailable")
        return 0, total
    start, end = locator.get("start_char"), locator.get("end_char")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
        raise EvidenceLookupError("invalid_evidence_reference")
    assert isinstance(start, int) and isinstance(end, int)
    if not 0 <= start < end <= total:
        raise EvidenceLookupError("evidence_range_unavailable")
    if end - start > 4096:
        raise EvidenceLookupError("evidence_range_unavailable")
    return start, end


def _lexical_extent(
    row: sqlite3.Row,
    owner: str,
    snippet: str,
    page: int | None,
    *,
    start_char: int,
    end_char: int,
) -> dict[str, object]:
    total = row["evidence_total_chars"]
    if not isinstance(total, int) or len(snippet) != end_char - start_char:
        raise EvidenceLookupError("owner_evidence_extent_unavailable")
    extent: dict[str, object] = {
        "units": "characters",
        # The lexical body is the raw, locatable owner section (the document
        # fulltext or one PDF page), not a whitespace-normalized model chunk.
        # SQL reads the exact requested range and its total length from the same row.
        "exact_reference_range": {"start_char": start_char, "end_char": end_char, "basis": "source_section"},
        "returned_range": {"start_char": start_char, "end_char": end_char, "basis": "source_section"},
        "source_total_chars": total,
        "bounded": False,
        "document_scope": "pdf_page" if owner == "pdf" else "document",
    }
    if owner == "pdf":
        extent["pdf_page_index"] = page
    return extent


def _semantic_evidence(
    path: Path, source: Mapping[str, Any], citation: Mapping[str, Any],
    owner_row: sqlite3.Row, owner: str, source_kind: str, file_key: str, entity_id: str,
    locator: Mapping[str, Any],
) -> tuple[ResolvedSearchHit, dict[str, Any], dict[str, object]]:
    generation = citation.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or not 1 <= generation <= _SQLITE_MAX_INTEGER:
        raise EvidenceLookupError("invalid_evidence_reference")
    with semantic_read_context():
        with semantic_database(path, readonly=True) as connection:
            observation = _observe_owner(connection, "semantic")
            if _canonical_records(source.get("retrieval_publication")) != _canonical_records(observation["publications"]):
                raise EvidenceLookupError("retrieval_publication_changed")
            rows = connection.execute(
                """SELECT member.member_id,member.entity_id,member.item_id,
                          member.model_signature,model.vector_space,member.generation_id,chunk.text_chars
                   FROM embedding_generation_members member
                   JOIN published_embedding_heads head
                     ON head.generation_id=member.generation_id
                    AND head.model_signature=member.model_signature
                   JOIN embedding_generations generation
                     ON generation.generation_id=head.generation_id
                    AND generation.model_signature=head.model_signature
                   JOIN embedding_models model ON model.model_signature=member.model_signature
                   JOIN semantic_item_revisions revision
                     ON revision.item_revision_id=member.item_revision_id
                    AND revision.item_id=member.item_id
                   JOIN semantic_chunk_revisions chunk
                     ON chunk.chunk_revision_id=member.chunk_revision_id
                    AND chunk.item_id=member.item_id AND chunk.chunk_id=member.entity_id
                   WHERE member.generation_id=? AND member.entity_id=?
                     AND member.entity_kind='text_chunk' AND model.modality='text'
                     AND generation.status='ready'
                     AND revision.source_kind=? AND revision.source_identity=? LIMIT 2""",
                (generation, entity_id, source_kind, file_key),
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            member = rows[0]
            hit = SearchHit(
                ref_id=int(member["member_id"]), entity_id=str(member["entity_id"]),
                item_id=str(member["item_id"]), indexed_model_signature=str(member["model_signature"]),
                vector_space=str(member["vector_space"]), modality=EmbeddingModality.TEXT,
                score=0.0, generation_id=int(member["generation_id"]),
            )
        resolved, = resolve_search_hits(path, (hit,), snippet_chars=4096)
        if resolved.source_kind != source_kind or resolved.source_identity != file_key:
            raise EvidenceLookupError("evidence_identity_changed")
        live_revision = _owner_revision(owner_row, owner)
        if (resolved.source_revision_is_current is not True
                or resolved.source_status != owner_row["status"]
                or any(type(actual := resolved.source_revision.get(name)) is not type(value)
                       or actual != value for name, value in live_revision.items())
                or resolved.source_revision.get("revision_id") != live_revision.get("revision_id")):
            raise EvidenceLookupError("owner_revision_changed")
        if owner == "archive" and any(
            resolved.section_provenance.get(name) != owner_row[name]
            for name in ("container_key", "container_path", "member_chain", "member_path",
                         "archive_depth", "content_kind", "media_type", "container_status")
        ):
            raise EvidenceLookupError("evidence_locator_changed")
        resolved, extent = _semantic_fragment(resolved, locator, chunk_chars=int(member["text_chars"]))
    return resolved, observation, extent


def lookup_owner_evidence(
    state_directory: Path,
    source: Mapping[str, Any],
    citation: Mapping[str, Any],
    *, read_context: SemanticReadContext | None = None,
) -> dict[str, Any]:
    """Return one ordinary Knowledge result from a bound owner record."""
    owner = source.get("owner")
    source_kind = source.get("source_kind")
    if not isinstance(owner, str) or owner not in {
        "text", "pdf", "docx", "office", "archive", "audio", "video", "image", "code",
    }:
        raise EvidenceLookupError("unsupported_evidence_lookup")
    allowed_kinds = (
        _LEXICAL_OWNER_FORMATS["office"].intersection(TEXT_SOURCE_KINDS)
        if owner == "office" else {owner}
    )
    if owner == "image":
        allowed_kinds = {"image", "image_ocr"}
    if not isinstance(source_kind, str) or source_kind not in allowed_kinds:
        raise EvidenceLookupError("unsupported_evidence_lookup")
    file_key = citation.get("source_identity")
    if not isinstance(file_key, str) or not file_key or len(file_key) > 1024:
        raise EvidenceLookupError("invalid_evidence_reference")
    locator = citation.get("locator")
    if not isinstance(locator, Mapping):
        raise EvidenceLookupError("invalid_evidence_reference")
    page = locator.get("page")
    # PDF owner page_number is zero-based; preserve its locator verbatim.
    if page is not None and (
        isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= _SQLITE_MAX_INTEGER
    ):
        raise EvidenceLookupError("invalid_evidence_reference")
    if owner == "pdf":
        section = str(page)
        lexical_entity_id = f"lexical:pdf:{file_key}:page:{section}"
    elif owner in {"text", "docx", "audio"}:
        section = "fulltext"
        lexical_entity_id = f"lexical:{owner}:{file_key}:fulltext"
    elif owner == "video":
        section = str(locator.get("section_id", ""))
        if not section.isdecimal():
            raise EvidenceLookupError("invalid_evidence_reference")
        lexical_entity_id = f"lexical:video:{file_key}:frame:{section}"
    else:
        section = "fulltext"
        lexical_entity_id = None
    entity_id = citation.get("retrieval_entity_id")
    if not isinstance(entity_id, str) or not entity_id or len(entity_id) > 4096:
        raise EvidenceLookupError("invalid_evidence_reference")
    lexical = entity_id == lexical_entity_id
    if entity_id.startswith("lexical:") and not lexical:
        raise EvidenceLookupError("unsupported_evidence_lookup")
    if lexical and owner == "pdf" and page is None:
        raise EvidenceLookupError("unsupported_evidence_lookup")
    if citation.get("evidence_id") != f"evidence:{source_kind}:{entity_id}":
        raise EvidenceLookupError("invalid_evidence_reference")
    source_id = source.get("source_id")
    if not isinstance(source_id, str) or not source_id or source_id != citation.get("source_id"):
        raise EvidenceLookupError("invalid_evidence_reference")

    path = state_directory / f"{owner}.sqlite3"
    extent: dict[str, object] | None = None
    with semantic_read_context(read_context) as context, context.acquire(
        path, mode=preferred_sqlite_read_mode(path).value,
    ) as connection:
        observation = _observe_owner(connection, owner)
        owners = [observation]
        if (_canonical_records(source.get("publication")) != _canonical_records(observation["publications"])
                or _canonical_records(source.get("owner_watermarks")) != _canonical_records(observation["watermarks"])):
            raise EvidenceLookupError("owner_publication_changed")
        if not lexical:
            row = _owner_record(connection, owner, source_kind, file_key)
            resolved, semantic_observation, extent = _semantic_evidence(
                state_directory / "semantic.sqlite3", source, citation, row,
                owner, source_kind, file_key, entity_id, locator,
            )
            _validate_owner_locator(connection, owner, row, resolved)
            owners.append(semantic_observation)
        elif owner in {"text", "docx"}:
            rows = connection.execute(
                """SELECT d.*,length(f.body) AS evidence_total_chars
                   FROM documents d JOIN document_fts f ON f.file_key=d.file_key
                   WHERE d.file_key=? AND d.status IN (?,?) LIMIT 2""",
                (file_key, "complete", "partial" if owner == "docx" else "complete"),
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            start_char, end_char = _lexical_range(
                owner, locator, page=page, total=int(rows[0]["evidence_total_chars"]),
            )
            rows = connection.execute(
                """SELECT d.*,substr(f.body,?+1,?) AS evidence_text,length(f.body) AS evidence_total_chars
                   FROM documents d JOIN document_fts f ON f.file_key=d.file_key
                   WHERE d.file_key=? AND d.status IN (?,?) LIMIT 2""",
                (start_char, end_char - start_char, file_key,
                 "complete", "partial" if owner == "docx" else "complete"),
            ).fetchall()
        elif owner == "audio":
            rows = connection.execute(
                """SELECT d.*,length(f.body) AS evidence_total_chars,
                          (SELECT COUNT(*) FROM segments s
                           WHERE s.file_key=d.file_key) AS evidence_segment_count,
                          (SELECT s.start_ms FROM segments s
                           WHERE s.file_key=d.file_key ORDER BY s.segment_index LIMIT 1)
                           AS evidence_start_ms,
                          (SELECT s.end_ms FROM segments s
                           WHERE s.file_key=d.file_key ORDER BY s.segment_index LIMIT 1)
                           AS evidence_end_ms
                   FROM documents d JOIN transcript_fts f ON f.file_key=d.file_key
                   WHERE d.file_key=? AND d.status IN ('complete','no_speech')
                   LIMIT 2""",
                (file_key,),
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            # Lexical Audio historically publishes one aggregate transcript
            # reference.  Segment timing is retained when there is exactly one
            # segment; multiple segments remain a bounded document excerpt
            # rather than an invented segment binding.
            if locator.get("section_kind") not in {"transcript", "audio_segment"}:
                raise EvidenceLookupError("evidence_locator_changed")
            if any(name in locator for name in ("start_ms", "end_ms")):
                if int(rows[0]["evidence_segment_count"] or 0) != 1 or any(
                    locator.get(name) != rows[0][f"evidence_{name}"]
                    for name in ("start_ms", "end_ms")
                ):
                    raise EvidenceLookupError("evidence_locator_changed")
            audio_range_locator = {
                "section_kind": "transcript",
                "section_id": "fulltext",
                **{
                    name: locator[name]
                    for name in ("start_char", "end_char")
                    if name in locator
                },
            }
            start_char, end_char = _lexical_range(
                owner, audio_range_locator, page=page, total=int(rows[0]["evidence_total_chars"]),
            )
            rows = connection.execute(
                """SELECT d.*,substr(f.body,?+1,?) AS evidence_text,
                          length(f.body) AS evidence_total_chars,
                          (SELECT COUNT(*) FROM segments s
                           WHERE s.file_key=d.file_key) AS evidence_segment_count,
                          (SELECT s.start_ms FROM segments s
                           WHERE s.file_key=d.file_key ORDER BY s.segment_index LIMIT 1)
                           AS evidence_start_ms,
                          (SELECT s.end_ms FROM segments s
                           WHERE s.file_key=d.file_key ORDER BY s.segment_index LIMIT 1)
                           AS evidence_end_ms
                   FROM documents d JOIN transcript_fts f ON f.file_key=d.file_key
                   WHERE d.file_key=? AND d.status IN ('complete','no_speech')
                   LIMIT 2""",
                (start_char, end_char - start_char, file_key),
            ).fetchall()
        elif owner == "video":
            frame = locator.get("section_id")
            if isinstance(frame, bool) or not isinstance(frame, str) or not frame.isdecimal():
                raise EvidenceLookupError("invalid_evidence_reference")
            rows = connection.execute(
                """SELECT d.*,fr.timestamp_ms AS evidence_timestamp_ms,
                          fr.content_xxh3_128 AS evidence_content_xxh3_128,
                          fr.ocr_available,fr.ocr_text AS evidence_text,
                          length(fr.ocr_text) AS evidence_total_chars
                   FROM documents d JOIN frames fr ON fr.file_key=d.file_key
                   WHERE d.file_key=? AND fr.frame_index=?
                     AND d.status IN ('complete','partial') AND fr.ocr_available=1
                   LIMIT 2""",
                (file_key, int(frame)),
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            if any(
                locator.get(name) != rows[0][f"evidence_{name}"]
                for name in ("start_ms", "end_ms")
                if name in locator
            ):
                raise EvidenceLookupError("evidence_locator_changed")
            start_char, end_char = _lexical_range(
                "text",
                {
                    "section_kind": "document",
                    "section_id": "fulltext",
                    **{
                        name: locator[name]
                        for name in ("start_char", "end_char")
                        if name in locator
                    },
                },
                page=None, total=int(rows[0]["evidence_total_chars"] or 0),
            )
            rows = connection.execute(
                """SELECT d.*,fr.timestamp_ms AS evidence_timestamp_ms,
                          fr.content_xxh3_128 AS evidence_content_xxh3_128,
                          fr.ocr_available,substr(fr.ocr_text,?+1,?) AS evidence_text,
                          length(fr.ocr_text) AS evidence_total_chars
                   FROM documents d JOIN frames fr ON fr.file_key=d.file_key
                   WHERE d.file_key=? AND fr.frame_index=?
                     AND d.status IN ('complete','partial') AND fr.ocr_available=1
                   LIMIT 2""",
                (start_char, end_char - start_char, file_key, int(frame)),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT d.*,length(f.text) AS evidence_total_chars
                   FROM documents d JOIN page_fts f ON f.file_key=d.file_key
                   WHERE d.file_key=? AND CAST(f.page_number AS INTEGER)=?
                     AND d.status IN ('done','partial') LIMIT 2""", (file_key, page),
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            start_char, end_char = _lexical_range(
                owner, locator, page=page, total=int(rows[0]["evidence_total_chars"]),
            )
            rows = connection.execute(
                """SELECT d.*,substr(f.text,?+1,?) AS evidence_text,length(f.text) AS evidence_total_chars
                   FROM documents d JOIN page_fts f ON f.file_key=d.file_key
                   WHERE d.file_key=? AND CAST(f.page_number AS INTEGER)=?
                     AND d.status IN ('done','partial') LIMIT 2""",
                (start_char, end_char - start_char, file_key, page),
            ).fetchall()
        if lexical:
            if len(rows) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            row = rows[0]
            if owner == "pdf":
                resolved_section_kind = "pdf_page"
            elif owner == "audio":
                resolved_section_kind = "transcript"
            elif owner == "video":
                resolved_section_kind = "video_frame_ocr"
            else:
                resolved_section_kind = "document"
            section_provenance: dict[str, object] = {}
            if owner == "audio" and int(row["evidence_segment_count"] or 0) == 1:
                section_provenance.update(
                    {
                        "start_ms": int(row["evidence_start_ms"]),
                        "end_ms": int(row["evidence_end_ms"]),
                    }
                )
            elif owner == "video":
                timestamp_ms = int(row["evidence_timestamp_ms"])
                hours, remainder = divmod(max(0, timestamp_ms), 3_600_000)
                minutes, remainder = divmod(remainder, 60_000)
                seconds, milliseconds = divmod(remainder, 1000)
                section_provenance.update(
                    {
                        "start_ms": timestamp_ms,
                        "end_ms": timestamp_ms + 1,
                        "timestamp": f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}",
                        "frame_index": int(section),
                        "content_xxh3_128": str(row["evidence_content_xxh3_128"]),
                    }
                )
            resolved = ResolvedSearchHit(
                hit=SearchHit(ref_id=0, entity_id=entity_id, item_id=f"item:{owner}:{file_key}",
                              indexed_model_signature="owner-evidence-direct-v1",
                              vector_space="owner:evidence:text:v1", modality=EmbeddingModality.TEXT,
                              score=0.0, generation_id=0),
                path=str(row["path"]), source_kind=owner, source_identity=file_key,
                section_kind=resolved_section_kind, section_id=section,
                start_char=start_char, end_char=end_char, snippet=str(row["evidence_text"] or ""),
                source_revision=_owner_revision(row, owner),
                section_provenance=section_provenance,
                source_status=str(row["status"]),
            )
            extent = _lexical_extent(
                row, owner, resolved.snippet or "", page,
                start_char=start_char, end_char=end_char,
            )
        candidate = _candidate_from_resolved(resolved, ranking_name=f"fts_{owner}" if lexical else "semantic_text",
                                             source_rank=1, producer="owner-evidence-direct-v1")
        for key, actual in (
            ("resource_id", candidate.resource.resource_id),
            ("revision_id", candidate.revision.revision_id),
            ("processing_signature", candidate.revision.processing_signature),
        ):
            if source.get(key) != actual:
                raise EvidenceLookupError("owner_revision_changed")
        if candidate.evidence.evidence_id != citation["evidence_id"]:
            raise EvidenceLookupError("evidence_identity_changed")
        if candidate.resource.owner != owner or candidate.resource.source_kind != source_kind:
            raise EvidenceLookupError("evidence_identity_changed")
        hit = KnowledgeHit(rank=1, resource=candidate.resource, revision=candidate.revision,
                           evidence=candidate.evidence, signals=(candidate.signal,), fused_score=0.0,
                           reasons=("direct_published_owner_evidence",), warnings=candidate.warnings)
    payload = hit.to_dict()
    if extent is not None:
        payload["evidence_extent"] = extent
    snapshot_digest = hashlib.sha256(json.dumps(owners, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {
        "complete": True, "truncated": False, "rankings": [], "hits": [payload],
        "snapshot": {"snapshot_id": f"owner-evidence:{snapshot_digest}",
                     "origin_snapshot_id": source.get("snapshot_id"),
                     "consistency": "owner_revalidated", "validation_scope": "referenced_owners_only",
                     "owners": owners},
    }
