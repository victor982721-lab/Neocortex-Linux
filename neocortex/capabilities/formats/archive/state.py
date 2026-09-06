"""Durable, read-only-queryable state for members contained in ZIP archives."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path

from neocortex.persistence.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    READWRITE_EXISTING,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)

from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_application_schema_version,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)

from .logical import issue_diagnosis


# region [01] Schema and connection contract


ARCHIVE_SCHEMA_VERSION = 2
MAX_ARCHIVE_QUERY_RESULTS = 1_000

_ARCHIVE_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="archive state",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=32_768,
        wal_autocheckpoint_pages=2_048,
        journal_size_limit_bytes=134_217_728,
    ),
)

_ARCHIVE_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS containers(
        container_key TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        member_count INTEGER NOT NULL DEFAULT 0,
        indexed_count INTEGER NOT NULL DEFAULT 0,
        metadata_only_count INTEGER NOT NULL DEFAULT 0,
        nested_archive_count INTEGER NOT NULL DEFAULT 0,
        issue_count INTEGER NOT NULL DEFAULT 0,
        text_chars INTEGER NOT NULL DEFAULT 0,
        max_depth INTEGER NOT NULL DEFAULT 0,
        error_type TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS archive_containers_path_idx
        ON containers(path)""",
    """CREATE INDEX IF NOT EXISTS archive_containers_status_idx
        ON containers(status,last_seen_run_id,path)""",
    """CREATE TABLE IF NOT EXISTS documents(
        file_key TEXT PRIMARY KEY,
        container_key TEXT NOT NULL REFERENCES containers(container_key)
            ON DELETE CASCADE,
        path TEXT NOT NULL,
        container_path TEXT NOT NULL,
        member_chain TEXT NOT NULL,
        member_path TEXT NOT NULL,
        archive_depth INTEGER NOT NULL,
        content_kind TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        compressed_size INTEGER NOT NULL,
        crc32 INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0,
        text_xxh3_128 TEXT,
        detail TEXT,
        error_type TEXT,
        error_message TEXT,
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS archive_documents_path_idx
        ON documents(path)""",
    """CREATE INDEX IF NOT EXISTS archive_documents_container_idx
        ON documents(container_key,archive_depth,member_chain)""",
    """CREATE INDEX IF NOT EXISTS archive_documents_status_idx
        ON documents(status,content_kind,container_path,member_chain)""",
    """CREATE TABLE IF NOT EXISTS archive_issues(
        issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        container_key TEXT NOT NULL REFERENCES containers(container_key)
            ON DELETE CASCADE,
        member_chain TEXT,
        archive_depth INTEGER NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_ns INTEGER NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS archive_issues_container_idx
        ON archive_issues(container_key,issue_id)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        container_path UNINDEXED,
        container_name,
        member_chain,
        content_kind,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
)


def _create_archive_v1_schema(connection: sqlite3.Connection) -> None:
    for statement in _ARCHIVE_SCHEMA_DDL:
        connection.execute(statement)


def _create_archive_v0_bootstrap(connection: sqlite3.Connection) -> None:
    """The only recognized v0 is an empty metadata-only bootstrap."""

    connection.execute(_ARCHIVE_SCHEMA_DDL[0])


_ARCHIVE_V2_DDL = (
    "ALTER TABLE documents ADD COLUMN document_role TEXT NOT NULL DEFAULT 'archive_member'",
    "ALTER TABLE documents ADD COLUMN logical_document_chain TEXT",
    "ALTER TABLE documents ADD COLUMN independently_organizable INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN independently_disposable INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE archive_logical_documents(
        container_key TEXT NOT NULL REFERENCES containers(container_key) ON DELETE CASCADE,
        member_chain TEXT NOT NULL,
        physical_media_type TEXT NOT NULL,
        declared_mime TEXT,
        logical_kind TEXT,
        proposed_extension TEXT,
        evidence_json TEXT NOT NULL,
        identification_status TEXT NOT NULL,
        integrity_status TEXT NOT NULL,
        opening_status TEXT NOT NULL,
        PRIMARY KEY(container_key,member_chain)
    ) WITHOUT ROWID""",
    """CREATE INDEX archive_issues_reason_idx ON archive_issues(reason_code,issue_id)""",
)


