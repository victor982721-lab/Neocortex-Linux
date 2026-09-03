"""CLI adapter for explicit, backup-first database removal."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

from neocortex.persistence.database_purge import (
    DATABASE_PURGE_CONFIRMATION,
    DATABASE_RESTORE_CONFIRMATION,
    DATABASE_STORE_NAMES,
    DatabasePurgeError,
    DatabasePurgeResult,
    DatabaseRestoreError,
    backup_state_owners,
    execute_database_purge,
    plan_database_purge,
    restore_state_owners,
)
from neocortex.persistence.state_publication import (
    StatePublicationError,
    read_state_epoch,
    read_state_publications,
)
from neocortex.persistence.sqlite_integrity import IntegrityCheckMode
from neocortex.workflow.state_health import inspect_state_health


DATABASE_BACKUP_CONFIRMATION = "BACKUP_DATABASES"
STATE_MAINTENANCE_SCHEMA = "neocortex.state-maintenance/v1"
_MAX_PUBLICATION_LIMIT = 100


def _maintenance_payload(
    operation: str,
    *,
    read_only: bool,
    status: str,
    exit_code: int,
    result: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build one bounded envelope for state maintenance commands."""

    return {
        "schema": STATE_MAINTENANCE_SCHEMA,
        "kind": "state-maintenance",
        "operation": operation,
        "request_id": str(uuid4()),
        "scope": "state",
        "read_only": read_only,
        "status": status,
        "exit_code": exit_code,
        "error": None if error is None else dict(error),
        "result": {} if result is None else dict(result),
    }


def _maintenance_error(
    operation: str,
    *,
    read_only: bool,
    exc: BaseException,
    exit_code: int = 2,
) -> dict[str, object]:
    return _maintenance_payload(
        operation,
        read_only=read_only,
        status="error",
        exit_code=exit_code,
        error={
            "code": type(exc).__name__,
            "message": str(exc),
            "retryable": isinstance(exc, (StatePublicationError, OSError)),
        },
    )


def _emit_maintenance(
    payload: Mapping[str, object],
    *,
    json_output: bool,
    prefix: str,
) -> None:
    """Emit JSON as one document or a small, stable human summary."""

    if json_output:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return
    result = payload.get("result")
    details = result if isinstance(result, Mapping) else {}
    print(
        f"{prefix} status={payload.get('status')} "
        f"exit_code={payload.get('exit_code')} "
        f"state={details.get('state_directory', '-')} "
        f"stores={','.join(str(item) for item in details.get('stores', ())) or '-'}"
    )
    if details.get("backup_directory"):
        print(f"{prefix}_BACKUP directory={details.get('backup_directory')}")
    if details.get("manifest"):
        print(
            f"{prefix}_MANIFEST path={details.get('manifest')} "
            f"sha256={details.get('manifest_sha256', '-')}"
        )
    if details.get("state_epoch") is not None:
        epoch = details.get("state_epoch")
        epoch_value = epoch.get("epoch", "-") if isinstance(epoch, Mapping) else "-"
        print(f"{prefix}_EPOCH value={epoch_value}")
    error = payload.get("error")
    if isinstance(error, Mapping):
        print(
            f"{prefix}_ERROR code={error.get('code', 'error')} "
            f"message={error.get('message', '-')}",
            file=sys.stderr,
        )


def _selected_stores(values: object) -> tuple[str, ...]:
    if values is None:
        return DATABASE_STORE_NAMES
    if isinstance(values, str):
        selected = (values,)
    elif isinstance(values, (tuple, list)):
        selected = tuple(values)
    else:
        raise DatabasePurgeError("database stores must be a sequence")
    if any(not isinstance(value, str) for value in selected):
        raise DatabasePurgeError("database stores must be text")
    if len(set(selected)) != len(selected):
        raise DatabasePurgeError("database stores cannot repeat")
    unknown = sorted(set(selected) - set(DATABASE_STORE_NAMES))
    if unknown:
        raise DatabasePurgeError(f"unknown database store: {unknown[0]}")
    return tuple(name for name in DATABASE_STORE_NAMES if name in selected)


