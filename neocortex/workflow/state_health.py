"""Sidecar-safe read-only health inspection for published NeoCortex state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    immutable_sqlite_database,
)

STATE_HEALTH_SCHEMA_VERSION = 1
STATE_OWNER_DATABASES: tuple[tuple[str, str], ...] = (
    ("framework", "framework.sqlite3"),
    ("inventory", "dedup.sqlite3"),
    ("catalog", "document_catalog.sqlite3"),
    ("semantic", "semantic.sqlite3"),
    ("text", "text.sqlite3"),
    ("pdf", "pdf.sqlite3"),
    ("docx", "docx.sqlite3"),
    ("office", "office.sqlite3"),
    ("archive", "archive.sqlite3"),
    ("audio", "audio.sqlite3"),
    ("video", "video.sqlite3"),
    ("image", "image.sqlite3"),
    ("code", "code.sqlite3"),
)
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


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
    schema_version: int | None
    user_version: int | None
    table_count: int
    sidecars: tuple[SQLiteSidecarHealth, ...]
    observations: dict[str, dict[str, int]]
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "name": self.name,
            "observations": self.observations,
            "path": self.path,
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

    def to_dict(self) -> dict[str, object]:
        return {
            "blocked_count": self.blocked_count,
            "healthy_count": self.healthy_count,
            "kind": "state-health",
            "missing_count": self.missing_count,
            "orphaned_sidecar_count": self.orphaned_sidecar_count,
            "overall": self.overall,
            "owners": [owner.to_dict() for owner in self.owners],
            "schema_version": STATE_HEALTH_SCHEMA_VERSION,
            "state_directory": self.state_directory,
            "unreadable_count": self.unreadable_count,
        }


def _sidecars(path: Path) -> tuple[SQLiteSidecarHealth, ...]:
    result: list[SQLiteSidecarHealth] = []
    for suffix in _SIDECAR_SUFFIXES:
        candidate = Path(f"{path}{suffix}")
        try:
            size = candidate.stat().st_size
        except FileNotFoundError:
            continue
        result.append(SQLiteSidecarHealth(suffix, int(size)))
    return tuple(result)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _metadata_version(connection: sqlite3.Connection, tables: set[str]) -> int | None:
    if "metadata" not in tables:
        return None
    rows = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        return None
    try:
        return int(str(rows[0][0]))
    except (TypeError, ValueError):
        return None


def _status_observations(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, dict[str, int]]:
    observations: dict[str, dict[str, int]] = {}
    for table in ("initial_runs", "scans", "catalog_generations", "catalog_publications"):
        if table not in tables:
            continue
        if table == "catalog_publications":
            count = int(
                connection.execute("SELECT COUNT(*) FROM catalog_publications").fetchone()[0]
            )
            observations[table] = {"published": count}
            continue
        rows = connection.execute(
            f'SELECT status,COUNT(*) FROM "{table}" '
            "GROUP BY status ORDER BY status"
        ).fetchall()
        observations[table] = {str(row[0]): int(row[1]) for row in rows}
    return observations


def _healthy_owner(
    name: str,
    path: Path,
    sidecars: tuple[SQLiteSidecarHealth, ...],
) -> StateOwnerHealth:
    with immutable_sqlite_database(path) as connection:
        tables = _table_names(connection)
        return StateOwnerHealth(
            name=name,
            path=str(path),
            status="healthy",
            schema_version=_metadata_version(connection, tables),
            user_version=int(connection.execute("PRAGMA user_version").fetchone()[0]),
            table_count=len(tables),
            sidecars=sidecars,
            observations=_status_observations(connection, tables),
        )


def inspect_state_health(state_directory: Path) -> StateHealth:
    """Inspect every known owner without creating, migrating, or checkpointing it."""

    state = Path(state_directory).expanduser().resolve(strict=False)
    owners: list[StateOwnerHealth] = []
    for name, filename in STATE_OWNER_DATABASES:
        path = state / filename
        sidecars = _sidecars(path)
        if not path.is_file():
            status = "orphaned_sidecars" if sidecars else "missing"
            owners.append(
                StateOwnerHealth(
                    name,
                    str(path),
                    status,
                    None,
                    None,
                    0,
                    sidecars,
                    {},
                    "database is absent but sidecars remain"
                    if status == "orphaned_sidecars"
                    else "database is absent",
                )
            )
            continue
        try:
            owners.append(_healthy_owner(name, path, sidecars))
        except ImmutableSQLiteUnavailable as exc:
            owners.append(
                StateOwnerHealth(
                    name,
                    str(path),
                    "blocked",
                    None,
                    None,
                    0,
                    sidecars,
                    {},
                    str(exc),
                )
            )
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            owners.append(
                StateOwnerHealth(
                    name,
                    str(path),
                    "unreadable",
                    None,
                    None,
                    0,
                    sidecars,
                    {},
                    f"{type(exc).__name__}: {exc}",
                )
            )

    healthy = sum(owner.status == "healthy" for owner in owners)
    missing = sum(owner.status == "missing" for owner in owners)
    orphaned = sum(owner.status == "orphaned_sidecars" for owner in owners)
    blocked = sum(owner.status == "blocked" for owner in owners)
    unreadable = sum(owner.status == "unreadable" for owner in owners)
    overall = "healthy" if healthy == len(owners) else "partial"
    return StateHealth(
        state_directory=str(state),
        overall=overall,
        owners=tuple(owners),
        healthy_count=healthy,
        missing_count=missing,
        orphaned_sidecar_count=orphaned,
        blocked_count=blocked,
        unreadable_count=unreadable,
    )


__all__ = [
    "STATE_HEALTH_SCHEMA_VERSION",
    "STATE_OWNER_DATABASES",
    "SQLiteSidecarHealth",
    "StateHealth",
    "StateOwnerHealth",
    "inspect_state_health",
]
