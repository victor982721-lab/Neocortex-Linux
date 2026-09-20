"""Schema validation for one registered scratch workspace manifest."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .path_identity import PathIdentity
from .scratch_contracts import (
    _MAX_METADATA_BYTES, _MAX_REASON_BYTES, _activity_process_scope_issue,
    _canonical_json, _manifest_digest, _same_identity,
    _seal_mapping, _sealed_workspace_digest, _workspace_payload_issue,
    ScratchRecord, ScratchSecurityError, ScratchState, SCRATCH_SCHEMA,
)
from .scratch_tree import policy_revision


def record_from_payload(
    manager,
    path: Path,
    payload: Mapping[str, Any],
    *,
    size_bytes: int,
    now_ns: int | None = None,
    observation_issue: str | None = None,
    bounded_observation: bool = False,
    payload_observed: bool = False,
    size_complete: bool = True,
) -> ScratchRecord:
    if payload.get("schema") != SCRATCH_SCHEMA:
        raise ScratchSecurityError("unsupported scratch manifest schema")
    expected_digest = payload.get("manifest_digest")
    if not isinstance(expected_digest, str) or expected_digest != _manifest_digest(payload):
        raise ScratchSecurityError("scratch manifest digest mismatch")
    record_id = payload.get("record_id")
    owner = payload.get("owner")
    state = payload.get("state")
    manifest_path = payload.get("path")
    identity = payload.get("path_identity")
    root_identity = payload.get("root_identity")
    created_ns = payload.get("created_ns")
    updated_ns = payload.get("updated_ns")
    run_id = payload.get("run_id")
    artifact_id = payload.get("artifact_id")
    if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
        raise ScratchSecurityError("scratch record id is invalid")
    if not isinstance(owner, str) or not owner or len(owner.encode("utf-8")) > 128:
        raise ScratchSecurityError("scratch owner is invalid")
    if artifact_id is not None and (
        not isinstance(artifact_id, str)
        or not artifact_id.strip()
        or len(artifact_id.encode("utf-8")) > 256
    ):
        raise ScratchSecurityError("scratch artifact id is invalid")
    if state not in {member.value for member in ScratchState}:
        raise ScratchSecurityError("scratch state is invalid")
    if not isinstance(manifest_path, str) or Path(manifest_path) != path:
        raise ScratchSecurityError("scratch manifest path claim changed")
    if (
        not isinstance(identity, list)
        or len(identity) != 3
        or any(type(value) is not int for value in identity)
        or not _same_identity(path, identity)
    ):
        raise ScratchSecurityError("scratch workspace identity changed")
    if (
        not isinstance(root_identity, list)
        or len(root_identity) != 3
        or any(type(value) is not int for value in root_identity)
        or not _same_identity(manager.root, root_identity)
    ):
        raise ScratchSecurityError("scratch root identity changed")
    if type(created_ns) is not int or created_ns < 0 or type(updated_ns) is not int or updated_ns < 0:
        raise ScratchSecurityError("scratch manifest timestamps are invalid")
    if run_id is not None and (
        (type(run_id) is int and run_id < 1)
        or not isinstance(run_id, (int, str))
        or (isinstance(run_id, str) and not run_id.strip())
    ):
        raise ScratchSecurityError("scratch run id is invalid")
    if isinstance(run_id, str) and len(run_id.encode("utf-8")) > 256:
        raise ScratchSecurityError("scratch run id is too large")
    retain = payload.get("retain_on_success", False)
    if type(retain) is not bool:
        raise ScratchSecurityError("scratch retention flag is invalid")
    retire_after = payload.get("retire_after_ns")
    if retire_after is not None and (type(retire_after) is not int or retire_after < 0):
        raise ScratchSecurityError("scratch retirement time is invalid")
    results = payload.get("result_paths", [])
    if not isinstance(results, list) or any(not isinstance(value, str) for value in results):
        raise ScratchSecurityError("scratch result paths are invalid")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping) or len(_canonical_json(metadata).encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ScratchSecurityError("scratch metadata is invalid")
    reason = payload.get("reason")
    if reason is not None and (
        not isinstance(reason, str)
        or len(reason.encode("utf-8", errors="backslashreplace")) > _MAX_REASON_BYTES
    ):
        raise ScratchSecurityError("scratch reason is invalid")
    payload_size_bytes = payload.get("payload_size_bytes")
    if payload_size_bytes is not None and (
        type(payload_size_bytes) is not int or payload_size_bytes < 0
    ):
        raise ScratchSecurityError("scratch payload size observation is invalid")
    seal = _seal_mapping(payload.get("seal"))
    profile = manager._payload_profile(path, payload)
    binary_path = payload.get("posix_path_identity")
    if binary_path is not None and binary_path != PathIdentity.from_path(path).as_dict():
        raise ScratchSecurityError("scratch POSIX path identity changed")
    workspace_metadata = path.lstat()
    issue: str | None = None
    if workspace_metadata.st_uid != os.geteuid() or workspace_metadata.st_mode & 0o077:
        issue = "workspace_mode_drift"
    else:
        issue = observation_issue if bounded_observation or payload_observed else _workspace_payload_issue(path, profile=profile)
    if not size_complete:
        issue = issue or "payload_observation_incomplete"
    if state == ScratchState.COMPLETED.value:
        issue = issue or _activity_process_scope_issue(metadata, reason)
    if (
        issue is None
        and state == ScratchState.COMPLETED.value
        and payload_size_bytes is not None
        and int(size_bytes) != payload_size_bytes
    ):
        issue = "payload_changed_after_completion"
    if issue is None and seal is not None:
        if bounded_observation:
            # A bounded plan cannot claim to have verified a full seal;
            # preserve the workspace until an effect pass obtains the
            # complete observation.
            issue = "seal_observation_incomplete"
        else:
            try:
                observed_digest, observed_members, observed_bytes = _sealed_workspace_digest(path, profile=profile)
            except ScratchSecurityError:
                issue = "workspace_seal_unavailable"
            else:
                if (
                    observed_digest != seal["digest"]
                    or observed_members != seal["members"]
                    or observed_bytes != seal["apparent_bytes"]
                ):
                    issue = "workspace_seal_drift"
    eligible = (
        owner == manager.owner
        and state == ScratchState.COMPLETED.value
        and (retire_after is None or retire_after <= (time.time_ns() if now_ns is None else now_ns))
        and issue is None
    )
    if manager.owner is not None and owner != manager.owner:
        issue = "owner_mismatch"
        reason = "workspace belongs to another owner"
    elif issue is not None and reason is None:
        reason = issue
    return ScratchRecord(
        record_id=record_id,
        owner=owner,
        run_id=run_id,
        path=path,
        state=ScratchState(state),
        created_ns=created_ns,
        updated_ns=updated_ns,
        path_identity=tuple(identity),
        root_identity=tuple(root_identity),
        size_bytes=max(0, int(size_bytes)),
        size_complete=size_complete,
        retain_on_success=retain,
        retire_after_ns=retire_after,
        result_paths=tuple(Path(value) for value in results),
        metadata=dict(metadata),
        reason=reason,
        manifest_digest=expected_digest,
        artifact_id=artifact_id,
        payload_size_bytes=payload_size_bytes,
        seal=seal,
        payload_profile=profile.value,
        policy_revision=policy_revision(profile),
        fixture_grant=payload.get("fixture_grant"),
        eligible=eligible,
        issue=issue,
    )
