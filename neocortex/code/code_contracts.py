"""Typed contracts for incremental source-code intelligence.

The code route keeps exact observations separate from inferred structure.  Every
record therefore carries explicit confirmation, confidence, range and provenance
fields instead of promoting a parser or heuristic result to permanent truth.
"""

from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from neocortex.deduplication import FileSnapshot

from neocortex.safety.route_filters import CandidateSelection
from .code_retention import DEFAULT_CODE_RETENTION_POLICY, CodeRetentionPolicy
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

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
    explicit_project_roots: tuple[Path, ...] = ()
    selection: CandidateSelection = field(default_factory=CandidateSelection)
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
]
