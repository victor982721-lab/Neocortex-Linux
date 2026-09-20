"""Focused supply-chain and release-boundary regressions."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tools import pip_bootstrap, release_linux


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux release contract")


def _wheelhouse(tmp_path: Path) -> Path:
    root = tmp_path / "wheelhouse"
    root.mkdir()
    entries: list[dict[str, str]] = []
    for name, version in (("pip", "26.2.1"), ("build", "1.5.0")):
        normalized = name.replace("-", "_")
        filename = f"{normalized}-{version}-py3-none-any.whl"
        wheel = root / filename
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
    (root / release_linux.WHEELHOUSE_MANIFEST_NAME).write_text(
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
    return root


def test_wheelhouse_provenance_binds_manifest_and_sorted_artifact_set(tmp_path: Path) -> None:
    root = _wheelhouse(tmp_path)
    artifacts = release_linux._validate_wheelhouse(root)
    provenance = release_linux._wheelhouse_provenance(root, artifacts)

    assert provenance["manifest_sha256"] == release_linux._sha256_file(
        root / release_linux.WHEELHOUSE_MANIFEST_NAME
    )
    assert provenance["artifact_count"] == 2
    assert release_linux._validate_wheelhouse_provenance(provenance) == provenance

    rows = provenance["artifacts"]
    assert isinstance(rows, list)
    rows.reverse()
    with pytest.raises(release_linux.LinuxReleaseError, match="canonically ordered"):
        release_linux._validate_wheelhouse_provenance(provenance)


def test_runtime_lock_accepts_hashes_and_binds_canonical_wheelhouse() -> None:
    lock = Path(__file__).resolve().parents[1] / release_linux.RUNTIME_DEPENDENCY_LOCK_NAME
    entries = release_linux._runtime_dependency_lock(lock)
    hashes = release_linux._runtime_dependency_hashes(lock)

    assert len(entries) == 61
    assert len(hashes) == len(entries)
    assert all(value is not None for value in hashes.values())
    assert hashes["pip"] == pip_bootstrap.PIP_BOOTSTRAP_SHA256


def test_default_bootstrap_fails_closed_before_any_network_request(tmp_path: Path) -> None:
    with pytest.raises(pip_bootstrap.PipBootstrapError, match="implicit network bootstrap"):
        pip_bootstrap.bootstrap_python(tmp_path / "python", tmp_path)


def test_reproducibility_helper_requires_distinct_equal_outputs(tmp_path: Path) -> None:
    first = tmp_path / "first.whl"
    second = tmp_path / "second.whl"
    first.write_bytes(b"same wheel bytes")
    second.write_bytes(b"same wheel bytes")

    digest = release_linux.compare_reproducible_builds(first, second)

    assert digest == hashlib.sha256(first.read_bytes()).hexdigest()
    second.write_bytes(b"different wheel bytes")
    with pytest.raises(release_linux.LinuxReleaseError, match="not byte-identical"):
        release_linux.compare_reproducible_builds(first, second)


def test_preflight_rejects_missing_wheelhouse_without_creating_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"

    with pytest.raises(release_linux.LinuxReleaseError, match="offline wheelhouse"):
        release_linux._resolve_wheelhouse(tmp_path / "missing-wheelhouse")

    assert not corpus.exists()


def test_source_manifest_binds_git_tree_lock_and_constraints() -> None:
    source_sha = "a" * 40
    blobs = {
        "constraints.txt": ("100644", "b" * 40),
        release_linux.RUNTIME_DEPENDENCY_LOCK_NAME: ("100644", "c" * 40),
    }
    manifest = release_linux._source_manifest(
        source_sha,
        blobs,
        runtime_dependency_lock_sha256="d" * 64,
        constraints_sha256="e" * 64,
    )

    assert (
        release_linux._validate_source_manifest(
            manifest,
            expected_source_sha=source_sha,
        )
        == manifest
    )
    altered = dict(manifest)
    altered["constraints_sha256"] = "f" * 64
    with pytest.raises(release_linux.LinuxReleaseError, match="digest differs"):
        release_linux._validate_source_manifest(altered)


def test_release_fixture_manifests_are_self_consistent() -> None:
    root = Path(__file__).parent / "fixtures" / "release"
    source = json.loads((root / "source-manifest-v1.json").read_text())
    source_sha = source["source_sha"]
    assert (
        release_linux._validate_source_manifest(
            source,
            expected_source_sha=source_sha,
        )
        == source
    )
    reproducibility = json.loads((root / "reproducibility-v1.json").read_text())
    assert (
        release_linux._validate_reproducibility_metadata(
            reproducibility,
            expected_source_sha=source_sha,
            expected_source_manifest_sha256=source["manifest_sha256"],
        )
        == reproducibility
    )


def test_reproducible_build_wrapper_runs_two_independent_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sha = "a" * 40
    source_manifest = release_linux._source_manifest(
        source_sha,
        {
            "constraints.txt": ("100644", "b" * 40),
            release_linux.RUNTIME_DEPENDENCY_LOCK_NAME: ("100644", "c" * 40),
        },
        runtime_dependency_lock_sha256="d" * 64,
        constraints_sha256="e" * 64,
    )
    outputs: list[Path] = []

    def fake_build(_layout, workspace, **_kwargs):
        output = workspace / "wheelhouse" / "neocortex_framework-0.12.0-py3-none-any.whl"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"identical wheel")
        outputs.append(workspace)
        return output

    monkeypatch.setattr(release_linux, "_build_wheel_once", fake_build)
    monkeypatch.setattr(
        release_linux,
        "validate_release_artifact",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        release_linux,
        "compare_logical_payloads",
        lambda *_args, **_kwargs: SimpleNamespace(sha256="f" * 64),
    )

    result = release_linux._build_wheel(
        cast(release_linux.LinuxReleaseLayout, SimpleNamespace()),
        tmp_path / "workspace",
        source_sha=source_sha,
        pip_wheel=tmp_path / "pip.whl",
        wheelhouse=tmp_path / "wheelhouse",
        source_manifest=source_manifest,
    )

    assert len(outputs) == 2
    assert outputs[0] != outputs[1]
    assert result == outputs[0] / "wheelhouse" / "neocortex_framework-0.12.0-py3-none-any.whl"
    metadata = json.loads(
        (tmp_path / "workspace" / release_linux.REPRODUCIBILITY_METADATA_NAME).read_text()
    )
    source_manifest_sha = source_manifest["manifest_sha256"]
    assert isinstance(source_manifest_sha, str)
    assert (
        release_linux._validate_reproducibility_metadata(
            metadata,
            expected_source_sha=source_sha,
            expected_source_manifest_sha256=source_manifest_sha,
        )
        == metadata
    )


def test_live_wheelhouse_revalidation_rejects_post_install_drift(tmp_path: Path) -> None:
    root = _wheelhouse(tmp_path)
    artifacts = release_linux._validate_wheelhouse(root)
    provenance = release_linux._wheelhouse_provenance(root, artifacts)
    release_linux._revalidate_wheelhouse(root, provenance)

    wheel = next(root.glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"drift")
    with pytest.raises(release_linux.LinuxReleaseError, match="hash mismatch"):
        release_linux._revalidate_wheelhouse(root, provenance)


def test_real_offline_bootstrap_uses_local_wheel_and_no_index(tmp_path: Path, monkeypatch) -> None:
    wheel = tmp_path / pip_bootstrap.PIP_BOOTSTRAP_FILENAME
    observation = tmp_path / "bootstrap-observation.json"
    fake_pip = """
