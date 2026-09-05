"""Bounded, sidecar-safe health inspection for published NeoCortex state.

The health command is deliberately a *reader*, not a repair path.  It never
opens a published owner through SQLite's ordinary ``mode=ro`` URI, never
creates a missing owner and never checkpoints or removes sidecars.  A quiescent
owner is opened through :mod:`neocortex.persistence.sqlite_immutable`; an
active, unstable or linked owner is reported without opening it.

Schema validators are resolved lazily.  Importing the command therefore does
not import every format runtime (or an optional parser/model dependency), and
an absent owner does not cause its format module to be imported merely to
report ``missing``.
"""

from __future__ import annotations

import importlib
import math
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    immutable_sqlite_database,
)
from neocortex.persistence.sqlite_cancellation import (
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from neocortex.persistence.sqlite_schema_contract import SQLiteSchemaContractError


STATE_HEALTH_SCHEMA_VERSION = 2

# Keep this exported compatibility tuple stable for callers that only need the
# file names.  The inspection itself obtains the authoritative order, owner
# names and expected versions from STATE_STORE_REGISTRY lazily below.
STATE_OWNER_DATABASES: tuple[tuple[str, str], ...] = (
    ("inventory", "dedup.sqlite3"),
    ("framework", "framework.sqlite3"),
    ("catalog", "document_catalog.sqlite3"),
    ("pdf", "pdf.sqlite3"),
    ("docx", "docx.sqlite3"),
    ("office", "office.sqlite3"),
    ("audio", "audio.sqlite3"),
    ("video", "video.sqlite3"),
    ("image", "image.sqlite3"),
    ("semantic", "semantic.sqlite3"),
    ("code", "code.sqlite3"),
    ("archive", "archive.sqlite3"),
    ("text", "text.sqlite3"),
)

_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_KNOWN_STATUS_TABLES = (
    "initial_runs",
    "scans",
    "catalog_generations",
    "catalog_publications",
)

# Health is intended for an operator-facing status command.  These bounds
# prevent a malformed database from turning a status request into an
# unbounded scan while retaining enough evidence to diagnose the failure.
MAX_TABLES = 4096
MAX_FTS_TABLES = 128
MAX_STATUS_ROWS = 256
MAX_FOREIGN_KEY_ERRORS = 32
MAX_UNKNOWN_DATABASES = 256
MAX_PROCESS_IDS = 256
MAX_PROCESS_FDS = 256
MAX_PROCESS_RESULTS = 16
DEFAULT_INSPECTION_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SQLiteSidecarHealth:
    suffix: str
    size: int

    def to_dict(self) -> dict[str, object]:
        return {"size": self.size, "suffix": self.suffix}


@dataclass(frozen=True, slots=True)
class StateOwnerHealth:
    name: str
    path: str
    status: str
    expected_schema_version: int | None
    schema_version: int | None
    user_version: int | None
    table_count: int
    sidecars: tuple[SQLiteSidecarHealth, ...]
    observations: dict[str, dict[str, int]]
    detail: str | None = None
    # Only bounded process identity is retained.  We intentionally do not
    # expose command lines, environments or open-file paths from /proc.
    processes: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "expected_schema_version": self.expected_schema_version,
            "name": self.name,
            "observations": self.observations,
            "path": self.path,
            "processes": [dict(process) for process in self.processes],
            "schema_version": self.schema_version,
            "sidecars": [item.to_dict() for item in self.sidecars],
            "status": self.status,
            "table_count": self.table_count,
            "user_version": self.user_version,
        }


