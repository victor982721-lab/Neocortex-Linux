"""Public status, work-package and diff projections for Hito 5 evidence."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from neocortex.api.cli.cli_code import _read_code_status_snapshot
from neocortex.code.code_publication_diff import _supply_chain_delta
from neocortex.code.code_review_models import build_code_review_recommendations
from neocortex.code.code_review_work_packages import (
    plan_code_review_work_packages,
)
from tests.test_code_review_work_packages import (
    _planning_findings,
    _unused_analysis,
    _unused_candidate,
)
from tests.test_code_supply_chain_analysis import _database, _read


def test_code_status_projects_bounded_supply_chain_with_six_gates(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, findings=False)

    snapshot = _read_code_status_snapshot(database)
    supply_chain = snapshot.supply_chain

    assert supply_chain["status"] == "ready"
    assert supply_chain["authority"] == "advisory"
    assert supply_chain["mutation_authority"] is False
    assert len(supply_chain["gates"]) == 6
    assert len(supply_chain["observations"]) <= 200
    providers = {item["provider_id"]: item for item in supply_chain["providers"]}
    assert set(providers) == {
        "semgrep-neocortex-invariants",
        "deptry-project-dependencies",
        "pip-audit-known-vulnerabilities",
        "installed-package-inventory",
    }
    assert providers["pip-audit-known-vulnerabilities"]["freshness"] == "stale"


def test_work_package_attaches_advisory_supply_chain_context(tmp_path: Path) -> None:
    database = _database(tmp_path, findings=True)
    supply_chain = _read(database, 2)
    findings = _planning_findings()
    recommendations = build_code_review_recommendations(findings, limit=3)

    packages = plan_code_review_work_packages(
        findings,
        recommendations,
        (),
        unused_analysis=_unused_analysis(_unused_candidate()),  # type: ignore[arg-type]
        supply_chain=supply_chain,
    )[0]

    assert len(packages) == 1
    package = packages[0]
    assert len(package.supply_chain_gates) == 6
    assert package.supply_chain_observations
    assert package.supply_chain_relations
    assert {item.gate for item in package.supply_chain_gates}.issubset(
        set(package.acceptance_gates)
    )
    assert package.mutation_authority is False
    assert "supply_chain_evidence_is_advisory_and_has_zero_mutation_authority" in (
        package.limitations
    )


def test_supply_chain_diff_explains_categories_providers_gates_and_observations(
    tmp_path: Path,
) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    baseline_root.mkdir()
    current_root.mkdir()
    baseline = _read(_database(baseline_root, findings=False), 2)
    current = _read(_database(current_root, findings=True), 2)

    delta = _supply_chain_delta(baseline, current)

    assert delta.status == "ready"
    assert len(delta.categories) == 5
    assert len(delta.providers) == 4
    assert len(delta.gates) == 6
    assert delta.added_visible or delta.changed_visible
    assert any(item.change in {"added", "changed"} for item in delta.examples)
    gate_deltas = {item.gate: item for item in delta.gates}
    assert gate_deltas["semgrep_invariants"].current_status == "failed"
    assert gate_deltas["installed_package_integrity"].current_status == "failed"
    assert "score" not in asdict(delta)
    assert delta.authority == "advisory"
    assert delta.mutation_authority is False
