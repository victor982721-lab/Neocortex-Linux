"""Exact, read-only reachability evidence for the implemented Text capability.

This is intentionally narrower than a product capability graph.  It joins the
versioned ``text.extract`` manifests to the built-in route registry and to the
owner-native Text attempts, receipts, outbox events, output bindings, and
published materialization heads.  A published head is evidence of a durable
result; it is not evidence that a human saw or valued that result.
"""

from __future__ import annotations
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from neocortex.capabilities.runtime import CAPABILITY_MANIFESTS, TEXT_EXTRACT_CAPABILITY_ID
from neocortex.capability_broker import CapabilityManifest

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
from neocortex.runtime.orchestration.route_registry import builtin_route_registry
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database
from neocortex.sqlite_schema_contract import (
    read_application_schema_version,
    validate_sqlite_schema_contract,
)
from neocortex.capabilities.formats.text.text_state import TEXT_SCHEMA_VERSION, text_schema_contract

CODE_CAPABILITY_REACHABILITY_SCHEMA = "neocortex.code-capability-reachability/v1"
CAPABILITY_REACHABILITY_POLICY = "text-extract-durable-reachability-v1"
CAPABILITY_REACHABILITY_MAX_ATTEMPTS = 100_000
CAPABILITY_REACHABILITY_EXAMPLE_LIMIT = 20

_LIMITATIONS = (
    "route_registration_is_not_public_entrypoint_execution",
    "durable_text_head_is_not_user_visible_or_product_value_evidence",
    "terminal_state_does_not_prove_process_death_or_power_loss_recovery",
    "only_current_text_extract_manifests_are_evaluated",
    "no_static_or_dynamic_reachability_is_inferred_for_other_capabilities",
)

CAPABILITY_REACHABILITY_QUESTION = AnalysisQuestionSpec(
    question_id="capability.text_extract_has_durable_execution_path",
    version="v1",
    subject_kinds=("capability",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "resolved_manifest_and_route_contract",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "resolved_owner_native_execution_projection",
            "question",
            "supporting",
            ("runtime_observation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "user_visible_consumer_observed",
            "decision",
            "supporting",
            ("runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "acceptance_scenario_executed",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "declaration_only_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("runtime_observation", "experiment_result"),
        ),
    ),
    hypotheses=(
        "capability_is_only_declared",
        "capability_has_a_durable_execution_path",
        "capability_reaches_user_visible_value",
    ),
    counterevidence_rules=(
        "manifest_without_matching_terminal_attempt_supports_declaration_only",
        "terminal_attempt_without_current_head_does_not_prove_published_result",
        "published_head_without_public_entrypoint_observation_does_not_prove_user_value",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "exercise_text_capability_from_public_entrypoint",
            "experiment",
            "Run a bounded public Text route scenario and resolve its receipt and output.",
        ),
        AnalysisNextActionSpec(
            "observe_user_visible_consumer_of_text_result",
            "characterization",
            "Trace one published Text result through a read API to visible output.",
        ),
        AnalysisNextActionSpec(
            "inspect_manifest_attempt_mismatches",
            "counterevidence_search",
            "Resolve any attempt whose manifest, provider, or durable closure disagrees.",
        ),
    ),
)

CAPABILITY_REACHABILITY_AVAILABILITY_QUESTION = AnalysisQuestionSpec(
    question_id="capability.text_extract_evidence_provider_is_resolved",
    version="v1",
    subject_kinds=("run",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "text_owner_snapshot_resolved",
            "question",
            "supporting",
            ("contract", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "manifest_and_route_contract_resolved",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "durable_execution_projection_resolved",
            "decision",
            "supporting",
            ("runtime_observation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "public_entrypoint_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("runtime_observation", "experiment_result"),
        ),
    ),
    hypotheses=(
        "capability_evidence_is_unavailable_or_incompatible",
        "capability_evidence_is_resolved_for_characterization",
    ),
    counterevidence_rules=(
        "an_unresolved_text_owner_snapshot_cannot_support_a_reachability_claim",
        "a_manifest_without_owner_native_runtime_evidence_is_declaration_only",
        "provider_absence_is_not_evidence_that_the_capability_is_unreachable",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "resolve_text_owner_snapshot",
            "characterization",
            "Resolve a current compatible Text owner snapshot before evaluating reachability.",
        ),
        AnalysisNextActionSpec(
            "rerun_capability_reachability_projection",
            "experiment",
            "Rerun the bounded capability projection against the resolved owner snapshot.",
        ),
    ),
)


class CapabilityReachabilityResolutionError(ValueError):
    """The exact Text capability projection cannot be resolved."""


