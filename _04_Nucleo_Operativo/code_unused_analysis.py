"""Explainable, advisory consensus for potentially unused Python symbols.

The consumer correlates already-published static findings with the current Code
graph and optional trusted-deep coverage.  It never mutates source content and
never treats a tool confidence value as deletion authority.
"""

from __future__ import annotations

import ast
import math
import re
import sqlite3
import tomllib
import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Literal, cast

from .code_coverage_analysis import read_code_coverage_analysis
from .external_evidence_models import ExternalProviderEvidence, ExternalProviderFinding
from .external_evidence_store import (
    read_external_evidence_suite,
    read_external_provider_evidence,
)
from .semantic_models import canonical_json, fingerprint_text

CODE_UNUSED_SCHEMA = "neocortex.code-unused-analysis/v1"
VULTURE_UNUSED_PROVIDER_ID = "vulture-unused-static"
PYRIGHT_UNUSED_PROVIDER_ID = "pyright-trusted-project"

CODE_UNUSED_CANDIDATE_LIMIT = 20_000
CODE_UNUSED_FINDING_LIMIT = 25_000
CODE_UNUSED_SOURCE_BYTES_LIMIT = 32 * 1024 * 1024
CODE_UNUSED_SOURCE_FILE_BYTES_LIMIT = 2 * 1024 * 1024
CODE_UNUSED_SAMPLE_LIMIT = 1_000
CODE_UNUSED_TEXT_LIMIT = 8_192
CODE_UNUSED_PUBLIC_CANDIDATE_LIMIT = 20
CODE_UNUSED_SYMBOL_LIMIT = 100_000
CODE_UNUSED_REFERENCE_LIMIT = 500_000
VULTURE_HIGH_CONFIDENCE = 0.90

UnusedState = Literal[
    "explained_usage",
    "dynamic_usage_possible",
    "insufficient_evidence",
    "probable_unused_high_consensus",
]
CoverageState = Literal["complete", "partial", "missing"]
ProviderState = Literal["ready", "abstained", "missing"]
AnalysisState = Literal["ready", "abstained"]
GateState = Literal["passed", "failed", "not_evaluated"]
CalibrationLabel = Literal["used", "unused"]


def _validate_signals(signals: UnusedEvidenceSignals) -> None:
    if signals.coverage_status not in {"complete", "partial", "missing"}:
        raise ValueError("unused coverage status is invalid")
    if signals.vulture_confidence is not None and (
        isinstance(signals.vulture_confidence, bool)
        or not math.isfinite(signals.vulture_confidence)
        or not 0.0 <= signals.vulture_confidence <= 1.0
    ):
        raise ValueError("unused Vulture confidence is invalid")
    for value in (signals.graph_references, signals.graph_calls, signals.graph_imports):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("unused graph count is invalid")
    if len(signals.evidence_ids) > CODE_UNUSED_FINDING_LIMIT:
        raise ValueError("unused evidence identity bound exceeded")
    if len(set(signals.evidence_ids)) != len(signals.evidence_ids):
        raise ValueError("unused evidence identities are duplicated")


@dataclass(frozen=True, slots=True)
class UnusedEvidenceSignals:
    vulture_reported: bool
    vulture_confidence: float | None
    pyright_reported: bool
    vulture_complete: bool
    pyright_complete: bool
    providers_aligned: bool
    graph_references: int
    graph_calls: int
    graph_imports: int
    in_all: bool
    reexported: bool
    entry_point: bool
    callback: bool
    registry: bool
    fixture: bool
    protocol: bool
    special: bool
    coverage_observed: bool
    coverage_status: CoverageState
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_signals(self)


@dataclass(frozen=True, slots=True)
class UnusedConsensusCandidate:
    candidate_id: str
    version_id: int
    symbol_id: int | None
    relative_path: str
    module_id: str | None
    symbol: str | None
    name: str
    kind: str
    start_line: int
    end_line: int
    state: UnusedState
    provider_ids: tuple[str, ...]
    signals: UnusedEvidenceSignals
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        if self.state not in {
            "explained_usage",
            "dynamic_usage_possible",
            "insufficient_evidence",
            "probable_unused_high_consensus",
        }:
            raise ValueError("invalid unused consensus state")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("unused candidate authority must remain advisory and non-mutating")
        if not self.candidate_id or not self.relative_path or not self.name:
            raise ValueError("unused candidate identity is incomplete")
        if (
            isinstance(self.version_id, bool)
            or not isinstance(self.version_id, int)
            or self.version_id < 1
        ):
            raise ValueError("unused candidate version identity is invalid")
        if self.symbol_id is not None and (
            isinstance(self.symbol_id, bool)
            or not isinstance(self.symbol_id, int)
            or self.symbol_id < 1
        ):
            raise ValueError("unused candidate symbol identity is invalid")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (self.start_line, self.end_line)
            )
            or self.end_line < self.start_line
        ):
            raise ValueError("unused candidate source span is invalid")
        if len(set(self.provider_ids)) != len(self.provider_ids):
            raise ValueError("unused candidate providers are duplicated")
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("unused candidate evidence is duplicated")
        if self.state == "probable_unused_high_consensus":
            if (
                set(self.provider_ids)
                != {
                    PYRIGHT_UNUSED_PROVIDER_ID,
                    VULTURE_UNUSED_PROVIDER_ID,
                }
                or len(self.provider_ids) != 2
            ):
                raise ValueError("high-consensus unused candidate requires canonical providers")
            classified_state, canonical_reasons = _classify(self.signals)
            if classified_state != self.state:
                raise ValueError("high-consensus unused state disagrees with its signals")
            if self.reasons != canonical_reasons:
                raise ValueError("high-consensus unused reasons must be derived from signals")
            if not self.provider_ids or not self.evidence or not self.signals.evidence_ids:
                raise ValueError("high-consensus unused candidate requires provider evidence")
            if set(self.evidence) != set(self.signals.evidence_ids):
                raise ValueError("unused candidate evidence must match its signal evidence")


@dataclass(frozen=True, slots=True)
class UnusedCalibrationSample:
    sample_id: str
    label: CalibrationLabel
    signals: UnusedEvidenceSignals

    def __post_init__(self) -> None:
        if not self.sample_id or self.label not in {"used", "unused"}:
            raise ValueError("unused calibration sample is invalid")


@dataclass(frozen=True, slots=True)
class UnusedCalibrationReport:
    signature: str
    dataset_id: str
    total: int
    positive_labels: int
    negative_labels: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    abstained_positive: int
    abstained_negative: int
    precision: float | None
    recall: float | None
    abstention_rate: float | None
    precision_denominator: int
    recall_denominator: int
    abstention_denominator: int


