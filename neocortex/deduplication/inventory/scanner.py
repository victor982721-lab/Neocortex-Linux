"""SQLite-backed execution and publication of filesystem inventory scans."""

from __future__ import annotations

import math
import os
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress
from neocortex.platform.policy import stat_birthtime_ns

from ..domain.errors import InventoryError
from ..domain.models import ScanSummary
from .policy import InventoryExclusionPolicy, resolve_inventory_exclusion_policy
from .traversal import (
    FileObservation,
    InventoryTraversal,
    InventoryUnsupportedPathEncoding,
    RootIdentity,
    ScanCounters,
)
from .resume import (
    InventoryDirectoryDigest,
    InventoryPrefixDigest,
    InventoryResumeCheckpoint,
    InventoryResumeCheckpointStore,
    InventoryResumeError,
    InventoryResumeConflictError,
    batch_digest,
    dfs_order_key,
    empty_batch_digest,
    empty_directory_digest,
    empty_prefix_digest,
    relative_cursor,
)


DEFAULT_BATCH_SIZE = 5000
MAX_BATCH_SIZE = 10_000
MAX_SCAN_FILES = 10_000_000
MAX_SCAN_BYTES = 1 << 40
FILE_UPSERT_SQL = """
    INSERT INTO files(path, volume_id, file_id, size, mtime_ns, birthtime_ns, scan_id)
    VALUES(?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(scan_id, path) DO UPDATE SET
        volume_id=excluded.volume_id,
        file_id=excluded.file_id,
        size=excluded.size,
        mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns
"""

type InventoryRow = tuple[str, bytes, bytes, int, int, int, int]


class InventoryScanCancelled(InventoryError):
    """The caller cancelled a bounded inventory scan before publication."""

    reason_code = "cancelled"


