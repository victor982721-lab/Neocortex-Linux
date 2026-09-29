"""Owner-bound invalidation after a verified Trash effect.

This owner consumes only Framework ``file_actions`` receipts.  It never
searches by path alone: every retirement target is the exact physical identity
from the applied action's expected snapshot.  A path reused by another inode
therefore remains untouched.  Route owners and Semantic are invalidated with
their existing identity keys; historical generations and append-only receipts
are preserved.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
from neocortex.persistence.state_publication import (
    StatePublicationError,
    publication_idempotency_key,
    read_state_epoch,
    record_state_publication,
)
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.semantic.semantic_models import ContentFingerprint
from neocortex.safety.kio_trash import (
    FULL_ALGORITHM,
    is_metadata_binding,
    metadata_binding,
    trash_receipt_paths,
)


SCHEMA = "neocortex.document-retirement/v1"
_TRASH_ACTIONS = (
    "trash_redlist",
    "trash_artifact",
    "trash_duplicate",
    "trash_empty_file",
)
_ROUTE_DATABASES = (
    ("pdf", "pdf.sqlite3", "documents"),
    ("docx", "docx.sqlite3", "documents"),
    ("office", "office.sqlite3", "documents"),
    ("text", "text.sqlite3", "documents"),
    ("audio", "audio.sqlite3", "documents"),
    ("video", "video.sqlite3", "documents"),
    ("image", "image.sqlite3", "images"),
)
_PAGE = 256
_OWNER_BATCH = 256
_MAX_SEMANTIC_MATCHES = 4096
_EMPTY_FULL_DIGEST = FULL_ALGORITHM + ":" + hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class RetiredSource:
    action_id: int
    action_type: str
    snapshot: FileSnapshot
    source_digest: str
    receipt: Mapping[str, object]

    @property
    def file_key(self) -> str:
        return file_key_from_snapshot(self.snapshot)


@dataclass(slots=True)
class RetirementSummary:
    """Bounded, JSON-safe retirement result."""

    status: str = "complete"
    observed_actions: int = 0
    zip_receipts_checked: int = 0
    unique_sources: int = 0
    retired_sources: int = 0
    skipped_replaced: int = 0
    invalidated_rows: int = 0
    semantic_items_deactivated: int = 0
    framework_rows_removed: int = 0
    dedup_rows_removed: int = 0
    recovery_required: int = 0
    publication_epoch: int = 0
    errors: list[str] | None = None
    replay: bool = False

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def error(self, detail: object) -> None:
        assert self.errors is not None
        if len(self.errors) < 32:
            self.errors.append(str(detail)[:512])
        self.status = "recovery_required"
        self.recovery_required += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "status": self.status,
            "observed_actions": self.observed_actions,
            "zip_receipts_checked": self.zip_receipts_checked,
            "unique_sources": self.unique_sources,
            "retired_sources": self.retired_sources,
            "skipped_replaced": self.skipped_replaced,
            "invalidated_rows": self.invalidated_rows,
            "semantic_items_deactivated": self.semantic_items_deactivated,
            "framework_rows_removed": self.framework_rows_removed,
            "dedup_rows_removed": self.dedup_rows_removed,
            "recovery_required": self.recovery_required,
            "publication_epoch": self.publication_epoch,
            "errors": tuple(self.errors or ()),
            "replay": self.replay,
            "idempotent": True,
        }


def _absolute_inside(root: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        raise ValueError("retirement source path must be absolute")
    path = Path(os.path.abspath(raw))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("retirement source path escapes the corpus root") from exc
    return path


def _path_inside(root: Path, raw: object) -> bool:
    if not isinstance(raw, str) or not os.path.isabs(raw):
        return False
    try:
        Path(os.path.abspath(raw)).relative_to(root)
    except ValueError:
        return False
    return True


def _parse_identity(raw: object, *, path: Path) -> FileSnapshot:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65_536:
        raise ValueError("retirement expected identity is missing or too large")
    value = json.loads(raw)
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise ValueError("retirement expected identity schema is unsupported")
    source = value.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("retirement expected identity has no source")
    source_path = source.get("path")
    if not isinstance(source_path, str) or os.path.normcase(os.path.abspath(source_path)) != os.path.normcase(os.fspath(path)):
        raise ValueError("retirement expected identity path differs from action source")
    try:
        volume = int(str(source["volume_id"]), 16)
        inode = int(str(source["file_id"]), 16)
        size = int(source["size"])
        mtime = int(source["mtime_ns"])
        birthtime = int(source["birthtime_ns"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("retirement expected identity metadata is invalid") from exc
    if min(volume, inode, size, mtime) < 0 or birthtime < -1:
        raise ValueError("retirement expected identity metadata is negative")
    return FileSnapshot(str(path), volume, inode, size, mtime, birthtime)


def _validate_receipt(
    raw: object,
    snapshot: FileSnapshot,
    *,
    root: Path,
    action_type: str,
) -> tuple[str, Mapping[str, object]]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65_536:
        raise ValueError("retirement effect receipt is missing or too large")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("retirement effect receipt is not an object")
    if (
        value.get("schema_version") != 1
        or value.get("receipt_type") != "successful_return_and_observation"
        or value.get("operation") != "trash"
        or value.get("source_absent") is not True
        or value.get("target_path") is not None
        or value.get("source_path") != snapshot.path
    ):
        raise ValueError("retirement effect receipt is not an applied Trash receipt")
    digest = value.get("source_digest")
    if not isinstance(digest, str):
        raise ValueError("retirement effect receipt lacks source digest")
    if is_metadata_binding(digest):
        if metadata_binding(snapshot) != digest:
            raise ValueError("retirement metadata binding differs from expected identity")
    elif digest.startswith(FULL_ALGORITHM + ":"):
        raw_digest = digest.split(":", 1)[1]
        if len(raw_digest) != 64 or any(character not in "0123456789abcdef" for character in raw_digest):
            raise ValueError("retirement full digest is malformed")
    else:
        raise ValueError("retirement source digest is unsupported")
    if action_type == "trash_empty_file":
        if snapshot.size != 0:
            raise ValueError("trash_empty_file source identity is not empty")
        if digest != _EMPTY_FULL_DIGEST:
            raise ValueError("trash_empty_file digest is not the empty-file SHA-256")
    trash_receipt_paths(value.get("trash"), snapshot, digest)
    return digest, value


def _read_retired_sources(
    framework_state: Any,
    *,
    root: Path,
    run_id: int,
    summary: RetirementSummary,
    checkpoint: Callable[[], None] | None = None,
) -> Iterator[RetiredSource]:
    connection = getattr(framework_state, "_connection", None)
    if connection is None:
        raise RuntimeError("FrameworkState live connection is required")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(file_actions)")}
    required = {"action_id", "run_id", "action_type", "status", "source_path", "expected_identity_json", "effect_receipt_json"}
    if not required.issubset(columns):
        raise RuntimeError("Framework file_actions schema lacks retirement fields")
    action_placeholders = ",".join("?" for _ in _TRASH_ACTIONS)
    cursor = connection.execute(
        f"""SELECT action_id,action_type,source_path,expected_identity_json,effect_receipt_json
        FROM file_actions WHERE run_id=? AND status='applied'
        AND action_type IN ({action_placeholders}) ORDER BY action_id""",
        (run_id, *_TRASH_ACTIONS),
    )
    while True:
        if checkpoint is not None:
            checkpoint()
        rows = cursor.fetchmany(_PAGE)
        if not rows:
            break
        for row in rows:
            summary.observed_actions += 1
            try:
                source_path = _absolute_inside(root, row[2])
                snapshot = _parse_identity(row[3], path=source_path)
                action_type = str(row[1])
                digest, receipt = _validate_receipt(
                    row[4], snapshot, root=root, action_type=action_type
                )
                summary.unique_sources += 1
                yield RetiredSource(int(row[0]), action_type, snapshot, digest, receipt)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                summary.error(f"action {row[0]}: {type(exc).__name__}: {exc}")
    # ZIP Intake publishes one bounded lifecycle receipt per consumed source.
    # These sources do not have a Framework file_action row, but their exact
    # physical identity can still retire any owner representation keyed to it.
    event_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(run_events)")}
    if {"run_id", "phase", "details_json"}.issubset(event_columns):
        event_cursor = connection.execute(
            "SELECT details_json FROM run_events WHERE run_id=? AND phase='lifecycle-stage' "
            "AND json_extract(details_json,'$.stage')='zip-consumption' ORDER BY event_id",
            (run_id,),
        )
        while True:
            if checkpoint is not None:
                checkpoint()
            events = event_cursor.fetchmany(_PAGE)
            if not events:
                break
            for event in events:
                try:
                    payload = json.loads(str(event[0]))
                    details = payload.get("details", payload) if isinstance(payload, Mapping) else {}
                    outcomes = details.get("source_outcomes", ()) if isinstance(details, Mapping) else ()
                    if not isinstance(outcomes, (list, tuple)):
                        raise ValueError("zip source_outcomes is not a sequence")
                    for outcome in outcomes:
                        if not isinstance(outcome, Mapping):
                            raise ValueError("zip source outcome is not an object")
                        if (
                            outcome.get("status") != "applied"
                            or outcome.get("published") is not True
                            or outcome.get("trashed") is not True
                        ):
                            continue
                        identity = outcome.get("source_identity")
                        source_path = _absolute_inside(root, outcome.get("source_path"))
                        if not isinstance(identity, Mapping):
                            raise ValueError("zip source identity is missing")
                        try:
                            volume_id = int(identity["device"])
                            file_id = int(identity["inode"])
                            size = int(identity["size"])
                            mtime_ns = int(identity["mtime_ns"])
                        except (KeyError, TypeError, ValueError, OverflowError) as exc:
                            raise ValueError("zip source identity metadata is invalid") from exc
                        if min(volume_id, file_id, size, mtime_ns) < 0:
                            raise ValueError("zip source identity metadata is negative")
                        snapshot = FileSnapshot(
                            str(source_path), volume_id, file_id, size, mtime_ns, -1,
                        )
                        digest = outcome.get("source_sha256")
                        if (
                            not isinstance(digest, str)
                            or len(digest) != 64
                            or any(c not in "0123456789abcdef" for c in digest.casefold())
                        ):
                            raise ValueError("zip source digest is missing")
                        summary.unique_sources += 1
                        summary.zip_receipts_checked += 1
                        yield RetiredSource(
                            0,
                            "zip_consumption",
                            snapshot,
                            FULL_ALGORITHM + ":" + digest.casefold(),
                            dict(outcome),
                        )
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    summary.error(f"zip consumption receipt: {type(exc).__name__}: {exc}")


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _column_types(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]): str(row[2]).upper()
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


@contextmanager
def _owner_transaction(connection: sqlite3.Connection, *, verify_only: bool) -> Iterator[None]:
    """Hold one bounded owner lease for a retirement batch."""

    connection.execute("BEGIN" if verify_only else "BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _validate_route_row(
    row: sqlite3.Row,
    columns: set[str],
    source: RetiredSource,
    *,
    table: str,
    root: Path,
) -> None:
    row_path = row["path"]
    if not isinstance(row_path, str) or not _path_inside(root, row_path):
        raise RuntimeError(f"{table} owner row is outside the retirement root")
    for field, expected in (
        ("size", source.snapshot.size),
        ("mtime_ns", source.snapshot.mtime_ns),
        ("birthtime_ns", source.snapshot.birthtime_ns),
    ):
        if field in columns and int(row[field]) != expected and not (
            field == "birthtime_ns" and expected == -1
        ):
            raise RuntimeError(f"{table} row identity metadata differs for {source.file_key}")


def _retire_route_database_batch(
    database: Path,
    sources: tuple[RetiredSource, ...],
    *,
    table: str,
    root: Path,
    verify_only: bool,
) -> tuple[str, int]:
    if not database.is_file():
        return "absent", 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(existing_sqlite_uri(database), uri=True, timeout=60.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        columns = _columns(connection, table)
        if not {"file_key", "path"}.issubset(columns):
            raise RuntimeError(f"{table} owner schema lacks file identity/path")
        with _owner_transaction(connection, verify_only=verify_only):
            keys: list[str] = []
            for source in sources:
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE file_key=?", (source.file_key,)
                ).fetchone()
                if row is None:
                    continue
                _validate_route_row(row, columns, source, table=table, root=root)
                keys.append(source.file_key)
            if verify_only:
                return "verified" if keys else "absent", 0
            total = 0
            candidates = tuple(
                candidate
                for candidate in _table_names(connection)
                if "file_key" in _columns(connection, candidate)
            )
            for key in dict.fromkeys(keys):
                for candidate in candidates:
                    cursor = connection.execute(
                        f"DELETE FROM {candidate} WHERE file_key=?", (key,)
                    )
                    total += max(0, int(cursor.rowcount))
            return "retired" if keys else "absent", total
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


def _retire_route_database(
    database: Path,
    source: RetiredSource,
    *,
    table: str,
    root: Path,
    verify_only: bool,
) -> tuple[str, int]:
    return _retire_route_database_batch(
        database,
        (source,),
        table=table,
        root=root,
        verify_only=verify_only,
    )


def _retire_catalog_batch(
    database: Path,
    sources: tuple[RetiredSource, ...],
    *,
    root: Path,
    verify_only: bool,
) -> tuple[str, int]:
    if not database.is_file():
        return "absent", 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(existing_sqlite_uri(database), uri=True, timeout=60.0)
        connection.row_factory = sqlite3.Row
        columns = _columns(connection, "documents")
        required = {"source_kind", "file_key", "path", "active"}
        if not required.issubset(columns):
            raise RuntimeError("catalog documents schema lacks retirement fields")
        with _owner_transaction(connection, verify_only=verify_only):
            keys: list[str] = []
            for source in sources:
                rows = connection.execute(
                    "SELECT * FROM documents WHERE file_key=? AND active=1 LIMIT 257",
                    (source.file_key,),
                ).fetchall()
                if len(rows) > 256:
                    raise RuntimeError("catalog identity match exceeds its bound")
                if not rows:
                    continue
                if any(
                    not isinstance(row["path"], str)
                    or not _path_inside(root, row["path"])
                    for row in rows
                ):
                    raise RuntimeError("catalog owner row is outside the retirement root")
                keys.append(source.file_key)
            if verify_only:
                return "verified" if keys else "absent", 0
            if not keys:
                return "absent", 0
            assignments = "active=0"
            if "updated_ns" in columns:
                assignments += ",updated_ns=?"
                timestamp = time.time_ns()
            else:
                timestamp = None
            total = 0
            for key in dict.fromkeys(keys):
                parameters: tuple[object, ...] = (
                    (timestamp, key) if timestamp is not None else (key,)
                )
                cursor = connection.execute(
                    f"UPDATE documents SET {assignments} WHERE file_key=? AND active=1",
                    parameters,
                )
                total += max(0, int(cursor.rowcount))
            return "retired", total
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


def _retire_catalog(
    database: Path,
    source: RetiredSource,
    *,
    root: Path,
    verify_only: bool,
) -> tuple[str, int]:
    return _retire_catalog_batch(
        database,
        (source,),
        root=root,
        verify_only=verify_only,
    )


def _semantic_rows_for_source(
    connection: sqlite3.Connection,
    source: RetiredSource,
    *,
    root: Path,
) -> list[tuple[str, ContentFingerprint]]:
    rows = connection.execute(
        "SELECT item_id,source_identity,path,source_revision_json,content_xxh3_128,"
        "content_bytes,content_xxh3_64_guard "
        "FROM semantic_items WHERE active=1 AND source_identity=? LIMIT 257",
        (source.file_key,),
    ).fetchall()
    if len(rows) > 256:
        raise RuntimeError("semantic retirement identity match exceeds its bound")
    if not rows:
        rows = connection.execute(
            "SELECT item_id,source_identity,path,source_revision_json,content_xxh3_128,"
            "content_bytes,content_xxh3_64_guard FROM semantic_items WHERE active=1 "
            "AND json_extract(source_revision_json,'$.volume_id')=? "
            "AND json_extract(source_revision_json,'$.file_id')=? LIMIT 257",
            (source.snapshot.volume_id, source.snapshot.file_id),
        ).fetchall()
        if len(rows) > 256:
            raise RuntimeError("semantic retirement revision match exceeds its bound")
    found: list[tuple[str, ContentFingerprint]] = []
    for row in rows:
        if row["path"] is not None and (
            not isinstance(row["path"], str) or not _path_inside(root, row["path"])
        ):
            # Leave malformed/outside rows untouched rather than broadening
            # retirement scope through a path-only match.
            continue
        matches = str(row["source_identity"]) == source.file_key
        if not matches:
            try:
                revision = json.loads(str(row["source_revision_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                revision = {}
            if isinstance(revision, Mapping):
                matches = (
                    str(revision.get("volume_id")) == str(source.snapshot.volume_id)
                    and str(revision.get("file_id")) == str(source.snapshot.file_id)
                )
        if not matches:
            continue
        found.append(
            (
                str(row["item_id"]),
                ContentFingerprint(
                    str(row["content_xxh3_128"]),
                    int(row["content_bytes"]),
                    str(row["content_xxh3_64_guard"]),
                ),
            )
        )
    return found


def _same_semantic_fingerprint(row: sqlite3.Row, fingerprint: ContentFingerprint) -> bool:
    return (
        str(row["content_xxh3_128"]) == fingerprint.xxh3_128
        and int(row["content_bytes"]) == fingerprint.byte_count
        and str(row["content_xxh3_64_guard"]) == fingerprint.xxh3_64_guard
    )


def _retire_semantic_batch(
    database: Path,
    sources: tuple[RetiredSource, ...],
    *,
    root: Path,
    verify_only: bool,
) -> tuple[str, int]:
    if not database.is_file():
        return "absent", 0
    from neocortex.semantic.semantic_schema import semantic_database

    with semantic_database(database, readonly=verify_only) as connection:
        columns = _columns(connection, "semantic_items")
        required = {
            "item_id",
            "source_identity",
            "path",
            "source_revision_json",
            "content_xxh3_128",
            "content_bytes",
            "content_xxh3_64_guard",
            "active",
        }
        if not required.issubset(columns):
            raise RuntimeError("semantic_items schema lacks retirement fields")
        with _owner_transaction(connection, verify_only=verify_only):
            found: dict[str, ContentFingerprint] = {}
            for source in sources:
                for item_id, fingerprint in _semantic_rows_for_source(
                    connection, source, root=root
                ):
                    prior = found.get(item_id)
                    if prior is not None and prior != fingerprint:
                        raise RuntimeError(f"semantic item fingerprints disagree: {item_id}")
                    found[item_id] = fingerprint
                    if len(found) > _MAX_SEMANTIC_MATCHES:
                        raise RuntimeError("semantic retirement batch exceeds its bound")
            if not found:
                return "absent", 0
            if verify_only:
                return "verified", 0
            changed = 0
            updated_ns = time.time_ns()
            for item_id, fingerprint in found.items():
                row = connection.execute(
                    "SELECT content_xxh3_128,content_bytes,content_xxh3_64_guard "
                    "FROM semantic_items WHERE item_id=? AND active=1",
                    (item_id,),
                ).fetchone()
                if row is None or not _same_semantic_fingerprint(row, fingerprint):
                    raise RuntimeError(f"semantic item changed during retirement: {item_id}")
                connection.execute(
                    "UPDATE text_chunks SET active=0,updated_ns=? "
                    "WHERE item_id=? AND active=1",
                    (updated_ns, item_id),
                )
                connection.execute(
                    "UPDATE semantic_evidence SET active=0,updated_ns=? "
                    "WHERE item_id=? AND active=1",
                    (updated_ns, item_id),
                )
                cursor = connection.execute(
                    "UPDATE semantic_items SET active=0,updated_ns=? "
                    "WHERE item_id=? AND active=1",
                    (updated_ns, item_id),
                )
                if int(cursor.rowcount) != 1:
                    raise RuntimeError(f"semantic item changed during retirement: {item_id}")
                changed += 1
            return "retired", changed


def _retire_semantic(
    database: Path,
    source: RetiredSource,
    *,
    root: Path,
    verify_only: bool,
) -> tuple[str, int]:
    return _retire_semantic_batch(
        database,
        (source,),
        root=root,
        verify_only=verify_only,
    )


def _retire_framework_batch(
    connection: sqlite3.Connection,
    sources: tuple[RetiredSource, ...],
    *,
    verify_only: bool,
) -> tuple[str, int]:
    columns = _columns(connection, "route_candidates")
    required = {"volume_id", "file_id"}
    if not required.issubset(columns):
        return "absent", 0
    types = _column_types(connection, "route_candidates")
    text_identity = any(
        token in types.get(field, "")
        for field in ("volume_id", "file_id")
        for token in ("CHAR", "CLOB", "TEXT")
    )
    identities: list[tuple[int, int]] = []
    for source in sources:
        identity = (source.snapshot.volume_id, source.snapshot.file_id)
        parameters: tuple[object, object] = (
            (f"{identity[0]:x}", f"{identity[1]:x}")
            if text_identity
            else identity
        )
        rows = connection.execute(
            "SELECT 1 FROM route_candidates WHERE volume_id=? AND file_id=? LIMIT 257",
            parameters,
        ).fetchall()
        if len(rows) > 256:
            raise RuntimeError("framework identity match exceeds its bound")
        if rows:
            identities.append(identity)
    if verify_only:
        return "verified" if identities else "absent", 0
    total = 0
    for volume_id, file_id in dict.fromkeys(identities):
        parameters = (
            (f"{volume_id:x}", f"{file_id:x}")
            if text_identity
            else (volume_id, file_id)
        )
        cursor = connection.execute(
            "DELETE FROM route_candidates WHERE volume_id=? AND file_id=?",
            parameters,
        )
        total += max(0, int(cursor.rowcount))
    return "retired" if identities else "absent", total


def _retire_framework(
    connection: sqlite3.Connection,
    source: RetiredSource,
    *,
    verify_only: bool,
) -> tuple[str, int]:
    return _retire_framework_batch(connection, (source,), verify_only=verify_only)


def _retire_dedup_batch(
    database: Path,
    sources: tuple[RetiredSource, ...],
    *,
    verify_only: bool,
) -> tuple[str, int]:
    if not database.is_file():
        return "absent", 0
    with DedupIndex(database) as index:
        removals: dict[int, list[str]] = {}
        scheduled: set[tuple[int, str]] = set()
        removed_rows = 0
        for source in sources:
            scan_id = index.current_scan_for_path(source.snapshot.path)
            rows = index._connection.execute(
                "SELECT path,volume_id,file_id FROM files WHERE scan_id=? AND path=? LIMIT 257",
                (scan_id, source.snapshot.path),
            ).fetchall()
            if len(rows) > 256:
                raise RuntimeError("dedup path match exceeds its bound")
            if not rows:
                continue
            for row in rows:
                identity = (
                    int.from_bytes(bytes(row[1]), "little"),
                    int.from_bytes(bytes(row[2]), "little"),
                )
                if identity != source.snapshot.identity:
                    raise RuntimeError("dedup current row belongs to a replacement identity")
            scheduled_key = (scan_id, source.snapshot.path)
            if scheduled_key not in scheduled:
                scheduled.add(scheduled_key)
                removals.setdefault(scan_id, []).append(source.snapshot.path)
                removed_rows += len(rows)
        if verify_only:
            return "verified" if removals else "absent", 0
        for scan_id, paths in removals.items():
            index.apply_reconciliation(scan_id, remove_paths=tuple(dict.fromkeys(paths)))
        return "retired" if removals else "absent", removed_rows


def _retire_dedup(database: Path, source: RetiredSource, *, verify_only: bool) -> tuple[str, int]:
    return _retire_dedup_batch(database, (source,), verify_only=verify_only)


@contextmanager
def _framework_transaction(connection: sqlite3.Connection):
    outer = connection.in_transaction
    savepoint = f"document_retirement_{id(connection):x}"
    if outer:
        connection.execute(f"SAVEPOINT {savepoint}")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if outer:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.commit()
    except BaseException:
        if outer:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif connection.in_transaction:
            connection.rollback()
        raise


def _source_batches(
    sources: Iterator[RetiredSource],
    *,
    batch_size: int = _OWNER_BATCH,
) -> Iterator[tuple[RetiredSource, ...]]:
    batch: list[RetiredSource] = []
    for source in sources:
        batch.append(source)
        if len(batch) == batch_size:
            yield tuple(batch)
            batch.clear()
    if batch:
        yield tuple(batch)


def run_document_retirement(
    state_directory: Path,
    *,
    root: Path,
    framework_state: Any,
    run_id: int,
    framework_lock_held: bool = True,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Invalidate owner-current representations of applied Trash sources."""

    if not isinstance(state_directory, Path) or not state_directory.is_absolute():
        raise ValueError("state_directory must be an absolute Path")
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("root must be an absolute Path")
    if type(run_id) is not int or run_id < 1:
        raise ValueError("run_id must be positive")
    summary = RetirementSummary()
    # The run manifest is immutable while the framework lock is held.  Keep
    # the key independent of the receipt count/identities so a large run is
    # processed as a stream rather than first materialized in Python.
    publication_id = publication_idempotency_key("document-retirement", str(run_id))
    lock = nullcontext() if framework_lock_held else FrameworkRunLock(state_directory / "framework.lock")
    try:
        with lock:
            epoch = read_state_epoch(state_directory)
            prepared = record_state_publication(
                state_directory,
                operation="document-retirement",
                owners=("source_cache", "catalog", "semantic", "framework", "dedup"),
                status="partial",
                idempotency_key=publication_id,
                expected_epoch=epoch.epoch,
                detail="Trash owner invalidation prepared",
            )
            summary.replay = prepared.status == "complete"
            framework_connection = getattr(framework_state, "_connection", None)
            if framework_connection is None:
                raise RuntimeError("FrameworkState live connection is required")
            with _framework_transaction(framework_connection):
                sources = _read_retired_sources(
                    framework_state,
                    root=root,
                    run_id=run_id,
                    summary=summary,
                    checkpoint=checkpoint,
                )
                for source_batch in _source_batches(sources):
                    if checkpoint is not None:
                        checkpoint()
                    for _owner, filename, table in _ROUTE_DATABASES:
                        _status, count = _retire_route_database_batch(
                            state_directory / filename,
                            source_batch,
                            table=table,
                            root=root,
                            verify_only=summary.replay,
                        )
                        summary.invalidated_rows += count
                    _status, count = _retire_catalog_batch(
                        state_directory / "document_catalog.sqlite3",
                        source_batch,
                        root=root,
                        verify_only=summary.replay,
                    )
                    summary.invalidated_rows += count
                    _status, count = _retire_semantic_batch(
                        state_directory / "semantic.sqlite3",
                        source_batch,
                        root=root,
                        verify_only=summary.replay,
                    )
                    summary.semantic_items_deactivated += count
                    _status, count = _retire_framework_batch(
                        framework_connection,
                        source_batch,
                        verify_only=summary.replay,
                    )
                    summary.framework_rows_removed += count
                    _status, count = _retire_dedup_batch(
                        state_directory / "dedup.sqlite3",
                        source_batch,
                        verify_only=summary.replay,
                    )
                    summary.dedup_rows_removed += count
                    summary.retired_sources += len(source_batch)
            # Invalid receipts are retained as recovery evidence.  Do not
            # advance the cross-owner publication while any source was
            # rejected; valid sources above remain idempotently retryable.
            if summary.recovery_required:
                return summary.as_dict()
            publication = record_state_publication(
                state_directory,
                operation="document-retirement",
                owners=("source_cache", "catalog", "semantic", "framework", "dedup"),
                status="complete",
                idempotency_key=publication_id,
                expected_epoch=prepared.epoch,
                detail="Trash owner invalidation completed",
            )
            summary.status = "complete"
            summary.publication_epoch = publication.epoch if hasattr(publication, "epoch") else 0
    except (OSError, sqlite3.Error, RuntimeError, ValueError, StatePublicationError) as exc:
        summary.error(f"retirement: {type(exc).__name__}: {exc}")
    return summary.as_dict()


__all__ = ["SCHEMA", "run_document_retirement"]
