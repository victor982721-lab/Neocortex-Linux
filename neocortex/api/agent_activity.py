"""Small public lifecycle for externally-owned agent activities.

The activity surface is deliberately an adapter over the two existing runtime
owners.  It does not introduce another database or a second manifest format:
the activity identity and its lifecycle live in the registered scratch
manifest, while published deliverables are ordinary non-disposable
``ArtifactRegistry`` records.  A caller therefore only needs a state
directory and a stable activity id to recover an interrupted activity from a
new Python process.

This module is intentionally deterministic and local.  ``run`` accepts an
argv sequence (never a shell string), executes with the private workspace as
its working directory, and exposes the workspace path through environment
variables.  It is a convenience for an external producer, not a daemon or a
global process watcher.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from neocortex.runtime.artifact_registry import (
    ArtifactConflictError,
    ArtifactRegistryError,
    ArtifactRecord,
    ArtifactRegistry,
)
from neocortex.runtime.scratch import ScratchManager, ScratchRecord, ScratchState, ScratchWorkspace
from neocortex.runtime.scratch import (
    ScratchSecurityError,
    workspace_payload_digest as _scratch_workspace_digest,
)
from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)
from neocortex.runtime.control.process_scope import verified_process_quiescence

if TYPE_CHECKING:
    from neocortex.workflow.retention.planner import (
        TerminalRetentionPlan,
        TerminalRetentionPolicy,
    )

__all__ = [
    "AGENT_ACTIVITY_CLI_SCHEMA",
    "AGENT_ACTIVITY_SCHEMA",
    "DEFAULT_AGENT_OWNER",
    "ActivitySnapshot",
    "AgentActivity",
    "AgentActivityChanged",
    "AgentActivityConflict",
    "AgentActivityError",
    "AgentActivityNotFound",
    "AgentActivityProcessError",
    "AgentActivityRecoveryRequired",
    "ProcessResult",
    "PublishedDeliverable",
]


AGENT_ACTIVITY_SCHEMA = "neocortex.agent-activity/v1"
AGENT_ACTIVITY_CLI_SCHEMA = "neocortex.agent-activity-cli/v1"
# This is the owner accepted by the existing framework maintenance command.
# External activities remain distinguishable by their activity metadata; they
# do not get a second maintenance owner or a bypass around owner checks.
DEFAULT_AGENT_OWNER = "neocortex-framework"
_SCRATCH_SCOPE = "owned-temp"
_ACTIVITY_META_KEY = "agent_activity"
_NOTE_SCHEMA = "neocortex.agent-activity-note/v1"
_NOTE_PREFIX = "agent-activity-note:"
_MAX_ACTIVITY_ID_BYTES = 160
_MAX_OWNER_BYTES = 128
_MAX_OUTPUT_BYTES = 1 << 20
_MAX_FILE_BYTES = 1 << 40
_CHUNK_BYTES = 1024 * 1024


class AgentActivityError(RuntimeError):
    """Base error for the external activity contract."""


class AgentActivityNotFound(AgentActivityError):
    """No durable scratch claim matched the requested activity."""


class AgentActivityConflict(AgentActivityError):
    """A requested activity/publication collides with an existing claim."""


class AgentActivityChanged(AgentActivityError):
    """A source or sealed workspace changed at a protected boundary."""


class AgentActivityRecoveryRequired(AgentActivityError):
    """An effect happened but its durable confirmation still needs replay."""


class AgentActivityProcessError(AgentActivityError):
    """The deterministic external process returned a non-zero status."""


def _bounded_text(value: object, *, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    return value


def _canonical_activity_owner(value: object) -> str:
    """Validate the only owner currently wired to public maintenance.

    The activity facade must not advertise arbitrary owners while the public
    maintenance/``--all`` paths have a single canonical owner.  Keeping this
    check at the facade boundary is safer than accepting a claim that later
    becomes an ``owner_mismatch`` during cleanup.
    """

    owner = _bounded_text(value, label="activity owner", limit=_MAX_OWNER_BYTES)
    if owner != DEFAULT_AGENT_OWNER:
        raise AgentActivityConflict(
            f"activity owner is not registered for the public lifecycle: {owner}"
        )
    return owner


def _absolute_path(value: Path | str, *, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if "\x00" in str(path):
        raise ValueError(f"{label} contains NUL")
    return path


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    birthtime = getattr(metadata, "st_birthtime_ns", None)
    return int(metadata.st_dev), int(metadata.st_ino), int(birthtime) if birthtime is not None else -1


def _private_root(path: Path, *, create: bool) -> Path:
    """Create/validate one explicit private publication/state directory."""

    path = _absolute_path(path, label="root")
    if create:
        try:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise AgentActivityError(f"cannot create private root: {path}") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgentActivityError(f"private root is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentActivityError("private root must be a directory, not a symlink")
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise AgentActivityError("private root failed the owner/mode check")
    return path


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata = {} if value is None else dict(value)
    try:
        encoded = json.dumps(metadata, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("activity metadata must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("activity metadata exceeds the durable limit")
    return json.loads(encoded)


def _read_note(reason: str | None) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.startswith(_NOTE_PREFIX):
        return {}
    try:
        value = json.loads(reason[len(_NOTE_PREFIX) :])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) and value.get("schema") == _NOTE_SCHEMA else {}


def _encode_note(note: Mapping[str, Any]) -> str:
    payload = {"schema": _NOTE_SCHEMA, **dict(note)}
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    value = _NOTE_PREFIX + encoded
    if len(value.encode("utf-8")) > 8 * 1024:
        raise ValueError("activity lifecycle note exceeds the durable limit")
    return value


def _sha256_file(path: Path, *, max_bytes: int = _MAX_FILE_BYTES) -> tuple[str, int, tuple[int, int, int], int]:
    """Hash one regular file with no-follow and before/after identity checks."""

    try:
        initial = path.lstat()
    except OSError as exc:
        raise AgentActivityChanged(f"activity source is unavailable: {path}") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise AgentActivityChanged("activity source must be a regular file")
    if initial.st_uid != os.geteuid() or initial.st_nlink != 1:
        raise AgentActivityChanged("activity source failed owner/link checks")
    if initial.st_size > max_bytes:
        raise AgentActivityChanged("activity source exceeds the bounded publication size")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AgentActivityChanged("activity source could not be opened safely") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(fd)
        if _identity(opened) != _identity(initial) or opened.st_size != initial.st_size:
            raise AgentActivityChanged("activity source changed before publication")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            while True:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AgentActivityChanged("activity source exceeds the bounded publication size")
                digest.update(chunk)
        final = path.lstat()
    except AgentActivityChanged:
        raise
    except OSError as exc:
        raise AgentActivityChanged("activity source could not be read safely") from exc
    if _identity(final) != _identity(initial) or final.st_size != total:
        raise AgentActivityChanged("activity source changed during publication")
    return "sha256:" + digest.hexdigest(), total, _identity(initial), int(initial.st_mtime_ns)


def _sealed_workspace_digest(path: Path, *, profile: str = "strict") -> tuple[str, int, int]:
    """Use the scratch owner's canonical member/identity/content seal."""

    try:
        return _scratch_workspace_digest(path, max_file_bytes=_MAX_FILE_BYTES, profile=profile)
    except (ScratchSecurityError, OSError, UnicodeError) as exc:
        raise AgentActivityChanged("workspace could not be sealed") from exc


