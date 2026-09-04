"""Issue explicit, digest-bound AuthorizationGrants for reviewed curation items.

Issuing a grant is the only state-changing operation in this module. It records
an immutable authorization in the Framework extension, but deliberately does
not create a ``file_actions`` row, call KIO, or touch the corpus. A future
``apply`` must revalidate the grant and the physical identities immediately
before an effect.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from neocortex.curation.lifecycle import (
    CURATION_REVIEW_SELECTOR_SIGNATURE,
    CURATION_REVIEW_SCOPE,
    CURATION_REVIEW_TASK_TYPE,
    _fence,
    _logical_key,
    _validate_plan_digest,
    _validate_item_id,
    _validate_actor,
)
from neocortex.curation.preview import CurationItem, CurationPlanPage, build_curation_plan_page
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.workflow.authorization.contracts import (
    AUTHORIZATION_ACTIONS,
    AUTHORIZATION_BACKEND,
    AUTHORIZATION_SELECTOR_SIGNATURE,
    AUTHORIZATION_SCOPE,
    AUTHORIZATION_TASK_TYPE,
    AuthorizationGrant,
    MAX_AUTHORIZATION_ITEMS,
)
from neocortex.workflow.authorization.repository import (
    AuthorizationGrantResult,
    issue_authorization_grant,
)
from neocortex.workflow.review.review_task_contracts import ReviewTaskRecord, ReviewTaskState
from neocortex.workflow.review.review_task_repository import (
    lookup_review_task_version_heads,
    read_review_task,
)


ClockNs = Callable[[], int]


class CurationAuthorizationError(RuntimeError):
    """A requested grant cannot be bound to complete reviewed evidence."""


class CurationAuthorizationSnapshotChanged(CurationAuthorizationError):
    """The curation plan or one reviewed task changed before grant issuance."""


class CurationAuthorizationUnavailable(CurationAuthorizationError):
    """The required published plan or Framework extension is unavailable."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _key_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _item_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("item_ids must be an array")
    if not 1 <= len(value) <= MAX_AUTHORIZATION_ITEMS:
        raise ValueError(f"item_ids must contain between 1 and {MAX_AUTHORIZATION_ITEMS} items")
    result = tuple(_validate_item_id(item) for item in value)
    if len(set(result)) != len(result):
        raise ValueError("item_ids cannot contain duplicates")
    return result


def _positive_clock(clock_ns: ClockNs) -> int:
    value = clock_ns()
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CurationAuthorizationError("authorization clock returned an invalid timestamp")
    return value


def _positive_or_zero_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    return value


def _page(state_directory: Path, plan_digest: str) -> CurationPlanPage:
    page = build_curation_plan_page(Path(state_directory), 1, None)
    if page.coverage != "complete":
        raise CurationAuthorizationUnavailable("curation plan coverage is not complete")
    if page.plan_digest != plan_digest:
        raise CurationAuthorizationSnapshotChanged("curation plan digest changed")
    if page.root is None or not page.root.startswith("/"):
        raise CurationAuthorizationError("curation plan root is unavailable")
    return page


