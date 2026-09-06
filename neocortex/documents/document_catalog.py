"""Incremental cross-format catalog built from durable document text caches."""

from __future__ import annotations
import codecs
import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from neocortex.platform.policy import sqlite_path_collation, stat_birthtime_ns
from typing import TYPE_CHECKING, Iterator, Literal

from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)
from neocortex.persistence.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)
from neocortex.persistence.sqlite_immutable import (
    open_immutable_sqlite_connection,
    preferred_sqlite_read_mode,
    sqlite_read_session,
)

from .document_taxonomy import (
    DocumentClassification,
    DocumentSignals,
    TechnicalTaxonomy,
    classify_document,
    document_classifier_signature,
    load_taxonomy,
)
from .document_catalog_schema import (
    CATALOG_SCHEMA_VERSION,
    document_catalog_schema_contract,
    migrate_document_catalog_schema,
    validate_v5_document_catalog_schema,
    validate_v6_document_catalog_schema,
    validate_v7_document_catalog_schema,
)
from .document_resource_binding import (
    ResourceBindingError,
    build_resource_binding,
    parse_resource_binding,
    physical_identity_from_components,
)
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.foundation.file_identity import (
    FileIdentity,
    FileIdentityEncoding,
    decode_file_identity,
)
from neocortex.persistence.sqlite_schema_contract import (
    read_metadata_schema_version,
    validate_sqlite_schema_contract,
)
from neocortex.platform.content_capability_manifest import (
    content_capability_for_source,
)

if TYPE_CHECKING:
    from neocortex.runtime.control.cancellation import CancellationToken


# region [01] Schema, connections and bounded source records

# Primary titles, identifiers and document structure belong near the beginning.
# A smaller bounded prefix avoids classifying a 200-page report by one appendix.
MAX_CLASSIFICATION_TEXT_CHARS = 64_000
CATALOG_WRITE_BATCH = 100
CATALOG_PROGRESS_INTERVAL = 25
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
    "archive",
    "code",
]
SourceCoverage = Literal["complete", "partial", "blocked"]
_CATALOG_WRITE_LOCK = threading.RLock()
_PATH_COLLATION = sqlite_path_collation()
_CATALOG_DOCUMENT_COLUMNS = (
    "source_kind",
    "file_key",
    "path",
    "volume_id",
    "file_id",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "source_status",
    "processing_signature",
    "text_fingerprint",
    "classifier_signature",
    "primary_kind",
    "primary_subtype",
    "primary_authority",
    "primary_organization",
    "primary_client",
    "primary_project",
    "primary_workstream",
    "confidence",
    "uncertainty",
    "standard_references_json",
    "organizations_json",
    "clients_json",
    "projects_json",
    "workstreams_json",
    "topics_json",
    "equipment_json",
    "activities_json",
    "classification_json",
    "catalog_status",
    "error_type",
    "error_message",
    "active",
    "last_seen_catalog_run_id",
    "updated_ns",
    "resource_binding_json",
)


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
    virtual: bool = False
    resource_binding_json: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogUpdateSummary:
    catalog_run_id: int
    source_kind: SourceKind
    candidates: int = 0
    classified: int = 0
    cache_hits: int = 0
    review_required: int = 0
    errors: int = 0
    stale_marked: int = 0
    source_stale: int = 0
    source_missing: bool = False


@dataclass(frozen=True, slots=True)
class CatalogBuild:
    """Identity and optimistic base of one isolated catalog construction."""

    catalog_run_id: int
    generation_id: int
    source_kind: SourceKind
    base_generation_id: int | None


class CatalogPublicationConflict(RuntimeError):
    """A later builder cannot replace a publication based on an older pointer."""


@dataclass(frozen=True, slots=True)
class CatalogDocumentView:
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


_DOCUMENT_CATALOG_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="document catalog",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=32768,
        wal_autocheckpoint_pages=4096,
        journal_size_limit_bytes=268435456,
    ),
)


