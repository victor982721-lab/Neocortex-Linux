"""Explicit, contained USN double for privilege-independent framework tests."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from unittest.mock import patch

from neocortex.enumeration import JournalCursor, NtfsEntry, UsnChangeBatch
from neocortex.enumeration.ntfs.volume import VolumeHandle
from _04_Nucleo_Operativo import inventory_coordinator, orchestrator, reconcile


# region [01] Contained filesystem snapshots

_AUDIT_LAB_ENVIRONMENT = "NEOCORTEX_AUDIT_LAB_ROOT"
_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_JOURNAL_ID = 0x4E454F4354455354
_REASON_DATA_OVERWRITE = 0x00000001
_REASON_FILE_CREATE = 0x00000100
_REASON_FILE_DELETE = 0x00000200
_REASON_RENAME_OLD_NAME = 0x00001000
_REASON_RENAME_NEW_NAME = 0x00002000
_MAX_FIXTURE_ENTRIES = 20_000


class SyntheticUsnContainmentError(RuntimeError):
    """The synthetic journal root no longer satisfies its test-only boundary."""


@dataclass(frozen=True, slots=True)
class _Entry:
    path: Path
    volume_id: int
    file_id: int
    parent_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.volume_id, self.file_id


@dataclass(frozen=True, slots=True)
class _Snapshot:
    files: dict[tuple[int, int], _Entry]
    directories: dict[int, Path]


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT
    )


def _birthtime_ns(metadata: os.stat_result) -> int:
    return int(getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns))


# endregion [01]


# region [02] Bounded reader


class _SyntheticReader:
    def __init__(self, journal: "SyntheticUsnJournal", start: JournalCursor):
        self._journal = journal
        self._start = start
        self._paths: dict[int, Path] = {}

    def __enter__(self) -> "_SyntheticReader":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def resolve_path(self, file_id: int) -> str:
        path = self._paths.get(file_id)
        if path is None:
            raise FileNotFoundError(file_id)
        return str(path)

    def iter_until(self, target_usn: int):
        batch, paths = self._journal._batch(self._start, target_usn)
        self._paths = paths
        if batch is not None:
            yield batch


# endregion [02]


# region [03] Public explicit context


class SyntheticUsnJournal:
    """Patch only raw-USN lookup points for one dedicated temporary corpus."""

    def __init__(self, root: Path):
        candidate = Path(root)
        if not candidate.is_absolute() or str(candidate).startswith(("\\\\", "//")):
            raise SyntheticUsnContainmentError("synthetic USN root must be an absolute local path")
        self.root = candidate.resolve(strict=True)
        if not self.root.is_dir():
            raise SyntheticUsnContainmentError("synthetic USN root is not a directory")
        boundary_raw = os.environ.get(_AUDIT_LAB_ENVIRONMENT)
        boundary = Path(tempfile.gettempdir() if boundary_raw is None else boundary_raw).resolve(
            strict=True
        )
        if self.root == boundary or not self.root.is_relative_to(boundary):
            raise SyntheticUsnContainmentError(
                "synthetic USN root must be below the active temporary laboratory"
            )
        self._boundary = boundary
        self._root_identity = self._identity(self.root)
        self._snapshots: dict[int, _Snapshot] = {}
        self._next_usn = 1
        self._patches: ExitStack | None = None
        self.raw_volume_open_attempts = 0
        self._validate_root()

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        metadata = path.stat(follow_symlinks=False)
        return int(metadata.st_dev), int(metadata.st_ino)

    def _validate_root(self) -> None:
        current = self.root.resolve(strict=True)
        if current != self.root or not current.is_relative_to(self._boundary):
            raise SyntheticUsnContainmentError("synthetic USN root changed location")
        if self._identity(current) != self._root_identity:
            raise SyntheticUsnContainmentError("synthetic USN root changed identity")
        probe = current
        while True:
            if _is_reparse(probe):
                raise SyntheticUsnContainmentError(
                    f"synthetic USN root crosses a reparse point: {probe}"
                )
            if probe == self._boundary:
                break
            if probe == probe.parent:
                raise SyntheticUsnContainmentError("synthetic USN boundary was not reached")
            probe = probe.parent

    def _snapshot(self) -> _Snapshot:
        self._validate_root()
        files: dict[tuple[int, int], _Entry] = {}
        directories: dict[int, Path] = {}
        pending = [self.root]
        count = 0
        while pending:
            directory = pending.pop()
            directory_stat = directory.stat(follow_symlinks=False)
            if int(directory_stat.st_dev) != self._root_identity[0]:
                raise SyntheticUsnContainmentError("fixture directory changed volume")
            directories[int(directory_stat.st_ino)] = directory
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
            for entry in entries:
                count += 1
                if count > _MAX_FIXTURE_ENTRIES:
                    raise SyntheticUsnContainmentError(
                        "synthetic USN fixture exceeds its bounded entry limit"
                    )
                path = Path(entry.path)
                # DirEntry.stat() reports zero device/inode fields on the
                # supported Windows/Python combination; Path.stat() exposes
                # the physical identifiers used by production snapshots.
                metadata = path.stat(follow_symlinks=False)
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                if entry.is_symlink() or attributes & _REPARSE_POINT:
                    raise SyntheticUsnContainmentError(
                        f"synthetic USN fixture contains a reparse point: {path}"
                    )
                if int(metadata.st_dev) != self._root_identity[0]:
                    raise SyntheticUsnContainmentError(
                        f"synthetic USN fixture changed volume: {path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise SyntheticUsnContainmentError(
                        f"synthetic USN fixture contains a special entry: {path}"
                    )
                identity = int(metadata.st_dev), int(metadata.st_ino)
                files[identity] = _Entry(
                    path=path,
                    volume_id=identity[0],
                    file_id=identity[1],
                    parent_id=int(directory_stat.st_ino),
                    size=int(metadata.st_size),
                    mtime_ns=int(metadata.st_mtime_ns),
                    birthtime_ns=_birthtime_ns(metadata),
                )
        return _Snapshot(files, directories)

    def capture(self, volume: str | Path) -> JournalCursor:
        self._validate_root()
        if str(volume).casefold().rstrip("\\/") != self.root.drive.casefold():
            raise SyntheticUsnContainmentError("synthetic cursor requested for a different volume")
        next_usn = self._next_usn
        self._next_usn += 1
        self._snapshots[next_usn] = self._snapshot()
        return JournalCursor(self.root.drive, _JOURNAL_ID, next_usn)

    def consume_changes(
        self,
        volume: str | Path,
        start: JournalCursor,
        **_options: object,
    ) -> _SyntheticReader:
        if str(volume).casefold().rstrip("\\/") != self.root.drive.casefold():
            raise SyntheticUsnContainmentError("synthetic reader requested for a different volume")
        return _SyntheticReader(self, start)

    @staticmethod
    def _record(entry: _Entry, reason: int, usn: int) -> NtfsEntry:
        return NtfsEntry(
            entry.file_id,
            entry.parent_id,
            entry.path.name,
            usn,
            None,
            reason,
            0,
            0,
            0,
            3,
            0,
        )

    def _batch(
        self,
        start: JournalCursor,
        target_usn: int,
    ) -> tuple[UsnChangeBatch | None, dict[int, Path]]:
        if start.journal_id != _JOURNAL_ID or start.volume.casefold() != self.root.drive.casefold():
            raise SyntheticUsnContainmentError("synthetic cursor is incompatible")
        before = self._snapshots.get(start.next_usn)
        after = self._snapshots.get(target_usn)
        if before is None or after is None:
            raise SyntheticUsnContainmentError("synthetic cursor snapshot is unavailable")
        paths = dict(before.directories)
        paths.update(after.directories)
        records: list[NtfsEntry] = []
        usn = start.next_usn
        old_ids = set(before.files)
        new_ids = set(after.files)
        for identity in sorted(old_ids - new_ids):
            records.append(self._record(before.files[identity], _REASON_FILE_DELETE, usn))
            usn += 1
        for identity in sorted(new_ids - old_ids):
            records.append(self._record(after.files[identity], _REASON_FILE_CREATE, usn))
            usn += 1
        for identity in sorted(old_ids & new_ids):
            old = before.files[identity]
            new = after.files[identity]
            if old.path != new.path:
                records.append(self._record(old, _REASON_RENAME_OLD_NAME, usn))
                usn += 1
                records.append(self._record(new, _REASON_RENAME_NEW_NAME, usn))
                usn += 1
            elif (old.size, old.mtime_ns, old.birthtime_ns) != (
                new.size,
                new.mtime_ns,
                new.birthtime_ns,
            ):
                records.append(self._record(new, _REASON_DATA_OVERWRITE, usn))
                usn += 1
        target = JournalCursor(start.volume, start.journal_id, target_usn)
        return UsnChangeBatch(start, target, tuple(records)), paths

    def _forbid_raw_volume_open(self, _handle: VolumeHandle) -> None:
        self.raw_volume_open_attempts += 1
        raise AssertionError("a synthetic USN test attempted to open a raw volume")

    def start(self) -> "SyntheticUsnJournal":
        if self._patches is not None:
            return self
        stack = ExitStack()

        def forbid_raw_open(_handle: VolumeHandle) -> None:
            self.raw_volume_open_attempts += 1
            raise AssertionError("a synthetic USN test attempted to open a raw volume")

        stack.enter_context(patch.object(orchestrator, "query_journal_cursor", self.capture))
        stack.enter_context(
            patch.object(inventory_coordinator, "query_journal_cursor", self.capture)
        )
        stack.enter_context(patch.object(reconcile, "consume_changes", self.consume_changes))
        stack.enter_context(patch.object(VolumeHandle, "open", forbid_raw_open))
        self._patches = stack
        return self

    def close(self) -> None:
        stack, self._patches = self._patches, None
        if stack is not None:
            stack.close()
        # ``unittest`` cleanups run after an enclosing TemporaryDirectory has
        # removed its corpus. Every capture validated the fixture while native
        # aliases were patched; an already-removed fixture has no callable
        # mutation boundary left to inspect.
        if self.root.exists():
            self._validate_root()

    def __enter__(self) -> "SyntheticUsnJournal":
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


# endregion [03]


__all__ = [
    "SyntheticUsnContainmentError",
    "SyntheticUsnJournal",
]
