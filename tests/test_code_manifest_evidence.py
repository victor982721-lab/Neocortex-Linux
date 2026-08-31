"""Focused contract tests for bounded project-manifest evidence."""
# region [00] Contexto del módulo
# Módulo: tests/test_code_manifest_evidence.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.code.ingestion.code_analyzer_common import manifest_evidence
from neocortex.code.code_contracts import DiagnosticSeverity
# endregion [01]

# region [02] Implementación


def test_python_manifest_preserves_project_and_dependency_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    text = (
        "[project]\n"
        "name = 'neocortex-fixture'\n"
        "dependencies = ['requests>=2', 'typed-extra[fast] ~= 1.0', 7]\n"
    )

    hints, dependencies, diagnostics = manifest_evidence(path, text)

    assert diagnostics == ()
    assert [(hint.ecosystem, hint.name, hint.manifest_kind) for hint in hints] == [
        ("python", "neocortex-fixture", "pyproject")
    ]
    assert hints[0].root_hint == str(tmp_path)
    assert [
        (item.name, item.kind, item.scope, item.version_spec, item.evidence)
        for item in dependencies
    ] == [
        ("requests", "python", None, ">=2", "manifest:python"),
        ("typed-extra", "python", None, "[fast] ~= 1.0", "manifest:python"),
    ]
    assert all(
        item.source_range and item.source_range.start_byte == 0 for item in dependencies
    )
    assert all(
        item.source_range and item.source_range.end_byte == len(text.encode("utf-8"))
        for item in dependencies
    )


def test_cargo_manifest_preserves_scopes_and_workspace_metadata(tmp_path: Path) -> None:
    path = tmp_path / "Cargo.toml"
    text = (
        "[package]\nname = 'motor'\n"
        "[workspace]\nmembers = ['core']\n"
        "[dependencies]\nserde = '1'\nlocal = { path = '../local' }\n"
        "[dev-dependencies]\nproptest = { version = '2', features = ['std'] }\n"
        "[build-dependencies]\ncc = '1.1'\n"
    )

    hints, dependencies, diagnostics = manifest_evidence(path, text)

    assert diagnostics == ()
    assert [(hint.ecosystem, hint.name, hint.manifest_kind) for hint in hints] == [
        ("rust", "motor", "cargo")
    ]
    assert hints[0].metadata == {"workspace": True}
    assert [
        (item.name, item.kind, item.scope, item.version_spec, item.evidence)
        for item in dependencies
    ] == [
        ("serde", "cargo", "runtime", "1", "manifest:cargo"),
        ("local", "cargo", "runtime", None, "manifest:cargo"),
        ("proptest", "cargo", "development", "2", "manifest:cargo"),
        ("cc", "cargo", "build", "1.1", "manifest:cargo"),
    ]


def test_node_manifest_preserves_runtime_and_development_dependencies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "package.json"
    text = (
        '{"name":"ui-console","dependencies":{"react":"19"},'
        '"devDependencies":{"typescript":"^6"}}'
    )

    hints, dependencies, diagnostics = manifest_evidence(path, text)

    assert diagnostics == ()
    assert [(hint.ecosystem, hint.name, hint.manifest_kind) for hint in hints] == [
        ("javascript", "ui-console", "package")
    ]
    assert [
        (item.name, item.kind, item.scope, item.version_spec, item.evidence)
        for item in dependencies
    ] == [
        ("react", "package.json", None, "19", "manifest:package.json"),
        ("typescript", "package.json", None, "^6", "manifest:package.json"),
    ]


@pytest.mark.parametrize(
    ("name", "text", "tool"),
    (
        ("pyproject.toml", "[project\nname = 'broken'", "tomllib"),
        ("package.json", '{"name": "broken",}', "json"),
    ),
)
def test_invalid_manifest_configuration_retains_versioned_diagnostic(
    tmp_path: Path,
    name: str,
    text: str,
    tool: str,
) -> None:
    hints, dependencies, diagnostics = manifest_evidence(tmp_path / name, text)

    assert hints == ()
    assert dependencies == ()
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.source == "manifest-parser"
    assert diagnostic.code == "manifest_parse_error"
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert diagnostic.message
    assert diagnostic.tool_name == tool
    assert diagnostic.tool_version == "stdlib"
    assert diagnostic.confirmed is True
    assert diagnostic.confidence == 1.0
# endregion [02]
