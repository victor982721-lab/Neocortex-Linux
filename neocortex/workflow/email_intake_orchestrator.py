"""Framework-owned integration for physical EML attachment children.

This stage is intentionally narrow: Identify supplies the message/rfc822
decision, the Text owner parses/materializes bounded children, and one normal
successor Inventory/Identify pass hands the new physical files to existing
deduplication and routes.  It is not a second content-ingestion pipeline.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from neocortex.capabilities.formats.text.email_intake import (
    EmailAttachment,
    EmailAttachmentError,
    EmailAttachmentLimits,
    EmailAttachmentResolution,
    materialize_email_attachments,
    prepare_email_attachments,
)
from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.deduplication.admission import size_is_admitted, validate_max_file_bytes
from neocortex.deduplication.fingerprinting import FULL_ALGORITHM
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from neocortex.persistence.framework_state_types import RunBudgetExceeded

if TYPE_CHECKING:
    from neocortex.integrations.inventory.inventory_boundary import NormalInventoryBoundary
    from neocortex.runtime.control.cancellation import CancellationToken


EMAIL_INTAKE_SCHEMA = "neocortex.email-intake/v1"
EMAIL_INTAKE_STAGE = "email-intake"


@dataclass(frozen=True, slots=True)
class EmailIntakeStageResult:
    """Bounded stage projection consumed by Framework lifecycle reporting."""

    status: str
    parents_examined: int = 0
    parents_with_attachments: int = 0
    attachments_planned: int = 0
    attachments_materialized: int = 0
    attachments_replayed: int = 0
    attachments_consumed: int = 0
    failed: int = 0
    filesystem_changed: bool = False
    reconciliation_required: bool = False
    errors: tuple[Mapping[str, object], ...] = ()
    created_paths: tuple[str, ...] = ()
    consumption_provenance: tuple[Mapping[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": EMAIL_INTAKE_SCHEMA,
            "status": self.status,
            "parents_examined": self.parents_examined,
            "parents_with_attachments": self.parents_with_attachments,
            "attachments_planned": self.attachments_planned,
            "attachments_materialized": self.attachments_materialized,
            "attachments_replayed": self.attachments_replayed,
            "attachments_consumed": self.attachments_consumed,
            "failed": self.failed,
            "filesystem_changed": self.filesystem_changed,
            "reconciliation_required": self.reconciliation_required,
            "errors": [dict(error) for error in self.errors[:32]],
            "created_paths": list(self.created_paths[:256]),
            "consumption_provenance": [dict(item) for item in self.consumption_provenance[:32]],
        }


def _detection_key(snapshot: FileSnapshot) -> tuple[int, int, int, int, int]:
    return (
        int(snapshot.volume_id),
        int(snapshot.file_id),
        int(snapshot.size),
        int(snapshot.mtime_ns),
        int(snapshot.birthtime_ns),
    )


def _parent_key(snapshot: FileSnapshot) -> str:
    return (
        f"{snapshot.volume_id:x}-{snapshot.file_id:x}-"
        f"{snapshot.size:x}-{snapshot.mtime_ns:x}"
    )


def _inside_corpus(path: Path, root: Path) -> bool:
    try:
        return path.is_absolute() and path.resolve(strict=True) == path and path.is_relative_to(root)
    except OSError:
        return False


def _email_limits(config: object) -> EmailAttachmentLimits:
    return EmailAttachmentLimits(
        max_parts=int(getattr(config, "email_max_parts", 4_096)),
        max_depth=int(getattr(config, "email_max_depth", 64)),
        max_part_bytes=int(getattr(config, "email_max_part_bytes", 8 * 1024 * 1024)),
        max_total_bytes=int(getattr(config, "email_max_total_bytes", 64 * 1024 * 1024)),
        max_source_bytes=int(getattr(config, "email_max_source_bytes", 256 * 1024 * 1024)),
    )


def _ensure_attachment_parent(root: Path, mutation_guard: object | None) -> Path:
    """Create the route parent only after guard and pinned-root validation."""

    if mutation_guard is None:
        raise EmailAttachmentError("unsafe", "mutation_guard_required")
    destination_parent = root / "Adjuntos_de_correos"
    require_paths_allowed = getattr(mutation_guard, "require_paths_allowed", None)
    if not callable(require_paths_allowed):
        raise EmailAttachmentError("unsafe", "mutation_guard_invalid")
    require_paths_allowed(root, destination_parent)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise EmailAttachmentError("blocked", "corpus_root_open_failed", str(exc)) from exc
    child_fd = -1
    try:
        root_stat = os.fstat(root_fd)
        current = root.lstat()
        if (
            (int(root_stat.st_dev), int(root_stat.st_ino))
            != (int(current.st_dev), int(current.st_ino))
            or not os.path.isdir(root)
        ):
            raise EmailAttachmentError("source_changed", "corpus_root_changed")
        try:
            os.mkdir("Adjuntos_de_correos", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        child_fd = os.open("Adjuntos_de_correos", flags, dir_fd=root_fd)
        child_stat = os.fstat(child_fd)
        child_path_stat = destination_parent.lstat()
        if (
            (int(child_stat.st_dev), int(child_stat.st_ino))
            != (int(child_path_stat.st_dev), int(child_path_stat.st_ino))
            or not os.path.isdir(destination_parent)
        ):
            raise EmailAttachmentError("source_changed", "attachment_root_changed")
    except EmailAttachmentError:
        raise
    except OSError as exc:
        raise EmailAttachmentError("blocked", "attachment_root_unavailable", str(exc)) from exc
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(root_fd)
    require_paths_allowed(root, destination_parent)
    return destination_parent


def _resolver_for_inventory(
    *,
    dedup_index: DedupIndex,
    scan_id: int,
    root: Path,
    max_file_bytes: int | None,
    allow_content_equivalent: bool,
    historical_consumed_lookup: Callable[[EmailAttachment], Mapping[str, object] | None],
    consumption_provenance: list[Mapping[str, object]],
    checkpoint: Callable[[], None],
) -> Callable[[EmailAttachment], str | os.PathLike[str] | EmailAttachmentResolution | None]:
    def resolve(item: EmailAttachment):
        checkpoint()
        if item.child_device is not None and item.child_inode is not None:
            snapshot = dedup_index.unique_snapshot_for_identity(
                scan_id,
                int(item.child_device),
                int(item.child_inode),
            )
            if snapshot is not None and _inside_corpus(Path(snapshot.path), root):
                return snapshot.path
        receipt = historical_consumed_lookup(item)
        if receipt is not None:
            identity = receipt.get("source_identity")
            source_path = identity.get("path") if isinstance(identity, Mapping) else None
            try:
                if not isinstance(source_path, str) or not Path(os.path.abspath(source_path)).is_relative_to(root.resolve()):
                    return None
            except OSError:
                return None
            consumption_provenance.append(
                {
                    "run_id": receipt.get("run_id"),
                    "stage_event_id": receipt.get("stage_event_id"),
                    "stage_name": receipt.get("stage_name"),
                    "source_sha256": item.sha256,
                }
            )
            return EmailAttachmentResolution(
                None,
                reuse_kind="consumed_archive",
                provenance=receipt,
            )
        if not allow_content_equivalent:
            return None
        matches: list[FileSnapshot] = []
        for snapshot in dedup_index.snapshots_by_size(
            scan_id,
            int(item.size),
            max_file_bytes=max_file_bytes,
        ):
            checkpoint()
            if not _inside_corpus(Path(snapshot.path), root):
                continue
            observation = dedup_index.observe_fingerprint(snapshot, FULL_ALGORITHM)
            if observation is None or observation.digest.hex() != item.sha256:
                continue
            if all(snapshot.identity != candidate.identity for candidate in matches):
                matches.append(snapshot)
            if len(matches) > 1:
                return None
        if len(matches) != 1:
            return None
        return EmailAttachmentResolution(matches[0].path, reuse_kind="content_equivalent")

    return resolve


def run_email_intake_stage(
    *,
    root: Path,
    state_directory: Path,
    config: object,
    state: object,
    run_id: int,
    boundary: "NormalInventoryBoundary",
    dedup_index: DedupIndex,
    scan_id: int,
    identified_types: Mapping[object, object],
    apply: bool,
    cancellation: "CancellationToken",
    progress: ProgressCallback | None,
) -> EmailIntakeStageResult:
    """Materialize only identified EML attachments under one bounded stage."""

    if not bool(getattr(config, "email_intake_enabled", True)):
        return EmailIntakeStageResult("skipped_disabled")
    if str(getattr(config, "route", "none")).casefold() != "all" or bool(
        getattr(config, "route_only", False)
    ):
        return EmailIntakeStageResult("skipped_scope")
    boundary.verify()
    max_file_bytes = validate_max_file_bytes(getattr(config, "max_file_bytes", None))
    limits = _email_limits(config)
    cancellation_checkpoint = getattr(cancellation, "checkpoint", None)
    if not callable(cancellation_checkpoint):
        raise TypeError("email intake requires a cancellation checkpoint")
    run_budget_check = getattr(state, "check_run_budget", None)

    latest_budget: Mapping[str, object] | None = None

    def checkpoint() -> None:
        nonlocal latest_budget
        cancellation_checkpoint()
        if callable(run_budget_check):
            observed_budget = run_budget_check(run_id)
            if isinstance(observed_budget, Mapping):
                latest_budget = observed_budget

    def sql_checkpoint() -> None:
        # SQLite progress callbacks must not recursively query the same owner.
        # Reservations cannot change on this owner thread during a statement;
        # enforce its captured deadline plus the live cancellation token here,
        # then refresh durable cancellation/budget state at the row boundary.
        cancellation_checkpoint()
        if latest_budget is None:
            return
        deadline = latest_budget.get("deadline_ns")
        now = time.time_ns()
        if type(deadline) is int and now >= deadline:
            expired = dict(latest_budget)
            expired["expired"] = True
            expired["elapsed_until_ns"] = now
            started = expired.get("started_ns")
            if type(started) is int:
                expired["elapsed_seconds"] = max(0.0, (now - started) / 1_000_000_000)
            raise RunBudgetExceeded("time", expired)
    guard_factory = getattr(state, "corpus_mutation_guard", None)
    mutation_guard = guard_factory(run_id) if callable(guard_factory) else None
    if apply and mutation_guard is None:
        raise EmailAttachmentError("unsafe", "mutation_guard_required")
    record_event = getattr(state, "record_event", None)
    if not callable(record_event):
        raise TypeError("email intake requires FrameworkState.record_event")
    manifest_root = state_directory / "email-intake"
    historical_reader = getattr(state, "read_historical_zip_consumption", None)

    def historical_consumed_lookup(item: EmailAttachment) -> Mapping[str, object] | None:
        if not callable(historical_reader):
            return None
        values = historical_reader(
            root,
            source_sha256=item.sha256,
            current_run_id=run_id,
            max_candidates=64,
            checkpoint=checkpoint,
            sql_checkpoint=sql_checkpoint,
        )
        if not isinstance(values, (list, tuple)):
            return None
        # Equal ZIP bytes may have several legitimate source identities. Do
        # not let a more recent, unrelated occurrence hide this child's fact.
        for value in values:
            checkpoint()
            if not isinstance(value, Mapping):
                continue
            identity = value.get("source_identity")
            if not isinstance(identity, Mapping):
                continue
            if (
                item.child_device is not None and item.child_inode is not None
                and identity.get("device") == item.child_device
                and identity.get("inode") == item.child_inode
                and identity.get("size") == item.size
                and identity.get("mtime_ns") == item.child_mtime_ns
            ):
                return value
        return None
    errors: list[Mapping[str, object]] = []
    parents_examined = parents_with_attachments = planned_count = 0
    materialized_count = replayed_count = failed = 0
    consumed_count = 0
    consumption_provenance: list[Mapping[str, object]] = []
    filesystem_changed = False
    reconciliation_required = False
    recovery_required = False
    created_paths: list[str] = []
    emit_progress(
        progress,
        ProgressEvent(
            "framework",
            EMAIL_INTAKE_STAGE,
            "Materializando adjuntos de correo",
            0,
            None,
            "mensajes",
            metrics=(ProgressMetric("attachments", 0),),
        ),
    )
    for index, snapshot in enumerate(
        (value for value in dedup_index.snapshots(scan_id)
         if getattr(identified_types.get(_detection_key(value)), "mime", None) == "message/rfc822"
         and size_is_admitted(value.size, max_file_bytes)
         and _inside_corpus(Path(value.path), root)),
        start=1,
    ):
        checkpoint()
        parents_examined += 1
        source = Path(snapshot.path)
        try:
            prepared = prepare_email_attachments(
                source,
                limits=limits,
                checkpoint=checkpoint,
            )
            descriptors = prepared.attachments
            if not descriptors:
                emit_progress(
                    progress,
                    ProgressEvent(
                        "framework",
                        EMAIL_INTAKE_STAGE,
                        "Materializando adjuntos de correo",
                        index,
                        None,
                        "mensajes",
                        metrics=(ProgressMetric("attachments", planned_count),),
                    ),
                )
                continue
            parents_with_attachments += 1
            planned_count += len(descriptors)
            key = _parent_key(snapshot)
            attachments_root = root / "Adjuntos_de_correos"
            destination = attachments_root / key
            manifest = manifest_root / f"{key}.json"
            # A replay may have a durable external manifest while the
            # attachment directory was intentionally consumed/cleaned.  Do
            # not recreate the corpus parent merely to discover that replay
            # must abstain or report historical consumption.
            if apply and not (destination.exists() or manifest.exists()):
                attachments_root = _ensure_attachment_parent(root, mutation_guard)
                destination = attachments_root / key
            if mutation_guard is not None:
                require_paths_allowed = getattr(mutation_guard, "require_paths_allowed", None)
                if not callable(require_paths_allowed):
                    raise EmailAttachmentError("unsafe", "mutation_guard_invalid")
                require_paths_allowed(source, destination)
            parent_consumption_provenance: list[Mapping[str, object]] = []
            resolver = _resolver_for_inventory(
                dedup_index=dedup_index,
                scan_id=scan_id,
                root=root,
                max_file_bytes=max_file_bytes,
                allow_content_equivalent=bool(
                    getattr(config, "email_allow_content_equivalent_reuse", False)
                ),
                historical_consumed_lookup=historical_consumed_lookup,
                consumption_provenance=parent_consumption_provenance,
                checkpoint=checkpoint,
            )
            replay_resolver = resolver if (not destination.exists() or callable(historical_reader)) else None
            result = materialize_email_attachments(
                source,
                destination,
                apply=apply,
                manifest_path=manifest,
                limits=limits,
                resolver=replay_resolver,
                allow_content_equivalent=bool(
                    getattr(config, "email_allow_content_equivalent_reuse", False)
                ),
                prepared=prepared,
                checkpoint=checkpoint,
            )
            if apply:
                if result.status == "applied":
                    materialized_count += sum(
                        item.status == "materialized" for item in result.attachments
                    )
                    replayed_count += sum(
                        item.status == "replayed" for item in result.attachments
                    )
                    filesystem_changed = True
                    reconciliation_required = True
                    created_paths.extend(
                        item.child_path
                        for item in result.attachments
                        if item.status == "materialized" and item.child_path is not None
                    )
                elif result.status == "replayed":
                    replayed_count += sum(
                        item.child_reuse_kind != "consumed_archive"
                        for item in result.attachments
                    )
                consumed = sum(
                    item.child_reuse_kind == "consumed_archive"
                    for item in result.attachments
                )
                if consumed:
                    consumed_count += consumed
                    consumption_provenance.append(
                        {
                            "parent": str(source),
                            "attachments_consumed": consumed,
                            "coverage": "historical_source_consumed_current_children_unverified",
                            "receipts": parent_consumption_provenance[:8],
                        }
                    )
            record_event(
                run_id,
                "info",
                EMAIL_INTAKE_STAGE,
                "Adjuntos de correo evaluados",
                {
                    "parent": str(source),
                    "status": result.status,
                    "attachments": len(result.attachments),
                    "destination": str(destination),
                },
            )
        except EmailAttachmentError as exc:
            failed += 1
            if exc.reason in {
                "child_resolver_missing",
                "child_resolver_failed",
                "manifest_extra_or_missing_child",
                "manifest_child_missing",
                "consumed_archive_receipt_missing",
                "consumed_archive_receipt_mismatch",
                "consumed_archive_identity_missing",
                "consumed_archive_identity_mismatch",
            }:
                recovery_required = True
                reconciliation_required = True
            if exc.reason in {
                "stage_publish_failed",
                "manifest_replace_failed",
                "parent_identity_changed_after_stage",
                "destination_parent_changed",
            }:
                reconciliation_required = True
            errors.append(
                {
                    "parent": str(source),
                    "status": exc.status,
                    "reason": exc.reason,
                    "detail": exc.detail,
                }
            )
        except (OSError, ValueError, UnicodeError) as exc:
            failed += 1
            errors.append(
                {
                    "parent": str(source),
                    "status": "failed",
                    "reason": "email_intake_parent_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        emit_progress(
            progress,
            ProgressEvent(
                "framework",
                EMAIL_INTAKE_STAGE,
                "Materializando adjuntos de correo",
                index,
                        None,
                "mensajes",
                metrics=(ProgressMetric("attachments", planned_count),),
            ),
        )
    if filesystem_changed:
        boundary.verify()
    return EmailIntakeStageResult(
        "recovery_required" if recovery_required else "completed" if failed == 0 else "partial",
        parents_examined,
        parents_with_attachments,
        planned_count,
        materialized_count,
        replayed_count,
        consumed_count,
        failed,
        filesystem_changed,
        reconciliation_required,
        tuple(errors),
        tuple(dict.fromkeys(created_paths))[:256],
        tuple(consumption_provenance)[:32],
    )


__all__ = ("EMAIL_INTAKE_SCHEMA", "EmailIntakeStageResult", "run_email_intake_stage")
