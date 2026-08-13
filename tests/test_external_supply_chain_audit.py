"""Focused contracts for bounded supply-chain producers."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import subprocess
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from email.message import Message
from importlib.metadata import Distribution
from pathlib import Path
from typing import cast

import pytest

import _04_Nucleo_Operativo.external_supply_chain_audit as audit

_FIXTURES = Path(__file__).with_name("fixtures")
_OBSERVED = datetime(2026, 8, 3, 12, 30, tzinfo=timezone.utc)


def test_installed_environment_inventory_excludes_source_checkout_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    site_packages = tmp_path / "environment" / "site-packages"
    source.mkdir()
    site_packages.mkdir(parents=True)
    monkeypatch.chdir(source)
    monkeypatch.setattr(
        audit.sys,
        "path",
        ["", str(source), str(site_packages), str(site_packages), str(tmp_path / "missing")],
    )
    observed_paths: list[tuple[str, ...]] = []
    sentinel = cast("Distribution", object())

    def distributions(*, path: list[str]):
        observed_paths.append(tuple(path))
        return (sentinel,)

    monkeypatch.setattr(audit.importlib.metadata, "distributions", distributions)

    assert audit.installed_environment_distributions() == (sentinel,)
    assert observed_paths == [(str(site_packages.resolve()),)]


class _FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str,
        version: str,
        requires: list[str] | None = None,
        license_expression: str | None = None,
        license_text: str | None = None,
        license_classifiers: tuple[str, ...] = (),
        record_path: Path | None = None,
    ) -> None:
        metadata = Message()
        metadata["Metadata-Version"] = "2.4"
        metadata["Name"] = name
        metadata["Version"] = version
        if license_expression is not None:
            metadata["License-Expression"] = license_expression
        if license_text is not None:
            metadata["License"] = license_text
        for classifier in license_classifiers:
            metadata["Classifier"] = classifier
        self.metadata = metadata
        self.version = version
        self.requires = requires
        self._root = root
        self._record_path = record_path

    def read_text(self, filename: str) -> str | None:
        if filename != "RECORD" or self._record_path is None:
            return None
        return self._record_path.read_text("utf-8")

    def locate_file(self, path: str) -> Path:
        return self._root / Path(path)


def _metric(
    result: audit.PipAuditExecution | audit.InstalledPackageInventoryExecution,
    subject_key: str,
    name: str,
) -> audit.ExternalProviderMetric:
    return next(
        item
        for item in result.metrics
        if item.subject_key == subject_key and item.metric_name == name
    )


def _pip_payload() -> bytes:
    dependencies = json.loads((_FIXTURES / "pip_audit_vulnerabilities_v1.json").read_text("utf-8"))
    return json.dumps({"dependencies": dependencies, "fixes": []}).encode()


def _install_fixture(tmp_path: Path) -> tuple[Path, Path, list[Distribution]]:
    install_root = tmp_path / "runtime"
    site_packages = install_root / "Lib" / "site-packages"
    package_file = site_packages / "neocortex" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_bytes = b"healthy"
    package_file.write_bytes(package_bytes)
    dist_info = site_packages / "neocortex_framework-0.7.2.dist-info"
    dist_info.mkdir(parents=True)
    record_path = dist_info / "RECORD"
    digest = base64.urlsafe_b64encode(hashlib.sha256(package_bytes).digest()).decode().rstrip("=")
    record_path.write_text(
        "neocortex/__init__.py," + f"sha256={digest},{len(package_bytes)}\n"
        "neocortex_framework-0.7.2.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    framework = _FakeDistribution(
        site_packages,
        name="neocortex-framework",
        version="0.7.2",
        requires=["demo-dep>=1", "optional-extra>=3; extra == 'full'"],
        license_expression="MIT",
        license_text="MIT License",
        license_classifiers=("License :: OSI Approved :: MIT License",),
        record_path=record_path,
    )
    dependency = _FakeDistribution(
        site_packages,
        name="Demo.Dep",
        version="1.5",
        license_text="BSD-3-Clause",
    )
    mismatch = _FakeDistribution(
        site_packages,
        name="mismatch-dep",
        version="1.5",
        license_expression="Apache-2.0",
    )
    return (
        install_root,
        package_file,
        cast(
            list[Distribution],
            [framework, dependency, mismatch],
        ),
    )


def _trace_pip_audit_phase(
    monkeypatch: pytest.MonkeyPatch,
    trace: list[str],
    phase: str,
) -> None:
    implementation = getattr(audit, phase)

    def traced(*args, **kwargs):
        trace.append(phase)
        return implementation(*args, **kwargs)

    monkeypatch.setattr(audit, phase, traced)


def test_pip_audit_public_contract_phase_order_and_complete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert str(inspect.signature(audit.execute_pip_audit_known_vulnerabilities)) == (
        "(environment: 'Mapping[str, str]', *, observed_at: 'datetime | None' = None, "
        "freshness_seconds: 'int' = 86400) -> 'PipAuditExecution'"
    )
    trace: list[str] = []
    phases = (
        "_validate_pip_audit_freshness",
        "_execute_pip_audit_process",
        "_validated_pip_audit_payload",
        "_prepare_pip_audit_context",
        "_build_pip_audit_outputs",
        "_validate_pip_audit_exit_status",
        "_build_pip_audit_execution",
    )
    for phase in phases:
        _trace_pip_audit_phase(monkeypatch, trace, phase)
    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.0")
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            1,
            _pip_payload(),
            b"2 vulnerabilities",
        ),
    )

    result = audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)

    assert tuple(trace) == phases
    assert tuple(item.name for item in dataclass_fields(result)) == (
        "metrics",
        "relations",
        "counters",
        "tool_version",
        "source",
        "observed_at_utc",
        "observed_date_utc",
        "snapshot_id",
        "freshness_status",
        "fresh_until_utc",
        "stdout_bytes",
        "stderr_bytes",
        "process_invocations",
        "uses_network",
        "limitations",
    )
    assert result.counters == audit.PipAuditCounters(3, 2, 1, 1, 2, 2)
    assert result.process_invocations == 1
    assert result.stdout_bytes == len(_pip_payload())
    assert result.stderr_bytes == len(b"2 vulnerabilities")
    assert tuple(item.portable_metric_id for item in result.metrics) == tuple(
        sorted(item.portable_metric_id for item in result.metrics)
    )
    assert tuple(item.portable_relation_id for item in result.relations) == tuple(
        sorted(item.portable_relation_id for item in result.relations)
    )


def test_pip_audit_propagates_interruption_before_decode_and_cleans_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = KeyboardInterrupt("injected pip-audit cancellation")
    scratch_paths: list[Path] = []
    payload_decoded = False

    def interrupt(arguments, **_kwargs):
        cache_path = Path(arguments[arguments.index("--cache-dir") + 1])
        scratch_paths.append(cache_path.parent)
        assert cache_path.is_file()
        raise cancellation

    def reject_decode(_raw: bytes) -> list[object]:
        nonlocal payload_decoded
        payload_decoded = True
        raise AssertionError("cancelled subprocess output must not be decoded")

    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.0")
    monkeypatch.setattr(audit, "run_bounded_capture", interrupt)
    monkeypatch.setattr(audit, "_pip_audit_payload", reject_decode)
    environment = {"TEMP": str(tmp_path), "SAFE_SENTINEL": "unchanged"}

    with pytest.raises(KeyboardInterrupt) as raised:
        audit.execute_pip_audit_known_vulnerabilities(environment, observed_at=_OBSERVED)

    assert raised.value is cancellation
    assert payload_decoded is False
    assert scratch_paths and not scratch_paths[0].exists()
    assert environment == {"TEMP": str(tmp_path), "SAFE_SENTINEL": "unchanged"}


def test_pip_audit_projection_cancellation_never_builds_partial_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = KeyboardInterrupt("injected pip-audit projection cancellation")
    real_append = audit._append_pip_audit_package
    appended_packages = 0
    execution_started = False

    def cancel_after_first(context, projection, raw_package):
        nonlocal appended_packages
        if appended_packages:
            raise cancellation
        real_append(context, projection, raw_package)
        appended_packages += 1

    def reject_execution(*_args, **_kwargs):
        nonlocal execution_started
        execution_started = True
        raise AssertionError("partial pip-audit evidence must not become an execution")

    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.0")
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            1,
            _pip_payload(),
            b"2 vulnerabilities",
        ),
    )
    monkeypatch.setattr(audit, "_append_pip_audit_package", cancel_after_first)
    monkeypatch.setattr(audit, "_build_pip_audit_execution", reject_execution)

    with pytest.raises(KeyboardInterrupt) as raised:
        audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)

    assert raised.value is cancellation
    assert appended_packages == 1
    assert execution_started is False


def test_pip_audit_is_bounded_no_fix_and_normalizes_advisories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_commands: list[tuple[str, ...]] = []
    observed_scratch: list[Path] = []

    def run(arguments, **kwargs):
        command = tuple(arguments)
        observed_commands.append(command)
        assert kwargs["timeout_seconds"] == 180.0
        assert kwargs["stdout_limit_bytes"] == 8 * 1024 * 1024
        assert kwargs["stderr_limit_bytes"] == 128 * 1024
        cache_path = Path(command[command.index("--cache-dir") + 1])
        assert kwargs["environment"]["SAFE_SENTINEL"] == "1"
        assert kwargs["environment"]["HOME"] == str(cache_path.parent)
        assert "PIP_AUDIT_FORMAT" not in kwargs["environment"]
        if audit.os.name == "nt":
            assert kwargs["environment"]["USERPROFILE"] == str(cache_path.parent)
        observed_scratch.append(cache_path.parent)
        assert cache_path.is_file()
        return subprocess.CompletedProcess(arguments, 1, _pip_payload(), b"2 vulnerabilities")

    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.0")
    monkeypatch.setattr(audit, "run_bounded_capture", run)
    result = audit.execute_pip_audit_known_vulnerabilities(
        {"SAFE_SENTINEL": "1", "PIP_AUDIT_FORMAT": "cyclonedx-json"},
        observed_at=_OBSERVED,
    )

    command = observed_commands[0]
    assert command[1:3] == ("-m", "pip_audit")
    assert command[command.index("--vulnerability-service") + 1] == "pypi"
    assert command[command.index("--aliases") + 1] == "on"
    assert command[command.index("--desc") + 1] == "off"
    assert command[command.index("--progress-spinner") + 1] == "off"
    assert command[command.index("--timeout") + 1] == "15"
    assert not Path(command[command.index("--cache-dir") + 1]).exists()
    assert observed_scratch and not observed_scratch[0].exists()
    assert "--fix" not in command
    assert result.counters == audit.PipAuditCounters(3, 2, 1, 1, 2, 2)
    assert result.uses_network is True
    assert result.source.startswith("PyPI JSON API")
    assert result.observed_at_utc == "2026-08-03T12:30:00Z"
    assert result.observed_date_utc == "2026-08-03"
    assert result.fresh_until_utc == "2026-08-04T12:30:00Z"
    assert result.freshness_status == "fresh_at_observation"
    assert _metric(result, "package:demo-pkg", "known_vulnerability_count").value == 2
    advisory = _metric(result, "package:demo-pkg", "known_vulnerability:PYSEC-2026-1")
    assert advisory.category == "known_vulnerability"
    assert advisory.metadata["aliases"] == ["CVE-2026-0001", "GHSA-aaaa-bbbb-cccc"]
    assert advisory.metadata["descriptions_collected"] is False
    assert "description" not in advisory.metadata
    assert (
        _metric(result, "project:installed-environment", "audit_current_at_observation").value == 1
    )
    assert {item.relation_kind for item in result.relations} == {"package_has_known_vulnerability"}
    assert all(item.metadata["mutation_authority"] is False for item in result.relations)


def test_pip_audit_merges_duplicate_advisory_rows_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_pip_payload())
    payload["dependencies"][0]["vulns"].append(
        {
            "id": "pysec-2026-1",
            "aliases": ["GHSA-aaaa-bbbb-cccc", "CVE-2026-0002"],
            "fix_versions": ["1.2", "1.1"],
        }
    )

    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.1")
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 1, json.dumps(payload).encode(), b"2 vulnerabilities"
        ),
    )

    result = audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)

    assert result.counters == audit.PipAuditCounters(3, 2, 1, 1, 2, 3)
    assert _metric(result, "package:demo-pkg", "known_vulnerability_count").value == 2
    advisory = _metric(result, "package:demo-pkg", "known_vulnerability:PYSEC-2026-1")
    assert advisory.metadata["aliases"] == [
        "CVE-2026-0001",
        "CVE-2026-0002",
        "GHSA-aaaa-bbbb-cccc",
    ]
    assert advisory.metadata["fix_versions"] == ["1.1", "1.2"]
    assert len(result.relations) == 2
    assert len({item.portable_metric_id for item in result.metrics}) == len(result.metrics)
    assert (
        "duplicate_advisory_rows_are_merged_by_casefolded_id_with_alias_and_fix_union"
        in result.limitations
    )


def test_pip_audit_accepts_clean_exit_and_rejects_failure_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.7")
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            0,
            b'{"dependencies": [], "fixes": []}',
            b"",
        ),
    )
    clean = audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)
    assert clean.counters.vulnerabilities == 0
    assert clean.process_invocations == 1

    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            2,
            b'{"dependencies": [], "fixes": []}',
            b"bad",
        ),
    )
    with pytest.raises(ValueError, match="unexpected_exit:2"):
        audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)

    payload = json.dumps(
        {
            "dependencies": [
                {"name": "first", "version": "1", "vulns": []},
                {"name": "second", "version": "1", "vulns": []},
            ],
            "fixes": [],
        }
    ).encode()
    monkeypatch.setattr(audit, "_MAX_AUDIT_PACKAGES", 1)
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, payload, b""),
    )
    with pytest.raises(ValueError, match="package count"):
        audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)


def test_pip_audit_rejects_failed_exit_one_schema_drift_and_fix_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.1")
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            1,
            b"",
            b"fatal cache initialization",
        ),
    )
    with pytest.raises(ValueError, match="unexpected_exit:1:fatal cache initialization"):
        audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)

    network_error = b"urllib3 NameResolutionError: No address associated with hostname"
    monkeypatch.setattr(
        audit,
        "run_bounded_capture",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            1,
            b"",
            network_error,
        ),
    )
    with pytest.raises(
        ValueError,
        match=(
            "^pip_audit_network_unavailable:1:stderr_sha256:"
            + hashlib.sha256(network_error).hexdigest()
            + "$"
        ),
    ):
        audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)

    invalid_payloads = (
        b"[]",
        b'{"dependencies": [], "fixes": [], "future": true}',
        b'{"dependencies": [], "fixes": [{"name": "changed"}]}',
    )
    for payload in invalid_payloads:
        monkeypatch.setattr(
            audit,
            "run_bounded_capture",
            lambda arguments, _payload=payload, **_kwargs: subprocess.CompletedProcess(
                arguments,
                0,
                _payload,
                b"",
            ),
        )
        with pytest.raises(ValueError):
            audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)


def test_pip_audit_exit_status_must_match_vulnerability_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "2.10.1")
    clean = b'{"dependencies": [], "fixes": []}'
    vulnerable = json.dumps(
        {
            "dependencies": [
                {
                    "name": "demo",
                    "version": "1",
                    "vulns": [{"id": "PYSEC-1", "aliases": [], "fix_versions": []}],
                }
            ],
            "fixes": [],
        }
    ).encode()
    for returncode, payload in ((1, clean), (0, vulnerable)):
        monkeypatch.setattr(
            audit,
            "run_bounded_capture",
            lambda arguments, _returncode=returncode, _payload=payload, **_kwargs: (
                subprocess.CompletedProcess(
                    arguments,
                    _returncode,
                    _payload,
                    b"",
                )
            ),
        )
        with pytest.raises(ValueError, match="exit status disagrees"):
            audit.execute_pip_audit_known_vulnerabilities({}, observed_at=_OBSERVED)


def test_installed_inventory_correlates_pyproject_licenses_requirements_and_record(
    tmp_path: Path,
) -> None:
    install_root, _package_file, distributions = _install_fixture(tmp_path)
    result = audit.execute_installed_package_inventory(
        _FIXTURES / "pyproject_inventory_v1.toml",
        distributions=distributions,
        installation_root=install_root,
        observed_at=_OBSERVED,
    )

    assert result.counters.distributions == 3
    assert result.counters.pyproject_required_dependencies == 4
    assert result.counters.pyproject_required_dependencies_applicable == 3
    assert result.counters.pyproject_required_dependencies_installed == 2
    assert result.counters.pyproject_required_dependencies_missing == 1
    assert result.counters.pyproject_required_dependencies_version_compatible == 1
    assert result.counters.pyproject_required_dependencies_version_mismatch == 1
    assert result.counters.pyproject_optional_dependencies == 1
    assert result.counters.record_hash_verified == 1
    assert result.counters.record_size_verified == 1
    assert result.files_hashed == 1
    assert result.bytes_hashed == len(b"healthy")
    assert result.process_invocations == 0
    assert result.uses_network is False
    assert result.freshness_status == "current_at_observation_only"
    assert (
        _metric(result, "package:neocortex-framework", "wheel_record_integrity_current").value == 1
    )
    assert _metric(result, "package:neocortex-framework", "license_metadata_ambiguous").value == 1
    assert _metric(result, "package:demo-dep", "license_metadata_ambiguous").value == 0
    assert (
        _metric(result, "project:installed-environment", "inventory_current_at_observation").value
        == 1
    )
    relation_kinds = {item.relation_kind for item in result.relations}
    assert relation_kinds == {
        "package_declares_license",
        "package_requires_distribution",
        "project_declares_dependency",
    }
    license_relations = [
        item for item in result.relations if item.relation_kind == "package_declares_license"
    ]
    assert license_relations
    assert all(item.metadata["category"] == "license_inventory" for item in license_relations)
    assert all(item.metadata["legal_compatibility_assessed"] is False for item in license_relations)
    dependency = next(
        item
        for item in result.relations
        if item.relation_kind == "project_declares_dependency"
        and item.target_key == "package:demo-dep"
    )
    assert dependency.metadata["target_installed"] is True
    demo_evaluation = _metadata_rows(dependency.metadata["base_dependency_evaluations"])[0]
    assert demo_evaluation["marker_evaluated"] is True
    assert demo_evaluation["marker_applies"] is True
    assert demo_evaluation["presence_gate_evaluated"] is True
    assert demo_evaluation["version_constraint_evaluated"] is True
    assert demo_evaluation["version_compatible"] is True
    assert demo_evaluation["installed_version"] == "1.5"
    import sys

    marker_environment = demo_evaluation["marker_environment"]
    assert isinstance(marker_environment, dict)
    assert marker_environment["python_version"] == (
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )


def test_base_dependency_gates_exclude_false_markers_and_optional_extras(
    tmp_path: Path,
) -> None:
    install_root, _package_file, distributions = _install_fixture(tmp_path)
    result = audit.execute_installed_package_inventory(
        _FIXTURES / "pyproject_inventory_v1.toml",
        distributions=distributions,
        installation_root=install_root,
        observed_at=_OBSERVED,
    )

    assert (
        _metric(
            result,
            "project:installed-environment",
            "pyproject_required_applicable_dependency_count",
        ).value
        == 3
    )
    assert (
        _metric(
            result,
            "project:installed-environment",
            "pyproject_required_missing_dependency_count",
        ).value
        == 1
    )
    assert (
        _metric(
            result,
            "project:installed-environment",
            "pyproject_required_version_mismatch_count",
        ).value
        == 1
    )
    ignored = next(
        item
        for item in result.relations
        if item.relation_kind == "project_declares_dependency"
        and item.target_key == "package:ignored-dep"
    )
    ignored_evaluation = _metadata_rows(ignored.metadata["base_dependency_evaluations"])[0]
    assert ignored_evaluation["marker_applies"] is False
    assert ignored_evaluation["presence_gate_evaluated"] is False
    assert ignored_evaluation["version_constraint_evaluated"] is False
    assert ignored_evaluation["version_compatible"] is None
    mismatch = next(
        item
        for item in result.relations
        if item.relation_kind == "project_declares_dependency"
        and item.target_key == "package:mismatch-dep"
    )
    assert (
        _metadata_rows(mismatch.metadata["base_dependency_evaluations"])[0]["version_compatible"]
        is False
    )
    optional = next(
        item
        for item in result.relations
        if item.relation_kind == "project_declares_dependency"
        and item.target_key == "package:optional-extra"
    )
    optional_metadata = _metadata_rows(optional.metadata["optional_declarations"])[0]
    assert optional_metadata["extra_group_selected"] is False
    assert optional_metadata["presence_gate_evaluated"] is False
    assert optional_metadata["version_constraint_evaluated"] is False
    assert all("recorded_not_evaluated" not in item for item in result.limitations)


def test_installed_inventory_detects_altered_record_member_and_changes_snapshot(
    tmp_path: Path,
) -> None:
    install_root, package_file, distributions = _install_fixture(tmp_path)
    first = audit.execute_installed_package_inventory(
        _FIXTURES / "pyproject_inventory_v1.toml",
        distributions=distributions,
        installation_root=install_root,
        observed_at=_OBSERVED,
    )
    package_file.write_bytes(b"tampered!")
    second = audit.execute_installed_package_inventory(
        _FIXTURES / "pyproject_inventory_v1.toml",
        distributions=distributions,
        installation_root=install_root,
        observed_at=_OBSERVED,
    )

    assert second.counters.record_hash_mismatches == 1
    assert second.counters.record_size_mismatches == 1
    assert (
        _metric(second, "package:neocortex-framework", "wheel_record_integrity_current").value == 0
    )
    assert second.snapshot_id != first.snapshot_id


def test_installed_inventory_has_hard_distribution_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root, _package_file, distributions = _install_fixture(tmp_path)
    monkeypatch.setattr(audit, "_MAX_DISTRIBUTIONS", 1)
    with pytest.raises(ValueError, match="distribution count"):
        audit.execute_installed_package_inventory(
            _FIXTURES / "pyproject_inventory_v1.toml",
            distributions=distributions,
            installation_root=install_root,
            observed_at=_OBSERVED,
        )


def _metadata_rows(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast(list[dict[str, object]], value)


def _record_fixture(
    install_root: Path,
    record: str,
) -> tuple[_FakeDistribution, Path]:
    site_packages = install_root / "Lib" / "site-packages"
    dist_info = site_packages / "neocortex_framework-0.7.2.dist-info"
    dist_info.mkdir(parents=True)
    record_path = dist_info / "RECORD"
    record_path.write_text(record, encoding="utf-8")
    return (
        _FakeDistribution(
            site_packages,
            name="neocortex-framework",
            version="0.7.2",
            record_path=record_path,
        ),
        record_path,
    )


def test_record_verification_mixed_entry_contract_is_deterministic(
    tmp_path: Path,
) -> None:
    from dataclasses import asdict

    install_root = tmp_path / "runtime"
    site_packages = install_root / "Lib" / "site-packages"
    contents = {
        "good.py": b"good",
        "wrong-hash.py": b"actual",
        "wrong-size.py": b"x",
        "invalid-size.py": b"size",
        "invalid-hash.py": b"hash",
        "blank.txt": b"blank",
        "pkg/cache.pyc": b"cache",
    }
    for relative_path, content in contents.items():
        target = site_packages / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (site_packages / "directory").mkdir()

    def encoded_digest(content: bytes) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")

    record = (
        "\n".join(
            (
                f"good.py,sha256={encoded_digest(contents['good.py'])},4",
                f"wrong-hash.py,sha256={encoded_digest(b'different')},6",
                f"wrong-size.py,sha256={encoded_digest(contents['wrong-size.py'])},2",
                f"missing.py,sha256={encoded_digest(b'missing')},7",
                f"../../../outside.txt,sha256={encoded_digest(b'outside')},7",
                f"directory,sha256={encoded_digest(b'')},0",
                "invalid-size.py,,not-a-number",
                "invalid-hash.py,sha999=YWJj,4",
                "blank.txt,,",
                "pkg/cache.pyc,,",
                "neocortex_framework-0.7.2.dist-info/RECORD,,",
                "too,few",
                ",sha256=YWJj,1",
            )
        )
        + "\n"
    )
    fake_distribution, record_path = _record_fixture(install_root, record)
    distribution = cast(Distribution, fake_distribution)
    before = dict(contents)

    first = audit._record_verification(distribution, installation_root=install_root)
    second = audit._record_verification(distribution, installation_root=install_root)

    assert first == second
    assert asdict(first) == {
        "present": True,
        "digest": hashlib.sha256(record.encode("utf-8")).hexdigest(),
        "entries": 13,
        "hash_verified": 2,
        "size_verified": 3,
        "missing_files": 1,
        "hash_mismatches": 1,
        "size_mismatches": 1,
        "unverifiable_entries": 3,
        "unsafe_entries": 2,
        "malformed_entries": 4,
        "files_hashed": 3,
        "bytes_hashed": 11,
    }
    assert first.current is False
    assert record_path.read_text("utf-8") == record
    assert {path: (site_packages / path).read_bytes() for path in contents} == before


def test_record_verification_absence_and_signature_are_frozen(tmp_path: Path) -> None:
    from dataclasses import asdict
    from inspect import signature

    distribution = cast(
        Distribution,
        _FakeDistribution(
            tmp_path,
            name="neocortex-framework",
            version="0.7.2",
        ),
    )

    assert str(signature(audit._record_verification)) == (
        "(distribution: 'importlib.metadata.Distribution', *, "
        "installation_root: 'Path') -> '_RecordVerification'"
    )
    assert asdict(audit._record_verification(distribution, installation_root=tmp_path)) == {
        "present": False,
        "digest": None,
        "entries": 0,
        "hash_verified": 0,
        "size_verified": 0,
        "missing_files": 0,
        "hash_mismatches": 0,
        "size_mismatches": 0,
        "unverifiable_entries": 0,
        "unsafe_entries": 0,
        "malformed_entries": 0,
        "files_hashed": 0,
        "bytes_hashed": 0,
    }


def test_record_verification_bounds_fail_atomically_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root, package_file, distributions = _install_fixture(tmp_path)
    distribution = distributions[0]
    record_path = cast(_FakeDistribution, distribution)._record_path
    assert record_path is not None
    record_bytes = record_path.read_bytes()
    logical_record_bytes = record_path.read_text("utf-8").encode("utf-8")
    package_bytes = package_file.read_bytes()

    monkeypatch.setattr(audit, "_MAX_RECORD_BYTES", len(logical_record_bytes) - 1)
    with pytest.raises(ValueError, match="byte bound"):
        audit._record_verification(distribution, installation_root=install_root)
    monkeypatch.setattr(audit, "_MAX_RECORD_BYTES", len(logical_record_bytes))

    monkeypatch.setattr(audit, "_MAX_RECORD_ENTRIES", 1)
    with pytest.raises(ValueError, match="entry count"):
        audit._record_verification(distribution, installation_root=install_root)
    monkeypatch.setattr(audit, "_MAX_RECORD_ENTRIES", 2)

    monkeypatch.setattr(audit, "_MAX_RECORD_HASH_BYTES", len(package_bytes) - 1)
    with pytest.raises(ValueError, match="hash bytes"):
        audit._record_verification(distribution, installation_root=install_root)
    monkeypatch.setattr(audit, "_MAX_RECORD_HASH_BYTES", len(package_bytes))

    recovered = audit._record_verification(distribution, installation_root=install_root)

    assert recovered.current is True
    assert recovered.files_hashed == 1
    assert recovered.bytes_hashed == len(package_bytes)
    assert record_path.read_bytes() == record_bytes
    assert package_file.read_bytes() == package_bytes
