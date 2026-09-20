"""Lifecycle and bounded observation operations for registered scratch.

``ScratchManager`` remains the sole owner of locks and public state.  These
functions hold the cohesive observation, creation, apply, reconciliation, and
retirement workflows so the manager class coordinates instead of embedding
every lifecycle branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, cast

from .path_identity import PathIdentity
from .scratch_contracts import (
    BYTE_LIMIT, DEPTH_LIMIT, ENTRY_LIMIT, MANIFEST_NAME, SCRATCH_SCHEMA,
    _MAX_CHECKPOINT_BYTES, _MAX_METADATA_BYTES, _MAX_SCAN_BYTES, _bounded_text, _canonical_json, _identity, _manifest_digest, _read_manifest_bytes, _remove_tree_no_follow,
    _same_identity, _validate_scan_limits, _write_json_atomic,
    FixturePayloadGrant, ScratchError, ScratchPlan,
    ScratchRootError, ScratchSecurityError, ScratchState,
    _ScratchScanBudget, ScratchRecord,
)
from .scratch_tree import (
    PayloadProfile, ScratchTreeError, _checked_child, _mount_id, directory_batch,
    opened_claimed_tree, policy_revision,
)


def observe_workspace_batch(manager,
    record_id: str, *, operation_id: str, batch_entries: int = 1000,
    batch_bytes: int = 64 << 20, max_entries: int = 10_000_000,
    max_bytes: int = _MAX_SCAN_BYTES, max_depth: int = 2048, max_fds: int = 8,
    hash_files: bool = False, cancelled: Any | None = None,
    deadline_ns: int | None = None,
    checked_child_fn: Any | None = None, batch_reader_fn: Any | None = None,
) -> dict[str, Any]:
    """Advance one owner-journal observation without restarting its prefix.

    A generation is observation evidence, never a deletion authorization.
    Its cursor uses Linux directory cookies plus exact byte names and
    revalidated directory identities. Each completed batch is atomically
    persisted before the caller can export its presentation. Changed
    frontier segments rewind to their saved accounting/hash prefix.
    """
    checked_child = _checked_child if checked_child_fn is None else checked_child_fn
    batch_reader = directory_batch if batch_reader_fn is None else batch_reader_fn
    if manager.owner is None:
        raise ScratchSecurityError("observation checkpoints require an exact owner")
    for value, label, maximum in ((batch_entries, "batch_entries", 10_000),
                                  (batch_bytes, "batch_bytes", 1 << 40),
                                  (max_entries, "max_entries", 100_000_000),
                                  (max_bytes, "max_bytes", _MAX_SCAN_BYTES),
                                  (max_depth, "max_depth", 2048), (max_fds, "max_fds", 2048)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"invalid observation {label}")
    if max_fds < 5:
        raise ValueError("observation max_fds must be at least 5")
    if type(hash_files) is not bool:
        raise ValueError("hash_files must be a boolean")
    if (not isinstance(record_id, str) or len(record_id) != 32
            or any(ch not in "0123456789abcdef" for ch in record_id)):
        raise ScratchSecurityError("observation record identifier is invalid")
    operation_id = _bounded_text(operation_id, label="observation operation", limit=256)
    operation_name = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    path = manager.root / f"workspace-{record_id}"
    with manager._scratch_lock():
        payload = json.loads(_read_manifest_bytes(path / MANIFEST_NAME))
        if (payload.get("schema") != SCRATCH_SCHEMA or payload.get("owner") != manager.owner
                or payload.get("record_id") != record_id
                or payload.get("manifest_digest") != _manifest_digest(payload)
                or payload.get("path") != str(path)
                or not _same_identity(path, payload.get("path_identity", []))
                or not _same_identity(manager.root, payload.get("root_identity", []))):
            raise ScratchSecurityError("observation workspace claim is not verified")
        profile = manager._payload_profile(path, payload)
        policy = {"profile": profile.value, "revision": policy_revision(profile),
                  "max_entries": max_entries, "max_bytes": max_bytes,
                  "max_depth": max_depth, "hash_files": hash_files,
                  "fixture_grant": payload.get("fixture_grant")}
        policy_digest = hashlib.sha256(_canonical_json(policy).encode()).hexdigest()
        journal = manager._control_directory(create=True)
        checkpoint_path = journal / f"observation-{operation_name}.json"
        try:
            checkpoint = json.loads(_read_manifest_bytes(checkpoint_path, limit=_MAX_CHECKPOINT_BYTES))
        except FileNotFoundError:
            checkpoint = None
        def directory_claim(fd: int) -> list[int]:
            metadata = os.fstat(fd)
            return [metadata.st_dev, metadata.st_ino, metadata.st_size,
                    metadata.st_mtime_ns, metadata.st_ctime_ns, _mount_id(fd)]
        with opened_claimed_tree(path, profile=profile) as (root_fd, mount_id):
            if checkpoint is None:
                checkpoint = {"schema": "neocortex.scratch-observation/v1",
                              "operation_id": operation_id, "generation_id": uuid.uuid4().hex,
                              "record_id": record_id, "owner": manager.owner,
                              "root_identity": payload["path_identity"],
                              "posix_path_identity": PathIdentity.from_path(path).as_dict(),
                              "mount_id": mount_id, "policy_digest": policy_digest,
                              "members": 0, "apparent_bytes": 0, "hashed_bytes": 0,
                              "content_chain": "0" * 64, "batches": 0,
                              "coverage": "not_measured", "observation_only": True, "byte_semantics": "entry_lengths",
                              "frontier": [{"name": None, "depth": 0, "claim": directory_claim(root_fd),
                                  "cookie": 0, "last_name": None, "start_members": 0,
                                  "start_bytes": 0, "start_hashed_bytes": 0,
                                  "start_chain": "0" * 64}], "errors": []}
            elif (checkpoint.get("schema") != "neocortex.scratch-observation/v1"
                  or checkpoint.get("manifest_digest") != _manifest_digest(checkpoint)
                  or checkpoint.get("owner") != manager.owner
                  or checkpoint.get("record_id") != record_id
                  or checkpoint.get("root_identity") != payload["path_identity"]
                  or checkpoint.get("mount_id") != mount_id
                  or checkpoint.get("policy_digest") != policy_digest
                  or checkpoint.get("operation_id") != operation_id):
                raise ScratchSecurityError("observation generation or policy changed")
            frontier = checkpoint.get("frontier")
            if (not isinstance(frontier, list) or len(frontier) > max_depth + 1
                    or any(type(checkpoint.get(key)) is not int or checkpoint[key] < 0
                           for key in ("members", "apparent_bytes", "hashed_bytes", "batches"))
                    or checkpoint["members"] > max_entries
                    or checkpoint["apparent_bytes"] > max_bytes
                    or checkpoint["hashed_bytes"] > checkpoint["apparent_bytes"]
                    or checkpoint.get("coverage") not in {"complete", "partial", "not_measured"}
                    or not isinstance(checkpoint.get("content_chain"), str)
                    or len(checkpoint["content_chain"]) != 64
                    or any(ch not in "0123456789abcdef" for ch in checkpoint["content_chain"])
                    or not isinstance(checkpoint.get("errors"), list) or len(checkpoint["errors"]) > 32):
                raise ScratchSecurityError("observation checkpoint structure is invalid")
            for depth, boundary in enumerate(frontier):
                if (not isinstance(boundary, dict) or boundary.get("depth") != depth
                        or not isinstance(boundary.get("claim"), list) or len(boundary["claim"]) != 6
                        or any(type(value) is not int for value in boundary["claim"])
                        or any(type(boundary.get(key)) is not int or boundary[key] < 0
                               for key in ("cookie", "start_members", "start_bytes", "start_hashed_bytes"))):
                    raise ScratchSecurityError("observation frontier is invalid")
                if depth:
                    try:
                        raw_component = PathIdentity.from_dict(boundary["name"]).to_bytes()
                    except (TypeError, ValueError, KeyError, AttributeError) as exc:
                        raise ScratchSecurityError("observation frontier name is invalid") from exc
                    if raw_component in {b"", b".", b".."} or b"/" in raw_component:
                        raise ScratchSecurityError("observation frontier name is not a POSIX component")
            if checkpoint["coverage"] == "complete":
                return dict(checkpoint)
            consumed = read_bytes = 0
            hash_records: list[dict[str, Any]] = []
            hash_receipt_bytes = 0
            issue = None
            rewound = 0
            rewound_prefixes: list[int] = []
            while checkpoint["frontier"]:
                if cancelled is not None and cancelled():
                    issue = "cancelled"
                    break
                if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                    issue = "deadline"
                    break
                if consumed >= batch_entries:
                    issue = "batch_limit"
                    break
                frame = checkpoint["frontier"][-1]
                fd = os.dup(root_fd)
                try:
                    reset = False
                    # Reopen every ancestor by FD and validate its exact
                    # generation before trusting any persistent cookie.
                    for index, boundary in enumerate(checkpoint["frontier"]):
                        if index:
                            component = PathIdentity.from_dict(boundary["name"]).to_bytes()
                            following, _ = checked_child(fd, component, mount_id, profile=profile)
                            os.close(fd)
                            fd = following
                        current_claim = directory_claim(fd)
                        if current_claim != boundary["claim"]:
                            if current_claim[:2] != boundary["claim"][:2] or current_claim[-1] != boundary["claim"][-1]:
                                raise ScratchSecurityError("observation directory identity changed")
                            checkpoint["members"] = boundary["start_members"]
                            checkpoint["apparent_bytes"] = boundary["start_bytes"]
                            checkpoint["hashed_bytes"] = boundary["start_hashed_bytes"]
                            checkpoint["content_chain"] = boundary["start_chain"]
                            rewound_prefixes.append(checkpoint["members"])
                            hash_records[:] = [item for item in hash_records
                                               if item["member_index"] <= checkpoint["members"]]
                            hash_receipt_bytes = sum(len(_canonical_json(item).encode("utf-8"))
                                                     for item in hash_records)
                            boundary.update(claim=current_claim, cookie=0, last_name=None)
                            del checkpoint["frontier"][index + 1:]
                            rewound += 1
                            reset = True
                            break
                    if reset:
                        if rewound > batch_entries:
                            issue = "generation_unstable"
                            break
                        continue
                    names, exhausted = batch_reader(fd, cookie=frame["cookie"],
                                                       limit=batch_entries - consumed)
                    descended = False
                    for raw_name, cookie in names:
                        if cancelled is not None and cancelled():
                            issue = "cancelled"
                            break
                        if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                            issue = "deadline"
                            break
                        if frame["depth"] == 0 and raw_name == os.fsencode(MANIFEST_NAME):
                            frame["cookie"] = cookie
                            continue
                        if checkpoint["members"] >= max_entries:
                            issue = ENTRY_LIMIT
                            break
                        child, metadata = checked_child(fd, raw_name, mount_id, profile=profile)
                        try:
                            if stat.S_ISDIR(metadata.st_mode) and frame["depth"] >= max_depth:
                                issue = DEPTH_LIMIT
                                break
                            size = metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
                            if size > max_bytes - checkpoint["apparent_bytes"]:
                                issue = BYTE_LIMIT
                                break
                            if hash_files and size > batch_bytes - read_bytes:
                                issue = "batch_byte_limit"
                                break
                            member = {"name": PathIdentity.from_path(raw_name).as_dict(),
                                      "parent": [boundary["name"] for boundary in checkpoint["frontier"][1:]], "identity": list(_identity(metadata)),
                                      "member_index": checkpoint["members"] + 1,
                                      "mode": metadata.st_mode, "size": size,
                                      "mtime_ns": metadata.st_mtime_ns, "ctime_ns": metadata.st_ctime_ns}
                            if hash_files and stat.S_ISREG(metadata.st_mode):
                                hasher = hashlib.sha256()
                                read_fd = os.open(f"/proc/self/fd/{child}", os.O_RDONLY | os.O_NONBLOCK)
                                with os.fdopen(read_fd, "rb") as stream:
                                    total = 0
                                    while chunk := stream.read(min(1 << 20, size - total + 1)):
                                        total += len(chunk)
                                        read_bytes += len(chunk)
                                        if total > size:
                                            raise ScratchSecurityError("observation payload changed")
                                        stop_requested = cancelled is not None and cancelled()
                                        expired = deadline_ns is not None and time.monotonic_ns() >= deadline_ns
                                        if stop_requested or expired:
                                            issue = "cancelled" if stop_requested else "deadline"
                                            break
                                        hasher.update(chunk)
                                    after = os.fstat(stream.fileno())
                                if issue:
                                    break
                                if (total != size or after.st_mtime_ns != metadata.st_mtime_ns
                                        or after.st_ctime_ns != metadata.st_ctime_ns
                                        or _identity(after) != _identity(metadata)):
                                    raise ScratchSecurityError("observation payload changed")
                                member["sha256"] = hasher.hexdigest()
                                member_bytes = len(_canonical_json(member).encode("utf-8"))
                                if member_bytes > _MAX_CHECKPOINT_BYTES - 4096 - hash_receipt_bytes:
                                    issue = "checkpoint_byte_limit"
                                    break
                                hash_records.append(dict(member))
                                hash_receipt_bytes += member_bytes
                                checkpoint["hashed_bytes"] += total
                            elif stat.S_ISLNK(metadata.st_mode):
                                member["target"] = PathIdentity.from_path(os.fsencode(os.readlink(raw_name, dir_fd=fd))).as_dict()
                            checkpoint["content_chain"] = hashlib.sha256(
                                bytes.fromhex(checkpoint["content_chain"]) + _canonical_json(member).encode()).hexdigest()
                            checkpoint["members"] += 1
                            checkpoint["apparent_bytes"] += size
                            consumed += 1
                            frame["cookie"] = cookie
                            frame["last_name"] = PathIdentity.from_path(raw_name).as_dict()
                            if stat.S_ISDIR(metadata.st_mode):
                                checkpoint["frontier"].append({
                                    "name": PathIdentity.from_path(raw_name).as_dict(), "depth": frame["depth"] + 1,
                                    "claim": directory_claim(child), "cookie": 0, "last_name": None,
                                    "start_members": checkpoint["members"], "start_bytes": checkpoint["apparent_bytes"],
                                    "start_hashed_bytes": checkpoint["hashed_bytes"], "start_chain": checkpoint["content_chain"]})
                                descended = True
                                break
                        finally:
                            os.close(child)
                    if issue:
                        break
                    if exhausted and not descended:
                        checkpoint["frontier"].pop()
                except (OSError, ScratchTreeError) as exc:
                    issue = exc.issue if isinstance(exc, ScratchTreeError) else "payload_identity_unavailable"
                    break
                finally:
                    os.close(fd)
            checkpoint["coverage"] = "complete" if not checkpoint["frontier"] and issue is None else (
                "partial" if checkpoint["members"] else "not_measured")
            checkpoint["issue"] = issue
            checkpoint["batch_members"] = consumed
            checkpoint["batch_read_bytes"] = read_bytes
            checkpoint["rewound_segments"] = rewound
            checkpoint["rewound_member_prefixes"] = rewound_prefixes
            checkpoint["batches"] += 1
            checkpoint["updated_ns"] = time.time_ns()
            if issue and issue not in {"batch_limit", "batch_byte_limit"}:
                checkpoint["errors"] = [*checkpoint["errors"][-31:], issue]
            if hash_records:
                receipt_name = f"observation-{operation_name}-batch-{checkpoint['batches']}.json"
                hash_receipt = {
                    "schema": "neocortex.scratch-observation-hashes/v1",
                    "generation_id": checkpoint["generation_id"], "policy_digest": policy_digest,
                    "root_identity": checkpoint["root_identity"],
                    "posix_path_identity": checkpoint["posix_path_identity"],
                    "batch": checkpoint["batches"], "content_chain": checkpoint["content_chain"],
                    "hashes": hash_records, "rewound_member_prefixes": rewound_prefixes,
                }
                hash_receipt["manifest_digest"] = _manifest_digest(hash_receipt)
                _write_json_atomic(journal / receipt_name, hash_receipt, limit=_MAX_CHECKPOINT_BYTES)
                checkpoint["last_hash_receipt"] = receipt_name
            checkpoint["manifest_digest"] = _manifest_digest(checkpoint)
            _write_json_atomic(checkpoint_path, checkpoint, limit=_MAX_CHECKPOINT_BYTES)
            return dict(checkpoint)

def create(manager,
    *,
    run_id: int | str | None = None,
    retain_on_success: bool = False,
    metadata: Mapping[str, Any] | None = None,
    payload_profile: PayloadProfile | str = PayloadProfile.STRICT,
    fixture_grant: FixturePayloadGrant | None = None,
    workspace_factory: Any | None = None,
) -> Any:
    """Create one private registered workspace after an explicit claim."""

    if manager.owner is None:
        raise ScratchSecurityError(
            "federated scratch view is read-only for workspace creation"
        )
    if not manager._ensure_root(create=manager.create_root):
        raise ScratchRootError("scratch root is absent")
    if type(retain_on_success) is not bool:
        raise ValueError("scratch retain_on_success must be a boolean")
    if run_id is not None and (
        (type(run_id) is int and run_id < 1)
        or type(run_id) is bool
        or (not isinstance(run_id, (int, str)))
        or (isinstance(run_id, str) and not run_id.strip())
    ):
        raise ValueError("scratch run_id must be a positive integer, string or null")
    if isinstance(run_id, str) and len(run_id.encode("utf-8")) > 256:
        raise ValueError("scratch run_id exceeds 256 UTF-8 bytes")
    profile = PayloadProfile(payload_profile)
    grant_payload = None
    if profile is PayloadProfile.FIXTURE_POSIX_V1:
        if not isinstance(fixture_grant, FixturePayloadGrant):
            raise ScratchSecurityError("fixture profile requires an issued creation grant")
        grant_payload = manager._read_fixture_grant(fixture_grant.grant_id)
        if (grant_payload.get("state") != "issued" or fixture_grant.owner != manager.owner
                or any(grant_payload.get(key) != getattr(fixture_grant, key) for key in
                       ("grant_id", "owner", "activity_id", "creation_grant_id"))):
            raise ScratchSecurityError("fixture creation grant is unavailable or consumed")
    elif fixture_grant is not None:
        raise ScratchSecurityError("strict profile cannot consume a fixture grant")
    safe_metadata: dict[str, Any] = {} if metadata is None else dict(metadata)
    if len(_canonical_json(safe_metadata).encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("scratch metadata exceeds the durable size limit")
    for _ in range(8):
        record_id = uuid.uuid4().hex
        path = manager.root / f"workspace-{record_id}"
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        created = time.time_ns()
        identity = _identity(path.lstat())
        root_identity = _identity(manager.root.lstat())
        preserved_observation = (
            manager._preserve_workspace_observation(path)
            if manager._artifact_registry_configured
            else None
        )
        payload: dict[str, Any] = {
            "schema": SCRATCH_SCHEMA,
            "record_id": record_id,
            "owner": manager.owner,
            "run_id": run_id,
            "path": str(path),
            "path_identity": list(identity),
            "root_identity": list(root_identity),
            "state": ScratchState.ACTIVE.value,
            "created_ns": created,
            "updated_ns": created,
            "retain_on_success": bool(retain_on_success),
            "retire_after_ns": None,
            "result_paths": [],
            "metadata": safe_metadata,
            "reason": None,
        }
        payload["payload_profile"] = profile.value
        payload["policy_revision"] = policy_revision(profile)
        payload["posix_path_identity"] = PathIdentity.from_path(path).as_dict()
        if grant_payload is not None:
            grant_payload.update(state="bound", record_id=record_id,
                                 path_identity=list(identity),
                                 posix_path_identity=PathIdentity.from_path(path).as_dict())
            grant_payload["manifest_digest"] = _manifest_digest(grant_payload)
            _write_json_atomic(manager._control_directory() / f"grant-{grant_payload['grant_id']}.json",
                               grant_payload)
            payload["fixture_grant"] = {key: grant_payload[key] for key in
                ("grant_id", "owner", "activity_id", "creation_grant_id", "policy_revision")}
        if manager._artifact_registry_configured:
            # The manifest is the scratch owner's durable claim.  Write
            # it before invoking the secondary registry so a failed hook
            # leaves evidence for recovery rather than silently removing
            # an unclaimed workspace.
            manager._add_artifact_manifest_fields(payload)
        payload["manifest_digest"] = _manifest_digest(payload)
        try:
            _write_json_atomic(path / MANIFEST_NAME, payload)
        except BaseException:
            _remove_tree_no_follow(path, expected_identity=identity)
            raise
        if manager._artifact_registry_configured:
            if preserved_observation is not None:
                manager._restore_workspace_observation(path, preserved_observation)
            # Registration is a delivery gate: callers never receive a
            # workspace whose artifact record could not be established.
            # Deliberately do not clean up here; the workspace remains a
            # registered-scratch recovery subject for a later inspection.
            manager._register_artifact(path, payload)
        record = manager._record_from_payload(path, payload, size_bytes=0)
        if workspace_factory is None:
            raise ScratchError("scratch workspace factory is unavailable")
        return workspace_factory(manager, record, retain_on_success=bool(retain_on_success))
    raise ScratchError("could not allocate a unique scratch workspace")

def apply(manager,
    plan: ScratchPlan | None = None,
    *,
    now_ns: int | None = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
    max_bytes: int | None = None,
    max_fds: int = 2048,
    scan_max_entries: int | None = None,
    scan_max_depth: int | None = None,
    scan_max_bytes: int | None = None,
) -> ScratchPlan:
    """Re-scan and retire only completed, still-eligible records."""

    if plan is not None:
        if not isinstance(plan, ScratchPlan) or plan.root != manager.root:
            raise ScratchSecurityError("scratch plan belongs to a different root")
        # Supplied limits may narrow a selection but cannot broaden the
        # original observation or introduce records created afterward.
        max_entries = plan.max_entries if max_entries is None else max_entries
        max_depth = plan.max_depth if max_depth is None else max_depth
        max_bytes = plan.max_bytes if max_bytes is None else max_bytes
    if scan_max_entries is not None:
        max_entries = scan_max_entries
    if scan_max_depth is not None:
        max_depth = scan_max_depth
    if scan_max_bytes is not None:
        max_bytes = scan_max_bytes
    if plan is not None:
        if max_entries is not None and plan.max_entries is not None:
            max_entries = min(max_entries, plan.max_entries)
        if max_depth is not None and plan.max_depth is not None:
            max_depth = min(max_depth, plan.max_depth)
        if max_bytes is not None and plan.max_bytes is not None:
            max_bytes = min(max_bytes, plan.max_bytes)
        max_fds = min(max_fds, plan.max_fds)
    max_entries, max_depth, max_bytes = _validate_scan_limits(
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
    )
    if type(max_fds) is not int or not 5 <= max_fds <= 2048:
        raise ValueError("scratch max_fds must be between 5 and 2048")
    now = time.time_ns() if now_ns is None else now_ns
    if type(now) is not int or now < 0:
        raise ValueError("scratch now_ns must be a non-negative integer")
    if not manager._ensure_root(create=False):
        return ScratchPlan(
            manager.root,
            reason="scratch root is absent",
            read_only=False,
            root_blocked="scratch root is absent",
            max_entries=max_entries,
            max_depth=max_depth,
            max_bytes=max_bytes,
        )
    # Reconcile registry intents before discovering candidates.  This is a
    # metadata-only recovery pass: it may confirm a path already absent,
    # but it never repeats a physical retirement.  A registry without the
    # optional recovery hook remains compatible with legacy scratch.
    registry_for_recovery = (
        manager._artifact_registry_instance()
        if manager._artifact_registry_configured
        else None
    )
    if registry_for_recovery is not None:
        recover = getattr(registry_for_recovery, "recover_retirements", None)
        if callable(recover):
            try:
                recover()
            except BaseException:
                # Do not turn an inability to reconcile one registry into
                # permission to remove scratch.  The per-record guard
                # below will preserve the candidate and expose the
                # recovery boundary.
                pass
    budget = _ScratchScanBudget(max_entries, max_depth, max_bytes, max_fds=max_fds)
    observed = tuple(manager._scan_records(now_ns=now, budget=budget))
    if not budget.truncated:
        observed = manager._complete_seal_observations(observed, budget)
    if budget.truncated:
        observed = tuple(
            replace(
                record,
                eligible=False,
                issue=record.issue or "scan_incomplete",
                reason=record.reason or "scan_incomplete",
            )
            for record in observed
        )
    if plan is not None:
        planned_records = {record.record_id: record for record in plan.records}
        selected = []
        for record in observed:
            prior = planned_records.get(record.record_id)
            if prior is None or not prior.eligible:
                selected.append(replace(record, eligible=False))
            elif (not plan.complete or not all(item.size_complete for item in observed)
                  or manager._selection_claim(prior) != manager._selection_claim(record)):
                selected.append(replace(record, eligible=False, issue="selection_changed",
                                        reason="scratch selection changed after planning"))
            else:
                selected.append(record)
        observed_ids = {record.record_id for record in observed}
        selected.extend(replace(record, eligible=False, issue="selection_changed",
                                reason="scratch selection is absent after planning")
                        for record in plan.records if record.record_id not in observed_ids
                        and not manager._retirement_replayed(record))
        observed = tuple(selected)
    candidates = [record for record in observed if record.eligible]
    applied_records: list[ScratchRecord] = []
    remaining: list[ScratchRecord] = []
    batch_factory = (
        getattr(registry_for_recovery, "retirement_batch_guard", None)
        if registry_for_recovery is not None
        else None
    )
    batch_context = batch_factory() if callable(batch_factory) else nullcontext(None)
    with cast(Any, batch_context) as retirement_session:
        for record in observed:
            if not record.eligible:
                remaining.append(record)
                continue
            try:
                manager._retire_record(
                    record,
                    workspace_records=observed,
                    retirement_session=retirement_session,
                )
            except ScratchSecurityError as exc:
                remaining.append(
                    replace(record, state="blocked", reason=str(exc), eligible=False)
                )
            except OSError as exc:
                remaining.append(
                    replace(record, state="failed", reason=str(exc), eligible=False)
                )
            else:
                applied_records.append(record)
    records_after = tuple(remaining)
    summary = manager._summarize(
        records_after,
        read_only=False,
        unmanaged=manager._unmanaged_entries(limit=max_entries),
        truncated=budget.truncated,
        truncation_reasons=tuple(budget.truncation_reasons),
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
    )
    status = (
        "recovery_required"
        if summary.recovery_required
        else "failed"
        if summary.failed
        else "blocked"
        if summary.blocked
        else "applied"
    )
    return ScratchPlan(
        root=summary.root,
        records=records_after,
        planned=len(candidates),
        applied=len(applied_records),
        kept=summary.kept,
        blocked=summary.blocked,
        failed=summary.failed,
        recovery_required=summary.recovery_required,
        planned_bytes=sum(record.size_bytes for record in candidates),
        applied_bytes=sum(record.size_bytes for record in applied_records),
        kept_bytes=summary.kept_bytes,
        blocked_bytes=summary.blocked_bytes,
        failed_bytes=summary.failed_bytes,
        recovery_required_bytes=summary.recovery_required_bytes,
        status=status,
        reason=summary.reason,
        read_only=False,
        unmanaged=summary.unmanaged,
        root_blocked=summary.root_blocked,
        truncated=budget.truncated,
        truncation_reasons=tuple(budget.truncation_reasons),
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        max_fds=max_fds, observed_members=budget.payload_entries, observed_bytes=budget.observed_bytes,
    )

def perform_retirement(manager, record: ScratchRecord) -> None:
    journal = manager._control_directory(create=True)
    receipt_path = journal / f"retired-{record.record_id}.json"
    receipt: dict[str, Any] = {"schema": "neocortex.scratch-retirement/v1", "record_id": record.record_id,
               "owner": record.owner, "source_manifest_digest": record.manifest_digest,
               "path_identity": list(record.path_identity or ()),
               "posix_path_identity": record.posix_path_identity,
               "payload_profile": record.payload_profile, "policy_revision": record.policy_revision,
               "status": "prepared", "effects": 0}
    def publish() -> None:
        receipt["manifest_digest"] = _manifest_digest(receipt)
        _write_json_atomic(receipt_path, receipt)
    publish()
    def account(count: int) -> None:
        receipt["effects"] += count
    try:
        _remove_tree_no_follow(record.path, expected_identity=record.path_identity,
                               profile=record.payload_profile, effect_callback=account)
    except BaseException as exc:
        receipt["status"] = "recovery_required" if receipt["effects"] else "blocked"
        receipt["issue"] = f"{type(exc).__name__}: {exc}"
        publish()
        raise
    receipt["status"] = "retired"
    publish()

def retire_record(manager,
    record: ScratchRecord,
    *,
    workspace_records: Sequence[ScratchRecord] | None = None,
    retirement_session: Any | None = None,
) -> None:
    if manager.owner is None:
        raise ScratchSecurityError(
            "federated scratch view is read-only for retirement"
        )
    if record.owner != manager.owner or record.state != ScratchState.COMPLETED.value:
        raise ScratchSecurityError("only this owner's completed scratch can be retired")
    manager._ensure_root(create=False)
    parent_metadata = manager.root.lstat()
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & 0o077
    ):
        raise ScratchSecurityError("scratch root changed its private-directory claim")
    current = manager._record_for_path(record.path)
    if current is None or current.record_id != record.record_id or not current.eligible:
        raise ScratchSecurityError("scratch record is no longer eligible")
    if current.path_identity is None or not _same_identity(record.path, current.path_identity):
        raise ScratchSecurityError("scratch workspace identity changed before retirement")
    if workspace_records is None:
        workspace_records = tuple(manager._scan_records(now_ns=time.time_ns()))
    if not manager._workspace_dependency_observation_complete(workspace_records):
        raise ScratchSecurityError("scratch dependency observation incomplete")
    registry_snapshot: Sequence[Any] | None = None
    if manager._artifact_registry_configured:
        registry = manager._artifact_registry_instance()
        if retirement_session is not None:
            snapshot = getattr(retirement_session, "records", None)
            registry_snapshot = snapshot if isinstance(snapshot, Sequence) else None
        if registry_snapshot is None and registry is not None:
            records_method = getattr(registry, "records", None)
            if callable(records_method):
                candidate_snapshot = records_method()
                registry_snapshot = (
                    candidate_snapshot
                    if isinstance(candidate_snapshot, Sequence)
                    else None
                )
    dependents = manager._live_workspace_dependents(
        current,
        workspace_records,
        registry_records=registry_snapshot,
    )
    if dependents:
        raise ScratchSecurityError(
            "scratch workspace has live dependents: " + ", ".join(dependents[:16])
        )
    if manager._artifact_registry_configured and current.artifact_id is not None:
        # The registry guard is held across the physical effect.  A live
        # consumer cannot be registered between the dependency check and
        # the unlink, and a failed unlink never receives a false retired
        # state.  Legacy scratch manifests without an artifact projection
        # remain compatible and are retired by their own owner only.
        current_payload: dict[str, Any] = {
            "record_id": current.record_id,
            "artifact_id": current.artifact_id,
            "owner": current.owner,
            "run_id": current.run_id,
            "path": str(current.path),
            "path_identity": list(current.path_identity),
            "state": current.state.value
            if isinstance(current.state, ScratchState)
            else current.state,
            "retain_on_success": current.retain_on_success,
            "retire_after_ns": current.retire_after_ns,
            "metadata": dict(current.metadata),
            "payload_profile": current.payload_profile,
            "policy_revision": current.policy_revision,
            "fixture_grant": current.fixture_grant,
            "manifest_digest": current.manifest_digest,
            "path_size_bytes": current.size_bytes,
            "path_mtime_ns": 0,
        }
        registry = manager._artifact_registry_instance()
        guard = getattr(registry, "retirement_guard", None) if registry is not None else None
        if retirement_session is not None:
            guard_context = retirement_session.guard(current.artifact_id)
        else:
            guard_context = guard(current.artifact_id) if callable(guard) else None
        if callable(guard):
            try:
                with cast(Any, guard_context) as guarded:
                    # The registry guard may have persisted an intent
                    # claim in its own manifest.  Preserve that metadata
                    # when the scratch projection publishes the terminal
                    # state; otherwise the successful update would erase
                    # the recovery receipt before it can be confirmed.
                    if guarded is not None:
                        guarded_metadata = getattr(guarded, "metadata", None)
                        if isinstance(guarded_metadata, Mapping):
                            current_payload["metadata"] = dict(guarded_metadata)
                    manager._perform_retirement(current)
                    manager._update_artifact(
                        current.path,
                        current_payload,
                        state=ScratchState.RETIRED.value,
                    )
            except OSError:
                raise
            except ScratchSecurityError:
                raise
            except BaseException as exc:
                manager._raise_artifact_failure("retire", exc)
            return
        if retirement_session is not None:
            # A session was supplied only by the canonical registry
            # implementation; reaching this branch indicates a broken
            # adapter, so preserve rather than unlink without its policy
            # guard.
            raise ScratchSecurityError("artifact retirement batch guard unavailable")
        manager._perform_retirement(current)
        manager._update_artifact(
            current.path,
            current_payload,
            state=ScratchState.RETIRED.value,
        )
        return
    manager._perform_retirement(current)

def reconcile_terminal(manager,
    record_id: str,
    *,
    release_authorized: bool,
    evidence: Mapping[str, Any] | None = None,
    now_ns: int | None = None,
) -> ScratchRecord:
    """Reconcile one failed/recovery workspace into completed-retain.

    Age, a missing PID, and a TTL are not evidence.  This owner-side
    operation requires an explicit release authorization plus bounded
    evidence, validates the workspace/dependency/publication boundaries
    under the scratch/registry locks, and leaves the payload intact for
    the ordinary completed-workspace retirement path.
    """

    if manager.owner is None:
        raise ScratchSecurityError(
            "federated scratch view is read-only for reconciliation"
        )
    if type(release_authorized) is not bool or not release_authorized:
        raise ScratchSecurityError("terminal reconciliation requires release authorization")
    if not isinstance(evidence, Mapping) or not evidence:
        raise ScratchSecurityError("terminal reconciliation requires explicit evidence")
    if len(_canonical_json(dict(evidence)).encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ScratchSecurityError("terminal reconciliation evidence exceeds the durable limit")
    when = time.time_ns() if now_ns is None else now_ns
    if type(when) is not int or when < 0:
        raise ValueError("terminal reconciliation now_ns must be a non-negative integer")
    record_id = _bounded_text(record_id, label="scratch record id", limit=128)
    registry = manager._artifact_registry_instance() if manager._artifact_registry_configured else None
    registry_lock = getattr(registry, "_registry_lock", None) if registry is not None else None
    registry_context = registry_lock() if callable(registry_lock) else nullcontext()
    with manager._scratch_lock():
        with cast(Any, registry_context):
            records = tuple(manager._scan_records(now_ns=when))
            current = next((item for item in records if item.record_id == record_id), None)
            if current is None or current.owner != manager.owner:
                raise ScratchSecurityError("scratch workspace does not belong to this owner")
            if current.state not in {
                ScratchState.FAILED_RETAINED,
                ScratchState.RECOVERY_REQUIRED,
            }:
                raise ScratchError("only failed-retained or recovery-required workspaces can be reconciled")
            if not current.valid or current.path_identity is None:
                raise ScratchSecurityError("terminal workspace identity is not verified")
            if not _same_identity(current.path, current.path_identity):
                raise ScratchSecurityError("terminal workspace identity changed")
            if not manager._workspace_dependency_observation_complete(records):
                raise ScratchSecurityError("scratch dependency observation incomplete")
            dependents = manager._live_workspace_dependents(current, records)
            if dependents:
                raise ScratchSecurityError(
                    "scratch workspace has live dependents: " + ", ".join(dependents[:16])
                )
            if current.state == ScratchState.RECOVERY_REQUIRED and not (
                evidence.get("recovered") is True
                or evidence.get("recovery_resolved") is True
            ):
                raise ScratchSecurityError("recovery evidence is incomplete")
            note: Mapping[str, Any] = {}
            if isinstance(current.reason, str) and current.reason.startswith("agent-activity-note:"):
                try:
                    parsed = json.loads(current.reason[len("agent-activity-note:") :])
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, Mapping):
                    note = parsed
            publication = note.get("publication")
            if publication is not None and evidence.get("publication_resolved") is not True:
                raise ScratchSecurityError("publication reconciliation is still pending")
            registry_snapshot: Sequence[Any] | None = None
            if registry is not None:
                records_method = getattr(registry, "records", None)
                if callable(records_method):
                    candidate_snapshot = records_method()
                    registry_snapshot = (
                        candidate_snapshot
                        if isinstance(candidate_snapshot, Sequence)
                        else None
                    )
                if registry_snapshot is not None and current.artifact_id is not None:
                    matching = next(
                        (
                            item
                            for item in registry_snapshot
                            if getattr(item, "artifact_id", None) == current.artifact_id
                        ),
                        None,
                    )
                    if matching is None or not getattr(matching, "valid", False):
                        raise ScratchSecurityError("registry recovery evidence is incomplete")
                    if getattr(matching, "owner", None) != manager.owner:
                        raise ScratchSecurityError("registry owner does not match terminal workspace")
            reconciliation = {
                "schema": "neocortex.scratch-terminal-reconciliation/v1",
                "authorized": True,
                "authorized_ns": when,
                "prior_state": current.state.value
                if isinstance(current.state, ScratchState)
                else str(current.state),
                "prior_reason": current.reason,
                "evidence": dict(evidence),
            }
            metadata = dict(current.metadata)
            metadata["terminal_reconciliation"] = reconciliation
            return manager._update_state(
                current.path,
                current.record_id,
                ScratchState.COMPLETED,
                retain_on_success=True,
                retire_after_ns=when,
                reason=current.reason,
                metadata=metadata,
            )
