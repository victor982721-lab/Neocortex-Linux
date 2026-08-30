"""Generic contracts for bounded, advisory external code evidence."""

from __future__ import annotations
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .code_external_evidence import (
    ExternalDiagnostic,
    ExternalEvidenceFile,
    ExternalEvidencePublication,
)
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

AnalysisProfile = Literal["protected", "trusted-static", "trusted-deep"]
ProviderTrust = Literal["untrusted-safe", "trusted-static", "trusted-execution"]
InvalidationStrategy = Literal[
    "file_local",
    "module_closure",
    "dependency_closure",
    "project_wide",
    "dynamic_suite",
]
ProviderGateStatus = Literal["passed", "failed", "baseline", "not_evaluated", "abstained"]
TypeConsensusKind = Literal[
    "both_report", "mypy_only", "pyright_only", "contradictory", "not_comparable"
]
ExternalSubjectKind = Literal["file", "symbol", "module", "project", "run", "contract", "scc"]
_EXTERNAL_SUBJECT_KINDS = frozenset(
    {"file", "symbol", "module", "project", "run", "contract", "scc"}
)

EXTERNAL_PROVIDER_SCHEMA = "neocortex.external-provider/v1"
EXTERNAL_SUITE_SCHEMA = "neocortex.external-evidence-suite/v1"
_PYRIGHT_PROVIDER_ID = "pyright-trusted-project"
_PYRIGHT_STAGE_ROOT = re.compile(
    r"(?i)(?:[a-z]:)?[\\/]+(?:[^\\/\r\n]+[\\/]+)*"
    r"neocortex-pyright-trusted-project-[^\\/\r\n]+[\\/]+source"
)
_UNCHANGED_EXTERNAL_FINDING_MESSAGE = re.compile(r"(?!x)x")
_EXTERNAL_FINDING_MESSAGE_PATHS = {_PYRIGHT_PROVIDER_ID: _PYRIGHT_STAGE_ROOT}


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    timeout_seconds: float
    memory_bound_bytes: int
    input_bytes_bound: int
    output_bytes_bound: int
    diagnostic_bound: int

    def as_payload(self) -> dict[str, object]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_bound_bytes": self.memory_bound_bytes,
            "input_bytes_bound": self.input_bytes_bound,
            "output_bytes_bound": self.output_bytes_bound,
            "diagnostic_bound": self.diagnostic_bound,
        }


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    provider_schema: str
    tool_name: str
    profile: AnalysisProfile
    trust_requirement: ProviderTrust
    scope: str
    source: str
    configuration_signature: str
    project_configuration_digest: str | None
    environment_signature: str
    comparability_signature: str
    execution_strategy: str
    invalidation_strategy: InvalidationStrategy
    cache_policy: str
    limits: ProviderLimits
    loads_project_configuration: bool = False
    loads_plugins: bool = False
    imports_content: bool = False
    executes_content: bool = False
    uses_network: bool = False
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id or not self.provider_schema or not self.tool_name:
            raise ValueError("external provider identity cannot be empty")
        if not self.source.startswith("external:"):
            raise ValueError("external provider source must use the external namespace")
        expected_trust = {
            "protected": "untrusted-safe",
            "trusted-static": "trusted-static",
            "trusted-deep": "trusted-execution",
        }[self.profile]
        if self.trust_requirement != expected_trust:
            raise ValueError("external provider profile and trust requirement disagree")
        if self.mutation_authority:
            raise ValueError("external evidence providers cannot mutate content")

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": EXTERNAL_PROVIDER_SCHEMA,
            "provider_id": self.provider_id,
            "provider_schema": self.provider_schema,
            "tool_name": self.tool_name,
            "profile": self.profile,
            "trust_requirement": self.trust_requirement,
            "scope": self.scope,
            "source": self.source,
            "configuration_signature": self.configuration_signature,
            "project_configuration_digest": self.project_configuration_digest,
            "environment_signature": self.environment_signature,
            "comparability_signature": self.comparability_signature,
            "execution_strategy": self.execution_strategy,
            "invalidation_strategy": self.invalidation_strategy,
            "cache_policy": self.cache_policy,
            "limits": self.limits.as_payload(),
            "loads_project_configuration": self.loads_project_configuration,
            "loads_plugins": self.loads_plugins,
            "imports_content": self.imports_content,
            "executes_content": self.executes_content,
            "uses_network": self.uses_network,
            "authority": self.authority,
            "mutation_authority": self.mutation_authority,
        }


