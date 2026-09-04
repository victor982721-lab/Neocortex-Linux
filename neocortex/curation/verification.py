"""Bounded verification of published curation candidates.

The verifier is deliberately read-only with respect to the corpus and its
owners.  It consumes one already-published :class:`CurationPlanPage`, checks
the recorded physical identities, hashes the current regular files and then
performs the byte comparison required for an exact duplicate claim.  It never
creates ReviewTasks, AuthorizationGrants or ``file_actions``.
"""

from __future__ import annotations

import errno
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Literal, cast

from neocortex.deduplication.domain.errors import FileChangedError
from neocortex.deduplication.domain.models import (
    VALID_VERIFICATION_MODES,
    VerificationMode,
)
from neocortex.deduplication.fingerprinting import (
    snapshot_path,
    stat_matches_snapshot,
)

from .preview import CurationItem, CurationPlanPage, CurationSourceHead


CURATION_VERIFICATION_SCHEMA_VERSION = 1
MAX_VERIFICATION_ITEMS = 100
MAX_VERIFICATION_FILES = 512
MAX_VERIFICATION_BYTES = 128 * 1024 * 1024
_READ_CHUNK_SIZE = 1024 * 1024

VerificationStatus = Literal["verified", "source_changed", "not_verified", "not_applicable"]
WorkStopReason = Literal["budget_exhausted", "cancelled", "deadline_exceeded"]


class CurationVerificationError(RuntimeError):
    """The published curation evidence cannot be verified safely."""


class CurationVerificationSnapshotChanged(CurationVerificationError):
    """The plan or one of its physical sources changed during verification."""


class CurationVerificationUnavailable(CurationVerificationError):
    """The requested verification could not run with the available evidence."""

    def __init__(self, message: str, *, reason_code: str = "verification_unavailable") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class CurationWorkBudget:
    """Optional work limits for one read-only verification invocation.

    The monotonic deadline is an absolute value in the clock's domain. The
    injectable clock exists only to make the contract deterministic in tests;
    ordinary callers use :func:`time.monotonic`.
    """

    max_items: int = MAX_VERIFICATION_ITEMS
    max_files: int = MAX_VERIFICATION_FILES
    max_bytes: int = MAX_VERIFICATION_BYTES
    deadline_monotonic: float | None = None
    # ``None`` is the conventional return value of ``CancellationToken``
    # checkpoints; a truthy boolean requests a stop, while any other value is
    # rejected fail-closed.
    cancellation_check: Callable[[], bool | None] | None = None
    monotonic_clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("max_items", self.max_items, MAX_VERIFICATION_ITEMS),
            ("max_files", self.max_files, MAX_VERIFICATION_FILES),
            ("max_bytes", self.max_bytes, MAX_VERIFICATION_BYTES),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} is outside the verification bound")
        deadline = self.deadline_monotonic
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or deadline < 0
        ):
            raise ValueError("deadline_monotonic must be a finite non-negative number")
        if self.cancellation_check is not None and not callable(self.cancellation_check):
            raise TypeError("cancellation_check must be callable")
        if not callable(self.monotonic_clock):
            raise TypeError("monotonic_clock must be callable")


class _WorkBudgetStop(CurationVerificationUnavailable):
    """Internal bounded stop signal that preserves completed observations."""

    def __init__(self, reason: WorkStopReason) -> None:
        super().__init__(reason.replace("_", " "), reason_code=reason)