class InventoryScanBudgetExceeded(InventoryError):
    """A bounded inventory scan reached its configured work limit."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.replace("_", " "))


class InventoryScanDeadlineExceeded(InventoryScanBudgetExceeded):
    """The monotonic deadline elapsed before the next bounded operation."""

    def __init__(self) -> None:
        super().__init__("deadline_exceeded")


class InventoryWorkBudget:
    """Finite work admission for one inventory producer.

    Limits are counted across a resumed scan, not only the tail after the
    checkpoint.  The callback is cooperative and may either return ``None``
    or ``False`` to continue, return ``True`` to cancel, or raise its own
    cancellation exception, which is preserved by the scanner.
    """

    def __init__(
        self,
        *,
        max_files: int = MAX_SCAN_FILES,
        max_bytes: int = MAX_SCAN_BYTES,
        deadline_monotonic: float | None = None,
        cancellation_check: Callable[[], bool | None] | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or not 1 <= max_files <= MAX_SCAN_FILES
        ):
            raise ValueError(f"max_files must be between 1 and {MAX_SCAN_FILES}")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_SCAN_BYTES
        ):
            raise ValueError(f"max_bytes must be between 1 and {MAX_SCAN_BYTES}")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
            or deadline_monotonic < 0
        ):
            raise ValueError("deadline_monotonic must be a finite non-negative number")
        if cancellation_check is not None and not callable(cancellation_check):
            raise TypeError("cancellation_check must be callable")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.deadline_monotonic = (
            None if deadline_monotonic is None else float(deadline_monotonic)
        )
        self.cancellation_check = cancellation_check
        self.monotonic_clock = monotonic_clock


class _InventoryWorkState:
    """Mutable admission/accounting state owned by one scanner invocation."""

    def __init__(self, budget: InventoryWorkBudget, *, files: int, bytes_seen: int) -> None:
        if files < 0 or bytes_seen < 0:
            raise ValueError("initial inventory work counters must be non-negative")
        if files > budget.max_files or bytes_seen > budget.max_bytes:
            raise InventoryResumeError("checkpoint counters exceed the requested work budget")
        self.budget = budget
        self.files = files
        self.bytes_seen = bytes_seen

    def check(self, size: int = 0) -> None:
        callback = self.budget.cancellation_check
        if callback is not None:
            decision = callback()
            if decision is True:
                raise InventoryScanCancelled("inventory scan cancelled")
            if decision is not False and decision is not None:
                raise InventoryScanCancelled("inventory scan cancelled")
        deadline = self.budget.deadline_monotonic
        if deadline is not None and self.budget.monotonic_clock() >= deadline:
            raise InventoryScanDeadlineExceeded()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("inventory file size must be a non-negative integer")
        if size == 0:
            return
        self._reserve_file(size)

    def check_file(self, size: int) -> None:
        """Admit one file, including zero-byte files, before it is persisted."""

        self.check()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("inventory file size must be a non-negative integer")
        self._reserve_file(size)

    def _reserve_file(self, size: int) -> None:
        if self.files >= self.budget.max_files:
            raise InventoryScanBudgetExceeded("budget_exhausted")
        if size > self.budget.max_bytes - self.bytes_seen:
            raise InventoryScanBudgetExceeded("budget_exhausted")
        self.files += 1
        self.bytes_seen += size


class _NoopInventorySink:
    """Read-only sink used to replay a terminal checkpoint for drift checks."""

    @property
    def full(self) -> bool:
        return False

    def append(self, observation: FileObservation) -> None:
        del observation

    def flush(self) -> None:
        return None


def _validated_batch_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_BATCH_SIZE
    ):
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    return value


def id_blob(value: int) -> bytes:
    """Encode an unsigned filesystem identity for the SQLite schema."""

    if value < 0 or value.bit_length() > 128:
        raise InventoryError("filesystem identity does not fit an unsigned 128-bit value")
    return value.to_bytes(16, "little")


class InventoryBatch:
    """Commit at most ``batch_size`` file rows in each transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        scan_id: int,
        batch_size: int,
        before_flush: Callable[[], None] | None = None,
        on_flush: Callable[[tuple[FileObservation, ...]], None] | None = None,
    ) -> None:
        self._connection = connection
        self._scan_id = scan_id
        self._batch_size = _validated_batch_size(batch_size)
        self._before_flush = before_flush
        self._on_flush = on_flush
        self._rows: list[InventoryRow] = []
        self._observations: list[FileObservation] = []

    def append(self, observation: FileObservation) -> None:
        self._rows.append(
            (
                observation.path,
                id_blob(observation.volume_id),
                id_blob(observation.file_id),
                observation.size,
                observation.mtime_ns,
                observation.birthtime_ns,
                self._scan_id,
            )
        )
        self._observations.append(observation)

    @property
    def full(self) -> bool:
        return len(self._rows) >= self._batch_size

    def flush(self) -> None:
        if not self._rows:
            return
        if self._before_flush is not None:
            self._before_flush()
        with self._connection:
            self._connection.executemany(FILE_UPSERT_SQL, self._rows)
        # A cancellation/deadline that becomes true while SQLite commits must
        # leave the batch unpublished in the checkpoint.  The committed rows
        # are deliberately retained as a recoverable tail and removed by the
        # next resume before any new rows are admitted.
        if self._before_flush is not None:
            self._before_flush()
        observations = tuple(self._observations)
        self._rows.clear()
        self._observations.clear()
        if self._on_flush is not None:
            self._on_flush(observations)