@dataclass(frozen=True, slots=True)
class ExternalRunInput:
    version_id: int
    portable_input_id: str
    relative_path: str
    eligible: bool
    covered: bool
    coverage_reason: str | None
    size: int
    content_digest: str

    @classmethod
    def from_file(
        cls,
        item: ExternalEvidenceFile,
        *,
        covered: bool,
        coverage_reason: str | None = None,
    ) -> ExternalRunInput:
        digest = f"xxh3_128:{item.raw_xxh3_128}:xxh3_64:{item.raw_xxh3_64_guard}"
        identity_payload = canonical_json(
            {
                "path": item.relative_path,
                "size": item.size,
                "content_digest": digest,
            }
        )
        portable_id = "external-input-v1:xxh3_128:" + fingerprint_text(identity_payload).xxh3_128
        return cls(
            item.version_id,
            portable_id,
            item.relative_path,
            True,
            covered,
            coverage_reason,
            item.size,
            digest,
        )


@dataclass(frozen=True, slots=True)
class ExternalProviderFinding:
    portable_finding_id: str
    version_id: int
    relative_path: str
    category: str
    code: str
    severity: str
    message: str
    observation_confirmed: bool
    tool_confidence: float | None
    calibrated_confidence: float | None
    gate_authority: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    url: str | None = None
    fix_available: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)
    mutation_authority: bool = False

    def __post_init__(self) -> None:
        if self.mutation_authority:
            raise ValueError("external findings cannot authorize mutation")
        for confidence in (self.tool_confidence, self.calibrated_confidence):
            if confidence is not None and not 0.0 <= confidence <= 1.0:
                raise ValueError("external finding confidence must be within 0..1")
        if self.start_line < 1 or self.start_column < 0:
            raise ValueError("external finding start location is invalid")
        if self.end_line < self.start_line or self.end_column < 0:
            raise ValueError("external finding end location is invalid")

    @classmethod
    def from_diagnostic(
        cls,
        diagnostic: ExternalDiagnostic,
        *,
        category: str,
        severity: str = "warning",
        gate_authority: str = "advisory",
        metadata: Mapping[str, object] | None = None,
    ) -> ExternalProviderFinding:
        return cls(
            diagnostic.identity,
            diagnostic.version_id,
            diagnostic.relative_path,
            category,
            diagnostic.code,
            severity,
            diagnostic.message,
            True,
            1.0,
            None,
            gate_authority,
            diagnostic.start_line,
            diagnostic.start_column,
            diagnostic.end_line,
            diagnostic.end_column,
            diagnostic.url,
            diagnostic.fix_available,
            {} if metadata is None else metadata,
        )

    def digest_payload(self) -> dict[str, object]:
        return {
            "portable_finding_id": self.portable_finding_id,
            "path": self.relative_path,
            "category": self.category,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "observation_confirmed": self.observation_confirmed,
            "tool_confidence": self.tool_confidence,
            "calibrated_confidence": self.calibrated_confidence,
            "gate_authority": self.gate_authority,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "url": self.url,
            "fix_available": self.fix_available,
            "metadata": dict(self.metadata),
            "mutation_authority": self.mutation_authority,
        }


