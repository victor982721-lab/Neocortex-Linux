"""Behavioral contracts for the bounded Rust lexical analyzer."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from _04_Nucleo_Operativo.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    CodeRouteConfig,
)
from _04_Nucleo_Operativo.code_rust import RustAnalyzer


REPRESENTATIVE_RUST = """use serde::Serialize;
use std::fmt;
mod internal;

pub struct Engine { value: i32 }

impl Serialize for Engine {
    pub async fn run(&self, input: i32) -> i32 {
        if input > 0 && ready() { helper(input) } else { fallback!() }
    }
}

macro_rules! fallback { () => { 0 } }

fn helper(value: i32) -> i32 {
    match value { 0 => 0, _ => value }
}
"""


def _source(tmp_path: Path, text: str, *, name: str = "engine.rs") -> CodeFileInput:
    raw = text.encode("utf-8")
    return CodeFileInput(
        snapshot=FileSnapshot(str(tmp_path / name), 1, 2, len(raw), 3, 4),
        text=text,
        raw_bytes=raw,
        encoding="utf-8",
        classification=ArtifactClassification(
            language="rust",
            artifact_kind=ArtifactKind.SOURCE,
            confidence=1.0,
            evidence=("extension:.rs",),
        ),
        processing_signature="rust-characterization-fixture-v1",
    )


def _config(tmp_path: Path, *, complexity_warning: int = 3) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        chunk_chars=1024,
        complexity_warning=complexity_warning,
    )


def _analyze(
    tmp_path: Path,
    text: str,
    *,
    name: str = "engine.rs",
    complexity_warning: int = 3,
) -> CodeAnalysis:
    return RustAnalyzer().analyze(
        _source(tmp_path, text, name=name),
        _config(tmp_path, complexity_warning=complexity_warning),
    )


def test_rust_analyzer_signature_and_empty_source_contract(tmp_path: Path) -> None:
    assert str(inspect.signature(RustAnalyzer.analyze)) == (
        "(self, source: 'CodeFileInput', config: 'CodeRouteConfig') -> 'CodeAnalysis'"
    )

    result = _analyze(tmp_path, "", name="empty.rs")

    assert result.status is AnalysisStatus.TEXT_ONLY
    assert result.analyzer_id == "neocortex-rust-lexical"
    assert result.analyzer_version == "1"
    assert result.parser_kind == "rust-lexical-fallback"
    assert result.symbols == ()
    assert result.references == ()
    assert result.dependencies == ()
    assert result.diagnostics == ()
    assert result.chunks == ()
    assert [
        (
            metric.name,
            metric.value,
            metric.symbol_qualified_name,
            metric.confirmed,
            metric.provenance,
        )
        for metric in result.metrics
    ] == [
        ("line_count", 1, None, True, "rust-lexical"),
        ("symbol_count", 0, None, True, "rust-lexical"),
        ("reference_count", 0, None, True, "rust-lexical"),
    ]
    assert result.provenance == {
        "parser": None,
        "lexical_analyzer": "1",
        "syntax_confirmed": False,
        "cargo_tools_executed": False,
    }
    assert result.normalized_xxh3_128 is None
    assert result.token_xxh3_128 is None
    assert result.structure_xxh3_128 == result.text_xxh3_128


def test_rust_analyzer_preserves_ordered_structural_evidence(tmp_path: Path) -> None:
    result = _analyze(tmp_path, REPRESENTATIVE_RUST)

    assert result.status is AnalysisStatus.PARTIAL
    assert [
        (
            symbol.kind,
            symbol.name,
            symbol.qualified_name,
            symbol.parent_qualified_name,
            symbol.visibility,
            symbol.confirmed,
            symbol.complexity,
            symbol.source_range.start_line,
            symbol.source_range.end_line,
        )
        for symbol in result.symbols
    ] == [
        (
            "impl",
            "impl Serialize for Engine",
            "engine.impl Serialize for Engine",
            None,
            None,
            False,
            None,
            7,
            11,
        ),
        ("mod", "internal", "engine.internal", None, "private", False, None, 3, 5),
        ("struct", "Engine", "engine.Engine", None, "public", False, None, 5, 5),
        (
            "method",
            "run",
            "engine.impl Engine.run",
            "engine.impl Engine",
            "public",
            False,
            3,
            8,
            10,
        ),
        ("fn", "helper", "engine.helper", None, "private", False, 2, 15, 17),
        ("macro", "fallback", "engine.fallback", None, "private", False, None, 13, 13),
    ]
    assert [
        (
            reference.kind,
            reference.name,
            reference.source_qualified_name,
            reference.target_hint,
            reference.confirmed,
            reference.confidence,
            reference.evidence,
        )
        for reference in result.references
    ] == [
        (
            "implements_trait",
            "Serialize",
            "engine.impl Serialize for Engine",
            "Serialize",
            False,
            0.8,
            "rust-lexical:impl-header",
        ),
        (
            "import",
            "serde::Serialize",
            "engine",
            "serde::Serialize",
            False,
            0.9,
            "rust-lexical:use",
        ),
        ("import", "std::fmt", "engine", "std::fmt", False, 0.9, "rust-lexical:use"),
        (
            "module_declaration",
            "internal",
            "engine",
            "internal",
            False,
            0.9,
            "rust-lexical:mod",
        ),
        ("call", "run", "engine", "run", False, 0.6, "rust-lexical:call-shape"),
        ("call", "ready", "engine", "ready", False, 0.6, "rust-lexical:call-shape"),
        ("call", "helper", "engine", "helper", False, 0.6, "rust-lexical:call-shape"),
        (
            "macro_call",
            "fallback",
            "engine",
            "fallback",
            False,
            0.6,
            "rust-lexical:call-shape",
        ),
        ("call", "helper", "engine", "helper", False, 0.6, "rust-lexical:call-shape"),
    ]
    assert [
        (
            dependency.name,
            dependency.kind,
            dependency.confirmed,
            dependency.confidence,
            dependency.evidence,
        )
        for dependency in result.dependencies
    ] == [("serde", "rust_use", False, 0.8, "rust-lexical:use-root")]
    assert [
        (diagnostic.code, diagnostic.message, diagnostic.confirmed, diagnostic.confidence)
        for diagnostic in result.diagnostics
    ] == [
        (
            "high_complexity_inferred",
            "engine.impl Engine.run has lexical complexity 3",
            False,
            0.65,
        )
    ]
    assert [
        (metric.name, metric.value, metric.symbol_qualified_name, metric.confirmed)
        for metric in result.metrics
    ] == [
        ("lexical_complexity", 3, "engine.impl Engine.run", False),
        ("lexical_complexity", 2, "engine.helper", False),
        ("line_count", 17, None, True),
        ("symbol_count", 6, None, True),
        ("reference_count", 9, None, True),
    ]
    assert tuple(chunk.text for chunk in result.chunks) == (REPRESENTATIVE_RUST,)


@pytest.mark.parametrize(
    ("text", "expected_code", "expected_message", "expected_line"),
    (
        (
            "fn broken() {\n    call(]\n",
            "rust_unbalanced_delimiter",
            "unmatched closing delimiter ']'",
            2,
        ),
        (
            "fn broken() {\n    call();\n",
            "rust_unclosed_delimiter",
            "unclosed delimiter '{'",
            1,
        ),
    ),
)
def test_rust_analyzer_preserves_first_delimiter_diagnostic(
    tmp_path: Path,
    text: str,
    expected_code: str,
    expected_message: str,
    expected_line: int,
) -> None:
    result = _analyze(tmp_path, text, name="broken.rs", complexity_warning=10)

    assert result.status is AnalysisStatus.PARTIAL
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.code == expected_code
    assert diagnostic.message == expected_message
    assert diagnostic.confirmed
    assert diagnostic.metadata == {"scope": "lexical-only"}
    assert diagnostic.source_range is not None
    assert diagnostic.source_range.start_line == expected_line


def test_rust_analyzer_is_deterministic_and_ignores_delimiters_in_quotes(
    tmp_path: Path,
) -> None:
    text = 'fn quoted() { let braces = "([{}])"; quoted_call(); }\n'
    source = _source(tmp_path, text, name="quoted.rs")
    config = _config(tmp_path, complexity_warning=10)
    analyzer = RustAnalyzer()

    first = analyzer.analyze(source, config)
    second = analyzer.analyze(source, config)

    assert first == second
    assert first.status is AnalysisStatus.TEXT_ONLY
    assert first.diagnostics == ()
    assert first.text_xxh3_128 == first.raw_xxh3_128
    assert first.structure_xxh3_128 is not None