def connect_document_catalog(
    path: Path,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    if readonly:
        # Keep the legacy connection factory sidecar-safe for quiescent
        # callers.  Callers that need to read a live WAL must use the context
        # manager below, which owns a temporary snapshot and its cleanup.
        if not path.is_file():
            # Preserve the existing-file error contract for callers that use
            # this low-level connection seam directly.  Context-managed
            # readers choose the safe temporary snapshot path when needed.
            return connect_sqlite(
                path,
                mode=READONLY_EXISTING,
                policy=_DOCUMENT_CATALOG_SQLITE_POLICY,
            )
        return open_immutable_sqlite_connection(path, timeout_seconds=60.0)
    return connect_sqlite(
        path,
        mode=READONLY_EXISTING if readonly else READWRITE_CREATE,
        policy=_DOCUMENT_CATALOG_SQLITE_POLICY,
    )


@contextmanager
def document_catalog_database(path: Path, *, readonly: bool = False):
    if readonly:
        # A normal SQLite ``mode=ro`` connection can create SHM/WAL files on
        # close.  Public catalog readers therefore use the shared immutable
        # or temporary-copy kernel; writers retain the connection policy above.
        mode = preferred_sqlite_read_mode(path)
        if mode.value == "immutable_strict":
            connection = connect_document_catalog(path, readonly=True)
            try:
                yield connection
            finally:
                connection.close()
        else:
            with sqlite_read_session(path, mode=mode, timeout_seconds=60.0) as connection:
                yield connection
        return
    connection = connect_document_catalog(path, readonly=readonly)
    try:
        yield connection
    finally:
        connection.close()


def initialize_document_catalog(path: Path) -> None:
    """Validate current state read-only or back up and migrate a known legacy catalog."""

    with _CATALOG_WRITE_LOCK:
        prior = _read_catalog_version(path)
        if prior == CATALOG_SCHEMA_VERSION:
            return
        with document_catalog_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                locked_prior = read_metadata_schema_version(
                    connection,
                    label="document catalog",
                )
                if locked_prior != prior:
                    raise RuntimeError(
                        "document catalog schema version changed before migration lock"
                    )
                if locked_prior is not None:
                    _backup_catalog_before_migration(path, locked_prior)
                migrate_document_catalog_schema(
                    connection,
                    locked_prior or 0,
                    identity_migrator=_migrate_identity_text_to_decimal,
                )
                validate_sqlite_schema_contract(
                    connection,
                    document_catalog_schema_contract(),
                    label="document catalog",
                    exact=True,
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


def _read_catalog_version(path: Path) -> int | None:
    if not path.is_file():
        return None
    with document_catalog_database(path, readonly=True) as connection:
        version = read_metadata_schema_version(connection, label="document catalog")
        if version is not None and version > CATALOG_SCHEMA_VERSION:
            raise RuntimeError(
                f"document catalog schema {version} is newer than supported "
                f"schema {CATALOG_SCHEMA_VERSION}"
            )
        if version == CATALOG_SCHEMA_VERSION:
            validate_sqlite_schema_contract(
                connection,
                document_catalog_schema_contract(),
                label="document catalog",
                exact=True,
            )
        elif version == 5:
            validate_v5_document_catalog_schema(connection)
        elif version == 6:
            validate_v6_document_catalog_schema(connection)
        elif version == 7:
            validate_v7_document_catalog_schema(connection)
    return version


def _backup_catalog_before_migration(path: Path, prior: int) -> Path:
    """Keep a consistent, private pre-migration copy before any schema change.

    The caller already holds BEGIN IMMEDIATE. The sidecar-safe read kernel sees
    exactly that committed base without opening a second ordinary owner reader.
    """
    destination = path.with_name(
        f"{path.name}.pre-v{prior}-to-v{CATALOG_SCHEMA_VERSION}-{time.time_ns()}.sqlite3"
    )
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + 60.0

    def bounded_backup(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("document catalog migration backup deadline exceeded")

    try:
        with document_catalog_database(path, readonly=True) as source:
            target = connect_sqlite(
                destination,
                mode=READWRITE_CREATE,
                policy=SQLiteConnectionPolicy(label="catalog migration backup"),
            )
            try:
                source.backup(target, pages=256, progress=bounded_backup, sleep=0.01)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("catalog migration backup failed integrity verification")
            finally:
                target.close()
        with destination.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            os.fsync(handle.fileno())
        receipt = destination.with_suffix(destination.suffix + ".json")
        with receipt.open("x", encoding="utf-8") as handle:
            os.chmod(receipt, 0o600)
            json.dump(
                {
                    "source": str(path.absolute()),
                    "backup": str(destination.absolute()),
                    "prior_schema": prior,
                    "target_schema": CATALOG_SCHEMA_VERSION,
                    "sha256": digest,
                    "bytes": destination.stat().st_size,
                },
                handle,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        parent_descriptor = os.open(
            destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        # The exclusive destination is our own incomplete staging, never a source.
        destination.unlink(missing_ok=True)
        raise
    return destination


def _migrate_identity_text_to_decimal(connection: sqlite3.Connection) -> None:
    """Repair v1/v2 rows whose neutral identity fields retained hex file-key text."""

    last_source = ""
    last_key = ""
    while True:
        rows = connection.execute(
            """SELECT source_kind,file_key,volume_id,file_id FROM documents
            WHERE source_kind>? OR (source_kind=? AND file_key>?)
            ORDER BY source_kind,file_key LIMIT 500""",
            (last_source, last_source, last_key),
        ).fetchall()
        if not rows:
            break
        updates: list[tuple[str, str, str, str]] = []
        for row in rows:
            if row["source_kind"] in {"archive", "code"}:
                continue  # owner keys are not filesystem keys; preserve legacy evidence
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            if volume_id and (volume_id != str(row["volume_id"]) or file_id != str(row["file_id"])):
                updates.append((volume_id, file_id, str(row["source_kind"]), str(row["file_key"])))
        connection.executemany(
            """UPDATE documents SET volume_id=?,file_id=?
            WHERE source_kind=? AND file_key=?""",
            updates,
        )
        last_source = str(rows[-1]["source_kind"])
        last_key = str(rows[-1]["file_key"])
    last_plan_id = 0
    while True:
        rows = connection.execute(
            """SELECT plan_id,file_key,volume_id,file_id,source_kind FROM organization_plans
            WHERE plan_id>? ORDER BY plan_id LIMIT 500""",
            (last_plan_id,),
        ).fetchall()
        if not rows:
            break
        plan_updates: list[tuple[str, str, int]] = []
        for row in rows:
            if row["source_kind"] in {"archive", "code"}:
                continue
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            if volume_id and (volume_id != str(row["volume_id"]) or file_id != str(row["file_id"])):
                plan_updates.append((volume_id, file_id, int(row["plan_id"])))
        connection.executemany(
            "UPDATE organization_plans SET volume_id=?,file_id=? WHERE plan_id=?",
            plan_updates,
        )
        last_plan_id = int(rows[-1]["plan_id"])


# endregion [01]


# region [02] Incremental cross-format catalog update


def _source_coverage(
    source_kind: SourceKind,
    source_status: str,
    *,
    text_truncated: bool = False,
    container_status: str | None = None,
) -> SourceCoverage:
    """Normalize route-specific status into the catalog coverage contract.

    The catalog may retain a useful partial observation for review, but only
    an explicitly complete producer result is allowed to enter the complete
    coverage state.  This small adapter prevents route-specific values such as
    ``done``, ``indexed`` or ``text_only`` from being interpreted as a fully
    searchable document by downstream consumers.
    """

    if source_kind == "image":
        complete = source_status == "done" and not text_truncated
    elif source_kind == "archive":
        complete = source_status == "indexed" and container_status == "complete"
    elif source_kind == "code":
        complete = source_status == "complete" and not text_truncated
    else:
        complete = source_status in {"complete", "done"} and not text_truncated
    return "complete" if complete else "partial"


def _catalog_source_is_virtual(document: SourceDocument) -> bool:
    """Return whether ``document.path`` is a logical locator, not a file path."""

    return document.virtual


def _catalog_input_root(
    source_root: Path | None,
) -> tuple[Path | None, tuple[int, int, int] | None]:
    if source_root is None:
        return None, None
    root = Path(os.path.abspath(source_root))
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or root.resolve(strict=True) != root:
        raise ValueError(
            "catalog source_root must be a canonical directory without symlink aliases"
        )
    return root, (metadata.st_dev, metadata.st_ino, stat_birthtime_ns(metadata))


def _catalog_path_in_scope(path: str, root: Path) -> bool:
    # Persisted owner anchors, never a parsed archive locator or a resolved link.
    return Path(path).is_absolute() and Path(os.path.abspath(path)).is_relative_to(root)


def _source_document_is_in_scope(document: SourceDocument, root: Path) -> bool:
    if document.resource_binding_json is not None:
        binding = parse_resource_binding(document.resource_binding_json)
        anchor = binding["physical_anchor_path"]
    else:
        anchor = None if document.virtual else document.path
    if anchor is None:
        raise ResourceBindingError(
            "source scope requires a proven physical anchor",
            field="physical_anchor_path",
            encoding="unresolved",
            value=document.file_key,
            code="source_scope_unresolved",
        )
    return _catalog_path_in_scope(anchor, root)


def _preserve_catalog_outside_scope(
    connection: sqlite3.Connection, build: CatalogBuild, root: Path
) -> None:
    """Carry unchanged published rows outside this input into the next generation.

    Untagged historical archive references are retained, not reclassified or
    retired by guessing a physical path. The organization reader keeps these
    unresolved references advisory-only.
    """
    columns = ",".join(_CATALOG_DOCUMENT_COLUMNS)
    rows = connection.execute(
        "SELECT source_kind,file_key,path,resource_binding_json FROM documents "
        "WHERE source_kind=? AND active=1 ORDER BY file_key",
        (build.source_kind,),
    )
    for row in rows:
        raw = row["resource_binding_json"]
        if raw is not None:
            anchor = parse_resource_binding(raw)["physical_anchor_path"]
        else:
            anchor = None if row["source_kind"] == "archive" else str(row["path"])
        if anchor is not None and _catalog_path_in_scope(anchor, root):
            continue
        connection.execute(
            f"INSERT INTO catalog_generation_documents(generation_id,{columns}) "
            f"SELECT ?,{columns} FROM documents WHERE source_kind=? AND file_key=?",
            (build.generation_id, build.source_kind, row["file_key"]),
        )


def update_document_catalog_source(
    catalog_path: Path,
    source_path: Path,
    source_kind: SourceKind,
    *,
    framework_run_id: int | None = None,
    taxonomy_path: Path | None = None,
    max_text_chars: int = MAX_CLASSIFICATION_TEXT_CHARS,
    verify_source_paths: bool = True,
    progress: ProgressCallback | None = None,
    progress_operation: str | None = None,
    cancellation: "CancellationToken | None" = None,
    source_root: Path | None = None,
) -> CatalogUpdateSummary:
    """Classify one source cache incrementally with bounded text sampling."""

    # Resolve through the canonical capability registry before touching the
    # catalog, so a new owner cannot silently enter the generic office reader
    # without declaring its route, state database and consumers.
    content_capability_for_source(source_kind)
    scoped_root, root_identity = _catalog_input_root(source_root)
    if max_text_chars < 1:
        raise ValueError("max_text_chars must be positive")
    max_text_chars = min(max_text_chars, MAX_CLASSIFICATION_TEXT_CHARS)
    taxonomy = load_taxonomy(taxonomy_path)
    initialize_document_catalog(catalog_path)
    with _CATALOG_WRITE_LOCK, document_catalog_database(catalog_path) as catalog:
        build = _begin_catalog_run(
            catalog,
            source_kind=source_kind,
            framework_run_id=framework_run_id,
        )
        if not source_path.is_file():
            summary = CatalogUpdateSummary(
                catalog_run_id=build.catalog_run_id,
                source_kind=source_kind,
                source_missing=True,
            )
            _abandon_catalog_build(catalog, build, summary)
            _emit_catalog_progress(
                progress,
                operation=progress_operation or source_kind,
                source_kind=source_kind,
                completed=0,
                total=0,
                classified=0,
                cache_hits=0,
                errors=0,
                review=0,
                finished=True,
            )
            return summary
        candidates = classified = hits = review = errors = source_stale = 0
        try:
            if scoped_root is not None:
                _preserve_catalog_outside_scope(catalog, build, scoped_root)
            with _readonly_source(source_path) as source:
                candidate_total = _source_document_count(source, source_kind)
                _emit_catalog_progress(
                    progress,
                    operation=progress_operation or source_kind,
                    source_kind=source_kind,
                    completed=0,
                    total=candidate_total,
                    classified=0,
                    cache_hits=0,
                    errors=0,
                    review=0,
                )
                for document in _iter_source_documents(
                    source,
                    source_kind,
                    verify_source_paths=verify_source_paths,
                    source_root=scoped_root,
                ):
                    if cancellation is not None:
                        cancellation.checkpoint()
                    if scoped_root is not None and not _source_document_is_in_scope(
                        document, scoped_root
                    ):
                        continue
                    candidates += 1
                    if (
                        verify_source_paths
                        and not _catalog_source_is_virtual(document)
                        and not _source_snapshot_is_current(document)
                    ):
                        source_stale += 1
                        continue
                    document = _attach_resource_binding(document)
                    if _catalog_cache_hit(catalog, document, taxonomy):
                        _stage_cached_document(catalog, build, document)
                        hits += 1
                    else:
                        try:
                            leading_text = _load_leading_text(
                                source,
                                document,
                                max_text_chars=max_text_chars,
                            )
                            classification = classify_document(
                                DocumentSignals(
                                    source_kind=document.source_kind,
                                    path=document.path,
                                    source_status=document.source_status,
                                    title=document.title,
                                    author=document.author,
                                    metadata=document.metadata,
                                    leading_text=leading_text,
                                    page_count=document.page_count,
                                ),
                                taxonomy,
                            )
                            _store_classification(
                                catalog,
                                build,
                                document,
                                classification,
                            )
                            classified += 1
                            if classification.uncertainty == "alta":
                                review += 1
                        except (UnicodeError, ValueError, zlib.error) as exc:
                            _store_catalog_error(
                                catalog,
                                build,
                                document,
                                taxonomy,
                                exc,
                            )
                            errors += 1
                    catalog.execute(
                        "UPDATE catalog_generation_documents SET resource_binding_json=? "
                        "WHERE generation_id=? AND source_kind=? AND file_key=?",
                        (
                            document.resource_binding_json,
                            build.generation_id,
                            document.source_kind,
                            document.file_key,
                        ),
                    )
                    if candidates % CATALOG_PROGRESS_INTERVAL == 0 or candidates == candidate_total:
                        _emit_catalog_progress(
                            progress,
                            operation=progress_operation or source_kind,
                            source_kind=source_kind,
                            completed=candidates,
                            total=candidate_total,
                            classified=classified,
                            cache_hits=hits,
                            errors=errors,
                            review=review,
                        )
                    if candidates % CATALOG_WRITE_BATCH == 0:
                        catalog.commit()
            summary = CatalogUpdateSummary(
                catalog_run_id=build.catalog_run_id,
                source_kind=source_kind,
                candidates=candidates,
                classified=classified,
                cache_hits=hits,
                review_required=review,
                errors=errors,
                source_stale=source_stale,
            )
            if scoped_root is not None and _catalog_input_root(scoped_root)[1] != root_identity:
                raise RuntimeError("catalog input root identity changed before publication")
            summary = _publish_catalog_build(catalog, build, summary)
            _emit_catalog_progress(
                progress,
                operation=progress_operation or source_kind,
                source_kind=source_kind,
                completed=candidates,
                total=candidate_total,
                classified=classified,
                cache_hits=hits,
                errors=errors,
                review=review,
                finished=True,
            )
            return summary
        except BaseException as exc:
            _fail_catalog_build(catalog, build, exc)
            raise


def _source_document_count(
    connection: sqlite3.Connection,
    source_kind: SourceKind,
) -> int:
    """Count exactly the rows consumed by ``_iter_source_documents``."""

    if source_kind == "pdf":
        predicate = "status IN ('done','partial')"
        parameters: tuple[str, ...] = ()
    elif source_kind == "docx":
        predicate = "status IN ('complete','partial')"
        parameters = ()
    elif source_kind == "audio":
        predicate = "status='complete'"
        parameters = ()
    elif source_kind == "text":
        predicate = "status='complete'"
        parameters = ()
    elif source_kind == "video":
        predicate = "status IN ('complete','partial')"
        parameters = ()
    elif source_kind == "image":
        return int(
            connection.execute("SELECT COUNT(*) FROM images WHERE status='done'").fetchone()[0]
        )
    elif source_kind == "archive":
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM documents AS d
                JOIN containers AS c ON c.container_key=d.container_key
                WHERE c.status IN ('complete','partial')
                AND d.status IN ('indexed','metadata_only','archive')"""
            ).fetchone()[0]
        )
    elif source_kind == "code":
        # Code publishes one current version per file.  Text-only and bounded
        # partial analyses remain useful catalog assets, but are explicitly
        # marked partial by the adapter and can never become a complete
        # published classification.
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM files AS f
                JOIN file_versions AS v ON v.version_id=f.current_version_id
                WHERE f.status='current'
                AND v.analysis_status IN ('complete','partial','text_only','skipped_limit')"""
            ).fetchone()[0]
        )
    else:
        predicate = "format=? AND status='complete'"
        parameters = (source_kind,)
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM documents WHERE {predicate}",
            parameters,
        ).fetchone()[0]
    )


def _emit_catalog_progress(
    progress: ProgressCallback | None,
    *,
    operation: str,
    source_kind: SourceKind,
    completed: int,
    total: int,
    classified: int,
    cache_hits: int,
    errors: int,
    review: int,
    finished: bool = False,
) -> None:
    label = source_kind.upper()
    emit_progress(
        progress,
        ProgressEvent(
            operation,
            f"catalog-{source_kind}",
            f"Catálogo {label} {'actualizado' if finished else 'clasificándose'}",
            completed,
            total,
            "documentos",
            finished,
            (
                ProgressMetric("cache_hits", cache_hits),
                ProgressMetric("classified", classified),
                ProgressMetric("review", review),
                ProgressMetric("errors", errors),
                ProgressMetric("remaining", max(0, total - completed)),
            ),
        ),
    )


def update_document_catalog(
    state_directory: Path,
    *,
    taxonomy_path: Path | None = None,
    framework_run_id: int | None = None,
    source_root: Path | None = None,
) -> tuple[CatalogUpdateSummary, ...]:
    """Update every durable document cache without scanning the filesystem."""

    catalog_path = state_directory / "document_catalog.sqlite3"
    sources: tuple[tuple[Path, SourceKind], ...] = (
        (state_directory / "pdf.sqlite3", "pdf"),
        (state_directory / "docx.sqlite3", "docx"),
        (state_directory / "office.sqlite3", "xlsx"),
        (state_directory / "office.sqlite3", "pptx"),
        (state_directory / "office.sqlite3", "odt"),
        (state_directory / "text.sqlite3", "text"),
        (state_directory / "audio.sqlite3", "audio"),
    )
    # Asset owners are optional in older installations.  Include them when
    # present, without changing the established summaries for installations
    # that have not enabled those routes yet.  Their source-specific adapters
    # below keep their taxonomies separate while sharing this catalog's
    # publication boundary.
    optional_assets: tuple[tuple[Path, SourceKind], ...] = tuple(
        (
            state_directory / content_capability_for_source(source_kind).state_database,
            source_kind,
        )
        for source_kind in ("archive", "code", "image", "video")
        if (state_directory / content_capability_for_source(source_kind).state_database).is_file()
    )
    return tuple(
        update_document_catalog_source(
            catalog_path,
            source_path,
            source_kind,
            framework_run_id=framework_run_id,
            taxonomy_path=taxonomy_path,
            source_root=source_root,
        )
        for source_path, source_kind in (*sources, *optional_assets)
    )


def _begin_catalog_run(
    connection: sqlite3.Connection,
    *,
    source_kind: SourceKind,
    framework_run_id: int | None,
) -> CatalogBuild:
    now = time.time_ns()
    cursor = connection.execute(
        """INSERT INTO catalog_runs(
        framework_run_id,source_kind,mode,status,started_ns)
        VALUES(?,?,'classify','running',?)""",
        (framework_run_id, source_kind, now),
    )
    if cursor.lastrowid is None:
        connection.rollback()
        raise RuntimeError("catalog run insert did not return an identifier")
    catalog_run_id = int(cursor.lastrowid)
    published = connection.execute(
        """SELECT generation_id FROM catalog_publications
        WHERE source_kind=?""",
        (source_kind,),
    ).fetchone()
    base_generation_id = None if published is None else int(published[0])
    generation = connection.execute(
        """INSERT INTO catalog_generations(
        catalog_run_id,source_kind,base_generation_id,status,started_ns)
        VALUES(?,?,?,'building',?)""",
        (catalog_run_id, source_kind, base_generation_id, now),
    )
    if generation.lastrowid is None:
        connection.rollback()
        raise RuntimeError("catalog generation insert did not return an identifier")
    connection.commit()
    return CatalogBuild(
        catalog_run_id=catalog_run_id,
        generation_id=int(generation.lastrowid),
        source_kind=source_kind,
        base_generation_id=base_generation_id,
    )


def _abandon_catalog_build(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    summary: CatalogUpdateSummary,
) -> None:
    now = time.time_ns()
    connection.execute(
        """UPDATE catalog_runs SET status='completed',completed_ns=?,summary_json=?
        WHERE catalog_run_id=?""",
        (
            now,
            json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")),
            summary.catalog_run_id,
        ),
    )
    connection.execute(
        """UPDATE catalog_generations SET status='abandoned',completed_ns=?,
        error_type='SourceMissing',error_message='source cache is unavailable'
        WHERE generation_id=? AND status='building'""",
        (now, build.generation_id),
    )
    connection.commit()


def _fail_catalog_build(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    error: BaseException,
) -> None:
    """Persist failure only after rolling back any unfinished build transaction."""

    connection.rollback()
    now = time.time_ns()
    cancelled = isinstance(error, CancellationRequested)
    generation_status = "cancelled" if cancelled else "failed"
    run_status = "cancelled" if cancelled else "failed"
    connection.execute(
        """UPDATE catalog_runs SET status=?,completed_ns=?,error_type=?,error_message=?
        WHERE catalog_run_id=? AND status='running'""",
        (run_status, now, type(error).__name__, str(error), build.catalog_run_id),
    )
    connection.execute(
        """UPDATE catalog_generations SET status=?,completed_ns=?,error_type=?,
        error_message=? WHERE generation_id=? AND status='building'""",
        (
            generation_status,
            now,
            type(error).__name__,
            str(error),
            build.generation_id,
        ),
    )
    connection.commit()


def _publish_catalog_build(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    summary: CatalogUpdateSummary,
) -> CatalogUpdateSummary:
    """Atomically project a complete generation if its base pointer is current."""

    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        published = connection.execute(
            """SELECT generation_id FROM catalog_publications
            WHERE source_kind=?""",
            (build.source_kind,),
        ).fetchone()
        current_generation_id = None if published is None else int(published[0])
        if current_generation_id != build.base_generation_id:
            now = time.time_ns()
            connection.execute(
                """UPDATE catalog_generations SET status='superseded',completed_ns=?,
                error_type='CatalogPublicationConflict',
                error_message='published generation changed while this build ran'
                WHERE generation_id=? AND status='building'""",
                (now, build.generation_id),
            )
            connection.execute(
                """UPDATE catalog_runs SET status='superseded',completed_ns=?,
                error_type='CatalogPublicationConflict',
                error_message='published generation changed while this build ran'
                WHERE catalog_run_id=? AND status='running'""",
                (now, build.catalog_run_id),
            )
            connection.commit()
            raise CatalogPublicationConflict(
                f"catalog {build.source_kind} publication advanced from "
                f"{build.base_generation_id!r} to {current_generation_id!r}"
            )
        stale = int(
            connection.execute(
                """SELECT COUNT(*) FROM documents AS published_document
                WHERE published_document.source_kind=?
                AND published_document.active=1 AND NOT EXISTS(
                    SELECT 1 FROM catalog_generation_documents AS staged
                    WHERE staged.generation_id=? AND staged.active=1
                    AND staged.source_kind=published_document.source_kind
                    AND staged.file_key=published_document.file_key
                )""",
                (build.source_kind, build.generation_id),
            ).fetchone()[0]
        )
        published_summary = replace(summary, stale_marked=stale)
        now = time.time_ns()
        _replace_catalog_projection(connection, build, now=now)
        if build.base_generation_id is None:
            cursor = connection.execute(
                """INSERT INTO catalog_publications(
                source_kind,generation_id,published_ns) VALUES(?,?,?)
                ON CONFLICT(source_kind) DO NOTHING""",
                (build.source_kind, build.generation_id, now),
            )
        else:
            cursor = connection.execute(
                """UPDATE catalog_publications SET generation_id=?,published_ns=?
                WHERE source_kind=? AND generation_id=?""",
                (
                    build.generation_id,
                    now,
                    build.source_kind,
                    build.base_generation_id,
                ),
            )
        if cursor.rowcount != 1:
            raise CatalogPublicationConflict(
                f"catalog {build.source_kind} publication compare-and-swap failed"
            )
        connection.execute(
            """UPDATE catalog_generations SET status='published',completed_ns=?,
            published_ns=? WHERE generation_id=? AND status='building'""",
            (now, now, build.generation_id),
        )
        connection.execute(
            """UPDATE catalog_runs SET status='completed',completed_ns=?,summary_json=?
            WHERE catalog_run_id=? AND status='running'""",
            (
                now,
                json.dumps(asdict(published_summary), sort_keys=True, separators=(",", ":")),
                build.catalog_run_id,
            ),
        )
        connection.commit()
        return published_summary
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _replace_catalog_projection(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    *,
    now: int,
) -> None:
    """Replace the compatible current projection inside the publish transaction."""

    connection.execute(
        f"""UPDATE documents SET active=0,updated_ns=?
        WHERE source_kind<>? AND active=1 AND EXISTS(
            SELECT 1 FROM catalog_generation_documents AS staged
            WHERE staged.generation_id=? AND staged.active=1
            AND staged.path=documents.path COLLATE {_PATH_COLLATION})""",
        (now, build.source_kind, build.generation_id),
    )
    connection.execute(
        """UPDATE documents SET active=0,updated_ns=?
        WHERE source_kind=? AND active=1""",
        (now, build.source_kind),
    )
    columns = ",".join(_CATALOG_DOCUMENT_COLUMNS)
    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in _CATALOG_DOCUMENT_COLUMNS
        if column not in {"source_kind", "file_key"}
    )
    connection.execute(
        f"""INSERT INTO documents({columns})
        SELECT {columns} FROM catalog_generation_documents
        WHERE generation_id=?
        ON CONFLICT(source_kind,file_key) DO UPDATE SET {updates}""",
        (build.generation_id,),
    )
    connection.execute(
        """INSERT INTO classification_history(
        source_kind,file_key,processing_signature,text_fingerprint,
        classifier_signature,path,classification_json,classified_ns)
        SELECT source_kind,file_key,processing_signature,COALESCE(text_fingerprint,''),
        classifier_signature,path,classification_json,updated_ns
        FROM catalog_generation_documents AS staged
        WHERE generation_id=? AND catalog_status<>'error'
        ON CONFLICT(source_kind,file_key,processing_signature,text_fingerprint,
        classifier_signature,path) DO NOTHING""",
        (build.generation_id,),
    )
    connection.execute(
        """UPDATE organization_plans SET status='superseded',completed_ns=?,
        detail='source is no longer active in the technical catalog'
        WHERE status='planned' AND NOT EXISTS(
            SELECT 1 FROM documents AS published_document
            WHERE published_document.source_kind=organization_plans.source_kind
            AND published_document.file_key=organization_plans.file_key
            AND published_document.active=1)""",
        (now,),
    )


