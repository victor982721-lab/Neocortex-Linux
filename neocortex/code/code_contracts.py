"""Typed contracts for incremental source-code intelligence.

The code route keeps exact observations separate from inferred structure.  Every
record therefore carries explicit confirmation, confidence, range and provenance
fields instead of promoting a parser or heuristic result to permanent truth.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from typing import Literal, Protocol

from neocortex.deduplication import FileSnapshot

from neocortex.safety.route_filters import CandidateSelection
from .code_retention import DEFAULT_CODE_RETENTION_POLICY, CodeRetentionPolicy
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

DEEP_CONFIGURATION_SCHEMA = "neocortex.code-deep-configuration/v2"
LEGACY_DEEP_CONFIGURATION_SCHEMA = "neocortex.code-deep-configuration/v1"
DEFAULT_DEEP_MAX_TESTS = 3000
DEFAULT_DEEP_TIME_BUDGET_SECONDS = 600
DEFAULT_DEEP_SHARD_SIZE = 20
DEFAULT_DEEP_MUTATION_MAX_MUTANTS = 20
DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS = 30
DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS = 600
MAX_DEEP_TEST_SELECTORS = 2_000
MAX_DEEP_TEST_SELECTOR_WIRE_BYTES = 180 * 1024


def normalize_deep_test_selectors(values: Sequence[str]) -> tuple[str, ...]:
    """Validate and canonicalize relative pytest paths and node identifiers."""

    if len(values) > MAX_DEEP_TEST_SELECTORS:
        raise ValueError("too many deep test selectors")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
            or len(value.encode("utf-8")) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("deep test selector is malformed")
        selector = value.replace("\\", "/")
        path_text, *node_parts = selector.split("::")
        components = path_text.split("/")
        if (
            path_text.startswith("/")
            or re.match(r"^[A-Za-z]:", path_text)
            or ":" in path_text
            or any(component in {"", ".", ".."} for component in components)
            or components[0].casefold() != "tests"
            or not components[-1].casefold().startswith("test_")
            or not components[-1].casefold().endswith(".py")
            or any(not part or part.strip() != part for part in node_parts)
        ):
            raise ValueError(
                "deep test selector must be a relative tests/test_*.py path or node id"
            )
        canonical = path_text + "".join(f"::{part}" for part in node_parts)
        identity = canonical.casefold()
        if identity in seen:
            raise ValueError("deep test selectors contain a duplicate")
        seen.add(identity)
        normalized.append(canonical)
    wire_bytes = sum(
        len(selector.encode("utf-8")) + len("--deep-test-selector") + 8 for selector in normalized
    )
    if wire_bytes > MAX_DEEP_TEST_SELECTOR_WIRE_BYTES:
        raise ValueError("deep test selectors exceed the completion-manifest wire bound")
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def normalize_deep_mutation_target(value: str | None) -> str | None:
    """Validate and canonicalize one root-relative Python mutation target."""

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("deep mutation target is malformed")
    target = value.replace("\\", "/")
    components = target.split("/")
    if (
        target.startswith("/")
        or re.match(r"^[A-Za-z]:", target)
        or ":" in target
        or any(component in {"", ".", ".."} for component in components)
        or not components[-1].casefold().endswith(".py")
    ):
        raise ValueError("deep mutation target must be a root-relative Python path")
    return target


def normalize_deep_mutation_symbol(value: str | None) -> str | None:
    """Validate one optional Python symbol or qualified name."""

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value) is None
    ):
        raise ValueError("deep mutation symbol must be a Python qualified name")
    return value


def deep_configuration_payload(
    *,
    analysis_profile: str,
    test_selectors: Sequence[str],
    max_tests: int,
    time_budget_seconds: int,
    shard_size: int,
    mutation_target: str | None = None,
    mutation_symbol: str | None = None,
    mutation_max_mutants: int = DEFAULT_DEEP_MUTATION_MAX_MUTANTS,
    mutation_timeout_seconds: int = DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS,
    mutation_time_budget_seconds: int = DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS,
) -> dict[str, object]:
    """Return the separate, versioned execution contract for deep analysis."""

    selectors = normalize_deep_test_selectors(test_selectors)
    normalized_mutation_target = normalize_deep_mutation_target(mutation_target)
    normalized_mutation_symbol = normalize_deep_mutation_symbol(mutation_symbol)
    if analysis_profile not in {"protected", "trusted-static", "trusted-deep"}:
        raise ValueError("code analysis_profile is unsupported")
    if not 1 <= max_tests <= 10_000:
        raise ValueError("deep max_tests must be between 1 and 10000")
    if not 30 <= time_budget_seconds <= 900:
        raise ValueError("deep time_budget_seconds must be between 30 and 900")
    if not 1 <= shard_size <= 250:
        raise ValueError("deep shard_size must be between 1 and 250")
    if isinstance(mutation_max_mutants, bool) or not 1 <= mutation_max_mutants <= 100:
        raise ValueError("deep mutation_max_mutants must be between 1 and 100")
    if isinstance(mutation_timeout_seconds, bool) or not 1 <= mutation_timeout_seconds <= 120:
        raise ValueError("deep mutation_timeout_seconds must be between 1 and 120")
    if (
        isinstance(mutation_time_budget_seconds, bool)
        or not 10 <= mutation_time_budget_seconds <= 900
    ):
        raise ValueError("deep mutation_time_budget_seconds must be between 10 and 900")
    content_executed = analysis_profile == "trusted-deep"
    if not content_executed and (
        selectors
        or max_tests != DEFAULT_DEEP_MAX_TESTS
        or time_budget_seconds != DEFAULT_DEEP_TIME_BUDGET_SECONDS
        or shard_size != DEFAULT_DEEP_SHARD_SIZE
        or normalized_mutation_target is not None
        or normalized_mutation_symbol is not None
        or mutation_max_mutants != DEFAULT_DEEP_MUTATION_MAX_MUTANTS
        or mutation_timeout_seconds != DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS
        or mutation_time_budget_seconds != DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS
    ):
        raise ValueError("deep configuration requires trusted-deep")
    if normalized_mutation_symbol is not None and normalized_mutation_target is None:
        raise ValueError("deep mutation symbol requires a mutation target")
    if normalized_mutation_target is not None and not selectors:
        raise ValueError("deep mutation target requires explicit test selectors")
    if normalized_mutation_target is None and (
        mutation_max_mutants != DEFAULT_DEEP_MUTATION_MAX_MUTANTS
        or mutation_timeout_seconds != DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS
        or mutation_time_budget_seconds != DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS
    ):
        raise ValueError("deep mutation limits require a mutation target")
    return {
        "schema": DEEP_CONFIGURATION_SCHEMA,
        "analysis_profile": analysis_profile,
        "content_executed": content_executed,
        "suite_selection": ("selected" if selectors else "full")
        if content_executed
        else "not_applicable",
        "test_selectors": list(selectors),
        "max_tests": max_tests,
        "time_budget_seconds": time_budget_seconds,
        "shard_size": shard_size,
        "mutation_target": normalized_mutation_target,
        "mutation_symbol": normalized_mutation_symbol,
        "mutation_max_mutants": mutation_max_mutants,
        "mutation_timeout_seconds": mutation_timeout_seconds,
        "mutation_time_budget_seconds": mutation_time_budget_seconds,
    }


def _legacy_deep_configuration_payload(
    *,
    analysis_profile: str,
    test_selectors: Sequence[str],
    max_tests: int,
    time_budget_seconds: int,
    shard_size: int,
) -> dict[str, object]:
    """Reconstruct the immutable v1 payload used by historical manifests."""

    current = deep_configuration_payload(
        analysis_profile=analysis_profile,
        test_selectors=test_selectors,
        max_tests=max_tests,
        time_budget_seconds=time_budget_seconds,
        shard_size=shard_size,
    )
    return {
        "schema": LEGACY_DEEP_CONFIGURATION_SCHEMA,
        "analysis_profile": current["analysis_profile"],
        "content_executed": current["content_executed"],
        "suite_selection": current["suite_selection"],
        "test_selectors": current["test_selectors"],
        "max_tests": current["max_tests"],
        "time_budget_seconds": current["time_budget_seconds"],
        "shard_size": current["shard_size"],
    }


def deep_configuration_signature(payload: Mapping[str, object]) -> str:
    """Fingerprint one validated deep-configuration payload."""

    common_keys = {
        "schema",
        "analysis_profile",
        "content_executed",
        "suite_selection",
        "test_selectors",
        "max_tests",
        "time_budget_seconds",
        "shard_size",
    }
    schema = payload.get("schema")
    if schema == LEGACY_DEEP_CONFIGURATION_SCHEMA:
        expected_keys = common_keys
        prefix = "code-deep-v1:"
    elif schema == DEEP_CONFIGURATION_SCHEMA:
        expected_keys = common_keys | {
            "mutation_target",
            "mutation_symbol",
            "mutation_max_mutants",
            "mutation_timeout_seconds",
            "mutation_time_budget_seconds",
        }
        prefix = "code-deep-v2:"
    else:
        raise ValueError("deep configuration schema is unsupported")
    if set(payload) != expected_keys:
        raise ValueError("deep configuration payload is malformed")
    return prefix + fingerprint_text(canonical_json(dict(payload))).xxh3_128


# region [01] Artifact and evidence vocabulary


class ArtifactKind(StrEnum):
    """Structural role of one textual artifact."""

    SOURCE = "source"
    SCRIPT = "script"
    CONFIG = "config"
    MANIFEST = "manifest"
    LOCK = "lock"
    DATA = "data"
    TEMPLATE = "template"
    DOCUMENTATION = "documentation"
    FIXTURE = "fixture"
    EXAMPLE = "example"
    GENERATED = "generated"
    VENDORED = "vendored"
    PLAIN_TEXT = "plain_text"


class AnalysisStatus(StrEnum):
    """Durable outcome of one versioned analysis."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    TEXT_ONLY = "text_only"
    SKIPPED_LIMIT = "skipped_limit"
    BINARY = "binary"
    ERROR = "error"