import json, os, pathlib, sys, sysconfig, zipfile
wheel = pathlib.Path(sys.argv[-1])
target = pathlib.Path(sysconfig.get_paths()["purelib"])
with zipfile.ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith("pip/"):
            archive.extract(name, target)
pathlib.Path(os.environ["NEO_BOOTSTRAP_OBSERVATION"]).write_text(
    json.dumps({
        "argv": sys.argv[1:],
        "index": os.environ.get("PIP_INDEX_URL"),
        "extra_index": os.environ.get("PIP_EXTRA_INDEX_URL"),
        "config": os.environ.get("PIP_CONFIG_FILE"),
    }),
    encoding="utf-8",
)
"""
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pip/__init__.py", "__version__ = '26.2.1'\n")
        archive.writestr("pip/__main__.py", fake_pip)
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:9/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "http://127.0.0.1:9/extra")
    monkeypatch.setenv("NEO_BOOTSTRAP_OBSERVATION", str(observation))

    python = pip_bootstrap.create_pip_environment(
        tmp_path / "environment",
        wheel,
        symlinks=False,
        sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )

    assert python.is_file()
    payload = json.loads(observation.read_text())
    assert "--no-index" in payload["argv"]
    assert "--no-deps" in payload["argv"]
    assert payload["index"] is None
    assert payload["extra_index"] is None
    assert payload["config"] == os.devnull


def test_wheelhouse_rejects_incompatible_binary_before_install(tmp_path: Path) -> None:
    root = _wheelhouse(tmp_path)
    path = root / release_linux.WHEELHOUSE_MANIFEST_NAME
    manifest = json.loads(path.read_text())
    artifact = manifest["artifacts"][0]
    original = root / artifact["filename"]
    artifact["filename"] = artifact["filename"].replace("py3-none-any", "cp312-cp312-win_amd64")
    original.rename(root / artifact["filename"])
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(release_linux.LinuxReleaseError, match="compatible runtime tag"):
        release_linux._validate_wheelhouse(root)


def test_missing_wheel_report_lists_the_complete_required_set(tmp_path: Path) -> None:
    root = _wheelhouse(tmp_path)
    with pytest.raises(release_linux.LinuxReleaseError) as raised:
        release_linux._validate_wheelhouse(root, required={"numpy": "2.5.3", "ctranslate2": "4.8.1"})
    assert "numpy==2.5.3" in str(raised.value)
    assert "ctranslate2==4.8.1" in str(raised.value)


@pytest.mark.parametrize("fault", [None, "missing_extra", "wrong_version", "python", "remote_url"])
def test_full_offline_closure_evaluates_transitive_extras_and_markers(tmp_path: Path, fault: str | None) -> None:
    root = tmp_path / "closure"
    root.mkdir()
    rows = []
    requirements = {
        "rootpkg": ['child[needed]>=2.0; sys_platform == "linux"', 'not_for_linux; sys_platform == "win32"'],
        "child": ['leaf==3.0; extra == "needed"'],
        "leaf": [],
    }
    if fault == "remote_url":
        requirements["rootpkg"] = ["child @ https://vendor.example/child.whl"]
    versions = {"rootpkg": "1.0", "child": "2.0", "leaf": "3.0"}
    if fault == "wrong_version":
        versions["child"] = "1.0"
    if fault == "missing_extra":
        versions.pop("leaf")
    for name, version in versions.items():
        filename = f"{name}-{version}-py3-none-any.whl"
        wheel = root / filename
        metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
        metadata += "".join(f"Requires-Dist: {value}\n" for value in requirements[name])
        if fault == "python" and name == "leaf":
            metadata += "Requires-Python: <3\n"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
        rows.append({"filename": filename, "name": name, "version": version,
                     "sha256": release_linux._sha256_file(wheel)})
    (root / release_linux.WHEELHOUSE_MANIFEST_NAME).write_text(json.dumps({
        "schema_version": 1, "kind": "neocortex_wheelhouse", "python": "cp313",
        "platform": "linux_x86_64", "artifacts": rows,
    }), encoding="utf-8")
    artifacts = release_linux._validate_wheelhouse(root)
    if fault is None:
        release_linux._validate_runtime_dependency_closure(artifacts, versions, project_requirements=["rootpkg==1.0"])
    else:
        with pytest.raises(release_linux.LinuxReleaseError, match="offline runtime"):
            release_linux._validate_runtime_dependency_closure(artifacts, versions, project_requirements=["rootpkg==1.0"])
