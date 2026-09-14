"""Read-only, streaming adapters from durable route caches to semantic items."""

from __future__ import annotations

from .semantic_source_budget import (
    install_source_progress,
    source_read_checkpoint,
    source_snapshot_budget,
)
import hashlib
import os
import json
import re
import sqlite3
import unicodedata
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Protocol

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import FULL_ALGORITHM, stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.platform.policy import sqlite_path_collation
from neocortex.platform.content_capability_manifest import (
    CONTENT_CAPABILITIES,
    content_capability_for_source,
)

from neocortex.foundation.file_identity import FileIdentityError, decode_file_identity
from .derivation_contracts import MaterializationRef
from neocortex.knowledge.knowledge_contracts import RevisionRef, RevisionState
from .semantic_models import (
    ContentFingerprint,
    SemanticItem,
    TextSection,
    fingerprint_bytes,
    fingerprint_chunks,
    fingerprint_text,
)
from .semantic_quality import (
    SEMANTIC_TEXT_QUALITY_POLICY,
    clean_title_candidate,
    content_title_from_sample,
)
from .semantic_admission import (
    ContentAdmissionPolicy,
    filter_text_source_records,
)
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    SQLiteReadSession,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationIntegrityError,
    validate_text_publications_from_connection,
)


# region [01] Public records and explicit limits

TEXT_SOURCE_KINDS = (
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "odt",
    "audio",
    "archive",
    "text",
    "code",
    "video",
)
IMAGE_SOURCE_KIND = "image"
VIDEO_SOURCE_KIND = "video"
# Physical Semantic source names are projected from the canonical content
# manifest.  ``image_ocr`` is a channel inside the image owner, not a second
# SQLite database, so only catalog-owned source kinds enter this map.
SOURCE_DATABASE_NAMES = {
    source_kind: capability.state_database
    for capability in CONTENT_CAPABILITIES
    for source_kind in capability.catalog_source_kinds
}


