"""Runtime cache placement stays outside the protected HOME cache tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.runtime.config.runtime_cache import (
    XDG_CACHE_HOME_ENVIRONMENT,
    configure_runtime_cache,
    resolve_runtime_cache_directory,
)


def test_default_runtime_cache_is_a_state_child_and_does_not_create_home_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    environment: dict[str, str] = {}
    state = tmp_path / "state"

    cache = configure_runtime_cache(state, environ=environment)

    assert cache == state / "runtime-cache"
    assert environment[XDG_CACHE_HOME_ENVIRONMENT] == str(cache)
    assert not (home / ".cache").exists()
    assert not state.exists()


def test_explicit_cache_override_is_preserved_when_disjoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    override = tmp_path / "cache"
    environment = {XDG_CACHE_HOME_ENVIRONMENT: str(override)}

    cache = configure_runtime_cache(tmp_path / "state", environ=environment)

    assert cache == override
    assert environment[XDG_CACHE_HOME_ENVIRONMENT] == str(override)
    assert not override.exists()


def test_explicit_home_cache_override_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    environment = {XDG_CACHE_HOME_ENVIRONMENT: str(home / ".cache")}

    with pytest.raises(ValueError, match="disjoint from the protected HOME cache"):
        configure_runtime_cache(tmp_path / "state", environ=environment)


def test_state_under_home_cache_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="state_directory must be disjoint"):
        resolve_runtime_cache_directory(home / ".cache" / "state")
