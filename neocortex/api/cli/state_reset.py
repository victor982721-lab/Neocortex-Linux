"""CLI adapter for the selectable NeoCortex state reset.

The public API facade owns validation, plan/digest binding and the persistence
engine call.  This leaf only maps argparse's namespace to that facade and
renders its bounded receipt.  In particular, no reset is attempted unless the
caller explicitly supplies ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping


STATE_RESET_CONFIRMATION = "RESET_STATE"
STATE_RESET_SCHEMA = "neocortex.state-reset/v1"
STATE_RESET_SCOPES = ("runs", "runs-and-caches", "all")


def _exit_code(payload: Mapping[str, object]) -> int:
    value = payload.get("exit_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _fallback_error(*, args: argparse.Namespace, error: BaseException) -> dict[str, object]:
    """Keep an import/adapter failure in the same public envelope."""

    scope = getattr(args, "scope", "-")
    if not isinstance(scope, str):
        scope = "-"
    return {
        "schema": STATE_RESET_SCHEMA,
        "kind": "state-reset",
        "operation": "state-reset",
        "request_id": "state-reset-cli-error",
        "scope": scope,
        "read_only": not bool(getattr(args, "apply", False)),
        "status": "error",
        "exit_code": 1,
        "error": {
            "code": type(error).__name__,
            "type": type(error).__name__,
            "message": str(error)[:1_000],
            "retryable": True,
        },
        "result": {},
    }


def _render(payload: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return

    result = payload.get("result")
    details = result if isinstance(result, Mapping) else {}
    scope = payload.get("scope", details.get("scope", "-"))
    mode = details.get("mode", "preview")
    print(
        "STATE_RESET "
        f"mode={mode} status={payload.get('status', '-')} scope={scope} "
        f"state={details.get('state_directory', '-')} "
        f"plan={details.get('plan_digest', '-')}"
    )
    if details.get("backup_directory"):
        print(f"STATE_RESET_BACKUP directory={details.get('backup_directory')}")
    if details.get("manifest"):
        print(
            f"STATE_RESET_MANIFEST path={details.get('manifest')} "
            f"sha256={details.get('manifest_sha256', '-')}"
        )
    for key in ("run_count", "cache_count", "database_count", "file_count"):
        if key in details:
            print(f"STATE_RESET_{key.upper()} value={details[key]}")
    error = payload.get("error")
    if isinstance(error, Mapping):
        print(
            f"STATE_RESET_ERROR code={error.get('code', 'error')} "
            f"message={error.get('message', '-')}",
            file=sys.stderr,
        )


def run_state_reset(args: argparse.Namespace) -> int:
    """Run the canonical ``Neocortex state reset`` CLI leaf.

    ``state_reset_payload`` calls ``plan_state_reset`` for both modes and, for
    apply, passes the exact digest into ``execute_state_reset``.  The CLI does
    not duplicate those fences or call the persistence module directly, so the
    Python facade and this command retain one contract.
    """

    try:
        from neocortex.api.state_reset import state_reset_payload

        payload = state_reset_payload(
            args.state_directory,
            scope=args.scope,
            apply=bool(getattr(args, "apply", False)),
            confirmation=getattr(args, "confirm_state_reset", None),
            plan_digest=getattr(args, "plan_digest", None),
            backup_directory=getattr(args, "backup_directory", None),
        )
        if not isinstance(payload, Mapping):
            raise TypeError("state reset API returned a non-mapping payload")
    except Exception as exc:  # pragma: no cover - only import/adapter failures
        payload = _fallback_error(args=args, error=exc)

    _render(payload, json_output=bool(getattr(args, "json", False)))
    return _exit_code(payload)


__all__ = [
    "STATE_RESET_CONFIRMATION",
    "STATE_RESET_SCHEMA",
    "STATE_RESET_SCOPES",
    "run_state_reset",
]
