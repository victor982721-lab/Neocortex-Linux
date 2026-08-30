"""Deterministic work packages over bounded published Code review evidence."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import sqlite3
from dataclasses import dataclass, replace
from typing import Literal, cast

from .code_architecture_analysis import (
    CodeArchitectureAnalysis,
    bounded_import_chains,
)
from .code_coverage_analysis import (
    CodeCoverageAnalysis,
    project_work_package_coverage,
    project_work_package_coverage_scope,
)
from .code_engineering_analytics import (
    CodeEngineeringAnalytics,
    engineering_profile_for_module,
)
from .code_unused_analysis import CodeUnusedAnalysis, UnusedConsensusCandidate
from .code_supply_chain_analysis import CodeSupplyChainAnalysis, SupplyChainObservation
from .code_review_actionability import classify_source_role
from .code_review_models import (
    CodeReviewFinding,
    CodeReviewRecommendation,
    CodeReviewWorkPackage,
    CodeReviewWorkPackageStep,
    RecommendationStatus,
)
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

CODE_REVIEW_PLANNING = "python-maintenance-work-packages-v5"
CODE_REVIEW_UNUSED_WORK_PACKAGE_LIMIT = 3
CODE_REVIEW_SUPPLY_CHAIN_EVIDENCE_LIMIT = 20

_UNUSED_REQUIRED_PRECISION_GATES = frozenset(
    {
        "calibration_probable_unused_precision",
        "holdout_probable_unused_precision",
    }
)
_UNUSED_ACCEPTANCE_GATES = (
    "unused_analysis_comparable",
    "candidate_remains_probable_unused_high_consensus",
    "dynamic_usage_ruled_out_by_human_review",
    "human_confirmation_recorded",
    "tests_passed",
    "public_import_surface_preserved",
    "architecture_contracts_not_degraded",
    "no_new_import_cycles",
    "unused_coverage_status_honest",
)
_SUPPLY_CHAIN_ACCEPTANCE_GATES = (
    "semgrep_invariants",
    "dependency_declaration_integrity",
    "vulnerability_snapshot_current",
    "no_known_vulnerabilities",
    "installed_package_integrity",
    "license_inventory_available",
)


@dataclass(frozen=True, slots=True)
class CodeReviewPlanningLink:
    """One confirmed direct or two-hop call path between review findings."""

    source_finding_id: str
    target_finding_id: str
    depth: Literal[1, 2]
    via_symbol: str | None
    confidence: float
    provenance: tuple[str, ...]


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _planning_link_query(symbol_count: int) -> str:
    placeholders = _placeholders(symbol_count)
    pair_limit = symbol_count * max(symbol_count - 1, 0)
    return f"""
    WITH current_python_symbols AS (
        SELECT s.symbol_id,s.qualified_name,f.current_path,
               (SELECT p.probable_root FROM project_memberships pm
                JOIN projects p ON p.project_id=pm.project_id
                WHERE pm.version_id=v.version_id AND pm.selected=1
                  AND p.status='current' AND p.probable_root IS NOT NULL
                ORDER BY pm.confidence DESC,LENGTH(p.probable_root) DESC,
                         p.project_id
                LIMIT 1) AS project_root
        FROM symbols s
        JOIN file_versions v ON v.version_id=s.version_id
        JOIN files f ON f.current_version_id=v.version_id AND f.status='current'
        WHERE v.invalidated_ns IS NULL AND v.analysis_status='complete'
          AND v.language='python' AND v.generated=0 AND v.vendored=0
          AND s.confirmed=1
    ), confirmed_calls AS (
        SELECT r.source_symbol_id,r.target_symbol_id,
               MAX(r.confidence) AS confidence,
               MIN(r.evidence) AS evidence
        FROM code_references r
        JOIN current_python_symbols source
          ON source.symbol_id=r.source_symbol_id
        JOIN current_python_symbols target
          ON target.symbol_id=r.target_symbol_id
        WHERE r.kind='call' AND r.confirmed=1
        GROUP BY r.source_symbol_id,r.target_symbol_id
    ), paths AS (
        SELECT r1.source_symbol_id AS source_id,
               r1.target_symbol_id AS target_id,
               1 AS depth,NULL AS via_symbol,NULL AS via_path,
               NULL AS via_project_root,
               r1.confidence AS confidence,
               r1.evidence AS first_evidence,NULL AS second_evidence
        FROM confirmed_calls r1
        WHERE r1.source_symbol_id IN ({placeholders})
          AND r1.target_symbol_id IN ({placeholders})
        UNION ALL
        SELECT r1.source_symbol_id AS source_id,
               r2.target_symbol_id AS target_id,
               2 AS depth,bridge.qualified_name AS via_symbol,
               bridge.current_path AS via_path,
               bridge.project_root AS via_project_root,
               MIN(r1.confidence,r2.confidence) AS confidence,
               r1.evidence AS first_evidence,
               r2.evidence AS second_evidence
        FROM confirmed_calls r1
        JOIN confirmed_calls r2
          ON r2.source_symbol_id=r1.target_symbol_id
        JOIN current_python_symbols bridge
          ON bridge.symbol_id=r1.target_symbol_id
        WHERE r1.source_symbol_id IN ({placeholders})
          AND r2.target_symbol_id IN ({placeholders})
    ), ranked_paths AS (
        SELECT source_id,target_id,depth,via_symbol,via_path,confidence,
               first_evidence,second_evidence,
               ROW_NUMBER() OVER(
                   PARTITION BY source_id,target_id
                   ORDER BY depth,COALESCE(via_symbol,''),first_evidence,
                            COALESCE(second_evidence,'')
               ) AS route_rank
        FROM paths
        WHERE source_id<>target_id
          AND (via_path IS NULL OR
               neocortex_source_role(via_path,via_project_root)='production')
    )
    SELECT source_id,target_id,depth,via_symbol,via_path,confidence,
           first_evidence,second_evidence
    FROM ranked_paths
    WHERE route_rank=1
    ORDER BY source_id,target_id,depth,COALESCE(via_symbol,''),
             first_evidence,COALESCE(second_evidence,'')
    LIMIT {pair_limit}
    """


def read_code_review_planning_links(
    connection: sqlite3.Connection,
    finding_ids_by_symbol_id: dict[int, str],
) -> tuple[CodeReviewPlanningLink, ...]:
    """Read bounded confirmed call paths without mutating the Code database."""

    symbol_ids = tuple(sorted(finding_ids_by_symbol_id))
    if not symbol_ids:
        return ()
    connection.create_function(
        "neocortex_source_role",
        2,
        lambda path, root: classify_source_role(
            str(path),
            None if root is None else str(root),
        ),
        deterministic=True,
    )
    parameters = (*symbol_ids, *symbol_ids, *symbol_ids, *symbol_ids)
    rows = connection.execute(
        _planning_link_query(len(symbol_ids)),
        parameters,
    ).fetchall()
    links: dict[tuple[str, str], CodeReviewPlanningLink] = {}
    for row in rows:
        source = finding_ids_by_symbol_id[int(row["source_id"])]
        target = finding_ids_by_symbol_id[int(row["target_id"])]
        key = (source, target)
        if key in links:
            continue
        depth = cast(Literal[1, 2], int(row["depth"]))
        evidence = [str(row["first_evidence"])]
        if row["second_evidence"] is not None:
            evidence.append(str(row["second_evidence"]))
        links[key] = CodeReviewPlanningLink(
            source_finding_id=source,
            target_finding_id=target,
            depth=depth,
            via_symbol=None if row["via_symbol"] is None else str(row["via_symbol"]),
            confidence=float(row["confidence"]),
            provenance=tuple(evidence),
        )
    return tuple(links[key] for key in sorted(links))


def _annotate_engineering(
    package: CodeReviewWorkPackage,
    analysis: CodeEngineeringAnalytics | None,
) -> CodeReviewWorkPackage:
    profile = (
        None
        if analysis is None or package.primary_module is None
        else engineering_profile_for_module(analysis, package.primary_module)
    )
    gates = () if analysis is None else analysis.gates
    evidence = (
        ("engineering:not_evaluated",)
        if analysis is None
        else (
            f"engineering:{analysis.status}",
            *(
                ("engineering_profile:not_recorded",)
                if profile is None
                else (
                    f"engineering_profile:{profile.module_id}",
                    *(
                        f"engineering_dimension:{dimension.dimension}:{dimension.status}"
                        for dimension in (
                            profile.complexity,
                            profile.coverage,
                            profile.mutation,
                            profile.history,
                            profile.graph,
                        )
                    ),
                )
            ),
            *(f"engineering_gate:{gate.gate}:{gate.status}" for gate in gates),
        )
    )
    limitations: tuple[str, ...] = ()
    if analysis is None:
        limitations = ("engineering_analytics_not_evaluated",)
    else:
        limitations = (
            *analysis.limitations,
            *(
                ()
                if analysis.status == "ready"
                else ("engineering_analytics_not_ready:" + (analysis.reason or analysis.status),)
            ),
            *(
                ()
                if profile is not None
                else ("engineering_profile_not_recorded_for_primary_module",)
            ),
        )
    return replace(
        package,
        engineering_profile=profile,
        engineering_gates=gates,
        evidence=tuple(dict.fromkeys((*package.evidence, *evidence))),
        limitations=tuple(dict.fromkeys((*package.limitations, *limitations))),
    )


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").strip("/").casefold()


def _path_matches_package(path: str, package: CodeReviewWorkPackage) -> bool:
    candidate_path = _normalized_path(path)
    package_paths = {
        _normalized_path(candidate.relative_path) for candidate in package.unused_candidates
    }
    return any(
        item == candidate_path
        or item.endswith("/" + candidate_path)
        or candidate_path.endswith("/" + item)
        for item in package_paths
    )


def _key_matches_package(value: str | None, package: CodeReviewWorkPackage) -> bool:
    if not value:
        return False
    normalized = value.replace("\\", "/").casefold()
    terms = {
        package.primary_symbol.casefold(),
        package.primary_symbol.rsplit(".", 1)[-1].casefold(),
        *(() if package.primary_module is None else (package.primary_module.casefold(),)),
    }
    return any(term and (normalized == term or term in normalized) for term in terms)


def _supply_chain_observation_relevant(
    observation: SupplyChainObservation,
    package: CodeReviewWorkPackage,
) -> bool:
    return (
        (observation.path is not None and _path_matches_package(observation.path, package))
        or _key_matches_package(observation.subject_key, package)
        or _key_matches_package(observation.target_key, package)
        or observation.subject_kind == "project"
        or observation.target_kind == "project"
        or observation.gate_authority != "advisory"
    )


def _annotate_supply_chain(
    package: CodeReviewWorkPackage,
    analysis: CodeSupplyChainAnalysis | None,
) -> CodeReviewWorkPackage:
    gates = () if analysis is None else analysis.gates
    observations = (
        ()
        if analysis is None
        else tuple(
            item
            for item in analysis.observations
            if _supply_chain_observation_relevant(item, package)
        )[:CODE_REVIEW_SUPPLY_CHAIN_EVIDENCE_LIMIT]
    )
    relations = (
        ()
        if analysis is None
        else tuple(
            item
            for item in analysis.observations
            if item.evidence_kind == "relation"
            and (
                item.subject_kind in {"project", "module", "package", "dependency"}
                or item.target_kind in {"project", "module", "package", "dependency"}
            )
        )[:CODE_REVIEW_SUPPLY_CHAIN_EVIDENCE_LIMIT]
    )
    status = "not_evaluated" if analysis is None else analysis.status
    reason = "supply_chain_result_missing" if analysis is None else analysis.reason
    limitations = [
        *package.limitations,
        "supply_chain_evidence_is_advisory_and_has_zero_mutation_authority",
    ]
    if status != "ready":
        limitations.append("supply_chain_gates_require_ready_evidence:" + (reason or status))
    return replace(
        package,
        supply_chain_observations=observations,
        supply_chain_relations=relations,
        supply_chain_gates=gates,
        acceptance_gates=tuple(
            dict.fromkeys((*package.acceptance_gates, *_SUPPLY_CHAIN_ACCEPTANCE_GATES))
        ),
        evidence=(
            *package.evidence,
            f"supply_chain:{status}",
            f"supply_chain_observations:{len(observations)}",
            f"supply_chain_relations:{len(relations)}",
        ),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _unused_package_id(candidate: UnusedConsensusCandidate) -> str:
    payload = canonical_json(
        {
            "planning": "unused-characterization-work-packages-v1",
            "candidate_id": candidate.candidate_id,
        }
    )
    return "code-unused-work-package-v1:xxh3_128:" + fingerprint_text(payload).xxh3_128


def _unused_package_steps(
    candidate: UnusedConsensusCandidate,
) -> tuple[CodeReviewWorkPackageStep, ...]:
    target = candidate.symbol or candidate.name
    return (
        CodeReviewWorkPackageStep(
            1,
            "characterize",
            target,
            "verify_import_reexport_callback_registry_protocol_and_entry_point_usage",
        ),
        CodeReviewWorkPackageStep(
            2,
            "characterize",
            target,
            "run_targeted_tests_and_public_import_smoke_without_mutating_code",
        ),
        CodeReviewWorkPackageStep(
            3,
            "characterize",
            target,
            "record_explicit_human_confirmation_or_reclassify_with_new_evidence",
        ),
        CodeReviewWorkPackageStep(
            4,
            "characterize",
            target,
            "require_comparable_unused_analysis_replay_before_any_separate_change",
        ),
    )


def _unused_characterization_packages(
    analysis: CodeUnusedAnalysis | None,
    *,
    architecture: CodeArchitectureAnalysis | None,
    test_coverage: CodeCoverageAnalysis | None,
    engineering_analytics: CodeEngineeringAnalytics | None,
    excluded_candidate_ids: frozenset[str],
    supply_chain: CodeSupplyChainAnalysis | None = None,
) -> tuple[CodeReviewWorkPackage, ...]:
    precision_gates: dict[str, str] = {}
    if isinstance(analysis, CodeUnusedAnalysis):
        precision_gates = {
            gate.gate: gate.status
            for gate in analysis.gates
            if gate.gate in _UNUSED_REQUIRED_PRECISION_GATES
        }
    if (
        not isinstance(analysis, CodeUnusedAnalysis)
        or analysis.status != "ready"
        or analysis.authority != "advisory"
        or analysis.mutation_authority
        or any(precision_gates.get(gate) != "passed" for gate in _UNUSED_REQUIRED_PRECISION_GATES)
    ):
        return ()
    selected = tuple(
        candidate
        for candidate in analysis.candidates
        if candidate.state == "probable_unused_high_consensus"
        and candidate.candidate_id not in excluded_candidate_ids
    )[:CODE_REVIEW_UNUSED_WORK_PACKAGE_LIMIT]
    packages: list[CodeReviewWorkPackage] = []
    for candidate in selected:
        target = candidate.symbol or candidate.name
        module_id = candidate.module_id
        import_chains = (
            ()
            if architecture is None or module_id is None
            else bounded_import_chains(architecture, module_id)
        )
        affected_contracts = (
            ()
            if architecture is None or module_id is None
            else tuple(
                sorted(
                    contract.contract_id
                    for contract in architecture.contracts
                    if module_id in contract.importer_modules
                    or module_id in contract.imported_modules
                )
            )
        )
        coverage_projection = (
            None if test_coverage is None else project_work_package_coverage(test_coverage, target)
        )
        coverage_scope = (
            None
            if test_coverage is None
            else project_work_package_coverage_scope(test_coverage, target)
        )
        packages.append(
            _annotate_supply_chain(
                _annotate_engineering(
                    CodeReviewWorkPackage(
                        package_rank=0,
                        package_id=_unused_package_id(candidate),
                        title=f"{target} unused-code characterization",
                        objective="characterize_high_consensus_unused_candidate_without_mutation",
                        primary_finding_id=candidate.candidate_id,
                        primary_hotspot_id=candidate.candidate_id,
                        primary_symbol=target,
                        primary_module=module_id,
                        change_risk="unknown",
                        members=(),
                        members_truncated=False,
                        consumer_module_examples=(),
                        import_chains=import_chains,
                        affected_architecture_contracts=affected_contracts,
                        test_coverage=coverage_projection,
                        test_coverage_scope=coverage_scope,
                        contracts_to_preserve=(
                            "public_import_and_reexport_surface",
                            "callbacks_registries_protocols_and_entry_points",
                            "runtime_and_test_fixture_behavior",
                        ),
                        steps=_unused_package_steps(candidate),
                        recommended_validation=(
                            "inspect_import_reexport_and___all___usage",
                            "inspect_callbacks_registries_protocols_and_entry_points",
                            "run_targeted_tests_and_public_import_smoke",
                            "record_human_confirmation_before_any_separate_change",
                        ),
                        acceptance_gates=_UNUSED_ACCEPTANCE_GATES,
                        evidence=(
                            f"unused_candidate:{candidate.candidate_id}:{candidate.state}",
                            *(f"provider:{item}" for item in candidate.provider_ids),
                            *(f"reason:{item}" for item in candidate.reasons),
                            f"calibration_signature:{analysis.calibration_signature}",
                            f"coverage_status:{analysis.coverage_status}",
                            "architecture:"
                            + (
                                architecture.status if architecture is not None else "not_evaluated"
                            ),
                        ),
                        limitations=(
                            "characterization_package_is_advice_not_change_authorization",
                            "candidate_requires_explicit_human_confirmation",
                            "dynamic_usage_may_remain_unobserved",
                            "coverage_can_explain_usage_but_never_strengthens_missing_evidence",
                            "package_has_zero_delete_or_mutation_authority",
                            *(
                                ()
                                if architecture is not None and architecture.status == "ready"
                                else ("architecture_gates_require_comparable_ready_evidence",)
                            ),
                        ),
                        confidence="unused_high_consensus_advisory",
                        package_kind="unused_characterization",
                        unused_candidates=(candidate,),
                        requires_human_confirmation=True,
                        mutation_authority=False,
                    ),
                    engineering_analytics,
                ),
                supply_chain,
            )
        )
    return tuple(packages)


def build_code_review_work_packages(
    findings: tuple[CodeReviewFinding, ...],
    recommendations: tuple[CodeReviewRecommendation, ...],
    links: tuple[CodeReviewPlanningLink, ...],
    *,
    architecture: CodeArchitectureAnalysis | None = None,
    architecture_root: str | None = None,
    test_coverage: CodeCoverageAnalysis | None = None,
    engineering_analytics: CodeEngineeringAnalytics | None = None,
    unused_analysis: CodeUnusedAnalysis | None = None,
    supply_chain: CodeSupplyChainAnalysis | None = None,
) -> tuple[CodeReviewWorkPackage, ...]:
    """Refuse hotspot change packages until decision evidence is resolvable.

    The v11 structural detector has observation authority only.  Unused-code
    characterization packages are built separately by
    :func:`plan_code_review_work_packages` and remain non-mutating.
    """

    del (
        findings,
        recommendations,
        links,
        architecture,
        architecture_root,
        test_coverage,
        engineering_analytics,
        unused_analysis,
        supply_chain,
    )
    return ()


def plan_code_review_work_packages(
    findings: tuple[CodeReviewFinding, ...],
    recommendations: tuple[CodeReviewRecommendation, ...],
    links: tuple[CodeReviewPlanningLink, ...],
    *,
    architecture: CodeArchitectureAnalysis | None = None,
    architecture_root: str | None = None,
    test_coverage: CodeCoverageAnalysis | None = None,
    engineering_analytics: CodeEngineeringAnalytics | None = None,
    unused_analysis: CodeUnusedAnalysis | None = None,
    supply_chain: CodeSupplyChainAnalysis | None = None,
) -> tuple[
    tuple[CodeReviewWorkPackage, ...],
    RecommendationStatus,
    str | None,
]:
    """Return packages and their explicit ready or abstained envelope state."""

    packages = build_code_review_work_packages(
        findings,
        recommendations,
        links,
        architecture=architecture,
        architecture_root=architecture_root,
        test_coverage=test_coverage,
        engineering_analytics=engineering_analytics,
        unused_analysis=unused_analysis,
        supply_chain=supply_chain,
    )
    annotated_candidate_ids = frozenset(
        candidate.candidate_id for package in packages for candidate in package.unused_candidates
    )
    unused_packages = _unused_characterization_packages(
        unused_analysis,
        architecture=architecture,
        test_coverage=test_coverage,
        engineering_analytics=engineering_analytics,
        excluded_candidate_ids=annotated_candidate_ids,
        supply_chain=supply_chain,
    )
    packages = tuple(
        replace(package, package_rank=rank)
        for rank, package in enumerate((*packages, *unused_packages), start=1)
    )
    if packages:
        return packages, "ready", None
    return (
        (),
        "abstained",
        "no_evidence_ready_change_or_calibrated_characterization_candidate",
    )


__all__ = [
    "CODE_REVIEW_PLANNING",
    "CODE_REVIEW_UNUSED_WORK_PACKAGE_LIMIT",
    "CodeReviewPlanningLink",
    "build_code_review_work_packages",
    "plan_code_review_work_packages",
    "read_code_review_planning_links",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_review_work_packages")
