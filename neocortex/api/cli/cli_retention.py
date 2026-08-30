"""Read-only CLI facade for bounded retention planning."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/cli_retention.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import argparse
import json
import sqlite3
import time
# endregion [01]

# region [02] Implementación


def run_retention_status(args: argparse.Namespace) -> int:
    """Print a dry-run retention page without creating or migrating state."""

    from neocortex.workflow.retention.planner import (
        RetentionPolicy,
        plan_retention,
        retention_plan_payload,
    )

    stores = None if args.retention_store is None else tuple(args.retention_store)
    after = {
        store: getattr(args, f"retention_{store}_after")
        for store in ("semantic", "catalog", "inventory", "framework")
        if stores is None or store in stores
    }
    try:
        plan = plan_retention(
            args.state_directory,
            policy=RetentionPolicy(
                minimum_age_ns=args.retention_min_age_days,
                batch_size=args.retention_batch_size,
            ),
            after=after,
            stores=stores,
            now_ns=time.time_ns(),
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR retention-status {exc}")
        return 2
    if args.retention_json:
        print(
            json.dumps(
                retention_plan_payload(plan),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        age = "-" if plan.policy.minimum_age_ns is None else plan.policy.minimum_age_ns
        print(
            "RETENTION_PLAN dry_run=1 deletion_supported=0 "
            f"estimate={plan.estimate_kind} minimum_age_ns={age} "
            f"batch={plan.policy.batch_size} keep_published="
            f"{plan.policy.keep_published} sqlite_shm_side_effect="
            f"{'possible' if plan.sqlite_read_snapshot_may_touch_shm else 'none'}"
        )
        for store in plan.stores:
            print(
                f"RETENTION_STORE name={store.store} status={store.status} "
                f"schema={store.schema_version or '-'} items={len(store.items)} "
                f"eligible_rows={store.eligible_rows} "
                f"eligible_bytes={store.eligible_bytes} "
                f"protected_rows={store.protected_rows} "
                f"protected_bytes={store.protected_bytes} "
                f"db={store.database_bytes} wal={store.wal_bytes} "
                f"shm={store.shm_bytes} truncated={int(store.truncated)} "
                f"next_after={store.next_after or '-'} database={store.database} "
                f"detail={store.detail or '-'}"
            )
            for hold in store.holds:
                print(
                    f"RETENTION_HOLD store={store.store} name={hold.name} "
                    f"rows={hold.rows} estimated_bytes={hold.estimated_bytes} "
                    f"reason={hold.reason}"
                )
            for item in store.items:
                print(
                    f"RETENTION_ITEM store={store.store} entity={item.entity} "
                    f"id={item.key} scope={item.scope} "
                    f"status={item.recorded_status} "
                    f"disposition={item.disposition} rows={item.estimated_rows} "
                    f"estimated_bytes={item.estimated_bytes} "
                    f"reasons={','.join(item.reasons) or '-'}"
                )
    return 2 if any(store.status == "blocked" for store in plan.stores) else 0


__all__ = ["run_retention_status"]
# endregion [02]


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_retention')
