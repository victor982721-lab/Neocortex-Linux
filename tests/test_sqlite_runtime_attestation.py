"""Directed native identity checks; approval records below are synthetic fixtures."""

from __future__ import annotations

import copy
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from neocortex.platform import sqlite_runtime_attestation as native


TEST_CAPABILITIES = ("base", "platform")
pytestmark = [pytest.mark.capability("base", "platform"),
              pytest.mark.skipif(os.name != "posix", reason="Linux release probe")]


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    (root / "bin").mkdir(parents=True)
    # Probe ``-I`` through a real venv-shaped candidate.  A copied executable
    # with only ``bin/python`` is treated as a standalone prefix and cannot
    # discover the standard library (notably ``encodings``); a symlink merely
    # delegates prefix discovery to the host venv.  Keep the fixture small and
    # bind it to the actual native runtime used by this test process.
    executable = root / "bin" / "python"
    shutil.copy2(sys.executable, executable)
    executable.chmod(0o755)
    (root / "pyvenv.cfg").write_text(
        f"home = {Path(sys.base_prefix) / 'bin'}\n"
        "include-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def attestation(release_root: Path) -> dict:
    return native.collect_release_sqlite_attestation(release_root)


def _policy() -> dict:
    return {
        "schema": native.POLICY_SCHEMA, "policy_id": "test-only-empty-policy",
        "required_capabilities": list(native.CAPABILITIES), "approved_builds": [],
    }


def _approved_fixture(attestation: dict) -> dict:
    policy = _policy()
    policy["policy_id"] = "test-only-synthetic-vendor-backport"
    policy["approved_builds"] = [{
        "identity_sha256": attestation["identity_sha256"],
        "evidence": {
            "basis": "vendor_backport", "provider": "TEST FIXTURE; not a real vendor approval",
            "build_reference": "test-build-1", "reviewed_on": "2026-09-18",
            "source_url": "https://vendor.example/fixture-not-real",
        },
    }]
    return policy


def _evaluate(attestation: dict, policy: dict) -> dict:
    return native.evaluate_sqlite_runtime(
        attestation, policy, expected_policy_sha256=native.canonical_sha256(policy),
    )


def test_real_probe_uses_candidate_python_owned_temporary_and_no_user_state(
    release_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    run = native.subprocess.run

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return run(*args, **kwargs)

    monkeypatch.setattr(native.subprocess, "run", observe)
    result = native.collect_release_sqlite_attestation(release_root)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0][0:4] == (str(release_root / "bin" / "python"), "-I", "-B", "-c")
    assert result["python"]["invoked_executable"] == str(release_root / "bin" / "python")
    assert kwargs["env"]["HOME"] == kwargs["cwd"] == kwargs["env"]["TMPDIR"]
    assert "PYTHONPATH" not in kwargs["env"]
    assert not Path(kwargs["cwd"]).exists(), "probe scratch must be removed after explicit closes"
    assert sorted(path.relative_to(release_root).as_posix() for path in release_root.rglob("*")) == [
        "bin",
        "bin/python",
        "pyvenv.cfg",
    ]
    assert result["capabilities"] == dict.fromkeys(native.CAPABILITIES, True)
    assert result["sqlite"]["source_id"]
    assert result["sqlite"]["compile_options"] == sorted(result["sqlite"]["compile_options"])
    assert len(result["probe_sha256"]) == len(result["identity_sha256"]) == 64


def test_no_vendor_is_approved_by_version_or_successful_capabilities(attestation: dict) -> None:
    result = _evaluate(attestation, _policy())
    assert result["status"] == "unaccredited"
    assert result["missing_capabilities"] == []
    assert result["approval_evidence"] is None


def test_exact_synthetic_vendor_backport_build_can_be_approved(attestation: dict) -> None:
    result = _evaluate(attestation, _approved_fixture(attestation))
    assert result["status"] == "approved"
    assert result["approval_evidence"]["basis"] == "vendor_backport"


@pytest.mark.parametrize("changed", ["source_id", "module", "library", "python"])
def test_same_version_with_changed_identity_is_unaccredited(attestation: dict, changed: str) -> None:
    policy = _approved_fixture(attestation)
    modified = copy.deepcopy(attestation)
    if changed == "source_id":
        modified["sqlite"]["source_id"] += " TEST_ALTERNATIVE_BUILD"
    elif changed == "module":
        modified["sqlite"]["module"]["sha256"] = "a" * 64
    elif changed == "python":
        modified["python"]["executable"]["sha256"] = "b" * 64
    else:
        modified["sqlite"]["native_libraries"] = {
            "mode": "process_maps", "files": [{"sha256": "c" * 64}], "reason": None,
        }
    modified["identity_sha256"] = native.canonical_sha256(native.runtime_identity(modified))
    assert modified["sqlite"]["version"] == attestation["sqlite"]["version"]
    assert _evaluate(modified, policy)["status"] == "unaccredited"


def test_attestation_with_stale_identity_digest_is_rejected(attestation: dict) -> None:
    attestation["sqlite"]["module"]["sha256"] = "d" * 64
    with pytest.raises(native.SQLiteAttestationError, match="identity digest"):
        _evaluate(attestation, _policy())


def test_missing_fts5_is_incompatible_even_for_an_approved_build(attestation: dict) -> None:
    policy = _approved_fixture(attestation)
    attestation["capabilities"]["fts5"] = False
    result = _evaluate(attestation, policy)
    assert result["status"] == "incompatible"
    assert result["missing_capabilities"] == ["fts5"]
    assert result["approval_evidence"] is None


def test_tampered_policy_cannot_self_approve(attestation: dict) -> None:
    pinned_digest = native.canonical_sha256(_policy())
    with pytest.raises(native.SQLiteAttestationError, match="trusted binding"):
        native.evaluate_sqlite_runtime(
            attestation, _approved_fixture(attestation), expected_policy_sha256=pinned_digest,
        )


def test_staging_rename_preserves_identity_and_reprobes_real_interpreter(
    release_root: Path, attestation: dict,
) -> None:
    published = release_root.with_name("published")
    release_root.rename(published)
    observed = native.collect_release_sqlite_attestation(published)
    assert observed["python"]["invoked_executable"] != attestation["python"]["invoked_executable"]
    assert observed["identity_sha256"] == attestation["identity_sha256"]


def test_shell_proxy_cannot_substitute_an_unbound_interpreter(release_root: Path) -> None:
    python = release_root / "bin" / "python"
    python.unlink()
    python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)
    with pytest.raises(native.SQLiteAttestationError, match="did not run as release bin/python"):
        native.collect_release_sqlite_attestation(release_root)