@dataclass(frozen=True, slots=True)
class UnusedProviderStatus:
    provider_id: str
    status: ProviderState
    reason: str | None
    tool_run_id: int | None
    effective_tool_run_id: int | None
    findings: int
    eligible_candidates: int
    covered_candidates: int
    comparability: str
    source_provider_schema: str | None = None
    source_tool_name: str | None = None
    source_tool_version: str | None = None
    source_comparability_signature: str | None = None

    def __post_init__(self) -> None:
        if self.provider_id not in {PYRIGHT_UNUSED_PROVIDER_ID, VULTURE_UNUSED_PROVIDER_ID}:
            raise ValueError("unused provider identity is unsupported")
        if self.status not in {"ready", "abstained", "missing"}:
            raise ValueError("invalid unused provider status")
        if self.status == "ready":
            if self.reason is not None or self.tool_run_id is None:
                raise ValueError("ready unused provider requires a tool-run receipt")
            if self.comparability != "comparable":
                raise ValueError("ready unused provider must be comparable")
            if (
                not self.source_provider_schema
                or not self.source_tool_name
                or not self.source_tool_version
                or not self.source_comparability_signature
            ):
                raise ValueError("ready unused provider requires exact source provenance")
        elif not self.reason:
            raise ValueError("non-ready unused provider requires a reason")


@dataclass(frozen=True, slots=True)
class UnusedGateEvaluation:
    gate: str
    status: GateState
    reason: str | None

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "not_evaluated"}:
            raise ValueError("invalid unused calibration gate status")


@dataclass(frozen=True, slots=True)
class CodeUnusedAnalysis:
    database: str
    analysis_run_id: int | None
    status: AnalysisState
    reason: str | None
    policy_signature: str
    provider_signature: str
    evidence_signature: str
    calibration_signature: str
    coverage_status: CoverageState
    providers: tuple[UnusedProviderStatus, ...]
    candidates: tuple[UnusedConsensusCandidate, ...]
    counts: Mapping[str, int]
    calibration: UnusedCalibrationReport
    holdout: UnusedCalibrationReport
    gates: tuple[UnusedGateEvaluation, ...]
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        if self.status not in {"ready", "abstained"}:
            raise ValueError("invalid unused analysis status")
        if self.coverage_status not in {"complete", "partial", "missing"}:
            raise ValueError("invalid unused analysis coverage status")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("unused analysis authority must remain advisory and non-mutating")
        if self.status == "ready" and self.reason is not None:
            raise ValueError("ready unused analysis cannot carry an abstention reason")
        if self.status == "abstained" and not self.reason:
            raise ValueError("abstained unused analysis requires a reason")
        if self.policy_signature != CODE_UNUSED_POLICY_SIGNATURE:
            raise ValueError("unused analysis policy signature is not canonical")
        if not self.evidence_signature:
            raise ValueError("unused analysis requires an evidence receipt")
        if self.status == "ready" and not self.provider_signature:
            raise ValueError("ready unused analysis requires a provider receipt")
        if self.status == "ready":
            provider_ids = tuple(provider.provider_id for provider in self.providers)
            if (
                set(provider_ids) != {PYRIGHT_UNUSED_PROVIDER_ID, VULTURE_UNUSED_PROVIDER_ID}
                or len(provider_ids) != 2
            ):
                raise ValueError("ready unused analysis requires both canonical providers")
            if any(provider.status != "ready" for provider in self.providers):
                raise ValueError("ready unused analysis cannot contain a non-ready provider")
            if self.provider_signature != build_unused_provider_signature(self.providers):
                raise ValueError("unused analysis provider receipt is not reproducible")
        expected_evidence_signature = _evidence_signature(self.candidates)
        if self.evidence_signature != expected_evidence_signature:
            raise ValueError("unused analysis evidence receipt disagrees with candidates")
        expected_calibration_signature = _calibration_signature(self.calibration, self.holdout)
        if self.calibration_signature != expected_calibration_signature:
            raise ValueError("unused analysis calibration receipt is not canonical")
        expected_gates = _calibration_gates(self.calibration, self.holdout)
        if self.gates != expected_gates:
            raise ValueError("unused analysis calibration gates are not reproducible")
        expected_counts: Counter[str] = Counter(candidate.state for candidate in self.candidates)
        expected_counts["total"] = len(self.candidates)
        for state_name in (
            "explained_usage",
            "dynamic_usage_possible",
            "insufficient_evidence",
            "probable_unused_high_consensus",
        ):
            expected_counts.setdefault(state_name, 0)
        if dict(self.counts) != dict(expected_counts):
            raise ValueError("unused analysis counts disagree with candidates")

    def as_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["counts"] = dict(sorted(self.counts.items()))
        candidates = cast(list[dict[str, object]], payload["candidates"])
        payload["candidates_total"] = len(candidates)
        payload["candidates_truncated"] = len(candidates) > CODE_UNUSED_PUBLIC_CANDIDATE_LIMIT
        payload["candidates"] = candidates[:CODE_UNUSED_PUBLIC_CANDIDATE_LIMIT]
        return {"kind": "code-unused-analysis", "schema": CODE_UNUSED_SCHEMA, **payload}

    def digest_payload(self) -> dict[str, object]:
        """Return replay-stable evidence without database-local identities."""

        payload = asdict(self)
        payload = {"kind": "code-unused-analysis", "schema": CODE_UNUSED_SCHEMA, **payload}
        payload.pop("database")
        payload.pop("analysis_run_id")
        providers = cast(list[dict[str, object]], payload["providers"])
        for provider in providers:
            provider.pop("tool_run_id")
            provider.pop("effective_tool_run_id")
        candidates = cast(list[dict[str, object]], payload["candidates"])
        for candidate in candidates:
            candidate.pop("version_id")
            candidate.pop("symbol_id")
        return payload


_POLICY_PAYLOAD = {
    "schema": CODE_UNUSED_SCHEMA,
    "vulture_high_confidence": VULTURE_HIGH_CONFIDENCE,
    "precedence": [
        "confirmed_usage",
        "dynamic_usage_possible",
        "high_consensus",
        "insufficient_evidence",
    ],
    "states": [
        "explained_usage",
        "dynamic_usage_possible",
        "insufficient_evidence",
        "probable_unused_high_consensus",
    ],
    "mutation_authority": False,
}
CODE_UNUSED_POLICY_SIGNATURE = (
    "unused-policy-v1:xxh3_128:" + fingerprint_text(canonical_json(_POLICY_PAYLOAD)).xxh3_128
)


def _signal(
    *,
    vulture: bool = False,
    confidence: float | None = None,
    pyright: bool = False,
    complete: bool = True,
    references: int = 0,
    calls: int = 0,
    imports: int = 0,
    in_all: bool = False,
    reexported: bool = False,
    entry_point: bool = False,
    callback: bool = False,
    registry: bool = False,
    fixture: bool = False,
    protocol: bool = False,
    special: bool = False,
    coverage_observed: bool = False,
    coverage_status: CoverageState = "missing",
) -> UnusedEvidenceSignals:
    return UnusedEvidenceSignals(
        vulture,
        confidence,
        pyright,
        complete,
        complete,
        complete,
        references,
        calls,
        imports,
        in_all,
        reexported,
        entry_point,
        callback,
        registry,
        fixture,
        protocol,
        special,
        coverage_observed,
        coverage_status,
        (),
    )


