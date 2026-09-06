"""Resolve user keeper choices against the current inventory and physical scope."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from neocortex.deduplication import DedupIndex, FileSnapshot, KeeperPolicy, snapshot_path
from neocortex.platform.policy import stat_birthtime_ns

MAX_KEEPER_SELECTORS = 256


class KeeperSelectionError(ValueError):
    """A requested preference cannot be honored without guessing or escaping scope."""


def validate_keeper_configuration(
    keep_paths: tuple[Path, ...], preferred_roots: tuple[Path, ...]
) -> None:
    for values in (keep_paths, preferred_roots):
        if not isinstance(values, tuple) or any(not isinstance(value, Path) for value in values):
            raise KeeperSelectionError("keeper paths must be immutable tuples of Path values")
        if any("\0" in str(value) for value in values):
            raise KeeperSelectionError("keeper paths cannot contain NUL")
    if len(keep_paths) + len(preferred_roots) > MAX_KEEPER_SELECTORS:
        raise KeeperSelectionError(f"at most {MAX_KEEPER_SELECTORS} keeper selectors are supported")


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat_birthtime_ns(metadata)


@dataclass(frozen=True, slots=True)
class _DirectoryBinding:
    path: Path
    identity: tuple[int, int, int]

    def verify(self) -> None:
        metadata = self.path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or self.path.resolve(strict=True) != self.path
            or _identity(metadata) != self.identity
        ):
            raise KeeperSelectionError("dedup_keeper_directory_identity_changed")


@dataclass(frozen=True, slots=True)
class KeeperInputSelection:
    policy: KeeperPolicy
    root: _DirectoryBinding
    keep_snapshots: tuple[FileSnapshot, ...]
    preferred_roots: tuple[_DirectoryBinding, ...]

    def verify(self) -> None:
        self.root.verify()
        for root in self.preferred_roots:
            root.verify()
        for expected in self.keep_snapshots:
            path = Path(expected.path)
            if path.resolve(strict=True) != path or snapshot_path(path) != expected:
                raise KeeperSelectionError("dedup_keep_snapshot_changed")


def _selected_path(value: Path, root: Path, *, directory: bool) -> Path:
    selected = Path(os.path.abspath(value.expanduser()))
    if not selected.is_relative_to(root):
        raise KeeperSelectionError("dedup_keeper_selection_outside_inventory_root")
    try:
        metadata = selected.lstat()
        if selected.resolve(strict=True) != selected:
            raise KeeperSelectionError("dedup_keeper_selection_has_symlink_or_alias")
        valid_kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if not valid_kind:
            raise KeeperSelectionError("dedup_keeper_selection_has_wrong_file_type")
    except OSError as exc:
        raise KeeperSelectionError("dedup_keeper_selection_unavailable") from exc
    return selected


def preflight_keeper_inputs(
    root: Path,
    *,
    keep_paths: tuple[Path, ...] = (),
    preferred_roots: tuple[Path, ...] = (),
) -> None:
    """Reject out-of-scope selectors before an inventory operation creates state."""

    validate_keeper_configuration(keep_paths, preferred_roots)
    if not keep_paths and not preferred_roots:
        return
    selected_root = Path(os.path.abspath(root.expanduser()))
    if selected_root.resolve(strict=True) != selected_root or not stat.S_ISDIR(
        selected_root.lstat().st_mode
    ):
        raise KeeperSelectionError("dedup_keeper_inventory_root_is_not_canonical")
    for path in keep_paths:
        _selected_path(path, selected_root, directory=False)
    for path in preferred_roots:
        _selected_path(path, selected_root, directory=True)


def resolve_keeper_inputs(
    index: DedupIndex,
    scan_id: int,
    *,
    keep_paths: tuple[Path, ...] = (),
    preferred_roots: tuple[Path, ...] = (),
) -> KeeperInputSelection:
    """Keep physical identities; never label mtime as a document revision."""

    validate_keeper_configuration(keep_paths, preferred_roots)
    root = Path(os.path.abspath(index.scan_root(scan_id)))
    root_binding = _DirectoryBinding(root, index.scan_root_identity(scan_id))
    root_binding.verify()
    selected_keeps = tuple(
        dict.fromkeys(_selected_path(path, root, directory=False) for path in keep_paths)
    )
    selected_roots = tuple(
        dict.fromkeys(_selected_path(path, root, directory=True) for path in preferred_roots)
    )
    roots = tuple(_DirectoryBinding(path, _identity(path.lstat())) for path in selected_roots)
    wanted = {str(path) for path in selected_keeps}
    observed_keeps: dict[str, FileSnapshot] = {}
    roots_with_members: set[Path] = set()
    if wanted or selected_roots:
        for snapshot in index.snapshots(scan_id):
            path = Path(snapshot.path)
            if snapshot.path in wanted:
                observed_keeps[snapshot.path] = snapshot
            roots_with_members.update(
                preferred for preferred in selected_roots if path.is_relative_to(preferred)
            )
    if wanted != set(observed_keeps):
        raise KeeperSelectionError("dedup_keep_not_in_selected_inventory")
    if set(selected_roots) != roots_with_members:
        raise KeeperSelectionError("dedup_preferred_root_has_no_inventory_members")
    snapshots = tuple(observed_keeps[str(path)] for path in selected_keeps)
    selection = KeeperInputSelection(
        policy=KeeperPolicy(
            explicit_keep_identities=tuple(sorted({snapshot.identity for snapshot in snapshots})),
            preferred_roots=tuple(str(path) for path in selected_roots),
        ),
        root=root_binding,
        keep_snapshots=snapshots,
        preferred_roots=roots,
    )
    selection.verify()
    return selection


__all__ = [
    "KeeperInputSelection",
    "KeeperSelectionError",
    "preflight_keeper_inputs",
    "resolve_keeper_inputs",
    "validate_keeper_configuration",
]
