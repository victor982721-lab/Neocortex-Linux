"""Stable per-user application paths shared by CLI and desktop frontends."""

from __future__ import annotations

import os
from pathlib import Path

from neocortex.platform.policy import current_platform_policy


# region [01] Per-user paths

APPLICATION_DIRECTORY_NAME = "Neocortex"
EPS_FRAMEWORK_DIRECTORY_NAME = "Generador de bitácoras EPS"


def local_application_data_directory() -> Path:
    """Return the conventional local, non-roaming application directory."""

    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        base = Path(configured)
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Local"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    if not base.is_absolute():
        raise ValueError(f"local application data path must be absolute: {base}")
    return base / APPLICATION_DIRECTORY_NAME


def default_state_directory() -> Path:
    """Return the fixed durable state location for normal application use."""

    if os.name != "nt" and "LOCALAPPDATA" not in os.environ:
        return current_platform_policy().state_directory
    return local_application_data_directory() / "state"


def default_code_project_roots() -> tuple[Path, ...]:
    """Return the exact personal project allowlist used by integrated Code."""

    return (
        source_repository_directory(),
        Path.home() / "Frameworks" / EPS_FRAMEWORK_DIRECTORY_NAME,
    )


def default_generated_artifact_directories() -> tuple[Path, ...]:
    """Return project build trees that are never corpus source material."""

    projects = (
        source_repository_directory(),
        Path.home() / "Frameworks" / EPS_FRAMEWORK_DIRECTORY_NAME,
        Path.home() / "OneDrive" / "Documentos" / "Bitácoras EPS",
    )
    return tuple(
        project / directory for project in projects for directory in ("build", "dist", "wheelhouse")
    )


def default_ui_settings_path() -> Path:
    if os.name != "nt" and "LOCALAPPDATA" not in os.environ:
        return current_platform_policy().config_directory / "ui.ini"
    return local_application_data_directory() / "ui.ini"


def source_repository_directory() -> Path:
    """Return the canonical editable source repository for this user."""

    return Path.home() / APPLICATION_DIRECTORY_NAME / "Repository"


def program_installation_directory() -> Path:
    """Return the per-user root for immutable, versioned installations."""

    if os.name != "nt" and "LOCALAPPDATA" not in os.environ:
        return current_platform_policy().data_directory
    return local_application_data_directory().parent / "Programs" / APPLICATION_DIRECTORY_NAME


def self_analysis_data_directory() -> Path:
    """Return the non-production root for self-analysis state and artifacts."""

    return local_application_data_directory() / "self-analysis"


def stable_launcher_path() -> Path:
    """Return the stable per-user launcher path outside version directories."""

    if os.name != "nt" and "LOCALAPPDATA" not in os.environ:
        return current_platform_policy().stable_launcher
    return program_installation_directory() / "bin" / "Neocortex.exe"


# endregion [01]
