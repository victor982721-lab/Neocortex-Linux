"""Bounded filesystem traversal used by the persistent deduplication index."""

from __future__ import annotations

import json
import os
import sqlite3
import stat as stat_module
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

import xxhash
from neocortex.platform_policy import current_platform_policy, stat_birthtime_ns

from .errors import InventoryError
from .models import ScanSummary
from _03_Progreso import ProgressCallback, ProgressEvent, emit_progress


# region [01] Inventory policy and filesystem identity

DEFAULT_BATCH_SIZE = 5000
DEFAULT_EXCLUDED_PATHS = (
    Path.home() / "AppData",
    Path.home() / ".codex",
    Path.home() / ".cache",
    Path.home() / ".sbx-denybin",
    Path.home() / "Neocortex" / "Laboratory",
    Path.home() / "Neocortex" / "Lab",
    Path.home() / "Neocortex" / "Checkpoints",
    Path.home() / "Neocortex" / "Backups",
    Path.home() / "Neocortex" / "external_backups",
    Path.home() / "Neocortex" / "Repository" / "Laboratory",
    Path.home() / "Neocortex" / "Laboratories",
    Path.home() / "Neocortex" / "TestTemp",
    current_platform_policy().state_directory,
    current_platform_policy().config_directory,
    current_platform_policy().data_directory,
    Path.home() / ".local" / "bin",
    Path.home() / ".local" / "share" / "applications",
)
DEFAULT_GENERATED_DIRECTORY_NAMES = (
    ".cdx",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "site-packages",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "pytest",
)
DEFAULT_GENERATED_DIRECTORY_PREFIXES = (
    ".tmp",
    "basetemp",
    "inline-snapshot-",
    "tmp",
)
DEFAULT_GENERATED_DIRECTORY_FRAGMENTS = ("pytest",)
DEFAULT_GENERATED_FILE_SUFFIXES = (
    ".desktop",
    ".pyc",
    ".pyo",
)
INTERNAL_DIRECTORY_PREFIXES = (".dedupe-quarantine-",)
INVENTORY_EXCLUSION_SIGNATURE_VERSION = "inventory-exclusion-policy-v4"
MAX_INVENTORY_EXCLUSION_RULES = 1024
MAX_INVENTORY_EXCLUSION_RULE_CHARS = 255
MAX_INVENTORY_EXCLUSION_PATH_CHARS = 32_767
FILE_ATTRIBUTE_HIDDEN = getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 0x00000002)
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)


def _name_key(value: str) -> str:
    """Normalize safety exclusions conservatively on every platform."""

    return value.casefold()


def _normalize_named_rules(
    values: Iterable[str],
    *,
    kind: str,
    suffixes: bool = False,
) -> tuple[str, ...]:
    """Validate and normalize bounded exact-name or suffix rules."""

    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{kind} rules must be strings")
        if (
            not value
            or len(value) > MAX_INVENTORY_EXCLUSION_RULE_CHARS
            or value in {".", ".."}
            or any(character in value for character in ("/", "\\", "*", "?", "[", "]"))
        ):
            raise ValueError(f"invalid {kind} rule: {value!r}")
        if suffixes and (not value.startswith(".") or value == "."):
            raise ValueError(f"{kind} rules must be non-empty dotted suffixes")
        normalized.add(_name_key(value))
        if len(normalized) > MAX_INVENTORY_EXCLUSION_RULES:
            raise ValueError(f"{kind} rules exceed {MAX_INVENTORY_EXCLUSION_RULES} entries")
    return tuple(sorted(normalized))


