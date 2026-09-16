"""The tracked development supply is complete, authentic and target-specific.

These checks read distribution metadata, not optional native modules, and do not
contact an index, instantiate models or create product state.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


ROOT = Path(__file__).resolve().parents[1]
SUPPLY = ROOT / "dev-resources" / "offline"
TARGET_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.13.5",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.13.5",
    "platform_python_implementation": "CPython",
    "python_version": "3.13",
    "sys_platform": "linux",
    "extra": "",
}
PROFILES = {
    "runtime-base": ("runtime",),
    "fast-hash": ("fast-hash",),
    "build": ("build",),
    "test-base": ("test-base",),
    "documents-image": ("documents", "image"),
}


def _json() -> dict:
    return json.loads((SUPPLY / "provenance.json").read_text(encoding="utf-8"))


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _locked(path: Path) -> dict[str, tuple[str, str]]:
    entries = {}
    for line in path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([\w.-]+)==([^\s]+)\s+--hash=sha256:([a-f0-9]{64})\s*", line)
        assert match, f"Unpinned/unhashed requirement in {path.name}: {line}"
        name, version, digest = match.groups()
        name = canonicalize_name(name)
        assert name not in entries, f"Duplicate pin: {name}"
        entries[name] = (version, digest)
    assert entries, f"Empty lock: {path}"
    return entries


def _direct(profile: str) -> list[Requirement]:
    metadata = _project()
    project = metadata["project"]
    if profile == "runtime":
        values = project["dependencies"]
    elif profile == "build":
        values = [*metadata["build-system"]["requires"], *project["optional-dependencies"]["build"], "pip"]
    else:
        values = project["optional-dependencies"][profile]
    return [Requirement(value) for value in values]


def _required(requirement: Requirement) -> bool:
    return requirement.marker is None or requirement.marker.evaluate(TARGET_ENVIRONMENT)


def test_offline_artifacts_have_original_hashes_licenses_and_compatible_tags() -> None:
    provenance = _json()
    assert provenance["schema_version"] == 1
    target = provenance["target"]
    assert target["python_version"] == "3.13"
    assert target["python_full_version"] == "3.13.5"
    assert target["abi"] == "cp313"
    assert target["architecture"] == "x86_64"
    assert target["libc"] == "glibc"
    assert target["glibc_max"] == "2.41"
    platforms = [f"manylinux_2_{minor}_x86_64" for minor in range(41, 4, -1)]
    platforms += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
    allowed = set(cpython_tags((3, 13), abis=("cp313",), platforms=platforms))
    allowed.update(compatible_tags((3, 13), interpreter="cp313", platforms=platforms))
    filenames = set()
    names = set()
    for artifact in provenance["artifacts"]:
        filename = artifact["filename"]
        path = SUPPLY / "artifacts" / filename
        assert Path(filename).name == filename
        assert not path.is_symlink() and path.is_file()
        assert path.stat().st_size == artifact["size_bytes"]
        assert _hash(path) == artifact["sha256"]
        assert artifact["sha256"] == artifact["source_sha256"]
        assert artifact["source_url"].startswith("https://files.pythonhosted.org/")
        name, version, _, tags = parse_wheel_filename(filename)
        assert name == canonicalize_name(artifact["name"])
        assert str(version) == artifact["version"]
        assert tags & allowed, f"Wheel is not compatible with CPython 3.13 GIL/Linux: {filename}"
        assert all(tag.abi != "cp313t" and tag.interpreter != "cp314" for tag in tags)
        assert set(map(str, tags)) == set(artifact["wheel_tags"])
        assert TARGET_ENVIRONMENT["python_full_version"] in SpecifierSet(artifact["requires_python"] or "")
        assert filename not in filenames and name not in names
        filenames.add(filename)
        names.add(name)
        with zipfile.ZipFile(path) as wheel:
            member = next(
                item for item in wheel.namelist()
                if item.count("/") == 1 and item.endswith(".dist-info/METADATA")
            )
            metadata = BytesParser().parsebytes(wheel.read(member))
            assert canonicalize_name(metadata["Name"]) == name
            assert metadata["Version"] == artifact["version"]
            assert metadata.get_all("Requires-Dist", []) == artifact["requires_dist"]
            assert artifact["licenses"], f"No retained original license: {filename}"
            for license_entry in artifact["licenses"]:
                license_path = SUPPLY / license_entry["path"]
                assert license_path.resolve().is_relative_to((SUPPLY / "licenses").resolve())
                assert not license_path.is_symlink() and license_path.is_file()
                assert _hash(license_path) == license_entry["sha256"]
                assert license_path.read_bytes() == wheel.read(license_entry["source_member"])
    assert filenames == {path.name for path in (SUPPLY / "artifacts").iterdir()}
    # Inference engines, Qt and large model weights are deliberately not supplied.
    assert names.isdisjoint({"fastembed", "onnxruntime", "ctranslate2", "faster-whisper", "pyside6", "mcp"})


@pytest.mark.parametrize("profile", tuple(PROFILES))
def test_offline_profile_locks_supply_exact_direct_and_transitive_closure(profile: str) -> None:
    artifacts = {canonicalize_name(item["name"]): item for item in _json()["artifacts"]}
    pins = _locked(SUPPLY / "locks" / f"{profile}-cp313-linux-x86_64.lock")
    pending = [requirement for group in PROFILES[profile] for requirement in _direct(group)]
    seen = set()
    while pending:
        requirement = pending.pop()
        if not _required(requirement):
            continue
        name = canonicalize_name(requirement.name)
        assert name in pins, f"Missing {requirement} from {profile}"
        version, digest = pins[name]
        assert version in requirement.specifier, f"{name}=={version} violates {requirement}"
        artifact = artifacts[name]
        assert (version, digest) == (artifact["version"], artifact["sha256"])
        if name not in seen:
            seen.add(name)
            pending.extend(Requirement(value) for value in artifact["requires_dist"])
    assert seen == set(pins), f"Unneeded packages included in {profile}: {set(pins) - seen}"


def test_offline_aggregate_lock_is_exactly_the_provisioned_capabilities() -> None:
    union = {}
    for profile in PROFILES:
        for name, pin in _locked(SUPPLY / "locks" / f"{profile}-cp313-linux-x86_64.lock").items():
            assert name not in union or union[name] == pin
            union[name] = pin
    assert union == _locked(ROOT / "constraints-linux-cp313.lock")
    assert set(union) == {canonicalize_name(item["name"]) for item in _json()["artifacts"]}


def test_offline_supply_stays_outside_importable_and_built_product() -> None:
    metadata = _project()
    assert metadata["tool"]["setuptools"]["packages"]["find"]["include"] == ["neocortex*"]
    assert "prune dev-resources" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    assert "!dev-resources/offline/artifacts/*.whl" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert not (SUPPLY / "__init__.py").exists()
    assert metadata["project"]["optional-dependencies"]["test-base"] == [
        "pytest>=9.1,<10",
        "setuptools==83.0.0",
    ]
    assert "setuptools==83.0.0" in metadata["build-system"]["requires"]
