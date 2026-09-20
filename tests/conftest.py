"""Repository-wide pytest hooks with an opt-in audit containment contract."""
# region [00] Contexto del módulo
# Módulo: tests/conftest.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sys
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
from tests.capability_selection import (
    CapabilitySelectionError,
    parse_selection,
    read_test_capabilities,
    validate_capabilities,
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


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--capabilities",
        default="all",
        help=(
            "Collect explicit NeoCortex test capabilities before optional imports: "
            "all (default), or comma-separated base,documents,image,inference,ui,platform,agent. "
            "Selected missing dependencies are errors, not automatic skips."
        ),
    )


def pytest_configure(config: Any) -> None:
    """Fail before collection if an activated audit could write outside its root."""

    try:
        config._neocortex_test_capabilities = parse_selection(config.getoption("capabilities"))
    except CapabilitySelectionError as exc:
        raise pytest.UsageError(str(exc)) from exc
    config._neocortex_excluded_capability_modules = set()
    config._neocortex_excluded_platform_modules = set()
    config._neocortex_deselected_test_count = 0
    config.addinivalue_line(
        "markers", "capability(*names): explicit per-test capabilities in a collectable mixed module"
    )
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


def _exclude_module(config: Any, path: Path) -> bool:
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return False
    try:
        declaration = read_test_capabilities(path)
    except CapabilitySelectionError as exc:
        raise pytest.UsageError(str(exc)) from exc
    if declaration.platforms and sys.platform not in declaration.platforms:
        config._neocortex_excluded_platform_modules.add(path)
        return True
    if not declaration.capabilities & config._neocortex_test_capabilities:
        config._neocortex_excluded_capability_modules.add(path)
        return True
    return False


def pytest_ignore_collect(collection_path: Path, config: Any) -> bool | None:
    if collection_path.is_file() and _exclude_module(config, collection_path):
        return True
    return None


class _ExcludedCapabilityModule(pytest.Module):
    def collect(self) -> tuple[()]:
        return ()


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path: Path, parent: Any) -> Any:
    # pytest intentionally bypasses ignore_collect for a directly named file.
    # Return an empty collector rather than importing that excluded module.
    if _exclude_module(parent.config, module_path):
        return _ExcludedCapabilityModule.from_parent(parent, path=module_path)
    return None


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Keep real NTFS mutation fixtures opt-in to a contained laboratory."""

    selected, deselected = [], []
    for item in items:
        # A function's dependency boundary overrides a shared contract module's
        # labels; unioning both would accidentally pull optional tests into base.
        marker = item.get_closest_marker("capability")
        try:
            declared = read_test_capabilities(Path(str(item.path))).capabilities
            capabilities = (
                validate_capabilities(marker.args, label=f"{item.nodeid}: capability marker")
                if marker else frozenset({"base"}) if "base" in declared else declared
            )
            if not capabilities <= declared:
                raise CapabilitySelectionError(
                    f"{item.nodeid}: capability marker is absent from TEST_CAPABILITIES"
                )
        except CapabilitySelectionError as exc:
            raise pytest.UsageError(str(exc)) from exc
        if capabilities & config._neocortex_test_capabilities:
            selected.append(item)
        else:
            deselected.append(item)
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        config._neocortex_deselected_test_count += len(deselected)
def pytest_terminal_summary(terminalreporter: Any, config: Any) -> None:
    excluded = len(config._neocortex_excluded_capability_modules)
    foreign = len(config._neocortex_excluded_platform_modules)
    deselected = config._neocortex_deselected_test_count
    terminalreporter.write_line(
        f"NeoCortex test capabilities={config.getoption('capabilities')}; "
        f"excluded before import: {excluded} capability modules, {foreign} platform modules; "
        f"deselected: {deselected} tests"
    )


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
