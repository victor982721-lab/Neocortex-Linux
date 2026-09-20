"""Manifest serialization and no-follow revalidation for artifact records.

The :class:`~neocortex.runtime.artifact_registry.ArtifactRegistry` owns locks
and lifecycle transitions.  This module owns the schema-bound conversion
between durable JSON manifests and validated :class:`ArtifactRecord` values,
keeping persistence parsing separate from planning and retirement policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from dataclasses import replace

from .artifact_models import (
    ARTIFACT_KINDS, ARTIFACT_REGISTRY_SCHEMA, ARTIFACT_STATES, ArtifactKind,
    ArtifactManifestError, ArtifactRecord, ArtifactSecurityError, ArtifactState,
    MAX_ARTIFACT_ID_BYTES, MAX_MANIFEST_BYTES, MAX_REASON_BYTES, MAX_TEXT_BYTES,
    _artifact_observation_issue, _bounded_identity, _bounded_json, _bounded_mapping,
    _bounded_run_id, _bounded_text, _canonical_json, _identity,
    _path_size_no_follow, _private_artifact_issue, _private_directory_issue,
    _validate_absolute_path,
)


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _manifest_file_issue(metadata: os.stat_result) -> str | None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "manifest_type_drift"
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        return "manifest_protection_drift"
    return None


_MANIFEST_FIELDS = frozenset(
    {
        "schema", "artifact_id", "owner", "producer", "run_id", "purpose",
        "path", "root", "path_identity", "root_identity", "kind", "state",
        "created_ns", "updated_ns", "source_ref", "digest", "dependencies",
        "retain_until_ns", "ttl_ns", "disposable", "metadata",
        "path_size_bytes", "path_mtime_ns", "manifest_digest",
    }
)


def payload_from_record(registry, record: ArtifactRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": ARTIFACT_REGISTRY_SCHEMA,
        "artifact_id": record.artifact_id,
        "owner": record.owner,
        "producer": record.producer,
        "run_id": record.run_id,
        "purpose": record.purpose,
        "path": str(record.path),
        "root": str(record.root),
        "path_identity": None
        if record.path_identity is None
        else list(record.path_identity),
        "root_identity": None
        if record.root_identity is None
        else list(record.root_identity),
        "kind": record.kind,
        "state": record.state,
        "created_ns": record.created_ns,
        "updated_ns": record.updated_ns,
        "source_ref": record.source_ref,
        "digest": record.digest,
        "dependencies": list(record.dependencies),
        "retain_until_ns": record.retain_until_ns,
        "ttl_ns": record.ttl_ns,
        "disposable": record.disposable,
        "metadata": dict(record.metadata),
        "path_size_bytes": record.path_size_bytes,
        "path_mtime_ns": record.path_mtime_ns,
    }
    payload["manifest_digest"] = _manifest_digest(payload)
    return payload

def read_manifest_payload(registry, manifest_path: Path) -> dict[str, Any]:
    try:
        metadata = manifest_path.lstat()
    except OSError as exc:
        raise ArtifactManifestError("artifact manifest is unavailable") from exc
    issue = _manifest_file_issue(metadata)
    if issue is not None:
        raise ArtifactManifestError(f"artifact manifest failed safety check: {issue}")
    if metadata.st_size > registry.max_manifest_bytes:
        raise ArtifactManifestError("artifact manifest is too large")
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactManifestError("artifact manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ArtifactManifestError("artifact manifest must be an object")
    if set(payload) != _MANIFEST_FIELDS:
        raise ArtifactManifestError("artifact manifest fields are not schema-bound")
    if payload.get("schema") != ARTIFACT_REGISTRY_SCHEMA:
        raise ArtifactManifestError("unsupported artifact registry schema")
    expected = payload.get("manifest_digest")
    if not isinstance(expected, str) or expected != _manifest_digest(payload):
        raise ArtifactManifestError("artifact manifest digest mismatch")
    # Validate size independently of the configured default.  This keeps a
    # caller-supplied larger limit from bypassing the durable hard bound.
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ArtifactManifestError("artifact manifest exceeds the hard size limit")
    return dict(payload)

def record_from_payload(
    registry,
    payload: Mapping[str, Any],
    *,
    manifest_path: Path,
    revalidate: bool = True,
) -> ArtifactRecord:
    if payload.get("schema") != ARTIFACT_REGISTRY_SCHEMA:
        raise ArtifactManifestError("unsupported artifact registry schema")
    artifact_id = _bounded_text(
        payload.get("artifact_id"),
        label="artifact_id",
        limit=MAX_ARTIFACT_ID_BYTES,
    )
    owner = _bounded_text(payload.get("owner"), label="artifact owner")
    producer = _bounded_text(payload.get("producer"), label="artifact producer")
    purpose = _bounded_text(payload.get("purpose"), label="artifact purpose")
    path_value = payload.get("path")
    root_value = payload.get("root")
    if not isinstance(path_value, (str, Path)) or not isinstance(root_value, (str, Path)):
        raise ArtifactManifestError("artifact path/root claims are invalid")
    path = _validate_absolute_path(path_value, label="artifact path")
    root = _validate_absolute_path(root_value, label="artifact root")
    path_identity = _bounded_identity(payload.get("path_identity"), label="artifact path_identity")
    root_identity = _bounded_identity(payload.get("root_identity"), label="artifact root_identity")
    kind = payload.get("kind")
    state = payload.get("state")
    if kind not in ARTIFACT_KINDS:
        raise ArtifactManifestError("artifact kind is unsupported")
    if state not in ARTIFACT_STATES:
        raise ArtifactManifestError("artifact state is unsupported")
    run_id = _bounded_run_id(payload.get("run_id"))
    created_ns = payload.get("created_ns")
    updated_ns = payload.get("updated_ns")
    if type(created_ns) is not int or created_ns < 0:
        raise ArtifactManifestError("artifact created_ns is invalid")
    if type(updated_ns) is not int or updated_ns < created_ns:
        raise ArtifactManifestError("artifact updated_ns is invalid")
    source_ref = _bounded_json(
        payload.get("source_ref"),
        label="artifact source_ref",
        limit=MAX_TEXT_BYTES,
    )
    digest = payload.get("digest")
    if digest is not None:
        digest = _bounded_text(digest, label="artifact digest", limit=MAX_TEXT_BYTES)
    dependencies = registry._normalize_dependencies(payload.get("dependencies"))
    if list(dependencies) != payload.get("dependencies"):
        raise ArtifactManifestError("artifact dependencies are not canonical")
    retain_until_ns = payload.get("retain_until_ns")
    ttl_ns = payload.get("ttl_ns")
    for name, value in (("retain_until_ns", retain_until_ns), ("ttl_ns", ttl_ns)):
        if value is not None and (type(value) is not int or value < 0):
            raise ArtifactManifestError(f"artifact {name} is invalid")
    disposable = payload.get("disposable")
    if type(disposable) is not bool:
        raise ArtifactManifestError("artifact disposable is invalid")
    metadata = _bounded_mapping(
        payload.get("metadata"),
        label="artifact metadata",
        limit=registry.max_metadata_bytes,
    )
    path_size_bytes = payload.get("path_size_bytes")
    path_mtime_ns = payload.get("path_mtime_ns")
    for name, value in (
        ("path_size_bytes", path_size_bytes),
        ("path_mtime_ns", path_mtime_ns),
    ):
        if type(value) is not int or value < 0:
            raise ArtifactManifestError(f"artifact {name} is invalid")
    record = ArtifactRecord(
        artifact_id=artifact_id,
        owner=owner,
        producer=producer,
        run_id=run_id,
        purpose=purpose,
        path=path,
        root=root,
        path_identity=path_identity,
        root_identity=root_identity,
        kind=kind,
        state=state,
        created_ns=created_ns,
        updated_ns=updated_ns,
        source_ref=source_ref,
        digest=digest,
        dependencies=dependencies,
        retain_until_ns=retain_until_ns,
        ttl_ns=ttl_ns,
        disposable=disposable,
        metadata=metadata,
        path_size_bytes=path_size_bytes,
        path_mtime_ns=path_mtime_ns,
        manifest_digest=payload.get("manifest_digest"),
        manifest_path=manifest_path,
    )
    if not revalidate:
        return record
    return revalidate_record(registry, record)

def revalidate_record(registry, record: ArtifactRecord) -> ArtifactRecord:
    """Re-read root/path identity and return a detailed fail-closed record."""

    try:
        registry_metadata = registry.root.lstat()
    except OSError:
        return replace(record, valid=False, issue="root_identity_drift", reason="root_identity_drift")
    registry_issue = _private_directory_issue(registry_metadata, root=True)
    if registry_issue is not None:
        return replace(record, valid=False, issue=registry_issue, reason=registry_issue)
    try:
        root_metadata = record.root.lstat()
    except OSError:
        return replace(record, valid=False, issue="artifact_root_drift", reason="artifact_root_drift")
    root_issue = _private_directory_issue(root_metadata, root=True)
    if root_issue is not None:
        return replace(record, valid=False, issue="artifact_root_drift", reason=root_issue)
    if record.root_identity is None or _identity(root_metadata) != record.root_identity:
        return replace(record, valid=False, issue="artifact_root_drift", reason="artifact_root_drift")
    try:
        path_metadata = record.path.lstat()
    except OSError:
        if record.state == ArtifactState.RETIRED.value:
            # Retirement is a durable tombstone: the owning lifecycle may
            # have removed the claimed path after recording this state.
            # Keep the manifest protected rather than reopening a cleanup
            # obligation for an intentionally absent artifact.
            return replace(record, valid=True, issue=None, reason=None, size_bytes=0)
        return replace(record, valid=False, issue="artifact_missing", reason="artifact_missing")
    path_issue = _private_artifact_issue(path_metadata)
    if path_issue is not None:
        return replace(record, valid=False, issue=path_issue, reason=path_issue)
    if record.path_identity is None or _identity(path_metadata) != record.path_identity:
        return replace(
            record,
            valid=False,
            issue="artifact_identity_drift",
            reason="artifact_identity_drift",
        )
    # Active producers are allowed to grow/replace their output while the
    # lifecycle claim is still open.  Once an artifact is completed, size
    # and mtime become useful revalidation observations in addition to the
    # physical identity (which may otherwise be reused on Linux).
    if record.state == ArtifactState.COMPLETED.value and stat.S_ISREG(path_metadata.st_mode) and (
        record.path_size_bytes is None
        or record.path_mtime_ns is None
        or int(path_metadata.st_size) != record.path_size_bytes
        or int(path_metadata.st_mtime_ns) != record.path_mtime_ns
    ):
        return replace(
            record,
            valid=False,
            issue="artifact_identity_drift",
            reason="artifact_identity_drift",
        )
    profile = "strict"
    policy = record.metadata.get("scratch_payload_policy")
    if policy is not None:
        try:
            from neocortex.runtime.scratch import verified_workspace_payload_profile
            if not isinstance(policy, Mapping) or not record.artifact_id.startswith("scratch:"):
                raise ArtifactSecurityError("scratch payload policy has no owner binding")
            profile = verified_workspace_payload_profile(record.path, owner=record.owner,
                                                          expected_policy=policy).value
        except (OSError, RuntimeError, ValueError) as exc:
            return replace(record, valid=False, issue="payload_policy_unverified", reason=str(exc)[:512])
    if stat.S_ISDIR(path_metadata.st_mode):
        from neocortex.runtime.scratch_tree import observe_claimed_tree
        observed = observe_claimed_tree(record.path, limit=registry.max_records,
                     max_bytes=registry.max_bytes, profile=profile, include_control_manifest=True)
        size, size_issue = observed.apparent_bytes, _artifact_observation_issue(observed.issue)
        allocated = observed.allocated_bytes
        entries = observed.members
    else:
        size, size_issue = _path_size_no_follow(record.path, max_entries=registry.max_records,
                                               max_bytes=registry.max_bytes, profile=profile)
        allocated = getattr(path_metadata, "st_blocks", 0) * 512
        entries = 1
    if size_issue is not None:
        return replace(record, size_bytes=size, allocated_bytes=allocated,
                       observed_payload_entries=entries, valid=False, issue=size_issue, reason=size_issue)
    return replace(record, size_bytes=size, allocated_bytes=allocated,
                   observed_payload_entries=entries, valid=True, issue=None, reason=None)

def invalid_record(registry, manifest_path: Path, issue: str) -> ArtifactRecord:
    bounded_issue = _bounded_text(issue, label="artifact issue", limit=MAX_REASON_BYTES)
    return ArtifactRecord(
        artifact_id=f"invalid:{manifest_path.name}",
        owner="unknown",
        producer="unknown",
        run_id=None,
        purpose="invalid-manifest",
        path=manifest_path,
        root=registry.root,
        path_identity=None,
        root_identity=None,
        kind=ArtifactKind.EXTERNAL.value,
        state=ArtifactState.RECOVERY_REQUIRED.value,
        created_ns=0,
        updated_ns=0,
        disposable=False,
        size_bytes=0,
        valid=False,
        issue=bounded_issue,
        reason=bounded_issue,
        classification="unknown",
        manifest_path=manifest_path,
    )

def load_record(registry, manifest_path: Path) -> ArtifactRecord:
    payload = read_manifest_payload(registry, manifest_path)
    return record_from_payload(registry, payload, manifest_path=manifest_path)

def registration_equal(existing: ArtifactRecord, candidate: ArtifactRecord) -> bool:
    fields = (
        "artifact_id",
        "owner",
        "producer",
        "run_id",
        "purpose",
        "path",
        "root",
        "path_identity",
        "root_identity",
        "kind",
        "state",
        "created_ns",
        "updated_ns",
        "source_ref",
        "digest",
        "dependencies",
        "retain_until_ns",
        "ttl_ns",
        "disposable",
        "metadata",
        "path_size_bytes",
        "path_mtime_ns",
    )
    return all(getattr(existing, name) == getattr(candidate, name) for name in fields)
