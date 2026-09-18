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
import os
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
    FULL_ALGORITHM,
    files_equal_exact,
    full_fingerprint,
    snapshot_path,
)
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.workflow.authorization.contracts import (
    AUTHORIZATION_ACTIONS,
    AUTHORIZATION_BACKEND,
    AUTHORIZATION_SELECTOR_SIGNATURE,
    AUTHORIZATION_SCOPE,
    AUTHORIZATION_TASK_TYPE,
    AuthorizationEffect,
    AuthorizationRootSnapshot,
    AuthorizationReviewTaskHead,
    AuthorizationGrant,
    MAX_AUTHORIZATION_ITEMS,
    review_task_heads_digest,
    authorized_effects_digest,
)
from neocortex.workflow.authorization.repository import (
    AuthorizationGrantResult,
    issue_authorization_grant,
)
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskRecord,
    ReviewTaskState,
)
from neocortex.workflow.review.review_task_repository import (
    lookup_review_task_version_heads,
    read_review_task,
)
from neocortex.curation.verification import (
    CurationVerificationError,
    CurationVerificationSnapshotChanged,
    _authoritative_group_reference,
    _authoritative_inventory_session,
    _authoritative_keeper,
    _iter_authoritative_redundant_members,
    _validate_authoritative_inventory,
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
    required_text = ("item_id", "kind", "status", "action", "source_path", "reason")
    if any(not isinstance(raw_item.get(field), str) for field in required_text):
        raise CurationAuthorizationError("curation item snapshot contains non-text fields")
    destination = raw_item.get("destination_path")
    if destination is not None and not isinstance(destination, str):
        raise CurationAuthorizationError("curation item destination is malformed")
    evidence = raw_item.get("evidence")
    if not isinstance(evidence, Mapping):
        raise CurationAuthorizationError("curation item evidence is malformed")
    try:
        return CurationItem(
            item_id=raw_item["item_id"],
            kind=raw_item["kind"],
            status=raw_item["status"],
            action=raw_item["action"],
            source_path=raw_item["source_path"],
            destination_path=destination,
            reason=raw_item["reason"],
            evidence=dict(evidence),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CurationAuthorizationError("curation item snapshot is malformed") from exc


def _validate_requested_effect(
    item: CurationItem,
    action: str,
    *,
    state_directory: Path | None = None,
    inventory_root: str | None = None,
) -> int:
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
        if item.kind != "organization_plan":
            raise CurationAuthorizationError(
                "move/rename is supported only for organization proposals"
            )
        destination = item.destination_path
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise CurationAuthorizationError("move/rename requires an absolute destination")
        if destination == item.source_path:
            raise CurationAuthorizationError("move/rename destination equals its source")
        _validate_organization_effect_scope(
            item, state_directory=state_directory, inventory_root=inventory_root
        )
    size = item.evidence.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return 0
    return size


def _validate_organization_effect_scope(
    item: CurationItem, *, state_directory: Path | None, inventory_root: str | None
) -> None:
    """A logical proposal or legacy unscoped row cannot become a physical grant.

    Backend availability is deliberately not required: authorization and the
    later explicitly injected effect backend remain independent boundaries.
    """
    from neocortex.documents.document_catalog import document_catalog_database
    from neocortex.documents.document_organization_scope import (
        OrganizationInputScope,
        assess_organization_resource,
    )
    from neocortex.documents.document_resource_binding import (
        binding_curation_identity,
        parse_resource_binding,
    )

    try:
        binding = parse_resource_binding(
            json.dumps(item.evidence.get("resource_binding"), allow_nan=False)
        )
        physical = binding_curation_identity(binding)
        if physical is None or physical != item.evidence.get("identity"):
            raise ValueError("organization requires a resolved physical resource identity")
        scope = OrganizationInputScope.from_json(item.evidence.get("source_scope_json"))
        if scope.scope_id != item.evidence.get("source_scope_id"):
            raise ValueError("organization scope digest is inconsistent")
        if inventory_root is not None and not scope.root.is_relative_to(Path(inventory_root)):
            raise ValueError("organization scope is outside the reviewed inventory root")
        if state_directory is not None:
            with document_catalog_database(
                state_directory / "document_catalog.sqlite3", readonly=True
            ) as catalog:
                scope.verify(catalog)
        else:
            scope.verify()
        assessed = assess_organization_resource(
            {
                "resource_binding_json": json.dumps(binding),
                "source_kind": item.evidence.get("source_kind"),
                "file_key": item.evidence.get("file_key"),
                "source_path": item.source_path,
            },
            scope,
        )
        if not assessed.included:
            raise ValueError(assessed.reason or "organization resource is outside its proven scope")
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise CurationAuthorizationError(
            f"organization physical identity/scope is unverified: {error}"
        ) from error


def _snapshot_from_item_evidence(item: CurationItem) -> FileSnapshot:
    """Rehydrate one inventory snapshot from the reviewed item evidence."""

    identity = item.evidence.get("identity")
    if not isinstance(identity, Mapping):
        raise CurationAuthorizationError("curation item lacks physical identity evidence")
    try:
        volume_id = int(str(identity["volume_id"]), 16)
        file_id = int(str(identity["file_id"]), 16)
        birthtime_ns = int(identity["birthtime_ns"])
        size = item.evidence["size"]
        mtime_ns = item.evidence["mtime_ns"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CurationAuthorizationError("curation item physical identity is malformed") from exc
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (volume_id, file_id, size, mtime_ns)
        )
        or birthtime_ns < -1
    ):
        raise CurationAuthorizationError("curation item physical identity is invalid")
    return FileSnapshot(
        item.source_path,
        volume_id,
        file_id,
        size,
        mtime_ns,
        birthtime_ns,
    )


def _snapshot_from_member(value: object, *, item_id: str) -> FileSnapshot:
    if not isinstance(value, Mapping):
        raise CurationAuthorizationError(f"duplicate item {item_id} has malformed member evidence")
    identity = value.get("identity")
    if not isinstance(identity, Mapping):
        raise CurationAuthorizationError(f"duplicate item {item_id} member lacks identity")
    try:
        path = value["path"]
        volume_id = int(str(identity["volume_id"]), 16)
        file_id = int(str(identity["file_id"]), 16)
        birthtime_ns = int(identity["birthtime_ns"])
        size = value["size"]
        mtime_ns = value["mtime_ns"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CurationAuthorizationError(f"duplicate item {item_id} member is malformed") from exc
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(
            isinstance(number, bool) or not isinstance(number, int) or number < 0
            for number in (volume_id, file_id, size, mtime_ns)
        )
        or birthtime_ns < -1
    ):
        raise CurationAuthorizationError(f"duplicate item {item_id} member identity is invalid")
    return FileSnapshot(path, volume_id, file_id, size, mtime_ns, birthtime_ns)


def _full_digest(snapshot: FileSnapshot) -> str:
    try:
        current = snapshot_path(snapshot.path)
        if current != snapshot:
            raise CurationAuthorizationSnapshotChanged(
                f"source changed before authorization: {snapshot.path}"
            )
        digest = full_fingerprint(current)
    except FileChangedError as exc:
        raise CurationAuthorizationSnapshotChanged(
            f"source changed while hashing: {snapshot.path}"
        ) from exc
    except OSError as exc:
        raise CurationAuthorizationUnavailable(f"source cannot be hashed: {snapshot.path}") from exc
    return f"{FULL_ALGORITHM}:" + digest.hex()


def _effect_manifest(
    items: tuple[CurationItem, ...],
    task_ids: tuple[str, ...],
    action: str,
    *,
    page: CurationPlanPage | None = None,
    state_directory: str | Path | None = None,
    inventory_database: str | Path | None = None,
) -> tuple[tuple[AuthorizationEffect, ...], int]:
    """Expand reviewed items into immutable, byte-verified physical effects.

    Duplicate-group members in a ReviewTask are presentation evidence and can
    be truncated.  When an owner path and published page are supplied, this
    function resolves every member from the fenced inventory publication,
    ignoring the embedded list entirely.  The legacy list path remains only
    for private compatibility callers that do not provide an owner.
    """

    if state_directory is not None and inventory_database is not None:
        raise ValueError("state_directory and inventory_database are mutually exclusive")
    authoritative_database: Path | None = None
    if state_directory is not None:
        authoritative_database = Path(state_directory)
        if not authoritative_database.is_absolute():
            raise ValueError("state_directory must be absolute")
        authoritative_database = authoritative_database / "dedup.sqlite3"
    elif inventory_database is not None:
        authoritative_database = Path(inventory_database)
        if not authoritative_database.is_absolute():
            raise ValueError("inventory_database must be absolute")
    if authoritative_database is not None and page is None:
        raise CurationAuthorizationError(
            "authoritative duplicate membership requires the published curation page"
        )
    effects: list[AuthorizationEffect] = []
    total_bytes = 0
    inventory_context: AbstractContextManager[Any]
    if authoritative_database is None:
        inventory_context = nullcontext(None)
    else:
        inventory_context = _authoritative_inventory_session(authoritative_database)
    try:
        with inventory_context as inventory_connection:
            if inventory_connection is not None:
                assert page is not None
                _validate_authoritative_inventory(inventory_connection, page=page)
            for item, task_id in zip(items, task_ids, strict=True):
                if item.kind == "duplicate_group":
                    if inventory_connection is not None:
                        assert page is not None
                        reference = _authoritative_group_reference(
                            inventory_connection,
                            item,
                            page=page,
                        )
                        if reference.redundant_count > MAX_AUTHORIZATION_ITEMS - len(effects):
                            raise CurationAuthorizationError(
                                "duplicate group exceeds the physical effect bound"
                            )
                        root = Path(os.path.abspath(page.root)) if page.root is not None else None
                        if root is None:
                            raise CurationAuthorizationSnapshotChanged(
                                "curation plan root is unavailable"
                            )
                        keep = _authoritative_keeper(
                            inventory_connection,
                            reference,
                            item_id=item.item_id,
                            root=root,
                        )
                        redundant_members = _iter_authoritative_redundant_members(
                            inventory_connection,
                            reference,
                            item_id=item.item_id,
                            root=root,
                            keeper=keep,
                        )
                        expected_keeper_digest = f"{FULL_ALGORITHM}:" + reference.full_fingerprint
                    else:
                        if item.evidence.get("members_truncated") is True:
                            raise CurationAuthorizationError(
                                "duplicate group evidence is truncated and cannot be authorized"
                            )
                        members = item.evidence.get("members")
                        if not isinstance(members, list) or not members:
                            raise CurationAuthorizationError(
                                "duplicate group lacks complete member evidence"
                            )
                        keep = None
                        redundant_list: list[FileSnapshot] = []
                        for raw_member in members:
                            member = _snapshot_from_member(raw_member, item_id=item.item_id)
                            role = (
                                raw_member.get("role") if isinstance(raw_member, Mapping) else None
                            )
                            if role == "keep":
                                if keep is not None:
                                    raise CurationAuthorizationError(
                                        "duplicate group has multiple keepers"
                                    )
                                keep = member
                            elif role == "redundant":
                                redundant_list.append(member)
                            else:
                                raise CurationAuthorizationError(
                                    "duplicate group member role is unsupported"
                                )
                        if keep is None or not redundant_list:
                            raise CurationAuthorizationError(
                                "duplicate group lacks keeper or redundant members"
                            )
                        redundant_members = iter(redundant_list)
                        expected_keeper_digest = None
                    assert keep is not None
                    keeper_digest = _full_digest(keep)
                    if (
                        expected_keeper_digest is not None
                        and keeper_digest != expected_keeper_digest
                    ):
                        raise CurationAuthorizationSnapshotChanged(
                            f"keeper content changed during authorization: {keep.path}"
                        )
                    for source in redundant_members:
                        if len(effects) >= MAX_AUTHORIZATION_ITEMS:
                            raise CurationAuthorizationError(
                                "authorization exceeds the physical effect bound"
                            )
                        source_digest = _full_digest(source)
                        try:
                            if not files_equal_exact(source, keep):
                                raise CurationAuthorizationError(
                                    "duplicate group member is no longer byte-identical to its keeper"
                                )
                        except FileChangedError as exc:
                            raise CurationAuthorizationSnapshotChanged(
                                f"duplicate member changed during authorization: {source.path}"
                            ) from exc
                        effects.append(
                            AuthorizationEffect(
                                effect_id=f"{item.item_id}:effect:{len(effects) + 1}",
                                item_id=item.item_id,
                                task_id=task_id,
                                ordinal=len(effects) + 1,
                                action=action,
                                kind=item.kind,
                                source=source,
                                source_digest=source_digest,
                                keeper=keep,
                                keeper_digest=keeper_digest,
                            )
                        )
                        total_bytes += source.size
                elif item.kind in {"empty_file", "organization_plan"}:
                    source = _snapshot_from_item_evidence(item)
                    if item.kind == "organization_plan":
                        try:
                            current = snapshot_path(item.source_path)
                        except OSError as exc:
                            raise CurationAuthorizationSnapshotChanged(
                                f"organization source is unavailable: {item.source_path}"
                            ) from exc
                        if current != source:
                            raise CurationAuthorizationSnapshotChanged(
                                "organization source no longer matches its reviewed physical identity"
                            )
                    source_digest = _full_digest(source)
                    effects.append(
                        AuthorizationEffect(
                            effect_id=f"{item.item_id}:effect:{len(effects) + 1}",
                            item_id=item.item_id,
                            task_id=task_id,
                            ordinal=len(effects) + 1,
                            action=action,
                            kind=item.kind,
                            source=source,
                            source_digest=source_digest,
                            target_path=item.destination_path,
                        )
                    )
                    total_bytes += source.size
                else:
                    raise CurationAuthorizationError(
                        f"curation item kind is not effect-capable: {item.kind}"
                    )
    except CurationVerificationSnapshotChanged as exc:
        raise CurationAuthorizationSnapshotChanged(str(exc)) from exc
    except CurationVerificationError as exc:
        raise CurationAuthorizationUnavailable(str(exc)) from exc
    if not effects:
        raise CurationAuthorizationError("authorization produced no physical effects")
    if len(effects) > MAX_AUTHORIZATION_ITEMS:
        raise CurationAuthorizationError("authorization exceeds the physical effect bound")
    return tuple(effects), total_bytes


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
        review_task_heads: list[AuthorizationReviewTaskHead] = []
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
            if (
                head.task_id != record.task.task_id
                or head.task_version != record.task.task_version
                or head.event_id != record.current_event.event_id
                or head.source_snapshot_fingerprint != record.source_snapshot_fingerprint
                or head.source_input_fingerprint != record.source.fingerprint
                or head.selector_signature != record.selector_signature
            ):
                raise CurationAuthorizationSnapshotChanged(
                    "ReviewTask head changed during authorization"
                )
            item = _item_from_task(record, item_id, digest)
            total_known_bytes += _validate_requested_effect(
                item, action, state_directory=state_directory, inventory_root=page.root
            )
            records.append(record)
            items.append(item)
            review_task_heads.append(
                AuthorizationReviewTaskHead.create(
                    item_id=item_id,
                    logical_key=head.logical_key,
                    task_id=head.task_id,
                    task_version=head.task_version,
                    state=head.state.value,
                    event_id=head.event_id,
                    source_snapshot_fingerprint=head.source_snapshot_fingerprint,
                    source_input_fingerprint=head.source_input_fingerprint,
                    selector_signature=head.selector_signature,
                    decision=head.decision,
                )
            )
        if max_bytes_value < total_known_bytes:
            raise CurationAuthorizationError("max_bytes is below the reviewed item size")
        authorized_effects, effect_bytes = _effect_manifest(
            tuple(items),
            tuple(record.task.task_id for record in records),
            action,
            page=page,
            state_directory=state_directory,
        )
        # The owner is append-only but the published inventory can advance
        # while physical effects are being hashed.  Rebuild the same plan
        # identity before issuing the grant so a post-sample membership change
        # cannot be hidden behind an unchanged 64-member preview sample.
        after_page = _page(state_directory, digest)
        if (
            after_page.snapshot_id != page.snapshot_id
            or after_page.source_heads != page.source_heads
        ):
            raise CurationAuthorizationSnapshotChanged(
                "curation plan changed while authorizing duplicate membership"
            )
        if max_bytes_value < effect_bytes:
            raise CurationAuthorizationError("max_bytes is below the authorized effect size")
        root = page.root
        if root is None:
            raise CurationAuthorizationUnavailable("curation plan root cannot be snapshotted")
        try:
            root_snapshot_value = snapshot_path(root)
        except (FileNotFoundError, OSError) as exc:
            raise CurationAuthorizationUnavailable(
                "curation plan root cannot be snapshotted"
            ) from exc
        source_heads = tuple(
            CanonicalJsonObject.from_mapping(head.to_dict()) for head in page.source_heads
        )
        if not source_heads:
            raise CurationAuthorizationUnavailable("curation plan has no source-head manifest")
        from neocortex.workflow.authorization.contracts import _source_heads_digest

        source_heads_digest = _source_heads_digest(source_heads)
        semantic = {
            "action": action,
            "actor": actor_text,
            "backend": AUTHORIZATION_BACKEND,
            "expires_ns": expires_ns,
            "item_ids": list(ids),
            "max_actions": len(authorized_effects),
            "max_bytes": max_bytes_value,
            "plan_digest": digest,
            "snapshot_id": page.snapshot_id,
            "source_snapshot_fingerprint": fence.source_snapshot_fingerprint,
            "task_ids": [record.task.task_id for record in records],
            "review_task_heads": [head.to_dict() for head in review_task_heads],
            "review_task_heads_digest": review_task_heads_digest(tuple(review_task_heads)),
            "root_snapshot": {
                "root": root,
                "volume_id": f"{root_snapshot_value.volume_id:x}",
                "file_id": f"{root_snapshot_value.file_id:x}",
                "birthtime_ns": root_snapshot_value.birthtime_ns,
            },
            "source_heads": [head.to_dict() for head in source_heads],
            "source_heads_digest": source_heads_digest,
            "authorized_effects": [effect.to_dict() for effect in authorized_effects],
            "authorized_effects_digest": authorized_effects_digest(authorized_effects),
        }
        key = authorization_key
        if key is None:
            key = "curation-authorization-key-v1:" + _key_digest(semantic)
        else:
            if not isinstance(key, str) or not key or key.strip() != key or len(key) > 512:
                raise ValueError("authorization_key must be a bounded trimmed string")
        grant_digest = _key_digest({"authorization_key": key, **semantic})
        try:
            grant = AuthorizationGrant(
                grant_id=f"curation-authorization-grant-v1:{grant_digest}",
                authorization_key=key,
                scope=AUTHORIZATION_SCOPE,
                task_type=AUTHORIZATION_TASK_TYPE,
                selector_signature=AUTHORIZATION_SELECTOR_SIGNATURE,
                plan_digest=digest,
                snapshot_id=page.snapshot_id,
                source_snapshot_fingerprint=fence.source_snapshot_fingerprint,
                root=root,
                actor=actor_text,
                action=action,
                backend=AUTHORIZATION_BACKEND,
                item_ids=ids,
                task_ids=tuple(record.task.task_id for record in records),
                max_actions=len(authorized_effects),
                max_bytes=max_bytes_value,
                issued_ns=issued_ns,
                expires_ns=expires_ns,
                review_task_heads=tuple(review_task_heads),
                review_task_heads_digest=review_task_heads_digest(tuple(review_task_heads)),
                root_snapshot=AuthorizationRootSnapshot(
                    root=root,
                    volume_id=root_snapshot_value.volume_id,
                    file_id=root_snapshot_value.file_id,
                    birthtime_ns=root_snapshot_value.birthtime_ns,
                ),
                source_heads=source_heads,
                source_heads_digest=source_heads_digest,
                authorized_effects=authorized_effects,
            )
        except ValueError as exc:
            raise CurationAuthorizationError(
                f"authorization grant validation failed: {exc}"
            ) from exc
        result = issue_authorization_grant(database, grant)
        return CurationAuthorizationOutcome(result=result, items=tuple(items))


__all__ = (
    "CurationAuthorizationError",
    "CurationAuthorizationOutcome",
    "CurationAuthorizationSnapshotChanged",
    "CurationAuthorizationUnavailable",
    "authorize_curation_items",
)
