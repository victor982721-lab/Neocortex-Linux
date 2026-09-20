"""Neutral physical mutation backends used by the framework action owner.

This module is deliberately below the authorization and review layers.  It
contains only the physical identity binding and the two Linux mutation seams
needed by the framework workflow: a descriptor-relative POSIX rename and the
prepared KIO Trash adapter.  Authorization grants, ReviewTasks, curation
plans, and their repositories remain owned by their respective callers.

The public backend contract is intentionally small.  A caller supplies an
``ApplyCandidate`` whose ``effect`` exposes the source snapshot, source
binding, action, and (for rename) target path.  Duck typing here is
intentional: the integrated framework action owner and the grant-bound
curation owner may use their own effect records without importing one another.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Protocol

from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
    FULL_ALGORITHM,
    files_equal_exact,
    full_fingerprint,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.safety.kio_trash import (
    KioTrashBatchItem,
    KioTrashResult,
    KioTrashService,
    KioTrashStatus,
    KioTrashVerification,
    is_metadata_binding,
    metadata_binding,
    trash_receipt_paths,
)
from neocortex.workflow.actions.action_policy import validate_mutation_path
from neocortex.workflow.actions.file_action_recovery import effect_receipt_json


ApplyStatus = Literal["applied", "blocked", "recovery_required"]


class _BackendOutcomeMeta(type):
    """Keep old fixture outcomes consumable without importing curation.

    The framework action owner historically accepted the structurally
    identical ``curation.application.BackendOutcome``.  A few external
    fixture seams still return that value while the import boundary moves to
    this neutral owner.  Recognizing only that exact legacy class by module
    and name preserves the transition without importing or depending on the
    curation package.
    """

    def __instancecheck__(cls, value: object) -> bool:
        if type.__instancecheck__(cls, value):
            return True
        value_type = type(value)
        return (
            value_type.__module__ == "neocortex.curation.application"
            and value_type.__name__ == "BackendOutcome"
            and getattr(value, "status", None)
            in {"applied", "blocked", "recovery_required"}
            and isinstance(getattr(value, "reason", None), str)
        )


@dataclass(frozen=True, slots=True)
class BackendOutcome(metaclass=_BackendOutcomeMeta):
    """Typed result returned by an injected physical backend."""

    status: ApplyStatus
    reason: str
    detail: str | None = None
    receipt_json: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in {
            "applied",
            "blocked",
            "recovery_required",
        }:
            raise ValueError("unsupported backend outcome status")
        if (
            not isinstance(self.reason, str)
            or not self.reason
            or self.reason.strip() != self.reason
        ):
            raise ValueError("backend outcome reason must be non-empty and trimmed")
        if len(self.reason.encode("utf-8")) > 512:
            raise ValueError("backend outcome reason is too long")
        if self.detail is not None and (
            not isinstance(self.detail, str) or len(self.detail.encode("utf-8")) > 4_096
        ):
            raise ValueError("backend outcome detail is too long")
        if self.receipt_json is not None and (
            not isinstance(self.receipt_json, str)
            or len(self.receipt_json.encode("utf-8")) > 65_536
        ):
            raise ValueError("backend outcome receipt is too long")
        if self.status == "applied" and not self.receipt_json:
            raise ValueError("applied backend outcome requires a receipt")


@dataclass(frozen=True, slots=True)
class ApplyCandidate:
    """One already-admitted effect plus its enclosing mutation root.

    ``effect`` is intentionally an opaque record.  The physical backends
    inspect only the small public attributes documented by their callers and
    do not import authorization or review contracts.
    """

    grant_id: str
    grant_digest: str
    root: Path
    effect: object


class MutationBackend(Protocol):
    """Narrow backend seam used by action owners and fixture tests."""

    name: str

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        """Perform one admitted effect and return typed physical evidence."""

        ...


class MutationError(RuntimeError):
    """A physical preflight or observation failed before a mutation."""


class MutationSnapshotChanged(MutationError):
    """The admitted source or destination identity no longer matches."""


class MutationUnavailable(MutationError):
    """A required local physical primitive is unavailable."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _same_snapshot(left: FileSnapshot, right: FileSnapshot) -> bool:
    return (
        left.path == right.path
        and left.identity == right.identity
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
        and left.birthtime_ns == right.birthtime_ns
    )


