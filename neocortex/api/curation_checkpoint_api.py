"""Controlled API for durable curation page checkpoints.

Checkpoint publication is deliberately separate from ``curation_scan`` and
``curation_verify``: those existing operations remain read-only, while this
module writes only a bounded manifest under an explicitly supplied state
directory.  The API is not registered in MCP, and it never selects a corpus,
backend or filesystem effect on behalf of a caller.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.curation.checkpoints import (
    MAX_WORK_BYTES,
    MAX_WORK_FILES,
    MAX_WORK_ITEMS,
    CurationCheckpoint,
    CurationCheckpointBudget,
    CurationCheckpointCorruptError,
    CurationCheckpointError,
    CurationCheckpointRoot,
    CurationCheckpointSourceHead,
    CurationCheckpointStorageError,
    CurationCheckpointValidation,
    CurationSnapshotObservation,
    create_checkpoint,
    compute_batch_digest,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint,
)
from neocortex.curation.preview import (
    CurationPlanPage,
    CurationStateError,
    _decode_cursor,
    build_curation_plan_page,
)
from neocortex.curation.verification import (
    MAX_VERIFICATION_BYTES,
    MAX_VERIFICATION_FILES,
    MAX_VERIFICATION_ITEMS,
    CurationWorkBudget,
    verify_curation_page,
)
from neocortex.runtime.config.app_paths import default_state_directory


CURATION_CHECKPOINT_STATUS_API_SCHEMA = "neocortex.curation-checkpoint-status/v1"
CURATION_CHECKPOINT_CREATE_API_SCHEMA = "neocortex.curation-checkpoint-create/v1"
CURATION_CHECKPOINT_RESUME_API_SCHEMA = "neocortex.curation-checkpoint-resume/v1"
_CHECKPOINT_DIRECTORY = "curation/checkpoints"
_CHECKPOINT_ID = re.compile(r"^checkpoint-[0-9a-f]{32}$")
_MAX_PAGE = 100

CheckpointOperation = Literal["scan", "verify"]


class CurationCheckpointApiError(RuntimeError):
    """A bounded checkpoint API request cannot be completed safely."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _request_id(value: str | None, *, prefix: str) -> str:
    if value is None:
        return f"{prefix}-{uuid4().hex}"
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CurationCheckpointApiError("invalid_request", "request_id is invalid")
    return value


def _checkpoint_id(value: object) -> str:
    if not isinstance(value, str) or not _CHECKPOINT_ID.fullmatch(value):
        raise CurationCheckpointApiError("invalid_request", "checkpoint_id is invalid")
    return value


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PAGE:
        raise CurationCheckpointApiError("invalid_request", "checkpoint page limit is invalid")
    return value


def _optional_budget(value: object, *, label: str, maximum: int) -> int:
    if value is None:
        raise AssertionError("optional budget helper requires a default at the call site")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise CurationCheckpointApiError("invalid_request", f"{label} is outside its bound")
    return value


def _state_directory(value: object, *, required: bool) -> Path:
    if value is None:
        if required:
            raise CurationCheckpointApiError(
                "invalid_request",
                "checkpoint publication requires an explicit fixture state directory",
            )
        return default_state_directory()
    if not isinstance(value, (str, os.PathLike)):
        raise CurationCheckpointApiError("invalid_request", "state_directory is invalid")
    try:
        path = Path(os.fsdecode(os.fspath(value)))
    except TypeError as error:
        raise CurationCheckpointApiError("invalid_request", "state_directory is invalid") from error
    if not path.is_absolute():
        raise CurationCheckpointApiError("invalid_request", "state_directory must be absolute")
    return Path(os.path.abspath(path))


def _path_for(state_directory: Path, checkpoint_id: str) -> Path:
    return state_directory / _CHECKPOINT_DIRECTORY / f"{checkpoint_id}.json"


