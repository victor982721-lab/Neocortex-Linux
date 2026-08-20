"""Registry and exact-replay contracts for Hito 5 supply-chain providers."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.external_evidence_providers as providers_module
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidenceFile
from _04_Nucleo_Operativo.external_dependency_hygiene import (
    DEPTRY_LIMITATIONS,
    DependencyHygieneExecution,
)
from _04_Nucleo_Operativo.external_evidence_models import ExternalProviderBaseline
from _04_Nucleo_Operativo.external_evidence_providers import (
    DeptryProjectDependenciesProvider,
    InstalledPackageInventoryProvider,
    PipAuditKnownVulnerabilitiesProvider,
    SemgrepNeocortexInvariantsProvider,
    providers_for_profile,
)
from _04_Nucleo_Operativo.external_semgrep_invariants import (
    SEMGREP_INVARIANT_RULE_IDS,
    SEMGREP_RULESET_SHA256,
    SemgrepInvariantExecution,
)
from _04_Nucleo_Operativo.external_supply_chain_audit import (
    PIP_AUDIT_LIMITATIONS,
    InstalledPackageCounters,
    InstalledPackageInventoryExecution,
    PipAuditCounters,
    PipAuditExecution,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes


def _root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    (root / "pyproject.toml").write_text(
        """[project]