def _create_archive_schema(connection: sqlite3.Connection) -> None:
    _create_archive_v1_schema(connection)
    for statement in _ARCHIVE_V2_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _archive_v1_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_archive_v1_schema)


@lru_cache(maxsize=1)
def _archive_v0_bootstrap_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_archive_v0_bootstrap)


@lru_cache(maxsize=1)
def archive_schema_contract() -> SQLiteSchemaContract:
    """Return the exact schema understood by writers and Knowledge readers."""

    return schema_contract_from_builder(_create_archive_schema)


@contextmanager
def archive_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
    preserve_journal_mode: bool = False,
):
    """Open archive state; migration may preserve an existing owner's journal mode."""

    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    policy = (
        replace(_ARCHIVE_SQLITE_POLICY, writer_pragmas=None)
        if preserve_journal_mode
        else _ARCHIVE_SQLITE_POLICY
    )
    connection = connect_sqlite(path, mode=mode, policy=policy)
    try:
        yield connection
    finally:
        connection.close()


def initialize_archive_state(path: Path) -> None:
    """Create v2 or migrate exact v1/empty v0 atomically; readers never migrate."""

    prior: int | None = None
    if path.is_file():
        with archive_database(path, readonly=True) as connection:
            prior = read_application_schema_version(connection, label="archive")
            if prior is not None and prior > ARCHIVE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"archive schema {prior} is newer than supported schema "
                    f"{ARCHIVE_SCHEMA_VERSION}"
                )
            if prior == ARCHIVE_SCHEMA_VERSION:
                validate_sqlite_schema_contract(
                    connection,
                    archive_schema_contract(),
                    label="archive",
                    exact=True,
                )
                return
            if prior is not None and prior not in {0, 1}:
                raise RuntimeError(f"archive schema {prior} cannot be migrated")
            if prior == 0:
                validate_sqlite_schema_contract(
                    connection,
                    _archive_v0_bootstrap_contract(),
                    label="archive v0 metadata-only bootstrap",
                    exact=True,
                )
            if prior == 1:
                validate_sqlite_schema_contract(
                    connection, _archive_v1_schema_contract(), label="archive v1", exact=True
                )

    # Journal-mode PRAGMAs are not transactional. Applying the normal WAL
    # writer policy before BEGIN changed a valid DELETE owner's bytes even
    # when every migration statement rolled back. Existing owners retain
    # their journal mode; ordinary route writes still use the normal policy.
    with archive_database(
        path,
        create=prior is None,
        preserve_journal_mode=prior is not None,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            observed = read_application_schema_version(connection, label="archive")
            if observed != prior:
                raise RuntimeError("archive schema changed before migration")
            if prior == 1:
                validate_sqlite_schema_contract(
                    connection, _archive_v1_schema_contract(), label="archive v1", exact=True
                )
                for statement in _ARCHIVE_V2_DDL:
                    connection.execute(statement)
            else:
                if prior == 0:
                    validate_sqlite_schema_contract(
                        connection,
                        _archive_v0_bootstrap_contract(),
                        label="archive v0 metadata-only bootstrap",
                        exact=True,
                    )
                _create_archive_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(ARCHIVE_SCHEMA_VERSION),),
            )
            validate_sqlite_schema_contract(
                connection,
                archive_schema_contract(),
                label="archive",
                exact=True,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _validate_reader(connection: sqlite3.Connection) -> None:
    observed = read_metadata_schema_version(connection, label="archive")
    if observed != ARCHIVE_SCHEMA_VERSION:
        raise RuntimeError(
            f"archive schema {observed!r} is incompatible with schema {ARCHIVE_SCHEMA_VERSION}; "
            "writer migration required, read-only query did not migrate state"
        )
    validate_sqlite_schema_contract(
        connection,
        archive_schema_contract(),
        label="archive",
        exact=True,
    )


# endregion [01]


# region [02] Bounded read-only status and query API


@dataclass(frozen=True, slots=True)
class ArchiveStatus:
    available: bool
    schema_version: int | None = None
    containers: int = 0
    complete: int = 0
    partial: int = 0
    errors: int = 0
    members: int = 0
    indexed: int = 0
    metadata_only: int = 0
    nested_archives: int = 0
    issues: int = 0
    text_chars: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArchiveSearchHit:
    file_key: str
    virtual_path: str
    container_path: str
    member_chain: str
    member_path: str
    archive_depth: int
    content_kind: str
    media_type: str
    status: str
    size: int
    snippet: str | None = None
    document_role: str = "archive_member"
    logical_document_chain: str | None = None
    independently_organizable: bool = False
    independently_disposable: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_archive_status(path: Path) -> ArchiveStatus:
    """Read aggregate state without creating, migrating or checkpointing it."""

    if not path.is_file():
        return ArchiveStatus(False)
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        row = connection.execute(
            """SELECT COUNT(*) AS containers,
            SUM(status='complete') AS complete,
            SUM(status='partial') AS partial,
            SUM(status='error') AS errors,
            COALESCE(SUM(member_count),0) AS members,
            COALESCE(SUM(indexed_count),0) AS indexed,
            COALESCE(SUM(metadata_only_count),0) AS metadata_only,
            COALESCE(SUM(nested_archive_count),0) AS nested_archives,
            COALESCE(SUM(issue_count),0) AS issues,
            COALESCE(SUM(text_chars),0) AS text_chars
            FROM containers"""
        ).fetchone()
    assert row is not None
    return ArchiveStatus(
        True,
        ARCHIVE_SCHEMA_VERSION,
        int(row["containers"]),
        int(row["complete"] or 0),
        int(row["partial"] or 0),
        int(row["errors"] or 0),
        int(row["members"] or 0),
        int(row["indexed"] or 0),
        int(row["metadata_only"] or 0),
        int(row["nested_archives"] or 0),
        int(row["issues"] or 0),
        int(row["text_chars"] or 0),
    )


def _validate_result_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_ARCHIVE_QUERY_RESULTS:
        raise ValueError(f"archive result limit must be between 1 and {MAX_ARCHIVE_QUERY_RESULTS}")


def _escaped_like_fragment(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_hit(row: sqlite3.Row) -> ArchiveSearchHit:
    return ArchiveSearchHit(
        file_key=str(row["file_key"]),
        virtual_path=str(row["path"]),
        container_path=str(row["container_path"]),
        member_chain=str(row["member_chain"]),
        member_path=str(row["member_path"]),
        archive_depth=int(row["archive_depth"]),
        content_kind=str(row["content_kind"]),
        media_type=str(row["media_type"]),
        status=str(row["status"]),
        size=int(row["size"]),
        snippet=None if row["snippet"] is None else str(row["snippet"]),
        document_role=str(row["document_role"]),
        logical_document_chain=row["logical_document_chain"],
        independently_organizable=bool(row["independently_organizable"]),
        independently_disposable=bool(row["independently_disposable"]),
    )


def search_archive_state(
    path: Path,
    query: str,
    limit: int = 20,
    *,
    container_fragment: str | None = None,
    include_components: bool = False,
) -> tuple[ArchiveSearchHit, ...]:
    """Search member names and extracted text through read-only FTS5 state."""

    from neocortex.semantic.semantic_lexical import compile_natural_fts_query

    _validate_result_limit(limit)
    normalized = compile_natural_fts_query(query)
    container_pattern = (
        None
        if container_fragment is None
        else f"%{_escaped_like_fragment(container_fragment.strip())}%"
    )
    if container_fragment is not None and not container_fragment.strip():
        raise ValueError("archive container fragment must be non-empty")
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            """WITH ranked AS MATERIALIZED (
            SELECT f.rowid AS fts_rowid,f.file_key,f.path,
            bm25(document_fts) AS raw_bm25
            FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
            WHERE document_fts MATCH ?1
            AND EXISTS (SELECT 1 FROM containers AS c WHERE c.container_key=d.container_key
                        AND c.status IN ('complete','partial'))
            AND (?2 IS NULL OR d.container_path LIKE ?2 ESCAPE '\\')
            AND (?4 OR d.document_role<>'document_component')
            ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?3
            )
            SELECT d.file_key,d.path,d.container_path,d.member_chain,d.member_path,
            d.archive_depth,d.content_kind,d.media_type,d.status,d.size,
            d.document_role,d.logical_document_chain,
            d.independently_organizable,d.independently_disposable,
            snippet(document_fts,6,'[',']',' ... ',24) AS snippet
            FROM ranked JOIN document_fts ON document_fts.rowid=ranked.fts_rowid
            JOIN documents AS d ON d.file_key=ranked.file_key
            WHERE document_fts MATCH ?1
            ORDER BY ranked.raw_bm25,ranked.path COLLATE NOCASE""",
            (normalized, container_pattern, limit, include_components),
        ).fetchall()
    return tuple(_row_to_hit(row) for row in rows)


def list_archive_members(
    path: Path,
    limit: int = 20,
    *,
    container_fragment: str | None = None,
) -> tuple[ArchiveSearchHit, ...]:
    """List a bounded page of members in stable container/chain order."""

    _validate_result_limit(limit)
    container_pattern = (
        None
        if container_fragment is None
        else f"%{_escaped_like_fragment(container_fragment.strip())}%"
    )
    if container_fragment is not None and not container_fragment.strip():
        raise ValueError("archive container fragment must be non-empty")
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            """SELECT file_key,path,container_path,member_chain,member_path,
            archive_depth,content_kind,media_type,status,size,NULL AS snippet,
            document_role,logical_document_chain,independently_organizable,independently_disposable
            FROM documents AS d
            WHERE (? IS NULL OR container_path LIKE ? ESCAPE '\\')
            AND EXISTS (SELECT 1 FROM containers AS c WHERE c.container_key=d.container_key
                        AND c.status IN ('complete','partial'))
            ORDER BY container_path COLLATE NOCASE,member_chain COLLATE NOCASE
            LIMIT ?""",
            (container_pattern, container_pattern, limit),
        ).fetchall()
    return tuple(_row_to_hit(row) for row in rows)