@dataclass(frozen=True, slots=True)
class PublishedDeliverable:
    """One no-replace publication result."""

    deliverable_id: str
    path: Path
    digest: str
    size_bytes: int
    status: Literal["published", "already_published"]
    artifact_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "deliverable_id": self.deliverable_id,
            "path": str(self.path),
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded result from the external deterministic producer."""

    pid: int
    returncode: int
    stdout: str
    stderr: str
    started_ns: int
    finished_ns: int
    tool_temp_coverage: str = "incomplete"
    process_scope_coverage: str = "posix-process-group"

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "started_ns": self.started_ns,
            "finished_ns": self.finished_ns,
            "tool_temp_coverage": self.tool_temp_coverage,
            "process_scope_coverage": self.process_scope_coverage,
        }


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    """Durable, bounded view used by CLI and external callers."""

    activity_id: str
    owner: str
    workspace_id: str
    workspace_path: Path
    state: str
    created_ns: int
    updated_ns: int
    run_id: int | str | None
    process_pid: int | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    publications: tuple[PublishedDeliverable, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in {"completed", "failed-retained", "recovery_required", "retired"}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": AGENT_ACTIVITY_SCHEMA,
            "activity_id": self.activity_id,
            "owner": self.owner,
            "workspace_id": self.workspace_id,
            "workspace_path": str(self.workspace_path),
            "state": self.state,
            "created_ns": self.created_ns,
            "updated_ns": self.updated_ns,
            "run_id": self.run_id,
            "process_pid": self.process_pid,
            "metadata": dict(self.metadata),
            "reason": self.reason,
            "publications": [item.to_dict() for item in self.publications],
        }


def _record_activity_id(record: ScratchRecord) -> str | None:
    value = record.metadata.get(_ACTIVITY_META_KEY)
    if not isinstance(value, Mapping):
        return None
    activity_id = value.get("activity_id")
    return activity_id if isinstance(activity_id, str) else None


def _record_activity_meta(record: ScratchRecord) -> Mapping[str, Any]:
    value = record.metadata.get(_ACTIVITY_META_KEY)
    return value if isinstance(value, Mapping) else {}


def _registry_records(registry: ArtifactRegistry) -> tuple[ArtifactRecord, ...]:
    """Normalize the registry's single-target/all-target verification union."""

    result = registry.verify(None)
    if not isinstance(result, tuple):
        return ()
    return tuple(item for item in result if isinstance(item, ArtifactRecord))


