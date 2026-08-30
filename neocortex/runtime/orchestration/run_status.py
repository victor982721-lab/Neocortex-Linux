"""Read-only operational status queries for framework executions."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.runtime.orchestration.run_lifecycle import (
    DEFAULT_STALE_HEARTBEAT_SECONDS,
    process_is_alive,
)

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module


# region [01] Status models


@dataclass(frozen=True, slots=True)
class PhaseStatus:
    route_name: str
    phase_name: str
    status: str
    started_ns: int
    completed_ns: int | None
    error_type: str | None


@dataclass(frozen=True, slots=True)
class RouteStatus:
    route_name: str
    status: str
    current_phase: str | None
    started_ns: int
    completed_ns: int | None
    heartbeat_ns: int | None
    error_type: str | None
    phases: tuple[PhaseStatus, ...]


@dataclass(frozen=True, slots=True)
class RunStatus:
    run_id: int
    run_kind: str
    status: str
    root: str
    source_run_id: int | None
    current_phase: str | None
    owner_pid: int | None
    owner_alive: bool | None
    heartbeat_ns: int | None
    heartbeat_stale: bool | None
    started_ns: int
    completed_ns: int | None
    routes: tuple[RouteStatus, ...]
    recovery_required_actions: int = 0


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.run_status")


# endregion [01]


# region [02] Read-only query


def list_run_status(
    database_path: Path,
    *,
    limit: int = 5,
    run_id: int | None = None,
    stale_after_seconds: float = DEFAULT_STALE_HEARTBEAT_SECONDS,
) -> tuple[RunStatus, ...]:
    """Return bounded status without creating or migrating persistent state."""

    if limit < 1 or limit > 1000:
        raise ValueError("status limit must be between 1 and 1000")
    if stale_after_seconds <= 0:
        raise ValueError("stale heartbeat threshold must be positive")
    connection = connect_existing_framework(
        database_path, readonly=True, timeout_seconds=10
    )
    try:
        run_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(initial_runs)")
        }
        if not run_columns:
            raise sqlite3.DatabaseError("framework state has no initial_runs table")
        optional = {
            name: name if name in run_columns else f"NULL AS {name}"
            for name in (
                "run_kind",
                "source_run_id",
                "current_phase",
                "owner_pid",
                "heartbeat_ns",
            )
        }
        where = "" if run_id is None else "WHERE run_id=?"
        parameters: tuple[object, ...] = () if run_id is None else (run_id,)
        rows = connection.execute(
            f"""SELECT run_id,root,started_ns,completed_ns,status,
            {optional["run_kind"]},{optional["source_run_id"]},
            {optional["current_phase"]},{optional["owner_pid"]},
            {optional["heartbeat_ns"]}
            FROM initial_runs {where} ORDER BY run_id DESC LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        now = time.time_ns()
        threshold_ns = int(stale_after_seconds * 1_000_000_000)
        return tuple(
            _run_status(connection, row, now=now, threshold_ns=threshold_ns)
            for row in rows
        )
    finally:
        connection.close()


def _run_status(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: int,
    threshold_ns: int,
) -> RunStatus:
    run_id = int(row["run_id"])
    run_state = str(row["status"])
    heartbeat = None if row["heartbeat_ns"] is None else int(row["heartbeat_ns"])
    stale = (
        None
        if run_state != "running" or heartbeat is None
        else now - heartbeat > threshold_ns
    )
    owner_pid = None if row["owner_pid"] is None else int(row["owner_pid"])
    routes = _route_statuses(connection, run_id)
    current_phase = None if row["current_phase"] is None else str(row["current_phase"])
    if current_phase is None:
        current_phase = next(
            (
                route.current_phase
                for route in routes
                if route.status == "running" and route.current_phase is not None
            ),
            None,
        )
    return RunStatus(
        run_id=run_id,
        run_kind=str(row["run_kind"] or "initial"),
        status=run_state,
        root=str(row["root"]),
        source_run_id=(
            None if row["source_run_id"] is None else int(row["source_run_id"])
        ),
        current_phase=current_phase,
        owner_pid=owner_pid,
        owner_alive=process_is_alive(owner_pid) if run_state == "running" else None,
        heartbeat_ns=heartbeat,
        heartbeat_stale=stale,
        started_ns=int(row["started_ns"]),
        completed_ns=(
            None if row["completed_ns"] is None else int(row["completed_ns"])
        ),
        routes=routes,
        recovery_required_actions=_recovery_required_action_count(
            connection, run_id
        ),
    )


