"""Isolated framework worker speaking the NeoCortex UI line protocol."""

from __future__ import annotations

import _thread
import contextlib
import io
import multiprocessing
import os
import sys
import threading
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from neocortex.progress import ProgressEvent

from ..read.issues import route_issue_count
from .messages import (
    MAX_UNAVAILABLE_CAUSE_LENGTH,
    MAX_UNAVAILABLE_CAUSES,
    MAX_UNAVAILABLE_NAME_LENGTH,
    decode_message,
    encode_message,
    progress_payload,
    sanitize_text,
)
from ..application.request import (
    FULL_DEADLINE_SECONDS,
    FULL_MAX_ITEMS,
    PILOT_DEADLINE_SECONDS,
    PILOT_MAX_ITEMS,
    PROFILE_DEFAULTS,
)


# region [01] Protocol output and cancellation input

_OUTPUT_LOCK = threading.Lock()
_ACTIVE_PROGRESS_LOCK = threading.Lock()
_ACTIVE_PROGRESS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_MAX_ACTIVE_PROGRESS = 24
_HEARTBEAT_INTERVAL_SECONDS = 2.0
_WORKER_RUN_ID = ""
_MESSAGE_SEQUENCE = 0
_ACTIVE_BUDGET: _ExecutionBudget | None = None
_ACTIVE_SEMANTIC_STATE: _SemanticStageState | None = None


@dataclass(slots=True)
class _ExecutionBudget:
    """Bounded UI execution budget enforced independently of route defaults."""

    orchestrator: Any
    profile: str
    max_items: int
    deadline_seconds: float
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = None
    _cancel_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _cancelled: bool = False
    reason: str | None = None

    @classmethod
    def from_environment(cls, orchestrator: Any) -> _ExecutionBudget:
        profile = os.environ.get("NEOCORTEX_UI_PROFILE", "pilot")
        if profile not in PROFILE_DEFAULTS:
            raise ValueError("Invalid UI execution profile")
        default_items, default_deadline = PROFILE_DEFAULTS[profile]
        raw_items = os.environ.get("NEOCORTEX_UI_MAX_ITEMS")
        raw_deadline = os.environ.get("NEOCORTEX_UI_DEADLINE_SECONDS")
        try:
            max_items = default_items if raw_items is None else int(raw_items)
            deadline = default_deadline if raw_deadline is None else float(raw_deadline)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid UI execution budget") from exc
        max_allowed = PILOT_MAX_ITEMS if profile == "pilot" else FULL_MAX_ITEMS
        deadline_allowed = (
            PILOT_DEADLINE_SECONDS if profile == "pilot" else FULL_DEADLINE_SECONDS
        )
        if not 1 <= max_items <= max_allowed or not 0.001 <= deadline <= deadline_allowed:
            raise ValueError("UI execution budget is outside its bounded profile")
        return cls(orchestrator, profile, max_items, deadline)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._deadline_loop,
            name="neocortex-ui-budget",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(2.0, self.deadline_seconds + 0.5))

    def observe(self, payload: dict[str, Any]) -> None:
        total = payload.get("total")
        completed = payload.get("completed")
        if (
            (isinstance(total, int) and total > self.max_items)
            or (isinstance(completed, int) and completed > self.max_items)
        ):
            self.cancel("item_budget")

    def cancel(self, reason: str) -> None:
        with self._cancel_lock:
            if self._cancelled or self._stop.is_set():
                return
            self._cancelled = True
            self.reason = reason
        self.orchestrator.request_cancellation()
        _thread.interrupt_main()

    def _deadline_loop(self) -> None:
        if not self._stop.wait(self.deadline_seconds):
            self.cancel("time_budget")


@dataclass(slots=True)
class _SemanticStageState:
    """Bounded UI projection of the integrated Semantic lifecycle stage."""

    requested: bool = False
    started: bool = False
    status: str = "not_requested"
    exit_code: int | None = None
    recovery_required: bool = False
    coverage_partial: bool = False
    selected_sources: tuple[str, ...] = ()
    result_seen: bool = False
    unavailable: dict[str, str] = field(default_factory=dict)


def _semantic_stage_details(args: Any) -> dict[str, object]:
    """Use the same durable budget/selection contract as the CLI."""

    from neocortex.api.cli.cli_app import _semantic_stage_details as cli_stage_details

    return cli_stage_details(args)


