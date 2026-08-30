"""Conceptual project discovery and provenance-preserving reconstruction."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .code_contracts import ReconstructionEntry, ReconstructionManifest
from .code_schema import readonly_code_database


# region [01] Read models and project discovery


RECONSTRUCTION_STRATEGIES = frozenset({"latest", "coherent", "branches"})


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: int
    name: str
    ecosystem: str
    probable_root: str | None
    confidence: float
    status: str
    current_files: int
    historical_files: int


@dataclass(frozen=True, slots=True)
class _Variant:
    project_id: int
    project_name: str
    ecosystem: str
    proposed_path: str
    source_path: str
    version_id: int
    raw_xxh3_128: str | None
    text_xxh3_128: str | None
    relation: str
    confidence: float
    conflict_group: str | None
    valid_from_ns: int
    invalidated_ns: int | None
    analysis_status: str
    manifest_kind: str | None

    @classmethod
    def from_sql(cls, row: sqlite3.Row) -> "_Variant":
        return cls(
            project_id=int(row[0]),
            project_name=str(row[1]),
            ecosystem=str(row[2]),
            proposed_path=_safe_relative(str(row[3])),
            source_path=str(row[4]),
            version_id=int(row[5]),
            raw_xxh3_128=None if row[6] is None else str(row[6]),
            text_xxh3_128=None if row[7] is None else str(row[7]),
            relation=str(row[8]),
            confidence=float(row[9]),
            conflict_group=None if row[10] is None else str(row[10]),
            valid_from_ns=int(row[11]),
            invalidated_ns=None if row[12] is None else int(row[12]),
            analysis_status=str(row[13]),
            manifest_kind=None if row[14] is None else str(row[14]),
        )


def _safe_relative(value: str) -> str:
    """Normalize a proposal without ever resolving or touching the filesystem."""

    normalized = value.replace("\\", "/").lstrip("/")
    parts = tuple(
        part for part in PurePosixPath(normalized).parts if part not in {"", "."}
    )
    if not parts or any(part == ".." or ":" in part for part in parts):
        return PurePosixPath(Path(value).name).as_posix()
    return PurePosixPath(*parts).as_posix()


def list_projects(
    path: Path, *, include_historical: bool = False
) -> tuple[ProjectSummary, ...]:
    """List probable project instances with current and historical coverage."""

    with readonly_code_database(path) as connection:
        predicate = "1=1" if include_historical else "p.status<>'historical'"
        rows = connection.execute(
            f"""SELECT p.project_id,p.name,p.ecosystem,p.probable_root,p.confidence,
            p.status,SUM(CASE WHEN v.invalidated_ns IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN v.invalidated_ns IS NOT NULL THEN 1 ELSE 0 END)
            FROM projects p LEFT JOIN project_memberships m ON m.project_id=p.project_id
            LEFT JOIN file_versions v ON v.version_id=m.version_id
            WHERE {predicate}
            GROUP BY p.project_id ORDER BY p.name COLLATE NOCASE,p.ecosystem,p.project_id"""
        ).fetchall()
        return tuple(
            ProjectSummary(
                project_id=int(row[0]),
                name=str(row[1]),
                ecosystem=str(row[2]),
                probable_root=None if row[3] is None else str(row[3]),
                confidence=float(row[4]),
                status=str(row[5]),
                current_files=int(row[6] or 0),
                historical_files=int(row[7] or 0),
            )
            for row in rows
        )


def resolve_project_id(path: Path, value: str | int) -> int:
    """Resolve an exact id or an unambiguous case-insensitive project name."""

    if isinstance(value, int) or str(value).isdigit():
        return int(value)
    with readonly_code_database(path) as connection:
        rows = connection.execute(
            """SELECT project_id FROM projects WHERE name=? COLLATE NOCASE
            AND status<>'historical' ORDER BY project_id""",
            (str(value),),
        ).fetchall()
    if not rows:
        raise LookupError(f"code project not found: {value}")
    if len(rows) > 1:
        identifiers = ", ".join(str(int(row[0])) for row in rows[:20])
        raise LookupError(
            f"project name is ambiguous; select an explicit id ({identifiers})"
        )
    return int(rows[0][0])


# endregion [01]


# region [02] Explicit reconstruction strategies


def _load_variants(
    connection: sqlite3.Connection, project_id: int
) -> tuple[_Variant, ...]:
    rows = connection.execute(
        """SELECT p.project_id,p.name,p.ecosystem,m.proposed_path,v.path_observed,
        v.version_id,v.raw_xxh3_128,v.text_xxh3_128,m.relation,m.confidence,
        m.conflict_group,v.valid_from_ns,v.invalidated_ns,v.analysis_status,
        p.manifest_kind FROM projects p
        JOIN project_memberships m ON m.project_id=p.project_id
        JOIN file_versions v ON v.version_id=m.version_id
        WHERE p.project_id=?
        ORDER BY m.proposed_path COLLATE NOCASE,v.valid_from_ns,v.version_id""",
        (project_id,),
    ).fetchall()
    if not rows:
        raise LookupError(f"code project has no indexed variants: {project_id}")
    return tuple(_Variant.from_sql(row) for row in rows)


def _coherence_key(variant: _Variant) -> tuple[object, ...]:
    relation_score = {
        "manifest": 4,
        "under_manifest_root": 3,
        "inferred_root": 1,
    }.get(variant.relation, 0)
    status_score = {
        "complete": 3,
        "text_only": 2,
        "partial": 1,
    }.get(variant.analysis_status, 0)
    return (
        variant.invalidated_ns is None,
        relation_score,
        status_score,
        variant.confidence,
        variant.valid_from_ns,
        variant.version_id,
    )


def _selected_versions(
    groups: dict[str, list[_Variant]], strategy: str
) -> tuple[set[int], tuple[str, ...]]:
    selected: set[int] = set()
    conflicts: list[str] = []
    for proposed_path, variants in sorted(groups.items()):
        distinct = {
            item.raw_xxh3_128 or item.text_xxh3_128 or f"version:{item.version_id}"
            for item in variants
        }
        if len(distinct) > 1:
            conflicts.append(
                f"{proposed_path}: {len(variants)} variants, {len(distinct)} contents"
            )
        if strategy == "branches":
            continue
        if strategy == "latest":
            chosen = max(
                variants,
                key=lambda item: (item.valid_from_ns, item.version_id),
            )
        else:
            chosen = max(variants, key=_coherence_key)
        selected.add(chosen.version_id)
    return selected, tuple(conflicts)


def reconstruct_project(
    path: Path,
    project: str | int,
    *,
    strategy: str = "coherent",
) -> ReconstructionManifest:
    """Build a conceptual manifest; never creates, moves or overwrites files."""

    strategy = strategy.casefold()
    if strategy not in RECONSTRUCTION_STRATEGIES:
        raise ValueError("reconstruction strategy must be latest, coherent or branches")
    project_id = resolve_project_id(path, project)
    with readonly_code_database(path) as connection:
        variants = _load_variants(connection, project_id)

    groups: defaultdict[str, list[_Variant]] = defaultdict(list)
    for variant in variants:
        groups[os.path.normcase(variant.proposed_path)].append(variant)
    selected, conflicts = _selected_versions(dict(groups), strategy)
    conflict_by_path = {
        key: f"project-{project_id}-path-{index}"
        for index, (key, values) in enumerate(sorted(groups.items()), start=1)
        if len(
            {
                item.raw_xxh3_128 or item.text_xxh3_128 or f"version:{item.version_id}"
                for item in values
            }
        )
        > 1
    }
    entries = tuple(
        ReconstructionEntry(
            proposed_path=variant.proposed_path,
            source_path=variant.source_path,
            version_id=variant.version_id,
            xxh3_128=(
                variant.raw_xxh3_128
                or variant.text_xxh3_128
                or f"unavailable:version:{variant.version_id}"
            ),
            relation=variant.relation,
            confidence=variant.confidence,
            selected=variant.version_id in selected,
            conflict_group=conflict_by_path.get(
                os.path.normcase(variant.proposed_path)
            ),
        )
        for variant in variants
    )
    manifest_kind = next(
        (item.manifest_kind for item in variants if item.manifest_kind), None
    )
    evidence = (
        f"strategy:{strategy}",
        f"indexed-variants:{len(variants)}",
        f"manifest:{manifest_kind or 'not-observed'}",
        "conceptual-only:no-filesystem-mutation",
    )
    first = variants[0]
    return ReconstructionManifest(
        project_id=project_id,
        project_name=first.project_name,
        ecosystem=first.ecosystem,
        strategy=strategy,
        entries=entries,
        conflicts=conflicts,
        evidence=evidence,
    )


# endregion [02]


__all__ = [  # noqa: RUF022
    "ProjectSummary",
    "RECONSTRUCTION_STRATEGIES",
    "list_projects",
    "reconstruct_project",
    "resolve_project_id",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_projects")