def _recovery_required_action_count(
    connection: sqlite3.Connection,
    run_id: int,
) -> int:
    table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='file_actions'"""
    ).fetchone()
    if table is None:
        return 0
    return int(
        connection.execute(
            """SELECT COUNT(*) FROM file_actions
            WHERE run_id=? AND status='recovery_required'""",
            (run_id,),
        ).fetchone()[0]
    )


def _route_statuses(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[RouteStatus, ...]:
    columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(route_runs)")
    }
    current = "current_phase" if "current_phase" in columns else "NULL AS current_phase"
    heartbeat = "heartbeat_ns" if "heartbeat_ns" in columns else "NULL AS heartbeat_ns"
    rows = connection.execute(
        f"""SELECT route_name,status,started_ns,completed_ns,error_type,
        {current},{heartbeat} FROM route_runs WHERE run_id=? ORDER BY route_name""",
        (run_id,),
    ).fetchall()
    phase_table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='route_phase_runs'"""
    ).fetchone()
    phases: dict[str, list[PhaseStatus]] = {}
    if phase_table is not None:
        for phase in connection.execute(
            """SELECT route_name,phase_name,status,started_ns,completed_ns,error_type
            FROM route_phase_runs WHERE run_id=?
            ORDER BY route_name,started_ns,phase_name""",
            (run_id,),
        ):
            phases.setdefault(str(phase["route_name"]), []).append(
                PhaseStatus(
                    route_name=str(phase["route_name"]),
                    phase_name=str(phase["phase_name"]),
                    status=str(phase["status"]),
                    started_ns=int(phase["started_ns"]),
                    completed_ns=(
                        None
                        if phase["completed_ns"] is None
                        else int(phase["completed_ns"])
                    ),
                    error_type=(
                        None
                        if phase["error_type"] is None
                        else str(phase["error_type"])
                    ),
                )
            )
    else:
        legacy_mappings = {
            "pdf-extraction": "extraction",
            "pdf-text-dedup": "text_dedup",
            "pdf-derived": "derived",
        }
        for phase in connection.execute(
            """SELECT phase,occurred_ns FROM run_events WHERE run_id=?
            AND phase IN ('pdf-extraction','pdf-text-dedup','pdf-derived')
            ORDER BY event_id""",
            (run_id,),
        ):
            phase_name = legacy_mappings[str(phase["phase"])]
            occurred_ns = int(phase["occurred_ns"])
            phases.setdefault("pdf", []).append(
                PhaseStatus(
                    route_name="pdf",
                    phase_name=phase_name,
                    status="completed",
                    started_ns=occurred_ns,
                    completed_ns=occurred_ns,
                    error_type=None,
                )
            )
    results = []
    for route in rows:
        route_name = str(route["route_name"])
        route_phases = tuple(phases.get(route_name, ()))
        current_phase = (
            None if route["current_phase"] is None else str(route["current_phase"])
        )
        if (
            current_phase is None
            and str(route["status"]) == "running"
            and route_name == "pdf"
        ):
            completed = {phase.phase_name for phase in route_phases}
            if "text_dedup" in completed and "derived" not in completed:
                current_phase = "derived_pending"
            elif "extraction" in completed and "text_dedup" not in completed:
                current_phase = "text_dedup_pending"
        results.append(
            RouteStatus(
                route_name=str(route["route_name"]),
                status=str(route["status"]),
                current_phase=current_phase,
                started_ns=int(route["started_ns"]),
                completed_ns=(
                    None
                    if route["completed_ns"] is None
                    else int(route["completed_ns"])
                ),
                heartbeat_ns=(
                    None
                    if route["heartbeat_ns"] is None
                    else int(route["heartbeat_ns"])
                ),
                error_type=(
                    None if route["error_type"] is None else str(route["error_type"])
                ),
                phases=route_phases,
            )
        )
    return tuple(results)


def serialized_run_status(status: RunStatus) -> str:
    """Return stable JSON for callers that prefer machine-readable status."""

    return json.dumps(
        {
            "run_id": status.run_id,
            "run_kind": status.run_kind,
            "status": status.status,
            "root": status.root,
            "source_run_id": status.source_run_id,
            "current_phase": status.current_phase,
            "owner_pid": status.owner_pid,
            "owner_alive": status.owner_alive,
            "heartbeat_ns": status.heartbeat_ns,
            "heartbeat_stale": status.heartbeat_stale,
            "started_ns": status.started_ns,
            "completed_ns": status.completed_ns,
            "recovery_required_actions": status.recovery_required_actions,
            "routes": [
                {
                    "route_name": route.route_name,
                    "status": route.status,
                    "current_phase": route.current_phase,
                    "started_ns": route.started_ns,
                    "completed_ns": route.completed_ns,
                    "heartbeat_ns": route.heartbeat_ns,
                    "error_type": route.error_type,
                    "phases": [
                        {
                            "phase_name": phase.phase_name,
                            "status": phase.status,
                            "started_ns": phase.started_ns,
                            "completed_ns": phase.completed_ns,
                            "error_type": phase.error_type,
                        }
                        for phase in route.phases
                    ],
                }
                for route in status.routes
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


# endregion [02]