DEFAULT_CALIBRATION_SAMPLES = (
    UnusedCalibrationSample(
        "cal-used-reference",
        "used",
        _signal(vulture=True, confidence=1.0, pyright=True, references=2),
    ),
    UnusedCalibrationSample(
        "cal-used-import", "used", _signal(vulture=True, confidence=0.9, pyright=True, imports=1)
    ),
    UnusedCalibrationSample(
        "cal-used-export", "used", _signal(vulture=True, confidence=1.0, pyright=True, in_all=True)
    ),
    UnusedCalibrationSample(
        "cal-used-coverage",
        "used",
        _signal(
            vulture=True,
            confidence=1.0,
            pyright=True,
            coverage_observed=True,
            coverage_status="complete",
        ),
    ),
    UnusedCalibrationSample(
        "cal-dynamic-registry",
        "used",
        _signal(vulture=True, confidence=1.0, pyright=True, registry=True),
    ),
    UnusedCalibrationSample(
        "cal-unused-both-100", "unused", _signal(vulture=True, confidence=1.0, pyright=True)
    ),
    UnusedCalibrationSample(
        "cal-unused-both-90", "unused", _signal(vulture=True, confidence=0.9, pyright=True)
    ),
    UnusedCalibrationSample(
        "cal-unused-vulture-only", "unused", _signal(vulture=True, confidence=1.0)
    ),
)

DEFAULT_HOLDOUT_SAMPLES = (
    UnusedCalibrationSample(
        "hold-used-call", "used", _signal(vulture=True, confidence=1.0, pyright=True, calls=1)
    ),
    UnusedCalibrationSample(
        "hold-used-entry",
        "used",
        _signal(vulture=True, confidence=1.0, pyright=True, entry_point=True),
    ),
    UnusedCalibrationSample(
        "hold-dynamic-protocol",
        "used",
        _signal(vulture=True, confidence=1.0, pyright=True, protocol=True),
    ),
    UnusedCalibrationSample(
        "hold-unused-both", "unused", _signal(vulture=True, confidence=1.0, pyright=True)
    ),
    UnusedCalibrationSample(
        "hold-unused-low", "unused", _signal(vulture=True, confidence=0.6, pyright=True)
    ),
    UnusedCalibrationSample(
        "hold-unused-incomplete",
        "unused",
        _signal(vulture=True, confidence=1.0, pyright=True, complete=False),
    ),
)


def _confirmed_usage_reasons(signals: UnusedEvidenceSignals) -> tuple[str, ...]:
    reasons = []
    for observed, reason in (
        (bool(signals.graph_references), "indexed_reference"),
        (bool(signals.graph_calls), "indexed_call"),
        (bool(signals.graph_imports), "indexed_import"),
        (signals.in_all, "declared_in___all__"),
        (signals.reexported, "reexported"),
        (signals.entry_point, "declared_entry_point"),
        (signals.coverage_observed, "observed_by_declared_coverage_scope"),
    ):
        if observed:
            reasons.append(reason)
    return tuple(reasons)


def _dynamic_usage_reasons(signals: UnusedEvidenceSignals) -> tuple[str, ...]:
    reasons = []
    for observed, reason in (
        (signals.callback, "callback_binding"),
        (signals.registry, "registry_binding"),
        (signals.fixture, "fixture_binding"),
        (signals.protocol, "protocol_contract"),
        (signals.special, "special_runtime_protocol"),
    ):
        if observed:
            reasons.append(reason)
    return tuple(reasons)


def _has_high_unused_consensus(signals: UnusedEvidenceSignals) -> bool:
    return bool(
        signals.vulture_reported
        and signals.pyright_reported
        and signals.vulture_confidence is not None
        and signals.vulture_confidence >= VULTURE_HIGH_CONFIDENCE
        and signals.vulture_complete
        and signals.pyright_complete
        and signals.providers_aligned
    )


def _insufficient_evidence_reasons(signals: UnusedEvidenceSignals) -> tuple[str, ...]:
    reasons = []
    if not signals.vulture_reported:
        reasons.append("vulture_did_not_report")
    elif signals.vulture_confidence is None or (
        signals.vulture_confidence < VULTURE_HIGH_CONFIDENCE
    ):
        reasons.append("vulture_confidence_below_calibrated_threshold")
    if not signals.pyright_reported:
        reasons.append("pyright_did_not_report")
    if not signals.vulture_complete or not signals.pyright_complete:
        reasons.append("provider_measurement_incomplete")
    if not signals.providers_aligned:
        reasons.append("provider_domains_not_aligned")
    if signals.coverage_status != "complete":
        reasons.append(f"coverage_{signals.coverage_status}_does_not_support_usage")
    return tuple(sorted(set(reasons)))


def _classify(signals: UnusedEvidenceSignals) -> tuple[UnusedState, tuple[str, ...]]:
    _validate_signals(signals)
    confirmed_usage = _confirmed_usage_reasons(signals)
    if confirmed_usage:
        return "explained_usage", confirmed_usage
    dynamic_usage = _dynamic_usage_reasons(signals)
    if dynamic_usage:
        return "dynamic_usage_possible", dynamic_usage
    if _has_high_unused_consensus(signals):
        return "probable_unused_high_consensus", (
            "vulture_high_confidence",
            "pyright_reported_unused",
            "no_observed_usage_or_dynamic_contract",
        )
    return "insufficient_evidence", _insufficient_evidence_reasons(signals)


def classify_unused_candidate(signals: UnusedEvidenceSignals) -> UnusedState:
    """Classify one candidate with evidence precedence and no mutation authority."""

    return _classify(signals)[0]


def _sample_signature(samples: Sequence[UnusedCalibrationSample], dataset_id: str) -> str:
    payload = {
        "dataset_id": dataset_id,
        "samples": [asdict(item) for item in sorted(samples, key=lambda item: item.sample_id)],
    }
    return "unused-calibration-v1:xxh3_128:" + fingerprint_text(canonical_json(payload)).xxh3_128


