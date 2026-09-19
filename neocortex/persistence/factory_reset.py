"""Small, filesystem-only factory reset for NeoCortex operational state.

The factory reset is deliberately different from the owner-aware selective
reset.  It does not inspect SQLite, create a backup, or write a receipt: after
the caller has selected a safe ``state`` directory it removes every entry
under that directory except installation receipts and stable control locks. The
installation, corpus, releases, models, and all paths outside the state root
remain outside this operation.

The destructive boundary is protected by the locks used by the existing
writers, a bounded ``/proc`` file-descriptor check, and SQLite's effect guard
for owners whose sidecar layout is complete.  A non-empty WAL is an ordinary
deletion target here; no read-only SQLite session or checkpoint is attempted.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    sqlite_owner_effect_guard,
)
from neocortex.persistence.state_publication import STATE_PUBLICATION_LOCK_FILENAME
from neocortex.platform.policy import current_platform_policy
from neocortex.runtime.config.app_paths import (
    default_state_directory,
    source_repository_directory,
)


_INSTALLATION_RECEIPTS = "installation-receipts"
_FIXED_LOCK_NAMES = frozenset(
    {
        "framework.lock",
        "release.lock",
        STATE_PUBLICATION_LOCK_FILENAME,
        "state-reset.lock",
    }
)
_LOCK_SUFFIXES = (".route.lock", ".lock")
_SQLITE_MAIN_SUFFIXES = (".sqlite3", ".sqlite", ".db")
_SQLITE_SIDECARS = frozenset({"-journal", "-wal", "-shm"})
_SQLITE_SIDECAR_SUFFIXES = tuple(sorted(_SQLITE_SIDECARS))
_MAX_ENTRIES = 1_000_000
_MAX_PROC_FDS = 1_000_000


class FactoryResetError(RuntimeError):
    """The factory reset was refused or could not be completed safely."""

    def __init__(
        self,
        message: str,
        *,
        partial_result: dict[str, object] | None = None,
    ) -> None:
        self.partial_result = partial_result
        super().__init__(message)


EntryKind = Literal["file", "directory", "symlink"]


@dataclass(frozen=True, slots=True)
class _Entry:
    path: Path
    relative: str
    kind: EntryKind
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Scan:
    entries: tuple[_Entry, ...]
    preserved: tuple[Path, ...]
    device: int
    inode: int
    mount_id: int


@dataclass(frozen=True, slots=True)
class _HeldLocks:
    paths: tuple[Path, ...]
    created: tuple[Path, ...]


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:  # pragma: no cover - internal invariant
        raise FactoryResetError(f"factory-reset path escaped state root: {path}") from exc


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_symlink_components(path: Path) -> None:
    """Reject a symlink in the requested root or any existing parent."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise FactoryResetError(
                f"factory-reset path cannot be inspected: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise FactoryResetError(
                f"factory-reset root cannot contain symlinks: {path}"
            )


def _protected_roots() -> tuple[Path, ...]:
    """Return installation and source roots which must never be reset."""

    try:
        policy = current_platform_policy()
    except (OSError, TypeError, ValueError) as exc:
        raise FactoryResetError("canonical NeoCortex paths cannot be resolved") from exc

    # ``factory_reset.py`` is in ``<repo>/neocortex/persistence``.  Keeping
    # this source root local avoids importing the application and never needs
    # to resolve a user-supplied path through a symlink.
    repository = Path(__file__).absolute().parents[2]
    roots = {
        repository,
        source_repository_directory().absolute(),
        repository / "data",
        repository / "models",
        policy.corpus_root.absolute(),
        policy.data_directory.absolute(),
        policy.releases_directory.absolute(),
        policy.models_directory.absolute(),
        policy.current_release.absolute(),
    }
    corpus_override = os.environ.get("NEOCORTEX_CORPUS_ROOT")
    if corpus_override:
        corpus = Path(corpus_override).expanduser()
        if not corpus.is_absolute():
            raise FactoryResetError("configured corpus root must be absolute")
        roots.add(corpus.resolve())
    return tuple(sorted(roots, key=os.fspath))