# endregion [02]


# region [03] Source readers and bounded decompression


@contextmanager
def _readonly_source(path: Path):
    try:
        mode = preferred_sqlite_read_mode(path)
        with sqlite_read_session(path, mode=mode, timeout_seconds=60.0) as connection:
            yield connection
    except CancellationRequested:
        # Cancellation is a control signal, not a source-reader failure;
        # preserve it so the surrounding catalog transaction can roll back
        # without changing the public cancellation contract.
        raise
    except Exception as exc:
        # Keep the catalog adapter's established error surface while ensuring
        # all filesystem-sensitive reads are owned by SQLiteReadSession.
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"catalog source reader unavailable: {exc}") from exc


def _iter_source_documents(
    connection: sqlite3.Connection,
    source_kind: SourceKind,
    *,
    verify_source_paths: bool = False,
    source_root: Path | None = None,
) -> Iterator[SourceDocument]:
    if source_kind == "pdf":
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,normalized_text_xxh3_128,metadata_json,page_count
            FROM documents WHERE status IN ('done','partial') ORDER BY path"""
        )
        for row in rows:
            metadata = _json_mapping(row["metadata_json"])
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            yield SourceDocument(
                source_kind="pdf",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                volume_id=volume_id,
                file_id=file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=str(row["status"]),
                processing_signature=str(row["processing_signature"]),
                text_fingerprint=(
                    None
                    if row["normalized_text_xxh3_128"] is None
                    else str(row["normalized_text_xxh3_128"])
                ),
                title=str(metadata.get("title") or ""),
                author=str(metadata.get("author") or ""),
                metadata=_metadata_text(metadata),
                page_count=(None if row["page_count"] is None else int(row["page_count"])),
                coverage=_source_coverage("pdf", str(row["status"])),
            )
        return
    if source_kind == "docx":
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,text_xxh3_128,title,author,created,modified
            FROM documents WHERE status IN ('complete','partial') ORDER BY path"""
        )
    elif source_kind == "text":
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,text_xxh3_128,title,author,metadata_json
            FROM documents WHERE status='complete' ORDER BY path"""
        )
        for row in rows:
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            yield SourceDocument(
                source_kind="text",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                volume_id=volume_id,
                file_id=file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=str(row["status"]),
                processing_signature=str(row["processing_signature"]),
                text_fingerprint=(
                    None if row["text_xxh3_128"] is None else str(row["text_xxh3_128"])
                ),
                title=str(row["title"] or ""),
                author=str(row["author"] or ""),
                metadata=_metadata_text(_json_mapping(row["metadata_json"])),
            )
        return
    elif source_kind == "audio":
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,text_xxh3_128,title,language,duration_seconds,
            speech_duration_seconds,model_name,backend_version,
            media_metadata_json FROM documents
            WHERE status='complete' ORDER BY path"""
        )
        for row in rows:
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            metadata = _json_mapping(row["media_metadata_json"])
            metadata.update(
                language=row["language"],
                duration_seconds=row["duration_seconds"],
                speech_duration_seconds=row["speech_duration_seconds"],
                model_name=row["model_name"],
                backend_version=row["backend_version"],
            )
            yield SourceDocument(
                source_kind="audio",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                volume_id=volume_id,
                file_id=file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=str(row["status"]),
                processing_signature=str(row["processing_signature"]),
                text_fingerprint=(
                    None if row["text_xxh3_128"] is None else str(row["text_xxh3_128"])
                ),
                title=str(row["title"] or ""),
                author="",
                metadata=_metadata_text(metadata),
            )
        return
    elif source_kind == "video":
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,title,duration_seconds,format_name,
            video_streams,audio_streams,subtitle_streams,chapters,frame_count,
            ocr_frame_count,ocr_text_chars,probe_json,audio_status
            FROM documents WHERE status IN ('complete','partial') ORDER BY path"""
        )
        for row in rows:
            status = str(row["status"])
            frame_count = int(row["frame_count"] or 0)
            ocr_frame_count = int(row["ocr_frame_count"] or 0)
            ocr_text_chars = int(row["ocr_text_chars"] or 0)
            probe = str(row["probe_json"] or "{}")
            fingerprint = hashlib.sha256(
                f"{frame_count}:{ocr_frame_count}:{ocr_text_chars}:{probe}".encode(
                    "utf-8", "surrogatepass"
                )
            ).hexdigest()
            metadata = {
                "duration_seconds": row["duration_seconds"],
                "format_name": row["format_name"],
                "video_streams": row["video_streams"],
                "audio_streams": row["audio_streams"],
                "subtitle_streams": row["subtitle_streams"],
                "chapters": row["chapters"],
                "frame_count": frame_count,
                "ocr_frame_count": ocr_frame_count,
                "ocr_text_chars": ocr_text_chars,
                "audio_status": row["audio_status"],
            }
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            yield SourceDocument(
                source_kind="video",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                volume_id=volume_id,
                file_id=file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=status,
                processing_signature=str(row["processing_signature"]),
                text_fingerprint=fingerprint,
                title=str(row["title"] or ""),
                author="",
                metadata=_metadata_text(metadata),
                coverage=_source_coverage("video", status),
            )
        return
    elif source_kind == "image":
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,mime,category,confidence,ocr_text_xxh3_128,
            ocr_text_truncated,decode_quality,decode_provenance
            FROM images WHERE status='done' ORDER BY path"""
        )
        for row in rows:
            truncated = bool(row["ocr_text_truncated"])
            status = str(row["status"])
            metadata = {
                "mime": row["mime"],
                "category": row["category"],
                "confidence": row["confidence"],
                "decode_quality": row["decode_quality"],
                "decode_provenance": row["decode_provenance"],
                "ocr_text_truncated": truncated,
            }
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            yield SourceDocument(
                source_kind="image",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                volume_id=volume_id,
                file_id=file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=status,
                processing_signature=str(row["processing_signature"] or "image-state-v6"),
                text_fingerprint=(
                    None if row["ocr_text_xxh3_128"] is None else str(row["ocr_text_xxh3_128"])
                ),
                title=Path(str(row["path"])).stem,
                author="",
                metadata=_metadata_text(metadata),
                coverage=_source_coverage(
                    "image",
                    status,
                    text_truncated=truncated,
                ),
                text_truncated=truncated,
            )
        return
    elif source_kind == "archive":
        archive_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")
        }
        container_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(containers)")
        }
        anchor_projection = ",".join(
            f"c.{field} AS anchor_{field}"
            if field in container_columns
            else f"NULL AS anchor_{field}"
            for field in ("size", "mtime_ns", "birthtime_ns")
        )
        role_projection = (
            "d.document_role,d.logical_document_chain,d.independently_organizable,d.independently_disposable"
            if "document_role" in archive_columns
            else "'archive_member' AS document_role,NULL AS logical_document_chain,"
            "0 AS independently_organizable,0 AS independently_disposable"
        )
        rows = connection.execute(
            f"""SELECT d.file_key,d.path,d.container_path,d.member_chain,
            d.member_path,d.size,d.mtime_ns,d.birthtime_ns,d.status,
            d.processing_signature,d.text_xxh3_128,d.content_kind,d.media_type,
            c.status AS container_status,c.container_key,c.path AS anchor_path,
            {anchor_projection},
            {role_projection}
            FROM documents AS d JOIN containers AS c
            ON c.container_key=d.container_key
            WHERE c.status IN ('complete','partial')
            AND d.status IN ('indexed','metadata_only','archive')
            ORDER BY d.path"""
        )
        for row in rows:
            status = str(row["status"])
            container_status = str(row["container_status"])
            member_path = str(row["member_path"])
            title = member_path.rsplit("/", 1)[-1]
            metadata = {
                "container_path": row["container_path"],
                "member_chain": row["member_chain"],
                "content_kind": row["content_kind"],
                "media_type": row["media_type"],
                "member_status": status,
                "container_status": container_status,
                "document_role": row["document_role"],
                "logical_document_chain": row["logical_document_chain"],
                "independently_organizable": bool(row["independently_organizable"]),
                "independently_disposable": bool(row["independently_disposable"]),
            }
            volume_id, file_id = _split_file_key(str(row["file_key"]))
            anchor_identity = (
                decode_file_identity(
                    str(row["container_key"]), encoding=FileIdentityEncoding.PACKED_HEX_V1
                )
                if all(
                    row[f"anchor_{field}"] is not None
                    for field in ("size", "mtime_ns", "birthtime_ns")
                )
                else None
            )
            logical_root = row["document_role"] == "logical_document" and row["member_chain"] == ""
            if logical_root:
                if anchor_identity is None:
                    raise ResourceBindingError(
                        "logical outer document lacks its physical anchor",
                        field="container_key",
                        encoding="packed-hex-v1",
                        value=row["container_key"],
                    )
                volume_id, file_id = anchor_identity.decimal_components
            binding = build_resource_binding(
                source_kind="archive",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                identity=anchor_identity,
                birthtime_ns=-1 if anchor_identity is None else int(row["anchor_birthtime_ns"]),
                size=0 if anchor_identity is None else int(row["anchor_size"]),
                mtime_ns=0 if anchor_identity is None else int(row["anchor_mtime_ns"]),
                representation_kind="physical_file" if logical_root else "archive_member",
                anchor_path=str(row["anchor_path"]),
                archive_member=None
                if logical_root
                else {
                    "container_key": str(row["container_key"]),
                    "container_path": str(row["anchor_path"]),
                    "member_chain": str(row["member_chain"]),
                },
                representation_metadata={
                    key: metadata[key]
                    for key in (
                        "document_role",
                        "logical_document_chain",
                        "independently_organizable",
                        "independently_disposable",
                    )
                },
            )
            yield SourceDocument(
                source_kind="archive",
                file_key=str(row["file_key"]),
                path=str(row["path"]),
                volume_id=volume_id,
                file_id=file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=status,
                processing_signature=str(row["processing_signature"]),
                text_fingerprint=(
                    None if row["text_xxh3_128"] is None else str(row["text_xxh3_128"])
                ),
                title=title,
                author="",
                metadata=_metadata_text(metadata),
                coverage=_source_coverage(
                    "archive",
                    status,
                    container_status=container_status,
                ),
                virtual=not logical_root,
                resource_binding_json=json.dumps(binding, sort_keys=True, separators=(",", ":")),
            )
        return
    elif source_kind == "code":
        rows = connection.execute(
            """SELECT f.file_id,f.volume_id,f.physical_file_id,f.current_path,
            v.size,v.mtime_ns,v.birthtime_ns,v.analysis_status,
            v.processing_signature,v.language,v.artifact_kind,v.text_xxh3_128,
            v.text_truncated,v.version_id,v.provenance_json
            FROM files AS f JOIN file_versions AS v
            ON v.version_id=f.current_version_id
            WHERE f.status='current'
            AND v.analysis_status IN ('complete','partial','text_only','skipped_limit')
            ORDER BY f.current_path"""
        )
        for row in rows:
            if source_root is not None and not _catalog_path_in_scope(
                str(row["current_path"]), source_root
            ):
                continue
            status = str(row["analysis_status"])
            file_key = f"code:{int(row['file_id'])}"
            identity = _code_source_identity(
                connection, row, verify_source_paths=verify_source_paths
            )
            volume_id, physical_file_id = identity.decimal_components
            metadata = {
                "language": row["language"],
                "artifact_kind": row["artifact_kind"],
                "version_id": row["version_id"],
                "text_truncated": bool(row["text_truncated"]),
            }
            yield SourceDocument(
                source_kind="code",
                file_key=file_key,
                path=str(row["current_path"]),
                volume_id=volume_id,
                file_id=physical_file_id,
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                birthtime_ns=int(row["birthtime_ns"]),
                source_status=status,
                processing_signature=str(row["processing_signature"]),
                text_fingerprint=(
                    None if row["text_xxh3_128"] is None else str(row["text_xxh3_128"])
                ),
                title=Path(str(row["current_path"])).stem,
                author="",
                metadata=_metadata_text(metadata),
                coverage=_source_coverage(
                    "code",
                    status,
                    text_truncated=bool(row["text_truncated"]),
                ),
                text_truncated=bool(row["text_truncated"]),
            )
        return
    else:
        rows = connection.execute(
            """SELECT file_key,path,size,mtime_ns,birthtime_ns,status,
            processing_signature,text_xxh3_128,title,author,NULL AS created,
            NULL AS modified,subject FROM documents
            WHERE format=? AND status='complete' ORDER BY path""",
            (source_kind,),
        )
    for row in rows:
        volume_id, file_id = _split_file_key(str(row["file_key"]))
        metadata = {
            "title": row["title"],
            "author": row["author"],
            "created": row["created"],
            "modified": row["modified"],
        }
        if source_kind != "docx":
            metadata["subject"] = row["subject"]
        yield SourceDocument(
            source_kind=source_kind,
            file_key=str(row["file_key"]),
            path=str(row["path"]),
            volume_id=volume_id,
            file_id=file_id,
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            birthtime_ns=int(row["birthtime_ns"]),
            source_status=str(row["status"]),
            processing_signature=str(row["processing_signature"]),
            text_fingerprint=(None if row["text_xxh3_128"] is None else str(row["text_xxh3_128"])),
            title=str(row["title"] or ""),
            author=str(row["author"] or ""),
            metadata=_metadata_text(metadata),
            coverage=_source_coverage(source_kind, str(row["status"])),
        )


