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
from pathlib import Path
from typing import Literal
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.curation.checkpoints import (
    CURATION_CHECKPOINT_CONTRACT,
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
    CurationSnapshotObservation,
    create_checkpoint,
    compute_batch_digest,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint,
)
from neocortex.curation.preview import CurationPlanPage, build_curation_plan_page
from neocortex.curation.verification import verify_curation_page
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
    try:
        path = Path(value)
    except TypeError as error:
        raise CurationCheckpointApiError("invalid_request", "state_directory is invalid") from error
    if not path.is_absolute():
        raise CurationCheckpointApiError("invalid_request", "state_directory must be absolute")
    return Path(os.path.abspath(path))


def _path_for(state_directory: Path, checkpoint_id: str) -> Path:
    return state_directory / _CHECKPOINT_DIRECTORY / f"{checkpoint_id}.json"


def _safe_root(value: object) -> CurationCheckpointRoot:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise CurationCheckpointApiError("snapshot_changed", "published curation root is unavailable")
    root = Path(os.path.normpath(value))
    if Path(os.path.realpath(root)) != root:
        raise CurationCheckpointApiError("snapshot_changed", "published curation root contains a symlink")
    try:
        first = root.lstat()
    except OSError as error:
        raise CurationCheckpointApiError("snapshot_changed", "published curation root cannot be inspected") from error
    if stat.S_ISLNK(first.st_mode) or not stat.S_ISDIR(first.st_mode):
        raise CurationCheckpointApiError("snapshot_changed", "published curation root is not a directory")
    try:
        second = root.lstat()
    except OSError as error:
        raise CurationCheckpointApiError("snapshot_changed", "published curation root changed") from error
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


