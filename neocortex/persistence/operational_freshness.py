"""Owner-local reset barriers separate historical identities from selection.

The metadata record is written only on a staged owner inside its transaction.
Schema versions that introduce this reader contract fence out older readers.
Explicit historical lookup keeps its meaning; selection or resumable work must
call ``require_operational_identity`` before publishing or deriving a head.
"""
from __future__ import annotations

import json
import sqlite3
from typing import TypedDict

OPERATIONAL_RESET_KEY = "operational_reset_barrier"
OPERATIONAL_RESET_SCHEMA = "neocortex.operational-reset-barrier/v1"


class OperationalResetBarrier(TypedDict):
    schema: str
    owner: str
    identity_floor: int
    plan_digest: str


def read_operational_barrier(connection: sqlite3.Connection, owner: str) -> OperationalResetBarrier | None:
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (OPERATIONAL_RESET_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("operational reset barrier is corrupt") from exc
    if (
        not isinstance(value, dict) or value.get("schema") != OPERATIONAL_RESET_SCHEMA
        or value.get("owner") != owner or type(value.get("identity_floor")) is not int
        or value["identity_floor"] < 0 or not isinstance(value.get("plan_digest"), str)
    ):
        raise RuntimeError("operational reset barrier is incompatible")
    return OperationalResetBarrier(schema=OPERATIONAL_RESET_SCHEMA, owner=owner,
                                   identity_floor=value["identity_floor"], plan_digest=value["plan_digest"])


def operational_identity_floor(connection: sqlite3.Connection, owner: str) -> int:
    barrier = read_operational_barrier(connection, owner)
    return 0 if barrier is None else int(barrier["identity_floor"])


def require_operational_identity(connection: sqlite3.Connection, owner: str, identity: int) -> None:
    if identity <= operational_identity_floor(connection, owner):
        raise RuntimeError(f"{owner} identity {identity} was retired by an operational reset")


def next_operational_identity(connection: sqlite3.Connection, owner: str, table: str, column: str) -> int:
    allowed = {("inventory", "scans", "scan_id"),
               ("catalog", "catalog_runs", "catalog_run_id"),
               ("catalog", "catalog_generations", "generation_id")}
    if (owner, table, column) not in allowed:
        raise ValueError("unknown operational identity allocator")
    row = connection.execute(f'SELECT COALESCE(MAX("{column}"),0) FROM "{table}"').fetchone()
    return max(int(row[0]), operational_identity_floor(connection, owner)) + 1


def write_operational_barrier(
    connection: sqlite3.Connection, owner: str, *, identity_floor: int, plan_digest: str,
) -> None:
    if not connection.in_transaction:
        raise RuntimeError("reset barrier requires the owner's staged transaction")
    floor = max(identity_floor, operational_identity_floor(connection, owner))
    payload = {"schema": OPERATIONAL_RESET_SCHEMA, "owner": owner,
               "identity_floor": floor, "plan_digest": plan_digest}
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (OPERATIONAL_RESET_KEY, json.dumps(payload, sort_keys=True, separators=(",", ":"))),
    )
