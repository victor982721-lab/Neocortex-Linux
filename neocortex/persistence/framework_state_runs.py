"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
from neocortex.persistence.framework_state_common import corpus_mutation_guard
from neocortex.persistence.framework_state_types import (
    DurableInventoryBinding,
    InventoryRunEvidence,
    RunBudgetExceeded,
    bounded_lifecycle_name as _bounded_lifecycle_name,
)
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.persistence.operational_freshness import require_operational_identity
from neocortex.persistence.operational_freshness import operational_identity_floor
from neocortex.runtime.orchestration.run_manifest import (
    RUN_BUDGET_SCHEMA,
    RUN_CHECKPOINT_SCHEMA,
    RUN_STAGE_SCHEMA,
    RunBudget,
    verify_event_payload,
)
from neocortex.platform.policy import sqlite_path_collation

_PATH_COLLATION = sqlite_path_collation()

class FrameworkStateRunsMixin:
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    def begin_initial_run(
        self,
        root: Path,
        cursor: object | None,
        *,
        inventory_policy_signature: str | None = None,
    ) -> int:
        policy = CorpusAccessPolicy.capture("normal", root)
        signature = inventory_policy_signature
        if signature is not None and (
            not signature or signature.strip() != signature or len(signature.encode("utf-8")) > 4096
        ):
            raise ValueError("inventory policy signature must be trimmed and bounded")
        now = time.time_ns()
        del cursor
        journal_values = (None, None, None)
        with self._connection:
            # State-reset may retain only an allocator floor while removing
            # the visible run ledger.  Allocate explicitly inside the same
            # writer transaction so a new run can never reuse an identifier
            # still present in owner/cache provenance.
            from neocortex.persistence.framework_run_ids import framework_next_run_id

            self._connection.execute("BEGIN IMMEDIATE")
            run_id = framework_next_run_id(self._connection)
            result = self._connection.execute(
                """INSERT INTO initial_runs(
                run_id,root,started_ns,status,run_kind,current_phase,owner_pid,heartbeat_ns,
                journal_volume,journal_id,start_usn,corpus_access_mode,
                root_device_id_hex,root_file_id_hex,root_birthtime_ns,state_directory,
                inventory_policy_signature)
                VALUES(?,?,?,'running','initial','prepare',?,?, ?,?,?, ?,?,?,?,?,?)""",
                (
                    run_id,
                    str(policy.root),
                    now,
                    os.getpid(),
                    now,
                    *journal_values,
                    policy.mode,
                    policy.root_device_id_hex,
                    policy.root_file_id_hex,
                    policy.root_birthtime_ns,
                    str(Path(os.path.realpath(self.path.parent))),
                    signature,
                ),
            )
        if result.lastrowid is None:
            raise RuntimeError("SQLite did not return a framework run identifier")
        return run_id

    def begin_operational_run(
        self,
        root: Path,
        *,
        run_kind: str,
        source_run_id: int,
    ) -> int:
        """Start a route-only or resumed run without inventory side effects."""

        if run_kind not in {"route_only", "resume"}:
            raise ValueError(f"invalid operational run kind: {run_kind}")
        require_operational_identity(self._connection, "framework", source_run_id)
        source_status = self._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?",
            (source_run_id,),
        ).fetchone()
        if source_status is None:
            raise ValueError(f"source run {source_run_id} does not exist")
        if str(source_status[0]) == "running":
            raise ValueError(f"source run {source_run_id} is still running")
        now = time.time_ns()
        with self._connection:
            from neocortex.persistence.framework_run_ids import framework_next_run_id

            self._connection.execute("BEGIN IMMEDIATE")
            run_id = framework_next_run_id(self._connection)
            result = self._connection.execute(
                """INSERT INTO initial_runs(
                run_id,root,started_ns,status,run_kind,source_run_id,current_phase,
                owner_pid,heartbeat_ns,scan_id,journal_volume,journal_id,start_usn,
                end_usn,reconciliation_records,inventory_attempts,inventory_mode,
                corpus_access_mode,root_device_id_hex,root_file_id_hex,
                root_birthtime_ns,state_directory,inventory_policy_signature)
                SELECT ?,?,?,'running',?,?, 'route_prepare',?,?,scan_id,journal_volume,
                journal_id,start_usn,end_usn,0,0,'reused',corpus_access_mode,
                root_device_id_hex,root_file_id_hex,root_birthtime_ns,state_directory,
                inventory_policy_signature
                FROM initial_runs WHERE run_id=?""",
                (
                    run_id,
                    str(root),
                    now,
                    run_kind,
                    source_run_id,
                    os.getpid(),
                    now,
                    source_run_id,
                ),
            )
        if result.lastrowid is None or result.rowcount != 1:
            raise ValueError(f"source run {source_run_id} does not exist")
        return run_id

    def abort_run_start(
        self,
        run_id: int,
        exc: BaseException | None = None,
        *,
        cancelled: bool = False,
    ) -> bool:
        """Terminalize a run whose post-insert startup sequence failed.

        ``begin_operational_run`` must remain compatible with callers that
        build a manifest and copy inputs in separate bounded steps.  This
        compensating transition gives those callers a fail-closed boundary:
        any exception after the row insert can be recorded as terminal rather
        than leaving a synthetic ``running`` owner behind.
        """

        row = self._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} does not exist")
        if str(row[0]) != "running":
            return False
        if cancelled:
            reason = "user"
            if isinstance(exc, RunBudgetExceeded):
                reason = "budget"
            try:
                self.request_run_cancellation(run_id, reason)
            except (OSError, RuntimeError, sqlite3.Error):
                # The terminal row transition below is still authoritative;
                # callers can inspect the error event when cancellation could
                # not be appended.
                pass
            transitioned = self.cancel_initial_run(run_id)
        else:
            transitioned = self.fail_initial_run(run_id)
        if transitioned and exc is not None:
            self.record_event(
                run_id,
                "warning" if cancelled else "error",
                "lifecycle-start",
                "Inicio de ejecución abortado",
                {
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:8192],
                    "cancelled": cancelled,
                },
            )
        return transitioned

    def corpus_mutation_guard(self, run_id: int) -> CorpusMutationGuard:
        """Return the immutable corpus mutation guard for one durable run."""

        return corpus_mutation_guard(self._connection, run_id)

    def latest_durable_inventory_binding(
        self,
        root: Path,
        *,
        corpus_access_mode: str | None = None,
        inventory_policy_signature: str | None = None,
    ) -> DurableInventoryBinding | None:
        """Return the newest completed owner with its exact published cursor."""

        if corpus_access_mode not in {None, "normal", "analyze_only"}:
            raise ValueError("invalid corpus access mode filter")
        row = self._connection.execute(
            f"""SELECT run_id,scan_id,corpus_access_mode,
            inventory_policy_signature,journal_volume,journal_id,end_usn
            FROM initial_runs
            WHERE root=? COLLATE {_PATH_COLLATION} AND status='completed'
            AND scan_id IS NOT NULL
            AND run_kind='initial' AND run_id>?
            ORDER BY run_id DESC LIMIT 1""",
            (str(Path(os.path.abspath(os.path.realpath(root)))), operational_identity_floor(self._connection, "framework")),
        ).fetchone()
        if row is None:
            return None
        if corpus_access_mode is not None and str(row[2]) != corpus_access_mode:
            return None
        if inventory_policy_signature is not None and row[3] != inventory_policy_signature:
            return None
        return DurableInventoryBinding(
            run_id=int(row[0]),
            scan_id=int(row[1]),
            corpus_access_mode=str(row[2]),
            inventory_policy_signature=(None if row[3] is None else str(row[3])),
            end_cursor=None,
        )

    def latest_durable_inventory_run(
        self,
        root: Path,
        *,
        corpus_access_mode: str | None = None,
        inventory_policy_signature: str | None = None,
    ) -> tuple[int, int] | None:
        """Retain the historical run/scan API over the stronger binding."""

        binding = self.latest_durable_inventory_binding(
            root,
            corpus_access_mode=corpus_access_mode,
            inventory_policy_signature=inventory_policy_signature,
        )
        if binding is None:
            return None
        return binding.run_id, binding.scan_id

    def set_run_phase(self, run_id: int, phase: str) -> None:
        with self._connection:
            self._connection.execute(
                """UPDATE initial_runs SET current_phase=?,heartbeat_ns=?
                WHERE run_id=? AND status='running'""",
                (phase, time.time_ns(), run_id),
            )

    def update_run_start_cursor(
        self,
        run_id: int,
        cursor: object | None,
    ) -> None:
        """Keep the legacy cursor columns explicitly empty on Linux."""

        del cursor
        journal_values = (None, None, None)
        with self._connection:
            updated = self._connection.execute(
                "UPDATE initial_runs SET journal_volume=?,journal_id=?,start_usn=? "
                "WHERE run_id=? AND status='running'",
                (*journal_values, run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} cannot update its effective inventory cursor")

    @staticmethod
    def _validate_inventory_binding(
        scan_id: int,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
        candidate_rows: int,
    ) -> None:
        if (
            type(scan_id) is not int
            or scan_id <= 0
            or type(reconciliation_records) is not int
            or reconciliation_records < 0
            or type(inventory_attempts) is not int
            or inventory_attempts < 0
            or inventory_mode not in {"full", "incremental"}
            or type(candidate_rows) is not int
            or candidate_rows < 0
        ):
            raise ValueError("invalid initial routing snapshot")

    def publish_initial_routing_snapshot(
        self,
        run_id: int,
        scan_id: int,
        reconciliation_records: int,
        inventory_attempts: int,
        inventory_mode: str,
        candidate_rows: int,
    ) -> bool:
        """Atomically publish a complete inventory and routing candidate snapshot."""

        self._validate_inventory_binding(
            scan_id,
            reconciliation_records,
            inventory_attempts,
            inventory_mode,
            candidate_rows,
        )
        with self._connection:
            row = self._connection.execute(
                """SELECT status,run_kind,scan_id,reconciliation_records,
                inventory_attempts,inventory_mode FROM initial_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"initial run {run_id} does not exist")
            status, run_kind, current_scan_id, *metadata = row
            if str(run_kind) != "initial" or str(status) != "running":
                raise ValueError(f"run {run_id} cannot bind inventory while {run_kind}/{status}")
            actual_candidates = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            if actual_candidates != candidate_rows:
                raise ValueError(f"run {run_id} routing candidate count changed before publication")
            if current_scan_id is not None:
                persisted = (int(current_scan_id), *(metadata))
                requested = (
                    scan_id,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                )
                if persisted != requested:
                    raise ValueError(f"run {run_id} has conflicting routing snapshot metadata")
                marker = self._connection.execute(
                    """SELECT 1 FROM run_events WHERE run_id=?
                    AND phase='routing-snapshot'
                    AND message='Snapshot de rutas publicado' LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if marker is None:
                    raise ValueError(
                        f"run {run_id} inventory is bound without publication evidence"
                    )
                return False
            now = time.time_ns()
            result = self._connection.execute(
                """UPDATE initial_runs SET scan_id=?,reconciliation_records=?,
                inventory_attempts=?,inventory_mode=?,heartbeat_ns=?
                WHERE run_id=? AND status='running'
                AND run_kind='initial'
                AND scan_id IS NULL""",
                (
                    scan_id,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                    now,
                    run_id,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"run {run_id} routing snapshot was not published")
            details_json = json.dumps(
                {
                    "schema": "neocortex.routing-snapshot/v1",
                    "scan_id": scan_id,
                    "candidate_rows": candidate_rows,
                    "reconciliation_records": reconciliation_records,
                    "attempts": inventory_attempts,
                    "mode": inventory_mode,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','routing-snapshot',
                'Snapshot de rutas publicado',?)""",
                (run_id, now, details_json),
            )
        return True

    def recover_initial_routing_snapshot(
        self,
        run_id: int,
        evidence: InventoryRunEvidence,
        candidate_rows: int,
    ) -> bool:
        """Recover only a legacy snapshot already proven complete by route work."""

        self._validate_inventory_binding(
            evidence.scan_id,
            evidence.reconciliation_records,
            evidence.inventory_attempts,
            evidence.inventory_mode,
            candidate_rows,
        )
        with self._connection:
            actual_candidates = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            route_runs = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM route_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            if actual_candidates != candidate_rows or route_runs <= 0:
                raise ValueError(f"source run {run_id} has no complete legacy routing snapshot")
            row = self._connection.execute(
                """SELECT status,run_kind,scan_id,reconciliation_records,
                inventory_attempts,inventory_mode FROM initial_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"initial run {run_id} does not exist")
            status, run_kind, current_scan_id, *metadata = row
            if str(run_kind) != "initial" or str(status) != "interrupted":
                raise ValueError(f"run {run_id} cannot recover inventory while {run_kind}/{status}")
            requested = (
                evidence.scan_id,
                evidence.reconciliation_records,
                evidence.inventory_attempts,
                evidence.inventory_mode,
            )
            if current_scan_id is not None:
                if (int(current_scan_id), *metadata) != requested:
                    raise ValueError(f"run {run_id} has conflicting recovered snapshot metadata")
                return False
            now = time.time_ns()
            result = self._connection.execute(
                """UPDATE initial_runs SET scan_id=?,reconciliation_records=?,
                inventory_attempts=?,inventory_mode=?,heartbeat_ns=?
                WHERE run_id=? AND status='interrupted' AND run_kind='initial'
                AND scan_id IS NULL""",
                (*requested, now, run_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"run {run_id} inventory recovery lost its CAS")
            details_json = json.dumps(
                {
                    "schema": "neocortex.inventory-recovery/v1",
                    "scan_id": evidence.scan_id,
                    "inventory_event_id": evidence.event_id,
                    "files": evidence.files,
                    "candidate_rows": candidate_rows,
                    "validation": "complete_scan_root_identity_file_and_route_counts",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','inventory-recovery',
                'Vínculo de inventario recuperado',?)""",
                (run_id, now, details_json),
            )
        return True

    def record_event(
        self,
        run_id: int,
        level: str,
        phase: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one structured operational event without external log artifacts."""

        if level not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"invalid event level: {level}")
        details_json = (
            None
            if details is None
            else json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        )
        with self._connection:
            self._connection.execute(
                "INSERT INTO run_events(run_id,occurred_ns,level,phase,message,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, time.time_ns(), level, phase, message, details_json),
            )

    @staticmethod
    def _budget_configuration(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        value = manifest.get("budget", {})
        if not isinstance(value, Mapping):
            raise ValueError("run manifest budget is not an object")
        durable = value.get("durable")
        if durable is not None:
            if not isinstance(durable, Mapping):
                raise ValueError("run manifest durable budget is not an object")
            return durable
        return value

    def _append_lifecycle_event_once(
        self,
        run_id: int,
        *,
        level: str,
        phase: str,
        message: str,
        idempotency_key: str,
        details: Mapping[str, Any],
    ) -> bool:
        """Append one lifecycle event exactly once under the writer lock."""

        payload = dict(details)
        payload["idempotency_key"] = idempotency_key
        details_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        rows = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?
            ORDER BY event_id""",
            (run_id, phase, message),
        ).fetchall()
        for row in rows:
            try:
                existing = json.loads(str(row[0]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"run {run_id} has malformed {phase} lifecycle event"
                ) from exc
            if isinstance(existing, Mapping) and existing.get("idempotency_key") == idempotency_key:
                if str(row[0]) != details_json:
                    raise RuntimeError(
                        f"run {run_id} has a conflicting lifecycle event {idempotency_key}"
                    )
                return False
        self._connection.execute(
            """INSERT INTO run_events(
            run_id,occurred_ns,level,phase,message,details_json)
            VALUES(?,?,?,?,?,?)""",
            (run_id, time.time_ns(), level, phase, message, details_json),
        )
        return True

    def _ensure_run_budget_event(
        self,
        run_id: int,
        budget: Mapping[str, Any] | None,
        *,
        manifest_digest: str | None = None,
    ) -> bool:
        """Create the immutable budget baseline without changing the schema."""

        normalized = RunBudget.from_mapping(budget)
        rows = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-budget'
            AND message='Run budget initialized' ORDER BY event_id DESC LIMIT 2""",
            (run_id,),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"run {run_id} has duplicate lifecycle budgets")
        if rows:
            try:
                existing = json.loads(str(rows[0][0]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle budget is malformed") from exc
            if not isinstance(existing, Mapping):
                raise RuntimeError(f"run {run_id} lifecycle budget is not an object")
            for key, expected in normalized.payload().items():
                if existing.get(key) != expected:
                    raise RuntimeError(f"run {run_id} has a conflicting lifecycle budget")
            if manifest_digest is not None:
                existing_digest = existing.get("manifest_digest")
                if existing_digest not in {None, manifest_digest}:
                    raise RuntimeError(
                        f"run {run_id} lifecycle budget is bound to another manifest"
                    )
                if existing_digest is None:
                    self._append_lifecycle_event_once(
                        run_id,
                        level="info",
                        phase="lifecycle-budget",
                        message="Run budget bound",
                        idempotency_key="manifest-binding",
                        details={
                            "schema": RUN_BUDGET_SCHEMA,
                            "kind": "bound",
                            "manifest_digest": manifest_digest,
                        },
                    )
            return False

        now = time.time_ns()
        deadline_ns = (
            None
            if normalized.max_duration_seconds is None
            else now + int(float(normalized.max_duration_seconds) * 1_000_000_000)
        )
        details = {
            **normalized.payload(),
            "manifest_digest": manifest_digest,
            "started_ns": now,
            "deadline_ns": deadline_ns,
            "consumed_items": 0,
            "consumed_bytes": 0,
            "cancel_requested": False,
            "cancel_reason": None,
            "reservation_count": 0,
            "idempotency_key": "baseline",
        }
        self._connection.execute(
            """INSERT INTO run_events(
            run_id,occurred_ns,level,phase,message,details_json)
            VALUES(?,?,'info','lifecycle-budget','Run budget initialized',?)""",
            (run_id, now, json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
        )
        return True

    def _budget_rows(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-budget'
            ORDER BY event_id""",
            (run_id,),
        ).fetchall()
        parsed: list[dict[str, Any]] = []
        for event_id, details_json in rows:
            try:
                value = json.loads(str(details_json))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle budget is malformed") from exc
            if not isinstance(value, dict) or value.get("schema") != RUN_BUDGET_SCHEMA:
                raise RuntimeError(f"run {run_id} lifecycle budget schema is unsupported")
            if value.get("idempotency_key") is not None and (
                not isinstance(value.get("idempotency_key"), str)
                or not value["idempotency_key"]
                or len(value["idempotency_key"]) > 256
            ):
                raise RuntimeError(f"run {run_id} lifecycle budget idempotency key is invalid")
            value["event_id"] = int(event_id)
            parsed.append(value)
        return parsed

    def _read_run_budget_locked(self, run_id: int) -> dict[str, Any] | None:
        rows = self._budget_rows(run_id)
        if not rows:
            return None
        baseline = rows[0]
        try:
            normalized = RunBudget.from_mapping(baseline)
        except ValueError as exc:
            raise RuntimeError(f"run {run_id} lifecycle budget is invalid") from exc
        if baseline.get("kind") not in {None, "baseline"}:
            raise RuntimeError(f"run {run_id} lifecycle budget baseline is invalid")
        state: dict[str, Any] = {
            "schema": RUN_BUDGET_SCHEMA,
            "manifest_digest": baseline.get("manifest_digest"),
            "max_items": normalized.max_items,
            "max_bytes": normalized.max_bytes,
            "max_duration_seconds": normalized.max_duration_seconds,
            "started_ns": baseline.get("started_ns"),
            "deadline_ns": baseline.get("deadline_ns"),
            "consumed_items": int(baseline.get("consumed_items", 0)),
            "consumed_bytes": int(baseline.get("consumed_bytes", 0)),
            "cancel_requested": bool(baseline.get("cancel_requested", False)),
            "cancel_reason": baseline.get("cancel_reason"),
            "reservations": {},
            "last_event_id": int(baseline["event_id"]),
        }
        for event in rows[1:]:
            state["last_event_id"] = int(event["event_id"])
            kind = event.get("kind")
            if kind == "consumed":
                reservation_id = str(event.get("reservation_id", ""))
                if not reservation_id or len(reservation_id) > 256:
                    raise RuntimeError(f"run {run_id} lifecycle budget has invalid reservation")
                if reservation_id in state["reservations"]:
                    # A duplicate reservation is not a second effect.
                    continue
                raw_items = event.get("items", 0)
                raw_bytes = event.get("bytes", 0)
                if type(raw_items) is not int or raw_items < 0:
                    raise RuntimeError(f"run {run_id} lifecycle budget has invalid items")
                if type(raw_bytes) is not int or raw_bytes < 0:
                    raise RuntimeError(f"run {run_id} lifecycle budget has invalid bytes")
                items = raw_items
                byte_count = raw_bytes
                stage = event.get("stage")
                if stage is not None:
                    _bounded_lifecycle_name(stage, label="budget stage")
                state["consumed_items"] += items
                state["consumed_bytes"] += byte_count
                state["reservations"][reservation_id] = {
                    "items": items,
                    "bytes": byte_count,
                    "worker": event.get("worker"),
                    "stage": stage,
                    "event_id": int(event["event_id"]),
                }
            elif kind == "cancelled":
                reason = event.get("reason")
                if not isinstance(reason, str) or not reason or len(reason) > 512:
                    raise RuntimeError(f"run {run_id} lifecycle cancellation reason is invalid")
                state["cancel_requested"] = True
                state["cancel_reason"] = reason
            elif kind == "bound":
                digest = event.get("manifest_digest")
                if not isinstance(digest, str) or not digest.startswith("sha256:"):
                    raise RuntimeError(f"run {run_id} lifecycle budget binding is invalid")
                state["manifest_digest"] = digest
        now = time.time_ns()
        terminal = self._connection.execute(
            "SELECT completed_ns FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        completed_ns = None if terminal is None or terminal[0] is None else int(terminal[0])
        elapsed_until_ns = now if completed_ns is None else completed_ns
        deadline_ns = state["deadline_ns"]
        state["elapsed_ns"] = max(0, elapsed_until_ns - int(state["started_ns"]))
        state["elapsed_seconds"] = state["elapsed_ns"] / 1_000_000_000
        state["elapsed_until_ns"] = elapsed_until_ns
        state["elapsed_scope"] = (
            "budget_start_to_observation"
            if completed_ns is None
            else "budget_start_to_run_completion"
        )
        state["consumed_bytes_kind"] = "reserved_input_bytes_not_physical_io"
        state["expired"] = deadline_ns is not None and elapsed_until_ns >= int(deadline_ns)
        state["remaining_items"] = (
            None
            if state["max_items"] is None
            else max(0, int(state["max_items"]) - state["consumed_items"])
        )
        state["remaining_bytes"] = (
            None
            if state["max_bytes"] is None
            else max(0, int(state["max_bytes"]) - state["consumed_bytes"])
        )
        state["reservation_count"] = len(state["reservations"])
        return state

    def publish_run_budget(
        self,
        run_id: int,
        budget: Mapping[str, Any] | None = None,
        *,
        manifest_digest: str | None = None,
    ) -> bool:
        """Persist an immutable budget baseline, linked to the manifest."""

        with self._connection:
            return self._ensure_run_budget_event(
                run_id,
                budget,
                manifest_digest=manifest_digest,
            )

    def read_run_budget(self, run_id: int) -> dict[str, Any] | None:
        """Read the current budget snapshot from the writer owner."""

        return self._read_run_budget_locked(run_id)

    def read_run_stage_budget(self, run_id: int) -> dict[str, dict[str, int]]:
        """Return reserved item/byte totals grouped by lifecycle stage."""

        snapshot = self._read_run_budget_locked(run_id)
        if snapshot is None:
            return {}
        result: dict[str, dict[str, int]] = {}
        reservations = snapshot.get("reservations", {})
        if not isinstance(reservations, Mapping):
            raise RuntimeError(f"run {run_id} lifecycle budget reservations are invalid")
        for reservation in reservations.values():
            if not isinstance(reservation, Mapping):
                raise RuntimeError(f"run {run_id} lifecycle budget reservation is invalid")
            stage = reservation.get("stage")
            if stage is None:
                stage = "unattributed"
            stage = _bounded_lifecycle_name(stage, label="budget stage")
            bucket = result.setdefault(stage, {"items": 0, "bytes": 0, "reservations": 0})
            bucket["items"] += int(reservation.get("items", 0))
            bucket["bytes"] += int(reservation.get("bytes", 0))
            bucket["reservations"] += 1
        return result

    def run_budget(self, run_id: int) -> dict[str, Any] | None:
        """Compatibility alias for callers that treat budgets as run state."""

        return self.read_run_budget(run_id)

    def reserve_run_budget(
        self,
        run_id: int,
        reservation_id: str,
        *,
        items: int = 0,
        bytes: int = 0,
        worker: str | None = None,
        stage: str | None = None,
        item_count: int | None = None,
        byte_count: int | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve durable work for one worker.

        The reservation id is the effect boundary. Repeating it returns the
        same durable reservation and never increments the counters again.
        Reservations are deliberately not refunded after a worker failure: a
        retry must use a new run or an explicitly supported route replay.
        """

        if item_count is not None:
            if items != 0 and items != item_count:
                raise ValueError("items and item_count disagree")
            items = item_count
        if byte_count is not None:
            if bytes != 0 and bytes != byte_count:
                raise ValueError("bytes and byte_count disagree")
            bytes = byte_count
        if not reservation_id or len(reservation_id) > 256:
            raise ValueError("reservation_id must be non-empty and bounded")
        if stage is not None:
            stage = _bounded_lifecycle_name(stage, label="budget stage")
        if type(items) is not int or items < 0 or type(bytes) is not int or bytes < 0:
            raise ValueError("budget reservation items and bytes must be non-negative integers")
        with self._connection:
            run = self._connection.execute(
                "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"run {run_id} does not exist")
            snapshot = self._read_run_budget_locked(run_id)
            if snapshot is None:
                self._ensure_run_budget_event(run_id, None)
                snapshot = self._read_run_budget_locked(run_id)
            assert snapshot is not None
            existing = snapshot["reservations"].get(reservation_id)
            if existing is not None:
                if (
                    existing["items"] != items
                    or existing["bytes"] != bytes
                    or existing.get("stage") != stage
                ):
                    raise ValueError(f"run {run_id} reservation {reservation_id} conflicts")
                replay = dict(snapshot)
                replay["replayed"] = True
                replay["reservation"] = dict(existing)
                return replay
            if str(run[0]) != "running":
                raise RunBudgetExceeded("terminal", snapshot)
            reason = None
            if snapshot["cancel_requested"]:
                reason = "cancelled"
            elif snapshot["expired"]:
                reason = "time"
            elif (
                snapshot["max_items"] is not None
                and snapshot["consumed_items"] + items > int(snapshot["max_items"])
            ):
                reason = "items"
            elif (
                snapshot["max_bytes"] is not None
                and snapshot["consumed_bytes"] + bytes > int(snapshot["max_bytes"])
            ):
                reason = "bytes"
            if reason is not None:
                raise RunBudgetExceeded(reason, snapshot)
            details = {
                "schema": RUN_BUDGET_SCHEMA,
                "kind": "consumed",
                "manifest_digest": snapshot["manifest_digest"],
                "reservation_id": reservation_id,
                "worker": worker,
                "stage": stage,
                "items": items,
                "bytes": bytes,
                "consumed_items": snapshot["consumed_items"] + items,
                "consumed_bytes": snapshot["consumed_bytes"] + bytes,
                "idempotency_key": f"reservation:{reservation_id}",
            }
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','lifecycle-budget','Run budget consumed',?)""",
                (run_id, time.time_ns(), json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )
            result = self._read_run_budget_locked(run_id)
            assert result is not None
            result["replayed"] = False
            result["reservation"] = {
                "items": items,
                "bytes": bytes,
                "worker": worker,
                "stage": stage,
            }
            return result

    def consume_run_budget(
        self,
        run_id: int,
        reservation_id: str,
        *,
        items: int = 0,
        bytes: int = 0,
        worker: str | None = None,
        stage: str | None = None,
        item_count: int | None = None,
        byte_count: int | None = None,
    ) -> dict[str, Any]:
        """Record committed work using the same idempotent reservation ledger."""

        return self.reserve_run_budget(
            run_id,
            reservation_id,
            items=items,
            bytes=bytes,
            worker=worker,
            stage=stage,
            item_count=item_count,
            byte_count=byte_count,
        )

    def reserve_run_stage(
        self,
        run_id: int,
        stage: str,
        reservation_id: str,
        *,
        items: int = 0,
        bytes: int = 0,
        worker: str | None = None,
    ) -> dict[str, Any]:
        """Reserve shared lifecycle work while recording its owning stage."""

        return self.reserve_run_budget(
            run_id,
            reservation_id,
            items=items,
            bytes=bytes,
            worker=worker,
            stage=stage,
        )

    def consume_run_stage(
        self,
        run_id: int,
        stage: str,
        reservation_id: str,
        *,
        items: int = 0,
        bytes: int = 0,
        worker: str | None = None,
    ) -> dict[str, Any]:
        """Record shared lifecycle work while retaining stage provenance."""

        return self.consume_run_budget(
            run_id,
            reservation_id,
            items=items,
            bytes=bytes,
            worker=worker,
            stage=stage,
        )

    def request_run_cancellation(self, run_id: int, reason: str = "user") -> bool:
        """Persist cancellation once so a later process observes the request."""

        if not reason or len(reason) > 512:
            raise ValueError("cancellation reason must be non-empty and bounded")
        with self._connection:
            if self._connection.execute(
                "SELECT 1 FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"run {run_id} does not exist")
            snapshot = self._read_run_budget_locked(run_id)
            if snapshot is None:
                self._ensure_run_budget_event(run_id, None)
                snapshot = self._read_run_budget_locked(run_id)
            assert snapshot is not None
            if snapshot["cancel_requested"]:
                # A terminal budget/cancel path may be observed twice (for
                # example a worker notices the deadline and the outer
                # lifecycle then records termination).  The first durable
                # reason is authoritative; a later, more specific reason must
                # not strand the run in ``running`` while trying to append a
                # duplicate cancellation marker.
                return False
            details = {
                "schema": RUN_BUDGET_SCHEMA,
                "kind": "cancelled",
                "manifest_digest": snapshot["manifest_digest"],
                "reason": reason,
                "idempotency_key": "cancellation",
            }
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'warning','lifecycle-budget','Run cancellation requested',?)""",
                (run_id, time.time_ns(), json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )
            return True

    @staticmethod
    def request_run_cancellation_external(
        database: str | Path,
        run_id: int,
        reason: str = "user",
    ) -> bool:
        """Persist cancellation from a signal/UI thread without sharing SQLite objects."""

        if not reason or len(reason) > 512:
            raise ValueError("cancellation reason must be non-empty and bounded")
        connection = connect_existing_framework(
            Path(database), readonly=False, timeout_seconds=10
        )
        try:
            with connection:
                if connection.execute(
                    "SELECT 1 FROM initial_runs WHERE run_id=?", (run_id,)
                ).fetchone() is None:
                    raise ValueError(f"run {run_id} does not exist")
                row = connection.execute(
                    """SELECT details_json FROM run_events
                    WHERE run_id=? AND phase='lifecycle-budget'
                    AND message='Run budget initialized'
                    ORDER BY event_id DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                manifest_digest = None
                if row is not None and row[0] is not None:
                    try:
                        baseline = json.loads(str(row[0]))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(f"run {run_id} lifecycle budget is malformed") from exc
                    if isinstance(baseline, Mapping):
                        manifest_digest = baseline.get("manifest_digest")
                existing = connection.execute(
                    """SELECT details_json FROM run_events
                    WHERE run_id=? AND phase='lifecycle-budget'
                    AND message='Run cancellation requested'
                    ORDER BY event_id DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if existing is not None:
                    try:
                        existing_payload = json.loads(str(existing[0]))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            f"run {run_id} lifecycle cancellation is malformed"
                        ) from exc
                    # The first durable cancellation is authoritative.  A
                    # signal/UI caller may race the worker with a different
                    # reason; treating that retry as an idempotent no-op keeps
                    # the run terminalizable without appending contradictory
                    # lifecycle events.
                    if not isinstance(existing_payload, Mapping):
                        raise RuntimeError(
                            f"run {run_id} lifecycle cancellation is malformed"
                        )
                    return False
                if row is None:
                    now = time.time_ns()
                    baseline = {
                        **RunBudget().payload(),
                        "manifest_digest": None,
                        "started_ns": now,
                        "deadline_ns": None,
                        "consumed_items": 0,
                        "consumed_bytes": 0,
                        "cancel_requested": False,
                        "cancel_reason": None,
                        "reservation_count": 0,
                        "idempotency_key": "baseline",
                    }
                    connection.execute(
                        """INSERT INTO run_events(
                        run_id,occurred_ns,level,phase,message,details_json)
                        VALUES(?,?,'info','lifecycle-budget',
                        'Run budget initialized',?)""",
                        (
                            run_id,
                            now,
                            json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                details = {
                    "schema": RUN_BUDGET_SCHEMA,
                    "kind": "cancelled",
                    "manifest_digest": manifest_digest,
                    "reason": reason,
                    "idempotency_key": "cancellation",
                }
                connection.execute(
                    """INSERT INTO run_events(
                    run_id,occurred_ns,level,phase,message,details_json)
                    VALUES(?,?,'warning','lifecycle-budget',
                    'Run cancellation requested',?)""",
                    (
                        run_id,
                        time.time_ns(),
                        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                return True
        finally:
            connection.close()

    def run_cancellation_requested(self, run_id: int) -> bool:
        snapshot = self._read_run_budget_locked(run_id)
        return bool(snapshot and snapshot["cancel_requested"])

    def check_run_budget(self, run_id: int) -> dict[str, Any]:
        """Return a live snapshot or raise before a worker crosses its frontier."""

        snapshot = self._read_run_budget_locked(run_id)
        if snapshot is None:
            raise ValueError(f"run {run_id} has no durable lifecycle budget")
        if snapshot["elapsed_scope"] == "budget_start_to_run_completion":
            raise RunBudgetExceeded("terminal", snapshot)
        if snapshot["cancel_requested"]:
            raise RunBudgetExceeded("cancelled", snapshot)
        if snapshot["expired"]:
            raise RunBudgetExceeded("time", snapshot)
        return snapshot

    def _check_run_completion_budget_locked(self, run_id: int) -> dict[str, Any] | None:
        """Enforce the lifecycle deadline immediately before terminal publication.

        Route workers normally check the budget cooperatively while they run,
        but a final result can arrive between two polling intervals.  Keep the
        check inside the same writer transaction as the terminal status update
        so a run cannot become ``completed`` after its durable deadline.
        Legacy runs without a manifest/budget retain their historical behavior.
        """

        row = self._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or str(row[0]) != "running":
            return self._read_run_budget_locked(run_id)
        snapshot = self._read_run_budget_locked(run_id)
        if snapshot is None:
            return None
        if snapshot["cancel_requested"]:
            raise RunBudgetExceeded("cancelled", snapshot)
        if snapshot["expired"]:
            raise RunBudgetExceeded("time", snapshot)
        return snapshot

    def publish_run_manifest(self, run_id: int, manifest: Mapping[str, Any]) -> bool:
        """Publish one immutable lifecycle manifest idempotently as an event.

        The manifest is an append-only, schema-tagged event in the current
        Framework owner schema. Publishing it adds no separate schema
        migration; the run tables remain the authoritative lifecycle state.
        """

        verified = verify_event_payload(manifest)
        if int(verified["run_id"]) != run_id:
            raise ValueError(f"run manifest owner does not match run {run_id}")
        payload_json = json.dumps(verified, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            if self._connection.execute(
                "SELECT 1 FROM initial_runs WHERE run_id=?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"run {run_id} does not exist")
            rows = self._connection.execute(
                """SELECT details_json FROM run_events
                WHERE run_id=? AND phase='lifecycle-manifest'
                AND message='Run manifest published' ORDER BY event_id DESC LIMIT 2""",
                (run_id,),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError(f"run {run_id} has duplicate lifecycle manifests")
            if rows:
                existing = rows[0][0]
                if existing != payload_json:
                    raise RuntimeError(f"run {run_id} has a conflicting lifecycle manifest")
                self._ensure_run_budget_event(
                    run_id,
                    self._budget_configuration(verified),
                    manifest_digest=str(verified["digest"]),
                )
                return False
            self._connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,'info','lifecycle-manifest','Run manifest published',?)""",
                (run_id, time.time_ns(), payload_json),
            )
            self._ensure_run_budget_event(
                run_id,
                self._budget_configuration(verified),
                manifest_digest=str(verified["digest"]),
            )
        return True

    def read_run_manifest(self, run_id: int) -> dict[str, Any] | None:
        """Read and validate the immutable manifest for one run."""

        row = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-manifest'
            AND message='Run manifest published' ORDER BY event_id DESC LIMIT 2""",
            (run_id,),
        ).fetchall()
        if len(row) > 1:
            raise RuntimeError(f"run {run_id} has duplicate lifecycle manifests")
        if not row or row[0][0] is None:
            return None
        try:
            payload = json.loads(str(row[0][0]))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"run {run_id} lifecycle manifest is malformed") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"run {run_id} lifecycle manifest is not an object")
        return verify_event_payload(payload)

    def read_run_route_capabilities(self, run_id: int) -> dict[str, str]:
        """Return the immutable replay capability declared by each route."""

        manifest = self.read_run_manifest(run_id)
        if manifest is None:
            return {}
        value = manifest.get("route_capabilities", {})
        if not isinstance(value, Mapping):
            raise RuntimeError(f"run {run_id} route capabilities are invalid")
        capabilities = {str(name): str(capability) for name, capability in value.items()}
        if any(
            capability not in {"phase_resume", "safe_replay", "not_resumable"}
            for capability in capabilities.values()
        ):
            raise RuntimeError(f"run {run_id} route capability is unsupported")
        return capabilities

    def publish_run_stage(
        self,
        run_id: int,
        stage: str,
        status: str,
        *,
        details: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        checkpoint: Mapping[str, Any] | None = None,
    ) -> bool:
        """Append one bounded lifecycle stage transition idempotently.

        Stages are kept in the Framework owner as metadata only.  A stage may
        describe work performed by another owner (for example Semantic), but
        this method never opens or mutates that owner.  The run manifest digest
        is copied into every event so a reader can reject a stage detached
        from its immutable input boundary.
        """

        if not isinstance(stage, str) or not stage or len(stage) > 128:
            raise ValueError("lifecycle stage must be non-empty and bounded")
        if not isinstance(status, str) or status not in {
            "pending",
            "running",
            "completed",
            "partial",
            "failed",
            "interrupted",
            "skipped",
        }:
            raise ValueError("unsupported lifecycle stage status")
        selected_details: dict[str, Any] = {} if details is None else dict(details)
        encoded = json.dumps(
            selected_details,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise ValueError("lifecycle stage details exceed the durable limit")
        key = idempotency_key or f"{stage}:{status}"
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("lifecycle stage idempotency key is invalid")
        with self._connection:
            manifest = self.read_run_manifest(run_id)
            if manifest is None:
                raise ValueError(f"run {run_id} has no lifecycle manifest")
            latest_row = self._connection.execute(
                """SELECT details_json FROM run_events
                WHERE run_id=? AND phase='lifecycle-stage'
                AND message='Lifecycle stage transitioned'
                AND json_extract(details_json,'$.stage')=?
                ORDER BY event_id DESC LIMIT 1""",
                (run_id, stage),
            ).fetchone()
            if latest_row is not None and latest_row[0] is not None:
                try:
                    latest = json.loads(str(latest_row[0]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"run {run_id} lifecycle stage is malformed") from exc
                latest_status = latest.get("status") if isinstance(latest, Mapping) else None
                if latest_status in {"completed", "skipped"} and status != latest_status:
                    raise RuntimeError(
                        f"run {run_id} lifecycle stage {stage} is already {latest_status}"
                    )
            payload = {
                "schema": RUN_STAGE_SCHEMA,
                "run_id": run_id,
                "manifest_digest": manifest["digest"],
                "stage": stage,
                "status": status,
                "details": selected_details,
                "idempotency_key": key,
            }
            changed = self._append_lifecycle_event_once(
                run_id,
                level="error" if status == "failed" else "warning" if status in {"partial", "interrupted"} else "info",
                phase="lifecycle-stage",
                message="Lifecycle stage transitioned",
                idempotency_key=key,
                details=payload,
            )
            if checkpoint is not None:
                self._publish_run_checkpoint_locked(
                    run_id,
                    manifest,
                    stage,
                    checkpoint,
                    idempotency_key=f"{key}:checkpoint",
                )
            return changed

    @staticmethod
    def _validated_checkpoint(
        checkpoint: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bytes]:
        if not isinstance(checkpoint, Mapping):
            raise ValueError("lifecycle checkpoint must be an object")
        selected = dict(checkpoint)
        encoded = json.dumps(
            selected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise ValueError("lifecycle checkpoint exceeds the durable limit")
        return selected, encoded

    def _publish_run_checkpoint_locked(
        self,
        run_id: int,
        manifest: Mapping[str, Any],
        stage: str,
        checkpoint: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> bool:
        selected, _encoded = self._validated_checkpoint(checkpoint)
        return self._append_lifecycle_event_once(
            run_id,
            level="info",
            phase="lifecycle-checkpoint",
            message="Lifecycle checkpoint persisted",
            idempotency_key=idempotency_key,
            details={
                "schema": RUN_CHECKPOINT_SCHEMA,
                "run_id": run_id,
                "manifest_digest": manifest["digest"],
                "stage": stage,
                "checkpoint": selected,
                "idempotency_key": idempotency_key,
            },
        )

    def read_run_stages(self, run_id: int) -> tuple[dict[str, Any], ...]:
        """Read and validate bounded lifecycle stage events for one run."""

        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-stage'
            AND message='Lifecycle stage transitioned'
            ORDER BY event_id LIMIT 65""",
            (run_id,),
        ).fetchall()
        if len(rows) > 64:
            raise RuntimeError(f"run {run_id} has too many lifecycle stages")
        manifest = self.read_run_manifest(run_id)
        if manifest is None and rows:
            raise RuntimeError(f"run {run_id} lifecycle stages have no manifest")
        result: list[dict[str, Any]] = []
        for event_id, details_json in rows:
            try:
                payload = json.loads(str(details_json))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle stage is malformed") from exc
            if not isinstance(payload, dict) or payload.get("schema") != RUN_STAGE_SCHEMA:
                raise RuntimeError(f"run {run_id} lifecycle stage schema is unsupported")
            if payload.get("run_id") != run_id:
                raise RuntimeError(f"run {run_id} lifecycle stage has an invalid owner")
            if manifest is not None and payload.get("manifest_digest") != manifest.get("digest"):
                raise RuntimeError(f"run {run_id} lifecycle stage is detached from its manifest")
            if not isinstance(payload.get("details"), dict):
                raise RuntimeError(f"run {run_id} lifecycle stage details are invalid")
            payload["event_id"] = int(event_id)
            # Releases before the idempotency field was copied into the
            # public stage envelope still have a valid append-only event key
            # in the event identity.  Project a bounded legacy key so a
            # cancelled historical run remains readable; new writes always
            # persist the canonical key above.
            if not isinstance(payload.get("idempotency_key"), str):
                payload["idempotency_key"] = f"legacy:event:{int(event_id)}"
            result.append(payload)
        return tuple(result)

    def publish_run_checkpoint(
        self,
        run_id: int,
        stage: str,
        checkpoint: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> bool:
        """Persist one bounded checkpoint owned by a manifest-bound stage.

        Checkpoints are metadata only: they never open another owner and do
        not grant permission to resume.  The manifest digest is copied into
        the event so recovery can reject a checkpoint detached from the run
        boundary.  The event is append-only and idempotent by caller key.
        """

        stage = _bounded_lifecycle_name(stage, label="lifecycle stage")
        selected, encoded = self._validated_checkpoint(checkpoint)
        key = idempotency_key or f"{stage}:checkpoint:sha256:{hashlib.sha256(encoded).hexdigest()}"
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("lifecycle checkpoint idempotency key is invalid")
        with self._connection:
            manifest = self.read_run_manifest(run_id)
            if manifest is None:
                raise ValueError(f"run {run_id} has no lifecycle manifest")
            return self._publish_run_checkpoint_locked(
                run_id,
                manifest,
                stage,
                selected,
                idempotency_key=key,
            )

    def read_run_checkpoints(self, run_id: int) -> tuple[dict[str, Any], ...]:
        """Read and validate bounded checkpoints for one lifecycle run."""

        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='lifecycle-checkpoint'
            AND message='Lifecycle checkpoint persisted'
            ORDER BY event_id LIMIT 65""",
            (run_id,),
        ).fetchall()
        if len(rows) > 64:
            raise RuntimeError(f"run {run_id} has too many lifecycle checkpoints")
        manifest = self.read_run_manifest(run_id)
        if manifest is None and rows:
            raise RuntimeError(f"run {run_id} lifecycle checkpoints have no manifest")
        result: list[dict[str, Any]] = []
        for event_id, details_json in rows:
            try:
                payload = json.loads(str(details_json))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"run {run_id} lifecycle checkpoint is malformed") from exc
            if not isinstance(payload, dict) or payload.get("schema") != RUN_CHECKPOINT_SCHEMA:
                raise RuntimeError(f"run {run_id} lifecycle checkpoint schema is unsupported")
            if payload.get("run_id") != run_id:
                raise RuntimeError(f"run {run_id} lifecycle checkpoint has an invalid owner")
            if manifest is not None and payload.get("manifest_digest") != manifest.get("digest"):
                raise RuntimeError(f"run {run_id} lifecycle checkpoint is detached from its manifest")
            _bounded_lifecycle_name(payload.get("stage"), label="lifecycle stage")
            if not isinstance(payload.get("checkpoint"), dict):
                raise RuntimeError(f"run {run_id} lifecycle checkpoint details are invalid")
            payload["event_id"] = int(event_id)
            if not isinstance(payload.get("idempotency_key"), str):
                payload["idempotency_key"] = f"legacy:event:{int(event_id)}"
            result.append(payload)
        return tuple(result)

    def read_run_stage_state(self, run_id: int) -> dict[str, dict[str, Any]]:
        """Return the latest durable transition for each stage name."""

        latest: dict[str, dict[str, Any]] = {}
        for event in self.read_run_stages(run_id):
            latest[str(event["stage"])] = dict(event)
        return latest

    def _pending_organization_stages(self, run_id: int) -> tuple[str, ...]:
        latest = self.read_run_stage_state(run_id)
        return tuple(
            stage for stage in ("organization_plan", "organization_apply")
            if stage in latest and latest[stage].get("status") not in {"completed", "skipped"}
        )

    def _check_organization_completion_locked(self, run_id: int) -> None:
        """Keep durable organization obligations inside the terminal frontier."""

        row = self._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None or str(row[0]) != "running":
            return
        pending = self._pending_organization_stages(run_id)
        if pending:
            raise RuntimeError(
                f"run {run_id} cannot complete with pending organization stages: "
                + ", ".join(pending)
            )

    def require_operational_run(self, run_id: int) -> None:
        require_operational_identity(self._connection, "framework", run_id)

    def resumable_route_candidate_run_ids(self) -> tuple[int, ...]:
        """Return runs whose route inputs remain needed for recovery/replay."""

        rows = self._connection.execute(
            """SELECT DISTINCT run_id FROM route_runs
            WHERE status IN ('running','interrupted','failed','cancelled') AND run_id>?
            ORDER BY run_id""", (operational_identity_floor(self._connection, "framework"),)
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

__all__ = ["FrameworkStateRunsMixin"]
