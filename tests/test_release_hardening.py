"""Focused supply-chain and release-boundary regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

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
                "python": "cp314",
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

    assert len(entries) >= 50
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