class AgentActivity:
    """Public composition of one external activity and existing owners."""

    def __init__(
        self,
        *,
        state_directory: Path,
        owner: str,
        manager: ScratchManager,
        registry: ArtifactRegistry,
        record: ScratchRecord,
    ) -> None:
        self.state_directory = _absolute_path(state_directory, label="state directory")
        self.owner = _bounded_text(owner, label="activity owner", limit=_MAX_OWNER_BYTES)
        self._manager = manager
        self._registry = registry
        self._record = record
        self._workspace = ScratchWorkspace(manager, record, retain_on_success=True)
        self._retired = record.state == ScratchState.RETIRED

    @property
    def activity_id(self) -> str:
        value = _record_activity_id(self._record)
        if value is None:
            raise AgentActivityError("scratch record is not an agent activity")
        return value

    @property
    def workspace_id(self) -> str:
        return self._record.record_id

    @property
    def path(self) -> Path:
        return self._record.path

    @property
    def state(self) -> str:
        if self._retired:
            return ScratchState.RETIRED.value
        value = self._refresh_record().state
        return value.value if isinstance(value, ScratchState) else str(value)

    @property
    def artifact_id(self) -> str:
        return self._record.artifact_id or f"scratch:{self.workspace_id}"

    @property
    def record(self) -> ScratchRecord:
        return self._refresh_record()

    @classmethod
    def prepare(
        cls,
        state_directory: Path | str,
        activity_id: str,
        *,
        owner: str = DEFAULT_AGENT_OWNER,
        run_id: int | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        process_pid: int | None = None,
        workspace_root: Path | str | None = None,
        payload_profile: str = "strict",
        fixture_creation_grant_id: str | None = None,
        fixture_authorized: bool = False,
    ) -> Self:
        """Create and register one private activity workspace."""

        state = _private_root(_absolute_path(state_directory, label="state directory"), create=True)
        normalized_id = _bounded_text(activity_id, label="activity_id", limit=_MAX_ACTIVITY_ID_BYTES)
        normalized_owner = _canonical_activity_owner(owner)
        if process_pid is not None and (
            type(process_pid) is not int or process_pid < 1
        ):
            raise ValueError("process_pid must be a positive integer or null")
        scratch_root = (state / "scratch" / _SCRATCH_SCOPE if workspace_root is None
                        else _private_root(_absolute_path(workspace_root, label="workspace root"), create=True))
        if state.is_relative_to(scratch_root) and state != scratch_root:
            raise AgentActivityConflict("activity workspace root cannot contain its durable state")
        registry_root = state / "artifacts"
        registry = ArtifactRegistry(registry_root, owner=normalized_owner, create_root=True)
        manager = ScratchManager(
            scratch_root,
            owner=normalized_owner,
            create_root=True,
            artifact_registry=registry,
        )
        existing = [item for item in manager.records() if _record_activity_id(item) == normalized_id]
        if existing:
            raise AgentActivityConflict(f"activity id is already registered: {normalized_id}")
        activity_meta: dict[str, Any] = {
            "schema": AGENT_ACTIVITY_SCHEMA,
            "activity_id": normalized_id,
            "owner": normalized_owner,
            "workspace_root": str(scratch_root),
        }
        if process_pid is not None:
            activity_meta["process_pid"] = process_pid
        user_metadata = _safe_metadata(metadata)
        reserved = {"schema", "activity_id", "owner", "process_pid", "workspace_root"}
        if reserved.intersection(user_metadata):
            raise ValueError("activity metadata contains reserved lifecycle keys")
        activity_meta.update(user_metadata)
        fixture_grant = None
        if payload_profile != "strict":
            if payload_profile != "fixture_posix_v1" or fixture_creation_grant_id is None:
                raise AgentActivityConflict("fixture profile requires its explicit creation grant")
            fixture_grant = manager.issue_fixture_grant(activity_id=normalized_id,
                                creation_grant_id=fixture_creation_grant_id, authorized=fixture_authorized)
        workspace = manager.create(
            run_id=run_id,
            retain_on_success=True,
            metadata={_ACTIVITY_META_KEY: activity_meta},
            payload_profile=payload_profile,
            fixture_grant=fixture_grant,
        )
        return cls(
            state_directory=state,
            owner=normalized_owner,
            manager=manager,
            registry=registry,
            record=workspace.record,
        )

    @classmethod
    def resume(
        cls,
        state_directory: Path | str,
        activity_id: str,
        *,
        owner: str = DEFAULT_AGENT_OWNER,
    ) -> Self:
        """Reopen one durable activity without the original Python object."""

        state = _absolute_path(state_directory, label="state directory")
        normalized_id = _bounded_text(activity_id, label="activity_id", limit=_MAX_ACTIVITY_ID_BYTES)
        normalized_owner = _canonical_activity_owner(owner)
        registry = ArtifactRegistry(state / "artifacts", owner=normalized_owner, create_root=False)
        workspace_root = state / "scratch" / _SCRATCH_SCOPE
        registered = [item for item in _registry_records(registry)
                      if item.owner == normalized_owner and item.artifact_id.startswith("scratch:")
                      and isinstance(item.metadata.get(_ACTIVITY_META_KEY), Mapping)
                      and item.metadata[_ACTIVITY_META_KEY].get("activity_id") == normalized_id]
        if len(registered) > 1:
            raise AgentActivityConflict(f"activity id has multiple durable claims: {normalized_id}")
        if registered:
            root_value = registered[0].metadata[_ACTIVITY_META_KEY].get("workspace_root")
            if root_value is not None:
                claimed_root = _absolute_path(root_value, label="activity workspace root")
                if registered[0].path.parent != claimed_root:
                    raise AgentActivityConflict("activity workspace root does not match producer claim")
                workspace_root = claimed_root
        manager = ScratchManager(
            workspace_root,
            owner=normalized_owner,
            create_root=False,
            artifact_registry=registry,
        )
        records = [
            item
            for item in manager.records()
            if item.owner == normalized_owner and _record_activity_id(item) == normalized_id
        ]
        if not records:
            # A retired scratch workspace has intentionally disappeared.  Its
            # registry tombstone remains the durable terminal evidence, so a
            # fresh process can still inspect/replay the activity without
            # recreating the deleted directory.
            try:
                registry_records = _registry_records(registry)
            except (ArtifactRegistryError, OSError):
                registry_records = ()
            retired = [
                item
                for item in registry_records
                if isinstance(item, ArtifactRecord)
                and item.state == "retired"
                and item.artifact_id.startswith("scratch:")
                and isinstance(item.metadata.get(_ACTIVITY_META_KEY), Mapping)
                and item.metadata[_ACTIVITY_META_KEY].get("activity_id") == normalized_id
                and item.owner == normalized_owner
            ]
            if len(retired) == 1:
                tombstone = retired[0]
                activity_meta = dict(tombstone.metadata.get(_ACTIVITY_META_KEY, {}))
                synthetic = ScratchRecord(
                    record_id=tombstone.artifact_id.removeprefix("scratch:"),
                    owner=tombstone.owner,
                    run_id=tombstone.run_id,
                    path=tombstone.path,
                    state=ScratchState.RETIRED,
                    created_ns=tombstone.created_ns,
                    updated_ns=tombstone.updated_ns,
                    path_identity=tombstone.path_identity,
                    root_identity=tombstone.root_identity,
                    size_bytes=0,
                    retain_on_success=False,
                    retire_after_ns=None,
                    metadata={_ACTIVITY_META_KEY: activity_meta},
                    reason="retired tombstone",
                    artifact_id=tombstone.artifact_id,
                    eligible=False,
                    valid=True,
                )
                return cls(
                    state_directory=state,
                    owner=normalized_owner,
                    manager=manager,
                    registry=registry,
                    record=synthetic,
                )
            raise AgentActivityNotFound(f"activity id is not present: {normalized_id}")
        if len(records) > 1:
            raise AgentActivityConflict(f"activity id has multiple durable claims: {normalized_id}")
        return cls(
            state_directory=state,
            owner=normalized_owner,
            manager=manager,
            registry=registry,
            record=records[0],
        )

    def _refresh_record(self) -> ScratchRecord:
        records = [item for item in self._manager.records() if item.record_id == self._record.record_id]
        if not records:
            # A retired workspace is intentionally no longer openable.  Keep
            # the previous record for a useful terminal error rather than
            # silently treating a missing path as a fresh activity.
            return self._record
        self._record = records[0]
        self._workspace = ScratchWorkspace(self._manager, self._record, retain_on_success=True)
        return self._record

    def snapshot(self) -> ActivitySnapshot:
        record = self._refresh_record()
        activity = _record_activity_meta(record)
        note = _read_note(record.reason)
        process_pid = note.get("process_pid", activity.get("process_pid"))
        if not isinstance(process_pid, int):
            process_pid = None
        publications: list[PublishedDeliverable] = []
        publication = note.get("publication")
        if isinstance(publication, Mapping):
            candidate = self._publication_from_intent(publication)
            if candidate is not None:
                publications.append(candidate)
        if not publications:
            # Terminal tombstones do not retain the scratch reason.  The
            # published ArtifactRegistry records do, so recover their exact
            # claims without inventing a second activity log.
            try:
                registry_records = _registry_records(self._registry)
            except (ArtifactRegistryError, OSError):
                registry_records = ()
            for item in registry_records:
                if not isinstance(item, ArtifactRecord) or item.state != "completed" or not item.verified:
                    continue
                source_ref = item.source_ref
                if not isinstance(source_ref, Mapping) or source_ref.get("activity_id") != self.activity_id:
                    continue
                if not isinstance(item.digest, str):
                    continue
                try:
                    observed_digest, observed_size, _identity_value, _mtime = _sha256_file(item.path)
                except AgentActivityError:
                    continue
                if observed_digest == item.digest:
                    publications.append(
                        PublishedDeliverable(
                            item.artifact_id,
                            item.path,
                            item.digest,
                            observed_size,
                            "already_published",
                            item.artifact_id,
                        )
                    )
        return ActivitySnapshot(
            activity_id=self.activity_id,
            owner=record.owner,
            workspace_id=record.record_id,
            workspace_path=record.path,
            state=(
                "retired"
                if self._retired
                else record.state.value
                if isinstance(record.state, ScratchState)
                else str(record.state)
            ),
            created_ns=record.created_ns,
            updated_ns=record.updated_ns,
            run_id=record.run_id,
            process_pid=process_pid,
            metadata=dict(activity),
            reason=record.reason,
            publications=tuple(publications),
        )

    def associate_process(
        self, pid: int, *, start_ticks: int | None = None
    ) -> ActivitySnapshot:
        """Persist a bounded process claim in the existing scratch manifest."""

        if type(pid) is not int or pid < 1:
            raise ValueError("process pid must be a positive integer")
        if start_ticks is not None and (type(start_ticks) is not int or start_ticks < 0):
            raise ValueError("process start ticks must be a non-negative integer or null")
        record = self._refresh_record()
        if record.state not in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            raise AgentActivityError("process can only be associated with an open activity")
        note = _read_note(record.reason)
        note["process_pid"] = pid
        note["process_start_ticks"] = start_ticks
        note["process_status"] = "running"
        # A new producer cannot inherit a previous producer's completion proof.
        note.pop("process_scope_receipt", None)
        note.pop("process_scope_coverage", None)
        note.pop("process_scope", None)
        updated = self._manager._update_state(  # owner lifecycle write; no new storage is introduced
            record.path,
            record.record_id,
            ScratchState(record.state),
            reason=_encode_note(note),
        )
        self._record = updated
        self._workspace = ScratchWorkspace(self._manager, updated, retain_on_success=True)
        return self.snapshot()

    def run(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
        temporary_directory_contract: Literal["environment", "explicit-dir", "unknown"] = "unknown",
        delegated_cgroup_root: Path | str | None = None,
    ) -> ProcessResult:
        """Run one argv-only external producer inside the private workspace."""

        if isinstance(command, (str, bytes)) or not command or any(
            not isinstance(item, str) or not item for item in command
        ):
            raise ValueError("command must be a non-empty argv sequence")
        if timeout is not None and (isinstance(timeout, bool) or timeout <= 0):
            raise ValueError("timeout must be positive or null")
        if temporary_directory_contract not in {"environment", "explicit-dir", "unknown"}:
            raise ValueError("unsupported temporary directory contract")
        self._require_open()
        # Persist the launch obligation before Popen: a failed PID publication
        # must not make a possibly running producer look like a manual activity.
        launch_note = _read_note(self._refresh_record().reason)
        launch_note["process_status"] = "starting"
        launch_note["process_scope_coverage"] = "pending"
        launch_note.pop("process_scope_receipt", None)
        self._set_note(launch_note)
        started = time.time_ns()
        child_env = os.environ.copy()
        child_env.update(
            {
                "NEOCORTEX_ACTIVITY_ID": self.activity_id,
                "NEOCORTEX_ACTIVITY_WORKSPACE": str(self.path),
            }
        )
        if env is not None:
            for key, value in env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("process environment keys and values must be strings")
            child_env.update(env)
        # Activity-owned temporary storage cannot be redirected by ambient
        # desktop variables or a caller override. Tools with explicit dir=
        # parameters use NEOCORTEX_ACTIVITY_WORKSPACE as their same context.
        child_env.update({key: str(self.path) for key in ("TMPDIR", "TMP", "TEMP")})
        process_pid: int | None = None

        def register_started(pid: int, start_ticks: int | None) -> None:
            nonlocal process_pid
            process_pid = pid
            self.associate_process(pid, start_ticks=start_ticks)

        process_scope = None
        def persist_scope(receipt: Mapping[str, Any]) -> None:
            note = _read_note(self._refresh_record().reason)
            note["process_scope_receipt"] = dict(receipt)
            self._set_note(note)

        try:
            if delegated_cgroup_root is not None:
                from neocortex.runtime.control.process_scope import DelegatedProcessScope
                process_scope = DelegatedProcessScope(Path(delegated_cgroup_root), self.activity_id,
                                                       receipt_writer=persist_scope)
            captured = run_bounded_capture(
                command,
                cwd=self.path,
                environment=child_env,
                timeout_seconds=timeout,
                stdout_limit_bytes=_MAX_OUTPUT_BYTES,
                stderr_limit_bytes=_MAX_OUTPUT_BYTES,
                on_started=register_started,
                process_scope=process_scope,
            )
        except subprocess.TimeoutExpired as exc:
            self._mark_failed("external process timed out")
            raise AgentActivityProcessError("external process timed out") from exc
        except OSError as exc:
            self._mark_failed(f"external process failed: {type(exc).__name__}")
            raise AgentActivityProcessError("external process could not start or complete safely") from exc
        except SubprocessOutputLimitError as exc:
            self._mark_failed("external process output limit exceeded")
            raise AgentActivityProcessError("external process output limit exceeded") from exc
        except BaseException as exc:
            # Capture has already attempted bounded group cleanup. Preserve
            # an uncertain activity; never reinterpret a failed claim write
            # or cancellation as successful process completion.
            try:
                self._mark_failed(f"external process failed: {type(exc).__name__}")
            except BaseException as lifecycle_error:
                exc.add_note(f"activity failure could not be persisted: {type(lifecycle_error).__name__}")
            raise
        finally:
            if process_scope is not None:
                try:
                    process_scope.close()
                except BaseException:
                    self._mark_failed("process scope cleanup could not be verified")
                    raise
        if process_pid is None:
            self._mark_failed("external process has no durable process identity")
            raise AgentActivityRecoveryRequired("external process identity is unavailable")
        finished = time.time_ns()
        result = ProcessResult(
            pid=process_pid,
            returncode=int(captured.returncode),
            stdout=captured.stdout.decode("utf-8", errors="surrogateescape"),
            stderr=captured.stderr.decode("utf-8", errors="surrogateescape"),
            started_ns=started,
            finished_ns=finished,
            tool_temp_coverage=("incomplete" if temporary_directory_contract == "unknown"
                                else "declared_" + temporary_directory_contract),
            process_scope_coverage=("cgroup-v2" if process_scope is not None and process_scope.quiescent
                                    else "incomplete_for_detached_descendants"),
        )
        note = _read_note(self._refresh_record().reason)
        note["process_status"] = ("exited" if result.process_scope_coverage == "cgroup-v2"
                                  else "cleanup_unverified")
        note["process_scope"] = "cgroup-v2" if process_scope is not None else "posix-process-group"
        note["process_scope_coverage"] = result.process_scope_coverage
        note["tool_temp_coverage"] = result.tool_temp_coverage
        self._set_note(note)
        if result.returncode != 0:
            self._mark_failed(f"external process returned {result.returncode}")
            if check:
                raise AgentActivityProcessError(f"external process returned {result.returncode}")
        return result

    def _require_open(self) -> ScratchRecord:
        record = self._refresh_record()
        if record.state not in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            raise AgentActivityError(f"activity is not open: {record.state}")
        if not record.path.is_dir():
            raise AgentActivityRecoveryRequired("activity workspace is missing")
        return record

    def _mark_failed(self, reason: str) -> None:
        record = self._refresh_record()
        if record.state in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            note = _read_note(record.reason)
            note["failure"] = reason
            if note.get("process_status") == "running":
                note["process_status"] = "cleanup_unverified"
            self._workspace.fail(_encode_note(note))
            self._refresh_record()

    def _set_note(self, note: Mapping[str, Any]) -> ScratchRecord:
        record = self._require_open()
        updated = self._manager._update_state(
            record.path,
            record.record_id,
            ScratchState(record.state),
            reason=_encode_note(note),
        )
        self._record = updated
        self._workspace = ScratchWorkspace(self._manager, updated, retain_on_success=True)
        return updated

    def publish(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        deliverable_id: str | None = None,
    ) -> PublishedDeliverable:
        """Publish one file outside scratch without replacing an existing path."""

        record = self._refresh_record()
        if record.state not in {ScratchState.ACTIVE, ScratchState.COMMITTING, ScratchState.COMPLETED}:
            raise AgentActivityError(f"activity is not publishable: {record.state}")
        if not record.path.is_dir():
            raise AgentActivityRecoveryRequired("activity workspace is missing")
        source_path = _absolute_path(source, label="publication source")
        source_resolved = source_path.resolve(strict=True)
        workspace_resolved = record.path.resolve(strict=True)
        if not _within(source_resolved, workspace_resolved):
            raise AgentActivityConflict("publication source must remain inside the activity workspace")
        digest, size, _identity_value, _mtime = _sha256_file(source_path)
        if isinstance(destination, str) and not Path(destination).is_absolute():
            # A relative destination is interpreted below the explicitly
            # supplied publication root only when the caller passes the root
            # as the parent in the Path spelling (``root/name``).  Rejecting a
            # bare relative path avoids accidental cwd publication.
            raise ValueError("publication destination must be absolute")
        destination_path = _absolute_path(destination, label="publication destination")
        destination_root = destination_path.parent
        _private_root(destination_root, create=True)
        destination_path = destination_path.resolve(strict=False)
        if not _within(destination_path, destination_root.resolve(strict=True)):
            raise AgentActivityConflict("publication destination escaped its private root")
        normalized_id = deliverable_id or f"activity-{self.activity_id}-{destination_path.name}"
        normalized_id = _bounded_text(normalized_id, label="deliverable_id", limit=256)
        artifact_id = normalized_id
        source_ref = {
            "schema": AGENT_ACTIVITY_SCHEMA,
            "activity_id": self.activity_id,
            "workspace_id": record.record_id,
            "source": str(source_path),
            "source_digest": digest,
        }
        intent: dict[str, Any] = {
            "deliverable_id": normalized_id,
            "artifact_id": artifact_id,
            "source": str(source_path),
            "destination": str(destination_path),
            "digest": digest,
            "size_bytes": size,
            "source_ref": source_ref,
        }
        note = _read_note(record.reason)
        old_intent = note.get("publication")
        if old_intent is not None and old_intent != intent:
            raise AgentActivityConflict("activity has a different pending publication intent")
        destination_exists = destination_path.exists() or destination_path.is_symlink()
        if destination_exists and old_intent is None:
            # Do not leave an intent behind for a path that was already
            # present: matching bytes do not grant authority to adopt it.
            raise AgentActivityConflict("publication destination already exists without our registry claim")
        if old_intent is None:
            note["publication"] = intent
            self._set_note(note)
            record = self._refresh_record()
        existing = self._registry.verify(artifact_id)
        if isinstance(existing, ArtifactRecord) and existing.verified:
            if existing.path != destination_path or existing.digest != digest:
                raise AgentActivityConflict("deliverable id is bound to a different destination/content")
            observed_digest, observed_size, _identity_value, _mtime = _sha256_file(destination_path)
            if observed_digest != digest or observed_size != size:
                raise AgentActivityChanged("published deliverable changed before replay")
            if self.artifact_id in existing.dependencies:
                self._registry.update(existing, dependencies=tuple(d for d in existing.dependencies
                                                                  if d != self.artifact_id))
            return PublishedDeliverable(normalized_id, destination_path, digest, size, "already_published", artifact_id)
        effect_already_present = False
        if destination_exists:
            observed_digest, observed_size, _identity_value, _mtime = _sha256_file(destination_path)
            if observed_digest != digest or observed_size != size:
                raise AgentActivityConflict("pending publication destination has different content")
            effect_already_present = True
        if not effect_already_present:
            temp = destination_root / f".neocortex-agent-publish-{secrets.token_hex(16)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd: int | None = None
            try:
                fd = os.open(temp, flags, 0o600)
                with os.fdopen(fd, "wb", closefd=True) as target:
                    fd = None
                    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    source_fd = os.open(source_path, source_flags)
                    try:
                        with os.fdopen(source_fd, "rb", closefd=True) as stream:
                            while True:
                                chunk = stream.read(_CHUNK_BYTES)
                                if not chunk:
                                    break
                                target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    finally:
                        source_fd = -1
                os.link(temp, destination_path, follow_symlinks=False)
                temp.unlink(missing_ok=True)
                directory_fd = os.open(destination_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except FileExistsError as exc:
                raise AgentActivityConflict("publication destination appeared during no-replace publish") from exc
            except OSError as exc:
                raise AgentActivityRecoveryRequired("publication effect is incomplete and requires replay") from exc
            finally:
                if fd is not None:
                    os.close(fd)
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            self._registry.register(
                artifact_id,
                producer=self.owner,
                path=destination_path,
                root=destination_root,
                owner=self.owner,
                run_id=record.run_id,
                purpose="external agent deliverable",
                kind="canonical",
                state="completed",
                source_ref=source_ref,
                digest=digest,
                dependencies=(self.artifact_id,),
                disposable=False,
                metadata={"activity_id": self.activity_id, "workspace_id": record.record_id},
                created_ns=record.created_ns,
            )
        except ArtifactConflictError:
            # A concurrent/replayed writer may have registered the same exact
            # id.  Re-read it and accept only the exact path/content claim.
            existing = self._registry.verify(artifact_id)
            if not isinstance(existing, ArtifactRecord) or not existing.verified:
                raise AgentActivityRecoveryRequired("publication exists but registry confirmation is unavailable") from None
            if existing.path != destination_path or existing.digest != digest:
                raise AgentActivityConflict("deliverable registry claim conflicts with publication") from None
        except Exception as exc:
            raise AgentActivityRecoveryRequired("publication was written but registry confirmation failed") from exc
        # The final copy is durable and verified before its temporary input is
        # released. Canonical deliverables never become disposable work.
        observed_digest, observed_size, _identity_value, _mtime = _sha256_file(destination_path)
        if observed_digest != digest or observed_size != size:
            raise AgentActivityChanged("published deliverable changed before dependency release")
        published_record = self._registry.verify(artifact_id)
        if not isinstance(published_record, ArtifactRecord) or not published_record.verified:
            raise AgentActivityRecoveryRequired("publication claim must be verified before dependency release")
        self._registry.update(published_record, dependencies=tuple(d for d in published_record.dependencies
                                                                  if d != self.artifact_id))
        return PublishedDeliverable(normalized_id, destination_path, digest, size, "published", artifact_id)

    def _publication_from_intent(self, publication: Mapping[str, Any]) -> PublishedDeliverable | None:
        try:
            deliverable_id = _bounded_text(publication.get("deliverable_id"), label="deliverable_id", limit=256)
            destination = publication.get("destination")
            if not isinstance(destination, (Path, str)):
                return None
            path = _absolute_path(destination, label="publication destination")
            digest = _bounded_text(publication.get("digest"), label="publication digest", limit=256)
            raw_size = publication.get("size_bytes")
            if isinstance(raw_size, bool) or not isinstance(raw_size, (int, str)):
                return None
            size = int(raw_size)
        except (TypeError, ValueError):
            return None
        record = self._registry.verify(deliverable_id)
        if isinstance(record, ArtifactRecord) and record.verified and record.path == path and record.digest == digest:
            try:
                observed_digest, observed_size, _identity_value, _mtime = _sha256_file(path)
            except AgentActivityError:
                return None
            if observed_digest == digest and observed_size == size:
                return PublishedDeliverable(deliverable_id, path, digest, size, "already_published", deliverable_id)
        return None

    def close(self, result_paths: Iterable[Path | str] = ()) -> ActivitySnapshot:
        """Seal and complete the activity while retaining scratch for maintenance."""

        if self._record.payload_profile == "fixture_posix_v1":
            with self._manager.fixture_permissions(self.workspace_id, authorized=True):
                return self._close_with_access(result_paths)
        return self._close_with_access(result_paths)

    def _close_with_access(self, result_paths: Iterable[Path | str]) -> ActivitySnapshot:

        record = self._require_open()
        process_note = _read_note(record.reason)
        process_pid = process_note.get("process_pid", _record_activity_meta(record).get("process_pid"))
        if not verified_process_quiescence(process_note, activity_id=self.activity_id,
                                           process_pid=process_pid):
            raise AgentActivityRecoveryRequired(
                "activity process claim requires verified producer quiescence; "
                "run with an explicitly delegated cgroup v2 scope before sealing")
        pending_publication = _read_note(record.reason).get("publication")
        if isinstance(pending_publication, Mapping):
            source_value = pending_publication.get("source")
            expected_digest = pending_publication.get("digest")
            expected_size = pending_publication.get("size_bytes")
            if not isinstance(source_value, (str, Path)) or not isinstance(expected_digest, str):
                raise AgentActivityRecoveryRequired("publication intent is incomplete")
            if isinstance(expected_size, bool) or not isinstance(expected_size, int):
                raise AgentActivityRecoveryRequired("publication intent size is incomplete")
            observed_digest, observed_size, _identity_value, _mtime = _sha256_file(Path(source_value))
            if observed_digest != expected_digest or observed_size != expected_size:
                raise AgentActivityChanged("source changed after publication; close is blocked")
            destination_value = pending_publication.get("destination")
            if not isinstance(destination_value, (str, Path)):
                raise AgentActivityRecoveryRequired("publication destination is incomplete")
            confirmed = self._publication_from_intent(pending_publication)
            if confirmed is None:
                raise AgentActivityRecoveryRequired("publication requires registry reconciliation before close")
        seal_digest, members, apparent = _sealed_workspace_digest(record.path, profile=record.payload_profile)
        note = _read_note(record.reason)
        note["seal"] = {
            "digest": seal_digest,
            "members": members,
            "apparent_bytes": apparent,
        }
        self._set_note(note)
        record = self._refresh_record()
        paths = tuple(result_paths)
        try:
            completed = self._workspace.complete(paths, retain=True)
        except BaseException:
            # ScratchManager retains an active/committing claim on a failed
            # transition; a later process can resume/reconcile it.
            raise
        if completed is not None:
            self._record = completed
        return self.snapshot()

    def retire(self) -> ActivitySnapshot:
        """Retire only a sealed, completed workspace through its owner."""

        if self._record.payload_profile == "fixture_posix_v1":
            with self._manager.fixture_permissions(self.workspace_id, authorized=True):
                return self._retire_with_access()
        return self._retire_with_access()

    def _retire_with_access(self) -> ActivitySnapshot:

        record = self._refresh_record()
        if record.state != ScratchState.COMPLETED:
            raise AgentActivityError("only a completed activity can be retired")
        note = _read_note(record.reason)
        if not verified_process_quiescence(note, activity_id=self.activity_id,
                process_pid=_record_activity_meta(record).get("process_pid")):
            raise AgentActivityRecoveryRequired("retirement requires verified producer quiescence")
        seal = record.seal if isinstance(record.seal, Mapping) else note.get("seal")
        if not isinstance(seal, Mapping):
            raise AgentActivityChanged("completed activity has no durable seal")
        digest, members, apparent = _sealed_workspace_digest(record.path, profile=record.payload_profile)
        if digest != seal.get("digest") or members != seal.get("members") or apparent != seal.get("apparent_bytes"):
            raise AgentActivityChanged("workspace changed after close; retirement is blocked")
        self._workspace.retire()
        # The physical workspace is intentionally absent after retirement;
        # retain the old record only as a terminal return value.
        self._record = record
        self._retired = True
        return self.snapshot()

    def _terminal_registry_records(self) -> tuple[ArtifactRecord, ...]:
        """Return this activity's bounded registry projections only."""

        result: list[ArtifactRecord] = []
        for item in _registry_records(self._registry):
            activity = item.metadata.get(_ACTIVITY_META_KEY)
            source_ref = item.source_ref
            if (
                isinstance(activity, Mapping)
                and activity.get("activity_id") == self.activity_id
            ) or (
                isinstance(source_ref, Mapping)
                and source_ref.get("activity_id") == self.activity_id
            ):
                result.append(item)
        return tuple(result)

    def terminal_retention_plan(
        self,
        *,
        policy: "TerminalRetentionPolicy",
        now_ns: int | None = None,
        baseline_record_ids: Iterable[str] = (),
    ) -> "TerminalRetentionPlan":
        """Plan this activity's terminal registry evidence without effects.

        The policy is intentionally required.  A caller must choose the
        minimum age and category quotas explicitly; the planner still requires
        reconciliation, release authorization and absence of recovery/pins
        before an item can become eligible.
        """

        from neocortex.workflow.retention.planner import (
            TerminalRetentionRecord,
            plan_terminal_retention,
        )

        now = time.time_ns() if now_ns is None else now_ns
        records: list[TerminalRetentionRecord] = []
        for item in self._terminal_registry_records():
            metadata = item.metadata
            is_tombstone = item.state == "retired" and item.artifact_id.startswith("scratch:")
            records.append(
                TerminalRetentionRecord(
                    record_id=item.artifact_id,
                    status=item.state,
                    category="tombstone" if is_tombstone else "other",
                    terminal_ns=item.updated_ns if item.state in {"retired", "failed"} else None,
                    apparent_bytes=max(0, item.path_size_bytes or 0),
                    allocated_bytes=max(0, item.path_size_bytes or 0),
                    physical_identity=item.path_identity,
                    reconciled=is_tombstone,
                    recovery_required=item.state == "recovery_required",
                    replay_required=False,
                    pinned=metadata.get("pinned") is True,
                    grant_active=metadata.get("grant_active") is True,
                    authorization_active=metadata.get("authorization_active") is True,
                    release_authorized=is_tombstone,
                    evidence_required=metadata.get("evidence_required") is True,
                    tombstone=is_tombstone,
                    owner=item.owner,
                )
            )
        return plan_terminal_retention(
            records,
            now_ns=now,
            policy=policy,
            baseline_record_ids=tuple(baseline_record_ids),
        )

    def apply_terminal_retention(
        self,
        *,
        policy: "TerminalRetentionPolicy",
        now_ns: int | None = None,
        release_authorized: bool,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        """Apply only eligible tombstone-manifest retention for this activity."""

        if type(release_authorized) is not bool or not release_authorized:
            raise AgentActivityConflict("terminal retention requires explicit release authorization")
        plan = self.terminal_retention_plan(policy=policy, now_ns=now_ns)
        if getattr(plan, "status", None) != "ready" or getattr(plan, "truncated", False):
            raise AgentActivityRecoveryRequired("terminal retention plan is incomplete")
        eligible_ids = tuple(
            item.record.record_id
            for item in plan.eligible_items
            if item.record.category == "tombstone"
        )
        receipt = self._registry.apply_tombstone_retention(
            eligible_ids,
            release_authorized=True,
            evidence={
                "schema": AGENT_ACTIVITY_SCHEMA,
                "activity_id": self.activity_id,
                "plan_fingerprint": plan.fingerprint,
            },
            operation_id=operation_id,
        )
        return {
            "schema": AGENT_ACTIVITY_SCHEMA,
            "activity_id": self.activity_id,
            "plan": plan.to_dict(),
            "receipt": receipt,
        }

    def reconcile(
        self,
        action: Literal["resume", "publish", "complete", "retire", "fail", "release"] = "resume",
        *,
        source: Path | str | None = None,
        destination: Path | str | None = None,
        deliverable_id: str | None = None,
        result_paths: Iterable[Path | str] = (),
        reason: str = "external activity reconciled as failed",
        release_authorized: bool = False,
        evidence: Mapping[str, Any] | None = None,
    ) -> ActivitySnapshot | PublishedDeliverable:
        """Reconcile durable state from a fresh process, never by age alone."""

        if action == "resume":
            self._refresh_record()
            return self.snapshot()
        if action == "publish":
            record = self._refresh_record()
            note = _read_note(record.reason).get("publication")
            if source is None and isinstance(note, Mapping):
                source = note.get("source")
            if destination is None and isinstance(note, Mapping):
                destination = note.get("destination")
            if deliverable_id is None and isinstance(note, Mapping):
                value = note.get("deliverable_id")
                deliverable_id = value if isinstance(value, str) else None
            if source is None or destination is None:
                raise AgentActivityRecoveryRequired("publication intent is incomplete")
            return self.publish(source, destination, deliverable_id=deliverable_id)
        if action == "complete":
            return self.close(result_paths)
        if action == "retire":
            return self.retire()
        if action == "fail":
            self._mark_failed(reason)
            return self.snapshot()
        if action == "release":
            if type(release_authorized) is not bool or not release_authorized:
                raise AgentActivityConflict(
                    "terminal release requires explicit release authorization"
                )
            record = self._refresh_record()
            if record.state not in {
                ScratchState.FAILED_RETAINED,
                ScratchState.RECOVERY_REQUIRED,
            }:
                raise AgentActivityError(
                    f"activity is not awaiting terminal reconciliation: {record.state}"
                )
            process_note = _read_note(record.reason)
            process_pid = process_note.get("process_pid", _record_activity_meta(record).get("process_pid"))
            if not verified_process_quiescence(process_note, activity_id=self.activity_id,
                                               process_pid=process_pid):
                raise AgentActivityRecoveryRequired("terminal release requires verified producer quiescence")
            claims = dict(evidence or {})
            claims.setdefault("activity_id", self.activity_id)
            claims.setdefault("workspace_id", record.record_id)
            # Seal the failed payload before changing its state.  The owner
            # revalidates identity/content again during reconcile and during
            # the later retirement effect.
            seal_digest, members, apparent = _sealed_workspace_digest(record.path, profile=record.payload_profile)
            self._manager.seal_workspace(
                record.record_id,
                seal={
                    "schema": "neocortex.scratch-seal/v1",
                    "digest": seal_digest,
                    "members": members,
                    "apparent_bytes": apparent,
                },
            )
            reconciled = self._manager.reconcile_terminal(
                record.record_id,
                release_authorized=True,
                evidence=claims,
            )
            self._record = reconciled
            self._workspace = ScratchWorkspace(self._manager, reconciled, retain_on_success=True)
            return self.snapshot()
        raise ValueError(f"unsupported reconciliation action: {action}")