def _safe_root(value: object) -> CurationCheckpointRoot:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise CurationCheckpointApiError(
            "snapshot_changed", "published curation root is unavailable"
        )
    root = Path(os.path.normpath(value))
    if Path(os.path.realpath(root)) != root:
        raise CurationCheckpointApiError(
            "snapshot_changed", "published curation root contains a symlink"
        )
    try:
        first = root.lstat()
    except OSError as error:
        raise CurationCheckpointApiError(
            "snapshot_changed", "published curation root cannot be inspected"
        ) from error
    if stat.S_ISLNK(first.st_mode) or not stat.S_ISDIR(first.st_mode):
        raise CurationCheckpointApiError(
            "snapshot_changed", "published curation root is not a directory"
        )
    try:
        second = root.lstat()
    except OSError as error:
        raise CurationCheckpointApiError(
            "snapshot_changed", "published curation root changed"
        ) from error
    if (first.st_dev, first.st_ino, first.st_mtime_ns) != (
        second.st_dev,
        second.st_ino,
        second.st_mtime_ns,
    ):
        raise CurationCheckpointApiError("snapshot_changed", "published curation root changed")
    return CurationCheckpointRoot.from_stat_result(root, first)


def _observation_from_scan(scan: Mapping[str, object]) -> CurationSnapshotObservation:
    snapshot = scan.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise CurationCheckpointApiError("unavailable", "scan snapshot is unavailable")
    plan_digest = snapshot.get("plan_digest")
    snapshot_id = snapshot.get("snapshot_id")
    source_heads = snapshot.get("source_heads")
    if not isinstance(plan_digest, str) or not isinstance(snapshot_id, str):
        raise CurationCheckpointApiError("unavailable", "scan snapshot digests are unavailable")
    if not isinstance(source_heads, list):
        raise CurationCheckpointApiError("unavailable", "scan source heads are unavailable")
    try:
        heads = tuple(CurationCheckpointSourceHead.from_mapping(head) for head in source_heads)
        return CurationSnapshotObservation(
            root=_safe_root(snapshot.get("root")),
            source_heads=heads,
            plan_digest=plan_digest,
            snapshot_id=snapshot_id,
        )
    except CurationCheckpointApiError:
        raise
    except CurationCheckpointError as error:
        raise CurationCheckpointApiError("unavailable", "scan snapshot is not trusted") from error


def _cursor(value: object) -> str | None:
    # The planner owns its cursor encoding; do not maintain a second decoder.
    try:
        _decode_cursor(value)  # type: ignore[arg-type]
    except (CurationStateError, TypeError, ValueError) as error:
        raise CurationCheckpointApiError("invalid_request", "curation cursor is invalid") from error
    return value  # type: ignore[return-value]


def _bundle(
    *,
    state_directory: Path,
    limit: int,
    cursor: str | None,
) -> tuple[CurationPlanPage, CurationSnapshotObservation]:
    try:
        page = build_curation_plan_page(state_directory, limit, _cursor(cursor))
    except CurationStateError as error:
        message = str(error)
        code = (
            "snapshot_changed"
            if message == "curation cursor snapshot changed"
            else ("invalid_request" if message.startswith("curation cursor") else "unavailable")
        )
        raise CurationCheckpointApiError(
            code, "curation plan cannot be read: " + message
        ) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise CurationCheckpointApiError("unavailable", "curation plan cannot be read") from error
    if not isinstance(page, CurationPlanPage) or page.coverage not in {"complete", "partial"}:
        raise CurationCheckpointApiError("unavailable", "curation plan coverage is unavailable")
    observation = _observation_from_scan(
        {
            "snapshot": {
                "plan_digest": page.plan_digest,
                "snapshot_id": page.snapshot_id,
                "root": page.root,
                "source_heads": [head.to_dict() for head in page.source_heads],
            }
        }
    )
    return page, observation


def _request_budget(
    *,
    max_items: int | None,
    max_files: int | None,
    max_bytes: int | None,
) -> CurationCheckpointBudget:
    # Validate all caller bounds before constructing a page or opening corpus files.
    return CurationCheckpointBudget(
        max_items=MAX_WORK_ITEMS
        if max_items is None
        else _optional_budget(
            max_items,
            label="max_items",
            maximum=MAX_WORK_ITEMS,
        ),
        max_files=MAX_WORK_FILES
        if max_files is None
        else _optional_budget(
            max_files,
            label="max_files",
            maximum=MAX_WORK_FILES,
        ),
        max_bytes=MAX_WORK_BYTES
        if max_bytes is None
        else _optional_budget(
            max_bytes,
            label="max_bytes",
            maximum=MAX_WORK_BYTES,
        ),
    )


def _budget_available(operation: CheckpointOperation, budget: CurationCheckpointBudget) -> bool:
    return budget.items_remaining > 0 and (
        operation == "scan" or (budget.files_remaining > 0 and budget.bytes_remaining > 0)
    )


