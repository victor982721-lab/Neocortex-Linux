"""Bounded public CLI adapter for one durable ReviewTask decision journey."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from _04_Nucleo_Operativo.value_review_port import (
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskCASConflict,
    ReviewTaskRecord,
    ReviewTaskState,
    ReviewTaskTransition,
    append_review_task_event,
    read_review_task,
    read_review_task_event_by_key,
    read_review_task_history,
)

from .read_api import KnowledgeExitCode, ReadScope, scope_bindings


REVIEW_TASK_API_SCHEMA = "neocortex.review-task/v1"
_DECISION_SCOPES = frozenset({"until-source-change", "until-policy-change", "permanent"})
_STATE_ERRORS = (OSError, RuntimeError, TypeError, ValueError, sqlite3.DatabaseError)


def _binding(scope: str | ReadScope):
    selected = scope if isinstance(scope, ReadScope) else ReadScope(scope)
    if selected is ReadScope.ALL:
        raise ValueError("review task requires personal or framework scope")
    bindings = scope_bindings(selected)
    if len(bindings) != 1:
        raise RuntimeError("review task did not resolve exactly one scope")
    return selected, bindings[0]


def _record_dict(record: ReviewTaskRecord) -> dict[str, object]:
    task = record.task
    source = record.source
    event = record.current_event
    return {
        "task": task.to_dict(),
        "source": source.to_dict(),
        "source_snapshot_fingerprint": record.source_snapshot_fingerprint,
        "batch_id": record.batch_id,
        "selector_signature": record.selector_signature,
        "state": record.state.value,
        "current_event": event.to_dict(),
    }


def _selector_signature(record: ReviewTaskRecord) -> str:
    value = record.selector_signature
    if not isinstance(value, str) or not value:
        raise RuntimeError("review task record lacks its selector signature")
    return value


def _unavailable(
    operation: str,
    scope: str,
    task_id: str,
    error: BaseException,
) -> dict[str, object]:
    return {
        "schema": REVIEW_TASK_API_SCHEMA,
        "kind": "neocortex_review_task",
        "operation": operation,
        "scope": scope,
        "task_id": task_id,
        "status": "unavailable",
        "reason": str(error),
        "exit_code": int(KnowledgeExitCode.CORRUPT),
    }


def _idempotent_event_payload(
    database: Path,
    *,
    event_key: str,
    operation: str,
    scope: str,
    task_id: str,
    expected_event_id: str,
    to_state: ReviewTaskState,
    actor: str,
    note: str | None,
    decision: str | None = None,
    decision_scope: str | None = None,
) -> dict[str, object] | None:
    event = read_review_task_event_by_key(database, event_key)
    if event is None:
        return None
    decision_payload = None if event.decision is None else event.decision.to_dict()
    decision_keys = {
        "decision",
        "schema",
        "scope",
        "selector_signature",
        "source_input_fingerprint",
        "source_snapshot_fingerprint",
    }
    record = None if decision is None else read_review_task(database, task_id)
    if (
        event.task_id != task_id
        or event.previous_event_id != expected_event_id
        or event.to_state is not to_state
        or event.actor_kind is not ReviewTaskActorKind.HUMAN
        or event.actor_id != actor
        or event.note != note
        or (decision is None and decision_payload is not None)
        or (
            decision is not None
            and (
                decision_payload is None
                or set(decision_payload) != decision_keys
                or decision_payload.get("decision") != decision
                or decision_payload.get("scope") != decision_scope
                or decision_payload.get("schema") != "neocortex.review-task-decision/v1"
                or record is None
                or decision_payload.get("selector_signature") != record.selector_signature
                or decision_payload.get("source_input_fingerprint") != record.source.fingerprint
                or decision_payload.get("source_snapshot_fingerprint")
                != record.source_snapshot_fingerprint
            )
        )
    ):
        raise RuntimeError("review task idempotency key changed semantic command")
    return {
        "schema": REVIEW_TASK_API_SCHEMA,
        "kind": "neocortex_review_task",
        "operation": operation,
        "scope": scope,
        "task_id": task_id,
        "status": "complete",
        "idempotent": True,
        "event": event.to_dict(),
        "exit_code": int(KnowledgeExitCode.SUCCESS),
    }


def review_task_show_payload(
    task_id: str,
    scope: str | ReadScope,
) -> dict[str, object]:
    selected, binding = _binding(scope)
    database = binding.state_directory / "framework.sqlite3"
    try:
        record = read_review_task(database, task_id)
    except _STATE_ERRORS as exc:
        return {
            "schema": REVIEW_TASK_API_SCHEMA,
            "kind": "neocortex_review_task",
            "operation": "show",
            "scope": selected.value,
            "task_id": task_id,
            "status": "unavailable",
            "reason": str(exc),
            "exit_code": int(KnowledgeExitCode.CORRUPT),
        }
    return {
        "schema": REVIEW_TASK_API_SCHEMA,
        "kind": "neocortex_review_task",
        "operation": "show",
        "scope": selected.value,
        "task_id": task_id,
        "status": "not_found" if record is None else "ready",
        "record": None if record is None else _record_dict(record),
        "exit_code": int(
            KnowledgeExitCode.NO_RESULTS if record is None else KnowledgeExitCode.SUCCESS
        ),
    }


def review_task_decide_payload(
    task_id: str,
    scope: str | ReadScope,
    *,
    expected_event_id: str,
    decision: str,
    decision_scope: str,
    actor: str,
    note: str | None,
    clock_ns=time.time_ns,
) -> dict[str, object]:
    selected, binding = _binding(scope)
    if decision not in {"resolved", "dismissed"}:
        raise ValueError("decision must be resolved or dismissed")
    if decision_scope not in _DECISION_SCOPES:
        raise ValueError("decision_scope is invalid")
    database = binding.state_directory / "framework.sqlite3"
    identity_payload = json.dumps(
        {
            "actor": actor,
            "decision": decision,
            "decision_scope": decision_scope,
            "expected_event_id": expected_event_id,
            "note": note,
            "operation": "decide",
            "task_id": task_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
    event_key = f"review-task-event-key:human:{digest}"
    try:
        replay = _idempotent_event_payload(
            database,
            event_key=event_key,
            operation="decide",
            scope=selected.value,
            task_id=task_id,
            expected_event_id=expected_event_id,
            to_state=ReviewTaskState(decision),
            actor=actor,
            note=note,
            decision=decision,
            decision_scope=decision_scope,
        )
        if replay is not None:
            return replay
        current = read_review_task(database, task_id)
    except _STATE_ERRORS as exc:
        return _unavailable("decide", selected.value, task_id, exc)
    if current is None:
        return {
            "schema": REVIEW_TASK_API_SCHEMA,
            "kind": "neocortex_review_task",
            "operation": "decide",
            "scope": selected.value,
            "task_id": task_id,
            "status": "not_found",
            "exit_code": int(KnowledgeExitCode.NO_RESULTS),
        }
    if current.current_event.event_id != expected_event_id:
        return {
            "schema": REVIEW_TASK_API_SCHEMA,
            "kind": "neocortex_review_task",
            "operation": "decide",
            "scope": selected.value,
            "task_id": task_id,
            "status": "snapshot_changed",
            "reason": "review_task_expected_event_changed",
            "current_event_id": current.current_event.event_id,
            "exit_code": int(KnowledgeExitCode.SNAPSHOT_CHANGED),
        }
    now_ns = clock_ns()
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns <= 0:
        raise RuntimeError("review task clock returned an invalid timestamp")
    decision_payload = CanonicalJsonObject.from_mapping(
        {
            "decision": decision,
            "schema": "neocortex.review-task-decision/v1",
            "scope": decision_scope,
            "selector_signature": _selector_signature(current),
            "source_input_fingerprint": current.source.fingerprint,
            "source_snapshot_fingerprint": current.source_snapshot_fingerprint,
        }
    )
    transition = ReviewTaskTransition(
        event_id=f"review-task-event:human:{digest}",
        event_key=event_key,
        task_id=task_id,
        expected_event_id=expected_event_id,
        expected_state=current.state,
        to_state=ReviewTaskState(decision),
        actor_kind=ReviewTaskActorKind.HUMAN,
        actor_id=actor,
        provenance=CanonicalJsonObject.from_mapping(
            {
                "interface": "neocortex-cli",
                "operation": "review-task-decide",
                "schema": REVIEW_TASK_API_SCHEMA,
            }
        ),
        decision=decision_payload,
        note=note,
        observed_ns=now_ns,
        recorded_ns=now_ns,
    )
    try:
        result = append_review_task_event(database, transition)
    except _STATE_ERRORS as exc:
        try:
            replay = _idempotent_event_payload(
                database,
                event_key=event_key,
                operation="decide",
                scope=selected.value,
                task_id=task_id,
                expected_event_id=expected_event_id,
                to_state=ReviewTaskState(decision),
                actor=actor,
                note=note,
                decision=decision,
                decision_scope=decision_scope,
            )
        except _STATE_ERRORS:
            replay = None
        if replay is not None:
            return replay
        if isinstance(exc, ReviewTaskCASConflict):
            return {
                "schema": REVIEW_TASK_API_SCHEMA,
                "kind": "neocortex_review_task",
                "operation": "decide",
                "scope": selected.value,
                "task_id": task_id,
                "status": "snapshot_changed",
                "reason": str(exc),
                "exit_code": int(KnowledgeExitCode.SNAPSHOT_CHANGED),
            }
        return _unavailable("decide", selected.value, task_id, exc)
    return {
        "schema": REVIEW_TASK_API_SCHEMA,
        "kind": "neocortex_review_task",
        "operation": "decide",
        "scope": selected.value,
        "task_id": task_id,
        "status": "complete",
        "idempotent": result.idempotent,
        "event": result.event.to_dict(),
        "exit_code": int(KnowledgeExitCode.SUCCESS),
    }


def review_task_claim_payload(
    task_id: str,
    scope: str | ReadScope,
    *,
    expected_event_id: str,
    actor: str,
    note: str | None,
    clock_ns=time.time_ns,
) -> dict[str, object]:
    selected, binding = _binding(scope)
    database = binding.state_directory / "framework.sqlite3"
    identity_payload = json.dumps(
        {
            "actor": actor,
            "expected_event_id": expected_event_id,
            "note": note,
            "operation": "claim",
            "task_id": task_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
    event_key = f"review-task-event-key:human:{digest}"
    try:
        replay = _idempotent_event_payload(
            database,
            event_key=event_key,
            operation="claim",
            scope=selected.value,
            task_id=task_id,
            expected_event_id=expected_event_id,
            to_state=ReviewTaskState.IN_REVIEW,
            actor=actor,
            note=note,
        )
        if replay is not None:
            return replay
        current = read_review_task(database, task_id)
    except _STATE_ERRORS as exc:
        return _unavailable("claim", selected.value, task_id, exc)
    if current is None:
        return {
            "schema": REVIEW_TASK_API_SCHEMA,
            "kind": "neocortex_review_task",
            "operation": "claim",
            "scope": selected.value,
            "task_id": task_id,
            "status": "not_found",
            "exit_code": int(KnowledgeExitCode.NO_RESULTS),
        }
    if current.current_event.event_id != expected_event_id:
        return {
            "schema": REVIEW_TASK_API_SCHEMA,
            "kind": "neocortex_review_task",
            "operation": "claim",
            "scope": selected.value,
            "task_id": task_id,
            "status": "snapshot_changed",
            "reason": "review_task_expected_event_changed",
            "current_event_id": current.current_event.event_id,
            "exit_code": int(KnowledgeExitCode.SNAPSHOT_CHANGED),
        }
    now_ns = clock_ns()
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns <= 0:
        raise RuntimeError("review task clock returned an invalid timestamp")
    transition = ReviewTaskTransition(
        event_id=f"review-task-event:human:{digest}",
        event_key=event_key,
        task_id=task_id,
        expected_event_id=expected_event_id,
        expected_state=current.state,
        to_state=ReviewTaskState.IN_REVIEW,
        actor_kind=ReviewTaskActorKind.HUMAN,
        actor_id=actor,
        provenance=CanonicalJsonObject.from_mapping(
            {
                "interface": "neocortex-cli",
                "operation": "review-task-claim",
                "schema": REVIEW_TASK_API_SCHEMA,
            }
        ),
        decision=None,
        note=note,
        observed_ns=now_ns,
        recorded_ns=now_ns,
    )
    try:
        result = append_review_task_event(database, transition)
    except _STATE_ERRORS as exc:
        try:
            replay = _idempotent_event_payload(
                database,
                event_key=event_key,
                operation="claim",
                scope=selected.value,
                task_id=task_id,
                expected_event_id=expected_event_id,
                to_state=ReviewTaskState.IN_REVIEW,
                actor=actor,
                note=note,
            )
        except _STATE_ERRORS:
            replay = None
        if replay is not None:
            return replay
        if isinstance(exc, ReviewTaskCASConflict):
            return {
                "schema": REVIEW_TASK_API_SCHEMA,
                "kind": "neocortex_review_task",
                "operation": "claim",
                "scope": selected.value,
                "task_id": task_id,
                "status": "snapshot_changed",
                "reason": str(exc),
                "exit_code": int(KnowledgeExitCode.SNAPSHOT_CHANGED),
            }
        return _unavailable("claim", selected.value, task_id, exc)
    return {
        "schema": REVIEW_TASK_API_SCHEMA,
        "kind": "neocortex_review_task",
        "operation": "claim",
        "scope": selected.value,
        "task_id": task_id,
        "status": "complete",
        "idempotent": result.idempotent,
        "event": result.event.to_dict(),
        "exit_code": int(KnowledgeExitCode.SUCCESS),
    }


def review_task_history_payload(
    task_id: str,
    scope: str | ReadScope,
) -> dict[str, object]:
    selected, binding = _binding(scope)
    database = binding.state_directory / "framework.sqlite3"
    try:
        events = read_review_task_history(database, task_id)
    except _STATE_ERRORS as exc:
        return {
            "schema": REVIEW_TASK_API_SCHEMA,
            "kind": "neocortex_review_task",
            "operation": "history",
            "scope": selected.value,
            "task_id": task_id,
            "status": "unavailable",
            "reason": str(exc),
            "exit_code": int(KnowledgeExitCode.CORRUPT),
        }
    return {
        "schema": REVIEW_TASK_API_SCHEMA,
        "kind": "neocortex_review_task",
        "operation": "history",
        "scope": selected.value,
        "task_id": task_id,
        "status": "not_found" if not events else "ready",
        "events": [event.to_dict() for event in events],
        "exit_code": int(KnowledgeExitCode.NO_RESULTS if not events else KnowledgeExitCode.SUCCESS),
    }


def _print(value: str, *, file: TextIO | None = None) -> None:
    print(value, file=sys.stdout if file is None else file)


def _run(payload: Mapping[str, object], *, json_output: bool) -> int:
    if json_output:
        _print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        status = payload.get("status", "unknown")
        _print(f"ReviewTask {payload.get('task_id')}: {status}")
        record = payload.get("record")
        if isinstance(record, dict):
            task = record.get("task")
            event = record.get("current_event")
            if isinstance(task, dict):
                _print(f"  Motivo: {task.get('reason_code')} · prioridad {task.get('priority')}")
            if isinstance(event, dict):
                _print(f"  Evento actual: {event.get('event_id')} · {event.get('to_state')}")
        event = payload.get("event")
        if isinstance(event, dict):
            _print(f"  Evento publicado: {event.get('event_id')} · {event.get('to_state')}")
    value = payload.get("exit_code", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def run_review_task_show(*, task_id: str, scope: str, json_output: bool) -> int:
    return _run(review_task_show_payload(task_id, scope), json_output=json_output)


def run_review_task_history(*, task_id: str, scope: str, json_output: bool) -> int:
    return _run(review_task_history_payload(task_id, scope), json_output=json_output)


def run_review_task_claim(
    *,
    task_id: str,
    scope: str,
    expected_event_id: str,
    actor: str,
    note: str | None,
    json_output: bool,
) -> int:
    return _run(
        review_task_claim_payload(
            task_id,
            scope,
            expected_event_id=expected_event_id,
            actor=actor,
            note=note,
        ),
        json_output=json_output,
    )


def run_review_task_decide(
    *,
    task_id: str,
    scope: str,
    expected_event_id: str,
    decision: str,
    decision_scope: str,
    actor: str,
    note: str | None,
    json_output: bool,
) -> int:
    payload = review_task_decide_payload(
        task_id,
        scope,
        expected_event_id=expected_event_id,
        decision=decision,
        decision_scope=decision_scope,
        actor=actor,
        note=note,
    )
    return _run(payload, json_output=json_output)


__all__ = (
    "REVIEW_TASK_API_SCHEMA",
    "review_task_claim_payload",
    "review_task_decide_payload",
    "review_task_history_payload",
    "review_task_show_payload",
    "run_review_task_claim",
    "run_review_task_decide",
    "run_review_task_history",
    "run_review_task_show",
)