class _WorkBudgetState:
    """Mutable accounting for one immutable :class:`CurationWorkBudget`."""

    def __init__(
        self,
        budget: CurationWorkBudget,
        *,
        max_items: int,
        max_files: int,
        max_bytes: int,
    ) -> None:
        self.budget = budget
        self.max_items = min(max_items, budget.max_items)
        self.max_files = min(max_files, budget.max_files)
        self.max_bytes = min(max_bytes, budget.max_bytes)
        self.items_started = 0
        self.files_checked = 0
        self.bytes_checked = 0

    def _control_reason(self) -> WorkStopReason | None:
        callback = self.budget.cancellation_check
        if callback is not None:
            try:
                decision = callback()
                if decision is True:
                    return "cancelled"
                if decision is not False and decision is not None:
                    return "cancelled"
            except Exception:
                return "cancelled"
        deadline = self.budget.deadline_monotonic
        if deadline is not None:
            try:
                observed = self.budget.monotonic_clock()
                if (
                    isinstance(observed, bool)
                    or not isinstance(observed, (int, float))
                    or not math.isfinite(float(observed))
                    or observed >= deadline
                ):
                    return "deadline_exceeded"
            except Exception:
                return "deadline_exceeded"
        return None

    def start_item(self) -> None:
        reason = self._control_reason()
        if reason is not None:
            raise _WorkBudgetStop(reason)
        if self.items_started >= self.max_items:
            raise _WorkBudgetStop("budget_exhausted")
        self.items_started += 1

    def reserve_file(self, size: int) -> None:
        reason = self._control_reason()
        if reason is not None:
            raise _WorkBudgetStop(reason)
        if (
            self.files_checked >= self.max_files
            or size < 0
            or size > self.max_bytes - self.bytes_checked
        ):
            raise _WorkBudgetStop("budget_exhausted")
        self.files_checked += 1

    def before_read(self) -> None:
        reason = self._control_reason()
        if reason is not None:
            raise _WorkBudgetStop(reason)

    def record_bytes(self, count: int) -> None:
        if count < 0 or count > self.max_bytes - self.bytes_checked:
            raise _WorkBudgetStop("budget_exhausted")
        self.bytes_checked += count


@dataclass(slots=True)
class _VerificationMetrics:
    """Bounded payload-buffer metrics for one verification page."""

    chunks: int = 0
    peak_buffer: int = 0
    keeper_replays: int = 0

    def observe_chunk(self, source_size: int, reference_size: int = 0) -> None:
        self.chunks += 1
        self.peak_buffer = max(self.peak_buffer, source_size + reference_size)


@dataclass(frozen=True, slots=True)
class CurationVerificationItem:
    """Verification result for one curation item."""

    item_id: str
    kind: str
    source_path: str
    persisted_mode: VerificationMode | None
    observed_mode: Literal["full_hash"] | None
    status: VerificationStatus
    reason: str
    checked_files: int
    verified_files: int

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_files": self.checked_files,
            "item_id": self.item_id,
            "kind": self.kind,
            "observed_mode": self.observed_mode,
            "persisted_mode": self.persisted_mode,
            "reason": self.reason,
            "source_path": self.source_path,
            "status": self.status,
            "verified_files": self.verified_files,
        }


@dataclass(frozen=True, slots=True)
class CurationVerificationResult:
    """Bounded result for a page or selected items."""

    plan_digest: str
    snapshot_id: str
    coverage: Literal["complete", "partial"]
    status: Literal["complete", "partial", "snapshot_changed"]
    items_total: int
    items_verified: int
    items_failed: int
    items_skipped: int
    files_checked: int
    bytes_checked: int
    items: tuple[CurationVerificationItem, ...]
    source_heads: tuple[CurationSourceHead, ...] = ()
    metrics: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        if self.metrics is not None:
            if any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in self.metrics.items()
            ):
                raise ValueError("verification metrics must contain non-negative integers")
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    def to_dict(self) -> dict[str, object]:
        return {
            "bytes_checked": self.bytes_checked,
            "coverage": self.coverage,
            "files_checked": self.files_checked,
            "items": [item.to_dict() for item in self.items],
            "items_failed": self.items_failed,
            "items_skipped": self.items_skipped,
            "items_total": self.items_total,
            "items_verified": self.items_verified,
            "plan_digest": self.plan_digest,
            "snapshot_id": self.snapshot_id,
            "source_heads": [head.to_dict() for head in self.source_heads],
            "status": self.status,
            "metrics": (None if self.metrics is None else dict(self.metrics)),
        }


def _bounded_root(value: object) -> Path:
    if not isinstance(value, str) or not value or not value.startswith("/"):
        raise CurationVerificationUnavailable(
            "curation plan root is not absolute",
            reason_code="root_invalid",
        )
    return Path(os.path.abspath(value))


