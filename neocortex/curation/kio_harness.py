"""Declarative fixture harness for the future KDE/KIO integration gate.

The production KIO adapter remains intentionally unpromoted.  This module
only describes a disposable fixture, its expected command and verification
inputs; it has no subprocess runner and refuses any request to execute a real
desktop client.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


KIO_DESKTOP_HARNESS_SCHEMA = "neocortex.kio-desktop-harness/v1"
KIO_DESKTOP_HARNESS_CLIENTS = ("kioclient6", "kioclient5", "kioclient")
KIO_DESKTOP_HARNESS_TRASH_URL = "trash:/"
KIO_DESKTOP_HARNESS_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


class KioDesktopHarnessError(ValueError):
    """A fixture spec is unsafe or requests real desktop execution."""


def _fixture_path(value: str | Path, *, root: Path, label: str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise KioDesktopHarnessError(f"{label} escapes the fixture root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise KioDesktopHarnessError(f"{label} has an unsafe fixture path")
    if path.resolve(strict=False) != path:
        raise KioDesktopHarnessError(f"{label} traverses a symbolic link")
    return path


@dataclass(frozen=True, slots=True)
class KioDesktopHarnessSpec:
    """Fixture-only KIO contract with no capability to spawn a process."""

    fixture_root: Path
    source: Path
    trash_root: Path
    config_home: Path
    client_path: Path
    client_name: str = "kioclient5"
    timeout_seconds: float = 120.0
    execution: Literal["fixture-only"] = "fixture-only"

    def __post_init__(self) -> None:
        root = Path(os.path.abspath(os.fspath(self.fixture_root)))
        if root.resolve(strict=False) != root:
            raise KioDesktopHarnessError("fixture_root traverses a symbolic link")
        try:
            root.relative_to(KIO_DESKTOP_HARNESS_TEMP_ROOT)
        except ValueError as exc:
            raise KioDesktopHarnessError(
                "KIO desktop harness is limited to temporary fixtures"
            ) from exc
        if self.execution != "fixture-only":
            raise KioDesktopHarnessError("real KIO execution is not available in this harness")
        if self.client_name not in KIO_DESKTOP_HARNESS_CLIENTS:
            raise KioDesktopHarnessError("client_name is not an allowed KIO fixture client")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1.0 <= float(self.timeout_seconds) <= 300.0
        ):
            raise KioDesktopHarnessError("timeout_seconds must be between 1 and 300 seconds")
        object.__setattr__(self, "fixture_root", root)
        object.__setattr__(self, "source", _fixture_path(self.source, root=root, label="source"))
        object.__setattr__(
            self,
            "trash_root",
            _fixture_path(self.trash_root, root=root, label="trash_root"),
        )
        object.__setattr__(
            self,
            "config_home",
            _fixture_path(self.config_home, root=root, label="config_home"),
        )
        object.__setattr__(
            self,
            "client_path",
            _fixture_path(self.client_path, root=root, label="client_path"),
        )
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    @property
    def command(self) -> tuple[str, str, str, str]:
        """Return the argv expected by an injected fixture runner."""

        return (str(self.client_path), "move", str(self.source), KIO_DESKTOP_HARNESS_TRASH_URL)

    @property
    def environment(self) -> dict[str, str]:
        """Return the isolated environment fragment for an injected runner."""

        return {"XDG_CONFIG_HOME": str(self.config_home)}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": KIO_DESKTOP_HARNESS_SCHEMA,
            "schema_version": 1,
            "execution": self.execution,
            "client_name": self.client_name,
            "client_path": str(self.client_path),
            "config_home": str(self.config_home),
            "fixture_root": str(self.fixture_root),
            "source": str(self.source),
            "trash_root": str(self.trash_root),
            "trash_url": KIO_DESKTOP_HARNESS_TRASH_URL,
            "timeout_seconds": self.timeout_seconds,
            "real_execution": False,
            "runner": "injected-only",
            "required_observations": (
                "source_absent",
                "trash_entry_identity",
                "trash_info_source",
                "receipt",
            ),
        }


__all__ = (
    "KIO_DESKTOP_HARNESS_CLIENTS",
    "KIO_DESKTOP_HARNESS_SCHEMA",
    "KIO_DESKTOP_HARNESS_TRASH_URL",
    "KioDesktopHarnessError",
    "KioDesktopHarnessSpec",
)
