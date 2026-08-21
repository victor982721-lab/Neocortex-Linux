"""Stage an exact Git-owned source tree without ignored build residue."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final


_GIT_RECORD_SEPARATOR: Final = "\0"


class SourceStagingError(ValueError):
    """The tracked source manifest or one of its owners is unsafe."""


@dataclass(frozen=True, slots=True)
class StagedSourceSummary:
    file_count: int
    regular_bytes: int
    symlink_count: int


def parse_git_tracked_paths(output: str) -> tuple[str, ...]:
    """Parse exact ``git ls-files -z`` output into canonical relative paths."""

    if not output or not output.endswith(_GIT_RECORD_SEPARATOR):
        raise SourceStagingError("Git tracked-source output must be non-empty and NUL-terminated")
    values = output.removesuffix(_GIT_RECORD_SEPARATOR).split(_GIT_RECORD_SEPARATOR)
    if len(values) != len(set(values)):
        raise SourceStagingError("Git tracked-source output contains duplicate paths")
    for value in values:
        _validated_relative_path(value)
    return tuple(sorted(values, key=lambda value: (value.casefold(), value)))


def _validated_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise SourceStagingError(f"tracked source path is not canonical POSIX: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceStagingError(f"tracked source path is unsafe: {value!r}")
    return path


def stage_tracked_source(
    source_root: Path,
    destination: Path,
    relative_paths: tuple[str, ...],
) -> StagedSourceSummary:
    """Copy only declared tracked owners into a new, residue-free tree."""

    source = source_root.resolve(strict=True)
    target_root = destination.resolve(strict=False)
    if target_root.exists():
        raise SourceStagingError("tracked source destination must not already exist")
    if not relative_paths or len(relative_paths) != len(set(relative_paths)):
        raise SourceStagingError("tracked source manifest must be non-empty and unique")
    target_root.mkdir(parents=True)
    regular_bytes = 0
    symlink_count = 0
    for raw_path in sorted(relative_paths, key=lambda value: (value.casefold(), value)):
        relative = _validated_relative_path(raw_path)
        owner = source.joinpath(*relative.parts)
        target = target_root.joinpath(*relative.parts)
        try:
            metadata = owner.lstat()
        except OSError as error:
            raise SourceStagingError(f"tracked source owner is unavailable: {raw_path}") from error
        target.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISLNK(metadata.st_mode):
            os.symlink(os.readlink(owner), target)
            symlink_count += 1
        elif stat.S_ISREG(metadata.st_mode):
            shutil.copy2(owner, target, follow_symlinks=False)
            regular_bytes += metadata.st_size
        else:
            raise SourceStagingError(f"tracked source owner is not a file or symlink: {raw_path}")
    return StagedSourceSummary(len(relative_paths), regular_bytes, symlink_count)


__all__ = [
    "SourceStagingError",
    "StagedSourceSummary",
    "parse_git_tracked_paths",
    "stage_tracked_source",
]
