"""Durable, discoverable reset intent and cross-process reconciliation.

The immutable intent is registered before the rollback directory is acquired.
Its registry metadata binds every later receipt and the directory identity.
Reconciliation never treats a pathname or an incomplete copy as authority to
overwrite a source that has changed since the recorded operation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neocortex.runtime.artifact_registry import ArtifactRecord, ArtifactRegistry, MAX_METADATA_BYTES

RESET_OPERATION_PURPOSE = "state-reset-operation"
RESET_OPERATION_SCHEMA = "neocortex.state-reset-operation/v1"
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024
ACTIVE_OPERATION_HANDLE: ContextVar[Any] = ContextVar("state_reset_operation_handle", default=None)
RESET_RECOVERY_METADATA_LIMIT = MAX_METADATA_BYTES


def _operation_metadata(storage: Path, *, transient: bool, phase: str, identity: Any,
                        promotions: dict[str, Any], temporaries: dict[str, Any],
                        receipt: Path | None = None, receipt_digest: str | None = None) -> dict[str, Any]:
    payload = {"phase": phase, "storage": str(storage), "transient": transient,
               "storage_identity": identity, "promotions": promotions,
               "restore_temporaries": temporaries}
    if receipt is not None:
        payload.update(receipt=str(receipt), receipt_digest=receipt_digest)
    return payload


def recovery_metadata_bound(state: Path, entries: tuple[Any, ...], storage: Path | None = None) -> int:
    """Reserve the exact JSON envelope with worst-case Linux identity widths.

    Every selected path may need a recorded restoration. At most one named
    temporary can coexist: recovery reconciles the previous temporary first.
    No count-only approximation or post-effect metadata growth grants safety.
    """
    largest_identity = (1 << 64) - 1
    identity = [largest_identity, largest_identity]
    promotions: dict[str, Any] = {}
    largest_temporary: dict[str, Any] = {}
    temporary_size = 0
    for entry in entries:
        promotion = {"owner": "raw-rollback", "identity": identity, "size": largest_identity}
        if entry.kind == "directory":
            promotion["kind"] = "directory"
        else:
            promotion["sha256"] = "f" * 64
            temporary = {str(entry.path.parent / (".state-reset-restore-" + "f" * 32)): {
                "target": str(entry.path), "parent_identity": identity, "identity": identity,
            }}
            size = len(json.dumps(temporary, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())
            if size > temporary_size:
                temporary_size, largest_temporary = size, temporary
        promotions[str(entry.path)] = promotion
    selected_storage = storage or state.parent / (".neocortex-state-reset-raw-" + "f" * 32)
    payload = _operation_metadata(selected_storage, transient=False, phase="rolled-back-cleanup-pending",
                                  identity=identity, promotions=promotions, temporaries=largest_temporary,
                                  receipt=selected_storage / "state-reset-manifest.json", receipt_digest="f" * 64)
    return len(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())


def _read_registry_record(registry: ArtifactRegistry, artifact_id: str) -> ArtifactRecord:
    record = registry.verify(artifact_id)
    if not isinstance(record, ArtifactRecord):
        raise RuntimeError("a single artifact identity must resolve to one record")
    return record


@contextmanager
def _directory(root: Path, parts: tuple[str, ...], *, create: bool = False):
    """Open each component relative to its parent without following symlinks."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts:
            if component in {"", ".", ".."}:
                raise RuntimeError("reset recovery directory component is invalid")
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _restore_absent_files(state: Path, entries: tuple[Any, ...], raw_root: Path, promotions: dict[str, Any]) -> None:
    """No-replace publication preserves a file concurrently created after preview."""
    from neocortex.persistence import state_reset as reset
    # Refuse an already observed third-party change before restoring any file.
    for entry in entries:
        reset._reject_symlink_components(entry.path)
        if entry.kind == "file" and os.path.lexists(entry.path):
            if not reset._entry_matches(entry) and not _matches_promotion(entry.path, promotions.get(str(entry.path))):
                raise reset.StateResetRecoveryRequiredError("source changed since reset; recovery refuses overwrite")
        elif entry.kind == "directory" and os.path.lexists(entry.path):
            observed = entry.path.lstat()
            promotion = promotions.get(str(entry.path), {})
            if ((observed.st_dev, observed.st_ino) != (entry.device, entry.inode)
                    and [observed.st_dev, observed.st_ino] != promotion.get("identity")):
                raise reset.StateResetRecoveryRequiredError("reset recovery directory identity changed")
    for entry in entries:
        parts = Path(entry.relative_path).parts
        if entry.kind == "directory":
            with _directory(state, parts, create=True) as directory_fd:
                observed = os.fstat(directory_fd)
                os.fchmod(directory_fd, entry.mode)
                operation = ACTIVE_OPERATION_HANDLE.get()
                if operation is not None:
                    operation.promotions[str(entry.path)] = {
                        "owner": "raw-rollback", "kind": "directory",
                        "identity": [observed.st_dev, observed.st_ino], "size": observed.st_size,
                    }
                    operation.update("applying", operation.storage / "state-reset-manifest.json")
            continue
        with _directory(state, parts[:-1], create=True) as destination, _directory(raw_root, parts[:-1]) as source_parent:
            try:
                current = os.stat(parts[-1], dir_fd=destination, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None:
                if not reset._entry_matches(entry):
                    promotion = promotions.get(str(entry.path))
                    if not _matches_promotion(entry.path, promotion):
                        raise reset.StateResetRecoveryRequiredError("reset recovery source appeared or changed")
                    if (isinstance(promotion, dict) and promotion.get("owner") == "raw-rollback"
                            and promotion.get("sha256") == entry.sha256
                            and stat.S_IMODE(current.st_mode) == entry.mode):
                        # A previous recovery already published these exact
                        # original bytes. Keep the inode, including any
                        # producer binding compensated before a process died.
                        continue
                    os.unlink(parts[-1], dir_fd=destination)
                else:
                    continue
            temporary = ".state-reset-restore-" + uuid.uuid4().hex
            operation = ACTIVE_OPERATION_HANDLE.get()
            if operation is None:
                raise reset.StateResetRecoveryRequiredError("restore temporary requires its durable operation")
            temporary_path = entry.path.parent / temporary
            parent_identity = os.fstat(destination)
            operation.restore_temporaries[str(temporary_path)] = {
                "target": str(entry.path), "parent_identity": [parent_identity.st_dev, parent_identity.st_ino],
                "identity": None,
            }
            operation.update("applying", operation.storage / "state-reset-manifest.json")
            source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent)
            output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                entry.mode, dir_fd=destination)
            temporary_identity = os.fstat(output_fd)
            operation.restore_temporaries[str(temporary_path)]["identity"] = [temporary_identity.st_dev, temporary_identity.st_ino]
            operation.update("applying", operation.storage / "state-reset-manifest.json")
            try:
                digest = hashlib.sha256()
                with os.fdopen(source_fd, "rb") as source, os.fdopen(output_fd, "wb") as output:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fchmod(output.fileno(), entry.mode)
                    os.fsync(output.fileno())
                if digest.hexdigest() != entry.sha256:
                    raise reset.StateResetRecoveryRequiredError("reset rollback payload changed during copying")
                record_state_reset_promotion("raw-rollback", entry.path.parent / temporary, entry.path)
                os.link(temporary, parts[-1], src_dir_fd=destination, dst_dir_fd=destination, follow_symlinks=False)
                os.fsync(destination)
            finally:
                os.unlink(temporary, dir_fd=destination)
                operation.restore_temporaries.pop(str(temporary_path), None)
                operation.update("applying", operation.storage / "state-reset-manifest.json")