@dataclass(frozen=True, slots=True)
class ExternalProviderMetric:
    """One portable numeric observation owned by an external provider run."""

    portable_metric_id: str
    subject_kind: ExternalSubjectKind
    subject_key: str
    category: str
    metric_name: str
    value: float
    unit: str
    version_id: int | None = None
    symbol_id: int | None = None
    project_id: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.portable_metric_id or not self.subject_key:
            raise ValueError("external metric identity cannot be empty")
        if self.subject_kind not in _EXTERNAL_SUBJECT_KINDS:
            raise ValueError("external metric subject kind is invalid")
        if not self.category or not self.metric_name or not self.unit:
            raise ValueError("external metric definition cannot be empty")
        if isinstance(self.value, bool) or not math.isfinite(float(self.value)):
            raise ValueError("external metric value must be finite")
        object.__setattr__(self, "value", float(self.value))
        for local_id in (self.version_id, self.symbol_id, self.project_id):
            if local_id is not None and (
                isinstance(local_id, bool) or not isinstance(local_id, int) or local_id < 1
            ):
                raise ValueError("external metric local identity is invalid")

    def digest_payload(self) -> dict[str, object]:
        return {
            "portable_metric_id": self.portable_metric_id,
            "subject_kind": self.subject_kind,
            "subject_key": self.subject_key,
            "category": self.category,
            "metric_name": self.metric_name,
            "value": self.value,
            "unit": self.unit,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ExternalProviderRelation:
    """One portable, explainable edge owned by an external provider run."""

    portable_relation_id: str
    relation_kind: str
    source_kind: ExternalSubjectKind
    source_key: str
    target_kind: ExternalSubjectKind
    target_key: str
    directed: bool = True
    confidence: float | None = None
    source_version_id: int | None = None
    source_symbol_id: int | None = None
    source_project_id: int | None = None
    target_version_id: int | None = None
    target_symbol_id: int | None = None
    target_project_id: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.portable_relation_id or not self.relation_kind:
            raise ValueError("external relation identity cannot be empty")
        if not self.source_key or not self.target_key:
            raise ValueError("external relation endpoint cannot be empty")
        if (
            self.source_kind not in _EXTERNAL_SUBJECT_KINDS
            or self.target_kind not in _EXTERNAL_SUBJECT_KINDS
        ):
            raise ValueError("external relation endpoint kind is invalid")
        if not isinstance(self.directed, bool):
            raise ValueError("external relation direction must be a boolean")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not 0.0 <= self.confidence <= 1.0:
                raise ValueError("external relation confidence must be within 0..1")
            object.__setattr__(self, "confidence", float(self.confidence))
        for local_id in (
            self.source_version_id,
            self.source_symbol_id,
            self.source_project_id,
            self.target_version_id,
            self.target_symbol_id,
            self.target_project_id,
        ):
            if local_id is not None and (
                isinstance(local_id, bool) or not isinstance(local_id, int) or local_id < 1
            ):
                raise ValueError("external relation local identity is invalid")

    def digest_payload(self) -> dict[str, object]:
        return {
            "portable_relation_id": self.portable_relation_id,
            "relation_kind": self.relation_kind,
            "source_kind": self.source_kind,
            "source_key": self.source_key,
            "target_kind": self.target_kind,
            "target_key": self.target_key,
            "directed": self.directed,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


def external_findings_digest(
    findings: Sequence[ExternalProviderFinding],
) -> str:
    ordered = sorted(
        (item.digest_payload() for item in findings),
        key=lambda item: str(item["portable_finding_id"]),
    )
    payload = canonical_json({"findings": ordered})
    return "external-findings-v1:xxh3_128:" + fingerprint_text(payload).xxh3_128


def external_provider_result_digest(
    findings: Sequence[ExternalProviderFinding],
    metrics: Sequence[ExternalProviderMetric] = (),
    relations: Sequence[ExternalProviderRelation] = (),
) -> str:
    """Digest all provider evidence while preserving the exact Hito 1 digest."""

    if not metrics and not relations:
        return external_findings_digest(findings)
    ordered_findings = sorted(
        (item.digest_payload() for item in findings),
        key=lambda item: str(item["portable_finding_id"]),
    )
    ordered_metrics = sorted(
        (item.digest_payload() for item in metrics),
        key=lambda item: str(item["portable_metric_id"]),
    )
    ordered_relations = sorted(
        (item.digest_payload() for item in relations),
        key=lambda item: str(item["portable_relation_id"]),
    )
    payload = canonical_json(
        {
            "findings": ordered_findings,
            "metrics": ordered_metrics,
            "relations": ordered_relations,
        }
    )
    return "external-provider-result-v2:xxh3_128:" + fingerprint_text(payload).xxh3_128


def external_metric_identity(
    provider_id: str,
    *,
    subject_kind: ExternalSubjectKind,
    subject_key: str,
    category: str,
    metric_name: str,
    unit: str,
) -> str:
    return external_signature(
        "external-metric-v1",
        {
            "provider_id": provider_id,
            "subject_kind": subject_kind,
            "subject_key": subject_key,
            "category": category,
            "metric_name": metric_name,
            "unit": unit,
        },
    )


def external_relation_identity(
    provider_id: str,
    *,
    relation_kind: str,
    source_kind: ExternalSubjectKind,
    source_key: str,
    target_kind: ExternalSubjectKind,
    target_key: str,
    directed: bool = True,
) -> str:
    return external_signature(
        "external-relation-v1",
        {
            "provider_id": provider_id,
            "relation_kind": relation_kind,
            "source_kind": source_kind,
            "source_key": source_key,
            "target_kind": target_kind,
            "target_key": target_key,
            "directed": directed,
        },
    )


def external_root_identity(root: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(root))
    payload = canonical_json({"root": normalized.replace("\\", "/")})
    return "external-root-v1:xxh3_128:" + fingerprint_text(payload).xxh3_128


def external_signature(prefix: str, payload: Mapping[str, object]) -> str:
    if not prefix or any(character.isspace() for character in prefix):
        raise ValueError("external signature prefix is invalid")
    return f"{prefix}:xxh3_128:" + fingerprint_text(canonical_json(payload)).xxh3_128


def normalize_external_finding_message(provider_id: str, message: str) -> str:
    """Remove provider-owned volatile paths from otherwise portable evidence."""

    pattern = _EXTERNAL_FINDING_MESSAGE_PATHS.get(provider_id, _UNCHANGED_EXTERNAL_FINDING_MESSAGE)
    return pattern.sub("<project>", message)


def external_finding_identity(
    provider_id: str,
    *,
    relative_path: str,
    category: str,
    code: str,
    message: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> str:
    """Build a stable finding identity from normalized public evidence."""

    return external_signature(
        "external-finding-v1",
        {
            "provider_id": provider_id,
            "path": relative_path,
            "category": category,
            "code": code,
            "message": normalize_external_finding_message(provider_id, message),
            "start_line": start_line,
            "start_column": start_column,
            "end_line": end_line,
            "end_column": end_column,
        },
    )


@dataclass(frozen=True, slots=True)
class ExternalProviderPublication:
    descriptor: ProviderDescriptor
    publication: ExternalEvidencePublication
    observed_root: str
    root_identity: str
    input_signature: str
    inputs: tuple[ExternalRunInput, ...]
    findings: tuple[ExternalProviderFinding, ...]
    counters: Mapping[str, int]
    coverage_complete: bool
    result_digest: str | None
    portable_publication_id: str
    replay_source_tool_run_id: int | None = None
    verification_signature: str | None = None
    limitations: tuple[str, ...] = ()
    metrics: tuple[ExternalProviderMetric, ...] = ()
    relations: tuple[ExternalProviderRelation, ...] = ()

    @property
    def status(self) -> str:
        return self.publication.status

    @property
    def execution(self) -> str:
        return self.publication.execution


@dataclass(frozen=True, slots=True)
class ExternalEvidenceBundle:
    """One legacy projection plus normalized providers, published atomically."""

    legacy: ExternalEvidencePublication | None
    providers: tuple[ExternalProviderPublication, ...]


@dataclass(frozen=True, slots=True)
class ExternalProviderBaseline:
    tool_run_id: int
    provider_id: str
    tool_version: str
    input_signature: str
    comparability_signature: str
    result_digest: str
    portable_finding_ids: tuple[str, ...]
    portable_metric_ids: tuple[str, ...] = ()
    portable_relation_ids: tuple[str, ...] = ()
    fresh_until_unix_seconds: float | None = None
    reuse_mode: Literal["comparison_only", "exact_replay"] = "comparison_only"

    def __post_init__(self) -> None:
        if self.reuse_mode not in {"comparison_only", "exact_replay"}:
            raise ValueError("external provider baseline reuse mode is invalid")


@dataclass(frozen=True, slots=True)
class ExternalProviderEvidence:
    provider_id: str
    tool_run_id: int
    effective_tool_run_id: int | None
    status: Literal["ready", "abstained"]
    reason: str | None
    findings: tuple[ExternalProviderFinding, ...] = ()
    metrics: tuple[ExternalProviderMetric, ...] = ()
    relations: tuple[ExternalProviderRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalProviderAttestation:
    """One ready provider publication with its exact persisted contract.

    Unlike :class:`ExternalProviderEvidence`, this model retains the run and
    configuration fields needed to attest a bounded subset of already
    published evidence without executing the provider again.
    """

    analysis_run_id: int
    processing_signature: str
    provider_id: str
    provider_schema: str
    profile: AnalysisProfile
    tool_run_id: int
    effective_tool_run_id: int
    tool_name: str
    tool_version: str
    tool_status: Literal["completed", "skipped"]
    execution: Literal["full", "cache_replay"]
    observed_root: str
    root_identity: str
    input_signature: str
    descriptor_configuration_signature: str
    environment_signature: str
    comparability_signature: str
    result_digest: str
    portable_publication_id: str
    coverage_complete: bool
    content_executed: bool
    eligible_files: int
    covered_files: int
    counters: Mapping[str, int]
    inputs: tuple[ExternalRunInput, ...] = ()
    limitations: tuple[str, ...] = ()
    findings: tuple[ExternalProviderFinding, ...] = ()
    metrics: tuple[ExternalProviderMetric, ...] = ()
    relations: tuple[ExternalProviderRelation, ...] = ()

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (
                self.analysis_run_id,
                self.tool_run_id,
                self.effective_tool_run_id,
            )
        ):
            raise ValueError("external provider attestation run identity is invalid")
        if any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in (
                self.provider_id,
                self.provider_schema,
                self.processing_signature,
                self.tool_name,
                self.tool_version,
                self.observed_root,
                self.root_identity,
                self.input_signature,
                self.descriptor_configuration_signature,
                self.environment_signature,
                self.comparability_signature,
                self.result_digest,
                self.portable_publication_id,
            )
        ):
            raise ValueError("external provider attestation contract is incomplete")
        if self.profile not in {"protected", "trusted-static", "trusted-deep"}:
            raise ValueError("external provider attestation profile is invalid")
        if self.tool_status not in {"completed", "skipped"}:
            raise ValueError("external provider attestation tool status is invalid")
        if self.execution not in {"full", "cache_replay"}:
            raise ValueError("external provider attestation execution is invalid")
        if self.tool_status == "skipped" and self.execution != "cache_replay":
            raise ValueError("external provider attestation skipped a non-replay run")
        if not isinstance(self.coverage_complete, bool) or not isinstance(
            self.content_executed, bool
        ):
            raise ValueError("external provider attestation booleans are invalid")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (self.eligible_files, self.covered_files)
            )
            or self.covered_files > self.eligible_files
        ):
            raise ValueError("external provider attestation coverage is invalid")
        if any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.counters.items()
        ):
            raise ValueError("external provider attestation counters are invalid")
        if len(self.inputs) != self.eligible_files or any(
            not isinstance(item, ExternalRunInput) or not item.eligible for item in self.inputs
        ):
            raise ValueError("external provider attestation inputs are invalid")
        if sum(item.covered for item in self.inputs) != self.covered_files:
            raise ValueError("external provider attestation input coverage is invalid")