def _semantic_result_is_complete(value: object) -> bool:
    """Keep the UI's partial marker aligned with the CLI stage readiness gate."""

    if getattr(value, "complete", False) is not True:
        return False
    for generation in getattr(value, "generations", ()):
        summary = getattr(generation, "summary", None)
        if (
            getattr(summary, "status", None) != "ready"
            or getattr(summary, "unfinished", 0) != 0
            or getattr(summary, "errors", 0) != 0
            or getattr(summary, "stale", 0) != 0
        ):
            return False
    return True


def _bounded_cause_mapping(value: object) -> dict[str, str]:
    """Project typed availability causes into a bounded UI-safe mapping."""

    if not isinstance(value, Mapping):
        return {}
    bounded: dict[str, str] = {}
    for raw_name, raw_cause in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_cause, str):
            continue
        name = sanitize_text(raw_name, limit=MAX_UNAVAILABLE_NAME_LENGTH)
        cause = sanitize_text(raw_cause, limit=MAX_UNAVAILABLE_CAUSE_LENGTH)
        if not name or not cause or name in bounded:
            continue
        bounded[name] = cause
        if len(bounded) >= MAX_UNAVAILABLE_CAUSES:
            break
    return bounded


def _bounded_scan_counter(value: object) -> int | None:
    """Keep inventory scan counters typed and within the protocol envelope."""

    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        return None
    return value


def _observe_semantic_progress(
    semantic_state: _SemanticStageState,
    event: ProgressEvent,
) -> None:
    """Capture bounded Semantic causes carried by the shared progress stream."""

    if event.operation != "semantic":
        return
    metrics = {metric.name: metric.value for metric in event.metrics}
    cause = metrics.get("cause")
    if cause is None:
        return
    scope = metrics.get("scope") or metrics.get("source") or metrics.get("owner") or "semantic"
    semantic_state.unavailable.update(
        _bounded_cause_mapping({scope: cause})
    )


def _reset_protocol_state() -> None:
    global _WORKER_RUN_ID, _MESSAGE_SEQUENCE
    worker_run_id = os.environ.get("NEOCORTEX_UI_RUN_ID", "").strip() or uuid4().hex
    if len(worker_run_id) > 128 or any(ord(character) < 0x20 for character in worker_run_id):
        raise ValueError("Invalid UI worker run identifier")
    _WORKER_RUN_ID = worker_run_id
    _MESSAGE_SEQUENCE = 0


def _emit(message_type: str, **payload: Any) -> None:
    global _MESSAGE_SEQUENCE
    with _OUTPUT_LOCK:
        _MESSAGE_SEQUENCE += 1
        record = encode_message(
            message_type,
            worker_run_id=_WORKER_RUN_ID or "standalone",
            sequence=_MESSAGE_SEQUENCE,
            **payload,
        )
        sys.stdout.buffer.write(record)
        sys.stdout.buffer.flush()


def _track_progress(event: ProgressEvent) -> dict[str, Any]:
    """Keep a bounded snapshot of unfinished work for UI heartbeats."""

    payload = progress_payload(event)
    key = event.key
    with _ACTIVE_PROGRESS_LOCK:
        if event.finished:
            _ACTIVE_PROGRESS.pop(key, None)
        else:
            _ACTIVE_PROGRESS.pop(key, None)
            _ACTIVE_PROGRESS[key] = (time.monotonic(), payload)
            while len(_ACTIVE_PROGRESS) > _MAX_ACTIVE_PROGRESS:
                oldest = next(iter(_ACTIVE_PROGRESS))
                _ACTIVE_PROGRESS.pop(oldest)
    return payload


def _active_progress_snapshot() -> list[dict[str, Any]]:
    with _ACTIVE_PROGRESS_LOCK:
        ordered = sorted(_ACTIVE_PROGRESS.values(), key=lambda item: item[0])
        return [dict(payload) for _updated_at, payload in ordered]


def _reset_active_progress() -> None:
    with _ACTIVE_PROGRESS_LOCK:
        _ACTIVE_PROGRESS.clear()


def _progress(event: ProgressEvent) -> None:
    payload = _track_progress(event)
    budget = _ACTIVE_BUDGET
    if budget is not None:
        budget.observe(payload)
    _emit("progress", **payload)


def _emit_heartbeats(stop: threading.Event, started_at: float) -> None:
    """Report liveness without growing the UI log or retaining unbounded state."""

    while not stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
        _emit(
            "heartbeat",
            elapsed_seconds=int(time.monotonic() - started_at),
            active=_active_progress_snapshot(),
        )