def _required_text(label: str, value: object, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _text_tuple(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, value) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    return result


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class CapabilityImplementationObservation:
    observation_id: str
    capability_id: str
    capability_version: str
    implementation_id: str
    manifest_fingerprint: str
    provider: str
    provider_version: str | None
    route_name: str
    route_registered: bool
    attempts: int
    running_attempts: int
    terminal_attempts: int
    succeeded_attempts: int
    failed_attempts: int
    cancelled_attempts: int
    abandoned_attempts: int
    executed_attempts: int
    cache_hit_attempts: int
    replay_attempts: int
    attempted_attempts: int
    unknown_execution_mode_attempts: int
    manifest_matching_attempts: int
    manifest_matching_terminal_attempts: int
    manifest_mismatch_attempt_ids: tuple[str, ...]
    manifest_mismatch_truncated: bool
    receipt_linked_terminal_attempts: int
    outbox_linked_terminal_attempts: int
    total_output_bindings: int
    total_current_materialization_heads: int
    manifest_matching_output_bindings: int
    manifest_matching_current_heads: int
    durable_integrity_gap_count: int
    durable_integrity_gap_attempt_ids: tuple[str, ...]
    durable_integrity_gaps_truncated: bool
    reachability: Literal[
        "declared_only",
        "terminal_execution_observed",
        "published_result_observed",
    ]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value in (
            ("capability observation id", self.observation_id),
            ("capability id", self.capability_id),
            ("capability version", self.capability_version),
            ("implementation id", self.implementation_id),
            ("manifest fingerprint", self.manifest_fingerprint),
            ("provider", self.provider),
            ("route name", self.route_name),
        ):
            _required_text(label, value)
        if self.provider_version is not None:
            _required_text("provider version", self.provider_version)
        if not isinstance(self.route_registered, bool):
            raise ValueError("route registration must be boolean")
        count_names = (
            "attempts",
            "running_attempts",
            "terminal_attempts",
            "succeeded_attempts",
            "failed_attempts",
            "cancelled_attempts",
            "abandoned_attempts",
            "executed_attempts",
            "cache_hit_attempts",
            "replay_attempts",
            "attempted_attempts",
            "unknown_execution_mode_attempts",
            "manifest_matching_attempts",
            "manifest_matching_terminal_attempts",
            "receipt_linked_terminal_attempts",
            "outbox_linked_terminal_attempts",
            "total_output_bindings",
            "total_current_materialization_heads",
            "manifest_matching_output_bindings",
            "manifest_matching_current_heads",
            "durable_integrity_gap_count",
        )
        for name in count_names:
            _nonnegative(name, getattr(self, name))
        _text_tuple("manifest mismatch attempt", self.manifest_mismatch_attempt_ids)
        _text_tuple("durable gap attempt", self.durable_integrity_gap_attempt_ids)
        if not isinstance(self.manifest_mismatch_truncated, bool) or not isinstance(
            self.durable_integrity_gaps_truncated, bool
        ):
            raise ValueError("capability truncation flags must be boolean")
        if self.running_attempts + self.terminal_attempts != self.attempts:
            raise ValueError("capability attempt lifecycle counts do not partition attempts")
        if not 0 <= self.manifest_matching_terminal_attempts <= self.manifest_matching_attempts:
            raise ValueError("manifest-matching terminal count is invalid")
        if self.manifest_matching_attempts > self.attempts:
            raise ValueError("manifest-matching attempts exceed observed attempts")
        if self.receipt_linked_terminal_attempts > self.terminal_attempts or (
            self.outbox_linked_terminal_attempts > self.terminal_attempts
        ):
            raise ValueError("durable closure counts exceed terminal attempts")
        if self.manifest_matching_output_bindings > self.total_output_bindings or (
            self.manifest_matching_current_heads > self.total_current_materialization_heads
        ):
            raise ValueError("manifest-matching outputs exceed implementation totals")
        mismatch_count = self.attempts - self.manifest_matching_attempts
        if len(self.manifest_mismatch_attempt_ids) != min(
            mismatch_count, CAPABILITY_REACHABILITY_EXAMPLE_LIMIT
        ) or self.manifest_mismatch_truncated != (
            mismatch_count > CAPABILITY_REACHABILITY_EXAMPLE_LIMIT
        ):
            raise ValueError("manifest mismatch examples are not derived from exact counts")
        if len(self.durable_integrity_gap_attempt_ids) != min(
            self.durable_integrity_gap_count, CAPABILITY_REACHABILITY_EXAMPLE_LIMIT
        ) or self.durable_integrity_gaps_truncated != (
            self.durable_integrity_gap_count > CAPABILITY_REACHABILITY_EXAMPLE_LIMIT
        ):
            raise ValueError("durable gap examples are not derived from exact counts")
        if (
            self.succeeded_attempts
            + self.failed_attempts
            + self.cancelled_attempts
            + self.abandoned_attempts
            != self.terminal_attempts
        ):
            raise ValueError("capability terminal outcomes do not partition terminal attempts")
        if (
            self.executed_attempts
            + self.cache_hit_attempts
            + self.replay_attempts
            + self.attempted_attempts
            + self.unknown_execution_mode_attempts
            != self.terminal_attempts
        ):
            raise ValueError("capability execution modes do not partition terminal attempts")
        expected_reachability = (
            "published_result_observed"
            if self.manifest_matching_current_heads > 0
            else (
                "terminal_execution_observed"
                if self.manifest_matching_terminal_attempts > 0
                else "declared_only"
            )
        )
        if self.reachability != expected_reachability:
            raise ValueError("capability reachability is not derived from durable observations")
        expected_id = analysis_identity(
            "capability-implementation-observation-v1",
            {key: value for key, value in asdict(self).items() if key != "observation_id"},
        )
        if self.observation_id != expected_id:
            raise ValueError("capability observation identity is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("capability observation must remain advisory and non-mutating")


@dataclass(frozen=True, slots=True)
class CodeCapabilityReachabilityAnalysis:
    analysis_id: str
    status: Literal["ready", "abstained"]
    reason: str | None
    policy_id: str
    snapshot_id: str | None
    snapshot_freshness: Literal["current", "publication_only", "unknown"]
    source_version: str
    text_owner_schema: int | None
    route_registry_names: tuple[str, ...]
    manifest_count: int
    total_attempts: int | None
    unattributed_attempts: int | None
    unknown_implementation_ids: tuple[str, ...]
    malformed_configuration_attempt_ids: tuple[str, ...]
    malformed_configuration_truncated: bool
    observations: tuple[CapabilityImplementationObservation, ...]
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("capability analysis id", self.analysis_id)
        _required_text("capability policy id", self.policy_id)
        _required_text("capability source version", self.source_version)
        if self.policy_id != CAPABILITY_REACHABILITY_POLICY:
            raise ValueError("capability reachability policy is invalid")
        if self.status not in {"ready", "abstained"}:
            raise ValueError("capability analysis status is invalid")
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("capability snapshot freshness is invalid")
        _text_tuple("route registry name", self.route_registry_names)
        _text_tuple("unknown implementation", self.unknown_implementation_ids)
        _text_tuple("malformed configuration attempt", self.malformed_configuration_attempt_ids)
        _nonnegative("manifest count", self.manifest_count)
        if not isinstance(self.malformed_configuration_truncated, bool):
            raise ValueError("malformed configuration truncation flag must be boolean")
        if self.limitations != _LIMITATIONS:
            raise ValueError("capability analysis limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("capability analysis must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("capability abstention reason", self.reason, maximum=256)
            if (
                self.snapshot_id is not None
                or self.text_owner_schema is not None
                or self.total_attempts is not None
                or self.unattributed_attempts is not None
                or self.route_registry_names
                or self.unknown_implementation_ids
                or self.malformed_configuration_attempt_ids
                or self.observations
            ):
                raise ValueError("abstained capability analysis cannot assert partial evidence")
            if self.snapshot_freshness != "unknown":
                raise ValueError("abstained capability analysis freshness must be unknown")
        else:
            if self.reason is not None:
                raise ValueError("ready capability analysis cannot carry an abstention reason")
            _required_text("capability snapshot id", self.snapshot_id)
            if self.text_owner_schema != TEXT_SCHEMA_VERSION:
                raise ValueError("ready capability analysis requires the current Text schema")
            if self.total_attempts is None or self.unattributed_attempts is None:
                raise ValueError("ready capability analysis requires exact attempt counts")
            _nonnegative("total attempts", self.total_attempts)
            _nonnegative("unattributed attempts", self.unattributed_attempts)
            if len(self.observations) != self.manifest_count:
                raise ValueError("capability observations must cover every selected manifest")
            if tuple(item.implementation_id for item in self.observations) != tuple(
                sorted(item.implementation_id for item in self.observations)
            ):
                raise ValueError("capability observations must be deterministically ordered")
        expected_id = analysis_identity(
            "code-capability-reachability-analysis-v1",
            {key: value for key, value in asdict(self).items() if key != "analysis_id"},
        )
        if self.analysis_id != expected_id:
            raise ValueError("capability analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_CAPABILITY_REACHABILITY_SCHEMA, **asdict(self)}


def _analysis(values: dict[str, object]) -> CodeCapabilityReachabilityAnalysis:
    identity_values = dict(values)
    raw_observations = identity_values.get("observations")
    if isinstance(raw_observations, tuple):
        identity_values["observations"] = tuple(
            asdict(item) if isinstance(item, CapabilityImplementationObservation) else item
            for item in raw_observations
        )
    return CodeCapabilityReachabilityAnalysis(
        analysis_id=analysis_identity("code-capability-reachability-analysis-v1", identity_values),
        **values,  # type: ignore[arg-type]
    )


def abstained_capability_reachability(
    reason: str,
    *,
    source_version: str,
    manifest_count: int | None = None,
) -> CodeCapabilityReachabilityAnalysis:
    if manifest_count is None:
        manifest_count = len(_text_manifests())
    values: dict[str, object] = {
        "status": "abstained",
        "reason": _required_text("capability abstention reason", reason, maximum=256),
        "policy_id": CAPABILITY_REACHABILITY_POLICY,
        "snapshot_id": None,
        "snapshot_freshness": "unknown",
        "source_version": _required_text("capability source version", source_version),
        "text_owner_schema": None,
        "route_registry_names": (),
        "manifest_count": manifest_count,
        "total_attempts": None,
        "unattributed_attempts": None,
        "unknown_implementation_ids": (),
        "malformed_configuration_attempt_ids": (),
        "malformed_configuration_truncated": False,
        "observations": (),
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _analysis(values)


def _text_manifests() -> tuple[CapabilityManifest, ...]:
    manifests = tuple(
        sorted(
            (
                item
                for item in CAPABILITY_MANIFESTS
                if item.capability_id == TEXT_EXTRACT_CAPABILITY_ID
            ),
            key=lambda item: item.implementation_id,
        )
    )
    if not manifests or len({item.implementation_id for item in manifests}) != len(manifests):
        raise CapabilityReachabilityResolutionError("Text capability manifests are invalid")
    return manifests


def _bounded_ids(values: Sequence[str]) -> tuple[tuple[str, ...], bool]:
    ordered = tuple(sorted(set(values)))
    return ordered[:CAPABILITY_REACHABILITY_EXAMPLE_LIMIT], (
        len(ordered) > CAPABILITY_REACHABILITY_EXAMPLE_LIMIT
    )


def _rows_by_attempt(connection: sqlite3.Connection, query: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in connection.execute(query):
        attempt_id = _required_text("attempt identity", row[0])
        count = _nonnegative("attempt relation count", row[1])
        if attempt_id in result:
            raise CapabilityReachabilityResolutionError("attempt relation aggregation repeats")
        result[attempt_id] = count
    return result


def _observation(
    manifest: CapabilityManifest,
    rows: tuple[Mapping[str, object], ...],
    *,
    route_registered: bool,
    receipt_counts: Mapping[str, int],
    outbox_counts: Mapping[str, int],
    output_counts: Mapping[str, int],
    head_counts: Mapping[str, int],
) -> CapabilityImplementationObservation:
    statuses = Counter(str(row["status"]) for row in rows)
    modes = Counter(
        "unknown" if row["execution_mode"] is None else str(row["execution_mode"])
        for row in rows
        if str(row["status"]) != "running"
    )
    mismatches: list[str] = []
    gaps: list[str] = []
    matching_ids: set[str] = set()
    matching = 0
    for row in rows:
        attempt_id = _required_text("attempt id", row["attempt_id"])
        configuration = row["configuration"]
        expected = {
            "capability_id": manifest.capability_id,
            "capability_implementation": manifest.implementation_id,
            "capability_manifest_fingerprint": manifest.contract_fingerprint,
            "capability_provider": manifest.provider,
            "capability_provider_version": manifest.provider_version,
        }
        if (
            isinstance(configuration, Mapping)
            and all(configuration.get(key) == value for key, value in expected.items())
            and row["stage_id"] == manifest.capability_id
            and row["stage_version"] == manifest.capability_version
            and row["provider"] == manifest.provider
            and row["provider_version"] == manifest.provider_version
        ):
            matching += 1
            matching_ids.add(attempt_id)
        else:
            mismatches.append(attempt_id)
        terminal = str(row["status"]) != "running"
        if terminal and (receipt_counts.get(attempt_id) != 1 or outbox_counts.get(attempt_id) != 1):
            gaps.append(attempt_id)
        if not terminal and (receipt_counts.get(attempt_id, 0) or outbox_counts.get(attempt_id, 0)):
            gaps.append(attempt_id)
    mismatch_ids, mismatch_truncated = _bounded_ids(mismatches)
    gap_ids, gap_truncated = _bounded_ids(gaps)
    terminal_count = sum(
        statuses[name] for name in ("succeeded", "failed", "cancelled", "abandoned")
    )
    total_head_count = sum(
        head_counts.get(_required_text("attempt id", row["attempt_id"]), 0) for row in rows
    )
    matching_terminal_count = sum(
        _required_text("attempt id", row["attempt_id"]) in matching_ids
        and str(row["status"]) != "running"
        for row in rows
    )
    total_output_count = sum(
        output_counts.get(_required_text("attempt id", row["attempt_id"]), 0) for row in rows
    )
    matching_output_count = sum(
        output_counts.get(_required_text("attempt id", row["attempt_id"]), 0)
        for row in rows
        if _required_text("attempt id", row["attempt_id"]) in matching_ids
    )
    matching_head_count = sum(
        head_counts.get(_required_text("attempt id", row["attempt_id"]), 0)
        for row in rows
        if _required_text("attempt id", row["attempt_id"]) in matching_ids
    )
    values: dict[str, object] = {
        "capability_id": manifest.capability_id,
        "capability_version": manifest.capability_version,
        "implementation_id": manifest.implementation_id,
        "manifest_fingerprint": manifest.contract_fingerprint,
        "provider": manifest.provider,
        "provider_version": manifest.provider_version,
        "route_name": "text",
        "route_registered": route_registered,
        "attempts": len(rows),
        "running_attempts": statuses["running"],
        "terminal_attempts": terminal_count,
        "succeeded_attempts": statuses["succeeded"],
        "failed_attempts": statuses["failed"],
        "cancelled_attempts": statuses["cancelled"],
        "abandoned_attempts": statuses["abandoned"],
        "executed_attempts": modes["executed"],
        "cache_hit_attempts": modes["cache_hit"],
        "replay_attempts": modes["replay"],
        "attempted_attempts": modes["attempted"],
        "unknown_execution_mode_attempts": modes["unknown"],
        "manifest_matching_attempts": matching,
        "manifest_matching_terminal_attempts": matching_terminal_count,
        "manifest_mismatch_attempt_ids": mismatch_ids,
        "manifest_mismatch_truncated": mismatch_truncated,
        "receipt_linked_terminal_attempts": sum(
            receipt_counts.get(_required_text("attempt id", row["attempt_id"])) == 1
            for row in rows
            if str(row["status"]) != "running"
        ),
        "outbox_linked_terminal_attempts": sum(
            outbox_counts.get(_required_text("attempt id", row["attempt_id"])) == 1
            for row in rows
            if str(row["status"]) != "running"
        ),
        "total_output_bindings": total_output_count,
        "total_current_materialization_heads": total_head_count,
        "manifest_matching_output_bindings": matching_output_count,
        "manifest_matching_current_heads": matching_head_count,
        "durable_integrity_gap_count": len(set(gaps)),
        "durable_integrity_gap_attempt_ids": gap_ids,
        "durable_integrity_gaps_truncated": gap_truncated,
    }
    values["reachability"] = (
        "published_result_observed"
        if matching_head_count > 0
        else "terminal_execution_observed"
        if matching_terminal_count > 0
        else "declared_only"
    )
    values["authority"] = "advisory"
    values["mutation_authority"] = False
    return CapabilityImplementationObservation(
        observation_id=analysis_identity("capability-implementation-observation-v1", values),
        **values,  # type: ignore[arg-type]
    )


def analyze_capability_reachability(
    state_directory: Path,
    *,
    source_version: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"] = "current",
) -> CodeCapabilityReachabilityAnalysis:
    """Resolve current ``text.extract`` manifests against exact durable Text facts."""

    manifests = _text_manifests()
    state_directory = Path(state_directory)
    try:
        registry = builtin_route_registry()
        registry_names = tuple(registry)
        if registry_names != BUILTIN_ROUTE_ORDER or any(
            registry[name].name != name or not callable(registry[name].execute)
            for name in registry_names
        ):
            raise CapabilityReachabilityResolutionError("built-in route registry is inconsistent")
        text_path = state_directory / "text.sqlite3"
        with immutable_sqlite_database(text_path) as connection:
            text_schema = read_application_schema_version(connection, label="text")
            if text_schema != TEXT_SCHEMA_VERSION:
                raise CapabilityReachabilityResolutionError("Text schema is not current")
            validate_sqlite_schema_contract(
                connection,
                text_schema_contract(),
                label="text",
                exact=True,
            )
            total_attempts = int(
                connection.execute("SELECT COUNT(*) FROM text_derivation_attempts").fetchone()[0]
            )
            if total_attempts > CAPABILITY_REACHABILITY_MAX_ATTEMPTS:
                raise CapabilityReachabilityResolutionError("Text attempt bound exceeded")
            raw_rows = tuple(
                connection.execute(
                    """SELECT attempt_id,stage_id,stage_version,provider,provider_version,
                              status,execution_mode,effective_configuration_json
                    FROM text_derivation_attempts ORDER BY attempt_id"""
                ).fetchall()
            )
            receipt_counts = _rows_by_attempt(
                connection,
                "SELECT attempt_id,COUNT(*) FROM text_work_receipts GROUP BY attempt_id",
            )
            outbox_counts = _rows_by_attempt(
                connection,
                "SELECT attempt_id,COUNT(*) FROM text_derivation_outbox GROUP BY attempt_id",
            )
            output_counts = _rows_by_attempt(
                connection,
                "SELECT attempt_id,COUNT(*) FROM text_derivation_output_bindings GROUP BY attempt_id",
            )
            head_counts = _rows_by_attempt(
                connection,
                """SELECT r.attempt_id,COUNT(*)
                FROM text_materialization_heads h
                JOIN text_materializations m
                  ON m.owner=h.materialization_owner
                 AND m.materialization_id=h.materialization_id
                JOIN text_work_receipts r ON r.receipt_id=m.producer_receipt_id
                GROUP BY r.attempt_id""",
            )
    except (
        CapabilityReachabilityResolutionError,
        ImmutableSQLiteUnavailable,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as exc:
        return abstained_capability_reachability(
            f"capability_reachability_unresolvable:{type(exc).__name__}",
            source_version=source_version,
            manifest_count=len(manifests),
        )
    by_implementation: dict[str, list[Mapping[str, object]]] = {
        item.implementation_id: [] for item in manifests
    }
    malformed: list[str] = []
    unknown: set[str] = set()
    unattributed = 0
    for row in raw_rows:
        attempt_id = _required_text("attempt id", row["attempt_id"])
        try:
            configuration = json.loads(
                _required_text(
                    "attempt effective configuration", row["effective_configuration_json"]
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            configuration = None
        if not isinstance(configuration, dict):
            malformed.append(attempt_id)
            unattributed += 1
            continue
        values = dict(row)
        values["configuration"] = configuration
        projected = _MappingRow(values)
        implementation = configuration.get("capability_implementation")
        if not isinstance(implementation, str) or not implementation:
            unattributed += 1
        elif implementation in by_implementation:
            by_implementation[implementation].append(projected)
        else:
            unknown.add(implementation)
            unattributed += 1
    malformed_ids, malformed_truncated = _bounded_ids(malformed)
    observations = tuple(
        _observation(
            manifest,
            tuple(by_implementation[manifest.implementation_id]),
            route_registered="text" in registry,
            receipt_counts=receipt_counts,
            outbox_counts=outbox_counts,
            output_counts=output_counts,
            head_counts=head_counts,
        )
        for manifest in manifests
    )
    snapshot_id = analysis_identity(
        "capability-reachability-snapshot-v1",
        {
            "source_version": source_version,
            "snapshot_freshness": snapshot_freshness,
            "text_schema": text_schema,
            "route_registry_names": registry_names,
            "manifest_fingerprints": tuple(item.contract_fingerprint for item in manifests),
            "attempt_observations": tuple(item.observation_id for item in observations),
            "total_attempts": total_attempts,
            "unattributed_attempts": unattributed,
            "unknown_implementation_ids": tuple(sorted(unknown)),
            "malformed_configuration_attempt_ids": malformed_ids,
            "malformed_configuration_truncated": malformed_truncated,
        },
    )
    values = {
        "status": "ready",
        "reason": None,
        "policy_id": CAPABILITY_REACHABILITY_POLICY,
        "snapshot_id": snapshot_id,
        "snapshot_freshness": snapshot_freshness,
        "source_version": source_version,
        "text_owner_schema": text_schema,
        "route_registry_names": registry_names,
        "manifest_count": len(manifests),
        "total_attempts": total_attempts,
        "unattributed_attempts": unattributed,
        "unknown_implementation_ids": tuple(sorted(unknown)),
        "malformed_configuration_attempt_ids": malformed_ids,
        "malformed_configuration_truncated": malformed_truncated,
        "observations": observations,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _analysis(values)


class _MappingRow(dict[str, object]):
    """Small sqlite-row compatible mapping used after strict JSON decoding."""


def _contract_evidence(
    observation: CapabilityImplementationObservation,
    *,
    snapshot_id: str,
    freshness: Literal["current", "publication_only", "unknown"],
) -> AnalysisEvidenceRef:
    facts = (
        AnalysisFact("capability_id", observation.capability_id),
        AnalysisFact("capability_version", observation.capability_version),
        AnalysisFact("implementation_id", observation.implementation_id),
        AnalysisFact("manifest_fingerprint", observation.manifest_fingerprint),
        AnalysisFact("provider", observation.provider),
        AnalysisFact("provider_version", observation.provider_version),
        AnalysisFact("route_name", observation.route_name),
        AnalysisFact("route_registered", observation.route_registered),
        AnalysisFact("snapshot_freshness", freshness),
    )
    digest = analysis_identity("capability-contract-projection-v1", asdict(observation))
    subject_key = f"capability:{observation.capability_id}:{observation.implementation_id}"
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "capability-contract-evidence-v1",
            {"subject_key": subject_key, "digest": digest, "snapshot": snapshot_id},
        ),
        subject_key=subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id="capability-manifest-and-route-resolver",
        producer_version="v1",
        source_schema="neocortex.capability-manifest/v1+route-registry/v1",
        source_record_kind="capability_manifest_and_route",
        source_record_id=observation.manifest_fingerprint,
        source_projection_digest=digest,
        snapshot_id=snapshot_id,
        revision_id=observation.manifest_fingerprint,
        facts=facts,
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="capability-manifest-route-resolver",
        resolver_version="v1",
        limitations=("route_registration_does_not_prove_public_entrypoint_execution",),
    )


def _runtime_evidence(
    observation: CapabilityImplementationObservation,
    *,
    snapshot_id: str,
) -> AnalysisEvidenceRef:
    facts = (
        *(
            AnalysisFact(name, getattr(observation, name), "count")
            for name in (
                "attempts",
                "running_attempts",
                "terminal_attempts",
                "succeeded_attempts",
                "failed_attempts",
                "cancelled_attempts",
                "abandoned_attempts",
                "executed_attempts",
                "cache_hit_attempts",
                "replay_attempts",
                "manifest_matching_attempts",
                "manifest_matching_terminal_attempts",
                "receipt_linked_terminal_attempts",
                "outbox_linked_terminal_attempts",
                "total_output_bindings",
                "total_current_materialization_heads",
                "manifest_matching_output_bindings",
                "manifest_matching_current_heads",
                "durable_integrity_gap_count",
            )
        ),
        AnalysisFact("reachability", observation.reachability),
        AnalysisFact(
            "manifest_mismatch_attempt_count",
            observation.attempts - observation.manifest_matching_attempts,
            "count",
        ),
        AnalysisFact(
            "manifest_mismatch_examples_truncated",
            observation.manifest_mismatch_truncated,
        ),
        AnalysisFact(
            "manifest_mismatch_attempt_ids_json",
            json.dumps(observation.manifest_mismatch_attempt_ids, separators=(",", ":")),
        ),
        AnalysisFact(
            "durable_integrity_gap_attempt_ids_json",
            json.dumps(observation.durable_integrity_gap_attempt_ids, separators=(",", ":")),
        ),
    )
    digest = analysis_identity("capability-runtime-projection-v1", asdict(observation))
    subject_key = f"capability:{observation.capability_id}:{observation.implementation_id}"
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "capability-runtime-evidence-v1",
            {"subject_key": subject_key, "digest": digest, "snapshot": snapshot_id},
        ),
        subject_key=subject_key,
        role="supporting",
        evidence_kind="runtime_observation",
        source_owner_id="text",
        producer_id="text-owner-capability-reachability-resolver",
        producer_version="v1",
        source_schema=f"neocortex.text-state/sqlite-v{TEXT_SCHEMA_VERSION}",
        source_record_kind="text_capability_execution_projection",
        source_record_id=observation.implementation_id,
        source_projection_digest=digest,
        snapshot_id=snapshot_id,
        revision_id=observation.manifest_fingerprint,
        facts=facts,
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="text-owner-capability-reachability-resolver",
        resolver_version="v1",
        limitations=(
            "durable_execution_does_not_prove_user_visible_consumption",
            "current_head_does_not_prove_acceptance_scenario",
        ),
    )


def capability_reachability_questions(
    analysis: CodeCapabilityReachabilityAnalysis,
    *,
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Build generic epistemic evaluations only from a resolved exact projection."""

    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("capability question rank offset must be non-negative")
    if analysis.status != "ready" or analysis.snapshot_id is None:
        spec = CAPABILITY_REACHABILITY_AVAILABILITY_QUESTION
        reason = analysis.reason or "capability_reachability_evidence_unavailable"
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "code-capability-reachability-availability-question-v1",
                {
                    "analysis_id": analysis.analysis_id,
                    "question": spec.question_id,
                    "reason": reason,
                },
            ),
            question_id=spec.question_id,
            question_version=spec.version,
            question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
            rank=rank_offset + 1,
            subject=AnalysisSubjectRef(
                subject_kind="run",
                subject_key=f"capability-reachability-analysis:{analysis.analysis_id}",
                display_name="Text capability reachability evidence",
                source_owner_id="code",
                snapshot_id=analysis.analysis_id,
                snapshot_freshness="unknown",
                revision_id=analysis.source_version,
            ),
            evidence=(),
            requirements=(
                AnalysisRequirementEvaluation(
                    "text_owner_snapshot_resolved",
                    "missing",
                    (),
                    reason,
                ),
                AnalysisRequirementEvaluation(
                    "manifest_and_route_contract_resolved",
                    "not_evaluated",
                    (),
                    "route_contract_not_joined_without_owner_snapshot",
                ),
                AnalysisRequirementEvaluation(
                    "durable_execution_projection_resolved",
                    "missing",
                    (),
                    "owner_native_execution_projection_unavailable",
                ),
                AnalysisRequirementEvaluation(
                    "public_entrypoint_counterevidence_evaluated",
                    "not_evaluated",
                    (),
                    "counterevidence_not_evaluated_without_resolved_execution_subject",
                ),
            ),
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
            limitations=(
                *_LIMITATIONS,
                "analysis_envelope_identity_is_not_a_resolved_text_owner_snapshot",
                reason,
            ),
        )
        validate_analysis_question_evaluation(spec, evaluation)
        return (spec,), (evaluation,)
    fingerprint = analysis_question_spec_fingerprint(CAPABILITY_REACHABILITY_QUESTION)
    evaluations: list[AnalysisQuestionEvaluation] = []
    for index, observation in enumerate(analysis.observations, start=1):
        subject_key = f"capability:{observation.capability_id}:{observation.implementation_id}"
        contract = _contract_evidence(
            observation,
            snapshot_id=analysis.snapshot_id,
            freshness=analysis.snapshot_freshness,
        )
        runtime = _runtime_evidence(observation, snapshot_id=analysis.snapshot_id)
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "code-question-evaluation-v1",
                {
                    "analysis_id": analysis.analysis_id,
                    "observation_id": observation.observation_id,
                    "question_spec": fingerprint,
                    "contract_evidence": contract.evidence_id,
                    "runtime_evidence": runtime.evidence_id,
                },
            ),
            question_id=CAPABILITY_REACHABILITY_QUESTION.question_id,
            question_version=CAPABILITY_REACHABILITY_QUESTION.version,
            question_spec_fingerprint=fingerprint,
            rank=rank_offset + index,
            subject=AnalysisSubjectRef(
                subject_kind="capability",
                subject_key=subject_key,
                display_name=observation.implementation_id,
                source_owner_id="text",
                snapshot_id=analysis.snapshot_id,
                snapshot_freshness=analysis.snapshot_freshness,
                revision_id=observation.manifest_fingerprint,
            ),
            evidence=(contract, runtime),
            requirements=(
                AnalysisRequirementEvaluation(
                    "resolved_manifest_and_route_contract",
                    "satisfied",
                    (contract.evidence_id,),
                    "current_manifest_and_route_contract_resolved",
                ),
                AnalysisRequirementEvaluation(
                    "resolved_owner_native_execution_projection",
                    "satisfied",
                    (runtime.evidence_id,),
                    "complete_text_owner_execution_tables_scanned",
                ),
                AnalysisRequirementEvaluation(
                    "user_visible_consumer_observed",
                    "missing",
                    (),
                    "no_user_visible_consumer_evidence_linked",
                ),
                AnalysisRequirementEvaluation(
                    "acceptance_scenario_executed",
                    "missing",
                    (),
                    "no_public_entrypoint_acceptance_result_linked",
                ),
                AnalysisRequirementEvaluation(
                    "declaration_only_counterevidence_evaluated",
                    "not_evaluated",
                    (),
                    "counterevidence_requires_public_entrypoint_experiment",
                ),
            ),
            observation_status="confirmed",
            inference_status="abstained",
            inferences=(),
            hypotheses=CAPABILITY_REACHABILITY_QUESTION.hypotheses,
            question_readiness="ready",
            decision_readiness="experiment_required",
            decision=None,
            decision_reason="decision_evidence_incomplete",
            counterevidence_status="not_evaluated",
            next_action_ids=tuple(
                item.action_id for item in CAPABILITY_REACHABILITY_QUESTION.next_actions
            ),
            limitations=_LIMITATIONS,
        )
        validate_analysis_question_evaluation(CAPABILITY_REACHABILITY_QUESTION, evaluation)
        evaluations.append(evaluation)
    return ((CAPABILITY_REACHABILITY_QUESTION,) if evaluations else ()), tuple(evaluations)


def parse_capability_reachability_payload(
    payload: Mapping[str, object],
) -> CodeCapabilityReachabilityAnalysis:
    """Strictly reconstruct the public v1 projection."""

    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != CODE_CAPABILITY_REACHABILITY_SCHEMA
    ):
        raise ValueError("capability reachability payload schema is invalid")
    expected = {field.name for field in fields(CodeCapabilityReachabilityAnalysis)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("capability reachability payload fields are invalid")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, Sequence) or isinstance(
        raw_observations, (str, bytes, bytearray)
    ):
        raise ValueError("capability reachability observations are invalid")
    observation_fields = {field.name for field in fields(CapabilityImplementationObservation)}
    observations: list[CapabilityImplementationObservation] = []
    for raw in raw_observations:
        if not isinstance(raw, Mapping) or set(raw) != observation_fields:
            raise ValueError("capability reachability observation fields are invalid")
        values = dict(raw)
        for key in ("manifest_mismatch_attempt_ids", "durable_integrity_gap_attempt_ids"):
            values[key] = _text_tuple(f"capability observation {key}", values[key])
        observations.append(CapabilityImplementationObservation(**values))
    values = {key: value for key, value in payload.items() if key != "schema"}
    for key in (
        "route_registry_names",
        "unknown_implementation_ids",
        "malformed_configuration_attempt_ids",
        "limitations",
    ):
        values[key] = _text_tuple(f"capability analysis {key}", values[key])
    values["observations"] = tuple(observations)
    return CodeCapabilityReachabilityAnalysis(**cast(Any, values))


__all__ = [
    "CAPABILITY_REACHABILITY_AVAILABILITY_QUESTION",
    "CAPABILITY_REACHABILITY_MAX_ATTEMPTS",
    "CAPABILITY_REACHABILITY_POLICY",
    "CAPABILITY_REACHABILITY_QUESTION",
    "CODE_CAPABILITY_REACHABILITY_SCHEMA",
    "CapabilityImplementationObservation",
    "CapabilityReachabilityResolutionError",
    "CodeCapabilityReachabilityAnalysis",
    "abstained_capability_reachability",
    "analyze_capability_reachability",
    "capability_reachability_questions",
    "parse_capability_reachability_payload",
]