def iter_video_source_records(
    state_directory: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Lazily expose Video frame OCR through the Semantic source namespace."""

    from .video_source import iter_video_source_records as _iter_video

    yield from _iter_video(state_directory, connection=connection)


def _video_source_head(state_directory: Path) -> "SemanticSourceHead":
    """Adapt the Video-specific head to the common Semantic head contract.

    Video keeps a dedicated adapter because its frame OCR projection has
    locators and coverage states that do not belong to the ordinary text
    caches.  The manifest nevertheless exposes Video as a Semantic source,
    so callers asking for source heads must receive the same envelope instead
    of falling through to the text-cache query dispatcher.
    """

    from .video_source import video_source_head

    observed = video_source_head(state_directory)
    capability = content_capability_for_source(VIDEO_SOURCE_KIND)
    return SemanticSourceHead(
        source_kind=observed.source_kind,
        database_name=observed.database_name,
        adapter_version=observed.adapter_version,
        schema_version=capability.state_schema_version,
        row_count=observed.row_count,
        digest=observed.digest,
        complete=observed.complete,
        reason=observed.reason,
        coverage=observed.coverage,
        source_status=observed.source_status,
        truncated=observed.truncated,
    )


SOURCE_ADAPTER_VERSION = "semantic-source-adapters-v3"
IMAGE_SOURCE_ADAPTER_VERSION = "semantic-image-source-v4-no-nudenet"
CODE_SOURCE_ADAPTER_VERSION = "semantic-code-source-v1"
SEMANTIC_TITLE_SECTION_KIND = "semantic_metadata_title"
SEMANTIC_TITLE_POLICY = "semantic-content-aware-title-v3"
SEMANTIC_TEXT_ENUMERATION_PROTOCOL = "bounded-v1"
SEMANTIC_SOURCE_HEAD_PROTOCOL = "semantic-source-head-v1"
MAX_SEMANTIC_TITLE_CHARS = 512
MAX_SECTION_TEXT_BYTES = 32 * 1024 * 1024
MAX_SECTION_TEXT_CHARS = 20_000_000
FILE_HASH_BUFFER_BYTES = 4 * 1024 * 1024
_TEXT_PUBLICATION_VALIDATION_BATCH = 250
_PATH_COLLATION = sqlite_path_collation()


@dataclass(frozen=True, slots=True)
class TextSourceRecord:
    """One natural text section; adjacent rows with the same item are grouped."""

    item: SemanticItem
    section: TextSection


@dataclass(frozen=True, slots=True)
class ImageSourceRecord:
    """One image item and its optional bounded OCR representation."""

    item: SemanticItem
    ocr_section: TextSection | None


@dataclass(frozen=True, slots=True)
class SemanticSourceHead:
    """Compact exact projection of one durable source cache."""

    source_kind: str
    database_name: str
    adapter_version: str
    schema_version: int
    row_count: int
    digest: str
    complete: bool
    reason: str | None = None
    # ``coverage`` is deliberately separate from ``complete``.  A source may
    # have been enumerated without errors while an upstream route only
    # published a bounded/partial projection (for example truncated image
    # OCR).  Generation finalization consumes this field and must not turn
    # that projection into a complete published head.
    coverage: str = "complete"
    source_status: str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.coverage not in {"complete", "partial", "blocked"}:
            raise ValueError("semantic source coverage is invalid")
        if not isinstance(self.truncated, bool):
            raise ValueError("semantic source truncation must be boolean")
        if self.complete and self.coverage != "complete":
            raise ValueError("a complete source head must have complete coverage")
        if self.source_status is not None and (
            not isinstance(self.source_status, str) or not self.source_status.strip()
        ):
            raise ValueError("semantic source status cannot be blank when present")

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": SEMANTIC_SOURCE_HEAD_PROTOCOL,
            "source_kind": self.source_kind,
            "database_name": self.database_name,
            "adapter_version": self.adapter_version,
            "schema_version": self.schema_version,
            "row_count": self.row_count,
            "digest": self.digest,
            "complete": self.complete,
            "reason": self.reason,
            "coverage": self.coverage,
            "source_status": self.source_status,
            "truncated": self.truncated,
        }


class SemanticSourceError(RuntimeError):
    """A durable route cache contains invalid or unsafe source evidence."""


def require_readable_source_heads(heads: Sequence[SemanticSourceHead]) -> None:
    """Reject unknown owner snapshots before creating an embedding candidate.

    A readable partial projection may still be staged as ``ready_partial``;
    a blocked projection cannot establish which source revision was indexed.
    """

    blocked = tuple(
        head.source_kind
        for head in heads
        if head.coverage == "blocked" or (not head.complete and head.coverage != "partial")
    )
    if blocked:
        raise SemanticSourceError(
            "semantic source heads are blocked; retry after owners are readable and stable: "
            + ", ".join(blocked)
        )


class _DigestWriter(Protocol):
    def update(self, value: bytes, /) -> object: ...


_GENERIC_BASENAME = re.compile(
    r"(?i)^(?:(?:19|20)\d{2}\s*[-—]\s*)?"
    r"(?:(?:documento|archivo)\s+(?:personal\s+)?(?:protegido\s+)?recuperado\b|"
    r"scan(?:ned)?\b|img[_ -]?\d+\b|document\b|untitled\b|"
    r"[0-9a-f]{16,}\b)"
)


def _basename_title(item: SemanticItem) -> str | None:
    """Return bounded basename evidence without importing parent directories."""

    if item.path is None:
        return None
    candidate_path = item.path.strip()
    if not candidate_path or candidate_path.endswith(("/", "\\")):
        return None
    basename = PureWindowsPath(candidate_path.replace("/", "\\")).name
    raw_title = PureWindowsPath(basename).stem
    if any(unicodedata.category(character) == "Cc" for character in raw_title):
        return None
    title = " ".join(raw_title.split())
    if not title or len(title) > MAX_SEMANTIC_TITLE_CHARS:
        return None
    return title


def semantic_item_title_section(
    item: SemanticItem,
    content_sample: str | None = None,
) -> TextSection | None:
    """Project a useful title while retaining its exact evidence basis."""

    source_title_value = item.provenance.get("source_title")
    source_title = (
        clean_title_candidate(source_title_value) if isinstance(source_title_value, str) else None
    )
    basename_title = _basename_title(item)
    generic_basename = basename_title is None or bool(_GENERIC_BASENAME.match(basename_title))
    content_title = (
        content_title_from_sample(content_sample) if generic_basename and content_sample else None
    )
    title = source_title or content_title or basename_title
    if title is None:
        return None
    basis = (
        "durable_source_title"
        if source_title is not None
        else (
            "bounded_leading_content_heading"
            if content_title is not None
            else "basename_without_final_extension"
        )
    )
    provenance: dict[str, object] = {
        "policy_signature": SEMANTIC_TITLE_POLICY,
        "basis": basis,
        "mutable_metadata": True,
        "advisory_only": True,
    }
    if content_title is not None:
        provenance["generic_basename_replaced"] = True
    if source_title is not None:
        provenance["source_title_preferred"] = True
    return TextSection(
        section_kind=SEMANTIC_TITLE_SECTION_KIND,
        section_id=SEMANTIC_TITLE_POLICY,
        text=title,
        provenance=provenance,
    )


def iter_text_sections_with_metadata(
    item: SemanticItem,
    sections: Iterable[TextSection],
) -> Iterator[TextSection]:
    """Append optional metadata evidence after all source-owned sections."""

    sample_parts: list[str] = []
    remaining = 32_768
    for section in sections:
        if remaining > 0 and section.text:
            fragment = section.text[:remaining]
            sample_parts.append(fragment)
            remaining -= len(fragment)
        yield section
    title = semantic_item_title_section(item, "\n".join(sample_parts))
    if title is not None:
        yield title


def semantic_text_processing_signature(
    *,
    pipeline_version: str,
    chunking_signature: str,
    source_kinds: Sequence[str],
) -> str:
    """Build the shared producer/planner identity for durable text projection."""

    selected_sources = tuple(source_kinds)
    if (
        not pipeline_version.strip()
        or not chunking_signature.strip()
        or not selected_sources
        or any(not source.strip() for source in selected_sources)
    ):
        raise ValueError("semantic text processing signature inputs cannot be blank")
    return (
        f"{pipeline_version}|{SOURCE_ADAPTER_VERSION}|{chunking_signature}|"
        f"sources={','.join(selected_sources)}|title-policy={SEMANTIC_TITLE_POLICY}|"
        f"quality-policy={SEMANTIC_TEXT_QUALITY_POLICY}|"
        f"enumeration={SEMANTIC_TEXT_ENUMERATION_PROTOCOL}|"
        f"source-head={SEMANTIC_SOURCE_HEAD_PROTOCOL}"
    )


def semantic_source_database(state_directory: Path, source_kind: str) -> Path:
    """Resolve one route-owned source database through the shared contract."""

    try:
        database_name = SOURCE_DATABASE_NAMES[source_kind]
    except KeyError as exc:
        supported = ", ".join(SOURCE_DATABASE_NAMES)
        raise ValueError(f"unsupported semantic source {source_kind!r}; use {supported}") from exc
    return state_directory / database_name


# endregion [01]


# region [02] Shared SQLite, compression and identity helpers


@contextmanager
def _readonly_database(
    path: Path,
    *,
    expected_fence: SQLiteImmutableFence | None = None,
):
    """Open one owner through a snapshot matching a fence captured beforehand."""

    try:
        mode = preferred_sqlite_read_mode(path)
        session = SQLiteReadSession(
            path,
            mode=mode,
            timeout_seconds=60.0,
            budget=source_snapshot_budget(),
        )
        with session as connection:
            install_source_progress(connection)
            if expected_fence is not None and session.source_fence != expected_fence:
                raise SemanticSourceError("source_changed_before_head_snapshot")
            yield connection
    except FileNotFoundError as exc:
        raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    except ImmutableSQLiteUnavailable as exc:
        raise SemanticSourceError(str(exc)) from exc


@contextmanager
def _attached_readonly_database(
    connection: sqlite3.Connection,
    path: Path,
    *,
    schema: str,
    expected_fence: SQLiteImmutableFence | None = None,
):
    """Attach a second owner through the fenced SQLite read kernel.

    SQLite's ordinary ``mode=ro`` ATTACH URI is not safe for a published owner:
    opening it can create ``-wal``/``-shm`` files when the owner is live.  A
    strict owner is attached with ``immutable=1`` after a double filesystem
    fence; a live owner is first copied by :class:`SQLiteReadSession` and only
    the detached temporary bytes are attached.  The temporary session remains
    open until DETACH has completed so its files cannot be removed while the
    connection still references them.
    """

    if schema not in {"dedup"}:
        raise ValueError("unsupported attached SQLite schema")
    session: SQLiteReadSession | None = None
    attached = False
    primary_error: BaseException | None = None
    try:
        mode = preferred_sqlite_read_mode(path)
        session = SQLiteReadSession(path, mode=mode, timeout_seconds=60.0, budget=source_snapshot_budget())
        session.open()
        install_source_progress(session.connection)
        if expected_fence is not None and session.source_fence != expected_fence:
            raise SemanticSourceError("source_changed_before_head_snapshot")
        attached_path = session.temporary_database or path
        # The source has already been lstat/fstat fenced by the session.  The
        # immutable URI is deliberately built here rather than using the old
        # ``mode=ro`` helper, so this ATTACH cannot create sidecars.
        attach_uri = attached_path.resolve(strict=True).as_uri() + "?immutable=1"
        connection.execute(f"ATTACH DATABASE ? AS {schema}", (attach_uri,))
        attached = True
        yield
    except ImmutableSQLiteUnavailable as exc:
        primary_error = SemanticSourceError(str(exc))
        raise primary_error from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if attached:
            try:
                connection.execute(f"DETACH DATABASE {schema}")
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
                else:
                    try:
                        primary_error.add_note(
                            "semantic source attached owner detach failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    except Exception:
                        pass
        if session is not None:
            try:
                session.close()
            except BaseException as exc:
                if primary_error is None:
                    raise
                try:
                    primary_error.add_note(
                        "semantic source attached owner close failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                except Exception:
                    pass


@contextmanager
def _borrow_or_open_database(
    path: Path,
    connection: sqlite3.Connection | None,
    *,
    expected_fence: SQLiteImmutableFence | None = None,
):
    """Use a caller-owned snapshot or open a fenced private reader."""

    if connection is not None:
        yield connection
        return
    if expected_fence is None:
        reader = _readonly_database(path)
    else:
        reader = _readonly_database(path, expected_fence=expected_fence)
    with reader as opened:
        yield opened


@contextmanager
def _borrow_or_open_image_with_dedup(
    image_database: Path,
    dedup_database: Path | None,
    connection: sqlite3.Connection | None,
    *,
    dedup_attached: bool,
    image_expected_fence: SQLiteImmutableFence | None = None,
    dedup_expected_fence: SQLiteImmutableFence | None = None,
):
    """Borrow/open the image owner and safely attach the optional dedup owner."""

    with _borrow_or_open_database(
        image_database,
        connection,
        expected_fence=image_expected_fence,
    ) as opened:
        if dedup_attached or dedup_database is None or not dedup_database.is_file():
            yield opened
            return
        with _attached_readonly_database(
            opened,
            dedup_database,
            schema="dedup",
            expected_fence=dedup_expected_fence,
        ):
            yield opened


def _decode_text(payload: bytes | memoryview, expected_chars: int) -> str:
    """Decode one zlib payload with hard byte and character ceilings."""

    if expected_chars < 0 or expected_chars > MAX_SECTION_TEXT_CHARS:
        raise SemanticSourceError(
            f"declared section text length is outside bounds: {expected_chars}"
        )
    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(bytes(payload), MAX_SECTION_TEXT_BYTES + 1)
    if len(decoded) > MAX_SECTION_TEXT_BYTES or decompressor.unconsumed_tail:
        raise SemanticSourceError("compressed section exceeds the byte limit")
    remaining = MAX_SECTION_TEXT_BYTES + 1 - len(decoded)
    decoded += decompressor.flush(remaining)
    if len(decoded) > MAX_SECTION_TEXT_BYTES:
        raise SemanticSourceError("compressed section exceeds the byte limit")
    if not decompressor.eof:
        raise SemanticSourceError("compressed section is incomplete or truncated")
    if decompressor.unused_data:
        raise SemanticSourceError("compressed section contains trailing or concatenated data")
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticSourceError("section text is not valid UTF-8") from exc
    if len(text) > MAX_SECTION_TEXT_CHARS:
        raise SemanticSourceError("decoded section exceeds the character limit")
    if len(text) != expected_chars:
        raise SemanticSourceError("decoded section length does not match its durable metadata")
    return text


def _item_id(source_kind: str, source_identity: str) -> str:
    return f"item:{source_kind}:{source_identity}"


def _descriptor_fingerprint(
    *,
    source_kind: str,
    stored_xxh3_128: str | None,
    byte_or_char_count: int,
    processing_signature: str,
) -> ContentFingerprint:
    """Fingerprint a versioned source descriptor when the cache owns the text hash."""

    digest = stored_xxh3_128 or "unavailable"
    descriptor = (
        f"{SOURCE_ADAPTER_VERSION}\0{source_kind}\0{digest}\0"
        f"{byte_or_char_count}\0{processing_signature}"
    )
    return fingerprint_text(descriptor)


def _text_source_revision(row: sqlite3.Row) -> dict[str, object]:
    """Preserve the route-owned physical revision without synthesizing values."""

    revision: dict[str, object] = {
        "size": int(row["size"]),
        "mtime_ns": int(row["mtime_ns"]),
        "birthtime_ns": int(row["birthtime_ns"]),
        "processing_signature": str(row["processing_signature"]),
    }
    if row["last_seen_run_id"] is not None:
        revision["last_seen_run_id"] = int(row["last_seen_run_id"])
    if "source_revision_id" in row.keys() and row["source_revision_id"] is not None:
        revision_id = str(row["source_revision_id"])
        if not revision_id.strip():
            raise SemanticSourceError("source revision identity cannot be blank")
        revision["revision_id"] = revision_id
        native_columns = (
            "source_resource_id",
            "source_revision_producer",
            "source_revision_processing_signature",
            "source_revision_state",
            "source_revision_fingerprint_algorithm",
            "source_revision_fingerprint",
        )
        if any(column not in row.keys() or row[column] is None for column in native_columns):
            raise SemanticSourceError("text source revision is missing its owner-native contract")
        try:
            native_revision = RevisionRef(
                resource_id=str(row["source_resource_id"]),
                revision_id=revision_id,
                producer=str(row["source_revision_producer"]),
                processing_signature=str(row["source_revision_processing_signature"]),
                generation=(
                    None
                    if row["source_revision_generation"] is None
                    else int(row["source_revision_generation"])
                ),
                state=RevisionState(str(row["source_revision_state"])),
                observed_at_utc=(
                    None
                    if row["source_revision_observed_at_utc"] is None
                    else str(row["source_revision_observed_at_utc"])
                ),
            )
        except (TypeError, ValueError) as exc:
            raise SemanticSourceError(
                "text source revision has an invalid owner-native contract"
            ) from exc
        fingerprint_algorithm = str(row["source_revision_fingerprint_algorithm"])
        fingerprint = str(row["source_revision_fingerprint"])
        if not fingerprint_algorithm.strip() or not fingerprint.strip():
            raise SemanticSourceError("text source revision fingerprint cannot be blank")
        revision["owner_revision"] = {
            "owner": "text",
            "revision": native_revision.to_dict(),
            "fingerprint_algorithm": fingerprint_algorithm,
            "fingerprint": fingerprint,
        }
        materialization_columns = (
            "source_materialization_json",
            "source_materialization_fingerprint_algorithm",
            "source_materialization_fingerprint",
        )
        if any(
            column not in row.keys() or row[column] is None for column in materialization_columns
        ):
            raise SemanticSourceError(
                "text source revision is missing its published representation"
            )
        try:
            materialization_payload = json.loads(str(row["source_materialization_json"]))
            materialization = MaterializationRef.from_dict(materialization_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticSourceError(
                "text source representation materialization is invalid"
            ) from exc
        if (
            materialization.owner != "text"
            or materialization.kind != "text_representation"
            or materialization.revision is None
            or materialization.revision.revision_id != revision_id
        ):
            raise SemanticSourceError(
                "text source representation does not match its owner revision"
            )
        representation_algorithm = str(row["source_materialization_fingerprint_algorithm"])
        representation_fingerprint = str(row["source_materialization_fingerprint"])
        if not representation_algorithm.strip() or not representation_fingerprint.strip():
            raise SemanticSourceError("text source representation fingerprint cannot be blank")
        revision["consumed_materialization"] = {
            "materialization": materialization.to_dict(),
            "fingerprint_algorithm": representation_algorithm,
            "fingerprint": representation_fingerprint,
        }
    if "is_partial" in row.keys():
        is_partial = row["is_partial"]
        if not isinstance(is_partial, int) or is_partial not in {0, 1}:
            raise SemanticSourceError("PDF source has an invalid is_partial value")
        revision["is_partial"] = bool(is_partial)
    return revision


def _source_item(
    row: sqlite3.Row,
    *,
    source_kind: str,
    text_fingerprint_column: str,
    text_count_column: str,
) -> SemanticItem:
    processing_signature = str(row["processing_signature"])
    source_identity = str(row["file_key"])
    provenance: dict[str, object] = {
        "adapter": SOURCE_ADAPTER_VERSION,
        "processing_signature": processing_signature,
        "source_status": str(row["status"]),
        "fingerprint_basis": "durable-source-text-descriptor",
    }
    if "title" in row.keys() and row["title"] is not None:
        provenance["source_title"] = str(row["title"])
    if "author" in row.keys() and row["author"] is not None:
        provenance["source_author"] = str(row["author"])
    return SemanticItem(
        item_id=_item_id(source_kind, source_identity),
        source_kind=source_kind,
        source_identity=source_identity,
        identity_version=f"{SOURCE_ADAPTER_VERSION}|{processing_signature}",
        fingerprint=_descriptor_fingerprint(
            source_kind=source_kind,
            stored_xxh3_128=(
                None if row[text_fingerprint_column] is None else str(row[text_fingerprint_column])
            ),
            byte_or_char_count=int(row[text_count_column]),
            processing_signature=processing_signature,
        ),
        path=str(row["path"]),
        source_revision=_text_source_revision(row),
        provenance=provenance,
    )


def _current_source_item(
    row: sqlite3.Row,
    *,
    source_kind: str,
    text_fingerprint_column: str,
    text_count_column: str,
    current_file_key: str | None,
    current_item: SemanticItem | None,
) -> tuple[str, SemanticItem]:
    """Reuse only the current ordered file's immutable semantic identity."""

    file_key = str(row["file_key"])
    if current_item is None or file_key != current_file_key:
        current_item = _source_item(
            row,
            source_kind=source_kind,
            text_fingerprint_column=text_fingerprint_column,
            text_count_column=text_count_column,
        )
    return file_key, current_item


# endregion [02]


# region [03] Text cache adapters


def _iter_pdf(
    path: Path,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    with _borrow_or_open_database(path, connection) as connection:
        rows = connection.execute(
            """SELECT d.file_key,d.path,d.processing_signature,d.status,
            d.size,d.mtime_ns,d.birthtime_ns,d.last_seen_run_id,d.is_partial,
            d.normalized_text_xxh3_128,d.normalized_text_chars,
            p.page_number,p.source,p.text_zlib,p.text_chars
            FROM documents d JOIN pages p ON p.file_key=d.file_key
            WHERE d.status IN ('done','partial')
            ORDER BY d.file_key,p.page_number"""
        )
        current_file_key: str | None = None
        current_item: SemanticItem | None = None
        for row in rows:
            current_file_key, current_item = _current_source_item(
                row,
                source_kind="pdf",
                text_fingerprint_column="normalized_text_xxh3_128",
                text_count_column="normalized_text_chars",
                current_file_key=current_file_key,
                current_item=current_item,
            )
            yield TextSourceRecord(
                current_item,
                TextSection(
                    section_kind="pdf_page",
                    section_id=str(int(row["page_number"])),
                    text=_decode_text(row["text_zlib"], int(row["text_chars"])),
                    provenance={
                        "adapter": SOURCE_ADAPTER_VERSION,
                        "extraction_source": str(row["source"]),
                    },
                ),
            )


def _iter_docx(
    path: Path,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    with _borrow_or_open_database(path, connection) as connection:
        rows = connection.execute(
            """SELECT d.file_key,d.path,d.processing_signature,d.status,
            d.size,d.mtime_ns,d.birthtime_ns,d.last_seen_run_id,
            d.text_xxh3_128,d.text_chars,d.text_zlib AS document_text_zlib,
            p.part_name,p.part_kind,p.ordinal,p.text_zlib,p.text_chars AS part_chars
            FROM documents d LEFT JOIN document_parts p ON p.file_key=d.file_key
            WHERE d.status IN ('complete','partial')
            ORDER BY d.file_key,p.ordinal,p.part_name"""
        )
        current_file_key: str | None = None
        current_item: SemanticItem | None = None
        for row in rows:
            if row["part_name"] is None:
                payload = row["document_text_zlib"]
                if payload is None or int(row["text_chars"]) == 0:
                    continue
                section = TextSection(
                    "docx_document",
                    "body",
                    _decode_text(payload, int(row["text_chars"])),
                    {"adapter": SOURCE_ADAPTER_VERSION},
                )
            else:
                section = TextSection(
                    section_kind=f"docx_{row['part_kind']}",
                    section_id=str(row["part_name"]),
                    text=_decode_text(row["text_zlib"], int(row["part_chars"])),
                    provenance={
                        "adapter": SOURCE_ADAPTER_VERSION,
                        "part_ordinal": int(row["ordinal"]),
                    },
                )
            current_file_key, current_item = _current_source_item(
                row,
                source_kind="docx",
                text_fingerprint_column="text_xxh3_128",
                text_count_column="text_chars",
                current_file_key=current_file_key,
                current_item=current_item,
            )
            yield TextSourceRecord(current_item, section)


def _iter_office(
    path: Path,
    source_kind: str,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    with _borrow_or_open_database(path, connection) as connection:
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,last_seen_run_id,text_xxh3_128,
            text_chars,text_zlib FROM documents
            WHERE format=? AND status='complete' ORDER BY file_key""",
            (source_kind,),
        )
        for row in rows:
            if row["text_zlib"] is None or int(row["text_chars"]) == 0:
                continue
            item = _source_item(
                row,
                source_kind=source_kind,
                text_fingerprint_column="text_xxh3_128",
                text_count_column="text_chars",
            )
            yield TextSourceRecord(
                item,
                TextSection(
                    section_kind=f"{source_kind}_document",
                    section_id="body",
                    text=_decode_text(row["text_zlib"], int(row["text_chars"])),
                    provenance={"adapter": SOURCE_ADAPTER_VERSION},
                ),
            )


def _iter_audio(
    path: Path,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    with _borrow_or_open_database(path, connection) as connection:
        rows = connection.execute(
            """SELECT d.file_key,d.path,d.processing_signature,d.status,
            d.size,d.mtime_ns,d.birthtime_ns,d.last_seen_run_id,
            d.text_xxh3_128,d.text_chars,s.segment_index,s.start_ms,s.end_ms,s.text
            FROM documents d JOIN segments s ON s.file_key=d.file_key
            WHERE d.status='complete' ORDER BY d.file_key,s.segment_index"""
        )
        current_file_key: str | None = None
        current_item: SemanticItem | None = None
        for row in rows:
            text = str(row["text"])
            if not text.strip():
                continue
            current_file_key, current_item = _current_source_item(
                row,
                source_kind="audio",
                text_fingerprint_column="text_xxh3_128",
                text_count_column="text_chars",
                current_file_key=current_file_key,
                current_item=current_item,
            )
            yield TextSourceRecord(
                current_item,
                TextSection(
                    section_kind="audio_segment",
                    section_id=str(int(row["segment_index"])),
                    text=text,
                    provenance={
                        "adapter": SOURCE_ADAPTER_VERSION,
                        "start_ms": int(row["start_ms"]),
                        "end_ms": int(row["end_ms"]),
                    },
                ),
            )


def _archive_role_projections(
    connection: sqlite3.Connection,
) -> tuple[bool, bool, str, str]:
    """Return role projections while keeping pre-role Archive fixtures readable."""

    document_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")
    }
    role_column_present = "document_role" in document_columns
    logical_chain_column_present = "logical_document_chain" in document_columns
    role_projection = (
        "d.document_role AS document_role"
        if role_column_present
        else "NULL AS document_role"
    )
    logical_chain_projection = (
        "d.logical_document_chain AS logical_document_chain"
        if logical_chain_column_present
        else "NULL AS logical_document_chain"
    )
    return (
        role_column_present,
        logical_chain_column_present,
        role_projection,
        logical_chain_projection,
    )


def _archive_section_projection(
    row: sqlite3.Row,
    *,
    role_column_present: bool,
    logical_chain_column_present: bool,
) -> tuple[str, str, bool, dict[str, object]]:
    """Classify one Archive row without inventing a virtual or physical path."""

    file_key = row["file_key"]
    path = row["path"]
    container_path = row["container_path"]
    container_key = row["container_key"]
    member_chain = row["member_chain"]
    member_path = row["member_path"]
    if not all(
        isinstance(value, str) and value.strip()
        for value in (file_key, path, container_path, container_key)
    ):
        raise SemanticSourceError("archive source row has a missing physical path or identity")
    if not isinstance(member_chain, str) or not isinstance(member_path, str):
        raise SemanticSourceError("archive source row has a malformed member identity")
    raw_depth = row["archive_depth"]
    if isinstance(raw_depth, bool) or not isinstance(raw_depth, (int, str)):
        raise SemanticSourceError("archive source row has an invalid archive depth")
    try:
        archive_depth = int(raw_depth)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SemanticSourceError("archive source row has an invalid archive depth") from exc
    if archive_depth < 0:
        raise SemanticSourceError("archive source row has a negative archive depth")

    role_value = row["document_role"]
    if role_column_present:
        if not isinstance(role_value, str) or not role_value.strip():
            raise SemanticSourceError("archive source row has a missing document role")
        role = role_value
    else:
        role = None
    logical_chain = row["logical_document_chain"]
    if logical_chain_column_present and logical_chain is not None and not isinstance(
        logical_chain, str
    ):
        raise SemanticSourceError("archive source row has a malformed logical document chain")

    physical_root = (
        member_chain == ""
        and member_path == ""
        and archive_depth == 0
        and path == container_path
        and (not role_column_present or role == "logical_document")
        and (not logical_chain_column_present or logical_chain == "")
    )
    if physical_root:
        section_kind = "archive_document"
        section_id = "body"
        inside_zip = False
        evidence = "logical_document_role" if role_column_present else "legacy_physical_root_shape"
    else:
        if member_chain == "":
            if role == "logical_document":
                raise SemanticSourceError("archive logical document root identity is inconsistent")
            raise SemanticSourceError("archive virtual member has an empty member_chain")
        if archive_depth < 1:
            raise SemanticSourceError("archive virtual member has an invalid archive depth")
        if role_column_present and role not in {
            "archive_member",
            "document_component",
            "logical_document",
        }:
            raise SemanticSourceError("archive virtual member has an unsupported document role")
        if not member_path:
            raise SemanticSourceError("archive virtual member has an empty member_path")
        expected_path = f"{container_path}!/{member_chain}"
        if path != expected_path:
            raise SemanticSourceError("archive virtual member path does not match its member_chain")
        section_kind = "archive_member"
        section_id = member_chain
        inside_zip = True
        evidence = "archive_member_role" if role_column_present else "legacy_member_shape"

    provenance: dict[str, object] = {
        "inside_zip": inside_zip,
        "container_path": container_path,
        "container_key": container_key,
        "member_chain": member_chain,
        "member_path": member_path,
        "archive_depth": archive_depth,
        "content_kind": str(row["content_kind"]),
        "media_type": str(row["media_type"]),
        "container_status": str(row["container_status"]),
    }
    if not inside_zip:
        provenance["physical_root"] = True
        provenance["section_identity_evidence"] = evidence
        if role is not None:
            provenance["document_role"] = role
        if logical_chain_column_present and logical_chain is not None:
            provenance["logical_document_chain"] = logical_chain
    return section_kind, section_id, inside_zip, provenance


def _iter_archive(
    path: Path,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Stream text-bearing virtual members with explicit ZIP provenance."""

    with _borrow_or_open_database(path, connection) as connection:
        (
            role_column_present,
            logical_chain_column_present,
            role_projection,
            logical_chain_projection,
        ) = _archive_role_projections(connection)
        rows = connection.execute(
            f"""SELECT d.file_key,d.path,d.processing_signature,d.status,
            d.size,d.mtime_ns,d.birthtime_ns,d.last_seen_run_id,
            d.text_xxh3_128,d.text_chars,d.text_zlib,
            d.container_path,d.container_key,d.member_chain,d.member_path,
            d.archive_depth,d.content_kind,d.media_type,c.status AS container_status,
            {role_projection},{logical_chain_projection}
            FROM documents d JOIN containers c ON c.container_key=d.container_key
            WHERE d.status='indexed' AND d.text_zlib IS NOT NULL AND d.text_chars>0
            AND c.status IN ('complete','partial')
            ORDER BY d.file_key"""
        )
        for row in rows:
            section_kind, section_id, _inside_zip, provenance = _archive_section_projection(
                row,
                role_column_present=role_column_present,
                logical_chain_column_present=logical_chain_column_present,
            )
            provenance["adapter"] = SOURCE_ADAPTER_VERSION
            item = _source_item(
                row,
                source_kind="archive",
                text_fingerprint_column="text_xxh3_128",
                text_count_column="text_chars",
            )
            yield TextSourceRecord(
                item,
                TextSection(
                    section_kind=section_kind,
                    section_id=section_id,
                    text=_decode_text(row["text_zlib"], int(row["text_chars"])),
                    provenance=provenance,
                ),
            )


def _iter_text(
    path: Path,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Stream physical generic-text bodies and typed extraction evidence."""

    with _borrow_or_open_database(path, connection) as connection:
        document_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")
        }
        has_revision_table = (
            connection.execute(
                """SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='text_input_revisions'"""
            ).fetchone()
            is not None
        )
        validate_owner_publication = "revision_id" in document_columns and has_revision_table
        if validate_owner_publication:
            rows = connection.execute(
                """SELECT d.file_key,d.path,d.processing_signature,d.status,
                d.size,d.mtime_ns,d.birthtime_ns,d.last_seen_run_id,
                d.text_xxh3_128,d.text_chars,d.text_zlib,d.content_kind,
                d.media_type,d.title,d.author,d.metadata_json,d.text_truncated,
                d.detail,d.revision_id AS source_revision_id,
                r.resource_id AS source_resource_id,
                r.producer AS source_revision_producer,
                r.processing_signature AS source_revision_processing_signature,
                r.generation AS source_revision_generation,
                r.revision_state AS source_revision_state,
                r.observed_at_utc AS source_revision_observed_at_utc,
                r.fingerprint_algorithm AS source_revision_fingerprint_algorithm,
                r.fingerprint AS source_revision_fingerprint,
                materialization.materialization_json AS source_materialization_json,
                materialization.fingerprint_algorithm
                  AS source_materialization_fingerprint_algorithm,
                materialization.fingerprint AS source_materialization_fingerprint
                FROM documents d LEFT JOIN text_input_revisions r
                  ON r.revision_id=d.revision_id
                LEFT JOIN text_materialization_heads head
                  ON head.resource_id=r.resource_id
                 AND head.materialization_kind='text_representation'
                 AND head.revision_id=r.revision_id
                LEFT JOIN text_materializations materialization
                  ON materialization.owner=head.materialization_owner
                 AND materialization.materialization_id=head.materialization_id
                WHERE d.status='complete' AND d.text_zlib IS NOT NULL
                  AND d.text_chars>0 ORDER BY d.file_key"""
            )
        else:
            rows = connection.execute(
                """SELECT file_key,path,processing_signature,status,size,mtime_ns,
                birthtime_ns,last_seen_run_id,text_xxh3_128,text_chars,text_zlib,
                content_kind,media_type,title,author,metadata_json,text_truncated,
                detail,NULL AS source_revision_id
                FROM documents WHERE status='complete' AND text_zlib IS NOT NULL
                AND text_chars>0 ORDER BY file_key"""
            )
        while batch := rows.fetchmany(_TEXT_PUBLICATION_VALIDATION_BATCH):
            if validate_owner_publication:
                legacy_resource_ids: list[str] = []
                for row in batch:
                    if row["source_revision_id"] is not None:
                        continue
                    try:
                        identity = decode_file_identity(str(row["file_key"]))
                    except (FileIdentityError, TypeError, ValueError):
                        continue
                    legacy_resource_ids.append(
                        f"resource:file:{identity.volume_id}:{identity.file_id}:"
                        f"{int(row['birthtime_ns'])}"
                    )
                if legacy_resource_ids:
                    placeholders = ",".join("?" for _ in legacy_resource_ids)
                    downgraded = connection.execute(
                        f"""SELECT resource_id FROM text_input_revisions
                        WHERE resource_id IN ({placeholders})
                        UNION SELECT resource_id FROM text_materialization_heads
                        WHERE resource_id IN ({placeholders})
                        UNION SELECT resource_id FROM text_materializations
                        WHERE resource_id IN ({placeholders}) LIMIT 1""",
                        (
                            *legacy_resource_ids,
                            *legacy_resource_ids,
                            *legacy_resource_ids,
                        ),
                    ).fetchone()
                    if downgraded is not None:
                        raise SemanticSourceError(
                            "text source publication was downgraded to legacy state"
                        )
                publications = tuple(
                    (str(row["file_key"]), str(row["source_revision_id"]))
                    for row in batch
                    if row["source_revision_id"] is not None
                )
                if publications:
                    try:
                        validate_text_publications_from_connection(
                            connection,
                            publications,
                        )
                    except TextDerivationIntegrityError as exc:
                        raise SemanticSourceError(
                            "text source publication failed owner-local validation"
                        ) from exc
            for row in batch:
                item = _source_item(
                    row,
                    source_kind="text",
                    text_fingerprint_column="text_xxh3_128",
                    text_count_column="text_chars",
                )
                yield TextSourceRecord(
                    item,
                    TextSection(
                        section_kind="document",
                        section_id="fulltext",
                        text=_decode_text(row["text_zlib"], int(row["text_chars"])),
                        provenance={
                            "adapter": SOURCE_ADAPTER_VERSION,
                            "content_kind": str(row["content_kind"]),
                            "media_type": str(row["media_type"]),
                            "title": str(row["title"] or ""),
                            "author": str(row["author"] or ""),
                            "metadata_json": str(row["metadata_json"]),
                            "text_truncated": bool(row["text_truncated"]),
                            "detail": str(row["detail"] or ""),
                            "inside_zip": False,
                        },
                    ),
                )


def _iter_code(
    path: Path,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Stream current bounded code chunks with structural provenance."""

    with _borrow_or_open_database(path, connection) as connection:
        rows = connection.execute(
            """SELECT f.volume_id,f.physical_file_id,f.current_path AS path,
            f.last_seen_run_id AS source_last_seen_run_id,
            v.version_id,v.size,v.mtime_ns,v.birthtime_ns,v.raw_xxh3_128,
            v.first_observed_run_id,v.last_observed_run_id,
            v.text_xxh3_128,v.text_chars,v.processing_signature,v.analysis_status,
            v.language,v.artifact_kind,v.analyzer_id,v.analyzer_version,v.parser_kind,
            c.chunk_index,c.kind AS chunk_kind,c.start_line,c.end_line,c.text,
            s.qualified_name AS symbol
            FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
            JOIN code_chunks c ON c.version_id=v.version_id
            LEFT JOIN symbols s ON s.symbol_id=c.symbol_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            AND v.analysis_status IN ('complete','partial','text_only')
            ORDER BY v.version_id,c.chunk_index"""
        )
        current_version_id: int | None = None
        current_item: SemanticItem | None = None
        for row in rows:
            version_id = int(row["version_id"])
            if version_id != current_version_id:
                source_identity = f"{row['volume_id']}:{row['physical_file_id']}"
                text_digest = str(row["text_xxh3_128"] or "unavailable")
                descriptor = fingerprint_text(
                    f"{CODE_SOURCE_ADAPTER_VERSION}\0{source_identity}\0"
                    f"{text_digest}\0{int(row['text_chars'])}\0"
                    f"{row['processing_signature']}"
                )
                current_item = SemanticItem(
                    item_id=_item_id("code", source_identity),
                    source_kind="code",
                    source_identity=source_identity,
                    identity_version=(
                        f"{CODE_SOURCE_ADAPTER_VERSION}|{row['processing_signature']}|"
                        f"{row['analyzer_id']}:{row['analyzer_version']}"
                    ),
                    fingerprint=descriptor,
                    path=str(row["path"]),
                    source_revision={
                        "version_id": version_id,
                        "size": int(row["size"]),
                        "mtime_ns": int(row["mtime_ns"]),
                        "birthtime_ns": int(row["birthtime_ns"]),
                        "processing_signature": str(row["processing_signature"]),
                        "last_seen_run_id": int(row["source_last_seen_run_id"]),
                        "first_observed_run_id": int(row["first_observed_run_id"]),
                        "last_observed_run_id": int(row["last_observed_run_id"]),
                        "raw_content_xxh3_128": row["raw_xxh3_128"],
                    },
                    provenance={
                        "adapter": CODE_SOURCE_ADAPTER_VERSION,
                        "processing_signature": str(row["processing_signature"]),
                        "analysis_status": str(row["analysis_status"]),
                        "language": row["language"],
                        "artifact_kind": str(row["artifact_kind"]),
                        "analyzer_id": str(row["analyzer_id"]),
                        "analyzer_version": str(row["analyzer_version"]),
                        "parser_kind": str(row["parser_kind"]),
                        "fingerprint_basis": "durable-code-text-descriptor",
                    },
                )
                current_version_id = version_id
            assert current_item is not None
            yield TextSourceRecord(
                current_item,
                TextSection(
                    section_kind=f"code_{row['chunk_kind']}",
                    section_id=str(int(row["chunk_index"])),
                    text=str(row["text"]),
                    provenance={
                        "adapter": CODE_SOURCE_ADAPTER_VERSION,
                        "version_id": version_id,
                        "language": row["language"],
                        "symbol": row["symbol"],
                        "start_line": int(row["start_line"]),
                        "end_line": int(row["end_line"]),
                    },
                ),
            )


def _source_head_query(
    connection: sqlite3.Connection,
    source_kind: str,
) -> tuple[str, tuple[object, ...]]:
    """Return the compact ordered projection that controls Semantic output."""

    if source_kind == "pdf":
        return (
            """SELECT d.file_key,d.path,d.processing_signature,d.status,d.size,
            d.mtime_ns,d.birthtime_ns,d.is_partial,d.normalized_text_xxh3_128,
            d.normalized_text_chars,p.page_number,p.source,p.text_chars
            FROM documents d JOIN pages p ON p.file_key=d.file_key
            WHERE d.status IN ('done','partial') ORDER BY d.file_key,p.page_number""",
            (),
        )
    if source_kind == "docx":
        return (
            """SELECT d.file_key,d.path,d.processing_signature,d.status,d.size,
            d.mtime_ns,d.birthtime_ns,d.text_xxh3_128,d.text_chars,
            p.part_name,p.part_kind,p.ordinal,p.text_chars
            FROM documents d LEFT JOIN document_parts p ON p.file_key=d.file_key
            WHERE d.status IN ('complete','partial') AND
            (p.part_name IS NOT NULL OR (d.text_zlib IS NOT NULL AND d.text_chars>0))
            ORDER BY d.file_key,p.ordinal,p.part_name""",
            (),
        )
    if source_kind in {"xlsx", "pptx", "odt"}:
        return (
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,text_xxh3_128,text_chars
            FROM documents WHERE format=? AND status='complete'
            AND text_zlib IS NOT NULL AND text_chars>0 ORDER BY file_key""",
            (source_kind,),
        )
    if source_kind == "audio":
        return (
            """SELECT d.file_key,d.path,d.processing_signature,d.status,d.size,
            d.mtime_ns,d.birthtime_ns,d.text_xxh3_128,d.text_chars,
            s.segment_index,s.start_ms,s.end_ms,length(s.text)
            FROM documents d JOIN segments s ON s.file_key=d.file_key
            WHERE d.status='complete' AND trim(s.text)<>''
            ORDER BY d.file_key,s.segment_index""",
            (),
        )
    if source_kind == "archive":
        (
            _role_column_present,
            _logical_chain_column_present,
            role_projection,
            logical_chain_projection,
        ) = _archive_role_projections(connection)
        return (
            f"""SELECT d.file_key,d.path,d.processing_signature,d.status,d.size,
            d.mtime_ns,d.birthtime_ns,d.text_xxh3_128,d.text_chars,
            d.container_path,d.container_key,d.member_chain,d.member_path,
            d.archive_depth,d.content_kind,d.media_type,c.status,
            {role_projection},{logical_chain_projection}
            FROM documents d JOIN containers c ON c.container_key=d.container_key
            WHERE d.status='indexed' AND d.text_zlib IS NOT NULL AND d.text_chars>0
            AND c.status IN ('complete','partial') ORDER BY d.file_key""",
            (),
        )
    if source_kind == "text":
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")}
        revision_projection = (
            "d.revision_id,r.resource_id,r.producer,r.processing_signature,r.generation,"
            "r.revision_state,r.fingerprint_algorithm,r.fingerprint,"
            "materialization.materialization_id,materialization.fingerprint_algorithm,"
            "materialization.fingerprint"
            if "revision_id" in columns
            and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_input_revisions'"
            ).fetchone()
            is not None
            else "NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL"
        )
        joins = (
            """LEFT JOIN text_input_revisions r ON r.revision_id=d.revision_id
            LEFT JOIN text_materialization_heads head ON head.resource_id=r.resource_id
              AND head.materialization_kind='text_representation'
              AND head.revision_id=r.revision_id
            LEFT JOIN text_materializations materialization
              ON materialization.owner=head.materialization_owner
             AND materialization.materialization_id=head.materialization_id"""
            if "revision_id" in columns
            and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_input_revisions'"
            ).fetchone()
            is not None
            else ""
        )
        return (
            f"""SELECT d.file_key,d.path,d.processing_signature,d.status,d.size,
            d.mtime_ns,d.birthtime_ns,d.text_xxh3_128,d.text_chars,d.content_kind,
            d.media_type,d.title,d.author,d.metadata_json,d.text_truncated,d.detail,
            {revision_projection} FROM documents d {joins}
            WHERE d.status='complete' AND d.text_zlib IS NOT NULL AND d.text_chars>0
            ORDER BY d.file_key""",
            (),
        )
    if source_kind == "code":
        return (
            """SELECT f.volume_id,f.physical_file_id,f.current_path,
            v.version_id,v.size,v.mtime_ns,v.birthtime_ns,v.raw_xxh3_128,
            v.text_xxh3_128,v.text_chars,v.processing_signature,v.analysis_status,
            v.language,v.artifact_kind,v.analyzer_id,v.analyzer_version,v.parser_kind,
            c.chunk_index,c.kind,c.start_line,c.end_line,length(c.text),s.qualified_name
            FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
            JOIN code_chunks c ON c.version_id=v.version_id
            LEFT JOIN symbols s ON s.symbol_id=c.symbol_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            AND v.analysis_status IN ('complete','partial','text_only')
            ORDER BY v.version_id,c.chunk_index""",
            (),
        )
    raise ValueError(f"unsupported semantic text source: {source_kind}")


def _update_head_digest(hasher: _DigestWriter, value: object) -> None:
    source_read_checkpoint()
    if value is None:
        payload = b"n"
    elif isinstance(value, bytes):
        payload = b"b" + value
    elif isinstance(value, memoryview):
        payload = b"b" + bytes(value)
    elif isinstance(value, int):
        payload = b"i" + str(value).encode("ascii")
    elif isinstance(value, float):
        payload = b"f" + value.hex().encode("ascii")
    else:
        payload = b"s" + str(value).encode("utf-8", "surrogatepass")
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _required_source_fence(path: Path) -> SQLiteImmutableFence:
    """Capture an owner fence before any SQLite snapshot can be opened."""

    try:
        return capture_sqlite_read_fence(path)
    except FileNotFoundError as exc:
        # Preserve the historical missing-owner classification used by source
        # heads while keeping the fence acquisition outside SQLite.
        raise sqlite3.OperationalError(f"unable to inspect database file: {path}") from exc
    except ImmutableSQLiteUnavailable as exc:
        raise SemanticSourceError(str(exc)) from exc


def _optional_source_fence(path: Path) -> SQLiteImmutableFence | None:
    """Capture an optional owner fence, retaining absence as ``None``."""

    try:
        return capture_sqlite_read_fence(path)
    except FileNotFoundError:
        return None


def _assert_source_fence_unchanged(
    path: Path,
    expected_fence: SQLiteImmutableFence,
) -> None:
    """Reject an owner that changed after its snapshot was consumed."""

    try:
        observed_fence = capture_sqlite_read_fence(path)
    except (FileNotFoundError, ImmutableSQLiteUnavailable) as exc:
        raise SemanticSourceError("source_changed_during_head_projection") from exc
    if observed_fence != expected_fence:
        raise SemanticSourceError("source_changed_during_head_projection")


def _assert_optional_source_fence_unchanged(
    path: Path,
    expected_fence: SQLiteImmutableFence | None,
) -> None:
    """Reject an optional owner that appeared, disappeared or changed."""

    try:
        observed_fence = _optional_source_fence(path)
    except ImmutableSQLiteUnavailable as exc:
        raise SemanticSourceError("source_changed_during_head_projection") from exc
    if observed_fence != expected_fence:
        raise SemanticSourceError("source_changed_during_head_projection")


def _owner_stamp(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    """Legacy observable stamp retained for compatibility with diagnostics.

    The authoritative drift barrier is ``capture_sqlite_read_fence``; this
    bounded stamp remains as a supplementary hook for older callers that
    observe or monkeypatch the former owner seam.
    """

    values: list[tuple[str, int, int, int]] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        values.append((suffix, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns))
    return tuple(values)


def _text_source_head(state_directory: Path, source_kind: str) -> SemanticSourceHead:
    database = semantic_source_database(state_directory, source_kind)
    hasher = hashlib.sha256()
    row_count = schema_version = 0
    try:
        before_fence = _required_source_fence(database)
        before_stamp = _owner_stamp(database)
        with _readonly_database(database, expected_fence=before_fence) as connection:
            before_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            query, parameters = _source_head_query(connection, source_kind)
            connection.execute("BEGIN")
            try:
                rows = connection.execute(query, parameters)
                for row in rows:
                    for value in row:
                        _update_head_digest(hasher, value)
                    hasher.update(b"\n")
                    row_count += 1
            finally:
                connection.execute("COMMIT")
            after_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
        _assert_source_fence_unchanged(database, before_fence)
        after_stamp = _owner_stamp(database)
        if before_version != after_version or before_stamp != after_stamp:
            raise SemanticSourceError("source_changed_during_head_projection")
    except (OSError, sqlite3.DatabaseError, SemanticSourceError, ValueError) as exc:
        return SemanticSourceHead(
            source_kind,
            database.name,
            SOURCE_ADAPTER_VERSION,
            schema_version,
            row_count,
            "sha256:" + hasher.hexdigest(),
            False,
            type(exc).__name__,
            "blocked",
            "blocked",
        )
    hasher.update(SEMANTIC_SOURCE_HEAD_PROTOCOL.encode("ascii"))
    hasher.update(source_kind.encode("ascii"))
    hasher.update(str(schema_version).encode("ascii"))
    return SemanticSourceHead(
        source_kind,
        database.name,
        SOURCE_ADAPTER_VERSION,
        schema_version,
        row_count,
        "sha256:" + hasher.hexdigest(),
        True,
        None,
        "complete",
        "complete",
        False,
    )


def iter_text_source_records(
    state_directory: Path,
    source_kind: str,
    *,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Yield one selected source incrementally without scanning source files."""

    # Video frame OCR is text evidence, but its owner schema and locator
    # contract are deliberately maintained by ``video_source``.  It belongs
    # to the default textual source set while retaining its dedicated adapter.
    if source_kind not in TEXT_SOURCE_KINDS:
        raise ValueError(f"unsupported semantic text source: {source_kind}")
    database = semantic_source_database(state_directory, source_kind)
    if not database.is_file():
        return
    if source_kind == VIDEO_SOURCE_KIND:
        yield from iter_video_source_records(state_directory, connection=connection)
    elif source_kind == "pdf":
        yield from _iter_pdf(database, connection)
    elif source_kind == "docx":
        yield from _iter_docx(database, connection)
    elif source_kind == "audio":
        yield from _iter_audio(database, connection)
    elif source_kind == "archive":
        yield from _iter_archive(database, connection)
    elif source_kind == "text":
        yield from _iter_text(database, connection)
    elif source_kind == "code":
        yield from _iter_code(database, connection)
    else:
        yield from _iter_office(database, source_kind, connection)


# endregion [03]


# region [04] Image cache adapter and bounded binary fingerprints


def _snapshot_from_image_row(row: sqlite3.Row) -> FileSnapshot:
    file_key = str(row["file_key"])
    try:
        identity = decode_file_identity(file_key)
    except FileIdentityError as exc:
        raise SemanticSourceError(f"invalid image file identity: {file_key}") from exc
    return FileSnapshot(
        path=str(row["path"]),
        volume_id=identity.volume_id,
        file_id=identity.file_id,
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
    )


def _stream_file_fingerprint(snapshot: FileSnapshot) -> ContentFingerprint:
    def chunks() -> Iterator[bytes]:
        buffer = bytearray(FILE_HASH_BUFFER_BYTES)
        view = memoryview(buffer)
        with open(native_io_path(snapshot.path), "rb", buffering=0) as stream:
            if not stat_matches_snapshot(snapshot, os.fstat(stream.fileno())):
                raise SemanticSourceError(
                    f"image source changed before fingerprinting: {snapshot.path}"
                )
            while count := stream.readinto(buffer):
                yield bytes(view[:count])
            if not stat_matches_snapshot(snapshot, os.fstat(stream.fileno())):
                raise SemanticSourceError(
                    f"image source changed during fingerprinting: {snapshot.path}"
                )

    return fingerprint_chunks(chunks())


def _image_descriptor_fingerprint(
    digest: bytes | memoryview,
    size: int,
) -> ContentFingerprint:
    """Wrap a raw full-file XXH3-128 result in one stable cache descriptor."""

    value = bytes(digest)
    if len(value) != 16:
        raise SemanticSourceError("dedup full fingerprint must contain 16 bytes")
    return fingerprint_bytes(
        b"dedup-full-xxh3-128-descriptor-v1\0" + value + size.to_bytes(8, "little", signed=False)
    )


def _dedup_uses_isolated_generations(connection: sqlite3.Connection) -> bool:
    primary_key = tuple(
        str(row[1])
        for row in sorted(
            connection.execute("PRAGMA dedup.table_info(files)"),
            key=lambda row: int(row[5]) if int(row[5]) else 99,
        )
        if int(row[5])
    )
    return primary_key == ("scan_id", "path")


def _image_rows(
    image_database: Path,
    dedup_database: Path | None,
    connection: sqlite3.Connection | None = None,
    *,
    dedup_attached: bool = False,
    include_ocr_payload: bool = True,
) -> Iterator[sqlite3.Row]:
    with _borrow_or_open_image_with_dedup(
        image_database,
        dedup_database,
        connection,
        dedup_attached=dedup_attached,
    ) as connection:
        image_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(images)")}
        run_projection = (
            ",i.last_seen_run_id"
            if "last_seen_run_id" in image_columns
            else ",NULL AS last_seen_run_id"
        )
        image_projection = f"""i.file_key,i.path,i.size,i.mtime_ns,i.birthtime_ns,
            i.processing_signature,i.status AS source_status,i.category,
            i.document_candidate{run_projection}"""
        if "ocr_text_zlib" in image_columns:
            ocr_payload = "i.ocr_text_zlib" if include_ocr_payload else "NULL"
            ocr_projection = f""",{ocr_payload} AS ocr_text_zlib,i.ocr_text_chars,
            i.ocr_text_xxh3_128,i.ocr_text_truncated"""
        else:
            ocr_projection = """,NULL AS ocr_text_zlib,NULL AS ocr_text_chars,
            NULL AS ocr_text_xxh3_128,0 AS ocr_text_truncated"""
        has_dedup = dedup_attached or bool(dedup_database and dedup_database.is_file())
        if has_dedup:
            assert dedup_database is not None
            if _dedup_uses_isolated_generations(connection):
                # A path may coexist in several roots or unpublished scans.
                # Reuse only the newest valid checkpoint generation.
                query = f"""SELECT {image_projection}{ocr_projection},
                fp.digest AS full_digest FROM images i
                LEFT JOIN dedup.files f ON f.path=i.path COLLATE {_PATH_COLLATION}
                    AND f.size=i.size AND f.mtime_ns=i.mtime_ns
                    AND f.birthtime_ns=i.birthtime_ns
                    AND f.scan_id=(
                        SELECT candidate.scan_id FROM dedup.files candidate
                        JOIN dedup.inventory_checkpoints checkpoint
                          ON checkpoint.scan_id=candidate.scan_id
                         AND checkpoint.valid=1
                        WHERE candidate.path=i.path COLLATE {_PATH_COLLATION}
                          AND candidate.size=i.size
                          AND candidate.mtime_ns=i.mtime_ns
                          AND candidate.birthtime_ns=i.birthtime_ns
                        ORDER BY checkpoint.updated_ns DESC,
                                 candidate.scan_id DESC LIMIT 1)
                LEFT JOIN dedup.fingerprints fp ON fp.volume_id=f.volume_id
                    AND fp.file_id=f.file_id AND fp.size=f.size
                    AND fp.mtime_ns=f.mtime_ns AND fp.birthtime_ns=f.birthtime_ns
                    AND fp.algorithm=?
                WHERE i.status='done' ORDER BY i.file_key"""
            else:
                query = f"""SELECT {image_projection}{ocr_projection},
                fp.digest AS full_digest FROM images i
                LEFT JOIN dedup.files f ON f.path=i.path COLLATE {_PATH_COLLATION}
                    AND f.size=i.size AND f.mtime_ns=i.mtime_ns
                    AND f.birthtime_ns=i.birthtime_ns
                LEFT JOIN dedup.fingerprints fp ON fp.volume_id=f.volume_id
                    AND fp.file_id=f.file_id AND fp.size=f.size
                    AND fp.mtime_ns=f.mtime_ns AND fp.birthtime_ns=f.birthtime_ns
                    AND fp.algorithm=?
                WHERE i.status='done' ORDER BY i.file_key"""
            rows = connection.execute(query, (FULL_ALGORITHM,))
        else:
            rows = connection.execute(
                f"""SELECT {image_projection}{ocr_projection},
                NULL AS full_digest FROM images i
                WHERE i.status='done' ORDER BY i.file_key"""
            )
        try:
            for row in rows:
                yield row
        finally:
            try:
                rows.close()
            except sqlite3.ProgrammingError:
                # A caller may abort after closing its borrowed owner snapshot.
                pass


def _image_source_head(state_directory: Path) -> SemanticSourceHead:
    image_database = semantic_source_database(state_directory, IMAGE_SOURCE_KIND)
    dedup_database = state_directory / "dedup.sqlite3"
    hasher = hashlib.sha256()
    row_count = schema_version = 0
    complete = True
    truncated = False
    missing_full_digest = False
    source_statuses: set[str] = set()
    try:
        image_fence = _required_source_fence(image_database)
        dedup_fence = _optional_source_fence(dedup_database)
        dedup_available = dedup_fence is not None
        read_dedup_database = dedup_database if dedup_available else None
        with _borrow_or_open_image_with_dedup(
            image_database,
            read_dedup_database,
            None,
            dedup_attached=False,
            image_expected_fence=image_fence,
            dedup_expected_fence=dedup_fence,
        ) as connection:
            before_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            before_dedup_version = (
                int(connection.execute("PRAGMA dedup.data_version").fetchone()[0])
                if dedup_available
                else 0
            )
            schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            status_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM images GROUP BY status ORDER BY status"
            ).fetchall()
            for status_row in status_rows:
                status = str(status_row["status"])
                source_statuses.add(status)
                _update_head_digest(hasher, status)
                _update_head_digest(hasher, int(status_row["count"]))
                if status != "done":
                    complete = False
            rows = _image_rows(
                image_database,
                read_dedup_database,
                connection,
                dedup_attached=dedup_available,
                include_ocr_payload=False,
            )
            for row in rows:
                if row["full_digest"] is None:
                    complete = False
                    missing_full_digest = True
                if bool(row["ocr_text_truncated"]):
                    truncated = True
                for name in (
                    "file_key",
                    "path",
                    "size",
                    "mtime_ns",
                    "birthtime_ns",
                    "processing_signature",
                    "source_status",
                    "category",
                    "document_candidate",
                    "ocr_text_chars",
                    "ocr_text_xxh3_128",
                    "ocr_text_truncated",
                    "full_digest",
                ):
                    _update_head_digest(hasher, row[name])
                hasher.update(b"\n")
                row_count += 1
            after_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            after_dedup_version = (
                int(connection.execute("PRAGMA dedup.data_version").fetchone()[0])
                if dedup_available
                else 0
            )
        if (
            before_version != after_version
            or before_dedup_version != after_dedup_version
        ):
            raise SemanticSourceError("source_changed_during_head_projection")
        _assert_source_fence_unchanged(image_database, image_fence)
        _assert_optional_source_fence_unchanged(dedup_database, dedup_fence)
    except (OSError, sqlite3.DatabaseError, SemanticSourceError, ValueError) as exc:
        return SemanticSourceHead(
            IMAGE_SOURCE_KIND,
            image_database.name,
            IMAGE_SOURCE_ADAPTER_VERSION,
            schema_version,
            row_count,
            "sha256:" + hasher.hexdigest(),
            False,
            type(exc).__name__,
            "blocked",
            "blocked",
            truncated,
        )
    hasher.update(SEMANTIC_SOURCE_HEAD_PROTOCOL.encode("ascii"))
    hasher.update(IMAGE_SOURCE_KIND.encode("ascii"))
    hasher.update(str(schema_version).encode("ascii"))
    return SemanticSourceHead(
        IMAGE_SOURCE_KIND,
        image_database.name,
        IMAGE_SOURCE_ADAPTER_VERSION,
        schema_version,
        row_count,
        "sha256:" + hasher.hexdigest(),
        complete,
        None
        if complete
        else (
            "dedup_full_fingerprint_missing"
            if missing_full_digest
            else "image_ocr_truncated"
            if truncated
            else "image_source_not_complete"
        ),
        "complete" if complete else "partial",
        "done" if source_statuses <= {"done"} else "partial",
        truncated,
    )


def semantic_source_heads(
    state_directory: Path,
    source_kinds: Sequence[str],
) -> tuple[SemanticSourceHead, ...]:
    """Project compact source identities without decoding or reading source files."""

    selected = tuple(dict.fromkeys(source_kinds))
    if not selected or any(kind not in SOURCE_DATABASE_NAMES for kind in selected):
        raise ValueError("semantic source head kinds are invalid")
    return tuple(
        _image_source_head(state_directory)
        if source_kind == IMAGE_SOURCE_KIND
        else _video_source_head(state_directory)
        if source_kind == VIDEO_SOURCE_KIND
        else _text_source_head(state_directory, source_kind)
        for source_kind in selected
    )


def iter_image_source_records(
    state_directory: Path,
    *,
    verify_snapshots: bool = True,
) -> Iterator[ImageSourceRecord]:
    """Yield images and verified OCR, reusing exact dedup fingerprints when present."""

    image_database = semantic_source_database(state_directory, IMAGE_SOURCE_KIND)
    if not image_database.is_file():
        return
    dedup_database = state_directory / "dedup.sqlite3"
    for row in _image_rows(image_database, dedup_database):
        snapshot = _snapshot_from_image_row(row)
        if verify_snapshots:
            try:
                stat = os.stat(native_io_path(snapshot.path), follow_symlinks=False)
            except OSError as exc:
                raise SemanticSourceError(
                    f"image source is unavailable during semantic refresh: {snapshot.path}"
                ) from exc
            if not stat_matches_snapshot(snapshot, stat):
                raise SemanticSourceError(
                    f"image source changed before semantic refresh: {snapshot.path}"
                )
        if row["full_digest"] is not None:
            raw_digest = bytes(row["full_digest"])
            fingerprint_acquisition = "dedup-cache"
        else:
            streamed_fingerprint = _stream_file_fingerprint(snapshot)
            raw_digest = bytes.fromhex(streamed_fingerprint.xxh3_128)
            fingerprint_acquisition = "streamed-source"
        fingerprint = _image_descriptor_fingerprint(raw_digest, snapshot.size)
        fingerprint_basis = "raw-full-xxh3-128-size-descriptor-v1"
        raw_content_xxh3_128 = raw_digest.hex()
        processing_signature = str(row["processing_signature"] or "unprocessed")
        source_status = str(row["source_status"] or "unknown")
        ocr_truncated = bool(row["ocr_text_truncated"])
        coverage = "complete" if source_status == "done" and not ocr_truncated else "partial"
        source_revision: dict[str, object] = {
            "volume_id": snapshot.volume_id,
            "file_id": snapshot.file_id,
            "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns,
            "birthtime_ns": snapshot.birthtime_ns,
            "fingerprint_algorithm": fingerprint_basis,
            "fingerprint_digest": fingerprint.xxh3_128,
            "raw_content_xxh3_128": raw_content_xxh3_128,
            "source_status": source_status,
            "coverage": coverage,
            "ocr_text_truncated": ocr_truncated,
        }
        if row["processing_signature"] is not None:
            source_revision["processing_signature"] = str(row["processing_signature"])
        if row["last_seen_run_id"] is not None:
            source_revision["last_seen_run_id"] = int(row["last_seen_run_id"])
        item = SemanticItem(
            item_id=_item_id("image", str(row["file_key"])),
            source_kind="image",
            source_identity=str(row["file_key"]),
            identity_version=(
                f"{IMAGE_SOURCE_ADAPTER_VERSION}|{processing_signature}|"
                f"snapshot={snapshot.size}:{snapshot.mtime_ns}:{snapshot.birthtime_ns}"
            ),
            fingerprint=fingerprint,
            path=snapshot.path,
            source_revision=source_revision,
            provenance={
                "adapter": IMAGE_SOURCE_ADAPTER_VERSION,
                "processing_signature": processing_signature,
                "source_status": source_status,
                "coverage": coverage,
                "ocr_text_truncated": ocr_truncated,
                "category": row["category"],
                "document_candidate": bool(row["document_candidate"]),
                "fingerprint_basis": fingerprint_basis,
                "fingerprint_acquisition": fingerprint_acquisition,
            },
        )
        ocr_section = None
        if row["ocr_text_zlib"] is not None:
            ocr_text = _decode_text(
                row["ocr_text_zlib"],
                int(row["ocr_text_chars"]),
            )
            ocr_fingerprint = fingerprint_text(ocr_text).xxh3_128
            if ocr_fingerprint != str(row["ocr_text_xxh3_128"]):
                raise SemanticSourceError(f"image OCR fingerprint mismatch: {snapshot.path}")
            ocr_section = TextSection(
                section_kind="image_ocr",
                section_id="ocr",
                text=ocr_text,
                provenance={
                    "adapter": IMAGE_SOURCE_ADAPTER_VERSION,
                    "processing_signature": processing_signature,
                    "source_status": source_status,
                    "coverage": coverage,
                    "truncated": ocr_truncated,
                },
            )
        yield ImageSourceRecord(item, ocr_section)


def iter_admitted_text_source_records(
    state_directory: Path,
    source_kind: str,
    *,
    policy: ContentAdmissionPolicy,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Project only visible text records without deleting source diagnostics.

    Admission is intentionally applied after the owner adapter has produced a
    bounded ``SemanticItem``.  The source head and the semantic database stay
    untouched, so a policy correction does not invalidate or recompute an
    unchanged vector.
    """

    records = iter_text_source_records(state_directory, source_kind, connection=connection)
    yield from filter_text_source_records(records, policy)


def iter_admitted_image_source_records(
    state_directory: Path,
    *,
    policy: ContentAdmissionPolicy,
    verify_snapshots: bool = True,
) -> Iterator[ImageSourceRecord]:
    """Project only visible images while retaining excluded diagnostics."""

    records = iter_image_source_records(
        state_directory,
        verify_snapshots=verify_snapshots,
    )
    yield from filter_text_source_records(records, policy)


def admitted_text_source_iterator(
    policy: ContentAdmissionPolicy,
) -> Callable[[Path, str], Iterator[TextSourceRecord]]:
    """Bind a policy to the callback shape consumed by text ``--all`` stages.

    The returned callback is intentionally side-effect free beyond the existing
    source-owner reads.  A caller can select it at the workflow boundary while
    leaving the legacy iterator available for policy-free compatibility paths.
    """

    if not isinstance(policy, ContentAdmissionPolicy):
        raise TypeError("admission iterator requires a ContentAdmissionPolicy")

    def iterator(state_directory: Path, source_kind: str) -> Iterator[TextSourceRecord]:
        yield from iter_admitted_text_source_records(
            state_directory,
            source_kind,
            policy=policy,
        )

    return iterator


def admitted_image_source_iterator(
    policy: ContentAdmissionPolicy,
) -> Callable[[Path], Iterator[ImageSourceRecord]]:
    """Bind a policy to the callback shape consumed by image ``--all`` stages."""

    if not isinstance(policy, ContentAdmissionPolicy):
        raise TypeError("admission iterator requires a ContentAdmissionPolicy")

    def iterator(state_directory: Path) -> Iterator[ImageSourceRecord]:
        yield from iter_admitted_image_source_records(state_directory, policy=policy)

    return iterator


# endregion [04]