@dataclass(frozen=True, slots=True)
class StateHealth:
    state_directory: str
    overall: str
    owners: tuple[StateOwnerHealth, ...]
    healthy_count: int
    missing_count: int
    orphaned_sidecar_count: int
    blocked_count: int
    unreadable_count: int
    unknown_count: int = 0
    corrupt_count: int = 0
    active_count: int = 0
    incompatible_count: int = 0
    future_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "active_count": self.active_count,
            "blocked_count": self.blocked_count,
            "corrupt_count": self.corrupt_count,
            "future_count": self.future_count,
            "healthy_count": self.healthy_count,
            "incompatible_count": self.incompatible_count,
            "kind": "state-health",
            "missing_count": self.missing_count,
            "orphaned_sidecar_count": self.orphaned_sidecar_count,
            "overall": self.overall,
            "owners": [owner.to_dict() for owner in self.owners],
            "schema_version": STATE_HEALTH_SCHEMA_VERSION,
            "state_directory": self.state_directory,
            "unreadable_count": self.unreadable_count,
            "unknown_count": self.unknown_count,
        }


class _HealthSchemaError(RuntimeError):
    """The SQLite bytes are readable but do not satisfy the owner contract."""


class _HealthCorruptError(RuntimeError):
    """SQLite reported an integrity or FTS corruption condition."""


class _HealthBudgetError(RuntimeError):
    """The bounded health inspection ran out of its time budget."""


@dataclass(frozen=True, slots=True)
class _OwnerDescriptor:
    name: str
    filename: str
    expected_schema_version: int


@dataclass(frozen=True, slots=True)
class _ValidatorSpec:
    """Import path for one exact current owner validator.

    ``kind=validator`` calls a public/private validator supplied by the owner;
    ``kind=contract`` builds the owner contract and validates it with the
    shared exact SQLite contract checker; ``kind=semantic`` is the one owner
    whose versioned validator takes the expected version explicitly.
    """

    module: str
    symbol: str
    kind: Literal["validator", "contract", "semantic"] = "validator"


# This is a declarative map only.  No module named here is imported until a
# corresponding, present and version-compatible database is actually read.
_VALIDATOR_SPECS: dict[str, _ValidatorSpec] = {
    "inventory": _ValidatorSpec(
        "neocortex.deduplication.persistence.validation", "validate_inventory_schema"
    ),
    "framework": _ValidatorSpec(
        "neocortex.persistence.framework_schema", "validate_framework_schema_v22"
    ),
    "catalog": _ValidatorSpec(
        "neocortex.documents.document_catalog_schema",
        "document_catalog_schema_contract",
        "contract",
    ),
    "pdf": _ValidatorSpec(
        "neocortex.capabilities.formats.pdf.pdf_schema", "validate_pdf_schema"
    ),
    "docx": _ValidatorSpec(
        "neocortex.capabilities.formats.docx.schema", "validate_docx_schema"
    ),
    "office": _ValidatorSpec(
        "neocortex.capabilities.formats.office.state", "_office_schema_contract", "contract"
    ),
    "audio": _ValidatorSpec(
        "neocortex.capabilities.formats.audio.state", "_audio_schema_contract", "contract"
    ),
    "video": _ValidatorSpec(
        "neocortex.capabilities.formats.video.state", "validate_video_schema"
    ),
    "image": _ValidatorSpec(
        "neocortex.capabilities.formats.image.state", "_validate_current_image_schema"
    ),
    "semantic": _ValidatorSpec(
        "neocortex.semantic.semantic_schema", "_validate_version_contract", "semantic"
    ),
    "code": _ValidatorSpec(
        "neocortex.code.code_schema", "validate_code_schema"
    ),
    "archive": _ValidatorSpec(
        "neocortex.capabilities.formats.archive.state", "archive_schema_contract", "contract"
    ),
    "text": _ValidatorSpec(
        "neocortex.capabilities.formats.text.text_state", "text_schema_contract", "contract"
    ),
}


def _state_store_descriptors() -> tuple[_OwnerDescriptor, ...]:
    """Return the canonical owner registry without importing it at module load."""

    # The topology module imports format schema modules and is intentionally
    # not part of the cheap ``--help``/module-import path.
    from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY

    descriptors = tuple(
        _OwnerDescriptor(
            store.state_owner_id,
            store.database_name,
            store.expected_schema_version,
        )
        for store in STATE_STORE_REGISTRY.stores
    )
    if not descriptors:
        raise RuntimeError("state store registry is empty")
    if tuple((item.name, item.filename) for item in descriptors) != STATE_OWNER_DATABASES:
        raise RuntimeError("state store registry does not match state-health owner topology")
    return descriptors