def _assert_safe_path_components(root: Path, path: Path) -> None:
    """Reject a root or ancestor symlink before opening corpus content.

    A lexical ``relative_to`` check is insufficient when a directory below the
    root is replaced by a symlink.  Walk every directory component with
    ``lstat``; the final component is checked separately by the caller and by
    the descriptor-based reader below.
    """

    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise CurationVerificationSnapshotChanged(
            f"curation root disappeared: {root}"
        ) from exc
    except OSError as exc:
        raise CurationVerificationUnavailable(
            f"curation root cannot be inspected: {root}",
            reason_code="source_inspection_unavailable",
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise CurationVerificationSnapshotChanged(
            f"curation root is not a regular directory: {root}"
        )
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CurationVerificationSnapshotChanged(
            "curation source path escapes the published root"
        ) from exc
    current = root
    for component in relative.parts[:-1]:
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError as exc:
            raise CurationVerificationSnapshotChanged(
                f"curation source ancestor disappeared: {current}"
            ) from exc
        except OSError as exc:
            raise CurationVerificationUnavailable(
                f"curation source ancestor cannot be inspected: {current}",
                reason_code="source_inspection_unavailable",
            ) from exc
        if stat.S_ISLNK(component_stat.st_mode):
            raise CurationVerificationSnapshotChanged(
                f"curation source ancestor is a symlink: {current}"
            )
        if not stat.S_ISDIR(component_stat.st_mode):
            raise CurationVerificationSnapshotChanged(
                f"curation source ancestor is not a directory: {current}"
            )


def _bounded_path(value: object, *, root: Path) -> Path:
    if not isinstance(value, str) or not value or not value.startswith("/"):
        raise CurationVerificationSnapshotChanged("curation source path is not absolute")
    path = Path(os.path.abspath(value))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CurationVerificationSnapshotChanged(
            "curation source path escapes the published root"
        ) from exc
    return path


def _identity_numbers(value: object) -> tuple[int, int, int]:
    if not isinstance(value, dict):
        raise CurationVerificationSnapshotChanged("curation member identity is invalid")
    try:
        volume = int(str(value["volume_id"]), 16)
        file_id = int(str(value["file_id"]), 16)
        birthtime = int(value["birthtime_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CurationVerificationSnapshotChanged(
            "curation member identity is invalid"
        ) from exc
    return volume, file_id, birthtime


def _snapshot_for_member(
    member: dict[str, object],
    *,
    root: Path,
) -> tuple[Path, Any]:
    path = _bounded_path(member.get("path"), root=root)
    _assert_safe_path_components(root, path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise CurationVerificationSnapshotChanged(
            f"curation source disappeared: {path}"
        ) from exc
    except OSError as exc:
        raise CurationVerificationUnavailable(
            f"curation source cannot be inspected: {path}",
            reason_code="source_inspection_unavailable",
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise CurationVerificationSnapshotChanged(
            f"curation source is not a regular file: {path}"
        )
    try:
        current = snapshot_path(path)
    except (FileChangedError, OSError, ValueError) as exc:
        raise CurationVerificationSnapshotChanged(
            f"curation source cannot be snapshotted: {path}"
        ) from exc
    expected_volume, expected_file, expected_birth = _identity_numbers(member.get("identity"))
    expected_size = member.get("size")
    expected_mtime = member.get("mtime_ns")
    if (
        current.volume_id != expected_volume
        or current.file_id != expected_file
        or current.birthtime_ns != expected_birth
        or current.size != expected_size
        or current.mtime_ns != expected_mtime
    ):
        raise CurationVerificationSnapshotChanged(
            f"curation source identity changed: {path}"
        )
    return path, current


def _open_regular_file_beneath(root: Path, path: Path) -> int:
    """Open ``path`` through no-following directory descriptors.

    This closes the ancestor-symlink race between the lexical/lstat checks and
    the actual read.  The returned descriptor is owned by the caller.
    """

    relative = path.relative_to(root)
    if not relative.parts:
        raise CurationVerificationSnapshotChanged("curation source path is the root")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # ``O_NONBLOCK`` prevents a final-component swap to a FIFO from hanging a
    # supposedly bounded verification before the cooperative deadline can be
    # observed.  The descriptor is still required to be a regular, single-link
    # file immediately after opening.
    common_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_NONBLOCK", 0)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(
            root,
            common_flags | os.O_DIRECTORY,
        )
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                common_flags | os.O_DIRECTORY,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.parts[-1],
            common_flags,
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(file_fd)
            raise OSError(errno.ELOOP, "curation source is not a regular single-link file")
        os.close(directory_fd)
        directory_fd = None
        return file_fd
    except OSError as exc:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise CurationVerificationSnapshotChanged(
                f"curation source path contains a symlink or non-directory: {path}"
            ) from exc
        if exc.errno in {errno.ENOENT, errno.ESTALE}:
            raise CurationVerificationSnapshotChanged(
                f"curation source disappeared: {path}"
            ) from exc
        raise CurationVerificationUnavailable(
            f"curation source cannot be opened: {path}",
            reason_code="io_unavailable",
        ) from exc


def _read_stable_payload(
    path: Path,
    snapshot: Any,
    *,
    root: Path,
    work: _WorkBudgetState,
    destination: BinaryIO,
    metrics: _VerificationMetrics,
) -> str:
    """Stream one file into a temporary reference and return its digest.

    The destination is temporary storage, never a corpus path.  Keeping the
    reference outside process memory lets every later member compare against
    the keeper without retaining a second copy of the file.
    """

    try:
        import xxhash
    except ImportError as exc:  # pragma: no cover - dependency is in releases
        raise CurationVerificationUnavailable(
            "exact curation verification requires xxhash",
            reason_code="dependency_unavailable",
        ) from exc
    descriptor = _open_regular_file_beneath(root, path)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not stat_matches_snapshot(snapshot, before)
        ):
            raise CurationVerificationSnapshotChanged(f"curation source identity changed: {path}")
        hasher = xxhash.xxh3_128()
        remaining = snapshot.size
        while remaining:
            work.before_read()
            try:
                chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, remaining))
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    f"curation source cannot be read: {path}",
                    reason_code="io_unavailable",
                ) from exc
            if not chunk:
                raise CurationVerificationSnapshotChanged(f"curation source ended early: {path}")
            hasher.update(chunk)
            work.record_bytes(len(chunk))
            try:
                written = destination.write(chunk)
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    "temporary curation verification storage cannot be written",
                    reason_code="temporary_storage_unavailable",
                ) from exc
            if written != len(chunk):
                raise CurationVerificationUnavailable(
                    "temporary curation verification storage wrote a short chunk",
                    reason_code="temporary_storage_unavailable",
                )
            metrics.observe_chunk(len(chunk))
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not stat_matches_snapshot(snapshot, after)
        ):
            raise CurationVerificationSnapshotChanged(
                f"curation source identity changed: {path}"
            )
        try:
            destination.flush()
        except OSError as exc:
            raise CurationVerificationUnavailable(
                "temporary curation verification storage cannot be flushed",
                reason_code="temporary_storage_unavailable",
            ) from exc
        work.before_read()
        return hasher.digest().hex()
    finally:
        os.close(descriptor)