name = "neocortex-framework"
dependencies = ["example>=1"]
[tool.ruff]
[tool.mypy]
[tool.pyright]
""",
        encoding="utf-8",
    )
    return root, scratch


def _file(path: Path, root: Path, version_id: int) -> ExternalEvidenceFile:
    observed = path.stat()
    digest = fingerprint_bytes(path.read_bytes())
    return ExternalEvidenceFile(
        version_id,
        str(path),
        path.relative_to(root).as_posix(),
        observed.st_size,
        observed.st_mtime_ns,
        digest.xxh3_128,
        digest.xxh3_64_guard,
    )


def _baseline(
    publication,
    tool_run_id: int = 71,
    *,
    fresh_until_unix_seconds: float | None = None,
) -> ExternalProviderBaseline:
    assert publication.result_digest is not None
    return ExternalProviderBaseline(
        tool_run_id,
        publication.descriptor.provider_id,
        publication.publication.tool_version,
        publication.input_signature,
        publication.descriptor.comparability_signature,
        publication.result_digest,
        tuple(item.portable_finding_id for item in publication.findings),
        (),
        (),
        fresh_until_unix_seconds,
        reuse_mode="exact_replay",
    )


def test_semgrep_and_deptry_use_their_exact_python_domains_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, scratch = _root(tmp_path)
    py_file = root / "module.py"
    stub_file = root / "module.pyi"
    pyw_file = root / "window.pyw"
    rule_fixture = (
        root
        / "tests"
        / "fixtures"
        / "semgrep_invariants"
        / "_04_Nucleo_Operativo"
        / "external_fixture_provider.py"
    )
    rule_fixture.parent.mkdir(parents=True)
    for path in (py_file, stub_file, pyw_file, rule_fixture):
        path.write_text("VALUE = 1\n", encoding="utf-8")
    files = tuple(
        _file(path, root, version_id)
        for version_id, path in enumerate((py_file, stub_file, pyw_file, rule_fixture), start=1)
    )
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "test-1")
    monkeypatch.setattr(providers_module, "managed_semgrep_version", lambda: "1.172.0")

    semgrep_paths: list[tuple[str, ...]] = []

    def semgrep_execute(_stage, staged, _environment):
        selected = tuple(sorted(item.relative_path for item in staged.values()))
        semgrep_paths.append(selected)
        limitations = (
            "semgrep_ce_single_file_analysis",
            "local_neocortex_rules_only",
            "advisory_only_no_mutation_authority",
            "autofix_disabled",
        ) + (("windows_pysemgrep_x509_compatibility",) if os.name == "nt" else ())
        return SemgrepInvariantExecution(
            (),
            11,
            0,
            1,
            len(selected),
            sum(item.size for item in staged.values()),
            len(SEMGREP_INVARIANT_RULE_IDS),
            "pysemgrep" if os.name == "nt" else "semgrep",
            SEMGREP_RULESET_SHA256,
            "manifest:fixture",
            limitations,
        )

    semgrep = SemgrepNeocortexInvariantsProvider(root, executor=semgrep_execute)
    semgrep_publication = semgrep.run(root, files, baseline=None, scratch_root=scratch)
    assert semgrep_paths == [("module.py", "module.pyi")]
    assert semgrep_publication.status == "completed"
    assert semgrep_publication.counters["semgrep_rule_count"] == 3
    semgrep_replay = semgrep.run(
        root,
        files,
        baseline=_baseline(semgrep_publication),
        scratch_root=scratch,
    )
    assert semgrep_replay.execution == "cache_replay"
    assert semgrep_replay.counters["process_invocations"] == 0

    deptry_paths: list[tuple[str, ...]] = []

    def deptry_execute(_stage, staged, _config, _environment):
        deptry_paths.append(tuple(sorted(item.relative_path for item in staged.values())))
        return DependencyHygieneExecution(
            (),
            (),
            (),
            7,
            0,
            1,
            DEPTRY_LIMITATIONS,
            {"dependency_issue_count": 0, "dependency_gate_issue_count": 0},
        )

    deptry = DeptryProjectDependenciesProvider(root, executor=deptry_execute)
    deptry_publication = deptry.run(root, files, baseline=None, scratch_root=scratch)
    assert deptry_paths == [
        (
            "module.py",
            "tests/fixtures/semgrep_invariants/_04_Nucleo_Operativo/external_fixture_provider.py",
        )
    ]
    assert deptry_publication.status == "completed"
    assert deptry_publication.counters["dependency_gate_issue_count"] == 0
    deptry_replay = deptry.run(
        root,
        files,
        baseline=_baseline(deptry_publication, 72),
        scratch_root=scratch,
    )
    assert deptry_replay.execution == "cache_replay"
    assert deptry_replay.counters["files_verified"] == 2


def test_environment_providers_replay_declared_snapshots_with_real_inventory_costs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, scratch = _root(tmp_path)
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "test-1")
    monkeypatch.setattr(
        providers_module,
        "_installed_distribution_signature",
        lambda **_kwargs: "installed-environment:fixture",
    )
    pip_limitations = PIP_AUDIT_LIMITATIONS
    pip_provider = PipAuditKnownVulnerabilitiesProvider(root)

    def execute_pip_audit(environment: dict[str, str]) -> PipAuditExecution:
        temporary_paths = {Path(environment[name]) for name in ("TEMP", "TMP", "TMPDIR")}
        assert len(temporary_paths) == 1
        temporary = temporary_paths.pop()
        assert temporary.is_dir()
        assert temporary.parent == scratch
        return PipAuditExecution(
            (),
            (),
            PipAuditCounters(5, 5, 0, 0, 0, 0),
            "test-1",
            "fixture",
            f"{pip_provider._utc_date}T00:00:00Z",
            pip_provider._utc_date,
            "snapshot:pip",
            "fresh_at_observation",
            f"{pip_provider._utc_date}T23:59:59Z",
            19,
            0,
            1,
            True,
            pip_limitations,
        )

    pip_provider.executor = execute_pip_audit
    pip_publication = pip_provider.run(root, (), baseline=None, scratch_root=scratch)
    assert pip_publication.status == "completed"
    assert pip_publication.descriptor.uses_network is True
    assert pip_publication.counters["packages_audited"] == 5
    pip_provider.executor = lambda _environment: pytest.fail("pip-audit executed on replay")
    pip_replay = pip_provider.run(
        root,
        (),
        baseline=_baseline(
            pip_publication,
            73,
            fresh_until_unix_seconds=time.time() + 3600,
        ),
        scratch_root=scratch,
    )
    assert pip_replay.execution == "cache_replay"
    assert pip_replay.counters["process_invocations"] == 0

    executions = 0

    def execute_after_expiry(environment: dict[str, str]) -> PipAuditExecution:
        nonlocal executions
        executions += 1
        return execute_pip_audit(environment)

    pip_provider.executor = execute_after_expiry
    expired = pip_provider.run(
        root,
        (),
        baseline=_baseline(
            pip_publication,
            74,
            fresh_until_unix_seconds=time.time() - 1,
        ),
        scratch_root=scratch,
    )
    assert expired.execution == "full"
    assert executions == 1

    inventory_limitations = (
        "optional_extra_and_transitive_requirement_constraints_are_recorded_not_gated",
        "base_direct_url_origin_is_recorded_not_verified",
        "license_metadata_is_inventory_not_legal_compatibility_analysis",
        "multiple_license_declarations_remain_explicitly_ambiguous",
        "record_verification_cannot_detect_files_omitted_from_record_without_enumeration",
        "inventory_is_current_only_at_its_observation_time",
        "advisory_only_no_mutation_authority",
    )
    inventory_execution = InstalledPackageInventoryExecution(
        (),
        (),
        InstalledPackageCounters(2, 1, 1, 1, 1, 0, 1, 0, 0, 2, 0, 0, 3, 2, 2, 0, 0, 0, 0, 0),
        "fixture",
        "2026-08-03T00:00:00Z",
        "2026-08-03",
        "snapshot:inventory",
        "current_at_observation_only",
        "pyproject:sha256:fixture",
        "test-1",
        2,
        4096,
        0,
        False,
        inventory_limitations,
    )
    calls = 0

    def inventory_execute(_pyproject):
        nonlocal calls
        calls += 1
        return inventory_execution

    inventory = InstalledPackageInventoryProvider(root, executor=inventory_execute)
    signature = inventory.baseline_input_signature(())
    first = inventory.run(root, (), baseline=None, scratch_root=scratch)
    assert calls == 1
    assert first.input_signature == signature
    assert first.counters["inventory_files_hashed"] == 2
    assert first.counters["bytes_read"] == 4096
    replay = inventory.run(
        root,
        (),
        baseline=_baseline(first, 74),
        scratch_root=scratch,
    )
    assert calls == 1
    assert replay.execution == "cache_replay"
    assert replay.counters["inventory_bytes_hashed"] == 4096
    assert replay.counters["bytes_read"] == 4096


def test_pip_audit_snapshot_is_bound_to_local_supply_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _scratch = _root(tmp_path)
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "test-1")
    monkeypatch.setattr(
        providers_module,
        "_installed_distribution_signature",
        lambda **_kwargs: "installed-environment:fixture",
    )

    before = PipAuditKnownVulnerabilitiesProvider(root)
    before_signature = before.baseline_input_signature(())
    (root / "constraints-linux-cp314.lock").write_text(
        "example==1.0\n",
        encoding="utf-8",
    )
    after = PipAuditKnownVulnerabilitiesProvider(root)

    assert after.baseline_input_signature(()) != before_signature
    assert after._source_input_bytes > before._source_input_bytes
    assert after.descriptor.configuration_signature != (before.descriptor.configuration_signature)


def test_code_validation_offline_policy_never_invokes_pip_audit_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, scratch = _root(tmp_path)
    monkeypatch.setattr(providers_module, "_package_version", lambda _name: "test-1")
    monkeypatch.setattr(
        providers_module,
        "_installed_distribution_signature",
        lambda **_kwargs: "installed-environment:fixture",
    )
    monkeypatch.setenv(
        "NEOCORTEX_PIP_AUDIT_NETWORK_POLICY",
        "disabled-by-code-validation",
    )
    provider = PipAuditKnownVulnerabilitiesProvider(
        root,
        executor=lambda _environment: pytest.fail("offline validation attempted network"),
    )

    publication = provider.run(root, (), baseline=None, scratch_root=scratch)

    assert publication.status == "failed"
    assert publication.counters["process_invocations"] == 0
    assert publication.publication.provenance["error"]["reason"] == (
        "pip_audit_network_unavailable:disabled_by_local_code_validation_policy"
    )


def test_trusted_static_registry_exposes_supply_chain_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _scratch = _root(tmp_path)
    monkeypatch.setattr(providers_module, "_package_version", lambda name: f"{name}-test")
    monkeypatch.setattr(
        providers_module,
        "_pyright_locations",
        lambda: (Path("node"), Path("pyright.js"), "pyright-test"),
    )
    monkeypatch.setattr(
        providers_module,
        "_installed_distribution_signature",
        lambda **_kwargs: "installed-environment:fixture",
    )

    providers = providers_for_profile("trusted-static", root)
    by_id = {item.descriptor.provider_id: item.descriptor for item in providers}

    assert len(providers) == 13
    assert {
        "semgrep-neocortex-invariants",
        "deptry-project-dependencies",
        "pip-audit-known-vulnerabilities",
        "installed-package-inventory",
    }.issubset(by_id)
    assert by_id["pip-audit-known-vulnerabilities"].uses_network is True
    assert by_id["installed-package-inventory"].uses_network is False
    assert all(by_id[name].authority == "advisory" for name in by_id)
    assert all(by_id[name].mutation_authority is False for name in by_id)
