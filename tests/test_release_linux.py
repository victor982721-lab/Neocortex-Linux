"""Atomic Linux release activation, receipts, and rollback regressions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import venv
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.platform.policy import PlatformPolicy
from neocortex.platform import sqlite_runtime_attestation as sqlite_native
from tools import release_linux
from tools.release_linux import LinuxReleaseLayout


TEST_CAPABILITIES = ("base", 'platform')


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux release contract")
pytestmark = [pytestmark, pytest.mark.capability("base", 'platform')]
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
        inventory_backend="portable-full-scan",
        identity_backend="posix-st_dev-st_ino",
        path_collation="BINARY",
        containment_backend="posix-session-process-group-rlimit",
        elevation="not-required",
        mutation_backend="intentionally-unavailable",
        mutation_available=False,
        compatible=True,
    )


def _native_fixture() -> tuple[release_linux.ReleaseVerification, dict]:
    """Synthetic exact-build evidence; it never accredits a production binary."""
    attestation = {
        "schema": sqlite_native.ATTESTATION_SCHEMA,
        "python": {"implementation": "cpython", "version": "3.13.15", "cache_tag": "cpython-313",
                   "executable": {"sha256": "a" * 64}},
        "sqlite": {"version": "fixture", "source_id": "TEST FIXTURE ONLY",
                   "compile_options": ["ENABLE_FTS5"], "module": {"sha256": "b" * 64},
                   "native_libraries": {"mode": "process_maps", "files": [{"sha256": "c" * 64}]}},
        "capabilities": dict.fromkeys(sqlite_native.CAPABILITIES, True),
        "probe_sha256": "d" * 64,
    }
    attestation["identity_sha256"] = sqlite_native.canonical_sha256(sqlite_native.runtime_identity(attestation))
    policy = {
        "schema": sqlite_native.POLICY_SCHEMA, "policy_id": "TEST FIXTURE ONLY",
        "required_capabilities": list(sqlite_native.CAPABILITIES),
        "approved_builds": [{"identity_sha256": attestation["identity_sha256"], "evidence": {
            "basis": "upstream", "provider": "TEST FIXTURE", "build_reference": "fixture",
            "reviewed_on": "2026-09-18", "source_url": "https://vendor.example/test-only",
        }}],
    }
    interpreter = {"implementation": "cpython", "version": "3.13.15", "cache_tag": "cpython-313",
                   "executable": "bin/python"}
    record = sqlite_native.native_runtime_record(
        attestation, policy, expected_policy_sha256=sqlite_native.canonical_sha256(policy),
    )
    return release_linux.ReleaseVerification(release_linux.PIP_BOOTSTRAP_VERSION, interpreter, record), policy


def _fixture_policy_options(source: Path) -> dict:
    return {"sqlite_policy": source / sqlite_native.POLICY_FILENAME,
            "sqlite_policy_sha256": sqlite_native.canonical_sha256(_native_fixture()[1])}


def _fake_interpreter(root: Path) -> None:
    (root / "bin").mkdir(exist_ok=True)
    python = root / "bin" / "python"
    if not python.exists():
        python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        python.chmod(0o755)


def _release(layout: LinuxReleaseLayout, name: str, *, native: bool = False) -> Path:
    root = layout.releases / name
    (root / "bin").mkdir(parents=True)
    _fake_interpreter(root)
    command = root / "bin" / "Neocortex"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    if release_linux.parse_release_id(name) is not None:
        source_sha = release_linux.parse_release_id(name)[1].ljust(40, "0")
        manifest = {
            "schema_version": 1,
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
        if native:
            verification, _ = _native_fixture()
            lock = _write_runtime_lock(root)
            wheelhouse_parent = layout.releases.parent / "fixture-artifacts"
            wheelhouse_parent.mkdir(exist_ok=True)
            wheelhouse = wheelhouse_parent / "wheelhouse"
            if not wheelhouse.exists():
                _wheelhouse_fixture(wheelhouse_parent, ("pip", release_linux.PIP_BOOTSTRAP_VERSION))
            manifest = release_linux._release_manifest(
                release_name=name, source_sha=source_sha,
                wheel=Path(manifest["wheel_filename"]), wheel_sha="a" * 64,
                runtime_dependency_lock=lock, verification=verification,
                wheelhouse_provenance=release_linux._wheelhouse_provenance(
                    wheelhouse, release_linux._validate_wheelhouse(wheelhouse),
                ),
            )
            manifest["release_tree_sha256"] = release_linux._release_tree_digest(root)
        (root / release_linux.RELEASE_MANIFEST_NAME).write_text(
            json.dumps(manifest) + "\n", encoding="utf-8"
        )
    return root


def _activate(layout: LinuxReleaseLayout, release: Path) -> None:
    layout.current.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(release, layout.current.parent), layout.current)


def _write_runtime_lock(source: Path) -> Path:
    (source / sqlite_native.POLICY_FILENAME).write_text(json.dumps(_native_fixture()[1]), encoding="utf-8")
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
                "python": "cp313",
                "platform": "linux_x86_64",
                "artifacts": entries,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return wheelhouse


def _minimal_release_wheelhouse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Return a complete synthetic wheelhouse for preflight-only tests."""

    wheelhouse = _wheelhouse_fixture(
        tmp_path,
        ("pip", release_linux.PIP_BOOTSTRAP_VERSION),
        ("build", "1.5.0"),
        ("setuptools", "83.0.0"),
        ("wheel", "0.48.0"),
    )
    pip_wheel = next(wheelhouse.glob("pip-*.whl"))
    monkeypatch.setattr(
        release_linux,
        "PIP_BOOTSTRAP_SHA256",
        hashlib.sha256(pip_wheel.read_bytes()).hexdigest(),
    )
    return wheelhouse


