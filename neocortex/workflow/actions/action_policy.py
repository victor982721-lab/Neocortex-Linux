"""Shared non-destructive path and snapshot safety policy for actions."""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from typing import Any, Iterator

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.inventory.index import InventoryExclusionPolicy
# region [01] Snapshot and destination policy

PROFILE_SYSTEM_PREFIXES = ("ntuser.dat", "usrclass.dat")
PROFILE_SYSTEM_NAMES = frozenset({"ntuser.ini"})
FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)


def same_snapshot(planned: FileSnapshot, current: FileSnapshot) -> bool:
    return (
        planned.identity == current.identity
        and planned.size == current.size
        and planned.mtime_ns == current.mtime_ns
        and planned.birthtime_ns == current.birthtime_ns
    )


def corrected_path(path: Path, extension: str) -> Path:
    if path.suffix:
        return path.with_suffix(extension)
    return path.with_name(path.name + extension)


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse_entry(path: Path, entry_stat: os.stat_result) -> bool:
    """Recognize links and Windows reparse entries without following them."""

    attributes = int(getattr(entry_stat, "st_file_attributes", 0))
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return bool(
        stat_module.S_ISLNK(entry_stat.st_mode)
        or is_junction(path)
        or attributes & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _is_within_root(root_key: str, candidate_key: str) -> bool:
    try:
        return os.path.commonpath((root_key, candidate_key)) == root_key
    except ValueError:
        return False


def validate_descendant_path(
    root: str | Path,
    path: str | Path,
    *,
    role: str,
) -> tuple[Path, Path]:
    """Validate lexical containment even when the root or destination is absent."""

    root_path = Path(os.path.abspath(os.fspath(root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    root_key = path_key(root_path)
    candidate_key = path_key(candidate)
    if candidate_key == root_key:
        raise RuntimeError(f"{role} is the inventory root itself: {candidate}")
    if not _is_within_root(root_key, candidate_key):
        raise RuntimeError(f"{role} escapes the inventory root lexically: {candidate}")
    try:
        relative = Path(os.path.relpath(candidate, root_path))
    except ValueError as exc:
        raise RuntimeError(f"{role} cannot be related to the inventory root: {candidate}") from exc
    if relative.is_absolute() or any(part == os.pardir for part in relative.parts):
        raise RuntimeError(f"{role} escapes the inventory root lexically: {candidate}")
    return root_path, relative


def validate_mutation_path(
    root: str | Path,
    path: str | Path,
    *,
    role: str,
    allow_missing_leaf: bool = False,
    allow_missing_tail: bool = False,
) -> os.stat_result | None:
    """Prove a mutation path is a non-reparse descendant of ``root``.

    The lexical check rejects ``..``/drive escapes before touching the path.
    Every component at and below the already-canonical inventory root is then
    inspected with ``lstat`` so a symlink, junction, or other Windows reparse
    point is never followed deliberately.  A final ``realpath`` containment
    check independently verifies the physical destination.

    ``None`` is returned for an allowed missing leaf or suffix. Existing
    ancestors are still inspected and must pass the same checks. Missing
    suffixes are useful only while creating destination directories one level
    at a time; callers must revalidate after each creation and before mutation.
    """

    root_path, relative = validate_descendant_path(root, path, role=role)
    candidate = root_path / relative

    try:
        root_stat = os.lstat(root_path)
    except OSError as exc:
        raise RuntimeError(
            f"inventory root cannot be inspected for {role}: {root_path}: {exc}"
        ) from exc
    if _is_reparse_entry(root_path, root_stat):
        raise RuntimeError(
            f"inventory root became a symlink, junction, or reparse point: {root_path}"
        )
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(f"inventory root is not a directory: {root_path}")

    current = root_path
    leaf_stat: os.stat_result | None = None
    physical_subject = root_path
    for position, part in enumerate(relative.parts):
        current /= part
        is_leaf = position == len(relative.parts) - 1
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError as exc:
            if allow_missing_tail or (allow_missing_leaf and is_leaf):
                leaf_stat = None
                physical_subject = current.parent
                break
            raise RuntimeError(
                f"{role} component no longer exists inside the inventory root: {current}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"{role} component cannot be inspected safely: {current}: {exc}"
            ) from exc
        if _is_reparse_entry(current, current_stat):
            raise RuntimeError(f"{role} traverses a symlink, junction, or reparse point: {current}")
        if not is_leaf and not stat_module.S_ISDIR(current_stat.st_mode):
            raise RuntimeError(f"{role} has a non-directory intermediate component: {current}")
        if is_leaf:
            leaf_stat = current_stat
        physical_subject = current

    try:
        physical_root_key = path_key(os.path.realpath(root_path))
        physical_subject_key = path_key(os.path.realpath(physical_subject))
    except OSError as exc:
        raise RuntimeError(
            f"{role} physical location cannot be resolved safely: {candidate}: {exc}"
        ) from exc
    if not _is_within_root(physical_root_key, physical_subject_key):
        raise RuntimeError(f"{role} escapes the inventory root physically: {candidate}")
    return leaf_stat


PROTECTED_WINDOWS_ROOTS = tuple(
    dict.fromkeys(
        path_key(value)
        for value in (
            os.environ.get("SystemRoot"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramData"),
        )
        if value
    )
)


def protected_path_reason(
    path: str | Path,
    *,
    check_attributes: bool = True,
) -> str | None:
    """Return why an action must not modify this OS/profile-managed path."""

    source = Path(path)
    name = source.name.casefold()
    if name in PROFILE_SYSTEM_NAMES or name.startswith(PROFILE_SYSTEM_PREFIXES):
        return "protected Windows user-profile state"
    key = path_key(source)
    for root in PROTECTED_WINDOWS_ROOTS:
        try:
            inside = os.path.commonpath((key, root)) == root
        except ValueError:
            inside = False
        if inside:
            return f"protected Windows system tree: {root}"
    if not check_attributes:
        return None
    try:
        attributes = int(getattr(os.stat(source, follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return None
    if attributes & FILE_ATTRIBUTE_SYSTEM:
        return "protected file with Windows SYSTEM attribute"
    if attributes & FILE_ATTRIBUTE_HIDDEN:
        return "protected file with Windows HIDDEN attribute"
    return None


# endregion [01]


# region [02] Bounded directory traversal


def postorder_directories(
    root: Path,
    exclusion_policy: InventoryExclusionPolicy,
    error_count: list[int],
) -> Iterator[Path]:
    """Yield eligible directories child-first with memory bounded by depth."""

    stack: list[tuple[Path, Any | None]] = [(root, None)]
    try:
        while stack:
            directory, iterator = stack[-1]
            if iterator is None:
                try:
                    iterator = os.scandir(directory)
                    stack[-1] = (directory, iterator)
                except OSError:
                    error_count[0] += 1
                    stack.pop()
                    continue
            try:
                entry = next(iterator)
            except StopIteration:
                iterator.close()
                stack.pop()
                if directory != root:
                    yield directory
                continue
            except OSError:
                error_count[0] += 1
                iterator.close()
                stack.pop()
                continue
            try:
                is_junction = getattr(entry, "is_junction", lambda: False)()
                if entry.is_symlink() or is_junction or not entry.is_dir(follow_symlinks=False):
                    continue
                child = Path(entry.path)
                attributes = int(
                    getattr(
                        entry.stat(follow_symlinks=False),
                        "st_file_attributes",
                        0,
                    )
                )
                if exclusion_policy.excludes_directory(child, file_attributes=attributes):
                    continue
                stack.append((child, None))
            except OSError:
                error_count[0] += 1
    finally:
        for _directory, iterator in stack:
            if iterator is not None:
                iterator.close()
# endregion [02]