def _retire_restore_temporaries(state: Path, operation: Any) -> None:
    """Remove only exact restoration temporaries recorded by this operation."""
    from neocortex.persistence import state_reset as reset
    for raw_path, claim in tuple(operation.restore_temporaries.items()):
        path = Path(raw_path)
        target = Path(claim["target"])
        if not path.is_absolute() or state not in path.parents or path.parent != target.parent:
            raise reset.StateResetRecoveryRequiredError("restore temporary escaped its recorded target")
        reset._reject_symlink_components(path)
        if os.path.lexists(path):
            with _directory(state, path.relative_to(state).parts[:-1]) as directory:
                parent = os.fstat(directory)
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if ([parent.st_dev, parent.st_ino] != claim.get("parent_identity")
                        or [current.st_dev, current.st_ino] != claim.get("identity")
                        or not stat.S_ISREG(current.st_mode)):
                    raise reset.StateResetRecoveryRequiredError("restore temporary identity is not verified")
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
        operation.restore_temporaries.pop(raw_path)
        operation.update("applying", operation.storage / "state-reset-manifest.json")


def _matches_promotion(path: Path, promotion: Any) -> bool:
    from neocortex.persistence import state_reset as reset
    if not isinstance(promotion, dict):
        return False
    try:
        metadata = path.lstat()
        return (stat.S_ISREG(metadata.st_mode)
                and [metadata.st_dev, metadata.st_ino] == promotion.get("identity")
                and metadata.st_size == promotion.get("size")
                and reset._sha256(path) == promotion.get("sha256"))
    except OSError:
        return False