def _bundle(
    operation: CheckpointOperation,
    *,
    state_directory: Path,
    plan_id: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[Mapping[str, object], Mapping[str, object] | None, dict[str, object], CurationSnapshotObservation]:
    try:
        page_object = build_curation_plan_page(state_directory, limit, cursor)
    except (OSError, RuntimeError, ValueError) as error:
        raise CurationCheckpointApiError("unavailable", "curation plan cannot be read") from error
    if not isinstance(page_object, CurationPlanPage):
        raise CurationCheckpointApiError("unavailable", "curation plan page is invalid")
    if page_object.coverage not in {"complete", "partial"}:
        raise CurationCheckpointApiError("unavailable", "curation plan coverage is unavailable")
    page = page_object.to_dict()
    source_heads = [head.to_dict() for head in page_object.source_heads]
    scan: Mapping[str, object] = {
        "status": "complete" if page_object.coverage == "complete" else "partial",
        "coverage": page_object.coverage,
        "snapshot": {
            "plan_digest": page_object.plan_digest,
            "snapshot_id": page_object.snapshot_id,
            "root": page_object.root,
            "scan_id": page_object.scan_id,
            "source_heads": source_heads,
        },
        "result": {
            "plan_digest": page_object.plan_digest,
            "snapshot_id": page_object.snapshot_id,
            "scan_id": page_object.scan_id,
            "root": page_object.root,
            "source_heads": source_heads,
            "page": page,
            "source": "published_curation_plan",
        },
        "error": None,
    }
    verify: Mapping[str, object] | None = None
    if operation == "verify":
        if plan_id is None:
            raise CurationCheckpointApiError("invalid_request", "verify checkpoints require plan_id")
        if plan_id != page_object.plan_digest:
            raise CurationCheckpointApiError("snapshot_changed", "curation plan digest changed")
        verification = verify_curation_page(page_object)
        if verification.status == "snapshot_changed":
            raise CurationCheckpointApiError("snapshot_changed", "curation source changed during verification")
        verify = {
            "status": verification.status,
            "coverage": verification.coverage,
            "result": verification.to_dict(),
            "error": None,
        }
    observation = _observation_from_scan(scan)
    return scan, verify, page, observation


def _budget_for_page(
    page: Mapping[str, object],
    verify: Mapping[str, object] | None,
    *,
    max_items: int | None,
    max_files: int | None,
    max_bytes: int | None,
    previous: CurationCheckpointBudget | None = None,
) -> CurationCheckpointBudget:
    items = page.get("items")
    item_count = len(items) if isinstance(items, list) else 0
    total = page.get("items_total")
    total_items = total if isinstance(total, int) and not isinstance(total, bool) else item_count
    default_items = max(total_items, item_count, 1)
    limit_items = default_items if max_items is None else _optional_budget(
        max_items, label="max_items", maximum=MAX_WORK_ITEMS
    )
    limit_files = MAX_WORK_FILES if max_files is None else _optional_budget(
        max_files, label="max_files", maximum=MAX_WORK_FILES
    )
    limit_bytes = MAX_WORK_BYTES if max_bytes is None else _optional_budget(
        max_bytes, label="max_bytes", maximum=MAX_WORK_BYTES
    )
    result = verify.get("result") if isinstance(verify, Mapping) else None
    files = result.get("files_checked", 0) if isinstance(result, Mapping) else 0
    bytes_checked = result.get("bytes_checked", 0) if isinstance(result, Mapping) else 0
    if not isinstance(files, int) or isinstance(files, bool) or files < 0:
        raise CurationCheckpointApiError("unavailable", "verify file counter is invalid")
    if not isinstance(bytes_checked, int) or isinstance(bytes_checked, bool) or bytes_checked < 0:
        raise CurationCheckpointApiError("unavailable", "verify byte counter is invalid")
    previous_items = 0 if previous is None else previous.items_completed
    previous_files = 0 if previous is None else previous.files_checked
    previous_bytes = 0 if previous is None else previous.bytes_checked
    try:
        return CurationCheckpointBudget(
            max_items=limit_items,
            max_files=limit_files,
            max_bytes=limit_bytes,
            items_completed=previous_items + item_count,
            files_checked=previous_files + files,
            bytes_checked=previous_bytes + bytes_checked,
        )
    except CurationCheckpointError as error:
        raise CurationCheckpointApiError("budget_exhausted", "checkpoint work budget is exhausted") from error


def _batch_payload(page: Mapping[str, object], verify: Mapping[str, object] | None) -> dict[str, object]:
    payload: dict[str, object] = {"page": dict(page)}
    if verify is not None:
        result = verify.get("result")
        payload["verification"] = dict(result) if isinstance(result, Mapping) else None
        payload["status"] = verify.get("status")
    return payload


def _checkpoint_digest(checkpoint: CurationCheckpoint) -> str:
    return "sha256:" + hashlib.sha256(checkpoint.to_json().encode("utf-8")).hexdigest()


def _metadata(checkpoint: CurationCheckpoint, *, checkpoint_id: str | None = None) -> dict[str, object]:
    budget = checkpoint.budget
    return {
        "checkpoint_id": checkpoint_id or checkpoint.event_id,
        "checkpoint_digest": _checkpoint_digest(checkpoint),
        "contract": CURATION_CHECKPOINT_CONTRACT,
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
        "snapshot": None if result is None else {
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
    """Publish one bounded scan/verify page checkpoint on explicit state."""

    try:
        request = _request_id(request_id, prefix="curation-checkpoint-create")
        if operation not in {"scan", "verify"}:
            raise CurationCheckpointApiError("invalid_request", "checkpoint operation is invalid")
        page_limit = _limit(limit)
        state_root = _state_directory(state_directory, required=True)
        if plan_id is not None and (not isinstance(plan_id, str) or plan_id.strip() != plan_id):
            raise CurationCheckpointApiError("invalid_request", "plan_id is invalid")
        _scan, verify, page, observation = _bundle(
            operation,
            state_directory=state_root,
            plan_id=plan_id,
            limit=page_limit,
            cursor=cursor,
        )
        effective_plan = observation.plan_digest
        budget = _budget_for_page(
            page,
            verify,
            max_items=max_items,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        next_cursor = page.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise CurationCheckpointApiError("unavailable", "scan next cursor is invalid")
        operation_status = "complete" if next_cursor is None and (
            verify is None or verify.get("status") == "complete"
        ) else "partial"
        batch = _batch_payload(page, verify)
        batch_digest = compute_batch_digest(
            operation=operation,
            plan_digest=effective_plan,
            snapshot_id=observation.snapshot_id,
            cursor_before=page.get("cursor"),
            cursor_after=next_cursor,
            batch=batch,
            budget=budget,
        )
        checkpoint = create_checkpoint(
            operation=operation,
            state=operation_status,  # type: ignore[arg-type]
            root=observation.root,
            source_heads=observation.source_heads,
            plan_digest=effective_plan,
            snapshot_id=observation.snapshot_id,
            cursor=next_cursor,
            batch_digest=batch_digest,
            budget=budget,
        )
        target = _path_for(state_root, checkpoint.event_id)
        write_checkpoint(target, checkpoint)
        result = _metadata(checkpoint)
        return _envelope(
            schema=CURATION_CHECKPOINT_CREATE_API_SCHEMA,
            operation="curation-checkpoint-create",
            request_id=request,
            status="complete",
            coverage="complete" if checkpoint.state == "complete" else "partial",
            read_only=False,
            result=result,
            error=None,
            exit_code=0,
        )
    except (CurationCheckpointApiError, CurationCheckpointError, OSError, TypeError, ValueError) as error:
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
    """Inspect and revalidate one checkpoint without writing state."""

    try:
        request = _request_id(request_id, prefix="curation-checkpoint-status")
        identifier = _checkpoint_id(checkpoint_id)
        state_root = _state_directory(state_directory, required=False)
        checkpoint = read_checkpoint(_path_for(state_root, identifier))
        if checkpoint.state in {"invalid", "snapshot_changed"}:
            validation = validate_checkpoint(checkpoint, lambda: (_ for _ in ()).throw(RuntimeError()))
        else:
            scan, _verify, _page, observation = _bundle(
                "scan",
                state_directory=state_root,
                plan_id=None,
                limit=_MAX_PAGE,
                cursor=checkpoint.cursor,
            )
            validation = validate_checkpoint(checkpoint, lambda: observation)
        result = _metadata(checkpoint, checkpoint_id=identifier)
        result.update(
            {
                "resume_status": (
                    "resume" if validation.resumable else "complete" if checkpoint.state == "complete" else validation.status
                ),
                "reason_code": validation.reason_code,
                "replay_required": validation.resumable,
                "observed": None if validation.observed is None else {
                    "root": validation.observed.root.to_dict(),
                    "source_heads": [head.to_dict() for head in validation.observed.source_heads],
                    "plan_digest": validation.observed.plan_digest,
                    "snapshot_id": validation.observed.snapshot_id,
                },
            }
        )
        if validation.status == "valid":
            return _envelope(
                schema=CURATION_CHECKPOINT_STATUS_API_SCHEMA,
                operation="curation-checkpoint-status",
                request_id=request,
                status="complete",
                coverage="complete",
                read_only=True,
                result=result,
                error=None,
                exit_code=0,
            )
        code = "snapshot_changed" if validation.status == "snapshot_changed" else "corrupt"
        error = {"code": code, "message": validation.detail, "retryable": False}
        return _envelope(
            schema=CURATION_CHECKPOINT_STATUS_API_SCHEMA,
            operation="curation-checkpoint-status",
            request_id=request,
            status=validation.status,
            coverage="unavailable",
            read_only=True,
            result=result,
            error=error,
            exit_code=5 if code == "snapshot_changed" else 7,
        )
    except (CurationCheckpointApiError, CurationCheckpointError, OSError, TypeError, ValueError) as error:
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
    """Replay the next bounded page and publish a deterministic successor checkpoint."""

    try:
        request = _request_id(request_id, prefix="curation-checkpoint-resume")
        identifier = _checkpoint_id(checkpoint_id)
        state_root = _state_directory(state_directory, required=True)
        checkpoint = read_checkpoint(_path_for(state_root, identifier))
        if checkpoint.state == "complete":
            result = _metadata(checkpoint, checkpoint_id=identifier)
            result.update({"resumed_from": identifier, "replay_required": False})
            return _envelope(
                schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
                operation="curation-checkpoint-resume",
                request_id=request,
                status="complete",
                coverage="complete",
                read_only=True,
                result=result,
                error=None,
                exit_code=0,
            )
        scan, verify, page, observation = _bundle(
            checkpoint.operation,
            state_directory=state_root,
            plan_id=checkpoint.plan_digest if checkpoint.operation == "verify" else None,
            limit=_MAX_PAGE,
            cursor=checkpoint.cursor,
        )
        validation = validate_checkpoint(checkpoint, lambda: observation)
        if validation.status != "valid":
            error = CurationCheckpointApiError(
                validation.status,
                validation.detail,
                retryable=False,
            )
            raise error
        previous_budget = checkpoint.budget
        budget = _budget_for_page(
            page,
            verify,
            max_items=previous_budget.max_items,
            max_files=previous_budget.max_files,
            max_bytes=previous_budget.max_bytes,
            previous=previous_budget,
        )
        next_cursor = page.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise CurationCheckpointApiError("unavailable", "resume next cursor is invalid")
        next_state = "complete" if next_cursor is None and (
            verify is None or verify.get("status") == "complete"
        ) else "partial"
        batch = _batch_payload(page, verify)
        batch_digest = compute_batch_digest(
            operation=checkpoint.operation,
            plan_digest=checkpoint.plan_digest,
            snapshot_id=checkpoint.snapshot_id,
            cursor_before=checkpoint.cursor,
            cursor_after=next_cursor,
            batch=batch,
            budget=budget,
        )
        event_seed = (
            f"{checkpoint.event_id}\0{batch_digest}\0{next_cursor or ''}".encode("utf-8")
        )
        event_id = "checkpoint-" + hashlib.sha256(event_seed).hexdigest()[:32]
        successor = create_checkpoint(
            operation=checkpoint.operation,
            state=next_state,  # type: ignore[arg-type]
            root=observation.root,
            source_heads=observation.source_heads,
            plan_digest=checkpoint.plan_digest,
            snapshot_id=checkpoint.snapshot_id,
            cursor=next_cursor,
            batch_digest=batch_digest,
            budget=budget,
            event_id=event_id,
            previous_checkpoint_digest=_checkpoint_digest(checkpoint),
        )
        write_checkpoint(_path_for(state_root, successor.event_id), successor)
        result = _metadata(successor)
        result.update({"resumed_from": identifier, "replay_required": successor.state != "complete"})
        return _envelope(
            schema=CURATION_CHECKPOINT_RESUME_API_SCHEMA,
            operation="curation-checkpoint-resume",
            request_id=request,
            status="complete",
            coverage="complete" if successor.state == "complete" else "partial",
            read_only=False,
            result=result,
            error=None,
            exit_code=0,
        )
    except (CurationCheckpointApiError, CurationCheckpointError, OSError, TypeError, ValueError) as error:
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