def _canonical_path_rules(
    values: Iterable[str | Path],
    *,
    kind: str,
) -> dict[str, str]:
    """Return bounded absolute real paths keyed by platform-normalized identity."""

    canonical_by_key: dict[str, str] = {}
    for value in values:
        raw_path = os.fspath(value)
        if not isinstance(raw_path, str):
            raise TypeError(f"{kind} rules must be text paths")
        if not raw_path or "\0" in raw_path:
            raise ValueError(f"invalid {kind} rule: {raw_path!r}")
        canonical = os.path.realpath(os.path.abspath(raw_path))
        if len(canonical) > MAX_INVENTORY_EXCLUSION_PATH_CHARS:
            raise ValueError(f"{kind} rules exceed {MAX_INVENTORY_EXCLUSION_PATH_CHARS} characters")
        key = os.path.normcase(canonical)
        canonical_by_key.setdefault(key, canonical)
        if len(canonical_by_key) > MAX_INVENTORY_EXCLUSION_RULES:
            raise ValueError(f"{kind} rules exceed {MAX_INVENTORY_EXCLUSION_RULES} entries")
    return canonical_by_key


def _absolute_path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _containing_path_key(
    path_key: str,
    candidate_roots: frozenset[str],
) -> str | None:
    """Find the nearest candidate equal to or containing one canonical key."""

    candidate = path_key
    while True:
        if candidate in candidate_roots:
            return candidate
        parent = os.path.normcase(os.path.dirname(candidate))
        if parent == candidate:
            return None
        candidate = parent


def _validate_restricted_topology(
    restricted_path_keys: frozenset[str],
    restricted_allowed_tree_keys: frozenset[str],
    restricted_allowed_file_keys: frozenset[str],
) -> None:
    """Reject ambiguous nested roots and allow rules outside their boundary."""

    for candidate_root in restricted_path_keys:
        parent = os.path.normcase(os.path.dirname(candidate_root))
        if (
            parent != candidate_root
            and _containing_path_key(parent, restricted_path_keys) is not None
        ):
            raise ValueError("restricted roots must not contain one another")
    for kind, allowed_keys in (
        ("restricted allowed tree", restricted_allowed_tree_keys),
        ("restricted allowed file", restricted_allowed_file_keys),
    ):
        for allowed_key in allowed_keys:
            containing_root = _containing_path_key(
                allowed_key,
                restricted_path_keys,
            )
            if containing_root is None:
                raise ValueError(f"{kind} must be within a restricted root")
            if allowed_key == containing_root:
                raise ValueError(f"{kind} must be below a restricted root")


def _restricted_traversal_directory_keys(
    restricted_path_keys: frozenset[str],
    restricted_allowed_tree_keys: frozenset[str],
    restricted_allowed_file_keys: frozenset[str],
) -> frozenset[str]:
    """Precompute the exact directories needed to reach allowlisted content."""

    traversal_keys: set[str] = set()

    def add_ancestors(candidate: str, restricted_root: str) -> None:
        while True:
            traversal_keys.add(candidate)
            if candidate == restricted_root:
                return
            candidate = os.path.normcase(os.path.dirname(candidate))

    for allowed_tree_key in restricted_allowed_tree_keys:
        restricted_root = _containing_path_key(
            allowed_tree_key,
            restricted_path_keys,
        )
        if restricted_root is not None:
            add_ancestors(allowed_tree_key, restricted_root)
    for allowed_file_key in restricted_allowed_file_keys:
        restricted_root = _containing_path_key(
            allowed_file_key,
            restricted_path_keys,
        )
        if restricted_root is not None:
            add_ancestors(
                os.path.normcase(os.path.dirname(allowed_file_key)),
                restricted_root,
            )
    return frozenset(traversal_keys)