def _nonnegative_epoch(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected epoch must be a non-negative integer")
    return value


def _health_exit_code(health: object, owners: object | None = None) -> int:
    if owners is None:
        owners = getattr(health, "owners", ())
    owner_values = owners if isinstance(owners, (list, tuple)) else ()
    statuses = {str(getattr(owner, "status", "unreadable")) for owner in owner_values}
    if not statuses or statuses <= {"healthy"}:
        return 0
    if statuses & {"corrupt"}:
        return 7
    if statuses & {"future", "incompatible"}:
        return 6
    if statuses & {"blocked", "active"}:
        return 5
    if statuses & {"unreadable"}:
        return 1
    return 4


def _state_status_result(
    state_directory: object,
    *,
    stores: object = None,
    publication_limit: int = 10,
) -> tuple[dict[str, object], int]:
    state = _absolute_path(state_directory, label="state directory")
    selected = _selected_stores(stores)
    if not 1 <= publication_limit <= _MAX_PUBLICATION_LIMIT:
        raise ValueError(f"publication limit must be between 1 and {_MAX_PUBLICATION_LIMIT}")
    health = inspect_state_health(state)
    selected_owner_rows = (
        list(health.owners)
        if stores is None
        else [owner for owner in health.owners if owner.name in selected]
    )
    owners = [owner.to_dict() for owner in selected_owner_rows]
    publications = read_state_publications(state)
    result: dict[str, object] = {
        "state_directory": str(health.state_directory),
        "stores": list(selected),
        "overall": health.overall,
        "health": {
            "healthy_count": health.healthy_count,
            "missing_count": health.missing_count,
            "orphaned_sidecar_count": health.orphaned_sidecar_count,
            "blocked_count": health.blocked_count,
            "active_count": health.active_count,
            "incompatible_count": health.incompatible_count,
            "future_count": health.future_count,
            "unreadable_count": health.unreadable_count,
            "unknown_count": health.unknown_count,
            "corrupt_count": health.corrupt_count,
        },
        "owners": owners,
        "state_epoch": read_state_epoch(state).as_payload(),
        "publications": [
            item.as_payload() for item in publications[-publication_limit:]
        ],
        "publication_count": len(publications),
    }
    return result, _health_exit_code(health, selected_owner_rows)


def _absolute_path(value: object, *, label: str) -> Path:
    if isinstance(value, Path):
        selected = value.expanduser()
    elif isinstance(value, str):
        selected = Path(value).expanduser()
    else:
        raise ValueError(f"{label} must be a path")
    if not selected.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return selected


def _argument_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _payload_exit_code(payload: Mapping[str, object]) -> int:
    value = payload.get("exit_code")
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return value


def _maintenance_stores(args: argparse.Namespace) -> tuple[str, ...]:
    return _selected_stores(getattr(args, "store", None))


def run_database_status(args: argparse.Namespace) -> int:
    """Expose sidecar-safe owner health and publication metadata."""

    operation = "database-status"
    try:
        result, exit_code = _state_status_result(
            args.state_directory,
            stores=getattr(args, "store", None),
            publication_limit=_argument_int(
                getattr(args, "publication_limit", 10),
                "publication limit",
            ),
        )
    except (OSError, RuntimeError, StatePublicationError, ValueError) as exc:
        payload = _maintenance_error(operation, read_only=True, exc=exc)
        _emit_maintenance(
            payload,
            json_output=bool(getattr(args, "json", False)),
            prefix="DATABASE_STATUS",
        )
        return _payload_exit_code(payload)
    payload = _maintenance_payload(
        operation,
        read_only=True,
        status="complete" if exit_code == 0 else "partial",
        exit_code=exit_code,
        result=result,
    )
    _emit_maintenance(
        payload,
        json_output=bool(getattr(args, "json", False)),
        prefix="DATABASE_STATUS",
    )
    return exit_code


def run_database_backup(args: argparse.Namespace) -> int:
    """Preview or create a verified multi-owner backup.

    The default is a read-only preview.  Creating backup bytes is explicit and
    requires both ``--apply`` and the non-secret confirmation token, which
    prevents a copied command from unexpectedly writing a large artifact.
    """

    operation = "database-backup"
    apply = bool(getattr(args, "apply", False))
    try:
        state = _absolute_path(args.state_directory, label="state directory")
        backup = _absolute_path(args.backup_directory, label="backup directory")
        stores = _maintenance_stores(args)
        expected_epoch = _nonnegative_epoch(getattr(args, "expected_epoch", None))
        integrity_mode_value = str(getattr(args, "integrity_mode", "full"))
        if integrity_mode_value not in {"quick", "full"}:
            raise ValueError("integrity must be quick or full")
        integrity_mode = cast(IntegrityCheckMode, integrity_mode_value)
        if not apply:
            health_result, health_code = _state_status_result(
                state,
                stores=getattr(args, "store", None),
                publication_limit=1,
            )
            observed_epoch = health_result["state_epoch"]
            if expected_epoch is not None and (
                not isinstance(observed_epoch, Mapping)
                or observed_epoch.get("epoch") != expected_epoch
            ):
                observed_value = (
                    observed_epoch.get("epoch")
                    if isinstance(observed_epoch, Mapping)
                    else "-"
                )
                raise ValueError(
                    f"state publication epoch changed: expected {expected_epoch}, "
                    f"observed {observed_value}"
                )
            result: dict[str, object] = {
                "state_directory": str(state),
                "backup_directory": str(backup),
                "stores": list(stores),
                "integrity_mode": integrity_mode,
                "state_epoch": observed_epoch,
                "source_health": health_result,
                "requires_confirmation": True,
                "confirmation_option": "--confirm-database-backup BACKUP_DATABASES",
            }
            payload = _maintenance_payload(
                operation,
                read_only=True,
                status="preview" if health_code == 0 else "partial",
                exit_code=health_code,
                result=result,
            )
        else:
            if getattr(args, "confirm_database_backup", None) != DATABASE_BACKUP_CONFIRMATION:
                raise DatabasePurgeError(
                    f"apply requires confirmation token {DATABASE_BACKUP_CONFIRMATION!r}"
                )
            result_object = backup_state_owners(
                state,
                backup,
                stores=stores,
                release_sha=getattr(args, "release_sha", None),
                integrity_mode=integrity_mode,
                expected_epoch=expected_epoch,
            )
            result = result_object.as_payload()
            result["stores"] = list(stores)
            result["mode"] = "applied"
            complete = bool(result.get("complete"))
            payload = _maintenance_payload(
                operation,
                read_only=False,
                status="complete" if complete else "partial",
                exit_code=0 if complete else 4,
                result=result,
            )
    except (DatabasePurgeError, DatabaseRestoreError, OSError, RuntimeError, ValueError) as exc:
        payload = _maintenance_error(operation, read_only=not apply, exc=exc)
    _emit_maintenance(
        payload,
        json_output=bool(getattr(args, "json", False)),
        prefix="DATABASE_BACKUP",
    )
    return _payload_exit_code(payload)


def run_database_restore(args: argparse.Namespace) -> int:
    """Validate or publish a complete state backup using an explicit digest."""

    operation = "database-restore"
    apply = bool(getattr(args, "apply", False))
    try:
        state = _absolute_path(args.state_directory, label="state directory")
        backup = _absolute_path(args.backup_directory, label="backup directory")
        stores = _maintenance_stores(args)
        expected_epoch = _nonnegative_epoch(getattr(args, "expected_epoch", None))
        expected_manifest = getattr(args, "expected_manifest_sha256", None)
        if apply and not expected_manifest:
            raise DatabaseRestoreError(
                "apply requires --manifest-sha256 with the verified manifest digest"
            )
        if apply and (
            getattr(args, "confirm_database_restore", None)
            != DATABASE_RESTORE_CONFIRMATION
        ):
            raise DatabaseRestoreError(
                f"apply requires confirmation token {DATABASE_RESTORE_CONFIRMATION!r}"
            )
        result_object = restore_state_owners(
            state,
            backup,
            stores=stores,
            apply=apply,
            confirmation=getattr(args, "confirm_database_restore", None),
            expected_epoch=expected_epoch,
            expected_manifest_sha256=expected_manifest,
        )
        result = result_object.as_payload()
        result["stores"] = list(stores)
        result["mode"] = "applied" if apply else "preview"
        payload = _maintenance_payload(
            operation,
            read_only=not apply,
            status="complete",
            exit_code=0,
            result=result,
        )
    except (DatabasePurgeError, DatabaseRestoreError, OSError, RuntimeError, ValueError) as exc:
        payload = _maintenance_error(operation, read_only=not apply, exc=exc)
    _emit_maintenance(
        payload,
        json_output=bool(getattr(args, "json", False)),
        prefix="DATABASE_RESTORE",
    )
    return _payload_exit_code(payload)


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return
    mode = str(payload.get("mode", "preview"))
    file_count = payload.get("deleted_file_count", payload.get("file_count", 0))
    byte_count = payload.get("deleted_bytes", payload.get("total_bytes", 0))
    stores = payload.get("stores")
    store_names = stores if isinstance(stores, list) else []
    print(
        "DATABASE_PURGE "
        f"mode={mode} state={payload.get('state_directory')} "
        f"stores={','.join(str(item) for item in store_names) or '-'} "
        f"files={file_count} "
        f"bytes={byte_count} "
        f"plan={payload.get('plan_digest', '-')}"
    )
    targets = payload.get("targets")
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, Mapping):
            continue
        print(
            "DATABASE_PURGE_TARGET "
            f"owner={target.get('owner')} database={target.get('database')} "
            f"files={len(target.get('files', ())) if isinstance(target.get('files'), list) else 0} "
            f"bytes={target.get('bytes', 0)}"
        )
    conflicts = payload.get("lock_conflicts")
    if isinstance(conflicts, list) and conflicts:
        print("DATABASE_PURGE_BLOCKED locks=" + ",".join(str(item) for item in conflicts))
    unknown = payload.get("unknown_sqlite_files")
    if isinstance(unknown, list) and unknown:
        print("DATABASE_PURGE_PRESERVED unknown=" + ",".join(str(item) for item in unknown))
    if payload.get("backup_directory"):
        print(
            "DATABASE_PURGE_BACKUP "
            f"directory={payload.get('backup_directory')} manifest={payload.get('manifest')}"
        )


