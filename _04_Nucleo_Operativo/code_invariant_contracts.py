"""Versioned invariant and runtime-scenario declarations for Code assurance.

The registry links exact pytest nodeids to deliberately named invariants.  A
passing nodeid is evidence that one declared scenario executed successfully;
it is never promoted to a proof that the invariant holds for all executions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .code_analysis_epistemics import analysis_identity

CODE_INVARIANT_REGISTRY_SCHEMA = "neocortex.code-invariant-registry/v1"
CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA = "neocortex.code-runtime-scenario-registry/v1"


def _required(label: str, value: object, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeScenarioSpec:
    scenario_id: str
    version: str
    test_nodeid: str
    scenario_kind: Literal["state_fixture", "process_death", "metamorphic"]
    isolation: Literal["pytest_tmp_path", "spawned_process_and_tmp_path"]
    limitation: str

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("scenario id", self.scenario_id, 256),
            ("scenario version", self.version, 64),
            ("scenario test nodeid", self.test_nodeid, 16_384),
            ("scenario limitation", self.limitation, 512),
        ):
            _required(label, value, maximum)
        if "::test_" not in self.test_nodeid:
            raise ValueError("runtime scenario must bind one exact pytest test")
        if self.scenario_kind not in {"state_fixture", "process_death", "metamorphic"}:
            raise ValueError("runtime scenario kind is invalid")
        if self.isolation not in {"pytest_tmp_path", "spawned_process_and_tmp_path"}:
            raise ValueError("runtime scenario isolation is invalid")


@dataclass(frozen=True, slots=True)
class InvariantSpec:
    invariant_id: str
    version: str
    statement: str
    scope: Literal["state", "publication", "analyzer"]
    scenario_ids: tuple[str, ...]
    failure_impact: Literal["consistency", "publication", "analyzer_integrity"]

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("invariant id", self.invariant_id, 256),
            ("invariant version", self.version, 64),
            ("invariant statement", self.statement, 2_048),
        ):
            _required(label, value, maximum)
        if self.scope not in {"state", "publication", "analyzer"}:
            raise ValueError("invariant scope is invalid")
        if not self.scenario_ids or len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError("invariant scenarios must be non-empty and unique")
        if self.failure_impact not in {"consistency", "publication", "analyzer_integrity"}:
            raise ValueError("invariant failure impact is invalid")


RUNTIME_SCENARIOS = (
    RuntimeScenarioSpec(
        scenario_id="analyzer.hotspot_name_path_call_invariance",
        version="v1",
        test_nodeid=(
            "tests/test_code_review_epistemics.py::"
            "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
        ),
        scenario_kind="metamorphic",
        isolation="pytest_tmp_path",
        limitation="declared_transformations_only_not_general_detector_invariance",
    ),
    RuntimeScenarioSpec(
        scenario_id="semantic.staging_process_death_resume",
        version="v1",
        test_nodeid=(
            "tests/test_semantic_text_staging_session.py::"
            "test_process_death_preserves_committed_prefix_and_resume_publishes_atomically"
        ),
        scenario_kind="process_death",
        isolation="spawned_process_and_tmp_path",
        limitation="process_exit_is_observed_but_power_loss_and_filesystem_failure_are_not",
    ),
    RuntimeScenarioSpec(
        scenario_id="state.text_semantic_exact_projection",
        version="v1",
        test_nodeid=(
            "tests/test_code_state_projection_analysis.py::"
            "test_projection_excludes_empty_text_and_observes_exact_alignment"
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation="fixture_alignment_is_not_live_cross_owner_recovery_evidence",
    ),
    RuntimeScenarioSpec(
        scenario_id="state.text_terminal_relational_closure",
        version="v1",
        test_nodeid=(
            "tests/test_code_state_topology_analysis.py::"
            "test_exact_text_closure_preserves_running_and_legacy_negative_controls"
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation="final_relational_closure_does_not_prove_historical_atomicity",
    ),
)

INVARIANT_SPECS = (
    InvariantSpec(
        invariant_id="analyzer.metrics_do_not_become_semantic_change_authority",
        version="v1",
        statement=(
            "Renames, path conventions, wrappers, and transaction-like call spellings do not "
            "create a semantic change decision."
        ),
        scope="analyzer",
        scenario_ids=("analyzer.hotspot_name_path_call_invariance",),
        failure_impact="analyzer_integrity",
    ),
    InvariantSpec(
        invariant_id="semantic.building_generation_is_not_published_after_process_death",
        version="v1",
        statement=(
            "A process death during Semantic staging leaves the prior head visible and a resume "
            "publishes the completed generation atomically."
        ),
        scope="publication",
        scenario_ids=("semantic.staging_process_death_resume",),
        failure_impact="publication",
    ),
    InvariantSpec(
        invariant_id="state.published_semantic_text_matches_eligible_text_revisions",
        version="v1",
        statement=(
            "A published Semantic text head represents exactly eligible non-empty Text revisions "
            "under the declared source-owner contract."
        ),
        scope="state",
        scenario_ids=("state.text_semantic_exact_projection",),
        failure_impact="consistency",
    ),
    InvariantSpec(
        invariant_id="state.text_terminal_publications_are_relationally_closed",
        version="v1",
        statement=(
            "Terminal Text attempts have their required receipt and outbox relations while "
            "running and legacy records remain separately classified."
        ),
        scope="state",
        scenario_ids=("state.text_terminal_relational_closure",),
        failure_impact="consistency",
    ),
)


def _validate_registry() -> None:
    scenario_ids = tuple(item.scenario_id for item in RUNTIME_SCENARIOS)
    invariant_ids = tuple(item.invariant_id for item in INVARIANT_SPECS)
    if scenario_ids != tuple(sorted(scenario_ids)) or len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("runtime-scenario registry must be unique and canonically ordered")
    if invariant_ids != tuple(sorted(invariant_ids)) or len(set(invariant_ids)) != len(
        invariant_ids
    ):
        raise ValueError("invariant registry must be unique and canonically ordered")
    declared = set(scenario_ids)
    referenced = {scenario for item in INVARIANT_SPECS for scenario in item.scenario_ids}
    if referenced != declared:
        raise ValueError("every runtime scenario must be linked by exactly the registry surface")


_validate_registry()


def invariant_registry_payload() -> dict[str, object]:
    return {
        "schema": CODE_INVARIANT_REGISTRY_SCHEMA,
        "scenario_schema": CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA,
        "invariants": tuple(asdict(item) for item in INVARIANT_SPECS),
        "scenarios": tuple(asdict(item) for item in RUNTIME_SCENARIOS),
        "claim_scope": "declared_scenario_execution_not_formal_proof",
    }


def invariant_registry_fingerprint() -> str:
    return analysis_identity("code-invariant-registry-v1", invariant_registry_payload())


def runtime_scenario(scenario_id: str) -> RuntimeScenarioSpec:
    selected = _required("scenario id", scenario_id, 256)
    match = next((item for item in RUNTIME_SCENARIOS if item.scenario_id == selected), None)
    if match is None:
        raise ValueError(f"unknown runtime scenario: {selected}")
    return match


__all__ = [
    "CODE_INVARIANT_REGISTRY_SCHEMA",
    "CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA",
    "INVARIANT_SPECS",
    "RUNTIME_SCENARIOS",
    "InvariantSpec",
    "RuntimeScenarioSpec",
    "invariant_registry_fingerprint",
    "invariant_registry_payload",
    "runtime_scenario",
]
