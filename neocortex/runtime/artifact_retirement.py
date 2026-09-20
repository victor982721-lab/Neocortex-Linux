"""Artifact retirement, recovery, and terminal tombstone retention.

The registry class supplies storage, observation, and owner policy.  This
module composes those primitives into the effect/recovery state machines while
keeping the physical effect outside this owner.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from .artifact_models import (
    MANIFEST_SUFFIX, ArtifactConflictError, ArtifactManifestError,
    ArtifactRecord, ArtifactRegistryError, ArtifactRootError, ArtifactSecurityError,
    ArtifactState, MAX_ARTIFACT_ID_BYTES, _MAX_RETENTION_RECEIPT_BYTES, _RETIREMENT_CONFIRMED_PHASE, _RETIREMENT_KEY,
    _RETIREMENT_PENDING_PHASES, _TOMBSTONE_RETENTION_DIR, _TOMBSTONE_RETENTION_SCHEMA,
    _bounded_mapping, _bounded_text, _identity, _private_directory_issue,
)
from .artifact_manifest import _manifest_file_issue
from .artifact_storage import (
    _retention_receipt_digest, _retention_receipt_name, _write_json_atomic,
)


@contextmanager
def retirement_guard(registry, artifact: str | ArtifactRecord) -> Iterator[ArtifactRecord]:
    """Serialize policy/dependency release with the physical effect.

    Entering the guard durably records an ``applying`` intent in the
    artifact manifest before the caller unlinks anything.  If the caller
    raises after the path has disappeared, the intent is changed to
    ``applied_unverified`` so a fresh process can confirm the tombstone
    without repeating the physical effect.  If the path remains, the
    intent becomes ``recovery_required`` and the artifact is preserved.
    """

    artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactRecord) else artifact
    artifact_id = _bounded_text(
        artifact_id,
        label="artifact_id",
        limit=MAX_ARTIFACT_ID_BYTES,
    )
    registry._ensure_root(create=False)
    with registry._registry_lock():
        yield from registry._retirement_guard_locked(artifact_id)

def retirement_guard_locked(
    registry,
    artifact: str | ArtifactRecord,
    *,
    records: tuple[ArtifactRecord, ...] | None = None,
    unmanaged: tuple[Path, ...] = (),
    truncated: bool = False,
) -> Iterator[ArtifactRecord]:
    if registry.owner is None:
        raise ArtifactSecurityError("federated artifact registry view is read-only for retirement")
    artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactRecord) else artifact
    artifact_id = _bounded_text(
        artifact_id,
        label="artifact_id",
        limit=MAX_ARTIFACT_ID_BYTES,
    )
    current = registry._load_record(registry._manifest_path(artifact_id))
    if not current.verified:
        raise ArtifactSecurityError(
            current.reason or "artifact cannot be retired after drift"
        )
    if registry.owner is not None and current.owner != registry.owner:
        raise ArtifactSecurityError("artifact owner does not match this registry")
    if records is None:
        records, unmanaged, truncated, _reasons = registry._scan_records(
            max_records=registry.max_records,
        )
    if not registry._dependency_observation_complete(
        records,
        unmanaged,
        truncated=truncated,
    ):
        raise ArtifactSecurityError("dependency observation incomplete")
    category, reason = registry._classify_for_owner(current, now_ns=time.time_ns())
    if category != "eligible":
        raise ArtifactSecurityError(f"artifact policy prevents retirement: {reason}")
    dependents = registry._live_dependents(current, records)
    if dependents:
        raise ArtifactSecurityError(
            "artifact has live dependents: " + ", ".join(dependents[:16])
        )
    prepared = registry._prepare_retirement_locked(current)
    try:
        yield prepared
    except BaseException:
        # Never let a recovery bookkeeping failure hide the primary effect
        # error.  The original manifest remains protected if this write is
        # itself interrupted; a later recover_retirements() can retry the
        # metadata-only reconciliation.
        try:
            registry._record_retirement_failure_locked(prepared)
        except BaseException:
            pass
        raise
    else:
        # A caller may use the guard only to inspect/coordinate and then
        # decide not to perform the effect.  If the path and completed
        # state are still intact, remove the pending intent so it does
        # not permanently block a valid dependency acquisition.  A
        # missing path or terminal state is left durable for recovery.
        try:
            latest = registry._load_record(registry._manifest_path(prepared.artifact_id))
            if (
                latest.state == ArtifactState.COMPLETED.value
                and latest.verified
                and latest.path.exists()
            ):
                registry._clear_retirement_intent_locked(latest)
        except BaseException:
            # Conservatively retain the intent; the next apply/recovery
            # pass will reconcile it without repeating an effect.
            pass

def prepare_retirement(registry, record: ArtifactRecord) -> ArtifactRecord:
    existing = registry._retirement_claim(record)
    if existing is not None and existing.get("phase") in _RETIREMENT_PENDING_PHASES:
        raise ArtifactSecurityError("artifact retirement already requires recovery")
    operation_id = uuid.uuid4().hex
    metadata = registry._metadata_with_retirement(
        record,
        phase="applying",
        operation_id=operation_id,
        applying_ns=time.time_ns(),
    )
    updated = replace(
        record,
        metadata=metadata,
        updated_ns=max(record.updated_ns, time.time_ns()),
    )
    payload = registry._payload_from_record(updated)
    manifest_path = record.manifest_path or registry._manifest_path(record.artifact_id)
    _write_json_atomic(manifest_path, payload, exclusive=False)
    metadata_stat = manifest_path.lstat()
    issue = _manifest_file_issue(metadata_stat)
    if issue is not None:
        raise ArtifactManifestError(f"published manifest failed safety check: {issue}")
    return registry._record_from_payload(
        payload,
        manifest_path=manifest_path,
    )

def record_retirement_failure(registry, record: ArtifactRecord) -> None:
    """Persist the post-exception observation without performing effects."""

    manifest_path = record.manifest_path or registry._manifest_path(record.artifact_id)
    try:
        payload = registry._read_manifest_payload(manifest_path)
        raw = registry._record_from_payload(payload, manifest_path=manifest_path, revalidate=False)
        claim = registry._retirement_claim(raw)
        if claim is None:
            return
        try:
            raw.path.lstat()
        except FileNotFoundError:
            phase = "applied_unverified"
            observed: dict[str, Any] = {
                "observed_path_exists": False,
                "observed_ns": time.time_ns(),
            }
            state = raw.state
        else:
            phase = "recovery_required"
            observed = {
                "observed_path_exists": True,
                "observed_ns": time.time_ns(),
            }
            state = ArtifactState.RECOVERY_REQUIRED.value
        metadata = registry._metadata_with_retirement(
            raw,
            phase=phase,
            operation_id=str(claim["operation_id"]),
            **observed,
        )
        updated = replace(
            raw,
            state=state,
            metadata=metadata,
            updated_ns=max(raw.updated_ns, time.time_ns()),
        )
        updated_payload = registry._payload_from_record(updated)
        registry._write_existing_registration(manifest_path, updated_payload)
    except FileNotFoundError:
        # A missing manifest cannot be safely reconstructed.  Preserve the
        # absence as an external recovery obligation rather than inventing
        # a retired row.
        return

def clear_retirement_intent(registry, record: ArtifactRecord) -> None:
    metadata = dict(record.metadata)
    metadata.pop(_RETIREMENT_KEY, None)
    updated = replace(
        record,
        metadata=_bounded_mapping(metadata, label="artifact metadata"),
        updated_ns=max(record.updated_ns, time.time_ns()),
    )
    manifest_path = record.manifest_path or registry._manifest_path(record.artifact_id)
    registry._write_existing_registration(manifest_path, registry._payload_from_record(updated))

def recover_retirements(registry) -> dict[str, object]:
    """Reconcile durable retirement intents without repeating effects.

    A missing claimed path plus an ``applying``/``applied_unverified``
    intent is sufficient evidence that the physical effect happened.  The
    recovery only publishes the terminal manifest state; it never creates
    or removes a workspace.  Existing paths are retained and moved to a
    recovery-required state for explicit reconciliation.
    """

    if registry.owner is None:
        raise ArtifactSecurityError(
            "federated artifact registry view is read-only for recovery"
        )
    if not registry._ensure_root(create=False):
        return {
            "schema": "neocortex.artifact-retirement-recovery/v1",
            "status": "ready",
            "confirmed": 0,
            "recovery_required": 0,
            "truncated": False,
        }
    entries, truncated, _reasons = registry._scan_entries(max_records=registry.max_records)
    confirmed: list[str] = []
    recovery: list[str] = []
    for entry in entries:
        if not entry.name.endswith(MANIFEST_SUFFIX):
            continue
        manifest_path = registry.root / entry.name
        try:
            payload = registry._read_manifest_payload(manifest_path)
            raw = registry._record_from_payload(
                payload,
                manifest_path=manifest_path,
                revalidate=False,
            )
            claim = registry._retirement_claim(raw)
        except (ArtifactRegistryError, OSError, TypeError, ValueError):
            continue
        # A root-wide lock coordinates owners; it does not authorize this
        # owner to reconcile another producer's durable intent.
        if raw.owner != registry.owner:
            continue
        if claim is None or claim.get("phase") not in _RETIREMENT_PENDING_PHASES:
            continue
        try:
            raw.path.lstat()
        except FileNotFoundError:
            metadata = registry._metadata_with_retirement(
                raw,
                phase=_RETIREMENT_CONFIRMED_PHASE,
                operation_id=str(claim["operation_id"]),
                observed_path_exists=False,
                confirmed_ns=time.time_ns(),
                receipt={"effect": "removed", "replayed": True},
            )
            updated = replace(
                raw,
                state=ArtifactState.RETIRED.value,
                metadata=metadata,
                updated_ns=max(raw.updated_ns, time.time_ns()),
            )
            updated_payload = registry._payload_from_record(updated)
            registry._write_existing_registration(manifest_path, updated_payload)
            confirmed.append(raw.artifact_id)
        else:
            metadata = registry._metadata_with_retirement(
                raw,
                phase="recovery_required",
                operation_id=str(claim["operation_id"]),
                observed_path_exists=True,
                recovery_ns=time.time_ns(),
            )
            updated = replace(
                raw,
                state=ArtifactState.RECOVERY_REQUIRED.value,
                metadata=metadata,
                updated_ns=max(raw.updated_ns, time.time_ns()),
            )
            updated_payload = registry._payload_from_record(updated)
            registry._write_existing_registration(manifest_path, updated_payload)
            recovery.append(raw.artifact_id)
    return {
        "schema": "neocortex.artifact-retirement-recovery/v1",
        "status": "blocked" if recovery or truncated else "applied",
        "confirmed": len(confirmed),
        "confirmed_artifacts": confirmed,
        "recovery_required": len(recovery),
        "recovery_artifacts": recovery,
        "truncated": truncated,
    }

def apply_tombstone_retention(
    registry,
    artifact_ids: Iterable[str],
    *,
    release_authorized: bool,
    evidence: Mapping[str, Any] | None = None,
    operation_id: str | None = None,
) -> dict[str, object]:
    """Prune explicitly released retired manifests, never their payloads.

    This is deliberately narrower than artifact retirement.  It accepts
    only already-retired, verified tombstones whose claimed payload is
    absent and whose metadata does not retain recovery/replay/pin/grant
    obligations.  A small durable receipt is written before each unlink;
    replay observes missing manifests and confirms the prior effect rather
    than attempting it again.  Unknown, corrupt, duplicate or truncated
    selections remain blocked.
    """

    if registry.owner is None:
        raise ArtifactSecurityError(
            "federated artifact registry view is read-only for retention"
        )
    if type(release_authorized) is not bool or not release_authorized:
        raise ArtifactSecurityError("tombstone retention requires release authorization")
    if not isinstance(evidence, Mapping) or not evidence:
        raise ArtifactSecurityError("tombstone retention requires explicit evidence")
    evidence_payload = _bounded_mapping(
        evidence,
        label="tombstone retention evidence",
        limit=_MAX_RETENTION_RECEIPT_BYTES,
    )
    operation = operation_id or uuid.uuid4().hex
    operation = _bounded_text(operation, label="tombstone retention operation", limit=128)
    identifiers: list[str] = []
    seen: set[str] = set()
    for raw in artifact_ids:
        identifier = _bounded_text(raw, label="tombstone artifact id", limit=MAX_ARTIFACT_ID_BYTES)
        if identifier in seen:
            raise ArtifactSecurityError("tombstone retention selection contains duplicates")
        seen.add(identifier)
        identifiers.append(identifier)
        if len(identifiers) > min(registry.max_records, 1_000):
            raise ArtifactSecurityError("tombstone retention selection is truncated")

    retention_root = registry.root / _TOMBSTONE_RETENTION_DIR
    try:
        retention_root.mkdir(mode=0o700, exist_ok=True)
        metadata = retention_root.lstat()
    except OSError as exc:
        raise ArtifactRootError("tombstone retention journal is unavailable") from exc
    if _private_directory_issue(metadata, root=True) is not None:
        raise ArtifactRootError("tombstone retention journal failed its private-directory check")
    receipt_path = retention_root / _retention_receipt_name(operation)
    try:
        existing_raw = receipt_path.read_bytes()
    except FileNotFoundError:
        existing_raw = None
    except OSError as exc:
        raise ArtifactManifestError("tombstone retention receipt is unavailable") from exc
    if existing_raw is not None:
        try:
            existing = json.loads(existing_raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactManifestError("tombstone retention receipt is invalid") from exc
        if not isinstance(existing, Mapping) or existing.get("receipt_digest") != _retention_receipt_digest(existing):
            raise ArtifactManifestError("tombstone retention receipt digest mismatch")
        if existing.get("operation_id") != operation:
            raise ArtifactConflictError("tombstone retention operation id collides")
        return dict(existing)

    receipt: dict[str, Any] = {
        "schema": _TOMBSTONE_RETENTION_SCHEMA,
        "operation_id": operation,
        "owner": registry.owner,
        "artifact_ids": identifiers,
        "evidence": evidence_payload,
        "release_authorized": True,
        "status": "applying",
        "items": [],
        "created_ns": time.time_ns(),
    }

    def write_receipt() -> None:
        receipt["receipt_digest"] = _retention_receipt_digest(receipt)
        _write_json_atomic(receipt_path, receipt, exclusive=not receipt_path.exists())

    write_receipt()
    blocked: list[dict[str, object]] = []
    confirmed: list[str] = []
    for identifier in identifiers:
        try:
            manifest_path = registry._manifest_path(identifier)
            if not manifest_path.exists():
                blocked.append(
                    {"artifact_id": identifier, "reason": "tombstone manifest is absent"}
                )
                receipt["items"].append(
                    {
                        "artifact_id": identifier,
                        "phase": "recovery_required",
                        "reason": "tombstone manifest is absent",
                    }
                )
                write_receipt()
                continue
            record = registry._load_record(manifest_path)
            if not record.verified or record.owner != registry.owner:
                raise ArtifactSecurityError("tombstone is not verified by this owner")
            if record.state != ArtifactState.RETIRED.value:
                raise ArtifactSecurityError("only retired tombstones can be pruned")
            if record.dependencies:
                raise ArtifactSecurityError("tombstone retains dependency claims")
            claim = registry._retirement_claim(record)
            if claim is not None and claim.get("phase") in _RETIREMENT_PENDING_PHASES:
                raise ArtifactSecurityError("tombstone has pending recovery")
            record_metadata = record.metadata
            protected_keys = (
                "pinned",
                "pin",
                "grant_active",
                "authorization_active",
                "replay_required",
                "recovery_required",
                "evidence_required",
            )
            if any(record_metadata.get(key) is True for key in protected_keys):
                raise ArtifactSecurityError("tombstone retains an active obligation")
            try:
                record.path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ArtifactSecurityError("retired payload is still present")
            manifest_metadata = manifest_path.lstat()
            issue = _manifest_file_issue(manifest_metadata)
            if issue is not None:
                raise ArtifactManifestError(f"tombstone manifest failed safety check: {issue}")
            item: dict[str, Any] = {
                "artifact_id": identifier,
                "manifest": str(manifest_path),
                "manifest_identity": list(_identity(manifest_metadata)),
                "phase": "applying",
            }
            receipt["items"].append(item)
            write_receipt()
            current = manifest_path.lstat()
            if _identity(current) != tuple(item["manifest_identity"]):
                raise ArtifactSecurityError("tombstone manifest changed before pruning")
            os.unlink(manifest_path)
            directory_fd = os.open(registry.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            item["phase"] = "confirmed"
            item["confirmed_ns"] = time.time_ns()
            confirmed.append(identifier)
            write_receipt()
        except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
            blocked.append({"artifact_id": identifier, "reason": str(exc)})
            receipt["items"].append(
                {"artifact_id": identifier, "phase": "recovery_required", "reason": str(exc)}
            )
            write_receipt()

    receipt["status"] = "recovery_required" if blocked else "applied"
    receipt["confirmed"] = len(confirmed)
    receipt["blocked"] = blocked
    receipt["completed_ns"] = time.time_ns()
    write_receipt()
    return dict(receipt)

def recover_tombstone_retention(registry) -> dict[str, object]:
    """Confirm or preserve interrupted tombstone-retention receipts."""

    if registry.owner is None:
        raise ArtifactSecurityError(
            "federated artifact view is read-only for retention recovery"
        )
    journal = registry.root / _TOMBSTONE_RETENTION_DIR
    if not journal.exists():
        return {"schema": _TOMBSTONE_RETENTION_SCHEMA, "status": "ready", "confirmed": 0, "recovery_required": 0}
    entries = sorted(journal.glob("receipt-*.json"))[: min(registry.max_records, 1_000)]
    confirmed = 0
    recovery_required = 0
    for receipt_path in entries:
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping) or payload.get("receipt_digest") != _retention_receipt_digest(payload):
                recovery_required += 1
                continue
            if payload.get("owner") != registry.owner:
                recovery_required += 1
                continue
            changed = False
            items = payload.get("items")
            if not isinstance(items, list):
                recovery_required += 1
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("phase") != "applying":
                    continue
                manifest_value = item.get("manifest")
                if not isinstance(manifest_value, str) or Path(manifest_value).parent != registry.root:
                    item["phase"] = "recovery_required"
                    item["reason"] = "manifest path is not registry-local"
                    changed = True
                    continue
                if not Path(manifest_value).exists():
                    item["phase"] = "confirmed"
                    item["replayed"] = True
                    changed = True
                else:
                    item["phase"] = "recovery_required"
                    item["reason"] = "manifest still exists after interrupted effect"
                    changed = True
            if changed:
                phases = [item.get("phase") for item in items if isinstance(item, Mapping)]
                payload = dict(payload)
                payload["status"] = "recovery_required" if "recovery_required" in phases else "applied"
                payload["items"] = items
                payload["recovered_ns"] = time.time_ns()
                payload["receipt_digest"] = _retention_receipt_digest(payload)
                _write_json_atomic(receipt_path, payload, exclusive=False)
            if payload.get("status") == "recovery_required":
                recovery_required += 1
            else:
                confirmed += 1
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            recovery_required += 1
    return {
        "schema": _TOMBSTONE_RETENTION_SCHEMA,
        "status": "recovery_required" if recovery_required else "applied",
        "confirmed": confirmed,
        "recovery_required": recovery_required,
    }
