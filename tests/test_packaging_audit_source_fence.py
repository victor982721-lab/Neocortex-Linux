"""Release source identity must survive races in the mutable checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from neocortex.platform.policy import current_platform_policy
from tools import release_linux


def _git(source: Path, *arguments: str) -> str:
    return subprocess.run(
        (
            "git", "-C", os.fspath(source),
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false",
            "-c", "user.name=Packaging Fixture",
            "-c", "user.email=fixture@example.invalid",
            *arguments,
        ),
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout


@pytest.fixture
def release_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "checkout"
    (source / "neocortex").mkdir(parents=True)
    (source / "neocortex/__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "constraints.txt").write_text("setuptools==83.0.0\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (source / "module.link").symlink_to("neocortex/__init__.py")
    _git(source, "init", "--template=")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    sha = release_linux._source_sha(source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = release_linux.LinuxReleaseLayout(source, current_platform_policy())
    build_effects: list[str] = []

    def create_environment(root, *_args, **_kwargs):
        build_effects.append("create_environment")
        (root / "bin").mkdir(parents=True)

    def runner(arguments, **kwargs):
        command = tuple(os.fspath(item) for item in arguments)
        if command[0] == "git":
            return release_linux._run(arguments, **kwargs)
        if "--outdir" in command:
            build_effects.append("build")
            output = Path(command[command.index("--outdir") + 1])
            (output / "neocortex_framework-fixture-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_linux, "_create_pip_environment", create_environment)

    def build():
        return release_linux._build_wheel(
            layout, workspace, source_sha=sha,
            pip_wheel=tmp_path / "pip.whl", runner=runner,
        )

    return source, workspace, sha, build_effects, build


@pytest.mark.parametrize("change", ("content", "execute_mode", "file_kind", "link_target"))
def test_release_rejects_owner_changed_after_source_sha(release_source, change: str) -> None:
    source, _workspace, _sha, effects, build = release_source
    owner = source / "neocortex/__init__.py"
    if change == "content":
        owner.write_text("VALUE = 2\n", encoding="utf-8")
    elif change == "execute_mode":
        owner.chmod(0o755)
    elif change == "file_kind":
        owner.unlink()
        owner.symlink_to("../constraints.txt")
    else:
        link = source / "module.link"
        link.unlink()
        link.symlink_to("constraints.txt")

    with pytest.raises(release_linux.LinuxReleaseError, match="differs from source commit"):
        build()
    assert effects == []


def test_release_detects_edit_copied_and_reverted_before_status_check(
    release_source, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, workspace, _sha, effects, build = release_source
    owner = source / "neocortex/__init__.py"
    original = owner.read_bytes()
    stage = release_linux.stage_tracked_source

    def race(source_root, destination, relative_paths):
        owner.write_bytes(b"VALUE = 'transient edit'\n")
        try:
            return stage(source_root, destination, relative_paths)
        finally:
            owner.write_bytes(original)

    monkeypatch.setattr(release_linux, "stage_tracked_source", race)
    with pytest.raises(release_linux.LinuxReleaseError, match="differs from source commit"):
        build()
    assert _git(source, "status", "--porcelain") == ""
    assert (workspace / "source/neocortex/__init__.py").read_bytes() != original
    assert effects == []


def test_release_uses_captured_commit_paths_not_mutable_index(release_source) -> None:
    source, workspace, _sha, effects, build = release_source
    _git(source, "rm", "--cached", "constraints.txt")
    (source / "untracked.py").write_text("NOT_COMMITTED = True\n", encoding="utf-8")

    assert build().read_bytes() == b"wheel"
    assert (workspace / "source/constraints.txt").is_file()
    assert not (workspace / "source/untracked.py").exists()
    assert (workspace / "source/module.link").is_symlink()
    assert effects == ["create_environment", "build"]


def test_release_rejects_clean_new_commit_that_differs_from_captured_sha(release_source) -> None:
    source, _workspace, sha, effects, build = release_source
    (source / "neocortex/__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(source, "add", "neocortex/__init__.py")
    _git(source, "commit", "-m", "concurrent edit")
    assert _git(source, "status", "--porcelain") == ""
    assert _git(source, "rev-parse", "HEAD").strip() != sha

    with pytest.raises(release_linux.LinuxReleaseError, match="differs from source commit"):
        build()
    assert effects == []


@pytest.mark.parametrize(
    "tree",
    (
        "100644 blob " + "a" * 40 + "\t../outside\0",
        "160000 commit " + "a" * 40 + "\tsubmodule\0",
        "100644 blob " + "a" * 40 + "\towner\0" + "100644 blob " + "a" * 40 + "\towner\0",
        "100644 blob invalid\towner\0",
        "100644 blob " + "a" * 40 + "\towner",
    ),
)
def test_release_rejects_unsupported_commit_tree(tmp_path: Path, tree: str) -> None:
    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, tree, "")

    with pytest.raises(release_linux.LinuxReleaseError):
        release_linux._source_commit_blobs(tmp_path, "b" * 40, runner)
