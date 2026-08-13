"""Isolated execution and receipts for allow-listed Code experiments.

The executor deliberately supports one runner in v1: the exact invariant
scenarios declared in :mod:`code_invariant_contracts`.  It delegates to the
existing trusted-deep provider, which owns canonical-root validation, bounded
pytest execution, coverage collection, output limits, process containment and
durable shard checkpoints.  This adapter adds an experiment-level receipt and
refuses free-form commands, repository-provided selector text, or product-state
mutation.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from .code_contracts import deep_configuration_payload, deep_configuration_signature
from .code_experiment_planner import CodeExperimentProposal, experiment_template
from .code_external_evidence import ExternalEvidenceFile, read_external_evidence_files
from .code_invariant_contracts import RUNTIME_SCENARIOS, invariant_registry_fingerprint
from .code_schema import readonly_code_database
from .external_evidence_models import external_provider_result_digest
from .external_evidence_providers import PytestCoverageTrustedDeepProvider
from .semantic_models import fingerprint_bytes

CODE_EXPERIMENT_RECEIPT_SCHEMA = "neocortex.code-experiment-receipt/v1"
CODE_EXPERIMENT_EXECUTION_POLICY = "allowlisted-trusted-deep-scenarios-v1"
CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES = 128


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
    test_nodeid: str
    outcome: Literal["passed", "failed", "skipped"]
    relation_id: str

    def __post_init__(self) -> None:
        _required("experiment scenario id", self.scenario_id, 256)
        _required("experiment test nodeid", self.test_nodeid, 16_384)
        _required("experiment relation id", self.relation_id, 1_024)
        if self.outcome not in {"passed", "failed", "skipped"}:
            raise ValueError("experiment scenario outcome is invalid")
        scenario = next(
            (item for item in RUNTIME_SCENARIOS if item.scenario_id == self.scenario_id),
            None,
        )
        if scenario is None or scenario.test_nodeid != self.test_nodeid:
            raise ValueError("experiment outcome is not bound to its declared scenario")


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
    canonical_state_unchanged: bool
    configuration_signature: str
    invariant_registry_fingerprint: str
    provider_id: str
    provider_schema: str
    provider_status: str
    provider_execution: str
    provider_input_signature: str
    provider_result_digest: str | None
    selected_scenarios: tuple[str, ...]
    selected_nodeids: tuple[str, ...]
    outcomes: tuple[CodeExperimentOutcome, ...]
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
            ("experiment invariant registry", self.invariant_registry_fingerprint),
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
        if self.policy_id != CODE_EXPERIMENT_EXECUTION_POLICY:
            raise ValueError("experiment execution policy is invalid")
        if self.runner_kind != "trusted_deep_declared_scenarios":
            raise ValueError("experiment receipt runner is invalid")
        if not isinstance(self.canonical_state_unchanged, bool):
            raise ValueError("experiment canonical-state guard must be boolean")
        _texts("selected experiment scenario", self.selected_scenarios, sorted_values=True)
        _texts("selected experiment nodeid", self.selected_nodeids)
        _texts("experiment limitation", self.limitations)
        if len(self.outcomes) > CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES or any(
            not isinstance(item, CodeExperimentOutcome) for item in self.outcomes
        ):
            raise ValueError("experiment outcomes are invalid or out of bounds")
        if tuple(item.scenario_id for item in self.outcomes) != self.selected_scenarios:
            raise ValueError("experiment outcomes do not cover selected scenarios exactly")
        if tuple(item.test_nodeid for item in self.outcomes) != self.selected_nodeids:
            raise ValueError("experiment outcomes do not cover selected nodeids exactly")
        for label, value in (
            ("passed scenarios", self.passed),
            ("failed scenarios", self.failed),
            ("skipped scenarios", self.skipped),
            ("experiment duration", self.duration_ms),
            ("experiment process invocations", self.process_invocations),
            ("experiment stdout bytes", self.stdout_bytes),
            ("experiment stderr bytes", self.stderr_bytes),
        ):
            _nonnegative(label, value)
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
            else "failed"
            if self.failed or self.skipped or not self.canonical_state_unchanged
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
        return {"schema": CODE_EXPERIMENT_RECEIPT_SCHEMA, **asdict(self)}


def _receipt_identity(receipt: CodeExperimentReceipt) -> str:
    from .code_analysis_epistemics import analysis_identity

    values = {key: value for key, value in asdict(receipt).items() if key != "receipt_id"}
    values["duration_ms"] = 0
    return analysis_identity(
        "code-experiment-receipt-v1",
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
    return result


def _file_digest(path: Path) -> str:
    metadata = path.stat()
    if not path.is_file() or metadata.st_size > 4 * 1024 * 1024 * 1024:
        raise ValueError("experiment Code database is missing or exceeds its bound")
    observed = fingerprint_bytes(path.read_bytes())
    after = path.stat()
    if after.st_size != metadata.st_size or after.st_mtime_ns != metadata.st_mtime_ns:
        raise ValueError("experiment Code database changed during digest read")
    return f"xxh3_128:{observed.xxh3_128}:xxh3_64:{observed.xxh3_64_guard}"


def _manifest_digest(files: tuple[ExternalEvidenceFile, ...]) -> str:
    from .external_evidence_models import external_signature

    return external_signature(
        "code-experiment-source-manifest-v1",
        {"files": [item.signature_payload() for item in files]},
    )


def _outcomes(
    publication, selected_scenarios: tuple[str, ...]
) -> tuple[CodeExperimentOutcome, ...]:
    scenario_by_nodeid = {item.test_nodeid: item for item in RUNTIME_SCENARIOS}
    selected = set(selected_scenarios)
    result: list[CodeExperimentOutcome] = []
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
        result.append(
            CodeExperimentOutcome(
                scenario.scenario_id,
                scenario.test_nodeid,
                cast(Any, outcome),
                relation.portable_relation_id,
            )
        )
    ordered = tuple(sorted(result, key=lambda item: item.scenario_id))
    if len({item.scenario_id for item in ordered}) != len(ordered):
        raise ValueError("experiment provider repeated a scenario outcome")
    return ordered


def execute_code_experiment(
    proposal: CodeExperimentProposal,
    *,
    source_root: Path,
    code_database_path: Path,
    scratch_root: Path,
    source_version: str,
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
    manifest_digest = _manifest_digest(files)
    scenario_map = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    selected_scenarios = template.scenario_ids
    selected_nodeids = tuple(sorted(scenario_map[item].test_nodeid for item in selected_scenarios))
    payload = deep_configuration_payload(
        analysis_profile="trusted-deep",
        test_selectors=selected_nodeids,
        max_tests=len(selected_nodeids),
        time_budget_seconds=template.timeout_seconds,
        shard_size=min(len(selected_nodeids), 4),
    )
    signature = deep_configuration_signature(payload)
    provider = PytestCoverageTrustedDeepProvider(source, payload, signature)
    started = time.monotonic_ns()
    publication = provider.run(source, files, baseline=None, scratch_root=scratch)
    duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    after = _file_digest(database)
    outcomes = _outcomes(publication, selected_scenarios)
    provider_status = publication.status
    status: Literal["passed", "failed", "abstained"] = (
        "abstained"
        if provider_status != "completed" or len(outcomes) != len(selected_scenarios)
        else "failed"
        if any(item.outcome != "passed" for item in outcomes) or before != after
        else "passed"
    )
    reason = (
        None
        if status != "abstained"
        else str(
            publication.publication.provenance.get("reason")
            or "provider_did_not_publish_complete_outcomes"
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
        "policy_id": CODE_EXPERIMENT_EXECUTION_POLICY,
        "proposal_id": proposal.proposal_id,
        "template_id": template.template_id,
        "template_version": template.version,
        "runner_kind": "trusted_deep_declared_scenarios",
        "source_root": os.fspath(source),
        "source_version": _required("experiment source version", source_version),
        "source_manifest_digest": manifest_digest,
        "code_database_digest_before": before,
        "code_database_digest_after": after,
        "canonical_state_unchanged": before == after,
        "configuration_signature": signature,
        "invariant_registry_fingerprint": invariant_registry_fingerprint(),
        "provider_id": publication.descriptor.provider_id,
        "provider_schema": publication.descriptor.provider_schema,
        "provider_status": provider_status,
        "provider_execution": publication.execution,
        "provider_input_signature": publication.input_signature,
        "provider_result_digest": provider_result,
        "selected_scenarios": selected_scenarios,
        "selected_nodeids": selected_nodeids,
        "outcomes": outcomes,
        "passed": sum(item.outcome == "passed" for item in outcomes),
        "failed": sum(item.outcome == "failed" for item in outcomes),
        "skipped": sum(item.outcome == "skipped" for item in outcomes),
        "duration_ms": duration_ms,
        "process_invocations": publication.counters.get("process_invocations", 0),
        "stdout_bytes": publication.counters.get("stdout_bytes", 0),
        "stderr_bytes": publication.counters.get("stderr_bytes", 0),
        "limitations": (
            "receipt_proves_selected_test_outcomes_not_formal_invariant_truth",
            "coverage_is_main_process_only",
            "process_death_scenario_is_not_power_loss",
            "no_product_mutation_authority",
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    from .code_analysis_epistemics import analysis_identity

    identity_values = _receipt_identity_values(values)
    return CodeExperimentReceipt(
        receipt_id=analysis_identity("code-experiment-receipt-v1", identity_values),
        **values,  # type: ignore[arg-type]
    )


def parse_code_experiment_receipt_payload(
    payload: Mapping[str, object],
) -> CodeExperimentReceipt:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_EXPERIMENT_RECEIPT_SCHEMA:
        raise ValueError("experiment receipt payload schema is invalid")
    expected = {field.name for field in fields(CodeExperimentReceipt)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("experiment receipt payload fields are invalid")
    raw_outcomes = payload.get("outcomes")
    if not isinstance(raw_outcomes, Sequence) or isinstance(raw_outcomes, (str, bytes, bytearray)):
        raise ValueError("experiment receipt outcomes are invalid")
    outcome_fields = {field.name for field in fields(CodeExperimentOutcome)}
    outcomes = tuple(
        CodeExperimentOutcome(**cast(Any, dict(item)))
        for item in raw_outcomes
        if isinstance(item, Mapping) and set(item) == outcome_fields
    )
    if len(outcomes) != len(raw_outcomes):
        raise ValueError("experiment receipt outcome fields are invalid")
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["outcomes"] = outcomes
    values["selected_scenarios"] = _texts(
        "selected experiment scenario", values["selected_scenarios"], sorted_values=True
    )
    values["selected_nodeids"] = _texts("selected experiment nodeid", values["selected_nodeids"])
    values["limitations"] = _texts("experiment receipt limitation", values["limitations"])
    return CodeExperimentReceipt(**cast(Any, values))


__all__ = [
    "CODE_EXPERIMENT_EXECUTION_POLICY",
    "CODE_EXPERIMENT_RECEIPT_MAX_OUTCOMES",
    "CODE_EXPERIMENT_RECEIPT_SCHEMA",
    "CodeExperimentOutcome",
    "CodeExperimentReceipt",
    "execute_code_experiment",
    "parse_code_experiment_receipt_payload",
]
