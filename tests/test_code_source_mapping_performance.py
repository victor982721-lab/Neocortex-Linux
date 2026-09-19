"""Source coordinates and chunk owners retain evidence with bounded work."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tomllib
from typing import cast

import pytest

from neocortex.code.code_contracts import (
    CodeChunk,
    CodeFileInput,
    CodeRouteConfig,
    SourceRange,
    SymbolRecord,
)
from neocortex.code.ingestion.code_analyzer_common import SourceMap, searchable_chunks
from neocortex.code.ingestion.code_detection import classify_artifact
from neocortex.code.ingestion.code_generic import GenericAnalyzer
from neocortex.code.ingestion.code_python import PythonAnalyzer, _annotated_chunks
from neocortex.code.ingestion.code_rust import RustAnalyzer
from neocortex.deduplication import FileSnapshot


class _MeasuredText(str):
    encoded_chars = 0

    def encode(self, encoding="utf-8", errors="strict"):
        type(self).encoded_chars += len(self)
        return super().encode(encoding, errors)

    def splitlines(self, keepends=False):
        return [type(self)(line) for line in super().splitlines(keepends)]

    def __getitem__(self, index):
        return type(self)(super().__getitem__(index))


@pytest.mark.parametrize("text", ("", "a\r\nβ界🙂\n", "a" * 9000, "aβ界🙂" * 3000))
def test_source_map_preserves_clamped_utf8_and_character_coordinates(text: str) -> None:
    source_map = SourceMap.build(text)
    lines = tuple(text.splitlines(keepends=True)) or ("",)
    for line_number in (-2, *range(1, len(lines) + 1), len(lines) + 10):
        line_index = min(max(line_number - 1, 0), len(lines) - 1)
        line = lines[line_index]
        prior = sum(len(value.encode("utf-8")) for value in lines[:line_index])
        for column in (-1, 0, 1, 255, 256, 257, 999, len(line), len(line) * 4 + 1):
            assert source_map.byte_offset(line_number, column, utf8_column=False) == (
                prior + len(line[: min(max(column, 0), len(line))].encode("utf-8"))
            )
            assert source_map.byte_offset(line_number, column, utf8_column=True) == (
                prior + min(max(column, 0), len(line.encode("utf-8")))
            )


@pytest.mark.parametrize("utf8_column", (False, True))
def test_long_unicode_line_does_not_reencode_every_prefix(utf8_column: bool) -> None:
    text = _MeasuredText("aβ界🙂" * 20_000)
    source_map = SourceMap.build(text)
    _MeasuredText.encoded_chars = 0
    for column in range(0, len(text), 79):
        source_map.byte_offset(1, column, utf8_column=utf8_column)
    # Byte columns require no encoding.  Character columns may encode at most
    # one 256-character checkpoint block per query, independent of line size.
    expected_bound = 0 if utf8_column else 256 * (len(text) // 79 + 1)
    assert _MeasuredText.encoded_chars <= expected_bound


def test_unicode_chunks_keep_exact_byte_slices_and_reject_wrong_map() -> None:
    text = "aβ界🙂" * 1800 + "\r\nsecond\n"
    source_map = SourceMap.build(text)
    chunks = searchable_chunks(text, 513, source_map=source_map)
    raw = text.encode("utf-8")
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= 513 for chunk in chunks)
    for chunk in chunks:
        assert raw[chunk.source_range.start_byte : chunk.source_range.end_byte].decode() == chunk.text
    with pytest.raises(ValueError, match="does not match"):
        searchable_chunks("different", 513, source_map=source_map)


@pytest.mark.parametrize(
    "name,text,analyzer",
    (
        ("source.py", "def value():\n    return 'β界🙂'\n", PythonAnalyzer()),
        ("broken.py", "def value(\n", PythonAnalyzer()),
        ("source.rs", "fn value() { helper(); }\n", RustAnalyzer()),
        ("source.js", "function value() { return 1; }\n", GenericAnalyzer()),
        ("broken.json", '{"invalid": ', GenericAnalyzer()),
    ),
)
def test_analyzer_shares_source_map_with_chunks_and_error_locations(
    tmp_path: Path, monkeypatch, name: str, text: str, analyzer,
) -> None:
    raw = text.encode()
    source = CodeFileInput(
        FileSnapshot(str(tmp_path / name), 1, 2, len(raw), 3, 4), text, raw, "utf-8",
        classify_artifact(str(tmp_path / name), text), "mapping-fixture",
    )
    config = CodeRouteConfig(tmp_path / "code.sqlite3", tmp_path / "dedup.sqlite3")
    original = SourceMap.build
    builds = []

    def build(cls, payload):
        del cls
        builds.append(payload)
        return original(payload)

    monkeypatch.setattr(SourceMap, "build", classmethod(build))
    result = analyzer.analyze(source, config)
    assert "".join(chunk.text for chunk in result.chunks) == text
    assert builds == [text]


@pytest.mark.parametrize("name,text,parser", (
    ("package.json", '{"name":"fixture","dependencies":{"example":"1"}}', json),
    ("composer.json", '{"name":"fixture","require":{"example":"1"}}', json),
    ("pyproject.toml", "[project]\nname='fixture'\ndependencies=['example>=1']\n", tomllib),
    ("Cargo.toml", "[package]\nname='fixture'\n[dependencies]\nexample='1'\n", tomllib),
    ("Pipfile", "[packages]\nexample='*'\n", tomllib),
))
def test_valid_manifest_has_one_syntax_parse(tmp_path: Path, monkeypatch, name, text, parser) -> None:
    raw = text.encode()
    source = CodeFileInput(
        FileSnapshot(str(tmp_path / name), 1, 2, len(raw), 3, 4), text, raw, "utf-8",
        classify_artifact(str(tmp_path / name), text), "manifest-parse-fixture",
    )
    config = CodeRouteConfig(tmp_path / "code.sqlite3", tmp_path / "dedup.sqlite3")
    original = parser.loads
    parses = []

    def loads(value, *args, **kwargs):
        parses.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(parser, "loads", loads)
    result = GenericAnalyzer().analyze(source, config)
    assert result.provenance["syntax_confirmed"]
    assert result.diagnostics == ()
    if name != "Pipfile":
        assert result.project_hints[0].name == "fixture"
        assert result.dependencies[0].name == "example"
    assert parses == [text]


@pytest.mark.parametrize("name,text,syntax_code", (
    ("package.json", '{"invalid": ', "json_parse_error"),
    ("pyproject.toml", "[project", "toml_parse_error"),
))
def test_invalid_manifest_keeps_both_diagnostics_and_syntax_position(
    tmp_path: Path, name: str, text: str, syntax_code: str,
) -> None:
    raw = text.encode()
    source = CodeFileInput(
        FileSnapshot(str(tmp_path / name), 1, 2, len(raw), 3, 4), text, raw, "utf-8",
        classify_artifact(str(tmp_path / name), text), "manifest-parse-fixture",
    )
    result = GenericAnalyzer().analyze(
        source, CodeRouteConfig(tmp_path / "code.sqlite3", tmp_path / "dedup.sqlite3"),
    )
    assert not result.provenance["syntax_confirmed"]
    assert [item.code for item in result.diagnostics] == ["manifest_parse_error", syntax_code]
    assert result.diagnostics[1].source_range is not None


def _symbol(name: str, start: int, end: int) -> SymbolRecord:
    return SymbolRecord("function", name, name, None, SourceRange(start, 0, end, 1, start, end + 1))


def test_chunk_owner_sweep_preserves_nested_spans_ties_and_input_order() -> None:
    symbols = (
        _symbol("module", 1, 100), _symbol("first-tie", 10, 20),
        _symbol("nested", 12, 13), _symbol("second-tie", 10, 20),
        _symbol("next", 21, 24), _symbol("same-line", 20, 20),
    )
    chunks = tuple(
        CodeChunk(index, "fixture", SourceRange(line, 0, line, 1, line, line + 1))
        for index, line in enumerate((22, 12, 101, 10, 20, 1, 24, 13, 14))
    )
    expected = []
    for chunk in chunks:
        owner = min(
            (symbol for symbol in symbols if symbol.source_range.start_line <= chunk.source_range.start_line <= symbol.source_range.end_line),
            key=lambda symbol: symbol.source_range.end_line - symbol.source_range.start_line,
            default=None,
        )
        expected.append(replace(chunk, kind="python_source", symbol_qualified_name=None if owner is None else owner.qualified_name))
    assert _annotated_chunks(chunks, symbols) == tuple(expected)


def test_chunk_annotation_visits_each_symbol_a_bounded_number_of_times() -> None:
    probes = 0

    class MeasuredSymbol:
        def __init__(self, index):
            self.qualified_name = f"function_{index}"
            self.span = SourceRange(index + 1, 0, index + 1, 1, index, index + 1)

        @property
        def source_range(self):
            nonlocal probes
            probes += 1
            return self.span

    symbols = tuple(cast(SymbolRecord, MeasuredSymbol(index)) for index in range(2_000))
    chunks = tuple(
        CodeChunk(index, "fixture", SourceRange(index + 1, 0, index + 1, 1, index, index + 1))
        for index in range(2_000)
    )
    annotated = _annotated_chunks(chunks, symbols)
    assert all(chunk.symbol_qualified_name == f"function_{index}" for index, chunk in enumerate(annotated))
    assert probes < 10 * (len(symbols) + len(chunks))
