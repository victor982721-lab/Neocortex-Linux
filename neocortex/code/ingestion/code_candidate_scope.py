"""Project-aware admission boundary for the integrated Code route.

The broad inventory is shared by every media route.  Code must therefore
distinguish owned software projects from arbitrary JSON, text and installed
dependencies without walking the corpus a second time.  This module honors an
explicit project allowlist; marker discovery never widens normal ``projects``
mode.  Bounded deterministic path rules run before candidate bytes are read.
"""

from __future__ import annotations
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal


# region [01] Stable project and exclusion vocabulary


PROJECT_MARKER_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "build.zig",
        "cargo.toml",
        "cmakelists.txt",
        "composer.json",
        "deno.json",
        "deno.jsonc",
        "gemfile",
        "go.mod",
        "mix.exs",
        "module.bazel",
        "package.json",
        "pom.xml",
        "pubspec.yaml",
        "pyproject.toml",
        "setup.py",
        "workspace",
        "workspace.bazel",
    }
)
PROJECT_MARKER_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".csproj", ".fsproj", ".sln", ".vbproj", ".vcxproj", ".xcodeproj"}
)

VENDORED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".venv",
        "env",
        "node_modules",
        "site-packages",
        "third-party",
        "third_party",
        "vendor",
        "vendors",
        "venv",
    }
)
GENERATED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".codex-lab",
        ".gradle",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".test-temp",
        ".tmp",
        ".turbo",
        "backups",
        "build",
        "checkpoints",
        "dist",
        "external_backups",
        "generated",
        "gen",
        "htmlcov",
        "lab",
        "laboratory",
        "laboratories",
        "obj",
        "out",
        "target",
        "test-temp",
        "testtemp",
        "wheelhouse",
        "__generated__",
    }
)
CACHE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
    }
)
_VENDORED_DIRECTORY_SUFFIXES: Final[tuple[str, ...]] = (
    ".egg-info",
    ".dist-info",
)
_CACHE_DIRECTORY_PREFIXES: Final[tuple[str, ...]] = (
    ".mypy_cache",
    ".pycache_",
    ".pytest-",
    ".ruff_cache",
    "pytest-cache-files-",
)

ScopeDecision = Literal["admit", "outside_project", "dependency", "generated", "cache"]


def is_project_marker(path: str | Path) -> bool:
    """Return whether a path is a sufficiently strong project-root marker."""

    candidate = Path(path)
    name = candidate.name.casefold()
    return name in PROJECT_MARKER_FILENAMES or candidate.suffix.casefold() in (
        PROJECT_MARKER_SUFFIXES
    )


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(path))))


def normalize_code_path(path: str | Path) -> str:
    """Return the lexical key shared by scope and route selection checks."""

    return _path_key(path)


def _is_vendored_part(part: str) -> bool:
    return part in VENDORED_DIRECTORY_NAMES or part.endswith(_VENDORED_DIRECTORY_SUFFIXES)


def _is_generated_part(part: str) -> bool:
    return part in GENERATED_DIRECTORY_NAMES


def _is_cache_part(part: str) -> bool:
    return part in CACHE_DIRECTORY_NAMES or part.startswith(_CACHE_DIRECTORY_PREFIXES)


def _nearest_root(path: str | Path, root_keys: frozenset[str]) -> str | None:
    candidate = _path_key(Path(path).parent)
    while True:
        if candidate in root_keys:
            return candidate
        parent = os.path.normcase(os.path.dirname(candidate))
        if parent == candidate:
            return None
        candidate = parent


def _relative_directory_parts(path: str | Path, root_key: str) -> tuple[str, ...]:
    parent_key = _path_key(Path(path).parent)
    try:
        relative = os.path.relpath(parent_key, root_key)
    except ValueError:
        return ()
    if relative == ".":
        return ()
    return tuple(part.casefold() for part in Path(relative).parts)


# endregion [01]


# region [02] Discovered immutable boundary


@dataclass(frozen=True, slots=True)
class ProjectCandidateScope:
    """A deterministic set of project roots and its candidate policy."""

    roots: tuple[str, ...]
    include_generated: bool = False
    include_vendored: bool = False
    _root_keys: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_root_keys",
            frozenset(_path_key(root) for root in self.roots),
        )

    @classmethod
    def discover(
        cls,
        paths: Iterable[str | Path],
        *,
        include_generated: bool,
        include_vendored: bool,
        explicit_roots: Iterable[str | Path] = (),
    ) -> "ProjectCandidateScope":
        """Build the explicit project allowlist for normal ``projects`` mode.

        ``paths`` is retained as part of the inventory-facing API, but marker
        discovery is deliberately not a source of authority here.  A marker
        in an arbitrary corpus must never promote its parent directory into a
        Code project; callers add a new project only by supplying its root
        explicitly.  The broad route is the separate, explicit opt-in for
        marker/code-like discovery.
        """

        # Do not inspect ``paths``.  Keeping the inventory iterable in the
        # signature preserves the bounded owner seam while making an empty or
        # disjoint allowlist fail closed instead of widening from markers.
        del paths
        raw_roots = {
            _path_key(root): os.path.abspath(os.path.expanduser(os.fspath(root)))
            for root in explicit_roots
        }

        accepted: dict[str, str] = {}
        for root_key, root in sorted(
            raw_roots.items(), key=lambda item: (len(Path(item[0]).parts), item[0])
        ):
            parent_key = _nearest_root(Path(root) / "project.marker", frozenset(accepted))
            if parent_key is not None:
                relative_parts = _relative_directory_parts(
                    Path(root) / "project.marker", parent_key
                )
                if not include_vendored and any(_is_vendored_part(part) for part in relative_parts):
                    continue
                if not include_generated and any(
                    _is_generated_part(part) for part in relative_parts
                ):
                    continue
                if any(_is_cache_part(part) for part in relative_parts):
                    continue
            accepted[root_key] = root

        return cls(
            roots=tuple(accepted[key] for key in sorted(accepted)),
            include_generated=include_generated,
            include_vendored=include_vendored,
        )

    @property
    def root_count(self) -> int:
        return len(self.roots)

    def decision(self, path: str | Path) -> ScopeDecision:
        """Explain whether one inventory path belongs to admitted project code."""

        root_key = _nearest_root(path, self._root_keys)
        if root_key is None:
            return "outside_project"
        relative_parts = _relative_directory_parts(path, root_key)
        if any(_is_cache_part(part) for part in relative_parts):
            return "cache"
        if not self.include_vendored and any(_is_vendored_part(part) for part in relative_parts):
            return "dependency"
        if not self.include_generated and any(_is_generated_part(part) for part in relative_parts):
            return "generated"
        return "admit"


# endregion [02]


__all__ = [
    "CACHE_DIRECTORY_NAMES",
    "GENERATED_DIRECTORY_NAMES",
    "PROJECT_MARKER_FILENAMES",
    "PROJECT_MARKER_SUFFIXES",
    "VENDORED_DIRECTORY_NAMES",
    "ProjectCandidateScope",
    "ScopeDecision",
    "is_project_marker",
    "normalize_code_path",
]
