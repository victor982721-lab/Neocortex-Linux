"""Contract checks for the public hygiene/lifecycle documentation.

These tests intentionally inspect the repository docs and the public facade
only.  They do not run a cleanup, inspect HOME/corpus, or treat documentation
as authorization for an effect.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from neocortex.api.cli.cli_parser import build_parser


ROOT = Path(__file__).resolve().parents[1]


def _docs() -> str:
    paths = (
        ROOT / "README.md",
        ROOT / "docs" / "CLI.md",
        ROOT / "docs" / "OPERATIONS.md",
        ROOT / "docs" / "CHANGELOG.md",
    )
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_hygiene_docs_keep_the_physical_effect_boundary_explicit() -> None:
    text = _docs()
    assert "zero deletion" in text.lower()
    assert "AgentActivity" in text
    assert "neocortex.api.agent_activity" in text
    assert "HOME" in text and ".codex" in text
    assert "`hygiene` como autorización física" in text
    assert "owner=\"actividad\"" not in text
    assert "registry = ArtifactRegistry(state /" not in text
    assert "workspace.complete((workspace.path" not in text
    assert "0.14.0-a02f6761ece2" not in text


def test_cli_docs_matrix_covers_every_continuity_criterion() -> None:
    text = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    for criterion in range(1, 13):
        assert f"C{criterion:02d}" in text
    assert "matriz compacta de aceptación del circuito" in text.lower()


def test_installed_public_activity_facade_is_named_in_the_package() -> None:
    module = importlib.import_module("neocortex.api.agent_activity")
    assert hasattr(module, "AgentActivity")
    for method in ("prepare", "resume", "run", "publish", "close", "retire", "reconcile"):
        assert hasattr(module.AgentActivity, method)


def test_help_retains_the_real_read_only_hygiene_and_maintenance_flags() -> None:
    help_text = build_parser().format_help()
    for token in (
        "--hygiene-preview",
        "--hygiene-json",
        "--maintenance-json",
        "--maintenance-audit-root",
    ):
        assert token in help_text