@dataclass(frozen=True, slots=True)
class InventoryExclusionPolicy:
    """Compiled, deterministic exclusion policy shared by scan and USN."""

    signature_version: ClassVar[str] = INVENTORY_EXCLUSION_SIGNATURE_VERSION

    explicit_roots: tuple[str, ...]
    explicit_path_keys: frozenset[str]
    directory_names: frozenset[str]
    directory_prefixes: tuple[str, ...]
    directory_fragments: tuple[str, ...]
    file_names: frozenset[str]
    file_suffixes: tuple[str, ...]
    restricted_roots: tuple[str, ...]
    restricted_path_keys: frozenset[str]
    restricted_allowed_trees: tuple[str, ...]
    restricted_allowed_tree_keys: frozenset[str]
    restricted_allowed_files: tuple[str, ...]
    restricted_allowed_file_keys: frozenset[str]
    restricted_directory_names: frozenset[str]
    restricted_file_names: frozenset[str]
    restricted_file_suffixes: tuple[str, ...]
    restricted_traversal_directory_keys: frozenset[str]
    signature: str

    @classmethod
    def compile(
        cls,
        explicit_roots: Iterable[str | Path] = (),
        *,
        directory_names: Iterable[str] = (),
        directory_prefixes: Iterable[str] = (),
        directory_fragments: Iterable[str] = (),
        file_names: Iterable[str] = (),
        file_suffixes: Iterable[str] = (),
        restricted_roots: Iterable[str | Path] = (),
        restricted_allowed_trees: Iterable[str | Path] = (),
        restricted_allowed_files: Iterable[str | Path] = (),
        restricted_directory_names: Iterable[str] = (),
        restricted_file_names: Iterable[str] = (),
        restricted_file_suffixes: Iterable[str] = (),
    ) -> "InventoryExclusionPolicy":
        """Compile bounded rules and fingerprint their canonical representation."""

        canonical_by_key = _canonical_path_rules(
            explicit_roots,
            kind="explicit exclusion root",
        )
        root_keys = tuple(sorted(canonical_by_key))
        normalized_directories = _normalize_named_rules(
            directory_names,
            kind="directory-name exclusion",
        )
        normalized_directory_prefixes = _normalize_named_rules(
            directory_prefixes,
            kind="directory-prefix exclusion",
        )
        normalized_directory_fragments = _normalize_named_rules(
            directory_fragments,
            kind="directory-fragment exclusion",
        )
        normalized_files = _normalize_named_rules(
            file_names,
            kind="file-name exclusion",
        )
        normalized_suffixes = _normalize_named_rules(
            file_suffixes,
            kind="file-suffix exclusion",
            suffixes=True,
        )
        restricted_by_key = _canonical_path_rules(
            restricted_roots,
            kind="restricted root",
        )
        restricted_tree_by_key = _canonical_path_rules(
            restricted_allowed_trees,
            kind="restricted allowed tree",
        )
        restricted_file_by_key = _canonical_path_rules(
            restricted_allowed_files,
            kind="restricted allowed file",
        )
        restricted_root_keys = tuple(sorted(restricted_by_key))
        restricted_tree_keys = tuple(sorted(restricted_tree_by_key))
        restricted_file_keys = tuple(sorted(restricted_file_by_key))
        restricted_path_key_set = frozenset(restricted_root_keys)
        restricted_allowed_tree_key_set = frozenset(restricted_tree_keys)
        restricted_allowed_file_key_set = frozenset(restricted_file_keys)
        _validate_restricted_topology(
            restricted_path_key_set,
            restricted_allowed_tree_key_set,
            restricted_allowed_file_key_set,
        )
        normalized_restricted_directories = _normalize_named_rules(
            restricted_directory_names,
            kind="restricted directory-name exclusion",
        )
        normalized_restricted_files = _normalize_named_rules(
            restricted_file_names,
            kind="restricted file-name exclusion",
        )
        normalized_restricted_suffixes = _normalize_named_rules(
            restricted_file_suffixes,
            kind="restricted file-suffix exclusion",
            suffixes=True,
        )
        restricted_traversal_keys = _restricted_traversal_directory_keys(
            restricted_path_key_set,
            restricted_allowed_tree_key_set,
            restricted_allowed_file_key_set,
        )
        payload = json.dumps(
            {
                "directory_names": normalized_directories,
                "directory_prefixes": normalized_directory_prefixes,
                "directory_fragments": normalized_directory_fragments,
                "explicit_root_keys": root_keys,
                "file_names": normalized_files,
                "file_suffixes": normalized_suffixes,
                "restricted_allowed_file_keys": restricted_file_keys,
                "restricted_allowed_tree_keys": restricted_tree_keys,
                "restricted_directory_names": normalized_restricted_directories,
                "restricted_file_names": normalized_restricted_files,
                "restricted_file_suffixes": normalized_restricted_suffixes,
                "restricted_root_keys": restricted_root_keys,
                "version": cls.signature_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = xxhash.xxh3_128_hexdigest(payload)
        return cls(
            explicit_roots=tuple(canonical_by_key[key] for key in root_keys),
            explicit_path_keys=frozenset(root_keys),
            directory_names=frozenset(normalized_directories),
            directory_prefixes=normalized_directory_prefixes,
            directory_fragments=normalized_directory_fragments,
            file_names=frozenset(normalized_files),
            file_suffixes=normalized_suffixes,
            restricted_roots=tuple(restricted_by_key[key] for key in restricted_root_keys),
            restricted_path_keys=restricted_path_key_set,
            restricted_allowed_trees=tuple(
                restricted_tree_by_key[key] for key in restricted_tree_keys
            ),
            restricted_allowed_tree_keys=restricted_allowed_tree_key_set,
            restricted_allowed_files=tuple(
                restricted_file_by_key[key] for key in restricted_file_keys
            ),
            restricted_allowed_file_keys=restricted_allowed_file_key_set,
            restricted_directory_names=frozenset(normalized_restricted_directories),
            restricted_file_names=frozenset(normalized_restricted_files),
            restricted_file_suffixes=normalized_restricted_suffixes,
            restricted_traversal_directory_keys=restricted_traversal_keys,
            signature=f"{cls.signature_version}:xxh3_128:{digest}",
        )

    def excludes_directory(
        self,
        path: str | Path,
        *,
        file_attributes: int | None = None,
    ) -> bool:
        """Return whether a directory is excluded by any compiled rule."""

        path_key = _absolute_path_key(path)
        restricted_root = _containing_path_key(
            path_key,
            self.restricted_path_keys,
        )
        if restricted_root is not None:
            directory_name = _name_key(os.path.basename(os.path.abspath(os.fspath(path))))
            if directory_name in self.restricted_directory_names:
                return True
            is_inside_allowed_tree = (
                _containing_path_key(
                    path_key,
                    self.restricted_allowed_tree_keys,
                )
                is not None
            )
            if (
                path_key not in self.restricted_traversal_directory_keys
                and not is_inside_allowed_tree
            ):
                return True
            if path_key in self.explicit_path_keys and path_key != restricted_root:
                return True
            return is_excluded_directory(
                path,
                frozenset(),
                file_attributes=file_attributes,
                excluded_directory_names=self.directory_names,
                excluded_directory_prefixes=self.directory_prefixes,
                excluded_directory_fragments=self.directory_fragments,
            )
        return is_excluded_directory(
            path,
            self.explicit_path_keys,
            file_attributes=file_attributes,
            excluded_directory_names=self.directory_names,
            excluded_directory_prefixes=self.directory_prefixes,
            excluded_directory_fragments=self.directory_fragments,
        )

    def excludes_file(self, path: str | Path) -> bool:
        """Match exact file names and bounded suffixes with native semantics."""

        name = _name_key(os.path.basename(os.path.abspath(os.fspath(path))))
        if name in self.file_names or any(name.endswith(suffix) for suffix in self.file_suffixes):
            return True
        path_key = _absolute_path_key(path)
        if _containing_path_key(path_key, self.restricted_path_keys) is None:
            return False
        if name in self.restricted_file_names or any(
            name.endswith(suffix) for suffix in self.restricted_file_suffixes
        ):
            return True
        if path_key in self.restricted_allowed_file_keys:
            return False
        return (
            _containing_path_key(
                path_key,
                self.restricted_allowed_tree_keys,
            )
            is None
        )


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


def is_excluded_directory(
    path: str | Path,
    excluded_path_keys: frozenset[str],
    *,
    file_attributes: int | None = None,
    excluded_directory_names: frozenset[str] = frozenset(),
    excluded_directory_prefixes: tuple[str, ...] = (),
    excluded_directory_fragments: tuple[str, ...] = (),
) -> bool:
    """Match a configured subtree, internal quarantine, or hidden directory."""

    absolute = os.path.abspath(os.fspath(path))
    if os.path.normcase(absolute) in excluded_path_keys:
        return True
    directory_name = _name_key(os.path.basename(absolute))
    if (
        directory_name in excluded_directory_names
        or any(directory_name.startswith(prefix) for prefix in excluded_directory_prefixes)
        or any(fragment in directory_name for fragment in excluded_directory_fragments)
        or any(directory_name.startswith(prefix) for prefix in INTERNAL_DIRECTORY_PREFIXES)
    ):
        return True
    if file_attributes is None:
        try:
            file_attributes = getattr(
                os.stat(absolute, follow_symlinks=False), "st_file_attributes", 0
            )
        except OSError:
            return False
    return bool(file_attributes & FILE_ATTRIBUTE_HIDDEN)


def exclusion_path_keys(paths: Iterable[str | Path]) -> frozenset[str]:
    """Normalize explicit subtree roots once for all inventory consumers."""

    return frozenset(
        os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path)))) for path in paths
    )


