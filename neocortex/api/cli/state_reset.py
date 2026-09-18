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
    if details.get("operation_id"):
        print(
            f"STATE_RESET_OPERATION id={details['operation_id']} "
            f"action={details.get('action', '-')} phase={details.get('phase', '-')} "
            f"receipt_digest={details.get('receipt_digest', '-')}"
        )
    counters = (
        "run_count", "selected_owner_count", "transformed_owner_count",
        "removed_owner_database_count", "preserved_owner_database_count",
        "deleted_file_count", "deleted_bytes", "remaining_selected_entry_count",
        "unassessed_state_root_count",
    ) if "selected_owner_count" in details else (
        "run_count", "cache_count", "database_count", "file_count"
    )
    for key in counters:
        if key in details:
            print(f"STATE_RESET_{key.upper()} value={details[key]}")
    if "operational_freshness" in details:
        print(
            "STATE_RESET_VERIFICATION "
            f"scope={details.get('verification_scope', 'selected_targets')} "
            f"verified={details.get('verified', False)} "
            f"operational_freshness={details['operational_freshness']}"
        )
        protected = details.get("protected_tables")
        if isinstance(protected, Mapping):
            for owner, tables in list(protected.items())[:16]:
                print(
                    "STATE_RESET_PRESERVED_OWNER "
                    f"owner={json.dumps(str(owner), ensure_ascii=True)} "
                    "reason=non_regenerable_tables "
                    f"tables={json.dumps(tables, ensure_ascii=True)}"
                )
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

        apply_requested = bool(getattr(args, "apply", False))
        yes_requested = bool(getattr(args, "yes", False))
        confirmation = getattr(args, "confirm_state_reset", None)
        plan_digest = getattr(args, "plan_digest", None)
        backup_directory = getattr(args, "backup_directory", None)

        operation_id = getattr(args, "reconcile_operation", None)
        if operation_id is not None:
            from neocortex.api.state_reset import state_reset_recovery_payload
            payload = state_reset_recovery_payload(
                args.state_directory, operation_id, apply=apply_requested,
                receipt_digest=getattr(args, "receipt_digest", None),
                confirmation=confirmation,
            )
            _render(payload, json_output=bool(getattr(args, "json", False)))
            return _exit_code(payload)

        # The ordinary interface binds confirmation to a fresh, hidden
        # preview.  Legacy callers that already supply RESET_STATE and a
        # digest retain their exact one-call contract.
        if apply_requested and yes_requested and plan_digest is None:
            preview = state_reset_payload(
                args.state_directory,
                scope=args.scope,
                apply=False,
            )
            preview_result = preview.get("result") if isinstance(preview, Mapping) else None
            if not isinstance(preview_result, Mapping):
                payload = preview
            else:
                digest = preview_result.get("plan_digest")
                if not isinstance(digest, str) or not digest:
                    payload = {
                        "schema": STATE_RESET_SCHEMA,
                        "kind": "state-reset",
                        "operation": "state-reset",
                        "scope": args.scope,
                        "status": "error",
                        "read_only": True,
                        "exit_code": 4,
                        "error": {
                            "code": "preview_digest_missing",
                            "type": "StateResetPlanError",
                            "message": "la vista previa no produjo un digest aplicable",
                            "retryable": True,
                        },
                        "result": {},
                    }
                else:
                    payload = state_reset_payload(
                        args.state_directory,
                        scope=args.scope,
                        apply=True,
                        yes=True,
                        confirmation=confirmation,
                        plan_digest=digest,
                        backup_directory=backup_directory,
                    )
        elif apply_requested and not yes_requested and plan_digest is None and confirmation is None:
            # Avoid an accidental destructive operation from a pipe or
            # automation.  Interactive callers must opt into the same exact
            # plan with --yes after seeing the preview.
            if not sys.stdin.isatty():
                payload = {
                    "schema": STATE_RESET_SCHEMA,
                    "kind": "state-reset",
                    "operation": "state-reset",
                    "scope": args.scope,
                    "status": "error",
                    "read_only": False,
                    "exit_code": 3,
                    "error": {
                        "code": "confirmation_required",
                        "type": "StateResetConfirmationError",
                        "message": "en un TTY use --yes; primero revise `state reset --scope ...`",
                        "retryable": False,
                    },
                    "result": {},
                }
            else:
                preview = state_reset_payload(
                    args.state_directory,
                    scope=args.scope,
                    apply=False,
                )
                preview_result = preview.get("result") if isinstance(preview, Mapping) else None
                digest = preview_result.get("plan_digest") if isinstance(preview_result, Mapping) else None
                if not isinstance(digest, str) or not digest:
                    payload = preview
                else:
                    print(
                        "Confirma el reset del alcance "
                        f"{args.scope!r} con el plan {digest}? [s/N] ",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
                    answer = sys.stdin.readline().strip().casefold()
                    if answer not in {"s", "si", "sí", "y", "yes"}:
                        payload = {
                            **preview,
                            "status": "cancelled",
                            "read_only": True,
                            "exit_code": 0,
                        }
                    else:
                        payload = state_reset_payload(
                            args.state_directory,
                            scope=args.scope,
                            apply=True,
                            yes=True,
                            plan_digest=digest,
                            backup_directory=backup_directory,
                        )
        else:
            payload = state_reset_payload(
                args.state_directory,
                scope=args.scope,
                apply=apply_requested,
                yes=yes_requested,
                confirmation=confirmation,
                plan_digest=plan_digest,
                backup_directory=backup_directory,
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
