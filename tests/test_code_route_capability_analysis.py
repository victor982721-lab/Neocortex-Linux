from __future__ import annotations

from dataclasses import replace

import pytest

from neocortex.capabilities import (
    CAPABILITY_SPECS,
    ROUTE_CAPABILITY_NAMES,
    RuntimeCapabilityStatus,
    inspect_runtime_capabilities,
)

from neocortex.code.code_capability_reachability_analysis import (
    abstained_capability_reachability,
)
from neocortex.code.code_experiment_planner import plan_code_experiments
from neocortex.code.code_route_capability_analysis import (
    parse_code_route_capability_payload,
    resolve_route_capability_analysis,
    route_capability_questions,
)
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    SnapshotConsistency,
)
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


def _snapshot(
    *,
    changed: bool = False,
    code_watermark: str = "1",
    state_by_owner: dict[str, OwnerAvailability] | None = None,
) -> KnowledgeSnapshot:
    selected = state_by_owner or {}
    owners = []
    for store in STATE_STORE_REGISTRY.stores:
        state = selected.get(store.state_owner_id, OwnerAvailability.AVAILABLE)
        is_changed = changed and store.state_owner_id == "text"
        owners.append(
            OwnerSnapshot(
                owner=store.state_owner_id,
                state=state,
                expected_schema_version=store.expected_schema_version,
                observed_schema_version=(
                    store.expected_schema_version if state is OwnerAvailability.AVAILABLE else None
                ),
                watermarks=(
                    LogicalWatermark(
                        "fixture",
                        code_watermark if store.state_owner_id == "code" else "1",
                    ),
                )
                if state is OwnerAvailability.AVAILABLE
                else (),
                data_version_before=1 if state is OwnerAvailability.AVAILABLE else None,
                data_version_after=(2 if is_changed else 1)
                if state is OwnerAvailability.AVAILABLE
                else None,
            )
        )
    return KnowledgeSnapshot.create(
        source_version="source-fixture",
        captured_at_utc="2026-08-12T00:00:00Z",
        captured_monotonic_ns=1,
        owners=tuple(owners),
        consistency=(
            SnapshotConsistency.SNAPSHOT_CHANGED if changed else SnapshotConsistency.STABLE
        ),
        attempts=2 if changed else 1,
        warnings=("fixture changed",) if changed else (),
    )


def _runtime(*, all_available: bool = True) -> tuple[RuntimeCapabilityStatus, ...]:
    if all_available:
        return inspect_runtime_capabilities(
            ROUTE_CAPABILITY_NAMES,
            module_finder=lambda _name: object(),
            distribution_version=lambda _name: "1.0",
            executable_finder=lambda name: f"/fixture/{name}",
        )
    return inspect_runtime_capabilities(
        ROUTE_CAPABILITY_NAMES,
        module_finder=lambda _name: None,
        distribution_version=lambda _name: (_ for _ in ()).throw(
            __import__("importlib").metadata.PackageNotFoundError
        ),
        executable_finder=lambda _name: None,
    )


def test_all_builtin_routes_have_explicit_bounded_observations() -> None:
    result = resolve_route_capability_analysis(
        _snapshot(),
        _runtime(),
        source_version="source-fixture",
    )

    assert result.status == "ready"
    assert result.route_registry_names == BUILTIN_ROUTE_ORDER
    assert tuple(item.route_name for item in result.observations) == BUILTIN_ROUTE_ORDER
    assert all(item.route_registered for item in result.observations)
    assert all(item.owner_schema_current for item in result.observations)
    assert all(
        item.evidence_level == "owner_state_observed_unattributed" for item in result.observations
    )
    assert all(not item.causal_durable_output_observed for item in result.observations)
    assert parse_code_route_capability_payload(result.as_payload()) == result

    specs, evaluations = route_capability_questions(result, rank_offset=0)
    assert len(specs) == 1
    assert len(evaluations) == len(BUILTIN_ROUTE_ORDER)
    assert tuple(item.rank for item in evaluations) == tuple(range(1, 10))
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None and not item.mutation_authority for item in evaluations)


