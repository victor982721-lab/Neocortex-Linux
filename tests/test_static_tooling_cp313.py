"""Validate the active CPython 3.13 static-tooling closure.

The historical CPython 3.14 snapshot has its own focused test.  This test
only checks the active CPython 3.13 lock and provenance records; it does not
install packages, inspect a host wheelhouse, or aggregate quality tools.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename


REPOSITORY = Path(__file__).resolve().parents[1]
STATIC_TOOLS = REPOSITORY / "dev-resources" / "offline" / "static-tools"
MANIFEST = STATIC_TOOLS / "manifest-cp313.json"
PROVENANCE = STATIC_TOOLS / "provenance-cp313.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LOCK_LINE = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:([0-9a-f]{64})$"
)


def _key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lock_entries(lock: Path) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for raw_line in lock.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line)
        assert match, f"unparseable hash-pinned static-tool requirement: {line}"
        name, version, digest = match.groups()
        key = _key(name)
        assert key not in entries, f"duplicate static-tool requirement: {name}"
        entries[key] = (version, digest)
    return entries


def test_active_cp313_static_tooling_records_are_self_consistent() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["kind"] == "neocortex_static_tooling_manifest"
    assert manifest["target"] == {
        "platform": "linux_x86_64",
        "python": "cp313",
        "python_full_version": "3.13.15",
    }
    assert manifest["scope"] == "active isolated CPython 3.13 static QA tooling"

    lock = (STATIC_TOOLS / manifest["lock_path"]).resolve()
    assert lock == REPOSITORY / "dev-resources" / "offline" / "locks" / lock.name
    assert _sha256(lock) == manifest["lock_sha256"]
    assert SHA256.fullmatch(manifest["lock_sha256"])
    assert (STATIC_TOOLS / manifest["provenance_path"]).resolve() == PROVENANCE
    assert _sha256(PROVENANCE) == manifest["provenance_sha256"]
    assert SHA256.fullmatch(manifest["provenance_sha256"])

    entries = _lock_entries(lock)
    assert len(entries) == manifest["closure_count"] == provenance["supply"]["artifact_count"]
    assert {item["name"] for item in manifest["analyzer_roots"]} == {
        "ruff",
        "mypy",
        "pyright",
        "semgrep",
    }
    expected_roots = {
        "ruff": "0.15.17",
        "mypy": "2.1.0",
        "pyright": "1.1.411",
        "semgrep": "1.176.1",
    }
    assert {
        item["name"]: item["version"] for item in manifest["analyzer_roots"]
    } == expected_roots
    for name, version in expected_roots.items():
        assert entries[name][0] == version
        assert SHA256.fullmatch(entries[name][1])

    provenance_artifacts = {}
    supported_tags = set(sys_tags())
    for artifact in provenance["artifacts"]:
        name, wheel_version, _build, tags = parse_wheel_filename(artifact["filename"])
        key = _key(name)
        assert key not in provenance_artifacts
        provenance_artifacts[key] = artifact
        assert artifact["version"] == str(wheel_version)
        assert entries[key] == (artifact["version"], artifact["sha256"])
        assert SHA256.fullmatch(artifact["sha256"])
        assert artifact["source_url"].startswith("https://files.pythonhosted.org/")
        assert artifact["filename"].endswith(".whl")
        assert artifact["size_bytes"] > 0
        requires_python = artifact["requires_python"]
        if requires_python:
            assert SpecifierSet(requires_python).contains("3.13.15", prereleases=True)
        assert tags & supported_tags, f"wheel is not compatible with this CPython/Linux host: {artifact['filename']}"
    assert set(provenance_artifacts) == set(entries)

    assert provenance["target"]["python_full_version"] == "3.13.15"
    assert provenance["provider"] == {
        "index_url": "https://pypi.org/simple",
        "name": "PyPI",
        "transport": "HTTPS",
    }
    assert provenance["supply"]["repository_artifacts"] is False
    assert provenance["supply"]["status"] == "verified-local-for-this-host"
    assert provenance["verification"]["pip_check"] == "No broken requirements found"
    assert "io_uring" in provenance["verification"]["semgrep_host_fallback"]
