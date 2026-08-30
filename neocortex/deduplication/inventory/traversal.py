"""Filesystem identity and bounded traversal for inventory scans."""

from __future__ import annotations

import os
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neocortex.platform.policy import stat_birthtime_ns
from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress

from ..domain.errors import InventoryError
from ..domain.models import ScanSummary
from .policy import FILE_ATTRIBUTE_REPARSE_POINT, InventoryExclusionPolicy


class DirectoryIterator(Protocol):
    """Minimal iterator surface returned by :func:`os.scandir`."""

    def __next__(self) -> os.DirEntry[str]: ...

    def close(self) -> None: ...


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
    ) -> None:
        self._root = root
        self._row_sink = row_sink
        self._exclusion_policy = exclusion_policy
        self._progress = progress
        self._last_progress_at = time.monotonic()
        self._counters = ScanCounters()

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
        self._row_sink.flush()
        return self._counters

    @property
    def counters(self) -> ScanCounters:
        return self._counters

    def _advance(self, stack: TraversalStack) -> None:
        directory, iterator = stack[-1]
        if iterator is None:
            iterator = self._open_directory(directory, stack)
            if iterator is None:
                return
        entry = self._next_entry(iterator, stack)
        if entry is not None:
            self._process_entry(entry, stack)

    def _open_directory(
        self,
        directory: str,
        stack: TraversalStack,
    ) -> DirectoryIterator | None:
        self._counters.directories_seen += 1
        try:
            iterator = os.scandir(directory)
        except OSError:
            self._counters.errors += 1
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
            stack.pop()
        except OSError:
            self._counters.errors += 1
            iterator.close()
            stack.pop()
        return None

    def _process_entry(
        self,
        entry: os.DirEntry[str],
        stack: TraversalStack,
    ) -> None:
        try:
            is_junction = getattr(entry, "is_junction", lambda: False)()
            if entry.is_symlink() or is_junction:
                self._counters.skipped_links += 1
                return
            if entry.is_dir(follow_symlinks=False):
                self._process_directory(entry, stack)
                return
            if entry.is_file(follow_symlinks=False):
                self._process_file(entry)
        except OSError:
            self._counters.errors += 1

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
            self._counters.excluded_directories += 1
            return
        stack.append((entry.path, None))

    def _process_file(self, entry: os.DirEntry[str]) -> None:
        if self._exclusion_policy.excludes_file(entry.path):
            return
        item_stat = entry.stat(follow_symlinks=False)
        observation = FileObservation.capture(entry, item_stat)
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