@dataclass(frozen=True)
class _PageWork:
    budget: CurationCheckpointBudget
    cursor: str | None
    traversal_complete: bool
    coverage: Literal["complete", "partial"]
    coverage_reasons: tuple[str, ...]
    batch: dict[str, object]
    stopped: bool


def _execute_page(
    operation: CheckpointOperation,
    *,
    state_directory: Path,
    page: CurationPlanPage,
    previous: CurationCheckpointBudget,
    coverage_reasons: tuple[str, ...] = (),
) -> _PageWork:
    verify: Mapping[str, object] | None = None
    completed = len(page.items)
    files = bytes_checked = 0
    stopped = False
    reasons = set(coverage_reasons)
    reasons.update(
        "source:" + head.owner + ":" + (head.reason or head.coverage)
        for head in page.source_heads
        if head.coverage != "complete"
    )
    if page.coverage != "complete" and not reasons:
        reasons.add("plan_coverage_partial")
    if operation == "verify":
        # Persisted budgets span all successors; verifier maxima bound one call.
        # Never pass a synthetic positive allowance when the real remainder is zero.
        if not _budget_available(operation, previous):
            raise CurationCheckpointApiError(
                "budget_exhausted", "checkpoint work budget is exhausted"
            )
        verification = verify_curation_page(
            page,
            budget=CurationWorkBudget(
                max_items=min(previous.items_remaining, MAX_VERIFICATION_ITEMS),
                max_files=min(previous.files_remaining, MAX_VERIFICATION_FILES),
                max_bytes=min(previous.bytes_remaining, MAX_VERIFICATION_BYTES),
            ),
            state_directory=state_directory,
        )
        if verification.status == "snapshot_changed":
            raise CurationCheckpointApiError(
                "snapshot_changed", "curation source changed during verification"
            )
        files, bytes_checked = verification.files_checked, verification.bytes_checked
        completed = 0
        for item in verification.items:
            if item.reason in {"budget_exhausted", "cancelled", "deadline_exceeded"}:
                stopped = True
                break
            completed += 1
            if item.status == "not_verified":
                reasons.add("verification:" + item.reason)
        verify = {
            "status": verification.status,
            "coverage": verification.coverage,
            "result": verification.to_dict(),
        }
    budget = replace(
        previous,
        items_completed=previous.items_completed + completed,
        files_checked=previous.files_checked + files,
        bytes_checked=previous.bytes_checked + bytes_checked,
    )
    next_cursor = page.next_cursor
    traversal_complete = completed == len(page.items) and next_cursor is None
    if completed < len(page.items):
        # The planner constructs the exact prefix cursor; an interrupted item is
        # not completed and must never be skipped by advancing to the page end.
        next_cursor = page.cursor
        if completed:
            prefix, _observation = _bundle(
                state_directory=state_directory,
                limit=completed,
                cursor=page.cursor,
            )
            if (
                prefix.plan_digest != page.plan_digest
                or prefix.snapshot_id != page.snapshot_id
                or prefix.source_heads != page.source_heads
                or prefix.items != page.items[:completed]
            ):
                raise CurationCheckpointApiError("snapshot_changed", "curation prefix changed")
            next_cursor = prefix.next_cursor
            if next_cursor is None:
                raise CurationCheckpointApiError(
                    "snapshot_changed", "curation prefix cursor disappeared"
                )
    stopped = stopped or (not traversal_complete and not _budget_available(operation, budget))
    if stopped and (completed == 0 or not _budget_available(operation, budget)):
        # With no completed item, retry would start the same indivisible group
        # under the same immutable global/per-call bounds. Remember that stop.
        # A completed prefix may still resume with a fresh per-call allowance.
        reasons.add("budget_exhausted")
    coverage: Literal["complete", "partial"] = (
        "complete" if traversal_complete and not reasons else "partial"
    )
    return _PageWork(
        budget,
        next_cursor,
        traversal_complete,
        coverage,
        tuple(sorted(reasons)),
        _batch_payload(page.to_dict(), verify),
        stopped,
    )


def _batch_payload(
    page: Mapping[str, object], verify: Mapping[str, object] | None
) -> dict[str, object]:
    payload: dict[str, object] = {"page": dict(page)}
    if verify is not None:
        result = verify.get("result")
        payload["verification"] = dict(result) if isinstance(result, Mapping) else None
        payload["status"] = verify.get("status")
    return payload


