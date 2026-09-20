"""Selection failures are distinguished from missing optional test dependencies."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.capability_selection import (
    CAPABILITIES,
    CapabilitySelectionError,
    parse_selection,
    read_test_capabilities,
)


def test_explicit_selection_is_not_environment_auto_detection() -> None:
    assert parse_selection("all") == CAPABILITIES
    assert parse_selection("base, documents,image") == {"base", "documents", "image"}
    for invalid in ("", "all,base", "base,", "unknown", "Base"):
        with pytest.raises(CapabilitySelectionError, match="--capabilities"):
            parse_selection(invalid)


def test_declarations_are_literal_and_never_execute_test_source(tmp_path: Path) -> None:
    path = tmp_path / "test_optional.py"
    sentinel = tmp_path / "must-not-exist"
    path.write_text(
        "TEST_CAPABILITIES = ('documents', 'image')\n"
        "TEST_PLATFORMS = ('linux',)\n"
        f"open({str(sentinel)!r}, 'w').close()\n"
        "import missing_native_extension\n",
        encoding="utf-8",
    )
    declaration = read_test_capabilities(path)
    assert declaration.capabilities == {"documents", "image"}
    assert declaration.platforms == {"linux"}
    assert not sentinel.exists()
    path.write_text("TEST_CAPABILITIES = __import__('os').name\n", encoding="utf-8")
    with pytest.raises(CapabilitySelectionError, match="literal"):
        read_test_capabilities(path)


def _collection_repository(tmp_path: Path) -> Path:
    directory = tmp_path / "tests"
    directory.mkdir()
    source = Path(__file__).parent
    (directory / "__init__.py").write_text("", encoding="utf-8")
    for name in ("conftest.py", "audit_lab_guard.py", "capability_selection.py"):
        shutil.copyfile(source / name, directory / name)
    (directory / "test_base.py").write_text(
        "def test_base():\n    assert True\n", encoding="utf-8"
    )
    (directory / "test_optional.py").write_text(
        "TEST_CAPABILITIES = ('inference',)\n"
        "import neocortex_deliberately_unavailable_optional_dependency\n"
        "def test_inference():\n    assert False\n",
        encoding="utf-8",
    )
    other_platform = "win32" if sys.platform != "win32" else "linux"
    (directory / "test_foreign.py").write_text(
        f"TEST_PLATFORMS = ({other_platform!r},)\n"
        "TEST_CAPABILITIES = ('platform',)\n"
        "import neocortex_deliberately_unavailable_legacy_dependency\n",
        encoding="utf-8",
    )
    (directory / "test_mixed.py").write_text(
        "TEST_CAPABILITIES = ('base', 'image')\n"
        "import pytest\n"
        "def test_core():\n    assert True\n"
        "@pytest.mark.capability('image')\n"
        "def test_image():\n"
        "    import neocortex_deliberately_unavailable_image_dependency\n",
        encoding="utf-8",
    )
    return tmp_path


def _pytest(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *arguments],
        cwd=directory,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_base_excludes_optional_modules_before_import_and_mixed_tests_before_run(
    tmp_path: Path,
) -> None:
    result = _pytest(_collection_repository(tmp_path), "--capabilities=base")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed, 1 deselected" in result.stdout
    assert "excluded before import: 1 capability modules, 1 platform modules" in result.stdout
    assert "ModuleNotFoundError" not in result.stdout


def test_explicit_file_cannot_bypass_preimport_selection(tmp_path: Path) -> None:
    result = _pytest(
        _collection_repository(tmp_path),
        "--capabilities=base", "tests/test_optional.py", "tests/test_base.py",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "excluded before import: 1 capability modules" in result.stdout


def test_optional_test_overrides_shared_base_module_marker(tmp_path: Path) -> None:
    directory = _collection_repository(tmp_path)
    mixed = directory / "tests" / "test_mixed.py"
    with mixed.open("a", encoding="utf-8") as output:
        output.write("\npytestmark = pytest.mark.capability('base', 'image')\n")
    result = _pytest(directory, "--capabilities=base")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed, 1 deselected" in result.stdout


def test_selecting_unavailable_capability_preserves_collection_failure(tmp_path: Path) -> None:
    result = _pytest(_collection_repository(tmp_path), "--capabilities=inference")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "neocortex_deliberately_unavailable_optional_dependency" in result.stdout
    assert "skipped" not in result.stdout


def test_selecting_mixed_capability_preserves_runtime_failure(tmp_path: Path) -> None:
    result = _pytest(_collection_repository(tmp_path), "--capabilities=image")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "neocortex_deliberately_unavailable_image_dependency" in result.stdout
    assert "1 failed, 1 deselected" in result.stdout


def test_default_all_does_not_silently_select_only_available_tests(tmp_path: Path) -> None:
    result = _pytest(_collection_repository(tmp_path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "test capabilities=all" in result.stdout
    assert "neocortex_deliberately_unavailable_optional_dependency" in result.stdout
    assert "neocortex_deliberately_unavailable_legacy_dependency" not in result.stdout


def test_invalid_selection_is_an_actionable_usage_error(tmp_path: Path) -> None:
    result = _pytest(_collection_repository(tmp_path), "--capabilities=missing")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "--capabilities requires names from:" in result.stderr


def test_marker_must_be_declared_for_preimport_discovery(tmp_path: Path) -> None:
    directory = _collection_repository(tmp_path)
    (directory / "tests" / "test_undeclared.py").write_text(
        "import pytest\n"
        "@pytest.mark.capability('documents')\n"
        "def test_undeclared():\n    assert True\n",
        encoding="utf-8",
    )
    result = _pytest(directory, "--capabilities=base")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "capability marker is absent from TEST_CAPABILITIES" in result.stderr
