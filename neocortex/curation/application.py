"""Grant-bound, fixture-injectable curation effects for Linux.

The application coordinator is deliberately narrower than the historical
framework action route.  It consumes one immutable AuthorizationGrant, expands
only the grant's authorized effect manifest, and crosses the existing
``file_actions`` frontier one effect at a time.  Real KDE/KIO is never selected
implicitly: callers must inject a backend (the public adapter therefore fails
closed unless a controlled test/application integration supplies one).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from neocortex.curation.authorization import _item_from_task
from neocortex.curation.lifecycle import (
    CURATION_REVIEW_SCOPE,
    CURATION_REVIEW_TASK_TYPE,
    _logical_key,
)
from neocortex.curation.preview import build_curation_plan_page
from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
    files_equal_exact,
    full_fingerprint,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.framework_state_common import _action_idempotency_key
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.workflow.actions.action_policy import validate_mutation_path
from neocortex.safety.kio_trash import (
    KioTrashStatus,
    KioTrashVerification,
    move_to_trash,
)
from neocortex.workflow.actions.file_action_reconciliation_store import (
    RecordedFileActionReconciliation,
)
from neocortex.workflow.actions.file_action_recovery import (
    effect_receipt_json,
    expected_identity_json,
    list_file_action_reconciliations,
)
from neocortex.workflow.authorization.contracts import (
    AUTHORIZATION_ACTIONS,
    AuthorizationEffect,
    AuthorizationGrant,
    AuthorizationRootSnapshot,
    AuthorizationReviewTaskHead,
    review_task_heads_digest,
)
from neocortex.workflow.authorization.repository import read_authorization_grant
from neocortex.workflow.review.review_task_repository import (
    lookup_review_task_version_heads,
    read_review_task,
)
from neocortex.workflow.review.review_task_contracts import CanonicalJsonObject


CURATION_APPLY_SCHEMA = "neocortex.curation-apply/v1"
CURATION_RECONCILE_SCHEMA = "neocortex.curation-reconcile/v1"
CURATION_APPLY_MAX_EFFECTS = 100
CURATION_APPLY_MAX_BYTES = 128 * 1024 * 1024


class CurationApplicationError(RuntimeError):
    """A grant cannot safely cross the filesystem effect frontier."""


class CurationApplicationSnapshotChanged(CurationApplicationError):
    """A durable plan, review head, root or physical source changed."""


class CurationApplicationUnavailable(CurationApplicationError):
    """A required owner, grant manifest or backend is unavailable."""


class CurationApplicationCancelled(CurationApplicationError):
    """The caller cancelled between effects."""


ApplyStatus = Literal["applied", "blocked", "recovery_required"]


@dataclass(frozen=True, slots=True)
class BackendOutcome:
    """Typed result returned by an injected physical backend."""

    status: ApplyStatus
    reason: str
    detail: str | None = None
    receipt_json: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"applied", "blocked", "recovery_required"}:
            raise ValueError("unsupported backend outcome status")
        if not self.reason or self.reason.strip() != self.reason:
            raise ValueError("backend outcome reason must be non-empty and trimmed")
        if self.status == "applied" and not self.receipt_json:
            raise ValueError("applied backend outcome requires a receipt")


@dataclass(frozen=True, slots=True)
class ApplyCandidate:
    """One grant effect plus its enclosing root and grant identity."""

    grant_id: str
    grant_digest: str
    root: Path
    effect: AuthorizationEffect


class MutationBackend(Protocol):
    """Narrow backend seam used by apply tests and future Linux integrations."""

    name: str

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        """Perform one already-preflighted effect and return typed evidence."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _grant_digest(grant: AuthorizationGrant) -> str:
    return "sha256:" + hashlib.sha256(grant.to_json().encode("utf-8")).hexdigest()


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
            raise CurationApplicationSnapshotChanged(
                f"source snapshot changed: {snapshot.path}"
            )
        digest = full_fingerprint(current)
    except CurationApplicationSnapshotChanged:
        raise
    except FileChangedError as exc:
        raise CurationApplicationSnapshotChanged(
            f"source changed while hashing: {snapshot.path}"
        ) from exc
    except OSError as exc:
        raise CurationApplicationUnavailable(
            f"source cannot be hashed: {snapshot.path}"
        ) from exc
    return "xxh3_128_full_v1:" + digest.hex()


