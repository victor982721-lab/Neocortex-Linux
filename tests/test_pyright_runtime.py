"""Exact-lock installation regressions for the owned Pyright runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tools import pyright_runtime, release_linux


PROJECT_ROOT = Path(__file__).resolve().parents[1]
Command = Sequence[str | os.PathLike[str]]


def _completed(arguments: Command, stdout: str = "") -> subprocess.CompletedProcess[str]:
    command = tuple(os.fspath(argument) for argument in arguments)
    return subprocess.CompletedProcess(command, 0, stdout, "")


def _materialize_runtime(target: Path, *, version: str = pyright_runtime.PYRIGHT_VERSION) -> None:
    package = target / "node_modules" / "pyright"
    binaries = target / "node_modules" / ".bin"
    package.mkdir(parents=True)
    binaries.mkdir()
    (package / "package.json").write_text(
        json.dumps({"name": "pyright", "version": version}),
        encoding="utf-8",
    )
    (package / "index.js").write_text("// locked Pyright fixture\n", encoding="utf-8")
    installed_lock = {
        "name": "neocortex-pyright-runtime",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "node_modules/pyright": {
                "version": pyright_runtime.PYRIGHT_VERSION,
                "resolved": (
                    "https://registry.npmjs.org/pyright/-/"
                    f"pyright-{pyright_runtime.PYRIGHT_VERSION}.tgz"
                ),
                "integrity": pyright_runtime.PYRIGHT_PACKAGE_INTEGRITY,
                "bin": {
                    "pyright": "index.js",
                    "pyright-langserver": "langserver.index.js",
                },
            }
        },
    }
    (target / "node_modules" / ".package-lock.json").write_text(
        json.dumps(installed_lock),
        encoding="utf-8",
    )
    if os.name == "nt":
        (binaries / "pyright.cmd").write_text("@echo off\r\n", encoding="utf-8")
    else:
        (binaries / "pyright").symlink_to(Path("..") / "pyright" / "index.js")


def _fake_executables(tmp_path: Path) -> tuple[Path, Path]:
    binary_root = tmp_path / "node-bin"
    binary_root.mkdir()
    node = binary_root / ("node.exe" if os.name == "nt" else "node")
    npm = binary_root / ("npm.cmd" if os.name == "nt" else "npm")
    node.write_bytes(b"fixture")
    npm.write_bytes(b"fixture")
    return node, npm


def test_canonical_pyright_lock_has_exact_manifest_and_integrity() -> None:
    manifest, lock = pyright_runtime.validate_pyright_lock()

    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == (
        pyright_runtime.PYRIGHT_MANIFEST_SHA256
    )
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == pyright_runtime.PYRIGHT_LOCK_SHA256
    payload = json.loads(lock.read_text(encoding="utf-8"))
    assert payload["packages"]["node_modules/pyright"] == {
        "version": "1.1.411",
        "resolved": "https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz",
        "integrity": pyright_runtime.PYRIGHT_PACKAGE_INTEGRITY,
        "license": "MIT",
        "bin": {"pyright": "index.js", "pyright-langserver": "langserver.index.js"},
        "engines": {"node": ">=14.0.0"},
        "optionalDependencies": {"fsevents": "~2.3.3"},
    }
    assert (
        "tools/pyright_runtime_lock/*.json text eol=lf"
        in (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    )


def test_tampered_lock_fails_before_node_or_npm_can_execute(tmp_path: Path) -> None:
    lock_directory = tmp_path / "lock"
    shutil.copytree(pyright_runtime.PYRIGHT_LOCK_DIRECTORY, lock_directory)
    with (lock_directory / pyright_runtime.PYRIGHT_LOCK_NAME).open("ab") as stream:
        stream.write(b"\n")
    node, npm = _fake_executables(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Command, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(os.fspath(argument) for argument in arguments))
        return _completed(arguments)

    target = tmp_path / "runtime"
    with pytest.raises(pyright_runtime.PyrightRuntimeError, match="exact SHA-256"):
        pyright_runtime.install_pyright_runtime(
            target,
            node=node,
            npm=npm,
            runner=runner,
            lock_directory=lock_directory,
        )

    assert calls == []
    assert not target.exists()


def test_install_is_lock_driven_and_scrubs_hostile_node_and_npm_environment(
    tmp_path: Path,
) -> None:
    node, npm = _fake_executables(tmp_path)
    target = tmp_path / "runtime"
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(
        arguments: Command,
        *,
        environment: Mapping[str, str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(os.fspath(argument) for argument in arguments)
        calls.append((command, dict(environment)))
        if command == (os.fspath(node), "--version"):
            return _completed(arguments, f"v{pyright_runtime.NODE_VERSION}\n")
        if command[:2] == (os.fspath(npm), "ci"):
            _materialize_runtime(target)
            return _completed(arguments)
        expected_index = target / "node_modules" / "pyright" / "index.js"
        if command == (os.fspath(node), os.fspath(expected_index), "--version"):
            return _completed(arguments, f"pyright {pyright_runtime.PYRIGHT_VERSION}\n")
        raise AssertionError(f"unexpected command: {command}")

    report = pyright_runtime.install_pyright_runtime(
        target,
        node=node,
        npm=npm,
        runner=runner,
        environment={
            "NODE_OPTIONS": "--require=/tmp/hostile-preload.js",
            "node_path": "/tmp/hostile-node-modules",
            "NPM_CONFIG_REGISTRY": "https://hostile.invalid/",
            "npm_config_ignore_scripts": "false",
            "PATH": "/tmp/hostile-bin",
            "SAFE_MARKER": "preserved",
        },
    )

    npm_command = calls[1][0]
    assert npm_command[:2] == (os.fspath(npm), "ci")
    assert npm_command[npm_command.index("--prefix") + 1] == os.fspath(target)
    assert "install" not in npm_command
    assert "--ignore-scripts" in npm_command
    assert "--no-audit" in npm_command
    assert "--no-fund" in npm_command
    assert "--omit=optional" in npm_command
    assert not any(argument.startswith("pyright@") for argument in npm_command)
    for _command, environment in calls:
        folded = {key.casefold(): value for key, value in environment.items()}
        assert "node_options" not in folded
        assert "node_path" not in folded
        assert "npm_config_registry" not in folded
        assert folded["npm_config_ignore_scripts"] == "true"
        assert folded["npm_config_bin_links"] == "true"
        assert folded["safe_marker"] == "preserved"
        assert environment["PATH"].split(os.pathsep)[0] == os.fspath(node.parent)
    assert (target / pyright_runtime.PYRIGHT_LOCK_NAME).read_bytes() == (
        pyright_runtime.PYRIGHT_LOCK_DIRECTORY / pyright_runtime.PYRIGHT_LOCK_NAME
    ).read_bytes()
    assert report == {
        "node": f"v{pyright_runtime.NODE_VERSION}",
        "pyright": f"pyright {pyright_runtime.PYRIGHT_VERSION}",
        "pyright_integrity": pyright_runtime.PYRIGHT_PACKAGE_INTEGRITY,
        "pyright_lock_sha256": pyright_runtime.PYRIGHT_LOCK_SHA256,
    }


def test_divergent_node_fails_before_npm_or_target_creation(tmp_path: Path) -> None:
    node, npm = _fake_executables(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Command, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = tuple(os.fspath(argument) for argument in arguments)
        calls.append(command)
        return _completed(arguments, "v23.0.0\n")

    target = tmp_path / "runtime"
    with pytest.raises(pyright_runtime.PyrightRuntimeError, match="unexpected Node version"):
        pyright_runtime.install_pyright_runtime(
            target,
            node=node,
            npm=npm,
            runner=runner,
        )

    assert calls == [(os.fspath(node), "--version")]
    assert not target.exists()


def test_divergent_installed_pyright_version_fails_closed(tmp_path: Path) -> None:
    node, _npm = _fake_executables(tmp_path)
    target = tmp_path / "runtime"
    target.mkdir()
    for name in (pyright_runtime.PYRIGHT_MANIFEST_NAME, pyright_runtime.PYRIGHT_LOCK_NAME):
        shutil.copyfile(pyright_runtime.PYRIGHT_LOCK_DIRECTORY / name, target / name)
    _materialize_runtime(target, version="1.1.410")

    with pytest.raises(pyright_runtime.PyrightRuntimeError, match="package version"):
        pyright_runtime.verify_pyright_runtime(target, node=node)


def test_failed_npm_ci_removes_partial_runtime(tmp_path: Path) -> None:
    node, npm = _fake_executables(tmp_path)
    target = tmp_path / "runtime"

    def runner(arguments: Command, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = tuple(os.fspath(argument) for argument in arguments)
        if command == (os.fspath(node), "--version"):
            return _completed(arguments, f"v{pyright_runtime.NODE_VERSION}\n")
        raise pyright_runtime.PyrightRuntimeError("synthetic npm failure")

    with pytest.raises(pyright_runtime.PyrightRuntimeError, match="synthetic npm failure"):
        pyright_runtime.install_pyright_runtime(
            target,
            node=node,
            npm=npm,
            runner=runner,
        )

    assert not target.exists()


def test_pyright_runtime_cli_is_directly_isolated_executable() -> None:
    completed = subprocess.run(
        (sys.executable, "-I", PROJECT_ROOT / "tools" / "pyright_runtime.py", "--help"),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "{install,verify}" in completed.stdout


@pytest.mark.skipif(os.name == "nt", reason="Linux release archive contract")
def test_linux_release_reuses_lock_installer_and_preserves_node_archive_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    release_root = tmp_path / "release"
    release_root.mkdir()
    semgrep_marker = release_root / "tools" / "semgrep" / "owned-marker"
    semgrep_marker.parent.mkdir(parents=True)
    semgrep_marker.write_text("preserve\n", encoding="utf-8")
    extracted_root = workspace / f"node-v{pyright_runtime.NODE_VERSION}-linux-x64"
    (extracted_root / "bin").mkdir(parents=True)
    (extracted_root / "bin" / "node").write_bytes(b"node")
    (extracted_root / "bin" / "npm").write_bytes(b"npm")
    archive = workspace / "node.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        output.add(extracted_root, arcname=extracted_root.name)
    shutil.rmtree(extracted_root)
    archive_sha = "a" * 64
    monkeypatch.setattr(release_linux, "_node_archive", lambda _workspace: (archive, archive_sha))
    installed: dict[str, object] = {}

    def install(target: Path, **kwargs: object) -> dict[str, str]:
        installed["target"] = target
        installed.update(kwargs)
        return {}

    monkeypatch.setattr(release_linux, "install_pyright_runtime", install)

    observed = release_linux._install_node_pyright(release_root, workspace)

    node_bin = release_root / "tools" / "node" / "bin"
    assert observed == archive_sha
    assert semgrep_marker.read_text(encoding="utf-8") == "preserve\n"
    assert installed["target"] == release_root / "tools" / "pyright"
    assert installed["node"] == node_bin / "node"
    assert installed["npm"] == node_bin / "npm"
    environment = installed["environment"]
    assert isinstance(environment, Mapping)
    assert environment["PATH"].split(os.pathsep)[0] == os.fspath(node_bin)


@pytest.mark.skipif(os.name == "nt", reason="Linux release directory contract")
@pytest.mark.parametrize(
    ("tools_kind", "message"),
    (("file", "unavailable"), ("symlink", "real directory")),
)
def test_linux_release_rejects_an_unsafe_tools_parent_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools_kind: str,
    message: str,
) -> None:
    release_root = tmp_path / "release"
    release_root.mkdir()
    tools_root = release_root / "tools"
    if tools_kind == "file":
        tools_root.write_text("unsafe\n", encoding="utf-8")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        tools_root.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        release_linux,
        "_node_archive",
        lambda _workspace: (tmp_path / "unused.tar.xz", "a" * 64),
    )

    with pytest.raises(release_linux.LinuxReleaseError, match=message):
        release_linux._install_node_pyright(release_root, tmp_path / "workspace")


@pytest.mark.skipif(os.name == "nt", reason="Linux release manifest contract")
def test_linux_release_manifest_requires_exact_pyright_lock_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_name = "0.9.0-" + "a" * 12 + "-cp314-linux-x86_64"
    source_sha = "a" * 40
    release_root = tmp_path / release_name
    release_root.mkdir()
    wheel = tmp_path / "neocortex_framework-0.9.0-py3-none-any.whl"
    payload = release_linux._release_manifest(
        release_name=release_name,
        source_sha=source_sha,
        wheel=wheel,
        wheel_sha="b" * 64,
        source_only_wheels={"dependency.whl": "c" * 64},
        node_sha="d" * 64,
        versions={
            "node": f"v{pyright_runtime.NODE_VERSION}",
            "pip": release_linux.PIP_BOOTSTRAP_VERSION,
            "pyright": f"pyright {pyright_runtime.PYRIGHT_VERSION}",
            "pyright_integrity": pyright_runtime.PYRIGHT_PACKAGE_INTEGRITY,
            "pyright_lock_sha256": pyright_runtime.PYRIGHT_LOCK_SHA256,
            "semgrep": release_linux.SEMGREP_TOOL_VERSION,
            "semgrep_runtime_sha256": "e" * 64,
        },
    )
    manifest = release_root / release_linux.RELEASE_MANIFEST_NAME
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(release_linux, "_require_immutable", lambda _root: None)

    assert (
        release_linux._read_release_manifest(
            release_root,
            release_name=release_name,
            source_sha=source_sha,
        )["pyright_integrity"]
        == pyright_runtime.PYRIGHT_PACKAGE_INTEGRITY
    )

    payload["pyright_integrity"] = "sha512-hostile"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(release_linux.LinuxReleaseError, match="manifest failed validation"):
        release_linux._read_release_manifest(
            release_root,
            release_name=release_name,
            source_sha=source_sha,
        )