def _listen_for_commands(orchestrator) -> None:
    cancel_requested = False
    pending = bytearray()
    descriptor = sys.stdin.fileno()
    chunk = _read_command_chunk(descriptor)
    while chunk:
        pending.extend(chunk)
        for raw_line in _complete_command_lines(pending):
            if _is_cancel_command(raw_line) and not cancel_requested:
                cancel_requested = True
                _acknowledge_cancellation(orchestrator)
        chunk = _read_command_chunk(descriptor)


def _read_command_chunk(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, 4096)
    except OSError:
        return b""


def _complete_command_lines(pending: bytearray) -> list[bytes]:
    lines: list[bytes] = []
    newline = pending.find(b"\n")
    while newline >= 0:
        lines.append(bytes(pending[: newline + 1]))
        del pending[: newline + 1]
        newline = pending.find(b"\n")
    return lines


def _is_cancel_command(raw_line: bytes) -> bool:
    try:
        record = decode_message(raw_line)
    except (ValueError, TypeError):
        return False
    return bool(
        record is not None and record.get("type") == "command" and record.get("command") == "cancel"
    )


def _acknowledge_cancellation(orchestrator: Any) -> None:
    orchestrator.request_cancellation()
    _emit("cancel_acknowledged")
    _thread.interrupt_main()


# endregion [01]


# region [02] Framework execution


class _WorkerUsageError(ValueError):
    """Invalid CLI input translated into one structured terminal record."""


def _argument_error_detail(exc: SystemExit, diagnostics: str = "") -> str:
    lines = [line.strip() for line in diagnostics.splitlines() if line.strip()]
    if lines:
        detail = lines[-1]
        marker = "error: "
        if marker in detail:
            detail = detail.split(marker, 1)[1]
        return detail
    value = str(exc).strip()
    return value if value and value not in {"0", "1", "2"} else "Argumentos no válidos"


def _summary_payload(result) -> dict[str, Any]:
    actions = getattr(result, "actions", None)
    route_results = getattr(result, "route_results", {})
    route_errors = {name: route_issue_count(summary) for name, summary in route_results.items()}
    raw_route_failures = getattr(result, "route_failures", {})
    route_unavailable = _bounded_cause_mapping(raw_route_failures)
    for name in raw_route_failures if isinstance(raw_route_failures, Mapping) else ():
        route_errors[name] = max(1, route_errors.get(name, 0))
    payload = {
        "run_id": int(result.run_id),
        "files_checked": int(getattr(actions, "files_checked", 0)),
        "action_errors": int(getattr(actions, "errors", 0)),
        "route_errors": route_errors,
        "routes": list(dict.fromkeys((*route_results, *getattr(result, "route_failures", {})))),
    }
    scan = getattr(result, "scan", None)
    for counter_name in ("excluded_directories", "skipped_links"):
        value = _bounded_scan_counter(getattr(scan, counter_name, None))
        if value is not None:
            payload[counter_name] = value
    if route_unavailable:
        payload["route_unavailable"] = route_unavailable
    return payload