def _load_leading_text(
    connection: sqlite3.Connection,
    document: SourceDocument,
    *,
    max_text_chars: int,
) -> str:
    if document.source_kind == "video":
        # Video OCR is stored in FTS rows rather than a document blob.  Read a
        # bounded prefix in timestamp order so a long recording cannot turn a
        # catalog pass into an unbounded memory operation.
        video_chunks: list[str] = []
        remaining = max_text_chars
        rows = connection.execute(
            """SELECT body FROM frame_fts WHERE file_key=?
            ORDER BY timestamp_ms,rowid""",
            (document.file_key,),
        )
        for row in rows:
            if remaining <= 0:
                break
            text = str(row[0] or "")[:remaining]
            if text:
                video_chunks.append(text)
                remaining -= len(text)
        return "\n".join(video_chunks)
    if document.source_kind == "code":
        # Code keeps the current file version and may retain either the
        # bounded source blob or chunk rows, depending on the analyzer.  The
        # fallback preserves useful path/symbol evidence without inventing a
        # complete source when the producer only published text-only output.
        raw_file_id = document.file_key.removeprefix("code:")
        try:
            file_id = int(raw_file_id)
        except ValueError as exc:
            raise RuntimeError("code catalog identity is malformed") from exc
        row = connection.execute(
            """SELECT v.version_id,v.text_zlib FROM files AS f
            JOIN file_versions AS v ON v.version_id=f.current_version_id
            WHERE f.file_id=? AND f.status='current'""",
            (file_id,),
        ).fetchone()
        if row is None:
            return ""
        if row["text_zlib"] is not None:
            return _decompress_prefix(bytes(row["text_zlib"]), max_text_chars)
        code_chunks: list[str] = []
        remaining = max_text_chars
        for chunk in connection.execute(
            """SELECT text FROM code_chunks WHERE version_id=?
            ORDER BY chunk_index""",
            (int(row["version_id"]),),
        ):
            if remaining <= 0:
                break
            text = str(chunk[0] or "")[:remaining]
            if text:
                code_chunks.append(text)
                remaining -= len(text)
        return "\n".join(code_chunks)
    if document.source_kind == "image":
        row = connection.execute(
            "SELECT ocr_text_zlib FROM images WHERE file_key=?",
            (document.file_key,),
        ).fetchone()
        if row is None or row[0] is None:
            return ""
        return _decompress_prefix(bytes(row[0]), max_text_chars)
    if document.source_kind != "pdf":
        row = connection.execute(
            "SELECT text_zlib FROM documents WHERE file_key=?",
            (document.file_key,),
        ).fetchone()
        if row is None or row[0] is None:
            return ""
        return _decompress_prefix(bytes(row[0]), max_text_chars)
    pdf_chunks: list[str] = []
    remaining = max_text_chars
    rows = connection.execute(
        """SELECT text_zlib FROM pages WHERE file_key=?
        ORDER BY page_number""",
        (document.file_key,),
    )
    for row in rows:
        if remaining <= 0:
            break
        text = _decompress_prefix(bytes(row[0]), remaining)
        pdf_chunks.append(text)
        remaining -= len(text)
    return "\n".join(pdf_chunks)