class InventoryScanner:
    """Publish one complete, root-identity-bound inventory scan."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def scan(
        self,
        root: str | Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        excluded_paths: Iterable[str | Path] | None = None,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
        checkpoint_path: str | Path | None = None,
        resume: bool = False,
        deterministic: bool = False,
        work_budget: InventoryWorkBudget | None = None,
    ) -> ScanSummary:
        """Run a bounded inventory, optionally with a durable DFS checkpoint.

        Checkpointed runs use deterministic byte-sorted directory order and
        keep their owner manifest outside the scanned root.  A resumed run
        revalidates the committed prefix before deleting only rows after the
        durable cursor, so a stale, swapped, or ambiguous corpus fails closed.
        """
        batch_size = _validated_batch_size(batch_size)
        if resume and checkpoint_path is None:
            raise ValueError("resume requires checkpoint_path")
        effective_policy = resolve_inventory_exclusion_policy(
            excluded_paths,
            exclusion_policy,
        )
        root_identity = RootIdentity.capture(root)
        if effective_policy.excludes_directory(root_identity.path):
            raise InventoryError("inventory root is excluded by its inventory policy")
        store = (
            None
            if checkpoint_path is None
            else InventoryResumeCheckpointStore(checkpoint_path, root=root_identity.path)
        )
        if store is not None and not resume and store.exists():
            raise InventoryResumeConflictError(
                "checkpoint already exists; pass resume=True to continue its owner"
            )
        budget = InventoryWorkBudget() if work_budget is None else work_budget
        if not isinstance(budget, InventoryWorkBudget):
            raise TypeError("work_budget must be an InventoryWorkBudget")

        checkpoint: InventoryResumeCheckpoint | None = None
        if resume:
            assert store is not None
            checkpoint = store.read()
            scan_id, already_complete = self._prepare_resume(
                checkpoint,
                root_identity,
                effective_policy.signature,
            )
            if already_complete:
                self._verify_complete_checkpoint(
                    checkpoint,
                    root_identity,
                    effective_policy,
                    budget,
                )
                return self._read_complete_summary(scan_id, root_identity.path)
            initial_counters = ScanCounters(
                checkpoint.files_seen,
                checkpoint.directories_seen,
                checkpoint.bytes_seen,
                checkpoint.skipped_links,
                checkpoint.excluded_directories,
                checkpoint.errors,
            )
        else:
            scan_id = self._begin_scan(root_identity, effective_policy.signature)
            initial_counters = ScanCounters()

        work = _InventoryWorkState(
            budget,
            files=initial_counters.files_seen,
            bytes_seen=initial_counters.bytes_seen,
        )
        prefix_digest = InventoryPrefixDigest(
            root_identity.path,
            value=(empty_prefix_digest() if checkpoint is None else checkpoint.prefix_digest),
        )
        directory_digest = InventoryDirectoryDigest(
            root_identity.path,
            value=(empty_directory_digest() if checkpoint is None else checkpoint.directory_digest),
        )
        prefix_validation_digest = (
            None if checkpoint is None else InventoryPrefixDigest(root_identity.path)
        )
        prefix_validation_directory_digest = (
            None if checkpoint is None else InventoryDirectoryDigest(root_identity.path)
        )
        prefix_batch_observations: deque[FileObservation] = deque(
            maxlen=(
                batch_size
                if checkpoint is None or checkpoint.batch_files == 0
                else checkpoint.batch_files
            )
        )
        last_checkpoint: list[InventoryResumeCheckpoint | None] = [checkpoint]
        cursor_evidence: list[tuple[ScanCounters, str]] = []

        if store is not None:
            if checkpoint is None:
                checkpoint = InventoryResumeCheckpoint(
                    root_path=root_identity.path,
                    root_dev=root_identity.volume_id,
                    root_inode=root_identity.file_id,
                    root_birthtime_ns=root_identity.birthtime_ns,
                    policy_signature=effective_policy.signature,
                    scan_id=scan_id,
                    last_relative_cursor=None,
                    batch_digest=empty_batch_digest(),
                    batch_files=0,
                    prefix_digest=empty_prefix_digest(),
                    directory_digest=empty_directory_digest(),
                    batch_index=0,
                    files_seen=0,
                    directories_seen=0,
                    bytes_seen=0,
                    skipped_links=0,
                    excluded_directories=0,
                    errors=0,
                    status="building",
                )
            else:
                checkpoint = replace(checkpoint, status="building", stop_reason=None)
            try:
                store.write(checkpoint)
            except BaseException as exc:
                try:
                    self._complete_interrupted_scan(scan_id, initial_counters)
                except Exception as recovery_error:
                    exc.add_note(
                        "inventory checkpoint setup could not finalize its scan: "
                        f"{type(recovery_error).__name__}: {recovery_error}"
                    )
                raise
            last_checkpoint[0] = checkpoint
            if resume:
                self._delete_rows_after_cursor(
                    scan_id,
                    root_identity.path,
                    checkpoint,
                )

        def before_flush() -> None:
            # Check immediately before SQLite starts its bounded transaction;
            # the callback after commit then records the exact committed batch.
            work.check()

        def observe_admitted_file(counters: ScanCounters) -> None:
            # A later interruption can flush this batch after visiting empty
            # directories, excluded entries or links beyond its last file.
            # Bind resumable evidence to the file cursor, not to flush time.
            cursor_evidence[:] = [(replace(counters), directory_digest.value)]

        def on_flush(observations: tuple[FileObservation, ...]) -> None:
            if store is None:
                return
            for observation in observations:
                prefix_digest.observe(observation)
            current, cursor_directory_digest = cursor_evidence[0]
            next_checkpoint = InventoryResumeCheckpoint(
                root_path=root_identity.path,
                root_dev=root_identity.volume_id,
                root_inode=root_identity.file_id,
                root_birthtime_ns=root_identity.birthtime_ns,
                policy_signature=effective_policy.signature,
                scan_id=scan_id,
                last_relative_cursor=relative_cursor(root_identity.path, observations[-1].path),
                batch_digest=batch_digest(root_identity.path, observations),
                batch_files=len(observations),
                prefix_digest=prefix_digest.value,
                directory_digest=cursor_directory_digest,
                batch_index=(last_checkpoint[0].batch_index + 1 if last_checkpoint[0] else 1),
                files_seen=current.files_seen,
                directories_seen=current.directories_seen,
                bytes_seen=current.bytes_seen,
                skipped_links=current.skipped_links,
                excluded_directories=current.excluded_directories,
                errors=current.errors,
                status="building",
            )
            store.write(next_checkpoint)
            last_checkpoint[0] = next_checkpoint

        batch = InventoryBatch(
            self._connection,
            scan_id=scan_id,
            batch_size=batch_size,
            before_flush=before_flush,
            on_flush=on_flush,
        )
        traversal = InventoryTraversal(
            root_identity,
            row_sink=batch,
            exclusion_policy=effective_policy,
            progress=progress,
            deterministic=deterministic or store is not None,
            resume_cursor=(None if checkpoint is None else checkpoint.last_relative_cursor),
            resume_observation=(
                None
                if prefix_validation_digest is None
                else lambda observation: self._validate_prefix_observation(
                    scan_id,
                    root_identity.path,
                    observation,
                    prefix_validation_digest,
                    prefix_batch_observations,
                )
            ),
            admitted_file_observer=(None if store is None else observe_admitted_file),
            directory_observer=(
                None
                if store is None
                else lambda path, metadata, is_prefix: (
                    (
                        prefix_validation_directory_digest.observe(
                            path,
                            dev=metadata.st_dev,
                            inode=metadata.st_ino,
                            birthtime_ns=stat_birthtime_ns(metadata),
                        )
                        if is_prefix and prefix_validation_directory_digest is not None
                        else directory_digest.observe(
                            path,
                            dev=metadata.st_dev,
                            inode=metadata.st_ino,
                            birthtime_ns=stat_birthtime_ns(metadata),
                        )
                    )
                )
            ),
            work_check=work.check,
            file_work_check=work.check_file,
            initial_counters=initial_counters,
        )
        scan_completed = False
        try:
            self._emit_started(progress)
            counters = traversal.run()
            if checkpoint is not None:
                self._validate_resume_prefix(
                    scan_id,
                    root_identity.path,
                    checkpoint,
                    traversal,
                    prefix_validation_digest,
                    prefix_validation_directory_digest,
                    prefix_batch_observations,
                )
            root_identity.verify_unchanged()
            work.check()
            self._complete_scan(scan_id, counters)
            scan_completed = True
            work.check()
            if traversal.unsupported_path_count:
                raise InventoryUnsupportedPathEncoding(
                    traversal.unsupported_paths,
                    path_count=traversal.unsupported_path_count,
                    scan_id=scan_id,
                )
            if counters.errors:
                raise InventoryError(
                    f"inventory scan {scan_id} was partial with "
                    f"{counters.errors} traversal errors; it was not published"
                )
            if store is not None:
                current = last_checkpoint[0]
                if current is None:
                    raise InventoryError("inventory checkpoint owner was not initialized")
                final = replace(
                    current,
                    status="complete",
                    stop_reason=None,
                    prefix_digest=prefix_digest.value,
                    directory_digest=directory_digest.value,
                    files_seen=counters.files_seen,
                    directories_seen=counters.directories_seen,
                    bytes_seen=counters.bytes_seen,
                    skipped_links=counters.skipped_links,
                    excluded_directories=counters.excluded_directories,
                    errors=counters.errors,
                )
                work.check()
                store.write(final)
                last_checkpoint[0] = final
        except BaseException as exc:
            if scan_completed:
                try:
                    self._demote_scan(scan_id, traversal.counters)
                except Exception as recovery_error:
                    exc.add_note(
                        "inventory completion could not be demoted after checkpoint failure: "
                        f"{type(recovery_error).__name__}: {recovery_error}"
                    )
            else:
                try:
                    self._complete_interrupted_scan(scan_id, traversal.counters)
                except Exception as recovery_error:
                    exc.add_note(
                        "inventory interruption could not finalize its scan: "
                        f"{type(recovery_error).__name__}: {recovery_error}"
                    )
            if store is not None:
                try:
                    current = last_checkpoint[0]
                    if current is not None:
                        partial = replace(
                            current,
                            status="partial",
                            stop_reason=self._stop_reason(exc),
                        )
                        store.write(partial)
                except Exception as checkpoint_error:
                    exc.add_note(
                        "inventory resume checkpoint could not be finalized: "
                        f"{type(checkpoint_error).__name__}: {checkpoint_error}"
                    )
            raise
        self._emit_completed(progress, counters.files_seen)
        return counters.summary(scan_id, root_identity.path)

    @staticmethod
    def _stop_reason(exc: BaseException) -> str:
        reason = getattr(exc, "reason_code", None)
        if isinstance(reason, str) and reason.strip():
            return reason
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return "interrupted"
        return "failed"

    def _prepare_resume(
        self,
        checkpoint: InventoryResumeCheckpoint,
        root: RootIdentity,
        policy_signature: str,
    ) -> tuple[int, bool]:
        if (
            checkpoint.root_path != root.path
            or checkpoint.root_dev != root.volume_id
            or checkpoint.root_inode != root.file_id
            or checkpoint.root_birthtime_ns != root.birthtime_ns
        ):
            raise InventoryResumeConflictError("checkpoint root identity changed")
        if checkpoint.policy_signature != policy_signature:
            raise InventoryResumeConflictError("checkpoint inventory policy changed")
        row = self._connection.execute(
            "SELECT root,root_volume_id,root_file_id,root_birthtime_ns,status,completed_ns,"
            "inventory_policy_signature FROM scans WHERE scan_id=?",
            (checkpoint.scan_id,),
        ).fetchone()
        if row is None:
            raise InventoryResumeConflictError("checkpoint scan owner is missing")
        scan_root, volume_blob, file_blob, birth, status, completed_ns, stored_policy = row
        volume_matches = isinstance(volume_blob, (bytes, bytearray, memoryview)) and bytes(
            volume_blob
        ) == id_blob(root.volume_id)
        file_matches = isinstance(file_blob, (bytes, bytearray, memoryview)) and bytes(
            file_blob
        ) == id_blob(root.file_id)
        if (
            os.path.abspath(str(scan_root)) != root.path
            or not volume_matches
            or not file_matches
            or birth != root.birthtime_ns
            or stored_policy != policy_signature
        ):
            raise InventoryResumeConflictError("checkpoint scan owner does not match the root")
        if status == "complete":
            if checkpoint.status == "complete" and completed_ns is not None:
                return checkpoint.scan_id, True
            if checkpoint.status not in {"building", "partial"}:
                raise InventoryResumeConflictError(
                    "complete scan has a non-terminal or incomplete checkpoint"
                )
            if checkpoint.errors:
                raise InventoryResumeConflictError(
                    "complete scan has an error-bearing checkpoint"
                )
            # A crash may have published SQLite's terminal row just before the
            # cross-resource checkpoint write.  Treat its committed tail as
            # recoverable and replay from the last durable checkpoint batch.
            return checkpoint.scan_id, False
        if status not in {"partial", "building"}:
            raise InventoryResumeConflictError("scan owner is not a resumable partial scan")
        if status == "partial" and completed_ns is None:
            raise InventoryResumeConflictError("partial scan has no completion marker")
        if checkpoint.status not in {"building", "partial"}:
            raise InventoryResumeConflictError("checkpoint status cannot resume this scan")
        if checkpoint.errors:
            raise InventoryResumeConflictError(
                "a scan with traversal errors cannot resume from an untrusted prefix"
            )
        return checkpoint.scan_id, False

    def _verify_complete_checkpoint(
        self,
        checkpoint: InventoryResumeCheckpoint,
        root: RootIdentity,
        policy: InventoryExclusionPolicy,
        budget: InventoryWorkBudget,
    ) -> None:
        digest = InventoryPrefixDigest(root.path)
        directory_digest = InventoryDirectoryDigest(root.path)
        batch_observations: deque[FileObservation] = deque(maxlen=checkpoint.batch_files or 1)
        counters = ScanCounters()
        work = _InventoryWorkState(budget, files=0, bytes_seen=0)
        expected_summary = ScanCounters(
            checkpoint.files_seen,
            checkpoint.directories_seen,
            checkpoint.bytes_seen,
            checkpoint.skipped_links,
            checkpoint.excluded_directories,
            checkpoint.errors,
        ).summary(checkpoint.scan_id, root.path)
        work.check()
        if self._read_complete_summary(checkpoint.scan_id, root.path) != expected_summary:
            raise InventoryResumeConflictError("complete checkpoint scan owner counters changed")

        def observe(observation: FileObservation) -> None:
            self._validate_prefix_observation(
                checkpoint.scan_id,
                root.path,
                observation,
                digest,
                batch_observations,
            )

        traversal = InventoryTraversal(
            root,
            row_sink=_NoopInventorySink(),
            exclusion_policy=policy,
            progress=None,
            deterministic=True,
            observation_observer=observe,
            directory_observer=(
                lambda path, metadata, _is_prefix: directory_digest.observe(
                    path,
                    dev=metadata.st_dev,
                    inode=metadata.st_ino,
                    birthtime_ns=stat_birthtime_ns(metadata),
                )
            ),
            work_check=work.check,
            file_work_check=work.check_file,
            initial_counters=counters,
        )
        observed = traversal.run()
        root.verify_unchanged()
        work.check()
        if (
            observed.files_seen != checkpoint.files_seen
            or observed.directories_seen != checkpoint.directories_seen
            or observed.bytes_seen != checkpoint.bytes_seen
            or observed.skipped_links != checkpoint.skipped_links
            or observed.excluded_directories != checkpoint.excluded_directories
            or observed.errors != checkpoint.errors
            or digest.value != checkpoint.prefix_digest
            or directory_digest.value != checkpoint.directory_digest
            or len(batch_observations) != checkpoint.batch_files
            or (
                checkpoint.batch_files > 0
                and batch_digest(root.path, batch_observations) != checkpoint.batch_digest
            )
            or (
                checkpoint.batch_files == 0
                and checkpoint.batch_digest != empty_batch_digest()
            )
        ):
            raise InventoryResumeConflictError("complete inventory checkpoint no longer matches")

        # Validating each observed file alone does not detect extra owner rows.
        # Read in bounded batches so cancellation also applies to this check.
        stored_files = 0
        stored_bytes = 0
        rows = self._connection.execute(
            "SELECT size FROM files WHERE scan_id=?", (checkpoint.scan_id,)
        )
        while True:
            work.check()
            batch_rows = rows.fetchmany(MAX_BATCH_SIZE)
            if not batch_rows:
                break
            stored_files += len(batch_rows)
            stored_bytes += sum(int(row[0]) for row in batch_rows)
            if stored_files > checkpoint.files_seen or stored_bytes > checkpoint.bytes_seen:
                raise InventoryResumeConflictError("complete checkpoint scan owner rows changed")
        if stored_files != checkpoint.files_seen or stored_bytes != checkpoint.bytes_seen:
            raise InventoryResumeConflictError("complete checkpoint scan owner rows changed")

    def _delete_rows_after_cursor(
        self,
        scan_id: int,
        root: str,
        checkpoint: InventoryResumeCheckpoint,
    ) -> None:
        stale: list[tuple[int, str]] = []
        cursor_key = (
            None
            if checkpoint.last_relative_cursor is None
            else dfs_order_key(root, os.path.join(root, checkpoint.last_relative_cursor))
        )
        last_path: str | None = None
        while True:
            if last_path is None:
                rows = self._connection.execute(
                    "SELECT path FROM files WHERE scan_id=? ORDER BY path LIMIT ?",
                    (scan_id, MAX_BATCH_SIZE),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT path FROM files WHERE scan_id=? AND path>? "
                    "ORDER BY path LIMIT ?",
                    (scan_id, last_path, MAX_BATCH_SIZE),
                ).fetchall()
            if not rows:
                break
            last_path = str(rows[-1][0])
            for (path_value,) in rows:
                path = str(path_value)
                if cursor_key is None or dfs_order_key(root, path) > cursor_key:
                    stale.append((scan_id, path))
            if len(stale) >= MAX_BATCH_SIZE:
                with self._connection:
                    self._connection.executemany(
                        "DELETE FROM files WHERE scan_id=? AND path=?",
                        stale,
                    )
                stale.clear()
        if stale:
            with self._connection:
                self._connection.executemany(
                    "DELETE FROM files WHERE scan_id=? AND path=?",
                    stale,
                )
        with self._connection:
            self._connection.execute(
                "UPDATE scans SET completed_ns=NULL,status='building',files_seen=?,"
                "directories_seen=?,bytes_seen=?,skipped_links=?,excluded_directories=?,errors=? "
                "WHERE scan_id=?",
                (
                    checkpoint.files_seen,
                    checkpoint.directories_seen,
                    checkpoint.bytes_seen,
                    checkpoint.skipped_links,
                    checkpoint.excluded_directories,
                    checkpoint.errors,
                    scan_id,
                ),
            )

    def _validate_prefix_observation(
        self,
        scan_id: int,
        root: str,
        observation: FileObservation,
        digest: InventoryPrefixDigest,
        batch_observations: deque[FileObservation] | None = None,
    ) -> None:
        row = self._connection.execute(
            "SELECT volume_id,file_id,size,mtime_ns,birthtime_ns FROM files "
            "WHERE scan_id=? AND path=?",
            (scan_id, observation.path),
        ).fetchone()
        if row is None:
            raise InventoryResumeConflictError(
                f"committed inventory prefix is missing {relative_cursor(root, observation.path)}"
            )
        if (
            bytes(row[0]) != id_blob(observation.volume_id)
            or bytes(row[1]) != id_blob(observation.file_id)
            or int(row[2]) != observation.size
            or int(row[3]) != observation.mtime_ns
            or int(row[4]) != observation.birthtime_ns
        ):
            raise InventoryResumeConflictError(
                f"committed inventory prefix changed at {relative_cursor(root, observation.path)}"
            )
        digest.observe(observation)
        if batch_observations is not None:
            batch_observations.append(observation)

    def _validate_resume_prefix(
        self,
        scan_id: int,
        root: str,
        checkpoint: InventoryResumeCheckpoint,
        traversal: InventoryTraversal,
        digest: InventoryPrefixDigest | None,
        directory_digest: InventoryDirectoryDigest | None,
        batch_observations: deque[FileObservation],
    ) -> None:
        if digest is None:
            return
        if digest.value != checkpoint.prefix_digest:
            raise InventoryResumeConflictError("committed inventory prefix digest changed")
        if directory_digest is None or directory_digest.value != checkpoint.directory_digest:
            raise InventoryResumeConflictError("committed inventory directory identities changed")
        if len(batch_observations) != checkpoint.batch_files:
            raise InventoryResumeConflictError("committed inventory batch cardinality changed")
        if checkpoint.batch_files > 0 and batch_digest(root, batch_observations) != checkpoint.batch_digest:
            raise InventoryResumeConflictError("committed inventory batch digest changed")
        if checkpoint.batch_files == 0 and checkpoint.batch_digest != empty_batch_digest():
            raise InventoryResumeConflictError("committed empty inventory batch changed")
        prefix = traversal.prefix_counters
        expected = ScanCounters(
            checkpoint.files_seen,
            checkpoint.directories_seen,
            checkpoint.bytes_seen,
            checkpoint.skipped_links,
            checkpoint.excluded_directories,
            checkpoint.errors,
        )
        if prefix != expected:
            raise InventoryResumeConflictError("committed inventory prefix counters changed")
        rows = self._connection.execute(
            "SELECT path,size FROM files WHERE scan_id=?",
            (scan_id,),
        )
        prefix_count = 0
        prefix_bytes = 0
        cursor = checkpoint.last_relative_cursor
        cursor_key = (
            None
            if cursor is None
            else dfs_order_key(root, os.path.join(root, cursor))
        )
        while batch := rows.fetchmany(MAX_BATCH_SIZE):
            for path_value, size_value in batch:
                if cursor_key is None:
                    continue
                if dfs_order_key(root, str(path_value)) > cursor_key:
                    continue
                prefix_count += 1
                prefix_bytes += int(size_value)
        if prefix_count != checkpoint.files_seen or prefix_bytes != checkpoint.bytes_seen:
            raise InventoryResumeConflictError("committed inventory prefix rows changed")

    def _read_complete_summary(self, scan_id: int, root: str) -> ScanSummary:
        row = self._connection.execute(
            "SELECT files_seen,directories_seen,bytes_seen,skipped_links,"
            "excluded_directories,errors,status FROM scans WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
        if row is None or row[6] != "complete" or any(value is None for value in row[:6]):
            raise InventoryResumeConflictError("complete checkpoint has no complete scan owner")
        if int(row[5]) != 0:
            raise InventoryResumeConflictError("complete checkpoint has traversal errors")
        return ScanSummary(
            scan_id,
            root,
            int(row[0]),
            int(row[1]),
            int(row[2]),
            int(row[3]),
            int(row[4]),
            int(row[5]),
        )

    def _demote_scan(self, scan_id: int, counters: ScanCounters) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE scans SET completed_ns=?,files_seen=?,directories_seen=?,"
                "bytes_seen=?,skipped_links=?,excluded_directories=?,errors=?,status='partial' "
                "WHERE scan_id=?",
                (
                    time.time_ns(),
                    counters.files_seen,
                    counters.directories_seen,
                    counters.bytes_seen,
                    counters.skipped_links,
                    counters.excluded_directories,
                    counters.errors,
                    scan_id,
                ),
            )

    def _begin_scan(
        self,
        root: RootIdentity,
        inventory_policy_signature: str,
    ) -> int:
        cursor = self._connection.execute(
            """INSERT INTO scans(
            root,root_volume_id,root_file_id,root_birthtime_ns,started_ns,
            inventory_policy_signature)
            VALUES(?,?,?,?,?,?)""",
            (
                root.path,
                id_blob(root.volume_id),
                id_blob(root.file_id),
                root.birthtime_ns,
                time.time_ns(),
                inventory_policy_signature,
            ),
        )
        if cursor.lastrowid is None:
            raise InventoryError("SQLite did not return a scan identifier")
        self._connection.commit()
        return int(cursor.lastrowid)

    def _complete_scan(self, scan_id: int, counters: ScanCounters) -> None:
        status = "complete" if counters.errors == 0 else "partial"
        with self._connection:
            self._connection.execute(
                "UPDATE scans SET completed_ns=?,files_seen=?,directories_seen=?,"
                "bytes_seen=?,skipped_links=?,excluded_directories=?,errors=?,status=? "
                "WHERE scan_id=?",
                (
                    time.time_ns(),
                    counters.files_seen,
                    counters.directories_seen,
                    counters.bytes_seen,
                    counters.skipped_links,
                    counters.excluded_directories,
                    counters.errors,
                    status,
                    scan_id,
                ),
            )

    def _complete_interrupted_scan(
        self,
        scan_id: int,
        counters: ScanCounters,
    ) -> None:
        with self._connection:
            result = self._connection.execute(
                """UPDATE scans SET completed_ns=?,
                files_seen=(SELECT COUNT(*) FROM files WHERE scan_id=?),
                directories_seen=?,
                bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files WHERE scan_id=?),
                skipped_links=?,excluded_directories=?,errors=?,status='partial'
                WHERE scan_id=? AND completed_ns IS NULL AND status='building'""",
                (
                    time.time_ns(),
                    scan_id,
                    counters.directories_seen,
                    scan_id,
                    counters.skipped_links,
                    counters.excluded_directories,
                    counters.errors,
                    scan_id,
                ),
            )
            if result.rowcount != 1:
                raise InventoryError(f"cannot finalize interrupted inventory scan {scan_id}")

    @staticmethod
    def _emit_started(progress: ProgressCallback | None) -> None:
        emit_progress(
            progress,
            ProgressEvent("dedup", "inventory", "Inventariando archivos", 0, unit="archivos"),
        )

    @staticmethod
    def _emit_completed(progress: ProgressCallback | None, files_seen: int) -> None:
        emit_progress(
            progress,
            ProgressEvent(
                "dedup",
                "inventory",
                "Inventario completado",
                files_seen,
                files_seen,
                "archivos",
                True,
            ),
        )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "FILE_UPSERT_SQL",
    "MAX_BATCH_SIZE",
    "MAX_SCAN_BYTES",
    "MAX_SCAN_FILES",
    "InventoryBatch",
    "InventoryRow",
    "InventoryScanBudgetExceeded",
    "InventoryScanCancelled",
    "InventoryScanDeadlineExceeded",
    "InventoryScanner",
    "InventoryWorkBudget",
    "id_blob",
]
