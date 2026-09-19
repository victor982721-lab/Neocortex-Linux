"""Exact historical selections backed by private producer and approval receipts.

This extends the historical owner; it is not a filesystem cleaner.  A pathname,
UID, manifest digest or user supplied ``approved`` field supplies no provenance.
The configured artifact registry must already contain the producer's claim.
An entry without the current scratch manifest additionally needs a verified,
protected second copy.  All effect authority is emitted by ``approve`` and kept
in the private state directory, separately from the selected payload.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from neocortex.runtime.artifact_registry import ArtifactRecord, ArtifactRegistry
from neocortex.runtime.historical_audit import HistoricalAuditError, _identity
from neocortex.runtime import scratch_tree
from neocortex.runtime.path_identity import PathIdentity

SCHEMA = "neocortex.historical-selection/v1"
_MAX_BYTES = 4 * 1024 * 1024


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json(value)).hexdigest()


def _encoded(path: Path) -> str:
    return base64.b64encode(os.fsencode(path)).decode("ascii")


def _decoded(value: str) -> Path:
    return Path(os.fsdecode(base64.b64decode(value, validate=True)))


@dataclass(frozen=True, slots=True)
class HistoricalSelection:
    path: Path
    provenance_artifact_id: str
    preserved_artifact_id: str | None = None

    def __post_init__(self) -> None:
        path = Path(self.path)
        if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
            raise ValueError("historical selection must be an exact absolute path")
        if not isinstance(self.provenance_artifact_id, str) or not self.provenance_artifact_id:
            raise ValueError("a producer artifact reference is required")
        object.__setattr__(self, "path", path)

    def to_dict(self) -> dict[str, object]:
        return {"path_bytes_base64": _encoded(self.path),
                "posix_path_identity": PathIdentity.from_path(self.path).as_dict(),
                "display_escaped": str(self.path).encode("unicode_escape").decode("ascii"),
                "provenance_artifact_id": self.provenance_artifact_id,
                "preserved_artifact_id": self.preserved_artifact_id}


@dataclass(frozen=True, slots=True)
class HistoricalSelectionPlan:
    root: Path
    owner: str
    records: tuple[Mapping[str, Any], ...]
    partial: bool = False
    limits: Mapping[str, int] = field(default_factory=dict)
    coverage_complete: bool = True

    @property
    def digest(self) -> str:
        return _digest(self._body())

    @property
    def status(self) -> str:
        if not self.coverage_complete:
            return "partial"
        return "planned" if all(r["status"] == "eligible" for r in self.records) else "blocked"

    def _body(self) -> dict[str, object]:
        return {"schema": SCHEMA, "root_bytes_base64": _encoded(self.root),
                "posix_root_identity": PathIdentity.from_path(self.root).as_dict(),
                "owner": self.owner, "partial": self.partial,
                "records": list(self.records), "limits": dict(self.limits),
                "coverage_complete": self.coverage_complete}

    def to_dict(self) -> dict[str, object]:
        return {**self._body(), "digest": self.digest, "status": self.status,
                "eligible": sum(r["status"] == "eligible" for r in self.records),
                "exclusive_reclaimable_bytes": None}


class _Budget:
    def __init__(self, manager: Any, *, deadline_ns: int | None = None,
                 cancelled: Callable[[], bool] | None = None) -> None:
        self.entries = 0
        self.bytes = 0
        self.maximum_entries = manager.max_entries
        self.maximum_bytes = manager.max_bytes
        self.deadline_ns = deadline_ns
        self.cancelled = cancelled

    def consume(self, entries: int = 0, size: int = 0) -> None:
        if self.cancelled is not None and self.cancelled():
            raise HistoricalAuditError("cancelled")
        if self.deadline_ns is not None and time.monotonic_ns() >= self.deadline_ns:
            raise HistoricalAuditError("deadline_exceeded")
        self.entries += entries
        self.bytes += size
        if self.entries > self.maximum_entries or self.bytes > self.maximum_bytes:
            raise HistoricalAuditError("observation_budget_exceeded")


@contextmanager
def _selected_fds(root: Path, path: Path, budget: _Budget) -> Iterator[tuple[int, int, list[dict[str, object]]]]:
    """Hold every selected ancestor; a shared sticky parent is context only."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise HistoricalAuditError("selection_outside_explicit_root") from exc
    if not relative.parts or any(p in {"", ".", ".."} or "/" in p for p in relative.parts):
        raise HistoricalAuditError("selection_must_name_one_descendant")
    fds: list[int] = []
    claims: list[dict[str, object]] = []
    try:
        fd = scratch_tree._open_directory_path(root)
        fds.append(fd)
        root_meta = os.fstat(fd)
        shared_sticky = bool(root_meta.st_mode & stat.S_ISVTX) and bool(root_meta.st_mode & 0o002)
        if not shared_sticky and (root_meta.st_uid != os.geteuid() or root_meta.st_mode & 0o022):
            raise HistoricalAuditError("historical_root_protection_unsafe")
        mount = scratch_tree._mount_id(fd)
        if mount not in scratch_tree._mount_ids():
            raise HistoricalAuditError("mount_boundary_unavailable")
        claims.append({"identity": list(_identity(root_meta)), "mount_id": mount,
                       "mode": stat.S_IMODE(root_meta.st_mode), "component_bytes_base64": None})
        for component in relative.parts:
            budget.consume(entries=1)
            child, metadata = scratch_tree._checked_child(fd, component, mount)
            fds.append(child)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022:
                raise HistoricalAuditError("selected_ancestor_protection_unsafe")
            claims.append({"identity": list(_identity(metadata)), "mount_id": mount,
                           "mode": stat.S_IMODE(metadata.st_mode),
                           "component_bytes_base64": base64.b64encode(os.fsencode(component)).decode("ascii")})
            fd = child
        yield fds[-2], fds[-1], claims
        # Checking each retained link also detects a path moved out from under
        # the original root while the descriptors remained valid.
        root_now = os.stat(root, follow_symlinks=False)
        if list(_identity(root_now)) != claims[0]["identity"]:
            raise HistoricalAuditError("selected_root_changed")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _observe_tree(fd: int, budget: _Budget, *, max_depth: int) -> dict[str, object]:
    """Hash exact names and bytes without following links or opening specials."""
    digest = hashlib.sha256()
    apparent = allocated = members = 0
    seen_inodes: set[tuple[int, int]] = set()
    mount = scratch_tree._mount_id(fd)
    stack: list[tuple[int, tuple[bytes, ...]]] = [(os.dup(fd), ())]
    try:
        while stack:
            directory_fd, components = stack.pop()
            try:
                if len(components) > max_depth:
                    raise HistoricalAuditError("depth_budget_exceeded")
                names = scratch_tree._entries(directory_fd, limit=max(0, budget.maximum_entries - budget.entries))
                for name in sorted(names, key=os.fsencode):
                    budget.consume(entries=1)
                    members += 1
                    child, metadata = scratch_tree._checked_child(directory_fd, name, mount)
                    raw_components = (*components, os.fsencode(name))
                    name_token = b"/".join(raw_components)
                    digest.update(len(name_token).to_bytes(8, "big") + name_token)
                    if stat.S_ISDIR(metadata.st_mode):
                        digest.update(b"D")
                        stack.append((child, raw_components))
                        continue
                    os.close(child)
                    digest.update(b"F")
                    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                      dir_fd=directory_fd)
                    try:
                        opened = os.fstat(file_fd)
                        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(metadata):
                            raise HistoricalAuditError("selected_file_changed")
                        digest.update(int(opened.st_size).to_bytes(8, "big"))
                        while True:
                            budget.consume()
                            chunk = os.read(file_fd, 64 * 1024)
                            if not chunk:
                                break
                            budget.consume(size=len(chunk))
                            digest.update(chunk)
                        after = os.fstat(file_fd)
                        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            raise HistoricalAuditError("selected_file_changed")
                        identity = (opened.st_dev, opened.st_ino)
                        if identity not in seen_inodes:
                            seen_inodes.add(identity)
                            apparent += opened.st_size
                            allocated += getattr(opened, "st_blocks", 0) * 512
                    finally:
                        os.close(file_fd)
            finally:
                os.close(directory_fd)
    finally:
        for directory_fd, _ in stack:
            os.close(directory_fd)
    return {"content_digest": "sha256:" + digest.hexdigest(), "members": members,
            "apparent_bytes": apparent, "allocated_bytes": allocated}


