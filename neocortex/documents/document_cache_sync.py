"""Idempotent path synchronization after an authorized document move."""

from __future__ import annotations
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from neocortex.platform.policy import sqlite_path_collation

from neocortex.semantic.semantic_schema import SEMANTIC_SCHEMA_VERSION
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
from neocortex.persistence.state_publication import (
    StatePublicationCommitError,
    StatePublicationError,
    publication_idempotency_key,
    read_state_epoch,
    record_state_publication,
)


# region [01] Explicit synchronization result schema


@dataclass(frozen=True, slots=True)
class CacheDatabaseSync:
    database: str
    status: str
    updated_rows: int = 0
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentCacheSyncResult:
    complete: bool
    updated_rows: int
    databases: tuple[CacheDatabaseSync, ...]
    publication_epoch: int = 0
    publication_status: str = "not_published"
    publication_id: str | None = None

    def as_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def error_message(self) -> str | None:
        errors = tuple(item for item in self.databases if item.status == "error")
        if not errors:
            return None
        return "; ".join(
            f"{item.database}: {item.detail or 'unknown synchronization error'}" for item in errors
        )


# endregion [01]


# region [02] Public multi-database synchronization


_MIN_COMPATIBLE_SEMANTIC_SCHEMA = 1
_MAX_COMPATIBLE_SEMANTIC_SCHEMA = SEMANTIC_SCHEMA_VERSION
_PENDING_ACTION_SYNC_BATCH_SIZE = 256
_PATH_COLLATION = sqlite_path_collation()