def _error(message: str) -> int:
    print(f"ERROR database-purge {message}", file=sys.stderr)
    return 2


def run_database_purge(args: argparse.Namespace) -> int:
    """Preview by default; apply only with the exact destructive token."""

    try:
        if bool(args.apply):
            confirmation = getattr(args, "confirm_database_purge", None)
            if confirmation != DATABASE_PURGE_CONFIRMATION:
                raise DatabasePurgeError(
                    f"apply requires confirmation token {DATABASE_PURGE_CONFIRMATION!r}"
                )
            expected_digest = getattr(args, "plan_digest", None)
            if not isinstance(expected_digest, str) or not expected_digest:
                raise DatabasePurgeError(
                    "apply requires --plan-digest from the read-only purge preview"
                )
            planned = plan_database_purge(args.state_directory, stores=args.store)
            if planned.plan_digest != expected_digest:
                raise DatabasePurgeError(
                    "purge plan digest does not match the current state; preview again"
                )
        result = execute_database_purge(
            args.state_directory,
            stores=args.store,
            backup_directory=args.backup_directory,
            apply=bool(args.apply),
            confirmation=args.confirm_database_purge,
        )
    except DatabasePurgeError as exc:
        return _error(str(exc))
    payload = (
        result.as_payload()
        if isinstance(result, DatabasePurgeResult)
        else result.as_payload(mode="preview")
    )
    _emit(payload, json_output=bool(args.json))
    conflicts = payload.get("lock_conflicts")
    return 2 if isinstance(conflicts, list) and conflicts else 0


__all__ = [
    "DATABASE_BACKUP_CONFIRMATION",
    "STATE_MAINTENANCE_SCHEMA",
    "run_database_backup",
    "run_database_purge",
    "run_database_restore",
    "run_database_status",
]