class HistoricalAdoption:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        if manager.state_directory is None:
            raise HistoricalAuditError("exact adoption requires an explicit private state directory")
        self.root = manager.root
        self.owner = manager.owner
        self.directory = manager.state_directory / "historical-adoptions"
        self.registry = ArtifactRegistry(manager.state_directory / "artifacts",
                                         owner=self.owner, create_root=False)

    def plan(self, selections: Sequence[HistoricalSelection], *, partial: bool = False,
             deadline_ns: int | None = None,
             cancelled: Callable[[], bool] | None = None,
             _budget: _Budget | None = None) -> HistoricalSelectionPlan:
        if not isinstance(partial, bool):
            raise TypeError("partial must be boolean")
        if len(selections) > self.manager.max_entries:
            raise HistoricalAuditError("selection_budget_exceeded")
        budget = _budget or _Budget(self.manager, deadline_ns=deadline_ns, cancelled=cancelled)
        records: list[dict[str, Any]] = []
        seen: set[Path] = set()
        coverage = True
        for selection in selections:
            if selection.path in seen:
                raise HistoricalAuditError("duplicate_selection")
            if any(selection.path.is_relative_to(p) or p.is_relative_to(selection.path) for p in seen):
                raise HistoricalAuditError("overlapping_selections")
            seen.add(selection.path)
            record: dict[str, Any] = {**selection.to_dict(), "status": "unknown"}
            record["selected_id"] = _digest(selection.to_dict()).removeprefix("sha256:")
            try:
                if self.manager.state_directory.is_relative_to(selection.path):
                    raise HistoricalAuditError("selection_contains_adoption_state")
                with _selected_fds(self.root, selection.path, budget) as (_, fd, ancestors):
                    record["ancestors"] = ancestors
                    producer = self.registry.verify(selection.provenance_artifact_id)
                    if (not isinstance(producer, ArtifactRecord) or not producer.verified
                            or producer.owner != self.owner or producer.path != selection.path
                            or list(producer.path_identity or ()) != ancestors[-1]["identity"]):
                        raise HistoricalAuditError("producer_provenance_unverified")
                    if producer.state != "completed" or not producer.disposable:
                        raise HistoricalAuditError("producer_active_protected_or_recovery_required")
                    if producer.root.is_relative_to(selection.path):
                        raise HistoricalAuditError("producer_receipt_root_must_survive_retirement")
                    registry_plan = self.registry.plan()
                    if registry_plan.truncated or registry_plan.unmanaged:
                        raise HistoricalAuditError("dependency_observation_incomplete")
                    eligible = {r.artifact_id for r in registry_plan.eligible_records}
                    if producer.artifact_id not in eligible:
                        raise HistoricalAuditError("producer_policy_or_dependency_protects_entry")
                    record["producer_manifest_digest"] = producer.manifest_digest
                    record["producer"] = producer.producer
                    record["purpose"] = producer.purpose
                    record["dependencies"] = list(producer.dependencies)
                    observation = _observe_tree(fd, budget, max_depth=self.manager.max_depth)
                    record.update(observation)
                    current_manifest = False
                    try:
                        raw = scratch_tree.read_private_manifest(selection.path / "manifest.json", limit=512 * 1024)
                        manifest = json.loads(raw)
                        current_manifest = (
                            manifest.get("schema") == "neocortex.scratch/v1"
                            and manifest.get("owner") == producer.owner
                            and manifest.get("state") == "completed"
                            and producer.artifact_id == f"scratch:{manifest.get('record_id')}"
                            and producer.digest == manifest.get("manifest_digest")
                            and isinstance(producer.metadata.get("scratch_seal"), Mapping)
                        )
                        if current_manifest:
                            from neocortex.runtime.scratch import workspace_payload_digest
                            observed_seal = workspace_payload_digest(selection.path)
                            seal = producer.metadata["scratch_seal"]
                            if observed_seal != (seal.get("digest"), seal.get("members"), seal.get("apparent_bytes")):
                                raise HistoricalAuditError("producer_payload_seal_changed")
                            record["observed_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
                    except (FileNotFoundError, OSError, ValueError, scratch_tree.ScratchTreeError):
                        current_manifest = False
                    if not current_manifest:
                        if selection.preserved_artifact_id is None:
                            raise HistoricalAuditError("unique_copy_preserved_requires_durable_copy_evidence")
                        copy = self.registry.verify(selection.preserved_artifact_id)
                        if (not isinstance(copy, ArtifactRecord) or not copy.verified
                                or copy.state != "completed" or copy.disposable
                                or copy.kind not in {"canonical", "operational"}
                                or copy.path.is_relative_to(selection.path)
                                or selection.path.is_relative_to(copy.path)):
                            raise HistoricalAuditError("durable_copy_evidence_unverified")
                        copy_fd = scratch_tree._open_directory_path(copy.path)
                        try:
                            preserved = _observe_tree(copy_fd, budget, max_depth=self.manager.max_depth)
                        finally:
                            os.close(copy_fd)
                        if preserved["content_digest"] != observation["content_digest"]:
                            raise HistoricalAuditError("durable_copy_content_mismatch")
                        record["preserved_manifest_digest"] = copy.manifest_digest
                    record["status"] = "eligible"
                    record["reason"] = None
            except (HistoricalAuditError, OSError, ValueError, scratch_tree.ScratchTreeError) as exc:
                record["reason"] = str(exc)[:512]
                if "budget" in str(exc) or str(exc) in {"cancelled", "deadline_exceeded"}:
                    coverage = False
            records.append(record)
        return HistoricalSelectionPlan(self.root, self.owner, tuple(records), partial,
                                       {"max_entries": self.manager.max_entries,
                                        "max_depth": self.manager.max_depth,
                                        "max_bytes": self.manager.max_bytes}, coverage)

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[tuple[int, bytes]]:
        state = self.manager.state_directory
        assert state is not None
        if create:
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_fd = scratch_tree._open_directory_path(state)
        directory_fd = lock_fd = -1
        try:
            metadata = os.fstat(state_fd)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise HistoricalAuditError("adoption_state_must_be_private")
            try:
                fcntl.flock(state_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HistoricalAuditError("state_directory_factory_reset_active") from exc
            if create:
                try:
                    os.mkdir("historical-adoptions", 0o700, dir_fd=state_fd)
                    os.fsync(state_fd)
                except FileExistsError:
                    pass
            directory_fd = os.open("historical-adoptions", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=state_fd)
            metadata = os.fstat(directory_fd)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise HistoricalAuditError("adoption_receipts_must_be_private")
            if scratch_tree._mount_id(directory_fd) != scratch_tree._mount_id(state_fd):
                raise HistoricalAuditError("adoption_receipt_mount_boundary")
            lock_fd = os.open("owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                              dir_fd=directory_fd)
            lock_meta = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_nlink != 1 or lock_meta.st_uid != os.geteuid():
                raise HistoricalAuditError("adoption_lock_unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                key = self._read(directory_fd, "authority.key", raw=True)
            except FileNotFoundError:
                if not create:
                    raise HistoricalAuditError("adoption_authority_absent") from None
                key = secrets.token_bytes(32)
                self._write(directory_fd, "authority.key", key, exclusive=True)
            if len(key) != 32:
                raise HistoricalAuditError("adoption_authority_invalid")
            yield directory_fd, key
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            if directory_fd >= 0:
                os.close(directory_fd)
            os.close(state_fd)

    @staticmethod
    def _read(fd: int, name: str, *, raw: bool = False) -> Any:
        opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            meta = os.fstat(opened)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.geteuid()
                    or meta.st_mode & 0o077 or meta.st_size > _MAX_BYTES):
                raise HistoricalAuditError("adoption_receipt_unsafe")
            data = os.read(opened, _MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise HistoricalAuditError("adoption_receipt_too_large")
            return data if raw else json.loads(data)
        finally:
            os.close(opened)

    @staticmethod
    def _write(fd: int, name: str, data: bytes, *, exclusive: bool = False) -> None:
        if len(data) > _MAX_BYTES:
            raise HistoricalAuditError("adoption_receipt_too_large")
        temporary = ".publish-" + secrets.token_hex(16)
        opened = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        try:
            with os.fdopen(opened, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=fd)
            else:
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass

    @staticmethod
    def _name(digest: str, suffix: str) -> str:
        token = digest.removeprefix("sha256:")
        if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
            raise HistoricalAuditError("invalid_proposal_digest")
        return token + suffix

    @staticmethod
    def _signed(body: Mapping[str, object], key: bytes) -> bytes:
        return _json({"body": body, "authenticator": hmac.new(key, _json(body), hashlib.sha256).hexdigest()})

    def _verified(self, fd: int, name: str, key: bytes) -> dict[str, Any]:
        value = self._read(fd, name)
        if (not isinstance(value, dict) or not isinstance(value.get("body"), dict)
                or not isinstance(value.get("authenticator"), str)
                or not hmac.compare_digest(value["authenticator"],
                    hmac.new(key, _json(value["body"]), hashlib.sha256).hexdigest())):
            raise HistoricalAuditError("adoption_receipt_authentication_failed")
        body = value["body"]
        if body.get("owner") != self.owner or body.get("root_bytes_base64") != _encoded(self.root):
            raise HistoricalAuditError("adoption_receipt_scope_mismatch")
        return body

    def prepare(self, plan: HistoricalSelectionPlan) -> HistoricalSelectionPlan:
        if not isinstance(plan, HistoricalSelectionPlan) or plan.root != self.root or plan.owner != self.owner:
            raise HistoricalAuditError("proposal_scope_mismatch")
        with self._locked(create=True) as (fd, key):
            name = self._name(plan.digest, ".proposal.json")
            body = plan._body()
            try:
                existing = self._verified(fd, name, key)
            except FileNotFoundError:
                self._write(fd, name, self._signed(body, key), exclusive=True)
            else:
                if existing != body:
                    raise HistoricalAuditError("proposal_conflict")
        return plan

    def load_plan(self, digest: str) -> HistoricalSelectionPlan:
        with self._locked(create=False) as (fd, key):
            body = self._verified(fd, self._name(digest, ".proposal.json"), key)
        if _digest(body) != digest:
            raise HistoricalAuditError("proposal_digest_mismatch")
        return HistoricalSelectionPlan(self.root, self.owner, tuple(body["records"]),
                                       body["partial"], body["limits"], body["coverage_complete"])

    @staticmethod
    def _selections(body: Mapping[str, Any]) -> tuple[HistoricalSelection, ...]:
        return tuple(HistoricalSelection(_decoded(r["path_bytes_base64"]),
                                         r["provenance_artifact_id"], r.get("preserved_artifact_id"))
                     for r in body["records"])

    def approve(self, digest: str, selected_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Explicit local operator action, never inferred from a proposal digest."""
        with self._locked(create=False) as (fd, key):
            body = self._verified(fd, self._name(digest, ".proposal.json"), key)
            if _digest(body) != digest:
                raise HistoricalAuditError("proposal_digest_mismatch")
            refreshed = self.plan(self._selections(body), partial=body["partial"])
            if refreshed.digest != digest:
                raise HistoricalAuditError("proposal_changed_before_approval")
            eligible = {r["selected_id"] for r in body["records"] if r["status"] == "eligible"}
            ids = tuple(sorted(eligible if selected_ids is None else selected_ids))
            if not ids or len(set(ids)) != len(ids) or not set(ids).issubset(eligible):
                raise HistoricalAuditError("approval_requires_exact_eligible_selection")
            if not body["coverage_complete"] or (not body["partial"] and len(eligible) != len(body["records"])):
                raise HistoricalAuditError("total_adoption_requires_complete_selection")
            receipt = {"schema": SCHEMA, "owner": self.owner, "root_bytes_base64": _encoded(self.root),
                       "plan_digest": digest, "selected_ids": list(ids), "approving_uid": os.geteuid(),
                       "state": "approved"}
            name = self._name(digest, ".authority.json")
            try:
                previous = self._verified(fd, name, key)
            except FileNotFoundError:
                self._write(fd, name, self._signed(receipt, key), exclusive=True)
            else:
                if previous != receipt:
                    raise HistoricalAuditError("approval_selection_conflict")
        return receipt

    def apply(self, digest: str, selected_ids: Sequence[str] | None = None, *,
              deadline_ns: int | None = None,
              cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        with self._locked(create=False) as (fd, key):
            body = self._verified(fd, self._name(digest, ".proposal.json"), key)
            authority = self._verified(fd, self._name(digest, ".authority.json"), key)
            if authority.get("plan_digest") != digest or authority.get("approving_uid") != os.geteuid():
                raise HistoricalAuditError("adoption_authority_mismatch")
            approved = set(authority["selected_ids"])
            ids = approved if selected_ids is None else set(selected_ids)
            if not ids or not ids.issubset(approved):
                raise HistoricalAuditError("selection_exceeds_adoption_authority")
            results: list[dict[str, Any]] = []
            budget = _Budget(self.manager, deadline_ns=deadline_ns, cancelled=cancelled)
            selected_records = [r for r in body["records"] if r["selected_id"] in ids]
            # A total batch never starts an irreversible effect if any of its
            # fresh claims are unknown. Existing intents are handled below by
            # their own receipt, including a target already absent on replay.
            if not body["partial"]:
                first_application = True
                for r in selected_records:
                    try:
                        self._verified(fd, self._name(digest, f".{r['selected_id']}.effect.json"), key)
                        first_application = False
                    except FileNotFoundError:
                        pass
                if first_application:
                    refreshed = self.plan(tuple(HistoricalSelection(_decoded(r["path_bytes_base64"]),
                                           r["provenance_artifact_id"], r.get("preserved_artifact_id"))
                                           for r in selected_records), _budget=budget)
                    if list(refreshed.records) != selected_records:
                        raise HistoricalAuditError("total_selection_changed_before_effect")
            for record in body["records"]:
                selected_id = record["selected_id"]
                if selected_id not in ids:
                    continue
                receipt_name = self._name(digest, f".{selected_id}.effect.json")
                result = {"schema": SCHEMA, "owner": self.owner, "root_bytes_base64": _encoded(self.root),
                          "selected_id": selected_id, "plan_digest": digest,
                          "provenance_artifact_id": record["provenance_artifact_id"],
                          "path_bytes_base64": record["path_bytes_base64"], "state": "prepared"}
                path = _decoded(record["path_bytes_base64"])
                effect_started = False
                try:
                    previous = self._verified(fd, receipt_name, key)
                except FileNotFoundError:
                    previous = None
                if previous is not None:
                    if previous["state"] == "retired" and not os.path.lexists(path):
                        results.append({**previous, "replayed": True})
                        continue
                    if not os.path.lexists(path):
                        self.registry.recover_retirements()
                        tombstone = self.registry.verify(record["provenance_artifact_id"])
                        if isinstance(tombstone, ArtifactRecord) and tombstone.state == "retired":
                            result["state"] = "retired"
                            self._write(fd, receipt_name, self._signed(result, key))
                            results.append({**result, "replayed": True})
                            continue
                    results.append({**result, "state": "recovery_required", "reason": "prior_effect_requires_reconciliation"})
                    continue
                try:
                    budget.consume()
                    selection = HistoricalSelection(path, record["provenance_artifact_id"],
                                                    record.get("preserved_artifact_id"))
                    current = self.plan((selection,), partial=body["partial"],
                                        deadline_ns=deadline_ns, cancelled=cancelled, _budget=budget)
                    if dict(current.records[0]) != dict(record):
                        raise HistoricalAuditError("selection_changed_before_effect")
                    # Preserve a durable exact intent before entering the
                    # registry guard, which also records its own lifecycle intent.
                    self._write(fd, receipt_name, self._signed(result, key), exclusive=True)
                    with self.registry.retirement_guard(selection.provenance_artifact_id) as producer:
                        if (producer.producer != record["producer"] or producer.purpose != record["purpose"]
                                or list(producer.dependencies) != record["dependencies"]):
                            raise HistoricalAuditError("producer_changed_at_effect_boundary")
                        if selection.preserved_artifact_id is not None:
                            preserved = self.registry.verify(selection.preserved_artifact_id)
                            if (not isinstance(preserved, ArtifactRecord) or not preserved.verified
                                    or preserved.manifest_digest != record.get("preserved_manifest_digest")):
                                raise HistoricalAuditError("preserved_copy_changed_at_effect_boundary")
                            preserved_fd = scratch_tree._open_directory_path(preserved.path)
                            try:
                                copy_observed = _observe_tree(preserved_fd, budget, max_depth=self.manager.max_depth)
                            finally:
                                os.close(preserved_fd)
                            if copy_observed["content_digest"] != record["content_digest"]:
                                raise HistoricalAuditError("preserved_copy_changed_at_effect_boundary")
                        with _selected_fds(self.root, path, budget) as (parent_fd, entry_fd, ancestors):
                            if ancestors != record["ancestors"]:
                                raise HistoricalAuditError("selected_ancestor_changed")
                            actual = _observe_tree(entry_fd, budget, max_depth=self.manager.max_depth)
                            if actual["content_digest"] != record["content_digest"]:
                                raise HistoricalAuditError("selected_payload_changed")
                            def boundary_check(path: Path = path, ancestors: list[dict[str, object]] = ancestors) -> None:
                                budget.consume()
                                with _selected_fds(self.root, path, budget) as (_, _, current_ancestors):
                                    if current_ancestors != ancestors:
                                        raise HistoricalAuditError("selected_ancestor_changed_during_effect")
                            effect_started = True
                            scratch_tree.remove_claimed_directory_contents(entry_fd, limit=self.manager.max_entries,
                                        max_depth=self.manager.max_depth, boundary_check=boundary_check)
                            boundary_check()
                            final = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                            if list(_identity(final)) != ancestors[-1]["identity"]:
                                raise HistoricalAuditError("selected_entry_changed_before_rmdir")
                            os.rmdir(path.name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                        self.registry.update(selection.provenance_artifact_id, state="retired")
                    result["state"] = "retired"
                    result["deleted_apparent_bytes"] = record["apparent_bytes"]
                    self._write(fd, receipt_name, self._signed(result, key))
                except (HistoricalAuditError, OSError, RuntimeError, ValueError) as exc:
                    result["state"] = "recovery_required" if effect_started or previous is not None or not os.path.lexists(path) else "blocked"
                    result["reason"] = str(exc)[:512]
                    # A prepared receipt is intentionally retained when its
                    # final publication fails; a fresh session reconciles it.
                results.append(result)
            complete = all(r["state"] == "retired" for r in results)
            return {"schema": SCHEMA, "plan_digest": digest,
                    "operation_status": "complete" if complete else "partial",
                    "coverage_complete": complete, "records": results,
                    "exclusive_reclaimable_bytes": None,
                    "receipt_directory": str(self.directory)}


__all__ = ["HistoricalSelection", "HistoricalSelectionPlan"]