def _decompress_prefix(blob: bytes, max_chars: int) -> str:
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(blob, max_chars * 4 + 4)
    utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    text = utf8_decoder.decode(decoded, final=False)
    return text[:max_chars]


def _json_mapping(value: object) -> dict[str, object]:
    if value is None:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _metadata_text(metadata: dict[str, object]) -> str:
    return " ".join(
        f"{key}={value}" for key, value in sorted(metadata.items()) if value not in (None, "")
    )[:20_000]


def _split_file_key(file_key: str) -> tuple[str, str]:
    try:
        return decode_file_identity(file_key).decimal_components
    except ValueError:
        # Archive members and Code files use owner-scoped stable identities,
        # not filesystem volume/inode keys.  Keep those identities intact in
        # the catalog instead of guessing numeric components or discarding the
        # source owner boundary.
        for owner in ("archive", "code"):
            prefix = f"{owner}:"
            if file_key.startswith(prefix) and len(file_key) > len(prefix):
                return owner, file_key[len(prefix) :]
        raise


def _code_source_identity(
    connection: sqlite3.Connection, row: sqlite3.Row, *, verify_source_paths: bool
) -> FileIdentity:
    """Decode the Code owner codec; legacy decimal needs independent evidence."""
    volume, inode = str(row["volume_id"]), str(row["physical_file_id"])
    hexadecimal = physical_identity_from_components(volume, inode, encoding="code-owner-hex")
    metadata = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    versions = (
        []
        if metadata is None
        else connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    )
    if len(versions) > 1 or (
        versions and str(versions[0][0]) not in {str(number) for number in range(1, 8)}
    ):
        raise ResourceBindingError(
            "Code owner schema is unsupported",
            field="schema_version",
            encoding="code-owner-schema",
            value=versions,
        )
    if versions and str(versions[0][0]) == "7":
        # A declared current producer always owns hex. Never reinterpret a
        # stale current identity as decimal just because that matches a path.
        return hexadecimal
    if verify_source_paths:
        try:
            current = os.stat(str(row["current_path"]), follow_symlinks=False)
        except OSError:
            return hexadecimal  # the caller records this observation as stale
        if (hexadecimal.volume_id, hexadecimal.file_id) == (current.st_dev, current.st_ino):
            return hexadecimal
        try:
            decimal = physical_identity_from_components(volume, inode, encoding="legacy-decimal")
        except ResourceBindingError:
            return hexadecimal
        if (decimal.volume_id, decimal.file_id) == (current.st_dev, current.st_ino):
            return decimal
        return hexadecimal  # neither codec matches: the snapshot check must fail
    try:
        decimal = physical_identity_from_components(volume, inode, encoding="legacy-decimal")
    except ResourceBindingError:
        return hexadecimal  # alphabetic hex is not a valid legacy decimal identity
    if decimal != hexadecimal:
        raise ResourceBindingError(
            "legacy Code identity encoding requires owner or physical evidence",
            field="volume_id,file_id",
            encoding="unresolved",
            value=[volume, inode],
            code="identity_encoding_unresolved",
        )
    return hexadecimal