def _digest_snapshot(snapshot: FileSnapshot) -> str:
    try:
        current = snapshot_path(snapshot.path)
        if not _same_snapshot(current, snapshot):
            raise MutationSnapshotChanged(f"source snapshot changed: {snapshot.path}")
        digest = full_fingerprint(current)
    except MutationSnapshotChanged:
        raise
    except FileChangedError as exc:
        raise MutationSnapshotChanged(
            f"source changed while hashing: {snapshot.path}"
        ) from exc
    except OSError as exc:
        raise MutationUnavailable(f"source cannot be hashed: {snapshot.path}") from exc
    return f"{FULL_ALGORITHM}:" + digest.hex()


def _binding_snapshot(snapshot: FileSnapshot, binding: str) -> bool:
    """Verify a source binding without hashing metadata-only effects."""

    if is_metadata_binding(binding):
        return metadata_binding(snapshot) == binding
    return _digest_snapshot(snapshot) == binding


def _validate_regular_unique(snapshot: FileSnapshot, *, role: str) -> FileSnapshot:
    path = Path(snapshot.path)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise MutationSnapshotChanged(f"{role} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise MutationError(f"{role} is a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise MutationError(f"{role} is not a regular file")
    if metadata.st_nlink != 1:
        raise MutationError(f"{role} has additional hard links")
    try:
        current = snapshot_path(path)
    except OSError as exc:
        raise MutationSnapshotChanged(f"{role} cannot be snapshotted") from exc
    if not _same_snapshot(current, snapshot) or not stat_matches_snapshot(snapshot, metadata):
        raise MutationSnapshotChanged(f"{role} changed: {path}")
    return current


def _validate_effect_paths(root: Path, effect: object) -> None:
    """Validate only source/keeper/target lexical and physical containment."""

    source = getattr(effect, "source", None)
    if not isinstance(source, FileSnapshot):
        raise MutationError("mutation source snapshot is missing")
    try:
        validate_mutation_path(root, source.path, role="mutation source")
        keeper = getattr(effect, "keeper", None)
        if keeper is not None:
            if not isinstance(keeper, FileSnapshot):
                raise MutationError("mutation keeper snapshot is invalid")
            validate_mutation_path(root, keeper.path, role="mutation keeper")
        target_path = getattr(effect, "target_path", None)
        if target_path is not None:
            validate_mutation_path(
                root,
                target_path,
                role="mutation target",
                allow_missing_leaf=True,
            )
    except (OSError, RuntimeError) as exc:
        raise MutationError(str(exc)) from exc


def _validate_effect_physical(effect: object, root: Path) -> None:
    """Revalidate the admitted identity immediately before a KIO effect."""

    _validate_effect_paths(root, effect)
    source = getattr(effect, "source", None)
    source_digest = getattr(effect, "source_digest", None)
    if not isinstance(source, FileSnapshot) or not isinstance(source_digest, str):
        raise MutationError("mutation source binding is missing")
    source = _validate_regular_unique(source, role="mutation source")
    if is_metadata_binding(source_digest):
        if metadata_binding(source) != source_digest:
            raise MutationSnapshotChanged("mutation source metadata binding changed")
    elif _digest_snapshot(source) != source_digest:
        raise MutationSnapshotChanged("mutation source digest changed")

    keeper = getattr(effect, "keeper", None)
    if keeper is not None:
        keeper_digest = getattr(effect, "keeper_digest", None)
        if not isinstance(keeper, FileSnapshot):
            raise MutationError("mutation keeper snapshot is invalid")
        keeper = _validate_regular_unique(keeper, role="mutation keeper")
        if not isinstance(keeper_digest, str) or _digest_snapshot(keeper) != keeper_digest:
            raise MutationSnapshotChanged("mutation keeper digest changed")
        try:
            if not files_equal_exact(source, keeper):
                raise MutationSnapshotChanged(
                    "mutation source is no longer byte-identical to its keeper"
                )
        except FileChangedError as exc:
            raise MutationSnapshotChanged(
                "mutation source or keeper changed during exact comparison"
            ) from exc

    target_path = getattr(effect, "target_path", None)
    if target_path is not None:
        target = Path(target_path)
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise MutationError("mutation target cannot be inspected") from exc
        if metadata is not None:
            raise MutationError("mutation target already exists")
        try:
            parent_device = os.stat(target.parent, follow_symlinks=False).st_dev
        except OSError as exc:
            raise MutationError("mutation target parent is unavailable") from exc
        if parent_device != source.volume_id:
            raise MutationError("mutation target is on another filesystem")


class PosixRenameBackend:
    """Same-filesystem, no-replace rename backend for contained fixtures.

    Both parent directories are opened from an ``O_NOFOLLOW`` root descriptor
    and the source is opened with ``O_NOFOLLOW`` before the libc
    ``renameat2(RENAME_NOREPLACE)`` call.  If the primitive is unavailable the
    backend abstains; it never falls back to copy/delete or an overwriting
    rename.
    """

    name = "posix-link-unlink-no-replace-v1"

    def apply(
        self,
        candidate: ApplyCandidate,
        *,
        before_syscall: Callable[[], None] | None = None,
    ) -> BackendOutcome:
        effect = candidate.effect
        action = getattr(effect, "action", None)
        target_path = getattr(effect, "target_path", None)
        source_snapshot = getattr(effect, "source", None)
        source_digest = getattr(effect, "source_digest", None)
        if (
            action not in {"move", "rename"}
            or target_path is None
            or not isinstance(source_snapshot, FileSnapshot)
            or not isinstance(source_digest, str)
        ):
            return BackendOutcome("blocked", "rename_action_requires_target")
        source = Path(source_snapshot.path)
        target = Path(target_path)
        root = Path(candidate.root)
        root_fd: int | None = None
        source_parent_fd: int | None = None
        target_parent_fd: int | None = None
        source_fd: int | None = None
        effect_crossed = False
        try:
            _validate_effect_paths(root, effect)
            if os.path.lexists(target):
                return BackendOutcome("blocked", "destination_exists")
            _validate_effect_physical(effect, root)
            if os.stat(target.parent, follow_symlinks=False).st_dev != source_snapshot.volume_id:
                return BackendOutcome("blocked", "exdev_same_filesystem_required")
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            root_fd = os.open(root, flags)
            source_parts = source.relative_to(root).parts
            target_parts = target.relative_to(root).parts
            source_parent_fd, source_name = _open_parent_dirfd(root_fd, source_parts)
            target_parent_fd, target_name = _open_parent_dirfd(root_fd, target_parts)
            source_fd = os.open(
                source_name,
                getattr(os, "O_PATH", os.O_RDONLY)
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=source_parent_fd,
            )
            source_metadata = os.fstat(source_fd)
            if (
                stat.S_ISLNK(source_metadata.st_mode)
                or not stat.S_ISREG(source_metadata.st_mode)
                or source_metadata.st_nlink != 1
                or not stat_matches_snapshot(source_snapshot, source_metadata)
            ):
                raise MutationSnapshotChanged("rename source changed before syscall")
            current_metadata = os.lstat(source)
            if (
                stat.S_ISLNK(current_metadata.st_mode)
                or not stat.S_ISREG(current_metadata.st_mode)
                or current_metadata.st_nlink != 1
                or not stat_matches_snapshot(source_snapshot, current_metadata)
                or (current_metadata.st_dev, current_metadata.st_ino)
                != (source_metadata.st_dev, source_metadata.st_ino)
            ):
                raise MutationSnapshotChanged(
                    "rename source changed immediately before syscall"
                )
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                return BackendOutcome("blocked", "renameat2_unavailable")
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            if before_syscall is not None:
                before_syscall()
            effect_crossed = True
            if (
                renameat2(
                    source_parent_fd,
                    os.fsencode(source_name),
                    target_parent_fd,
                    os.fsencode(target_name),
                    1,
                )
                != 0
            ):
                effect_crossed = False
                error_number = ctypes.get_errno()
                if error_number == errno.EEXIST:
                    return BackendOutcome(
                        "recovery_required" if before_syscall is not None else "blocked",
                        "destination_exists",
                    )
                if error_number == errno.EXDEV:
                    return BackendOutcome(
                        "recovery_required" if before_syscall is not None else "blocked",
                        "exdev_same_filesystem_required",
                    )
                return BackendOutcome(
                    "recovery_required" if before_syscall is not None else "blocked",
                    "rename_syscall_failed",
                    os.strerror(error_number),
                )
            for descriptor in dict.fromkeys(
                descriptor
                for descriptor in (source_parent_fd, target_parent_fd)
                if descriptor is not None
            ):
                os.fsync(descriptor)
        except MutationError as exc:
            if effect_crossed:
                return BackendOutcome("recovery_required", "rename_effect_ambiguous", str(exc))
            return BackendOutcome("blocked", "rename_preflight_failed", str(exc))
        except FileExistsError:
            if effect_crossed:
                return BackendOutcome("recovery_required", "rename_effect_ambiguous")
            return BackendOutcome("blocked", "destination_exists")
        except OSError as exc:
            if effect_crossed:
                return BackendOutcome("recovery_required", "rename_effect_ambiguous", str(exc))
            if exc.errno == errno.EXDEV:
                return BackendOutcome("blocked", "exdev_same_filesystem_required")
            return BackendOutcome(
                "blocked", "rename_preflight_failed", f"{type(exc).__name__}: {exc}"
            )
        except BaseException as exc:
            if effect_crossed:
                return BackendOutcome(
                    "recovery_required", "rename_effect_ambiguous", type(exc).__name__
                )
            return BackendOutcome(
                "blocked", "rename_interrupted_before_effect", type(exc).__name__
            )
        finally:
            for fd in (source_fd, source_parent_fd, target_parent_fd, root_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        try:
            if os.path.lexists(source):
                raise MutationError("rename source remains present")
            target_snapshot = _validate_regular_unique(
                FileSnapshot(
                    str(target),
                    source_snapshot.volume_id,
                    source_snapshot.file_id,
                    source_snapshot.size,
                    source_snapshot.mtime_ns,
                    source_snapshot.birthtime_ns,
                ),
                role="rename target",
            )
            # A metadata binding is deliberately verified as metadata only;
            # extension normalization must not introduce a full content hash.
            if not _binding_snapshot(target_snapshot, source_digest):
                raise MutationError("rename target digest mismatch")
            receipt = effect_receipt_json(
                operation=action,
                source_path=source_snapshot.path,
                target_path=str(target_path),
                target_snapshot=target_snapshot,
            )
            payload = json.loads(receipt)
            payload.update(
                {
                    "backend": self.name,
                    "source_digest": source_digest,
                    "target_digest": source_digest,
                }
            )
            return BackendOutcome(
                "applied", "rename_verified", receipt_json=_canonical_json(payload)
            )
        except BaseException as exc:
            return BackendOutcome("recovery_required", "rename_effect_unverified", str(exc))


def _open_parent_dirfd(root_fd: int, parts: tuple[str, ...]) -> tuple[int, str]:
    """Open a descendant parent without following any intermediate symlink."""

    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise MutationError("rename path has no safe relative leaf")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


class KioTrashBackend:
    """Prepared KIO Trash adapter with a fixture-injectable runner.

    The safety service owns claims, private KDE context, process invocation,
    Trash verification, receipts, and recovery classification.  This adapter
    only binds that evidence to the caller's source snapshot and returns the
    neutral ``BackendOutcome`` contract.
    """

    name = "kio-trash-path-bound-v1"

    def __init__(
        self,
        *,
        verifier: Callable[[Path, FileSnapshot, Path], KioTrashVerification] | None = None,
        runner: Callable[..., object] | None = None,
        which: Callable[[str], str | None] | None = None,
        environment: Mapping[str, str] | None = None,
        home_directory: Path | None = None,
        timeout_seconds: float = 120.0,
        private_claim: bool = True,
        private_config: bool = True,
        private_bus: bool = True,
    ) -> None:
        self._service = KioTrashService(
            verifier=verifier,
            runner=runner,  # type: ignore[arg-type]
            which=which,
            environment=environment,
            home_directory=home_directory,
            timeout_seconds=timeout_seconds,
            private_claim=private_claim,
            private_config=private_config,
            private_bus=private_bus,
        )
        self._revalidate_native = runner is None and private_claim

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        effect = candidate.effect
        if getattr(effect, "action", None) != "trash":
            return BackendOutcome("blocked", "kio_backend_supports_trash_only")
        if not hasattr(effect, "source") or not hasattr(candidate, "root"):
            return BackendOutcome("blocked", "kio_runner_not_injected")

        if self._revalidate_native:
            try:
                _validate_effect_physical(effect, Path(candidate.root))
            except (MutationError, OSError, RuntimeError, ValueError) as exc:
                return BackendOutcome("blocked", "kio_preflight_failed", str(exc))
        result = self._service.move(
            effect.source,
            source_digest=effect.source_digest,
        )
        return self._backend_outcome(effect, result)

    def _backend_outcome(self, effect: object, result: KioTrashResult) -> BackendOutcome:
        """Bind safety-owned physical evidence to the action receipt."""

        if result.status is KioTrashStatus.BLOCKED:
            return BackendOutcome("blocked", result.reason, result.detail)
        if result.status is KioTrashStatus.RECOVERY_REQUIRED:
            return BackendOutcome("recovery_required", result.reason, result.detail)
        if result.receipt is None:
            return BackendOutcome(
                "recovery_required",
                "kio_receipt_missing",
                "KIO reported an applied item without a receipt",
            )
        try:
            source = effect.source
            source_digest = effect.source_digest
            evidence = json.loads(result.receipt.trash_evidence)
            trash_receipt_paths(evidence, source, source_digest)
            receipt = effect_receipt_json(
                operation="trash",
                source_path=source.path,
                target_path=None,
            )
            payload = json.loads(receipt)
            payload.update(
                {
                    "backend": self.name,
                    "source_digest": source_digest,
                    "trash": evidence,
                }
            )
            return BackendOutcome(
                "applied",
                "kio_trash_verified",
                result.detail,
                receipt_json=_canonical_json(payload),
            )
        except (TypeError, ValueError) as exc:
            return BackendOutcome("recovery_required", "kio_receipt_invalid", str(exc))

    def apply_snapshot(
        self,
        snapshot: FileSnapshot,
        *,
        root: Path,
        source_digest: str,
    ) -> BackendOutcome:
        """Apply one planned snapshot without manufacturing a new plan."""

        effect = SimpleNamespace(
            action="trash",
            source=snapshot,
            source_digest=source_digest,
            keeper=None,
            keeper_digest=None,
            target_path=None,
        )
        return self.apply(ApplyCandidate("framework", "", Path(root), effect))

    def apply_many_snapshots(
        self,
        items: Sequence[tuple[FileSnapshot, str]],
        *,
        root: Path,
    ) -> tuple[BackendOutcome, ...]:
        """Apply several trash snapshots while preserving input order."""

        if not isinstance(items, Sequence):
            raise TypeError("KIO batch items must be a finite sequence")
        if not items:
            return ()
        root = Path(root)
        valid_effects: list[object] = []
        outcomes: list[BackendOutcome | None] = [None] * len(items)
        valid_items: list[KioTrashBatchItem] = []
        valid_indexes: list[int] = []
        for index, item in enumerate(items):
            if (
                not isinstance(item, Sequence)
                or isinstance(item, (str, bytes, bytearray))
                or len(item) != 2
                or not isinstance(item[0], FileSnapshot)
                or not isinstance(item[1], str)
            ):
                raise TypeError(
                    "each KIO batch item must contain (FileSnapshot, source_digest)"
                )
            snapshot, source_digest = item
            effect = SimpleNamespace(
                action="trash",
                source=snapshot,
                source_digest=source_digest,
                keeper=None,
                keeper_digest=None,
                target_path=None,
            )
            try:
                _validate_effect_physical(effect, root)
            except (MutationError, OSError, RuntimeError, ValueError) as exc:
                outcomes[index] = BackendOutcome(
                    "blocked", "kio_preflight_failed", str(exc)
                )
                continue
            valid_effects.append(effect)
            valid_items.append(KioTrashBatchItem(snapshot.path, snapshot, source_digest))
            valid_indexes.append(index)
        if not valid_items:
            return tuple(item for item in outcomes if item is not None)

        batch_outcomes = self._service.move_many(valid_items)
        for index, effect, result in zip(
            valid_indexes, valid_effects, batch_outcomes, strict=True
        ):
            outcomes[index] = self._backend_outcome(effect, result)
        return tuple(item for item in outcomes if item is not None)

    def apply_snapshot_batch(
        self,
        items: Sequence[tuple[FileSnapshot, str]],
        *,
        root: Path,
    ) -> tuple[BackendOutcome, ...]:
        """Compatibility alias for :meth:`apply_many_snapshots`."""

        return self.apply_many_snapshots(items, root=root)

    def apply_snapshots(
        self,
        items: Sequence[tuple[FileSnapshot, str]],
        *,
        root: Path,
    ) -> tuple[BackendOutcome, ...]:
        """Preferred plural alias for the batch backend seam."""

        return self.apply_many_snapshots(items, root=root)


__all__ = [
    "ApplyCandidate",
    "ApplyStatus",
    "BackendOutcome",
    "KioTrashBackend",
    "MutationBackend",
    "MutationError",
    "MutationSnapshotChanged",
    "MutationUnavailable",
    "PosixRenameBackend",
]