def _validate_regular_unique(snapshot: FileSnapshot, *, role: str) -> FileSnapshot:
    path = Path(snapshot.path)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise CurationApplicationSnapshotChanged(f"{role} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CurationApplicationError(f"{role} is a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise CurationApplicationError(f"{role} is not a regular file")
    if metadata.st_nlink != 1:
        raise CurationApplicationError(f"{role} has additional hard links")
    try:
        current = snapshot_path(path)
    except OSError as exc:
        raise CurationApplicationSnapshotChanged(f"{role} cannot be snapshotted") from exc
    if not _same_snapshot(current, snapshot) or not stat_matches_snapshot(snapshot, metadata):
        raise CurationApplicationSnapshotChanged(f"{role} changed: {path}")
    return current


def _root_snapshot(root: Path) -> AuthorizationRootSnapshot:
    try:
        current = snapshot_path(root)
    except OSError as exc:
        raise CurationApplicationSnapshotChanged("curation root cannot be snapshotted") from exc
    try:
        mode = os.lstat(root).st_mode
    except OSError as exc:
        raise CurationApplicationSnapshotChanged("curation root cannot be inspected") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise CurationApplicationError("curation root is not a real directory")
    return AuthorizationRootSnapshot(
        root=str(root),
        volume_id=current.volume_id,
        file_id=current.file_id,
        birthtime_ns=current.birthtime_ns,
    )


def _check_root_snapshot(expected: AuthorizationRootSnapshot, root: Path) -> None:
    current = _root_snapshot(root)
    if current != expected:
        raise CurationApplicationSnapshotChanged("curation root identity changed")


def _validate_effect_paths(root: Path, effect: AuthorizationEffect) -> None:
    try:
        validate_mutation_path(root, effect.source.path, role="curation source")
        if effect.keeper is not None:
            validate_mutation_path(root, effect.keeper.path, role="curation keeper")
        if effect.target_path is not None:
            validate_mutation_path(
                root,
                effect.target_path,
                role="curation target",
                allow_missing_leaf=True,
            )
    except (OSError, RuntimeError) as exc:
        raise CurationApplicationError(str(exc)) from exc


def _validate_effect_physical(effect: AuthorizationEffect, root: Path) -> None:
    _validate_effect_paths(root, effect)
    source = _validate_regular_unique(effect.source, role="curation source")
    if _digest_snapshot(source) != effect.source_digest:
        raise CurationApplicationSnapshotChanged("curation source digest changed")
    if effect.keeper is not None:
        keeper = _validate_regular_unique(effect.keeper, role="curation keeper")
        if effect.keeper_digest is None or _digest_snapshot(keeper) != effect.keeper_digest:
            raise CurationApplicationSnapshotChanged("curation keeper digest changed")
        try:
            if not files_equal_exact(source, keeper):
                raise CurationApplicationSnapshotChanged(
                    "curation source is no longer byte-identical to its keeper"
                )
        except FileChangedError as exc:
            raise CurationApplicationSnapshotChanged(
                "curation source or keeper changed during exact comparison"
            ) from exc
    if effect.target_path is not None:
        target = Path(effect.target_path)
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise CurationApplicationError("curation target cannot be inspected") from exc
        if metadata is not None:
            raise CurationApplicationError("curation target already exists")
        try:
            parent_device = os.stat(target.parent, follow_symlinks=False).st_dev
        except OSError as exc:
            raise CurationApplicationError("curation target parent is unavailable") from exc
        if parent_device != source.volume_id:
            raise CurationApplicationError("curation target is on another filesystem")


def _revalidate_review_heads(
    database: Path,
    grant: AuthorizationGrant,
) -> tuple[tuple[AuthorizationReviewTaskHead, ...], dict[str, object]]:
    if grant.review_task_heads is None or grant.review_task_heads_digest is None:
        raise CurationApplicationUnavailable("grant_review_task_heads_missing")
    keys = tuple(_logical_key(item_id) for item_id in grant.item_ids)
    current = lookup_review_task_version_heads(
        database,
        keys,
        scope=CURATION_REVIEW_SCOPE,
        task_type=CURATION_REVIEW_TASK_TYPE,
    )
    by_key = {head.logical_key: head for head in current}
    if len(by_key) != len(keys):
        raise CurationApplicationSnapshotChanged("a ReviewTask head is missing")
    manifest: list[AuthorizationReviewTaskHead] = []
    items: dict[str, object] = {}
    for expected in grant.review_task_heads:
        head = by_key.get(expected.logical_key)
        if head is None:
            raise CurationApplicationSnapshotChanged("a ReviewTask head disappeared")
        if (
            head.task_id != expected.task_id
            or head.task_version != expected.task_version
            or head.state.value != expected.state
            or head.event_id != expected.event_id
            or head.source_snapshot_fingerprint != expected.source_snapshot_fingerprint
            or head.source_input_fingerprint != expected.source_input_fingerprint
            or head.selector_signature != expected.selector_signature
        ):
            raise CurationApplicationSnapshotChanged("a ReviewTask head changed")
        record = read_review_task(database, head.task_id)
        if record is None or record.current_event.event_id != head.event_id:
            raise CurationApplicationSnapshotChanged("a ReviewTask record changed")
        decision = head.decision
        if decision is None or decision.to_dict().get("decision") != "resolved":
            raise CurationApplicationError("a ReviewTask is not resolved")
        try:
            items[expected.item_id] = _item_from_task(record, expected.item_id, grant.plan_digest)
        except Exception as exc:
            raise CurationApplicationSnapshotChanged("ReviewTask item snapshot changed") from exc
        manifest.append(
            AuthorizationReviewTaskHead.create(
                item_id=expected.item_id,
                logical_key=expected.logical_key,
                task_id=head.task_id,
                task_version=head.task_version,
                state=head.state.value,
                event_id=head.event_id,
                source_snapshot_fingerprint=head.source_snapshot_fingerprint,
                source_input_fingerprint=head.source_input_fingerprint,
                selector_signature=head.selector_signature,
                decision=decision,
            )
        )
    if tuple(manifest) != grant.review_task_heads:
        raise CurationApplicationSnapshotChanged("ReviewTask head manifest changed")
    return tuple(manifest), items


def _grant_context(
    state_directory: Path,
    database: Path,
    grant: AuthorizationGrant,
    run_id: int,
    state: FrameworkState,
) -> tuple[Path, tuple[AuthorizationEffect, ...]]:
    if grant.review_task_heads is None or grant.review_task_heads_digest is None:
        raise CurationApplicationUnavailable("grant is legacy and lacks ReviewTask heads")
    if (
        grant.root_snapshot is None
        or grant.source_heads is None
        or grant.source_heads_digest is None
        or grant.authorized_effects is None
        or grant.authorized_effects_digest is None
    ):
        raise CurationApplicationUnavailable("grant lacks the consumable 0.11 manifest")
    if len(grant.authorized_effects) > CURATION_APPLY_MAX_EFFECTS:
        raise CurationApplicationError("grant exceeds the effect bound")
    page = build_curation_plan_page(state_directory, 100, None)
    if page.coverage != "complete":
        raise CurationApplicationUnavailable("published curation plan is incomplete")
    if page.plan_digest != grant.plan_digest or page.snapshot_id != grant.snapshot_id:
        raise CurationApplicationSnapshotChanged("curation plan digest or snapshot changed")
    root = Path(page.root or "")
    if not root.is_absolute() or os.path.normcase(os.fspath(root)) != os.path.normcase(grant.root):
        raise CurationApplicationSnapshotChanged("curation plan root differs from grant")
    current_heads = tuple(CanonicalJsonObject.from_mapping(head.to_dict()) for head in page.source_heads)
    if current_heads != grant.source_heads:
        raise CurationApplicationSnapshotChanged("curation source heads changed")
    from neocortex.workflow.authorization.contracts import _source_heads_digest

    if _source_heads_digest(current_heads) != grant.source_heads_digest:
        raise CurationApplicationSnapshotChanged("curation source-head digest changed")
    _check_root_snapshot(grant.root_snapshot, root)
    try:
        guard = state.corpus_mutation_guard(run_id)
        guard.reject_run_mutation()
        if os.path.normcase(os.fspath(guard.policy.root)) != os.path.normcase(os.fspath(root)):
            raise CurationApplicationError("framework run root differs from grant")
        guard.require_paths_allowed(*(path for effect in grant.authorized_effects for path in (effect.source.path, effect.target_path, None if effect.keeper is None else effect.keeper.path)))
    except CurationApplicationError:
        raise
    except BaseException as exc:
        raise CurationApplicationUnavailable("framework mutation boundary is unavailable") from exc
    manifest, items = _revalidate_review_heads(database, grant)
    expected_head_digest = review_task_heads_digest(grant.review_task_heads)
    if expected_head_digest != grant.review_task_heads_digest:
        raise CurationApplicationError("grant ReviewTask heads digest is invalid")
    if review_task_heads_digest(tuple(manifest)) != grant.review_task_heads_digest:
        raise CurationApplicationSnapshotChanged("ReviewTask head digest changed")
    for effect in grant.authorized_effects:
        item = items.get(effect.item_id)
        if item is None:
            raise CurationApplicationSnapshotChanged("authorized effect item is missing")
        item_source = getattr(item, "source_path", None)
        item_target = getattr(item, "destination_path", None)
        if effect.action in {"move", "rename"}:
            if effect.source.path != item_source or effect.target_path != item_target:
                raise CurationApplicationSnapshotChanged("authorized move target changed")
        elif effect.kind == "empty_file":
            if effect.source.path != item_source:
                raise CurationApplicationSnapshotChanged("authorized empty-file source changed")
        elif effect.kind == "duplicate_group":
            evidence = getattr(item, "evidence", {})
            if not isinstance(evidence, Mapping) or evidence.get("keep_path") != (
                None if effect.keeper is None else effect.keeper.path
            ):
                raise CurationApplicationSnapshotChanged("authorized duplicate keeper changed")
        else:
            raise CurationApplicationError("authorized effect kind is unsupported")
    return root, grant.authorized_effects


def _intent_json(grant: AuthorizationGrant, effect: AuthorizationEffect) -> str:
    return _canonical_json(
        {
            "schema": "neocortex.curation-apply-intent/v1",
            "grant_id": grant.grant_id,
            "grant_digest": _grant_digest(grant),
            "plan_digest": grant.plan_digest,
            "source_heads_digest": grant.source_heads_digest,
            "review_task_heads_digest": grant.review_task_heads_digest,
            "effect": effect.to_dict(),
        }
    )


def _action_type(action: str) -> str:
    return {
        "trash": "trash_curation",
        "move": "move_curation",
        "rename": "rename_curation",
    }[action]


def _read_action_row(state: FrameworkState, action_id: int) -> tuple[str, str | None]:
    row = state._connection.execute(  # type: ignore[attr-defined]
        "SELECT status,effect_receipt_json FROM file_actions WHERE action_id=?",
        (action_id,),
    ).fetchone()
    if row is None:
        raise CurationApplicationUnavailable("file action disappeared after insertion")
    return str(row[0]), None if row[1] is None else str(row[1])


def _lookup_action_by_intent(
    state: FrameworkState,
    *,
    run_id: int,
    effect: AuthorizationEffect,
    intent: str,
) -> tuple[int, str] | None:
    key = _action_idempotency_key(
        run_id,
        _action_type(effect.action),
        effect.source.path,
        effect.target_path,
        None,
        intent,
        True,
    )
    row = state._connection.execute(  # type: ignore[attr-defined]
        "SELECT action_id,status FROM file_actions WHERE idempotency_key=?",
        (key,),
    ).fetchone()
    if row is None:
        # ``file_actions.idempotency_key`` predates grants and includes the
        # Framework run id.  The canonical intent itself is grant/effect bound,
        # so a replay in another operational run must still reuse the old row.
        rows = state._connection.execute(  # type: ignore[attr-defined]
            "SELECT action_id,status FROM file_actions WHERE evidence=? ORDER BY action_id LIMIT 2",
            (intent,),
        ).fetchall()
        if len(rows) > 1:
            raise CurationApplicationError("grant effect has multiple file-action intents")
        row = None if not rows else rows[0]
    if row is None:
        return None
    return int(row[0]), str(row[1])


def _expected_identity_for_effect(effect: AuthorizationEffect, root: Path) -> str:
    """Revalidate and serialize one effect before crossing the DB frontier."""

    _validate_effect_physical(effect, root)
    expected_json = expected_identity_json(
        effect.source,
        source_path=effect.source.path,
        target_path=effect.target_path,
    )
    payload = json.loads(expected_json)
    payload.update(
        {
            "effect_id": effect.effect_id,
            "keeper_digest": effect.keeper_digest,
            "source_digest": effect.source_digest,
        }
    )
    return _canonical_json(payload)


def _with_authorization_receipt(
    receipt_json: str,
    grant: AuthorizationGrant,
    effect: AuthorizationEffect,
) -> str:
    try:
        payload = json.loads(receipt_json)
    except (TypeError, ValueError) as exc:
        raise CurationApplicationError("backend receipt is not JSON") from exc
    if not isinstance(payload, dict):
        raise CurationApplicationError("backend receipt is not an object")
    payload.update(
        {
            "grant_id": grant.grant_id,
            "effect_id": effect.effect_id,
            "source_digest": effect.source_digest,
            "keeper_digest": effect.keeper_digest,
        }
    )
    return _canonical_json(payload)


def _validate_receipt(
    receipt_json: str,
    grant: AuthorizationGrant,
    effect: AuthorizationEffect,
) -> str:
    try:
        payload = json.loads(receipt_json)
    except (TypeError, ValueError) as exc:
        raise CurationApplicationError("effect receipt is not JSON") from exc
    if not isinstance(payload, dict):
        raise CurationApplicationError("effect receipt is not an object")
    if (
        payload.get("schema_version") != 1
        or payload.get("receipt_type") != "successful_return_and_observation"
        or payload.get("source_absent") is not True
        or payload.get("source_path") != effect.source.path
        or payload.get("target_path") != effect.target_path
        or payload.get("operation") != effect.action
        or payload.get("grant_id") != grant.grant_id
        or payload.get("effect_id") != effect.effect_id
        or payload.get("source_digest") != effect.source_digest
    ):
        raise CurationApplicationError("effect receipt does not match the grant effect")
    if effect.action in {"move", "rename"}:
        target_identity = payload.get("target_identity")
        if not isinstance(target_identity, dict):
            raise CurationApplicationError("rename receipt lacks target identity")
        expected_identity = {
            "path": effect.target_path,
            "volume_id": f"{effect.source.volume_id:x}",
            "file_id": f"{effect.source.file_id:x}",
            "size": effect.source.size,
            "mtime_ns": effect.source.mtime_ns,
            "birthtime_ns": effect.source.birthtime_ns,
        }
        if any(target_identity.get(key) != value for key, value in expected_identity.items()):
            raise CurationApplicationError("rename receipt target identity differs")
    if effect.action == "trash":
        trash = payload.get("trash")
        if not isinstance(trash, dict):
            raise CurationApplicationError("trash receipt lacks typed destination evidence")
        required = {"trash_path", "info_path", "volume_id", "file_id", "size", "digest"}
        if not required.issubset(trash):
            raise CurationApplicationError("trash receipt lacks restoration evidence")
    return _canonical_json(payload)


class PosixRenameBackend:
    """Same-filesystem, no-replace rename backend for contained fixtures.

    Linux Python has no portable ``renameat2`` wrapper.  The backend uses the
    kernel no-replace property of ``link(2)`` followed by an unlink, retaining
    ``recovery_required`` if the two-step sequence is interrupted.  It never
    overwrites an existing target and never falls back to copy/delete.
    """

    name = "posix-link-unlink-no-replace-v1"

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        effect = candidate.effect
        if effect.action not in {"move", "rename"} or effect.target_path is None:
            return BackendOutcome("blocked", "rename_action_requires_target")
        source = Path(effect.source.path)
        target = Path(effect.target_path)
        linked = False
        try:
            if os.path.lexists(target):
                return BackendOutcome("blocked", "destination_exists")
            _validate_effect_physical(effect, candidate.root)
            if os.stat(target.parent, follow_symlinks=False).st_dev != effect.source.volume_id:
                return BackendOutcome("blocked", "exdev_same_filesystem_required")
            os.link(source, target, follow_symlinks=False)
            linked = True
            os.unlink(source)
        except CurationApplicationError as exc:
            return BackendOutcome("blocked", "rename_preflight_failed", str(exc))
        except FileExistsError:
            return BackendOutcome("blocked", "destination_exists")
        except OSError as exc:
            if not linked and exc.errno == errno.EXDEV:
                return BackendOutcome("blocked", "exdev_same_filesystem_required")
            if linked:
                return BackendOutcome(
                    "recovery_required",
                    "rename_unlink_effect_ambiguous",
                    f"{type(exc).__name__}: {exc}",
                )
            return BackendOutcome("blocked", "rename_preflight_failed", f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            if linked:
                return BackendOutcome(
                    "recovery_required",
                    "rename_interrupted_after_link",
                    f"{type(exc).__name__}: effect outcome is unknown",
                )
            return BackendOutcome("blocked", "rename_interrupted_before_effect", type(exc).__name__)
        try:
            if os.path.lexists(source):
                raise CurationApplicationError("rename source remains present")
            target_snapshot = _validate_regular_unique(
                FileSnapshot(
                    str(target),
                    effect.source.volume_id,
                    effect.source.file_id,
                    effect.source.size,
                    effect.source.mtime_ns,
                    effect.source.birthtime_ns,
                ),
                role="rename target",
            )
            if _digest_snapshot(target_snapshot) != effect.source_digest:
                raise CurationApplicationError("rename target digest mismatch")
            receipt = effect_receipt_json(
                operation=effect.action,
                source_path=effect.source.path,
                target_path=effect.target_path,
                target_snapshot=target_snapshot,
            )
            payload = json.loads(receipt)
            payload.update(
                {
                    "backend": self.name,
                    "source_digest": effect.source_digest,
                    "target_digest": effect.source_digest,
                }
            )
            return BackendOutcome("applied", "rename_verified", receipt_json=_canonical_json(payload))
        except BaseException as exc:
            return BackendOutcome("recovery_required", "rename_effect_unverified", str(exc))


class KioTrashBackend:
    """Injected KIO trash adapter requiring structured destination evidence."""

    name = "kio-trash-path-bound-v1"

    def __init__(
        self,
        *,
        verifier: Callable[[Path, FileSnapshot, Path], KioTrashVerification],
        runner: Callable[..., object] | None = None,
        which: Callable[[str], str | None] | None = None,
        environment: Mapping[str, str] | None = None,
        home_directory: Path | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._verifier = verifier
        self._runner = runner
        self._which = which
        self._environment = environment
        self._home_directory = home_directory
        self._timeout_seconds = timeout_seconds

    def apply(self, candidate: ApplyCandidate) -> BackendOutcome:
        effect = candidate.effect
        if effect.action != "trash":
            return BackendOutcome("blocked", "kio_backend_supports_trash_only")
        kwargs: dict[str, object] = {
            "verifier": self._verifier,
            "timeout_seconds": self._timeout_seconds,
        }
        if self._runner is not None:
            kwargs["runner"] = self._runner
        if self._which is not None:
            kwargs["which"] = self._which
        if self._environment is not None:
            kwargs["environment"] = self._environment
        if self._home_directory is not None:
            kwargs["home_directory"] = self._home_directory
        result = move_to_trash(effect.source.path, effect.source, **kwargs)  # type: ignore[arg-type]
        if result.status is KioTrashStatus.RECOVERY_REQUIRED:
            return BackendOutcome("recovery_required", result.reason, result.detail)
        if result.status is KioTrashStatus.BLOCKED:
            return BackendOutcome("blocked", result.reason, result.detail)
        if result.receipt is None:
            return BackendOutcome("recovery_required", "kio_receipt_missing")
        try:
            evidence = json.loads(result.receipt.trash_evidence)
        except (TypeError, ValueError):
            return BackendOutcome("recovery_required", "kio_trash_evidence_unstructured")
        if not isinstance(evidence, dict):
            return BackendOutcome("recovery_required", "kio_trash_evidence_unstructured")
        required = {"trash_path", "info_path", "volume_id", "file_id", "size", "digest"}
        if not required.issubset(evidence):
            return BackendOutcome("recovery_required", "kio_trash_evidence_incomplete")
        trash_path = Path(str(evidence["trash_path"]))
        info_path = Path(str(evidence["info_path"]))
        if not trash_path.is_absolute() or not info_path.is_absolute():
            return BackendOutcome("recovery_required", "kio_trash_evidence_paths_invalid")
        try:
            trash_snapshot = snapshot_path(trash_path)
            info_stat = os.lstat(info_path)
            if stat.S_ISLNK(info_stat.st_mode) or not stat.S_ISREG(info_stat.st_mode):
                raise CurationApplicationError("KIO .trashinfo is not a regular file")
            if trash_snapshot.volume_id != effect.source.volume_id:
                raise CurationApplicationError("KIO trash destination is on another filesystem")
            if trash_snapshot.size != effect.source.size:
                raise CurationApplicationError("KIO trash destination size differs")
            if int(str(evidence["volume_id"]), 16) != trash_snapshot.volume_id:
                raise CurationApplicationError("KIO trash evidence volume differs")
            if int(str(evidence["file_id"]), 16) != trash_snapshot.file_id:
                raise CurationApplicationError("KIO trash evidence identity differs")
            if evidence["size"] != trash_snapshot.size or evidence["digest"] != effect.source_digest:
                raise CurationApplicationError("KIO trash evidence content differs")
            if _digest_snapshot(trash_snapshot) != effect.source_digest:
                raise CurationApplicationError("KIO trash destination digest differs")
        except BaseException as exc:
            return BackendOutcome("recovery_required", "kio_trash_evidence_mismatch", str(exc))
        receipt = effect_receipt_json(
            operation="trash",
            source_path=effect.source.path,
            target_path=None,
        )
        payload = json.loads(receipt)
        payload.update(
            {
                "backend": self.name,
                "source_digest": effect.source_digest,
                "trash": evidence,
            }
        )
        return BackendOutcome("applied", "kio_trash_verified", receipt_json=_canonical_json(payload))


@dataclass(frozen=True, slots=True)
class AppliedEffect:
    effect_id: str
    action_id: int | None
    status: ApplyStatus
    reason: str
    detail: str | None = None
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class CurationApplicationResult:
    grant_id: str
    status: Literal["complete", "partial", "blocked", "recovery_required", "unavailable"]
    effects: tuple[AppliedEffect, ...]
    actions_attempted: int
    bytes_attempted: int
    cancelled: bool = False

    @property
    def applied(self) -> int:
        return sum(effect.status == "applied" for effect in self.effects)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CURATION_APPLY_SCHEMA,
            "schema_version": 1,
            "grant_id": self.grant_id,
            "status": self.status,
            "effects": [
                {
                    "effect_id": effect.effect_id,
                    "action_id": effect.action_id,
                    "status": effect.status,
                    "reason": effect.reason,
                    "detail": effect.detail,
                    "idempotent": effect.idempotent,
                }
                for effect in self.effects
            ],
            "actions_attempted": self.actions_attempted,
            "bytes_attempted": self.bytes_attempted,
            "cancelled": self.cancelled,
            "read_only": False,
            "effects_state": "file_actions",
            "corpus": "effect_or_recovery",
        }


def apply_authorization_grant(
    state_directory: Path,
    database: Path,
    grant_id: str,
    *,
    run_id: int,
    backend: MutationBackend,
    state: FrameworkState | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
    cancellation_check: Callable[[], bool] | None = None,
) -> CurationApplicationResult:
    """Consume one grant through a bounded, one-effect-at-a-time frontier."""

    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be positive")
    if not grant_id or grant_id.strip() != grant_id:
        raise ValueError("grant_id must be a trimmed non-empty string")
    if not hasattr(backend, "apply"):
        raise TypeError("backend must implement apply(candidate)")
    state_directory = Path(state_directory)
    database = Path(database)
    owned_state = state is None
    effective_state = FrameworkState(database, existing_only=True) if state is None else state
    effects: list[AppliedEffect] = []
    actions_attempted = 0
    bytes_attempted = 0
    cancelled = False
    try:
        with FrameworkRunLock(database.parent / "framework.lock"):
            grant = read_authorization_grant(database, grant_id=grant_id)
            if grant is None:
                raise CurationApplicationUnavailable("authorization grant is unavailable")
            now = clock_ns()
            if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
                raise CurationApplicationError("authorization clock returned an invalid timestamp")
            if now >= grant.expires_ns:
                raise CurationApplicationError("authorization grant has expired")
            if grant.action not in AUTHORIZATION_ACTIONS:
                raise CurationApplicationError("authorization grant action is unsupported")
            root, authorized_effects = _grant_context(
                state_directory,
                database,
                grant,
                run_id,
                effective_state,
            )
            if sum(effect.source.size for effect in authorized_effects) > grant.max_bytes:
                raise CurationApplicationError("grant byte budget is exceeded")
            planned: list[tuple[AuthorizationEffect, int | None, str, str | None]] = []
            preflight_failed = False
            for effect in authorized_effects:
                if cancellation_check is not None and cancellation_check():
                    cancelled = True
                    break
                intent = _intent_json(grant, effect)
                existing = _lookup_action_by_intent(
                    effective_state,
                    run_id=run_id,
                    effect=effect,
                    intent=intent,
                )
                action_id = None if existing is None else existing[0]
                status = "new" if existing is None else existing[1]
                expected: str | None = None
                if status in {"new", "started"}:
                    try:
                        expected = _expected_identity_for_effect(effect, root)
                    except CurationApplicationError as exc:
                        if action_id is not None:
                            effective_state.finish_file_action(action_id, "failed", str(exc))
                        effects.append(
                            AppliedEffect(
                                effect.effect_id,
                                action_id,
                                "blocked",
                                "preflight_failed",
                                str(exc),
                            )
                        )
                        preflight_failed = True
                        break
                planned.append((effect, action_id, status, expected))

            # Every fresh effect is checked before the first backend call.  An
            # already-applied/recovery action is deliberately exempt so replay
            # remains idempotent even though its source may no longer exist.
            prepared: list[tuple[AuthorizationEffect, int, str]] = []
            if not cancelled and not preflight_failed:
                for effect, action_id, status, _expected in planned:
                    if action_id is None:
                        action_id = effective_state.begin_file_action(
                            run_id,
                            _action_type(effect.action),
                            effect.source.path,
                            effect.target_path,
                            None,
                            _intent_json(grant, effect),
                            True,
                        )
                        status = "started"
                    prepared.append((effect, action_id, status))

            if not cancelled and not preflight_failed and not any(
                effect.status == "blocked" for effect in effects
            ):
                for effect, action_id, status in prepared:
                    if cancellation_check is not None and cancellation_check():
                        cancelled = True
                        break
                    if status == "applied":
                        effects.append(
                            AppliedEffect(
                                effect.effect_id,
                                action_id,
                                "applied",
                                "already_applied",
                                idempotent=True,
                            )
                        )
                        continue
                    if status in {"recovery_required", "applying"}:
                        if status == "applying":
                            effective_state.require_file_action_recovery(
                                (action_id,),
                                "replayed grant found an applying action; reconcile before retry",
                            )
                        effects.append(
                            AppliedEffect(
                                effect.effect_id,
                                action_id,
                                "recovery_required",
                                "reconcile_before_retry",
                                idempotent=True,
                            )
                        )
                        break
                    if status in {"failed", "skipped", "planned"}:
                        effects.append(
                            AppliedEffect(
                                effect.effect_id,
                                action_id,
                                "blocked",
                                "existing_terminal_action",
                                idempotent=True,
                            )
                        )
                        break
                    frontier_crossed = False
                    try:
                        expected = _expected_identity_for_effect(effect, root)
                        effective_state.mark_file_actions_applying(((action_id, expected),))
                        frontier_crossed = True
                        actions_attempted += 1
                        bytes_attempted += effect.source.size
                        outcome = backend.apply(
                            ApplyCandidate(grant.grant_id, _grant_digest(grant), root, effect)
                        )
                    except CurationApplicationError as exc:
                        if frontier_crossed:
                            effective_state.require_file_action_recovery((action_id,), str(exc))
                            effects.append(
                                AppliedEffect(
                                    effect.effect_id,
                                    action_id,
                                    "recovery_required",
                                    "post_frontier_error",
                                    str(exc),
                                )
                            )
                        else:
                            effective_state.finish_file_action(action_id, "failed", str(exc))
                            effects.append(
                                AppliedEffect(
                                    effect.effect_id,
                                    action_id,
                                    "blocked",
                                    "preflight_failed",
                                    str(exc),
                                )
                            )
                        break
                    except BaseException as exc:
                        effective_state.require_file_action_recovery(
                            (action_id,),
                            f"apply exception: {type(exc).__name__}: {exc}",
                        )
                        effects.append(
                            AppliedEffect(
                                effect.effect_id,
                                action_id,
                                "recovery_required",
                                "apply_exception",
                                str(exc),
                            )
                        )
                        break
                    if outcome.status == "applied" and outcome.receipt_json is not None:
                        try:
                            receipt = _validate_receipt(
                                _with_authorization_receipt(outcome.receipt_json, grant, effect),
                                grant,
                                effect,
                            )
                        except CurationApplicationError as exc:
                            effective_state.require_file_action_recovery((action_id,), str(exc))
                            effects.append(
                                AppliedEffect(
                                    effect.effect_id,
                                    action_id,
                                    "recovery_required",
                                    "receipt_invalid",
                                    str(exc),
                                )
                            )
                            break
                        try:
                            effective_state.confirm_file_actions_applied(((action_id, receipt),))
                        except BaseException as exc:
                            effective_state.require_file_action_recovery(
                                (action_id,),
                                f"receipt persistence failed: {type(exc).__name__}: {exc}",
                            )
                            effects.append(
                                AppliedEffect(
                                    effect.effect_id,
                                    action_id,
                                    "recovery_required",
                                    "receipt_persistence_failed",
                                    str(exc),
                                )
                            )
                            break
                        effects.append(
                            AppliedEffect(effect.effect_id, action_id, "applied", outcome.reason, outcome.detail)
                        )
                        continue
                    detail = outcome.detail or outcome.reason
                    effective_state.require_file_action_recovery((action_id,), detail)
                    effects.append(
                        AppliedEffect(
                            effect.effect_id,
                            action_id,
                            "recovery_required",
                            outcome.reason,
                            detail,
                        )
                    )
                    break
            if cancelled:
                status_value: Literal["complete", "partial", "blocked", "recovery_required", "unavailable"] = "partial"
            elif any(effect.status == "recovery_required" for effect in effects):
                status_value = "recovery_required"
            elif any(effect.status == "blocked" for effect in effects):
                status_value = "blocked"
            elif len(effects) == len(authorized_effects):
                status_value = "complete"
            else:
                status_value = "partial"
            return CurationApplicationResult(
                grant_id=grant.grant_id,
                status=status_value,
                effects=tuple(effects),
                actions_attempted=actions_attempted,
                bytes_attempted=bytes_attempted,
                cancelled=cancelled,
            )
    finally:
        if owned_state:
            effective_state.close()


def reconcile_curation_actions(
    database: Path,
    *,
    actor: str,
    provenance: Mapping[str, object] | None = None,
    limit: int = 100,
    after_action_id: int = 0,
    run_id: int | None = None,
) -> tuple[RecordedFileActionReconciliation, ...]:
    """Record bounded read-only reconciliation evidence without retrying."""

    database = Path(database)
    with FrameworkRunLock(database.parent / "framework.lock"):
        records = list_file_action_reconciliations(
            database, limit=limit, after_action_id=after_action_id, run_id=run_id
        )
        if not records:
            return ()
        state = FrameworkState(database, existing_only=True)
        try:
            provenance_json = _canonical_json(
                {"schema": CURATION_RECONCILE_SCHEMA, **dict(provenance or {})}
            )
            result: list[RecordedFileActionReconciliation] = []
            for reconciliation in records:
                latest_row = state._connection.execute(  # type: ignore[attr-defined]
                    """SELECT reconciliation_event_id,action_id,sequence,previous_event_id,
                    reconciliation_key,observed_ns,recorded_ns,action_status,
                    reconciler_signature,event_schema_version,actor,provenance_json,
                    classification,recommendation,detail,evidence_json
                    FROM file_action_reconciliation_events WHERE action_id=?
                    ORDER BY reconciliation_event_id DESC LIMIT 1""",
                    (reconciliation.action_id,),
                ).fetchone()
                if latest_row is not None and tuple(latest_row[7:15]) == (
                    reconciliation.recorded_status,
                    reconciliation.reconciler_signature,
                    1,
                    actor,
                    provenance_json,
                    reconciliation.classification,
                    reconciliation.recommendation,
                    reconciliation.detail,
                ):
                    result.append(RecordedFileActionReconciliation(*latest_row))
                    continue
                previous = None if latest_row is None else int(latest_row[0])
                result.append(
                    state.record_file_action_reconciliation(
                        reconciliation,
                        actor=actor,
                        provenance_json=provenance_json,
                        expected_previous_event_id=previous,
                    )
                )
            return tuple(result)
        finally:
            state.close()


__all__ = (
    "CURATION_APPLY_MAX_BYTES",
    "CURATION_APPLY_MAX_EFFECTS",
    "CURATION_APPLY_SCHEMA",
    "CURATION_RECONCILE_SCHEMA",
    "AppliedEffect",
    "ApplyCandidate",
    "BackendOutcome",
    "CurationApplicationCancelled",
    "CurationApplicationError",
    "CurationApplicationResult",
    "CurationApplicationSnapshotChanged",
    "CurationApplicationUnavailable",
    "KioTrashBackend",
    "MutationBackend",
    "PosixRenameBackend",
    "apply_authorization_grant",
    "reconcile_curation_actions",
)