@dataclass(frozen=True, slots=True)
class ExternalProviderStatus:
    provider_id: str
    provider_schema: str
    profile: AnalysisProfile
    tool_name: str
    tool_version: str | None
    status: Literal["ready", "abstained", "not_recorded"]
    reason: str | None
    execution: str | None
    eligible_files: int
    covered_files: int
    findings: int
    added: int | None
    resolved: int | None
    comparable: bool
    result_digest: str | None
    comparability_signature: str | None
    gate: Literal["passed", "failed", "baseline", "not_evaluated"]
    limitations: tuple[str, ...] = ()
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False
    content_executed: bool = False
    counters: Mapping[str, int] = field(default_factory=dict)
    metrics: int = 0
    relations: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": EXTERNAL_PROVIDER_SCHEMA,
            "provider_id": self.provider_id,
            "provider_schema": self.provider_schema,
            "profile": self.profile,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "status": self.status,
            "reason": self.reason,
            "execution": self.execution,
            "eligible_files": self.eligible_files,
            "covered_files": self.covered_files,
            "findings": self.findings,
            "metrics": self.metrics,
            "relations": self.relations,
            "added": self.added,
            "resolved": self.resolved,
            "comparable": self.comparable,
            "result_digest": self.result_digest,
            "comparability_signature": self.comparability_signature,
            "gate": self.gate,
            "limitations": list(self.limitations),
            "authority": self.authority,
            "mutation_authority": self.mutation_authority,
            "content_executed": self.content_executed,
            "counters": dict(sorted(self.counters.items())),
        }