DEFAULT_INVENTORY_EXCLUSION_POLICY = InventoryExclusionPolicy.compile(
    DEFAULT_EXCLUDED_PATHS,
    directory_names=DEFAULT_GENERATED_DIRECTORY_NAMES,
    directory_prefixes=DEFAULT_GENERATED_DIRECTORY_PREFIXES,
    directory_fragments=DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    file_suffixes=DEFAULT_GENERATED_FILE_SUFFIXES,
)


def resolve_inventory_exclusion_policy(
    excluded_paths: Iterable[str | Path] | None,
    exclusion_policy: InventoryExclusionPolicy | None,
) -> InventoryExclusionPolicy:
    """Resolve legacy roots or an already compiled policy without ambiguity."""

    if exclusion_policy is not None:
        if excluded_paths is not None:
            raise ValueError("excluded_paths and exclusion_policy cannot be supplied together")
        return exclusion_policy
    if excluded_paths is None:
        return DEFAULT_INVENTORY_EXCLUSION_POLICY
    return InventoryExclusionPolicy.compile(excluded_paths)


def id_blob(value: int) -> bytes:
    """Encode an unsigned filesystem identity for the SQLite schema."""

    if value < 0 or value.bit_length() > 128:
        raise InventoryError("filesystem identity does not fit an unsigned 128-bit value")
    return value.to_bytes(16, "little")