def evaluate_unused_calibration(
    samples: Sequence[UnusedCalibrationSample],
    *,
    dataset_id: str = "neocortex-unused-calibration-custom/v1",
) -> UnusedCalibrationReport:
    """Evaluate precision, recall and abstention on an explicit labeled set."""

    if not dataset_id or len(dataset_id) > CODE_UNUSED_TEXT_LIMIT:
        raise ValueError("unused calibration dataset id is invalid")
    if len(samples) > CODE_UNUSED_SAMPLE_LIMIT:
        raise ValueError("unused calibration sample bound exceeded")
    ordered = tuple(sorted(samples, key=lambda item: item.sample_id))
    if any(not item.sample_id or len(item.sample_id) > CODE_UNUSED_TEXT_LIMIT for item in ordered):
        raise ValueError("unused calibration sample id is invalid")
    if len({item.sample_id for item in ordered}) != len(ordered):
        raise ValueError("unused calibration sample id is duplicated")
    counts: Counter[str] = Counter()
    for sample in ordered:
        state = classify_unused_candidate(sample.signals)
        positive = state == "probable_unused_high_consensus"
        negative = state == "explained_usage"
        if positive:
            counts["true_positive" if sample.label == "unused" else "false_positive"] += 1
        elif negative:
            counts["false_negative" if sample.label == "unused" else "true_negative"] += 1
        else:
            counts["abstained_positive" if sample.label == "unused" else "abstained_negative"] += 1
    positives = sum(item.label == "unused" for item in ordered)
    negatives = len(ordered) - positives
    precision_denominator = counts["true_positive"] + counts["false_positive"]
    recall_denominator = positives
    abstention_denominator = len(ordered)
    abstained = counts["abstained_positive"] + counts["abstained_negative"]
    return UnusedCalibrationReport(
        _sample_signature(ordered, dataset_id),
        dataset_id,
        len(ordered),
        positives,
        negatives,
        counts["true_positive"],
        counts["false_positive"],
        counts["true_negative"],
        counts["false_negative"],
        counts["abstained_positive"],
        counts["abstained_negative"],
        None if precision_denominator == 0 else counts["true_positive"] / precision_denominator,
        None if recall_denominator == 0 else counts["true_positive"] / recall_denominator,
        None if abstention_denominator == 0 else abstained / abstention_denominator,
        precision_denominator,
        recall_denominator,
        abstention_denominator,
    )


@dataclass(frozen=True, slots=True)
class _CurrentSymbol:
    symbol_id: int
    version_id: int
    parent_symbol_id: int | None
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    path_observed: str


@dataclass(slots=True)
class _CandidateAccumulator:
    version_id: int
    relative_path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    symbol: _CurrentSymbol | None
    vulture_findings: list[ExternalProviderFinding]
    pyright_findings: list[ExternalProviderFinding]


@dataclass(frozen=True, slots=True)
class _SourceObservations:
    exported_names: frozenset[str]
    decorated: Mapping[tuple[str, int], tuple[str, ...]]
    registry_names: frozenset[str]
    callback_names: frozenset[str]
    protocol_members: frozenset[tuple[str, int]]


@dataclass(frozen=True, slots=True)
class _GraphObservation:
    references: int = 0
    calls: int = 0
    imports: int = 0
    reexported: bool = False
    outgoing_decorators: tuple[str, ...] = ()
    protocol: bool = False


_PYRIGHT_UNUSED_PREFIX = "reportunused"
_QUOTED_NAME = re.compile(r'["\']([^"\']+)["\']')
_SPECIAL_RUNTIME_NAMES = frozenset(
    {
        "__aenter__",
        "__aexit__",
        "__aiter__",
        "__anext__",
        "__call__",
        "__dir__",
        "__enter__",
        "__exit__",
        "__getattr__",
        "__getattribute__",
        "__iter__",
        "__len__",
        "__next__",
    }
)
_REGISTRY_WORDS = ("register", "registry", "route", "command", "plugin")
_CALLBACK_WORDS = ("callback", "handler", "hook", "listener", "signal")


def _bounded_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > CODE_UNUSED_TEXT_LIMIT:
        raise ValueError(f"unused {label} is invalid")
    return value


def _finding_name(finding: ExternalProviderFinding, provider_id: str) -> str | None:
    if provider_id == VULTURE_UNUSED_PROVIDER_ID:
        value = finding.metadata.get("symbol_name")
        return value if isinstance(value, str) and value else None
    if not finding.code.casefold().startswith(_PYRIGHT_UNUSED_PREFIX):
        return None
    match = _QUOTED_NAME.search(finding.message)
    return None if match is None else match.group(1)


def _finding_kind(finding: ExternalProviderFinding, provider_id: str) -> str:
    if provider_id == VULTURE_UNUSED_PROVIDER_ID:
        value = finding.metadata.get("symbol_kind")
        return str(value) if isinstance(value, str) and value else "unknown"
    code = finding.code.casefold()
    for token, kind in (
        ("function", "function"),
        ("class", "class"),
        ("import", "import"),
        ("variable", "variable"),
        ("expression", "expression"),
    ):
        if token in code:
            return kind
    return "unknown"


def _canonical_kind(value: str) -> str:
    normalized = value.casefold()
    if normalized in {"function", "method", "property", "nested_function"}:
        return "callable"
    if normalized in {"variable", "attribute", "class_variable", "module_variable"}:
        return "variable"
    return normalized


def _current_symbols(connection: sqlite3.Connection) -> tuple[_CurrentSymbol, ...]:
    rows = connection.execute(
        """SELECT s.symbol_id,s.version_id,s.parent_symbol_id,s.kind,s.name,
        s.qualified_name,s.start_line,s.end_line,v.path_observed
        FROM symbols s JOIN file_versions v ON v.version_id=s.version_id
        JOIN files f ON f.current_version_id=v.version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        ORDER BY s.version_id,s.start_line,s.symbol_id LIMIT ?""",
        (CODE_UNUSED_SYMBOL_LIMIT + 1,),
    ).fetchall()
    if len(rows) > CODE_UNUSED_SYMBOL_LIMIT:
        raise ValueError("unused current symbol bound exceeded")
    return tuple(
        _CurrentSymbol(
            int(row["symbol_id"]),
            int(row["version_id"]),
            None if row["parent_symbol_id"] is None else int(row["parent_symbol_id"]),
            _bounded_text(row["kind"], label="symbol kind"),
            _bounded_text(row["name"], label="symbol name"),
            _bounded_text(row["qualified_name"], label="qualified symbol"),
            int(row["start_line"]),
            int(row["end_line"]),
            _bounded_text(row["path_observed"], label="symbol path"),
        )
        for row in rows
    )


def _resolve_symbol(
    finding: ExternalProviderFinding,
    name: str,
    symbols_by_owner: Mapping[tuple[int, str], tuple[_CurrentSymbol, ...]],
) -> _CurrentSymbol | None:
    matches = symbols_by_owner.get((finding.version_id, name.casefold()), ())
    if not matches:
        return None

    def distance(item: _CurrentSymbol) -> int:
        if item.start_line <= finding.start_line <= item.end_line:
            return 0
        if finding.start_line <= item.start_line <= finding.end_line:
            return 0
        return min(
            abs(item.start_line - finding.start_line),
            abs(item.end_line - finding.start_line),
        )

    ordered = sorted(matches, key=lambda item: (distance(item), item.start_line, item.symbol_id))
    nearest = distance(ordered[0])
    if nearest > 5 or (len(ordered) > 1 and distance(ordered[1]) == nearest):
        return None
    return ordered[0]


def _candidate_key(
    finding: ExternalProviderFinding,
    name: str,
    kind: str,
    symbol: _CurrentSymbol | None,
) -> tuple[object, ...]:
    if symbol is not None:
        return ("symbol", symbol.symbol_id)
    return (
        "raw",
        finding.version_id,
        finding.relative_path.replace("\\", "/").casefold(),
        name.casefold(),
        _canonical_kind(kind),
    )


