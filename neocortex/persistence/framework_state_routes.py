"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

# mypy: disable-error-code=attr-defined

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from neocortex.persistence.framework_state_common import finish_file_actions
from neocortex.runtime.orchestration.run_manifest import (
    RUN_BUDGET_SCHEMA,
    RUN_RECOVERY_SCHEMA,
    RUN_STAGE_SCHEMA,
)

class FrameworkStateRoutesMixin:
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    def begin_route_runs(
        self,
        run_id: int,
        route_names: Iterable[str],
        *,
        route_input_sources: Mapping[str, str] | None = None,
    ) -> None:
        now = time.time_ns()
        routes = tuple(route_names)
        input_sources = {
            str(name): str(source)
            for name, source in (route_input_sources or {}).items()
        }
        if input_sources and set(input_sources) != set(routes):
            raise ValueError("route input sources must cover exactly the selected routes")
        with self._connection:
            source_row = self._connection.execute(
                """SELECT source_run_id FROM initial_runs
                WHERE run_id=? AND status='running' AND scan_id IS NOT NULL""",
                (run_id,),
            ).fetchone()
            if source_row is None:
                raise ValueError(f"run {run_id} cannot start routes before snapshot publication")
            source_run_id = source_row[0]
            self._connection.executemany(
                """INSERT INTO route_runs(
                run_id,route_name,status,started_ns,current_phase,heartbeat_ns,
                source_run_id)
                VALUES(?,?,'running',?,'route_start',?,?)
                ON CONFLICT(run_id,route_name) DO NOTHING""",
                ((run_id, route_name, now, now, source_run_id) for route_name in routes),
            )
            if input_sources:
                self._append_lifecycle_event_once(
                    run_id,
                    level="info",
                    phase="route-inputs",
                    message="Route input sources bound",
                    idempotency_key="route-input-sources",
                    details={
                        "schema": "neocortex.route-input-sources/v1",
                        "route_input_sources": input_sources,
                    },
                )

    def read_route_input_sources(self, run_id: int) -> dict[str, str]:
        """Read the immutable route-input map bound before route workers."""

        row = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='route-inputs'
            AND message='Route input sources bound'
            ORDER BY event_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return {}
        try:
            payload = json.loads(str(row[0]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"run {run_id} route input sources are malformed") from exc
        sources = payload.get("route_input_sources") if isinstance(payload, Mapping) else None
        if not isinstance(sources, Mapping):
            raise RuntimeError(f"run {run_id} route input sources are invalid")
        return {str(name): str(source) for name, source in sources.items()}

    def begin_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        now = time.time_ns()
        with self._connection:
            inserted = self._connection.execute(
                """INSERT INTO route_phase_runs(
                run_id,route_name,phase_name,status,started_ns,heartbeat_ns,
                source_run_id)
                VALUES(?,?,?,'running',?,?,?)
                ON CONFLICT(run_id,route_name,phase_name) DO NOTHING""",
                (
                    run_id,
                    route_name,
                    phase_name,
                    now,
                    now,
                    source_run_id,
                ),
            )
            if inserted.rowcount != 1:
                return
            self._connection.execute(
                """UPDATE route_runs SET current_phase=?,heartbeat_ns=?
                WHERE run_id=? AND route_name=? AND status='running'""",
                (phase_name, now, run_id, route_name),
            )

    def complete_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        summary: Mapping[str, Any] | None = None,
    ) -> bool:
        now = time.time_ns()
        payload = (
            None
            if summary is None
            else json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        )
        with self._connection:
            updated = self._connection.execute(
                """UPDATE route_phase_runs SET status='completed',completed_ns=?,
                heartbeat_ns=?,summary_json=?,error_type=NULL,error_message=NULL
                WHERE run_id=? AND route_name=? AND phase_name=? AND status='running'""",
                (now, now, payload, run_id, route_name, phase_name),
            )
            if updated.rowcount == 1:
                return True
            status = self._connection.execute(
                """SELECT status FROM route_phase_runs
                WHERE run_id=? AND route_name=? AND phase_name=?""",
                (run_id, route_name, phase_name),
            ).fetchone()
            if status is not None and str(status[0]) == "completed":
                return False
            raise RuntimeError(
                f"route phase {run_id}/{route_name}/{phase_name} is not running"
            )

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None:
        now = time.time_ns()
        with self._connection:
            self._connection.execute(
                """UPDATE route_phase_runs SET status='failed',completed_ns=?,
                heartbeat_ns=?,error_type=?,error_message=?
                WHERE run_id=? AND route_name=? AND phase_name=?""",
                (
                    now,
                    now,
                    type(exc).__name__,
                    str(exc)[:8192],
                    run_id,
                    route_name,
                    phase_name,
                ),
            )

    def complete_route_run(
        self,
        run_id: int,
        route_name: str,
        summary: Mapping[str, Any],
    ) -> bool:
        payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            updated = self._connection.execute(
                """UPDATE route_runs SET status='completed',completed_ns=?,
                current_phase='completed',heartbeat_ns=?,summary_json=?,
                error_type=NULL,error_message=NULL
                WHERE run_id=? AND route_name=? AND status='running'""",
                (time.time_ns(), time.time_ns(), payload, run_id, route_name),
            )
            if updated.rowcount == 1:
                return True
            status = self._connection.execute(
                "SELECT status FROM route_runs WHERE run_id=? AND route_name=?",
                (run_id, route_name),
            ).fetchone()
            if status is not None and str(status[0]) == "completed":
                return False
            raise RuntimeError(f"route {run_id}/{route_name} is not running")

    def fail_route_run(
        self,
        run_id: int,
        route_name: str,
        exc: BaseException,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """UPDATE route_phase_runs SET status='failed',completed_ns=?,
                heartbeat_ns=?,error_type=?,error_message=?
                WHERE run_id=? AND route_name=? AND status='running'""",
                (
                    time.time_ns(),
                    time.time_ns(),
                    type(exc).__name__,
                    str(exc)[:8192],
                    run_id,
                    route_name,
                ),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='failed',completed_ns=?,
                current_phase='failed',heartbeat_ns=?,error_type=?,error_message=?
                WHERE run_id=? AND route_name=? AND status='running'""",
                (
                    time.time_ns(),
                    time.time_ns(),
                    type(exc).__name__,
                    str(exc)[:8192],
                    run_id,
                    route_name,
                ),
            )

    def mark_abandoned_runs(self) -> int:
        """Close runs left active after an unclean process termination."""

        with self._connection:
            active = self._connection.execute(
                """SELECT run_id FROM initial_runs
                WHERE status='running' ORDER BY run_id"""
            ).fetchall()
            active_ids = tuple(int(row[0]) for row in active)
            status_rows = self._connection.execute(
                "SELECT run_id,status FROM initial_runs"
            ).fetchall()
            run_statuses = {int(row[0]): str(row[1]) for row in status_rows}
            latest_stage_events: dict[tuple[int, str], Mapping[str, Any]] = {}
            for stage_row in self._connection.execute(
                """SELECT run_id,details_json FROM run_events
                WHERE phase='lifecycle-stage'
                AND message='Lifecycle stage transitioned'
                ORDER BY event_id"""
            ):
                try:
                    stage_payload = json.loads(str(stage_row[1]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("lifecycle stage recovery event is malformed") from exc
                if isinstance(stage_payload, Mapping):
                    stage_name = stage_payload.get("stage")
                    if isinstance(stage_name, str):
                        latest_stage_events[(int(stage_row[0]), stage_name)] = stage_payload
            stage_only_ids = {
                run_id
                for (run_id, _stage_name), stage_payload in latest_stage_events.items()
                if stage_payload.get("status") == "running"
                and run_statuses.get(run_id) not in {None, "running"}
            }
            recovery_ids = tuple(sorted(set(active_ids) | stage_only_ids))
            self._connection.execute(
                """UPDATE route_phase_runs SET status='interrupted',completed_ns=?,
                heartbeat_ns=?,error_type='InterruptedRun',
                error_message='framework phase was interrupted'
                WHERE status='running' AND run_id IN(
                SELECT run_id FROM initial_runs WHERE status='running')""",
                (time.time_ns(), time.time_ns()),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='interrupted',completed_ns=?,
                current_phase='interrupted',heartbeat_ns=?,
                error_type='InterruptedRun',error_message='framework run was interrupted'
                WHERE status='running' AND run_id IN(
                    SELECT run_id FROM initial_runs WHERE status='running')""",
                (time.time_ns(), time.time_ns()),
            )
            result = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='interrupted',
                current_phase='interrupted',heartbeat_ns=?
                WHERE status='running'""",
                (time.time_ns(), time.time_ns()),
            )
            for active_id in recovery_ids:
                route_names = tuple(
                    str(row[0])
                    for row in self._connection.execute(
                        """SELECT route_name FROM route_runs
                        WHERE run_id=? AND status='interrupted'
                        ORDER BY route_name""",
                        (active_id,),
                    )
                )
                candidate_rows, candidate_bytes = self.route_candidate_workload(active_id)
                route_input_sources = self.read_route_input_sources(active_id)
                start_event = self._connection.execute(
                    """SELECT details_json FROM run_events
                    WHERE run_id=? AND phase='run'
                    AND message='Ejecución aislada de rutas iniciada'
                    ORDER BY event_id DESC LIMIT 1""",
                    (active_id,),
                ).fetchone()
                if not route_input_sources and start_event is not None and start_event[0] is not None:
                    try:
                        details = json.loads(str(start_event[0]))
                    except (TypeError, json.JSONDecodeError):
                        details = None
                    if isinstance(details, Mapping) and isinstance(
                        details.get("route_input_sources"), Mapping
                    ):
                        route_input_sources = {
                            str(name): str(source)
                            for name, source in details["route_input_sources"].items()
                        }
                budget = self._read_run_budget_locked(active_id)
                if (
                    active_id in active_ids
                    and budget is not None
                    and not budget["cancel_requested"]
                ):
                    self._connection.execute(
                        """INSERT INTO run_events(
                        run_id,occurred_ns,level,phase,message,details_json)
                        VALUES(?,?,'warning','lifecycle-budget',
                        'Run cancellation requested',?)""",
                        (
                            active_id,
                            time.time_ns(),
                            json.dumps(
                                {
                                    "schema": RUN_BUDGET_SCHEMA,
                                    "kind": "cancelled",
                                    "manifest_digest": budget["manifest_digest"],
                                    "reason": "abrupt_termination",
                                    "idempotency_key": "cancellation",
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                manifest = self.read_run_manifest(active_id)
                self._append_lifecycle_event_once(
                    active_id,
                    level="warning",
                    phase="lifecycle-recovery",
                    message="Run abandoned after abrupt termination",
                    idempotency_key="abandoned",
                    details={
                        "schema": RUN_RECOVERY_SCHEMA,
                        "run_id": active_id,
                        "manifest_digest": None if manifest is None else manifest["digest"],
                        "status": "interrupted",
                        "routes": list(route_names),
                        "candidate_rows": candidate_rows,
                        "candidate_bytes": candidate_bytes,
                        "route_input_sources": route_input_sources,
                        "route_capabilities": self.read_run_route_capabilities(active_id),
                    },
                )
                if manifest is not None:
                    latest_stages: dict[str, dict[str, Any]] = {}
                    for stage_event in self.read_run_stages(active_id):
                        latest_stages[str(stage_event["stage"])] = stage_event
                    for stage_name, stage_event in latest_stages.items():
                        if stage_event.get("status") != "running":
                            continue
                        prior_details = stage_event.get("details")
                        details = dict(prior_details) if isinstance(prior_details, Mapping) else {}
                        details.update(
                            {
                                "reason": "abrupt_termination",
                                "previous_status": "running",
                            }
                        )
                        self._append_lifecycle_event_once(
                            active_id,
                            level="warning",
                            phase="lifecycle-stage",
                            message="Lifecycle stage transitioned",
                            idempotency_key=f"{stage_name}:abrupt-termination",
                            details={
                                "schema": RUN_STAGE_SCHEMA,
                                "run_id": active_id,
                                "manifest_digest": manifest["digest"],
                                "stage": stage_name,
                                "status": "interrupted",
                                "details": details,
                            },
                        )
        return int(result.rowcount) + len(stage_only_ids)

    def mark_abandoned_actions(self) -> int:
        """Distinguish abandoned intent from a crossed mutation frontier."""

        started_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT action_id FROM file_actions WHERE status='started' ORDER BY action_id"
            )
        )
        applying_ids = tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT action_id FROM file_actions WHERE status='applying' ORDER BY action_id"
            )
        )
        if started_ids:
            finish_file_actions(
                self._connection,
                started_ids,
                "failed",
                "framework interrupted before the mutation frontier; no "
                "filesystem effect was attempted",
            )
        if applying_ids:
            finish_file_actions(
                self._connection,
                applying_ids,
                "recovery_required",
                "framework interrupted after the mutation frontier; the "
                "filesystem effect is uncertain and requires reconciliation",
            )
        return len(started_ids) + len(applying_ids)

    def referenced_inventory_scan_ids(self) -> tuple[int, ...]:
        """Return every inventory generation referenced by durable run history."""

        return tuple(
            int(row[0])
            for row in self._connection.execute(
                "SELECT DISTINCT scan_id FROM initial_runs "
                "WHERE scan_id IS NOT NULL ORDER BY scan_id"
            )
        )

    def complete_initial_run(
        self,
        run_id: int,
        scan_id: int,
        cursor: object | None,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
    ) -> bool:
        self._validate_inventory_binding(
            scan_id,
            reconciliation_records,
            inventory_attempts,
            inventory_mode,
            0,
        )
        if cursor is None and (
            inventory_mode != "full" or reconciliation_records != 0 or inventory_attempts != 1
        ):
            raise ValueError("portable inventory must publish one unreconciled full scan")
        with self._connection:
            self._check_run_completion_budget_locked(run_id)
            self._check_organization_completion_locked(run_id)
            result = self._connection.execute(
                "UPDATE initial_runs SET completed_ns=?, status='completed', "
                "current_phase='completed',heartbeat_ns=?,end_usn=? "
                "WHERE run_id=? AND status='running' AND scan_id=? "
                "AND run_kind='initial' "
                "AND reconciliation_records=? AND inventory_attempts=? "
                "AND inventory_mode=?",
                (
                    time.time_ns(),
                    time.time_ns(),
                    None,
                    run_id,
                    scan_id,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                ),
            )
            if result.rowcount != 1:
                status = self._connection.execute(
                    "SELECT status FROM initial_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if status is not None and str(status[0]) == "completed":
                    return False
                raise RuntimeError(
                    f"run {run_id} cannot complete without its published snapshot"
                )
            self._append_lifecycle_event_once(
                run_id,
                level="info",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:completed",
                details={"status": "completed"},
            )
            return True

    def complete_operational_run(self, run_id: int) -> bool:
        with self._connection:
            self._check_run_completion_budget_locked(run_id)
            self._check_organization_completion_locked(run_id)
            result = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='completed',
                current_phase='completed',heartbeat_ns=?
                WHERE run_id=? AND status='running'
                AND run_kind IN ('route_only','resume')""",
                (time.time_ns(), time.time_ns(), run_id),
            )
            if result.rowcount != 1:
                status = self._connection.execute(
                    "SELECT status FROM initial_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if status is not None and str(status[0]) == "completed":
                    return False
                raise RuntimeError(f"run {run_id} is not a running operational execution")
            self._append_lifecycle_event_once(
                run_id,
                level="info",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:completed",
                details={"status": "completed"},
            )
            return True

    def fail_initial_run(self, run_id: int) -> bool:
        with self._connection:
            now = time.time_ns()
            transitioned = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='failed',
                current_phase='failed',heartbeat_ns=?
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            if transitioned.rowcount != 1:
                return False
            self._connection.execute(
                """UPDATE route_phase_runs SET status='failed',completed_ns=?,
                heartbeat_ns=?,error_type=COALESCE(error_type,'FrameworkRunFailed'),
                error_message=COALESCE(error_message,'framework run failed')
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='failed',completed_ns=?,
                current_phase='failed',heartbeat_ns=?,
                error_type=COALESCE(error_type,'FrameworkRunFailed'),
                error_message=COALESCE(error_message,'framework run failed')
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._append_lifecycle_event_once(
                run_id,
                level="error",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:failed",
                details={"status": "failed"},
            )
            return True

    def cancel_initial_run(self, run_id: int) -> bool:
        with self._connection:
            now = time.time_ns()
            transitioned = self._connection.execute(
                """UPDATE initial_runs SET completed_ns=?,status='cancelled',
                current_phase='cancelled',heartbeat_ns=?
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            if transitioned.rowcount != 1:
                return False
            self._connection.execute(
                """UPDATE route_phase_runs SET status='cancelled',completed_ns=?,
                heartbeat_ns=?,error_type='KeyboardInterrupt',
                error_message='framework run cancelled'
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._connection.execute(
                """UPDATE route_runs SET status='cancelled',completed_ns=?,
                current_phase='cancelled',heartbeat_ns=?,
                error_type='KeyboardInterrupt',error_message='framework run cancelled'
                WHERE run_id=? AND status='running'""",
                (now, now, run_id),
            )
            self._append_lifecycle_event_once(
                run_id,
                level="warning",
                phase="lifecycle-transition",
                message="Run transitioned",
                idempotency_key="status:cancelled",
                details={"status": "cancelled"},
            )
            return True

__all__ = ["FrameworkStateRoutesMixin"]
