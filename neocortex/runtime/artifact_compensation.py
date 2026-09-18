"""Owner-enrolled compensation for an exact reset retirement.

The private registry stores the original claim and content fence before the
retirement begins. Reconciliation observes an already restored path; it never
copies, unlinks or grants authority from caller-supplied hashes or JSON.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from neocortex.runtime import scratch_tree
from neocortex.runtime.artifact_registry import (
    ArtifactRecord, ArtifactRegistry, ArtifactSecurityError, _identity,
)

PREPARED_KEY = "retirement_compensation"
RECEIPT_KEY = "retirement_compensation_receipt"
SCHEMA = "neocortex.artifact-retirement-compensation/v1"
_MAX_DEPTH = 64


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _identity2(metadata: os.stat_result) -> list[int]:
    return [metadata.st_dev, metadata.st_ino]


def _observe(path: Path, registry: ArtifactRegistry) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bounded strict content/namespace seal, independent of restored inodes."""
    ancestors: list[tuple[int, int, int, int, int]] = []
    parent = scratch_tree._open_directory_path(path.parent, observed_ancestors=ancestors)
    entries: dict[str, Any] = {}
    total = 0
    try:
        mount = scratch_tree._mount_id(parent)

        def visit(parent_fd: int, name: str, current: Path, depth: int) -> None:
            nonlocal total
            if depth > _MAX_DEPTH or len(entries) >= registry.max_records:
                raise ArtifactSecurityError("compensation observation exceeds entry/depth budget")
            descriptor, metadata = scratch_tree._checked_child(parent_fd, name, mount)
            try:
                item: dict[str, Any] = {"kind": "directory" if stat.S_ISDIR(metadata.st_mode) else "file",
                    "identity": _identity2(metadata), "mode": stat.S_IMODE(metadata.st_mode)}
                entries[str(current)] = item
                if stat.S_ISDIR(metadata.st_mode):
                    names = scratch_tree._entries(descriptor, limit=registry.max_records - len(entries))
                    for child in sorted(names, key=os.fsencode):
                        visit(descriptor, child, current / child, depth + 1)
                else:
                    readable = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                    try:
                        opened = os.fstat(readable)
                        if _identity(opened) != _identity(metadata) or not stat.S_ISREG(opened.st_mode):
                            raise ArtifactSecurityError("compensation file changed before reading")
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := os.read(readable, 64 * 1024):
                            total += len(chunk)
                            size += len(chunk)
                            if total > registry.max_bytes:
                                raise ArtifactSecurityError("compensation observation exceeds byte budget")
                            digest.update(chunk)
                        after = os.fstat(readable)
                        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            raise ArtifactSecurityError("compensation file changed while reading")
                        item.update(size=size, sha256=digest.hexdigest())
                    finally:
                        os.close(readable)
                after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (_identity(after), after.st_mode, after.st_mtime_ns, after.st_ctime_ns) != (
                        _identity(metadata), metadata.st_mode, metadata.st_mtime_ns, metadata.st_ctime_ns):
                    raise ArtifactSecurityError("compensation namespace changed while observing")
            finally:
                os.close(descriptor)

        visit(parent, path.name, path, 0)
        verified_parent = scratch_tree._open_directory_path(path.parent, expected_ancestors=tuple(ancestors))
        os.close(verified_parent)
        content = [{"relative_bytes": base64.b64encode(os.fsencode(Path(name).relative_to(path))).decode("ascii"),
                    **{key: value for key, value in item.items() if key != "identity"}}
                   for name, item in sorted(entries.items(), key=lambda pair: os.fsencode(pair[0]))]
        return {"digest": "sha256:" + hashlib.sha256(_canonical(content)).hexdigest(),
                "members": len(entries), "bytes": total}, entries
    finally:
        os.close(parent)


