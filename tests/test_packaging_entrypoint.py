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

import neocortex
from neocortex.interface.entrypoint import entrypoint
from neocortex.deduplication.__main__ import main as legacy_dedup_main
# endregion [01]

# region [02] Implementación


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
    assert metadata["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "neocortex.__version__"}
    assert neocortex.__version__ == "0.9.0"


def test_source_manifest_excludes_release_internal_material() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest_lines = {
        line.strip()
        for line in (project_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    }

    assert "include AGENTS.md" not in manifest_lines
    assert "include NeoCortex_AGENTS.md" not in manifest_lines
    assert "exclude AGENTS.md" in manifest_lines
    assert "exclude NeoCortex_AGENTS.md" in manifest_lines
    assert "prune tests" in manifest_lines
    assert "recursive-include tests *.py" not in manifest_lines
    assert "recursive-include tests/fixtures/knowledge *.json" not in manifest_lines
    assert "include docs/KNOWLEDGE_EVOLUTION_*.md" not in manifest_lines
    assert "include docs/TECHNICAL_AUDIT_*.md" not in manifest_lines
    assert "include docs/TECHNICAL_EVOLUTION_*.md" not in manifest_lines
    assert "recursive-exclude docs KNOWLEDGE_EVOLUTION_*.md" in manifest_lines
    assert "recursive-exclude docs TECHNICAL_AUDIT_*.md" in manifest_lines
    assert "recursive-exclude docs TECHNICAL_EVOLUTION_*.md" in manifest_lines


def test_sdist_manifest_includes_active_docs_and_release_tools() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest_lines = {
        line.strip()
        for line in (project_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    }

    assert "include docs/SELF_ANALYSIS.md" in manifest_lines
    assert "include constraints-linux-cp314.lock" in manifest_lines
    assert "include tools/__init__.py" in manifest_lines
    assert "include tools/pyright_runtime.py" in manifest_lines
    assert "include tools/pyright_runtime_lock/package-lock.json" in manifest_lines
    assert "include tools/pyright_runtime_lock/package.json" in manifest_lines
    assert "recursive-include tools release_*.py" in manifest_lines

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


def test_installed_entrypoint_exposes_owned_pyright_shim_and_node(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prefix = tmp_path / "venv"
    owned_bin = prefix / "tools" / "pyright" / "node_modules" / ".bin"
    owned_node = tmp_path / "tools" / "node"
    owned_bin.mkdir(parents=True)
    owned_node.mkdir(parents=True)
    original_path = os.pathsep.join(("C:/system/bin", "C:/other/bin"))
    monkeypatch.setenv("PATH", original_path)
    monkeypatch.setattr("neocortex.interface.entrypoint.sys.prefix", str(prefix))

    with patch("neocortex.api.cli.cli_app.main", return_value=0):
        assert entrypoint(("--version",)) == 0
        assert entrypoint(("--version",)) == 0

    assert os.environ["PATH"].split(os.pathsep) == [
        str(owned_bin),
        str(owned_node),
        *original_path.split(os.pathsep),
    ]


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


def test_legacy_dedup_entrypoint_delegates_without_legacy_state(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "state" / "dedup.sqlite3"
    with patch("neocortex.api.cli.cli_app.main", return_value=0) as run_cli:
        result = legacy_dedup_main(
            (
                "--root",
                str(tmp_path),
                "--state-database",
                str(database),
                "--show-groups",
                "3",
            )
        )

    assert result == 0
    forwarded = run_cli.call_args.args[0]
    assert tuple(forwarded) == (
        "--root",
        str(tmp_path),
        "--state-directory",
        str(database.parent),
        "--show-groups",
        "3",
    )
    assert "obsoleto" in capsys.readouterr().err


# endregion [02]