def require_original_artifact_bindings(inventory: dict[str, Any] | None, registry: ArtifactRegistry) -> None:
    """Compensate only claims enrolled before effects under their own owner."""
    from neocortex.persistence import state_reset as reset
    pending = {str(node["node_id"]): node for node in (inventory or {}).get("nodes", ())
               if node.get("kind") == "artifact-claim" and node.get("disposition") == "retire"}
    ordered: list[dict[str, Any]] = []
    while pending:
        ready = sorted(key for key, node in pending.items()
                       if not any(parent in pending for parent in node.get("dependencies", ())))
        if not ready:
            raise reset.StateResetRecoveryRequiredError("artifact compensation dependencies contain a cycle")
        ordered.extend(pending.pop(key) for key in ready)
    # Inputs are rebound before consumers, the reverse of retirement order.
    for node in ordered:
        record = _read_registry_record(registry, str(node["node_id"]))
        if record.verified and record.manifest_digest == node["manifest_digest"]:
            continue
        operation = ACTIVE_OPERATION_HANDLE.get()
        try:
            if operation is None or record.owner != node.get("owner") or record.manifest_digest is None:
                raise RuntimeError("artifact compensation lacks its operation or owner")
            receipt = record.metadata.get("retirement_compensation_receipt", {})
            if not (record.verified and receipt.get("original_manifest_digest") == node["manifest_digest"]):
                record = registry.for_owner(record.owner).reconcile_restored_retirement(
                    record.artifact_id, recovery_artifact_id=operation.operation_id,
                    expected_retirement_manifest_digest=record.manifest_digest,
                )
                receipt = record.metadata.get("retirement_compensation_receipt", {})
            if (not record.verified or record.state != node["state"]
                    or receipt.get("original_manifest_digest") != node["manifest_digest"]):
                raise RuntimeError("artifact compensation did not restore its original contract")
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as exc:
            raise reset.StateResetRecoveryRequiredError(
                "restored artifact still requires producer binding reconciliation: " + str(node["node_id"])
            ) from exc


