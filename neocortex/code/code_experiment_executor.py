"""Execution and evidence reuse for allow-listed Code experiments.

The executor deliberately supports one runner in v2: exact, source-versioned
runtime scenarios declared in :mod:`code_invariant_contracts`.  It delegates to the
existing trusted-deep provider, which owns canonical-root validation, bounded
pytest execution, coverage collection, output limits, process containment and
durable shard checkpoints.  Canonical change validation attests its registered
scenario subset from that same run's exact per-test relations instead of
launching pytest again for every proposal.  The explicit experiment surface can
still execute one isolated proposal.  Both paths refuse free-form commands,
repository-provided selector text, or product-state mutation.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import os
import sqlite3
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from .code_contracts import deep_configuration_payload, deep_configuration_signature
from .code_experiment_planner import CodeExperimentProposal, experiment_template
from .external_deep_coverage import (
    DEEP_COVERAGE_PROVIDER_SCHEMA,
    PYTEST_COVERAGE_PROVIDER_ID,
)
from .code_external_evidence import ExternalEvidenceFile, read_external_evidence_files
from .code_invariant_contracts import (
    RUNTIME_SCENARIOS,
    runtime_scenario_registry_fingerprint,
)
from .code_schema import readonly_code_database
from .external_evidence_models import (
    ExternalProviderAttestation,
    ExternalProviderRelation,
    ExternalRunInput,
    external_provider_result_digest,
    external_relation_identity,
    external_signature,
)
from .external_evidence_providers import PytestCoverageTrustedDeepProvider
from neocortex.semantic.semantic_models import canonical_json, fingerprint_chunks
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteFileIdentity,
    capture_sqlite_immutable_fence,
)

CODE_EXPERIMENT_RECEIPT_V3_SCHEMA = "neocortex.code-experiment-receipt/v3"
CODE_EXPERIMENT_RECEIPT_SCHEMA = "neocortex.code-experiment-receipt/v4"
CODE_EXPERIMENT_EXECUTION_POLICY = "allowlisted-measured-gates-trusted-deep-v5"
CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY = "allowlisted-measured-gates-trusted-deep-v4"
CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES = 128
_V4_RECEIPT_FIELDS = frozenset(
    {
        "evidence_mode",
        "analysis_run_id",
        "provider_tool_run_id",
        "provider_effective_tool_run_id",
        "provider_tool_status",
        "provider_tool_name",
        "provider_tool_version",
        "provider_portable_publication_id",
        "provider_descriptor_configuration_signature",
        "provider_environment_signature",
        "provider_comparability_signature",
        "provider_suite_selection",
        "provider_suite_signature",
        "provider_measurement_scope_signature",
        "selected_relation_digest",
    }
)
_CODE_DATABASE_MAX_FENCE_BYTES = 64 * 1024 * 1024 * 1024
_CODE_DATABASE_ANCHOR_BYTES = 64 * 1024


def _required(label: str, value: object, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _texts(label: str, values: object, *, sorted_values: bool = False) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required(label, value, 16_384) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if sorted_values and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be sorted")
    return result


@dataclass(frozen=True, slots=True)
class CodeExperimentOutcome:
    scenario_id: str
    test_nodeids: tuple[str, ...]
    outcome: Literal["passed", "failed", "skipped"]
    relation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required("experiment scenario id", self.scenario_id, 256)
        _texts("experiment test nodeid", self.test_nodeids)
        _texts("experiment relation id", self.relation_ids, sorted_values=True)
        if not self.test_nodeids or len(self.relation_ids) != len(self.test_nodeids):
            raise ValueError("experiment outcome requires one receipt per selected nodeid")
        if self.outcome not in {"passed", "failed", "skipped"}:
            raise ValueError("experiment scenario outcome is invalid")
        scenario = next(
            (item for item in RUNTIME_SCENARIOS if item.scenario_id == self.scenario_id),
            None,
        )
        if scenario is None or scenario.test_nodeids != self.test_nodeids:
            raise ValueError("experiment outcome is not bound to its declared scenario")


@dataclass(frozen=True, slots=True)
class CodeExperimentGateOutcome:
    gate_id: str
    scenario_id: str
    test_nodeids: tuple[str, ...]
    status: Literal["passed", "failed", "not_evaluated"]
    relation_ids: tuple[str, ...]
    reason: str
    claim_scope: Literal["exact_test_contract_outcome_not_formal_truth"] = (
        "exact_test_contract_outcome_not_formal_truth"
    )

    def __post_init__(self) -> None:
        _required("experiment gate id", self.gate_id, 256)
        _required("experiment gate scenario id", self.scenario_id, 256)
        _texts("experiment gate test nodeid", self.test_nodeids)
        _texts("experiment gate relation id", self.relation_ids, sorted_values=True)
        _required("experiment gate reason", self.reason, 512)
        if self.status not in {"passed", "failed", "not_evaluated"}:
            raise ValueError("experiment gate status is invalid")
        scenario = next(
            (item for item in RUNTIME_SCENARIOS if item.scenario_id == self.scenario_id),
            None,
        )
        gate = (
            None
            if scenario is None
            else next(
                (item for item in scenario.gate_specs if item.gate_id == self.gate_id),
                None,
            )
        )
        if gate is None or gate.test_nodeids != self.test_nodeids:
            raise ValueError("experiment gate outcome is not bound to its declared contract")
        expected_reason = {
            "passed": "all_bound_test_contracts_passed",
            "failed": "one_or_more_bound_test_contracts_failed",
            "not_evaluated": "bound_test_contracts_not_all_observed_as_terminal_pass_or_fail",
        }[self.status]
        if self.reason != expected_reason:
            raise ValueError("experiment gate reason is not derived from its status")
        if self.status in {"passed", "failed"} and len(self.relation_ids) != len(self.test_nodeids):
            raise ValueError("evaluated experiment gate requires one receipt per nodeid")
        if len(self.relation_ids) > len(self.test_nodeids):
            raise ValueError("experiment gate has excess evidence")
        if self.claim_scope != "exact_test_contract_outcome_not_formal_truth":
            raise ValueError("experiment gate claim scope is invalid")


@dataclass(frozen=True, slots=True)
class CodeExperimentReceipt:
    receipt_id: str
    status: Literal["passed", "failed", "abstained"]
    reason: str | None
    policy_id: str
    proposal_id: str
    template_id: str
    template_version: str
    runner_kind: Literal["trusted_deep_declared_scenarios"]
    source_root: str
    source_version: str
    source_manifest_digest: str
    code_database_digest_before: str
    code_database_digest_after: str
    code_database_unchanged: bool
    configuration_signature: str
    scenario_registry_fingerprint: str
    provider_id: str
    provider_schema: str
    provider_status: str
    provider_execution: str
    provider_input_signature: str
    provider_result_digest: str | None
    selected_scenarios: tuple[str, ...]
    selected_nodeids: tuple[str, ...]
    outcomes: tuple[CodeExperimentOutcome, ...]
    gate_outcomes: tuple[CodeExperimentGateOutcome, ...]
    passed: int
    failed: int
    skipped: int
    duration_ms: int
    process_invocations: int
    stdout_bytes: int
    stderr_bytes: int
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False
    evidence_mode: Literal["primary_trusted_deep_projection"] | None = None
    analysis_run_id: int | None = None
    provider_tool_run_id: int | None = None
    provider_effective_tool_run_id: int | None = None
    provider_tool_status: Literal["completed", "skipped"] | None = None
    provider_tool_name: str | None = None
    provider_tool_version: str | None = None
    provider_portable_publication_id: str | None = None
    provider_descriptor_configuration_signature: str | None = None
    provider_environment_signature: str | None = None
    provider_comparability_signature: str | None = None
    provider_suite_selection: Literal["full", "selected"] | None = None
    provider_suite_signature: str | None = None
    provider_measurement_scope_signature: str | None = None
    selected_relation_digest: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("experiment receipt id", self.receipt_id),
            ("experiment execution policy", self.policy_id),
            ("experiment proposal id", self.proposal_id),
            ("experiment template id", self.template_id),
            ("experiment template version", self.template_version),
            ("experiment source root", self.source_root),
            ("experiment source version", self.source_version),
            ("experiment source manifest digest", self.source_manifest_digest),
            ("experiment Code digest before", self.code_database_digest_before),
            ("experiment Code digest after", self.code_database_digest_after),
            ("experiment configuration signature", self.configuration_signature),
            ("experiment scenario registry", self.scenario_registry_fingerprint),
            ("experiment provider id", self.provider_id),
            ("experiment provider schema", self.provider_schema),
            ("experiment provider status", self.provider_status),
            ("experiment provider execution", self.provider_execution),
            ("experiment provider input signature", self.provider_input_signature),
        ):
            _required(label, value)
        if self.provider_result_digest is not None:
            _required("experiment provider result digest", self.provider_result_digest)
        if self.status not in {"passed", "failed", "abstained"}:
            raise ValueError("experiment receipt status is invalid")
        if self.policy_id not in {
            CODE_EXPERIMENT_EXECUTION_POLICY,
            CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY,
        }:
            raise ValueError("experiment execution policy is invalid")
        provenance_values = (
            self.evidence_mode,
            self.analysis_run_id,
            self.provider_tool_run_id,
            self.provider_effective_tool_run_id,
            self.provider_tool_status,
            self.provider_tool_name,
            self.provider_tool_version,
            self.provider_portable_publication_id,
            self.provider_descriptor_configuration_signature,
            self.provider_environment_signature,
            self.provider_comparability_signature,
            self.provider_suite_selection,
            self.provider_suite_signature,
            self.provider_measurement_scope_signature,
            self.selected_relation_digest,
        )
        if self.policy_id == CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY:
            if any(value is not None for value in provenance_values):
                raise ValueError("v3 experiment receipt cannot carry v4 provenance")
        else:
            if self.evidence_mode != "primary_trusted_deep_projection":
                raise ValueError("attested experiment evidence mode is invalid")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (
                    self.analysis_run_id,
                    self.provider_tool_run_id,
                    self.provider_effective_tool_run_id,
                )
            ):
                raise ValueError("attested experiment run identity is invalid")
            for label, optional_value in (
                ("provider tool name", self.provider_tool_name),
                ("provider tool version", self.provider_tool_version),
                ("provider publication", self.provider_portable_publication_id),
                (
                    "provider descriptor configuration",
                    self.provider_descriptor_configuration_signature,
                ),
                ("provider environment", self.provider_environment_signature),
                ("provider comparability", self.provider_comparability_signature),
                ("provider suite", self.provider_suite_signature),
                ("provider measurement scope", self.provider_measurement_scope_signature),
                ("selected relation digest", self.selected_relation_digest),
            ):
                _required(f"experiment {label}", optional_value)
            if self.provider_tool_status not in {"completed", "skipped"}:
                raise ValueError("attested experiment provider tool status is invalid")
            if self.provider_suite_selection not in {"full", "selected"}:
                raise ValueError("attested experiment suite selection is invalid")
            if (
                self.provider_status != "completed"
                or self.process_invocations != 0
                or self.stdout_bytes != 0
                or self.stderr_bytes != 0
            ):
                raise ValueError("attested experiment must reuse one completed zero-process result")
        if self.runner_kind != "trusted_deep_declared_scenarios":
            raise ValueError("experiment receipt runner is invalid")
        if not isinstance(self.code_database_unchanged, bool):
            raise ValueError("experiment Code-database guard must be boolean")
        if self.code_database_unchanged != (
            self.code_database_digest_before == self.code_database_digest_after
        ):
            raise ValueError("experiment Code-database guard contradicts its digests")
        if (
            self.provider_id != PYTEST_COVERAGE_PROVIDER_ID
            or self.provider_schema != DEEP_COVERAGE_PROVIDER_SCHEMA
        ):
            raise ValueError("experiment receipt provider identity is invalid")
        if self.provider_status not in {
            "completed",
            "failed",
            "timeout",
            "unavailable",
            "skipped",
        }:
            raise ValueError("experiment receipt provider status is invalid")
        if self.provider_execution not in {"full", "cache_replay", "skipped"}:
            raise ValueError("experiment receipt provider execution is invalid")
        _texts("selected experiment scenario", self.selected_scenarios, sorted_values=True)
        _texts("selected experiment nodeid", self.selected_nodeids)
        _texts("experiment limitation", self.limitations)
        if len(self.outcomes) > CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES or any(
            not isinstance(item, CodeExperimentOutcome) for item in self.outcomes
        ):
            raise ValueError("experiment outcomes are invalid or out of bounds")
        if len(self.gate_outcomes) > CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES or any(
            not isinstance(item, CodeExperimentGateOutcome) for item in self.gate_outcomes
        ):
            raise ValueError("experiment gate outcomes are invalid or out of bounds")
        template = experiment_template(self.template_id)
        scenario_map = {item.scenario_id: item.test_nodeids for item in RUNTIME_SCENARIOS}
        if (
            self.template_version != template.version
            or self.runner_kind != template.runner_kind
            or self.selected_scenarios != template.scenario_ids
            or self.selected_nodeids
            != tuple(
                nodeid
                for scenario_id in self.selected_scenarios
                for nodeid in scenario_map[scenario_id]
            )
        ):
            raise ValueError("experiment receipt selection is not derived from its template")
        expected_scenarios = tuple(self.selected_scenarios)
        outcome_scenarios = tuple(item.scenario_id for item in self.outcomes)
        if len(set(outcome_scenarios)) != len(outcome_scenarios) or any(
            scenario_id not in expected_scenarios for scenario_id in outcome_scenarios
        ):
            raise ValueError("experiment outcomes are not a unique subset of the selection")
        if (
            tuple(
                scenario_id
                for scenario_id in expected_scenarios
                if scenario_id in set(outcome_scenarios)
            )
            != outcome_scenarios
        ):
            raise ValueError("experiment outcomes are not in canonical selection order")
        expected_gates = tuple(
            gate.gate_id
            for scenario_id in self.selected_scenarios
            for gate in next(
                item for item in RUNTIME_SCENARIOS if item.scenario_id == scenario_id
            ).gate_specs
        )
        observed_gates = tuple(item.gate_id for item in self.gate_outcomes)
        if (
            len(set(observed_gates)) != len(observed_gates)
            or any(gate_id not in expected_gates for gate_id in observed_gates)
            or tuple(gate_id for gate_id in expected_gates if gate_id in set(observed_gates))
            != observed_gates
        ):
            raise ValueError("experiment gate outcomes are not a canonical subset")
        for label, count in (
            ("passed scenarios", self.passed),
            ("failed scenarios", self.failed),
            ("skipped scenarios", self.skipped),
            ("experiment duration", self.duration_ms),
            ("experiment process invocations", self.process_invocations),
            ("experiment stdout bytes", self.stdout_bytes),
            ("experiment stderr bytes", self.stderr_bytes),
        ):
            _nonnegative(label, count)
        if (self.passed, self.failed, self.skipped) != (
            sum(item.outcome == "passed" for item in self.outcomes),
            sum(item.outcome == "failed" for item in self.outcomes),
            sum(item.outcome == "skipped" for item in self.outcomes),
        ):
            raise ValueError("experiment receipt counts do not match outcomes")
        expected_status = (
            "abstained"
            if self.provider_status != "completed"
            or len(self.outcomes) != len(self.selected_scenarios)
            or len(self.gate_outcomes) != len(expected_gates)
            or any(item.status == "not_evaluated" for item in self.gate_outcomes)
            else "failed"
            if self.failed
            or self.skipped
            or not self.code_database_unchanged
            or any(item.status == "failed" for item in self.gate_outcomes)
            else "passed"
        )
        if self.status != expected_status:
            raise ValueError("experiment receipt status is not derived from execution")
        if self.status == "abstained":
            _required("experiment abstention reason", self.reason, 512)
        elif self.reason is not None:
            raise ValueError("terminal experiment receipt cannot carry an abstention reason")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("experiment receipt must remain advisory and non-mutating")
        expected_id = _receipt_identity(self)
        if self.receipt_id != expected_id:
            raise ValueError("experiment receipt identity is invalid")

    def as_payload(self) -> dict[str, object]:
        values = asdict(self)
        if self.policy_id == CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY:
            for field_name in _V4_RECEIPT_FIELDS:
                values.pop(field_name)
            schema = CODE_EXPERIMENT_RECEIPT_V3_SCHEMA
        else:
            schema = CODE_EXPERIMENT_RECEIPT_SCHEMA
        return {"schema": schema, **values}


def _receipt_identity(receipt: CodeExperimentReceipt) -> str:
    from .code_analysis_epistemics import analysis_identity

    values = dict(receipt.as_payload())
    values.pop("schema")
    values.pop("receipt_id")
    values["duration_ms"] = 0
    return analysis_identity(
        (
            "code-experiment-receipt-v3"
            if receipt.policy_id == CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY
            else "code-experiment-receipt-v4"
        ),
        values,
    )


def _receipt_identity_values(values: Mapping[str, object]) -> dict[str, object]:
    result = dict(values)
    result["duration_ms"] = 0
    outcomes = result.get("outcomes")
    if isinstance(outcomes, tuple):
        result["outcomes"] = tuple(
            asdict(item) if isinstance(item, CodeExperimentOutcome) else item for item in outcomes
        )
    gate_outcomes = result.get("gate_outcomes")
    if isinstance(gate_outcomes, tuple):
        result["gate_outcomes"] = tuple(
            asdict(item) if isinstance(item, CodeExperimentGateOutcome) else item
            for item in gate_outcomes
        )
    return result


def _identity_matches_stat(identity: SQLiteFileIdentity, observed: os.stat_result) -> bool:
    return (
        identity.device == int(observed.st_dev)
        and identity.inode == int(observed.st_ino)
        and identity.mode == int(observed.st_mode)
        and identity.size == int(observed.st_size)
        and identity.mtime_ns == int(observed.st_mtime_ns)
        and identity.ctime_ns == int(observed.st_ctime_ns)
    )


def _file_digest(path: Path) -> str:
    """Digest a fixed-cost Linux identity fence plus bounded content anchors.

    The experiment only needs to prove that its canonical Code owner did not
    change while tests ran. Hashing the entire multi-gigabyte historical store
    twice per proposal made the guard scale with accumulated history and then
    rejected the live store at 4 GiB. Device/inode/size/mtime/ctime and inactive
    sidecars detect any ordinary SQLite write; fixed head/tail anchors retain a
    bounded content check without reading gigabytes.
    """

    selected = Path(path)
    try:
        before = capture_sqlite_immutable_fence(selected)
    except (OSError, ImmutableSQLiteUnavailable) as exc:
        raise ValueError("experiment Code database cannot be fenced") from exc
    size = before.main.size
    if size <= 0 or size > _CODE_DATABASE_MAX_FENCE_BYTES:
        raise ValueError("experiment Code database is empty or exceeds its fence bound")
    try:
        with selected.open("rb", buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            if not _identity_matches_stat(before.main, opened):
                raise ValueError("experiment Code database changed before fence read")
            head = stream.read(min(size, _CODE_DATABASE_ANCHOR_BYTES))
            tail = b""
            if size > _CODE_DATABASE_ANCHOR_BYTES:
                stream.seek(max(0, size - _CODE_DATABASE_ANCHOR_BYTES))
                tail = stream.read(_CODE_DATABASE_ANCHOR_BYTES)
            closed = os.fstat(stream.fileno())
            if not _identity_matches_stat(before.main, closed):
                raise ValueError("experiment Code database changed during fence read")
        after = capture_sqlite_immutable_fence(selected)
    except (OSError, ImmutableSQLiteUnavailable) as exc:
        raise ValueError("experiment Code database changed during fence read") from exc
    if before != after:
        raise ValueError("experiment Code database changed during fence read")
    descriptor = canonical_json(
        {
            "schema": "neocortex.code-database-identity-fence/v1",
            "main": asdict(before.main),
            "sidecars": [
                {"suffix": suffix, "identity": asdict(identity)}
                for suffix, identity in before.sidecars
            ],
            "anchor_bytes": _CODE_DATABASE_ANCHOR_BYTES,
        }
    ).encode("utf-8")
    observed = fingerprint_chunks((descriptor, head, tail))
    return (
        "neocortex.code-database-identity-fence/v1:"
        f"xxh3_128:{observed.xxh3_128}:xxh3_64:{observed.xxh3_64_guard}"
    )


def _manifest_digest(files: tuple[ExternalEvidenceFile, ...]) -> str:
    from .external_evidence_models import external_signature

    return external_signature(
        "code-experiment-source-manifest-v1",
        {"files": [item.signature_payload() for item in files]},
    )


def _outcomes(
    publication, selected_scenarios: tuple[str, ...]
) -> tuple[CodeExperimentOutcome, ...]:
    scenario_by_nodeid = {
        nodeid: item for item in RUNTIME_SCENARIOS for nodeid in item.test_nodeids
    }
    selected = set(selected_scenarios)
    relations_by_scenario: dict[str, list[tuple[str, str, str]]] = {}
    for relation in publication.relations:
        if relation.relation_kind != "declared_test_outcome":
            continue
        nodeid = relation.metadata.get("nodeid")
        outcome = relation.metadata.get("outcome")
        scenario = scenario_by_nodeid.get(str(nodeid))
        if (
            scenario is None
            or scenario.scenario_id not in selected
            or outcome
            not in {
                "passed",
                "failed",
                "skipped",
            }
        ):
            continue
        relations_by_scenario.setdefault(scenario.scenario_id, []).append(
            (str(nodeid), str(outcome), relation.portable_relation_id)
        )
    result: list[CodeExperimentOutcome] = []
    scenarios = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    for scenario_id in selected_scenarios:
        scenario = scenarios[scenario_id]
        receipts = tuple(
            sorted(relations_by_scenario.get(scenario_id, ()), key=lambda item: item[0])
        )
        if not receipts:
            continue
        observed_nodeids = tuple(item[0] for item in receipts)
        if observed_nodeids != scenario.test_nodeids:
            continue
        observed_outcomes = tuple(item[1] for item in receipts)
        aggregate = (
            "failed"
            if "failed" in observed_outcomes
            else "skipped"
            if "skipped" in observed_outcomes
            else "passed"
        )
        result.append(
            CodeExperimentOutcome(
                scenario_id,
                scenario.test_nodeids,
                cast(Any, aggregate),
                tuple(sorted(item[2] for item in receipts)),
            )
        )
    return tuple(result)


def _gate_outcomes(
    publication,
    selected_scenarios: tuple[str, ...],
) -> tuple[CodeExperimentGateOutcome, ...]:
    observed: dict[str, list[tuple[str, str]]] = {}
    selected_nodeids = {
        nodeid
        for scenario in RUNTIME_SCENARIOS
        if scenario.scenario_id in set(selected_scenarios)
        for nodeid in scenario.test_nodeids
    }
    for relation in publication.relations:
        if relation.relation_kind != "declared_test_outcome":
            continue
        nodeid = str(relation.metadata.get("nodeid"))
        outcome = str(relation.metadata.get("outcome"))
        if nodeid not in selected_nodeids or outcome not in {"passed", "failed", "skipped"}:
            continue
        observed.setdefault(nodeid, []).append((outcome, relation.portable_relation_id))
    scenario_by_id = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    results: list[CodeExperimentGateOutcome] = []
    for scenario_id in selected_scenarios:
        scenario = scenario_by_id[scenario_id]
        for gate in scenario.gate_specs:
            rows = tuple(observed.get(nodeid, ()) for nodeid in gate.test_nodeids)
            complete = all(len(items) == 1 for items in rows)
            outcomes = tuple(items[0][0] for items in rows if len(items) == 1)
            status: Literal["passed", "failed", "not_evaluated"] = (
                "not_evaluated"
                if not complete or "skipped" in outcomes
                else "failed"
                if "failed" in outcomes
                else "passed"
            )
            reason = {
                "passed": "all_bound_test_contracts_passed",
                "failed": "one_or_more_bound_test_contracts_failed",
                "not_evaluated": "bound_test_contracts_not_all_observed_as_terminal_pass_or_fail",
            }[status]
            results.append(
                CodeExperimentGateOutcome(
                    gate_id=gate.gate_id,
                    scenario_id=scenario_id,
                    test_nodeids=gate.test_nodeids,
                    status=status,
                    relation_ids=tuple(sorted(item[0][1] for item in rows if len(item) == 1)),
                    reason=reason,
                )
            )
    return tuple(results)


def _provider_test_counts(publication) -> tuple[int, int, int, int]:
    """Recover bounded run counts when no complete per-test receipt exists."""

    counters: dict[str, int] = {}
    for metric in publication.metrics:
        if metric.subject_kind != "run" or metric.metric_name not in {
            "tests_selected",
            "tests_passed",
            "tests_failed",
            "tests_skipped",
        }:
            continue
        value = metric.value
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError("experiment provider test count is invalid")
        integer = int(value)
        if float(value) != float(integer) or metric.metric_name in counters:
            raise ValueError("experiment provider test count is not canonical")
        counters[metric.metric_name] = integer
    if set(counters) != {
        "tests_selected",
        "tests_passed",
        "tests_failed",
        "tests_skipped",
    }:
        return 0, 0, 0, 0
    return (
        counters["tests_selected"],
        counters["tests_passed"],
        counters["tests_failed"],
        counters["tests_skipped"],
    )


def _require_stable_source_input(
    before: str,
    published: str,
    after: str,
) -> None:
    """Reject test outcomes that cannot be tied to one exact source tree."""

    if not before or published != before or after != before:
        raise ValueError("experiment source input changed during execution")


def _validated_attestation_roots(
    source_root: Path,
    code_database_path: Path,
) -> tuple[Path, Path]:
    source = Path(source_root).resolve(strict=True)
    database = Path(code_database_path).resolve(strict=True)
    if not source.is_dir() or not database.is_file():
        raise ValueError("experiment attestation roots are invalid")
    return source, database


def _require_current_analysis_run(
    connection: sqlite3.Connection,
    *,
    analysis_run_id: int,
    source_version: str,
) -> None:
    if (
        isinstance(analysis_run_id, bool)
        or not isinstance(analysis_run_id, int)
        or analysis_run_id < 1
    ):
        raise ValueError("experiment attestation analysis run is invalid")
    run = connection.execute(
        "SELECT status,processing_signature FROM analysis_runs WHERE analysis_run_id=?",
        (analysis_run_id,),
    ).fetchone()
    latest = connection.execute("SELECT MAX(analysis_run_id) FROM analysis_runs").fetchone()
    if run is None or str(run["status"]) != "completed":
        raise ValueError("experiment attestation source run is not completed")
    if str(run["processing_signature"]) != source_version:
        raise ValueError("experiment attestation source signature changed")
    if latest is None or int(latest[0]) != analysis_run_id:
        raise ValueError("experiment attestation source run is not latest")


def _validate_attestation_inputs(
    files: tuple[ExternalEvidenceFile, ...],
    attestation: ExternalProviderAttestation,
) -> None:
    expected = {
        item.portable_input_id: item
        for item in (ExternalRunInput.from_file(file, covered=True) for file in files)
    }
    observed = {item.portable_input_id: item for item in attestation.inputs}
    if len(expected) != len(files) or len(observed) != len(attestation.inputs):
        raise ValueError("experiment trusted-deep input identities are not unique")
    if expected.keys() != observed.keys():
        raise ValueError("experiment trusted-deep inputs do not match the current manifest")
    for portable_id, expected_item in expected.items():
        observed_item = observed[portable_id]
        if (
            observed_item.version_id,
            observed_item.relative_path,
            observed_item.eligible,
            observed_item.covered,
            observed_item.size,
            observed_item.content_digest,
        ) != (
            expected_item.version_id,
            expected_item.relative_path,
            True,
            True,
            expected_item.size,
            expected_item.content_digest,
        ):
            raise ValueError("experiment trusted-deep input contract changed")


def validate_code_experiment_declared_test_relations(
    attestation: ExternalProviderAttestation,
    *,
    suite_selection: Literal["full", "selected"],
    configuration_signature: str,
    suite_signature: str,
    measurement_scope_signature: str,
) -> tuple[ExternalProviderRelation, ...]:
    expected_context: dict[str, object] = {
        "suite_selection": suite_selection,
        "measurement_complete": True,
        "content_executed": True,
        "suite_signature": suite_signature,
        "configuration_signature": configuration_signature,
        "publication_input_signature": attestation.input_signature,
        "measurement_scope_signature": measurement_scope_signature,
        "claim_scope": "exact_selected_test_execution_outcome",
        "assertion_or_invariant_proof": False,
    }
    expected_metadata_fields = frozenset(
        {
            *expected_context,
            "tool_versions",
            "code_input_signature",
            "support_signature",
            "subprocess_coverage",
            "coverage_scope",
            "nodeid",
            "outcome",
        }
    )
    declared: list[ExternalProviderRelation] = []
    nodeids: set[str] = set()
    outcomes: dict[str, int] = {"passed": 0, "failed": 0, "skipped": 0}
    run_key = f"coverage-run:{measurement_scope_signature}"
    common_contract: tuple[tuple[tuple[str, str], ...], str, str] | None = None
    for relation in attestation.relations:
        if relation.relation_kind != "declared_test_outcome":
            continue
        nodeid = relation.metadata.get("nodeid")
        outcome = relation.metadata.get("outcome")
        if not isinstance(nodeid, str) or not nodeid or nodeid in nodeids:
            raise ValueError("experiment declared test outcome identity is invalid")
        if not isinstance(outcome, str) or outcome not in outcomes:
            raise ValueError("experiment declared test outcome is invalid")
        expected_id = external_relation_identity(
            attestation.provider_id,
            relation_kind="declared_test_outcome",
            source_kind="contract",
            source_key=f"pytest-nodeid:{nodeid}",
            target_kind="run",
            target_key=run_key,
        )
        local_ids = (
            relation.source_version_id,
            relation.source_symbol_id,
            relation.source_project_id,
            relation.target_version_id,
            relation.target_symbol_id,
            relation.target_project_id,
        )
        tool_versions = relation.metadata.get("tool_versions")
        if not isinstance(tool_versions, Mapping) or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in tool_versions.items()
        ):
            raise ValueError("experiment declared test outcome contract changed")
        relation_common = (
            tuple(sorted(cast(Mapping[str, str], tool_versions).items())),
            _required(
                "coverage code input signature", relation.metadata.get("code_input_signature")
            ),
            _required("coverage support signature", relation.metadata.get("support_signature")),
        )
        if common_contract is None:
            common_contract = relation_common
        if (
            relation.portable_relation_id != expected_id
            or relation.source_kind != "contract"
            or relation.source_key != f"pytest-nodeid:{nodeid}"
            or relation.target_kind != "run"
            or relation.target_key != run_key
            or not relation.directed
            or relation.confidence != 1.0
            or any(value is not None for value in local_ids)
            or set(relation.metadata) != expected_metadata_fields
            or any(relation.metadata.get(key) != value for key, value in expected_context.items())
            or relation.metadata.get("subprocess_coverage") is not False
            or relation.metadata.get("coverage_scope") != "main_process_only"
            or relation_common != common_contract
        ):
            raise ValueError("experiment declared test outcome contract changed")
        nodeids.add(nodeid)
        outcomes[outcome] += 1
        declared.append(relation)
    selected, passed, failed, skipped = _provider_test_counts(attestation)
    if (selected, passed, failed, skipped) != (
        len(declared),
        outcomes["passed"],
        outcomes["failed"],
        outcomes["skipped"],
    ):
        raise ValueError("experiment declared outcomes disagree with provider counts")
    return tuple(sorted(declared, key=lambda item: item.portable_relation_id))


def code_experiment_selected_relation_digest(
    relations: Sequence[ExternalProviderRelation],
    selected_nodeids: Sequence[str],
) -> str:
    selected = set(selected_nodeids)
    matching = tuple(
        relation for relation in relations if relation.metadata.get("nodeid") in selected
    )
    return external_signature(
        "code-experiment-selected-relations-v1",
        {
            "selected_nodeids": list(selected_nodeids),
            "complete": len(matching) == len(selected),
            "relations": [item.digest_payload() for item in matching],
        },
    )


def attest_code_experiments(
    proposals: Sequence[CodeExperimentProposal],
    *,
    source_root: Path,
    code_database_path: Path,
    source_version: str,
    analysis_run_id: int,
    provider_tool_run_id: int,
    provider_effective_tool_run_id: int,
    provider_suite_selection: Literal["full", "selected"],
    provider_configuration_signature: str,
    provider_suite_signature: str,
    provider_measurement_scope_signature: str,
) -> tuple[CodeExperimentReceipt, ...]:
    """Attest registered scenarios from one exact current full-suite publication.

    The trusted-deep producer has already executed the canonical selected suite
    before this boundary.  Re-running overlapping nodeids per proposal adds no
    independent evidence, so this function reads the normalized
    ``declared_test_outcome`` relations once, binds each requested subset to its
    proposal and emits zero-process receipts.  Missing, duplicate, stale or
    non-current evidence fails closed.
    """

    selected_proposals = tuple(proposals)
    if not selected_proposals or len(selected_proposals) > CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES:
        raise ValueError("experiment attestation proposal selection is empty or out of bounds")
    if len({item.proposal_id for item in selected_proposals}) != len(selected_proposals):
        raise ValueError("experiment attestation proposals cannot repeat")
    templates = []
    for proposal in selected_proposals:
        if (
            not isinstance(proposal, CodeExperimentProposal)
            or proposal.planning_status != "planned"
        ):
            raise ValueError("experiment attestation requires planned proposals")
        template = experiment_template(_required("proposal template id", proposal.template_id))
        if proposal.runner_kind != "trusted_deep_declared_scenarios" or not template.executable:
            raise ValueError("experiment proposal has no attestable allow-listed runner")
        templates.append(template)

    source, database = _validated_attestation_roots(source_root, code_database_path)
    selected_source_version = _required("experiment source version", source_version)
    before = _file_digest(database)
    from .external_evidence_models import external_root_identity
    from .external_evidence_store import read_external_provider_attestation

    with readonly_code_database(database) as connection:
        _require_current_analysis_run(
            connection,
            analysis_run_id=analysis_run_id,
            source_version=selected_source_version,
        )
        files = read_external_evidence_files(connection, source)
        attestation = read_external_provider_attestation(
            connection,
            analysis_run_id=analysis_run_id,
            tool_run_id=provider_tool_run_id,
            expected_processing_signature=selected_source_version,
            expected_provider_id=PYTEST_COVERAGE_PROVIDER_ID,
            expected_provider_schema=DEEP_COVERAGE_PROVIDER_SCHEMA,
            enforce_current_runtime=True,
        )
    after = _file_digest(database)
    if not files:
        raise ValueError("experiment source manifest contains no current Python inputs")
    if (
        Path(attestation.observed_root).resolve(strict=True) != source
        or attestation.root_identity != external_root_identity(source)
        or attestation.provider_id != PYTEST_COVERAGE_PROVIDER_ID
        or attestation.provider_schema != DEEP_COVERAGE_PROVIDER_SCHEMA
        or attestation.profile != "trusted-deep"
        or attestation.analysis_run_id != analysis_run_id
        or attestation.processing_signature != selected_source_version
        or attestation.tool_run_id != provider_tool_run_id
        or attestation.effective_tool_run_id != provider_effective_tool_run_id
        or not attestation.coverage_complete
        or not attestation.content_executed
        or attestation.covered_files != attestation.eligible_files
    ):
        raise ValueError("experiment trusted-deep attestation contract is incomplete")
    _validate_attestation_inputs(files, attestation)
    provider_result = external_provider_result_digest(
        attestation.findings,
        attestation.metrics,
        attestation.relations,
    )
    if provider_result != attestation.result_digest:
        raise ValueError("experiment trusted-deep attestation digest changed")
    declared_relations = validate_code_experiment_declared_test_relations(
        attestation,
        suite_selection=provider_suite_selection,
        configuration_signature=_required(
            "provider deep configuration signature",
            provider_configuration_signature,
        ),
        suite_signature=_required("provider suite signature", provider_suite_signature),
        measurement_scope_signature=_required(
            "provider measurement scope signature",
            provider_measurement_scope_signature,
        ),
    )
    validated_attestation = replace(attestation, relations=declared_relations)

    manifest_digest = _manifest_digest(files)
    scenario_map = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    started = time.monotonic_ns()
    receipts: list[CodeExperimentReceipt] = []
    from .code_analysis_epistemics import analysis_identity

    for proposal, template in zip(selected_proposals, templates, strict=True):
        selected_scenarios = template.scenario_ids
        selected_nodeids = tuple(
            nodeid
            for scenario_id in selected_scenarios
            for nodeid in scenario_map[scenario_id].test_nodeids
        )
        outcomes = _outcomes(validated_attestation, selected_scenarios)
        gate_outcomes = _gate_outcomes(validated_attestation, selected_scenarios)
        status: Literal["passed", "failed", "abstained"] = (
            "abstained"
            if len(outcomes) != len(selected_scenarios)
            or any(item.status == "not_evaluated" for item in gate_outcomes)
            else "failed"
            if any(item.outcome != "passed" for item in outcomes)
            or any(item.status == "failed" for item in gate_outcomes)
            or before != after
            else "passed"
        )
        values: dict[str, object] = {
            "status": status,
            "reason": ("published_coverage_outcomes_incomplete" if status == "abstained" else None),
            "policy_id": CODE_EXPERIMENT_EXECUTION_POLICY,
            "proposal_id": proposal.proposal_id,
            "template_id": template.template_id,
            "template_version": template.version,
            "runner_kind": "trusted_deep_declared_scenarios",
            "source_root": os.fspath(source),
            "source_version": selected_source_version,
            "source_manifest_digest": manifest_digest,
            "code_database_digest_before": before,
            "code_database_digest_after": after,
            "code_database_unchanged": before == after,
            "configuration_signature": provider_configuration_signature,
            "scenario_registry_fingerprint": runtime_scenario_registry_fingerprint(),
            "provider_id": attestation.provider_id,
            "provider_schema": attestation.provider_schema,
            "provider_status": "completed",
            "provider_execution": attestation.execution,
            "provider_input_signature": attestation.input_signature,
            "provider_result_digest": attestation.result_digest,
            "selected_scenarios": selected_scenarios,
            "selected_nodeids": selected_nodeids,
            "outcomes": outcomes,
            "gate_outcomes": gate_outcomes,
            "passed": sum(item.outcome == "passed" for item in outcomes),
            "failed": sum(item.outcome == "failed" for item in outcomes),
            "skipped": sum(item.outcome == "skipped" for item in outcomes),
            "duration_ms": max(0, (time.monotonic_ns() - started) // 1_000_000),
            "process_invocations": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "limitations": (
                "receipt_proves_selected_test_outcomes_not_a_question_conclusion_or_formal_proof",
                "exact_current_trusted_deep_test_relations_reused_without_test_reexecution",
                "provider_result_covers_the_validation_suite_not_only_this_scenario_subset",
                "coverage_is_main_process_only",
                "code_database_unchanged_uses_identity_sidecar_fence_and_bounded_content_anchors",
                "source_input_is_verified_by_the_current_published_manifest_and_final_git_fence",
                "process_death_scenario_is_not_power_loss",
                "no_product_mutation_authority",
            ),
            "authority": "advisory",
            "mutation_authority": False,
            "evidence_mode": "primary_trusted_deep_projection",
            "analysis_run_id": analysis_run_id,
            "provider_tool_run_id": attestation.tool_run_id,
            "provider_effective_tool_run_id": attestation.effective_tool_run_id,
            "provider_tool_status": attestation.tool_status,
            "provider_tool_name": attestation.tool_name,
            "provider_tool_version": attestation.tool_version,
            "provider_portable_publication_id": attestation.portable_publication_id,
            "provider_descriptor_configuration_signature": (
                attestation.descriptor_configuration_signature
            ),
            "provider_environment_signature": attestation.environment_signature,
            "provider_comparability_signature": attestation.comparability_signature,
            "provider_suite_selection": provider_suite_selection,
            "provider_suite_signature": provider_suite_signature,
            "provider_measurement_scope_signature": provider_measurement_scope_signature,
            "selected_relation_digest": code_experiment_selected_relation_digest(
                declared_relations,
                selected_nodeids,
            ),
        }
        identity_values = _receipt_identity_values(values)
        receipts.append(
            CodeExperimentReceipt(
                receipt_id=analysis_identity("code-experiment-receipt-v4", identity_values),
                **values,  # type: ignore[arg-type]
            )
        )
    return tuple(receipts)


def execute_code_experiment(
    proposal: CodeExperimentProposal,
    *,
    source_root: Path,
    code_database_path: Path,
    scratch_root: Path,
    source_version: str,
    expected_source_root: Path | None = None,
) -> CodeExperimentReceipt:
    """Execute exactly one registered proposal through its allow-listed runner."""

    if not isinstance(proposal, CodeExperimentProposal) or proposal.planning_status != "planned":
        raise ValueError("experiment execution requires a planned proposal")
    template = experiment_template(_required("proposal template id", proposal.template_id))
    if proposal.runner_kind != "trusted_deep_declared_scenarios" or not template.executable:
        raise ValueError("experiment proposal has no executable allow-listed runner")
    source = Path(source_root).resolve(strict=True)
    database = Path(code_database_path).resolve(strict=True)
    scratch = Path(scratch_root).resolve(strict=True)
    if not source.is_dir() or not scratch.is_dir() or not database.is_file():
        raise ValueError("experiment execution roots are invalid")
    if expected_source_root is not None:
        expected = Path(expected_source_root).resolve(strict=True)
        if source != expected:
            raise ValueError("experiment source root does not match the published manifest root")
    source_normalized = os.path.normcase(os.path.abspath(source))
    scratch_normalized = os.path.normcase(os.path.abspath(scratch))
    if os.path.commonpath((source_normalized, scratch_normalized)) == source_normalized:
        raise ValueError("experiment scratch cannot be inside the source repository")
    if os.path.commonpath((scratch_normalized, os.path.normcase(os.path.abspath(database)))) == (
        scratch_normalized
    ):
        raise ValueError("experiment scratch cannot own the canonical Code database")
    before = _file_digest(database)
    with readonly_code_database(database) as connection:
        files = read_external_evidence_files(connection, source)
    if not files:
        raise ValueError("experiment source manifest contains no current Python inputs")
    manifest_digest = _manifest_digest(files)
    scenario_map = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    selected_scenarios = template.scenario_ids
    selected_nodeids = tuple(
        nodeid for item in selected_scenarios for nodeid in scenario_map[item].test_nodeids
    )
    payload = deep_configuration_payload(
        analysis_profile="trusted-deep",
        test_selectors=selected_nodeids,
        max_tests=len(selected_nodeids),
        time_budget_seconds=template.timeout_seconds,
        shard_size=min(len(selected_nodeids), 50),
    )
    signature = deep_configuration_signature(payload)
    provider = PytestCoverageTrustedDeepProvider(source, payload, signature)
    source_input_signature = provider.baseline_input_signature(files)
    started = time.monotonic_ns()
    publication = provider.run(source, files, baseline=None, scratch_root=scratch)
    duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    post_provider = PytestCoverageTrustedDeepProvider(source, payload, signature)
    post_source_input_signature = post_provider.baseline_input_signature(files)
    _require_stable_source_input(
        source_input_signature,
        publication.input_signature,
        post_source_input_signature,
    )
    after = _file_digest(database)
    outcomes = _outcomes(publication, selected_scenarios)
    gate_outcomes = _gate_outcomes(publication, selected_scenarios)
    tests_selected, tests_passed, tests_failed, tests_skipped = _provider_test_counts(publication)
    complete_aggregate_pass = (
        not outcomes
        and tests_selected == len(selected_nodeids)
        and tests_passed == len(selected_nodeids)
        and tests_failed == 0
        and tests_skipped == 0
        and publication.coverage_complete
    )
    if complete_aggregate_pass:
        from .code_analysis_epistemics import analysis_identity

        outcomes = tuple(
            CodeExperimentOutcome(
                scenario_id,
                scenario_map[scenario_id].test_nodeids,
                "passed",
                tuple(
                    analysis_identity(
                        "code-experiment-aggregate-outcome-v1",
                        {
                            "provider_publication": publication.portable_publication_id,
                            "provider_result": publication.result_digest,
                            "scenario_id": scenario_id,
                            "nodeid": nodeid,
                            "selected": tests_selected,
                            "passed": tests_passed,
                        },
                    )
                    for nodeid in scenario_map[scenario_id].test_nodeids
                ),
            )
            for scenario_id in selected_scenarios
        )
    provider_status = publication.status
    status: Literal["passed", "failed", "abstained"] = (
        "abstained"
        if provider_status != "completed"
        or len(outcomes) != len(selected_scenarios)
        or any(item.status == "not_evaluated" for item in gate_outcomes)
        else "failed"
        if any(item.outcome != "passed" for item in outcomes)
        or any(item.status == "failed" for item in gate_outcomes)
        or before != after
        else "passed"
    )
    reason = (
        None
        if status != "abstained"
        else str(
            publication.publication.provenance.get("reason")
            or (
                "acceptance_gate_evidence_incomplete"
                if any(item.status == "not_evaluated" for item in gate_outcomes)
                else "provider_did_not_publish_complete_outcomes"
            )
        )[:512]
    )
    provider_result = (
        external_provider_result_digest(
            publication.findings,
            publication.metrics,
            publication.relations,
        )
        if provider_status == "completed"
        else publication.result_digest
    )
    values: dict[str, object] = {
        "status": status,
        "reason": reason,
        "policy_id": CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY,
        "proposal_id": proposal.proposal_id,
        "template_id": template.template_id,
        "template_version": template.version,
        "runner_kind": "trusted_deep_declared_scenarios",
        "source_root": os.fspath(source),
        "source_version": _required("experiment source version", source_version),
        "source_manifest_digest": manifest_digest,
        "code_database_digest_before": before,
        "code_database_digest_after": after,
        "code_database_unchanged": before == after,
        "configuration_signature": signature,
        "scenario_registry_fingerprint": runtime_scenario_registry_fingerprint(),
        "provider_id": publication.descriptor.provider_id,
        "provider_schema": publication.descriptor.provider_schema,
        "provider_status": provider_status,
        "provider_execution": publication.execution,
        "provider_input_signature": publication.input_signature,
        "provider_result_digest": provider_result,
        "selected_scenarios": selected_scenarios,
        "selected_nodeids": selected_nodeids,
        "outcomes": outcomes,
        "gate_outcomes": gate_outcomes,
        "passed": sum(item.outcome == "passed" for item in outcomes),
        "failed": sum(item.outcome == "failed" for item in outcomes),
        "skipped": sum(item.outcome == "skipped" for item in outcomes),
        "duration_ms": duration_ms,
        "process_invocations": publication.counters.get("process_invocations", 0),
        "stdout_bytes": publication.counters.get("stdout_bytes", 0),
        "stderr_bytes": publication.counters.get("stderr_bytes", 0),
        "limitations": (
            "receipt_proves_selected_test_outcomes_not_a_question_conclusion_or_formal_proof",
            *(
                ("per_test_outcome_relations_unavailable_aggregate_counts_only",)
                if complete_aggregate_pass
                else ()
            ),
            "coverage_is_main_process_only",
            "code_database_unchanged_uses_identity_sidecar_fence_and_bounded_content_anchors",
            "source_input_is_verified_before_and_after_but_corpus_and_other_state_are_not_guarded",
            "process_death_scenario_is_not_power_loss",
            "no_product_mutation_authority",
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    from .code_analysis_epistemics import analysis_identity

    identity_values = _receipt_identity_values(values)
    return CodeExperimentReceipt(
        receipt_id=analysis_identity("code-experiment-receipt-v3", identity_values),
        **values,  # type: ignore[arg-type]
    )


def parse_code_experiment_receipt_payload(
    payload: Mapping[str, object],
) -> CodeExperimentReceipt:
    if not isinstance(payload, Mapping) or payload.get("schema") not in {
        CODE_EXPERIMENT_RECEIPT_V3_SCHEMA,
        CODE_EXPERIMENT_RECEIPT_SCHEMA,
    }:
        raise ValueError("experiment receipt payload schema is invalid")
    schema = cast(str, payload["schema"])
    expected = {field.name for field in fields(CodeExperimentReceipt)} | {"schema"}
    if schema == CODE_EXPERIMENT_RECEIPT_V3_SCHEMA:
        expected -= _V4_RECEIPT_FIELDS
    if set(payload) != expected:
        raise ValueError("experiment receipt payload fields are invalid")
    raw_outcomes = payload.get("outcomes")
    if not isinstance(raw_outcomes, Sequence) or isinstance(raw_outcomes, (str, bytes, bytearray)):
        raise ValueError("experiment receipt outcomes are invalid")
    outcome_fields = {field.name for field in fields(CodeExperimentOutcome)}
    parsed_outcomes: list[CodeExperimentOutcome] = []
    for item in raw_outcomes:
        if not isinstance(item, Mapping) or set(item) != outcome_fields:
            raise ValueError("experiment receipt outcome fields are invalid")
        values = dict(item)
        values["test_nodeids"] = _texts("experiment test nodeid", values["test_nodeids"])
        values["relation_ids"] = _texts(
            "experiment relation id", values["relation_ids"], sorted_values=True
        )
        parsed_outcomes.append(CodeExperimentOutcome(**cast(Any, values)))
    outcomes = tuple(parsed_outcomes)
    raw_gate_outcomes = payload.get("gate_outcomes")
    if not isinstance(raw_gate_outcomes, Sequence) or isinstance(
        raw_gate_outcomes, (str, bytes, bytearray)
    ):
        raise ValueError("experiment gate outcomes are invalid")
    gate_fields = {field.name for field in fields(CodeExperimentGateOutcome)}
    parsed_gates: list[CodeExperimentGateOutcome] = []
    for item in raw_gate_outcomes:
        if not isinstance(item, Mapping) or set(item) != gate_fields:
            raise ValueError("experiment gate outcome fields are invalid")
        gate_values = dict(item)
        gate_values["test_nodeids"] = _texts(
            "experiment gate test nodeid", gate_values["test_nodeids"]
        )
        gate_values["relation_ids"] = _texts(
            "experiment gate relation id", gate_values["relation_ids"], sorted_values=True
        )
        parsed_gates.append(CodeExperimentGateOutcome(**cast(Any, gate_values)))
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["outcomes"] = outcomes
    values["gate_outcomes"] = tuple(parsed_gates)
    values["selected_scenarios"] = _texts(
        "selected experiment scenario", values["selected_scenarios"], sorted_values=True
    )
    values["selected_nodeids"] = _texts("selected experiment nodeid", values["selected_nodeids"])
    values["limitations"] = _texts("experiment receipt limitation", values["limitations"])
    expected_policy = (
        CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY
        if schema == CODE_EXPERIMENT_RECEIPT_V3_SCHEMA
        else CODE_EXPERIMENT_EXECUTION_POLICY
    )
    if values.get("policy_id") != expected_policy:
        raise ValueError("experiment receipt schema and policy disagree")
    return CodeExperimentReceipt(**cast(Any, values))


__all__ = [
    "CODE_EXPERIMENT_DIRECT_EXECUTION_POLICY",
    "CODE_EXPERIMENT_EXECUTION_POLICY",
    "CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES",
    "CODE_EXPERIMENT_RECEIPT_SCHEMA",
    "CODE_EXPERIMENT_RECEIPT_V3_SCHEMA",
    "CodeExperimentGateOutcome",
    "CodeExperimentOutcome",
    "CodeExperimentReceipt",
    "attest_code_experiments",
    "code_experiment_selected_relation_digest",
    "execute_code_experiment",
    "parse_code_experiment_receipt_payload",
    "validate_code_experiment_declared_test_relations",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_experiment_executor")