def test_nonzero_probe_is_not_an_unaccredited_success(release_root: Path) -> None:
    python = release_root / "bin" / "python"
    python.unlink()
    python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    python.chmod(0o755)
    with pytest.raises(native.SQLiteAttestationError, match="probe failed"):
        native.collect_release_sqlite_attestation(release_root)


def test_missing_runtime_fails_before_spawning(tmp_path: Path) -> None:
    with pytest.raises(native.SQLiteAttestationError, match="unavailable"):
        native.collect_release_sqlite_attestation(tmp_path / "missing")


@pytest.mark.parametrize("fault", ["missing_evidence", "missing_capability", "bad_schema"])
def test_incomplete_policies_are_rejected(attestation: dict, fault: str) -> None:
    policy = _approved_fixture(attestation)
    if fault == "missing_evidence":
        policy["approved_builds"][0].pop("evidence")
    elif fault == "missing_capability":
        policy["required_capabilities"].remove("fts5")
    else:
        policy["schema"] = "neocortex.sqlite-runtime-policy/v0"
    with pytest.raises(native.SQLiteAttestationError):
        _evaluate(attestation, policy)


def test_unobservable_dynamic_library_identity_remains_explicit(attestation: dict) -> None:
    attestation["sqlite"]["native_libraries"] = {
        "mode": "static_or_unobserved", "reason": "test_unobservable", "files": [],
    }
    attestation["identity_sha256"] = native.canonical_sha256(native.runtime_identity(attestation))
    assert native.runtime_identity(attestation)["native_mode"] == "static_or_unobserved"
    assert _evaluate(attestation, _policy())["status"] == "unaccredited"


