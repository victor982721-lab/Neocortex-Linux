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
from .external_deep_coverage import (
    DEEP_COVERAGE_PROVIDER_SCHEMA,
    PYTEST_COVERAGE_PROVIDER_ID,
)
from .code_external_evidence import ExternalEvidenceFile, read_external_evidence_files
from .code_invariant_contracts import RUNTIME_SCENARIOS, invariant_registry_fingerprint
from .code_schema import readonly_code_database
from .external_evidence_models import external_provider_result_digest
from .external_evidence_providers import PytestCoverageTrustedDeepProvider
from .semantic_models import fingerprint_chunks

CODE_EXPERIMENT_RECEIPT_SCHEMA = "neocortex.code-experiment-receipt/v1"
CODE_EXPERIMENT_EXECUTION_POLICY = "allowlisted-trusted-deep-scenarios-v2"
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
        if self.canonical_state_unchanged != (
            self.code_database_digest_before == self.code_database_digest_after
        ):
            raise ValueError("experiment canonical-state guard contradicts its digests")
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
    if metadata.st_size <= 0:
        raise ValueError("experiment Code database is empty")

    def chunks():
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size != metadata.st_size or before.st_mtime_ns != metadata.st_mtime_ns:
                raise ValueError("experiment Code database changed before digest read")
            while chunk := stream.read(1024 * 1024):
                yield chunk
            after_stream = os.fstat(stream.fileno())
            if (
                after_stream.st_size != before.st_size
                or after_stream.st_mtime_ns != before.st_mtime_ns
            ):
                raise ValueError("experiment Code database changed during digest read")

    observed = fingerprint_chunks(chunks())
    after = path.stat()
    if (
        observed.byte_count != metadata.st_size
        or after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
    ):
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
    for scenario_id in sorted(selected):
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
    started = time.monotonic_ns()
    publication = provider.run(source, files, baseline=None, scratch_root=scratch)
    duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    after = _file_digest(database)
    outcomes = _outcomes(publication, selected_scenarios)
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
            *(
                ("per_test_outcome_relations_unavailable_aggregate_counts_only",)
                if complete_aggregate_pass
                else ()
            ),
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