def test_release_identifier_is_version_sha_python_and_platform_bound() -> None:
    assert release_linux.release_id("a" * 40) == (
        f"{release_linux.__version__}-{'a' * 12}-cp313-linux-x86_64"
    )
    with pytest.raises(ValueError):
        release_linux.release_id("A" * 40)


@pytest.mark.parametrize(
    "name",
    [
        "0.9.0-aaaaaaaaaaaa-cp313-linux-x86_64",
        "0.10.0-bbbbbbbbbbbb-cp313-linux-x86_64",
        "0.10.0-rc.1-" + "c" * 12 + "-cp313-linux-x86_64",
        "0.10.0-" + "d" * 40 + "-cp313-linux-x86_64",
    ],
)
def test_release_identifier_parser_accepts_current_and_legacy_abis(name: str) -> None:
    assert release_linux.parse_release_id(name) is not None
    assert release_linux.parse_release_id("0.9.0-backup") is None
    assert release_linux.parse_release_id(name.replace("cp313", "cp314")) is not None


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This unit checks the wheelhouse boundary, not the independently tested
    # production-interpreter boundary. No release can be prepared in this case.
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.delenv(release_linux.WHEELHOUSE_ENVIRONMENT, raising=False)
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    corpus = tmp_path / "corpus"

    with pytest.raises(release_linux.LinuxReleaseError, match="offline wheelhouse is required"):
        release_linux.install_release(
            layout,
        **_fixture_policy_options(source),
            corpus_root=corpus,
            prepare_models=False,
        )
    assert not corpus.exists()