@pytest.mark.parametrize("field", ["attestation_sha256", "identity_sha256", "probe_sha256", "policy_id", "decision"])
def test_native_record_rejects_any_changed_binding(attestation: dict, field: str) -> None:
    policy = _approved_fixture(attestation)
    pin = native.canonical_sha256(policy)
    record = native.native_runtime_record(attestation, policy, expected_policy_sha256=pin)
    record[field] = "tampered"
    with pytest.raises(native.SQLiteAttestationError):
        native.validate_native_runtime_record(record, policy, expected_policy_sha256=pin)


def test_policy_symlink_and_missing_independent_pin_are_rejected(tmp_path: Path) -> None:
    import json

    policy = _policy()
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(policy), encoding="utf-8")
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises(native.SQLiteAttestationError):
        native.read_sqlite_policy(alias, expected_policy_sha256=native.canonical_sha256(policy))
    with pytest.raises(native.SQLiteAttestationError, match="trusted binding"):
        native.read_sqlite_policy(target, expected_policy_sha256="")


def test_packaged_helper_supports_builtin_sqlite_explicitly(attestation: dict) -> None:
    import _sqlite3

    module = attestation["sqlite"]["module"]
    if getattr(_sqlite3, "__file__", None) is None:
        assert module["kind"] == "builtin"
        assert module["sha256"] == attestation["python"]["executable"]["sha256"]
    else:
        assert module["kind"] == "extension"