def _sidecars(path: Path) -> tuple[SQLiteSidecarHealth, ...]:
    """Report sidecars with lstat, never following an endpoint symlink."""

    result: list[SQLiteSidecarHealth] = []
    for suffix in _SIDECAR_SUFFIXES:
        candidate = Path(f"{path}{suffix}")
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            # Retain the entry with a zero size.  The separate safety probe
            # will classify it as blocked; no ordinary stat follows it.
            result.append(SQLiteSidecarHealth(suffix, 0))
            continue
        result.append(SQLiteSidecarHealth(suffix, int(value.st_size)))
    return tuple(result)


def _sidecar_safety(path: Path) -> tuple[str | None, str | None]:
    """Return ``(status, detail)`` for a sidecar layout before SQLite opens it."""

    sidecars: dict[str, os.stat_result] = {}
    for suffix in _SIDECAR_SUFFIXES:
        candidate = Path(f"{path}{suffix}")
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return "blocked", f"SQLite sidecar cannot be inspected: {suffix} ({exc})"
        sidecars[suffix] = value
        if stat.S_ISLNK(value.st_mode):
            return "blocked", f"SQLite sidecar is a symlink: {suffix}"
        if not stat.S_ISREG(value.st_mode):
            return "blocked", f"SQLite sidecar is not a regular file: {suffix}"

    journal = sidecars.get("-journal")
    wal = sidecars.get("-wal")
    if journal is not None and journal.st_size > 0:
        return "active", "SQLite owner has a non-empty rollback journal"
    if wal is not None and wal.st_size > 0:
        return "active", "SQLite owner has a non-empty WAL"
    if not sidecars:
        return None, None
    # WAL=0/SHM=32768 also occurs while a live writer holds BEGIN IMMEDIATE.
    # Filesystem sizes cannot prove inactivity, and health must not open that
    # source owner just to discover its locks.
    return "blocked", "SQLite owner sidecars are not proven inactive"


def _regular_owner_kind(path: Path) -> tuple[str | None, str | None]:
    """Check the owner endpoint using lstat before any SQLite operation."""

    try:
        value = path.lstat()
    except FileNotFoundError:
        return "missing", "database is absent"
    except OSError as exc:
        return "unreadable", f"SQLite owner cannot be inspected: {exc}"
    if stat.S_ISLNK(value.st_mode):
        return "blocked", "SQLite owner is a symlink"
    if not stat.S_ISREG(value.st_mode):
        return "blocked", "SQLite owner is not a regular file"
    if value.st_size == 0:
        # A zero-byte SQLite endpoint is not an active writer.  It is an
        # invalid/incomplete owner and should not be reported as healthy merely
        # because immutable preflight refuses to open empty bytes.
        return "incompatible", "SQLite owner is empty"
    return None, None


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        f"ORDER BY name LIMIT {MAX_TABLES + 1}"
    ).fetchall()
    if len(rows) > MAX_TABLES:
        raise _HealthSchemaError(f"database contains more than {MAX_TABLES} application tables")
    return {str(row[0]) for row in rows}


def _canonical_metadata_version(connection: sqlite3.Connection, tables: set[str]) -> int | None:
    """Read one canonical ``metadata.schema_version`` without coercion."""

    if "metadata" not in tables:
        return None
    metadata_type = connection.execute(
        "SELECT type FROM sqlite_schema WHERE name='metadata' LIMIT 1"
    ).fetchone()
    if metadata_type is None or str(metadata_type[0]) != "table":
        raise _HealthSchemaError("metadata object is not a table")
    try:
        rows = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _HealthSchemaError(f"metadata table cannot be read: {exc}") from exc
    if len(rows) != 1:
        return None
    raw = rows[0][0]
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        return None
    if len(raw) > 9 or (raw != "0" and raw.startswith("0")):
        return None
    return int(raw)


