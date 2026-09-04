"""Atomic Linux release activation, receipts, and rollback regressions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.platform.policy import PlatformPolicy
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
    if release_linux.parse_release_id(name) is not None:
        source_sha = release_linux.parse_release_id(name)[1].ljust(40, "0")
        manifest = {
            "schema_version": release_linux.RECEIPT_SCHEMA_VERSION,
            "kind": "linux_release_manifest",
            "release_id": name,
            "source_sha": source_sha,
            "wheel_filename": (
                f"neocortex_framework-{release_linux.parse_release_id(name)[0]}-"
                "py3-none-any.whl"
            ),
            "wheel_sha256": "a" * 64,
            "pip_bootstrap_wheel_filename": release_linux.PIP_BOOTSTRAP_FILENAME,
            "pip_bootstrap_wheel_sha256": release_linux.PIP_BOOTSTRAP_SHA256,
            "pip": release_linux.PIP_BOOTSTRAP_VERSION,
        }
        (root / release_linux.RELEASE_MANIFEST_NAME).write_text(
            json.dumps(manifest) + "\n", encoding="utf-8"
        )
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


def _wheelhouse_fixture(tmp_path: Path, *specs: tuple[str, str]) -> Path:
    """Create a tiny hash-manifested wheelhouse for release-tool tests."""

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    entries: list[dict[str, str]] = []
    for name, version in specs:
        normalized = name.replace("-", "_")
        filename = f"{normalized}-{version}-py3-none-any.whl"
        wheel = wheelhouse / filename
        dist_info = f"{normalized}-{version}.dist-info"
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
        entries.append(
            {
                "filename": filename,
                "name": name,
                "version": version,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
        )
    (wheelhouse / release_linux.WHEELHOUSE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": release_linux.WHEELHOUSE_SCHEMA_VERSION,
                "kind": "neocortex_wheelhouse",
                "python": "cp314",
                "platform": "linux_x86_64",
                "artifacts": entries,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return wheelhouse


def test_release_identifier_is_version_sha_python_and_platform_bound() -> None:
    assert release_linux.release_id("a" * 40) == (
        f"{release_linux.__version__}-{'a' * 12}-cp314-linux-x86_64"
    )
    with pytest.raises(ValueError):
        release_linux.release_id("A" * 40)


@pytest.mark.parametrize(
    "name",
    [
        "0.9.0-aaaaaaaaaaaa-cp314-linux-x86_64",
        "0.10.0-bbbbbbbbbbbb-cp314-linux-x86_64",
        "0.10.0-rc.1-" + "c" * 12 + "-cp314-linux-x86_64",
        "0.10.0-" + "d" * 40 + "-cp314-linux-x86_64",
    ],
)
def test_release_identifier_parser_is_cross_version_but_strict(name: str) -> None:
    assert release_linux.parse_release_id(name) is not None
    assert release_linux.parse_release_id("0.9.0-backup") is None
    assert release_linux.parse_release_id(name.replace("cp314", "cp313")) is None


def test_offline_environment_drops_indexes_credentials_and_import_overrides() -> None:
    environment = release_linux._offline_environment(
        {
            "PATH": "/usr/bin",
            "PIP_INDEX_URL": "https://user:secret@example.invalid/simple",
            "PIP_EXTRA_INDEX_URL": "https://example.invalid/extra",
            "PIP_TRUSTED_HOST": "example.invalid",
            "PIP_CONFIG_FILE": "/tmp/attacker.conf",
            "PYTHONPATH": "/tmp/attacker",
            "PYTHONHOME": "/tmp/attacker-python",
            "PYTHONUSERBASE": "/tmp/attacker-user",
        }
    )

    assert environment["PATH"] == "/usr/bin"
    assert not any(name.upper().startswith("PIP_INDEX") for name in environment)
    assert not any(name.upper().startswith("PIP_EXTRA") for name in environment)
    assert "PIP_TRUSTED_HOST" not in environment
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "PYTHONUSERBASE" not in environment


def test_wheelhouse_manifest_requires_local_hash_and_metadata_integrity(tmp_path: Path) -> None:
    wheelhouse = _wheelhouse_fixture(tmp_path, ("pip", release_linux.PIP_BOOTSTRAP_VERSION))
    artifacts = release_linux._validate_wheelhouse(
        wheelhouse,
        required={"pip": release_linux.PIP_BOOTSTRAP_VERSION},
    )
    assert artifacts["pip"].path == wheelhouse / artifacts["pip"].filename

    artifact = next(wheelhouse.glob("*.whl"))
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(release_linux.LinuxReleaseError, match="hash mismatch"):
        release_linux._validate_wheelhouse(wheelhouse)


def test_wheel_metadata_ignores_nested_vendor_dist_info(tmp_path: Path) -> None:
    wheel = tmp_path / "setuptools-83.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "setuptools-83.0.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: setuptools\nVersion: 83.0.0\n",
        )
        archive.writestr("setuptools-83.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(
            "setuptools/_vendor/example-1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: example\nVersion: 1.0\n",
        )

    assert release_linux._wheel_metadata(wheel) == ("setuptools", "83.0.0")


def test_install_requires_an_explicit_local_wheelhouse_before_preparing_corpus(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    corpus = tmp_path / "corpus"

    with pytest.raises(release_linux.LinuxReleaseError, match="offline wheelhouse is required"):
        release_linux.install_release(
            layout,
            corpus_root=corpus,
            prepare_models=False,
            desktop=False,
        )
    assert not corpus.exists()


def test_build_workspace_staging_shares_the_release_filesystem(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))

    assert layout.staging == layout.releases / ".staging"
    assert layout.staging.parent == layout.releases


def test_layout_preflight_rejects_symlinked_release_root(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    policy.data_directory.parent.mkdir(parents=True)
    policy.data_directory.symlink_to(external, target_is_directory=True)
    layout = LinuxReleaseLayout(policy.data_directory, policy)

    with pytest.raises(release_linux.LinuxReleaseError, match="root component is unsafe"):
        release_linux._validate_layout(layout)


def test_source_stage_preflight_rejects_external_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    (source / "link.py").symlink_to(outside)

    with pytest.raises(release_linux.LinuxReleaseError, match="escapes source root"):
        release_linux._validate_tracked_source_links(source, ("link.py",))


def test_release_tree_rejects_unallowlisted_symlink(tmp_path: Path) -> None:
    root = _release(
        LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path)),
        release_linux.release_id("a" * 40),
    )
    outside = tmp_path / "outside"
    outside.write_text("not in release\n", encoding="utf-8")
    (root / "external").symlink_to(outside)

    with pytest.raises(release_linux.LinuxReleaseError, match="unsafe symlink"):
        release_linux._validate_release_tree(root, expected_tree_sha256=None)


def test_staging_reaper_removes_only_stale_workspaces(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    stale = layout.staging / "stale"
    stale.mkdir(parents=True)
    (stale / "partial").write_text("partial\n", encoding="utf-8")
    old = time.time() - release_linux._STAGING_STALE_SECONDS - 1
    os.utime(stale, (old, old))

    assert release_linux._reap_staging(layout) == ("stale",)
    assert not stale.exists()


def test_staging_reaper_abstains_from_active_workspace(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    active = layout.staging / "active"
    active.mkdir(parents=True)
    release_linux._write_staging_marker(active, release_name=release_linux.release_id("a" * 40))

    with pytest.raises(release_linux.LinuxReleaseError, match="active"):
        release_linux._reap_staging(layout)
    assert active.is_dir()


def test_staging_reaper_rejects_corrupt_marker_even_when_old(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    broken = layout.staging / "broken"
    broken.mkdir(parents=True)
    (broken / release_linux._STAGING_MARKER).write_text("{\n", encoding="utf-8")
    old = time.time() - release_linux._STAGING_STALE_SECONDS - 1
    os.utime(broken, (old, old))

    with pytest.raises(release_linux.LinuxReleaseError, match="marker is invalid"):
        release_linux._reap_staging(layout)
    assert broken.is_dir()


def test_release_tree_digest_is_independent_of_filesystem_creation_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, names in ((first, ("b", "a")), (second, ("a", "b"))):
        root.mkdir()
        for name in names:
            child = root / "nested" / name
            child.parent.mkdir(exist_ok=True)
            child.write_text(f"payload-{name}\n", encoding="utf-8")

    assert release_linux._release_tree_digest(first) == release_linux._release_tree_digest(second)


def test_release_in_use_detects_process_holding_release_file_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    held = root / "held.bin"
    held.write_bytes(b"held")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; f=open(sys.argv[1], 'rb'); print('ready', flush=True); sys.stdin.read(1)",
            str(held),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert process.poll() is None
        assert process.pid in release_linux._release_in_use(root)
    finally:
        if process.stdin is not None:
            process.stdin.write("x")
            process.stdin.close()
        process.wait(timeout=5)


def test_reap_staging_restores_gc_tombstone_after_interrupted_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    old = _release(layout, release_linux.release_id("b" * 40))
    _activate(layout, current)

    transaction, names = release_linux._stage_old_releases(
        layout,
        current=current,
        rollback=None,
    )
    assert names == (old.name,)
    marker = transaction / release_linux._GC_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["pid"] = 999_999_999
    payload["starttime"] = "1"
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert release_linux._reap_staging(layout) == (transaction.name,)
    assert old.is_dir()
    assert not transaction.exists()


def test_reap_staging_commits_gc_tombstone_when_receipt_is_durable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    old = _release(layout, release_linux.release_id("b" * 40))
    _activate(layout, current)

    transaction, names = release_linux._stage_old_releases(
        layout,
        current=current,
        rollback=None,
    )
    marker = transaction / release_linux._GC_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["pid"] = 999_999_999
    payload["starttime"] = "1"
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release_linux._write_receipt(
        layout,
        {
            "schema_version": release_linux.RECEIPT_SCHEMA_VERSION,
            "kind": "linux_release_receipt",
            "operation": "install",
            "release_id": current.name,
            "retention_policy": "current_and_immediate_rollback_v1",
            "retained_releases": [current.name],
            "pruned_releases": list(names),
            "result": "success",
        },
    )

    assert release_linux._reap_staging(layout) == (transaction.name,)
    assert not old.exists()
    assert not transaction.exists()


def test_wheel_build_uses_a_git_owned_source_stage_without_build_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    package = source / "neocortex"
    package.mkdir(parents=True)
    (source / "constraints.txt").write_text("setuptools==83.0.0\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    residue = source / "build/lib/removed_package.py"
    residue.parent.mkdir(parents=True)
    residue.write_text("STALE = True\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed_build_source: list[Path] = []
    observed_commands: list[tuple[str, ...]] = []
    observed_environments: list[dict[str, str] | None] = []

    monkeypatch.setattr(
        release_linux,
        "_create_pip_environment",
        lambda root, *_args, **_kwargs: (root / "bin").mkdir(parents=True),
    )
    monkeypatch.setattr(release_linux, "_venv_python", lambda root: root / "bin/python")

    def runner(arguments, **_kwargs):
        command = tuple(os.fspath(item) for item in arguments)
        observed_commands.append(command)
        observed_environments.append(_kwargs.get("environment"))
        if "ls-files" in command:
            tracked = "constraints.txt\0neocortex/__init__.py\0pyproject.toml\0"
            return subprocess.CompletedProcess(command, 0, tracked, "")
        if "build" in command and "--outdir" in command:
            staged = Path(command[-1])
            observed_build_source.append(staged)
            assert staged != source
            assert (staged / "neocortex/__init__.py").is_file()
            assert not (staged / "build").exists()
            wheelhouse = Path(command[command.index("--outdir") + 1])
            (
                wheelhouse / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
            ).write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0, "", "")

    wheel = release_linux._build_wheel(
        layout,
        workspace,
        pip_wheel=tmp_path / "pip.whl",
        runner=runner,
    )

    assert observed_build_source == [workspace / "source"]
    assert wheel.read_bytes() == b"wheel"
    pip_install = next(command for command in observed_commands if "pip" in command and "install" in command)
    assert {"--no-index", "--require-hashes", "--only-binary=:all:"} <= set(pip_install)
    build_environment = next(
        environment for environment in observed_environments if environment is not None
    )
    assert build_environment["PIP_NO_INDEX"] == "1"
    assert "PIP_INDEX_URL" not in build_environment
    assert "PYTHONPATH" not in build_environment


def test_pip_bootstrap_policy_is_hash_pinned_and_matches_constraints() -> None:
    constraints = (PROJECT_ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()

    assert f"pip=={release_linux.PIP_BOOTSTRAP_VERSION}" in constraints
    assert release_linux.PIP_BOOTSTRAP_URL.startswith("https://files.pythonhosted.org/")
    assert release_linux.PIP_BOOTSTRAP_URL.endswith(release_linux.PIP_BOOTSTRAP_FILENAME)
    assert len(release_linux.PIP_BOOTSTRAP_SHA256) == 64


def test_linux_cp314_runtime_lock_is_exact_and_complete() -> None:
    lock = PROJECT_ROOT / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    entries = release_linux._runtime_dependency_lock(lock)

    assert len(entries) >= 50
    assert entries["pip"] == release_linux.PIP_BOOTSTRAP_VERSION
    assert "neocortex-framework" not in entries
    assert all(name == release_linux._normalized_distribution_name(name) for name in entries)
    assert "nudenet" not in entries
    assert not {"pytest", "ruff", "mypy", "coverage", "pip-audit"} & entries.keys()


def test_release_install_uses_the_runtime_lock_as_a_second_constraint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    wheel = tmp_path / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
    constraints = tmp_path / "constraints.txt"
    runtime_lock = tmp_path / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    pip_wheel = tmp_path / release_linux.PIP_BOOTSTRAP_FILENAME
    wheel.write_bytes(b"wheel")
    constraints.write_text("pip==26.2.1\n", encoding="utf-8")
    runtime_lock.write_text("pip==26.2.1\n", encoding="utf-8")
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
    assert {"--no-index", "--require-hashes", "--only-binary=:all:"} <= set(command)


def test_release_install_excludes_build_only_wheels_from_runtime_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = _wheelhouse_fixture(
        tmp_path,
        ("pip", "26.2.1"),
        ("build", "1.5.0"),
        ("setuptools", "83.0.0"),
        ("wheel", "0.48.0"),
        ("pyproject-hooks", "1.2.0"),
    )
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    project_wheel = (
        project_directory / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
    )
    project_wheel.write_bytes(b"project-wheel")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("pip==26.2.1\n", encoding="utf-8")
    runtime_lock = tmp_path / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    runtime_lock.write_text("pip==26.2.1\n", encoding="utf-8")
    release_root = tmp_path / "release"
    pip_wheel = tmp_path / release_linux.PIP_BOOTSTRAP_FILENAME
    observed: list[tuple[str, ...]] = []
    captured_requirements = ""

    monkeypatch.setattr(
        release_linux,
        "_create_pip_environment",
        lambda root, *_args, **_kwargs: (root / "bin").mkdir(parents=True),
    )

    def runner(arguments, **_kwargs):
        nonlocal captured_requirements
        observed.append(tuple(map(str, arguments)))
        if "--requirement" in arguments:
            requirement_path = Path(arguments[arguments.index("--requirement") + 1])
            captured_requirements = requirement_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    release_linux._install_wheel(
        release_root,
        project_wheel,
        constraints,
        runtime_lock,
        pip_wheel=pip_wheel,
        wheelhouse=wheelhouse,
        runner=runner,
    )

    assert "pip==26.2.1" in captured_requirements
    assert "build==1.5.0" not in captured_requirements
    assert "setuptools==83.0.0" not in captured_requirements
    assert "wheel==0.48.0" not in captured_requirements
    assert "pyproject-hooks==1.2.0" not in captured_requirements


def test_runtime_dependency_verifier_rejects_inventory_drift(tmp_path: Path) -> None:
    lock = tmp_path / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    lock.write_text("pip==26.2.1\n", encoding="utf-8")

    def runner(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments,
            0,
            '{"idna":"3.19","pip":"26.2.1"}',
            "",
        )

    with pytest.raises(release_linux.LinuxReleaseError, match="differs from its lock: idna"):
        release_linux._verify_runtime_dependency_lock(
            tmp_path / "python",
            lock,
            runner=runner,
            environment={},
        )


def test_product_release_manifest_excludes_development_tool_metadata(tmp_path: Path) -> None:
    lock = _write_runtime_lock(tmp_path)
    wheel = tmp_path / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    manifest = release_linux._release_manifest(
        release_name="candidate",
        source_sha="a" * 40,
        wheel=wheel,
        wheel_sha="b" * 64,
        runtime_dependency_lock=lock,
        versions={"pip": release_linux.PIP_BOOTSTRAP_VERSION},
    )

    assert manifest["runtime_profile"] == release_linux.RUNTIME_PROFILE
    assert not {
        "node_archive_filename",
        "node_archive_sha256",
        "pyright",
        "pyright_integrity",
        "pyright_lock_sha256",
        "semgrep",
        "semgrep_runtime_sha256",
    } & manifest.keys()

def test_linux_release_smoke_does_not_require_development_sqlglot() -> None:
    assert "sqlglot" not in release_linux._IMPORT_MODULES
    assert "nudenet" not in release_linux._IMPORT_MODULES


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


def test_new_virtual_environment_is_created_in_staging_then_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    sha = "e" * 40
    final_release = layout.releases / release_linux.release_id(sha)
    stale_release = _release(layout, release_linux.release_id("f" * 40))
    installed_at: list[Path] = []
    validated_wheels: list[Path] = []

    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(
        release_linux,
        "_prepare_pip_bootstrap",
        lambda workspace, **_kwargs: workspace / release_linux.PIP_BOOTSTRAP_FILENAME,
    )

    def build_wheel(_layout, workspace, **_kwargs):
        wheelhouse = workspace / "wheelhouse"
        wheelhouse.mkdir()
        wheel = wheelhouse / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
        wheel.write_bytes(b"project")
        return wheel

    def install_wheel(release_root, *_args, **_kwargs):
        installed_at.append(release_root)
        (release_root / "bin").mkdir(parents=True)
        command = release_root / "bin" / "Neocortex"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)

    monkeypatch.setattr(release_linux, "_build_wheel", build_wheel)
    monkeypatch.setattr(release_linux, "_install_wheel", install_wheel)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda release_root, *_args, **_kwargs: (
            {"pip": release_linux.PIP_BOOTSTRAP_VERSION}
            if release_root.parent.name.startswith(f"{release_linux.__version__}-")
            else pytest.fail("release validation used an unexpected path")
        ),
    )
    def validate_wheel(path: Path, **_kwargs: object) -> None:
        validated_wheels.append(path)

    monkeypatch.setattr(release_linux, "validate_release_artifact", validate_wheel)
    monkeypatch.setattr(release_linux, "_make_immutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_linux, "_require_immutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        release_linux,
        "_publish_public_access",
        lambda *_args, **_kwargs: ({}, {}),
    )

    corpus_root = tmp_path / "corpus"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    report = release_linux.install_release(
        layout,
        corpus_root=corpus_root,
        prepare_models=False,
        desktop=False,
        wheelhouse=wheelhouse,
    )

    assert len(installed_at) == 1
    assert len(validated_wheels) == 1
    assert installed_at[0] != final_release
    assert installed_at[0].parent.parent == layout.staging
    assert report["release_path"] == str(final_release)
    assert report["corpus_root"] == str(corpus_root)
    assert report["corpus_root_created"] is True
    assert corpus_root.is_dir()
    assert release_linux._current_target(layout) == final_release.resolve()
    assert not stale_release.exists()
    assert report["retention_policy"] == "current_and_immediate_rollback_v1"
    assert report["pruned_releases"] == (stale_release.name,)
    assert report["artifacts"]["runtime_dependency_lock_filename"] == (
        release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    )
    assert report["artifacts"]["runtime_dependency_count"] == 1


def test_virtualenv_console_shebangs_are_rebound_to_final_release(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "candidate"
    final = tmp_path / "releases" / release_linux.release_id("a" * 40)
    bin_directory = staging / "bin"
    bin_directory.mkdir(parents=True)
    script = bin_directory / "Neocortex"
    script.write_text(
        f"#!{staging}/bin/python\nprint('ok')\n",
        encoding="utf-8",
    )
    untouched = bin_directory / "plain.txt"
    untouched.write_text("data\n", encoding="utf-8")

    release_linux._rewrite_virtualenv_shebangs(staging, final)

    assert script.read_text(encoding="utf-8").startswith(f"#!{final}/bin/python\n")
    assert untouched.read_text(encoding="utf-8") == "data\n"


def test_virtualenv_metadata_drops_transient_direct_url_and_rebinds_cfg(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "candidate"
    final = tmp_path / "releases" / release_linux.release_id("b" * 40)
    (staging / "bin").mkdir(parents=True)
    (staging / "lib" / "python3.14" / "site-packages" / "demo-1.0.dist-info").mkdir(
        parents=True
    )
    direct = staging / "lib" / "python3.14" / "site-packages" / "demo-1.0.dist-info" / "direct_url.json"
    direct.write_text(
        '{"url": "file:///tmp/staging/demo.whl"}\n',
        encoding="utf-8",
    )
    record = direct.parent / "RECORD"
    record.write_text(
        "demo-1.0.dist-info/direct_url.json,sha256=abc,10\n"
        "demo.py,sha256=def,3\n",
        encoding="utf-8",
    )
    cfg = staging / "pyvenv.cfg"
    cfg.write_text(f"command = {staging}/bin/python -m venv {staging}\n", encoding="utf-8")
    activation = staging / "bin" / "activate"
    activation.write_text(f"VIRTUAL_ENV={staging}\n", encoding="utf-8")

    release_linux._rewrite_virtualenv_paths(staging, final)

    assert not direct.exists()
    assert record.read_text(encoding="utf-8") == "demo.py,sha256=def,3\n"
    assert str(staging) not in cfg.read_text(encoding="utf-8")
    assert str(final) in cfg.read_text(encoding="utf-8")
    assert activation.read_text(encoding="utf-8") == f"VIRTUAL_ENV={final}\n"


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
    old = _release(layout, release_linux.release_id("a" * 40))
    new = _release(layout, release_linux.release_id("b" * 40))
    _activate(layout, old)

    release_linux._replace_current(layout, new)

    assert release_linux._current_target(layout) == new.resolve()
    assert old.is_dir()
    assert new.is_dir()


def test_prune_old_releases_keeps_current_and_immediate_rollback(
    tmp_path: Path,
) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    rollback = _release(layout, release_linux.release_id("b" * 40))
    old = _release(layout, release_linux.release_id("c" * 40))
    old.chmod(0o555)

    pruned = release_linux._prune_old_releases(
        layout,
        current=current,
        rollback=rollback,
    )

    assert pruned == (old.name,)
    assert current.is_dir()
    assert rollback.is_dir()
    assert not old.exists()


def test_prune_old_releases_covers_all_supported_versions(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, "0.10.0-" + "a" * 12 + "-cp314-linux-x86_64")
    rollback = _release(layout, "0.9.0-" + "b" * 12 + "-cp314-linux-x86_64")
    old = _release(layout, "0.8.0-" + "c" * 12 + "-cp314-linux-x86_64")

    assert release_linux._prune_old_releases(layout, current=current, rollback=rollback) == (
        old.name,
    )
    assert current.is_dir() and rollback.is_dir()
    assert not old.exists()


def test_prune_old_releases_abstains_when_a_release_is_in_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    rollback = _release(layout, release_linux.release_id("b" * 40))
    old = _release(layout, release_linux.release_id("c" * 40))
    monkeypatch.setattr(release_linux, "_release_in_use", lambda _path: (1234,))

    with pytest.raises(release_linux.LinuxReleaseError, match="in use"):
        release_linux._prune_old_releases(
            layout,
            current=current,
            rollback=rollback,
        )

    assert old.is_dir()


def test_prune_old_releases_rejects_unsafe_matching_entries(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    rollback = _release(layout, release_linux.release_id("b" * 40))
    unsafe = layout.releases / "0.9.0-unsafe"
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.write_text("not a release", encoding="utf-8")

    with pytest.raises(release_linux.LinuxReleaseError, match="unknown entry"):
        release_linux._prune_old_releases(
            layout,
            current=current,
            rollback=rollback,
        )

    assert unsafe.is_file()


def test_retention_receipt_accepts_exact_current_and_rollback_pair(
    tmp_path: Path,
) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    rollback = _release(layout, release_linux.release_id("b" * 40))
    receipt = {
        "retention_policy": "current_and_immediate_rollback_v1",
        "previous_release": str(rollback),
        "retained_releases": [current.name, rollback.name],
    }

    release_linux._validate_retention_receipt(
        layout,
        current=current,
        receipt=receipt,
    )


def test_retention_receipt_rejects_an_unpruned_release(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40))
    rollback = _release(layout, release_linux.release_id("b" * 40))
    _release(layout, release_linux.release_id("c" * 40))
    receipt = {
        "retention_policy": "current_and_immediate_rollback_v1",
        "previous_release": str(rollback),
        "retained_releases": [current.name, rollback.name],
    }

    with pytest.raises(release_linux.LinuxReleaseError, match="retention drift"):
        release_linux._validate_retention_receipt(
            layout,
            current=current,
            receipt=receipt,
        )


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
    launcher_text = layout.launcher.read_text(encoding="utf-8")
    assert f"exec {release / 'bin' / 'Neocortex'} \"$@\"" in launcher_text
    assert "unset PYTHONPATH PYTHONHOME PYTHONUSERBASE PIP_CONFIG_FILE" in launcher_text


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
    old = _release(layout, release_linux.release_id("a" * 40))
    new = _release(layout, release_linux.release_id("b" * 40))
    _activate(layout, new)
    release_linux._make_immutable(old)
    release_linux._make_immutable(new)
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
    (source / "neocortex" / "interface" / "presentation" / "assets").mkdir(parents=True)
    _write_runtime_lock(source)
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40))
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
        lambda *_args, **_kwargs: {"pip": release_linux.PIP_BOOTSTRAP_VERSION},
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
            "pip": release_linux.PIP_BOOTSTRAP_VERSION,
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
            wheelhouse=tmp_path,
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
    old = _release(layout, release_linux.release_id("a" * 40))
    sha = "c" * 40
    candidate = _release(layout, release_linux.release_id(sha))
    _activate(layout, old)
    (candidate / release_linux.RELEASE_MANIFEST_NAME).write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: {"pip": release_linux.PIP_BOOTSTRAP_VERSION},
    )
    monkeypatch.setattr(
        release_linux,
        "_read_release_manifest",
        lambda *_args, **_kwargs: {
            "pip": release_linux.PIP_BOOTSTRAP_VERSION,
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
            wheelhouse=tmp_path,
            runner=fail_prepare,
        )

    assert release_linux._current_target(layout) == old.resolve()
    assert not layout.launcher.exists()
    assert not layout.alias.exists()
    assert not layout.desktop.exists()


def test_repromote_recovers_recorded_rollback_and_prunes_stale_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    rollback = _release(layout, release_linux.release_id("a" * 40))
    current = _release(layout, release_linux.release_id("b" * 40))
    stale = _release(layout, release_linux.release_id("c" * 40))
    _activate(layout, current)
    release_sha = "b" * 40

    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: release_sha)
    monkeypatch.setattr(
        release_linux,
        "_read_release_manifest",
        lambda *_args, **_kwargs: {"pip": release_linux.PIP_BOOTSTRAP_VERSION},
    )
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: {"pip": release_linux.PIP_BOOTSTRAP_VERSION},
    )
    monkeypatch.setattr(release_linux, "_publish_public_access", lambda *_args, **_kwargs: ({}, {}))
    release_linux._write_receipt(
        layout,
        {
            "schema_version": release_linux.RECEIPT_SCHEMA_VERSION,
            "kind": "linux_release_receipt",
            "operation": "install",
            "release_id": current.name,
            "release_path": str(current.resolve()),
            "previous_release": str(rollback.resolve()),
            "retention_policy": "current_and_immediate_rollback_v1",
            "retained_releases": [current.name, rollback.name],
            "pruned_releases": [],
            "result": "success",
        },
    )

    report = release_linux.install_release(
        layout,
        corpus_root=tmp_path / "corpus",
        prepare_models=False,
        desktop=False,
        wheelhouse=tmp_path,
    )

    assert report["operation"] == "repromote"
    assert report["previous_release"] == str(rollback.resolve())
    assert report["pruned_releases"] == (stale.name,)
    assert rollback.is_dir() and current.is_dir()
    assert not stale.exists()


def test_successful_rollback_records_evidence_and_retains_both_releases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40))
    new = _release(layout, release_linux.release_id("b" * 40))
    _activate(layout, new)
    release_linux._make_immutable(old)
    release_linux._make_immutable(new)

    report = release_linux.rollback_release(layout, target_release=old.name)

    assert report["operation"] == "rollback"
    assert report["previous_release"] == str(new.resolve())
    assert Path(str(report["receipt_path"])).is_file()
    assert release_linux._current_target(layout) == old.resolve()
    assert old.is_dir() and new.is_dir()


def test_rollback_prunes_stale_releases_and_repairs_launcher(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40))
    new = _release(layout, release_linux.release_id("b" * 40))
    stale = _release(layout, release_linux.release_id("c" * 40))
    _activate(layout, new)
    release_linux._make_immutable(old)
    release_linux._make_immutable(new)

    report = release_linux.rollback_release(layout, target_release=old.name)

    assert report["pruned_releases"] == (stale.name,)
    assert not stale.exists()
    assert layout.launcher.is_file()
    assert str(old / "bin" / "Neocortex") in layout.launcher.read_text(encoding="utf-8")


def test_rollback_receipt_failure_restores_gc_tombstones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40))
    new = _release(layout, release_linux.release_id("b" * 40))
    stale = _release(layout, release_linux.release_id("c" * 40))
    _activate(layout, new)
    release_linux._make_immutable(old)
    release_linux._make_immutable(new)
    monkeypatch.setattr(
        release_linux,
        "_write_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic receipt failure")),
    )

    with pytest.raises(OSError, match="synthetic receipt failure"):
        release_linux.rollback_release(layout, target_release=old.name)

    assert release_linux._current_target(layout) == new.resolve()
    assert stale.is_dir()
    assert not tuple(layout.staging.glob(".gc-*"))
