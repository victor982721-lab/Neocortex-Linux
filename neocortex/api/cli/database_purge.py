"""CLI adapter for explicit, backup-first database removal."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping

from neocortex.persistence.database_purge import (
    DatabasePurgeError,
    DatabasePurgeResult,
    execute_database_purge,
)


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


__all__ = ["run_database_purge"]
