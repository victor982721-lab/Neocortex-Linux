"""Compose owner targets with durable artifact claims for a full reset.

The walk is bounded to the explicitly selected state directory. Every observed
object has a disposition; a partial walk or an unclaimed object blocks a total
reset. The registry supplies producer roots rather than a second folder list.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from neocortex.runtime.artifact_registry import ArtifactRegistry

ACTIVE_RESET_OPERATION: ContextVar[str | None] = ContextVar("active_state_reset_operation", default=None)

RESET_INVENTORY_POLICY = "neocortex.reset-inventory/v1"
MAX_INVENTORY_ENTRIES = 100_000
MAX_INVENTORY_BYTES = 4 * 1024 * 1024 * 1024
MAX_INVENTORY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ResetInventory:
    nodes: tuple[dict[str, object], ...] = ()
    dependency_edges: tuple[tuple[str, str], ...] = ()
    blockers: tuple[str, ...] = ()
    complete: bool = True
    targets: tuple[Any, ...] = field(default=(), repr=False)
    artifact_records: tuple[Any, ...] = field(default=(), repr=False)
    archive_proofs: tuple[Any, ...] = field(default=(), repr=False)
    surviving_inputs: tuple[Any, ...] = field(default=(), repr=False)

    def as_payload(self) -> dict[str, object]:
        return {"policy": RESET_INVENTORY_POLICY, "nodes": list(self.nodes),
                "dependency_edges": [list(edge) for edge in self.dependency_edges],
                "blockers": list(self.blockers), "complete": self.complete,
                "limits": {"entries": MAX_INVENTORY_ENTRIES, "bytes": MAX_INVENTORY_BYTES,
                           "seconds": MAX_INVENTORY_SECONDS}}


def _path_key(path: Path) -> str:
    return os.fsencode(path).hex()


def observe_reset_inventory(state: Path, scope: Literal["runs", "runs-and-caches", "all"], targets: tuple[Any, ...]) -> ResetInventory:
    if scope != "all" or not state.exists():
        return ResetInventory()
    from neocortex.persistence import state_reset as reset
    from neocortex.capabilities.formats.archive.rebuild import (
        ArchiveManifestReference, ArchiveSurvivingInput,
        assess_archive_materialization_rebuildability,
    )
    registry_root = state / "artifacts"
    records: tuple[Any, ...] = ()
    blockers: list[str] = []
    complete = True
    if registry_root.exists():
        try:
            registry_plan = ArtifactRegistry(registry_root, owner=None).plan()
            records = registry_plan.records
            if registry_plan.truncated or registry_plan.root_blocked or registry_plan.unmanaged:
                blockers.append("registry-observation-incomplete")
                complete = False
        except (OSError, ValueError, RuntimeError) as exc:
            blockers.append(f"registry-unavailable:{type(exc).__name__}")
            complete = False
    selected = {entry.path: (target, entry) for target in targets for entry in target.entries}
    claims: dict[Path, list[Any]] = {}
    nodes: list[dict[str, object]] = []
    edges: list[tuple[str, str]] = []
    extra_targets: list[Any] = []
    archive_proofs: list[Any] = []
    surviving_inputs: list[Any] = []
    disposable_claims: set[str] = set()
    historical_paths: set[Path] = set()
    operation_receipt_paths: set[Path] = set()
    archive_by_destination: dict[str, list[Any]] = {}
    for target in targets:
        if target.owner is not None:
            contract = reset.STATE_STORE_REGISTRY.by_owner(target.owner)
            nodes.append({"node_id": "owner:" + target.owner, "kind": "sqlite-owner",
                          "disposition": target.action, "policy_version": contract.lifecycle_policy_version,
                          "reader_schema": contract.expected_schema_version,
                          "authority": [rule.as_payload() for rule in contract.lifecycle_rules]})
    for record in records:
        if not record.verified and not ArtifactRegistry.retired_claim_is_historical(record, records):
            blockers.append(f"invalid-claim:{record.artifact_id}:{record.issue}")
        if record.state == "retired":
            historical_paths.add(record.path)
            nodes.append({"node_id": record.artifact_id, "kind": "historical-tombstone",
                          "path_bytes": _path_key(record.path), "manifest_digest": record.manifest_digest,
                          "disposition": "preserve", "reason": "completed-retirement-history"})
            continue
        # Reset operation receipts are independent authority and never reset payload.
        if record.purpose == "state-reset-operation":
            operation_receipt_paths.add(record.path)
            if record.state not in {"completed", "retired"} and record.artifact_id != ACTIVE_RESET_OPERATION.get():
                blockers.append(f"pending-reset-operation:{record.artifact_id}")
            claims.setdefault(record.path, []).append(record)
            continue
        claims.setdefault(record.path, []).append(record)
        edges.extend((record.artifact_id, dependency) for dependency in record.dependencies)
        if record.purpose == "archive-materialized-output" and record.state != "retired":
            destination = record.metadata.get("destination")
            if isinstance(destination, str):
                archive_by_destination.setdefault(destination, []).append(record)
        elif record.eligible:
            disposable_claims.add(record.artifact_id)
        elif record.state != "retired" and record.kind in {"operational", "temporary", "cache", "rebuildable"}:
            blockers.append(f"protected-operational-claim:{record.artifact_id}:{record.reason}")
        nodes.append({"node_id": record.artifact_id, "kind": "artifact-claim",
                      "owner": record.owner,
                      "path_bytes": _path_key(record.path), "manifest_digest": record.manifest_digest,
                      "disposition": "retire" if record.eligible else "preserve",
                      "reason": record.reason, "state": record.state,
                      "dependencies": list(record.dependencies)})
    retirement_paths = tuple(entry.path for target in targets if target.action == "remove-files" for entry in target.entries) + tuple(record.path for group in archive_by_destination.values() for record in group)
    for destination, group in sorted(archive_by_destination.items()):
        refs = tuple(ArchiveManifestReference.from_dict(record.metadata["manifest_ref"]) for record in group if isinstance(record.metadata.get("manifest_ref"), dict))
        if len(refs) != len(group):
            blockers.append(f"archive-manifest-missing:{_path_key(Path(destination))}")
            continue
        inputs = []
        for record in group:
            source_ref = record.source_ref
            if isinstance(source_ref, dict):
                source_path = source_ref.get("source_path") or source_ref.get("path")
                if isinstance(source_path, str):
                    inputs.append(ArchiveSurvivingInput(source_path))
        from neocortex.capabilities.formats.archive.rebuild import archive_manifest_surviving_inputs
        authorized_locations = tuple(record.metadata["source_path"] for record in group if isinstance(record.metadata.get("source_path"), str))
        inputs.extend(archive_manifest_surviving_inputs(refs, authorized_locations=authorized_locations))
        proof = assess_archive_materialization_rebuildability(destination, refs, inputs, retirement_set=retirement_paths)
        archive_proofs.extend(proof.outputs)
        surviving_inputs.extend(inputs)
        nodes.append({"node_id": "archive-proof:" + _path_key(Path(destination)),
                      "kind": "rebuild-proof", "proof": proof.to_dict(),
                      "disposition": "preserve", "reason": "required-until-output-retirement-verification"})
        for output_proof in proof.outputs:
            proof_id = "proof:" + _path_key(Path(output_proof.output_path))
            matching = next((record for record in group if str(record.path) == output_proof.output_path), None)
            if matching is not None:
                edges.append((matching.artifact_id, proof_id))
            nodes.append({"node_id": proof_id, "kind": "member-rebuild-proof", "disposition": "preserve",
                          "proof": output_proof.to_dict()})
            if output_proof.source_path is not None:
                source_id = "original:" + _path_key(Path(output_proof.source_path))
                edges.append((proof_id, source_id))
                nodes.append({"node_id": source_id, "kind": "original", "disposition": "preserve",
                              "identity": output_proof.source_identity, "sha256": output_proof.source_sha256})
        if not proof.rebuildable:
            blockers.extend(f"archive:{item}" for item in proof.blockers)
            blockers.extend(f"archive-unknown:{path}" for path in proof.unknown_paths)
        else:
            disposable_claims.update(record.artifact_id for record in group)
            for node in nodes:
                if node.get("node_id") in {record.artifact_id for record in group}:
                    node["disposition"] = "retire"
                    node["reason"] = "archive-rebuild-proof-verified"
    known_paths = reset._known_state_paths(state, scope)
    coordination_paths = set(reset._lock_paths(state)) | {state / "state-reset.lock"}
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    byte_count = count = 0
    pending = [state]
    while pending:
        directory = pending.pop()
        try:
            children = []
            with os.scandir(directory) as iterator:
                for directory_entry in iterator:
                    count += 1
                    if count > MAX_INVENTORY_ENTRIES or time.monotonic() > deadline:
                        complete = False
                        blockers.append("state-observation-budget-exhausted")
                        break
                    children.append(Path(directory_entry.path))
            if not complete and "state-observation-budget-exhausted" in blockers:
                break
            for path in sorted(children, key=os.fsencode):
                metadata = path.lstat()
                if path in coordination_paths:
                    continue
                if path in selected:
                    target, entry = selected[path]
                    disposition, reason = target.action, target.target_id
                    payload = entry.as_payload()
                    for root, overlapping in claims.items():
                        if path == root or root in path.parents:
                            if any(record.artifact_id not in disposable_claims for record in overlapping):
                                blockers.append(f"selected-path-has-protected-claim:{_path_key(path)}")
                else:
                    ancestors = [record for root, root_claims in claims.items()
                                 if path == root or root in path.parents for record in root_claims]
                    direct_claims = claims.get(path, [])
                    eligible = [record for record in ancestors if record.artifact_id in disposable_claims]
                    if direct_claims and len(ancestors) > len(direct_claims):
                        blockers.append(f"overlapping-producer-claims:{_path_key(path)}")
                        eligible = []
                    if eligible and any(record.artifact_id not in disposable_claims for record in ancestors):
                        # Directory ownership never grants authority over a
                        # nested producer's protected or unverified claim.
                        blockers.append(f"overlapping-protected-claim:{_path_key(path)}")
                        eligible = []
                    infrastructure = path in {registry_root, state / "state-reset-operations"} or registry_root in path.parents or path in operation_receipt_paths
                    preserved_area = any(parent.name in reset._PRESERVED_TOP_LEVEL_STATE_NAMES and parent.parent == state for parent in (path, *path.parents))
                    retained_backup = reset._valid_catalog_migration_backup_pair(state, path)
                    if len(direct_claims) > 1:
                        blockers.append(f"conflicting-claims:{_path_key(path)}")
                        eligible = []
                    if eligible:
                        disposition, reason = "remove-files", eligible[0].artifact_id
                    elif ancestors or infrastructure or preserved_area or retained_backup or path in known_paths:
                        disposition, reason = "preserve", "declared-authority-or-coordination"
                    elif stat.S_ISDIR(metadata.st_mode) and any(path in root.parents for root in (*claims, *selected, *historical_paths, *known_paths)):
                        disposition, reason = "preserve", "producer-root-ancestor"
                    else:
                        disposition, reason = "block", "unclaimed-state-object"
                        blockers.append(f"unclaimed:{_path_key(path)}")
                    if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                        blockers.append(f"unsupported-state-object:{_path_key(path)}")
                        payload = {"kind": "unsupported", "device": metadata.st_dev, "inode": metadata.st_ino}
                    elif stat.S_ISDIR(metadata.st_mode):
                        payload = {"kind": "directory", "device": metadata.st_dev, "inode": metadata.st_ino}
                        if disposition == "remove-files":
                            entry = reset._entry_for(state, path)
                            if entry is not None:
                                extra_targets.append(reset.StateResetTarget("artifact:" + reason + ":" + _path_key(path), "managed-artifact", None, "remove-files", (entry,)))
                    else:
                        byte_count += metadata.st_size
                        if byte_count > MAX_INVENTORY_BYTES:
                            complete = False
                            blockers.append("state-observation-byte-budget-exhausted")
                            break
                        entry = reset._entry_for(state, path)
                        if entry is None:
                            raise OSError("observed state entry disappeared")
                        payload = entry.as_payload()
                        if disposition == "remove-files":
                            extra_targets.append(reset.StateResetTarget("artifact:" + reason + ":" + _path_key(path), "managed-artifact", None, "remove-files", (entry,)))
                # Operation infrastructure changes as the authorized reset runs.
                # Its receipts bind their own lifecycle and are observed separately.
                infrastructure = path in {registry_root, state / "state-reset-operations"} or registry_root in path.parents or path in operation_receipt_paths
                if not infrastructure:
                    nodes.append({"node_id": "path:" + _path_key(path), "path_bytes": _path_key(path),
                                  "kind": "state-object", "disposition": disposition, "reason": reason,
                                  "observation": payload})
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and path != registry_root and registry_root not in path.parents:
                    pending.append(path)
        except (OSError, ValueError, RuntimeError) as exc:
            complete = False
            blockers.append(f"state-observation-failed:{type(exc).__name__}")
            break
    # Dependencies protect the source until every dependent is retired. A live
    # dependent outside this plan makes disposal inapplicable before any effect.
    record_ids = {record.artifact_id for record in records} | {str(node["node_id"]) for node in nodes}
    for child, parent in edges:
        if parent not in record_ids:
            blockers.append(f"missing-dependency:{child}:{parent}")
        if parent in disposable_claims and child not in disposable_claims:
            blockers.append(f"retained-dependent:{child}:{parent}")
    # Detect cycles in preview, before any owner transform can be promoted.
    outgoing: dict[str, set[str]] = {key: set() for key in record_ids}
    incoming = dict.fromkeys(record_ids, 0)
    for child, parent in set(edges):
        if parent in record_ids:
            outgoing[child].add(parent)
            incoming[parent] += 1
    ready = [key for key, degree in incoming.items() if degree == 0]
    visited = 0
    while ready:
        child = ready.pop()
        visited += 1
        for parent in outgoing[child]:
            incoming[parent] -= 1
            if incoming[parent] == 0:
                ready.append(parent)
    if visited != len(record_ids):
        blockers.append("artifact-dependency-cycle")
    return ResetInventory(tuple(nodes), tuple(sorted(set(edges))), tuple(sorted(set(blockers))), complete,
                          tuple(extra_targets), records, tuple(archive_proofs), tuple(surviving_inputs))


def retire_inventory_targets(plan: Any, entries: tuple[Any, ...], registry: ArtifactRegistry, delete: Any) -> tuple[Any, ...]:
    """Execute exact claimed effects inside the producer's registry guards."""
    if plan.inventory is None or not plan.inventory.artifact_records:
        return delete(entries)
    from neocortex.capabilities.formats.archive.rebuild import archive_output_retirement_guard
    inventory = plan.inventory
    proofs = {Path(proof.output_path): proof for proof in inventory.archive_proofs if proof.rebuildable}
    remaining = {entry.path: entry for entry in entries}
    deleted = []
    candidates = [record for record in inventory.artifact_records
                  if record.state != "retired"
                  and (record.path in remaining or any(record.path in path.parents for path in remaining))]
    # Dependents precede their inputs. Cycles cannot confer retirement authority.
    pending = {record.artifact_id: record for record in candidates}
    ordered = []
    while pending:
        referenced = {dependency for record in pending.values() for dependency in record.dependencies}
        ready = sorted(key for key in pending if key not in referenced)
        if not ready:
            raise RuntimeError("reset artifact dependency graph contains a cycle")
        for key in ready:
            ordered.append(pending.pop(key))
    for record in ordered:
        group = tuple(entry for path, entry in remaining.items() if path == record.path or record.path in path.parents)
        if not group:
            continue
        owned_registry = registry.for_owner(record.owner)
        operation_id = ACTIVE_RESET_OPERATION.get()
        if operation_id is None:
            raise RuntimeError("artifact reset retirement requires a durable operation")
        owned_registry.prepare_retirement_compensation(record.artifact_id, recovery_artifact_id=operation_id)
        proof = proofs.get(record.path)
        if proof is not None:
            guard = archive_output_retirement_guard(proof, owned_registry, inventory.surviving_inputs,
                                                    retirement_set=tuple(remaining))
        else:
            guard = owned_registry.retirement_guard(record.artifact_id)
        with guard:
            deleted.extend(delete(group))
            if any(os.path.lexists(entry.path) for entry in group):
                raise RuntimeError("reset artifact removal was not verified")
            if proof is not None:
                from neocortex.persistence import state_reset as reset
                source = Path(proof.source_path)
                metadata = source.lstat()
                if ((metadata.st_dev, metadata.st_ino) != proof.source_identity[:2]
                        or reset._sha256(source) != proof.source_sha256):
                    raise RuntimeError("Archive source changed during reset output retirement")
            owned_registry.update(record.artifact_id, state="retired")
        for entry in group:
            remaining.pop(entry.path, None)
    deleted.extend(delete(tuple(remaining.values())))
    return tuple(deleted)
