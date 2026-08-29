"""Exact read-only causal facts for one Knowledge resource identity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from neocortex.deduplication.schema import (
    SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION,
)
from neocortex.deduplication.schema import validate_inventory_schema
from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    read_application_schema_version,
    validate_sqlite_schema_contract,
)

from .document_catalog_schema import (
    CATALOG_SCHEMA_VERSION,
    document_catalog_schema_contract,
)
from .file_identity import encode_file_identity
from .knowledge_asset_health_contracts import (
    MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES,
    KnowledgeAssetFactSnapshot,
    KnowledgeAssetHealthExample,
    KnowledgeAssetHealthFact,
    KnowledgeAssetHealthStage,
    KnowledgeAssetHealthValue,
    KnowledgeAssetIdentity,
)
from .knowledge_contracts import KnowledgeSnapshot, OwnerAvailability
from .knowledge_snapshot import KnowledgeStatePaths
from .knowledge_asset_health_pdf import (
    PdfHealthOwnerReadIssue,
    PdfHealthRecord,
    read_pdf_health_records,
)
from neocortex.capabilities.formats.pdf.pdf_schema import PDF_SCHEMA_VERSION
from .semantic_models import canonical_json, fingerprint_text
from .sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database
from neocortex.capabilities.formats.text.text_state import TEXT_SCHEMA_VERSION, text_schema_contract


_ROW_LIMIT = MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES + 1
_ReadResult = TypeVar("_ReadResult")


@dataclass(frozen=True, slots=True)
class _InventoryRecord:
    root: str
    scan_id: int
    updated_ns: int
    path: str
    size: int
    mtime_ns: int
    birthtime_ns: int


@dataclass(frozen=True, slots=True)
class _TextRecord:
    file_key: str
    path: str
    size: int
    mtime_ns: int
    birthtime_ns: int
    processing_signature: str
    status: str
    content_kind: str
    media_type: str
    text_chars: int
    text_xxh3_128: str | None
    text_truncated: bool
    error_type: str | None
    retryable: bool
    last_seen_run_id: int
    updated_ns: int
    revision_id: str | None
    revision_resource_id: str | None
    revision_processing_signature: str | None
    revision_state: str | None


@dataclass(frozen=True, slots=True)
class _CatalogRecord:
    generation_id: int
    source_kind: str
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
    catalog_status: str
    active: bool
    updated_ns: int


class _OwnerReadIssue(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _TextProbe:
    rows: tuple[sqlite3.Row, ...] = ()
    gap: str | None = None


@dataclass(frozen=True, slots=True)
class _PdfProbe:
    records: tuple[PdfHealthRecord, ...] = ()
    gap: str | None = None


@dataclass(slots=True)
class _ExampleCollector:
    values: list[KnowledgeAssetHealthExample]
    truncated: bool = False

    def add(self, example: KnowledgeAssetHealthExample) -> None:
        if len(self.values) >= MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES:
            self.truncated = True
            return
        self.values.append(example)

    def observe_window(self, rows: tuple[sqlite3.Row, ...]) -> tuple[sqlite3.Row, ...]:
        if len(rows) > MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES:
            self.truncated = True
        return rows[:MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES]


def _canonical_value(value: object | None) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _values(**values: object | None) -> tuple[KnowledgeAssetHealthValue, ...]:
    return tuple(
        KnowledgeAssetHealthValue(name, _canonical_value(value))
        for name, value in sorted(values.items())
    )


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _fact(
    *,
    stage: KnowledgeAssetHealthStage,
    owner: str,
    schema_version: int,
    record_id: str,
    status: str,
    values: tuple[KnowledgeAssetHealthValue, ...],
    publication_id: str | None = None,
) -> KnowledgeAssetHealthFact:
    projection = {
        "owner": owner,
        "publication_id": publication_id,
        "record_id": record_id,
        "schema_version": schema_version,
        "stage": stage.value,
        "status": status,
        "values": [value.to_dict() for value in values],
    }
    return KnowledgeAssetHealthFact(
        stage=stage,
        owner=owner,
        schema_version=schema_version,
        record_id=record_id,
        status=status,
        projection_digest=_digest(projection),
        values=values,
        publication_id=publication_id,
    )


def _example(
    *,
    stage: KnowledgeAssetHealthStage,
    code: str,
    record_id: str,
    values: tuple[KnowledgeAssetHealthValue, ...],
) -> KnowledgeAssetHealthExample:
    return KnowledgeAssetHealthExample(
        stage=stage,
        code=code,
        record_id=record_id,
        projection_digest=_digest(
            {
                "code": code,
                "record_id": record_id,
                "stage": stage.value,
                "values": [value.to_dict() for value in values],
            }
        ),
        values=values,
    )


def _owner_state_gap(snapshot: KnowledgeSnapshot, owner: str) -> str | None:
    selected = next((value for value in snapshot.owners if value.owner == owner), None)
    if selected is None:
        return f"{owner}_owner_absent"
    if selected.state is OwnerAvailability.AVAILABLE:
        return None
    return f"{owner}_owner_{selected.state.value}"


def _publication_present(
    snapshot: KnowledgeSnapshot,
    *,
    owner: str,
    scope: str,
    generation: int,
) -> bool:
    selected = next((value for value in snapshot.owners if value.owner == owner), None)
    if selected is None or selected.state is not OwnerAvailability.AVAILABLE:
        return False
    return any(
        head.scope == scope and head.generation == generation for head in selected.publications
    )


def _sqlite_error_is_corrupt(exc: sqlite3.Error) -> bool:
    return getattr(exc, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }


def _read_current_owner(
    path: Path,
    *,
    owner: str,
    expected_version: int,
    validator: Callable[[sqlite3.Connection], None],
    reader: Callable[[sqlite3.Connection], _ReadResult],
) -> _ReadResult:
    try:
        with immutable_sqlite_database(path) as connection:
            observed = read_application_schema_version(connection, label=owner)
            if observed is None:
                raise _OwnerReadIssue("schema_version_absent")
            if observed > expected_version:
                raise _OwnerReadIssue("future_schema")
            if observed < expected_version:
                raise _OwnerReadIssue("incompatible_schema")
            validator(connection)
            return reader(connection)
    except FileNotFoundError as exc:
        raise _OwnerReadIssue("owner_absent") from exc
    except ImmutableSQLiteUnavailable as exc:
        raise _OwnerReadIssue("owner_not_quiescent") from exc
    except _OwnerReadIssue:
        raise
    except SQLiteSchemaContractError as exc:
        raise _OwnerReadIssue("schema_invalid") from exc
    except sqlite3.Error as exc:
        code = "owner_corrupt" if _sqlite_error_is_corrupt(exc) else "owner_read_failed"
        raise _OwnerReadIssue(code) from exc
    except (RuntimeError, ValueError) as exc:
        raise _OwnerReadIssue("schema_invalid") from exc


def _validate_catalog(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        document_catalog_schema_contract(),
        label="document catalog",
        exact=True,
    )


def _validate_text(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        text_schema_contract(),
        label="text state",
        exact=True,
    )


def _identity_blob(value: int) -> bytes:
    return value.to_bytes(16, "little", signed=False)


def _file_key_candidates(identity: KnowledgeAssetIdentity) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                encode_file_identity(identity.volume_id, identity.file_id),
                f"{identity.volume_id}:{identity.file_id}",
            }
        )
    )


def _inventory_rows(
    connection: sqlite3.Connection,
    identity: KnowledgeAssetIdentity,
) -> tuple[tuple[sqlite3.Row, ...], tuple[sqlite3.Row, ...]]:
    parameters = (
        _identity_blob(identity.volume_id),
        _identity_blob(identity.file_id),
        identity.birthtime_ns,
        _ROW_LIMIT,
    )
    published = tuple(
        connection.execute(
            """SELECT c.root,c.scan_id,c.updated_ns,f.path,f.size,f.mtime_ns,
            f.birthtime_ns
            FROM inventory_checkpoints c
            JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root
            JOIN files f ON f.scan_id=c.scan_id
            WHERE c.valid=1 AND s.status='complete'
            AND f.volume_id=? AND f.file_id=? AND f.birthtime_ns=?
            ORDER BY c.root COLLATE BINARY,f.path COLLATE BINARY LIMIT ?""",
            parameters,
        ).fetchall()
    )
    unpublished: tuple[sqlite3.Row, ...] = ()
    if not published:
        unpublished = tuple(
            connection.execute(
                """SELECT s.root,f.scan_id,s.status,f.path,f.size,f.mtime_ns,
                f.birthtime_ns
                FROM files f JOIN scans s ON s.scan_id=f.scan_id
                WHERE f.volume_id=? AND f.file_id=? AND f.birthtime_ns=?
                AND NOT EXISTS(
                    SELECT 1 FROM inventory_checkpoints c
                    WHERE c.scan_id=f.scan_id AND c.root=s.root AND c.valid=1)
                ORDER BY f.scan_id,f.path COLLATE BINARY LIMIT ?""",
                parameters,
            ).fetchall()
        )
    return published, unpublished


def _inventory_record(row: sqlite3.Row) -> _InventoryRecord:
    return _InventoryRecord(
        root=str(row["root"]),
        scan_id=int(row["scan_id"]),
        updated_ns=int(row["updated_ns"]),
        path=str(row["path"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
    )


def _inventory_fact(
    identity: KnowledgeAssetIdentity,
    record: _InventoryRecord,
) -> KnowledgeAssetHealthFact:
    return _fact(
        stage=KnowledgeAssetHealthStage.INVENTORY,
        owner="inventory",
        schema_version=INVENTORY_SCHEMA_VERSION,
        publication_id=f"inventory-scan:{record.scan_id}",
        record_id=f"inventory:{record.scan_id}:{identity.resource_id}",
        status="published",
        values=_values(
            birthtime_ns=record.birthtime_ns,
            file_id=identity.file_id,
            mtime_ns=record.mtime_ns,
            path=record.path,
            root=record.root,
            scan_id=record.scan_id,
            size=record.size,
            updated_ns=record.updated_ns,
            volume_id=identity.volume_id,
        ),
    )


def _capture_inventory(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
    gaps: set[str],
    counterevidence: set[str],
    examples: _ExampleCollector,
) -> tuple[KnowledgeAssetHealthFact | None, _InventoryRecord | None]:
    owner_gap = _owner_state_gap(snapshot, "inventory")
    if owner_gap is not None:
        gaps.add(owner_gap)
        return None, None
    try:
        published, unpublished = _read_current_owner(
            paths.inventory,
            owner="inventory",
            expected_version=INVENTORY_SCHEMA_VERSION,
            validator=validate_inventory_schema,
            reader=lambda connection: _inventory_rows(connection, identity),
        )
    except _OwnerReadIssue as exc:
        gaps.add(f"inventory_{exc.code}")
        return None, None
    assert isinstance(published, tuple) and isinstance(unpublished, tuple)
    if len(published) != 1:
        if not published:
            gaps.add("published_inventory_record_missing")
            for row in examples.observe_window(unpublished):
                values = _values(
                    path=str(row["path"]),
                    scan_id=int(row["scan_id"]),
                    scan_status=str(row["status"]),
                )
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.INVENTORY,
                        code="unpublished_inventory_record",
                        record_id=f"inventory:{int(row['scan_id'])}:{identity.resource_id}",
                        values=values,
                    )
                )
            if unpublished:
                counterevidence.add("unpublished_inventory_record_observed")
        else:
            counterevidence.add("inventory_identity_ambiguous")
            for row in examples.observe_window(published):
                values = _values(
                    path=str(row["path"]),
                    root=str(row["root"]),
                    scan_id=int(row["scan_id"]),
                )
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.INVENTORY,
                        code="published_inventory_candidate",
                        record_id=f"inventory:{int(row['scan_id'])}:{identity.resource_id}",
                        values=values,
                    )
                )
        return None, None
    try:
        record = _inventory_record(published[0])
        fact = _inventory_fact(identity, record)
    except (OverflowError, TypeError, ValueError):
        gaps.add("inventory_record_invalid")
        return None, None
    if not _publication_present(
        snapshot,
        owner="inventory",
        scope=record.root,
        generation=record.scan_id,
    ):
        counterevidence.add("inventory_publication_snapshot_mismatch")
    return fact, record


def _text_rows(
    connection: sqlite3.Connection,
    identity: KnowledgeAssetIdentity,
) -> tuple[sqlite3.Row, ...]:
    candidates = _file_key_candidates(identity)
    placeholders = ",".join("?" for _ in candidates)
    return tuple(
        connection.execute(
            f"""SELECT d.file_key,d.path,d.size,d.mtime_ns,d.birthtime_ns,
            d.processing_signature,d.status,d.content_kind,d.media_type,d.text_chars,
            d.text_xxh3_128,d.text_truncated,d.error_type,d.retryable,
            d.last_seen_run_id,d.updated_ns,d.revision_id,
            r.resource_id AS revision_resource_id,
            r.processing_signature AS revision_processing_signature,
            r.revision_state
            FROM documents d LEFT JOIN text_input_revisions r
            ON r.revision_id=d.revision_id
            WHERE d.file_key IN ({placeholders})
            ORDER BY d.file_key LIMIT ?""",
            (*candidates, _ROW_LIMIT),
        ).fetchall()
    )


def _text_record(row: sqlite3.Row) -> _TextRecord:
    return _TextRecord(
        file_key=str(row["file_key"]),
        path=str(row["path"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
        processing_signature=str(row["processing_signature"]),
        status=str(row["status"]).casefold(),
        content_kind=str(row["content_kind"]),
        media_type=str(row["media_type"]),
        text_chars=int(row["text_chars"]),
        text_xxh3_128=None if row["text_xxh3_128"] is None else str(row["text_xxh3_128"]),
        text_truncated=bool(int(row["text_truncated"])),
        error_type=None if row["error_type"] is None else str(row["error_type"]),
        retryable=bool(int(row["retryable"])),
        last_seen_run_id=int(row["last_seen_run_id"]),
        updated_ns=int(row["updated_ns"]),
        revision_id=None if row["revision_id"] is None else str(row["revision_id"]),
        revision_resource_id=(
            None if row["revision_resource_id"] is None else str(row["revision_resource_id"])
        ),
        revision_processing_signature=(
            None
            if row["revision_processing_signature"] is None
            else str(row["revision_processing_signature"])
        ),
        revision_state=None if row["revision_state"] is None else str(row["revision_state"]),
    )


def _text_fact(record: _TextRecord) -> KnowledgeAssetHealthFact:
    return _fact(
        stage=KnowledgeAssetHealthStage.SOURCE_OWNER,
        owner="text",
        schema_version=TEXT_SCHEMA_VERSION,
        record_id=f"text:{record.file_key}",
        status=record.status,
        values=_values(
            birthtime_ns=record.birthtime_ns,
            content_kind=record.content_kind,
            error_type=record.error_type,
            file_key=record.file_key,
            last_seen_run_id=record.last_seen_run_id,
            media_type=record.media_type,
            mtime_ns=record.mtime_ns,
            path=record.path,
            processing_signature=record.processing_signature,
            retryable=record.retryable,
            revision_id=record.revision_id,
            revision_processing_signature=record.revision_processing_signature,
            revision_resource_id=record.revision_resource_id,
            revision_state=record.revision_state,
            size=record.size,
            text_chars=record.text_chars,
            text_truncated=record.text_truncated,
            text_xxh3_128=record.text_xxh3_128,
            updated_ns=record.updated_ns,
        ),
    )


def _probe_text(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
) -> _TextProbe:
    owner_gap = _owner_state_gap(snapshot, "text")
    if owner_gap is not None:
        return _TextProbe(gap=owner_gap)
    if paths.text is None:
        return _TextProbe(gap="text_owner_absent")
    try:
        rows = _read_current_owner(
            paths.text,
            owner="text",
            expected_version=TEXT_SCHEMA_VERSION,
            validator=_validate_text,
            reader=lambda connection: _text_rows(connection, identity),
        )
    except _OwnerReadIssue as exc:
        return _TextProbe(gap=f"text_{exc.code}")
    assert isinstance(rows, tuple)
    return _TextProbe(rows=rows)


def _capture_text(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
    gaps: set[str],
    counterevidence: set[str],
    examples: _ExampleCollector,
    *,
    probe: _TextProbe | None = None,
) -> tuple[KnowledgeAssetHealthFact | None, _TextRecord | None]:
    selected_probe = probe or _probe_text(paths, identity, snapshot)
    if selected_probe.gap is not None:
        gaps.add(selected_probe.gap)
        return None, None
    rows = selected_probe.rows
    if len(rows) != 1:
        if not rows:
            gaps.add("text_source_record_missing")
        else:
            counterevidence.add("text_source_identity_ambiguous")
            for row in examples.observe_window(rows):
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.SOURCE_OWNER,
                        code="text_source_candidate",
                        record_id=f"text:{row['file_key']!s}",
                        values=_values(
                            file_key=str(row["file_key"]),
                            path=str(row["path"]),
                            status=str(row["status"]).casefold(),
                        ),
                    )
                )
        return None, None
    try:
        record = _text_record(rows[0])
        fact = _text_fact(record)
    except (OverflowError, TypeError, ValueError):
        gaps.add("text_record_invalid")
        return None, None
    if record.revision_id is None:
        counterevidence.add("text_revision_missing")
    elif (
        record.revision_resource_id != identity.resource_id
        or record.revision_processing_signature != record.processing_signature
    ):
        counterevidence.add("text_revision_contract_mismatch")
    return fact, record


def _probe_pdf(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
) -> _PdfProbe:
    owner_gap = _owner_state_gap(snapshot, "pdf")
    if owner_gap is not None:
        return _PdfProbe(gap=owner_gap)
    try:
        return _PdfProbe(records=read_pdf_health_records(paths.pdf, identity))
    except PdfHealthOwnerReadIssue as exc:
        return _PdfProbe(gap=f"pdf_owner_{exc.code}")


def _pdf_fact(record: PdfHealthRecord) -> KnowledgeAssetHealthFact:
    return _fact(
        stage=KnowledgeAssetHealthStage.SOURCE_OWNER,
        owner="pdf",
        schema_version=PDF_SCHEMA_VERSION,
        record_id=f"pdf:{record.file_key}",
        status=record.status,
        values=_values(
            birthtime_ns=record.birthtime_ns,
            completed_pages=record.completed_pages,
            file_key=record.file_key,
            fts_rows=record.fts_rows,
            fts_state_rows=record.fts_state_rows,
            is_partial=record.is_partial,
            last_seen_run_id=record.last_seen_run_id,
            metadata_valid=record.metadata_valid,
            mtime_ns=record.mtime_ns,
            native_pages=record.native_pages,
            normalized_text_chars=record.normalized_text_chars,
            normalized_text_xxh3_128=record.normalized_text_xxh3_128,
            ocr_pages=record.ocr_pages,
            page_count=record.page_count,
            page_end=record.page_end,
            page_errors_count=record.page_errors_count,
            page_start=record.page_start,
            path=record.path,
            page_error_projection_exact=record.page_error_projection_exact,
            persisted_page_errors=record.persisted_page_errors,
            persisted_pages=record.persisted_pages,
            processing_signature=record.processing_signature,
            recovery_engine=record.recovery_engine,
            recovery_present=record.recovery_present,
            recovery_recognized=record.recovery_recognized,
            recovery_version=record.recovery_version,
            size=record.size,
            source_kind="pdf",
            staging_pages=record.staging_pages,
            staging_projection_exact=record.staging_projection_exact,
            updated_ns=record.updated_ns,
            fts_projection_exact=record.fts_projection_exact,
        ),
    )


def _pdf_page_projection_counterevidence(
    record: PdfHealthRecord,
    counterevidence: set[str],
) -> None:
    if not record.metadata_valid:
        counterevidence.add("pdf_metadata_invalid")
    if record.recovery_present and not record.recovery_recognized:
        counterevidence.add("pdf_recovery_contract_unrecognized")
    if (
        record.page_errors_count != record.persisted_page_errors
        or not record.page_error_projection_exact
    ):
        counterevidence.add("pdf_page_errors_mismatch")

    if record.status in {"done", "partial"}:
        if (
            record.completed_pages != record.persisted_pages
            or record.native_pages + record.ocr_pages + record.persisted_page_errors
            != record.completed_pages
            or not record.selected_range_is_coherent
        ):
            counterevidence.add("pdf_completed_pages_mismatch")
        if (
            record.persisted_pages != record.fts_state_rows
            or record.fts_state_rows != record.fts_rows
            or not record.fts_projection_exact
        ):
            counterevidence.add("pdf_fts_projection_mismatch")
        if record.status == "partial" and (
            record.staging_pages != record.completed_pages or not record.staging_projection_exact
        ):
            counterevidence.add("pdf_completed_pages_mismatch")

    if record.status in {"done", "error", "protected"}:
        if record.staging_pages:
            counterevidence.add("pdf_terminal_staging_present")
    if record.status == "done":
        if not record.is_partial and (
            not record.full_range
            or record.page_count != record.completed_pages
            or record.page_errors_count != 0
        ):
            counterevidence.add("pdf_completed_pages_mismatch")


def _capture_pdf(
    identity: KnowledgeAssetIdentity,
    gaps: set[str],
    counterevidence: set[str],
    examples: _ExampleCollector,
    *,
    probe: _PdfProbe,
) -> tuple[KnowledgeAssetHealthFact | None, PdfHealthRecord | None]:
    if probe.gap is not None:
        gaps.add(probe.gap)
        return None, None
    rows = probe.records
    if len(rows) != 1:
        if not rows:
            gaps.add("pdf_source_record_missing")
        else:
            counterevidence.add("source_owner_identity_ambiguous")
            for record in rows[:MAX_KNOWLEDGE_ASSET_HEALTH_EXAMPLES]:
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.SOURCE_OWNER,
                        code="pdf_source_candidate",
                        record_id=f"pdf:{record.file_key}",
                        values=_values(
                            file_key=record.file_key,
                            path=record.path,
                            status=record.status,
                        ),
                    )
                )
        return None, None
    record = rows[0]
    _pdf_page_projection_counterevidence(record, counterevidence)
    expected_keys = set(_file_key_candidates(identity))
    if record.file_key not in expected_keys:
        counterevidence.add("pdf_file_key_identity_mismatch")
    return _pdf_fact(record), record


def _catalog_rows(
    connection: sqlite3.Connection,
    identity: KnowledgeAssetIdentity,
    source_kind: str,
) -> tuple[tuple[sqlite3.Row, ...], tuple[sqlite3.Row, ...]]:
    parameters = (
        source_kind,
        str(identity.volume_id),
        str(identity.file_id),
        identity.birthtime_ns,
        _ROW_LIMIT,
    )
    published = tuple(
        connection.execute(
            """SELECT d.generation_id,d.source_kind,d.file_key,d.path,
            d.volume_id,d.file_id,
            d.size,d.mtime_ns,d.birthtime_ns,d.source_status,d.processing_signature,
            d.text_fingerprint,d.catalog_status,d.active,d.updated_ns
            FROM catalog_publications p
            JOIN catalog_generations g ON g.generation_id=p.generation_id
            AND g.source_kind=p.source_kind AND g.status='published'
            JOIN catalog_generation_documents d ON d.generation_id=p.generation_id
            AND d.source_kind=p.source_kind
            WHERE p.source_kind=? AND d.source_kind=p.source_kind AND d.active=1
            AND d.volume_id=? AND d.file_id=? AND d.birthtime_ns=?
            ORDER BY d.file_key,d.path COLLATE BINARY LIMIT ?""",
            parameters,
        ).fetchall()
    )
    unpublished: tuple[sqlite3.Row, ...] = ()
    if not published:
        unpublished = tuple(
            connection.execute(
                """SELECT d.generation_id,g.status,d.file_key,d.path,d.source_status,
                d.catalog_status
                FROM catalog_generation_documents d
                JOIN catalog_generations g ON g.generation_id=d.generation_id
                LEFT JOIN catalog_publications p ON p.source_kind=d.source_kind
                AND p.generation_id=d.generation_id
                WHERE d.source_kind=? AND p.generation_id IS NULL
                AND d.volume_id=? AND d.file_id=? AND d.birthtime_ns=?
                ORDER BY d.generation_id DESC,d.file_key LIMIT ?""",
                parameters,
            ).fetchall()
        )
    return published, unpublished


def _catalog_record(row: sqlite3.Row) -> _CatalogRecord:
    return _CatalogRecord(
        generation_id=int(row["generation_id"]),
        source_kind=str(row["source_kind"]).casefold(),
        file_key=str(row["file_key"]),
        path=str(row["path"]),
        volume_id=str(row["volume_id"]),
        file_id=str(row["file_id"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
        source_status=str(row["source_status"]).casefold(),
        processing_signature=str(row["processing_signature"]),
        text_fingerprint=(
            None if row["text_fingerprint"] is None else str(row["text_fingerprint"])
        ),
        catalog_status=str(row["catalog_status"]).casefold(),
        active=bool(int(row["active"])),
        updated_ns=int(row["updated_ns"]),
    )


def _catalog_fact(
    identity: KnowledgeAssetIdentity,
    record: _CatalogRecord,
) -> KnowledgeAssetHealthFact:
    projection_values: dict[str, object | None] = {
        "active": record.active,
        "birthtime_ns": record.birthtime_ns,
        "file_id": record.file_id,
        "file_key": record.file_key,
        "generation_id": record.generation_id,
        "mtime_ns": record.mtime_ns,
        "path": record.path,
        "processing_signature": record.processing_signature,
        "resource_id": identity.resource_id,
        "size": record.size,
        "source_kind": record.source_kind,
        "source_status": record.source_status,
        "updated_ns": record.updated_ns,
        "volume_id": record.volume_id,
    }
    if record.source_kind == "pdf":
        projection_values["text_fingerprint"] = record.text_fingerprint
    return _fact(
        stage=KnowledgeAssetHealthStage.CATALOG,
        owner="catalog",
        schema_version=CATALOG_SCHEMA_VERSION,
        publication_id=f"catalog:{record.generation_id}",
        record_id=(f"catalog:{record.generation_id}:{record.source_kind}:{record.file_key}"),
        status=record.catalog_status,
        values=_values(**projection_values),
    )


def _capture_catalog(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
    gaps: set[str],
    counterevidence: set[str],
    examples: _ExampleCollector,
    *,
    source_kind: str = "text",
) -> tuple[KnowledgeAssetHealthFact | None, _CatalogRecord | None]:
    owner_gap = _owner_state_gap(snapshot, "catalog")
    if owner_gap is not None:
        gaps.add(owner_gap)
        return None, None
    try:
        published, unpublished = _read_current_owner(
            paths.catalog,
            owner="catalog",
            expected_version=CATALOG_SCHEMA_VERSION,
            validator=_validate_catalog,
            reader=lambda connection: _catalog_rows(connection, identity, source_kind),
        )
    except _OwnerReadIssue as exc:
        gaps.add(f"catalog_{exc.code}")
        return None, None
    assert isinstance(published, tuple) and isinstance(unpublished, tuple)
    if len(published) != 1:
        if not published:
            gaps.add(
                "published_pdf_catalog_record_missing"
                if source_kind == "pdf"
                else "published_catalog_record_missing"
            )
            for row in examples.observe_window(unpublished):
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.CATALOG,
                        code="unpublished_catalog_record",
                        record_id=(
                            f"catalog:{int(row['generation_id'])}:{source_kind}:{row['file_key']!s}"
                        ),
                        values=_values(
                            catalog_status=str(row["catalog_status"]).casefold(),
                            generation_id=int(row["generation_id"]),
                            generation_status=str(row["status"]).casefold(),
                            source_status=str(row["source_status"]).casefold(),
                        ),
                    )
                )
            if unpublished:
                counterevidence.add("unpublished_catalog_record_observed")
        else:
            counterevidence.add("catalog_identity_ambiguous")
            for row in examples.observe_window(published):
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.CATALOG,
                        code="published_catalog_candidate",
                        record_id=(
                            f"catalog:{int(row['generation_id'])}:{source_kind}:{row['file_key']!s}"
                        ),
                        values=_values(
                            generation_id=int(row["generation_id"]),
                            path=str(row["path"]),
                        ),
                    )
                )
        return None, None
    try:
        record = _catalog_record(published[0])
        fact = _catalog_fact(identity, record)
    except (OverflowError, TypeError, ValueError):
        gaps.add("catalog_record_invalid")
        return None, None
    if not _publication_present(
        snapshot,
        owner="catalog",
        scope=source_kind,
        generation=record.generation_id,
    ):
        counterevidence.add("catalog_publication_snapshot_mismatch")
    return fact, record


def _knowledge_search_fact(
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
    catalog: _CatalogRecord,
) -> KnowledgeAssetHealthFact:
    revision_payload = {
        "file_key": catalog.file_key,
        "mtime_ns": catalog.mtime_ns,
        "processing_signature": catalog.processing_signature,
        "size": catalog.size,
        "source_kind": catalog.source_kind,
    }
    revision_id = f"revision:catalog:{fingerprint_text(canonical_json(revision_payload)).xxh3_128}"
    status = "eligible" if catalog.active and catalog.catalog_status != "error" else "excluded"
    return _fact(
        stage=KnowledgeAssetHealthStage.KNOWLEDGE_SEARCH,
        owner="knowledge",
        schema_version=1,
        publication_id=f"catalog:{catalog.generation_id}",
        record_id=f"knowledge-search:{identity.resource_id}",
        status=status,
        values=_values(
            catalog_generation=catalog.generation_id,
            current_path=catalog.path,
            file_key=catalog.file_key,
            knowledge_snapshot_id=snapshot.snapshot_id,
            processing_signature=catalog.processing_signature,
            resource_id=identity.resource_id,
            revision_id=revision_id,
            source_kind=catalog.source_kind,
        ),
    )


def _compare_causal_records(
    identity: KnowledgeAssetIdentity,
    inventory: _InventoryRecord | None,
    text: _TextRecord | None,
    catalog: _CatalogRecord | None,
    counterevidence: set[str],
) -> None:
    expected_keys = set(_file_key_candidates(identity))
    if text is not None and text.file_key not in expected_keys:
        counterevidence.add("text_file_key_identity_mismatch")
    if catalog is not None and (
        catalog.file_key not in expected_keys
        or catalog.volume_id != str(identity.volume_id)
        or catalog.file_id != str(identity.file_id)
        or catalog.birthtime_ns != identity.birthtime_ns
    ):
        counterevidence.add("catalog_physical_identity_mismatch")
    if (
        inventory is not None
        and text is not None
        and (
            inventory.path != text.path
            or inventory.size != text.size
            or inventory.mtime_ns != text.mtime_ns
            or inventory.birthtime_ns != text.birthtime_ns
        )
    ):
        counterevidence.add("inventory_text_projection_mismatch")
    if (
        inventory is not None
        and catalog is not None
        and (
            inventory.path != catalog.path
            or inventory.size != catalog.size
            or inventory.mtime_ns != catalog.mtime_ns
            or inventory.birthtime_ns != catalog.birthtime_ns
        )
    ):
        counterevidence.add("inventory_catalog_projection_mismatch")
    if (
        text is not None
        and catalog is not None
        and (
            text.file_key != catalog.file_key
            or text.path != catalog.path
            or text.size != catalog.size
            or text.mtime_ns != catalog.mtime_ns
            or text.birthtime_ns != catalog.birthtime_ns
            or text.processing_signature != catalog.processing_signature
            or text.status != catalog.source_status
        )
    ):
        counterevidence.add("text_catalog_projection_mismatch")


def _compare_pdf_causal_records(
    identity: KnowledgeAssetIdentity,
    inventory: _InventoryRecord | None,
    pdf: PdfHealthRecord | None,
    catalog: _CatalogRecord | None,
    counterevidence: set[str],
) -> None:
    expected_keys = set(_file_key_candidates(identity))
    if pdf is not None and pdf.file_key not in expected_keys:
        counterevidence.add("pdf_file_key_identity_mismatch")
    if catalog is not None and (
        catalog.source_kind != "pdf"
        or catalog.file_key not in expected_keys
        or catalog.volume_id != str(identity.volume_id)
        or catalog.file_id != str(identity.file_id)
        or catalog.birthtime_ns != identity.birthtime_ns
    ):
        counterevidence.add("catalog_physical_identity_mismatch")
    if (
        inventory is not None
        and pdf is not None
        and (
            inventory.path != pdf.path
            or inventory.size != pdf.size
            or inventory.mtime_ns != pdf.mtime_ns
            or inventory.birthtime_ns != pdf.birthtime_ns
        )
    ):
        counterevidence.add("inventory_pdf_projection_mismatch")
    if (
        inventory is not None
        and catalog is not None
        and (
            inventory.path != catalog.path
            or inventory.size != catalog.size
            or inventory.mtime_ns != catalog.mtime_ns
            or inventory.birthtime_ns != catalog.birthtime_ns
        )
    ):
        counterevidence.add("inventory_catalog_projection_mismatch")
    if (
        pdf is not None
        and catalog is not None
        and (
            pdf.file_key != catalog.file_key
            or pdf.path != catalog.path
            or pdf.size != catalog.size
            or pdf.mtime_ns != catalog.mtime_ns
            or pdf.birthtime_ns != catalog.birthtime_ns
            or pdf.processing_signature != catalog.processing_signature
            or pdf.status != catalog.source_status
            or pdf.normalized_text_xxh3_128 != catalog.text_fingerprint
        )
    ):
        counterevidence.add("pdf_catalog_projection_mismatch")


def _catalog_source_hints(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
) -> frozenset[str]:
    if _owner_state_gap(snapshot, "catalog") is not None:
        return frozenset()

    def read(connection: sqlite3.Connection) -> tuple[str, ...]:
        rows = connection.execute(
            """SELECT DISTINCT d.source_kind
            FROM catalog_publications p
            JOIN catalog_generations g ON g.generation_id=p.generation_id
                AND g.source_kind=p.source_kind AND g.status='published'
            JOIN catalog_generation_documents d ON d.generation_id=p.generation_id
                AND d.source_kind=p.source_kind
            WHERE d.source_kind IN ('pdf','text') AND d.active=1
                AND d.volume_id=? AND d.file_id=? AND d.birthtime_ns=?
            ORDER BY d.source_kind LIMIT 3""",
            (
                str(identity.volume_id),
                str(identity.file_id),
                identity.birthtime_ns,
            ),
        ).fetchall()
        return tuple(str(row[0]).casefold() for row in rows)

    try:
        values = _read_current_owner(
            paths.catalog,
            owner="catalog",
            expected_version=CATALOG_SCHEMA_VERSION,
            validator=_validate_catalog,
            reader=read,
        )
    except _OwnerReadIssue:
        return frozenset()
    assert isinstance(values, tuple)
    return frozenset(value for value in values if value in {"pdf", "text"})


def _text_dispatch_records(probe: _TextProbe) -> tuple[_TextRecord, ...]:
    records: list[_TextRecord] = []
    for row in probe.rows:
        try:
            records.append(_text_record(row))
        except (OverflowError, TypeError, ValueError):
            continue
    return tuple(records)


def _matches_inventory(
    inventory: _InventoryRecord,
    source: _TextRecord | PdfHealthRecord,
) -> bool:
    return (
        inventory.path == source.path
        and inventory.size == source.size
        and inventory.mtime_ns == source.mtime_ns
        and inventory.birthtime_ns == source.birthtime_ns
    )


def _select_source_kind(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
    inventory: _InventoryRecord | None,
    text_probe: _TextProbe,
    pdf_probe: _PdfProbe,
    counterevidence: set[str],
    examples: _ExampleCollector,
) -> str | None:
    text_records = _text_dispatch_records(text_probe)
    pdf_records = pdf_probe.records
    if inventory is not None:
        current_text = tuple(
            record for record in text_records if _matches_inventory(inventory, record)
        )
        current_pdf = tuple(
            record for record in pdf_records if _matches_inventory(inventory, record)
        )
        if current_text and current_pdf:
            counterevidence.add("source_owner_identity_ambiguous")
            for owner, records in (("pdf", current_pdf), ("text", current_text)):
                record = records[0]
                examples.add(
                    _example(
                        stage=KnowledgeAssetHealthStage.SOURCE_OWNER,
                        code=f"{owner}_source_candidate",
                        record_id=f"{owner}:{record.file_key}",
                        values=_values(
                            file_key=record.file_key,
                            path=record.path,
                            status=record.status,
                        ),
                    )
                )
            return None
        if current_pdf:
            return "pdf"
        if current_text:
            return "text"

    hints = _catalog_source_hints(paths, identity, snapshot)
    if len(hints) == 1:
        return next(iter(hints))
    if len(hints) > 1:
        counterevidence.add("source_owner_identity_ambiguous")
        return None
    if text_records and pdf_records:
        counterevidence.add("source_owner_identity_ambiguous")
        return None
    if pdf_records:
        return "pdf"
    if text_records:
        return "text"
    # Preserve the v1 Text behavior when no published evidence identifies a
    # source owner.  This is a compatibility default, never path inference.
    return "text"


def capture_knowledge_asset_fact_snapshot(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot: KnowledgeSnapshot,
) -> KnowledgeAssetFactSnapshot:
    """Capture one bounded exact causal projection without reading the corpus."""

    if not isinstance(paths, KnowledgeStatePaths):
        raise TypeError("paths must be KnowledgeStatePaths")
    if not isinstance(identity, KnowledgeAssetIdentity):
        raise TypeError("identity must be KnowledgeAssetIdentity")
    if not isinstance(snapshot, KnowledgeSnapshot):
        raise TypeError("snapshot must be KnowledgeSnapshot")

    facts: list[KnowledgeAssetHealthFact] = []
    gaps: set[str] = set()
    counterevidence: set[str] = set()
    examples = _ExampleCollector([])

    inventory_fact, inventory = _capture_inventory(
        paths,
        identity,
        snapshot,
        gaps,
        counterevidence,
        examples,
    )
    if inventory_fact is not None:
        facts.append(inventory_fact)
    text_probe = _probe_text(paths, identity, snapshot)
    pdf_probe = _probe_pdf(paths, identity, snapshot)
    source_kind = _select_source_kind(
        paths,
        identity,
        snapshot,
        inventory,
        text_probe,
        pdf_probe,
        counterevidence,
        examples,
    )

    catalog: _CatalogRecord | None = None
    if source_kind == "text":
        text_fact, text = _capture_text(
            paths,
            identity,
            snapshot,
            gaps,
            counterevidence,
            examples,
            probe=text_probe,
        )
        if text_fact is not None:
            facts.append(text_fact)
        catalog_fact, catalog = _capture_catalog(
            paths,
            identity,
            snapshot,
            gaps,
            counterevidence,
            examples,
            source_kind="text",
        )
        if catalog_fact is not None:
            facts.append(catalog_fact)
        _compare_causal_records(identity, inventory, text, catalog, counterevidence)
    elif source_kind == "pdf":
        pdf_fact, pdf = _capture_pdf(
            identity,
            gaps,
            counterevidence,
            examples,
            probe=pdf_probe,
        )
        if pdf_fact is not None:
            facts.append(pdf_fact)
        pdf_status = None if pdf is None else pdf.status
        catalog_expected = pdf_status in {"done", "partial"} or pdf_status is None
        if catalog_expected:
            catalog_fact, catalog = _capture_catalog(
                paths,
                identity,
                snapshot,
                gaps,
                counterevidence,
                examples,
                source_kind="pdf",
            )
            if catalog_fact is not None:
                facts.append(catalog_fact)
        elif pdf_status in {"error", "protected"} and "pdf" in _catalog_source_hints(
            paths,
            identity,
            snapshot,
        ):
            counterevidence.add("pdf_terminal_catalog_projection_present")
        _compare_pdf_causal_records(identity, inventory, pdf, catalog, counterevidence)

    if catalog is not None:
        facts.append(_knowledge_search_fact(identity, snapshot, catalog))
    elif source_kind == "text" or (
        source_kind == "pdf" and (not facts or facts[-1].status in {"done", "partial"})
    ):
        gaps.add("knowledge_search_projection_missing")

    ordered_examples = tuple(
        sorted(
            examples.values,
            key=lambda value: (
                tuple(KnowledgeAssetHealthStage).index(value.stage),
                value.code,
                value.record_id,
                value.projection_digest,
            ),
        )
    )
    return KnowledgeAssetFactSnapshot.create(
        resource_id=identity.resource_id,
        knowledge_snapshot_id=snapshot.snapshot_id,
        facts=tuple(facts),
        gaps=tuple(sorted(gaps)),
        counterevidence=tuple(sorted(counterevidence)),
        examples=ordered_examples,
        examples_truncated=examples.truncated,
    )


__all__ = ["capture_knowledge_asset_fact_snapshot"]