@dataclass(frozen=True, slots=True)
class ArchiveLogicalDocument:
    container_key: str
    container_path: str
    member_chain: str
    virtual_path: str
    processing_signature: str
    physical_media_type: str
    declared_mime: str | None
    logical_kind: str | None
    proposed_extension: str | None
    evidence: tuple[str, ...]
    identification_status: str
    integrity_status: str
    opening_status: str
    independently_organizable: bool
    independently_disposable: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def list_archive_logical_documents(
    path: Path,
    limit: int = 20,
    *,
    container_fragment: str | None = None,
) -> tuple[ArchiveLogicalDocument, ...]:
    """List bounded published observations, not proofs of validity or usability."""

    _validate_result_limit(limit)
    container_fragment = _optional_query_filter(container_fragment, "container fragment")
    pattern = (
        None if container_fragment is None else f"%{_escaped_like_fragment(container_fragment)}%"
    )
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            """SELECT l.*,c.path AS container_path,c.processing_signature
            FROM archive_logical_documents AS l JOIN containers AS c USING(container_key)
            WHERE c.status IN ('complete','partial')
            AND (? IS NULL OR c.path LIKE ? ESCAPE '\\')
            ORDER BY c.path,l.member_chain LIMIT ?""",
            (pattern, pattern, limit),
        ).fetchall()
    return tuple(
        ArchiveLogicalDocument(
            container_key=row["container_key"],
            container_path=row["container_path"],
            member_chain=row["member_chain"],
            virtual_path=(
                f"{row['container_path']}!/{row['member_chain']}"
                if row["member_chain"]
                else row["container_path"]
            ),
            processing_signature=row["processing_signature"],
            physical_media_type=row["physical_media_type"],
            declared_mime=row["declared_mime"],
            logical_kind=row["logical_kind"],
            proposed_extension=row["proposed_extension"],
            evidence=tuple(json.loads(row["evidence_json"])),
            identification_status=row["identification_status"],
            integrity_status=row["integrity_status"],
            opening_status=row["opening_status"],
            independently_organizable=row["identification_status"] == "identified",
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class ArchiveIssue:
    issue_id: int
    container_key: str
    container_path: str
    member_chain: str | None
    archive_depth: int
    reason_code: str
    detail: str
    coverage_impact: str
    recoverability: str
    processing_signature: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArchiveIssuePage:
    items: tuple[ArchiveIssue, ...]
    next_cursor: str | None
    scope: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "next_cursor": self.next_cursor,
            "scope": self.scope,
        }


def _optional_query_filter(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2_048:
        raise ValueError(f"archive {label} must be non-empty and at most 2048 characters")
    return value.strip()


def _physical_path_scope(value: str | None) -> str | None:
    """Validate a lexical Linux scope without probing or resolving real paths."""

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4_096
        or "\x00" in value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("archive path scope must be an absolute Linux path without dot segments")
    return "/" + "/".join(part for part in value.split("/") if part)


def _issue_cursor(after_id: int, scope_digest: str, revision: str) -> str:
    payload = json.dumps(
        {"v": 1, "after_id": after_id, "scope": scope_digest, "revision": revision},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_issue_cursor(cursor: str | None, scope_digest: str, revision: str) -> int:
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2_048:
            raise ValueError("invalid cursor length")
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or value.get("v") != 1:
            raise ValueError("invalid cursor version")
        after_id = value["after_id"]
        if type(after_id) is not int or not 0 < after_id < 2**63:
            raise ValueError("invalid cursor key")
        if value["scope"] != scope_digest or value["revision"] != revision:
            raise ValueError("cursor scope or published owner revision changed")
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"invalid archive issue cursor: {exc}") from exc
    return after_id


def list_archive_issues(
    path: Path,
    limit: int = 20,
    *,
    container_fragment: str | None = None,
    member_fragment: str | None = None,
    reason_code: str | None = None,
    coverage_impact: str | None = None,
    recoverability: str | None = None,
    cursor: str | None = None,
    path_scope: str | None = None,
    container_key: str | None = None,
) -> ArchiveIssuePage:
    """Keyset page of published issues, bound to exact filters and owner revision.

    An issue is not a corrupt ZIP. Recovery labels describe possible next
    investigation, not validated recoverability or authority to change files.
    ``path_scope`` is an exact physical container path or its Linux descendants,
    with a case-sensitive path-component boundary, never a member-name filter.
    """

    _validate_result_limit(limit)
    normalized_key = _optional_query_filter(container_key, "container key")
    if normalized_key != container_key:
        raise ValueError("archive container key must be exact, without surrounding whitespace")
    filters = {
        "path_scope": _physical_path_scope(path_scope),
        "container_key": normalized_key,
        "container_fragment": _optional_query_filter(container_fragment, "container fragment"),
        "member_fragment": _optional_query_filter(member_fragment, "member fragment"),
        "reason_code": _optional_query_filter(reason_code, "reason code"),
        "coverage_impact": _optional_query_filter(coverage_impact, "coverage impact"),
        "recoverability": _optional_query_filter(recoverability, "recoverability"),
    }
    scope: dict[str, object] = {
        "owner_path": str(path.resolve()),
        "population": "published_archive_issues_only",
        "filters": filters,
    }
    scope_digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        version = connection.execute(
            """SELECT COUNT(*),COALESCE(MAX(updated_ns),0),
            (SELECT COALESCE(MAX(issue_id),0) FROM archive_issues),
            (SELECT COUNT(*) FROM archive_issues) FROM containers"""
        ).fetchone()
        revision = hashlib.sha256(json.dumps(tuple(version)).encode()).hexdigest()
        scope["owner_revision"] = revision
        after_id = _decode_issue_cursor(cursor, scope_digest, revision)
        connection.create_function(
            "archive_coverage_impact", 1, lambda code: issue_diagnosis(code)[0], deterministic=True
        )
        connection.create_function(
            "archive_recoverability", 1, lambda code: issue_diagnosis(code)[1], deterministic=True
        )
        clauses = ["i.issue_id>?", "c.status IN ('complete','partial','error')"]
        params: list[object] = [after_id]
        physical_scope = filters["path_scope"]
        if physical_scope is not None:
            child_prefix = physical_scope.rstrip("/") + "/"
            # A binary lexical range encodes the exact '/' boundary without
            # LIKE wildcard/case behavior, substring matches or ZIP!/ guessing.
            child_end = child_prefix[:-1] + "0"  # successor of ASCII '/'
            clauses.append(
                "(c.path COLLATE BINARY = ? OR "
                "(c.path COLLATE BINARY >= ? AND c.path COLLATE BINARY < ?))"
            )
            params.extend((physical_scope, child_prefix, child_end))
        if filters["container_key"] is not None:
            clauses.append("c.container_key COLLATE BINARY = ?")
            params.append(filters["container_key"])
        for key, column in (
            ("container_fragment", "c.path"),
            ("member_fragment", "i.member_chain"),
        ):
            value = filters[key]
            if value is not None:
                clauses.append(f"{column} LIKE ? ESCAPE '\\'")
                params.append(f"%{_escaped_like_fragment(value)}%")
        for key, expression in (
            ("reason_code", "i.reason_code"),
            ("coverage_impact", "archive_coverage_impact(i.reason_code)"),
            ("recoverability", "archive_recoverability(i.reason_code)"),
        ):
            if filters[key] is not None:
                clauses.append(f"{expression}=?")
                params.append(filters[key])
        params.append(limit + 1)
        rows = connection.execute(
            "SELECT i.*,c.path AS container_path,c.processing_signature "
            "FROM archive_issues AS i JOIN containers AS c USING(container_key) WHERE "
            + " AND ".join(clauses)
            + " ORDER BY i.issue_id LIMIT ?",
            params,
        ).fetchall()
    items = tuple(
        ArchiveIssue(
            issue_id=row["issue_id"],
            container_key=row["container_key"],
            container_path=row["container_path"],
            member_chain=row["member_chain"],
            archive_depth=row["archive_depth"],
            reason_code=row["reason_code"],
            detail=row["detail"],
            coverage_impact=issue_diagnosis(row["reason_code"])[0],
            recoverability=issue_diagnosis(row["reason_code"])[1],
            processing_signature=row["processing_signature"],
        )
        for row in rows[:limit]
    )
    next_cursor = (
        _issue_cursor(items[-1].issue_id, scope_digest, revision) if len(rows) > limit else None
    )
    return ArchiveIssuePage(items, next_cursor, scope)


# endregion [02]


__all__ = (
    "ARCHIVE_SCHEMA_VERSION",
    "ArchiveIssue",
    "ArchiveIssuePage",
    "ArchiveLogicalDocument",
    "ArchiveSearchHit",
    "ArchiveStatus",
    "archive_database",
    "archive_schema_contract",
    "initialize_archive_state",
    "list_archive_issues",
    "list_archive_logical_documents",
    "list_archive_members",
    "read_archive_status",
    "search_archive_state",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.archive.state"
del _defined_value