def test_platform_report_reprobes_and_rejects_drift(release_root: Path, attestation: dict, monkeypatch) -> None:
    import hashlib
    import json

    policy = _approved_fixture(attestation)
    pin = native.canonical_sha256(policy)
    record = native.native_runtime_record(attestation, policy, expected_policy_sha256=pin)
    (release_root / native.POLICY_FILENAME).write_text(json.dumps(policy), encoding="utf-8")
    manifest_path = release_root / "neocortex-release.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 2, "native_runtime": record,
        "source_sha": "a" * 40,
        "native_runtime_sha256": native.canonical_sha256(record),
    }), encoding="utf-8")
    assert native.observe_platform_native_runtime(release_root)["status"] == "unaccredited"
    receipts = release_root.parent / "receipts"
    receipts.mkdir()
    receipt = {
        "schema_version": 2, "kind": "linux_release_receipt", "result": "success",
        "release_path": str(release_root), "release_id": release_root.name, "source_sha": "a" * 40,
        "native_runtime": record, "native_runtime_sha256": native.canonical_sha256(record),
        "artifacts": {"release_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                      "native_runtime": record, "native_runtime_sha256": native.canonical_sha256(record)},
    }
    (receipts / "001.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert native.observe_platform_native_runtime(release_root, receipts_directory=receipts)["status"] == "approved"
    changed = copy.deepcopy(attestation)
    changed["sqlite"]["source_id"] += " drift"
    changed["identity_sha256"] = native.canonical_sha256(native.runtime_identity(changed))
    monkeypatch.setattr(native, "collect_release_sqlite_attestation", lambda *_a, **_kw: changed)
    report = native.observe_platform_native_runtime(release_root, receipts_directory=receipts)
    assert report["status"] == "unaccredited"
    assert report["reason"] == "native_runtime_identity_changed"
    assert report["stored"] == record


def test_platform_does_not_upgrade_legacy_or_incomplete_v2(release_root: Path) -> None:
    import json

    path = release_root / "neocortex-release.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert native.observe_platform_native_runtime(release_root)["status"] == "legacy_unaccredited"
    path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    report = native.observe_platform_native_runtime(release_root)
    assert report["status"] == "unaccredited"
    assert report["measurement"] == "failed"


def test_platform_probe_timeout_abstains_and_cleans_owned_temporary(
    release_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    calls = []

    def timed_out(*args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(native.subprocess, "run", timed_out)
    result = native.observe_platform_native_runtime(release_root, probe_timeout_seconds=0.25)
    assert result["status"] == "unaccredited"
    assert result["measurement"] == "failed"
    assert "could not execute" in result["reason"]
    assert calls[0]["timeout"] == 0.25
    assert not Path(calls[0]["cwd"]).exists()


@pytest.mark.parametrize("evidence_name", ["manifest", "policy", "receipt"])
@pytest.mark.parametrize("invalid_kind", ["fifo", "symlink", "oversized"])
def test_platform_rejects_special_or_unbounded_evidence_before_reading(
    release_root: Path, attestation: dict, monkeypatch: pytest.MonkeyPatch,
    evidence_name: str, invalid_kind: str,
) -> None:
    import hashlib
    import json

    policy = _approved_fixture(attestation)
    pin = native.canonical_sha256(policy)
    record = native.native_runtime_record(attestation, policy, expected_policy_sha256=pin)
    policy_path = release_root / native.POLICY_FILENAME
    policy_path.write_text(json.dumps(policy))
    manifest_path = release_root / "neocortex-release.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 2, "native_runtime": record, "source_sha": "a" * 40,
        "native_runtime_sha256": native.canonical_sha256(record),
    }))
    receipts = release_root.parent / "receipts"
    receipts.mkdir()
    receipt_path = receipts / "001.json"
    receipt_path.write_text(json.dumps({
        "schema_version": 2, "kind": "linux_release_receipt", "result": "success",
        "release_path": str(release_root), "release_id": release_root.name, "source_sha": "a" * 40,
        "native_runtime": record, "native_runtime_sha256": native.canonical_sha256(record),
        "artifacts": {"release_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                      "native_runtime": record, "native_runtime_sha256": native.canonical_sha256(record)},
    }))
    monkeypatch.setattr(native, "collect_release_sqlite_attestation", lambda *_a, **_kw: attestation)
    assert native.observe_platform_native_runtime(release_root, receipts_directory=receipts)["status"] == "approved"
    target = {"manifest": manifest_path, "policy": policy_path, "receipt": receipt_path}[evidence_name]
    old = target.read_bytes()
    target.unlink()
    if invalid_kind == "fifo":
        os.mkfifo(target, 0o600)
        original_open = native.os.open

        def nonblocking_open(path, flags, *args, **kwargs):
            if Path(path) == target:
                # Fail promptly if the guard regresses; never hang the test on
                # the intentionally writerless FIFO.
                assert flags & os.O_NONBLOCK
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(native.os, "open", nonblocking_open)
    elif invalid_kind == "symlink":
        alternate = target.with_suffix(".original")
        alternate.write_bytes(old)
        target.symlink_to(alternate)
    else:
        with target.open("wb") as source:
            source.truncate(2_000_001)
    result = native.observe_platform_native_runtime(release_root, receipts_directory=receipts)
    assert result["status"] == "unaccredited"
    assert result["measurement"] == "failed"
    assert target.lstat(), "invalid evidence must be preserved for review"


def test_policy_name_replacement_during_read_is_rejected(tmp_path: Path, monkeypatch) -> None:
    import json

    policy = _policy()
    path = tmp_path / "policy.json"
    contents = json.dumps(policy).encode()
    path.write_bytes(contents)
    original_fstat = native.os.fstat
    calls = 0

    def replace_after_read(descriptor):
        nonlocal calls
        calls += 1
        observed = original_fstat(descriptor)
        if calls == 2:
            path.rename(path.with_suffix(".previous"))
            path.write_bytes(contents)
        return observed

    monkeypatch.setattr(native.os, "fstat", replace_after_read)
    with pytest.raises(native.SQLiteAttestationError) as caught:
        native.read_sqlite_policy(path, expected_policy_sha256=native.canonical_sha256(policy))
    assert "changed while reading" in str(caught.value.__cause__)
    assert path.read_bytes() == contents
