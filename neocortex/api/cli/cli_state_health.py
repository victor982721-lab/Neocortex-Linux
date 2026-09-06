"""State-owner health command adapter."""

from __future__ import annotations

import argparse
import json

from neocortex.workflow.state_health import inspect_state_health


def run_state_health(args: argparse.Namespace) -> int:
    """Inspect all known SQLite owners without opening them directly."""

    try:
        options = {}
        for argument, keyword, default in (
            ("state_health_timeout", "timeout_seconds", 30.0),
            ("state_health_scope", "scope", "full"),
            ("state_health_max_owners", "max_owners", None),
            ("state_health_after_owner", "after_owner", None),
        ):
            value = getattr(args, argument, default)
            if value != default:
                options[keyword] = value
        owners = getattr(args, "state_health_owner", None)
        if owners is not None:
            options["owners"] = tuple(owners)
        health = inspect_state_health(args.state_directory, **options)
    except (OSError, RuntimeError, ValueError) as exc:
        if args.state_health_json:
            print(json.dumps({
                "kind": "state-health-error", "complete": False,
                "error": {"code": "state_health_failed", "type": type(exc).__name__, "detail": str(exc)},
            }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
        else:
            print(f"ERROR state-health {type(exc).__name__}: {exc}")
        return 2
    if args.state_health_json:
        print(
            json.dumps(
                health.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(
            f"STATE_HEALTH overall={health.overall} "
            f"scope={getattr(args, 'state_health_scope', 'full')} "
            f"healthy={health.healthy_count} missing={health.missing_count} "
            f"orphaned_sidecars={health.orphaned_sidecar_count} "
            f"blocked={health.blocked_count} active={health.active_count} "
            f"incompatible={health.incompatible_count} future={health.future_count} "
            f"corrupt={health.corrupt_count} unknown={health.unknown_count} "
            f"unreadable={health.unreadable_count} "
            f"not_verified={health.not_verified_count} "
            f"state={health.state_directory}"
        )
        for owner in health.owners:
            sidecars = ",".join(
                f"{sidecar.suffix}:{sidecar.size}" for sidecar in owner.sidecars
            ) or "-"
            print(
                f"STATE_OWNER name={owner.name} status={owner.status} "
                f"schema={owner.schema_version or '-'} user_version={owner.user_version or 0} "
                f"tables={owner.table_count} sidecars={sidecars} "
                f"detail={owner.detail or '-'} path={owner.path}"
            )
    return 0 if getattr(health, "scope_complete", health.overall == "healthy") else 2


__all__ = ["run_state_health"]
