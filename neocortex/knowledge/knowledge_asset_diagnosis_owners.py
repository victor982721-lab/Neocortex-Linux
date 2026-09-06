"""Exact, immutable adapters for existing Inventory and Archive diagnosis facts.

No database, cache, corpus read, or independent publication is introduced. The
health caller observes these projections twice under its existing owner fence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticEvidenceRef,
    AssetDiagnosticObservation,
    AssetDiagnosticObservationKind as Kind,
)
from .knowledge_asset_health_contracts import KnowledgeAssetHealthFact, KnowledgeAssetIdentity
from .knowledge_snapshot import KnowledgeStatePaths
from neocortex.foundation.file_identity import encode_file_identity
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database
from neocortex.persistence.sqlite_schema_contract import read_application_schema_version


def _digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _blob(value: int) -> bytes:
    return value.to_bytes(16, "little")


def _identity(row: sqlite3.Row) -> KnowledgeAssetIdentity:
    return KnowledgeAssetIdentity(int.from_bytes(row["volume_id"], "little"),
                                  int.from_bytes(row["file_id"], "little"), int(row["birthtime_ns"]))


def _duplicate_observations(
    connection: sqlite3.Connection,
    identity: KnowledgeAssetIdentity,
    snapshot_id: str,
) -> tuple[AssetDiagnosticObservation, ...]:
    from neocortex.deduplication.inventory.plan_evidence import decode_group_proof, decode_member_proof

    groups = connection.execute(
        """SELECT DISTINCT g.* FROM planned_duplicate_groups g
        JOIN planned_duplicate_members m USING(group_id)
        JOIN duplicate_plan_summaries summary ON summary.scan_id=g.scan_id
        JOIN inventory_checkpoints c ON c.scan_id=g.scan_id AND c.valid=1
        JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root AND s.status='complete'
        WHERE m.volume_id=? AND m.file_id=? AND m.birthtime_ns=?
        AND g.verification_mode='full_hash' AND summary.coverage='complete'
        ORDER BY g.group_id LIMIT 9""",
        (_blob(identity.volume_id), _blob(identity.file_id), identity.birthtime_ns),
    ).fetchall()
    if len(groups) > 8:
        raise ValueError("duplicate groups exceed diagnosis bound")
    observations: list[AssetDiagnosticObservation] = []
    for group in groups:
        if len(str(group["proof_json"])) > 65536:
            raise ValueError("duplicate group evidence exceeds diagnosis bound")
        proof = decode_group_proof(str(group["proof_json"]))
        if proof is None or (proof.comparison_method, proof.comparison_result) != ("byte_for_byte", "equal"):
            continue
        members = connection.execute(
            "SELECT * FROM planned_duplicate_members WHERE group_id=? ORDER BY member_order LIMIT 101",
            (group["group_id"],),
        ).fetchall()
        if not 2 <= len(members) <= 100 or len(members) != int(group["redundant_count"]) + 1:
            raise ValueError("duplicate group membership is incomplete")
        keepers = [row for row in members if row["role"] == "keep"]
        if len(keepers) != 1 or keepers[0]["path"] != group["keep_path"]:
            raise ValueError("duplicate keeper evidence is ambiguous")
        keeper = _identity(keepers[0])
        member_ids = tuple(sorted(_identity(row).resource_id for row in members))
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("hardlink aliases do not prove distinct duplicate resources")
        proof_payloads: list[dict[str, object]] = []
        for member in members:
            present = connection.execute(
                """SELECT 1 FROM files WHERE scan_id=? AND path=? AND volume_id=? AND file_id=?
                AND birthtime_ns=? AND size=? AND mtime_ns=? LIMIT 1""",
                (group["scan_id"], member["path"], member["volume_id"], member["file_id"],
                 member["birthtime_ns"], member["size"], member["mtime_ns"]),
            ).fetchone()
            if len(str(member["proof_json"])) > 65536:
                raise ValueError("duplicate member evidence exceeds diagnosis bound")
            receipt = decode_member_proof(str(member["proof_json"]))
            if present is None or int(member["size"]) != int(group["size"]):
                raise ValueError("duplicate membership is stale against published inventory")
            if member["role"] != "keep" and (
                member["role"] != "redundant"
                or receipt.comparison_method != "byte_for_byte"
                or receipt.comparison_result != "equal"
                or receipt.comparison_bytes != int(member["size"])
                or receipt.compared_to_identity != (keeper.volume_id, keeper.file_id)
            ):
                raise ValueError("duplicate member lacks exact comparison evidence")
            proof_payloads.append({"identity": _identity(member).resource_id,
                                   "proof": receipt.as_dict()})
        evidence = AssetDiagnosticEvidenceRef(
            "inventory", f"duplicate-group:{group['scan_id']}:{group['group_id']}",
            identity.resource_id, snapshot_id,
            _digest({"group": proof.as_dict(), "members": proof_payloads}),
            f"inventory-scan:{group['scan_id']}",
        )
        selection_basis = next((basis for basis in ("explicit_user_decision", "preferred_location")
                                if basis in proof.keeper_factors), proof.keeper_reason)
        missing = set(proof.missing_checks) | {"retention_and_dependency_review"}
        if selection_basis not in {"explicit_user_decision", "preferred_location"}:
            missing.add("contextual_keeper_selection")
        observations.append(AssetDiagnosticObservation(
            Kind.DUPLICATE_CONTENT, identity.resource_id, "byte_for_byte_equal", (evidence,), member_ids,
            tuple(sorted(missing)),
            selection_basis=selection_basis,
        ))
    return tuple(observations)


def _archive_observations(
    connection: sqlite3.Connection,
    identity: KnowledgeAssetIdentity,
    snapshot_id: str,
    inventory: KnowledgeAssetHealthFact,
) -> tuple[AssetDiagnosticObservation, ...]:
    from neocortex.capabilities.formats.archive.logical import identify_logical_document

    values = {item.name: item.value for item in inventory.values}
    keys = tuple(sorted({encode_file_identity(identity.volume_id, identity.file_id),
                         f"{identity.volume_id}:{identity.file_id}"}))
    rows = connection.execute(
        """SELECT l.*,c.processing_signature FROM archive_logical_documents l
        JOIN containers c USING(container_key)
        WHERE c.container_key IN (""" + ",".join("?" for _ in keys) + """ )
        AND c.path=? AND c.size=? AND c.mtime_ns=? AND c.birthtime_ns=?
        AND c.status IN ('complete','partial') AND l.member_chain='' LIMIT 2""",
        (*keys, values["path"], int(values["size"]), int(values["mtime_ns"]), identity.birthtime_ns),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("archive logical identity is ambiguous")
    if not rows:
        return ()
    row = rows[0]
    raw = str(row["evidence_json"])
    if len(raw) > 65536:
        raise ValueError("archive identification evidence exceeds bound")
    markers = json.loads(raw)
    if not isinstance(markers, list) or len(markers) > 32 or any(not isinstance(item, str) for item in markers):
        raise ValueError("archive identification markers are invalid")
    identified = identify_logical_document(tuple(markers), row["declared_mime"])
    if identified is None or not identified.identified:
        return ()
    if identified.logical_kind != row["logical_kind"] or identified.proposed_extension != row["proposed_extension"]:
        raise ValueError("archive logical identification contradicts its structural evidence")
    if row["identification_status"] != "identified" or row["physical_media_type"] != "application/zip":
        return ()
    ref = AssetDiagnosticEvidenceRef(
        "archive", f"archive-logical:{row['container_key']}:{row['processing_signature']}",
        identity.resource_id, snapshot_id, _digest(dict(row)),
        f"archive-processing:{row['processing_signature']}",
    )
    return (AssetDiagnosticObservation(
        Kind.LOGICAL_FORMAT, identity.resource_id, f"logical_format_{identified.logical_kind}", (ref,),
        missing_checks=("file_integrity_inspection", "opening_verification"),
    ),)


def capture_supplemental_diagnosis(
    paths: KnowledgeStatePaths,
    identity: KnowledgeAssetIdentity,
    snapshot_id: str,
    inventory: KnowledgeAssetHealthFact | None,
) -> tuple[tuple[AssetDiagnosticObservation, ...], tuple[str, ...]]:
    """Read only evidence aligned to the caller's published physical inventory."""

    if inventory is None:
        return (), ("diagnostic_published_inventory_missing",)
    from neocortex.capabilities.formats.archive.state import ARCHIVE_SCHEMA_VERSION, archive_schema_contract
    from neocortex.deduplication.persistence.ddl import SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION
    from neocortex.deduplication.persistence.validation import validate_inventory_schema
    from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

    def validate_archive(connection: sqlite3.Connection) -> None:
        validate_sqlite_schema_contract(connection, archive_schema_contract(), label="archive", exact=True)

    owners: tuple[tuple[str, Path | None, int, Callable[[sqlite3.Connection], None],
                        Callable[[sqlite3.Connection], tuple[AssetDiagnosticObservation, ...]]], ...] = (
        ("inventory", paths.inventory, INVENTORY_SCHEMA_VERSION, validate_inventory_schema,
         lambda connection: _duplicate_observations(connection, identity, snapshot_id)),
        ("archive", paths.archive, ARCHIVE_SCHEMA_VERSION, validate_archive,
         lambda connection: _archive_observations(connection, identity, snapshot_id, inventory)),
    )
    observations: list[AssetDiagnosticObservation] = []
    gaps: list[str] = []
    for owner, path, version, validator, reader in owners:
        if path is None or not path.exists():
            continue
        try:
            with immutable_sqlite_database(path) as connection:
                if read_application_schema_version(connection, label=owner) != version:
                    gaps.append(f"diagnostic_{owner}_schema_incompatible")
                    continue
                validator(connection)
                observations.extend(reader(connection))
        except (ImmutableSQLiteUnavailable, sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError, OSError):
            gaps.append(f"diagnostic_{owner}_evidence_unavailable")
    return tuple(observations), tuple(sorted(gaps))


__all__ = ["capture_supplemental_diagnosis"]
