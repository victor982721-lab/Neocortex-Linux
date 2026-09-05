"""Read-only Framework lifecycle queries for API and SDK consumers."""

from __future__ import annotations

from pathlib import Path

from neocortex.runtime.orchestration.run_status import (
    RunStatus,
    list_run_status,
    serialized_run_status,
)


def read_run_status(
    database_path: str | Path,
    *,
    limit: int = 5,
    run_id: int | None = None,
    stale_after_seconds: float = 300.0,
) -> tuple[RunStatus, ...]:
    """Read bounded lifecycle status without starting or mutating a run."""

    return list_run_status(
        Path(database_path),
        limit=limit,
        run_id=run_id,
        stale_after_seconds=stale_after_seconds,
    )


def read_run_status_json(
    database_path: str | Path,
    *,
    limit: int = 5,
    run_id: int | None = None,
    stale_after_seconds: float = 300.0,
) -> tuple[str, ...]:
    """Return the same bounded lifecycle status as stable JSON documents."""

    return tuple(
        serialized_run_status(status)
        for status in read_run_status(
            database_path,
            limit=limit,
            run_id=run_id,
            stale_after_seconds=stale_after_seconds,
        )
    )


__all__ = ["read_run_status", "read_run_status_json"]
