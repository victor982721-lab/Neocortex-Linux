"""Repository-wide pytest hooks with an opt-in audit containment contract."""
# region [00] Contexto del módulo
# Módulo: tests/conftest.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.audit_lab_guard import (
    AUDIT_LAB_ENVIRONMENT,
    AuditLabDirectoryIdentity,
    capture_audit_lab_directory_identity,
    require_unchanged_audit_lab_directory,
    validate_audit_lab_environment,
    validate_pytest_artifact_paths,
)
# endregion [01]

# region [02] Implementación


def _guarded_directory_identities(
    root: Path,
) -> tuple[tuple[str, AuditLabDirectoryIdentity], ...]:
    destinations: list[tuple[str, Path]] = [("audit laboratory", root)]
    destinations.extend((key, Path(os.environ[key])) for key in ("TEMP", "TMP", "TMPDIR"))
    destinations.append(("PYTHONPYCACHEPREFIX", Path(os.environ["PYTHONPYCACHEPREFIX"])))
    coverage_file = os.environ.get("COVERAGE_FILE")
    if coverage_file:
        destinations.append(("COVERAGE_FILE parent", Path(coverage_file).parent))
    return tuple(
        (label, capture_audit_lab_directory_identity(path, label=label))
        for label, path in destinations
    )


def pytest_configure(config: Any) -> None:
    """Fail before collection if an activated audit could write outside its root."""

    root_raw = os.environ.get(AUDIT_LAB_ENVIRONMENT)
    if root_raw is None:
        return
    root = validate_audit_lab_environment(root_raw)
    validate_pytest_artifact_paths(
        root,
        base_temp=config.option.basetemp,
        cache_directory=config.getini("cache_dir"),
    )
    config._neocortex_audit_lab_root = root
    config._neocortex_audit_lab_identities = _guarded_directory_identities(root)


def _windows_audit_lab_required() -> bool:
    return os.name == "nt" and not os.environ.get(AUDIT_LAB_ENVIRONMENT)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Keep real NTFS mutation fixtures opt-in to a contained laboratory."""

    if not _windows_audit_lab_required():
        return
    marker = pytest.mark.skip(reason="Windows NTFS contract requires an activated audit laboratory")
    for item in items:
        if Path(str(item.path)).name == "test_release_windows_ntfs.py":
            item.add_marker(marker)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Revalidate process destinations after the last test releases fixtures."""

    root = getattr(session.config, "_neocortex_audit_lab_root", None)
    if not isinstance(root, Path):
        return
    identities = getattr(session.config, "_neocortex_audit_lab_identities", ())
    for label, identity in identities:
        require_unchanged_audit_lab_directory(identity, label=label)
    validate_audit_lab_environment(str(root))
    validate_pytest_artifact_paths(
        root,
        base_temp=session.config.option.basetemp,
        cache_directory=session.config.getini("cache_dir"),
    )


# endregion [02]