def _candidate_accumulators(
    vulture: ExternalProviderEvidence,
    pyright: ExternalProviderEvidence,
    symbols: Sequence[_CurrentSymbol],
) -> tuple[_CandidateAccumulator, ...]:
    symbols_by_owner_lists: dict[tuple[int, str], list[_CurrentSymbol]] = {}
    for symbol in symbols:
        symbols_by_owner_lists.setdefault((symbol.version_id, symbol.name.casefold()), []).append(
            symbol
        )
    symbols_by_owner = {key: tuple(value) for key, value in symbols_by_owner_lists.items()}
    grouped: dict[tuple[object, ...], _CandidateAccumulator] = {}
    for provider_id, evidence in (
        (VULTURE_UNUSED_PROVIDER_ID, vulture),
        (PYRIGHT_UNUSED_PROVIDER_ID, pyright),
    ):
        if len(evidence.findings) > CODE_UNUSED_FINDING_LIMIT:
            raise ValueError("unused provider finding bound exceeded")
        for finding in evidence.findings:
            name = _finding_name(finding, provider_id)
            if name is None:
                continue
            kind = _finding_kind(finding, provider_id)
            resolved_symbol = _resolve_symbol(finding, name, symbols_by_owner)
            key = _candidate_key(finding, name, kind, resolved_symbol)
            current = grouped.get(key)
            if current is None:
                current = _CandidateAccumulator(
                    finding.version_id,
                    finding.relative_path.replace("\\", "/"),
                    name,
                    kind,
                    finding.start_line,
                    finding.end_line,
                    resolved_symbol,
                    [],
                    [],
                )
                grouped[key] = current
            current.start_line = min(current.start_line, finding.start_line)
            current.end_line = max(current.end_line, finding.end_line)
            target = (
                current.vulture_findings
                if provider_id == VULTURE_UNUSED_PROVIDER_ID
                else current.pyright_findings
            )
            target.append(finding)
    if len(grouped) > CODE_UNUSED_CANDIDATE_LIMIT:
        raise ValueError("unused candidate bound exceeded")
    return tuple(
        grouped[key]
        for key in sorted(
            grouped,
            key=lambda item: tuple(str(component).casefold() for component in item),
        )
    )


def _ast_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _ast_name(node.func)
    return ""


def _literal_all(node: ast.AST) -> frozenset[str]:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return frozenset()
    return frozenset(
        item.value
        for item in node.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    )


def _source_observations(text: str) -> _SourceObservations:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, TypeError):
        return _SourceObservations(frozenset(), {}, frozenset(), frozenset(), frozenset())
    exported: set[str] = set()
    decorated: dict[tuple[str, int], tuple[str, ...]] = {}
    registry_names: set[str] = set()
    callback_names: set[str] = set()
    protocol_members: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                exported.update(_literal_all(value) if value is not None else ())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decorators = tuple(filter(None, (_ast_name(item) for item in node.decorator_list)))
            decorated[(node.name, node.lineno)] = decorators
        if isinstance(node, ast.ClassDef):
            if any(_ast_name(base).casefold().endswith("protocol") for base in node.bases):
                for member in node.body:
                    if isinstance(
                        member,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    ):
                        protocol_members.add((member.name, member.lineno))
                    elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                        protocol_members.add((member.target.id, member.lineno))
        if not isinstance(node, ast.Call):
            continue
        callee = _ast_name(node.func).casefold()
        names = {
            item.id
            for item in (*node.args, *(keyword.value for keyword in node.keywords))
            if isinstance(item, ast.Name)
        }
        if any(word in callee for word in _REGISTRY_WORDS):
            registry_names.update(names)
        if any(word in callee for word in _CALLBACK_WORDS):
            callback_names.update(names)
    return _SourceObservations(
        frozenset(exported),
        decorated,
        frozenset(registry_names),
        frozenset(callback_names),
        frozenset(protocol_members),
    )


def _decode_source(row: sqlite3.Row) -> str | None:
    raw = row["text_zlib"]
    chars = int(row["text_chars"])
    if (
        raw is None
        or bool(row["text_truncated"])
        or chars < 0
        or chars > CODE_UNUSED_SOURCE_FILE_BYTES_LIMIT
    ):
        return None
    try:
        decompressor = zlib.decompressobj()
        payload = decompressor.decompress(bytes(raw), CODE_UNUSED_SOURCE_FILE_BYTES_LIMIT + 1)
        if len(payload) > CODE_UNUSED_SOURCE_FILE_BYTES_LIMIT or not decompressor.eof:
            return None
        text = payload.decode("utf-8")
    except (UnicodeDecodeError, ValueError, zlib.error):
        return None
    return text if len(text) == chars else None


def _source_observations_by_version(
    connection: sqlite3.Connection,
    version_ids: frozenset[int],
) -> dict[int, _SourceObservations]:
    if not version_ids:
        return {}
    rows = connection.execute(
        """SELECT v.version_id,v.text_zlib,v.text_chars,v.text_truncated
        FROM file_versions v JOIN files f ON f.current_version_id=v.version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        ORDER BY v.version_id"""
    ).fetchall()
    result: dict[int, _SourceObservations] = {}
    total_chars = 0
    for row in rows:
        version_id = int(row["version_id"])
        if version_id not in version_ids:
            continue
        total_chars += int(row["text_chars"])
        if total_chars > CODE_UNUSED_SOURCE_BYTES_LIMIT:
            raise ValueError("unused source text bound exceeded")
        text = _decode_source(row)
        if text is not None:
            result[version_id] = _source_observations(text)
    return result


def _entry_point_targets(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute(
        """SELECT v.text_zlib,v.text_chars,v.text_truncated
        FROM file_versions v JOIN files f ON f.current_version_id=v.version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND lower(replace(v.path_observed,'\\','/')) LIKE '%/pyproject.toml'
        ORDER BY v.version_id DESC LIMIT 2"""
    ).fetchall()
    targets: set[str] = set()
    for row in rows:
        text = _decode_source(row)
        if text is None:
            continue
        try:
            payload = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, TypeError):
            continue
        project = payload.get("project")
        if not isinstance(project, Mapping):
            continue
        groups: list[Mapping[str, object]] = []
        for key in ("scripts", "gui-scripts"):
            value = project.get(key)
            if isinstance(value, Mapping):
                groups.append(value)
        entry_points = project.get("entry-points")
        if isinstance(entry_points, Mapping):
            groups.extend(value for value in entry_points.values() if isinstance(value, Mapping))
        for group in groups:
            for value in group.values():
                if not isinstance(value, str):
                    continue
                target = value.split("[", 1)[0].strip().replace(":", ".")
                if target:
                    targets.add(target)
    return frozenset(targets)


