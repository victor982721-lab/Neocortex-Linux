"""Artifact profiles preserve explicit exclusions and immutable source proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest

from tools.export_profile import (
    ACTIVE_DOCUMENTS, SOURCE_ONLY_DOCUMENTS, MANIFEST_NAME, ExportProfileError,
    documentation_paths, export_from_git, release_profile, validate_export_directory,
)
from tools.release_artifacts import ArtifactValidationError
from tests.test_release_artifacts import _write_wheel

REPO = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "https://github.com/example/synthetic-neocortex"


def _git(root: Path, *args: str) -> str:
    # Only this disposable synthetic repository is written by the tests.
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Export fixture", "-c", "user.email=export@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}, text=True,
    ).strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    # Actual source-only files are copied as fixture data, never fabricated
    # to compensate for missing files in a real checkout or sanitized export.
    for relative in ACTIVE_DOCUMENTS | SOURCE_ONLY_DOCUMENTS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    (root / "README.md").write_text(
        "# Fixture\n\n[Instructions](AGENTS.md#alcance-y-entrada)\n"
        "[Current](.codex/handoffs/CURRENT.md)\n"
        "[Architecture](docs/ARCHITECTURE.md)\n"
        "[definition]: AGENTS.md#contrato-com%C3%BAn\n"
        '<a href="AGENTS.md">Agent reference</a>\n'
        "```markdown\n[example](AGENTS.md)\n```\n",
        encoding="utf-8",
    )
    (root / "neocortex" / "fixture.py").write_text("VALUE = 1\n")
    (root / "tools").mkdir()
    (root / "tools" / "AGENTS.md").write_bytes((REPO / "tools" / "AGENTS.md").read_bytes())
    (root / "tests").mkdir()
    shutil.copyfile(REPO / "tests/test_documentation_contract.py", root / "tests/test_documentation_contract.py")
    (root / ".codex" / "rules").mkdir()
    (root / ".codex" / "rules" / "fixture.rules").write_text("# source-only control\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "Freeze synthetic profile source")
    return root


@pytest.mark.parametrize("profile", ("checkout", "sanitized"))
def test_declared_profiles_validate_their_own_documents_and_hashes(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str,
) -> None:
    destination = tmp_path / profile
    manifest = export_from_git(source, destination, profile=profile, repository_url=REPOSITORY_URL)
    commit = _git(source, "rev-parse", "HEAD")
    verified = validate_export_directory(destination, manifest, expected_profile=profile, expected_commit=commit)
    assert verified.source_commit == commit
    assert verified.tree_hash == _git(source, "rev-parse", "HEAD^{tree}")
    assert set(verified.required_paths) == set(verified.allowed_paths)
    assert len(SOURCE_ONLY_DOCUMENTS) == 29
    assert all((destination / path).is_file() for path in documentation_paths(profile))
    if profile == "sanitized":
        assert not any((destination / path).exists() for path in SOURCE_ONLY_DOCUMENTS)
        assert verified.excluded_paths["AGENTS.md"] == "source_only_document"
        assert verified.excluded_paths["tools/AGENTS.md"] == "source_only_agent_control"
        assert not (destination / ".codex").exists()
    else:
        assert not verified.excluded_paths
    # Exercise the actual documentation checker under the explicitly chosen
    # fixture profile, including local links and the transformed source links.
    from tests import test_documentation_contract as docs
    monkeypatch.setattr(docs, "_PROJECT_ROOT", destination)
    monkeypatch.setattr(docs, "_DOCUMENTS", documentation_paths(profile))
    docs.test_documentation_inventory_is_exactly_the_canonical_set()
    docs.test_local_documentation_links_and_anchors_resolve("README.md")


def test_sanitized_links_are_commit_pinned_recorded_and_deterministic(source: Path, tmp_path: Path) -> None:
    first = export_from_git(source, tmp_path / "one", profile="sanitized", repository_url=REPOSITORY_URL)
    second = export_from_git(source, tmp_path / "two", profile="sanitized", repository_url=REPOSITORY_URL)
    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text())
    text = (first.parent / "README.md").read_text()
    prefix = REPOSITORY_URL + "/blob/" + payload["source_commit"]
    assert f"{prefix}/AGENTS.md#alcance-y-entrada" in text
    assert "[Architecture](docs/ARCHITECTURE.md)" in text
    assert "[example](AGENTS.md)" in text  # Fenced example stays literal.
    edits = payload["transformations"]["README.md"]
    assert len(edits) == 4
    assert {edit["excluded_target"] for edit in edits} == {"AGENTS.md", ".codex/handoffs/CURRENT.md"}
    assert payload["files"]["README.md"]["source_sha256"] != payload["files"]["README.md"]["sha256"]


@pytest.mark.parametrize("tamper", ("remove_required", "extra_file", "restore_excluded", "weaken_required", "weaken_exclusions", "source_tree", "content", "transform", "executable_mode", "repository_url"))
def test_export_rejects_undeclared_omissions_and_tampering(source: Path, tmp_path: Path, tamper: str) -> None:
    manifest = export_from_git(source, tmp_path / "export", profile="sanitized", repository_url=REPOSITORY_URL)
    payload = json.loads(manifest.read_text())
    if tamper == "remove_required":
        (manifest.parent / "docs/KNOWLEDGE.md").unlink()
    elif tamper == "extra_file":
        (manifest.parent / "unexpected.txt").write_text("extra")
    elif tamper == "restore_excluded":
        (manifest.parent / "AGENTS.md").write_bytes((source / "AGENTS.md").read_bytes())
    elif tamper == "weaken_required":
        payload["required_paths"].remove("docs/KNOWLEDGE.md")
    elif tamper == "weaken_exclusions":
        payload["excluded_paths"]["AGENTS.md"] = "arbitrary omission"
    elif tamper == "source_tree":
        del payload["source_tree_entries"]["neocortex/fixture.py"]
    elif tamper == "content":
        (manifest.parent / "neocortex/fixture.py").write_text("VALUE = 2\n")
    elif tamper == "transform":
        payload["transformations"]["README.md"][0]["after"] = "https://example.invalid/forged"
    elif tamper == "repository_url":
        payload["repository_url"] = "http://example.invalid/forged"
    else:
        (manifest.parent / "neocortex/fixture.py").chmod(0o755)
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ExportProfileError):
        validate_export_directory(manifest.parent, manifest, expected_profile="sanitized")


def test_missing_agents_never_selects_sanitized_profile(source: Path, tmp_path: Path) -> None:
    manifest = export_from_git(source, tmp_path / "export", profile="checkout", repository_url=REPOSITORY_URL)
    (manifest.parent / "AGENTS.md").unlink()
    with pytest.raises(ExportProfileError):
        validate_export_directory(manifest.parent, manifest, expected_profile="checkout")
    with pytest.raises(ExportProfileError, match="explicit declaration"):
        validate_export_directory(manifest.parent, manifest, expected_profile="sanitized")


def test_export_uses_frozen_git_source_and_rejects_missing_source_document(source: Path, tmp_path: Path) -> None:
    (source / "README.md").write_text("Dirty uncommitted content\n")
    manifest = export_from_git(source, tmp_path / "frozen", profile="checkout", repository_url=REPOSITORY_URL)
    assert "Dirty" not in (manifest.parent / "README.md").read_text()
    _git(source, "rm", "docs/KNOWLEDGE.md")
    _git(source, "commit", "-q", "-m", "Synthetic omitted required document")
    with pytest.raises(ExportProfileError, match="required canonical documents"):
        export_from_git(source, tmp_path / "missing", profile="sanitized", repository_url=REPOSITORY_URL)
    assert not (tmp_path / "missing").exists()


def test_release_profile_uses_real_wheel_record_validation(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "neocortex_framework-0.7.2-py3-none-any.whl")
    manifest = release_profile(wheel, source_commit="a" * 40, tree_hash="b" * 40)
    assert manifest["name"] == "release" and manifest["record_verified"]
    assert manifest["allowed_paths"] == manifest["required_paths"]
    assert "neocortex/py.typed" in manifest["required_paths"]
    broken = _write_wheel(
        tmp_path / "bad" / wheel.name,
        record_transform=lambda value: value.replace(b"sha256=", b"sha256=x", 1),
    )
    with pytest.raises(ArtifactValidationError):
        release_profile(broken, source_commit="a" * 40, tree_hash="b" * 40)


def test_profile_names_and_source_pins_are_explicit(source: Path, tmp_path: Path) -> None:
    assert documentation_paths("checkout") == ACTIVE_DOCUMENTS | SOURCE_ONLY_DOCUMENTS
    with pytest.raises(ExportProfileError):
        documentation_paths("auto")
    manifest = export_from_git(source, tmp_path / "export", profile="sanitized", repository_url=REPOSITORY_URL)
    with pytest.raises(ExportProfileError, match="requested export"):
        validate_export_directory(manifest.parent, manifest, expected_profile="sanitized", expected_commit="a" * 40)
    assert manifest.name == MANIFEST_NAME


def test_documentation_module_validates_explicit_sanitized_manifest_before_use(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = export_from_git(source, tmp_path / "export", profile="sanitized", repository_url=REPOSITORY_URL)
    monkeypatch.setenv("NEOCORTEX_DOCUMENTATION_PROFILE", "sanitized")
    monkeypatch.setenv("NEOCORTEX_EXPORT_SOURCE_COMMIT", _git(source, "rev-parse", "HEAD"))
    namespace = runpy.run_path(str(manifest.parent / "tests/test_documentation_contract.py"))
    assert namespace["_DOCUMENTS"] == ACTIVE_DOCUMENTS
    namespace["test_documentation_inventory_is_exactly_the_canonical_set"]()
    namespace["test_local_documentation_links_and_anchors_resolve"]("README.md")
    (manifest.parent / "docs/KNOWLEDGE.md").unlink()
    with pytest.raises(ExportProfileError):
        runpy.run_path(str(manifest.parent / "tests/test_documentation_contract.py"))