def _checkpoint_digest(checkpoint: CurationCheckpoint) -> str:
    return "sha256:" + hashlib.sha256(checkpoint.to_json().encode("utf-8")).hexdigest()


def _metadata(
    checkpoint: CurationCheckpoint, *, checkpoint_id: str | None = None
) -> dict[str, object]:
    budget = checkpoint.budget
    return {
        "checkpoint_id": checkpoint_id or checkpoint.event_id,
        "checkpoint_digest": _checkpoint_digest(checkpoint),
        "contract": checkpoint.to_dict()["contract"],
        "operation": checkpoint.operation,
        "state": checkpoint.state,
        "event_id": checkpoint.event_id,
        "cursor": checkpoint.cursor,
        "batch_digest": checkpoint.batch_digest,
        "previous_checkpoint_digest": checkpoint.previous_checkpoint_digest,
        "plan_digest": checkpoint.plan_digest,
        "snapshot_id": checkpoint.snapshot_id,
        "source_heads_digest": checkpoint.source_heads_digest,
        "root": checkpoint.root.to_dict(),
        "source_heads": [head.to_dict() for head in checkpoint.source_heads],
        "limit": checkpoint.page_limit,
        "limit_source": "persisted" if checkpoint.schema_version == 2 else "legacy_default",
        "traversal_complete": checkpoint.traversal_complete,
        "coverage": checkpoint.coverage,
        "coverage_reasons": list(checkpoint.coverage_reasons),
        "budget": {
            **budget.to_dict(),
            "items_remaining": budget.items_remaining,
            "files_remaining": budget.files_remaining,
            "bytes_remaining": budget.bytes_remaining,
        },
    }


def _envelope(
    *,
    schema: str,
    operation: str,
    request_id: str,
    status: str,
    coverage: str,
    read_only: bool,
    result: Mapping[str, object] | None,
    error: Mapping[str, object] | None,
    exit_code: int,
) -> dict[str, object]:
    return {
        "schema": schema,
        "schema_version": 1,
        "kind": "neocortex_curation_checkpoint",
        "operation": operation,
        "request_id": request_id,
        "plan_id": None if result is None else result.get("plan_digest"),
        "scope": "personal",
        "status": status,
        "coverage": coverage,
        "read_only": read_only,
        "effects": {
            "state": "none" if read_only else "curation_checkpoint",
            "corpus": "none",
            "external": "none",
        },
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "tools_authorized": False,
            "actions_authorized": False,
            "resume_authorized": False,
        },
        "snapshot": None
        if result is None
        else {
            "root": result.get("root"),
            "source_heads": result.get("source_heads", []),
            "plan_digest": result.get("plan_digest"),
            "snapshot_id": result.get("snapshot_id"),
        },
        "result": None if result is None else dict(result),
        "error": None if error is None else dict(error),
        "exit_code": exit_code,
    }


def _error_envelope(
    *,
    schema: str,
    operation: str,
    request_id: str,
    error: BaseException | CurationCheckpointApiError,
    read_only: bool,
) -> dict[str, object]:
    if isinstance(error, CurationCheckpointApiError):
        code = error.code
        retryable = error.retryable
    elif isinstance(error, CurationCheckpointCorruptError):
        code, retryable = "corrupt", False
    elif isinstance(error, CurationCheckpointStorageError):
        code, retryable = "unavailable", False
    elif isinstance(error, CurationCheckpointError):
        code, retryable = "invalid_request", False
    else:
        code, retryable = "unavailable", False
    exit_code = {
        "invalid_request": 2,
        "invalid_cursor": 2,
        "budget_exhausted": 2,
        "corrupt": 7,
        "schema_incompatible": 7,
        "snapshot_changed": 5,
        "unavailable": 1,
        "partial": 2,
    }.get(code, 1)
    top_status = "snapshot_changed" if code == "snapshot_changed" else "unavailable"
    return _envelope(
        schema=schema,
        operation=operation,
        request_id=request_id,
        status=top_status,
        coverage="unavailable",
        read_only=read_only,
        result=None,
        error={
            "code": code,
            "message": sanitize_untrusted_text(error, limit=800),
            "retryable": retryable,
        },
        exit_code=exit_code,
    )


