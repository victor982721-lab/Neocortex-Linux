"""Compiled filesystem exclusion policy for inventory producers."""

from __future__ import annotations

import json
import os
import stat as stat_module
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, sha256_128_hexdigest
from neocortex.platform.policy import current_platform_policy


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


def _invalid_named_rule_shape(value: str) -> bool:
    forbidden = frozenset(("/", "\\", "*", "?", "[", "]"))
    return (
        not value
        or len(value) > MAX_INVENTORY_EXCLUSION_RULE_CHARS
        or value in {".", ".."}
        or not forbidden.isdisjoint(value)
    )


def _normalized_named_rule(value: object, *, kind: str, suffixes: bool) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{kind} rules must be strings")
    if _invalid_named_rule_shape(value):
        raise ValueError(f"invalid {kind} rule: {value!r}")
    if suffixes and (not value.startswith(".") or value == "."):
        raise ValueError(f"{kind} rules must be non-empty dotted suffixes")
    return _name_key(value)


def _normalize_named_rules(
    values: Iterable[str],
    *,
    kind: str,
    suffixes: bool = False,
) -> tuple[str, ...]:
    """Validate and normalize bounded exact-name or suffix rules."""

    normalized: set[str] = set()
    for value in values:
        normalized.add(_normalized_named_rule(value, kind=kind, suffixes=suffixes))
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
    """Compiled, deterministic exclusion policy for portable inventory scans."""

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
        digest = sha256_128_hexdigest(payload)
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
            signature=f"{cls.signature_version}:{HASH_ALGORITHM_128.replace('-', '_')}:{digest}",
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
    if _matches_excluded_directory_name(
        directory_name,
        excluded_directory_names,
        excluded_directory_prefixes,
        excluded_directory_fragments,
    ):
        return True
    attributes = file_attributes
    if attributes is None:
        attributes = _directory_file_attributes(absolute)
    return attributes is not None and bool(attributes & FILE_ATTRIBUTE_HIDDEN)


def _matches_excluded_directory_name(
    directory_name: str,
    excluded_directory_names: frozenset[str],
    excluded_directory_prefixes: tuple[str, ...],
    excluded_directory_fragments: tuple[str, ...],
) -> bool:
    return bool(
        directory_name in excluded_directory_names
        or any(directory_name.startswith(prefix) for prefix in excluded_directory_prefixes)
        or any(fragment in directory_name for fragment in excluded_directory_fragments)
        or any(directory_name.startswith(prefix) for prefix in INTERNAL_DIRECTORY_PREFIXES)
    )


def _directory_file_attributes(path: str) -> int | None:
    try:
        return int(getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return None


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


__all__ = [
    "DEFAULT_EXCLUDED_PATHS",
    "DEFAULT_GENERATED_DIRECTORY_FRAGMENTS",
    "DEFAULT_GENERATED_DIRECTORY_NAMES",
    "DEFAULT_GENERATED_DIRECTORY_PREFIXES",
    "DEFAULT_GENERATED_FILE_SUFFIXES",
    "DEFAULT_INVENTORY_EXCLUSION_POLICY",
    "FILE_ATTRIBUTE_HIDDEN",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "INTERNAL_DIRECTORY_PREFIXES",
    "INVENTORY_EXCLUSION_SIGNATURE_VERSION",
    "InventoryExclusionPolicy",
    "exclusion_path_keys",
    "is_excluded_directory",
    "resolve_inventory_exclusion_policy",
]