class _WorkerHeartbeat:
    """Own the non-daemon heartbeat thread and its bounded shutdown."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_emit_heartbeats,
            args=(self._stop, time.monotonic()),
            name="neocortex-ui-heartbeat",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=_HEARTBEAT_INTERVAL_SECONDS + 1.0)


def _parse_worker_arguments(
    arguments: Sequence[str],
    build_parser: Callable[[], Any],
    validate_arguments: Callable[[Any], None],
) -> Any:
    diagnostics = io.StringIO()
    try:
        with contextlib.redirect_stderr(diagnostics):
            parsed = build_parser().parse_args(list(arguments))
    except SystemExit as exc:
        raise _WorkerUsageError(_argument_error_detail(exc, diagnostics.getvalue())) from None
    try:
        validate_arguments(parsed)
    except SystemExit as exc:
        raise _WorkerUsageError(_argument_error_detail(exc)) from None
    return parsed


def _prepare_framework(
    arguments: Sequence[str],
) -> tuple[Any, Callable[[Any], bool], _WorkerHeartbeat, _ExecutionBudget]:
    global _ACTIVE_SEMANTIC_STATE
    _ACTIVE_SEMANTIC_STATE = None
    from neocortex.api.cli.cli_config import framework_config_from_args
    from neocortex.api.cli.cli_parser import build_parser
    from neocortex.api.cli.cli_reporting import has_organization_errors
    from neocortex.api.cli.cli_validation import validate_arguments
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

    parsed = _parse_worker_arguments(arguments, build_parser, validate_arguments)
    config = framework_config_from_args(parsed)
    semantic_state = _SemanticStageState(requested=bool(getattr(parsed, "all", False)))
    lifecycle_stage_runner = None
    lifecycle_stage_details = None
    if semantic_state.requested:
        from neocortex.api.cli.cli_semantic import run_integrated_all_semantic_index

        lifecycle_stage_details = _semantic_stage_details(parsed)
        semantic_state.selected_sources = tuple(
            getattr(parsed, "semantic_source", None) or ()
        )

        def capture_semantic_result(scope: str, value: object) -> None:
            del scope
            semantic_state.result_seen = True
            raw_sources = getattr(value, "sources", ())
            if isinstance(raw_sources, (list, tuple)):
                observed_sources = list(semantic_state.selected_sources)
                for source in raw_sources[:32]:
                    if (
                        isinstance(source, str)
                        and source.strip()
                        and source not in observed_sources
                    ):
                        observed_sources.append(source)
                semantic_state.selected_sources = tuple(observed_sources[:32])
            if not _semantic_result_is_complete(value):
                semantic_state.coverage_partial = True

        def emit_semantic_progress(event: ProgressEvent) -> None:
            _observe_semantic_progress(semantic_state, event)
            _progress(event)

        def run_semantic_stage(run_id: int) -> object:
            semantic_state.started = True
            semantic_state.status = "running"
            semantic_state.exit_code = run_integrated_all_semantic_index(
                parsed,
                progress=emit_semantic_progress,
                result_sink=capture_semantic_result,
                print_output=False,
                run_id=run_id,
                framework_lock_held=True,
            )
            for attribute in (
                "_semantic_scope_unavailable",
                "_semantic_source_unavailable",
                "_semantic_unavailable",
            ):
                semantic_state.unavailable.update(
                    _bounded_cause_mapping(getattr(parsed, attribute, {}))
                )
            if semantic_state.exit_code != 0:
                semantic_state.status = "failed"
                semantic_state.recovery_required = True
            elif semantic_state.coverage_partial:
                semantic_state.status = "partial"
            elif semantic_state.result_seen:
                semantic_state.status = "completed"
            else:
                semantic_state.status = "skipped"
            return semantic_state.exit_code

        lifecycle_stage_runner = run_semantic_stage

    orchestrator = FrameworkOrchestrator(
        config,
        progress=_progress,
        lifecycle_stage_runner=lifecycle_stage_runner,
        lifecycle_stage_details=lifecycle_stage_details,
    )
    parsed._semantic_cancellation_check = lambda: orchestrator._cancellation.is_cancelled
    # Keep the private projection in this one-process worker context rather
    # than widening the helper's long-standing return tuple.
    _ACTIVE_SEMANTIC_STATE = semantic_state
    budget = _ExecutionBudget.from_environment(orchestrator)
    global _ACTIVE_BUDGET
    _ACTIVE_BUDGET = budget
    command_thread = threading.Thread(
        target=_listen_for_commands,
        args=(orchestrator,),
        name="neocortex-ui-command-listener",
        daemon=True,
    )
    command_thread.start()
    _emit(
        "started",
        root=str(config.root),
        state_directory=str(config.state_directory),
        apply=config.apply_actions,
        route=config.route,
        request_id=_WORKER_RUN_ID,
        profile=budget.profile,
        max_items=budget.max_items,
        deadline_seconds=budget.deadline_seconds,
    )
    heartbeat = _WorkerHeartbeat()
    heartbeat.start()
    budget.start()
    try:
        if bool(getattr(parsed, "all", False)) or getattr(parsed, "resume_run", None) is not None:
            from neocortex.api.cli.cli_semantic import prepare_integrated_semantic_start

            prepare_integrated_semantic_start(parsed, progress=_progress, print_output=False)
            orchestrator.config = framework_config_from_args(parsed)
            if semantic_state.requested:
                # The dispatcher may charge fresh-start metadata preflight
                # against an explicit time cap before this lifecycle stage is
                # published. Keep the durable stage details aligned with the
                # effective arguments that the orchestrator will execute.
                effective_stage_details = _semantic_stage_details(parsed)
                assert lifecycle_stage_details is not None
                lifecycle_stage_details.clear()
                lifecycle_stage_details.update(effective_stage_details)
                orchestrator._lifecycle_stage_details = dict(lifecycle_stage_details)
    except BaseException:
        heartbeat.stop()
        budget.stop()
        raise
    return orchestrator, has_organization_errors, heartbeat, budget


def _completed_outcome(
    result: Any,
    has_organization_errors: Callable[[Any], bool],
    *,
    semantic_state: _SemanticStageState | None = None,
) -> tuple[str, dict[str, Any], int]:
    terminal_payload = _summary_payload(result)
    organization_errors = has_organization_errors(result)
    action_errors = int(terminal_payload["action_errors"])
    semantic_exit_code = (
        0
        if semantic_state is None or semantic_state.exit_code is None
        else int(semantic_state.exit_code)
    )
    from neocortex.api.cli.cli_reporting import has_strict_route_errors

    strict_failures = bool(semantic_state and semantic_state.requested and has_strict_route_errors(result))
    exit_code = 2 if (
        action_errors or organization_errors or semantic_exit_code != 0
        or getattr(result, "route_failures", None) or strict_failures
    ) else 0
    route_errors = dict(terminal_payload["route_errors"])
    issue_count = action_errors + sum(int(value) for value in route_errors.values())
    issue_count += int(organization_errors)
    issue_count += int(semantic_exit_code != 0)
    terminal_payload.update(
        organization_errors=organization_errors,
        issues=issue_count,
        completion_status=("completed_with_issues" if issue_count else "completed"),
        exit_code=exit_code,
    )
    if semantic_state is not None:
        terminal_payload.update(
            semantic_status=semantic_state.status,
            semantic_exit_code=semantic_exit_code,
            semantic_recovery_required=semantic_state.recovery_required,
            semantic_selected_sources=list(semantic_state.selected_sources[:32]),
        )
        if semantic_state.unavailable:
            terminal_payload["semantic_unavailable"] = _bounded_cause_mapping(
                semantic_state.unavailable
            )
    return "completed", terminal_payload, exit_code


def _exception_outcome(exc: BaseException, stage: str) -> tuple[str, dict[str, Any], int]:
    from neocortex.persistence.state_publication import StatePublicationError
    from neocortex.persistence.framework_state_writer import RunBudgetExceeded

    if isinstance(exc, (StatePublicationError, RunBudgetExceeded)):
        return (
            "failed",
            {
                "error_type": type(exc).__name__,
                "error_code": "recovery_required" if isinstance(exc, StatePublicationError) else "budget_exhausted",
                "detail": sanitize_text(exc),
                "stage": sanitize_text(stage, limit=256),
                "traceback": "",
            },
            2,
        )
    return (
        "failed",
        {
            "error_type": type(exc).__name__,
            "detail": sanitize_text(exc),
            "stage": sanitize_text(stage, limit=256),
            "traceback": sanitize_text(
                "".join(traceback.format_exception(exc)),
                limit=20_000,
            ),
        },
        1,
    )


def run_worker(arguments: Sequence[str]) -> int:
    global _ACTIVE_SEMANTIC_STATE
    _reset_active_progress()
    _ACTIVE_SEMANTIC_STATE = None
    stage = "preparation"
    heartbeat: _WorkerHeartbeat | None = None
    budget: _ExecutionBudget | None = None
    semantic_state: _SemanticStageState | None = None
    budget_reason: str | None = None
    try:
        _reset_protocol_state()
        orchestrator, organization_check, heartbeat, budget = _prepare_framework(arguments)
        semantic_state = _ACTIVE_SEMANTIC_STATE
        stage = "execution"
        result = orchestrator.run()
    except KeyboardInterrupt:
        budget_reason = None if budget is None else budget.reason
        if semantic_state is not None and semantic_state.started:
            semantic_state.status = "interrupted"
            semantic_state.exit_code = 130
            semantic_state.recovery_required = True
        detail = (
            "Presupuesto de elementos agotado"
            if budget_reason == "item_budget"
            else "Presupuesto de tiempo agotado"
            if budget_reason == "time_budget"
            else "Cancelación cooperativa completada"
        )
        outcome = ("cancelled", {"detail": detail}, 130)
    except _WorkerUsageError as exc:
        outcome = (
            "failed",
            {"error_type": "InvalidArguments", "detail": str(exc), "stage": "preparation"},
            2,
        )
    except BaseException as exc:
        if semantic_state is not None and semantic_state.started:
            semantic_state.status = "failed"
            semantic_state.recovery_required = True
        outcome = _exception_outcome(exc, stage)
    else:
        outcome = _completed_outcome(
            result,
            organization_check,
            semantic_state=semantic_state,
        )
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if budget is not None:
            budget.stop()
        global _ACTIVE_BUDGET
        _ACTIVE_BUDGET = None
        _ACTIVE_SEMANTIC_STATE = None

    terminal_type, terminal_payload, exit_code = outcome
    _emit(terminal_type, **terminal_payload)
    return exit_code


def main(arguments: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    return run_worker(sys.argv[1:] if arguments is None else arguments)


# endregion [02]


if __name__ == "__main__":
    raise SystemExit(main())
