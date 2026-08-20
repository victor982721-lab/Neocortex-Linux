"""Atomic Linux release activation, receipts, and rollback regressions."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.platform_policy import PlatformPolicy
from tools import release_linux
from tools.release_linux import LinuxReleaseLayout


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux release contract")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _policy(tmp_path: Path) -> PlatformPolicy:
    data = tmp_path / "data" / "Neocortex"
    return PlatformPolicy(
        system="linux",
        corpus_root=tmp_path / "Documentos" / "NeoCortex" / "Corpus",
        state_directory=tmp_path / "state" / "Neocortex" / "state",
        config_directory=tmp_path / "config" / "Neocortex",
        data_directory=data,
        releases_directory=data / "releases",
        current_release=data / "current",
        models_directory=data / "models",
        runtimes_directory=data / "runtimes",
        stable_launcher=data / "bin" / "Neocortex",
        user_alias=tmp_path / "home" / ".local" / "bin" / "Neocortex",
        desktop_file=tmp_path / "home" / ".local" / "share" / "applications" / "neocortex.desktop",
        inventory_backend="portable-full-scan",
        identity_backend="posix-st_dev-st_ino",
        path_collation="BINARY",
        containment_backend="posix-session-process-group-rlimit",
        elevation="not-required",
        mutation_backend="intentionally-unavailable",
        mutation_available=False,
        compatible=True,
    )


def _release(layout: LinuxReleaseLayout, name: str) -> Path:
    root = layout.releases / name
    (root / "bin").mkdir(parents=True)
    command = root / "bin" / "Neocortex"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    return root


def _activate(layout: LinuxReleaseLayout, release: Path) -> None:
    layout.current.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(release, layout.current.parent), layout.current)


def _write_runtime_lock(source: Path) -> Path:
    lock = source / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    lock.write_text(
        f"pip=={release_linux.PIP_BOOTSTRAP_VERSION}\n",
        encoding="utf-8",
    )
    return lock


def test_release_identifier_is_version_sha_python_and_platform_bound() -> None:
    assert release_linux.release_id("a" * 40) == (f"0.9.0-{'a' * 12}-cp314-linux-x86_64")
    with pytest.raises(ValueError):
        release_linux.release_id("A" * 40)


def test_build_workspace_staging_shares_the_release_filesystem(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))

    assert layout.staging == layout.releases / ".staging"
    assert layout.staging.parent == layout.releases


def test_pip_bootstrap_policy_is_hash_pinned_and_matches_constraints() -> None:
    constraints = (PROJECT_ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()

    assert f"pip=={release_linux.PIP_BOOTSTRAP_VERSION}" in constraints
    assert release_linux.PIP_BOOTSTRAP_URL.startswith("https://files.pythonhosted.org/")
    assert release_linux.PIP_BOOTSTRAP_URL.endswith(release_linux.PIP_BOOTSTRAP_FILENAME)
    assert len(release_linux.PIP_BOOTSTRAP_SHA256) == 64


def test_linux_cp314_runtime_lock_is_exact_and_complete() -> None:
    lock = PROJECT_ROOT / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    entries = release_linux._runtime_dependency_lock(lock)

    assert len(entries) >= 100
    assert entries["pip"] == release_linux.PIP_BOOTSTRAP_VERSION
    assert "neocortex-framework" not in entries
    assert all(name == release_linux._normalized_distribution_name(name) for name in entries)


def test_release_install_uses_the_runtime_lock_as_a_second_constraint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    wheel = tmp_path / "neocortex_framework-0.9.0-py3-none-any.whl"
    constraints = tmp_path / "constraints.txt"
    runtime_lock = tmp_path / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    pip_wheel = tmp_path / release_linux.PIP_BOOTSTRAP_FILENAME
    wheel.write_bytes(b"wheel")
    constraints.write_text("pip==26.1.2\n", encoding="utf-8")
    runtime_lock.write_text("pip==26.1.2\n", encoding="utf-8")
    observed: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        release_linux,
        "_create_pip_environment",
        lambda root, *_args, **_kwargs: (root / "bin").mkdir(parents=True),
    )

    def runner(arguments, **_kwargs):
        observed.append(tuple(map(str, arguments)))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    release_linux._install_wheel(
        release_root,
        wheel,
        constraints,
        runtime_lock,
        pip_wheel=pip_wheel,
        runner=runner,
    )

    command = observed[-1]
    positions = [index for index, item in enumerate(command) if item == "--constraint"]
    assert [command[index + 1] for index in positions] == [
        str(constraints),
        str(runtime_lock),
    ]


def test_runtime_dependency_verifier_rejects_inventory_drift(tmp_path: Path) -> None:
    lock = tmp_path / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    lock.write_text("pip==26.1.2\n", encoding="utf-8")

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"idna":"3.19","pip":"26.1.2"}',
            "",
        )

    with pytest.raises(release_linux.LinuxReleaseError, match="differs from its lock: idna"):
        release_linux._verify_runtime_dependency_lock(
            tmp_path / "python",
            lock,
            runner=runner,
            environment={},
        )


def test_linux_release_smoke_imports_sqlglot_required_by_code_analysis() -> None:
    assert "sqlglot" in release_linux._IMPORT_MODULES


def test_pip_bootstrap_rejects_wrong_artifact_before_creating_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / release_linux.PIP_BOOTSTRAP_FILENAME
    wheel.write_bytes(b"not the pinned pip wheel")
    created = False

    class RejectBuilder:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr(release_linux.venv, "EnvBuilder", RejectBuilder)

    with pytest.raises(release_linux.LinuxReleaseError, match="exact SHA-256"):
        release_linux._create_pip_environment(tmp_path / "environment", wheel)

    assert created is False


def test_pip_bootstrap_never_invokes_the_bundled_venv_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic verified pip wheel"
    wheel = tmp_path / "pip-safe-py3-none-any.whl"
    wheel.write_bytes(payload)
    monkeypatch.setattr(release_linux, "PIP_BOOTSTRAP_FILENAME", wheel.name)
    monkeypatch.setattr(release_linux, "PIP_BOOTSTRAP_SHA256", hashlib.sha256(payload).hexdigest())
    builders: list[dict[str, object]] = []

    class FakeBuilder:
        def __init__(self, **kwargs: object) -> None:
            builders.append(kwargs)

        def create(self, root: Path) -> None:
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "python").write_bytes(b"")

    calls: list[tuple[str, ...]] = []

    def runner(arguments, **_kwargs):
        call = tuple(os.fspath(argument) for argument in arguments)
        calls.append(call)
        stdout = (
            f"{release_linux.PIP_BOOTSTRAP_VERSION}\n"
            if any("import pip" in argument for argument in call)
            else ""
        )
        return subprocess.CompletedProcess(call, 0, stdout, "")

    monkeypatch.setattr(release_linux.venv, "EnvBuilder", FakeBuilder)

    release_linux._create_pip_environment(tmp_path / "environment", wheel, runner=runner)

    assert builders == [{"with_pip": False, "clear": False, "symlinks": True}]
    install = calls[0]
    assert install[1:4] == ("-I", "-c", release_linux._PIP_WHEEL_RUNNER)
    assert "-m" not in install
    assert "--no-index" in install
    assert "--no-deps" in install
    assert calls[1][1:3] == ("-I", "-c")


def test_corpus_root_preparation_creates_once_and_rejects_non_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Documentos con acento á" / "NeoCortex" / "Corpus"

    assert release_linux._prepare_corpus_root(root) is True
    assert root.is_dir()
    assert release_linux._prepare_corpus_root(root) is False

    invalid = tmp_path / "not-a-directory"
    invalid.write_text("fixture", encoding="utf-8")
    with pytest.raises(release_linux.LinuxReleaseError, match="real directory"):
        release_linux._prepare_corpus_root(invalid)


def test_new_virtual_environment_is_created_at_its_final_non_movable_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    sha = "e" * 40
    final_release = layout.releases / release_linux.release_id(sha)
    installed_at: list[Path] = []

    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(
        release_linux,
        "_prepare_pip_bootstrap",
        lambda workspace: workspace / release_linux.PIP_BOOTSTRAP_FILENAME,
    )

    def build_wheel(_layout, workspace, **_kwargs):
        wheelhouse = workspace / "wheelhouse"
        wheelhouse.mkdir()
        wheel = wheelhouse / "neocortex_framework-0.9.0-py3-none-any.whl"
        dependency = wheelhouse / "yattag-1.16.1-py3-none-any.whl"
        wheel.write_bytes(b"project")
        dependency.write_bytes(b"dependency")
        return wheel, (dependency,)

    def install_wheel(release_root, *_args, **_kwargs):
        installed_at.append(release_root)
        (release_root / "bin").mkdir(parents=True)
        command = release_root / "bin" / "Neocortex"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)

    monkeypatch.setattr(release_linux, "_build_wheel", build_wheel)
    monkeypatch.setattr(release_linux, "_install_wheel", install_wheel)
    monkeypatch.setattr(release_linux, "_install_semgrep_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_linux, "_install_node_pyright", lambda *_args, **_kwargs: "1" * 64)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda release_root, *_args, **_kwargs: (
            {
                "node": "v24.18.1",
                "pyright": "pyright 1.1.411",
            }
            if release_root == final_release
            else pytest.fail("release validation used a movable staging venv")
        ),
    )
    monkeypatch.setattr(release_linux, "_make_immutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_linux, "_require_immutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        release_linux,
        "_publish_public_access",
        lambda *_args, **_kwargs: ({}, {}),
    )

    corpus_root = tmp_path / "corpus"
    report = release_linux.install_release(
        layout,
        corpus_root=corpus_root,
        prepare_models=False,
        desktop=False,
    )

    assert installed_at == [final_release]
    assert report["release_path"] == str(final_release)
    assert report["corpus_root"] == str(corpus_root)
    assert report["corpus_root_created"] is True
    assert corpus_root.is_dir()
    assert release_linux._current_target(layout) == final_release.resolve()
    assert report["artifacts"]["runtime_dependency_lock_filename"] == (
        release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    )
    assert report["artifacts"]["runtime_dependency_count"] == 1


def test_release_script_is_directly_executable_from_the_documented_path() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "release_linux.py"), "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "{install,verify,rollback}" in completed.stdout


def test_atomic_current_replacement_preserves_previous_releases(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    old = _release(layout, "old")
    new = _release(layout, "new")
    _activate(layout, old)

    release_linux._replace_current(layout, new)

    assert release_linux._current_target(layout) == new.resolve()
    assert old.is_dir()
    assert new.is_dir()


def test_launcher_works_through_user_alias_when_alias_lives_elsewhere(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    release = _release(layout, "active")
    _activate(layout, release)

    release_linux._publish_public_access(
        layout,
        tmp_path / "Corpus con espacio",
        desktop=False,
    )

    completed = subprocess.run(
        (layout.alias, "--version"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0


def test_desktop_entry_quotes_launcher_paths_with_spaces(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    spaced_data = tmp_path / "data with space" / "Neocortex"
    policy = replace(
        policy,
        data_directory=spaced_data,
        releases_directory=spaced_data / "releases",
        current_release=spaced_data / "current",
        stable_launcher=spaced_data / "bin" / "Neocortex",
    )
    layout = LinuxReleaseLayout(tmp_path / "source", policy)
    payload = release_linux._desktop_payload(layout)
    desktop = tmp_path / "neocortex.desktop"
    desktop.write_bytes(payload)

    assert b'Exec="' in payload
    validator = shutil.which("desktop-file-validate")
    if validator is not None:
        subprocess.run((validator, desktop), check=True, timeout=10)


def test_failed_rollback_receipt_restores_the_prior_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, "old")
    new = _release(layout, "new")
    _activate(layout, new)
    monkeypatch.setattr(
        release_linux,
        "_write_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic receipt failure")),
    )

    with pytest.raises(OSError, match="synthetic receipt failure"):
        release_linux.rollback_release(layout, target_release=old.name)

    assert release_linux._current_target(layout) == new.resolve()
    assert old.is_dir() and new.is_dir()


def test_failed_install_receipt_restores_current_launcher_and_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    (source / "_05_Interfaz" / "assets").mkdir(parents=True)
    _write_runtime_lock(source)
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, "old")
    sha = "b" * 40
    new = _release(layout, release_linux.release_id(sha))
    _activate(layout, old)
    layout.launcher.parent.mkdir(parents=True)
    layout.launcher.write_bytes(b"old launcher\n")
    layout.launcher.chmod(0o755)
    layout.alias.parent.mkdir(parents=True)
    os.symlink("old-target", layout.alias)

    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: {"node": "v24.18.1", "pyright": "pyright 1.1.411"},
    )
    monkeypatch.setattr(
        release_linux,
        "_read_release_manifest",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "kind": "linux_release_manifest",
            "release_id": new.name,
            "source_sha": sha,
            "wheel_filename": "neocortex.whl",
            "wheel_sha256": "1" * 64,
            "node_archive_filename": "node.tar.xz",
            "node_archive_sha256": "2" * 64,
            "node": "v24.18.1",
            "pyright": "pyright 1.1.411",
        },
    )
    manifest = new / release_linux.RELEASE_MANIFEST_NAME
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        release_linux,
        "_write_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic receipt failure")),
    )

    with pytest.raises(OSError, match="synthetic receipt failure"):
        release_linux.install_release(
            layout,
            corpus_root=tmp_path / "corpus",
            prepare_models=False,
            desktop=False,
        )

    assert release_linux._current_target(layout) == old.resolve()
    assert layout.launcher.read_bytes() == b"old launcher\n"
    assert layout.alias.is_symlink()
    assert os.readlink(layout.alias) == "old-target"
    assert new.is_dir()


def test_failed_model_preparation_never_promotes_or_publishes_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, "old")
    sha = "c" * 40
    candidate = _release(layout, release_linux.release_id(sha))
    _activate(layout, old)
    (candidate / release_linux.RELEASE_MANIFEST_NAME).write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: {"node": "v24.18.1", "pyright": "pyright 1.1.411"},
    )
    monkeypatch.setattr(
        release_linux,
        "_read_release_manifest",
        lambda *_args, **_kwargs: {
            "node": "v24.18.1",
            "pyright": "pyright 1.1.411",
        },
    )

    def fail_prepare(arguments, **_kwargs):
        assert tuple(map(str, arguments))[-3:] == ("models", "prepare", "--json")
        raise release_linux.LinuxReleaseError("synthetic incomplete model cache")

    with pytest.raises(release_linux.LinuxReleaseError, match="incomplete model cache"):
        release_linux.install_release(
            layout,
            corpus_root=tmp_path / "corpus",
            prepare_models=True,
            desktop=True,
            runner=fail_prepare,
        )

    assert release_linux._current_target(layout) == old.resolve()
    assert not layout.launcher.exists()
    assert not layout.alias.exists()
    assert not layout.desktop.exists()


def test_successful_rollback_records_evidence_and_retains_both_releases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, "old")
    new = _release(layout, "new")
    _activate(layout, new)

    report = release_linux.rollback_release(layout, target_release=old.name)

    assert report["operation"] == "rollback"
    assert report["previous_release"] == str(new.resolve())
    assert Path(str(report["receipt_path"])).is_file()
    assert release_linux._current_target(layout) == old.resolve()
    assert old.is_dir() and new.is_dir()
