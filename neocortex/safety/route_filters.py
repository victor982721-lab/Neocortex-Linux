"""Explicit, bounded selection criteria shared by content routes."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from neocortex.platform_policy import sqlite_path_collation


# region [01] Stable selection contract

_PATH_COLLATION = sqlite_path_collation()


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    """Select route work without changing the meaning of the persistent cache."""

    statuses: tuple[str, ...] = ()
    error_types: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    failed_pages_only: bool = False

    @property
    def active(self) -> bool:
        return bool(
            self.statuses
            or self.error_types
            or self.recommendations
            or self.paths
            or self.failed_pages_only
        )

    @property
    def force_incomplete_retry(self) -> bool:
        """Return whether the explicit filter itself requests incomplete work."""

        return bool(
            self.error_types
            or self.failed_pages_only
            or "retry" in self.recommendations
            or bool({"error", "partial", "protected", "processing"}.intersection(self.statuses))
        )

    @classmethod
    def from_values(
        cls,
        *,
        statuses: Iterable[str] = (),
        error_types: Iterable[str] = (),
        recommendations: Iterable[str] = (),
        paths: Iterable[str | Path] = (),
        failed_pages_only: bool = False,
    ) -> "CandidateSelection":
        return cls(
            statuses=_normalized_values(statuses),
            error_types=_normalized_values(error_types, casefold=False),
            recommendations=_normalized_values(recommendations),
            paths=tuple(dict.fromkeys(str(Path(value).expanduser().absolute()) for value in paths)),
            failed_pages_only=failed_pages_only,
        )


def _normalized_values(
    values: Iterable[str],
    *,
    casefold: bool = True,
) -> tuple[str, ...]:
    normalized = []
    for value in values:
        clean = value.strip()
        if not clean:
            continue
        normalized.append(clean.casefold() if casefold else clean)
    return tuple(dict.fromkeys(normalized))


# endregion [01]


# region [02] Framework candidate SQL


def framework_selection_predicate(
    selection: CandidateSelection,
    *,
    route_name: str,
    candidate_alias: str = "c",
) -> tuple[str, tuple[object, ...]]:
    """Return the path/review portion that can be evaluated in framework state."""

    clauses: list[str] = []
    parameters: list[object] = []
    if selection.paths:
        placeholders = ",".join("?" for _ in selection.paths)
        clauses.append(f"{candidate_alias}.path COLLATE {_PATH_COLLATION} IN ({placeholders})")
        parameters.extend(selection.paths)
    if selection.recommendations:
        placeholders = ",".join("?" for _ in selection.recommendations)
        clauses.append(
            "EXISTS(SELECT 1 FROM review_candidates r "
            f"WHERE r.route_name=? AND r.volume_id={candidate_alias}.volume_id "
            f"AND r.file_id={candidate_alias}.file_id AND r.status='open' "
            f"AND r.recommendation IN ({placeholders}))"
        )
        parameters.append(route_name)
        parameters.extend(selection.recommendations)
    if not clauses:
        return "1=1", ()
    return " AND ".join(clauses), tuple(parameters)


# endregion [02]