def _metadata_version(connection: sqlite3.Connection, tables: set[str]) -> int | None:
    """Compatibility alias retained for focused callers and old tests."""

    return _canonical_metadata_version(connection, tables)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _check_quick_integrity(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
    except sqlite3.Error as exc:
        raise _HealthCorruptError(f"SQLite quick_check failed: {exc}") from exc
    if row is None or str(row[0]).casefold() != "ok":
        detail = "no result" if row is None else str(row[0])
        raise _HealthCorruptError(f"SQLite quick_check reported {detail}")


def _check_foreign_keys(connection: sqlite3.Connection) -> None:
    try:
        cursor = connection.execute("PRAGMA foreign_key_check")
        try:
            for index, row in enumerate(cursor):
                if index >= MAX_FOREIGN_KEY_ERRORS:
                    raise _HealthCorruptError(
                        f"foreign_key_check exceeded {MAX_FOREIGN_KEY_ERRORS} violations"
                    )
                raise _HealthCorruptError(
                    "foreign_key_check violation: "
                    + ":".join("<null>" if item is None else str(item) for item in row)
                )
        finally:
            cursor.close()
    except _HealthCorruptError:
        raise
    except sqlite3.Error as exc:
        raise _HealthCorruptError(f"foreign_key_check failed: {exc}") from exc


def _fts_table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND lower(COALESCE(sql,'')) LIKE '%virtual table%' "
        "AND lower(COALESCE(sql,'')) LIKE '%fts5%' ORDER BY name "
        f"LIMIT {MAX_FTS_TABLES + 1}"
    ).fetchall()
    if len(rows) > MAX_FTS_TABLES:
        raise _HealthSchemaError(f"database contains more than {MAX_FTS_TABLES} FTS tables")
    return tuple(str(row[0]) for row in rows)


def _check_fts(connection: sqlite3.Connection) -> None:
    """Touch each FTS root through a bounded read to detect broken shadows."""

    for name in _fts_table_names(connection):
        try:
            # A LIMIT keeps this a bounded probe and forces SQLite to resolve
            # the virtual table and its shadow set without writing to it.
            connection.execute(f"SELECT rowid FROM {_quote_identifier(name)} LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            raise _HealthCorruptError(f"FTS table {name!r} cannot be read: {exc}") from exc


def _status_observations(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, dict[str, int]]:
    """Collect bounded status facts from known tables only."""

    observations: dict[str, dict[str, int]] = {}
    for table in _KNOWN_STATUS_TABLES:
        if table not in tables:
            continue
        quoted = _quote_identifier(table)
        try:
            if table == "catalog_publications":
                rows = connection.execute(
                    f"SELECT 1 FROM {quoted} LIMIT {MAX_STATUS_ROWS + 1}"
                ).fetchall()
                result: dict[str, int] = {"published": min(len(rows), MAX_STATUS_ROWS)}
            else:
                rows = connection.execute(
                    f"SELECT status FROM {quoted} LIMIT {MAX_STATUS_ROWS + 1}"
                ).fetchall()
                result = {}
                for row in rows[:MAX_STATUS_ROWS]:
                    key = "<null>" if row[0] is None else str(row[0])
                    result[key] = result.get(key, 0) + 1
            if len(rows) > MAX_STATUS_ROWS:
                result["__truncated__"] = 1
            # Preserve the compact legacy shape for empty status tables while
            # still retaining an explicit zero for publication tables.
            if result or table == "catalog_publications":
                observations[table] = result
        except sqlite3.Error as exc:
            # The exact schema validator should already have caught this; a
            # failed diagnostic still means the owner cannot be called healthy.
            raise _HealthSchemaError(f"status table {table!r} cannot be read: {exc}") from exc
    return observations


def _is_corrupt_sqlite_error(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "file is not a database",
            "database disk image is malformed",
            "malformed database schema",
            "file is encrypted",
        )
    )