# endregion


# region [02] Bounded traversal state

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


class _DirectoryIterator(Protocol):
    def __next__(self) -> os.DirEntry[str]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _RootIdentity:
    path: str
    volume_id: int
    file_id: int
    birthtime_ns: int

    @classmethod
    def capture(cls, root: str | Path) -> "_RootIdentity":
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


@dataclass(slots=True)
class _ScanCounters:
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


class _InventoryBatch:
    """Commit at most ``batch_size`` file rows in each transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        scan_id: int,
        volume_id: int,
        batch_size: int,
    ) -> None:
        self._connection = connection
        self._scan_id = scan_id
        self._volume_blob = id_blob(volume_id)
        self._batch_size = batch_size
        self._rows: list[InventoryRow] = []

    def append(self, entry: os.DirEntry[str], item_stat: os.stat_result) -> None:
        birthtime_ns = stat_birthtime_ns(item_stat)
        self._rows.append(
            (
                os.path.abspath(entry.path),
                self._volume_blob,
                id_blob(entry.inode()),
                item_stat.st_size,
                item_stat.st_mtime_ns,
                birthtime_ns,
                self._scan_id,
            )
        )

    @property
    def full(self) -> bool:
        return len(self._rows) >= self._batch_size

    def flush(self) -> None:
        if not self._rows:
            return
        with self._connection:
            self._connection.executemany(FILE_UPSERT_SQL, self._rows)
        self._rows.clear()


class _InventoryTraversal:
    """Depth-bounded DFS that never materializes a directory's children."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        root: _RootIdentity,
        scan_id: int,
        *,
        batch_size: int,
        exclusion_policy: InventoryExclusionPolicy,
        progress: ProgressCallback | None,
    ) -> None:
        self._root = root
        self._exclusion_policy = exclusion_policy
        self._progress = progress
        self._last_progress_at = time.monotonic()
        self._counters = _ScanCounters()
        self._batch = _InventoryBatch(
            connection,
            scan_id=scan_id,
            volume_id=root.volume_id,
            batch_size=batch_size,
        )

    def run(self) -> _ScanCounters:
        stack: list[tuple[str, _DirectoryIterator | None]] = [(self._root.path, None)]
        try:
            while stack:
                self._advance(stack)
        except BaseException as exc:
            try:
                self._batch.flush()
            except Exception as flush_error:
                exc.add_note(
                    "inventory interruption could not flush its pending batch: "
                    f"{type(flush_error).__name__}: {flush_error}"
                )
            raise
        finally:
            self._close_stack(stack)
        self._batch.flush()
        return self._counters

    @property
    def counters(self) -> _ScanCounters:
        return self._counters

    def _advance(self, stack: list[tuple[str, _DirectoryIterator | None]]) -> None:
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
        stack: list[tuple[str, _DirectoryIterator | None]],
    ) -> _DirectoryIterator | None:
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
        iterator: _DirectoryIterator,
        stack: list[tuple[str, _DirectoryIterator | None]],
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
        stack: list[tuple[str, _DirectoryIterator | None]],
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
        stack: list[tuple[str, _DirectoryIterator | None]],
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
        self._batch.append(entry, item_stat)
        self._counters.files_seen += 1
        self._counters.bytes_seen += item_stat.st_size
        self._report_progress()
        if self._batch.full:
            self._batch.flush()

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
    def _close_stack(stack: list[tuple[str, _DirectoryIterator | None]]) -> None:
        for _directory, iterator in stack:
            if iterator is not None:
                iterator.close()