def _compare_stable_file(
    path: Path,
    snapshot: Any,
    *,
    root: Path,
    reference: BinaryIO,
    work: _WorkBudgetState,
    metrics: _VerificationMetrics,
) -> tuple[bool, str]:
    """Hash and compare one source file against a temporary keeper stream."""

    try:
        import xxhash
    except ImportError as exc:  # pragma: no cover - dependency is in releases
        raise CurationVerificationUnavailable(
            "exact curation verification requires xxhash",
            reason_code="dependency_unavailable",
        ) from exc
    descriptor = _open_regular_file_beneath(root, path)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not stat_matches_snapshot(snapshot, before)
        ):
            raise CurationVerificationSnapshotChanged(f"curation source identity changed: {path}")
        hasher = xxhash.xxh3_128()
        equal = True
        remaining = snapshot.size
        reference.seek(0)
        while remaining:
            work.before_read()
            try:
                chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, remaining))
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    f"curation source cannot be read: {path}",
                    reason_code="io_unavailable",
                ) from exc
            if not chunk:
                raise CurationVerificationSnapshotChanged(f"curation source ended early: {path}")
            work.record_bytes(len(chunk))
            try:
                reference_chunk = reference.read(len(chunk))
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    "temporary curation verification storage cannot be read",
                    reason_code="temporary_storage_unavailable",
                ) from exc
            hasher.update(chunk)
            if chunk != reference_chunk:
                equal = False
            metrics.observe_chunk(len(chunk), len(reference_chunk))
            remaining -= len(chunk)
        if reference.read(1):
            equal = False
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not stat_matches_snapshot(snapshot, after)
        ):
            raise CurationVerificationSnapshotChanged(
                f"curation source identity changed: {path}"
            )
        work.before_read()
        return equal, hasher.digest().hex()
    finally:
        os.close(descriptor)


