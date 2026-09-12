"""Safe process-wide cache configuration for optional ONNX runtimes.

FastEmbed and its Hugging Face/ONNX dependencies may create cache files through
their own process trees.  The caller must configure this module before importing
or constructing those dependencies.  The environment mutation is intentional:
``multiprocessing`` workers created with the ``spawn`` start method inherit the
parent environment, so they observe the same bounded cache root.

This module does not create the state or cache directories.  Directory creation
remains owned by the lifecycle/state setup that has already authorized the
selected state directory.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


RUNTIME_CACHE_DIRECTORY_NAME = "runtime-cache"
XDG_CACHE_HOME_ENVIRONMENT = "XDG_CACHE_HOME"


class RuntimeCacheConfigurationError(ValueError):
    """The process-wide runtime cache cannot be placed safely."""


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path))


def _absolute_canonical_path(value: str | os.PathLike[str], *, label: str) -> Path:
    """Return an absolute path and reject lexical/physical aliases.

    The state tree may not yet exist when the CLI parses its configuration.  In
    that case ``realpath`` still resolves every existing ancestor and preserves
    the missing suffix, which lets us reject symlink/reparse traversal without
    creating anything.  A caller that wants a path relative to the current
    directory must resolve it before calling this boundary.
    """

    try:
        requested = Path(os.fspath(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeCacheConfigurationError(f"{label} must be an absolute path") from exc
    if not requested.is_absolute():
        raise RuntimeCacheConfigurationError(f"{label} must be absolute: {requested}")

    normalized = Path(os.path.abspath(os.path.normpath(os.fspath(requested))))
    physical = Path(os.path.realpath(os.fspath(normalized)))
    if _path_key(normalized) != _path_key(physical):
        raise RuntimeCacheConfigurationError(
            f"{label} cannot traverse a symlink or reparse point: {normalized}"
        )
    return normalized


def _same_or_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _trees_intersect(left: Path, right: Path) -> bool:
    return _same_or_descendant(left, right) or _same_or_descendant(right, left)


def _home_cache_directory() -> Path:
    return _absolute_canonical_path(Path.home() / ".cache", label="HOME cache directory")


def _validate_directory_candidate(path: Path, *, label: str) -> None:
    """Reject an existing non-directory without requiring a missing path."""

    try:
        exists = os.path.lexists(path)
    except OSError as exc:
        raise RuntimeCacheConfigurationError(
            f"{label} existence cannot be verified: {path}"
        ) from exc
    if exists and not path.is_dir():
        raise RuntimeCacheConfigurationError(f"{label} must be a directory: {path}")


def resolve_runtime_cache_directory(
    state_directory: str | os.PathLike[str],
) -> Path:
    """Resolve the default runtime cache as a strict child of ``state_directory``.

    The selected state tree and the protected ``$HOME/.cache`` tree must be
    disjoint.  The latter is where an unset ``XDG_CACHE_HOME`` would otherwise
    place ONNX/Hugging Face files in the protected user profile.  No filesystem
    mutation is performed.
    """

    state = _absolute_canonical_path(state_directory, label="state_directory")
    _validate_directory_candidate(state, label="state_directory")
    protected_home_cache = _home_cache_directory()
    if _trees_intersect(state, protected_home_cache):
        raise RuntimeCacheConfigurationError(
            "state_directory must be disjoint from the protected HOME cache: "
            f"{state} intersects {protected_home_cache}"
        )

    runtime_cache = _absolute_canonical_path(
        state / RUNTIME_CACHE_DIRECTORY_NAME,
        label="runtime cache directory",
    )
    if not _same_or_descendant(runtime_cache, state) or _path_key(runtime_cache) == _path_key(state):
        raise RuntimeCacheConfigurationError(
            "runtime cache directory must be a strict child of state_directory"
        )
    _validate_directory_candidate(runtime_cache, label="runtime cache directory")
    if _trees_intersect(runtime_cache, protected_home_cache):
        raise RuntimeCacheConfigurationError(
            "runtime cache directory must be disjoint from the protected HOME cache: "
            f"{runtime_cache} intersects {protected_home_cache}"
        )
    return runtime_cache


def configure_runtime_cache(
    state_directory: str | os.PathLike[str],
    *,
    environ: MutableMapping[str, str] | None = None,
) -> Path:
    """Configure and return the effective process-wide runtime cache root.

    ``XDG_CACHE_HOME`` is preserved when explicitly present, but its path is
    still required to be absolute, canonical, directory-shaped, and disjoint
    from the protected ``$HOME/.cache`` tree.  When it is absent, the bounded
    state child returned by :func:`resolve_runtime_cache_directory` is written
    to the supplied environment mapping (``os.environ`` by default).  Updating
    ``os.environ`` rather than using a temporary context is what makes the
    setting available to ONNX/FastEmbed workers started with ``spawn``.

    The function validates the selected state boundary even when an explicit
    cache override is supplied, and it never creates directories.
    """

    environment = os.environ if environ is None else environ
    default_cache = resolve_runtime_cache_directory(state_directory)
    raw_override = environment.get(XDG_CACHE_HOME_ENVIRONMENT)
    if raw_override is None:
        environment[XDG_CACHE_HOME_ENVIRONMENT] = os.fspath(default_cache)
        return default_cache

    override = _absolute_canonical_path(raw_override, label=XDG_CACHE_HOME_ENVIRONMENT)
    _validate_directory_candidate(override, label=XDG_CACHE_HOME_ENVIRONMENT)
    protected_home_cache = _home_cache_directory()
    if _trees_intersect(override, protected_home_cache):
        raise RuntimeCacheConfigurationError(
            f"{XDG_CACHE_HOME_ENVIRONMENT} must be disjoint from the protected HOME cache: "
            f"{override} intersects {protected_home_cache}"
        )
    return override


__all__ = [
    "RUNTIME_CACHE_DIRECTORY_NAME",
    "XDG_CACHE_HOME_ENVIRONMENT",
    "RuntimeCacheConfigurationError",
    "configure_runtime_cache",
    "resolve_runtime_cache_directory",
]