def _resume_metadata(checkpoint: CurationCheckpoint) -> dict[str, object]:
    terminal = checkpoint.state == "complete" or checkpoint.traversal_complete
    exhausted = (
        not _budget_available(checkpoint.operation, checkpoint.budget)
        or "budget_exhausted" in checkpoint.coverage_reasons
    )
    legacy_unknown = checkpoint.schema_version == 1 and not terminal and checkpoint.cursor is None
    resume_status = (
        "invalid"
        if legacy_unknown
        else "complete"
        if terminal and checkpoint.coverage == "complete"
        else "partial"
        if terminal
        else "budget_exhausted"
        if exhausted
        else "resume"
    )
    return {
        "resume_status": resume_status,
        "replay_required": not terminal and not exhausted and not legacy_unknown,
        "reason_code": (
            "legacy_continuation_unproven"
            if legacy_unknown
            else "coverage_partial"
            if terminal and checkpoint.coverage != "complete"
            else "checkpoint_complete"
            if terminal
            else "budget_exhausted"
            if exhausted
            else "snapshot_match"
        ),
    }


def _published_payload(
    checkpoint: CurationCheckpoint,
    *,
    schema: str,
    operation: str,
    request: str,
    read_only: bool,
    resumed_from: str | None = None,
    stopped: bool = False,
) -> dict[str, object]:
    result = _metadata(checkpoint)
    result.update(_resume_metadata(checkpoint))
    if resumed_from is not None:
        result["resumed_from"] = resumed_from
    if stopped:
        result["reason_code"] = "budget_exhausted"
    return _envelope(
        schema=schema,
        operation=operation,
        request_id=request,
        status="partial" if stopped else "complete",
        coverage=checkpoint.coverage,
        read_only=read_only,
        result=result,
        error={
            "code": "budget_exhausted",
            "message": "checkpoint work budget is exhausted",
            "retryable": False,
        }
        if stopped
        else None,
        exit_code=2 if stopped else 0,
    )


def _live_validation(checkpoint: CurationCheckpoint, state: Path) -> CurationCheckpointValidation:
    if checkpoint.state in {"invalid", "snapshot_changed"}:
        return validate_checkpoint(checkpoint, lambda: (_ for _ in ()).throw(RuntimeError()))
    # Observe current heads without ever applying the previous snapshot's cursor.
    _page, observation = _bundle(state_directory=state, limit=1, cursor=None)
    return validate_checkpoint(checkpoint, lambda: observation)


def _rejected_snapshot(
    validation: CurationCheckpointValidation,
    *,
    schema: str,
    operation: str,
    request: str,
) -> dict[str, object]:
    result = _metadata(validation.checkpoint)
    result.update(
        {
            "resume_status": validation.status,
            "reason_code": validation.reason_code,
            "replay_required": False,
            "stored_coverage": validation.checkpoint.coverage,
            "coverage": "unavailable",
            "observed": None
            if validation.observed is None
            else {
                "root": validation.observed.root.to_dict(),
                "source_heads": [head.to_dict() for head in validation.observed.source_heads],
                "plan_digest": validation.observed.plan_digest,
                "snapshot_id": validation.observed.snapshot_id,
            },
        }
    )
    code = "snapshot_changed" if validation.status == "snapshot_changed" else "corrupt"
    return _envelope(
        schema=schema,
        operation=operation,
        request_id=request,
        status=validation.status,
        coverage="unavailable",
        read_only=True,
        result=result,
        error={"code": code, "message": validation.detail, "retryable": False},
        exit_code=5 if code == "snapshot_changed" else 7,
    )


