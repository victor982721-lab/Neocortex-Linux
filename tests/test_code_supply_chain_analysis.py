"""Deterministic schema-v4 consumer tests for Hito 5 supply-chain evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidencePublication
from _04_Nucleo_Operativo.code_supply_chain_analysis import (
    CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT,
    CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS,
    CODE_SUPPLY_CHAIN_SCHEMA,
    analyze_code_supply_chain,
    parse_code_supply_chain_payload,
    read_code_supply_chain_analysis,
)
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderPublication,
    ExternalProviderRelation,
    ExternalRunInput,
    ProviderDescriptor,
    ProviderLimits,
    external_metric_identity,
    external_provider_result_digest,
    external_relation_identity,
)
from _04_Nucleo_Operativo.external_evidence_store import publish_external_provider
from tests.test_external_provider_schema_v4 import _create_current_owner

_OBSERVED_AT = 1_700_000_000
_NOW_UTC = dt.datetime(2023, 11, 15, tzinfo=dt.UTC)
_FRESH_UNTIL = int(dt.datetime(2023, 11, 16, tzinfo=dt.UTC).timestamp())
_STALE_UNTIL = int(dt.datetime(2023, 11, 14, tzinfo=dt.UTC).timestamp())


def _descriptor(provider_id: str) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id,
        f"neocortex.{provider_id}/v1",
        {
            "semgrep-neocortex-invariants": "semgrep",
            "deptry-project-dependencies": "deptry",
            "pip-audit-known-vulnerabilities": "pip-audit",
            "installed-package-inventory": "importlib-metadata",
        }[provider_id],
        "trusted-static",
        "trusted-static",
        "project",
        f"external:{provider_id}",
        f"fixture-configuration:{provider_id}",
        "fixture-project-configuration",
        "fixture-environment",
        f"fixture-comparability:{provider_id}",
        "bounded-fixture",
        "project_wide",
        "exact-input",
        ProviderLimits(1.0, 1_000_000, 1_000_000, 4_000_000, 1_000),
    )


def _finding(
    provider_id: str,
    index: int,
    category: str,
    *,
    gate_authority: str = "advisory",
) -> ExternalProviderFinding:
    return ExternalProviderFinding(
        f"finding:{provider_id}:{index:04d}",
        1,
        "a.py",
        category,
        f"CODE{index:04d}",
        "warning",
        f"fixture finding {index}",
        True,
        1.0,
        None,
        gate_authority,
        index + 1,
        0,
        index + 1,
        1,
        metadata={"source": "fixture-source"},
    )


def _metric(
    provider_id: str,
    metric_name: str,
    value: float,
    *,
    category: str,
    subject_key: str = "installed-environment",
    unit: str = "count",
    metadata: Mapping[str, object] | None = None,
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        external_metric_identity(
            provider_id,
            subject_kind="project",
            subject_key=subject_key,
            category=category,
            metric_name=metric_name,
            unit=unit,
        ),
        "project",
        subject_key,
        category,
        metric_name,
        value,
        unit,
        project_id=None,
        metadata={"source": "fixture-source", **(metadata or {})},
    )


def _license_relation(provider_id: str) -> ExternalProviderRelation:
    return ExternalProviderRelation(
        external_relation_identity(
            provider_id,
            relation_kind="package_declares_license",
            source_kind="project",
            source_key="neocortex",
            target_kind="contract",
            target_key="MIT",
        ),
        "package_declares_license",
        "project",
        "neocortex",
        "contract",
        "MIT",
        metadata={
            "category": "license_inventory",
            "source": "installed-metadata",
            "legal_compatibility_assessed": False,
        },
    )


def _provider_records(
    provider_id: str,
    *,
    findings: bool,
    audit_fresh_until: float | None,
    semgrep_count: int,
) -> tuple[
    tuple[ExternalProviderFinding, ...],
    tuple[ExternalProviderMetric, ...],
    tuple[ExternalProviderRelation, ...],
]:
    records: tuple[ExternalProviderFinding, ...]
    metrics: tuple[ExternalProviderMetric, ...]
    if provider_id == "semgrep-neocortex-invariants":
        records = tuple(
            _finding(provider_id, index, "project_invariant") for index in range(semgrep_count)
        )
        return records, (), ()
    if provider_id == "deptry-project-dependencies":
        records = (
            (
                _finding(
                    provider_id,
                    1,
                    "dependency_hygiene",
                    gate_authority="dependency_declaration_integrity",
                ),
                _finding(provider_id, 2, "dependency_hygiene"),
            )
            if findings
            else ()
        )
        metrics = (
            _metric(
                provider_id,
                "dependency_issue_count",
                float(len(records)),
                category="dependency_hygiene",
                subject_key="project:neocortex-framework",
            ),
            _metric(
                provider_id,
                "dependency_gate_issue_count",
                2 if findings else 0,
                category="dependency_hygiene",
                subject_key="project:neocortex-framework",
            ),
        )
        return records, metrics, ()
    if provider_id == "pip-audit-known-vulnerabilities":
        records = ()
        freshness_metadata = (
            {}
            if audit_fresh_until is None or audit_fresh_until < 0
            else {
                "fresh_until_utc": dt.datetime.fromtimestamp(
                    audit_fresh_until,
                    tz=dt.UTC,
                )
                .isoformat()
                .replace("+00:00", "Z")
            }
        )
        metric_items = [
            _metric(
                provider_id,
                "audit_observed_at_unix_seconds",
                _OBSERVED_AT,
                category="known_vulnerability",
                subject_key="project:installed-environment",
                unit="unix_seconds",
                metadata=freshness_metadata,
            ),
            _metric(
                provider_id,
                "audit_current_at_observation",
                1,
                category="known_vulnerability",
                subject_key="project:installed-environment",
                unit="boolean",
                metadata=freshness_metadata,
            ),
            _metric(
                provider_id,
                "known_vulnerability_count",
                1 if findings else 0,
                category="known_vulnerability",
                subject_key="project:installed-environment",
                metadata=freshness_metadata,
            ),
            _metric(
                provider_id,
                "known_vulnerability_count",
                1 if findings else 0,
                category="known_vulnerability",
                subject_key="package:fixture-dependency",
                metadata=freshness_metadata,
            ),
        ]
        if audit_fresh_until is not None:
            metric_items.append(
                _metric(
                    provider_id,
                    "audit_fresh_until_unix_seconds",
                    audit_fresh_until,
                    category="known_vulnerability",
                    subject_key="project:installed-environment",
                    unit="unix_seconds",
                    metadata=freshness_metadata,
                )
            )
        relations: tuple[ExternalProviderRelation, ...] = ()
        if findings:
            metric_items.append(
                _metric(
                    provider_id,
                    "known_vulnerability:PYSEC-FIXTURE",
                    1,
                    category="known_vulnerability",
                    subject_key="package:fixture-dependency",
                    metadata={"vulnerability_id": "PYSEC-FIXTURE", **freshness_metadata},
                )
            )
            relation_kind = "package_has_known_vulnerability"
            relations = (
                ExternalProviderRelation(
                    external_relation_identity(
                        provider_id,
                        relation_kind=relation_kind,
                        source_kind="project",
                        source_key="package:fixture-dependency",
                        target_kind="contract",
                        target_key="advisory:PYSEC-FIXTURE",
                    ),
                    relation_kind,
                    "project",
                    "package:fixture-dependency",
                    "contract",
                    "advisory:PYSEC-FIXTURE",
                    metadata={
                        "category": "known_vulnerability",
                        "vulnerability_id": "PYSEC-FIXTURE",
                    },
                ),
            )
        return records, tuple(metric_items), relations
    records = ()
    metrics = (
        _metric(
            provider_id,
            "inventory_observed_at_unix_seconds",
            _OBSERVED_AT,
            category="package_integrity",
            subject_key="project:installed-environment",
            unit="unix_seconds",
        ),
        _metric(
            provider_id,
            "inventory_current_at_observation",
            1,
            category="package_integrity",
            subject_key="project:installed-environment",
            unit="boolean",
        ),
        _metric(
            provider_id,
            "wheel_record_integrity_current",
            0 if findings else 1,
            category="package_integrity",
            subject_key="package:neocortex-framework",
            unit="boolean",
        ),
        _metric(
            provider_id,
            "record_missing_file_count",
            0,
            category="package_integrity",
            subject_key="package:neocortex-framework",
        ),
        _metric(
            provider_id,
            "record_hash_mismatch_count",
            1 if findings else 0,
            category="package_integrity",
            subject_key="package:neocortex-framework",
        ),
        _metric(
            provider_id,
            "record_size_mismatch_count",
            0,
            category="package_integrity",
            subject_key="package:neocortex-framework",
        ),
        _metric(
            provider_id,
            "record_unverifiable_entry_count",
            0,
            category="package_integrity",
            subject_key="package:neocortex-framework",
        ),
        _metric(
            provider_id,
            "record_unsafe_entry_count",
            0,
            category="package_integrity",
            subject_key="package:neocortex-framework",
        ),
        _metric(
            provider_id,
            "record_malformed_entry_count",
            0,
            category="package_integrity",
            subject_key="package:neocortex-framework",
        ),
        _metric(
            provider_id,
            "pyproject_required_missing_dependency_count",
            0,
            category="package_integrity",
            subject_key="project:installed-environment",
        ),
        _metric(
            provider_id,
            "pyproject_required_version_mismatch_count",
            1 if findings else 0,
            category="package_integrity",
            subject_key="project:installed-environment",
        ),
        _metric(
            provider_id,
            "packages_with_license_metadata",
            1,
            category="license_inventory",
            subject_key="project:installed-environment",
        ),
        _metric(
            provider_id,
            "packages_with_ambiguous_license_metadata",
            0,
            category="license_inventory",
            subject_key="project:installed-environment",
        ),
        _metric(
            provider_id,
            "packages_without_license_metadata",
            1 if findings else 0,
            category="license_inventory",
            subject_key="project:installed-environment",
        ),
    )
    return records, metrics, (_license_relation(provider_id),)


def _publication(
    provider_id: str,
    *,
    findings: bool,
    audit_fresh_until: float | None,
    semgrep_count: int,
) -> ExternalProviderPublication:
    provider_findings, metrics, relations = _provider_records(
        provider_id,
        findings=findings,
        audit_fresh_until=audit_fresh_until,
        semgrep_count=semgrep_count,
    )
    digest = external_provider_result_digest(provider_findings, metrics, relations)
    return ExternalProviderPublication(
        _descriptor(provider_id),
        ExternalEvidencePublication(
            _descriptor(provider_id).tool_name,
            "1.0",
            f"fixture-configuration:{provider_id}",
            "completed",
            1,
            2,
            {"execution": "full"},
        ),
        "C:/fixture",
        "fixture-root",
        "fixture-input",
        (ExternalRunInput(1, "input:a.py", "a.py", True, True, None, 4, "digest"),),
        provider_findings,
        {
            "eligible_files": 1,
            "covered_files": 1,
            "findings": len(provider_findings),
            "metrics": len(metrics),
            "relations": len(relations),
            "comparable": 0,
        },
        True,
        digest,
        f"publication:{provider_id}:full",
        metrics=metrics,
        relations=relations,
    )


def _replay(
    publication: ExternalProviderPublication,
    source_tool_run_id: int,
) -> ExternalProviderPublication:
    return ExternalProviderPublication(
        publication.descriptor,
        replace(
            publication.publication,
            status="skipped",
            started_ns=3,
            completed_ns=4,
            provenance={"execution": "cache_replay"},
        ),
        publication.observed_root,
        publication.root_identity,
        publication.input_signature,
        publication.inputs,
        (),
        {
            "eligible_files": 1,
            "covered_files": 1,
            "files_verified": 1,
            "bytes_verified": 4,
            "findings": len(publication.findings),
            "metrics": len(publication.metrics),
            "relations": len(publication.relations),
            "comparable": 1,
            "added": 0,
            "resolved": 0,
        },
        True,
        publication.result_digest,
        f"publication:{publication.descriptor.provider_id}:replay",
        source_tool_run_id,
        "verification:fixture",
    )


def _database(
    tmp_path: Path,
    *,
    findings: bool = True,
    audit_fresh_until: float | None = _FRESH_UNTIL,
    semgrep_count: int | None = None,
    providers: tuple[str, ...] = CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS,
) -> Path:
    database = tmp_path / "code.sqlite3"
    _create_current_owner(database, 1, 2)
    publications = tuple(
        _publication(
            provider_id,
            findings=findings,
            audit_fresh_until=audit_fresh_until,
            semgrep_count=(1 if findings else 0) if semgrep_count is None else semgrep_count,
        )
        for provider_id in providers
    )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        source_ids = tuple(
            publish_external_provider(connection, 1, publication) for publication in publications
        )
        connection.execute(
            "UPDATE analysis_runs SET status='completed',completed_ns=9 "
            "WHERE analysis_run_id=1"
        )
        connection.commit()
        for publication, source_id in zip(publications, source_ids, strict=True):
            publish_external_provider(connection, 2, _replay(publication, source_id))
        connection.execute(
            "UPDATE analysis_runs SET status='completed',completed_ns=10 "
            "WHERE analysis_run_id=2"
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _read(database: Path, run_id: int):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return read_code_supply_chain_analysis(
            connection,
            run_id,
            database=str(database),
            now_utc=_NOW_UTC,
        )
    finally:
        connection.close()


def _gate_map(analysis) -> dict[str, str]:
    return {gate.gate: gate.status for gate in analysis.gates}


def test_supply_chain_analysis_is_ready_and_replay_stable(tmp_path: Path) -> None:
    database = _database(tmp_path)

    baseline = _read(database, 1)
    replay = _read(database, 2)

    assert baseline.status == replay.status == "ready"
    assert baseline.digest == replay.digest
    assert baseline.as_payload()["schema"] == CODE_SUPPLY_CHAIN_SCHEMA
    assert tuple(item.provider_id for item in baseline.providers) == (
        "semgrep-neocortex-invariants",
        "deptry-project-dependencies",
        "pip-audit-known-vulnerabilities",
        "installed-package-inventory",
    )
    assert all(item.status == "ready" for item in baseline.providers)
    assert all(item.authority == "advisory" for item in baseline.providers)
    assert all(item.mutation_authority is False for item in baseline.providers)
    assert {item.category for item in baseline.observations} == {
        "project_invariant",
        "dependency_hygiene",
        "known_vulnerability",
        "package_integrity",
        "license_inventory",
    }
    assert len({item.observation_id for item in baseline.observations}) == len(
        baseline.observations
    )
    evidence_kinds = tuple(item.evidence_kind for item in baseline.observations)
    assert max(index for index, kind in enumerate(evidence_kinds) if kind == "finding") < min(
        index for index, kind in enumerate(evidence_kinds) if kind == "metric"
    )
    assert _gate_map(baseline) == {
        "semgrep_invariants": "failed",
        "dependency_declaration_integrity": "failed",
        "vulnerability_snapshot_current": "passed",
        "no_known_vulnerabilities": "failed",
        "installed_package_integrity": "failed",
        "license_inventory_available": "passed",
    }
    assert baseline.status == "ready"  # failed advisory gates remain readable evidence
    inventory_status = next(
        item for item in baseline.providers if item.provider_id == "installed-package-inventory"
    )
    assert inventory_status.findings == 0
    vulnerability_gate = next(
        item for item in baseline.gates if item.gate == "no_known_vulnerabilities"
    )
    assert vulnerability_gate.evidence_count == 1
    dependency_gate = next(
        item for item in baseline.gates if item.gate == "dependency_declaration_integrity"
    )
    assert dependency_gate.evidence_count == 2
    integrity_gate = next(
        item for item in baseline.gates if item.gate == "installed_package_integrity"
    )
    assert "record_hash_mismatch_count=1" in integrity_gate.reason
    assert "pyproject_required_version_mismatch_count=1" in integrity_gate.reason
    assert "license_metadata_incomplete_or_ambiguous" in baseline.limitations
    pip_status = next(
        item for item in baseline.providers if item.provider_id == "pip-audit-known-vulnerabilities"
    )
    assert pip_status.observed_date == "2023-11-14"
    assert pip_status.freshness == "current"
    assert all(item.execution == "cache_replay" for item in replay.providers)
    payload = json.dumps(baseline.as_payload(), ensure_ascii=False, sort_keys=True)
    assert len(payload.encode("utf-8")) < 512 * 1024
    assert parse_code_supply_chain_payload(json.loads(payload)) == baseline


def test_supply_chain_wire_rejects_a_tampered_gate_without_a_new_digest(
    tmp_path: Path,
) -> None:
    analysis = _read(_database(tmp_path, findings=False), 1)
    payload = json.loads(json.dumps(analysis.as_payload()))
    payload["gates"][0]["status"] = "failed"

    with pytest.raises(ValueError, match="digest"):
        parse_code_supply_chain_payload(payload)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    (
        (
            "providers",
            "mutation_authority",
            True,
            "supply-chain provider authority or status is invalid",
        ),
        (
            "observations",
            "authority",
            "binding",
            "supply-chain observation semantics are invalid",
        ),
        (
            "counts",
            "findings",
            -1,
            "supply-chain findings must be a non-negative integer",
        ),
        (
            "gates",
            "evidence_count",
            -1,
            "supply-chain gate evidence count must be a non-negative integer",
        ),
        (
            "digest",
            "byte_count",
            -1,
            "supply-chain digest byte count must be a non-negative integer",
        ),
        (
            None,
            "mutation_authority",
            True,
            "supply-chain analysis authority is invalid",
        ),
    ),
)
def test_supply_chain_wire_validates_semantics_before_accepting_a_digest(
    tmp_path: Path,
    section: str | None,
    field: str,
    value: object,
    message: str,
) -> None:
    analysis = _read(_database(tmp_path, findings=False), 1)
    payload = json.loads(json.dumps(analysis.as_payload()))
    target = payload if section is None else payload[section]
    if isinstance(target, list):
        target = target[0]
    target[field] = value

    with pytest.raises(ValueError, match=message):
        parse_code_supply_chain_payload(payload)


def test_zero_findings_is_valid_and_all_absolute_gates_pass(tmp_path: Path) -> None:
    analysis = _read(_database(tmp_path, findings=False), 1)

    assert analysis.status == "ready"
    assert analysis.counts.findings == 0
    assert set(_gate_map(analysis).values()) == {"passed"}
    assert analysis.counts.metrics > 0
    assert analysis.counts.license_inventory > 0


def test_missing_provider_never_passes_its_gates(tmp_path: Path) -> None:
    providers = tuple(
        item
        for item in CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS
        if item != "pip-audit-known-vulnerabilities"
    )
    analysis = _read(_database(tmp_path, findings=False, providers=providers), 1)

    assert analysis.status == "abstained"
    assert analysis.reason is not None
    assert "pip-audit-known-vulnerabilities:provider_not_recorded" in analysis.reason
    audit_status = next(
        item for item in analysis.providers if item.provider_id == "pip-audit-known-vulnerabilities"
    )
    assert audit_status.status == "not_recorded"
    gates = _gate_map(analysis)
    assert gates["vulnerability_snapshot_current"] == "not_evaluated"
    assert gates["no_known_vulnerabilities"] == "not_evaluated"


def test_stale_vulnerability_snapshot_is_readable_but_cannot_clear_gate(
    tmp_path: Path,
) -> None:
    analysis = _read(
        _database(tmp_path, findings=False, audit_fresh_until=_STALE_UNTIL),
        1,
    )

    assert analysis.status == "ready"
    audit_status = next(
        item for item in analysis.providers if item.provider_id == "pip-audit-known-vulnerabilities"
    )
    assert audit_status.freshness == "stale"
    gates = _gate_map(analysis)
    assert gates["vulnerability_snapshot_current"] == "failed"
    assert gates["no_known_vulnerabilities"] == "abstained"


def test_missing_or_invalid_freshness_deadline_never_passes_snapshot_gate(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing-deadline"
    missing_root.mkdir()
    missing = _read(
        _database(missing_root, findings=False, audit_fresh_until=None),
        1,
    )
    invalid_root = tmp_path / "invalid-deadline"
    invalid_root.mkdir()
    invalid = _read(
        _database(invalid_root, findings=False, audit_fresh_until=-1),
        1,
    )

    for analysis, limitation in (
        (missing, "freshness_deadline_not_recorded"),
        (invalid, "freshness_deadline_invalid"),
    ):
        audit_status = next(
            item
            for item in analysis.providers
            if item.provider_id == "pip-audit-known-vulnerabilities"
        )
        assert audit_status.freshness == "unknown"
        assert limitation in audit_status.limitations
        gates = _gate_map(analysis)
        assert gates["vulnerability_snapshot_current"] == "abstained"
        assert gates["no_known_vulnerabilities"] == "abstained"


def test_observation_bound_keeps_complete_counts(tmp_path: Path) -> None:
    total = CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT + 25
    analysis = _read(
        _database(
            tmp_path,
            findings=False,
            semgrep_count=total,
        ),
        1,
    )

    assert analysis.status == "ready"
    assert analysis.counts.project_invariant == total
    assert analysis.counts.observations == CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT
    assert analysis.counts.observations_truncated is True
    assert len(analysis.observations) == CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT
    assert "supply_chain_observations_truncated" in analysis.limitations


def test_observation_bound_retains_actionable_vulnerability_identity(tmp_path: Path) -> None:
    analysis = _read(
        _database(
            tmp_path,
            findings=True,
            semgrep_count=CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT - 10,
        ),
        1,
    )

    vulnerability_evidence = tuple(
        item
        for item in analysis.observations
        if item.provider_id == "pip-audit-known-vulnerabilities"
        and item.code in {"known_vulnerability:PYSEC-FIXTURE", "package_has_known_vulnerability"}
    )
    assert {item.evidence_kind for item in vulnerability_evidence} == {"metric", "relation"}
    assert {
        item.target_key for item in vulnerability_evidence if item.evidence_kind == "relation"
    } == {"advisory:PYSEC-FIXTURE"}


def test_latest_state_reader_is_read_only_and_missing_state_is_not_initialized(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    absent = analyze_code_supply_chain(missing)

    assert absent.status == "abstained"
    assert absent.reason == "code_state_missing"
    assert not missing.exists()

    state = tmp_path / "state"
    state.mkdir()
    database = _database(state, findings=False)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    current = analyze_code_supply_chain(state, now_utc=_NOW_UTC)

    assert current.status == "ready"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