@dataclass(frozen=True, slots=True)
class ProviderGateEvaluation:
    gate: str
    provider_id: str
    status: ProviderGateStatus
    reason: str

    def as_payload(self) -> dict[str, str]:
        return {
            "gate": self.gate,
            "provider_id": self.provider_id,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TypeConsensusSummary:
    status: TypeConsensusKind
    both_report: int = 0
    mypy_only: int = 0
    pyright_only: int = 0
    contradictory: int = 0
    not_comparable: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "both_report": self.both_report,
            "mypy_only": self.mypy_only,
            "pyright_only": self.pyright_only,
            "contradictory": self.contradictory,
            "not_comparable": self.not_comparable,
        }


@dataclass(frozen=True, slots=True)
class ExternalEvidenceSuiteStatus:
    profile: AnalysisProfile
    status: Literal["ready", "partial", "abstained", "not_recorded"]
    providers: tuple[ExternalProviderStatus, ...]
    type_consensus: TypeConsensusSummary
    gates: tuple[ProviderGateEvaluation, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": EXTERNAL_SUITE_SCHEMA,
            "profile": self.profile,
            "status": self.status,
            "providers": [item.as_payload() for item in self.providers],
            "type_consensus": self.type_consensus.as_payload(),
            "gates": [item.as_payload() for item in self.gates],
        }