def _attach_resource_binding(document: SourceDocument) -> SourceDocument:
    if document.resource_binding_json is not None:
        return document
    identity = physical_identity_from_components(
        document.volume_id, document.file_id, encoding="legacy-decimal"
    )
    binding = build_resource_binding(
        source_kind=document.source_kind,
        file_key=document.file_key,
        path=document.path,
        identity=identity,
        birthtime_ns=document.birthtime_ns,
        size=document.size,
        mtime_ns=document.mtime_ns,
        representation_kind="physical_file",
    )
    return replace(
        document, resource_binding_json=json.dumps(binding, sort_keys=True, separators=(",", ":"))
    )


def _source_snapshot_is_current(document: SourceDocument) -> bool:
    try:
        stat = os.stat(document.path, follow_symlinks=False)
    except OSError:
        return False
    birthtime_ns = stat_birthtime_ns(stat)
    # Every physical adapter has already resolved its owner's codec into
    # neutral decimal components. This check must not guess another radix.
    identity_matches = document.volume_id == str(stat.st_dev) and document.file_id == str(
        stat.st_ino
    )
    return (
        identity_matches
        and int(stat.st_size) == document.size
        and int(stat.st_mtime_ns) == document.mtime_ns
        and int(birthtime_ns) == document.birthtime_ns
    )