def _duplicate_verification(
    item: CurationItem,
    *,
    root: Path,
    work: _WorkBudgetState,
    metrics: _VerificationMetrics,
) -> CurationVerificationItem:
    evidence = item.evidence
    raw_mode = evidence.get("verification_mode")
    mode: VerificationMode | None = (
        cast(VerificationMode, raw_mode)
        if isinstance(raw_mode, str) and raw_mode in VALID_VERIFICATION_MODES
        else None
    )
    members = evidence.get("members")
    if not isinstance(members, list) or bool(evidence.get("members_truncated")):
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "not_verified",
            "evidence_truncated",
            0,
            0,
        )
    if not members or len(members) > MAX_VERIFICATION_FILES:
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "not_verified",
            "member_count_out_of_bounds",
            0,
            0,
        )
    try:
        typed_members = [cast(dict[str, object], member) for member in members]
        keep = next(member for member in typed_members if member.get("role") == "keep")
    except (StopIteration, TypeError):
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "not_verified",
            "keep_member_missing",
            0,
            0,
        )
    checked = 0
    verified = 0
    try:
        keep_path, keep_snapshot = _snapshot_for_member(keep, root=root)
        work.reserve_file(keep_snapshot.size)
        checked += 1
        with tempfile.TemporaryFile(mode="w+b") as keeper_stream:
            keep_digest = _read_stable_payload(
                keep_path,
                keep_snapshot,
                root=root,
                work=work,
                destination=keeper_stream,
                metrics=metrics,
            )
            if keep_digest != str(evidence.get("full_fingerprint")):
                raise CurationVerificationSnapshotChanged(
                    f"curation source content changed: {keep_path}"
                )
            verified += 1
            for member in typed_members:
                if member is keep:
                    continue
                path, snapshot = _snapshot_for_member(member, root=root)
                work.reserve_file(snapshot.size)
                checked += 1
                metrics.keeper_replays += 1
                equal, digest = _compare_stable_file(
                    path,
                    snapshot,
                    root=root,
                    reference=keeper_stream,
                    work=work,
                    metrics=metrics,
                )
                if digest != keep_digest or not equal:
                    raise CurationVerificationSnapshotChanged(
                        f"curation duplicate content changed: {path}"
                    )
                verified += 1
    except CurationVerificationError as exc:
        status: VerificationStatus = (
            "source_changed" if isinstance(exc, CurationVerificationSnapshotChanged) else "not_verified"
        )
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            status,
            (
                "source_changed"
                if status == "source_changed"
                else getattr(exc, "reason_code", "verification_unavailable")
            ),
            checked,
            verified,
        )
    except (FileChangedError, OSError, ValueError):
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "source_changed",
            "source_changed",
            checked,
            verified,
        )
    return CurationVerificationItem(
        item.item_id,
        item.kind,
        item.source_path,
        mode,
        "full_hash",
        "verified",
        "exact_content_verified",
        checked,
        verified,
    )


def _unprocessed_item(item: CurationItem, reason: WorkStopReason) -> CurationVerificationItem:
    raw_mode = item.evidence.get("verification_mode")
    mode: VerificationMode | None = (
        cast(VerificationMode, raw_mode)
        if isinstance(raw_mode, str) and raw_mode in VALID_VERIFICATION_MODES
        else None
    )
    return CurationVerificationItem(
        item.item_id,
        item.kind,
        item.source_path,
        mode,
        None,
        "not_verified",
        reason,
        0,
        0,
    )