class ExternalEvidenceProvider(Protocol):
    descriptor: ProviderDescriptor

    def tool_version(self) -> str | None:
        """Return the exact executable version or ``None`` when unavailable."""

    def run(
        self,
        root: Path,
        files: Sequence[ExternalEvidenceFile],
        *,
        baseline: ExternalProviderBaseline | None,
        scratch_root: Path,
    ) -> ExternalProviderPublication:
        """Produce one terminal, bounded publication without mutating content."""


__all__ = [
    "EXTERNAL_PROVIDER_SCHEMA",
    "EXTERNAL_SUITE_SCHEMA",
    "AnalysisProfile",
    "ExternalEvidenceBundle",
    "ExternalEvidenceProvider",
    "ExternalEvidenceSuiteStatus",
    "ExternalProviderAttestation",
    "ExternalProviderBaseline",
    "ExternalProviderEvidence",
    "ExternalProviderFinding",
    "ExternalProviderMetric",
    "ExternalProviderPublication",
    "ExternalProviderRelation",
    "ExternalProviderStatus",
    "ExternalRunInput",
    "ExternalSubjectKind",
    "ProviderDescriptor",
    "ProviderGateEvaluation",
    "ProviderLimits",
    "TypeConsensusSummary",
    "external_finding_identity",
    "external_findings_digest",
    "external_metric_identity",
    "external_provider_result_digest",
    "external_relation_identity",
    "external_root_identity",
    "external_signature",
    "normalize_external_finding_message",
]