# endregion [03]


# region [04] Cache validation and persistence


def _catalog_cache_hit(
    connection: sqlite3.Connection,
    document: SourceDocument,
    taxonomy: TechnicalTaxonomy,
) -> bool:
    row = connection.execute(
        """SELECT path,size,mtime_ns,birthtime_ns,source_status,
        processing_signature,text_fingerprint,classifier_signature,catalog_status,resource_binding_json
        FROM documents WHERE source_kind=? AND file_key=?""",
        (document.source_kind, document.file_key),
    ).fetchone()
    if row is None or str(row["catalog_status"]) == "error":
        return False
    classifier_signature = document_classifier_signature(taxonomy)
    return (
        row["resource_binding_json"] == document.resource_binding_json
        and row["resource_binding_json"] is not None
        and
        # Do not reuse a legacy cache row that predates the coverage contract:
        # partial or truncated producer output must be reclassified so its
        # published catalog status is downgraded to ``review``.
        not (document.coverage != "complete" and str(row["catalog_status"]) == "classified")
        and _catalog_paths_equal(str(row["path"]), document.path)
        and int(row["size"]) == document.size
        and int(row["mtime_ns"]) == document.mtime_ns
        and int(row["birthtime_ns"]) == document.birthtime_ns
        and str(row["source_status"]) == document.source_status
        and str(row["processing_signature"]) == document.processing_signature
        and row["text_fingerprint"] == document.text_fingerprint
        and str(row["classifier_signature"]) == classifier_signature
    )


def _catalog_paths_equal(left: str, right: str) -> bool:
    if _PATH_COLLATION == "BINARY":
        return left == right
    return left.casefold() == right.casefold()


def _stage_cached_document(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    document: SourceDocument,
) -> None:
    """Copy a cache hit into the isolated build without changing publication."""

    columns = ",".join(_CATALOG_DOCUMENT_COLUMNS)
    selected = ",".join(
        "1"
        if column == "active"
        else "?"
        if column in {"last_seen_catalog_run_id", "updated_ns"}
        else column
        for column in _CATALOG_DOCUMENT_COLUMNS
    )
    connection.execute(
        f"""INSERT INTO catalog_generation_documents(generation_id,{columns})
        SELECT ?,{selected} FROM documents
        WHERE source_kind=? AND file_key=?""",
        (
            build.generation_id,
            build.catalog_run_id,
            time.time_ns(),
            document.source_kind,
            document.file_key,
        ),
    )


