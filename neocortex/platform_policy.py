"""Central, side-effect-free platform and per-user path policy.

The policy is intentionally independent from the operational framework so the
inventory, CLI, desktop frontend, and release tooling can share one source of
truth without creating state while merely inspecting the current platform.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

APPLICATION_DIRECTORY_NAME = "Neocortex"
LINUX_MUTATION_REASON = "linux_mutation_backend_unavailable"
UNAVAILABLE_BIRTHTIME_NS = -1
POSIX_PHYSICAL_IDENTITY_SCHEME = "posix_device_inode_birthtime"
WINDOWS_PHYSICAL_IDENTITY_SCHEME = "windows_file_id_birthtime"
PHYSICAL_IDENTITY_SCHEMES = frozenset(
    {POSIX_PHYSICAL_IDENTITY_SCHEME, WINDOWS_PHYSICAL_IDENTITY_SCHEME}
)

_USER_DIR_PATTERN = re.compile(r'^XDG_DOCUMENTS_DIR=(?P<quote>["\'])(?P<value>.*)(?P=quote)$')


def _absolute_environment_path(name: str, fallback: Path) -> Path:
    candidate = Path(os.environ.get(name, os.fspath(fallback))).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{name} must name an absolute path: {candidate}")
    return candidate


def linux_config_home() -> Path:
    return _absolute_environment_path("XDG_CONFIG_HOME", Path.home() / ".config")


def linux_state_home() -> Path:
    return _absolute_environment_path("XDG_STATE_HOME", Path.home() / ".local" / "state")


def linux_data_home() -> Path:
    return _absolute_environment_path("XDG_DATA_HOME", Path.home() / ".local" / "share")


def resolve_xdg_documents_directory(
    *,
    home: Path | None = None,
    config_home: Path | None = None,
) -> Path:
    """Resolve ``XDG_DOCUMENTS_DIR`` without evaluating shell text.

    Only the formats emitted by ``xdg-user-dirs-update`` are accepted: an
    absolute path or a path beginning with the exact ``$HOME``/``${HOME}``
    token. Command substitutions, additional variables, and relative paths
    are ignored and cause the conservative ``~/Documents`` fallback.
    """

    profile = (Path.home() if home is None else Path(home)).expanduser()
    configured_home = linux_config_home() if config_home is None else Path(config_home)
    fallback = profile / "Documents"
    try:
        lines = (
            (configured_home / "user-dirs.dirs")
            .read_text(
                encoding="utf-8",
                errors="strict",
            )
            .splitlines()
        )
    except (OSError, UnicodeError):
        return fallback
    for raw_line in lines:
        match = _USER_DIR_PATTERN.fullmatch(raw_line.strip())
        if match is None:
            continue
        value = match.group("value")
        if any(token in value for token in ("`", "$(", "\x00")):
            return fallback
        if value == "$HOME" or value == "${HOME}":
            candidate = profile
        elif value.startswith("$HOME/"):
            candidate = profile / value[len("$HOME/") :]
        elif value.startswith("${HOME}/"):
            candidate = profile / value[len("${HOME}/") :]
        elif value.startswith("/") and "$" not in value:
            candidate = Path(value)
        else:
            return fallback
        if not candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            return fallback
        return candidate
    return fallback


@dataclass(frozen=True, slots=True)
class PlatformPolicy:
    """Resolved platform capabilities and canonical per-user paths."""

    system: str
    corpus_root: Path
    state_directory: Path
    config_directory: Path
    data_directory: Path
    releases_directory: Path
    current_release: Path
    models_directory: Path
    runtimes_directory: Path
    stable_launcher: Path
    user_alias: Path
    desktop_file: Path
    inventory_backend: str
    identity_backend: str
    containment_backend: str
    elevation: str
    mutation_backend: str
    mutation_available: bool
    compatible: bool

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        for key, value in tuple(payload.items()):
            if isinstance(value, Path):
                payload[key] = os.fspath(value)
        return payload


def current_platform_policy(*, platform_name: str | None = None) -> PlatformPolicy:
    """Return the current policy without creating or modifying any path."""

    effective_platform = os.name if platform_name is None else platform_name
    if effective_platform == "nt":
        local_base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        if not local_base.is_absolute():
            raise ValueError(f"LOCALAPPDATA must name an absolute path: {local_base}")
        app_data = local_base / APPLICATION_DIRECTORY_NAME
        program_root = local_base / "Programs" / APPLICATION_DIRECTORY_NAME
        return PlatformPolicy(
            system="windows",
            corpus_root=Path.home(),
            state_directory=app_data / "state",
            config_directory=app_data,
            data_directory=program_root,
            releases_directory=program_root / "releases",
            current_release=program_root / "current",
            models_directory=app_data / "models",
            runtimes_directory=program_root / "releases",
            stable_launcher=program_root / "bin" / "Neocortex.exe",
            user_alias=program_root / "bin" / "Neocortex.exe",
            desktop_file=program_root / "Neocortex.lnk",
            inventory_backend="ntfs-usn",
            identity_backend="windows-volume-file-id",
            containment_backend="windows-job-object",
            elevation="windows-administrator",
            mutation_backend="windows-handle-bound-ntfs",
            mutation_available=True,
            compatible=True,
        )

    config_root = linux_config_home() / APPLICATION_DIRECTORY_NAME
    state_root = linux_state_home() / APPLICATION_DIRECTORY_NAME / "state"
    data_root = linux_data_home() / APPLICATION_DIRECTORY_NAME
    return PlatformPolicy(
        system="linux",
        corpus_root=resolve_xdg_documents_directory() / "NeoCortex" / "Corpus",
        state_directory=state_root,
        config_directory=config_root,
        data_directory=data_root,
        releases_directory=data_root / "releases",
        current_release=data_root / "current",
        models_directory=data_root / "models",
        runtimes_directory=data_root / "runtimes",
        stable_launcher=data_root / "bin" / "Neocortex",
        user_alias=Path.home() / ".local" / "bin" / "Neocortex",
        desktop_file=linux_data_home() / "applications" / "neocortex.desktop",
        inventory_backend="portable-full-scan",
        identity_backend="posix-st_dev-st_ino",
        containment_backend="posix-session-process-group-rlimit",
        elevation="not-required",
        mutation_backend="intentionally-unavailable",
        mutation_available=False,
        compatible=True,
    )


def default_corpus_root() -> Path:
    configured = os.environ.get("NEOCORTEX_CORPUS_ROOT")
    if configured and os.name != "nt":
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"NEOCORTEX_CORPUS_ROOT must name an absolute path: {candidate}")
        return candidate
    return current_platform_policy().corpus_root


def stat_birthtime_ns(
    metadata: os.stat_result,
    *,
    platform_name: str | None = None,
) -> int:
    """Return real birth time or the explicit portable unavailable sentinel.

    Windows retains its historical ``ctime`` fallback for compatibility with
    existing NTFS state. POSIX never misrepresents inode-change time as birth.
    """

    birthtime = getattr(metadata, "st_birthtime_ns", None)
    if birthtime is not None:
        return int(birthtime)
    effective_platform = os.name if platform_name is None else platform_name
    if effective_platform == "nt":
        return int(metadata.st_ctime_ns)
    return UNAVAILABLE_BIRTHTIME_NS


def physical_identity_scheme_for_birthtime(birthtime_ns: int) -> str:
    """Name the identity scheme represented by a stored birth-time value.

    Existing Windows publications carry a non-negative NTFS creation time.
    Linux persists the explicit ``-1`` sentinel alongside ``st_dev`` and
    ``st_ino``; that tuple is still a resolved physical identity rather than a
    path-derived fallback.
    """

    if birthtime_ns == UNAVAILABLE_BIRTHTIME_NS:
        return POSIX_PHYSICAL_IDENTITY_SCHEME
    if birthtime_ns >= 0:
        return WINDOWS_PHYSICAL_IDENTITY_SCHEME
    raise ValueError("birthtime must be -1 or a non-negative integer")


def linux_mutation_requested(*, apply: bool, organization_apply: bool) -> bool:
    return os.name != "nt" and (apply or organization_apply)


def default_whisper_device() -> Literal["auto", "cpu", "cuda"]:
    return "cpu" if os.name != "nt" else "auto"


def default_whisper_compute_type() -> str:
    return "int8" if os.name != "nt" else "auto"


def default_whisper_model_cache() -> Path | None:
    if os.name == "nt":
        return None
    return current_platform_policy().models_directory / "whisper"


def default_local_models_only() -> bool:
    return os.name != "nt"


__all__ = [
    "APPLICATION_DIRECTORY_NAME",
    "LINUX_MUTATION_REASON",
    "PHYSICAL_IDENTITY_SCHEMES",
    "POSIX_PHYSICAL_IDENTITY_SCHEME",
    "UNAVAILABLE_BIRTHTIME_NS",
    "WINDOWS_PHYSICAL_IDENTITY_SCHEME",
    "PlatformPolicy",
    "current_platform_policy",
    "default_corpus_root",
    "default_local_models_only",
    "default_whisper_compute_type",
    "default_whisper_device",
    "default_whisper_model_cache",
    "linux_config_home",
    "linux_data_home",
    "linux_mutation_requested",
    "linux_state_home",
    "physical_identity_scheme_for_birthtime",
    "resolve_xdg_documents_directory",
    "stat_birthtime_ns",
]