def verify_curation_page(
    page: CurationPlanPage,
    *,
    item_ids: tuple[str, ...] | None = None,
    max_items: int = MAX_VERIFICATION_ITEMS,
    max_files: int = MAX_VERIFICATION_FILES,
    max_bytes: int = MAX_VERIFICATION_BYTES,
    budget: CurationWorkBudget | None = None,
) -> CurationVerificationResult:
    """Verify duplicate candidates from one already-published plan page."""

    if not isinstance(page, CurationPlanPage):
        raise TypeError("page must be a CurationPlanPage")
    if not 1 <= max_items <= MAX_VERIFICATION_ITEMS:
        raise ValueError("max_items is outside the verification bound")
    if not 1 <= max_files <= MAX_VERIFICATION_FILES:
        raise ValueError("max_files is outside the verification bound")
    if not 1 <= max_bytes <= MAX_VERIFICATION_BYTES:
        raise ValueError("max_bytes is outside the verification bound")
    if budget is not None and not isinstance(budget, CurationWorkBudget):
        raise TypeError("budget must be a CurationWorkBudget")
    root = _bounded_root(page.root)
    selected = page.items
    if item_ids is not None:
        if not item_ids:
            raise ValueError("item_ids cannot be empty")
        if len(item_ids) > max_items or len(set(item_ids)) != len(item_ids):
            raise ValueError("item_ids are outside the verification bound")
        by_id = {item.item_id: item for item in page.items}
        missing = [item_id for item_id in item_ids if item_id not in by_id]
        if missing:
            raise CurationVerificationSnapshotChanged("curation item is not in the published page")
        selected = tuple(by_id[item_id] for item_id in item_ids)
    elif len(selected) > max_items:
        raise CurationVerificationUnavailable("curation verification page exceeds its item bound")
    work = _WorkBudgetState(
        CurationWorkBudget() if budget is None else budget,
        max_items=max_items,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    metrics = _VerificationMetrics()
    results: list[CurationVerificationItem] = []
    for index, item in enumerate(selected):
        try:
            work.start_item()
        except _WorkBudgetStop as exc:
            stop_reason = cast(WorkStopReason, exc.reason_code)
            results.extend(_unprocessed_item(pending, stop_reason) for pending in selected[index:])
            break
        if item.kind != "duplicate_group":
            results.append(
                CurationVerificationItem(
                    item.item_id,
                    item.kind,
                    item.source_path,
                    None,
                    None,
                    "not_applicable",
                    "not_duplicate_kind",
                    0,
                    0,
                )
            )
            continue
        result = _duplicate_verification(
            item,
            root=root,
            work=work,
            metrics=metrics,
        )
        results.append(result)
        if result.reason in {"budget_exhausted", "cancelled", "deadline_exceeded"}:
            stop_reason = cast(WorkStopReason, result.reason)
            results.extend(
                _unprocessed_item(pending, stop_reason) for pending in selected[index + 1 :]
            )
            break
    verified_count = sum(item.status == "verified" for item in results)
    failed_count = sum(item.status == "source_changed" for item in results)
    # Non-duplicate entries (empty files and organization proposals) are
    # intentionally outside bytewise duplicate verification; they must not
    # downgrade a page whose applicable duplicate groups were all verified.
    skipped_count = sum(item.status == "not_verified" for item in results)
    files_checked = work.files_checked
    bytes_checked = work.bytes_checked
    status: Literal["complete", "partial", "snapshot_changed"] = (
        "snapshot_changed"
        if failed_count
        else "partial"
        if skipped_count or page.coverage != "complete" or page.next_cursor is not None
        else "complete"
    )
    coverage: Literal["complete", "partial"] = "complete" if status == "complete" else "partial"
    return CurationVerificationResult(
        page.plan_digest,
        page.snapshot_id,
        coverage,
        status,
        page.items_total,
        verified_count,
        failed_count,
        skipped_count,
        files_checked,
        bytes_checked,
        tuple(results),
        page.source_heads,
        {
            "files": files_checked,
            "bytes": bytes_checked,
            "chunks": metrics.chunks,
            "peak_buffer": metrics.peak_buffer,
            "keeper_replays": metrics.keeper_replays,
        },
    )


__all__ = (
    "CURATION_VERIFICATION_SCHEMA_VERSION",
    "MAX_VERIFICATION_BYTES",
    "MAX_VERIFICATION_FILES",
    "MAX_VERIFICATION_ITEMS",
    "CurationVerificationError",
    "CurationVerificationItem",
    "CurationVerificationResult",
    "CurationVerificationSnapshotChanged",
    "CurationVerificationUnavailable",
    "CurationWorkBudget",
    "verify_curation_page",
)