def curation_checkpoint_create_payload(
    operation: CheckpointOperation,
    *,
    plan_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    max_items: int | None = None,
    max_files: int | None = None,
    max_bytes: int | None = None,
    state_directory: str | Path | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Publish bounded progress even when verification stops inside a page."""
    try:
        request = _request_id(request_id, prefix="curation-checkpoint-create")
        if not isinstance(operation, str) or operation not in {"scan", "verify"}:
            raise CurationCheckpointApiError("invalid_request", "checkpoint operation is invalid")
        page_limit = _limit(limit)
        budget = _request_budget(max_items=max_items, max_files=max_files, max_bytes=max_bytes)
        cursor = _cursor(cursor)
        if plan_id is not None and (
            not isinstance(plan_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", plan_id) is None
        ):
            raise CurationCheckpointApiError("invalid_request", "plan_id is invalid")
        if operation == "verify" and plan_id is None:
            raise CurationCheckpointApiError(
                "invalid_request", "verify checkpoints require plan_id"
            )
        state_root = _state_directory(state_directory, required=True)
        page, observation = _bundle(
            state_directory=state_root,
            limit=min(page_limit, budget.items_remaining),
            cursor=cursor,
        )
        if plan_id is not None and plan_id != observation.plan_digest:
            raise CurationCheckpointApiError("snapshot_changed", "curation plan digest changed")
        if max_items is None:
            budget = replace(budget, max_items=min(MAX_WORK_ITEMS, max(page.items_total, 1)))
        work = _execute_page(operation, state_directory=state_root, page=page, previous=budget)
        batch_digest = compute_batch_digest(
            operation=operation,
            plan_digest=observation.plan_digest,
            snapshot_id=observation.snapshot_id,
            cursor_before=cursor,
            cursor_after=work.cursor,
            batch=work.batch,
            budget=work.budget,
        )
        checkpoint = create_checkpoint(
            operation=operation,
            state="complete" if work.coverage == "complete" else "partial",
            root=observation.root,
            source_heads=observation.source_heads,
            plan_digest=observation.plan_digest,
            snapshot_id=observation.snapshot_id,
            cursor=work.cursor,
            batch_digest=batch_digest,
            budget=work.budget,
            page_limit=page_limit,
            traversal_complete=work.traversal_complete,
            coverage=work.coverage,
            coverage_reasons=work.coverage_reasons,
        )
        write_checkpoint(_path_for(state_root, checkpoint.event_id), checkpoint)
        return _published_payload(
            checkpoint,
            schema=CURATION_CHECKPOINT_CREATE_API_SCHEMA,
            operation="curation-checkpoint-create",
            request=request,
            read_only=False,
            stopped=work.stopped,
        )
    except (
        CurationCheckpointApiError,
        CurationCheckpointError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        request = locals().get("request", f"curation-checkpoint-create-{uuid4().hex}")
        return _error_envelope(
            schema=CURATION_CHECKPOINT_CREATE_API_SCHEMA,
            operation="curation-checkpoint-create",
            request_id=str(request),
            error=error,
            read_only=False,
        )


def curation_checkpoint_status_payload(
    checkpoint_id: str,
    *,
    state_directory: str | Path | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Revalidate all checkpoints, including exhausted traversals, without replay."""
    try:
        request = _request_id(request_id, prefix="curation-checkpoint-status")
        identifier = _checkpoint_id(checkpoint_id)
        state_root = _state_directory(state_directory, required=False)
        checkpoint = read_checkpoint(_path_for(state_root, identifier))
        validation = _live_validation(checkpoint, state_root)
        if validation.status != "valid":
            return _rejected_snapshot(
                validation,
                schema=CURATION_CHECKPOINT_STATUS_API_SCHEMA,
                operation="curation-checkpoint-status",
                request=request,
            )
        _cursor(checkpoint.cursor)
        payload = _published_payload(
            checkpoint,
            schema=CURATION_CHECKPOINT_STATUS_API_SCHEMA,
            operation="curation-checkpoint-status",
            request=request,
            read_only=True,
        )
        result = payload["result"]
        if isinstance(result, dict) and validation.observed is not None:
            result["observed"] = {
                "root": validation.observed.root.to_dict(),
                "source_heads": [head.to_dict() for head in validation.observed.source_heads],
                "plan_digest": validation.observed.plan_digest,
                "snapshot_id": validation.observed.snapshot_id,
            }
        return payload
    except (
        CurationCheckpointApiError,
        CurationCheckpointError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        request = locals().get("request", f"curation-checkpoint-status-{uuid4().hex}")
        return _error_envelope(
            schema=CURATION_CHECKPOINT_STATUS_API_SCHEMA,
            operation="curation-checkpoint-status",
            request_id=str(request),
            error=error,
            read_only=True,
        )


def curation_checkpoint_resume_payload(
    checkpoint_id: str,
    *,
    state_directory: str | Path | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Revalidate before replay and spend only the immutable remaining budget."""
    try:
        request = _request_id(request_id, prefix="curation-checkpoint-resume")
        identifier = _checkpoint_id(checkpoint_id)
        state_root = _state_directory(state_directory, required=True)
        checkpoint = read_checkpoint(_path_for(state_root, identifier))
        validation = _live_validation(checkpoint, state_root)
        if validation.status != "valid":
            return _rejected_snapshot(
                validation,
                schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
                operation="curation-checkpoint-resume",
                request=request,
            )
        _cursor(checkpoint.cursor)
        exhausted = (
            not _budget_available(checkpoint.operation, checkpoint.budget)
            or "budget_exhausted" in checkpoint.coverage_reasons
        )
        if (
            checkpoint.schema_version == 1
            and checkpoint.state != "complete"
            and checkpoint.cursor is None
        ):
            result = _metadata(checkpoint)
            result.update(_resume_metadata(checkpoint))
            result["resumed_from"] = identifier
            return _envelope(
                schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
                operation="curation-checkpoint-resume",
                request_id=request,
                status="unavailable",
                coverage="partial",
                read_only=True,
                result=result,
                error={
                    "code": "schema_incompatible",
                    "retryable": False,
                    "message": "legacy checkpoint has no provable continuation; create a new checkpoint",
                },
                exit_code=7,
            )
        if checkpoint.state == "complete" or checkpoint.traversal_complete or exhausted:
            return _published_payload(
                checkpoint,
                schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
                operation="curation-checkpoint-resume",
                request=request,
                read_only=True,
                resumed_from=identifier,
                stopped=exhausted and not checkpoint.traversal_complete,
            )
        page, observation = _bundle(
            state_directory=state_root,
            limit=min(checkpoint.page_limit, checkpoint.budget.items_remaining),
            cursor=checkpoint.cursor,
        )
        # Recheck the page itself before verification, not just the earlier head read.
        validation = validate_checkpoint(checkpoint, lambda: observation)
        if validation.status != "valid":
            return _rejected_snapshot(
                validation,
                schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
                operation="curation-checkpoint-resume",
                request=request,
            )
        work = _execute_page(
            checkpoint.operation,
            state_directory=state_root,
            page=page,
            previous=checkpoint.budget,
            coverage_reasons=checkpoint.coverage_reasons,
        )
        if (
            work.budget == checkpoint.budget
            and work.cursor == checkpoint.cursor
            and work.traversal_complete == checkpoint.traversal_complete
            and work.coverage_reasons == checkpoint.coverage_reasons
        ):
            # A too-small unchanged budget must not create an infinite successor chain.
            return _published_payload(
                checkpoint,
                schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
                operation="curation-checkpoint-resume",
                request=request,
                read_only=True,
                resumed_from=identifier,
                stopped=work.stopped,
            )
        batch_digest = compute_batch_digest(
            operation=checkpoint.operation,
            plan_digest=checkpoint.plan_digest,
            snapshot_id=checkpoint.snapshot_id,
            cursor_before=checkpoint.cursor,
            cursor_after=work.cursor,
            batch=work.batch,
            budget=work.budget,
        )
        event_seed = f"{checkpoint.event_id}\0{batch_digest}\0{work.cursor or ''}".encode("utf-8")
        event_id = "checkpoint-" + hashlib.sha256(event_seed).hexdigest()[:32]
        successor = create_checkpoint(
            operation=checkpoint.operation,
            state="complete" if work.coverage == "complete" else "partial",
            root=observation.root,
            source_heads=observation.source_heads,
            plan_digest=checkpoint.plan_digest,
            snapshot_id=checkpoint.snapshot_id,
            cursor=work.cursor,
            batch_digest=batch_digest,
            budget=work.budget,
            event_id=event_id,
            previous_checkpoint_digest=_checkpoint_digest(checkpoint),
            page_limit=checkpoint.page_limit,
            traversal_complete=work.traversal_complete,
            coverage=work.coverage,
            coverage_reasons=work.coverage_reasons,
        )
        write_checkpoint(_path_for(state_root, successor.event_id), successor)
        return _published_payload(
            successor,
            schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
            operation="curation-checkpoint-resume",
            request=request,
            read_only=False,
            resumed_from=identifier,
            stopped=work.stopped,
        )
    except (
        CurationCheckpointApiError,
        CurationCheckpointError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        request = locals().get("request", f"curation-checkpoint-resume-{uuid4().hex}")
        return _error_envelope(
            schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
            operation="curation-checkpoint-resume",
            request_id=str(request),
            error=error,
            read_only=False,
        )


__all__ = (
    "CURATION_CHECKPOINT_CREATE_API_SCHEMA",
    "CURATION_CHECKPOINT_RESUME_API_SCHEMA",
    "CURATION_CHECKPOINT_STATUS_API_SCHEMA",
    "CurationCheckpointApiError",
    "curation_checkpoint_create_payload",
    "curation_checkpoint_resume_payload",
    "curation_checkpoint_status_payload",
)