def synchronize_moved_document(
    state_directory: Path,
    *,
    source_kind: str,
    file_key: str,
    old_path: str,
    new_path: str,
    volume_id: str,
    file_id: str,
) -> DocumentCacheSyncResult:
    """Update current caches and pending plans without rewriting audit history.

    A durable prepare blocks cross-owner readers before any bounded owner-local
    transaction commits. A partial result may resume the same transition;
    completed replays only verify the final paths and never commit new effects.
    """

    if source_kind not in {"pdf", "docx", "xlsx", "pptx", "odt", "text", "audio"}:
        raise ValueError(f"unsupported document source kind: {source_kind}")
    if _path_key(old_path) == _path_key(new_path):
        raise ValueError("cache synchronization requires two distinct paths")
    now_ns = time.time_ns()
    publication_epoch = 0
    publication_key = publication_idempotency_key(
        "document-cache-sync",
        source_kind,
        file_key,
        old_path,
        new_path,
        volume_id,
        file_id,
    )
    publication_owners = (
        source_kind,
        *(("docx_pdf_counterparts",) if source_kind == "pdf" else ()),
        "semantic",
        "framework",
        "dedup",
    )
    try:
        initial_epoch = read_state_epoch(state_directory)
        publication_epoch = initial_epoch.epoch
        prepared = record_state_publication(
            state_directory,
            operation="document-cache-sync",
            owners=publication_owners,
            status="partial",
            idempotency_key=publication_key,
            expected_epoch=initial_epoch.epoch,
            detail="owner-local cache path synchronization prepared",
        )
    except (OSError, StatePublicationError) as exc:
        return DocumentCacheSyncResult(
            complete=False,
            updated_rows=0,
            databases=(
                CacheDatabaseSync("publication", "error", detail=f"{type(exc).__name__}: {exc}"),
            ),
            publication_epoch=publication_epoch,
            publication_status="failed",
        )
    replay = prepared.status == "complete"
    source_database = state_directory / (
        f"{source_kind}.sqlite3"
        if source_kind in {"pdf", "docx", "text", "audio"}
        else "office.sqlite3"
    )
    results = [
        _synchronize_database(
            source_kind,
            source_database,
            required=True,
            verify_only=replay,
            operation=lambda connection: _sync_source_cache(
                connection,
                source_kind=source_kind,
                file_key=file_key,
                old_path=old_path,
                new_path=new_path,
                now_ns=now_ns,
            ),
        )
    ]
    if source_kind == "pdf":
        results.append(
            _synchronize_database(
                "docx_pdf_counterparts",
                state_directory / "docx.sqlite3",
                required=False,
                verify_only=replay,
                operation=lambda connection: _sync_pdf_counterparts(
                    connection,
                    old_path=old_path,
                    new_path=new_path,
                    now_ns=now_ns,
                ),
            )
        )
    results.append(
        _synchronize_database(
            "semantic",
            state_directory / "semantic.sqlite3",
            required=False,
            verify_only=replay,
            operation=lambda connection: _sync_semantic_cache(
                connection,
                source_kind=source_kind,
                file_key=file_key,
                old_path=old_path,
                new_path=new_path,
                now_ns=now_ns,
            ),
        )
    )
    results.extend(
        (
            _synchronize_database(
                "framework",
                state_directory / "framework.sqlite3",
                required=False,
                verify_only=replay,
                operation=lambda connection: _sync_framework_cache(
                    connection,
                    old_path=old_path,
                    new_path=new_path,
                    volume_id=volume_id,
                    file_id=file_id,
                ),
            ),
            _synchronize_database(
                "dedup",
                state_directory / "dedup.sqlite3",
                required=False,
                verify_only=replay,
                operation=lambda connection: _sync_dedup_cache(
                    connection,
                    old_path=old_path,
                    new_path=new_path,
                    volume_id=volume_id,
                    file_id=file_id,
                ),
            ),
        )
    )
    complete = not any(item.status == "error" for item in results)
    publication_epoch = prepared.epoch
    publication_status = prepared.status if complete or not replay else "failed"
    publication_id = prepared.event_id
    if complete and not replay:
        try:
            publication = record_state_publication(
                state_directory,
                operation="document-cache-sync",
                owners=publication_owners,
                status="complete",
                idempotency_key=publication_key,
                expected_epoch=initial_epoch.epoch,
            )
            publication_epoch = publication.epoch
            publication_status = publication.status
            publication_id = publication.event_id
        except StatePublicationCommitError as exc:
            if exc.durable:
                # The append-only journal committed even if the convenience
                # epoch pointer failed; retry must not repeat owner effects.
                publication_epoch = exc.publication.epoch
                publication_status = exc.publication.status
                publication_id = exc.publication.event_id
                results.append(CacheDatabaseSync("publication", "warning", detail=str(exc)))
            else:
                complete = False
                publication_status = "failed"
                results.append(CacheDatabaseSync("publication", "error", detail=str(exc)))
        except (OSError, StatePublicationError) as exc:
            results.append(
                CacheDatabaseSync(
                    "publication",
                    "error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            complete = False
            publication_status = "failed"
    return DocumentCacheSyncResult(
        complete=complete,
        updated_rows=sum(item.updated_rows for item in results),
        databases=tuple(results),
        publication_epoch=publication_epoch,
        publication_status=publication_status,
        publication_id=publication_id,
    )


def _synchronize_database(
    label: str,
    path: Path,
    *,
    required: bool,
    operation: Callable[[sqlite3.Connection], int],
    verify_only: bool = False,
) -> CacheDatabaseSync:
    if not path.is_file():
        if required:
            return CacheDatabaseSync(
                label,
                "error",
                detail=f"required database does not exist: {path}",
            )
        return CacheDatabaseSync(label, "absent")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(existing_sqlite_uri(path), uri=True, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError(f"{label} cache could not enable foreign keys")
        connection.execute("BEGIN IMMEDIATE")
        updated = operation(connection)
        if verify_only:
            if updated:
                raise RuntimeError("completed cache synchronization no longer matches owner paths")
            connection.rollback()
        else:
            connection.commit()
        return CacheDatabaseSync(label, "synced", updated_rows=updated)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        if connection is not None:
            connection.rollback()
        return CacheDatabaseSync(
            label,
            "error",
            detail=f"{type(exc).__name__}: {exc}",
        )
    except BaseException:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


# endregion [02]


# region [03] PDF and DOCX current-cache transitions


def _sync_source_cache(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    file_key: str,
    old_path: str,
    new_path: str,
    now_ns: int,
) -> int:
    _require_columns(connection, "documents", {"file_key", "path"})
    updated = _transition_keyed_path(
        connection,
        "documents",
        "file_key",
        file_key,
        old_path,
        new_path,
        required=True,
        updated_ns=now_ns,
    )
    inventory_table = (
        f"{source_kind}_inventory"
        if source_kind in {"pdf", "docx", "audio"}
        else "office_inventory"
        if source_kind in {"xlsx", "pptx", "odt"}
        else None
    )
    if inventory_table is not None and _table_exists(connection, inventory_table):
        _require_columns(connection, inventory_table, {"file_key", "path"})
        updated += _transition_keyed_path(
            connection,
            inventory_table,
            "file_key",
            file_key,
            old_path,
            new_path,
            required=False,
        )
    fts_table = (
        "page_fts"
        if source_kind == "pdf"
        else ("transcript_fts" if source_kind == "audio" else "document_fts")
    )
    if _table_exists(connection, fts_table):
        _require_columns(connection, fts_table, {"file_key", "path"})
        updated += _transition_fts_paths(
            connection,
            fts_table,
            file_key=file_key,
            old_path=old_path,
            new_path=new_path,
        )
    return updated


def _transition_keyed_path(
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
    key_value: str,
    old_path: str,
    new_path: str,
    *,
    required: bool,
    updated_ns: int | None = None,
) -> int:
    row = connection.execute(
        f"SELECT path FROM {table} WHERE {key_column}=?",
        (key_value,),
    ).fetchone()
    if row is None:
        if required:
            raise RuntimeError(f"{table} has no row for {key_column}={key_value!r}")
        return 0
    current = str(row["path"])
    if _path_key(current) == _path_key(new_path):
        return 0
    if _path_key(current) != _path_key(old_path):
        raise RuntimeError(f"{table} path is neither source nor destination: {current}")
    assignments = "path=?"
    parameters: list[object] = [new_path]
    if updated_ns is not None and "updated_ns" in _table_columns(connection, table):
        assignments += ",updated_ns=?"
        parameters.append(updated_ns)
    parameters.append(key_value)
    cursor = connection.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_column}=?",
        tuple(parameters),
    )
    return cursor.rowcount


def _transition_fts_paths(
    connection: sqlite3.Connection,
    table: str,
    *,
    file_key: str,
    old_path: str,
    new_path: str,
) -> int:
    rows = connection.execute(
        f"SELECT DISTINCT path FROM {table} WHERE file_key=? LIMIT 3",
        (file_key,),
    ).fetchall()
    unexpected = tuple(
        str(row["path"])
        for row in rows
        if _path_key(str(row["path"])) not in {_path_key(old_path), _path_key(new_path)}
    )
    if unexpected:
        raise RuntimeError(f"{table} contains an unexpected cached path: {unexpected[0]}")
    cursor = connection.execute(
        f"UPDATE {table} SET path=? WHERE file_key=? AND path=?",
        (new_path, file_key, old_path),
    )
    return cursor.rowcount


def _sync_pdf_counterparts(
    connection: sqlite3.Connection,
    *,
    old_path: str,
    new_path: str,
    now_ns: int,
) -> int:
    if not _table_exists(connection, "pdf_counterparts"):
        return 0
    columns = _table_columns(connection, "pdf_counterparts")
    if "pdf_path" not in columns:
        raise RuntimeError("pdf_counterparts lacks pdf_path")
    assignment = "pdf_path=?"
    parameters: list[object] = [new_path]
    if "updated_ns" in columns:
        assignment += ",updated_ns=?"
        parameters.append(now_ns)
    parameters.append(old_path)
    cursor = connection.execute(
        f"UPDATE pdf_counterparts SET {assignment} WHERE pdf_path=? COLLATE {_PATH_COLLATION}",
        tuple(parameters),
    )
    return cursor.rowcount


# endregion [03]


# region [04] Semantic current-state transition


def _sync_semantic_cache(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    file_key: str,
    old_path: str,
    new_path: str,
    now_ns: int,
) -> int:
    """Move one exact semantic identity without creating or reindexing state."""

    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if not (_MIN_COMPATIBLE_SEMANTIC_SCHEMA <= schema_version <= _MAX_COMPATIBLE_SEMANTIC_SCHEMA):
        raise RuntimeError(
            f"semantic schema version is not compatible with path synchronization: {schema_version}"
        )
    _require_columns(connection, "metadata", {"key", "value"})
    metadata_version = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if metadata_version is None:
        raise RuntimeError("semantic metadata lacks schema_version")
    try:
        declared_version = int(metadata_version["value"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("semantic metadata schema_version is invalid") from exc
    if declared_version != schema_version:
        raise RuntimeError("semantic schema version metadata does not match PRAGMA user_version")
    _require_columns(
        connection,
        "semantic_items",
        {"item_id", "source_kind", "source_identity", "path", "updated_ns"},
    )
    item_id = f"item:{source_kind}:{file_key}"
    row = connection.execute(
        """SELECT source_kind,source_identity,path FROM semantic_items
        WHERE item_id=?""",
        (item_id,),
    ).fetchone()
    if row is None:
        return 0
    if str(row["source_kind"]) != source_kind or str(row["source_identity"]) != file_key:
        raise RuntimeError(f"semantic item identity does not match its item_id: {item_id!r}")
    current_path = None if row["path"] is None else str(row["path"])
    if current_path is not None and _path_key(current_path) == _path_key(new_path):
        return 0
    if current_path is None or _path_key(current_path) != _path_key(old_path):
        raise RuntimeError(
            f"semantic_items path is neither source nor destination: {current_path!r}"
        )
    cursor = connection.execute(
        """UPDATE semantic_items SET path=?,updated_ns=?
        WHERE item_id=? AND source_kind=? AND source_identity=? AND path=?""",
        (new_path, now_ns, item_id, source_kind, file_key, current_path),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"semantic item path transition was not exact: {item_id!r}")
    return cursor.rowcount


# endregion [04]


# region [05] Framework and deduplication current-state transitions


def _sync_framework_cache(
    connection: sqlite3.Connection,
    *,
    old_path: str,
    new_path: str,
    volume_id: str,
    file_id: str,
) -> int:
    updated = 0
    volume_values = _identity_text_values(volume_id)
    file_values = _identity_text_values(file_id)
    volume_placeholders = ",".join("?" for _ in volume_values)
    file_placeholders = ",".join("?" for _ in file_values)
    if _table_exists(connection, "route_candidates"):
        _require_columns(
            connection,
            "route_candidates",
            {"run_id", "path", "volume_id", "file_id"},
        )
        running_predicate = ""
        if _table_exists(connection, "initial_runs"):
            running_predicate = (
                " AND run_id IN (SELECT run_id FROM initial_runs WHERE status='running')"
            )
        cursor = connection.execute(
            "UPDATE route_candidates SET path=? "
            f"WHERE volume_id IN ({volume_placeholders}) "
            f"AND file_id IN ({file_placeholders}) "
            f"AND path=? COLLATE {_PATH_COLLATION}{running_predicate}",
            (new_path, *volume_values, *file_values, old_path),
        )
        updated += cursor.rowcount
    if _table_exists(connection, "review_candidates"):
        _require_columns(
            connection,
            "review_candidates",
            {"path", "volume_id", "file_id", "status"},
        )
        cursor = connection.execute(
            "UPDATE review_candidates SET path=? "
            f"WHERE volume_id IN ({volume_placeholders}) "
            f"AND file_id IN ({file_placeholders}) "
            f"AND status='open' AND path=? COLLATE {_PATH_COLLATION}",
            (new_path, *volume_values, *file_values, old_path),
        )
        updated += cursor.rowcount
    if _table_exists(connection, "file_actions"):
        updated += _sync_pending_file_actions(
            connection,
            old_path=old_path,
            new_path=new_path,
        )
    return updated


def _sync_pending_file_actions(
    connection: sqlite3.Connection,
    *,
    old_path: str,
    new_path: str,
) -> int:
    _require_columns(connection, "file_actions", {"source_path", "status"})
    columns = _table_columns(connection, "file_actions")
    detailed = {"action_id", "action_type", "target_path"}.issubset(columns)
    if not detailed:
        cursor = connection.execute(
            "UPDATE file_actions SET source_path=? WHERE status='planned' "
            f"AND source_path=? COLLATE {_PATH_COLLATION}",
            (new_path, old_path),
        )
        return cursor.rowcount
    updated = 0
    while rows := connection.execute(
        f"""SELECT action_id,action_type,target_path FROM file_actions
        WHERE status='planned' AND source_path=? COLLATE {_PATH_COLLATION}
        ORDER BY action_id LIMIT ?""",
        (old_path, _PENDING_ACTION_SYNC_BATCH_SIZE),
    ).fetchall():
        batch_updated = 0
        for row in rows:
            target = None if row["target_path"] is None else str(row["target_path"])
            if str(row["action_type"]) == "correct_extension" and target is not None:
                if _path_key(str(Path(target).parent)) != _path_key(str(Path(old_path).parent)):
                    raise RuntimeError(
                        "pending extension-correction target is outside its source directory"
                    )
                target = str(Path(new_path).parent / Path(target).name)
            cursor = connection.execute(
                "UPDATE file_actions SET source_path=?,target_path=? WHERE action_id=?",
                (new_path, target, row["action_id"]),
            )
            batch_updated += cursor.rowcount
        if batch_updated != len(rows):
            raise RuntimeError("pending file actions changed during cache synchronization")
        updated += batch_updated
    cursor = connection.execute(
        "UPDATE file_actions SET target_path=? WHERE status='planned' "
        f"AND target_path=? COLLATE {_PATH_COLLATION}",
        (new_path, old_path),
    )
    return updated + cursor.rowcount


def _sync_dedup_cache(
    connection: sqlite3.Connection,
    *,
    old_path: str,
    new_path: str,
    volume_id: str,
    file_id: str,
) -> int:
    volume_blob = _identity_blob(volume_id)
    file_blob = _identity_blob(file_id)
    updated = 0
    if _table_exists(connection, "files"):
        _require_columns(connection, "files", {"path", "volume_id", "file_id"})
        rows = connection.execute(
            f"SELECT volume_id,file_id FROM files WHERE path=? COLLATE {_PATH_COLLATION}",
            (old_path,),
        ).fetchall()
        if rows:
            if any(
                bytes(row["volume_id"]) != volume_blob or bytes(row["file_id"]) != file_blob
                for row in rows
            ):
                raise RuntimeError(
                    "dedup files source path belongs to another identity "
                    "in at least one retained generation"
                )
            destination_rows = connection.execute(
                f"SELECT volume_id,file_id FROM files WHERE path=? COLLATE {_PATH_COLLATION}",
                (new_path,),
            ).fetchall()
            if any(
                bytes(row["volume_id"]) != volume_blob or bytes(row["file_id"]) != file_blob
                for row in destination_rows
            ):
                raise RuntimeError("dedup files destination belongs to another identity")
            cursor = connection.execute(
                f"UPDATE files SET path=? WHERE path=? COLLATE {_PATH_COLLATION}",
                (new_path, old_path),
            )
            updated += cursor.rowcount
        else:
            destinations = connection.execute(
                f"SELECT volume_id,file_id FROM files WHERE path=? COLLATE {_PATH_COLLATION}",
                (new_path,),
            ).fetchall()
            if any(
                bytes(destination["volume_id"]) != volume_blob
                or bytes(destination["file_id"]) != file_blob
                for destination in destinations
            ):
                raise RuntimeError("dedup files destination belongs to another identity")
    keep_groups: tuple[int, ...] = ()
    if _table_exists(connection, "planned_duplicate_members"):
        _require_columns(
            connection,
            "planned_duplicate_members",
            {"group_id", "role", "path", "volume_id", "file_id"},
        )
        keep_groups = tuple(
            int(row["group_id"])
            for row in connection.execute(
                "SELECT group_id FROM planned_duplicate_members "
                "WHERE role='keep' AND volume_id=? AND file_id=? "
                f"AND path=? COLLATE {_PATH_COLLATION}",
                (volume_blob, file_blob, old_path),
            )
        )
        cursor = connection.execute(
            "UPDATE planned_duplicate_members SET path=? "
            f"WHERE volume_id=? AND file_id=? AND path=? COLLATE {_PATH_COLLATION}",
            (new_path, volume_blob, file_blob, old_path),
        )
        updated += cursor.rowcount
    if keep_groups and _table_exists(connection, "planned_duplicate_groups"):
        _require_columns(
            connection,
            "planned_duplicate_groups",
            {"group_id", "keep_path"},
        )
        placeholders = ",".join("?" for _ in keep_groups)
        cursor = connection.execute(
            f"UPDATE planned_duplicate_groups SET keep_path=? "
            f"WHERE group_id IN ({placeholders}) "
            f"AND keep_path=? COLLATE {_PATH_COLLATION}",
            (new_path, *keep_groups, old_path),
        )
        updated += cursor.rowcount
    return updated


# endregion [05]


# region [06] Schema and identity helpers


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> None:
    columns = _table_columns(connection, table)
    missing = required - columns
    if missing:
        raise RuntimeError(f"{table} lacks required columns: {sorted(missing)}")


def _identity_blob(value: str) -> bytes:
    integer = int(value)
    if integer < 0 or integer.bit_length() > 128:
        raise ValueError("filesystem identity does not fit an unsigned 128-bit value")
    return integer.to_bytes(16, "little")


def _identity_text_values(value: str) -> tuple[str, ...]:
    integer = int(value)
    return tuple(dict.fromkeys((value, str(integer), f"{integer:x}", f"{integer:032x}")))


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


# endregion [06]