class DiagnosticSeverity(StrEnum):
    """Portable diagnostic severity independent of one tool."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ArtifactClassification:
    """Bounded artifact classification with explainable evidence."""

    language: str | None
    artifact_kind: ArtifactKind
    confidence: float
    evidence: tuple[str, ...]
    generated: bool = False
    vendored: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("artifact confidence must be between 0 and 1")
        if not self.evidence:
            raise ValueError("artifact classification requires evidence")


@dataclass(frozen=True, slots=True)
class SourceRange:
    """One half-open source range in both lines/columns and UTF-8 bytes."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    start_byte: int
    end_byte: int

    def __post_init__(self) -> None:
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("invalid source line range")
        if min(self.start_column, self.end_column, self.start_byte, self.end_byte) < 0:
            raise ValueError("source positions cannot be negative")
        if self.end_byte < self.start_byte:
            raise ValueError("invalid source byte range")


# endregion [01]


# region [02] Analyzer results


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """One definition emitted by a language analyzer."""

    kind: str
    name: str
    qualified_name: str
    signature: str | None
    source_range: SourceRange
    parent_qualified_name: str | None = None
    visibility: str | None = None
    docstring: str | None = None
    confirmed: bool = True
    complexity: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    """One import, call, reference, inheritance or implementation edge."""

    kind: str
    name: str
    source_range: SourceRange
    source_qualified_name: str | None = None
    target_hint: str | None = None
    confirmed: bool = False
    confidence: float = 0.5
    evidence: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("reference confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class DependencyRecord:
    """One declared or imported dependency."""

    name: str
    kind: str
    scope: str | None = None
    version_spec: str | None = None
    source_range: SourceRange | None = None
    confirmed: bool = True
    confidence: float = 1.0
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticRecord:
    """Versioned parser/compiler/linter or framework diagnostic."""

    source: str
    code: str
    severity: DiagnosticSeverity
    message: str
    source_range: SourceRange | None = None
    tool_name: str = "neocortex"
    tool_version: str = "unknown"
    confirmed: bool = True
    confidence: float = 1.0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("diagnostic confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class MetricRecord:
    """One numeric metric for a file or symbol scope."""

    name: str
    value: float
    symbol_qualified_name: str | None = None
    confirmed: bool = True
    provenance: str = "neocortex"


@dataclass(frozen=True, slots=True)
class CodeChunk:
    """A searchable, bounded section of source text."""

    index: int
    text: str
    source_range: SourceRange
    symbol_qualified_name: str | None = None
    kind: str = "source"


@dataclass(frozen=True, slots=True)
class ProjectHint:
    """Evidence that a file belongs to a package, crate or workspace."""

    ecosystem: str
    name: str
    root_hint: str
    confidence: float
    evidence: tuple[str, ...]
    manifest_kind: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodeFileInput:
    """Immutable decoded input passed to one analyzer."""

    snapshot: FileSnapshot
    text: str
    raw_bytes: bytes
    encoding: str
    classification: ArtifactClassification
    processing_signature: str


@dataclass(frozen=True, slots=True)
class CodeAnalysis:
    """Complete in-memory result persisted atomically for one file version."""

    input: CodeFileInput
    status: AnalysisStatus
    analyzer_id: str
    analyzer_version: str
    parser_kind: str
    text_xxh3_128: str
    text_xxh3_64_guard: str
    normalized_xxh3_128: str | None
    token_xxh3_128: str | None
    structure_xxh3_128: str | None
    raw_xxh3_128: str
    raw_xxh3_64_guard: str
    symbols: tuple[SymbolRecord, ...] = ()
    references: tuple[ReferenceRecord, ...] = ()
    dependencies: tuple[DependencyRecord, ...] = ()
    diagnostics: tuple[DiagnosticRecord, ...] = ()
    metrics: tuple[MetricRecord, ...] = ()
    chunks: tuple[CodeChunk, ...] = ()
    project_hints: tuple[ProjectHint, ...] = ()
    text_truncated: bool = False
    provenance: Mapping[str, object] = field(default_factory=dict)


class LanguageAnalyzer(Protocol):
    """Extensible analyzer contract; implementations may be loaded lazily."""

    analyzer_id: str
    analyzer_version: str
    languages: frozenset[str]

    def analyze(self, source: CodeFileInput, config: CodeRouteConfig) -> CodeAnalysis:
        """Analyze one immutable source observation without modifying it."""


# endregion [02]


# region [03] Route, search and reconstruction contracts


@dataclass(frozen=True, slots=True)
class CodeRouteConfig:
    """Bounded code-route configuration included in its processing signature."""

    state_path: Path
    dedup_path: Path
    max_file_bytes: int = 8 * 1024 * 1024
    max_text_chars: int = 4_000_000
    max_documents: int | None = None
    chunk_chars: int = 12_000
    retry_errors: bool = False
    cache_validation: Literal["metadata", "full"] = "metadata"
    candidate_scope: Literal["projects", "broad"] = "broad"
    include_generated: bool = True
    include_vendored: bool = True
    complexity_warning: int = 15
    function_lines_warning: int = 200
    external_evidence_root: Path | None = None
    explicit_project_roots: tuple[Path, ...] = ()
    analysis_profile: Literal["protected", "trusted-static", "trusted-deep"] = "protected"
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    deep_test_selectors: tuple[str, ...] = ()
    deep_max_tests: int = DEFAULT_DEEP_MAX_TESTS
    deep_time_budget_seconds: int = DEFAULT_DEEP_TIME_BUDGET_SECONDS
    deep_shard_size: int = DEFAULT_DEEP_SHARD_SIZE
    deep_mutation_target: str | None = None
    deep_mutation_symbol: str | None = None
    deep_mutation_max_mutants: int = DEFAULT_DEEP_MUTATION_MAX_MUTANTS
    deep_mutation_timeout_seconds: int = DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS
    deep_mutation_time_budget_seconds: int = DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS
    retention_policy: CodeRetentionPolicy = DEFAULT_CODE_RETENTION_POLICY

    def __post_init__(self) -> None:
        if self.max_file_bytes < 4096:
            raise ValueError("code max_file_bytes must be at least 4096")
        if self.max_text_chars < 1024:
            raise ValueError("code max_text_chars must be at least 1024")
        if self.max_documents is not None and self.max_documents < 1:
            raise ValueError("code max_documents must be positive")
        if self.cache_validation not in {"metadata", "full"}:
            raise ValueError("code cache_validation must be metadata or full")
        if self.candidate_scope not in {"projects", "broad"}:
            raise ValueError("code candidate_scope must be projects or broad")
        if not 1024 <= self.chunk_chars <= 1_000_000:
            raise ValueError("code chunk_chars must be between 1024 and 1000000")
        if self.complexity_warning < 1 or self.function_lines_warning < 1:
            raise ValueError("code diagnostic thresholds must be positive")
        if any(not root.is_absolute() for root in self.explicit_project_roots):
            raise ValueError("code explicit_project_roots must be absolute")
        normalized_selectors = normalize_deep_test_selectors(self.deep_test_selectors)
        payload = deep_configuration_payload(
            analysis_profile=self.analysis_profile,
            test_selectors=normalized_selectors,
            max_tests=self.deep_max_tests,
            time_budget_seconds=self.deep_time_budget_seconds,
            shard_size=self.deep_shard_size,
            mutation_target=self.deep_mutation_target,
            mutation_symbol=self.deep_mutation_symbol,
            mutation_max_mutants=self.deep_mutation_max_mutants,
            mutation_timeout_seconds=self.deep_mutation_timeout_seconds,
            mutation_time_budget_seconds=self.deep_mutation_time_budget_seconds,
        )
        object.__setattr__(
            self,
            "deep_test_selectors",
            normalized_selectors,
        )
        object.__setattr__(self, "deep_mutation_target", payload["mutation_target"])
        object.__setattr__(self, "deep_mutation_symbol", payload["mutation_symbol"])

    @property
    def processing_signature(self) -> str:
        payload = canonical_json(
            {
                "route": "code-route-v3",
                "max_file_bytes": self.max_file_bytes,
                "max_text_chars": self.max_text_chars,
                "chunk_chars": self.chunk_chars,
                "include_generated": self.include_generated,
                "include_vendored": self.include_vendored,
                "complexity_warning": self.complexity_warning,
                "function_lines_warning": self.function_lines_warning,
            }
        )
        return "code-v3:" + fingerprint_text(payload).xxh3_128

    @property
    def deep_configuration_payload(self) -> dict[str, object]:
        """Describe content execution separately from ordinary AST caching."""

        return deep_configuration_payload(
            analysis_profile=self.analysis_profile,
            test_selectors=self.deep_test_selectors,
            max_tests=self.deep_max_tests,
            time_budget_seconds=self.deep_time_budget_seconds,
            shard_size=self.deep_shard_size,
            mutation_target=self.deep_mutation_target,
            mutation_symbol=self.deep_mutation_symbol,
            mutation_max_mutants=self.deep_mutation_max_mutants,
            mutation_timeout_seconds=self.deep_mutation_timeout_seconds,
            mutation_time_budget_seconds=self.deep_mutation_time_budget_seconds,
        )

    @property
    def deep_configuration_signature(self) -> str:
        """Fingerprint only the trusted-deep suite and execution bounds."""

        return deep_configuration_signature(self.deep_configuration_payload)


@dataclass(frozen=True, slots=True)
class CodeRouteSummary:
    """Bounded operational counters for one integrated route run."""

    processing_signature: str = ""
    candidates: int = 0
    project_scope_enabled: int = 0
    project_roots: int = 0
    outside_project_skips: int = 0
    dependency_skips: int = 0
    generated_scope_skips: int = 0
    cache_skips: int = 0
    processed: int = 0
    cache_hits: int = 0
    cache_batches: int = 0
    text_only: int = 0
    partial: int = 0
    skipped_limit: int = 0
    binary_skips: int = 0
    generated: int = 0
    vendored: int = 0
    stale_inventory: int = 0
    symbols: int = 0
    references: int = 0
    diagnostics: int = 0
    projects: int = 0
    invalidated_versions: int = 0
    errors: int = 0
    bytes_read: int = 0
    text_chars: int = 0
    read_milliseconds: int = 0
    analyze_milliseconds: int = 0
    persist_milliseconds: int = 0
    cache_lookup_milliseconds: int = 0
    cache_update_milliseconds: int = 0
    cache_commit_milliseconds: int = 0
    graph_milliseconds: int = 0
    external_tool_runs: int = 0
    external_diagnostics: int = 0
    external_added_diagnostics: int = 0
    external_resolved_diagnostics: int = 0
    external_cache_hits: int = 0
    external_errors: int = 0
    external_milliseconds: int = 0


@dataclass(frozen=True, slots=True)
class CodeSearchQuery:
    """Complementary lexical and structural search request."""

    text: str = ""
    modes: tuple[str, ...] = ("hybrid",)
    path: str | None = None
    language: str | None = None
    project: str | None = None
    symbol: str | None = None
    diagnostic: str | None = None
    minimum_complexity: float | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("code search limit must be between 1 and 1000")
        if len(self.text) > 4096:
            raise ValueError("code search text cannot exceed 4096 characters")


@dataclass(frozen=True, slots=True)
class CodeRelationEndpoint:
    """Owner-local endpoint proven by a persisted code-state row.

    ``version_id`` and ``symbol_id`` are SQLite-local provenance, not global
    resource identities.  A file-level endpoint has no symbol fields.
    """

    version_id: int
    path: str
    symbol_id: int | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.version_id, bool) or self.version_id < 1:
            raise ValueError("code relation endpoint version_id must be positive")
        if not self.path:
            raise ValueError("code relation endpoint path cannot be empty")
        if self.symbol_id is not None and (isinstance(self.symbol_id, bool) or self.symbol_id < 1):
            raise ValueError("code relation endpoint symbol_id must be positive")
        if (self.symbol_id is None) != (self.symbol is None):
            raise ValueError("code relation endpoint symbol fields must be paired")


@dataclass(frozen=True, slots=True)
class CodeSearchRelation:
    """One typed relation observation with exact owner-local provenance.

    An unresolved edge deliberately has ``target=None``.  ``target_hint`` and
    ``name`` preserve analyzer evidence without fabricating an endpoint.
    ``confirmed``, ``confidence`` and ``provenance`` describe the analyzer's
    observation; they do not independently confirm a graph-derived binding.
    """

    family: Literal["reference", "dependency"]
    kind: str
    name: str
    source: CodeRelationEndpoint
    target: CodeRelationEndpoint | None
    target_hint: str | None
    resolved: bool
    confirmed: bool
    confidence: float
    provenance: str
    source_table: Literal["code_references", "dependencies"]
    source_row_id: int
    scope: str | None = None
    version_spec: str | None = None

    def __post_init__(self) -> None:
        expected_table = {
            "reference": "code_references",
            "dependency": "dependencies",
        }.get(self.family)
        if expected_table is None or self.source_table != expected_table:
            raise ValueError("code relation family and source table disagree")
        if not self.kind:
            raise ValueError("code relation kind cannot be empty")
        if not self.name:
            raise ValueError("code relation name cannot be empty")
        if self.resolved != (self.target is not None):
            raise ValueError("code relation resolved state and target disagree")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("code relation confidence must be between 0 and 1")
        if isinstance(self.source_row_id, bool) or self.source_row_id < 1:
            raise ValueError("code relation source_row_id must be positive")


@dataclass(frozen=True, slots=True)
class CodeSearchHit:
    """One explained search hit over the current file observation."""

    path: str
    project: str | None
    language: str | None
    artifact_kind: str
    symbol: str | None
    signature: str | None
    start_line: int
    end_line: int
    snippet: str
    score: float
    match_types: tuple[str, ...]
    evidence: tuple[str, ...]
    version_id: int
    observed_size: int
    observed_mtime_ns: int
    analysis_status: str
    relations: tuple[CodeSearchRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconstructionEntry:
    """One proposed project path and its immutable source provenance."""

    proposed_path: str
    source_path: str
    version_id: int
    xxh3_128: str
    relation: str
    confidence: float
    selected: bool
    conflict_group: str | None = None


@dataclass(frozen=True, slots=True)
class ReconstructionManifest:
    """Conceptual reconstruction; creating a tree is a separate authorized step."""

    project_id: int
    project_name: str
    ecosystem: str
    strategy: str
    entries: tuple[ReconstructionEntry, ...]
    conflicts: tuple[str, ...]
    evidence: tuple[str, ...]


def analyzer_for_language(
    analyzers: Sequence[LanguageAnalyzer], language: str | None
) -> LanguageAnalyzer:
    """Return the first exact analyzer or the declared generic fallback."""

    for analyzer in analyzers:
        if language is not None and language in analyzer.languages:
            return analyzer
    for analyzer in analyzers:
        if "*" in analyzer.languages:
            return analyzer
    raise LookupError("analyzer registry has no generic fallback")


# endregion [03]


__all__ = [
    "DEEP_CONFIGURATION_SCHEMA",
    "DEFAULT_DEEP_MAX_TESTS",
    "DEFAULT_DEEP_MUTATION_MAX_MUTANTS",
    "DEFAULT_DEEP_MUTATION_TIMEOUT_SECONDS",
    "DEFAULT_DEEP_MUTATION_TIME_BUDGET_SECONDS",
    "DEFAULT_DEEP_SHARD_SIZE",
    "DEFAULT_DEEP_TIME_BUDGET_SECONDS",
    "LEGACY_DEEP_CONFIGURATION_SCHEMA",
    "MAX_DEEP_TEST_SELECTORS",
    "MAX_DEEP_TEST_SELECTOR_WIRE_BYTES",
    "AnalysisStatus",
    "ArtifactClassification",
    "ArtifactKind",
    "CodeAnalysis",
    "CodeChunk",
    "CodeFileInput",
    "CodeRelationEndpoint",
    "CodeRouteConfig",
    "CodeRouteSummary",
    "CodeSearchHit",
    "CodeSearchQuery",
    "CodeSearchRelation",
    "DependencyRecord",
    "DiagnosticRecord",
    "DiagnosticSeverity",
    "LanguageAnalyzer",
    "MetricRecord",
    "ProjectHint",
    "ReconstructionEntry",
    "ReconstructionManifest",
    "ReferenceRecord",
    "SourceRange",
    "SymbolRecord",
    "analyzer_for_language",
    "deep_configuration_payload",
    "deep_configuration_signature",
    "normalize_deep_mutation_symbol",
    "normalize_deep_mutation_target",
    "normalize_deep_test_selectors",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_contracts")