def _graph_observations(
    connection: sqlite3.Connection,
    symbols: Sequence[_CurrentSymbol],
    candidate_symbol_ids: frozenset[int],
) -> dict[int, _GraphObservation]:
    if not candidate_symbol_ids:
        return {}
    paths_by_version = {item.version_id: item.path_observed for item in symbols}
    parent_by_symbol = {item.symbol_id: item.parent_symbol_id for item in symbols}
    reference_counts: Counter[int] = Counter()
    calls: Counter[int] = Counter()
    imports: Counter[int] = Counter()
    reexports: set[int] = set()
    decorators: dict[int, set[str]] = {}
    protocol_symbols: set[int] = set()
    rows = connection.execute(
        """SELECT r.reference_id,r.version_id,r.source_symbol_id,r.target_symbol_id,
        r.kind,r.name,r.target_hint FROM code_references r
        JOIN file_versions v ON v.version_id=r.version_id
        JOIN files f ON f.current_version_id=v.version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        ORDER BY r.reference_id LIMIT ?""",
        (CODE_UNUSED_REFERENCE_LIMIT + 1,),
    ).fetchall()
    if len(rows) > CODE_UNUSED_REFERENCE_LIMIT:
        raise ValueError("unused code reference bound exceeded")
    protocol_parents: set[int] = set()
    for row in rows:
        source_id = None if row["source_symbol_id"] is None else int(row["source_symbol_id"])
        target_id = None if row["target_symbol_id"] is None else int(row["target_symbol_id"])
        kind = str(row["kind"])
        name = str(row["name"])
        if source_id in candidate_symbol_ids and kind == "decorator":
            decorators.setdefault(source_id, set()).add(name)
        if kind == "inherits" and name.casefold().endswith("protocol") and source_id is not None:
            protocol_parents.add(source_id)
        if target_id not in candidate_symbol_ids or source_id == target_id:
            continue
        if kind == "call":
            calls[target_id] += 1
        elif kind in {"import", "import_binding"}:
            imports[target_id] += 1
            source_path = paths_by_version.get(int(row["version_id"]), "")
            if source_path.replace("\\", "/").casefold().endswith("/__init__.py"):
                reexports.add(target_id)
        else:
            reference_counts[target_id] += 1
    for symbol_id in candidate_symbol_ids:
        if parent_by_symbol.get(symbol_id) in protocol_parents or symbol_id in protocol_parents:
            protocol_symbols.add(symbol_id)
    return {
        symbol_id: _GraphObservation(
            reference_counts[symbol_id],
            calls[symbol_id],
            imports[symbol_id],
            symbol_id in reexports,
            tuple(sorted(decorators.get(symbol_id, ()))),
            symbol_id in protocol_symbols,
        )
        for symbol_id in candidate_symbol_ids
    }


def _coverage_evidence(
    connection: sqlite3.Connection,
    analysis_run_id: int,
) -> tuple[CoverageState, frozenset[str]]:
    coverage = read_code_coverage_analysis(connection, analysis_run_id)
    if coverage.status != "ready":
        return "missing", frozenset()
    status: CoverageState = "complete" if coverage.measurement_complete else "partial"
    observed: set[str] = set()
    for scope in coverage.symbols:
        if scope.totals.covered_lines <= 0 and not scope.executing_tests:
            continue
        for value in (scope.qualified_name, scope.symbol_key, scope.subject_key):
            if value:
                observed.add(value)
    return status, frozenset(observed)


def _coverage_matches(symbol: _CurrentSymbol | None, observed: frozenset[str]) -> bool:
    if symbol is None:
        return False
    qualified = symbol.qualified_name.casefold()
    return any(
        value.casefold() == qualified
        or value.casefold().endswith("." + qualified)
        or qualified.endswith("." + value.casefold())
        for value in observed
    )


def _module_id(relative_path: str) -> str | None:
    pure = PurePosixPath(relative_path.replace("\\", "/"))
    if pure.suffix.casefold() not in {".py", ".pyi"}:
        return None
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _is_entry_point(symbol: _CurrentSymbol | None, targets: frozenset[str]) -> bool:
    if symbol is None:
        return False
    if symbol.kind == "entrypoint":
        return True
    qualified = symbol.qualified_name.casefold()
    return any(
        target.casefold() == qualified
        or target.casefold().endswith("." + qualified)
        or qualified.endswith("." + target.casefold())
        for target in targets
    )


def _decorator_signals(
    accumulator: _CandidateAccumulator,
    graph: _GraphObservation,
    source: _SourceObservations | None,
) -> tuple[bool, bool, bool, bool]:
    decorators = set(graph.outgoing_decorators)
    if source is not None:
        for (name, line), values in source.decorated.items():
            if name == accumulator.name and abs(line - accumulator.start_line) <= 5:
                decorators.update(values)
    normalized = " ".join(decorators).casefold()
    fixture = "fixture" in normalized
    registry = any(word in normalized for word in _REGISTRY_WORDS)
    callback = any(word in normalized for word in _CALLBACK_WORDS)
    protocol = graph.protocol
    if source is not None:
        registry = registry or accumulator.name in source.registry_names
        callback = callback or accumulator.name in source.callback_names
        protocol = protocol or any(
            name == accumulator.name and abs(line - accumulator.start_line) <= 5
            for name, line in source.protocol_members
        )
    return callback, registry, fixture, protocol


def _candidate_identity(
    accumulator: _CandidateAccumulator,
    symbol: _CurrentSymbol | None,
) -> str:
    payload = {
        "path": accumulator.relative_path,
        "symbol": None if symbol is None else symbol.qualified_name,
        "name": accumulator.name,
        "kind": accumulator.kind,
        "start_line": accumulator.start_line if symbol is None else symbol.start_line,
    }
    return "code-unused-candidate-v1:xxh3_128:" + fingerprint_text(canonical_json(payload)).xxh3_128


