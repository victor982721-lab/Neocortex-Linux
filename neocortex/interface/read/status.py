"""Bounded read-only projections of durable framework run state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from _04_Nucleo_Operativo.framework_connection import connect_existing_framework
from _04_Nucleo_Operativo.run_status import list_run_status

from .issues import route_issue_count


# region [01] Projection schemas


@dataclass(frozen=True, slots=True)
class RunStatus:
    run_id: int
    root: str
    status: str
    phase: str
    run_kind: str
    started_ns: int
    completed_ns: int | None
    files_checked: int
    action_errors: int
    route_errors: int

    @property
    def duration_seconds(self) -> float:
        end = self.completed_ns or int(datetime.now().timestamp() * 1_000_000_000)
        return max(0.0, (end - self.started_ns) / 1_000_000_000)


# endregion [01]


# region [02] Repository


class StatusRepositoryError(RuntimeError):
    """Durable state exists but cannot be projected safely."""


class StatusRepository:
    """Open short-lived query-only connections so polling never owns the database."""

    def __init__(self, state_directory: Path):
        self.state_directory = Path(state_directory)

    @property
    def database_path(self) -> Path:
        return self.state_directory / "framework.sqlite3"

    def recent_runs(self, limit: int = 20) -> tuple[RunStatus, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("run history limit must be between 1 and 100")
        path = self.database_path
        if not path.is_file():
            return ()
        try:
            operational = list_run_status(path, limit=limit)
            details = self._run_details(path, tuple(run.run_id for run in operational))
        except (
            OSError,
            sqlite3.Error,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as exc:
            raise StatusRepositoryError(f"No se pudo leer el estado durable {path}: {exc}") from exc
        return tuple(
            RunStatus(
                run_id=run.run_id,
                root=run.root,
                status=run.status,
                phase=run.current_phase or run.status,
                run_kind=run.run_kind,
                started_ns=run.started_ns,
                completed_ns=run.completed_ns,
                files_checked=details.get(run.run_id, (0, 0, 0))[0],
                action_errors=details.get(run.run_id, (0, 0, 0))[1],
                route_errors=details.get(run.run_id, (0, 0, 0))[2],
            )
            for run in operational
        )

    @staticmethod
    def _run_details(
        path: Path,
        run_ids: tuple[int, ...],
    ) -> dict[int, tuple[int, int, int]]:
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _run_id in run_ids)
        connection = connect_existing_framework(path, readonly=True, timeout_seconds=1.0)
        try:
            details: dict[int, list[int]] = {run_id: [0, 0, 0] for run_id in run_ids}
            for row in connection.execute(
                f"""SELECT run_id,files_checked,errors FROM run_actions
                WHERE run_id IN ({placeholders})""",
                run_ids,
            ):
                values = details[int(row["run_id"])]
                values[0] = int(row["files_checked"])
                values[1] = int(row["errors"])
            for row in connection.execute(
                f"""SELECT run_id,route_name,status,error_type,summary_json
                FROM route_runs WHERE run_id IN ({placeholders})""",
                run_ids,
            ):
                summary = _decoded_route_summary(row)
                values = details[int(row["run_id"])]
                values[2] += route_issue_count(
                    summary,
                    failed=(str(row["status"]) == "failed" or row["error_type"] is not None),
                )
            return {run_id: (values[0], values[1], values[2]) for run_id, values in details.items()}
        finally:
            connection.close()

    def latest_event_details(self, run_id: int, phase: str) -> dict[str, Any]:
        path = self.database_path
        if not path.is_file():
            return {}
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_existing_framework(path, readonly=True, timeout_seconds=1.0)
            row = connection.execute(
                """SELECT details_json FROM run_events
                   WHERE run_id=? AND phase=? AND details_json IS NOT NULL
                   ORDER BY event_id DESC LIMIT 1""",
                (run_id, phase),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StatusRepositoryError(
                f"No se pudo leer el evento durable {run_id}/{phase}: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        if row is None:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise StatusRepositoryError(
                f"El evento durable {run_id}/{phase} contiene JSON no válido"
            ) from exc
        if not isinstance(value, dict):
            raise StatusRepositoryError(
                f"El evento durable {run_id}/{phase} no contiene un objeto JSON"
            )
        return value


def _decoded_route_summary(row: sqlite3.Row) -> dict[str, Any] | None:
    raw = row["summary_json"]
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"route summary {row['run_id']}/{row['route_name']} is invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"route summary {row['run_id']}/{row['route_name']} is not an object")
    return value


# endregion [02]