def _validation_error_status(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, _HealthCorruptError) or _is_corrupt_sqlite_error(exc):
        return "corrupt", str(exc)
    if isinstance(exc, _HealthBudgetError):
        return "blocked", str(exc)
    if isinstance(exc, (SQLiteSchemaContractError, _HealthSchemaError, RuntimeError, ValueError)):
        return "incompatible", str(exc)
    if isinstance(exc, sqlite3.Error):
        # A validator's missing object/column error is a schema mismatch; other
        # operational errors remain unreadable rather than being called corrupt.
        message = str(exc).casefold()
        if any(marker in message for marker in ("no such table", "no such index", "no such column")):
            return "incompatible", str(exc)
        return "unreadable", f"{type(exc).__name__}: {exc}"
    return "unreadable", f"{type(exc).__name__}: {exc}"


def _load_registry_validator(name: str, expected: int) -> Callable[[sqlite3.Connection], None]:
    """Resolve exactly one owner validator on first use."""

    spec = _VALIDATOR_SPECS.get(name)
    if spec is None:
        raise _HealthSchemaError(f"no exact schema validator is registered for owner {name!r}")
    try:
        module = importlib.import_module(spec.module)
        target = getattr(module, spec.symbol)
    except (ImportError, AttributeError) as exc:
        raise _HealthSchemaError(
            f"exact schema validator for owner {name!r} is unavailable: {exc}"
        ) from exc
    if not callable(target):
        raise _HealthSchemaError(f"exact schema validator for owner {name!r} is not callable")

    if spec.kind == "validator":

        def validate(connection: sqlite3.Connection) -> None:
            target(connection)

        return validate

    if spec.kind == "semantic":

        def validate_semantic(connection: sqlite3.Connection) -> None:
            target(connection, expected)

        return validate_semantic

    # Contract builders are deliberately invoked only after import, and each
    # is checked exact=True so an arbitrary database with the right integer
    # metadata cannot be declared healthy.
    from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

    def validate_contract(connection: sqlite3.Connection) -> None:
        contract = target()
        validate_sqlite_schema_contract(
            connection,
            contract,
            label=f"{name} state",
            exact=True,
        )

    return validate_contract


@lru_cache(maxsize=None)
def _exact_validator(name: str, expected: int) -> Callable[[sqlite3.Connection], None]:
    return _load_registry_validator(name, expected)