def _materialize_candidates(
    accumulators: Sequence[_CandidateAccumulator],
    *,
    graph: Mapping[int, _GraphObservation],
    sources: Mapping[int, _SourceObservations],
    entry_points: frozenset[str],
    coverage_status: CoverageState,
    coverage_observed: frozenset[str],
    providers_ready: bool,
) -> tuple[UnusedConsensusCandidate, ...]:
    result: list[UnusedConsensusCandidate] = []
    for item in accumulators:
        symbol = item.symbol
        graph_item = graph.get(symbol.symbol_id if symbol is not None else -1, _GraphObservation())
        source = sources.get(item.version_id)
        callback, registry, fixture, protocol = _decorator_signals(item, graph_item, source)
        vulture_confidence = max(
            (finding.tool_confidence or 0.0 for finding in item.vulture_findings),
            default=None,
        )
        evidence_ids = tuple(
            sorted(
                finding.portable_finding_id
                for finding in (*item.vulture_findings, *item.pyright_findings)
            )
        )
        version_ids = {
            finding.version_id for finding in (*item.vulture_findings, *item.pyright_findings)
        }
        signals = UnusedEvidenceSignals(
            bool(item.vulture_findings),
            vulture_confidence,
            bool(item.pyright_findings),
            providers_ready,
            providers_ready,
            providers_ready and symbol is not None and len(version_ids) == 1,
            graph_item.references,
            graph_item.calls,
            graph_item.imports,
            source is not None and item.name in source.exported_names,
            graph_item.reexported,
            _is_entry_point(symbol, entry_points),
            callback,
            registry,
            fixture,
            protocol,
            item.name in _SPECIAL_RUNTIME_NAMES,
            _coverage_matches(symbol, coverage_observed),
            coverage_status,
            evidence_ids,
        )
        state, reasons = _classify(signals)
        limitations = [
            "advisory_only_no_delete_or_mutation_authority",
            "static_absence_never_proves_runtime_non_use",
        ]
        if coverage_status != "complete":
            limitations.append("coverage_scope_is_not_complete_for_non_use_inference")
        if symbol is None:
            limitations.append("candidate_symbol_identity_not_resolved")
        result.append(
            UnusedConsensusCandidate(
                _candidate_identity(item, symbol),
                item.version_id,
                None if symbol is None else symbol.symbol_id,
                item.relative_path,
                _module_id(item.relative_path),
                None if symbol is None else symbol.qualified_name,
                item.name,
                item.kind,
                item.start_line if symbol is None else symbol.start_line,
                item.end_line if symbol is None else symbol.end_line,
                state,
                tuple(
                    provider_id
                    for provider_id, findings in (
                        (PYRIGHT_UNUSED_PROVIDER_ID, item.pyright_findings),
                        (VULTURE_UNUSED_PROVIDER_ID, item.vulture_findings),
                    )
                    if findings
                ),
                signals,
                reasons,
                evidence_ids,
                tuple(limitations),
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda candidate: (
                candidate.state != "probable_unused_high_consensus",
                candidate.relative_path.casefold(),
                candidate.start_line,
                candidate.candidate_id,
            ),
        )
    )


def _calibration_signature(
    calibration: UnusedCalibrationReport,
    holdout: UnusedCalibrationReport,
) -> str:
    return (
        "unused-calibration-suite-v1:xxh3_128:"
        + fingerprint_text(
            canonical_json(
                {
                    "policy_signature": CODE_UNUSED_POLICY_SIGNATURE,
                    "calibration": calibration.signature,
                    "holdout": holdout.signature,
                }
            )
        ).xxh3_128
    )


def _provider_signature(provider_rows: Sequence[object]) -> str:
    return (
        "unused-provider-suite-v1:xxh3_128:"
        + fingerprint_text(canonical_json({"providers": list(provider_rows)})).xxh3_128
    )


def build_unused_provider_signature(
    providers: Sequence[UnusedProviderStatus],
) -> str:
    """Build the exact receipt for the two normalized unused-code providers."""

    by_id = {provider.provider_id: provider for provider in providers}
    rows = []
    for provider_id in (PYRIGHT_UNUSED_PROVIDER_ID, VULTURE_UNUSED_PROVIDER_ID):
        provider = by_id.get(provider_id)
        if provider is None:
            raise ValueError("unused provider receipt is incomplete")
        rows.append(
            {
                "provider_id": provider.provider_id,
                "provider_schema": provider.source_provider_schema,
                "tool_name": provider.source_tool_name,
                "tool_version": provider.source_tool_version,
                "comparability_signature": provider.source_comparability_signature,
            }
        )
    return _provider_signature(rows)


def build_unused_analysis(
    candidates: Sequence[UnusedConsensusCandidate],
    *,
    providers: Sequence[UnusedProviderStatus],
    database: str = "",
    analysis_run_id: int | None = None,
    coverage_status: CoverageState = "missing",
    calibration_samples: Sequence[UnusedCalibrationSample] = (),
    holdout_samples: Sequence[UnusedCalibrationSample] = (),
) -> CodeUnusedAnalysis:
    """Build a ready analysis only from exact, validated provider receipts."""

    provider_tuple = tuple(providers)
    return analyze_code_unused(
        candidates,
        database=database,
        analysis_run_id=analysis_run_id,
        coverage_status=coverage_status,
        providers=provider_tuple,
        provider_signature=build_unused_provider_signature(provider_tuple),
        calibration_samples=calibration_samples,
        holdout_samples=holdout_samples,
        status="ready",
    )


def _evidence_signature(candidates: Sequence[UnusedConsensusCandidate]) -> str:
    rows = []
    for candidate in candidates:
        row = asdict(candidate)
        row.pop("version_id")
        row.pop("symbol_id")
        rows.append(row)
    return (
        "unused-evidence-v1:xxh3_128:"
        + fingerprint_text(canonical_json({"candidates": rows})).xxh3_128
    )


def _calibration_gates(
    calibration: UnusedCalibrationReport,
    holdout: UnusedCalibrationReport,
) -> tuple[UnusedGateEvaluation, ...]:
    gates: list[UnusedGateEvaluation] = []
    for name, report in (("calibration", calibration), ("holdout", holdout)):
        if report.precision is None:
            gates.append(
                UnusedGateEvaluation(
                    f"{name}_probable_unused_precision",
                    "not_evaluated",
                    "no_probable_unused_predictions",
                )
            )
        else:
            gates.append(
                UnusedGateEvaluation(
                    f"{name}_probable_unused_precision",
                    "passed" if report.false_positive == 0 else "failed",
                    "zero_false_positive_fixture"
                    if report.false_positive == 0
                    else "false_positive_fixture_observed",
                )
            )
        gates.append(
            UnusedGateEvaluation(
                f"{name}_probable_unused_recall_observed",
                "passed" if report.true_positive > 0 else "failed",
                "at_least_one_labeled_unused_detected"
                if report.true_positive > 0
                else "no_labeled_unused_detected",
            )
        )
    return tuple(gates)


def analyze_code_unused(
    candidates: Sequence[UnusedConsensusCandidate],
    *,
    database: str = "",
    analysis_run_id: int | None = None,
    coverage_status: CoverageState = "missing",
    providers: Sequence[UnusedProviderStatus] = (),
    provider_signature: str = "",
    calibration_samples: Sequence[UnusedCalibrationSample] = (),
    holdout_samples: Sequence[UnusedCalibrationSample] = (),
    status: AnalysisState = "abstained",
    reason: str | None = None,
) -> CodeUnusedAnalysis:
    """Consolidate one bounded, immutable and explicitly advisory analysis."""

    if len(candidates) > CODE_UNUSED_CANDIDATE_LIMIT:
        raise ValueError("unused candidate bound exceeded")
    calibration = evaluate_unused_calibration(
        calibration_samples or DEFAULT_CALIBRATION_SAMPLES,
        dataset_id="neocortex-unused-calibration/v1",
    )
    holdout = evaluate_unused_calibration(
        holdout_samples or DEFAULT_HOLDOUT_SAMPLES,
        dataset_id="neocortex-unused-holdout/v1",
    )
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.state != "probable_unused_high_consensus",
                item.relative_path.casefold(),
                item.start_line,
                item.candidate_id,
            ),
        )
    )
    counts: Counter[str] = Counter(candidate.state for candidate in ordered)
    counts["total"] = len(ordered)
    for state_name in (
        "explained_usage",
        "dynamic_usage_possible",
        "insufficient_evidence",
        "probable_unused_high_consensus",
    ):
        counts.setdefault(state_name, 0)
    limitations = (
        "unused_analysis_is_advisory_and_has_zero_mutation_authority",
        "probable_unused_high_consensus_requires_human_confirmation",
        "coverage_observation_can_explain_usage_but_absence_never_proves_non_use",
        "dynamic_imports_callbacks_registries_and_reflection_may_remain_unobserved",
    )
    effective_reason = reason
    if status == "abstained" and effective_reason is None:
        effective_reason = "unused_provider_receipt_not_supplied"
    return CodeUnusedAnalysis(
        database,
        analysis_run_id,
        status,
        effective_reason,
        CODE_UNUSED_POLICY_SIGNATURE,
        provider_signature,
        _evidence_signature(ordered),
        _calibration_signature(calibration, holdout),
        coverage_status,
        tuple(providers),
        ordered,
        dict(counts),
        calibration,
        holdout,
        _calibration_gates(calibration, holdout),
        limitations,
    )


