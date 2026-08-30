"""Focused contracts for the bounded local Semgrep invariant adapter."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

import neocortex.code.external_semgrep_invariants as adapter
from neocortex.code.code_external_evidence import ExternalEvidenceFile
from neocortex.semantic.semantic_models import fingerprint_bytes
from neocortex.code.semgrep_tool_contract import (
    ManagedSemgrepRuntime,
    resolve_semgrep_tool_runtime,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "semgrep_invariants"


def _stage(
    stage_root: Path,
    files: Mapping[str, str],
) -> dict[str, ExternalEvidenceFile]:
    source = stage_root / "source"
    source.mkdir(parents=True)
    staged: dict[str, ExternalEvidenceFile] = {}
    for version_id, (relative_path, content) in enumerate(sorted(files.items()), start=1):
        path = source / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        raw = path.read_bytes()
        fingerprint = fingerprint_bytes(raw)
        metadata = path.stat()
        staged[os.path.normcase(os.path.abspath(path))] = ExternalEvidenceFile(
            version_id,
            str(path),
            relative_path,
            metadata.st_size,
            metadata.st_mtime_ns,
            fingerprint.xxh3_128,
            fingerprint.xxh3_64_guard,
        )
    return staged


def _fixture_sources() -> dict[str, str]:
    return {
        item.relative_to(_FIXTURE_ROOT).as_posix(): item.read_text(encoding="utf-8")
        for item in sorted(_FIXTURE_ROOT.rglob("*.py"))
    }


def _environment() -> dict[str, str]:
    return dict(os.environ)


def _payload(
    scanned: list[str],
    *,
    findings: list[dict[str, object]] | None = None,
    errors: list[object] | None = None,
) -> bytes:
    return json.dumps(
        {
            "version": "1.172.0",
            "results": [] if findings is None else findings,
            "errors": [] if errors is None else errors,
            "paths": {"scanned": scanned},
            "time": {},
            "engine_requested": "OSS",
            "skipped_rules": [],
            "profiling_results": [],
        },
        ensure_ascii=True,
    ).encode("utf-8")


def _raw_finding(
    path: str,
    *,
    rule_id: str = "neocortex.no-shell-true",
    with_fix: bool = False,
) -> dict[str, object]:
    contract = adapter._RULE_CONTRACTS[rule_id]
    extra: dict[str, object] = {
        "message": contract.message,
        "metadata": dict(contract.metadata),
        "severity": "ERROR",
        "fingerprint": "requires login",
        "lines": "requires login",
        "validation_state": "NO_VALIDATOR",
        "engine_kind": "OSS",
    }
    if with_fix:
        extra["fix"] = "unsafe replacement"
    return {
        "check_id": rule_id,
        "path": path,
        "start": {"line": 3, "col": 5, "offset": 20},
        "end": {"line": 3, "col": 25, "offset": 40},
        "extra": extra,
    }


def _mock_runtime(monkeypatch: pytest.MonkeyPatch) -> adapter.SemgrepCliVariant:
    cli_variant: adapter.SemgrepCliVariant = "pysemgrep"
    runtime = ManagedSemgrepRuntime(
        tool_root=Path(sys.executable).parent,
        python=Path(sys.executable),
        wrapper=Path(sys.executable).parent / "neocortex_semgrep_scan.py",
        receipt=Path(sys.executable).parent / "neocortex-tool-runtime.json",
        receipt_sha256="0" * 64,
        version="1.172.0",
    )
    monkeypatch.setattr(
        adapter,
        "_managed_semgrep_runtime",
        lambda: runtime,
    )
    return cli_variant


def _require_real_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = os.environ.get("NEOCORTEX_TEST_SEMGREP_RUNTIME_ROOT")
    try:
        runtime = resolve_semgrep_tool_runtime(None if configured is None else Path(configured))
    except ValueError as exc:
        pytest.skip(f"managed Semgrep test runtime is unavailable: {exc}")
    monkeypatch.setattr(adapter, "_managed_semgrep_runtime", lambda: runtime)


def _hashes(root: Path) -> dict[str, str]:
    return {
        item.relative_to(root).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(root.rglob("*.py"))
    }


def test_real_semgrep_rules_report_only_the_positive_provider_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_real_runtime(monkeypatch)
    staged = _stage(tmp_path, _fixture_sources())
    source = tmp_path / "source"
    before = _hashes(source)

    result = adapter.execute_semgrep_invariants(tmp_path, staged, _environment())

    assert result.scanned_files == 3
    assert result.scanned_bytes == sum(item.size for item in staged.values())
    assert result.rule_count == 3
    assert result.process_invocations == 1
    assert result.stdout_bytes > 0
    assert result.ruleset_sha256 == adapter.SEMGREP_RULESET_SHA256
    assert len(result.input_manifest_sha256) == 64
    assert result.cli_variant == "pysemgrep"
    if os.name == "nt":
        assert "windows_pysemgrep_x509_compatibility" in result.limitations
    assert _hashes(source) == before

    findings_by_code: dict[str, list[object]] = {}
    for finding in result.findings:
        findings_by_code.setdefault(finding.code, []).append(finding)
        assert finding.relative_path.endswith("external_fixture_provider.py")
        assert finding.category == "project_invariant"
        assert finding.gate_authority == "advisory"
        assert finding.fix_available is False
        assert finding.mutation_authority is False
        assert finding.metadata["provider_schema"] == adapter.SEMGREP_INVARIANTS_PROVIDER_SCHEMA
    assert {key: len(value) for key, value in findings_by_code.items()} == {
        "NEOCORTEX_NO_EXTERNAL_PROVIDER_AUTOFIX": 2,
        "NEOCORTEX_NO_PROVIDER_MUTATION_AUTHORITY": 3,
        "NEOCORTEX_NO_SHELL_TRUE": 1,
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX Semgrep probes uname through PATH")
def test_real_semgrep_works_with_minimal_provider_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_real_runtime(monkeypatch)
    staged = _stage(
        tmp_path,
        {"neocortex/external_safe.py": "value = 1\n"},
    )

    result = adapter.execute_semgrep_invariants(tmp_path, staged, {})

    assert result.findings == ()
    assert result.scanned_files == 1
    assert result.cli_variant == "pysemgrep"


def test_command_and_environment_disable_network_registry_and_autofix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _stage(tmp_path, {"neocortex/external_safe.py": "value = 1\n"})
    cli_variant = _mock_runtime(monkeypatch)
    injected_environment = {
        "SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"),
        "HOME": "C:\\personal-home",
        "USERPROFILE": "C:\\personal-profile",
        "PATH": "/untrusted/bin",
        "HTTPS_PROXY": "https://proxy.invalid",
        "SEMGREP_APP_TOKEN": "secret",
        "SEMGREP_RULES": "p/default",
        "SEMGREP_SEND_METRICS": "on",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://telemetry.invalid",
    }

    def run(arguments, **kwargs):
        command = tuple(str(item) for item in arguments)
        assert command[0] == sys.executable
        assert command[1:3] == (
            "-I",
            str(Path(sys.executable).parent / "neocortex_semgrep_scan.py"),
        )
        assert "scan" not in command
        assert command[command.index("--config") + 1] == str(adapter._ruleset_path())
        assert "--json" in command
        assert "--metrics" in command
        assert command[command.index("--metrics") + 1] == "off"
        assert "--disable-version-check" in command
        assert "--oss-only" in command
        assert "--no-autofix" in command
        assert "--no-secrets-validation" in command
        assert "--disable-nosem" in command
        assert "--strict" in command
        assert "--no-error" in command
        assert "--autofix" not in command
        assert "--fix" not in command
        assert "--validate" not in command
        targets = list(command[command.index("--") + 1 :])
        assert targets == [os.path.join("source", "neocortex", "external_safe.py")]
        child_environment = kwargs["environment"]
        folded = {str(key).casefold(): value for key, value in child_environment.items()}
        for forbidden in (
            "home",
            "userprofile",
            "https_proxy",
            "semgrep_app_token",
            "semgrep_rules",
            "otel_exporter_otlp_endpoint",
        ):
            assert forbidden not in folded
        if os.name == "nt":
            assert "path" not in folded
        else:
            assert folded["path"] == os.defpath
        assert folded["semgrep_send_metrics"] == "off"
        assert folded["semgrep_enable_version_check"] == "0"
        assert folded["otel_sdk_disabled"] == "true"
        expected_temporary_parent = str(adapter._semgrep_temporary_parent(tmp_path))
        assert folded["temp"] == expected_temporary_parent
        assert folded["tmp"] == expected_temporary_parent
        assert folded["tmpdir"] == expected_temporary_parent
        for key in (
            "semgrep_log_file",
            "semgrep_settings_file",
            "semgrep_version_cache_path",
            "xdg_cache_home",
            "xdg_config_home",
        ):
            assert os.path.commonpath((str(tmp_path), str(folded[key]))) == str(tmp_path)
        assert kwargs["cwd"] == tmp_path
        assert 0 < kwargs["timeout_seconds"] <= 30.0
        assert kwargs["stdout_limit_bytes"] == 8 * 1024 * 1024
        assert kwargs["stderr_limit_bytes"] == 128 * 1024
        expected_memory = 1024 * 1024 * 1024 if os.name == "nt" else None
        assert kwargs["memory_limit_bytes"] == expected_memory
        return subprocess.CompletedProcess(command, 0, _payload(targets), b"")

    monkeypatch.setattr(adapter, "run_bounded_capture", run)

    result = adapter.execute_semgrep_invariants(tmp_path, staged, injected_environment)

    assert result.cli_variant == cli_variant
    assert result.findings == ()
    assert result.process_invocations == 1


def test_parser_normalizes_advisory_finding_without_fix_or_mutation_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _stage(
        tmp_path,
        {"neocortex/external_fixture_provider.py": "call(shell=True)\n"},
    )
    cli_variant = _mock_runtime(monkeypatch)

    def run(arguments, **_kwargs):
        command = tuple(str(item) for item in arguments)
        targets = list(command[command.index("--") + 1 :])
        return subprocess.CompletedProcess(
            command,
            0,
            _payload(targets, findings=[_raw_finding(targets[0])]),
            b"",
        )

    monkeypatch.setattr(adapter, "run_bounded_capture", run)

    result = adapter.execute_semgrep_invariants(tmp_path, staged, _environment())
    finding = result.findings[0]

    assert finding.code == "NEOCORTEX_NO_SHELL_TRUE"
    assert finding.severity == "error"
    assert finding.start_line == 3
    assert finding.start_column == 4
    assert finding.end_column == 24
    assert finding.tool_confidence == 1.0
    assert finding.calibrated_confidence is None
    assert finding.gate_authority == "advisory"
    assert finding.fix_available is False
    assert finding.mutation_authority is False
    assert finding.metadata["semgrep_cli_variant"] == cli_variant


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload, _target: payload.update(errors=[{"message": "parse error"}]), "errors"),
        (
            lambda payload, _target: payload["paths"].update(skipped=["source/skipped.py"]),
            "skipped",
        ),
        (
            lambda payload, target: payload.update(
                results=[_raw_finding(target, rule_id="neocortex.no-shell-true", with_fix=True)]
            ),
            "offered a fix",
        ),
        (
            lambda payload, target: payload.update(
                results=[
                    {
                        **_raw_finding(target),
                        "check_id": "registry.unauthorized-rule",
                    }
                ]
            ),
            "unauthorized rule",
        ),
        (
            lambda payload, _target: payload.update(results=[_raw_finding("source/outside.py")]),
            "unowned path",
        ),
    ],
)
def test_parser_fails_closed_on_incomplete_or_unauthorized_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, object], str], None],
    message: str,
) -> None:
    staged = _stage(tmp_path, {"neocortex/external_safe.py": "value = 1\n"})
    _mock_runtime(monkeypatch)

    def run(arguments, **_kwargs):
        command = tuple(str(item) for item in arguments)
        target = command[command.index("--") + 1]
        payload: dict[str, object] = json.loads(_payload([target]).decode("utf-8"))
        mutate(payload, target)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(payload).encode("utf-8"),
            b"",
        )

    monkeypatch.setattr(adapter, "run_bounded_capture", run)

    with pytest.raises(ValueError, match=message):
        adapter.execute_semgrep_invariants(tmp_path, staged, _environment())


def test_malformed_json_and_nonzero_exit_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _stage(tmp_path, {"module.py": "value = 1\n"})
    _mock_runtime(monkeypatch)
    monkeypatch.setattr(
        adapter,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, b"{bad-json", b""),
    )
    with pytest.raises(ValueError, match="JSON output is malformed"):
        adapter.execute_semgrep_invariants(tmp_path, staged, _environment())

    monkeypatch.setattr(
        adapter,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 2, b"", b"invalid local config"
        ),
    )
    with pytest.raises(ValueError, match="semgrep_unexpected_exit:2"):
        adapter.execute_semgrep_invariants(tmp_path, staged, _environment())


def test_staging_rejects_empty_extra_unowned_and_mismatched_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="file count"):
        adapter.execute_semgrep_invariants(tmp_path, {}, _environment())

    extra_root = tmp_path / "extra"
    staged = _stage(extra_root, {"module.py": "value = 1\n"})
    (extra_root / "source" / "outside.py").write_text("outside = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unowned file"):
        adapter.execute_semgrep_invariants(extra_root, staged, _environment())

    mismatch_root = tmp_path / "mismatch"
    staged = _stage(mismatch_root, {"module.py": "value = 1\n"})
    owner = next(iter(staged.values()))
    with pytest.raises(ValueError, match="exact source path"):
        adapter.execute_semgrep_invariants(
            mismatch_root,
            {str(mismatch_root / "source" / "other.py"): owner},
            _environment(),
        )


def test_hard_file_byte_finding_and_batch_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_root = tmp_path / "oversized"
    staged = _stage(oversized_root, {"module.py": "value = 1\n"})
    owner = next(iter(staged.values()))
    staged[next(iter(staged))] = ExternalEvidenceFile(
        owner.version_id,
        owner.path,
        owner.relative_path,
        adapter._MAX_FILE_BYTES + 1,
        owner.mtime_ns,
        owner.raw_xxh3_128,
        owner.raw_xxh3_64_guard,
    )
    with pytest.raises(ValueError, match="file size"):
        adapter.execute_semgrep_invariants(oversized_root, staged, _environment())

    finding_root = tmp_path / "findings"
    staged = _stage(finding_root, {"module.py": "value = 1\n"})
    _mock_runtime(monkeypatch)
    monkeypatch.setattr(adapter, "_MAX_FINDINGS", 0)

    def finding_run(arguments, **_kwargs):
        command = tuple(str(item) for item in arguments)
        target = command[command.index("--") + 1]
        return subprocess.CompletedProcess(
            command,
            0,
            _payload([target], findings=[_raw_finding(target)]),
            b"",
        )

    monkeypatch.setattr(adapter, "run_bounded_capture", finding_run)
    with pytest.raises(ValueError, match="findings exceed"):
        adapter.execute_semgrep_invariants(finding_root, staged, _environment())

    batch_root = tmp_path / "batches"
    staged = _stage(batch_root, {"a.py": "a = 1\n", "b.py": "b = 2\n"})
    monkeypatch.setattr(adapter, "_MAX_FINDINGS", 10_000)
    monkeypatch.setattr(adapter, "_MAX_BATCH_FILES", 1)
    calls = 0

    def batch_run(arguments, **_kwargs):
        nonlocal calls
        calls += 1
        command = tuple(str(item) for item in arguments)
        targets = list(command[command.index("--") + 1 :])
        assert len(targets) == 1
        return subprocess.CompletedProcess(command, 0, _payload(targets), b"")

    monkeypatch.setattr(adapter, "run_bounded_capture", batch_run)
    result = adapter.execute_semgrep_invariants(batch_root, staged, _environment())
    assert calls == 2
    assert result.process_invocations == 2
    assert result.scanned_files == 2


def test_ruleset_and_version_contracts_are_local_pinned_and_without_fixes() -> None:
    ruleset = adapter._ruleset_path()
    raw = ruleset.read_text(encoding="utf-8")

    assert ruleset.parent.name == "semgrep_rules"
    ruleset_bytes = ruleset.read_bytes()
    assert adapter._ruleset_digest(ruleset_bytes) == adapter.SEMGREP_RULESET_SHA256
    canonical_lf = ruleset_bytes.replace(b"\r\n", b"\n")
    assert (
        adapter._ruleset_digest(canonical_lf.replace(b"\n", b"\r\n"))
        == adapter.SEMGREP_RULESET_SHA256
    )
    assert (
        tuple(
            line.split(":", 1)[1].strip() for line in raw.splitlines() if line.startswith("  - id:")
        )
        == adapter.SEMGREP_INVARIANT_RULE_IDS
    )
    assert "\n    fix:" not in raw
    assert "p/default" not in raw
    assert "r/" not in raw
    adapter._validate_tool_version("1.172.0")
    adapter._validate_tool_version("1.172.9")
    with pytest.raises(ValueError, match=r"supported 1\.172"):
        adapter._validate_tool_version("1.173.0")
