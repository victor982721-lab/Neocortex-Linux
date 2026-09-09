# region [00] Contexto del módulo
# Módulo: tests/test_packaging_entrypoint.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import neocortex
from neocortex.interface.entrypoint import entrypoint
# endregion [01]

# region [02] Implementación


_SDIST_MARKDOWN_ALLOWLIST = {
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/CHANGELOG.md",
    "docs/CLI.md",
    "docs/FILE_INTELLIGENCE_AND_CURATION.md",
    "docs/KNOWLEDGE.md",
    "docs/LINUX_KUBUNTU.md",
    "docs/OPERATIONS.md",
    "docs/PERSISTENCE.md",
    "docs/RECOVERY.md",
    "docs/ROADMAP_90_DAYS.md",
    "docs/SECURITY.md",
}


def test_project_metadata_uses_package_version_and_installed_command() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)

    assert metadata["project"]["dynamic"] == ["version"]
    assert metadata["project"]["description"] == (
        "Linux-first incremental personal content framework"
    )
    assert metadata["project"]["requires-python"] == ">=3.13,<3.15"
    assert "Operating System :: POSIX :: Linux" in metadata["project"]["classifiers"]
    assert not any("Windows" in classifier for classifier in metadata["project"]["classifiers"])
    assert metadata["project"]["scripts"]["Neocortex"] == ("neocortex.interface.entrypoint:entrypoint")
    repository = "https://github.com/victor982721-lab/Neocortex-Linux"
    assert metadata["project"]["urls"] == {
        "Homepage": repository,
        "Repository": f"{repository}.git",
        "Issues": f"{repository}/issues",
    }
    assert metadata["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "neocortex.__version__"}
    assert neocortex.__version__ == "0.13.0"


def test_linux_release_direct_pins_satisfy_current_product_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        *metadata["project"]["dependencies"],
        *metadata["project"]["optional-dependencies"]["full"],
        *metadata["build-system"]["requires"],
    ]
    for filename in ("constraints.txt", "constraints-linux-cp314.lock"):
        pins = {}
        for line in (root / filename).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            requirement = Requirement(line.split(" --hash=", 1)[0])
            pin = tuple(requirement.specifier)
            assert len(pin) == 1 and pin[0].operator == "=="
            name = canonicalize_name(requirement.name)
            assert name not in pins
            pins[name] = pin[0].version
        for raw in requirements:
            requirement = Requirement(raw)
            assert pins[canonicalize_name(requirement.name)] in requirement.specifier


def test_source_manifest_excludes_release_internal_material() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest_lines = tuple(
        line.strip()
        for line in (project_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )

    assert "include AGENTS.md" not in manifest_lines
    assert "exclude AGENTS.md" in manifest_lines
    assert "prune tests" in manifest_lines
    assert "recursive-include tests *.py" not in manifest_lines
    assert "recursive-include tests/fixtures/knowledge *.json" not in manifest_lines
    assert "recursive-include docs *.md" not in manifest_lines
    assert "recursive-include neocortex README.md" not in manifest_lines


def test_sdist_manifest_includes_active_docs_and_release_tools() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest_lines = {
        line.strip()
        for line in (project_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    }

    markdown_includes = {
        line.removeprefix("include ")
        for line in manifest_lines
        if line.startswith("include ") and line.endswith(".md")
    }
    assert markdown_includes == _SDIST_MARKDOWN_ALLOWLIST
    assert "include constraints-linux-cp314.lock" in manifest_lines
    assert "include tools/__init__.py" in manifest_lines
    assert "include tools/release_archive_safety.py" in manifest_lines
    assert "include tools/release_artifacts.py" in manifest_lines
    assert "include tools/release_linux.py" in manifest_lines
    assert "recursive-include tools release_*.py" not in manifest_lines

    with (project_root / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    wheel_package_patterns = metadata["tool"]["setuptools"]["packages"]["find"]["include"]
    assert all(not str(pattern).startswith(("tests", "docs")) for pattern in wheel_package_patterns)


def test_svg_release_asset_has_canonical_lf_export() -> None:
    project_root = Path(__file__).resolve().parents[1]
    attributes = (project_root / ".gitattributes").read_text(encoding="utf-8")

    assert (
        "neocortex/interface/presentation/assets/neocortex-app-icon.svg text eol=lf"
        in attributes.splitlines()
    )


def test_installed_entrypoint_forwards_arguments_to_integrated_cli() -> None:
    with (
        patch("neocortex.interface.entrypoint._run_special_mode", return_value=None),
        patch("neocortex.api.cli.cli_app.main", return_value=7) as run_cli,
    ):
        result = entrypoint(("--status",))

    assert result == 7
    run_cli.assert_called_once_with(["--status"])


def test_interface_package_does_not_import_optional_qt_stack_eagerly() -> None:
    project_root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        (
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {os.fspath(project_root)!r}); "
                "import neocortex.interface; "
                "assert not any(name == 'PySide6' or name.startswith('PySide6.') "
                "for name in sys.modules)"
            ),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert probe.returncode == 0, probe.stderr


def test_interface_package_entrypoint_preserves_forwarding_contract() -> None:
    import neocortex.interface as interface

    with patch("neocortex.interface.application.app.main", return_value=11) as run_application:
        assert interface.main(("--portable",)) == 11

    run_application.assert_called_once_with(("--portable",))



# endregion [02]
