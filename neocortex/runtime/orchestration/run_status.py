"""Read-only operational status queries for framework executions."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from neocortex.persistence.sqlite_immutable import immutable_sqlite_database
from neocortex.runtime.orchestration.run_lifecycle import (
    DEFAULT_STALE_HEARTBEAT_SECONDS,
    process_is_alive,
)
from neocortex.runtime.orchestration.run_manifest import (
    RUN_BUDGET_SCHEMA,
    RUN_STAGE_SCHEMA,
    lifecycle_envelope,
    verify_event_payload,
)
from neocortex.runtime.orchestration.replay_metrics import (
    normalize_route_replay_metrics,
)
# region [01] Status models


@dataclass(frozen=True, slots=True)
class PhaseStatus:
    route_name: str
    phase_name: str
    status: str
    started_ns: int
    completed_ns: int | None
    error_type: str | None

    @property
    def elapsed_ns(self) -> int:
        end = time.time_ns() if self.completed_ns is None else self.completed_ns
        return max(0, end - self.started_ns)


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
    resume_capability: str = "not_resumable"
    candidates: int = 0
    processed: int = 0
    cache_hits: int = 0
    new_work: int = 0
    cached_errors: int = 0
    replay_status: str = "unobserved"

    @property
    def elapsed_ns(self) -> int:
        end = time.time_ns() if self.completed_ns is None else self.completed_ns
        return max(0, end - self.started_ns)


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
    manifest: dict[str, object] | None = None
    budget: dict[str, object] | None = None
    recovery: dict[str, object] | None = None
    resumed: bool = False
    replayed: bool = False
    skipped_routes: tuple[str, ...] = ()
    non_replayable_routes: tuple[str, ...] = ()
    stages: tuple[dict[str, object], ...] = ()

    @property
    def elapsed_ns(self) -> int:
        end = time.time_ns() if self.completed_ns is None else self.completed_ns
        return max(0, end - self.started_ns)
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
    with immutable_sqlite_database(database_path, timeout_seconds=10) as connection:
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
    manifest = _run_manifest(connection, run_id)
    budget = _run_budget(connection, run_id)
    recovery = _run_recovery(connection, run_id)
    stages = _run_stages(connection, run_id)
    route_capabilities = _route_capabilities(manifest)
    routes = _route_statuses(connection, run_id, route_capabilities)
    # A completed route was executed; it is not a skipped route merely because
    # its owner reused cached work.  Keep lifecycle replay (resume/recovery)
    # separate from per-route cache replay, which is exposed by
    # ``RouteStatus.replay_status`` and its counters below.
    skipped_routes = tuple(
        route.route_name for route in routes if route.status == "skipped"
    )
    non_replayable_routes = _non_replayable_routes(routes, recovery)
    # An interrupted source is recoverable, but it was not itself resumed.
    # ``resume`` identifies a new execution linked to that source.  Initial
    # runs can still report route-level cache replay without being lifecycle
    # replays.
    resumed = str(row["run_kind"] or "initial") == "resume"
    replayed = resumed
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
        manifest=manifest,
        budget=budget,
        recovery=recovery,
        resumed=resumed,
        replayed=replayed,
        skipped_routes=skipped_routes,
        non_replayable_routes=non_replayable_routes,
        stages=stages,
    )


def _run_manifest(connection: sqlite3.Connection, run_id: int) -> dict[str, object] | None:
    """Read one immutable lifecycle manifest without migrating the owner."""

    table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='run_events'"""
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """SELECT details_json FROM run_events
        WHERE run_id=? AND phase='lifecycle-manifest'
        AND message='Run manifest published' ORDER BY event_id DESC LIMIT 1""",
        (run_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("manifest is not an object")
        return verify_event_payload(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError(f"run {run_id} lifecycle manifest is invalid") from exc


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


def _run_budget(
    connection: sqlite3.Connection,
    run_id: int,
) -> dict[str, object] | None:
    """Read the append-only budget ledger without opening the owner database."""

    table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='run_events'"""
    ).fetchone()
    if table is None:
        return None
    rows = connection.execute(
        """SELECT event_id,details_json FROM run_events
        WHERE run_id=? AND phase='lifecycle-budget'
        ORDER BY event_id""",
        (run_id,),
    ).fetchall()
    if not rows:
        return None
    try:
        baseline = json.loads(str(rows[0]["details_json"]))
        if not isinstance(baseline, dict) or baseline.get("schema") != RUN_BUDGET_SCHEMA:
            raise ValueError("unsupported budget schema")
        reservations: set[str] = set()
        consumed_items = int(baseline.get("consumed_items", 0))
        consumed_bytes = int(baseline.get("consumed_bytes", 0))
        cancelled = bool(baseline.get("cancel_requested", False))
        cancel_reason = baseline.get("cancel_reason")
        for row in rows[1:]:
            event = json.loads(str(row["details_json"]))
            if not isinstance(event, dict) or event.get("schema") != RUN_BUDGET_SCHEMA:
                raise ValueError("malformed budget event")
            if event.get("kind") == "consumed":
                reservation_id = str(event.get("reservation_id", ""))
                if reservation_id in reservations:
                    continue
                reservations.add(reservation_id)
                consumed_items += int(event.get("items", 0))
                consumed_bytes += int(event.get("bytes", 0))
            elif event.get("kind") == "cancelled":
                cancelled = True
                cancel_reason = event.get("reason")
            elif event.get("kind") == "bound":
                baseline["manifest_digest"] = event.get("manifest_digest")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise sqlite3.DatabaseError(f"run {run_id} lifecycle budget is invalid") from exc
    now = time.time_ns()
    started_ns = int(baseline.get("started_ns", now))
    deadline_ns = baseline.get("deadline_ns")
    expired = deadline_ns is not None and now >= int(deadline_ns)
    max_items = baseline.get("max_items")
    max_bytes = baseline.get("max_bytes")
    return {
        "schema": RUN_BUDGET_SCHEMA,
        "manifest_digest": baseline.get("manifest_digest"),
        "max_items": max_items,
        "max_bytes": max_bytes,
        "max_duration_seconds": baseline.get("max_duration_seconds"),
        "started_ns": started_ns,
        "deadline_ns": deadline_ns,
        "consumed_items": consumed_items,
        "consumed_bytes": consumed_bytes,
        "remaining_items": None if max_items is None else max(0, int(max_items) - consumed_items),
        "remaining_bytes": None if max_bytes is None else max(0, int(max_bytes) - consumed_bytes),
        "elapsed_seconds": max(0, now - started_ns) / 1_000_000_000,
        "expired": expired,
        "cancel_requested": cancelled,
        "cancel_reason": cancel_reason,
        "reservation_count": len(reservations),
        "last_event_id": int(rows[-1]["event_id"]),
    }


def _run_recovery(
    connection: sqlite3.Connection,
    run_id: int,
) -> dict[str, object] | None:
    table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='run_events'"""
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """SELECT details_json FROM run_events
        WHERE run_id=? AND phase='lifecycle-recovery'
        ORDER BY event_id DESC LIMIT 1""",
        (run_id,),
    ).fetchone()
    if row is None or row["details_json"] is None:
        return None
    try:
        value = json.loads(str(row["details_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise sqlite3.DatabaseError(f"run {run_id} lifecycle recovery is invalid") from exc
    if not isinstance(value, dict):
        raise sqlite3.DatabaseError(f"run {run_id} lifecycle recovery is not an object")
    return value


def _run_stages(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[dict[str, object], ...]:
    """Read bounded cross-owner lifecycle stage transitions."""

    table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='run_events'"""
    ).fetchone()
    if table is None:
        return ()
    rows = connection.execute(
        """SELECT event_id,details_json FROM run_events
        WHERE run_id=? AND phase='lifecycle-stage'
        AND message='Lifecycle stage transitioned'
        ORDER BY event_id LIMIT 65""",
        (run_id,),
    ).fetchall()
    if len(rows) > 64:
        raise sqlite3.DatabaseError(f"run {run_id} has too many lifecycle stages")
    manifest = _run_manifest(connection, run_id)
    if manifest is None and rows:
        raise sqlite3.DatabaseError(f"run {run_id} lifecycle stages have no manifest")
    stages: list[dict[str, object]] = []
    for row in rows:
        try:
            value = json.loads(str(row["details_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise sqlite3.DatabaseError(f"run {run_id} lifecycle stage is invalid") from exc
        if not isinstance(value, dict) or value.get("schema") != RUN_STAGE_SCHEMA:
            raise sqlite3.DatabaseError(f"run {run_id} lifecycle stage schema is unsupported")
        if value.get("run_id") != run_id:
            raise sqlite3.DatabaseError(f"run {run_id} lifecycle stage owner is invalid")
        if manifest is not None and value.get("manifest_digest") != manifest.get("digest"):
            raise sqlite3.DatabaseError(f"run {run_id} lifecycle stage is detached from its manifest")
        if not isinstance(value.get("stage"), str) or not isinstance(value.get("status"), str):
            raise sqlite3.DatabaseError(f"run {run_id} lifecycle stage identity is invalid")
        if not isinstance(value.get("details"), dict):
            raise sqlite3.DatabaseError(f"run {run_id} lifecycle stage details are invalid")
        value["event_id"] = int(row["event_id"])
        stages.append(value)
    return tuple(stages)


def _non_replayable_routes(
    routes: tuple[RouteStatus, ...],
    recovery: dict[str, object] | None,
) -> tuple[str, ...]:
    if recovery is not None and int(recovery.get("candidate_rows", 0)) == 0:
        input_sources = recovery.get("route_input_sources", {})
        if not isinstance(input_sources, dict):
            input_sources = {}
        capabilities = recovery.get("route_capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}
        return tuple(
            route.route_name
            for route in routes
            if route.status in {"failed", "cancelled", "interrupted"}
            and (
                input_sources.get(route.route_name, "route_candidates")
                != "inventory_snapshot"
                or capabilities.get(route.route_name, "safe_replay") == "not_resumable"
            )
        )
    if recovery is not None:
        capabilities = recovery.get("route_capabilities", {})
        if isinstance(capabilities, dict):
            return tuple(
                route.route_name
                for route in routes
                if route.status in {"failed", "cancelled", "interrupted"}
                and capabilities.get(route.route_name, "safe_replay") == "not_resumable"
            )
    return ()


def _route_capabilities(manifest: dict[str, object] | None) -> dict[str, str]:
    if manifest is None:
        return {}
    value = manifest.get("route_capabilities", {})
    if not isinstance(value, dict):
        raise sqlite3.DatabaseError("run manifest route capabilities are invalid")
    capabilities = {str(name): str(capability) for name, capability in value.items()}
    if any(
        capability not in {"phase_resume", "safe_replay", "not_resumable"}
        for capability in capabilities.values()
    ):
        raise sqlite3.DatabaseError("run manifest route capability is unsupported")
    return capabilities


def _route_statuses(
    connection: sqlite3.Connection,
    run_id: int,
    route_capabilities: dict[str, str] | None = None,
) -> tuple[RouteStatus, ...]:
    route_capabilities = {} if route_capabilities is None else route_capabilities
    columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(route_runs)")
    }
    current = "current_phase" if "current_phase" in columns else "NULL AS current_phase"
    heartbeat = "heartbeat_ns" if "heartbeat_ns" in columns else "NULL AS heartbeat_ns"
    summary = "summary_json" if "summary_json" in columns else "NULL AS summary_json"
    rows = connection.execute(
        f"""SELECT route_name,status,started_ns,completed_ns,error_type,
        {current},{heartbeat},{summary} FROM route_runs WHERE run_id=? ORDER BY route_name""",
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
        raw_summary = route["summary_json"]
        if raw_summary is None:
            summary_payload: dict[str, object] = {}
        else:
            try:
                decoded_summary = json.loads(str(raw_summary))
            except (TypeError, json.JSONDecodeError) as exc:
                raise sqlite3.DatabaseError(
                    f"route {run_id}/{route_name} summary is invalid"
                ) from exc
            if not isinstance(decoded_summary, dict):
                raise sqlite3.DatabaseError(
                    f"route {run_id}/{route_name} summary is not an object"
                )
            summary_payload = decoded_summary
        replay_metrics = normalize_route_replay_metrics(
            route_name,
            summary_payload,
            replayability=route_capabilities.get(route_name, "not_resumable"),
        )
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
                resume_capability=route_capabilities.get(route_name, "not_resumable"),
                candidates=int(replay_metrics["candidates"]),
                processed=int(replay_metrics["processed"]),
                cache_hits=int(replay_metrics["cache_hits"]),
                new_work=int(replay_metrics["new_work"]),
                cached_errors=int(replay_metrics["cached_errors"]),
                replay_status=str(replay_metrics["replay_status"]),
            )
        )
    return tuple(results)


def serialized_run_status(status: RunStatus) -> str:
    """Return stable JSON for callers that prefer machine-readable status."""

    manifest_capabilities = None
    if isinstance(status.manifest, dict):
        value = status.manifest.get("route_capabilities")
        if isinstance(value, dict):
            manifest_capabilities = {str(name): str(capability) for name, capability in value.items()}

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
            "elapsed_ns": status.elapsed_ns,
            "recovery_required_actions": status.recovery_required_actions,
            "manifest": status.manifest,
            "budget": status.budget,
            "recovery": status.recovery,
            "resumed": status.resumed,
            "replayed": status.replayed,
            "skipped_routes": list(status.skipped_routes),
            "non_replayable_routes": list(status.non_replayable_routes),
            "stages": list(status.stages),
            "route_capabilities": manifest_capabilities,
            "lifecycle": lifecycle_envelope(
                manifest=status.manifest,
                status=status.status,
                routes=tuple(
                    {
                        "route_name": route.route_name,
                        "status": route.status,
                        "current_phase": route.current_phase,
                        "resume_capability": route.resume_capability,
                        "replayability": route.resume_capability,
                        "candidates": route.candidates,
                        "processed": route.processed,
                        "cache_hits": route.cache_hits,
                        "new_work": route.new_work,
                        "cached_errors": route.cached_errors,
                        "elapsed_ns": route.elapsed_ns,
                        "replay_status": route.replay_status,
                    }
                    for route in status.routes
                ),
                errors=tuple(
                    {
                        "route_name": route.route_name,
                        "error_type": route.error_type,
                    }
                    for route in status.routes
                    if route.error_type is not None
                ),
                resumed_from=status.source_run_id,
                resumed=status.resumed,
                replayed=status.replayed,
                skipped=status.skipped_routes,
                non_replayable=status.non_replayable_routes,
                budget=status.budget,
                recovery=status.recovery,
                stages=tuple(status.stages),
                route_capabilities=manifest_capabilities,
            ),
            "routes": [
                {
                    "route_name": route.route_name,
                    "status": route.status,
                    "current_phase": route.current_phase,
                        "started_ns": route.started_ns,
                        "completed_ns": route.completed_ns,
                        "elapsed_ns": route.elapsed_ns,
                    "heartbeat_ns": route.heartbeat_ns,
                    "error_type": route.error_type,
                    "resume_capability": route.resume_capability,
                    "replayability": route.resume_capability,
                    "candidates": route.candidates,
                    "processed": route.processed,
                    "cache_hits": route.cache_hits,
                    "new_work": route.new_work,
                    "cached_errors": route.cached_errors,
                    "replay_status": route.replay_status,
                    "phases": [
                        {
                            "phase_name": phase.phase_name,
                            "status": phase.status,
                            "started_ns": phase.started_ns,
                            "completed_ns": phase.completed_ns,
                            "elapsed_ns": phase.elapsed_ns,
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