def _safe_state_directory(path: str | Path | None) -> Path:
    selected = (
        Path(default_state_directory())
        if path is None
        else Path(path).expanduser()
    )
    if not selected.is_absolute():
        raise FactoryResetError("state directory must be absolute")
    selected = Path(os.path.abspath(os.fspath(selected)))
    if selected.name.casefold() != "state":
        raise FactoryResetError("state directory must have final component 'state'")
    _reject_symlink_components(selected)

    if selected == Path(selected.anchor) or selected == Path.home().absolute():
        raise FactoryResetError("refusing to reset a filesystem or home root")
    for protected in _protected_roots():
        if _is_within(selected, protected) or _is_within(protected, selected):
            raise FactoryResetError(
                f"refusing to reset protected NeoCortex path: {selected}"
            )

    try:
        metadata = selected.lstat()
    except FileNotFoundError:
        return selected
    except OSError as exc:
        raise FactoryResetError(
            f"state directory cannot be inspected: {selected}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FactoryResetError("state directory must be a real directory")
    if int(metadata.st_uid) != os.geteuid():
        raise FactoryResetError("state directory is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & stat.S_IWOTH:
        raise FactoryResetError("state directory is world-writable")
    return selected


def _entry_from_stat(root: Path, path: Path, metadata: os.stat_result) -> _Entry:
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        kind: EntryKind = "symlink"
    elif stat.S_ISREG(mode):
        kind = "file"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    else:
        raise FactoryResetError(
            f"factory-reset refuses special filesystem entry: {path}"
        )
    return _Entry(
        path=path,
        relative=_relative(root, path),
        kind=kind,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size) if kind in {"file", "symlink"} else 0,
        mtime_ns=int(metadata.st_mtime_ns),
    )


def _mount_id_for_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FactoryResetError(f"directory mount cannot be inspected: {path}") from exc
    try:
        return _mount_id_for_fd(descriptor)
    finally:
        os.close(descriptor)


def _mount_id_for_fd(descriptor: int) -> int:
    try:
        for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="ascii").splitlines():
            if line.startswith("mnt_id:"):
                return int(line.partition(":")[2].strip())
    except (OSError, ValueError) as exc:
        raise FactoryResetError("cannot inspect directory mount identity") from exc
    raise FactoryResetError("directory mount identity is unavailable")


