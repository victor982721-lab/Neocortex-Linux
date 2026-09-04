"""Filesystem identity and bounded traversal for inventory scans."""

from __future__ import annotations

import os
import stat as stat_module
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neocortex.platform.policy import stat_birthtime_ns
from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress

from ..domain.errors import InventoryError
from ..domain.models import ScanSummary
from .policy import FILE_ATTRIBUTE_REPARSE_POINT, InventoryExclusionPolicy
from .resume import MAX_SORTED_DIRECTORY_ENTRIES, relative_cursor


class DirectoryIterator(Protocol):
    """Minimal iterator surface returned by :func:`os.scandir`."""

    def __next__(self) -> os.DirEntry[str]: ...

    def close(self) -> None: ...


class _SortedDirectoryIterator:
    """Bounded deterministic iterator used by resumable scans."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = iter(entries)

    def __next__(self) -> os.DirEntry[str]:
        return next(self._entries)

    def close(self) -> None:
        return None


class InventoryRowSink(Protocol):
    """Persistence boundary consumed by the filesystem traversal."""

    def append(self, observation: "FileObservation") -> None: ...

    @property
    def full(self) -> bool: ...

    def flush(self) -> None: ...


def validate_inventory_root(root: str | Path) -> Path:
    """Reject a reparse root and return its stable canonical path."""

    absolute = os.path.abspath(os.fspath(root))
    try:
        root_stat = os.lstat(absolute)
    except OSError as exc:
        raise InventoryError(f"cannot inspect inventory root: {absolute}: {exc}") from exc

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    attributes = int(getattr(root_stat, "st_file_attributes", 0))
    if (
        stat_module.S_ISLNK(root_stat.st_mode)
        or is_junction(absolute)
        or attributes & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise InventoryError(
            f"inventory root cannot be a symlink, junction, or reparse point: {absolute}"
        )
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise InventoryError(f"inventory root is not a directory: {absolute}")

    canonical = os.path.realpath(absolute)
    if not os.path.isdir(canonical):
        raise InventoryError(f"inventory root is not a directory: {canonical}")
    return Path(canonical)


@dataclass(frozen=True, slots=True)
class RootIdentity:
    """Filesystem identity captured before traversal and verified before publish."""

    path: str
    volume_id: int
    file_id: int
    birthtime_ns: int

    @classmethod
    def capture(cls, root: str | Path) -> "RootIdentity":
        path = os.fspath(validate_inventory_root(root))
        root_stat = os.stat(path, follow_symlinks=False)
        return cls(
            path=path,
            volume_id=root_stat.st_dev,
            file_id=root_stat.st_ino,
            birthtime_ns=stat_birthtime_ns(root_stat),
        )

    def verify_unchanged(self) -> None:
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise InventoryError(
                f"inventory root disappeared while scanning: {self.path}: {exc}"
            ) from exc
        current_birthtime_ns = stat_birthtime_ns(current)
        if (
            current.st_dev != self.volume_id
            or current.st_ino != self.file_id
            or current_birthtime_ns != self.birthtime_ns
        ):
            raise InventoryError(f"inventory root changed while scanning: {self.path}")


@dataclass(frozen=True, slots=True)
class FileObservation:
    """Stable metadata captured from one non-link file during traversal."""

    path: str
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int

    @classmethod
    def capture(
        cls,
        entry: os.DirEntry[str],
        item_stat: os.stat_result,
    ) -> "FileObservation":
        return cls(
            path=os.path.abspath(entry.path),
            file_id=entry.inode(),
            size=item_stat.st_size,
            mtime_ns=item_stat.st_mtime_ns,
            birthtime_ns=stat_birthtime_ns(item_stat),
        )


@dataclass(slots=True)
class ScanCounters:
    """Mutable traversal totals used for progress and durable scan status."""

    files_seen: int = 0
    directories_seen: int = 0
    bytes_seen: int = 0
    skipped_links: int = 0
    excluded_directories: int = 0
    errors: int = 0

    def summary(self, scan_id: int, root: str) -> ScanSummary:
        return ScanSummary(
            scan_id,
            root,
            self.files_seen,
            self.directories_seen,
            self.bytes_seen,
            self.skipped_links,
            self.excluded_directories,
            self.errors,
        )


type TraversalStack = list[tuple[str, DirectoryIterator | None]]


class InventoryTraversal:
    """Depth-bounded DFS that never materializes a directory's children."""

    def __init__(
        self,
        root: RootIdentity,
        *,
        row_sink: InventoryRowSink,
        exclusion_policy: InventoryExclusionPolicy,
        progress: ProgressCallback | None,
        deterministic: bool = False,
        resume_cursor: str | None = None,
        resume_observation: Callable[[FileObservation], None] | None = None,
        observation_observer: Callable[[FileObservation], None] | None = None,
        directory_observer: Callable[[str, os.stat_result, bool], None] | None = None,
        work_check: Callable[[int], None] | None = None,
        file_work_check: Callable[[int], None] | None = None,
        initial_counters: ScanCounters | None = None,
    ) -> None:
        if resume_cursor is not None and resume_observation is None:
            raise ValueError("resume_observation is required with resume_cursor")
        self._root = root
        self._row_sink = row_sink
        self._exclusion_policy = exclusion_policy
        self._progress = progress
        self._deterministic = deterministic
        self._resume_cursor = resume_cursor
        self._resume_active = resume_cursor is None
        self._resume_found = resume_cursor is None
        self._resume_observation = resume_observation
        self._observation_observer = observation_observer
        self._directory_observer = directory_observer
        self._work_check = work_check
        self._file_work_check = file_work_check
        self._last_progress_at = time.monotonic()
        self._directory_identities: dict[str, tuple[int, int, int]] = {}
        self._prefix_counters = ScanCounters()
        self._counters = (
            ScanCounters()
            if initial_counters is None
            else ScanCounters(
                initial_counters.files_seen,
                initial_counters.directories_seen,
                initial_counters.bytes_seen,
                initial_counters.skipped_links,
                initial_counters.excluded_directories,
                initial_counters.errors,
            )
        )

    def run(self) -> ScanCounters:
        stack: TraversalStack = [(self._root.path, None)]
        try:
            while stack:
                self._advance(stack)
        except BaseException as exc:
            try:
                self._row_sink.flush()
            except Exception as flush_error:
                exc.add_note(
                    "inventory interruption could not flush its pending batch: "
                    f"{type(flush_error).__name__}: {flush_error}"
                )
            raise
        finally:
            self._close_stack(stack)
        if not self._resume_found:
            raise InventoryError(f"inventory resume cursor was not found: {self._resume_cursor}")
        self._row_sink.flush()
        return self._counters

    @property
    def counters(self) -> ScanCounters:
        return self._counters

    @property
    def prefix_counters(self) -> ScanCounters:
        """Counts observed before a resume cursor, excluding the active tail."""

        return self._prefix_counters

    def _advance(self, stack: TraversalStack) -> None:
        directory, iterator = stack[-1]
        if iterator is None:
            iterator = self._open_directory(directory, stack)
            if iterator is None:
                return
        if self._deterministic:
            current = os.stat(directory, follow_symlinks=False)
            observed_identity = (
                current.st_dev,
                current.st_ino,
                stat_birthtime_ns(current),
            )
            if observed_identity != self._directory_identities.get(directory):
                raise InventoryError("inventory directory ancestor changed while scanning")
        entry = self._next_entry(iterator, stack)
        if entry is not None:
            self._process_entry(entry, stack)

    def _open_directory(
        self,
        directory: str,
        stack: TraversalStack,
    ) -> DirectoryIterator | None:
        if self._resume_active:
            self._counters.directories_seen += 1
        else:
            self._prefix_counters.directories_seen += 1
        try:
            directory_stat = os.stat(directory, follow_symlinks=False)
            if not stat_module.S_ISDIR(directory_stat.st_mode):
                raise InventoryError("inventory directory is no longer a directory")
            if self._deterministic:
                canonical_directory = Path(os.path.realpath(directory))
                if canonical_directory != Path(os.path.abspath(directory)):
                    raise InventoryError(
                        "inventory directory escapes its root through a symlink"
                    )
                try:
                    canonical_directory.relative_to(Path(self._root.path))
                except ValueError as exc:
                    raise InventoryError("inventory directory escapes its root") from exc
                with os.scandir(directory) as source:
                    entries: list[os.DirEntry[str]] = []
                    for entry in source:
                        if len(entries) >= MAX_SORTED_DIRECTORY_ENTRIES:
                            raise InventoryError(
                                "inventory directory exceeds deterministic sort bound"
                            )
                        entries.append(entry)
                entries.sort(key=lambda entry: os.fsencode(entry.name))
                iterator: DirectoryIterator = _SortedDirectoryIterator(entries)
            else:
                iterator = os.scandir(directory)
            if self._deterministic or self._directory_observer is not None:
                after_stat = os.stat(directory, follow_symlinks=False)
                if (
                    after_stat.st_dev,
                    after_stat.st_ino,
                    stat_birthtime_ns(after_stat),
                ) != (
                    directory_stat.st_dev,
                    directory_stat.st_ino,
                    stat_birthtime_ns(directory_stat),
                ):
                    iterator.close()
                    raise InventoryError("inventory directory changed while opening")
            if self._directory_observer is not None:
                self._directory_observer(directory, directory_stat, not self._resume_active)
            self._directory_identities[directory] = (
                directory_stat.st_dev,
                directory_stat.st_ino,
                stat_birthtime_ns(directory_stat),
            )
        except InventoryError:
            raise
        except OSError:
            if self._resume_active:
                self._counters.errors += 1
            else:
                self._prefix_counters.errors += 1
            stack.pop()
            return None
        stack[-1] = (directory, iterator)
        return iterator

    def _next_entry(
        self,
        iterator: DirectoryIterator,
        stack: TraversalStack,
    ) -> os.DirEntry[str] | None:
        try:
            return next(iterator)
        except StopIteration:
            iterator.close()
            self._directory_identities.pop(stack[-1][0], None)
            stack.pop()
        except OSError:
            if self._resume_active:
                self._counters.errors += 1
            else:
                self._prefix_counters.errors += 1
            iterator.close()
            self._directory_identities.pop(stack[-1][0], None)
            stack.pop()
        return None

    def _process_entry(
        self,
        entry: os.DirEntry[str],
        stack: TraversalStack,
    ) -> None:
        try:
            if self._work_check is not None:
                self._work_check(0)
            is_junction = getattr(entry, "is_junction", lambda: False)()
            is_link = entry.is_symlink() or is_junction
            if not self._resume_active:
                relative = relative_cursor(self._root.path, entry.path)
                relative_bytes = os.fsencode(relative)
                cursor_bytes = os.fsencode(self._resume_cursor)  # type: ignore[arg-type]
                if relative_bytes > cursor_bytes:
                    self._resume_active = True
                    self._resume_found = True
                elif relative_bytes != cursor_bytes and not is_link:
                    if entry.is_file(follow_symlinks=False):
                        self._process_file(entry)
                        return
            if is_link:
                if self._resume_active:
                    self._counters.skipped_links += 1
                else:
                    self._prefix_counters.skipped_links += 1
                return
            if self._deterministic and Path(os.path.realpath(entry.path)) != Path(
                os.path.abspath(entry.path)
            ):
                raise InventoryError("inventory entry escapes its root through a symlink")
            if entry.is_dir(follow_symlinks=False):
                self._process_directory(entry, stack)
                return
            if entry.is_file(follow_symlinks=False):
                self._process_file(entry)
        except InventoryError:
            raise
        except OSError:
            if self._resume_active:
                self._counters.errors += 1
            else:
                self._prefix_counters.errors += 1

    def _process_directory(
        self,
        entry: os.DirEntry[str],
        stack: TraversalStack,
    ) -> None:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        if self._exclusion_policy.excludes_directory(
            entry.path,
            file_attributes=attributes,
        ):
            if self._resume_active:
                self._counters.excluded_directories += 1
            else:
                self._prefix_counters.excluded_directories += 1
            return
        stack.append((entry.path, None))

    def _process_file(self, entry: os.DirEntry[str]) -> None:
        if self._exclusion_policy.excludes_file(entry.path):
            return
        item_stat = entry.stat(follow_symlinks=False)
        observation = FileObservation.capture(entry, item_stat)
        if self._observation_observer is not None:
            self._observation_observer(observation)
        if not self._resume_active:
            self._prefix_counters.files_seen += 1
            self._prefix_counters.bytes_seen += observation.size
            if self._resume_observation is not None:
                self._resume_observation(observation)
            if relative_cursor(self._root.path, observation.path) == self._resume_cursor:
                self._resume_active = True
                self._resume_found = True
            return
        if self._file_work_check is not None:
            self._file_work_check(observation.size)
        self._row_sink.append(observation)
        self._counters.files_seen += 1
        self._counters.bytes_seen += observation.size
        self._report_progress()
        if self._row_sink.full:
            self._row_sink.flush()

    def _report_progress(self) -> None:
        now = time.monotonic()
        if self._counters.files_seen % 512 != 0 and now - self._last_progress_at < 0.25:
            return
        emit_progress(
            self._progress,
            ProgressEvent(
                "dedup",
                "inventory",
                "Inventariando archivos",
                self._counters.files_seen,
                unit="archivos",
            ),
        )
        self._last_progress_at = now

    @staticmethod
    def _close_stack(stack: TraversalStack) -> None:
        for _directory, iterator in stack:
            if iterator is not None:
                iterator.close()


__all__ = [
    "DirectoryIterator",
    "FileObservation",
    "InventoryRowSink",
    "InventoryTraversal",
    "RootIdentity",
    "ScanCounters",
    "TraversalStack",
    "validate_inventory_root",
]
