"""Bounded Rust lexical analyzer with explicit non-parser provenance."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import re
from dataclasses import dataclass, field
from pathlib import Path

from .code_analyzer_common import (
    SourceMap,
    comparison_fingerprints,
    searchable_chunks,
)
from .code_contracts import (
    AnalysisStatus,
    CodeAnalysis,
    CodeFileInput,
    CodeRouteConfig,
    DependencyRecord,
    DiagnosticRecord,
    DiagnosticSeverity,
    MetricRecord,
    ReferenceRecord,
    SymbolRecord,
)


# region [01] Bounded lexical patterns


_ITEM = re.compile(
    r"(?m)^[ \t]*(?P<visibility>pub(?:\([^\n)]*\))?\s+)?"
    r"(?P<prefix>(?:(?:async|unsafe|const|extern(?:\s+\"[^\"]+\")?)\s+)*)"
    r"(?P<kind>fn|struct|enum|trait|type|mod|union|static|const)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_IMPL = re.compile(
    r"(?m)^[ \t]*(?P<unsafe>unsafe\s+)?impl(?:\s*<[^\n{>]*>)?\s+"
    r"(?:(?P<trait>[A-Za-z_][\w:]*)\s+for\s+)?(?P<target>[A-Za-z_][\w:]*)"
)
_MACRO_RULES = re.compile(r"(?m)^[ \t]*(?:pub\s+)?macro_rules!\s*(?P<name>\w+)")
_USE = re.compile(r"(?m)^[ \t]*(?:pub\s+)?use\s+(?P<target>[^;\n]+)")
_MOD = re.compile(r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*;")
_CALL = re.compile(r"(?<![\w:])(?P<name>[A-Za-z_][\w:]*)\s*(?P<macro>!)?\s*\(")
_CONTROL_CALLS = frozenset({"if", "while", "for", "match", "loop", "return", "Some", "Ok", "Err"})
_BRANCH = re.compile(r"\b(?:if|for|while|match)\b|&&|\|\||\?")


def _matching_brace(text: str, start: int) -> int:
    """Return the first balanced closing brace after a declaration header."""

    brace = text.find("{", start, min(len(text), start + 16_384))
    if brace < 0:
        line_end = text.find("\n", start)
        return len(text) if line_end < 0 else line_end
    depth = 0
    quote: str | None = None
    escaped = False
    index = brace
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _delimiter_diagnostics(source_map: SourceMap) -> tuple[DiagnosticRecord, ...]:
    stack: list[tuple[str, int]] = []
    pairs = {')': '(', ']': '[', '}': '{'}
    quote: str | None = None
    escaped = False
    for index, character in enumerate(source_map.text):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "([{":
            stack.append((character, index))
        elif character in ")]}":
            if not stack or stack[-1][0] != pairs[character]:
                return (
                    DiagnosticRecord(
                        source="neocortex-rust-lexical",
                        code="rust_unbalanced_delimiter",
                        severity=DiagnosticSeverity.ERROR,
                        message=f"unmatched closing delimiter {character!r}",
                        source_range=source_map.offset_range(index, index + 1),
                        tool_name="rust-lexical",
                        tool_version="1",
                        confirmed=True,
                        metadata={"scope": "lexical-only"},
                    ),
                )
            stack.pop()
    if stack:
        character, index = stack[-1]
        return (
            DiagnosticRecord(
                source="neocortex-rust-lexical",
                code="rust_unclosed_delimiter",
                severity=DiagnosticSeverity.ERROR,
                message=f"unclosed delimiter {character!r}",
                source_range=source_map.offset_range(index, index + 1),
                tool_name="rust-lexical",
                tool_version="1",
                confirmed=True,
                metadata={"scope": "lexical-only"},
            ),
        )
    return ()


# endregion [01]


# region [02] Analyzer


_ImplSpan = tuple[int, int, str, str | None]


@dataclass(slots=True)
class _RustEvidence:
    impl_spans: list[_ImplSpan] = field(default_factory=list)
    symbols: list[SymbolRecord] = field(default_factory=list)
    references: list[ReferenceRecord] = field(default_factory=list)
    dependencies: list[DependencyRecord] = field(default_factory=list)
    metrics: list[MetricRecord] = field(default_factory=list)
    diagnostics: list[DiagnosticRecord] = field(default_factory=list)


def _collect_impl_evidence(
    text: str,
    source_map: SourceMap,
    module: str,
    evidence: _RustEvidence,
) -> None:
    for match in _IMPL.finditer(text):
        end = _matching_brace(text, match.start())
        target = match.group("target")
        trait = match.group("trait")
        evidence.impl_spans.append((match.start(), end, target, trait))
        name = f"impl {trait + ' for ' if trait else ''}{target}"
        qualified_name = f"{module}.{name}"
        evidence.symbols.append(
            SymbolRecord(
                kind="impl",
                name=name,
                qualified_name=qualified_name,
                signature=match.group(0).strip(),
                source_range=source_map.offset_range(match.start(), end),
                visibility=None,
                confirmed=False,
                metadata={"trait": trait, "target": target, "evidence": "lexical"},
            )
        )
        if trait:
            evidence.references.append(
                ReferenceRecord(
                    "implements_trait",
                    trait,
                    source_map.offset_range(match.start(), match.end()),
                    qualified_name,
                    trait,
                    False,
                    0.8,
                    "rust-lexical:impl-header",
                )
            )


def _containing_impl(
    impl_spans: list[_ImplSpan],
    offset: int,
) -> _ImplSpan | None:
    return next(
        (span for span in impl_spans if span[0] < offset < span[1]),
        None,
    )


def _rust_item_identity(
    module: str,
    kind: str,
    name: str,
    containing_impl: _ImplSpan | None,
) -> tuple[str, str | None, str]:
    symbol_kind = "method" if kind == "fn" and containing_impl else kind
    parent = None
    qualified = f"{module}.{name}"
    if containing_impl is not None:
        parent_name = f"impl {containing_impl[2]}"
        parent = f"{module}.{parent_name}"
        qualified = f"{parent}.{name}"
    return symbol_kind, parent, qualified


def _rust_item_signature(text: str, match: re.Match[str], end: int) -> str:
    signature_end = text.find("{", match.end(), min(len(text), match.end() + 8192))
    if signature_end < 0 or signature_end > end:
        signature_end = min(
            end,
            text.find("\n", match.end()) if "\n" in text[match.end() :] else end,
        )
    return text[match.start() : signature_end].strip()[:4096]


def _rust_item_complexity(
    *,
    text: str,
    match: re.Match[str],
    end: int,
    kind: str,
    qualified: str,
    source_map: SourceMap,
    config: CodeRouteConfig,
    analyzer_id: str,
    analyzer_version: str,
    evidence: _RustEvidence,
) -> int | None:
    if kind != "fn":
        return None
    complexity = 1 + len(_BRANCH.findall(text[match.end() : end]))
    evidence.metrics.append(
        MetricRecord(
            "lexical_complexity",
            complexity,
            qualified,
            False,
            "rust-lexical-v1",
        )
    )
    if complexity >= config.complexity_warning:
        evidence.diagnostics.append(
            DiagnosticRecord(
                source=analyzer_id,
                code="high_complexity_inferred",
                severity=DiagnosticSeverity.WARNING,
                message=f"{qualified} has lexical complexity {complexity}",
                source_range=source_map.offset_range(match.start(), end),
                tool_name="rust-lexical",
                tool_version=analyzer_version,
                confirmed=False,
                confidence=0.65,
                metadata={"threshold": config.complexity_warning},
            )
        )
    return complexity


def _collect_item_evidence(
    text: str,
    source_map: SourceMap,
    module: str,
    config: CodeRouteConfig,
    analyzer_id: str,
    analyzer_version: str,
    evidence: _RustEvidence,
) -> None:
    for match in _ITEM.finditer(text):
        kind = match.group("kind")
        name = match.group("name")
        end = _matching_brace(text, match.start())
        containing_impl = _containing_impl(evidence.impl_spans, match.start())
        symbol_kind, parent, qualified = _rust_item_identity(
            module,
            kind,
            name,
            containing_impl,
        )
        complexity = _rust_item_complexity(
            text=text,
            match=match,
            end=end,
            kind=kind,
            qualified=qualified,
            source_map=source_map,
            config=config,
            analyzer_id=analyzer_id,
            analyzer_version=analyzer_version,
            evidence=evidence,
        )
        evidence.symbols.append(
            SymbolRecord(
                kind=symbol_kind,
                name=name,
                qualified_name=qualified,
                signature=_rust_item_signature(text, match, end),
                source_range=source_map.offset_range(match.start(), end),
                parent_qualified_name=parent,
                visibility="public" if match.group("visibility") else "private",
                confirmed=False,
                complexity=complexity,
                metadata={
                    "prefix": match.group("prefix").strip(),
                    "evidence": "rust-lexical-item",
                },
            )
        )


def _collect_macro_evidence(
    text: str,
    source_map: SourceMap,
    module: str,
    evidence: _RustEvidence,
) -> None:
    for match in _MACRO_RULES.finditer(text):
        name = match.group("name")
        end = _matching_brace(text, match.start())
        evidence.symbols.append(
            SymbolRecord(
                "macro",
                name,
                f"{module}.{name}",
                f"macro_rules! {name}",
                source_map.offset_range(match.start(), end),
                visibility="private",
                confirmed=False,
                metadata={"evidence": "rust-lexical-macro"},
            )
        )


def _collect_use_evidence(
    text: str,
    source_map: SourceMap,
    module: str,
    evidence: _RustEvidence,
) -> None:
    for match in _USE.finditer(text):
        target = match.group("target").strip()
        root = target.split("::", 1)[0].lstrip(":")
        source_range = source_map.offset_range(match.start(), match.end())
        evidence.references.append(
            ReferenceRecord(
                "import",
                target,
                source_range,
                module,
                target,
                False,
                0.9,
                "rust-lexical:use",
            )
        )
        if root not in {"crate", "self", "super", "std", "core", "alloc"}:
            evidence.dependencies.append(
                DependencyRecord(
                    root,
                    "rust_use",
                    source_range=source_range,
                    confirmed=False,
                    confidence=0.8,
                    evidence="rust-lexical:use-root",
                )
            )


def _collect_module_evidence(
    text: str,
    source_map: SourceMap,
    module: str,
    evidence: _RustEvidence,
) -> None:
    for match in _MOD.finditer(text):
        evidence.references.append(
            ReferenceRecord(
                "module_declaration",
                match.group("name"),
                source_map.offset_range(match.start(), match.end()),
                module,
                match.group("name"),
                False,
                0.9,
                "rust-lexical:mod",
            )
        )


def _collect_call_evidence(
    text: str,
    source_map: SourceMap,
    module: str,
    evidence: _RustEvidence,
) -> None:
    definition_starts = {item.source_range.start_byte for item in evidence.symbols}
    for match in _CALL.finditer(text):
        name = match.group("name")
        if name in _CONTROL_CALLS:
            continue
        source_range = source_map.offset_range(match.start(), match.end())
        if source_range.start_byte in definition_starts:
            continue
        evidence.references.append(
            ReferenceRecord(
                "macro_call" if match.group("macro") else "call",
                name,
                source_range,
                module,
                name,
                False,
                0.6,
                "rust-lexical:call-shape",
            )
        )


def _rust_structure(symbols: list[SymbolRecord]) -> str:
    return "\n".join(
        f"{item.kind}\0{item.qualified_name}\0{item.signature or ''}"
        for item in symbols
    )


def _append_rust_summary_metrics(
    source_map: SourceMap,
    evidence: _RustEvidence,
) -> None:
    evidence.metrics.extend(
        (
            MetricRecord("line_count", len(source_map.lines), provenance="rust-lexical"),
            MetricRecord(
                "symbol_count",
                len(evidence.symbols),
                provenance="rust-lexical",
            ),
            MetricRecord(
                "reference_count",
                len(evidence.references),
                provenance="rust-lexical",
            ),
        )
    )


class RustAnalyzer:
    """Extract Rust items without pretending that regex evidence is an AST."""

    analyzer_id = "neocortex-rust-lexical"
    analyzer_version = "1"
    languages = frozenset({"rust"})

    def analyze(self, source: CodeFileInput, config: CodeRouteConfig) -> CodeAnalysis:
        text = source.text
        source_map = SourceMap.build(text)
        module = Path(source.snapshot.path).stem
        evidence = _RustEvidence(
            diagnostics=list(_delimiter_diagnostics(source_map)),
        )
        _collect_impl_evidence(text, source_map, module, evidence)
        _collect_item_evidence(
            text,
            source_map,
            module,
            config,
            self.analyzer_id,
            self.analyzer_version,
            evidence,
        )
        _collect_macro_evidence(text, source_map, module, evidence)
        _collect_use_evidence(text, source_map, module, evidence)
        _collect_module_evidence(text, source_map, module, evidence)
        _collect_call_evidence(text, source_map, module, evidence)
        fingerprints = comparison_fingerprints(
            source.raw_bytes,
            source.text,
            "rust",
            structure=_rust_structure(evidence.symbols),
        )
        _append_rust_summary_metrics(source_map, evidence)
        return CodeAnalysis(
            input=source,
            status=(
                AnalysisStatus.PARTIAL
                if evidence.diagnostics
                else AnalysisStatus.TEXT_ONLY
            ),
            analyzer_id=self.analyzer_id,
            analyzer_version=self.analyzer_version,
            parser_kind="rust-lexical-fallback",
            symbols=tuple(evidence.symbols),
            references=tuple(evidence.references),
            dependencies=tuple(evidence.dependencies),
            diagnostics=tuple(evidence.diagnostics),
            metrics=tuple(evidence.metrics),
            chunks=searchable_chunks(source.text, config.chunk_chars),
            provenance={
                "parser": None,
                "lexical_analyzer": self.analyzer_version,
                "syntax_confirmed": False,
                "cargo_tools_executed": False,
            },
            **fingerprints,
        )


# endregion [02]


__all__ = ["RustAnalyzer"]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_rust")