# endregion


# region [03] Scan lifecycle and durable publication


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
    ) -> ScanSummary:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        effective_policy = resolve_inventory_exclusion_policy(
            excluded_paths,
            exclusion_policy,
        )
        root_identity = _RootIdentity.capture(root)
        scan_id = self._begin_scan(root_identity, effective_policy.signature)
        traversal = _InventoryTraversal(
            self._connection,
            root_identity,
            scan_id,
            batch_size=batch_size,
            exclusion_policy=effective_policy,
            progress=progress,
        )
        try:
            self._emit_started(progress)
            counters = traversal.run()
            root_identity.verify_unchanged()
            self._complete_scan(scan_id, counters)
        except BaseException as exc:
            try:
                self._complete_interrupted_scan(scan_id, traversal.counters)
            except Exception as recovery_error:
                exc.add_note(
                    "inventory interruption could not finalize its scan: "
                    f"{type(recovery_error).__name__}: {recovery_error}"
                )
            raise
        if counters.errors:
            raise InventoryError(
                f"inventory scan {scan_id} was partial with "
                f"{counters.errors} traversal errors; it was not published"
            )
        self._emit_completed(progress, counters.files_seen)
        return counters.summary(scan_id, root_identity.path)

    def _begin_scan(
        self,
        root: _RootIdentity,
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

    def _complete_scan(self, scan_id: int, counters: _ScanCounters) -> None:
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
        counters: _ScanCounters,
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


# endregion