def _scan_state(root: Path) -> _Scan:
    """Collect deletion targets without following links or reading content."""

    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return _Scan((), (), 0, 0, 0)
    except OSError as exc:
        raise FactoryResetError(f"state directory cannot be inspected: {root}") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise FactoryResetError("state directory must be a real directory")
    root_device = int(root_metadata.st_dev)
    root_inode = int(root_metadata.st_ino)
    root_mount_id = _mount_id_for_directory(root)
    entries: list[_Entry] = []
    preserved: list[Path] = []

    def walk_closed(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise FactoryResetError(
                f"state directory cannot be enumerated: {directory}"
            ) from exc
        for child in children:
            path = directory / child.name
            if directory == root and child.name == _INSTALLATION_RECEIPTS:
                preserved.append(path)
                continue
            try:
                metadata = child.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise FactoryResetError(
                    f"state changed during factory-reset preflight: {path}"
                ) from exc
            except OSError as exc:
                raise FactoryResetError(
                    f"state entry cannot be inspected: {path}"
                ) from exc
            entry = _entry_from_stat(root, path, metadata)
            if entry.kind != "symlink" and entry.device != root_device:
                raise FactoryResetError(
                    f"factory-reset refuses to cross mount boundary: {path}"
                )
            entries.append(entry)
            if len(entries) > _MAX_ENTRIES:
                raise FactoryResetError(
                    "factory-reset state tree exceeds its entry bound"
                )
            if entry.kind == "directory":
                if _mount_id_for_directory(path) != root_mount_id:
                    raise FactoryResetError(
                        f"factory-reset refuses to cross mount boundary: {path}"
                    )
                walk_closed(path)

    walk_closed(root)
    return _Scan(
        tuple(entries), tuple(preserved), root_device, root_inode, root_mount_id
    )


def _is_lock_path(path: Path) -> bool:
    return path.name in _FIXED_LOCK_NAMES or any(
        path.name.endswith(suffix) for suffix in _LOCK_SUFFIXES
    )


def _lock_paths(root: Path, entries: Iterable[_Entry]) -> tuple[Path, ...]:
    from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY

    entries = tuple(entries)
    by_path = {entry.path: entry for entry in entries}
    paths = {root, *(root / name for name in _FIXED_LOCK_NAMES)}
    # Reserve even absent route locks before deleting an existing owner: a
    # first-time route writer must contend on the same stable lock inode.
    paths.update(root / (store.database_name + ".route.lock")
                 for store in STATE_STORE_REGISTRY.stores)
    paths.update(
        entry.path
        for entry in entries
        if entry.kind == "file" and _is_lock_path(entry.path)
    )
    artifacts = root / "artifacts"
    if by_path.get(artifacts, None) is not None and by_path[artifacts].kind == "directory":
        paths.add(artifacts)
    scratch = root / "scratch"
    if by_path.get(scratch, None) is not None and by_path[scratch].kind == "directory":
        paths.add(scratch)
        paths.update(
            entry.path
            for entry in entries
            if entry.kind == "directory"
            and entry.path.parent == scratch
        )
    historical = root / "historical-adoptions"
    if (
        by_path.get(historical, None) is not None
        and by_path[historical].kind == "directory"
    ):
        paths.add(historical / "owner.lock")
    return tuple(sorted(paths, key=os.fspath))


@contextmanager
def _held_locks(root: Path, entries: Iterable[_Entry]) -> Iterator[_HeldLocks]:
    """Acquire canonical writer locks; file presence is never the signal."""

    if os.name != "posix":
        raise FactoryResetError("factory-reset is supported only on Linux/POSIX")
    descriptors: list[tuple[Path, int]] = []
    created: list[Path] = []
    try:
        for path in _lock_paths(root, entries):
            existed = os.path.lexists(path)
            createable = path.name in _FIXED_LOCK_NAMES or path.name.endswith(".route.lock") or (
                path.name == "owner.lock"
                and path.parent.name == "historical-adoptions"
            )
            if existed:
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise FactoryResetError(
                        f"canonical lock cannot be inspected: {path}"
                    ) from exc
                if stat.S_ISLNK(metadata.st_mode) or not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                ):
                    raise FactoryResetError(
                        f"canonical lock is not a regular file or directory: {path}"
                    )
            elif not createable:
                raise FactoryResetError(f"canonical lock disappeared: {path}")
            is_directory = existed and stat.S_ISDIR(metadata.st_mode)
            flags = (
                (os.O_RDONLY | os.O_DIRECTORY if is_directory else os.O_RDWR)
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if not existed and createable:
                flags |= os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                # A writer may have created the canonical lock between the
                # preflight and this open.  Open that existing inode and let
                # flock decide whether it is actually active.
                existed = True
                try:
                    descriptor = os.open(
                        path,
                        os.O_RDWR
                        | os.O_CLOEXEC
                        | getattr(os, "O_NOFOLLOW", 0),
                    )
                except OSError as exc:
                    raise FactoryResetError(
                        f"canonical lock cannot be opened: {path}"
                    ) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise FactoryResetError(
                        f"canonical lock is a symlink: {path}"
                    ) from exc
                raise FactoryResetError(f"canonical lock cannot be opened: {path}") from exc
            try:
                if not is_directory:
                    # Never rewrite permissions on a pre-existing shared
                    # lock; the flock, not lockfile metadata, is authoritative.
                    if not existed:
                        os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise FactoryResetError(f"writer is active: {path}") from exc
            except OSError as exc:
                os.close(descriptor)
                raise FactoryResetError(f"canonical lock cannot be acquired: {path}") from exc
            descriptors.append((path, descriptor))
            if not existed:
                created.append(path)
        yield _HeldLocks(
            paths=tuple(path for path, _descriptor in descriptors),
            created=tuple(created),
        )
    finally:
        for _path, descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def _path_for_proc_target(raw: str) -> Path | None:
    if not raw.startswith("/"):
        return None
    if raw.endswith(" (deleted)"):
        raw = raw[: -len(" (deleted)")]
    return Path(os.path.abspath(raw))


def _scan_open_fds(
    root: Path,
    entries: Iterable[_Entry],
    *,
    allow_self_paths: Iterable[Path] = (),
) -> None:
    """Reject any process holding a descriptor to a deletion target.

    The canonical locks remain authoritative for cooperating writers.  This
    supplemental scan catches a writer that opened an owner directly or that
    has not yet created its lock file.  Process exit and fd close races are
    treated as benign; an inaccessible ``/proc`` root is not.
    """

    entries = tuple(entries)
    target_paths = {entry.path.absolute() for entry in entries}
    target_ids = {(entry.device, entry.inode) for entry in entries}
    allowed_paths = {path.absolute() for path in allow_self_paths}
    if not target_paths:
        return
    proc = Path("/proc")
    try:
        proc_entries = tuple(proc.iterdir())
    except OSError as exc:
        raise FactoryResetError("cannot inspect /proc for active writers") from exc
    inspected = 0
    own_pid = os.getpid()
    for process in proc_entries:
        if not process.name.isdecimal():
            continue
        fd_root = process / "fd"
        try:
            fd_entries = tuple(fd_root.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        except OSError as exc:
            raise FactoryResetError(
                f"cannot inspect process descriptors: {process.name}"
            ) from exc
        inspected += len(fd_entries)
        if inspected > _MAX_PROC_FDS:
            raise FactoryResetError("process descriptor inspection exceeds its bound")
        pid = int(process.name)
        for descriptor in fd_entries:
            try:
                raw_target = os.readlink(descriptor)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            target = _path_for_proc_target(raw_target)
            if target is None:
                continue
            if pid == own_pid and target in allowed_paths:
                continue
            try:
                opened = descriptor.stat()
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                if process.stat().st_uid == os.geteuid():
                    raise FactoryResetError("cannot inspect same-user state handle") from exc
                continue
            if (opened.st_dev, opened.st_ino) in target_ids:
                raise FactoryResetError(f"active process holds state entry: {target}")
            if _is_within(target, root) and target != root:
                # A descriptor to a preserved installation receipt is not an
                # operational writer and must not block installation use.
                if target.relative_to(root).parts[:1] == (_INSTALLATION_RECEIPTS,):
                    continue
                if target in target_paths:
                    raise FactoryResetError(
                        f"active writer descriptor targets state entry: {target}"
                    )


def _entry_matches(left: _Entry, right: _Entry) -> bool:
    return (
        left.relative == right.relative
        and left.kind == right.kind
        and left.device == right.device
        and left.inode == right.inode
        and (
            left.kind == "directory"
            or (left.size == right.size and left.mtime_ns == right.mtime_ns)
        )
    )


def _revalidate_scan(root: Path, before: _Scan, created_locks: Iterable[Path]) -> _Scan:
    after = _scan_state(root)
    if (after.device, after.inode, after.mount_id) != (
        before.device,
        before.inode,
        before.mount_id,
    ):
        raise FactoryResetError("state root changed during factory-reset lock acquisition")
    expected = {entry.relative: entry for entry in before.entries}
    allowed = {
        _relative(root, path): path
        for path in created_locks
        if path.exists()
    }
    observed = {entry.relative: entry for entry in after.entries}
    if set(observed) - set(expected) != set(allowed):
        raise FactoryResetError("state changed during factory-reset lock acquisition")
    if set(expected) - set(observed):
        raise FactoryResetError("state changed during factory-reset lock acquisition")
    for relative, expected_entry in expected.items():
        observed_entry = observed.get(relative)
        if observed_entry is None or not _entry_matches(expected_entry, observed_entry):
            raise FactoryResetError(
                f"state entry changed during factory-reset lock acquisition: {relative}"
            )
    return after


def _looks_like_sqlite_main(path: Path) -> bool:
    lowered = path.name.casefold()
    return lowered.endswith(_SQLITE_MAIN_SUFFIXES)


def _sqlite_guard_candidates(entries: tuple[_Entry, ...]) -> tuple[Path, ...]:
    regular = {entry.path for entry in entries if entry.kind == "file"}
    candidates: list[Path] = []
    for entry in entries:
        if (
            entry.kind != "file"
            or entry.size <= 0
            or not _looks_like_sqlite_main(entry.path)
        ):
            continue
        prefix = entry.path.name
        sibling_suffixes = {
            sibling.name[len(prefix) :]
            for sibling in regular
            if sibling.parent == entry.path.parent
            and sibling.name.startswith(prefix + "-")
        }
        # An incomplete sidecar set is usually an orphan from an interrupted
        # owner.  It has no SQLite connection to guard; /proc and the writer
        # locks still protect it, and it remains a deletion target.  Complete
        # layouts use the existing effect guard, which intentionally accepts a
        # non-empty WAL and never opens SQLite.
        if sibling_suffixes - _SQLITE_SIDECARS:
            continue
        if sibling_suffixes not in (set(), {"-journal"}, {"-wal", "-shm"}):
            continue
        candidates.append(entry.path)
    return tuple(sorted(candidates, key=os.fspath))


def _result(root: Path, status: str, deleted: Iterable[_Entry], preserved: Iterable[Path]) -> dict[str, object]:
    entries = tuple(deleted)
    return {
        "status": status,
        "state_directory": str(root),
        "deleted_count": len(entries),
        "deleted_bytes": sum(entry.size for entry in entries if entry.kind == "file"),
        "preserved": sorted(str(path) for path in preserved),
        "backup_created": False,
    }


def _open_root(root: Path, expected_device: int, expected_inode: int, expected_mount: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise FactoryResetError("state root cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or int(metadata.st_dev) != expected_device
            or int(metadata.st_ino) != expected_inode
            or _mount_id_for_fd(descriptor) != expected_mount
        ):
            raise FactoryResetError("state root changed during factory-reset")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent(root_fd: int, relative: str, root_device: int) -> tuple[int, str]:
    parts = tuple(part for part in relative.split("/") if part)
    if not parts:
        raise FactoryResetError("factory-reset cannot remove its state root")
    descriptor = os.dup(root_fd)
    root_mount = _mount_id_for_fd(root_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        for component in parts[:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if (not stat.S_ISDIR(metadata.st_mode)
                    or int(metadata.st_dev) != root_device
                    or _mount_id_for_fd(descriptor) != root_mount):
                raise FactoryResetError(
                    f"factory-reset parent escaped state mount: {relative}"
                )
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _delete_entry(root_fd: int, root_device: int, entry: _Entry) -> None:
    parent_fd, leaf = _open_parent(root_fd, entry.relative, root_device)
    try:
        try:
            metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise FactoryResetError(
                f"state entry disappeared during factory-reset: {entry.relative}"
            ) from exc
        if (
            int(metadata.st_dev) != entry.device
            or int(metadata.st_ino) != entry.inode
            or (
                entry.kind != "directory"
                and (
                    int(metadata.st_size) != entry.size
                    or int(metadata.st_mtime_ns) != entry.mtime_ns
                )
            )
        ):
            raise FactoryResetError(
                f"state entry changed before deletion: {entry.relative}"
            )
        if entry.kind == "directory":
            os.rmdir(leaf, dir_fd=parent_fd)
        else:
            # For a symlink this unlinks the link itself, never its target.
            os.unlink(leaf, dir_fd=parent_fd)
    except FactoryResetError:
        raise
    except OSError as exc:
        raise FactoryResetError(
            f"factory-reset could not remove {entry.relative}"
        ) from exc
    finally:
        os.close(parent_fd)


def factory_reset(state_directory: str | Path | None = None) -> dict[str, object]:
    """Delete all operational entries below one safe state root.

    The function is intentionally idempotent.  It returns a small, stable
    result mapping and never creates a backup, SQL snapshot, plan, digest, or
    receipt. Installation receipts and stable lock inodes are preserved.
    """

    root = _safe_state_directory(state_directory)
    initial = _scan_state(root)
    if not initial.entries:
        return _result(root, "complete", (), initial.preserved)

    # This first check is before even creating a coordination lock, so a busy
    # writer cannot cause a partially destructive preflight.
    _scan_open_fds(root, initial.entries)
    deleted: list[_Entry] = []
    try:
        with _held_locks(root, initial.entries) as held:
            locked_scan = _revalidate_scan(root, initial, held.created)
            # Preserve root control-lock inodes: unlink/recreate would let a
            # new writer bypass the lock we are holding. They carry no run data.
            stable_locks = frozenset(
                path for path in held.paths
                if (path.parent == root and _is_lock_path(path))
                or (path.name == "owner.lock" and path.parent == root / "historical-adoptions")
            )
            stable_control_paths = stable_locks | frozenset(
                path.parent for path in stable_locks if path.parent != root
            )
            entries = locked_scan.entries
            _scan_open_fds(
                root,
                entries,
                allow_self_paths=held.paths,
            )

            with ExitStack() as guards:
                guarded_paths = _sqlite_guard_candidates(entries)
                for database in guarded_paths:
                    try:
                        guards.enter_context(sqlite_owner_effect_guard(database))
                    except ImmutableSQLiteUnavailable as exc:
                        raise FactoryResetError(
                            f"SQLite owner is active or ambiguous: {database}"
                        ) from exc
                guard_fd_paths = [*held.paths]
                for database in guarded_paths:
                    guard_fd_paths.append(database)
                    guard_fd_paths.extend(
                        Path(f"{database}{suffix}")
                        for suffix in _SQLITE_SIDECAR_SUFFIXES
                    )
                _scan_open_fds(
                    root,
                    entries,
                    allow_self_paths=guard_fd_paths,
                )

                root_fd = _open_root(
                    root, locked_scan.device, locked_scan.inode, locked_scan.mount_id
                )
                try:
                    deletion_order = tuple(
                        sorted(
                            entries,
                            key=lambda entry: (
                                entry.relative.count("/"),
                                entry.kind == "directory",
                                entry.relative,
                            ),
                            reverse=True,
                        )
                    )
                    for entry in deletion_order:
                        if entry.path in stable_control_paths:
                            continue
                        _delete_entry(root_fd, locked_scan.device, entry)
                        deleted.append(entry)
                except FactoryResetError as exc:
                    partial = _result(root, "partial", deleted, locked_scan.preserved)
                    raise FactoryResetError(
                        f"factory-reset interrupted after {len(deleted)} entries",
                        partial_result=partial,
                    ) from exc
                finally:
                    os.close(root_fd)

            remaining = _scan_state(root)
            remaining_operational = tuple(
                entry for entry in remaining.entries if entry.path not in stable_control_paths
            )
            preserved = (*remaining.preserved, *stable_control_paths)
            if remaining_operational:
                partial = _result(root, "partial", deleted, preserved)
                raise FactoryResetError(
                    "factory-reset completed only partially",
                    partial_result=partial,
                )
            return _result(root, "complete", deleted, preserved)
    except FactoryResetError:
        raise


__all__ = ["FactoryResetError", "factory_reset"]