def _store_classification(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    document: SourceDocument,
    classification: DocumentClassification,
) -> None:
    now = time.time_ns()
    serialized = json.dumps(
        asdict(classification),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    references = json.dumps(
        [asdict(value) for value in classification.standard_references],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    organizations = json.dumps(
        [asdict(value) for value in classification.organizations],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    clients = json.dumps(
        [asdict(value) for value in classification.clients],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    projects = json.dumps(
        [asdict(value) for value in classification.projects],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    workstreams = json.dumps(
        [asdict(value) for value in classification.workstreams],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    topics = json.dumps(
        [asdict(value) for value in classification.topics],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    equipment = json.dumps(
        [asdict(value) for value in classification.equipment],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    activities = json.dumps(
        [asdict(value) for value in classification.activities],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # A partial producer observation is retained for evidence and review, but
    # it must not be published as a complete catalog classification.  Keep the
    # existing ``review`` contract so Knowledge and organization readers
    # abstain without introducing a second status vocabulary in the schema.
    catalog_status = (
        "review"
        if classification.uncertainty == "alta" or document.coverage != "complete"
        else "classified"
    )
    connection.execute(
        """INSERT INTO catalog_generation_documents(
        generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,birthtime_ns,
        source_status,processing_signature,text_fingerprint,classifier_signature,
        primary_kind,primary_subtype,primary_authority,primary_organization,
        primary_client,primary_project,primary_workstream,confidence,uncertainty,
        standard_references_json,organizations_json,clients_json,projects_json,
        workstreams_json,topics_json,equipment_json,activities_json,
        classification_json,catalog_status,
        error_type,error_message,active,last_seen_catalog_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
        NULL,NULL,1,?,?)
        ON CONFLICT(generation_id,source_kind,file_key) DO UPDATE SET
        path=excluded.path,volume_id=excluded.volume_id,file_id=excluded.file_id,
        size=excluded.size,mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
        source_status=excluded.source_status,
        processing_signature=excluded.processing_signature,
        text_fingerprint=excluded.text_fingerprint,
        classifier_signature=excluded.classifier_signature,
        primary_kind=excluded.primary_kind,
        primary_subtype=excluded.primary_subtype,
        primary_authority=excluded.primary_authority,
        primary_organization=excluded.primary_organization,
        primary_client=excluded.primary_client,
        primary_project=excluded.primary_project,
        primary_workstream=excluded.primary_workstream,
        confidence=excluded.confidence,uncertainty=excluded.uncertainty,
        standard_references_json=excluded.standard_references_json,
        organizations_json=excluded.organizations_json,
        clients_json=excluded.clients_json,projects_json=excluded.projects_json,
        workstreams_json=excluded.workstreams_json,topics_json=excluded.topics_json,
        equipment_json=excluded.equipment_json,activities_json=excluded.activities_json,
        classification_json=excluded.classification_json,
        catalog_status=excluded.catalog_status,error_type=NULL,error_message=NULL,
        active=1,last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
        updated_ns=excluded.updated_ns""",
        (
            build.generation_id,
            document.source_kind,
            document.file_key,
            document.path,
            document.volume_id,
            document.file_id,
            document.size,
            document.mtime_ns,
            document.birthtime_ns,
            document.source_status,
            document.processing_signature,
            document.text_fingerprint,
            classification.classifier_signature,
            classification.primary_kind,
            classification.primary_subtype,
            classification.primary_authority,
            classification.primary_organization,
            classification.primary_client,
            classification.primary_project,
            classification.primary_workstream,
            classification.confidence,
            classification.uncertainty,
            references,
            organizations,
            clients,
            projects,
            workstreams,
            topics,
            equipment,
            activities,
            serialized,
            catalog_status,
            build.catalog_run_id,
            now,
        ),
    )


def _store_catalog_error(
    connection: sqlite3.Connection,
    build: CatalogBuild,
    document: SourceDocument,
    taxonomy: TechnicalTaxonomy,
    error: BaseException,
) -> None:
    now = time.time_ns()
    connection.execute(
        """INSERT INTO catalog_generation_documents(
        generation_id,source_kind,file_key,path,volume_id,file_id,size,mtime_ns,birthtime_ns,
        source_status,processing_signature,text_fingerprint,classifier_signature,
        primary_kind,primary_client,primary_project,primary_workstream,
        confidence,uncertainty,standard_references_json,organizations_json,
        clients_json,projects_json,workstreams_json,topics_json,
        equipment_json,activities_json,classification_json,catalog_status,
        error_type,error_message,active,last_seen_catalog_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'otro',NULL,NULL,NULL,0.0,'alta',
        '[]','[]','[]','[]','[]','[]','[]','[]','{}','error',?,?,1,?,?)
        ON CONFLICT(generation_id,source_kind,file_key) DO UPDATE SET path=excluded.path,
        size=excluded.size,mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
        source_status=excluded.source_status,
        processing_signature=excluded.processing_signature,
        text_fingerprint=excluded.text_fingerprint,
        classifier_signature=excluded.classifier_signature,primary_kind='otro',
        primary_subtype=NULL,primary_authority=NULL,primary_organization=NULL,
        primary_client=NULL,primary_project=NULL,primary_workstream=NULL,
        confidence=0.0,
        uncertainty='alta',standard_references_json='[]',organizations_json='[]',
        clients_json='[]',projects_json='[]',workstreams_json='[]',topics_json='[]',
        equipment_json='[]',activities_json='[]',
        classification_json='{}',catalog_status='error',
        error_type=excluded.error_type,error_message=excluded.error_message,
        active=1,last_seen_catalog_run_id=excluded.last_seen_catalog_run_id,
        updated_ns=excluded.updated_ns""",
        (
            build.generation_id,
            document.source_kind,
            document.file_key,
            document.path,
            document.volume_id,
            document.file_id,
            document.size,
            document.mtime_ns,
            document.birthtime_ns,
            document.source_status,
            document.processing_signature,
            document.text_fingerprint,
            document_classifier_signature(taxonomy),
            type(error).__name__,
            str(error),
            build.catalog_run_id,
            now,
        ),
    )


# endregion [04]


# region [05] Bounded read-only catalog inspection


def _catalog_query_predicates(
    columns: set[str],
    *,
    primary_kind: str | None,
    authority: str | None,
    organization: str | None,
    client: str | None,
    project: str | None,
    workstream: str | None,
) -> tuple[list[str], list[object]] | None:
    clauses = ["active=1"]
    parameters: list[object] = []
    for column, value in (
        ("primary_kind", primary_kind),
        ("primary_authority", authority),
        ("primary_organization", organization),
    ):
        if value is not None:
            clauses.append(f"{column}=? COLLATE NOCASE")
            parameters.append(value)
    for column, value in (
        ("primary_client", client),
        ("primary_project", project),
        ("primary_workstream", workstream),
    ):
        if value is None:
            continue
        if column not in columns:
            return None
        clauses.append(f"{column}=? COLLATE NOCASE")
        parameters.append(value)
    return clauses, parameters


def _catalog_projection_column(
    columns: set[str],
    column: str,
    fallback: str,
) -> str:
    return column if column in columns else f"{fallback} AS {column}"


def _catalog_document_rows(
    connection: sqlite3.Connection,
    columns: set[str],
    clauses: list[str],
    parameters: list[object],
    limit: int,
) -> list[sqlite3.Row]:
    subtype_column = _catalog_projection_column(columns, "primary_subtype", "NULL")
    equipment_column = _catalog_projection_column(columns, "equipment_json", "'[]'")
    activities_column = _catalog_projection_column(columns, "activities_json", "'[]'")
    client_column = _catalog_projection_column(columns, "primary_client", "NULL")
    project_column = _catalog_projection_column(columns, "primary_project", "NULL")
    workstream_column = _catalog_projection_column(columns, "primary_workstream", "NULL")
    clients_column = _catalog_projection_column(columns, "clients_json", "'[]'")
    projects_column = _catalog_projection_column(columns, "projects_json", "'[]'")
    workstreams_column = _catalog_projection_column(columns, "workstreams_json", "'[]'")
    return connection.execute(
        f"""SELECT source_kind,path,primary_kind,{subtype_column},
        primary_authority,primary_organization,{client_column},{project_column},
        {workstream_column},standard_references_json,{clients_column},
        {projects_column},{workstreams_column},
        topics_json,{equipment_column},{activities_column},
        confidence,uncertainty,catalog_status FROM documents
        WHERE {" AND ".join(clauses)}
        ORDER BY primary_kind,primary_client,primary_project,
        primary_authority,primary_organization,path
        LIMIT ?""",
        (*parameters, limit),
    ).fetchall()


def _catalog_optional_text(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    return None if value is None else str(value)


def _catalog_document_view(row: sqlite3.Row) -> CatalogDocumentView:
    return CatalogDocumentView(
        source_kind=str(row["source_kind"]),
        path=str(row["path"]),
        primary_kind=str(row["primary_kind"]),
        primary_subtype=_catalog_optional_text(row, "primary_subtype"),
        primary_authority=_catalog_optional_text(row, "primary_authority"),
        primary_organization=_catalog_optional_text(row, "primary_organization"),
        primary_client=_catalog_optional_text(row, "primary_client"),
        primary_project=_catalog_optional_text(row, "primary_project"),
        primary_workstream=_catalog_optional_text(row, "primary_workstream"),
        standard_identifiers=_json_labels(
            row["standard_references_json"],
            "identifier",
        ),
        clients=_json_labels(row["clients_json"], "label"),
        projects=_json_labels(row["projects_json"], "label"),
        workstreams=_json_labels(row["workstreams_json"], "label"),
        topics=_json_labels(row["topics_json"], "label"),
        equipment=_json_labels(row["equipment_json"], "label"),
        activities=_json_labels(row["activities_json"], "label"),
        confidence=float(row["confidence"]),
        uncertainty=str(row["uncertainty"]),
        catalog_status=str(row["catalog_status"]),
    )


def list_catalog_documents(
    catalog_path: Path,
    *,
    limit: int,
    primary_kind: str | None = None,
    authority: str | None = None,
    organization: str | None = None,
    client: str | None = None,
    project: str | None = None,
    workstream: str | None = None,
) -> tuple[CatalogDocumentView, ...]:
    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    with document_catalog_database(catalog_path, readonly=True) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(documents)")}
        predicates = _catalog_query_predicates(
            columns,
            primary_kind=primary_kind,
            authority=authority,
            organization=organization,
            client=client,
            project=project,
            workstream=workstream,
        )
        if predicates is None:
            return ()
        clauses, parameters = predicates
        rows = _catalog_document_rows(
            connection,
            columns,
            clauses,
            parameters,
            limit,
        )
        return tuple(_catalog_document_view(row) for row in rows)


def _json_labels(value: object, key: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        str(item[key])
        for item in decoded
        if isinstance(item, dict) and isinstance(item.get(key), str)
    )


# endregion [05]