def _proc_processes(
    path: Path, *, deadline: float | None = None
) -> tuple[dict[str, object], ...]:
    """Return bounded process holders using only read-only /proc operations."""

    if deadline is not None and time.monotonic() >= deadline:
        return ()
    targets = {str(path), *(f"{path}{suffix}" for suffix in _SIDECAR_SUFFIXES)}
    try:
        entries = sorted(
            (item for item in Path("/proc").iterdir() if item.name.isdigit()),
            key=lambda item: int(item.name),
        )[:MAX_PROCESS_IDS]
    except OSError:
        return ()
    found: list[dict[str, object]] = []
    for process in entries:
        if deadline is not None and time.monotonic() >= deadline:
            break
        fd_directory = process / "fd"
        try:
            descriptors = sorted(
                fd_directory.iterdir(), key=lambda item: item.name
            )[:MAX_PROCESS_FDS]
        except OSError:
            continue
        matching = 0
        for descriptor in descriptors:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            target = target.removesuffix(" (deleted)")
            if target in targets:
                matching += 1
        if not matching:
            continue
        try:
            comm = (process / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            comm = "unknown"
        found.append({"comm": comm[:128], "fd_count": matching, "pid": int(process.name)})
        if len(found) >= MAX_PROCESS_RESULTS:
            break
    return tuple(found)


def _check_health_budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _HealthBudgetError("state-health inspection time budget exhausted")


@contextmanager
def _health_read(
    path: Path, *, deadline: float
) -> Iterator[tuple[sqlite3.Connection, SQLiteCancellationBridge]]:
    """Apply the shared cooperative budget to all SQL, including validators."""

    _check_health_budget(deadline)
    remaining = max(0.001, deadline - time.monotonic())
    with immutable_sqlite_database(path, timeout_seconds=remaining) as connection:
        bridge = SQLiteCancellationBridge(lambda: _check_health_budget(deadline))
        with sqlite_cancellation_scope(connection, bridge):
            bridge.checkpoint()
            try:
                yield connection, bridge
            except Exception:
                bridge.checkpoint()
                raise
            bridge.checkpoint()
    _check_health_budget(deadline)


def _owner_record(
    descriptor: _OwnerDescriptor,
    path: Path,
    sidecars: tuple[SQLiteSidecarHealth, ...],
    *,
    deadline: float,
) -> StateOwnerHealth:
    expected = descriptor.expected_schema_version
    status, detail = _regular_owner_kind(path)
    if status is not None:
        return StateOwnerHealth(
            name=descriptor.name,
            path=str(path),
            status=status,
            expected_schema_version=expected,
            schema_version=None,
            user_version=None,
            table_count=0,
            sidecars=sidecars,
            observations={},
            detail=detail,
            processes=_proc_processes(path, deadline=deadline) if status in {"blocked", "active"} else (),
        )

    sidecar_status, sidecar_detail = _sidecar_safety(path)
    if sidecar_status is not None:
        return StateOwnerHealth(
            name=descriptor.name,
            path=str(path),
            status=sidecar_status,
            expected_schema_version=expected,
            schema_version=None,
            user_version=None,
            table_count=0,
            sidecars=sidecars,
            observations={},
            detail=sidecar_detail,
            processes=_proc_processes(path, deadline=deadline),
        )

    tables: set[str] = set()
    schema_version: int | None = None
    user_version: int | None = None
    try:
        with _health_read(path, deadline=deadline) as (connection, budget):
            tables = _table_names(connection)
            budget.checkpoint()
            schema_version = _canonical_metadata_version(connection, tables)
            user_row = connection.execute("PRAGMA user_version").fetchone()
            if user_row is None:
                raise _HealthSchemaError("PRAGMA user_version returned no value")
            user_version = int(user_row[0])
            budget.checkpoint()
            if schema_version is None:
                raise _HealthSchemaError("schema metadata is absent or invalid")
            if schema_version > expected:
                return StateOwnerHealth(
                    name=descriptor.name,
                    path=str(path),
                    status="future",
                    expected_schema_version=expected,
                    schema_version=schema_version,
                    user_version=user_version,
                    table_count=len(tables),
                    sidecars=sidecars,
                    observations={},
                    detail=f"schema version {schema_version} is newer than {expected}",
                )
            if schema_version != expected:
                raise _HealthSchemaError(
                    f"schema version {schema_version} does not match {expected}"
                )
            if user_version not in {0, expected}:
                raise _HealthSchemaError(
                    f"PRAGMA user_version {user_version} does not match {expected}"
                )
            _check_quick_integrity(connection)
            budget.checkpoint()
            _check_foreign_keys(connection)
            budget.checkpoint()
            _check_fts(connection)
            budget.checkpoint()
            validator = _exact_validator(descriptor.name, expected)
            budget.checkpoint()
            validator(connection)
            budget.checkpoint()
            observations = _status_observations(connection, tables)
        return StateOwnerHealth(
            name=descriptor.name,
            path=str(path),
            status="healthy",
            expected_schema_version=expected,
            schema_version=schema_version,
            user_version=user_version,
            table_count=len(tables),
            sidecars=sidecars,
            observations=observations,
        )
    except ImmutableSQLiteUnavailable as exc:
        # A sidecar may have appeared or changed after the preflight; this is
        # an observation race, not evidence that the owner is corrupt.
        return StateOwnerHealth(
            name=descriptor.name,
            path=str(path),
            status="blocked",
            expected_schema_version=expected,
            schema_version=schema_version,
            user_version=user_version,
            table_count=len(tables),
            sidecars=_sidecars(path),
            observations={},
            detail=str(exc),
            processes=_proc_processes(path, deadline=deadline),
        )
    except Exception as exc:
        status, detail = _validation_error_status(exc)
        return StateOwnerHealth(
            name=descriptor.name,
            path=str(path),
            status=status,
            expected_schema_version=expected,
            schema_version=schema_version,
            user_version=user_version,
            table_count=len(tables),
            sidecars=_sidecars(path),
            observations={},
            detail=detail,
            processes=_proc_processes(path, deadline=deadline) if status in {"blocked", "active"} else (),
        )


def _unknown_state_entries(state: Path) -> tuple[Path, ...]:
    """Find unregistered owners, including sidecars with no main, without links."""

    known = {filename for _, filename in STATE_OWNER_DATABASES}
    entries: list[Path] = []
    seen: set[str] = set()
    try:
        children = sorted(state.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()
    for candidate in children:
        name = candidate.name
        for suffix in _SIDECAR_SUFFIXES:
            if name.endswith(f".sqlite3{suffix}"):
                name = name.removesuffix(suffix)
                break
        if name in known or name in seen or not name.endswith(".sqlite3"):
            continue
        try:
            value = candidate.lstat()
        except OSError:
            value = None
        if (
            name != candidate.name
            or value is None
            or stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
        ):
            entries.append(state / name)
            seen.add(name)
        if len(entries) >= MAX_UNKNOWN_DATABASES:
            break
    return tuple(entries)


def _unknown_record(
    path: Path,
    sidecars: tuple[SQLiteSidecarHealth, ...],
    *,
    deadline: float,
) -> StateOwnerHealth:
    status, detail = _regular_owner_kind(path)
    if status == "missing":
        if sidecars:
            status, detail = "orphaned_sidecars", "database is absent but sidecars remain"
        else:
            status, detail = "blocked", "unknown database disappeared during inspection"
    if status is not None:
        return StateOwnerHealth(
            name=f"unknown:{path.name}",
            path=str(path),
            status=status,
            expected_schema_version=None,
            schema_version=None,
            user_version=None,
            table_count=0,
            sidecars=sidecars,
            observations={},
            detail=detail,
            processes=_proc_processes(path, deadline=deadline) if status in {"blocked", "active"} else (),
        )
    sidecar_status, sidecar_detail = _sidecar_safety(path)
    if sidecar_status is not None:
        return StateOwnerHealth(
            name=f"unknown:{path.name}",
            path=str(path),
            status=sidecar_status,
            expected_schema_version=None,
            schema_version=None,
            user_version=None,
            table_count=0,
            sidecars=sidecars,
            observations={},
            detail=sidecar_detail,
            processes=_proc_processes(path, deadline=deadline),
        )
    tables: set[str] = set()
    schema_version: int | None = None
    user_version: int | None = None
    try:
        with _health_read(path, deadline=deadline) as (connection, budget):
            tables = _table_names(connection)
            budget.checkpoint()
            schema_version = _canonical_metadata_version(connection, tables)
            row = connection.execute("PRAGMA user_version").fetchone()
            user_version = None if row is None else int(row[0])
            budget.checkpoint()
            _check_quick_integrity(connection)
            budget.checkpoint()
            _check_foreign_keys(connection)
            budget.checkpoint()
            _check_fts(connection)
            budget.checkpoint()
            observations = _status_observations(connection, tables)
        return StateOwnerHealth(
            name=f"unknown:{path.name}",
            path=str(path),
            status="unknown",
            expected_schema_version=None,
            schema_version=schema_version,
            user_version=user_version,
            table_count=len(tables),
            sidecars=sidecars,
            observations=observations,
            detail="database is not registered in the state topology",
        )
    except ImmutableSQLiteUnavailable as exc:
        status = "blocked"
        detail = str(exc)
    except Exception as exc:
        status, detail = _validation_error_status(exc)
    return StateOwnerHealth(
        name=f"unknown:{path.name}",
        path=str(path),
        status=status,
        expected_schema_version=None,
        schema_version=schema_version,
        user_version=user_version,
        table_count=len(tables),
        sidecars=_sidecars(path),
        observations={},
        detail=detail,
        processes=_proc_processes(path, deadline=deadline) if status in {"blocked", "active"} else (),
    )


def inspect_state_health(
    state_directory: Path,
    *,
    timeout_seconds: float = DEFAULT_INSPECTION_TIMEOUT_SECONDS,
) -> StateHealth:
    """Inspect every registered owner without creating or mutating state.

    ``timeout_seconds`` is a shared cooperative budget: SQLite statements are
    interrupted by a progress handler and stages check the deadline before
    accepting results.  Registered owners not reached in time are ``blocked``.
    This is not a hard wall-clock limit for Python validators, imports or
    blocked filesystem calls; overruns cannot be reported as healthy.
    """

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("state-health timeout must be a positive number")
    timeout = float(timeout_seconds)
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("state-health timeout must be a positive finite number")
    state = Path(state_directory).expanduser().resolve(strict=False)
    deadline = time.monotonic() + timeout
    descriptors = _state_store_descriptors()
    owners: list[StateOwnerHealth] = []
    for descriptor in descriptors:
        path = state / descriptor.filename
        sidecars = _sidecars(path)
        if time.monotonic() >= deadline:
            owners.append(
                StateOwnerHealth(
                    name=descriptor.name,
                    path=str(path),
                    status="blocked",
                    expected_schema_version=descriptor.expected_schema_version,
                    schema_version=None,
                    user_version=None,
                    table_count=0,
                    sidecars=sidecars,
                    observations={},
                    detail="state-health inspection time budget exhausted",
                )
            )
            continue
        # Avoid opening absent owners at all, including their schema modules.
        kind, detail = _regular_owner_kind(path)
        if kind == "missing":
            # There is no owner to open, so every adjacent sidecar is orphaned
            # regardless of its layout.  lstat has already established its
            # existence and no bytes are followed or modified here.
            status = "orphaned_sidecars" if sidecars else None
            sidecar_detail = None
            owners.append(
                StateOwnerHealth(
                    name=descriptor.name,
                    path=str(path),
                    status=status or "missing",
                    expected_schema_version=descriptor.expected_schema_version,
                    schema_version=None,
                    user_version=None,
                    table_count=0,
                    sidecars=sidecars,
                    observations={},
                    detail=(
                        "database is absent but sidecars remain"
                        if status == "orphaned_sidecars"
                        else sidecar_detail or detail or "database is absent"
                    ),
                    processes=_proc_processes(path, deadline=deadline) if status in {"blocked", "active"} else (),
                )
            )
            continue
        owners.append(_owner_record(descriptor, path, sidecars, deadline=deadline))

    for path in _unknown_state_entries(state):
        sidecars = _sidecars(path)
        if time.monotonic() >= deadline:
            owners.append(
                StateOwnerHealth(
                    name=f"unknown:{path.name}",
                    path=str(path),
                    status="blocked",
                    expected_schema_version=None,
                    schema_version=None,
                    user_version=None,
                    table_count=0,
                    sidecars=sidecars,
                    observations={},
                    detail="state-health inspection time budget exhausted",
                )
            )
            continue
        owners.append(_unknown_record(path, sidecars, deadline=deadline))

    def count(status: str) -> int:
        return sum(owner.status == status for owner in owners)

    healthy = count("healthy")
    missing = count("missing")
    orphaned = count("orphaned_sidecars")
    blocked = count("blocked")
    unreadable = count("unreadable")
    unknown = count("unknown")
    corrupt = count("corrupt")
    active = count("active")
    incompatible = count("incompatible")
    future = count("future")
    overall = "healthy" if owners and healthy == len(owners) else "partial"
    return StateHealth(
        state_directory=str(state),
        overall=overall,
        owners=tuple(owners),
        healthy_count=healthy,
        missing_count=missing,
        orphaned_sidecar_count=orphaned,
        blocked_count=blocked,
        unreadable_count=unreadable,
        unknown_count=unknown,
        corrupt_count=corrupt,
        active_count=active,
        incompatible_count=incompatible,
        future_count=future,
    )


__all__ = [
    "DEFAULT_INSPECTION_TIMEOUT_SECONDS",
    "STATE_HEALTH_SCHEMA_VERSION",
    "STATE_OWNER_DATABASES",
    "SQLiteSidecarHealth",
    "StateHealth",
    "StateOwnerHealth",
    "inspect_state_health",
]
