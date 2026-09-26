# ruff: noqa: F401
"""Registered, private scratch workspaces for bounded NeoCortex work.

This module is deliberately a small filesystem owner.  It does not inspect
``/tmp`` (or any other parent) looking for names that happen to look like
NeoCortex artifacts.  A workspace is discoverable only when NeoCortex created
its private directory and its authenticated manifest.  The manifest binds the
owner, lifecycle state and POSIX identity of the directory; every retirement
re-validates those claims immediately before unlinking the workspace.

The owner is intentionally independent of SQLite and KIO.  SQLite snapshots,
release staging, corpus actions and recovery checkpoints have stronger,
separate contracts and must not be adopted by this cleaner.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import stat
import time
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Self, cast

from .scratch_tree import (
    ScratchTreeError, read_private_manifest, remove_claimed_tree, PayloadProfile,
    policy_revision, observe_claimed_tree, opened_claimed_tree,
    prepared_fixture_tree, _checked_child, _mount_id,
    directory_batch,
)
from .path_identity import PathIdentity
from .control.process_scope import verified_process_quiescence

from .scratch_contracts import (
    SCRATCH_SCHEMA, MANIFEST_NAME, _MAX_MANIFEST_BYTES, _MAX_CHECKPOINT_BYTES, _MAX_REASON_BYTES,
    _MAX_METADATA_BYTES, _MAX_RECORDS, _MAX_SCAN_DEPTH, _MAX_SCAN_BYTES, _ARTIFACT_REGISTRY_MODULES,
    _ARTIFACT_PRODUCER, _ARTIFACT_KIND, ENTRY_LIMIT, DEPTH_LIMIT, BYTE_LIMIT,
    _SEAL_SCHEMA, _CONTROL_DIRECTORY, _FIXTURE_GRANT_SCHEMA, _validate_scan_limit, _validate_scan_limits,
    ScratchError, ScratchSecurityError, ScratchRootError, ScratchManifestError, ScratchState,
    _canonical_json, _bounded_text, _identity, _same_identity, _validate_absolute_path,
    _path_is_within, _is_control_manifest, _read_manifest_bytes, _directory_size, _seal_mapping,
    _seal_from_lifecycle_reason, _activity_process_scope_issue, _sealed_workspace_digest, workspace_payload_digest, verified_workspace_payload_profile,
    _bounded_payload_observation, _ScratchScanBudget, _PayloadObservation, _workspace_payload_issue, _remove_tree_no_follow,
    _manifest_digest, _write_json_atomic, _load_artifact_registry_type, _invoke_artifact_callable, _scratch_write_locked,
    FixturePayloadGrant, ScratchRecord, ScratchPlan,
)

class ScratchWorkspace:
    """Context manager for one manager-owned private workspace."""

    def __init__(
        self,
        manager: "ScratchManager",
        record: ScratchRecord,
        *,
        retain_on_success: bool,
    ) -> None:
        self._manager = manager
        self._record_id = record.record_id
        self._path = record.path
        self._retain_on_success = retain_on_success
        self._state = ScratchState(record.state)
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record_id(self) -> str:
        return self._record_id

    @property
    def artifact_id(self) -> str | None:
        """Return the optional cross-owner artifact identity."""

        return self.record.artifact_id

    @property
    def state(self) -> ScratchState:
        record = self.record
        if not record.valid or not isinstance(record.state, ScratchState):
            raise ScratchManifestError(record.reason or "scratch manifest is invalid")
        self._state = record.state
        return record.state

    @property
    def record(self) -> ScratchRecord:
        record = self._manager._record_for_path(self._path)
        if record is None:
            raise ScratchSecurityError("workspace manifest no longer matches its claim")
        return record

    def mark_committing(self) -> "ScratchWorkspace":
        self._ensure_open()
        if self._state not in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            raise ScratchError(f"workspace is not active: {self._state.value}")
        self._state = ScratchState.COMMITTING
        self._manager._update_state(self._path, self._record_id, ScratchState.COMMITTING)
        return self

    def complete(
        self,
        result_paths: Iterable[Path | str] = (),
        *,
        retain: bool | None = None,
    ) -> ScratchRecord | None:
        self._ensure_open()
        if self._state not in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            raise ScratchError(f"workspace is not active: {self._state.value}")
        # Validate the durable-result claim before changing the state.  A
        # rejected result must leave an active workspace retryable, not turn it
        # into a misleading ``committing`` row.
        normalized_results = self._manager._validate_result_paths(self._path, result_paths)
        self._state = ScratchState.COMMITTING
        self._manager._update_state(self._path, self._record_id, ScratchState.COMMITTING)
        if retain is not None and type(retain) is not bool:
            raise ValueError("scratch retain must be a boolean or null")
        keep = self._retain_on_success if retain is None else retain
        record = self._manager._update_state(
            self._path,
            self._record_id,
            ScratchState.COMPLETED,
            retain_on_success=keep,
            result_paths=normalized_results,
            retire_after_ns=time.time_ns(),
        )
        self._state = ScratchState.COMPLETED
        if keep:
            return record
        try:
            self._manager._retire_record(record)
        except BaseException as exc:
            self._manager._update_state(
                self._path,
                self._record_id,
                ScratchState.RECOVERY_REQUIRED,
                reason=f"success cleanup failed: {type(exc).__name__}: {exc}",
            )
            raise
        self._closed = True
        return None

    def fail(self, reason: object) -> ScratchRecord:
        self._ensure_open()
        bounded = _bounded_text(reason, label="scratch failure reason")
        self._state = ScratchState.FAILED_RETAINED
        return self._manager._update_state(
            self._path,
            self._record_id,
            ScratchState.FAILED_RETAINED,
            reason=bounded,
        )

    def retire(self) -> None:
        self._ensure_open()
        record = self._manager._record_for_path(self._path)
        if record is None or record.record_id != self._record_id:
            raise ScratchSecurityError("workspace manifest no longer matches its claim")
        if record.state != ScratchState.COMPLETED.value:
            raise ScratchError("only a completed workspace can be retired")
        self._manager._retire_record(record)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ScratchError("workspace is already closed")
        try:
            metadata = self._path.lstat()
        except OSError as exc:
            raise ScratchSecurityError("scratch workspace is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("scratch workspace is no longer a directory")

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._closed:
            return None
        if exc is not None:
            try:
                self.fail(f"{type(exc).__name__}: {exc}")
            except BaseException:
                # Preserve the primary exception; the manager will report the
                # recovery-required workspace on the next plan.
                pass
            return None
        if self._state in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            self.complete()
        return None


class ScratchManager:
    """Create, inspect and retire only one private scratch scope."""

    def __init__(
        self,
        root: Path,
        *,
        owner: str | None = "neocortex",
        create_root: bool = False,
        artifact_registry: Any | None = None,
        artifact_registry_root: Path | None = None,
    ) -> None:
        self.root = _validate_absolute_path(Path(root), label="scratch root")
        # ``owner=None`` is an explicitly read-only federated view for a
        # shared scratch scope containing workspaces from several producers.
        # Creation, lifecycle updates and retirement require an exact owner;
        # this view is only for bounded records()/plan() inspection.
        self.owner = (
            None
            if owner is None
            else _bounded_text(owner, label="scratch owner", limit=128)
        )
        self.create_root = bool(create_root)
        self._scratch_thread_lock = threading.RLock()
        self._scratch_lock_depth = 0
        if artifact_registry is not None and artifact_registry_root is not None:
            raise ValueError(
                "scratch artifact_registry and artifact_registry_root are mutually exclusive"
            )
        self._artifact_registry = artifact_registry
        self._artifact_registry_root = (
            None
            if artifact_registry_root is None
            else _validate_absolute_path(
                Path(artifact_registry_root), label="artifact registry root"
            )
        )
        if create_root:
            self._ensure_root(create=True)

    def _ensure_root(self, *, create: bool) -> bool:
        try:
            metadata = self.root.lstat()
        except FileNotFoundError:
            if not create:
                return False
            self._mkdir_private(self.root)
            metadata = self.root.lstat()
        except OSError as exc:
            raise ScratchSecurityError(f"cannot inspect scratch root: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("scratch root must be a regular directory, not a symlink")
        if metadata.st_mode & 0o077:
            raise ScratchSecurityError("scratch root must be private (mode 0700 or stricter)")
        return True

    def _control_directory(self, *, create: bool = False) -> Path:
        path = self.root / _CONTROL_DIRECTORY
        if create:
            self._mkdir_private(path)
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            raise ScratchSecurityError("scratch control journal protection drifted")
        return path

    def _retirement_replayed(self, record: ScratchRecord) -> bool:
        try:
            path = self._control_directory() / f"retired-{record.record_id}.json"
            receipt = json.loads(_read_manifest_bytes(path))
            return (receipt.get("schema") == "neocortex.scratch-retirement/v1"
                    and receipt.get("manifest_digest") == _manifest_digest(receipt)
                    and receipt.get("status") == "retired"
                    and receipt.get("owner") == record.owner
                    and receipt.get("path_identity") == list(record.path_identity or ())
                    and receipt.get("source_manifest_digest") == record.manifest_digest
                    and receipt.get("posix_path_identity") == record.posix_path_identity)
        except (OSError, ValueError, ScratchError):
            return False

    def observe_workspace_batch(
        self, record_id: str, *, operation_id: str, batch_entries: int = 1000,
        batch_bytes: int = 64 << 20, max_entries: int = 10_000_000,
        max_bytes: int = _MAX_SCAN_BYTES, max_depth: int = 2048, max_fds: int = 8,
        hash_files: bool = False, cancelled: Any | None = None,
        deadline_ns: int | None = None,
    ) -> dict[str, Any]:
        from .scratch_lifecycle import observe_workspace_batch
        return observe_workspace_batch(
            self, record_id, operation_id=operation_id, batch_entries=batch_entries,
            batch_bytes=batch_bytes, max_entries=max_entries, max_bytes=max_bytes,
            max_depth=max_depth, max_fds=max_fds, hash_files=hash_files,
            cancelled=cancelled, deadline_ns=deadline_ns,
            checked_child_fn=_checked_child, batch_reader_fn=directory_batch,
        )

    def _perform_retirement(self, record: ScratchRecord) -> None:
        from .scratch_lifecycle import perform_retirement
        return perform_retirement(self, record)

    def issue_fixture_grant(self, *, activity_id: str, creation_grant_id: str,
                            authorized: bool) -> FixturePayloadGrant:
        """Issue a single-use fixture policy from an explicit owner action.

        A name, same UID, age, content digest or free-form metadata never issues
        this grant. Existing workspaces cannot be relabelled through this API.
        """
        if self.owner is None or authorized is not True:
            raise ScratchSecurityError("fixture profile requires explicit owner authorization")
        activity_id = _bounded_text(activity_id, label="fixture activity id", limit=256)
        creation_grant_id = _bounded_text(creation_grant_id, label="fixture creation grant", limit=256)
        if not self._ensure_root(create=self.create_root):
            raise ScratchRootError("scratch root is absent")
        with self._scratch_lock():
            journal = self._control_directory(create=True)
            grant_id = uuid.uuid4().hex
            payload = {"schema": _FIXTURE_GRANT_SCHEMA, "grant_id": grant_id,
                       "owner": self.owner, "activity_id": activity_id,
                       "creation_grant_id": creation_grant_id,
                       "payload_profile": PayloadProfile.FIXTURE_POSIX_V1.value,
                       "policy_revision": policy_revision(PayloadProfile.FIXTURE_POSIX_V1),
                       "root_identity": list(_identity(self.root.lstat())),
                       "state": "issued", "created_ns": time.time_ns()}
            payload["manifest_digest"] = _manifest_digest(payload)
            _write_json_atomic(journal / f"grant-{grant_id}.json", payload)
            return FixturePayloadGrant(grant_id, self.owner, activity_id, creation_grant_id)

    def _read_fixture_grant(self, grant_id: str) -> dict[str, Any]:
        if (not isinstance(grant_id, str) or len(grant_id) != 32
                or any(character not in "0123456789abcdef" for character in grant_id)):
            raise ScratchSecurityError("fixture grant identifier is invalid")
        value = json.loads(_read_manifest_bytes(self._control_directory() / f"grant-{grant_id}.json"))
        if (not isinstance(value, dict) or value.get("schema") != _FIXTURE_GRANT_SCHEMA
                or value.get("manifest_digest") != _manifest_digest(value)
                or value.get("root_identity") != list(_identity(self.root.lstat()))):
            raise ScratchSecurityError("fixture grant does not match its owner journal")
        return value

    def _payload_profile(self, path: Path, payload: Mapping[str, Any]) -> PayloadProfile:
        try:
            profile = PayloadProfile(payload.get("payload_profile", PayloadProfile.STRICT.value))
        except ValueError as exc:
            raise ScratchSecurityError("unsupported scratch payload profile") from exc
        legacy_revision = policy_revision(profile) if profile is PayloadProfile.STRICT else None
        if payload.get("policy_revision", legacy_revision) != policy_revision(profile):
            raise ScratchSecurityError("scratch payload policy revision changed")
        if profile is PayloadProfile.STRICT:
            if payload.get("fixture_grant") is not None:
                raise ScratchSecurityError("strict workspace cannot carry a fixture grant")
            return profile
        claim = payload.get("fixture_grant")
        if not isinstance(claim, Mapping):
            raise ScratchSecurityError("fixture profile has no creation grant")
        grant_id = claim.get("grant_id")
        if not isinstance(grant_id, str):
            raise ScratchSecurityError("fixture creation grant identifier is invalid")
        grant = self._read_fixture_grant(grant_id)
        if (grant.get("state") != "bound" or grant.get("owner") != payload.get("owner")
                or grant.get("record_id") != payload.get("record_id")
                or grant.get("path_identity") != payload.get("path_identity")
                or grant.get("posix_path_identity") != PathIdentity.from_path(path).as_dict()
                or any(claim.get(key) != grant.get(key) for key in
                       ("grant_id", "owner", "activity_id", "creation_grant_id", "policy_revision"))):
            raise ScratchSecurityError("fixture creation grant does not match workspace")
        return profile

    @contextmanager
    def fixture_permissions(self, record_id: str, *, authorized: bool):
        """Prepare granted fixture directories and restore surviving modes.

        This explicit owner context lets a 0000 fixture be sealed, completed,
        planned and retired under one durable permission receipt. Read-only
        planning alone never changes permissions or claims full coverage.
        """
        if authorized is not True or self.owner is None:
            raise ScratchSecurityError("fixture permission preparation requires owner authorization")
        if len(record_id) != 32 or any(ch not in "0123456789abcdef" for ch in record_id):
            raise ScratchSecurityError("fixture record identifier is invalid")
        with self._scratch_lock():
            path = self.root / f"workspace-{record_id}"
            payload = json.loads(_read_manifest_bytes(path / MANIFEST_NAME))
            if (payload.get("manifest_digest") != _manifest_digest(payload)
                    or payload.get("owner") != self.owner
                    or payload.get("record_id") != record_id
                    or not _same_identity(path, payload.get("path_identity", []))
                    or self._payload_profile(path, payload) is not PayloadProfile.FIXTURE_POSIX_V1):
                raise ScratchSecurityError("fixture permission claim is not authorized")
            journal = self._control_directory()
            receipt_path = journal / f"permissions-{record_id}-{uuid.uuid4().hex}.json"
            def publish(receipt: Mapping[str, Any]) -> None:
                value = {**dict(receipt), "record_id": record_id, "owner": self.owner,
                         "fixture_grant": payload["fixture_grant"],
                         "posix_path_identity": PathIdentity.from_path(path).as_dict()}
                value["manifest_digest"] = _manifest_digest(value)
                _write_json_atomic(receipt_path, value)
            try:
                with prepared_fixture_tree(path, receipt_writer=publish) as repairs:
                    yield repairs
            except ScratchTreeError as exc:
                raise ScratchSecurityError(str(exc)) from exc

    @contextmanager
    def _scratch_lock(self) -> Iterator[None]:
        """Serialize owner writers, including nested permission/lifecycle scopes."""
        with self._scratch_thread_lock:
            if self._scratch_lock_depth:
                self._scratch_lock_depth += 1
                try:
                    yield
                finally:
                    self._scratch_lock_depth -= 1
                return
            self._ensure_root(create=False)
            try:
                import fcntl
                fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError as exc:
                raise ScratchRootError("scratch root could not be locked") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._scratch_lock_depth = 1
                yield
            finally:
                self._scratch_lock_depth = 0
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        try:
            path.mkdir(parents=True, mode=0o700, exist_ok=False)
        except FileExistsError:
            pass
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ScratchSecurityError(f"cannot inspect created scratch root: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("created scratch root is not a directory")
        if metadata.st_uid != os.geteuid():
            raise ScratchSecurityError("scratch root is not owned by the current user")
        if metadata.st_mode & 0o077:
            # Never chmod an existing directory: doing so could alter a path
            # that another owner created between the existence check and this
            # call.  Newly-created directories already inherit 0700 or stricter
            # from mkdir/umask.
            detail = "scratch root must be private (mode 0700 or stricter)"
            raise ScratchSecurityError(detail)

    @property
    def _artifact_registry_configured(self) -> bool:
        return self._artifact_registry is not None or self._artifact_registry_root is not None

    def _artifact_registry_instance(self) -> Any | None:
        """Return the injected or lazily constructed registry.

        Supplying a registry object is the preferred composition seam.  A
        root is also accepted for callers that want the runtime to construct
        the canonical registry, but construction is delayed until a workspace
        is actually created or transitioned.  Consequently ``records()``,
        ``plan()`` and other read-only inspection never create a registry
        directory or call a registry hook.
        """

        if self._artifact_registry is not None:
            return self._artifact_registry
        if self._artifact_registry_root is None:
            return None
        registry_type = _load_artifact_registry_type()
        try:
            try:
                signature = inspect.signature(registry_type)
            except (TypeError, ValueError):
                signature = None
            parameters = () if signature is None else tuple(signature.parameters.values())
            accepts_root_keyword = any(
                parameter.name == "root"
                and parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                for parameter in parameters
            ) or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
            )
            if accepts_root_keyword or signature is None:
                self._artifact_registry = registry_type(
                    root=self._artifact_registry_root,
                    owner=self.owner,
                    create_root=True,
                )
            else:
                self._artifact_registry = registry_type(
                    self._artifact_registry_root,
                    owner=self.owner,
                    create_root=True,
                )
        except BaseException as exc:
            if isinstance(exc, ScratchSecurityError):
                raise
            raise ScratchSecurityError(
                f"could not construct artifact registry: {type(exc).__name__}: {exc}"
            ) from exc
        return self._artifact_registry

    @staticmethod
    def _artifact_id(record_id: str) -> str:
        return f"scratch:{record_id}"

    @staticmethod
    def _artifact_state(state: str) -> str:
        """Project scratch lifecycle into the ArtifactRegistry vocabulary."""

        # Scratch has an internal ``committing`` phase while the registry
        # deliberately keeps a smaller public state set.  It remains active
        # until the scratch owner has published ``completed``.
        return {
            ScratchState.ACTIVE.value: "active",
            ScratchState.COMMITTING.value: "active",
            ScratchState.COMPLETED.value: "completed",
            ScratchState.FAILED_RETAINED.value: "failed",
            ScratchState.RECOVERY_REQUIRED.value: "recovery_required",
            ScratchState.RETIRED.value: "retired",
        }.get(state, state)

    @staticmethod
    def _artifact_purpose(metadata: Mapping[str, Any]) -> str:
        for key in ("purpose", "operation", "component"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return _bounded_text(value, label="scratch artifact purpose", limit=512)
        return "scratch workspace"

    def _add_artifact_manifest_fields(self, payload: dict[str, Any]) -> None:
        """Add the registry projection to one scratch manifest payload."""

        record_id = payload.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ScratchSecurityError("scratch artifact projection has no record id")
        artifact_id = payload.get("artifact_id", self._artifact_id(record_id))
        if (
            not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or len(artifact_id.encode("utf-8")) > 256
        ):
            raise ScratchSecurityError("scratch artifact id is invalid")
        identity = payload.get("path_identity")
        if (
            not isinstance(identity, list)
            or len(identity) != 3
            or any(type(value) is not int for value in identity)
        ):
            raise ScratchSecurityError("scratch artifact identity is invalid")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ScratchSecurityError("scratch artifact metadata is invalid")
        state = payload.get("state")
        if not isinstance(state, str) or not state:
            raise ScratchSecurityError("scratch artifact lifecycle state is invalid")
        retain_on_success = payload.get("retain_on_success", False)
        if type(retain_on_success) is not bool:
            raise ScratchSecurityError("scratch artifact retention flag is invalid")
        retire_after_ns = payload.get("retire_after_ns")
        if retire_after_ns is not None and (
            type(retire_after_ns) is not int or retire_after_ns < 0
        ):
            raise ScratchSecurityError("scratch artifact retention time is invalid")
        manifest_path = payload.get("path")
        if not isinstance(manifest_path, str):
            raise ScratchSecurityError("scratch artifact path is invalid")
        try:
            path_metadata = Path(manifest_path).lstat()
        except OSError as exc:
            raise ScratchSecurityError("scratch artifact path is unavailable") from exc
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode):
            raise ScratchSecurityError("scratch artifact path is not a directory")
        source_ref = payload.get("source_ref", metadata.get("source_ref"))
        seal = _seal_mapping(payload.get("seal"))
        payload.update(
            {
                "artifact_id": artifact_id,
                "producer": _ARTIFACT_PRODUCER,
                "purpose": self._artifact_purpose(metadata),
                "kind": _ARTIFACT_KIND,
                "lifecycle_state": self._artifact_state(state),
                "artifact_state": self._artifact_state(state),
                "identity": list(identity),
                "source_ref": source_ref,
                "disposable": True,
                "path_size": max(0, int(path_metadata.st_size)),
                "path_mtime": max(0, int(path_metadata.st_mtime_ns)),
                "path_size_bytes": max(0, int(path_metadata.st_size)),
                "path_mtime_ns": max(0, int(path_metadata.st_mtime_ns)),
                "retention": {
                    "retain_on_success": retain_on_success,
                    "retain_until_ns": retire_after_ns,
                },
                "seal": seal,
            }
        )

    def _artifact_payload(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Project a scratch manifest into the registry's loose contract."""

        record_id = payload.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ScratchSecurityError("scratch artifact projection has no record id")
        artifact_id = payload.get("artifact_id", self._artifact_id(record_id))
        if (
            not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or len(artifact_id.encode("utf-8")) > 256
        ):
            raise ScratchSecurityError("scratch artifact id is invalid")
        identity_raw = payload.get("path_identity")
        if (
            not isinstance(identity_raw, (list, tuple))
            or len(identity_raw) != 3
            or any(type(value) is not int for value in identity_raw)
        ):
            raise ScratchSecurityError("scratch artifact identity is invalid")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ScratchSecurityError("scratch artifact metadata is invalid")
        selected_state = payload.get("state") if state is None else state
        if not isinstance(selected_state, str) or not selected_state:
            raise ScratchSecurityError("scratch artifact lifecycle state is invalid")
        artifact_state = self._artifact_state(selected_state)
        retain_on_success = payload.get("retain_on_success", False)
        if type(retain_on_success) is not bool:
            raise ScratchSecurityError("scratch artifact retention flag is invalid")
        retain_until_ns = payload.get("retire_after_ns")
        if retain_until_ns is not None and (
            type(retain_until_ns) is not int or retain_until_ns < 0
        ):
            raise ScratchSecurityError("scratch artifact retention time is invalid")
        identity = tuple(identity_raw)
        source_ref = payload.get("source_ref", metadata.get("source_ref"))
        purpose = payload.get("purpose", self._artifact_purpose(metadata))
        if not isinstance(purpose, str) or not purpose.strip():
            purpose = self._artifact_purpose(metadata)
        retention = payload.get(
            "retention",
            {
                "retain_on_success": retain_on_success,
                "retain_until_ns": retain_until_ns,
            },
        )
        seal = _seal_mapping(payload.get("seal"))
        registry_metadata = dict(metadata)
        registry_metadata["scratch_payload_policy"] = {
            "schema": "neocortex.scratch-payload-policy/v1",
            "payload_profile": payload.get("payload_profile", "strict"),
            "policy_revision": payload.get("policy_revision", "strict/v1"),
            "fixture_grant": payload.get("fixture_grant"),
        }
        registry_metadata["posix_path_identity"] = PathIdentity.from_path(path).as_dict()
        if seal is not None:
            # Keep the seal in the canonical registry projection as well as
            # the scratch manifest so a later tombstone/recovery reader does
            # not need to parse an opaque lifecycle note.
            registry_metadata.setdefault("scratch_seal", seal)
        registry_root = self.root
        configured_registry = self._artifact_registry
        if configured_registry is not None:
            candidate_root = getattr(configured_registry, "root", None)
            if candidate_root is not None:
                try:
                    registry_root = Path(candidate_root)
                except (TypeError, ValueError) as exc:
                    raise ScratchSecurityError("artifact registry root is invalid") from exc
        try:
            path_metadata = path.lstat()
        except FileNotFoundError:
            if artifact_state != "retired":
                raise ScratchSecurityError("scratch artifact path is unavailable") from None
            # Retirement is published after the physical effect.  The
            # registry accepts this terminal transition using its last
            # manifest observations; a missing path is never treated as a
            # fresh registration or as a successful pre-effect claim.
            path_size_bytes = max(0, int(payload.get("path_size_bytes", 0)))
            path_mtime_ns = max(0, int(payload.get("path_mtime_ns", 0)))
        except OSError as exc:
            raise ScratchSecurityError("scratch artifact path is unavailable") from exc
        else:
            if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode):
                raise ScratchSecurityError("scratch artifact path is not a directory")
            path_size_bytes = max(0, int(path_metadata.st_size))
            path_mtime_ns = max(0, int(path_metadata.st_mtime_ns))
        return {
            "artifact_id": artifact_id,
            "artifact": artifact_id,
            # Some staged registry adapters still spell their key
            # ``record_id``.  It identifies the same registry artifact, while
            # ``scratch_record_id`` preserves the runtime manifest id.
            "record_id": artifact_id,
            "scratch_record_id": record_id,
            "owner": payload.get("owner", self.owner),
            "producer": _ARTIFACT_PRODUCER,
            "run_id": payload.get("run_id"),
            "purpose": purpose,
            "metadata": registry_metadata,
            "path": path,
            "root": registry_root,
            "kind": _ARTIFACT_KIND,
            "state": artifact_state,
            "lifecycle_state": artifact_state,
            "scratch_state": selected_state,
            "path_identity": identity,
            "identity": identity,
            "source_ref": source_ref,
            "digest": payload.get("manifest_digest"),
            "manifest_digest": payload.get("manifest_digest"),
            "dependencies": metadata.get("dependencies", ()),
            "disposable": True,
            "retain_on_success": retain_on_success,
            "retain_until_ns": retain_until_ns,
            "retention": retention,
            "path_size": path_size_bytes,
            "path_mtime": path_mtime_ns,
            "path_size_bytes": path_size_bytes,
            "path_mtime_ns": path_mtime_ns,
        }

    @staticmethod
    def _preserve_workspace_observation(path: Path) -> tuple[tuple[int, int, int], int, int]:
        """Capture the directory observation used by the artifact registry."""

        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ScratchSecurityError("scratch artifact path is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("scratch artifact path is not a directory")
        return _identity(metadata), int(metadata.st_atime_ns), int(metadata.st_mtime_ns)

    @staticmethod
    def _restore_workspace_observation(
        path: Path,
        observation: tuple[tuple[int, int, int], int, int],
    ) -> None:
        """Restore only a still-identical workspace directory's timestamps."""

        expected_identity, atime_ns, mtime_ns = observation
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ScratchSecurityError("scratch workspace changed while publishing") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or _identity(metadata) != expected_identity
        ):
            raise ScratchSecurityError("scratch workspace identity changed while publishing")
        try:
            os.utime(path, ns=(atime_ns, mtime_ns), follow_symlinks=False)
        except OSError as exc:
            raise ScratchSecurityError(
                f"scratch workspace timestamps could not be preserved: {exc}"
            ) from exc

    @staticmethod
    def _registry_method(registry: Any, names: Sequence[str]) -> Any | None:
        for name in names:
            method = getattr(registry, name, None)
            if callable(method):
                return method
        return None

    @staticmethod
    def _raise_artifact_failure(operation: str, error: BaseException) -> None:
        if isinstance(error, ScratchSecurityError):
            raise error
        raise ScratchSecurityError(
            f"artifact registry {operation} failed: {type(error).__name__}: {error}"
        ) from error

    def _register_artifact(self, path: Path, payload: Mapping[str, Any]) -> None:
        registry = self._artifact_registry_instance()
        if registry is None:
            return
        try:
            method = self._registry_method(
                registry,
                ("register", "register_artifact", "register_temporary", "record"),
            )
            if method is None:
                raise TypeError("configured artifact registry has no register hook")
            result = _invoke_artifact_callable(
                method,
                self._artifact_payload(path, payload),
                operation="register",
            )
            if result is False:
                raise RuntimeError("registry rejected the artifact registration")
        except BaseException as exc:
            self._raise_artifact_failure("register", exc)

    def _update_artifact(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        state: str | None = None,
    ) -> None:
        registry = self._artifact_registry_instance()
        if registry is None:
            return
        try:
            fields = self._artifact_payload(path, payload, state=state)
            method = self._registry_method(
                registry,
                ("update", "update_artifact", "transition", "touch"),
            )
            if method is None:
                # A minimal registry may expose only an idempotent register
                # helper.  Re-registering the same artifact id is the only
                # safe compatibility fallback; failures remain fail-closed.
                method = self._registry_method(
                    registry,
                    ("register", "register_artifact", "register_temporary", "record"),
                )
            if method is None:
                raise TypeError("configured artifact registry has no update hook")
            result = _invoke_artifact_callable(method, fields, operation="update")
            if result is False:
                raise RuntimeError("registry rejected the artifact update")
        except BaseException as exc:
            self._raise_artifact_failure("update", exc)

    def create(
        self, *, run_id: int | str | None = None, retain_on_success: bool = False,
        metadata: Mapping[str, Any] | None = None,
        payload_profile: PayloadProfile | str = PayloadProfile.STRICT,
        fixture_grant: FixturePayloadGrant | None = None,
    ) -> ScratchWorkspace:
        if not self._ensure_root(create=self.create_root):
            raise ScratchRootError("scratch root is absent")
        with self._scratch_lock():
            return self._create(run_id=run_id, retain_on_success=retain_on_success,
                                metadata=metadata, payload_profile=payload_profile,
                                fixture_grant=fixture_grant)

    def _create(
        self,
        *,
        run_id: int | str | None = None,
        retain_on_success: bool = False,
        metadata: Mapping[str, Any] | None = None,
        payload_profile: PayloadProfile | str = PayloadProfile.STRICT,
        fixture_grant: FixturePayloadGrant | None = None,
    ) -> ScratchWorkspace:
        from .scratch_lifecycle import create
        return create(
            self, run_id=run_id, retain_on_success=retain_on_success, metadata=metadata,
            payload_profile=payload_profile, fixture_grant=fixture_grant,
            workspace_factory=ScratchWorkspace,
        )

    # Compatibility spelling for producer adapters that prefer an explicit
    # noun.  It is deliberately just an alias, not another implementation.
    create_workspace = create

    def records(self) -> tuple[ScratchRecord, ...]:
        if not self._ensure_root(create=False):
            return ()
        return tuple(self._scan_records(now_ns=time.time_ns()))

    def plan(
        self,
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
        if scan_max_entries is not None:
            max_entries = scan_max_entries
        if scan_max_depth is not None:
            max_depth = scan_max_depth
        if scan_max_bytes is not None:
            max_bytes = scan_max_bytes
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
        if not self._ensure_root(create=False):
            return ScratchPlan(
                self.root,
                reason="scratch root is absent",
                root_blocked="scratch root is absent",
                max_entries=max_entries,
                max_depth=max_depth,
                max_bytes=max_bytes,
            )
        # Keep the public module-level hard bound patchable for embedders and
        # tests while the bounded observer lives in ``scratch_contracts``.
        budget = _ScratchScanBudget(
            _MAX_RECORDS if max_entries is None else max_entries,
            max_depth,
            max_bytes,
            max_fds=max_fds,
        )
        records = tuple(self._scan_records(now_ns=now, budget=budget))
        if not budget.truncated:
            records = self._complete_seal_observations(records, budget)
        if budget.truncated:
            records = tuple(
                replace(
                    record,
                    eligible=False,
                    issue=record.issue or "scan_incomplete",
                    reason=record.reason or "scan_incomplete",
                )
                for record in records
            )
        return self._summarize(
            records,
            read_only=True,
            unmanaged=self._unmanaged_entries(limit=max_entries),
            truncated=budget.truncated,
            truncation_reasons=tuple(budget.truncation_reasons),
            max_entries=max_entries,
            max_depth=max_depth,
            max_bytes=max_bytes,
            max_fds=max_fds, observed_members=budget.payload_entries, observed_bytes=budget.observed_bytes,
        )

    @staticmethod
    def _selection_claim(record: ScratchRecord) -> tuple[Any, ...]:
        return (record.record_id, os.fsencode(record.path), record.owner,
                record.path_identity, record.root_identity, record.manifest_digest,
                record.status, record.size_bytes, record.size_complete,
                record.payload_profile, record.policy_revision,
                _canonical_json(record.seal), _canonical_json(record.fixture_grant))

    def verify(self, plan: ScratchPlan) -> ScratchPlan:
        """Verify exactly the supplied selection, without adding new candidates."""
        if not isinstance(plan, ScratchPlan) or plan.root != self.root:
            raise ScratchSecurityError("scratch plan belongs to a different root")
        current = self.plan(max_entries=plan.max_entries, max_depth=plan.max_depth,
                            max_bytes=plan.max_bytes, max_fds=plan.max_fds)
        by_id = {record.record_id: record for record in current.records}
        selected = []
        for prior in plan.records:
            fresh = by_id.get(prior.record_id)
            if (not plan.complete or not current.complete or fresh is None
                    or self._selection_claim(prior) != self._selection_claim(fresh)):
                selected.append(replace(prior, eligible=False, issue="selection_changed",
                                        reason="scratch selection changed after planning"))
            else:
                selected.append(replace(fresh, eligible=prior.eligible and fresh.eligible))
        return self._summarize(tuple(selected), read_only=True,
                               truncated=current.truncated, truncation_reasons=current.truncation_reasons,
                               max_entries=plan.max_entries, max_depth=plan.max_depth,
                               max_bytes=plan.max_bytes, max_fds=plan.max_fds)

    @_scratch_write_locked
    def apply(
        self,
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
        from .scratch_lifecycle import apply
        return apply(
            self, plan=plan, now_ns=now_ns, max_entries=max_entries, max_depth=max_depth,
            max_bytes=max_bytes, max_fds=max_fds, scan_max_entries=scan_max_entries,
            scan_max_depth=scan_max_depth, scan_max_bytes=scan_max_bytes,
        )

    def _unmanaged_entries(self, *, limit: int | None = None) -> tuple[Path, ...]:
        """List suspicious neighbours without treating them as candidates."""

        if limit == 0:
            return ()

        try:
            iterator = os.scandir(self.root)
        except OSError:
            return ()
        values: list[Path] = []
        try:
            for entry in iterator:
                if entry.name.startswith(("neocortex-", "scratch-")) and not entry.name.startswith("workspace-"):
                    values.append(self.root / entry.name)
                    if limit is not None and len(values) >= max(0, limit):
                        break
        finally:
            iterator.close()
        return tuple(values)

    def _complete_seal_observations(
        self, records: Sequence[ScratchRecord], budget: _ScratchScanBudget,
    ) -> tuple[ScratchRecord, ...]:
        """Verify seals within a shared hashing budget across all workspaces."""
        resolved: list[ScratchRecord] = []
        for record in records:
            if record.issue != "seal_observation_incomplete":
                resolved.append(record)
                continue
            remaining_entries = _MAX_RECORDS - budget.hashed_entries if budget.max_entries is None else max(
                0, budget.max_entries - budget.hashed_entries)
            remaining_bytes = _MAX_SCAN_BYTES - budget.hashed_bytes if budget.max_bytes is None else max(
                0, budget.max_bytes - budget.hashed_bytes)
            try:
                digest, members, apparent = _sealed_workspace_digest(
                    record.path, profile=record.payload_profile,
                    max_entries=remaining_entries, max_bytes=remaining_bytes,
                    max_depth=budget.max_depth if budget.max_depth is not None else 2048,
                    max_fds=budget.max_fds,
                )
            except ScratchSecurityError as exc:
                issue = "workspace_seal_unavailable"
                if "entry limit" in str(exc):
                    budget.note(ENTRY_LIMIT)
                elif "byte limit" in str(exc):
                    budget.note(BYTE_LIMIT)
                resolved.append(replace(record, eligible=False, issue=issue, reason=issue))
                continue
            budget.hashed_entries += members
            budget.hashed_bytes += apparent
            matches = record.seal is not None and (digest, members, apparent) == (
                record.seal["digest"], record.seal["members"], record.seal["apparent_bytes"])
            eligible = (matches and record.owner == self.owner and record.state == ScratchState.COMPLETED
                        and (record.retire_after_ns is None or record.retire_after_ns <= time.time_ns()))
            resolved.append(replace(record, eligible=eligible,
                                    issue=None if matches else "workspace_seal_drift",
                                    reason=None if matches else "workspace_seal_drift"))
        return tuple(resolved)

    def _scan_records(
        self,
        *,
        now_ns: int,
        budget: _ScratchScanBudget | None = None,
    ) -> Iterable[ScratchRecord]:
        effective_budget = budget if budget is not None else _ScratchScanBudget(_MAX_RECORDS, 2048, _MAX_SCAN_BYTES)
        try:
            iterator = os.scandir(self.root)
        except OSError as exc:
            raise ScratchSecurityError(f"cannot scan scratch root: {exc}") from exc
        entries: list[os.DirEntry[str]] = []
        try:
            if budget is not None and budget.max_entries is not None:
                while len(entries) <= budget.max_entries:
                    try:
                        candidate = next(iterator)
                        if candidate.name == _CONTROL_DIRECTORY:
                            continue
                        entries.append(candidate)
                    except StopIteration:
                        break
                if len(entries) > budget.max_entries:
                    budget.note(ENTRY_LIMIT)
                    entries = entries[: budget.max_entries]
            else:
                for entry in iterator:
                    if entry.name == _CONTROL_DIRECTORY:
                        continue
                    entries.append(entry)
                    if len(entries) > _MAX_RECORDS:
                        raise ScratchSecurityError("scratch root exceeds the registered-record limit")
        finally:
            iterator.close()
        if len(entries) > _MAX_RECORDS:
            raise ScratchSecurityError("scratch root exceeds the registered-record limit")
        ordered_entries = sorted(entries, key=lambda item: item.name)
        for entry in ordered_entries:
            if budget is not None:
                budget.entries += 1
            path = self.root / entry.name
            try:
                metadata = path.lstat()
            except OSError as exc:
                yield self._invalid_record(path, f"workspace disappeared: {exc}")
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                # Unregistered neighbours are never candidates.  Surface a
                # bounded blocked row only for a manifest-shaped entry; this
                # avoids treating arbitrary files in the private root as ours.
                if entry.name.startswith("workspace-"):
                    yield self._invalid_record(path, "workspace claim is not a directory")
                continue
            manifest_path = path / MANIFEST_NAME
            try:
                manifest_exists = manifest_path.lstat()
            except FileNotFoundError:
                if entry.name.startswith("workspace-"):
                    yield self._invalid_record(path, "workspace manifest is absent")
                continue
            except OSError as exc:
                yield self._invalid_record(path, f"workspace manifest is unavailable: {exc}")
                continue
            try:
                if stat.S_ISLNK(manifest_exists.st_mode) or not stat.S_ISREG(
                    manifest_exists.st_mode
                ):
                    raise ScratchManifestError("scratch manifest is not a regular file")
                if (
                    manifest_exists.st_uid != os.geteuid()
                    or manifest_exists.st_mode & 0o077
                    or manifest_exists.st_nlink != 1
                ):
                    raise ScratchManifestError("scratch manifest protection drifted")
                raw = _read_manifest_bytes(manifest_path)
                if len(raw) > _MAX_MANIFEST_BYTES:
                    raise ScratchSecurityError("scratch manifest is too large")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise ScratchSecurityError("scratch manifest is not an object")
                if payload.get("manifest_digest") != _manifest_digest(payload):
                    raise ScratchManifestError("scratch manifest digest mismatch")
                profile = self._payload_profile(path, payload)
                observation = _bounded_payload_observation(
                    path, effective_budget,
                    profile=profile,
                )
                record = self._record_from_payload(
                    path,
                    payload,
                    size_bytes=observation.size_bytes,
                    now_ns=now_ns,
                    observation_issue=observation.issue,
                    size_complete=observation.size_complete,
                    bounded_observation=budget is not None, payload_observed=True,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ScratchError, TypeError, ValueError) as exc:
                yield self._invalid_record(
                    path,
                    f"invalid scratch manifest: {type(exc).__name__}: {exc}",
                    issue=(
                        "workspace_mode_drift"
                        if "workspace mode" in str(exc)
                        else "manifest_invalid"
                    ),
                )
                continue
            yield record

    def _invalid_record(
        self,
        path: Path,
        reason: str,
        *,
        issue: str = "manifest_invalid",
    ) -> ScratchRecord:
        return ScratchRecord(
            record_id=f"invalid:{path.name}",
            owner="",
            run_id=None,
            path=path,
            state=ScratchState.RECOVERY_REQUIRED,
            created_ns=0,
            updated_ns=0,
            path_identity=None,
            # Invalid manifests do not authorize an unbounded payload walk.
            size_bytes=0,
            size_complete=False,
            retain_on_success=True,
            retire_after_ns=None,
            reason=_bounded_text(reason, label="scratch reason"),
            eligible=False,
            valid=False,
            issue=issue,
            root_identity=None,
        )

    def _record_from_payload(
        self,
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
        from .scratch_manifest import record_from_payload
        return record_from_payload(
            self, path, payload, size_bytes=size_bytes, now_ns=now_ns,
            observation_issue=observation_issue, bounded_observation=bounded_observation,
            payload_observed=payload_observed, size_complete=size_complete,
        )

    def _summarize(
        self,
        records: Sequence[ScratchRecord],
        *,
        read_only: bool,
        unmanaged: tuple[Path, ...] = (),
        truncated: bool = False,
        truncation_reasons: Sequence[str] = (),
        max_entries: int | None = None,
        max_depth: int | None = None,
        max_bytes: int | None = None,
        max_fds: int = 2048, observed_members: int = 0, observed_bytes: int = 0,
    ) -> ScratchPlan:
        planned = tuple(record for record in records if record.eligible)
        blocked = tuple(
            record
            for record in records
            if (
                record.issue is not None
                and record.state != ScratchState.RECOVERY_REQUIRED
            )
            or record.state == "blocked"
        )
        failed = tuple(
            record
            for record in records
            if record.state in {"failed", ScratchState.FAILED_RETAINED}
            and record not in blocked
        )
        recovery = tuple(
            record
            for record in records
            if record.state == ScratchState.RECOVERY_REQUIRED
        )
        kept = tuple(
            record
            for record in records
            if not record.eligible
            and record not in blocked
            and record not in failed
            and record not in recovery
        )
        if recovery:
            status = "recovery_required"
        elif failed:
            status = "failed"
        elif blocked:
            status = "blocked"
        elif truncated:
            status = "blocked"
        elif planned:
            status = "planned"
        else:
            status = "kept" if kept else "planned"
        return ScratchPlan(
            root=self.root,
            records=tuple(records),
            planned=len(planned),
            kept=len(kept),
            blocked=len(blocked),
            failed=len(failed),
            recovery_required=len(recovery),
            planned_bytes=sum(record.size_bytes for record in planned),
            kept_bytes=sum(record.size_bytes for record in kept),
            blocked_bytes=sum(record.size_bytes for record in blocked),
            failed_bytes=sum(record.size_bytes for record in failed),
            recovery_required_bytes=sum(record.size_bytes for record in recovery),
            status=status,
            reason=(
                next(iter(truncation_reasons))
                if truncated and truncation_reasons
                else None
            ),
            read_only=read_only,
            unmanaged=unmanaged,
            truncated=truncated,
            truncation_reasons=tuple(dict.fromkeys(truncation_reasons)),
            max_entries=max_entries,
            max_depth=max_depth,
            max_bytes=max_bytes,
            max_fds=max_fds, observed_members=observed_members, observed_bytes=observed_bytes,
        )

    def _record_for_path(self, path: Path) -> ScratchRecord | None:
        """Load one known workspace without rediscovering its siblings.

        Retirement used to call ``_scan_records`` for every candidate.  That
        made an apply of N workspaces perform an N² scratch-manifest walk.  A
        caller already holds the claimed path and identity from the bounded
        observation, so a direct manifest read is both cheaper and narrower;
        the manifest/root/payload identity checks remain in
        ``_record_from_payload``.
        """

        if not _path_is_within(path, self.root):
            return None
        manifest_path = path / MANIFEST_NAME
        try:
            metadata = manifest_path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                return None
            raw = _read_manifest_bytes(manifest_path)
            if len(raw) > _MAX_MANIFEST_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, Mapping):
                return None
            if payload.get("manifest_digest") != _manifest_digest(payload):
                raise ScratchManifestError("scratch manifest digest mismatch")
            profile = self._payload_profile(path, payload)
            observation = _bounded_payload_observation(path, _ScratchScanBudget(None, None, None), profile=profile)
            size = observation.size_bytes
            return self._record_from_payload(
                path,
                payload,
                size_bytes=size,
                size_complete=observation.size_complete,
                observation_issue=observation.issue, payload_observed=True,
                now_ns=time.time_ns(),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ScratchError, TypeError, ValueError) as exc:
            # Preserve the invalid row for callers such as ScratchWorkspace.state;
            # collapsing it to ``None`` would change a durable manifest
            # corruption into an unrelated "claim disappeared" error.
            try:
                return self._invalid_record(
                    path,
                    f"invalid scratch manifest: {type(exc).__name__}: {exc}",
                )
            except BaseException:
                return None

    @staticmethod
    def _workspace_dependencies(record: ScratchRecord) -> tuple[str, ...] | None:
        """Return a normalized live-dependency claim or ``None`` if unknown."""

        value = record.metadata.get("dependencies")
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            return None
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return None
            result.append(item)
        return tuple(sorted(set(result)))

    def _workspace_dependency_observation_complete(
        self,
        records: Sequence[ScratchRecord],
    ) -> bool:
        """Whether this private scratch root has a complete consumer view."""

        # Invalid/missing manifests are possible consumers whose dependency
        # claims cannot be read.  The conservative boundary is this one
        # registered scratch root, not the rest of the filesystem.
        for record in records:
            if not record.valid:
                return False
            if self._workspace_dependencies(record) is None:
                return False
        return True

    def _live_workspace_dependents(
        self,
        target: ScratchRecord,
        records: Sequence[ScratchRecord],
        *,
        registry_records: Sequence[Any] | None = None,
    ) -> tuple[str, ...]:
        target_id = target.artifact_id or self._artifact_id(target.record_id)
        registry_by_id = {
            getattr(item, "artifact_id", None): item
            for item in (registry_records or ())
            if getattr(item, "artifact_id", None)
        }
        dependents: list[str] = []
        for record in records:
            if record.record_id == target.record_id:
                continue
            registry_record = registry_by_id.get(record.artifact_id)
            if registry_record is not None:
                # The registry projection is the live authority.  The
                # scratch metadata remains provenance and a fallback only
                # when its registry projection is missing (for example while
                # diagnosing a damaged/legacy owner).
                if not getattr(registry_record, "valid", False):
                    dependencies = None
                else:
                    dependencies = tuple(getattr(registry_record, "dependencies", ()))
            else:
                dependencies = self._workspace_dependencies(record)
            if dependencies is None or target_id not in dependencies:
                continue
            if (
                not record.valid
                or record.state
                in {
                    ScratchState.ACTIVE,
                    ScratchState.FAILED_RETAINED,
                    ScratchState.RECOVERY_REQUIRED,
                }
            ):
                dependents.append(record.record_id)
        return tuple(sorted(set(dependents)))

    def _validate_result_paths(
        self,
        workspace: Path,
        result_paths: Iterable[Path | str],
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in result_paths:
            path = Path(raw)
            if not path.is_absolute():
                raise ScratchSecurityError("scratch result paths must be absolute")
            resolved = path.resolve(strict=False)
            if not _path_is_within(resolved, workspace.resolve(strict=True)):
                raise ScratchSecurityError("scratch result must remain inside the workspace")
            if not path.exists():
                raise ScratchSecurityError(f"scratch result does not exist: {path}")
            normalized.append(str(path))
        return tuple(normalized)

    def seal_workspace(
        self,
        record_id: str,
        *,
        seal: Mapping[str, Any] | None = None,
    ) -> ScratchRecord:
        """Persist a stable member/identity/content seal for one workspace.

        This is the safe producer-facing form used by external activity
        adapters.  The caller may supply a previously computed seal, but the
        manager always recomputes it under the owner lock and rejects a
        mismatch.  A later plan/apply recomputes the same claim, so equal-size
        replacements and edits cannot be retired silently.
        """

        if self.owner is None:
            raise ScratchSecurityError(
                "federated scratch view is read-only for sealing"
            )
        record_id = _bounded_text(record_id, label="scratch record id", limit=128)
        with self._scratch_lock():
            record = next(
                (item for item in self._scan_records(now_ns=time.time_ns()) if item.record_id == record_id),
                None,
            )
            if record is None or record.owner != self.owner:
                raise ScratchSecurityError("scratch workspace does not belong to this owner")
            if record.state not in {
                ScratchState.ACTIVE,
                ScratchState.COMMITTING,
                ScratchState.FAILED_RETAINED,
                ScratchState.RECOVERY_REQUIRED,
            }:
                raise ScratchError("workspace is not sealable in its current state")
            observed_digest, observed_members, observed_bytes = _sealed_workspace_digest(record.path, profile=record.payload_profile)
            observed = _seal_mapping(
                {
                    "schema": _SEAL_SCHEMA,
                    "digest": observed_digest,
                    "members": observed_members,
                    "apparent_bytes": observed_bytes,
                }
            )
            if observed is None:  # pragma: no cover - mapping is constructed above
                raise ScratchSecurityError("scratch seal could not be constructed")
            if seal is not None and _seal_mapping(seal) != observed:
                raise ScratchSecurityError("scratch seal changed before publication")
            return self._update_state(
                record.path,
                record.record_id,
                ScratchState(record.state),
                seal=observed,
            )

    # Short producer-facing alias; it is intentionally the same implementation
    # and does not introduce a second lifecycle protocol.
    seal = seal_workspace

    def reconcile_workspace_seal(
        self, record_id: str, *, prior_digest: str, release_authorized: bool,
        evidence: Mapping[str, Any],
    ) -> ScratchRecord:
        """Explicitly reconcile an inactive legacy/mismatched seal, preserving it.

        A mismatch never triggers this operation automatically. Publication
        remnants continue to participate in the payload; no basename-based
        exclusion or grant is invented during reconciliation.
        """
        if (self.owner is None or release_authorized is not True
                or not isinstance(evidence, Mapping)
                or evidence.get("quiescence_confirmed") is not True
                or evidence.get("deliverables_confirmed") is not True):
            raise ScratchSecurityError("seal reconciliation requires owner release and quiescence/deliverable evidence")
        with self._scratch_lock():
            records = tuple(self._scan_records(now_ns=time.time_ns()))
            record = next((item for item in records if item.record_id == record_id), None)
            if (record is None or not record.valid or record.owner != self.owner
                    or record.state not in {ScratchState.COMPLETED, ScratchState.FAILED_RETAINED,
                                            ScratchState.RECOVERY_REQUIRED}
                    or record.seal is None or record.seal["digest"] != prior_digest):
                raise ScratchSecurityError("prior inactive workspace seal is not verified")
            if (not self._workspace_dependency_observation_complete(records)
                    or self._live_workspace_dependents(record, records)):
                raise ScratchSecurityError("seal reconciliation dependency observation is incomplete or live")
            registry = self._artifact_registry_instance() if self._artifact_registry_configured else None
            registry_lock = getattr(registry, "_registry_lock", None) if registry is not None else None
            registry_context = registry_lock() if callable(registry_lock) else nullcontext()
            with cast(Any, registry_context):
                if registry is not None:
                    snapshot = tuple(registry.records())
                    if (any(not getattr(item, "valid", False) for item in snapshot)
                            or self._live_workspace_dependents(record, records, registry_records=snapshot)):
                        raise ScratchSecurityError("seal reconciliation registry dependencies are uncertain or live")
                digest, members, apparent = _sealed_workspace_digest(record.path, profile=record.payload_profile)
                next_seal = {"schema": _SEAL_SCHEMA, "digest": digest,
                             "members": members, "apparent_bytes": apparent}
                metadata = dict(record.metadata)
                prior = metadata.get("seal_reconciliation", [])
                if not isinstance(prior, list) or len(prior) >= 16:
                    raise ScratchSecurityError("seal reconciliation history requires owner archival")
                metadata["seal_reconciliation"] = [*prior, {
                    "schema": "neocortex.scratch-seal-reconciliation/v1",
                    "prior_seal": dict(record.seal), "next_seal": next_seal,
                    "authorized_ns": time.time_ns(), "evidence": dict(evidence),
                }]
                return self._update_state(record.path, record_id, ScratchState(record.state),
                                          retain_on_success=True, metadata=metadata, seal=next_seal)

    def reconcile_terminal(
        self,
        record_id: str,
        *,
        release_authorized: bool,
        evidence: Mapping[str, Any] | None = None,
        now_ns: int | None = None,
    ) -> ScratchRecord:
        from .scratch_lifecycle import reconcile_terminal
        return reconcile_terminal(
            self, record_id, release_authorized=release_authorized,
            evidence=evidence, now_ns=now_ns,
        )

    reconcile_failed = reconcile_terminal

    @_scratch_write_locked
    def _update_state(
        self,
        path: Path,
        record_id: str,
        state: ScratchState,
        *,
        retain_on_success: bool | None = None,
        result_paths: Sequence[str] | None = None,
        retire_after_ns: int | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        seal: Mapping[str, Any] | None = None,
    ) -> ScratchRecord:
        if self.owner is None:
            raise ScratchSecurityError(
                "federated scratch view is read-only for lifecycle updates"
            )
        record = self._record_for_path(path)
        if record is None or record.record_id != record_id or record.owner != self.owner:
            raise ScratchSecurityError("scratch manifest no longer matches its owner")
        manifest_path = path / MANIFEST_NAME
        manifest_metadata = manifest_path.lstat()
        if stat.S_ISLNK(manifest_metadata.st_mode) or not stat.S_ISREG(
            manifest_metadata.st_mode
        ):
            raise ScratchManifestError("scratch manifest is not a regular file")
        if manifest_metadata.st_uid != os.geteuid() or manifest_metadata.st_mode & 0o077:
            raise ScratchManifestError("scratch manifest protection drifted")
        raw = _read_manifest_bytes(manifest_path)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping) or payload.get("manifest_digest") != record.manifest_digest:
            raise ScratchSecurityError("scratch manifest changed during update")
        updated = dict(payload)
        updated["state"] = state.value
        updated["updated_ns"] = time.time_ns()
        if retain_on_success is not None:
            updated["retain_on_success"] = bool(retain_on_success)
        if result_paths is not None:
            updated["result_paths"] = list(result_paths)
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise ScratchSecurityError("scratch metadata is not an object")
            if len(_canonical_json(metadata).encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ScratchSecurityError("scratch metadata exceeds the durable limit")
            updated["metadata"] = dict(metadata)
        if state is ScratchState.COMPLETED:
            updated["retire_after_ns"] = retire_after_ns
            # Persist one bounded lifecycle observation in the workspace
            # manifest.  A later payload change is then a recovery boundary,
            # not silently disposable material.
            # ``_record_for_path`` has already observed the same pre-effect
            # payload under the owner lock.  Reuse only that accounting value;
            # the post-effect observation below remains independent and is the
            # one that validates the returned record.
            if not record.size_complete:
                raise ScratchSecurityError(
                    f"scratch size observation is incomplete: {record.issue}"
                )
            updated["payload_size_bytes"] = record.size_bytes
        if reason is not None:
            updated["reason"] = _bounded_text(reason, label="scratch reason")
        promoted_seal = _seal_mapping(seal) if seal is not None else _seal_from_lifecycle_reason(updated.get("reason"))
        if promoted_seal is not None:
            updated["seal"] = promoted_seal
        if self._artifact_registry_configured:
            self._add_artifact_manifest_fields(updated)
        updated["manifest_digest"] = _manifest_digest(updated)
        preserved_observation = (
            self._preserve_workspace_observation(path)
            if self._artifact_registry_configured
            else None
        )
        if self._artifact_registry_configured:
            # Update the registry before publishing the new scratch manifest.
            # A registry failure therefore leaves the previous scratch state
            # intact and prevents a lifecycle transition from being reported
            # as successful.
            self._update_artifact(path, updated)
        _write_json_atomic(manifest_path, updated)
        if preserved_observation is not None:
            self._restore_workspace_observation(path, preserved_observation)
        # Keep one independent physical observation after the manifest and
        # optional registry publication.  Feed that observation into the
        # record parser instead of making it walk the same tree again through
        # ``_workspace_payload_issue``; this is not a cache across lifecycle
        # boundaries and does not replace the pre-effect observation above.
        final_observation = _bounded_payload_observation(
            path,
            _ScratchScanBudget(None, None, None),
            profile=record.payload_profile,
        )
        if not final_observation.size_complete:
            raise ScratchSecurityError(
                f"scratch size observation is incomplete: {final_observation.issue}"
            )
        return self._record_from_payload(
            path,
            updated,
            size_bytes=final_observation.size_bytes,
            observation_issue=final_observation.issue,
            payload_observed=True,
            size_complete=final_observation.size_complete,
        )

    @_scratch_write_locked
    def _retire_record(
        self,
        record: ScratchRecord,
        *,
        workspace_records: Sequence[ScratchRecord] | None = None,
        retirement_session: Any | None = None,
    ) -> None:
        from .scratch_lifecycle import retire_record
        return retire_record(
            self, record, workspace_records=workspace_records,
            retirement_session=retirement_session,
        )


__all__ = [
    "MANIFEST_NAME",
    "SCRATCH_SCHEMA",
    "FixturePayloadGrant",
    "PayloadProfile",
    "ScratchError",
    "ScratchManager",
    "ScratchManifestError",
    "ScratchPlan",
    "ScratchRecord",
    "ScratchRootError",
    "ScratchSecurityError",
    "ScratchState",
    "ScratchWorkspace",
    "verified_workspace_payload_profile",
    "workspace_payload_digest",
]