def _operation(registry: ArtifactRegistry, operation_id: str) -> tuple[ArtifactRecord, dict[str, Any]]:
    operation = registry.verify(operation_id)
    if (not isinstance(operation, ArtifactRecord) or not operation.verified
            or operation.owner != "state-reset" or operation.producer != "state-reset"
            or operation.purpose != "state-reset-operation" or operation.kind != "operational"
            or operation.disposable or operation.state not in {"active", "recovery_required"}):
        raise ArtifactSecurityError("compensation requires a live verified reset operation")
    descriptor = scratch_tree._open_directory_path(operation.path.parent)
    try:
        fd = os.open(operation.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or _identity(before) != operation.path_identity
                    or before.st_nlink != 1 or before.st_mode & 0o077
                    or before.st_size > min(registry.max_bytes, 64 * 1024 * 1024)):
                raise ArtifactSecurityError("compensation operation intent is unsafe or exceeds budget")
            raw = stream.read(min(registry.max_bytes, 64 * 1024 * 1024) + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ArtifactSecurityError("compensation operation intent changed")
        if hashlib.sha256(raw).hexdigest() != str(operation.digest).removeprefix("sha256:"):
            raise ArtifactSecurityError("compensation operation intent digest changed")
        intent = json.loads(raw)
        if (not isinstance(intent, dict) or intent.get("schema") != "neocortex.state-reset-operation/v1"
                or intent.get("operation_id") != operation_id):
            raise ArtifactSecurityError("compensation operation intent is unrecognized")
        return operation, intent
    finally:
        os.close(descriptor)


def _intent_entries(intent: Mapping[str, Any], registry: ArtifactRegistry) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for target in intent.get("targets", ()):
        if not isinstance(target, Mapping) or target.get("action") != "remove-files":
            continue
        for entry in target.get("entries", ()):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
                raise ArtifactSecurityError("compensation original reset entry is malformed")
            if len(entries) >= registry.max_records:
                raise ArtifactSecurityError("compensation original reset entries exceed budget")
            previous = entries.setdefault(entry["path"], dict(entry))
            if previous != entry:
                raise ArtifactSecurityError("compensation original reset entries conflict")
    return entries


def _entry_matches(item: Mapping[str, Any], original: Mapping[str, Any]) -> bool:
    return (item.get("identity") == [original.get("device"), original.get("inode")]
            and item.get("kind") == original.get("kind") and item.get("mode") == original.get("mode")
            and (item.get("kind") == "directory" or (item.get("size") == original.get("size")
                 and item.get("sha256") == original.get("sha256"))))


def prepare(registry: ArtifactRegistry, artifact_id: str, *, recovery_artifact_id: str) -> ArtifactRecord:
    current = registry.verify(artifact_id)
    if not isinstance(current, ArtifactRecord) or not current.verified or current.owner != registry.owner:
        raise ArtifactSecurityError("compensation enrollment requires the exact verified producer owner")
    if current.state == "retired" or registry._retirement_claim(current) is not None:
        raise ArtifactSecurityError("compensation must be enrolled before retirement begins")
    operation, intent = _operation(registry, recovery_artifact_id)
    original_entries = _intent_entries(intent, registry)
    snapshot, entries = _observe(current.path, registry)
    if any(name not in original_entries or not _entry_matches(item, original_entries[name])
           for name, item in entries.items()):
        raise ArtifactSecurityError("compensation target differs from the enrolled reset plan")
    metadata = dict(current.metadata)
    existing = metadata.get(PREPARED_KEY)
    if existing is not None:
        if (not isinstance(existing, Mapping) or existing.get("recovery_artifact_id") != recovery_artifact_id
                or existing.get("content") != snapshot or existing.get("operation_digest") != operation.digest):
            raise ArtifactSecurityError("a different compensation operation is already enrolled")
        return current
    metadata[PREPARED_KEY] = {"schema": SCHEMA, "recovery_artifact_id": recovery_artifact_id,
        "operation_digest": operation.digest, "original": registry._payload_from_record(current),
        "content": snapshot}
    return registry.update(artifact_id, metadata=metadata)


def reconcile(registry: ArtifactRegistry, artifact_id: str, *, recovery_artifact_id: str,
              expected_retirement_manifest_digest: str) -> ArtifactRecord:
    manifest = registry.manifest_path(artifact_id)
    raw = registry._record_from_payload(registry._read_manifest_payload(manifest),
                                       manifest_path=manifest, revalidate=False)
    if registry.owner is None or raw.owner != registry.owner:
        raise ArtifactSecurityError("compensation requires the original producer owner")
    if raw.manifest_digest != expected_retirement_manifest_digest:
        raise ArtifactSecurityError("compensation retirement manifest changed")
    prior_receipt = raw.metadata.get(RECEIPT_KEY)
    enrollment = raw.metadata.get(PREPARED_KEY)
    if enrollment is None and isinstance(prior_receipt, Mapping):
        if prior_receipt.get("recovery_artifact_id") != recovery_artifact_id:
            raise ArtifactSecurityError("compensation replay belongs to another operation")
        verified = registry.verify(artifact_id)
        if not isinstance(verified, ArtifactRecord) or not verified.verified:
            raise ArtifactSecurityError("compensated artifact changed after recovery")
        content, _ = _observe(verified.path, registry)
        if content != prior_receipt.get("content"):
            raise ArtifactSecurityError("compensated artifact content changed after recovery")
        return verified
    if (not isinstance(enrollment, Mapping) or enrollment.get("schema") != SCHEMA
            or enrollment.get("recovery_artifact_id") != recovery_artifact_id):
        raise ArtifactSecurityError("retirement has no matching private compensation enrollment")
    retirement = registry._retirement_claim(raw)
    if retirement is None or retirement.get("phase") not in {"applying", "confirmed", "applied_unverified", "recovery_required"}:
        raise ArtifactSecurityError("compensation requires an observed retirement outcome")
    operation, intent = _operation(registry, recovery_artifact_id)
    if operation.digest != enrollment.get("operation_digest"):
        raise ArtifactSecurityError("compensation operation identity changed")
    original_payload = enrollment.get("original")
    if not isinstance(original_payload, Mapping):
        raise ArtifactSecurityError("compensation original claim is unavailable")
    original = registry._record_from_payload(dict(original_payload), manifest_path=manifest, revalidate=False)
    if (original.owner != raw.owner or original.artifact_id != raw.artifact_id
            or original.path != raw.path or original.root != raw.root
            or original.path_identity != raw.path_identity or original.root_identity != raw.root_identity):
        raise ArtifactSecurityError("compensation original claim does not match the retirement")
    content, entries = _observe(original.path, registry)
    if content != enrollment.get("content"):
        raise ArtifactSecurityError("restored compensation payload differs from the original")
    original_entries = _intent_entries(intent, registry)
    promotions = operation.metadata.get("promotions", {})
    if not isinstance(promotions, Mapping):
        raise ArtifactSecurityError("compensation has no durable producer promotions")
    for name, item in entries.items():
        previous = original_entries.get(name)
        if not isinstance(previous, Mapping):
            raise ArtifactSecurityError("restored compensation path is outside the original reset plan")
        if _entry_matches(item, previous):
            continue
        promotion = promotions.get(name)
        if (not isinstance(promotion, Mapping) or promotion.get("owner") != "raw-rollback"
                or promotion.get("identity") != item["identity"]
                or (item["kind"] == "directory" and promotion.get("kind") != "directory")
                or (item["kind"] == "file" and (promotion.get("sha256") != item["sha256"]
                    or promotion.get("size") != item["size"]))):
            raise ArtifactSecurityError("restored compensation inode has no verified rollback promotion")
    root_fd = scratch_tree._open_directory_path(original.root)
    try:
        root_metadata = os.fstat(root_fd)
        root_identity = _identity(root_metadata)
        if root_identity != original.root_identity:
            promotion = promotions.get(str(original.root))
            if (not isinstance(promotion, Mapping) or promotion.get("owner") != "raw-rollback"
                    or promotion.get("kind") != "directory"
                    or promotion.get("identity") != _identity2(root_metadata)
                    or str(original.root) not in original_entries):
                raise ArtifactSecurityError("compensation root changed without a verified directory promotion")
        registry._validate_dependencies_locked(original.dependencies, artifact_id=artifact_id)
        metadata = dict(original.metadata)
        metadata[RECEIPT_KEY] = {"schema": SCHEMA, "recovery_artifact_id": recovery_artifact_id,
            "original_manifest_digest": original.manifest_digest,
            "retirement_operation_id": retirement["operation_id"],
            "retirement_manifest_digest": raw.manifest_digest, "content": content,
            "confirmed_ns": time.time_ns()}
        target = original.path.lstat()
        if _identity2(target) != entries[str(original.path)]["identity"]:
            raise ArtifactSecurityError("restored compensation identity changed at publication")
        restored = replace(original, path_identity=_identity(target), root_identity=root_identity,
            path_size_bytes=target.st_size, path_mtime_ns=target.st_mtime_ns,
            metadata=metadata, updated_ns=max(raw.updated_ns, time.time_ns()))
        # Re-observe just before publishing the binding; any uncertainty keeps
        # the retirement and reset operation available for another recovery.
        if _observe(original.path, registry) != (content, entries):
            raise ArtifactSecurityError("restored compensation payload changed at publication")
        registry._write_existing_registration(manifest, registry._payload_from_record(restored))
        verified = registry.verify(artifact_id)
        if not isinstance(verified, ArtifactRecord) or not verified.verified:
            raise ArtifactSecurityError("compensation binding publication requires recovery")
        return verified
    finally:
        os.close(root_fd)
