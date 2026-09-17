"""CLI adapter for the public external-agent activity lifecycle.

The parser integration is intentionally a tiny opt-in registration function;
the root CLI owns the flat parser and dispatches ``run_agent_activity`` before
the Framework route graph.  This module contains no shell fallback and never
accepts a free-form command string.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from neocortex.api.agent_activity import (
    AGENT_ACTIVITY_CLI_SCHEMA,
    DEFAULT_AGENT_OWNER,
    AgentActivity,
    AgentActivityError,
)

__all__ = [
    "AGENT_ACTIVITY_CLI_SCHEMA",
    "register_agent_activity_arguments",
    "run_agent_activity",
]


_ACTIONS = ("prepare", "status", "run", "publish", "complete", "reconcile", "retire", "fail")


def register_agent_activity_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the public activity flags to the existing flat parser."""

    group = parser.add_argument_group("External agent activity lifecycle")
    group.add_argument(
        "--agent-activity-id",
        metavar="ID",
        help="stable external activity identity used for resume/reconcile",
    )
    group.add_argument(
        "--agent-owner",
        default=DEFAULT_AGENT_OWNER,
        metavar="OWNER",
        help=f"explicit activity owner (default: {DEFAULT_AGENT_OWNER})",
    )
    group.add_argument("--agent-run-id", metavar="ID")
    group.add_argument(
        "--agent-action",
        choices=_ACTIONS,
        default="status",
        help="activity operation (default: status)",
    )
    group.add_argument(
        "--agent-command",
        nargs=argparse.REMAINDER,
        metavar="ARGV",
        help=(
            "argv-only external producer for --agent-action run (no shell); "
            "put --agent-json before this option"
        ),
    )
    group.add_argument("--agent-source", type=Path, metavar="PATH")
    group.add_argument("--agent-destination", type=Path, metavar="PATH")
    group.add_argument("--agent-deliverable-id", metavar="ID")
    group.add_argument("--agent-result", action="append", type=Path, metavar="PATH")
    group.add_argument(
        "--agent-reconcile-action",
        choices=("resume", "publish", "complete", "retire", "fail", "release"),
        default="resume",
    )
    group.add_argument("--agent-failure-reason", default="external activity failed")
    group.add_argument(
        "--agent-release-authorized",
        action="store_true",
        help="explicitly authorize terminal reconciliation of a failed activity",
    )
    group.add_argument(
        "--agent-recovery-resolved",
        action="store_true",
        help="record explicit recovery evidence when releasing a recovery-required activity",
    )
    group.add_argument(
        "--agent-publication-resolved",
        action="store_true",
        help="record explicit publication reconciliation evidence",
    )
    group.add_argument(
        "--agent-json",
        action="store_true",
        help="emit one bounded JSON envelope",
    )


def _value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def _emit(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return
    result = payload.get("result")
    if isinstance(result, dict):
        status = result.get("state", result.get("status", payload.get("status", "ok")))
    else:
        status = payload.get("status", "ok")
    print(f"agent-activity status={status}")


def _require_id(args: argparse.Namespace) -> str:
    value = getattr(args, "agent_activity_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--agent-activity-id is required")
    return value


def _open_or_prepare(args: argparse.Namespace) -> AgentActivity:
    state = getattr(args, "state_directory", None)
    if not isinstance(state, (Path, str)):
        raise ValueError("state directory is required")
    activity_id = _require_id(args)
    owner = getattr(args, "agent_owner", DEFAULT_AGENT_OWNER)
    action = getattr(args, "agent_action", "status")
    if action == "prepare":
        return AgentActivity.prepare(state, activity_id, owner=owner, run_id=getattr(args, "agent_run_id", None))
    return AgentActivity.resume(state, activity_id, owner=owner)


def run_agent_activity(args: argparse.Namespace) -> int:
    """Dispatch one public lifecycle action and return a process exit code."""

    action = getattr(args, "agent_action", "status")
    json_output = bool(getattr(args, "agent_json", False) or getattr(args, "json_output", False))
    try:
        activity = _open_or_prepare(args)
        if action == "status" or action == "prepare":
            result: object = activity.snapshot()
        elif action == "run":
            command = getattr(args, "agent_command", None)
            if not command:
                raise ValueError("--agent-command is required for --agent-action run")
            result = activity.run(command, check=False)
            if result.returncode != 0:
                # A non-zero external producer is a durable activity failure,
                # not a successful CLI operation.  Keep the failed-retained
                # workspace for a fresh process to reconcile and expose the
                # failure with a non-zero command status.
                activity.reconcile(
                    "fail",
                    reason=f"external process returned {result.returncode}",
                )
                payload = {
                    "schema": AGENT_ACTIVITY_CLI_SCHEMA,
                    "operation": "agent_activity",
                    "status": "failed",
                    "code": "AgentActivityProcessError",
                    "reason": f"external process returned {result.returncode}",
                    "exit_code": 2,
                    "result": result,
                }
                _emit(payload, json_output=json_output)
                return 2
        elif action == "publish":
            source = getattr(args, "agent_source", None)
            destination = getattr(args, "agent_destination", None)
            if not isinstance(source, (Path, str)) or not isinstance(destination, (Path, str)):
                raise ValueError("publish requires --agent-source and --agent-destination")
            result = activity.publish(
                source,
                destination,
                deliverable_id=getattr(args, "agent_deliverable_id", None),
            )
        elif action == "complete":
            result = activity.close(getattr(args, "agent_result", None) or ())
        elif action == "retire":
            result = activity.retire()
        elif action == "fail":
            result = activity.reconcile("fail", reason=getattr(args, "agent_failure_reason", "external activity failed"))
        elif action == "reconcile":
            reconcile_action = getattr(args, "agent_reconcile_action", "resume")
            evidence: dict[str, object] = {}
            if getattr(args, "agent_recovery_resolved", False):
                evidence["recovered"] = True
            if getattr(args, "agent_publication_resolved", False):
                evidence["publication_resolved"] = True
            result = activity.reconcile(
                reconcile_action,
                source=getattr(args, "agent_source", None),
                destination=getattr(args, "agent_destination", None),
                deliverable_id=getattr(args, "agent_deliverable_id", None),
                result_paths=getattr(args, "agent_result", None) or (),
                reason=getattr(args, "agent_failure_reason", "external activity failed"),
                release_authorized=getattr(args, "agent_release_authorized", False),
                evidence=evidence,
            )
        else:  # pragma: no cover - argparse choices protect this branch
            raise ValueError(f"unsupported agent activity action: {action}")
        payload = {
            "schema": AGENT_ACTIVITY_CLI_SCHEMA,
            "operation": "agent_activity",
            "status": "ok",
            "read_only": action in {"status", "reconcile"} and getattr(args, "agent_reconcile_action", "resume") == "resume",
            "result": result,
        }
        _emit(payload, json_output=json_output)
        return 0
    except KeyboardInterrupt:
        raise
    except (AgentActivityError, OSError, ValueError) as exc:
        payload = {
            "schema": AGENT_ACTIVITY_CLI_SCHEMA,
            "operation": "agent_activity",
            "status": "blocked" if isinstance(exc, AgentActivityError) else "failed",
            "code": type(exc).__name__,
            "reason": str(exc),
            "exit_code": 2,
        }
        _emit(payload, json_output=json_output)
        return 2
