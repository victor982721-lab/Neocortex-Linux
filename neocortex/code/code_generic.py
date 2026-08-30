"""Generic textual analyzer and controlled fallback for unsupported languages."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import json
import re
import tomllib
from pathlib import Path
from typing import Final

from .code_analyzer_common import (
    SourceMap,
    comparison_fingerprints,
    manifest_evidence,
    searchable_chunks,
)
from .code_contracts import (
    AnalysisStatus,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    CodeRouteConfig,
    DiagnosticRecord,
    DiagnosticSeverity,
    MetricRecord,
    ReferenceRecord,
    SymbolRecord,
)


# region [01] Extensible lexical definitions


_DEFINITION_PATTERNS: Final[dict[str, tuple[tuple[str, re.Pattern[str]], ...]]] = {
    "javascript": (
        ("function", re.compile(r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("class", re.compile(r"(?m)^\s*(?:export\s+)?class\s+(\w+)")),
        ("function", re.compile(r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^\n)]*\)\s*=>")),
    ),
    "typescript": (
        ("function", re.compile(r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("class", re.compile(r"(?m)^\s*(?:export\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"(?m)^\s*(?:export\s+)?interface\s+(\w+)")),
        ("type", re.compile(r"(?m)^\s*(?:export\s+)?type\s+(\w+)\s*=")),
    ),
    "go": (
        ("function", re.compile(r"(?m)^\s*func\s+(?:\([^\n)]*\)\s*)?(\w+)\s*\(")),
        ("type", re.compile(r"(?m)^\s*type\s+(\w+)\s+")),
    ),
    "java": (
        ("class", re.compile(r"(?m)^\s*(?:public\s+)?(?:abstract\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"(?m)^\s*(?:public\s+)?interface\s+(\w+)")),
        ("enum", re.compile(r"(?m)^\s*(?:public\s+)?enum\s+(\w+)")),
    ),
    "kotlin": (
        ("class", re.compile(r"(?m)^\s*(?:data\s+)?class\s+(\w+)")),
        ("function", re.compile(r"(?m)^\s*(?:suspend\s+)?fun\s+(\w+)\s*\(")),
    ),
    "c": (
        ("function", re.compile(r"(?m)^\s*[\w* ]+\s+(\w+)\s*\([^;\n]*\)\s*\{")),
        ("struct", re.compile(r"(?m)^\s*(?:typedef\s+)?struct\s+(\w+)")),
    ),
    "cpp": (
        ("function", re.compile(r"(?m)^\s*[\w:*&<> ]+\s+(\w+)\s*\([^;\n]*\)\s*\{")),
        ("class", re.compile(r"(?m)^\s*(?:class|struct)\s+(\w+)")),
    ),
    "csharp": (
        ("class", re.compile(r"(?m)^\s*(?:public\s+)?(?:sealed\s+|abstract\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"(?m)^\s*(?:public\s+)?interface\s+(\w+)")),
    ),
    "ruby": (
        ("class", re.compile(r"(?m)^\s*class\s+(\w+)")),
        ("module", re.compile(r"(?m)^\s*module\s+(\w+)")),
        ("function", re.compile(r"(?m)^\s*def\s+(?:self\.)?(\w+[!?=]?)")),
    ),
    "php": (
        ("class", re.compile(r"(?m)^\s*(?:final\s+|abstract\s+)?class\s+(\w+)")),
        ("function", re.compile(r"(?m)^\s*(?:public\s+|private\s+|protected\s+)?function\s+(\w+)")),
    ),
    "powershell": (
        ("function", re.compile(r"(?im)^\s*function\s+([\w-]+)")),
        ("class", re.compile(r"(?im)^\s*class\s+(\w+)")),
    ),
    "shell": (
        ("function", re.compile(r"(?m)^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{")),
    ),
    "sql": (
        ("table", re.compile(r"(?im)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\[\"`]?([\w.]+)")),
        ("view", re.compile(r"(?im)\bCREATE\s+VIEW\s+[\[\"`]?([\w.]+)")),
        ("function", re.compile(r"(?im)\bCREATE\s+(?:FUNCTION|PROCEDURE)\s+[\[\"`]?([\w.]+)")),
    ),
}

_IMPORT_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "javascript": re.compile(r"(?m)(?:from\s+|require\s*\(\s*)['\"]([^'\"]+)"),
    "typescript": re.compile(r"(?m)(?:from\s+|require\s*\(\s*)['\"]([^'\"]+)"),
    "go": re.compile(r"(?m)^\s*import\s+(?:\w+\s+)?\"([^\"]+)\""),
    "java": re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.]+)"),
    "kotlin": re.compile(r"(?m)^\s*import\s+([\w.]+)"),
    "c": re.compile(r"(?m)^\s*#\s*include\s*[<\"]([^>\"]+)"),
    "cpp": re.compile(r"(?m)^\s*#\s*include\s*[<\"]([^>\"]+)"),
    "csharp": re.compile(r"(?m)^\s*using\s+([\w.]+)"),
    "ruby": re.compile(r"(?m)^\s*require(?:_relative)?\s+['\"]([^'\"]+)"),
    "php": re.compile(r"(?m)^\s*(?:use|require|include)\s+['\"]?([^;'\"]+)"),
}

_FENCED_CODE = re.compile(r"(?ms)^```(?P<language>[\w+-]*)[^\n]*\n(?P<body>.*?)^```\s*$")


# endregion [01]


# region [02] Syntax probes and lexical structure


def _syntax_diagnostics(source: CodeFileInput) -> tuple[DiagnosticRecord, ...]:
    language = source.classification.language
    try:
        if language in {"json", "jsonl"}:
            if language == "jsonl":
                for index, json_line in enumerate(source.text.splitlines(), start=1):
                    if json_line.strip():
                        json.loads(json_line)
            else:
                json.loads(source.text)
        elif language == "toml":
            tomllib.loads(source.text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        line_number = max(1, int(getattr(exc, "lineno", 1) or 1))
        column = max(0, int(getattr(exc, "colno", 1) or 1) - 1)
        source_map = SourceMap.build(source.text)
        return (
            DiagnosticRecord(
                source="stdlib-parser",
                code=f"{language}_parse_error",
                severity=DiagnosticSeverity.ERROR,
                message=str(exc)[:4096],
                source_range=source_map.source_range(
                    line_number, column, line_number, column + 1
                ),
                tool_name="json" if language in {"json", "jsonl"} else "tomllib",
                tool_version="stdlib",
                confirmed=True,
            ),
        )
    return ()


def _lexical_symbols(source: CodeFileInput, source_map: SourceMap) -> tuple[SymbolRecord, ...]:
    language = source.classification.language or ""
    module = Path(source.snapshot.path).stem
    symbols: list[SymbolRecord] = []
    for kind, pattern in _DEFINITION_PATTERNS.get(language, ()):
        for match in pattern.finditer(source.text):
            name = match.group(1)
            symbols.append(
                SymbolRecord(
                    kind=kind,
                    name=name,
                    qualified_name=f"{module}.{name}",
                    signature=match.group(0).strip()[:4096],
                    source_range=source_map.offset_range(match.start(), match.end()),
                    visibility="private" if name.startswith("_") else "public",
                    confirmed=False,
                    metadata={"evidence": f"generic-lexical:{language}"},
                )
            )
    if source.classification.artifact_kind is ArtifactKind.DOCUMENTATION:
        for index, match in enumerate(_FENCED_CODE.finditer(source.text)):
            language_hint = match.group("language") or "unknown"
            symbols.append(
                SymbolRecord(
                    kind="code_block",
                    name=f"code-block-{index + 1}",
                    qualified_name=f"{module}.code-block-{index + 1}",
                    signature=f"```{language_hint}",
                    source_range=source_map.offset_range(match.start(), match.end()),
                    visibility=None,
                    confirmed=True,
                    metadata={"language_hint": language_hint, "embedded": True},
                )
            )
    return tuple(symbols)


def _lexical_references(
    source: CodeFileInput, source_map: SourceMap
) -> tuple[ReferenceRecord, ...]:
    language = source.classification.language or ""
    pattern = _IMPORT_PATTERNS.get(language)
    if pattern is None:
        return ()
    module = Path(source.snapshot.path).stem
    return tuple(
        ReferenceRecord(
            kind="import",
            name=match.group(1).strip(),
            source_range=source_map.offset_range(match.start(), match.end()),
            source_qualified_name=module,
            target_hint=match.group(1).strip(),
            confirmed=False,
            confidence=0.8,
            evidence=f"generic-lexical:{language}:import",
        )
        for match in pattern.finditer(source.text)
    )


# endregion [02]


# region [03] Public fallback analyzer


class GenericAnalyzer:
    """Keep every decodable candidate searchable when no native parser exists."""

    analyzer_id = "neocortex-generic-text"
    analyzer_version = "1"
    languages = frozenset({"*"})

    def analyze(self, source: CodeFileInput, config: CodeRouteConfig) -> CodeAnalysis:
        source_map = SourceMap.build(source.text)
        symbols = _lexical_symbols(source, source_map)
        references = _lexical_references(source, source_map)
        hints, dependencies, manifest_diagnostics = manifest_evidence(
            Path(source.snapshot.path), source.text
        )
        syntax_diagnostics = _syntax_diagnostics(source)
        diagnostics = (*manifest_diagnostics, *syntax_diagnostics)
        language = source.classification.language
        native_syntax = language in {"json", "jsonl", "toml"} and not syntax_diagnostics
        source_like = source.classification.artifact_kind in {
            ArtifactKind.SOURCE,
            ArtifactKind.SCRIPT,
            ArtifactKind.GENERATED,
            ArtifactKind.VENDORED,
            ArtifactKind.EXAMPLE,
            ArtifactKind.FIXTURE,
        }
        status = AnalysisStatus.COMPLETE if native_syntax else AnalysisStatus.TEXT_ONLY
        if source_like:
            status = AnalysisStatus.PARTIAL
        if diagnostics:
            status = AnalysisStatus.PARTIAL
        structure = "\n".join(
            f"{item.kind}\0{item.qualified_name}\0{item.signature or ''}"
            for item in symbols
        )
        fingerprints = comparison_fingerprints(
            source.raw_bytes,
            source.text,
            language,
            structure=structure or None,
        )
        parser_kind = (
            f"stdlib-{language}" if native_syntax else "generic-lexical-fallback"
        )
        return CodeAnalysis(
            input=source,
            status=status,
            analyzer_id=self.analyzer_id,
            analyzer_version=self.analyzer_version,
            parser_kind=parser_kind,
            symbols=symbols,
            references=references,
            dependencies=dependencies,
            diagnostics=tuple(diagnostics),
            metrics=(
                MetricRecord("line_count", len(source_map.lines), provenance=parser_kind),
                MetricRecord("symbol_count", len(symbols), confirmed=False, provenance=parser_kind),
                MetricRecord("reference_count", len(references), confirmed=False, provenance=parser_kind),
            ),
            chunks=searchable_chunks(source.text, config.chunk_chars),
            project_hints=hints,
            provenance={
                "parser": parser_kind if native_syntax else None,
                "lexical_fallback": not native_syntax,
                "syntax_confirmed": native_syntax,
            },
            **fingerprints,
        )


# endregion [03]


__all__ = ["GenericAnalyzer"]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_generic")
