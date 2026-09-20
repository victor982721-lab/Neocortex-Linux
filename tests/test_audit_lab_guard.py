"""Regression coverage for fail-closed audit process containment."""
# region [00] Contexto del módulo
# Módulo: tests/test_audit_lab_guard.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from pathlib import Path

import pytest

from tests.audit_lab_guard import (
    AuditLabContractError,
    capture_audit_lab_directory_identity,
    require_unchanged_audit_lab_directory,
    validate_audit_lab_environment,
    validate_pytest_artifact_paths,
)
# endregion [01]

# region [02] Implementación


def _environment(root: Path, temp: Path, pycache: Path) -> dict[str, str]:
    return {
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
        "PYTHONPYCACHEPREFIX": str(pycache),
        "COVERAGE_FILE": str(root / "coverage" / ".coverage"),
    }


def test_valid_audit_environment_keeps_every_destination_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    temp = root / "tmp"
    pycache = root / "pycache"
    temp.mkdir()
    pycache.mkdir()

    assert (
        validate_audit_lab_environment(
            str(root),
            environment=_environment(root, temp, pycache),
            selected_temp_directory=str(temp),
        )
        == root
    )
    validate_pytest_artifact_paths(
        root,
        base_temp=root / "pytest" / "run",
        cache_directory=root / "pytest" / "cache",
    )


def test_missing_temp_directory_cannot_fall_back_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    root.mkdir()
    pycache = root / "pycache"
    pycache.mkdir()
    missing = root / "missing-temp"

    with pytest.raises(AuditLabContractError, match="TEMP does not exist"):
        validate_audit_lab_environment(
            str(root),
            environment=_environment(root, missing, pycache),
            selected_temp_directory=str(tmp_path),
        )


def test_guard_rejects_directory_replaced_at_the_same_path(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    replacement = tmp_path / "replacement"
    retained = tmp_path / "retained-original"
    watched.mkdir()
    replacement.mkdir()
    identity = capture_audit_lab_directory_identity(watched, label="watched")

    watched.rename(retained)
    replacement.rename(watched)

    with pytest.raises(AuditLabContractError, match="changed physical identity"):
        require_unchanged_audit_lab_directory(identity, label="watched")


@pytest.mark.parametrize("key", ("TEMP", "TMP", "TMPDIR", "PYTHONPYCACHEPREFIX"))
def test_process_destination_outside_root_is_rejected(
    tmp_path: Path,
    key: str,
) -> None:
    root = tmp_path / "lab"
    outside = tmp_path / "outside"
    temp = root / "tmp"
    pycache = root / "pycache"
    for directory in (root, outside, temp, pycache):
        directory.mkdir(exist_ok=True)
    environment = _environment(root, temp, pycache)
    environment[key] = str(outside)

    with pytest.raises(AuditLabContractError, match="escapes audit root"):
        validate_audit_lab_environment(
            str(root),
            environment=environment,
            selected_temp_directory=str(temp),
        )


@pytest.mark.parametrize("label", ("basetemp", "cache"))
def test_pytest_artifact_destination_outside_root_is_rejected(
    tmp_path: Path,
    label: str,
) -> None:
    root = tmp_path / "lab"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    values = {
        "base_temp": root / "pytest" / "run",
        "cache_directory": root / "pytest" / "cache",
    }
    values["base_temp" if label == "basetemp" else "cache_directory"] = outside

    with pytest.raises(AuditLabContractError, match="escapes audit root"):
        validate_pytest_artifact_paths(root, **values)


@pytest.mark.parametrize("label", ("basetemp", "cache"))
def test_pytest_artifact_destination_equal_to_root_is_rejected(
    tmp_path: Path,
    label: str,
) -> None:
    root = (tmp_path / "lab").resolve()
    root.mkdir()
    values = {
        "base_temp": root / "pytest" / "run",
        "cache_directory": root / "pytest" / "cache",
    }
    values["base_temp" if label == "basetemp" else "cache_directory"] = root

    with pytest.raises(AuditLabContractError, match="strict descendant"):
        validate_pytest_artifact_paths(root, **values)


def test_pytest_artifact_descendants_are_accepted(tmp_path: Path) -> None:
    root = (tmp_path / "lab").resolve()
    root.mkdir()

    validate_pytest_artifact_paths(
        root,
        base_temp=root / "pytest" / "run",
        cache_directory=root / "pytest" / "cache",
    )


@pytest.mark.parametrize("raw", ("relative", r"\\server\share\lab"))
def test_audit_root_must_be_absolute_local(tmp_path: Path, raw: str) -> None:
    del tmp_path
    with pytest.raises(AuditLabContractError, match="absolute local path"):
        validate_audit_lab_environment(raw, environment={}, selected_temp_directory=raw)


# endregion [02]