def record_state_reset_promotion(owner: str, final_database: Path, live_database: Path) -> None:
    """Persist the staged inode/hash before publishing it over a live owner."""
    from neocortex.persistence import state_reset as reset
    operation = ACTIVE_OPERATION_HANDLE.get()
    if operation is None:
        return
    metadata = final_database.lstat()
    operation.promotions[str(live_database)] = {
        "owner": owner, "identity": [metadata.st_dev, metadata.st_ino],
        "sha256": reset._sha256(final_database), "size": metadata.st_size,
    }
    operation.update("applying", operation.storage / "state-reset-manifest.json")


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RECEIPT_BYTES or before.st_nlink != 1:
            raise RuntimeError("reset receipt is not a bounded private regular file")
        raw = stream.read(_MAX_RECEIPT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError("reset receipt changed while reading")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("reset receipt is not an object")
    return value, hashlib.sha256(raw).hexdigest()


@dataclass(slots=True)
class ResetOperation:
    operation_id: str
    registry: ArtifactRegistry
    storage: Path
    intent: Path
    transient: bool
    identity: tuple[int, int] | None = None
    promotions: dict[str, Any] = field(default_factory=dict)
    restore_temporaries: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def prepare(cls, plan: Any, backup_directory: Path | None) -> "ResetOperation":
        from neocortex.persistence import state_reset as reset
        operation_id = "state-reset-" + uuid.uuid4().hex
        evidence_root = plan.state_directory / "state-reset-operations"
        if not evidence_root.exists():
            evidence_root.mkdir(mode=0o700)
        reset._reject_symlink_components(evidence_root)
        metadata = evidence_root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeError("reset operation evidence root is not private")
        storage = backup_directory if backup_directory is not None else plan.state_directory.parent / (".neocortex-state-reset-raw-" + uuid.uuid4().hex)
        intent = evidence_root / f"{operation_id}.json"
        payload = {"schema": RESET_OPERATION_SCHEMA, "operation_id": operation_id,
                   "state_directory": str(plan.state_directory), "scope": plan.scope,
                   "storage": str(storage), "transient": backup_directory is None,
                   "plan_digest": plan.plan_digest, "status": "intent", "targets": [target.as_payload() for target in plan.targets],
                   "state_inventory": None if plan.inventory is None else plan.inventory.as_payload()}
        reset._write_json(intent, payload)
        registry = ArtifactRegistry(plan.state_directory / "artifacts", owner="state-reset", create_root=True)
        digest = reset._sha256(intent)
        registry.register(operation_id, producer="state-reset", path=intent, root=evidence_root,
                          purpose=RESET_OPERATION_PURPOSE, kind="operational", state="active", disposable=False,
                          digest=digest, metadata={"phase": "intent", "storage": str(storage),
                                                   "transient": backup_directory is None})
        return cls(operation_id, registry, storage, intent, backup_directory is None)

    def acquired(self) -> None:
        metadata = self.storage.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RuntimeError("reset staging is not private")
        self.identity = (metadata.st_dev, metadata.st_ino)
        self.update("acquired")

    def update(self, phase: str, receipt: Path | None = None) -> None:
        digest = None
        if receipt is not None and receipt.exists():
            _, digest = _read_json(receipt)
        else:
            receipt = None
        metadata = _operation_metadata(self.storage, transient=self.transient, phase=phase, identity=self.identity,
                                       promotions=self.promotions, temporaries=self.restore_temporaries,
                                       receipt=receipt, receipt_digest=digest)
        self.registry.update(self.operation_id,
                             state="completed" if phase in {"complete", "aborted"} else ("recovery_required" if "pending" in phase or "failed" in phase else "active"),
                             metadata=metadata)


def reconcile_state_reset(
    state_directory: str | Path, operation_id: str, *, apply: bool = False,
    expected_receipt_digest: str | None = None, confirmation: str | None = None,
) -> dict[str, object]:
    from neocortex.persistence import state_reset as reset
    state = reset._safe_state_directory(state_directory)
    registry = ArtifactRegistry(state / "artifacts", owner="state-reset")
    with reset._held_locks(state), registry.observation_guard():
        record = _read_registry_record(registry, operation_id)
        if not record.verified or record.purpose != RESET_OPERATION_PURPOSE or record.owner != "state-reset":
            raise reset.StateResetError("reset recovery requires a verified operation claim")
        intent, intent_digest = _read_json(record.path)
        if intent_digest != record.digest or intent.get("schema") != RESET_OPERATION_SCHEMA or intent.get("state_directory") != str(state) or intent.get("operation_id") != operation_id:
            raise reset.StateResetError("reset operation intent changed")
        storage = Path(str(intent["storage"]))
        if not storage.is_absolute() or storage == state or state in storage.parents:
            raise reset.StateResetError("reset rollback storage violates its external boundary")
        reset._reject_symlink_components(storage)
        receipt_path = storage / "state-reset-manifest.json"
        phase = str(record.metadata.get("phase"))
        if phase in {"complete", "aborted"}:
            return {"schema": RESET_OPERATION_SCHEMA, "operation_id": operation_id,
                    "status": "no_changes", "action": "none", "applied": False,
                    "receipt_digest": record.metadata.get("receipt_digest") or intent_digest}
        if not storage.exists():
            if phase not in {"intent", "applied", "applied-cleanup-pending", "rolled-back", "rolled-back-cleanup-pending"}:
                raise reset.StateResetRecoveryRequiredError("recorded staging disappeared")
            receipt: dict[str, Any] = {}
            digest = intent_digest
            action = "close-unused-intent" if phase == "intent" else "close-verified-intent"
        else:
            metadata = storage.lstat()
            expected_identity = record.metadata.get("storage_identity")
            if expected_identity is None or [metadata.st_dev, metadata.st_ino] != list(expected_identity):
                raise reset.StateResetRecoveryRequiredError("reset staging identity is not verified")
            if phase == "acquired" and not os.path.lexists(receipt_path):
                # The area is acquired but no payload/effect phase was begun.
                receipt, digest = {}, intent_digest
            else:
                receipt, digest = _read_json(receipt_path)
                if digest != record.metadata.get("receipt_digest") or receipt.get("plan_digest") != intent["plan_digest"]:
                    raise reset.StateResetRecoveryRequiredError("reset receipt digest changed")
            if phase in {"acquired", "preparing", "prepared", "failed-before-effect", "pre-effect-cleanup-pending", "rolled-back", "rolled-back-cleanup-pending", "applied", "applied-cleanup-pending"}:
                action = "cleanup-transient" if intent["transient"] else "close-durable-backup"
            elif phase in {"applying", "rollback-failed"}:
                action = "restore-verified-rollback"
            else:
                raise reset.StateResetRecoveryRequiredError("reset staging phase requires manual reconciliation")
        entries = []
        for target in intent["targets"]:
            for item in target["entries"]:
                relative = item["relative_path"]
                if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts or str(state / relative) != item["path"]:
                    raise reset.StateResetError("reset recovery target escaped its authorized root")
                entries.append(reset.StateResetEntry(path=state / relative, **{key: value for key, value in item.items() if key != "path"}))
        # Replay only into absences or files identical to the exact original.
        # An unknown promoted inode or personal modification never gets overwritten.
        if action == "restore-verified-rollback":
            for entry in entries:
                if entry.kind != "file":
                    continue
                raw = storage / "reset-files" / entry.relative_path
                if not raw.is_file() or reset._sha256(raw) != entry.sha256:
                    raise reset.StateResetRecoveryRequiredError("rollback payload is incomplete or changed")
                reset._reject_symlink_components(entry.path)
                if os.path.lexists(entry.path) and not reset._entry_matches(entry) and not _matches_promotion(entry.path, record.metadata.get("promotions", {}).get(str(entry.path))):
                    raise reset.StateResetRecoveryRequiredError("source changed since reset; recovery refuses overwrite")
        payload: dict[str, object] = {"schema": RESET_OPERATION_SCHEMA, "operation_id": operation_id,
            "status": "preview", "action": action, "phase": phase,
            "receipt_digest": digest, "storage": str(storage), "applied": False}
        if not apply:
            return payload
        if confirmation != reset.STATE_RESET_CONFIRMATION or expected_receipt_digest != digest:
            raise reset.StateResetConfirmationError("recovery requires RESET_STATE and the exact receipt digest")
        operation = ResetOperation(operation_id, registry, storage, record.path, bool(intent["transient"]),
                                   None if not storage.exists() else (storage.stat().st_dev, storage.stat().st_ino),
                                   promotions=dict(record.metadata.get("promotions", {})),
                                   restore_temporaries=dict(record.metadata.get("restore_temporaries", {})))
        if action == "restore-verified-rollback":
            token = ACTIVE_OPERATION_HANDLE.set(operation)
            try:
                _retire_restore_temporaries(state, operation)
                with ExitStack() as guards:
                    for target in intent["targets"]:
                        if target["owner"] is not None:
                            database = state / reset.STATE_STORE_REGISTRY.by_owner(target["owner"]).database_name
                            if database.exists():
                                guards.enter_context(reset.sqlite_owner_effect_guard(database))
                    _restore_absent_files(state, tuple(entries), storage / "reset-files", operation.promotions)
                require_original_artifact_bindings(intent.get("state_inventory"), registry)
            except BaseException:
                operation.update("rollback-failed", receipt_path)
                raise
            finally:
                ACTIVE_OPERATION_HANDLE.reset(token)
            if any(entry.kind == "file" and reset._sha256(entry.path) != entry.sha256 for entry in entries):
                raise reset.StateResetRecoveryRequiredError("rollback verification failed")
        if storage.exists() and intent["transient"]:
            assert operation.identity is not None
            reset._retire_reset_rollback_area(storage, operation.identity)
        operation.update("complete" if phase.startswith("applied") else "aborted")
        payload.update(status="complete", applied=True)
        return payload