@pytest.mark.parametrize("python_version", ((3, 13, 5), (3, 14, 0), (3, 13, 4)))
def test_reference_platform_guard_keeps_the_production_interpreter_contract(
    monkeypatch: pytest.MonkeyPatch, python_version: tuple[int, int, int],
) -> None:
    monkeypatch.setattr(release_linux, "sys", SimpleNamespace(
        platform="linux", implementation=SimpleNamespace(name="cpython"),
        version_info=python_version,
    ))
    monkeypatch.setattr(release_linux, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    if python_version == (3, 13, 5):
        release_linux._require_reference_platform()
    else:
        with pytest.raises(release_linux.LinuxReleaseError, match=r"CPython >=3\.13\.5,<3\.14"):
            release_linux._require_reference_platform()


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
            "schema_version": 1,
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
        if "ls-tree" in command:
            tracked = ""
            for path in ("constraints.txt", "neocortex/__init__.py", "pyproject.toml"):
                payload = (source / path).read_bytes()
                blob = hashlib.sha1(
                    f"blob {len(payload)}\0".encode() + payload,
                    usedforsecurity=False,
                ).hexdigest()
                tracked += f"100644 blob {blob}\t{path}\0"
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
        source_sha="a" * 40,
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


def test_linux_cp313_runtime_lock_is_exact_for_the_available_offline_closure() -> None:
    lock = PROJECT_ROOT / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    entries = release_linux._runtime_dependency_lock(lock)

    # The installed product is the authenticated full runtime closure,
    # including inference, audio, MCP, documents, and image adapters.
    assert len(entries) == 61
    assert entries["pip"] == release_linux.PIP_BOOTSTRAP_VERSION
    assert "neocortex-framework" not in entries
    assert all(name == release_linux._normalized_distribution_name(name) for name in entries)
    assert "nudenet" not in entries
    assert not {"ruff", "mypy", "coverage", "pip-audit"} & entries.keys()


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


@pytest.mark.parametrize("probe", ["dependencies", "sqlite"])
def test_isolated_release_probes_preserve_the_runtime_tree(tmp_path: Path, probe: str) -> None:
    """A writable venv exposes writes hidden by readonly release permissions."""
    root = tmp_path / "release"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(root)
    python = root / "bin" / "python"
    site = root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (site / "bytecode_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site / "bytecode_probe.pth").write_text("import bytecode_probe\n", encoding="utf-8")
    distribution = site / "pip-26.2.1.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pip\nVersion: 26.2.1\n", encoding="utf-8"
    )
    lock = tmp_path / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    lock.write_text("pip==26.2.1\n", encoding="utf-8")
    before = release_linux._release_tree_digest(root)

    def runner(arguments, *, timeout, environment):
        return subprocess.run(
            arguments, cwd=tmp_path, env=environment, timeout=timeout,
            text=True, capture_output=True, check=True,
        )

    if probe == "dependencies":
        release_linux._verify_runtime_dependency_lock(
            python, lock, runner=runner,
            environment={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    else:
        observed = sqlite_native.collect_release_sqlite_attestation(root)
        assert observed["schema"] == sqlite_native.ATTESTATION_SCHEMA
    assert release_linux._release_tree_digest(root) == before
    assert not tuple(site.rglob("*.pyc"))


def test_product_release_manifest_excludes_development_tool_metadata(tmp_path: Path) -> None:
    lock = _write_runtime_lock(tmp_path)
    _fake_interpreter(tmp_path)
    wheel = tmp_path / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    manifest = release_linux._release_manifest(
        release_name="candidate",
        source_sha="a" * 40,
        wheel=wheel,
        wheel_sha="b" * 64,
        runtime_dependency_lock=lock,
        verification=_native_fixture()[0],
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

    assert builders == [{"with_pip": False, "clear": False, "symlinks": False}]
    install = calls[0]
    assert install[1:4] == ("-I", "-c", release_linux._PIP_WHEEL_RUNNER)
    assert "-m" not in install
    assert "--no-index" in install
    assert "--no-deps" in install
    assert calls[1][1:3] == ("-I", "-c")


def test_real_release_venv_copies_provider_and_preserves_native_identity_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use real venv/native probes; only the pip wheel install is a fixture."""
    payload = b"synthetic verified pip wheel"
    wheel = tmp_path / "pip-safe-py3-none-any.whl"
    wheel.write_bytes(payload)
    monkeypatch.setattr(release_linux, "PIP_BOOTSTRAP_FILENAME", wheel.name)
    monkeypatch.setattr(release_linux, "PIP_BOOTSTRAP_SHA256", hashlib.sha256(payload).hexdigest())

    def pip_fixture(arguments, **_kwargs):
        return subprocess.CompletedProcess(arguments, 0, release_linux.PIP_BOOTSTRAP_VERSION + "\n", "")

    staging, final = tmp_path / "candidate", tmp_path / "release"
    release_linux._create_pip_environment(staging, wheel, runner=pip_fixture)
    executable_sha256 = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    for name in ("python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"):
        executable = staging / "bin" / name
        assert executable.is_file() and not executable.is_symlink()
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == executable_sha256
    release_linux._validate_release_tree(staging, expected_tree_sha256=None)
    before = sqlite_native.collect_release_sqlite_attestation(staging)
    release_linux._rewrite_virtualenv_paths(staging, final)
    tree_digest = release_linux._validate_release_tree(staging, expected_tree_sha256=None)
    staging.rename(final)
    assert release_linux._validate_release_tree(final, expected_tree_sha256=tree_digest) == tree_digest
    after = sqlite_native.collect_release_sqlite_attestation(final)
    assert after["python"]["invoked_executable"] == str(final / "bin" / "python")
    assert before["identity_sha256"] == after["identity_sha256"]
    assert after["python"]["executable"]["sha256"] == executable_sha256
    assert after["capabilities"] == dict.fromkeys(sqlite_native.CAPABILITIES, True)
    if after["sqlite"]["module"].get("kind") == "builtin":
        assert after["sqlite"]["module"]["sha256"] == executable_sha256

    # Even the measured provider may not be reintroduced as an arbitrary
    # external interpreter link in the finalized release tree.
    external_provider = tmp_path / "external-provider-python"
    shutil.copyfile(Path(sys.executable).resolve(), external_provider)
    interpreter = final / "bin" / "python3"
    interpreter.unlink()
    interpreter.symlink_to(external_provider)
    with pytest.raises(release_linux.LinuxReleaseError, match="unsafe symlink: bin/python3"):
        release_linux._validate_release_tree(final, expected_tree_sha256=None)


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


def test_corpus_root_preparation_rejects_ancestor_alias_before_creating_anything(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    existing = real / "existing"
    existing.mkdir(parents=True, mode=0o750)
    mode = existing.stat().st_mode
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(release_linux.LinuxReleaseError, match="real directory"):
        release_linux._prepare_corpus_root(alias / "existing" / "Corpus")

    assert not (existing / "Corpus").exists()
    assert not tuple(existing.iterdir())
    assert existing.stat().st_mode == mode
    assert alias.is_symlink() and alias.resolve() == real


@pytest.mark.parametrize("explicit_corpus,corrupt_candidate", [(False, False), (True, False), (False, True)])
def test_new_virtual_environment_is_created_in_staging_then_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_corpus: bool,
    corrupt_candidate: bool,
) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("setuptools==83.0.0\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    sha = "e" * 40
    final_release = layout.releases / release_linux.release_id(sha)
    stale_release = _release(layout, release_linux.release_id("f" * 40))
    if corrupt_candidate:
        candidate = _release(layout, final_release.name)
        (candidate / release_linux.RELEASE_MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
        release_linux._make_immutable(candidate)
    installed_at: list[Path] = []
    validated_wheels: list[Path] = []
    smoke_roots: list[Path] = []
    operational_roots: list[Path] = []
    corpus_root = tmp_path / "corpus" if explicit_corpus else layout.policy.corpus_root
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(tmp_path / "ambient-smoke-fixture"))

    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(
        release_linux,
        "_prepare_pip_bootstrap",
        lambda workspace, **_kwargs: workspace / release_linux.PIP_BOOTSTRAP_FILENAME,
    )

    def build_wheel(_layout, workspace, **_kwargs):
        assert _kwargs["source_sha"] == sha
        shutil.copytree(source, workspace / "source")
        (source / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME).write_text(
            "pip==0.0\n", encoding="utf-8",
        )
        (source / "constraints.txt").write_text("changed after staging\n", encoding="utf-8")
        wheelhouse = workspace / "wheelhouse"
        wheelhouse.mkdir()
        wheel = wheelhouse / f"neocortex_framework-{release_linux.__version__}-py3-none-any.whl"
        wheel.write_bytes(b"project")
        return wheel

    def install_wheel(release_root, _wheel, constraints, runtime_lock, **_kwargs):
        assert constraints.parent == release_root.parent / "source"
        assert constraints.read_text(encoding="utf-8") == "setuptools==83.0.0\n"
        assert runtime_lock.parent == constraints.parent
        assert runtime_lock.read_text(encoding="utf-8") == f"pip=={release_linux.PIP_BOOTSTRAP_VERSION}\n"
        installed_at.append(release_root)
        (release_root / "bin").mkdir(parents=True)
        # This fixture tests staging/publication; runtime execution is replaced
        # by verify_candidate below. Keep the executable and symlink structural
        # contract local, without requiring a host-wide CPython installation.
        interpreter = release_root / "bin" / "python3.13"
        interpreter.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        interpreter.chmod(0o755)
        (release_root / "bin" / "python").symlink_to("python3.13")
        command = release_root / "bin" / "Neocortex"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)

    monkeypatch.setattr(release_linux, "_build_wheel", build_wheel)
    monkeypatch.setattr(release_linux, "_install_wheel", install_wheel)
    def verify_candidate(release_root, _layout, smoke_root, **_kwargs):
        assert release_root == final_release or release_root.parent.name.startswith(f"{release_linux.__version__}-")
        assert smoke_root != corpus_root
        assert smoke_root.is_dir() and not tuple(smoke_root.iterdir())
        smoke_roots.append(smoke_root)
        return _native_fixture()[0]

    monkeypatch.setattr(release_linux, "_verify_python_release", verify_candidate)
    def validate_wheel(path: Path, **_kwargs: object) -> None:
        validated_wheels.append(path)

    monkeypatch.setattr(release_linux, "validate_release_artifact", validate_wheel)
    monkeypatch.setattr(release_linux, "_make_immutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_linux, "_require_immutable", lambda *_args, **_kwargs: None)
    def publish_access(_layout, selected_root, **_kwargs):
        operational_roots.append(selected_root)
        return {}, {}

    monkeypatch.setattr(release_linux, "_publish_public_access", publish_access)

    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    report = release_linux.install_release(
        layout,
        **_fixture_policy_options(source),
        corpus_root=corpus_root if explicit_corpus else None,
        prepare_models=False,
        wheelhouse=wheelhouse,
    )

    assert len(installed_at) == 1
    assert len(validated_wheels) == 1
    assert installed_at[0] != final_release
    assert installed_at[0].parent.parent == layout.staging
    assert report["release_path"] == str(final_release)
    assert report["corpus_root"] == str(corpus_root)
    assert report["corpus_root_source"] == (
        "explicit_install" if explicit_corpus else "platform_default"
    )
    assert operational_roots == [corpus_root]
    assert len(smoke_roots) == 2 and not smoke_roots[0].exists()
    assert str(smoke_roots[0]) not in json.dumps(report)
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
    (staging / "lib" / "python3.13" / "site-packages" / "demo-1.0.dist-info").mkdir(
        parents=True
    )
    direct = staging / "lib" / "python3.13" / "site-packages" / "demo-1.0.dist-info" / "direct_url.json"
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
    current = _release(layout, "0.10.0-" + "a" * 12 + "-cp313-linux-x86_64")
    rollback = _release(layout, "0.9.0-" + "b" * 12 + "-cp313-linux-x86_64")
    old = _release(layout, "0.8.0-" + "c" * 12 + "-cp313-linux-x86_64")

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


@pytest.mark.parametrize("override", [None, "", "smoke"])
def test_launcher_preserves_process_corpus_override_without_persisting_it(
    tmp_path: Path,
    override: str | None,
) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    release = _release(layout, "active")
    (release / "bin" / "Neocortex").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$NEOCORTEX_CORPUS_ROOT" "$1"\n',
        encoding="utf-8",
    )
    _activate(layout, release)
    operational_root = tmp_path / "Operational ' corpus $HOME"
    release_linux._publish_public_access(layout, operational_root)
    environment = dict(os.environ)
    environment.pop("NEOCORTEX_CORPUS_ROOT", None)
    smoke_root = tmp_path / "smoke corpus"
    if override is not None:
        environment["NEOCORTEX_CORPUS_ROOT"] = str(smoke_root) if override else ""
    before = layout.launcher.read_bytes()

    completed = subprocess.run(
        (layout.alias, "argument with spaces"),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    expected_root = smoke_root if override else operational_root
    assert completed.stdout.splitlines() == [str(expected_root), "argument with spaces"]
    assert layout.launcher.read_bytes() == before
    assert str(smoke_root).encode() not in before


def test_release_parser_distinguishes_persistent_and_expected_corpus() -> None:
    parser = release_linux.build_parser()

    assert parser.parse_args(["install"]).corpus_root is None
    assert parser.parse_args(["install", "--corpus-root", "/custom"]).corpus_root == Path("/custom")
    assert parser.parse_args(["verify"]).expected_corpus_root is None
    assert parser.parse_args(["verify", "--corpus-root", "/custom"]).expected_corpus_root == Path(
        "/custom"
    )


@pytest.fixture
def verification_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LinuxReleaseLayout:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    sha = ("a" * 12).ljust(40, "0")
    current = _release(layout, release_linux.release_id(sha))
    _activate(layout, current)
    layout.policy.corpus_root.mkdir(parents=True)
    _, hashes = release_linux._publish_public_access(layout, layout.policy.corpus_root)
    release_linux._write_receipt(
        layout,
        {
            "schema_version": 1,
            "kind": "linux_release_receipt",
            "operation": "install",
            "release_id": current.name,
            "release_path": str(current),
            "source_sha": sha,
            "corpus_root": str(layout.policy.corpus_root),
            "corpus_root_source": "platform_default",
            "artifacts": hashes,
            "result": "success",
        },
    )
    release_linux._make_immutable(current)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_require_executable", lambda name: name)
    return layout


@pytest.mark.parametrize("effective_path_report", [False, True])
def test_verification_keeps_probe_corpus_ephemeral_and_public_configuration_unchanged(
    verification_layout: LinuxReleaseLayout,
    effective_path_report: bool,
) -> None:
    layout = verification_layout
    before = {path: path.read_bytes() for path in [layout.launcher, *layout.receipts.glob("*.json")]}
    smoke_roots: set[Path] = set()

    def runner(command, **kwargs):
        parts = tuple(map(str, command))
        environment = kwargs.get("environment")
        if environment is not None:
            root = Path(environment["NEOCORTEX_CORPUS_ROOT"])
            assert root != layout.policy.corpus_root
            assert root.is_dir() and not tuple(root.iterdir())
            smoke_roots.add(root)
        output = "{}"
        if parts[-1] == "import pip; print(pip.__version__)":
            output = release_linux.PIP_BOOTSTRAP_VERSION
        elif Path(parts[0]).name == "Neocortex" and parts[1:] == ("--version",):
            output = f"Neocortex {release_linux.__version__}"
        elif parts[1:] == ("doctor", "platform", "--json") and effective_path_report:
            output = json.dumps({"effective_paths": {"corpus": environment["NEOCORTEX_CORPUS_ROOT"]}})
        elif parts[0] in {"qpdf", "ffprobe"}:
            output = f"{parts[0]} fixture version\n"
        elif parts[0] == "tesseract":
            output = "Available languages:\neng\nspa\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    for _ in range(2):
        report = release_linux.verify_release(
            layout, expected_corpus_root=layout.policy.corpus_root, runner=runner,
        )
        assert report["verified"] is False
        assert report["native_runtime"]["status"] == "legacy_unaccredited"
        assert report["corpus_root"] == str(layout.policy.corpus_root)
        assert report["verification_corpus_policy"] == "ephemeral_empty_v1"
        assert report["verification_effective_corpus_checked"] is effective_path_report

    assert len(smoke_roots) == 2 and all(not root.exists() for root in smoke_roots)
    assert all(path.read_bytes() == payload for path, payload in before.items())


def test_verification_rejects_a_different_requested_corpus_before_execution(
    verification_layout: LinuxReleaseLayout,
) -> None:
    layout = verification_layout
    with pytest.raises(release_linux.LinuxReleaseError, match="requested operational root"):
        release_linux.verify_release(
            layout,
            expected_corpus_root=layout.policy.corpus_root.parent / "other",
            runner=lambda *_args, **_kwargs: pytest.fail("drifted install executed"),
        )


@pytest.mark.parametrize("drift", ["legacy_override", "wrong_corpus", "wrong_release"])
def test_verification_rejects_receipt_matched_launcher_drift_before_execution(
    verification_layout: LinuxReleaseLayout,
    drift: str,
) -> None:
    layout = verification_layout
    latest = release_linux._latest_receipt(layout)
    assert latest is not None
    if drift == "legacy_override":
        layout.launcher.write_text(
            "#!/bin/sh\nexport NEOCORTEX_CORPUS_ROOT=/tmp/old-smoke\nexit 0\n",
            encoding="utf-8",
        )
    elif drift == "wrong_corpus":
        payload = layout.launcher.read_text(encoding="utf-8")
        layout.launcher.write_text(
            payload.replace(str(layout.policy.corpus_root), "/tmp/other-corpus"),
            encoding="utf-8",
        )
    else:
        latest["release_id"] = release_linux.release_id("b" * 40)
    latest["artifacts"]["launcher_sha256"] = release_linux._sha256_file(layout.launcher)
    Path(latest["_path"]).write_text(json.dumps(latest), encoding="utf-8")

    with pytest.raises(release_linux.LinuxReleaseError, match=r"configuration|active release"):
        release_linux.verify_release(
            layout, runner=lambda *_args, **_kwargs: pytest.fail("drifted install executed"),
        )


def test_verification_rejects_an_effective_corpus_outside_the_smoke_fixture(
    verification_layout: LinuxReleaseLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: _native_fixture()[0],
    )

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0,
            stdout=json.dumps({"effective_paths": {"corpus": "/unexpected-corpus"}}),
            stderr="",
        )

    with pytest.raises(release_linux.LinuxReleaseError, match="isolated verification corpus"):
        release_linux.verify_release(verification_layout, runner=runner)


def test_failed_rollback_receipt_restores_the_prior_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40), native=True)
    new = _release(layout, release_linux.release_id("b" * 40), native=True)
    _activate(layout, new)
    monkeypatch.setattr(release_linux, "_verify_python_release", lambda *_a, **_k: _native_fixture()[0])
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
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("pip==26.2.1\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    old = _release(layout, release_linux.release_id("a" * 40), native=True)
    sha = ("b" * 12).ljust(40, "0")
    new = _release(layout, release_linux.release_id(sha), native=True)
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
        lambda *_args, **_kwargs: _native_fixture()[0],
    )
    monkeypatch.setattr(
        release_linux,
        "_write_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic receipt failure")),
    )

    for fixture_release in layout.releases.iterdir():
        release_linux._make_immutable(fixture_release)
    with pytest.raises(OSError, match="synthetic receipt failure"):
        release_linux.install_release(
            layout,
        **_fixture_policy_options(source),
            corpus_root=tmp_path / "corpus",
            prepare_models=False,
            wheelhouse=wheelhouse,
        )

    assert release_linux._current_target(layout) == old.resolve()
    assert layout.launcher.read_bytes() == b"old launcher\n"
    assert layout.alias.is_symlink()
    assert os.readlink(layout.alias) == "old-target"
    assert new.is_dir()


@pytest.mark.parametrize("manifest_state", ["missing", "corrupt"])
def test_install_preserves_active_release_when_its_manifest_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_state: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("pip==26.2.1\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    sha = "a" * 40
    current = _release(layout, release_linux.release_id(sha))
    manifest = current / release_linux.RELEASE_MANIFEST_NAME
    if manifest_state == "missing":
        manifest.unlink()
    else:
        manifest.write_text("{}\n", encoding="utf-8")
    release_linux._make_immutable(current)
    _activate(layout, current)
    command_bytes = (current / "bin" / "Neocortex").read_bytes()
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(
        release_linux,
        "_build_wheel",
        lambda *_args, **_kwargs: pytest.fail("unsafe rebuild of the current release"),
    )

    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    with pytest.raises(release_linux.LinuxReleaseError, match=r"manifest|identity"):
        release_linux.install_release(
            layout,
        **_fixture_policy_options(source),
            prepare_models=False,
            wheelhouse=wheelhouse,
        )

    assert current.is_dir()
    assert layout.current.is_symlink()
    assert release_linux._current_target(layout) == current.resolve()
    assert (current / "bin" / "Neocortex").read_bytes() == command_bytes
    assert manifest.exists() is (manifest_state != "missing")
    if manifest_state == "corrupt":
        assert manifest.read_bytes() == b"{}\n"


def test_install_preserves_noncurrent_manifestless_release_used_by_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("pip==26.2.1\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    sha = "b" * 40
    candidate = _release(layout, release_linux.release_id(sha))
    (candidate / release_linux.RELEASE_MANIFEST_NAME).unlink()
    current = _release(layout, release_linux.release_id("a" * 40))
    _activate(layout, current)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_release_in_use", lambda root: (4242,) if root == candidate else ())

    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    with pytest.raises(release_linux.LinuxReleaseError, match="in use by host processes"):
        release_linux.install_release(
            layout,
            prepare_models=False,
            wheelhouse=wheelhouse,
            **_fixture_policy_options(source),
        )

    assert candidate.is_dir() and (candidate / "bin" / "Neocortex").is_file()
    assert release_linux._current_target(layout) == current.resolve()


def test_missing_offline_models_never_promote_or_trigger_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("pip==26.2.1\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    old = _release(layout, release_linux.release_id("a" * 40), native=True)
    sha = ("c" * 12).ljust(40, "0")
    _release(layout, release_linux.release_id(sha), native=True)
    _activate(layout, old)

    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: sha)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: _native_fixture()[0],
    )

    smoke_roots: list[Path] = []

    def fail_prepare(arguments, **_kwargs):
        assert tuple(map(str, arguments))[-3:] == ("models", "status", "--json")
        smoke_root = Path(_kwargs["environment"]["NEOCORTEX_CORPUS_ROOT"])
        assert smoke_root != tmp_path / "corpus"
        assert smoke_root.is_dir() and not tuple(smoke_root.iterdir())
        smoke_roots.append(smoke_root)
        raise release_linux.LinuxReleaseError("synthetic incomplete model cache")

    for fixture_release in layout.releases.iterdir():
        release_linux._make_immutable(fixture_release)
    with pytest.raises(release_linux.LinuxReleaseError, match="incomplete model cache"):
        release_linux.install_release(
            layout,
        **_fixture_policy_options(source),
            corpus_root=tmp_path / "corpus",
            prepare_models=True,
            wheelhouse=wheelhouse,
            runner=fail_prepare,
        )

    assert release_linux._current_target(layout) == old.resolve()
    assert not layout.launcher.exists()
    assert not layout.alias.exists()
    assert len(smoke_roots) == 1 and not smoke_roots[0].exists()


def test_repromote_recovers_recorded_rollback_and_prunes_stale_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("pip==26.2.1\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    rollback = _release(layout, release_linux.release_id("a" * 40), native=True)
    current = _release(layout, release_linux.release_id("b" * 40), native=True)
    stale = _release(layout, release_linux.release_id("c" * 40), native=True)
    _activate(layout, current)
    release_sha = ("b" * 12).ljust(40, "0")

    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_args, **_kwargs: release_sha)
    monkeypatch.setattr(
        release_linux,
        "_verify_python_release",
        lambda *_args, **_kwargs: _native_fixture()[0],
    )
    monkeypatch.setattr(release_linux, "_publish_public_access", lambda *_args, **_kwargs: ({}, {}))
    release_linux._write_receipt(
        layout,
        {
            "schema_version": 1,
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

    for fixture_release in layout.releases.iterdir():
        release_linux._make_immutable(fixture_release)
    report = release_linux.install_release(
        layout,
        **_fixture_policy_options(source),
        corpus_root=tmp_path / "corpus",
        prepare_models=False,
        wheelhouse=wheelhouse,
    )

    assert report["operation"] == "repromote"
    assert report["previous_release"] == str(rollback.resolve())
    assert report["pruned_releases"] == (stale.name,)
    assert rollback.is_dir() and current.is_dir()
    assert not stale.exists()


def test_successful_rollback_records_evidence_and_retains_both_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40), native=True)
    new = _release(layout, release_linux.release_id("b" * 40), native=True)
    _activate(layout, new)
    monkeypatch.setattr(release_linux, "_verify_python_release", lambda *_a, **_k: _native_fixture()[0])
    release_linux._make_immutable(old)
    release_linux._make_immutable(new)

    report = release_linux.rollback_release(layout, target_release=old.name)

    assert report["operation"] == "rollback"
    assert report["previous_release"] == str(new.resolve())
    assert Path(str(report["receipt_path"])).is_file()
    assert release_linux._current_target(layout) == old.resolve()
    assert old.is_dir() and new.is_dir()


def test_rollback_prunes_stale_releases_and_repairs_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40), native=True)
    new = _release(layout, release_linux.release_id("b" * 40), native=True)
    stale = _release(layout, release_linux.release_id("c" * 40), native=True)
    _activate(layout, new)
    monkeypatch.setattr(release_linux, "_verify_python_release", lambda *_a, **_k: _native_fixture()[0])
    release_linux._make_immutable(old)
    release_linux._make_immutable(new)

    report = release_linux.rollback_release(layout, target_release=old.name)

    assert report["pruned_releases"] == (stale.name,)
    assert not stale.exists()
    assert layout.launcher.is_file()
    assert str(old / "bin" / "Neocortex") in layout.launcher.read_text(encoding="utf-8")


def test_rollback_receipt_failure_restores_gc_tombstones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation",
                        lambda *_a, **_k: _native_fixture()[0].native_runtime["attestation"])
    source = tmp_path / "source"
    source.mkdir()
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40), native=True)
    new = _release(layout, release_linux.release_id("b" * 40), native=True)
    stale = _release(layout, release_linux.release_id("c" * 40), native=True)
    _activate(layout, new)
    monkeypatch.setattr(release_linux, "_verify_python_release", lambda *_a, **_k: _native_fixture()[0])
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


@pytest.mark.parametrize("fault", ["missing", "changed", "wrong_digest"])
def test_v2_manifest_rejects_native_tampering_before_execution(tmp_path: Path, fault: str) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    root = _release(layout, release_linux.release_id("a" * 40), native=True)
    path = root / release_linux.RELEASE_MANIFEST_NAME
    manifest = json.loads(path.read_text())
    if fault == "missing":
        manifest.pop("native_runtime")
    elif fault == "changed":
        manifest["native_runtime"]["attestation"]["sqlite"]["source_id"] = "changed library"
        manifest["native_runtime_sha256"] = sqlite_native.canonical_sha256(manifest["native_runtime"])
    else:
        manifest["native_runtime_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    release_linux._make_immutable(root)
    with pytest.raises(release_linux.LinuxReleaseError, match=r"native runtime|SQLite runtime"):
        release_linux._read_release_manifest(root, release_name=root.name, source_sha=manifest["source_sha"])


@pytest.mark.parametrize("contents", ['{"schema_version":2}', '{broken', '{"schema_version":99}'])
def test_latest_receipt_never_falls_back_over_a_damaged_newer_receipt(tmp_path: Path, contents: str) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    layout.receipts.mkdir(parents=True)
    (layout.receipts / "001.json").write_text('{"schema_version":1}', encoding="utf-8")
    (layout.receipts / "002.json").write_text(contents, encoding="utf-8")
    with pytest.raises(release_linux.LinuxReleaseError, match="latest"):
        release_linux._latest_receipt(layout)


def test_legacy_rollback_is_readable_but_never_promoted(tmp_path: Path) -> None:
    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    old = _release(layout, release_linux.release_id("a" * 40))
    current = _release(layout, release_linux.release_id("b" * 40))
    _activate(layout, current)
    before = (old / release_linux.RELEASE_MANIFEST_NAME).read_bytes()
    release_linux._make_immutable(old)
    release_linux._make_immutable(current)
    with pytest.raises(release_linux.LinuxReleaseError, match="legacy_unaccredited"):
        release_linux.rollback_release(layout, target_release=old.name)
    assert release_linux._current_target(layout) == current.resolve()
    assert (old / release_linux.RELEASE_MANIFEST_NAME).read_bytes() == before


@pytest.mark.parametrize("fault", ["absent_pin", "changed_between_preflights"])
def test_sqlite_policy_preflight_fails_before_corpus_or_activation(tmp_path: Path, monkeypatch, fault: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_runtime_lock(source)
    (source / "constraints.txt").write_text("pip==26.2.1\n", encoding="utf-8")
    layout = LinuxReleaseLayout(source, _policy(tmp_path))
    wheelhouse = _minimal_release_wheelhouse(tmp_path, monkeypatch)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_source_sha", lambda *_a: "a" * 40)
    options = _fixture_policy_options(source)
    if fault == "absent_pin":
        options = {}
    else:
        real_preflight = release_linux._preflight_install
        calls = []

        def preflight(*args, **kwargs):
            result = real_preflight(*args, **kwargs)
            calls.append(result)
            altered = dict(_native_fixture()[1])
            altered["approved_builds"] = []
            (source / sqlite_native.POLICY_FILENAME).write_text(json.dumps(altered), encoding="utf-8")
            return result

        monkeypatch.setattr(release_linux, "_preflight_install", preflight)
    with pytest.raises(release_linux.LinuxReleaseError, match=r"policy|trusted binding"):
        release_linux.install_release(
            layout,
            prepare_models=False,
            wheelhouse=wheelhouse,
            **options,
        )
    assert not layout.policy.corpus_root.exists()
    assert not layout.current.exists()
    assert not layout.launcher.exists()


def test_native_replacement_at_activation_keeps_current_and_rollback(tmp_path: Path, monkeypatch) -> None:
    import copy

    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    target = _release(layout, release_linux.release_id("a" * 40), native=True)
    current = _release(layout, release_linux.release_id("b" * 40), native=True)
    _activate(layout, current)
    release_linux._make_immutable(target)
    release_linux._make_immutable(current)
    monkeypatch.setattr(release_linux, "_verify_python_release", lambda *_a, **_k: _native_fixture()[0])
    changed = copy.deepcopy(_native_fixture()[0].native_runtime["attestation"])
    changed["sqlite"]["native_libraries"]["files"][0]["sha256"] = "f" * 64
    changed["identity_sha256"] = sqlite_native.canonical_sha256(sqlite_native.runtime_identity(changed))
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation", lambda *_a: changed)
    with pytest.raises(release_linux.LinuxReleaseError, match="not approved"):
        release_linux.rollback_release(layout, target_release=target.name)
    assert release_linux._current_target(layout) == current.resolve()
    assert current.is_dir() and target.is_dir()
    assert not layout.launcher.exists()
    assert not tuple(layout.receipts.glob("*.json"))


def test_loader_overrides_are_stripped_by_environment_and_real_wrapper(tmp_path: Path) -> None:
    names = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")
    environment = release_linux._offline_environment(dict.fromkeys(names, "/invalid/fixture"))
    assert not set(names) & environment.keys()
    assert environment["HF_HUB_OFFLINE"] == "1"
    root = tmp_path / "release"
    (root / "bin").mkdir(parents=True)
    program = root / "bin" / "Neocortex"
    program.write_text('#!/bin/sh\n[ -z "${LD_PRELOAD+x}${LD_LIBRARY_PATH+x}${LD_AUDIT+x}" ]\n', encoding="utf-8")
    program.chmod(0o755)
    launcher = tmp_path / "launcher"
    launcher.write_bytes(release_linux._launcher_payload(tmp_path / "corpus", root))
    launcher.chmod(0o755)
    # Empty loader values avoid loading any library before the shell can unset
    # them, while ${VAR+x} proves that the wrapper removes the variables.
    result = subprocess.run([launcher], env={**os.environ, **dict.fromkeys(names, "")}, timeout=10)
    assert result.returncode == 0


@pytest.mark.parametrize("fault", [None, "receipt", "policy", "live_library"])
def test_v2_public_verify_binds_receipt_policy_and_current_measurement(tmp_path: Path, monkeypatch, fault: str | None) -> None:
    import copy

    layout = LinuxReleaseLayout(tmp_path / "source", _policy(tmp_path))
    current = _release(layout, release_linux.release_id("a" * 40), native=True)
    _activate(layout, current)
    layout.policy.corpus_root.mkdir(parents=True)
    _, public = release_linux._publish_public_access(layout, layout.policy.corpus_root)
    manifest = json.loads((current / release_linux.RELEASE_MANIFEST_NAME).read_text())
    receipt = {
        "schema_version": 2, "kind": "linux_release_receipt", "operation": "install",
        "release_id": current.name, "release_path": str(current), "source_sha": manifest["source_sha"],
        "current_link": str(layout.current), "corpus_root": str(layout.policy.corpus_root),
        "interpreter": manifest["interpreter"], "wheelhouse_provenance": manifest["wheelhouse_provenance"],
        "native_runtime": manifest["native_runtime"], "native_runtime_sha256": manifest["native_runtime_sha256"],
        "artifacts": {**manifest, **public,
                      "release_manifest_sha256": release_linux._sha256_file(current / release_linux.RELEASE_MANIFEST_NAME)},
        "result": "success",
    }
    if fault == "receipt":
        receipt["native_runtime_sha256"] = "0" * 64
    receipt_path = release_linux._write_receipt(layout, receipt)
    if fault == "policy":
        policy_path = current / sqlite_native.POLICY_FILENAME
        policy = json.loads(policy_path.read_text())
        policy["approved_builds"] = []
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
    release_linux._make_immutable(current)
    observed = copy.deepcopy(_native_fixture()[0].native_runtime["attestation"])
    if fault == "live_library":
        observed["sqlite"]["native_libraries"]["files"][0]["sha256"] = "e" * 64
        observed["identity_sha256"] = sqlite_native.canonical_sha256(sqlite_native.runtime_identity(observed))
    monkeypatch.setattr(sqlite_native, "collect_release_sqlite_attestation", lambda *_a: observed)
    monkeypatch.setattr(release_linux, "_require_reference_platform", lambda: None)
    monkeypatch.setattr(release_linux, "_require_executable", lambda name: name)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        parts = tuple(map(str, command))
        output = "{}"
        if parts[-1] == "import pip; print(pip.__version__)":
            output = release_linux.PIP_BOOTSTRAP_VERSION
        elif parts[-1] == release_linux._RUNTIME_INVENTORY_SCRIPT:
            output = json.dumps({"pip": release_linux.PIP_BOOTSTRAP_VERSION})
        elif Path(parts[0]).name == "Neocortex" and parts[1:] == ("--version",):
            output = f"Neocortex {release_linux.__version__}"
        elif parts[1:] == ("doctor", "platform", "--json"):
            output = json.dumps({"effective_paths": {"corpus": kwargs["environment"]["NEOCORTEX_CORPUS_ROOT"]}})
        elif parts[0] in {"qpdf", "ffprobe"}:
            output = "fixture version\n"
        elif parts[0] == "tesseract":
            output = "Available languages:\neng\nspa\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    if fault is None:
        report = release_linux.verify_release(layout, runner=runner)
        assert report["schema_version"] == 2
        assert report["verified"] is True
        assert report["native_runtime"]["status"] == "approved"
        assert report["native_runtime"]["observed"]["identity_sha256"] == observed["identity_sha256"]
    else:
        with pytest.raises(release_linux.LinuxReleaseError):
            release_linux.verify_release(layout, runner=runner)
        if fault in {"receipt", "policy"}:
            assert calls == [], "stored evidence is checked before any runtime command"
    assert release_linux._current_target(layout) == current.resolve()
    assert receipt_path.is_file()
