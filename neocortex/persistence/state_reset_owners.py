"""Bounded owner assessments for reset and its postconditions.

The registry supplies table authority; this adapter only implements the three
owner transforms currently supported. An unknown table or a protected owner
without its transform is an explicit pre-effect blocker.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from neocortex.persistence.operational_freshness import read_operational_barrier
from neocortex.persistence.sqlite_immutable import preferred_sqlite_read_mode, sqlite_read_session
from neocortex.safety.state_lifecycle_contracts import OwnerResetAssessment, OwnerResetVerification
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY

_MAX_ROWS = 1_000_000
_MAX_HASH_BYTES = 256 * 1024 * 1024
_MAX_SECONDS = 60.0


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_payload(row: tuple[object, ...]) -> bytes:
    # Type tags keep blobs distinct from strings and integers distinct from floats.
    value = [(type(item).__name__, item.hex() if isinstance(item, bytes) else item) for item in row]
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def _authority_digest(connection: sqlite3.Connection, tables: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    consumed = rows = 0
    deadline = time.monotonic() + _MAX_SECONDS
    for table in tables:
        digest.update(table.encode("utf-8"))
        # Order by every observed column, not rowid, so VACUUM preserves the proof.
        count = len(connection.execute(f"PRAGMA table_info({_q(table)})").fetchall())
        order = ",".join(str(index) for index in range(1, count + 1))
        predicate = " WHERE generation_id IN (SELECT generation_id FROM catalog_generations WHERE status='published')" if table == "catalog_generation_manifests" else ""
        for row in connection.execute(f"SELECT * FROM {_q(table)}{predicate} ORDER BY {order}"):
            encoded = _row_payload(tuple(row))
            consumed += len(encoded)
            rows += 1
            if consumed > _MAX_HASH_BYTES or rows > _MAX_ROWS or time.monotonic() > deadline:
                raise RuntimeError("owner authority observation budget exhausted")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _operational_count(connection: sqlite3.Connection, owner: str, table: str, floor: int) -> int:
    if owner == "framework" and table in {
        "initial_runs", "route_runs", "route_phase_runs", "run_events", "route_candidates", "run_actions",
    }:
        return int(connection.execute(f"SELECT COUNT(*) FROM {_q(table)} WHERE run_id>?", (floor,)).fetchone()[0])
    if owner == "inventory" and table == "scans":
        return int(connection.execute("SELECT COUNT(*) FROM scans WHERE scan_id>?", (floor,)).fetchone()[0])
    if owner == "catalog":
        if table in {"catalog_generations", "catalog_generation_documents"}:
            return int(connection.execute(f"SELECT COUNT(*) FROM {_q(table)} WHERE generation_id>?", (floor,)).fetchone()[0])
        if table == "catalog_runs":
            return int(connection.execute(
                "SELECT COUNT(*) FROM catalog_runs WHERE catalog_run_id NOT IN "
                "(SELECT catalog_run_id FROM catalog_generations WHERE generation_id<=? AND catalog_run_id IS NOT NULL)",
                (floor,),
            ).fetchone()[0])
    return int(connection.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0])


def assess_reset_owners(state: Path, owners: tuple[str, ...]) -> tuple[OwnerResetAssessment, ...]:
    results = []
    for owner in owners:
        contract = STATE_STORE_REGISTRY.by_owner(owner)
        rules = {rule.table: rule for rule in contract.lifecycle_rules}
        database = state / contract.database_name
        if not database.exists():
            continue
        with sqlite_read_session(database, mode=preferred_sqlite_read_mode(database), timeout_seconds=60.0) as connection:
            tables = tuple(str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ))
            unknown = tuple(table for table in tables if contract.lifecycle_rule(table) is None)
            authoritative = tuple(table for table in tables if (
                table in rules and rules[table].role == "authoritative"
            ))
            row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            version = None if row is None else int(row[0])
            barrier = read_operational_barrier(connection, owner)
            floor = 0 if barrier is None else int(barrier["identity_floor"])
            blockers = [f"unknown-table:{table}" for table in unknown]
            from neocortex.persistence import state_reset as reset
            if owner == "catalog":
                blockers.extend("not reconcilable:" + reason for reason in reset._catalog_evidence_anomalies(connection, set(tables)))
            if owner == "inventory":
                try:
                    reset._assert_inventory_protected_references(connection, set(tables))
                except RuntimeError as exc:
                    blockers.append(str(exc))
            if version != contract.expected_schema_version:
                blockers.append("schema-reader-contract-not-current")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                blockers.append("foreign-key-violation")
            table_counts = tuple((table, int(connection.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0])) for table in tables)
            counts = dict(table_counts)
            populated_authority = tuple(table for table in authoritative if counts[table])
            if populated_authority and owner not in {"framework", "inventory", "catalog"}:
                blockers.append("protected-owner-transform-unavailable")
            operational_rows = 0
            for table in tables:
                if table in rules and rules[table].role in {"derived", "operational"}:
                    operational_rows += _operational_count(connection, owner, table, floor)
            digest = _authority_digest(connection, authoritative)
            results.append(OwnerResetAssessment(
                owner, contract.lifecycle_policy_version, version, operational_rows,
                populated_authority, unknown, tuple(blockers), floor,
                barrier is not None, digest, table_counts,
            ))
    return tuple(results)


def verify_reset_owners(
    state: Path, before: tuple[OwnerResetAssessment, ...],
) -> tuple[OwnerResetVerification, ...]:
    observed = {item.owner_id: item for item in assess_reset_owners(state, tuple(item.owner_id for item in before))}
    results = []
    for prior in before:
        current = observed.get(prior.owner_id)
        if current is None:
            preserved = not prior.authoritative_tables
            results.append(OwnerResetVerification(prior.owner_id, preserved, preserved, preserved, preserved, 0))
            continue
        authority_ok = prior.authoritative_digest == current.authoritative_digest
        fresh = current.barrier_present and current.operational_rows == 0 and not current.blocked_reasons
        residuals = current.blocked_reasons + (("operational-rows-remain",) if current.operational_rows else ())
        results.append(OwnerResetVerification(
            prior.owner_id, authority_ok and fresh, fresh, authority_ok,
            "foreign-key-violation" not in current.blocked_reasons, current.identity_floor, residuals,
        ))
    return tuple(results)
