"""Focused validation of the offline static-tooling inventory.

This test checks only the manifest and its local hash inventory.  It does not
install packages, invoke analyzers, or aggregate repository quality gates.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
INVENTORY = REPOSITORY / "dev-resources" / "offline" / "static-tools"
MANIFEST_PATH = INVENTORY / "manifest.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest() -> dict[str, object]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["kind"] == "neocortex_static_tooling_manifest"
    assert payload["target"] == {"platform": "linux_x86_64", "python": "cp314"}
    return payload


def test_static_tooling_manifest_is_self_consistent() -> None:
    payload = _load_manifest()
    entries = payload["entries"]
    assert isinstance(entries, list)
    assert payload["missing_wheels_are_explicit"] is True

    runtime = payload["runtime_reference"]
    assert isinstance(runtime, dict)
    assert runtime["status"] == "historical-only"
    assert "CPython 3.14" in runtime["reason"]
    assert "CPython 3.13-only" in runtime["reason"]
    assert "lock_path" not in runtime
    assert "wheelhouse_manifest_sha256" not in runtime
    assert "wheelhouse_artifact_count" not in runtime

    expected_roots = {
        "ruff": ("0.15.17", "analyzer"),
        "mypy": ("2.1.0", "analyzer"),
        "pyright": ("1.1.411", "analyzer"),
        "semgrep": ("1.176.1", "analyzer"),
        "pytest": ("9.1.1", "test-extra"),
    }
    observed_roots = {
        item["name"]: (item["version"], item["role"]) for item in payload["observed_roots"]
    }
    assert observed_roots == expected_roots

    statuses: dict[str, int] = {}
    keys: set[tuple[str, str, tuple[str, ...]]] = set()
    for entry in entries:
        key = (entry["root"], entry["name"], tuple(entry["extras"]))
        assert key not in keys
        keys.add(key)

        status = entry["artifact_status"]
        statuses[status] = statuses.get(status, 0) + 1
        assert SHA256.fullmatch(str(entry["installed_record_sha256"]))

        if status == "available":
            artifact = REPOSITORY / str(entry["artifact_path"])
            assert artifact.is_file()
            assert artifact.name == entry["artifact_filename"]
            assert _sha256(artifact) == entry["artifact_sha256"]
            assert artifact.parent == REPOSITORY / "dev-resources" / "offline" / "artifacts"
        elif status == "runtime-shared":
            assert entry["artifact_path"] is None
            assert isinstance(entry["artifact_filename"], str)
            assert SHA256.fullmatch(str(entry["artifact_sha256"]))
        elif status in {"missing-wheel", "incompatible-local-wheel"}:
            assert entry["artifact_sha256"] is None or SHA256.fullmatch(
                str(entry["artifact_sha256"])
            )
            assert entry["missing_reason"]
        else:
            raise AssertionError(f"unknown static-tooling artifact status: {status}")

    assert statuses == payload["status_counts"]
    missing = [
        {
            "name": entry["name"],
            "reason": entry["missing_reason"],
            "root": entry["root"],
            "status": entry["artifact_status"],
            "version": entry["version"],
        }
        for entry in entries
        if entry["artifact_status"] in {"missing-wheel", "incompatible-local-wheel"}
    ]
    assert missing == payload["missing_wheels"]

    # This snapshot is deliberately decoupled from the product runtime lock.
    # A CPython 3.13 runtime or wheelhouse must not be inferred from historical
    # CPython 3.14 static-tool records, and this test must not inspect a host
    # wheelhouse or install any tooling.


def test_available_static_tool_requirements_match_manifest() -> None:
    payload = _load_manifest()
    requirements = INVENTORY / str(payload["available_requirements_path"])
    parsed: dict[str, tuple[str, str]] = {}
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:([0-9a-f]{64})", line)
        assert match, f"unparseable hash-pinned requirement: {line}"
        name, version, digest = match.groups()
        assert name not in parsed
        parsed[name] = (version, digest)

    assert set(parsed) == {"iniconfig", "pluggy", "pytest"}
    for name, (version, digest) in parsed.items():
        candidates = [
            entry
            for entry in payload["entries"]
            if entry["root"] == "pytest" and entry["name"] == name and entry["version"] == version
        ]
        assert len(candidates) == 1
        entry = candidates[0]
        assert entry["artifact_status"] == "available"
        assert entry["artifact_sha256"] == digest
        assert _sha256(REPOSITORY / str(entry["artifact_path"])) == digest
