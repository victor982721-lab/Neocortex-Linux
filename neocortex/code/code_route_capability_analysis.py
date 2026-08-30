"""Bounded product-route reachability evidence across every built-in route.

The existing Text capability projection can prove an owner-native durable
receipt/output chain.  Other routes do not yet publish the same causal
contract.  This module therefore joins only facts that can be resolved today:

* the exact built-in route registry;
* the versioned runtime prerequisite declarations and their safe probes;
* the public state-store registry;
* one stable cross-owner Knowledge snapshot; and
* the exact Text durable projection, when it is fenced by equal Knowledge
  snapshots before and after the Text read.

An available owner database, a watermark, or a registered route is not treated
as proof of a causal execution path, a read consumer, or user-visible value.
All conclusions remain advisory and non-mutating.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from neocortex.capabilities import (
    CAPABILITY_MANIFESTS,
    CAPABILITY_SPECS,
    RUNTIME_CAPABILITY_PROBE_POLICY,
    ROUTE_CAPABILITY_NAMES,
    TEXT_EXTRACT_CAPABILITY_ID,
    RuntimeCapabilityStatus,
    inspect_runtime_capabilities,
)

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from .code_capability_reachability_analysis import (
    CodeCapabilityReachabilityAnalysis,
    analyze_capability_reachability,
)
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    SnapshotConsistency,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths, collect_knowledge_snapshot
from neocortex.runtime.orchestration.route_registry import builtin_route_registry
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY, STATE_STORE_REGISTRY_SCHEMA

CODE_ROUTE_CAPABILITY_SCHEMA = "neocortex.code-route-capability-analysis/v1"
CODE_ROUTE_CAPABILITY_POLICY = "declared-route-owner-state-and-causal-receipts-v1"

_LIMITATIONS = (
    "route_registration_is_not_entrypoint_execution",
    "runtime_prerequisite_availability_is_not_capability_execution",
    "owner_publications_and_watermarks_are_not_attributed_to_a_route",
    "only_text_has_an_owner_native_capability_manifest_receipt_output_projection",
    "durable_output_is_not_read_api_or_user_visible_value_evidence",
    "capability_value_and_acceptance_require_an_isolated_public_scenario",
    "knowledge_snapshot_is_a_bounded_cross_owner_observation_not_a_distributed_transaction",
)

ROUTE_CAPABILITY_QUESTION = AnalysisQuestionSpec(
    question_id="capability.route_reaches_user_visible_outcome",
    version="v1",
    subject_kinds=("capability",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "route_runtime_and_state_contract_resolved",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "runtime_prerequisites_observed",
            "question",
            "supporting",
            ("runtime_observation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "state_owner_snapshot_observed",
            "question",
            "supporting",
            ("runtime_observation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "causal_durable_execution_path_observed",
            "decision",
            "supporting",
            ("runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "public_read_consumer_observed",
            "decision",
            "supporting",
            ("runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "public_acceptance_scenario_observed",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "declaration_only_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("runtime_observation",),
        ),
    ),
    hypotheses=(
        "route_is_only_declared",
        "route_has_owner_state_but_no_attributed_causal_path",
        "route_has_a_causal_durable_path",
        "route_reaches_user_visible_value",
    ),
    counterevidence_rules=(
        "a_registered_route_without_a_receipt_chain_is_declaration_only",
        "owner_state_without_route_attribution_does_not_prove_execution",
        "durable_output_without_a_read_consumer_does_not_prove_user_value",
        "an_unavailable_runtime_precondition_refutes_current_executability_not_design_reachability",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "exercise_bounded_route_from_public_entrypoint",
            "experiment",
            "Execute one isolated bounded route scenario and bind its run receipt to output.",
        ),
        AnalysisNextActionSpec(
            "trace_route_output_to_public_read_consumer",
            "characterization",
            "Trace one attributed durable output through its public read API and presentation.",
        ),
        AnalysisNextActionSpec(
            "add_owner_native_capability_receipt_contract",
            "characterization",
            "Define an owner-native manifest, receipt, output, and publication contract where absent.",
        ),
    ),
)

ROUTE_CAPABILITY_AVAILABILITY_QUESTION = AnalysisQuestionSpec(
    question_id="capability.route_portfolio_evidence_is_resolved",
    version="v1",
    subject_kinds=("run",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "stable_knowledge_snapshot_resolved",
            "question",
            "supporting",
            ("runtime_observation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "route_and_runtime_contracts_resolved",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "portfolio_acceptance_scenario_observed",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "snapshot_change_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("runtime_observation",),
        ),
    ),
    hypotheses=(
        "route_portfolio_evidence_is_unavailable_or_changed",
        "route_portfolio_evidence_is_resolved_for_characterization",
    ),
    counterevidence_rules=(
        "a_changed_cross_owner_snapshot_cannot_support_a_portfolio_comparison",
        "an_absent_owner_is_an_observation_not_proof_that_a_route_is_unreachable",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "recapture_stable_route_portfolio_snapshot",
            "experiment",
            "Recapture the bounded cross-owner snapshot and require identical fences.",
        ),
    ),
)


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _texts(label: str, values: object, *, sorted_values: bool = False) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, value) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if sorted_values and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be sorted")
    return result


@dataclass(frozen=True, slots=True)
class RouteCapabilityObservation:
    observation_id: str
    route_name: str
    state_owner_id: str
    state_store_id: str
    state_store_schema: int
    route_registered: bool
    runtime_state: Literal["available", "degraded", "unavailable"]
    declared_components: int
    required_components: int
    available_required_components: int
    missing_required_components: tuple[str, ...]
    missing_optional_components: tuple[str, ...]
    owner_state: Literal["available", "absent", "future", "corrupt", "incompatible"]
    owner_schema_current: bool
    owner_publications: int
    owner_watermarks: int
    capability_manifest_count: int
    causal_projection_status: Literal["resolved", "not_available", "abstained"]
    causal_durable_output_observed: bool
    public_read_consumer_observed: Literal[False]
    public_acceptance_scenario_observed: Literal[False]
    evidence_level: Literal[
        "declared_only",
        "owner_state_observed_unattributed",
        "causal_durable_path_observed",
    ]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value in (
            ("route capability observation id", self.observation_id),
            ("route name", self.route_name),
            ("state owner id", self.state_owner_id),
            ("state store id", self.state_store_id),
        ):
            _required_text(label, value)
        if self.route_name not in BUILTIN_ROUTE_ORDER:
            raise ValueError("route capability observation route is not built in")
        store = STATE_STORE_REGISTRY.by_owner(self.state_owner_id)
        if store.state_store_id != self.state_store_id:
            raise ValueError("route capability state owner/store mapping is inconsistent")
        if self.route_name != self.state_owner_id:
            raise ValueError("route capability v1 requires an explicit same-owner route mapping")
        if self.state_store_schema != store.expected_schema_version:
            raise ValueError("route capability state-store schema is not contractual")
        if not isinstance(self.route_registered, bool) or not self.route_registered:
            raise ValueError("route capability observation requires exact route registration")
        if self.runtime_state not in {"available", "degraded", "unavailable"}:
            raise ValueError("route capability runtime state is invalid")
        for label, count in (
            ("declared components", self.declared_components),
            ("required components", self.required_components),
            ("available required components", self.available_required_components),
            ("owner publications", self.owner_publications),
            ("owner watermarks", self.owner_watermarks),
            ("capability manifest count", self.capability_manifest_count),
        ):
            _nonnegative(label, count)
        if not 0 <= self.available_required_components <= self.required_components:
            raise ValueError("available required component count is invalid")
        if self.required_components > self.declared_components:
            raise ValueError("required component count exceeds declarations")
        _texts("missing required component", self.missing_required_components, sorted_values=True)
        _texts("missing optional component", self.missing_optional_components, sorted_values=True)
        if len(self.missing_required_components) != (
            self.required_components - self.available_required_components
        ):
            raise ValueError("missing required components do not match exact counts")
        expected_runtime = (
            "unavailable"
            if self.missing_required_components
            else "degraded"
            if self.missing_optional_components
            else "available"
        )
        if self.runtime_state != expected_runtime:
            raise ValueError("route runtime state is not derived from component observations")
        if self.owner_state not in {"available", "absent", "future", "corrupt", "incompatible"}:
            raise ValueError("route state-owner observation is invalid")
        if not isinstance(self.owner_schema_current, bool):
            raise ValueError("route owner schema-current flag must be boolean")
        if self.owner_state != "available" and (
            self.owner_schema_current or self.owner_publications or self.owner_watermarks
        ):
            raise ValueError("unavailable route owner cannot expose current durable state")
        if self.owner_state == "available" and not self.owner_schema_current:
            raise ValueError("available route owner must match its contracted schema")
        if self.causal_projection_status not in {"resolved", "not_available", "abstained"}:
            raise ValueError("route causal projection status is invalid")
        if not isinstance(self.causal_durable_output_observed, bool):
            raise ValueError("route causal output flag must be boolean")
        if self.route_name != "text" and self.causal_projection_status != "not_available":
            raise ValueError("only Text has a causal capability projection in v1")
        if self.causal_durable_output_observed and self.causal_projection_status != "resolved":
            raise ValueError("causal output requires a resolved causal projection")
        if self.public_read_consumer_observed or self.public_acceptance_scenario_observed:
            raise ValueError("route capability v1 has no public consumer or acceptance resolver")
        expected_level = (
            "causal_durable_path_observed"
            if self.causal_durable_output_observed
            else "owner_state_observed_unattributed"
            if self.owner_state == "available"
            and (self.owner_publications > 0 or self.owner_watermarks > 0)
            else "declared_only"
        )
        if self.evidence_level != expected_level:
            raise ValueError("route capability evidence level is not derived from observations")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("route capability observation must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "route-capability-observation-v1",
            {key: value for key, value in asdict(self).items() if key != "observation_id"},
        )
        if self.observation_id != expected_id:
            raise ValueError("route capability observation identity is invalid")


@dataclass(frozen=True, slots=True)
class CodeRouteCapabilityAnalysis:
    analysis_id: str
    status: Literal["ready", "abstained"]
    reason: str | None
    policy_id: str
    source_version: str
    knowledge_snapshot_id: str | None
    knowledge_snapshot_consistency: Literal["stable", "snapshot_changed"] | None
    knowledge_capture_attempts: int | None
    route_registry_names: tuple[str, ...]
    runtime_probe_policy: str
    state_store_registry_schema: str
    text_causal_analysis_id: str | None
    observations: tuple[RouteCapabilityObservation, ...]
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("route capability analysis id", self.analysis_id)
        _required_text("route capability policy", self.policy_id)
        _required_text("route capability source version", self.source_version)
        if self.policy_id != CODE_ROUTE_CAPABILITY_POLICY:
            raise ValueError("route capability analysis policy is invalid")
        if self.status not in {"ready", "abstained"}:
            raise ValueError("route capability analysis status is invalid")
        if self.runtime_probe_policy != RUNTIME_CAPABILITY_PROBE_POLICY:
            raise ValueError("route capability runtime probe policy is invalid")
        if self.state_store_registry_schema != STATE_STORE_REGISTRY_SCHEMA:
            raise ValueError("route capability state-store registry schema is invalid")
        if self.limitations != _LIMITATIONS:
            raise ValueError("route capability limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("route capability analysis must remain advisory and non-mutating")
        _texts("route registry name", self.route_registry_names)
        if self.status == "abstained":
            _required_text("route capability abstention reason", self.reason, maximum=256)
            if (
                self.knowledge_snapshot_id is not None
                or self.knowledge_snapshot_consistency is not None
                or self.knowledge_capture_attempts is not None
                or self.route_registry_names
                or self.text_causal_analysis_id is not None
                or self.observations
            ):
                raise ValueError("abstained route capability analysis cannot assert observations")
        else:
            if self.reason is not None:
                raise ValueError("ready route capability analysis cannot carry a reason")
            _required_text("Knowledge snapshot id", self.knowledge_snapshot_id)
            if self.knowledge_snapshot_consistency != "stable":
                raise ValueError("ready route capability analysis requires a stable snapshot")
            if self.knowledge_capture_attempts is None:
                raise ValueError("ready route capability analysis requires capture attempts")
            _nonnegative("Knowledge capture attempts", self.knowledge_capture_attempts)
            if self.knowledge_capture_attempts < 1:
                raise ValueError("Knowledge capture attempts must be positive")
            if self.route_registry_names != BUILTIN_ROUTE_ORDER:
                raise ValueError("route capability registry must cover the exact built-in order")
            if tuple(item.route_name for item in self.observations) != BUILTIN_ROUTE_ORDER:
                raise ValueError("route capability observations must cover every built-in route")
            text_observation = next(item for item in self.observations if item.route_name == "text")
            if (self.text_causal_analysis_id is not None) != (
                text_observation.causal_projection_status == "resolved"
            ):
                raise ValueError("Text causal analysis identity and observation disagree")
        expected_id = analysis_identity(
            "code-route-capability-analysis-v1",
            {key: value for key, value in asdict(self).items() if key != "analysis_id"},
        )
        if self.analysis_id != expected_id:
            raise ValueError("route capability analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_ROUTE_CAPABILITY_SCHEMA, **asdict(self)}


def _analysis(values: dict[str, object]) -> CodeRouteCapabilityAnalysis:
    identity_values = dict(values)
    raw = identity_values.get("observations")
    if isinstance(raw, tuple):
        identity_values["observations"] = tuple(
            asdict(item) if isinstance(item, RouteCapabilityObservation) else item for item in raw
        )
    return CodeRouteCapabilityAnalysis(
        analysis_id=analysis_identity("code-route-capability-analysis-v1", identity_values),
        **values,  # type: ignore[arg-type]
    )


def abstained_route_capability_analysis(
    reason: str,
    *,
    source_version: str,
) -> CodeRouteCapabilityAnalysis:
    return _analysis(
        {
            "status": "abstained",
            "reason": _required_text("route capability abstention reason", reason, maximum=256),
            "policy_id": CODE_ROUTE_CAPABILITY_POLICY,
            "source_version": _required_text("route capability source version", source_version),
            "knowledge_snapshot_id": None,
            "knowledge_snapshot_consistency": None,
            "knowledge_capture_attempts": None,
            "route_registry_names": (),
            "runtime_probe_policy": RUNTIME_CAPABILITY_PROBE_POLICY,
            "state_store_registry_schema": STATE_STORE_REGISTRY_SCHEMA,
            "text_causal_analysis_id": None,
            "observations": (),
            "limitations": _LIMITATIONS,
            "authority": "advisory",
            "mutation_authority": False,
        }
    )


def _owner_by_name(snapshot: KnowledgeSnapshot) -> dict[str, OwnerSnapshot]:
    result = {owner.owner: owner for owner in snapshot.owners}
    if len(result) != len(snapshot.owners):
        raise ValueError("Knowledge snapshot owner identities repeat")
    return result


def _runtime_by_name(
    statuses: tuple[RuntimeCapabilityStatus, ...],
) -> dict[str, RuntimeCapabilityStatus]:
    result = {status.capability: status for status in statuses}
    if len(result) != len(statuses):
        raise ValueError("runtime capability statuses repeat")
    if tuple(result) != ROUTE_CAPABILITY_NAMES or tuple(result) != BUILTIN_ROUTE_ORDER:
        raise ValueError("runtime capability probe does not cover the exact route registry")
    return result


def _route_observation(
    route_name: str,
    *,
    owner: OwnerSnapshot,
    runtime: RuntimeCapabilityStatus,
    text_causal: CodeCapabilityReachabilityAnalysis | None,
) -> RouteCapabilityObservation:
    store = STATE_STORE_REGISTRY.by_owner(route_name)
    if owner.expected_schema_version != store.expected_schema_version:
        raise ValueError("Knowledge owner expected schema disagrees with the state registry")
    spec = CAPABILITY_SPECS[route_name]
    required = tuple(item for item in runtime.components if item.requirement.required)
    missing_required = tuple(
        sorted(item.requirement.component for item in required if not item.available)
    )
    missing_optional = tuple(
        sorted(
            item.requirement.component
            for item in runtime.components
            if not item.requirement.required and not item.available
        )
    )
    manifests = (
        tuple(
            item
            for item in CAPABILITY_MANIFESTS
            if item.capability_id == TEXT_EXTRACT_CAPABILITY_ID
        )
        if route_name == "text"
        else ()
    )
    causal_status: Literal["resolved", "not_available", "abstained"] = "not_available"
    causal_output = False
    if route_name == "text" and text_causal is not None:
        causal_status = "resolved" if text_causal.status == "ready" else "abstained"
        causal_output = text_causal.status == "ready" and any(
            item.reachability == "published_result_observed" for item in text_causal.observations
        )
    values: dict[str, object] = {
        "route_name": route_name,
        "state_owner_id": route_name,
        "state_store_id": store.state_store_id,
        "state_store_schema": store.expected_schema_version,
        "route_registered": True,
        "runtime_state": runtime.state.value,
        "declared_components": len(spec.requirements),
        "required_components": len(required),
        "available_required_components": sum(item.available for item in required),
        "missing_required_components": missing_required,
        "missing_optional_components": missing_optional,
        "owner_state": owner.state.value,
        "owner_schema_current": owner.state is OwnerAvailability.AVAILABLE
        and owner.observed_schema_version == store.expected_schema_version,
        "owner_publications": len(owner.publications),
        "owner_watermarks": len(owner.watermarks),
        "capability_manifest_count": len(manifests),
        "causal_projection_status": causal_status,
        "causal_durable_output_observed": causal_output,
        "public_read_consumer_observed": False,
        "public_acceptance_scenario_observed": False,
        "evidence_level": (
            "causal_durable_path_observed"
            if causal_output
            else "owner_state_observed_unattributed"
            if owner.state is OwnerAvailability.AVAILABLE
            and (owner.publications or owner.watermarks)
            else "declared_only"
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    return RouteCapabilityObservation(
        observation_id=analysis_identity("route-capability-observation-v1", values),
        **values,  # type: ignore[arg-type]
    )


def resolve_route_capability_analysis(
    snapshot: KnowledgeSnapshot,
    runtime_statuses: tuple[RuntimeCapabilityStatus, ...],
    *,
    source_version: str,
    text_causal: CodeCapabilityReachabilityAnalysis | None = None,
) -> CodeRouteCapabilityAnalysis:
    """Resolve a portfolio from typed observations already captured by owners."""

    if not isinstance(snapshot, KnowledgeSnapshot):
        raise ValueError("route capability analysis requires a Knowledge snapshot")
    if snapshot.consistency is not SnapshotConsistency.STABLE:
        return abstained_route_capability_analysis(
            "knowledge_snapshot_changed",
            source_version=source_version,
        )
    if snapshot.source_version != source_version:
        raise ValueError("Knowledge snapshot and route capability source versions disagree")
    registry = builtin_route_registry()
    if tuple(registry) != BUILTIN_ROUTE_ORDER or any(
        registry[name].name != name or not callable(registry[name].execute)
        for name in BUILTIN_ROUTE_ORDER
    ):
        raise ValueError("built-in route registry is inconsistent")
    owners = _owner_by_name(snapshot)
    runtime = _runtime_by_name(runtime_statuses)
    missing_owners = tuple(name for name in BUILTIN_ROUTE_ORDER if name not in owners)
    if missing_owners:
        raise ValueError("Knowledge snapshot does not cover every route state owner")
    if text_causal is not None and text_causal.source_version != source_version:
        raise ValueError("Text causal projection and portfolio source versions disagree")
    observations = tuple(
        _route_observation(
            name,
            owner=owners[name],
            runtime=runtime[name],
            text_causal=text_causal,
        )
        for name in BUILTIN_ROUTE_ORDER
    )
    values: dict[str, object] = {
        "status": "ready",
        "reason": None,
        "policy_id": CODE_ROUTE_CAPABILITY_POLICY,
        "source_version": source_version,
        "knowledge_snapshot_id": snapshot.snapshot_id,
        "knowledge_snapshot_consistency": snapshot.consistency.value,
        "knowledge_capture_attempts": snapshot.attempts,
        "route_registry_names": BUILTIN_ROUTE_ORDER,
        "runtime_probe_policy": RUNTIME_CAPABILITY_PROBE_POLICY,
        "state_store_registry_schema": STATE_STORE_REGISTRY_SCHEMA,
        "text_causal_analysis_id": (
            text_causal.analysis_id
            if text_causal is not None and text_causal.status == "ready"
            else None
        ),
        "observations": observations,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _analysis(values)


def analyze_route_capabilities(
    state_directory: Path,
    *,
    source_version: str,
) -> CodeRouteCapabilityAnalysis:
    """Capture and fence every route observation without mutating owner state."""

    try:
        paths = KnowledgeStatePaths.from_directory(Path(state_directory))
        before = collect_knowledge_snapshot(paths, source_version=source_version)
        if before.consistency is not SnapshotConsistency.STABLE:
            return abstained_route_capability_analysis(
                "knowledge_snapshot_changed_before_capability_projection",
                source_version=source_version,
            )
        runtime = inspect_runtime_capabilities(ROUTE_CAPABILITY_NAMES)
        text_causal = analyze_capability_reachability(
            Path(state_directory),
            source_version=source_version,
        )
        after = collect_knowledge_snapshot(paths, source_version=source_version)
        if (
            after.consistency is not SnapshotConsistency.STABLE
            or before.snapshot_id != after.snapshot_id
        ):
            return abstained_route_capability_analysis(
                "knowledge_snapshot_changed_across_capability_projection",
                source_version=source_version,
            )
        return resolve_route_capability_analysis(
            after,
            runtime,
            source_version=source_version,
            text_causal=text_causal,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return abstained_route_capability_analysis(
            f"route_capability_unresolvable:{type(exc).__name__}",
            source_version=source_version,
        )


def _projection_digest(value: object) -> str:
    return analysis_identity("route-capability-source-projection-v1", value)


def _evidence(
    observation: RouteCapabilityObservation,
    *,
    analysis: CodeRouteCapabilityAnalysis,
    kind: Literal["contract", "runtime", "owner", "counterevidence", "causal"],
) -> AnalysisEvidenceRef:
    subject_key = f"capability:route:{observation.route_name}"
    facts: tuple[AnalysisFact, ...]
    if kind == "contract":
        role = "supporting"
        evidence_kind = "contract"
        owner = "code"
        producer = "route-capability-contract-resolver"
        schema = f"{STATE_STORE_REGISTRY_SCHEMA}+neocortex.runtime-capability/v1"
        record_kind = "route_runtime_state_contract"
        facts = (
            AnalysisFact("route_registered", observation.route_registered),
            AnalysisFact("state_owner_id", observation.state_owner_id),
            AnalysisFact("state_store_id", observation.state_store_id),
            AnalysisFact("state_store_schema", observation.state_store_schema),
            AnalysisFact("capability_manifest_count", observation.capability_manifest_count),
        )
    elif kind == "runtime":
        role = "supporting"
        evidence_kind = "runtime_observation"
        owner = "code"
        producer = "safe-runtime-capability-probe"
        schema = "neocortex.runtime-capability-status/v1"
        record_kind = "runtime_prerequisite_observation"
        facts = (
            AnalysisFact("runtime_state", observation.runtime_state),
            AnalysisFact("declared_components", observation.declared_components, "count"),
            AnalysisFact("required_components", observation.required_components, "count"),
            AnalysisFact(
                "available_required_components",
                observation.available_required_components,
                "count",
            ),
            AnalysisFact(
                "missing_required_components", len(observation.missing_required_components), "count"
            ),
            AnalysisFact(
                "missing_optional_components", len(observation.missing_optional_components), "count"
            ),
        )
    elif kind == "owner":
        role = "supporting"
        evidence_kind = "runtime_observation"
        owner = observation.state_owner_id
        producer = "knowledge-snapshot-resolver"
        schema = "neocortex.knowledge-contract/v1"
        record_kind = "state_owner_snapshot"
        facts = (
            AnalysisFact("owner_state", observation.owner_state),
            AnalysisFact("owner_schema_current", observation.owner_schema_current),
            AnalysisFact("owner_publications", observation.owner_publications, "count"),
            AnalysisFact("owner_watermarks", observation.owner_watermarks, "count"),
        )
    elif kind == "counterevidence":
        role = "counterevidence"
        evidence_kind = "runtime_observation"
        owner = "code"
        producer = "route-capability-counterevidence-resolver"
        schema = CODE_ROUTE_CAPABILITY_SCHEMA
        record_kind = "declaration_only_counterevidence"
        facts = (
            AnalysisFact("evidence_level", observation.evidence_level),
            AnalysisFact("owner_state_is_route_attributed", False),
            AnalysisFact("public_read_consumer_observed", False),
            AnalysisFact("public_acceptance_scenario_observed", False),
        )
    else:
        if not observation.causal_durable_output_observed:
            raise ValueError("causal evidence requires an observed durable output")
        role = "supporting"
        evidence_kind = "runtime_observation"
        owner = "text"
        producer = "text-owner-capability-reachability-resolver"
        schema = "neocortex.code-capability-reachability/v1"
        record_kind = "causal_durable_output_projection"
        facts = (
            AnalysisFact("causal_projection_status", observation.causal_projection_status),
            AnalysisFact("causal_durable_output_observed", True),
        )
    projection = {
        "analysis_id": analysis.analysis_id,
        "observation_id": observation.observation_id,
        "kind": kind,
        "facts": tuple(asdict(fact) for fact in facts),
    }
    digest = _projection_digest(projection)
    evidence_id = analysis_identity("route-capability-evidence-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=evidence_id,
        subject_key=subject_key,
        role=cast(Any, role),
        evidence_kind=cast(Any, evidence_kind),
        source_owner_id=owner,
        producer_id=producer,
        producer_version="v1",
        source_schema=schema,
        source_record_kind=record_kind,
        source_record_id=observation.observation_id,
        source_projection_digest=digest,
        snapshot_id=_required_text("route capability snapshot", analysis.knowledge_snapshot_id),
        revision_id=analysis.source_version,
        facts=facts,
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id=producer,
        resolver_version="v1",
        limitations=(
            "route_observation_has_no_mutation_authority",
            "declared_or_durable_state_does_not_prove_user_visible_value",
        ),
    )


def route_capability_questions(
    analysis: CodeRouteCapabilityAnalysis,
    *,
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("route capability rank offset must be non-negative")
    if analysis.status != "ready" or analysis.knowledge_snapshot_id is None:
        spec = ROUTE_CAPABILITY_AVAILABILITY_QUESTION
        reason = analysis.reason or "route_capability_evidence_unavailable"
        requirements = tuple(
            AnalysisRequirementEvaluation(
                requirement.requirement_id,
                "missing" if requirement.stage == "question" else "not_evaluated",
                (),
                reason
                if requirement.stage == "question"
                else "decision_evidence_not_evaluated_without_stable_snapshot",
            )
            for requirement in spec.requirements
        )
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "route-capability-availability-evaluation-v1",
                {"analysis_id": analysis.analysis_id, "reason": reason},
            ),
            question_id=spec.question_id,
            question_version=spec.version,
            question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
            rank=rank_offset + 1,
            subject=AnalysisSubjectRef(
                subject_kind="run",
                subject_key=f"route-capability-analysis:{analysis.analysis_id}",
                display_name="Route capability portfolio evidence",
                source_owner_id="code",
                snapshot_id=analysis.analysis_id,
                snapshot_freshness="unknown",
                revision_id=analysis.source_version,
            ),
            evidence=(),
            requirements=requirements,
            observation_status="abstained",
            inference_status="abstained",
            inferences=(),
            hypotheses=spec.hypotheses,
            question_readiness="abstained",
            decision_readiness="abstained",
            decision=None,
            decision_reason="question_evidence_incomplete",
            counterevidence_status="not_evaluated",
            next_action_ids=(),
            limitations=(*_LIMITATIONS, reason),
        )
        validate_analysis_question_evaluation(spec, evaluation)
        return (spec,), (evaluation,)
    spec = ROUTE_CAPABILITY_QUESTION
    fingerprint = analysis_question_spec_fingerprint(spec)
    evaluations: list[AnalysisQuestionEvaluation] = []
    for index, observation in enumerate(analysis.observations, start=1):
        contract = _evidence(observation, analysis=analysis, kind="contract")
        runtime = _evidence(observation, analysis=analysis, kind="runtime")
        owner = _evidence(observation, analysis=analysis, kind="owner")
        counter = _evidence(observation, analysis=analysis, kind="counterevidence")
        causal = (
            _evidence(observation, analysis=analysis, kind="causal")
            if observation.causal_durable_output_observed
            else None
        )
        evidence = (contract, runtime, owner, *((causal,) if causal is not None else ()), counter)
        requirements = (
            AnalysisRequirementEvaluation(
                "route_runtime_and_state_contract_resolved",
                "satisfied",
                (contract.evidence_id,),
                "exact_route_runtime_and_state_store_contract_joined",
            ),
            AnalysisRequirementEvaluation(
                "runtime_prerequisites_observed",
                "satisfied",
                (runtime.evidence_id,),
                "safe_metadata_spec_and_path_probe_completed",
            ),
            AnalysisRequirementEvaluation(
                "state_owner_snapshot_observed",
                "satisfied",
                (owner.evidence_id,),
                "stable_cross_owner_snapshot_resolved",
            ),
            AnalysisRequirementEvaluation(
                "causal_durable_execution_path_observed",
                "satisfied" if causal is not None else "missing",
                (causal.evidence_id,) if causal is not None else (),
                "owner_native_manifest_receipt_output_chain_resolved"
                if causal is not None
                else "owner_native_causal_receipt_output_chain_not_observed",
            ),
            AnalysisRequirementEvaluation(
                "public_read_consumer_observed",
                "missing",
                (),
                "no_public_read_consumer_execution_is_linked",
            ),
            AnalysisRequirementEvaluation(
                "public_acceptance_scenario_observed",
                "missing",
                (),
                "no_isolated_public_acceptance_scenario_is_linked",
            ),
            AnalysisRequirementEvaluation(
                "declaration_only_counterevidence_evaluated",
                "satisfied",
                (counter.evidence_id,),
                "declarations_owner_state_and_causal_receipts_are_separated",
            ),
        )
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "route-capability-question-evaluation-v1",
                {
                    "analysis_id": analysis.analysis_id,
                    "observation_id": observation.observation_id,
                    "question_spec": fingerprint,
                    "evidence_ids": tuple(item.evidence_id for item in evidence),
                },
            ),
            question_id=spec.question_id,
            question_version=spec.version,
            question_spec_fingerprint=fingerprint,
            rank=rank_offset + index,
            subject=AnalysisSubjectRef(
                subject_kind="capability",
                subject_key=f"capability:route:{observation.route_name}",
                display_name=observation.route_name,
                source_owner_id=observation.state_owner_id,
                snapshot_id=analysis.knowledge_snapshot_id,
                snapshot_freshness="current",
                revision_id=analysis.source_version,
            ),
            evidence=evidence,
            requirements=requirements,
            observation_status="confirmed",
            inference_status="abstained",
            inferences=(),
            hypotheses=spec.hypotheses,
            question_readiness="ready",
            decision_readiness="experiment_required",
            decision=None,
            decision_reason="decision_evidence_incomplete",
            counterevidence_status="evaluated",
            next_action_ids=tuple(item.action_id for item in spec.next_actions),
            limitations=_LIMITATIONS,
        )
        validate_analysis_question_evaluation(spec, evaluation)
        evaluations.append(evaluation)
    return (spec,), tuple(evaluations)


def parse_code_route_capability_payload(
    payload: Mapping[str, object],
) -> CodeRouteCapabilityAnalysis:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_ROUTE_CAPABILITY_SCHEMA:
        raise ValueError("route capability payload schema is invalid")
    expected = {field.name for field in fields(CodeRouteCapabilityAnalysis)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("route capability payload fields are invalid")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, Sequence) or isinstance(
        raw_observations, (str, bytes, bytearray)
    ):
        raise ValueError("route capability observations are invalid")
    observation_fields = {field.name for field in fields(RouteCapabilityObservation)}
    observations: list[RouteCapabilityObservation] = []
    for raw in raw_observations:
        if not isinstance(raw, Mapping) or set(raw) != observation_fields:
            raise ValueError("route capability observation fields are invalid")
        values = dict(raw)
        values["missing_required_components"] = _texts(
            "missing required component", values["missing_required_components"], sorted_values=True
        )
        values["missing_optional_components"] = _texts(
            "missing optional component", values["missing_optional_components"], sorted_values=True
        )
        observations.append(RouteCapabilityObservation(**cast(Any, values)))
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["route_registry_names"] = _texts("route registry name", values["route_registry_names"])
    values["limitations"] = _texts("route capability limitation", values["limitations"])
    values["observations"] = tuple(observations)
    return CodeRouteCapabilityAnalysis(**cast(Any, values))


__all__ = [
    "CODE_ROUTE_CAPABILITY_POLICY",
    "CODE_ROUTE_CAPABILITY_SCHEMA",
    "ROUTE_CAPABILITY_AVAILABILITY_QUESTION",
    "ROUTE_CAPABILITY_QUESTION",
    "CodeRouteCapabilityAnalysis",
    "RouteCapabilityObservation",
    "abstained_route_capability_analysis",
    "analyze_route_capabilities",
    "parse_code_route_capability_payload",
    "resolve_route_capability_analysis",
    "route_capability_questions",
]