def test_text_experiment_identity_ignores_unrelated_code_capture_watermark() -> None:
    first = resolve_route_capability_analysis(
        _snapshot(code_watermark="analysis-run:79"),
        _runtime(),
        source_version="source-fixture",
    )
    replay = resolve_route_capability_analysis(
        _snapshot(code_watermark="analysis-run:80"),
        _runtime(),
        source_version="source-fixture",
    )
    first_specs, first_evaluations = route_capability_questions(first, rank_offset=0)
    replay_specs, replay_evaluations = route_capability_questions(replay, rank_offset=0)
    first_plan = plan_code_experiments(first_specs, first_evaluations)
    replay_plan = plan_code_experiments(replay_specs, replay_evaluations)
    first_text = next(
        item for item in first_plan.proposals if item.subject_key == "capability:route:text"
    )
    replay_text = next(
        item for item in replay_plan.proposals if item.subject_key == "capability:route:text"
    )

    assert first.knowledge_snapshot_id != replay.knowledge_snapshot_id
    assert first_text.evaluation_id != replay_text.evaluation_id
    assert first_text.evaluation_binding_fingerprint == replay_text.evaluation_binding_fingerprint
    assert first_text.proposal_id == replay_text.proposal_id


def test_owner_state_and_runtime_availability_never_become_user_value() -> None:
    result = resolve_route_capability_analysis(
        _snapshot(),
        _runtime(),
        source_version="source-fixture",
    )
    original = result.observations[0]

    with pytest.raises(ValueError, match="public consumer or acceptance"):
        replace(original, public_read_consumer_observed=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="derived from observations"):
        replace(original, evidence_level="causal_durable_path_observed")


def test_missing_owner_is_observed_without_inventing_unreachability() -> None:
    snapshot = _snapshot(state_by_owner={"pdf": OwnerAvailability.ABSENT})
    result = resolve_route_capability_analysis(
        snapshot,
        _runtime(),
        source_version="source-fixture",
    )

    pdf = result.observations[0]
    assert pdf.route_name == "pdf"
    assert pdf.owner_state == "absent"
    assert pdf.evidence_level == "declared_only"
    assert pdf.runtime_state == "available"
    _, evaluations = route_capability_questions(result, rank_offset=4)
    assert evaluations[0].rank == 5
    assert evaluations[0].observation_status == "confirmed"
    assert evaluations[0].decision_readiness == "experiment_required"


def test_changed_knowledge_snapshot_abstains_without_partial_claims() -> None:
    result = resolve_route_capability_analysis(
        _snapshot(changed=True),
        _runtime(),
        source_version="source-fixture",
    )

    assert result.status == "abstained"
    assert result.observations == ()
    specs, evaluations = route_capability_questions(result, rank_offset=2)
    assert len(specs) == len(evaluations) == 1
    assert evaluations[0].rank == 3
    assert evaluations[0].observation_status == "abstained"
    assert evaluations[0].next_action_ids == ()


def test_text_abstention_is_not_relabelled_as_causal_output() -> None:
    text = abstained_capability_reachability(
        "fixture_unavailable",
        source_version="source-fixture",
    )
    result = resolve_route_capability_analysis(
        _snapshot(),
        _runtime(),
        source_version="source-fixture",
        text_causal=text,
    )

    observation = next(item for item in result.observations if item.route_name == "text")
    assert observation.causal_projection_status == "abstained"
    assert not observation.causal_durable_output_observed
    assert result.text_causal_analysis_id is None


def test_runtime_component_counts_are_exact_and_fail_closed() -> None:
    result = resolve_route_capability_analysis(
        _snapshot(),
        _runtime(),
        source_version="source-fixture",
    )
    pdf = result.observations[0]
    assert pdf.declared_components == len(CAPABILITY_SPECS["pdf"].requirements)
    with pytest.raises(ValueError, match="missing required components"):
        replace(pdf, available_required_components=0)


def test_wire_rejects_semantic_authority_and_identity_forgery() -> None:
    result = resolve_route_capability_analysis(
        _snapshot(),
        _runtime(),
        source_version="source-fixture",
    )
    payload = result.as_payload()
    payload["mutation_authority"] = True
    with pytest.raises(ValueError, match="advisory and non-mutating"):
        parse_code_route_capability_payload(payload)

    payload = result.as_payload()
    payload["schema"] = "neocortex.code-route-capability-analysis/v2"
    with pytest.raises(ValueError, match="schema"):
        parse_code_route_capability_payload(payload)

    payload = result.as_payload()
    payload["observations"][0]["evidence_level"] = "causal_durable_path_observed"  # type: ignore[index]
    with pytest.raises(ValueError, match="derived from observations"):
        parse_code_route_capability_payload(payload)


def test_direct_constructor_rejects_noncanonical_analysis_identity() -> None:
    result = resolve_route_capability_analysis(
        _snapshot(),
        _runtime(),
        source_version="source-fixture",
    )
    with pytest.raises(ValueError, match="identity"):
        replace(result, analysis_id="forged")