def _item_from_task(record: ReviewTaskRecord, item_id: str, plan_digest: str) -> CurationItem:
    try:
        snapshot = record.task.snapshot.to_dict()
    except (AttributeError, TypeError, ValueError) as exc:
        raise CurationAuthorizationError("curation ReviewTask snapshot is invalid") from exc
    if (
        snapshot.get("contract") != "neocortex.curation-item/v1"
        or snapshot.get("plan_digest") != plan_digest
        or snapshot.get("advisory_only") is not True
        or snapshot.get("mutation_authorized") is not False
    ):
        raise CurationAuthorizationSnapshotChanged("ReviewTask is not bound to the requested plan")
    raw_item = snapshot.get("item")
    if not isinstance(raw_item, Mapping):
        raise CurationAuthorizationError("curation ReviewTask lacks its item snapshot")
    if raw_item.get("item_id") != item_id:
        raise CurationAuthorizationError("curation ReviewTask item identity is contradictory")
    try:
        return CurationItem(
            item_id=str(raw_item["item_id"]),
            kind=str(raw_item["kind"]),
            status=str(raw_item["status"]),
            action=str(raw_item["action"]),
            source_path=str(raw_item["source_path"]),
            destination_path=(
                None
                if raw_item.get("destination_path") is None
                else str(raw_item["destination_path"])
            ),
            reason=str(raw_item["reason"]),
            evidence=dict(raw_item.get("evidence", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CurationAuthorizationError("curation item snapshot is malformed") from exc


def _validate_requested_effect(item: CurationItem, action: str) -> int:
    if action not in AUTHORIZATION_ACTIONS:
        raise ValueError("authorization action is unsupported")
    if action == "trash":
        if item.kind not in {"duplicate_group", "empty_file"}:
            raise CurationAuthorizationError("trash is not supported for this curation item")
        if item.kind == "duplicate_group" and item.evidence.get("verification_mode") != "full_hash":
            raise CurationAuthorizationError(
                "duplicate group lacks full-hash verification for trash authorization"
            )
    else:
        destination = item.destination_path
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise CurationAuthorizationError("move/rename requires an absolute destination")
        if destination == item.source_path:
            raise CurationAuthorizationError("move/rename destination equals its source")
    size = item.evidence.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return 0
    return size


@dataclass(frozen=True, slots=True)
class CurationAuthorizationOutcome:
    result: AuthorizationGrantResult
    items: tuple[CurationItem, ...]

    @property
    def grant(self) -> AuthorizationGrant:
        return self.result.grant

    @property
    def idempotent(self) -> bool:
        return self.result.idempotent

    def to_dict(self) -> dict[str, object]:
        return {
            "grant": self.grant.to_dict(),
            "idempotent": self.idempotent,
            "items": [item.to_dict() for item in self.items],
            "effects": {"state": "authorization_grant", "corpus": "none", "external": "none"},
            "trust": {
                "content_class": "untrusted_corpus_evidence",
                "instruction_authority": False,
                "actions_authorized": True,
                "physical_effect_applied": False,
            },
        }


def authorize_curation_items(
    state_directory: Path,
    database: Path,
    *,
    plan_digest: str,
    item_ids: list[str] | tuple[str, ...],
    action: str,
    actor: str,
    expires_ns: int,
    max_bytes: int,
    authorization_key: str | None = None,
    clock_ns: ClockNs = time.time_ns,
) -> CurationAuthorizationOutcome:
    """Issue one explicit grant after rechecking reviewed plan heads.

    This function writes only the Framework AuthorizationGrant extension. It is
    intentionally not an ``apply`` operation and does not create a file action.
    """

    digest = _validate_plan_digest(plan_digest)
    ids = _item_ids(item_ids)
    actor_text = _validate_actor(actor)
    if action not in AUTHORIZATION_ACTIONS:
        raise ValueError("authorization action is unsupported")
    max_bytes_value = _positive_or_zero_bytes(max_bytes)
    issued_ns = _positive_clock(clock_ns)
    if isinstance(expires_ns, bool) or not isinstance(expires_ns, int) or expires_ns <= issued_ns:
        raise ValueError("expires_ns must be later than the issuance time")
    state_directory = Path(state_directory)
    database = Path(database)
    if not database.is_file():
        raise CurationAuthorizationUnavailable("Framework review owner is absent")
    with FrameworkRunLock(database.parent / "framework.lock"):
        page = _page(state_directory, digest)
        fence = _fence(page)
        logical_keys = tuple(_logical_key(item_id) for item_id in ids)
        heads = lookup_review_task_version_heads(
            database,
            logical_keys,
            scope=CURATION_REVIEW_SCOPE,
            task_type=CURATION_REVIEW_TASK_TYPE,
        )
        by_key = {head.logical_key: head for head in heads}
        if len(by_key) != len(ids):
            raise CurationAuthorizationError("every authorized item needs a published ReviewTask")
        records: list[ReviewTaskRecord] = []
        items: list[CurationItem] = []
        total_known_bytes = 0
        for item_id in ids:
            head = by_key[_logical_key(item_id)]
            if head.state is not ReviewTaskState.RESOLVED:
                raise CurationAuthorizationError("every authorized item must be resolved")
            decision = None if head.decision is None else head.decision.to_dict()
            if (
                decision is None
                or decision.get("decision") != "resolved"
                or decision.get("selector_signature") != CURATION_REVIEW_SELECTOR_SIGNATURE
                or decision.get("source_snapshot_fingerprint") != fence.source_snapshot_fingerprint
            ):
                raise CurationAuthorizationSnapshotChanged(
                    "ReviewTask decision is not bound to this plan"
                )
            record = read_review_task(database, head.task_id)
            if record is None:
                raise CurationAuthorizationSnapshotChanged(
                    "ReviewTask disappeared before authorization"
                )
            if record.source_snapshot_fingerprint != fence.source_snapshot_fingerprint:
                raise CurationAuthorizationSnapshotChanged("ReviewTask source snapshot changed")
            item = _item_from_task(record, item_id, digest)
            total_known_bytes += _validate_requested_effect(item, action)
            records.append(record)
            items.append(item)
        if max_bytes_value < total_known_bytes:
            raise CurationAuthorizationError("max_bytes is below the reviewed item size")
        semantic = {
            "action": action,
            "actor": actor_text,
            "backend": AUTHORIZATION_BACKEND,
            "expires_ns": expires_ns,
            "item_ids": list(ids),
            "max_actions": len(ids),
            "max_bytes": max_bytes_value,
            "plan_digest": digest,
            "snapshot_id": page.snapshot_id,
            "source_snapshot_fingerprint": fence.source_snapshot_fingerprint,
            "task_ids": [record.task.task_id for record in records],
        }
        key = authorization_key
        if key is None:
            key = "curation-authorization-key-v1:" + _key_digest(semantic)
        else:
            if not isinstance(key, str) or not key or key.strip() != key or len(key) > 512:
                raise ValueError("authorization_key must be a bounded trimmed string")
        grant_digest = _key_digest({"authorization_key": key, **semantic})
        grant = AuthorizationGrant(
            grant_id=f"curation-authorization-grant-v1:{grant_digest}",
            authorization_key=key,
            scope=AUTHORIZATION_SCOPE,
            task_type=AUTHORIZATION_TASK_TYPE,
            selector_signature=AUTHORIZATION_SELECTOR_SIGNATURE,
            plan_digest=digest,
            snapshot_id=page.snapshot_id,
            source_snapshot_fingerprint=fence.source_snapshot_fingerprint,
            root=page.root,
            actor=actor_text,
            action=action,
            backend=AUTHORIZATION_BACKEND,
            item_ids=ids,
            task_ids=tuple(record.task.task_id for record in records),
            max_actions=len(ids),
            max_bytes=max_bytes_value,
            issued_ns=issued_ns,
            expires_ns=expires_ns,
        )
        result = issue_authorization_grant(database, grant)
        return CurationAuthorizationOutcome(result=result, items=tuple(items))


__all__ = (
    "CurationAuthorizationError",
    "CurationAuthorizationOutcome",
    "CurationAuthorizationSnapshotChanged",
    "CurationAuthorizationUnavailable",
    "authorize_curation_items",
)