def _provider_statuses(
    suite: object,
    evidence: Mapping[str, ExternalProviderEvidence],
) -> tuple[tuple[UnusedProviderStatus, ...], str, bool]:
    suite_rows = {item.provider_id: item for item in getattr(suite, "providers", ())}
    statuses: list[UnusedProviderStatus] = []
    signature_rows: list[dict[str, object]] = []
    ready = True
    for provider_id in (PYRIGHT_UNUSED_PROVIDER_ID, VULTURE_UNUSED_PROVIDER_ID):
        suite_status = suite_rows.get(provider_id)
        provider = evidence.get(provider_id)
        if suite_status is None or provider is None:
            state: ProviderState = "missing"
            reason = "provider_missing"
            ready = False
        elif suite_status.status != "ready" or provider.status != "ready":
            state = "abstained"
            reason = provider.reason or suite_status.reason or "provider_not_ready"
            ready = False
        else:
            state = "ready"
            reason = None
        statuses.append(
            UnusedProviderStatus(
                provider_id,
                state,
                reason,
                None if provider is None else provider.tool_run_id,
                None if provider is None else provider.effective_tool_run_id,
                0 if provider is None else len(provider.findings),
                0 if suite_status is None else suite_status.eligible_files,
                0 if suite_status is None else suite_status.covered_files,
                "comparable"
                if suite_status is not None
                and suite_status.status == "ready"
                and bool(suite_status.comparability_signature)
                else "not_comparable",
                None if suite_status is None else suite_status.provider_schema,
                None if suite_status is None else suite_status.tool_name,
                None if suite_status is None else suite_status.tool_version,
                None if suite_status is None else suite_status.comparability_signature,
            )
        )
        signature_rows.append(
            {
                "provider_id": provider_id,
                "provider_schema": None if suite_status is None else suite_status.provider_schema,
                "tool_name": None if suite_status is None else suite_status.tool_name,
                "tool_version": None if suite_status is None else suite_status.tool_version,
                "comparability_signature": None
                if suite_status is None
                else suite_status.comparability_signature,
            }
        )
    return tuple(statuses), _provider_signature(signature_rows), ready


def read_code_unused_analysis(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    calibration_samples: Sequence[UnusedCalibrationSample] = (),
    holdout_samples: Sequence[UnusedCalibrationSample] = (),
    database: str = "",
) -> CodeUnusedAnalysis:
    """Read and correlate only the latest published provider projections."""

    try:
        suite = read_external_evidence_suite(
            connection,
            analysis_run_id,
            enforce_current_runtime=True,
            provider_ids=(PYRIGHT_UNUSED_PROVIDER_ID, VULTURE_UNUSED_PROVIDER_ID),
        )
        evidence = read_external_provider_evidence(
            connection,
            analysis_run_id,
            provider_ids=(PYRIGHT_UNUSED_PROVIDER_ID, VULTURE_UNUSED_PROVIDER_ID),
        )
        providers, provider_signature, providers_ready = _provider_statuses(suite, evidence)
        if not providers_ready:
            return analyze_code_unused(
                (),
                database=database,
                analysis_run_id=analysis_run_id,
                providers=providers,
                provider_signature=provider_signature,
                calibration_samples=calibration_samples,
                holdout_samples=holdout_samples,
                status="abstained",
                reason="unused_static_provider_pair_not_ready",
            )
        vulture = evidence[VULTURE_UNUSED_PROVIDER_ID]
        pyright = evidence[PYRIGHT_UNUSED_PROVIDER_ID]
        symbols = _current_symbols(connection)
        accumulators = _candidate_accumulators(vulture, pyright, symbols)
        candidate_symbol_ids = frozenset(
            item.symbol.symbol_id for item in accumulators if item.symbol is not None
        )
        graph = _graph_observations(connection, symbols, candidate_symbol_ids)
        source_versions = frozenset(item.version_id for item in accumulators)
        sources = _source_observations_by_version(connection, source_versions)
        entry_points = _entry_point_targets(connection)
        coverage_status, coverage_observed = _coverage_evidence(connection, analysis_run_id)
        candidates = _materialize_candidates(
            accumulators,
            graph=graph,
            sources=sources,
            entry_points=entry_points,
            coverage_status=coverage_status,
            coverage_observed=coverage_observed,
            providers_ready=True,
        )
        return analyze_code_unused(
            candidates,
            database=database,
            analysis_run_id=analysis_run_id,
            coverage_status=coverage_status,
            providers=providers,
            provider_signature=provider_signature,
            calibration_samples=calibration_samples,
            holdout_samples=holdout_samples,
            status="ready",
        )
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
        return analyze_code_unused(
            (),
            database=database,
            analysis_run_id=analysis_run_id,
            calibration_samples=calibration_samples,
            holdout_samples=holdout_samples,
            status="abstained",
            reason=f"unused_evidence_unavailable:{type(exc).__name__}:{exc}",
        )


__all__ = [
    "CODE_UNUSED_POLICY_SIGNATURE",
    "CODE_UNUSED_SCHEMA",
    "DEFAULT_CALIBRATION_SAMPLES",
    "DEFAULT_HOLDOUT_SAMPLES",
    "PYRIGHT_UNUSED_PROVIDER_ID",
    "VULTURE_UNUSED_PROVIDER_ID",
    "CodeUnusedAnalysis",
    "UnusedCalibrationReport",
    "UnusedCalibrationSample",
    "UnusedConsensusCandidate",
    "UnusedEvidenceSignals",
    "UnusedGateEvaluation",
    "UnusedProviderStatus",
    "analyze_code_unused",
    "build_unused_analysis",
    "build_unused_provider_signature",
    "classify_unused_candidate",
    "evaluate_unused_calibration",
    "read_code_unused_analysis",
]
