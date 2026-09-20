"""Registration and durable field transitions for ArtifactRegistry.

The registry class owns the lock boundary; these functions own the large
normalization and candidate-building workflows for registration and update.
They deliberately call the registry's storage/revalidation primitives instead
of opening another connection or introducing a second owner.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from dataclasses import replace
from typing import Any

from .artifact_models import (
    ARTIFACT_KINDS, ARTIFACT_REGISTRY_SCHEMA, ARTIFACT_STATES, ArtifactConflictError, ArtifactKind,
    ArtifactRecord, ArtifactRegistryError, ArtifactSecurityError, ArtifactState, MAX_ARTIFACT_ID_BYTES,
    MAX_TEXT_BYTES,
    _DEFAULT_PRODUCER, _DEFAULT_PURPOSE, _RETIREMENT_KEY,
    _RETIREMENT_CONFIRMED_PHASE, _RETIREMENT_PENDING_PHASES, _UNSET, _bounded_json,
    _bounded_identity, _bounded_mapping, _bounded_run_id, _bounded_text, _canonical_json, _identity, _lstat,
    _private_artifact_issue, _private_directory_issue, _validate_absolute_path,
)
from .artifact_manifest import _MANIFEST_FIELDS, _manifest_digest
from .artifact_storage import _write_json_atomic


def register(
    registry,
    artifact_id: str | ArtifactRecord,
    producer: str | None = None,
    path: Path | str | None = None,
    *,
    owner: str | None = None,
    run_id: int | str | None = None,
    purpose: str | None = None,
    root: Path | str | None = None,
    kind: str = ArtifactKind.TEMPORARY.value,
    state: str = ArtifactState.ACTIVE.value,
    source_ref: Any = None,
    digest: str | None = None,
    dependencies: Iterable[str] | None = None,
    retain_until_ns: int | None = None,
    ttl_ns: int | None = None,
    ttl: int | None = None,
    disposable: bool = True,
    metadata: Mapping[str, Any] | None = None,
    created_ns: int | None = None,
    updated_ns: int | None = None,
    path_identity: Sequence[int] | None = None,
    identity: Sequence[int] | None = None,
    root_identity: Sequence[int] | None = None,
    lifecycle_state: str | None = None,
    retain_on_success: bool | None = None,
    retention: Mapping[str, Any] | None = None,
    manifest_digest: str | None = None,
) -> ArtifactRecord:
    """Register one claim, or replay an identical existing registration.

    Registration is an explicit write and may create the configured root.
    ``plan``/``verify`` never use that path.  An existing id is returned
    unchanged only when every durable registration field matches; a
    conflicting or invalid manifest is preserved and rejected.
    """

    if isinstance(artifact_id, ArtifactRecord):
        source_record = artifact_id
        if producer is None:
            producer = source_record.producer
        if path is None:
            path = source_record.path
        if owner is None:
            owner = source_record.owner
        if run_id is None:
            run_id = source_record.run_id
        if purpose is None:
            purpose = source_record.purpose
        if root is None:
            root = source_record.root
        if kind == ArtifactKind.TEMPORARY.value:
            kind = source_record.kind
        if state == ArtifactState.ACTIVE.value:
            state = source_record.state
        if source_ref is None:
            source_ref = source_record.source_ref
        if digest is None:
            digest = source_record.digest
        if dependencies is None:
            dependencies = source_record.dependencies
        if retain_until_ns is None:
            retain_until_ns = source_record.retain_until_ns
        if ttl_ns is None:
            ttl_ns = source_record.ttl_ns
        if ttl is None and source_record.ttl_ns is not None:
            ttl = source_record.ttl_ns
        if disposable is True:
            disposable = source_record.disposable
        if metadata is None:
            metadata = source_record.metadata
        if created_ns is None:
            created_ns = source_record.created_ns
        if updated_ns is None:
            updated_ns = source_record.updated_ns
        artifact_id = source_record.artifact_id
    artifact_id = _bounded_text(
        artifact_id,
        label="artifact_id",
        limit=MAX_ARTIFACT_ID_BYTES,
    )
    if registry.owner is None:
        raise ArtifactSecurityError(
            "federated artifact registry view is read-only for registration"
        )
    if owner is None:
        owner = registry.owner
    owner = _bounded_text(owner, label="artifact owner")
    if owner != registry.owner:
        raise ArtifactSecurityError("artifact owner does not match this registry")
    producer = _bounded_text(
        _DEFAULT_PRODUCER if producer is None else producer,
        label="artifact producer",
    )
    purpose = _bounded_text(
        _DEFAULT_PURPOSE if purpose is None else purpose,
        label="artifact purpose",
    )
    if path is None:
        raise ValueError("artifact path is required")
    path = _validate_absolute_path(path, label="artifact path")
    root = registry.root if root is None else _validate_absolute_path(root, label="artifact root")
    if lifecycle_state is not None:
        if state != ArtifactState.ACTIVE.value and state != lifecycle_state:
            raise ValueError("state and lifecycle_state disagree")
        state = lifecycle_state
    if retention is not None:
        if not isinstance(retention, Mapping):
            raise TypeError("artifact retention must be an object")
        if retain_until_ns is None:
            candidate_deadline = retention.get("retain_until_ns")
            if candidate_deadline is not None:
                retain_until_ns = candidate_deadline
        if ttl_ns is None:
            candidate_ttl = retention.get("ttl_ns")
            if candidate_ttl is not None:
                ttl_ns = candidate_ttl
    if retain_on_success is not None and type(retain_on_success) is not bool:
        raise TypeError("artifact retain_on_success must be a boolean or null")
    if retain_on_success is False and state == ArtifactState.COMPLETED.value:
        # The explicit disposable field remains authoritative; this
        # compatibility hint only prevents a producer from accidentally
        # making a retained scratch result appear non-disposable.
        disposable = bool(disposable)
    if manifest_digest is not None and digest is None:
        digest = manifest_digest
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unsupported artifact kind: {kind!r}")
    if state not in ARTIFACT_STATES:
        raise ValueError(f"unsupported artifact state: {state!r}")
    run_id = _bounded_run_id(run_id)
    if ttl is not None:
        if ttl_ns is not None and ttl_ns != ttl:
            raise ValueError("ttl and ttl_ns disagree")
        ttl_ns = ttl
    for name, value in (("retain_until_ns", retain_until_ns), ("ttl_ns", ttl_ns)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"artifact {name} must be a non-negative integer or null")
    normalized_dependencies = registry._normalize_dependencies(dependencies)
    # Dependencies are live registry claims.  Validate them while the
    # same directory lock used by retirement is held so acquisition cannot
    # race a physical retirement of the target.
    registry._validate_dependencies_locked(
        normalized_dependencies,
        artifact_id=artifact_id,
    )
    if digest is not None:
        digest = _bounded_text(digest, label="artifact digest", limit=MAX_TEXT_BYTES)
    normalized_source_ref = _bounded_json(
        source_ref,
        label="artifact source_ref",
        limit=MAX_TEXT_BYTES,
    )
    normalized_metadata = _bounded_mapping(
        metadata,
        label="artifact metadata",
        limit=registry.max_metadata_bytes,
    )
    if type(disposable) is not bool:
        raise TypeError("artifact disposable must be a boolean")
    registry._ensure_root(create=True)
    artifact_root_metadata = _lstat(root, label="artifact root")
    root_issue = _private_directory_issue(artifact_root_metadata, root=True)
    if root_issue is not None:
        raise ArtifactSecurityError(f"artifact root failed safety check: {root_issue}")
    artifact_root_identity = _identity(artifact_root_metadata)
    path_metadata = _lstat(path, label="artifact path")
    path_issue = _private_artifact_issue(path_metadata)
    if path_issue is not None:
        raise ArtifactSecurityError(f"artifact path failed safety check: {path_issue}")
    if path == registry.root:
        raise ArtifactSecurityError("artifact path cannot be the registry root")
    actual_path_identity = _identity(path_metadata)
    supplied_identity = path_identity if path_identity is not None else identity
    if supplied_identity is not None:
        expected_path_identity = _bounded_identity(supplied_identity, label="artifact path_identity")
        if expected_path_identity != actual_path_identity:
            raise ArtifactSecurityError("artifact path identity does not match the live path")
    if root_identity is not None:
        expected_root_identity = _bounded_identity(root_identity, label="artifact root_identity")
        if expected_root_identity != artifact_root_identity:
            raise ArtifactSecurityError("artifact root identity does not match the live root")
    now = time.time_ns() if created_ns is None else created_ns
    if type(now) is not int or now < 0:
        raise ValueError("artifact created_ns must be a non-negative integer")
    updated = now if updated_ns is None else updated_ns
    if type(updated) is not int or updated < now:
        raise ValueError("artifact updated_ns must be >= created_ns")
    if retain_until_ns is None and ttl_ns is not None:
        retain_until_ns = now + ttl_ns
    candidate = ArtifactRecord(
        artifact_id=artifact_id,
        owner=owner,
        producer=producer,
        run_id=run_id,
        purpose=purpose,
        path=path,
        # ``registry.root`` owns the registry manifests; ``root`` is the
        # separately claimed artifact boundary.  Preserve the latter in
        # the durable record so valid split-root registrations do not
        # manufacture an ``artifact_root_drift`` on first read.
        root=root,
        path_identity=actual_path_identity,
        root_identity=artifact_root_identity,
        kind=kind,
        state=state,
        created_ns=now,
        updated_ns=updated,
        source_ref=normalized_source_ref,
        digest=digest,
        dependencies=normalized_dependencies,
        retain_until_ns=retain_until_ns,
        ttl_ns=ttl_ns,
        disposable=disposable,
        metadata=normalized_metadata,
        path_size_bytes=max(0, int(path_metadata.st_size)),
        path_mtime_ns=max(0, int(path_metadata.st_mtime_ns)),
    )
    payload = registry._payload_from_record(candidate)
    manifest_path = registry._manifest_path(artifact_id)
    try:
        registry._write_registration(manifest_path, payload)
    except FileExistsError:
        try:
            existing = registry._load_record(manifest_path)
        except ArtifactRegistryError as exc:
            raise ArtifactConflictError("artifact id is already bound to an invalid manifest") from exc
        if registry._registration_equal(existing, candidate):
            return existing
        raise ArtifactConflictError("artifact id is already registered with different fields") from None
    return registry._load_record(manifest_path)

def update(
    registry,
    artifact: str | ArtifactRecord,
    *,
    producer: str | object = _UNSET,
    run_id: int | str | object | None = _UNSET,
    purpose: str | object = _UNSET,
    state: str | object = _UNSET,
    source_ref: Any = _UNSET,
    digest: str | object | None = _UNSET,
    dependencies: Iterable[str] | object | None = _UNSET,
    retain_until_ns: int | object | None = _UNSET,
    ttl_ns: int | object | None = _UNSET,
    ttl: int | object | None = _UNSET,
    disposable: bool | object = _UNSET,
    metadata: Mapping[str, Any] | object | None = _UNSET,
    updated_ns: int | None = None,
) -> ArtifactRecord:
    """Atomically update durable fields after a fresh revalidation.

    An update with no effective change is a replay-safe no-op and does not
    rewrite the manifest or advance ``updated_ns``.
    """

    artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactRecord) else artifact
    artifact_id = _bounded_text(
        artifact_id,
        label="artifact_id",
        limit=MAX_ARTIFACT_ID_BYTES,
    )
    registry._ensure_root(create=False)
    manifest_path = registry._manifest_path(artifact_id)
    current = registry._load_record(manifest_path)
    try:
        current_retirement = registry._retirement_claim(current)
    except ArtifactSecurityError:
        raise
    retiring_missing_path = (
        state == ArtifactState.RETIRED.value and current.issue == "artifact_missing"
        and current_retirement is not None
        and current_retirement.get("phase") in _RETIREMENT_PENDING_PHASES
    )
    if not current.verified and not retiring_missing_path:
        raise ArtifactSecurityError(current.reason or "artifact cannot be updated after drift")
    if registry.owner is None:
        raise ArtifactSecurityError(
            "federated artifact registry view is read-only for update"
        )
    if current.owner != registry.owner:
        raise ArtifactSecurityError("artifact owner does not match this registry")
    updates: dict[str, Any] = {}
    for name, value in (
        ("producer", producer),
        ("run_id", run_id),
        ("purpose", purpose),
        ("state", state),
        ("source_ref", source_ref),
        ("digest", digest),
        ("dependencies", dependencies),
        ("retain_until_ns", retain_until_ns),
        ("ttl_ns", ttl_ns),
        ("disposable", disposable),
        ("metadata", metadata),
    ):
        if value is not _UNSET:
            updates[name] = value
    if ttl is not _UNSET:
        if "ttl_ns" in updates and updates["ttl_ns"] != ttl:
            raise ValueError("ttl and ttl_ns disagree")
        updates["ttl_ns"] = ttl
    normalized: dict[str, Any] = {}
    if "producer" in updates:
        normalized["producer"] = _bounded_text(updates["producer"], label="artifact producer")
    if "run_id" in updates:
        normalized["run_id"] = _bounded_run_id(updates["run_id"])
    if "purpose" in updates:
        normalized["purpose"] = _bounded_text(updates["purpose"], label="artifact purpose")
    if "state" in updates:
        if updates["state"] not in ARTIFACT_STATES:
            raise ValueError(f"unsupported artifact state: {updates['state']!r}")
        normalized["state"] = updates["state"]
    if "source_ref" in updates:
        normalized["source_ref"] = _bounded_json(
            updates["source_ref"],
            label="artifact source_ref",
            limit=MAX_TEXT_BYTES,
        )
    if "digest" in updates:
        normalized["digest"] = (
            None
            if updates["digest"] is None
            else _bounded_text(updates["digest"], label="artifact digest", limit=MAX_TEXT_BYTES)
        )
    if "dependencies" in updates:
        normalized["dependencies"] = list(registry._normalize_dependencies(updates["dependencies"]))
        registry._validate_dependencies_locked(
            tuple(normalized["dependencies"]),
            artifact_id=current.artifact_id,
        )
    if "retain_until_ns" in updates:
        normalized["retain_until_ns"] = updates["retain_until_ns"]
    if "ttl_ns" in updates:
        normalized["ttl_ns"] = updates["ttl_ns"]
    for name in ("retain_until_ns", "ttl_ns"):
        value = normalized.get(name, getattr(current, name))
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"artifact {name} must be a non-negative integer or null")
    if "disposable" in updates:
        if type(updates["disposable"]) is not bool:
            raise TypeError("artifact disposable must be a boolean")
        normalized["disposable"] = updates["disposable"]
    if "metadata" in updates:
        normalized["metadata"] = _bounded_mapping(
            updates["metadata"],
            label="artifact metadata",
            limit=registry.max_metadata_bytes,
        )
    merged = {name: getattr(current, name) for name in _MANIFEST_FIELDS if name not in {"schema", "manifest_digest"}}
    merged.update(normalized)
    if merged.get("ttl_ns") is not None and "retain_until_ns" in updates:
        # An explicit retain_until remains authoritative.  If only ttl is
        # changed, derive a new deadline from the immutable creation time.
        pass
    elif "ttl_ns" in normalized:
        ttl_value = normalized["ttl_ns"]
        if ttl_value is None:
            merged["retain_until_ns"] = None
        elif type(ttl_value) is int:
            merged["retain_until_ns"] = current.created_ns + ttl_value
        else:
            raise ValueError("artifact ttl_ns must be an integer or null")
    if "retain_until_ns" in normalized and normalized["retain_until_ns"] is None:
        # Clearing an explicit deadline also clears no TTL; a non-null TTL
        # still gives the record its derived deadline at classification.
        pass
    if merged["state"] == ArtifactState.RETIRED.value:
        # ScratchManager publishes the terminal state after the physical
        # unlink.  Preserve and close the durable intent even when the
        # caller supplies its own metadata projection; otherwise a
        # successful retirement would lose the receipt on replay.
        candidate_metadata = merged.get("metadata", current.metadata)
        if not isinstance(candidate_metadata, Mapping):
            raise ArtifactSecurityError("artifact metadata is not an object")
        current_retirement = current.metadata.get(_RETIREMENT_KEY)
        if current_retirement is not None and _RETIREMENT_KEY not in candidate_metadata:
            # The scratch projection intentionally carries producer
            # metadata, not registry-internal receipts.  Carry the
            # reserved claim forward before closing it.
            candidate_metadata = dict(candidate_metadata)
            candidate_metadata[_RETIREMENT_KEY] = current_retirement
        candidate = replace(
            current,
            metadata=_bounded_mapping(
                candidate_metadata,
                label="artifact metadata",
                limit=registry.max_metadata_bytes,
            ),
        )
        retirement = registry._retirement_claim(candidate)
        if retirement is not None and retirement.get("phase") in _RETIREMENT_PENDING_PHASES:
            merged["metadata"] = registry._metadata_with_retirement(
                candidate,
                phase=_RETIREMENT_CONFIRMED_PHASE,
                operation_id=str(retirement["operation_id"]),
                observed_path_exists=False,
                confirmed_ns=time.time_ns(),
                receipt={"effect": "removed", "replayed": False},
            )
    changed = any(
        merged.get(name) != getattr(current, name)
        for name in (
            "producer",
            "run_id",
            "purpose",
            "state",
            "source_ref",
            "digest",
            "dependencies",
            "retain_until_ns",
            "ttl_ns",
            "disposable",
            "metadata",
        )
    )
    if not changed:
        return current
    timestamp = time.time_ns() if updated_ns is None else updated_ns
    if type(timestamp) is not int or timestamp < current.created_ns:
        raise ValueError("artifact updated_ns must be >= created_ns")
    merged["updated_ns"] = max(timestamp, current.updated_ns)
    path_size_bytes = current.path_size_bytes
    path_mtime_ns = current.path_mtime_ns
    if merged["state"] == ArtifactState.COMPLETED.value:
        # A producer may legitimately populate an active directory between
        # registration and completion.  Capture the final no-follow
        # observations at the transition instead of treating that normal
        # lifecycle growth as drift.
        try:
            completed_metadata = current.path.lstat()
        except OSError as exc:
            raise ArtifactSecurityError("artifact disappeared before completion") from exc
        completed_issue = _private_artifact_issue(completed_metadata)
        if completed_issue is not None:
            raise ArtifactSecurityError(
                f"artifact failed completion safety check: {completed_issue}"
            )
        if current.path_identity is None or _identity(completed_metadata) != current.path_identity:
            raise ArtifactSecurityError("artifact identity changed before completion")
        path_size_bytes = max(0, int(completed_metadata.st_size))
        path_mtime_ns = max(0, int(completed_metadata.st_mtime_ns))
    merged_payload: dict[str, Any] = {
        "schema": ARTIFACT_REGISTRY_SCHEMA,
        "artifact_id": current.artifact_id,
        "owner": current.owner,
        "producer": merged["producer"],
        "run_id": merged["run_id"],
        "purpose": merged["purpose"],
        "path": str(current.path),
        "root": str(current.root),
        "path_identity": list(current.path_identity or ()),
        "root_identity": list(current.root_identity or ()),
        "kind": current.kind,
        "state": merged["state"],
        "created_ns": current.created_ns,
        "updated_ns": merged["updated_ns"],
        "source_ref": merged["source_ref"],
        "digest": merged["digest"],
        "dependencies": list(merged["dependencies"]),
        "retain_until_ns": merged["retain_until_ns"],
        "ttl_ns": merged["ttl_ns"],
        "disposable": merged["disposable"],
        "metadata": merged["metadata"],
        "path_size_bytes": path_size_bytes,
        "path_mtime_ns": path_mtime_ns,
    }
    merged_payload["manifest_digest"] = _manifest_digest(merged_payload)
    if len(_canonical_json(merged_payload).encode("utf-8")) > registry.max_manifest_bytes:
        raise ValueError("artifact manifest exceeds the configured size limit")
    # Refuse to overwrite a manifest that changed after our read.
    latest = registry._read_manifest_payload(manifest_path)
    if latest.get("manifest_digest") != current.manifest_digest:
        raise ArtifactConflictError("artifact manifest changed during update")
    _write_json_atomic(manifest_path, merged_payload, exclusive=False)
    return registry._load_record(manifest_path)
