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
from collections.abc import Sequence
from typing import Any, Callable

from neocortex.progress import ProgressEvent

from ..read.issues import route_issue_count
from .messages import decode_message, encode_message, progress_payload


# region [01] Protocol output and cancellation input

_OUTPUT_LOCK = threading.Lock()
_ACTIVE_PROGRESS_LOCK = threading.Lock()
_ACTIVE_PROGRESS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_MAX_ACTIVE_PROGRESS = 24
_HEARTBEAT_INTERVAL_SECONDS = 2.0


def _emit(message_type: str, **payload: Any) -> None:
    record = encode_message(message_type, **payload)
    with _OUTPUT_LOCK:
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
    _emit("progress", **_track_progress(event))


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
    return {
        "run_id": int(result.run_id),
        "files_checked": int(getattr(actions, "files_checked", 0)),
        "action_errors": int(getattr(actions, "errors", 0)),
        "route_errors": route_errors,
        "routes": list(route_results),
    }


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
) -> tuple[Any, Callable[[Any], bool], _WorkerHeartbeat]:
    from _04_Nucleo_Operativo.cli_config import framework_config_from_args
    from _04_Nucleo_Operativo.cli_parser import build_parser
    from _04_Nucleo_Operativo.cli_reporting import has_organization_errors
    from _04_Nucleo_Operativo.cli_validation import validate_arguments
    from _04_Nucleo_Operativo.orchestrator import FrameworkOrchestrator

    parsed = _parse_worker_arguments(arguments, build_parser, validate_arguments)
    config = framework_config_from_args(parsed)
    orchestrator = FrameworkOrchestrator(config, progress=_progress)
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
    )
    heartbeat = _WorkerHeartbeat()
    heartbeat.start()
    return orchestrator, has_organization_errors, heartbeat


def _completed_outcome(
    result: Any,
    has_organization_errors: Callable[[Any], bool],
) -> tuple[str, dict[str, Any], int]:
    terminal_payload = _summary_payload(result)
    organization_errors = has_organization_errors(result)
    action_errors = int(terminal_payload["action_errors"])
    exit_code = 2 if action_errors or organization_errors else 0
    route_errors = dict(terminal_payload["route_errors"])
    issue_count = action_errors + sum(int(value) for value in route_errors.values())
    issue_count += int(organization_errors)
    terminal_payload.update(
        organization_errors=organization_errors,
        issues=issue_count,
        completion_status=("completed_with_issues" if issue_count else "completed"),
        exit_code=exit_code,
    )
    return "completed", terminal_payload, exit_code


def _exception_outcome(exc: BaseException, stage: str) -> tuple[str, dict[str, Any], int]:
    return (
        "failed",
        {
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "stage": stage,
            "traceback": "".join(traceback.format_exception(exc))[-20_000:],
        },
        1,
    )


def run_worker(arguments: Sequence[str]) -> int:
    _reset_active_progress()
    stage = "preparation"
    heartbeat: _WorkerHeartbeat | None = None
    try:
        orchestrator, organization_check, heartbeat = _prepare_framework(arguments)
        stage = "execution"
        result = orchestrator.run()
    except KeyboardInterrupt:
        outcome = ("cancelled", {"detail": "Cancelación cooperativa completada"}, 130)
    except _WorkerUsageError as exc:
        outcome = (
            "failed",
            {"error_type": "InvalidArguments", "detail": str(exc), "stage": "preparation"},
            2,
        )
    except BaseException as exc:
        outcome = _exception_outcome(exc, stage)
    else:
        outcome = _completed_outcome(result, organization_check)
    finally:
        if heartbeat is not None:
            heartbeat.stop()

    terminal_type, terminal_payload, exit_code = outcome
    _emit(terminal_type, **terminal_payload)
    return exit_code


def main(arguments: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    return run_worker(sys.argv[1:] if arguments is None else arguments)


# endregion [02]


if __name__ == "__main__":
    raise SystemExit(main())
